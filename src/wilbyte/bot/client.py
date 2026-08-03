"""The Discord bot: slash commands and @mentions over the RYTE pipeline.

    /run     playlist:<url> limit:3      build each post, approve it, schedule it
    /plan    playlist:<url>              what would be posted, and when
    /status                              ledger + next open slots
    /cover   kicker:.. headline:..       render a cover image on its own

    @RYTE <playlist link> 3          the same thing, in plain language

Runs are serialized behind a lock: slot assignment reads the blog's occupied
days from GHL, so two concurrent runs would hand out the same day twice.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path

import discord
from discord import app_commands

from .. import corpus
from .. import cover as cover_mod
from .. import formats, ghl, writer, youtube
from ..config import Config, ConfigError, load_config
from ..copywriter import CopywriterError
from ..corpus import Corpus
from ..models import CoverPlan
from ..pipeline import DEFAULT_OUTPUT_DIR, PipelineError
from ..scheduler import SchedulerError, next_open_slots
from ..state import Ledger
from ..writer import WriterError
from ..youtube import IngestError
from . import embeds, jobs, mentions
from .responders import InteractionResponder, MessageResponder, Responder
from .views import ApprovalView, Decision

log = logging.getLogger("wilbyte.bot")

# Errors that mean "this post failed" rather than "the bot is broken".
PIPELINE_ERRORS = (
    IngestError, CopywriterError, cover_mod.CoverError, ghl.GHLError,
    PipelineError, SchedulerError, ConfigError, WriterError, corpus.CorpusError,
)

# Ingestion guards: one mention shouldn't be able to upload the world.
MAX_LEARN_FILES = 10
MAX_LEARN_BYTES = 8_000_000


def _intents() -> discord.Intents:
    """Mentions deliver message content without the privileged intent.

    Discord sends the real `content` for DMs, for the bot's own messages, and
    for messages that @mention the bot - which covers everything this bot reads.
    Set DISCORD_MESSAGE_CONTENT=true only if you have enabled the privileged
    intent in the developer portal and want it requested.
    """
    intents = discord.Intents.default()
    if os.getenv("DISCORD_MESSAGE_CONTENT", "").strip().lower() in ("1", "true", "yes"):
        intents.message_content = True
    return intents


def parse_guild_id(raw: str | None) -> int | None:
    """Read DISCORD_GUILD_ID, returning None if it isn't a Discord snowflake.

    A misconfigured *optional* variable must never take the bot down, and the
    common mistake here is pasting the bot's invite URL - which does contain a
    long number, but it's the application id, not the server's. Extracting
    digits from it would silently sync commands to nowhere, so reject anything
    that isn't a bare id and let the caller fall back to a global sync.
    """
    if not raw:
        return None
    text = raw.strip().strip("\"'")
    if text.isdigit() and 15 <= len(text) <= 21:
        return int(text)
    return None


def _clip(text: str, limit: int = 60) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "…"


class WilByteBot(discord.Client):
    def __init__(self, config: Config):
        super().__init__(intents=_intents())
        self.config = config
        self.tree = app_commands.CommandTree(self)
        self.run_lock = asyncio.Lock()

    async def setup_hook(self) -> None:
        register_commands(self)
        raw_guild_id = self.config.secrets.discord_guild_id
        guild_id = parse_guild_id(raw_guild_id)

        if guild_id:
            # Guild-scoped commands appear immediately; global ones take ~1 hour.
            guild = discord.Object(id=guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Slash commands synced to guild %s", guild_id)
            return

        if raw_guild_id:
            log.error(
                "DISCORD_GUILD_ID is not a server id: %r. It should be ~18 digits, "
                "copied from Discord with right-click your server -> Copy Server ID "
                "(User Settings -> Advanced -> Developer Mode must be on). The bot "
                "invite URL is a different thing - that goes in a browser, not here.",
                _clip(raw_guild_id),
            )
            log.warning("Falling back to a global command sync for now.")

        await self.tree.sync()
        log.info("Slash commands synced globally (may take up to an hour to appear)")

    async def on_ready(self) -> None:
        log.info("Connected as %s", self.user)
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching, name="YouTube so you don't have to"
            )
        )

    async def on_message(self, message: discord.Message) -> None:
        if not is_direct_mention(message, self.user):
            return
        await handle_mention(self, message)


# ---------------------------------------------------------------- when to speak


def is_direct_mention(message, bot_user) -> bool:
    """True only when a human typed `@RYTE` in the message itself.

    Deliberately strict - the bot stays silent in a busy channel unless somebody
    actually asked it something. Each of these is a way to end up in
    `message.mentions` without having been addressed:

      - @everyone / @here sweeping the whole server
      - a role ping, when the bot happens to hold that role
      - replying to one of the bot's own messages with the ping toggle left on,
        which adds the author to `mentions` while the text contains no tag
      - another bot (Zapier and friends) posting something that tags it

    So the test is the literal mention token in the text, typed by a human.
    """
    if bot_user is None or message.author.bot:
        return False
    if message.mention_everyone:
        return False
    if bot_user not in message.mentions:
        return False
    return bool(re.search(rf"<@!?{bot_user.id}>", message.content or ""))


# ------------------------------------------------------------------ permissions


def is_allowed(*, channel_id: int | None, user, config: Config) -> tuple[bool, str]:
    """Channel and role gating. An empty allowlist means 'no restriction'."""
    channels = config.secrets.discord_channel_ids
    if channels and str(channel_id) not in channels:
        return False, "RYTE isn't enabled in this channel."

    roles = config.secrets.discord_role_ids
    if roles:
        member_roles = {str(r.id) for r in getattr(user, "roles", [])}
        if not member_roles & set(roles):
            return False, "You don't have the role required to run this."
    return True, ""


async def guard(interaction: discord.Interaction, config: Config) -> bool:
    allowed, reason = is_allowed(
        channel_id=interaction.channel_id, user=interaction.user, config=config
    )
    if not allowed:
        await interaction.response.send_message(reason, ephemeral=True)
    return allowed


# --------------------------------------------------------------------- mentions


async def handle_mention(bot: WilByteBot, message: discord.Message) -> None:
    config = bot.config
    allowed, reason = is_allowed(
        channel_id=message.channel.id, user=message.author, config=config
    )
    if not allowed:
        await message.reply(reason, mention_author=False)
        return

    request = mentions.parse(message.content, max_batch=config.discord.max_batch)
    responder = MessageResponder(message)

    if request.action == "help":
        await responder.send(mentions.HELP_TEXT)
        return

    async with message.channel.typing():
        try:
            if request.action == "status":
                await _send_status(responder, config)
                return

            if request.action == "corpus":
                await _send_corpus(responder)
                return

            if request.action == "check":
                await _send_check(responder, config, request.source)
                return

            if request.action == "learn":
                await _handle_learn(responder, message, request.format_key)
                return

            if request.action == "write":
                await _send_write(
                    responder, config,
                    format_key=request.format_key,
                    brief=request.brief or "",
                    token=message.id,
                )
                return

            if request.action == "cover":
                if not request.headline:
                    await responder.send(
                        "Give me two lines and I'll render it — "
                        "`@RYTE cover Aged, Fresh, Premium | Why Agents Stall`"
                    )
                    return
                await _send_cover(
                    responder, config,
                    kicker=request.kicker or "AGENT LEAD LAB",
                    headline=request.headline,
                    token=message.id,
                )
                return

            if request.action == "plan":
                await _send_plan(responder, config, request.source, request.limit)
                return
        except PIPELINE_ERRORS as exc:
            await responder.send(embed=embeds.error(str(exc)))
            return

    # action == "run"
    if bot.run_lock.locked():
        await responder.send(
            "A run is already going — give it a minute so the two don't claim "
            "the same posting slots."
        )
        return

    transcript_text = await _attached_transcript(message)
    async with bot.run_lock:
        await _execute_run(
            bot, responder, request.source, request.limit, request.mode, request.force,
            transcript_text=transcript_text,
        )


# Caption and transcript files people actually paste out of YouTube.
TRANSCRIPT_SUFFIXES = (".txt", ".vtt", ".srt", ".md")


async def _attached_transcript(message) -> str | None:
    """Read a transcript attached to the mention, if there is one.

    This is the escape hatch for when YouTube refuses to serve captions to the
    server: paste the transcript out of YouTube yourself and attach it.
    """
    for attachment in getattr(message, "attachments", []) or []:
        if not attachment.filename.lower().endswith(TRANSCRIPT_SUFFIXES):
            continue
        if attachment.size and attachment.size > MAX_LEARN_BYTES:
            continue
        raw = (await attachment.read()).decode("utf-8", errors="replace")
        if attachment.filename.lower().endswith((".vtt", ".srt")):
            raw = youtube.parse_captions(raw)
        if raw.strip():
            return raw
    return None


# --------------------------------------------------------------------- commands


def register_commands(bot: WilByteBot) -> None:
    config = bot.config

    @bot.tree.command(name="plan", description="Show which videos would be posted, and on what days")
    @app_commands.describe(
        playlist="YouTube playlist or video URL",
        limit="How many videos to plan (default 5)",
    )
    async def plan(interaction: discord.Interaction, playlist: str, limit: int = 5):
        if not await guard(interaction, config):
            return
        await interaction.response.defer(thinking=True)
        responder = InteractionResponder(interaction)
        try:
            await _send_plan(responder, config, playlist, limit)
        except PIPELINE_ERRORS as exc:
            await responder.send(embed=embeds.error(str(exc)))

    @bot.tree.command(name="run", description="Write, review, and schedule blog posts from a playlist")
    @app_commands.describe(
        playlist="YouTube playlist or video URL",
        limit="How many videos to process (default 1)",
        mode="scheduled (default), draft, or preview to send nothing to GHL",
        force="Reprocess videos already in the ledger",
    )
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="scheduled", value="scheduled"),
            app_commands.Choice(name="draft", value="draft"),
            app_commands.Choice(name="preview (nothing sent to GHL)", value="preview"),
        ]
    )
    async def run(
        interaction: discord.Interaction,
        playlist: str,
        limit: int = 1,
        mode: app_commands.Choice[str] | None = None,
        force: bool = False,
    ):
        if not await guard(interaction, config):
            return

        mode_value = mode.value if mode else "scheduled"
        if bot.run_lock.locked():
            await interaction.response.send_message(
                "A run is already going. Wait for it to finish so the two don't "
                "claim the same posting slots.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)
        async with bot.run_lock:
            await _execute_run(
                bot, InteractionResponder(interaction), playlist, limit, mode_value, force
            )

    @bot.tree.command(name="status", description="What's been posted and what's next")
    async def status(interaction: discord.Interaction):
        if not await guard(interaction, config):
            return
        await interaction.response.defer(thinking=True)
        responder = InteractionResponder(interaction)
        try:
            await _send_status(responder, config)
        except PIPELINE_ERRORS as exc:
            await responder.send(embed=embeds.error(str(exc)))

    @bot.tree.command(name="write", description="Write copy in the Agent Lead Lab voice")
    @app_commands.describe(format="What kind of copy", brief="What it should be about")
    @app_commands.choices(
        format=[
            app_commands.Choice(name=f.description, value=f.key) for f in formats.FORMATS
        ]
    )
    async def write(
        interaction: discord.Interaction, format: app_commands.Choice[str], brief: str
    ):
        if not await guard(interaction, config):
            return
        await interaction.response.defer(thinking=True)
        responder = InteractionResponder(interaction)
        try:
            await _send_write(
                responder, config,
                format_key=format.value, brief=brief, token=interaction.id,
            )
        except PIPELINE_ERRORS as exc:
            await responder.send(embed=embeds.error(str(exc)))

    @bot.tree.command(name="check", description="Test the GHL and YouTube connections")
    @app_commands.describe(playlist="Optional: a link to prove YouTube access works")
    async def check(interaction: discord.Interaction, playlist: str | None = None):
        if not await guard(interaction, config):
            return
        await interaction.response.defer(thinking=True)
        responder = InteractionResponder(interaction)
        try:
            await _send_check(responder, config, playlist)
        except PIPELINE_ERRORS as exc:
            await responder.send(embed=embeds.error(str(exc)))

    @bot.tree.command(name="corpus", description="What past copy RYTE has learned")
    async def corpus_cmd(interaction: discord.Interaction):
        if not await guard(interaction, config):
            return
        await interaction.response.defer(thinking=True)
        responder = InteractionResponder(interaction)
        try:
            await _send_corpus(responder)
        except PIPELINE_ERRORS as exc:
            await responder.send(embed=embeds.error(str(exc)))

    @bot.tree.command(name="cover", description="Render a cover image from two lines of text")
    @app_commands.describe(kicker="The highlighted 3-5 word line", headline="The big line underneath")
    async def cover(interaction: discord.Interaction, kicker: str, headline: str):
        if not await guard(interaction, config):
            return
        await interaction.response.defer(thinking=True)
        responder = InteractionResponder(interaction)
        try:
            await _send_cover(
                responder, config, kicker=kicker, headline=headline, token=interaction.id
            )
        except PIPELINE_ERRORS as exc:
            await responder.send(embed=embeds.error(str(exc)))


# ------------------------------------------------------------------ shared work


async def _maybe_open_ghl(config: Config):
    """Open a GHL session if credentials exist; otherwise run against an empty calendar.

    GHL_BLOG_ID is not required here - the session resolves the blog itself, and
    falls back to the location's only blog when there is just one.
    """
    if not (config.secrets.ghl_api_token and config.secrets.ghl_location_id):
        return None
    return await asyncio.to_thread(jobs.open_ghl, config)


async def _send_plan(responder: Responder, config: Config, source: str, limit: int) -> None:
    limit = max(1, min(limit, config.discord.max_batch))
    ledger = await asyncio.to_thread(Ledger.load)
    videos, skipped = await asyncio.to_thread(
        jobs.resolve_videos, source, ledger, limit=limit, force=False
    )
    if not videos:
        await responder.send(f"Nothing pending — all {skipped} video(s) are already processed.")
        return

    context = await _maybe_open_ghl(config)
    try:
        slots = await asyncio.to_thread(jobs.plan_slots, videos, context, config)
    finally:
        if context:
            await asyncio.to_thread(context.close)

    label = "the playlist" if len(videos) > 1 else "the video"
    await responder.send(
        embed=embeds.plan_summary(list(zip(videos, slots)), skipped=skipped, source=label)
    )


async def _send_status(responder: Responder, config: Config) -> None:
    ledger = await asyncio.to_thread(Ledger.load)
    entries = sorted(ledger.entries.values(), key=lambda e: e.processed_at, reverse=True)
    recent = [f"`{e.url_slug}` — {e.title[:60]}" for e in entries[:5]]

    context = await _maybe_open_ghl(config)
    booked = None
    try:
        if context:
            days = await asyncio.to_thread(jobs.taken_days, context, config)
            booked = len(days)
            slots = next_open_slots(days, 3, config.schedule)
        else:
            slots = next_open_slots(set(), 3, config.schedule)
    finally:
        if context:
            await asyncio.to_thread(context.close)

    await responder.send(
        embed=embeds.status_summary(
            processed=len(ledger.entries),
            recent=recent,
            next_slots=slots,
            booked_days=booked,
        )
    )


async def _send_check(responder: Responder, config: Config, source: str | None) -> None:
    """Everything a real run needs, tested from where the bot actually runs."""
    credentials = [
        (bool(getattr(config.secrets, attr)), f"{env}{'' if getattr(config.secrets, attr) else ' is not set'}")
        for attr, env, required, _ in _PREFLIGHT
        if required or attr != "discord_guild_id"
    ]
    ghl_rows = await asyncio.to_thread(jobs.check_ghl, config)
    youtube_rows = await asyncio.to_thread(jobs.check_youtube, source)

    await responder.send(
        embed=embeds.check_report(
            credentials=credentials, ghl=ghl_rows, youtube=youtube_rows
        )
    )


async def _send_write(
    responder: Responder, config: Config, *, format_key: str, brief: str, token: int
) -> None:
    fmt = formats.BY_KEY[format_key]
    if not brief:
        await responder.send(
            f"What should the {fmt.label.lower()} be about? "
            f"Try `@RYTE {fmt.key} <the idea>`."
        )
        return

    corpus_obj = await asyncio.to_thread(Corpus)
    result = await asyncio.to_thread(writer.generate, brief, fmt, config, corpus_obj)

    # The embed truncates long fields, so always attach the full text too.
    out = DEFAULT_OUTPUT_DIR / "copy" / f"{fmt.key}-{token}.txt"
    text = writer.render_text(result)
    await asyncio.to_thread(_write_file, out, text)

    await responder.send(
        embed=embeds.copy_result(result),
        file=discord.File(out, filename=f"{fmt.key}.txt"),
    )


def _write_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


async def _send_corpus(responder: Responder) -> None:
    corpus_obj = await asyncio.to_thread(Corpus)
    pieces = await asyncio.to_thread(lambda: corpus_obj.pieces)
    recent = [
        f"`{p.label:<8}` {p.preview[:70]}"
        for p in sorted(pieces, key=lambda p: p.added_at, reverse=True)[:5]
    ]
    await responder.send(
        embed=embeds.corpus_summary(
            counts=corpus_obj.counts(),
            total=len(pieces),
            words=corpus_obj.total_words(),
            recent=recent,
        )
    )


async def _handle_learn(responder: Responder, message, label: str | None) -> None:
    """Ingest whatever files are attached to the mention."""
    attachments = list(getattr(message, "attachments", []) or [])
    if not attachments:
        await responder.send(
            "Attach the copy and say `@RYTE learn` — .txt, .md, .csv or .json. "
            "Add a word like `sms` or `email` to label the whole file, or give the "
            "CSV a `format` column. In a plain text file, separate pieces with a "
            "line of `---`."
        )
        return

    if len(attachments) > MAX_LEARN_FILES:
        await responder.send(
            f"That's {len(attachments)} files — I'll take {MAX_LEARN_FILES} at a time."
        )
        attachments = attachments[:MAX_LEARN_FILES]

    corpus_obj = await asyncio.to_thread(Corpus)
    parsed: list = []
    sources: list[str] = []
    problems: list[str] = []

    for attachment in attachments:
        if attachment.size and attachment.size > MAX_LEARN_BYTES:
            problems.append(f"{attachment.filename}: over {MAX_LEARN_BYTES // 1_000_000}MB")
            continue
        try:
            data = await attachment.read()
            pieces = await asyncio.to_thread(
                corpus.parse_upload,
                data,
                filename=attachment.filename,
                label=label,
                added_by=str(responder.requester_id),
            )
        except corpus.CorpusError as exc:
            problems.append(str(exc))
            continue
        except Exception as exc:
            problems.append(f"{attachment.filename}: {exc}")
            continue

        if not pieces:
            problems.append(f"{attachment.filename}: nothing usable in it")
            continue
        parsed.extend(pieces)
        sources.append(f"{attachment.filename} — {len(pieces)} piece(s)")

    added = await asyncio.to_thread(corpus_obj.add, parsed) if parsed else []

    if not added and not problems:
        await responder.send("I already had all of that.")
        return

    if added or sources:
        await responder.send(
            embed=embeds.learn_result(
                added=len(added),
                skipped=len(parsed) - len(added),
                counts=corpus_obj.counts(),
                sources=sources,
            )
        )
    if problems:
        await responder.send(embed=embeds.error("\n".join(problems[:10])))


async def _send_cover(
    responder: Responder, config: Config, *, kicker: str, headline: str, token: int
) -> None:
    plan_obj = CoverPlan(kicker=kicker.upper(), headline=headline.upper())
    out = DEFAULT_OUTPUT_DIR / "previews" / f"cover-{token}.png"
    await asyncio.to_thread(cover_mod.render_cover, plan_obj, config, out)
    await responder.send(file=discord.File(out, filename="cover.png"))


# ----------------------------------------------------------------------- runner


async def _execute_run(
    bot: WilByteBot,
    responder: Responder,
    source: str,
    limit: int,
    mode: str,
    force: bool,
    transcript_text: str | None = None,
) -> None:
    config = bot.config
    limit = max(1, min(limit, config.discord.max_batch))
    output_dir = DEFAULT_OUTPUT_DIR

    try:
        ledger = await asyncio.to_thread(Ledger.load)
        videos, already_done = await asyncio.to_thread(
            jobs.resolve_videos, source, ledger,
            limit=limit, force=force, offline=bool(transcript_text),
        )
    except PIPELINE_ERRORS as exc:
        await responder.send(embed=embeds.error(str(exc)))
        return

    if not videos:
        await responder.send(
            f"Nothing pending — all {already_done} video(s) are already processed. "
            "Add **force** to redo one."
        )
        return

    context = None
    if mode != "preview":
        try:
            context = await _maybe_open_ghl(config)
            if context is None:
                await responder.send(
                    embed=embeds.error(
                        "No GHL credentials configured, so nothing can be posted. "
                        "Say **preview** to build posts locally instead."
                    )
                )
                return
        except PIPELINE_ERRORS as exc:
            await responder.send(embed=embeds.error(str(exc)))
            return

    created = skipped = failed = 0
    try:
        slot_pool = await asyncio.to_thread(jobs.plan_slots, videos, context, config)
        await responder.send(
            f"On it — building {len(videos)} post(s) in **{mode}** mode."
            + (f" Skipping {already_done} already done." if already_done else "")
        )

        for index, video in enumerate(videos, start=1):
            try:
                post = await asyncio.to_thread(
                    jobs.build, video, config, output_dir,
                    transcript_text=transcript_text if len(videos) == 1 else None,
                )
            except PIPELINE_ERRORS as exc:
                failed += 1
                await responder.send(embed=embeds.error(f"{video.title}\n{exc}"))
                continue

            # Show the slot this post would take without consuming it yet, so a
            # skip leaves the day free for the next post in the batch.
            post.scheduled_at = slot_pool[0] if slot_pool else None

            decision = await _review(
                responder, post, index=index, total=len(videos), mode=mode, config=config
            )

            if decision is Decision.SKIP:
                skipped += 1
                continue
            if decision is Decision.TIMEOUT:
                skipped += 1
                await responder.send(
                    f"No answer on **{post.title}** — skipped it. Files are in "
                    f"`{output_dir / post.url_slug}` if you want them."
                )
                continue
            if decision is Decision.STOP:
                skipped += 1
                await responder.send("Stopped.")
                break

            if mode == "preview":
                created += 1
                continue

            # `mode: draft` sends the whole batch to drafts; the per-post button can
            # still force a single draft while the run is otherwise scheduling.
            to_draft = decision is Decision.DRAFT or mode == "draft"
            status = ghl.STATUS_DRAFT if to_draft else ghl.STATUS_SCHEDULED
            if to_draft:
                post.scheduled_at = None
            elif slot_pool:
                slot_pool.pop(0)

            try:
                await asyncio.to_thread(jobs.publish, post, config, context, status=status)
                await asyncio.to_thread(jobs.record, ledger, post)
                created += 1
                await responder.send(
                    f"✅ **{post.title}** → `{post.url_slug}` "
                    + (
                        f"scheduled for {post.scheduled_at:%a %b %d at %I:%M %p}"
                        if post.scheduled_at
                        else "saved as a draft"
                    )
                )
            except PIPELINE_ERRORS as exc:
                failed += 1
                if status == ghl.STATUS_SCHEDULED and post.scheduled_at:
                    slot_pool.insert(0, post.scheduled_at)  # publishing failed, free the slot
                await responder.send(
                    embed=embeds.error(f"Failed to publish {post.title}\n{exc}")
                )

    except PIPELINE_ERRORS as exc:
        await responder.send(embed=embeds.error(str(exc)))
    finally:
        if context:
            await asyncio.to_thread(context.close)

    await responder.send(
        embed=embeds.result_summary(
            created=created, skipped=skipped, failed=failed,
            mode=mode, output_dir=str(output_dir),
        )
    )


async def _review(
    responder: Responder,
    post,
    *,
    index: int,
    total: int,
    mode: str,
    config: Config,
) -> Decision:
    """Post the preview card and wait for a button, unless approval is off."""
    cover_path = Path(post.cover_image_path) if post.cover_image_path else None
    file = discord.File(cover_path, filename="cover.png") if cover_path else None
    embed = embeds.post_preview(post, index=index, total=total, mode=mode)

    if not config.discord.require_approval:
        await responder.send(embed=embed, file=file)
        return Decision.DRAFT if mode == "draft" else Decision.APPROVE

    view = ApprovalView(
        requester_id=responder.requester_id,
        timeout=config.discord.approval_timeout_seconds,
    )
    await responder.send(embed=embed, file=file, view=view)
    await view.wait()
    return view.decision


# ------------------------------------------------------------------- entrypoint


def build_bot(config: Config | None = None) -> WilByteBot:
    return WilByteBot(config or load_config())


# What each credential unlocks, printed at boot so a crash in a hosted
# environment names the missing variable instead of just exiting.
_PREFLIGHT = [
    ("discord_bot_token", "DISCORD_BOT_TOKEN", True, "connect to Discord"),
    ("anthropic_api_key", "ANTHROPIC_API_KEY", False, "write the copy"),
    ("ghl_api_token", "GHL_API_TOKEN", False, "post to GoHighLevel"),
    ("ghl_location_id", "GHL_LOCATION_ID", False, "post to GoHighLevel"),
    ("ghl_blog_id", "GHL_BLOG_ID", False, "post to GoHighLevel"),
    ("discord_guild_id", "DISCORD_GUILD_ID", False, "sync slash commands instantly"),
]


def preflight(config: Config) -> list[str]:
    """Log which credentials are present. Returns the missing required ones."""
    log.info("RYTE starting up")
    log.info("config: %s", config.path)
    log.info("state:  %s", DEFAULT_OUTPUT_DIR)

    missing_required = []
    for attr, env_name, required, purpose in _PREFLIGHT:
        present = bool(getattr(config.secrets, attr))
        if present:
            log.info("  [ok]      %-20s (%s)", env_name, purpose)
        elif required:
            log.error("  [MISSING] %-20s needed to %s", env_name, purpose)
            missing_required.append(env_name)
        else:
            log.warning("  [not set] %-20s needed to %s", env_name, purpose)
    return missing_required


def run_bot(config: Config | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    try:
        config = config or load_config()
    except ConfigError as exc:
        log.error("Could not load configuration: %s", exc)
        raise SystemExit(1)

    missing = preflight(config)
    if missing:
        log.error(
            "Cannot start without %s. Set it in your host's environment variables "
            "(on Railway: the service's Variables tab), then redeploy.",
            " and ".join(missing),
        )
        raise SystemExit(1)

    try:
        build_bot(config).run(config.secrets.discord_bot_token, log_handler=None)
    except discord.LoginFailure:
        log.error(
            "Discord rejected the bot token. Copy a fresh one from the developer "
            "portal (Bot -> Reset Token) into DISCORD_BOT_TOKEN - note it is the "
            "bot token, not the application id, client secret, or public key."
        )
        raise SystemExit(1)
    except discord.PrivilegedIntentsRequired:
        log.error(
            "Discord requires the intents this bot asked for to be enabled in the "
            "developer portal. Either turn on Message Content there, or unset "
            "DISCORD_MESSAGE_CONTENT - mentions work without it."
        )
        raise SystemExit(1)
