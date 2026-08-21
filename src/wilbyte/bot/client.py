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
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from discord import app_commands

from .. import corpus
from .. import cover as cover_mod
from .. import (
    fathom, formats, ghl, notion, prefs, publisher, version, writer, youtube, zoom,
)
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
from .responders import (
    ChannelResponder,
    InteractionResponder,
    MessageResponder,
    Responder,
)
from .views import ApprovalView, Decision, RecordingPicker

log = logging.getLogger("wilbyte.bot")

# Errors that mean "this post failed" rather than "the bot is broken".
PIPELINE_ERRORS = (
    IngestError, CopywriterError, cover_mod.CoverError, ghl.GHLError,
    PipelineError, SchedulerError, ConfigError, WriterError, corpus.CorpusError,
    notion.NotionError,
    # Both were missing, and the cost was RYTE going silent mid-reply: a Fathom
    # rate limit escaped every handler, killed the task, and left the person
    # who posted a link watching nothing happen. An error nobody sees is the
    # worst kind.
    zoom.ZoomError, fathom.FathomError,
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
    # Watching a channel means reading messages that don't mention the bot, and
    # Discord withholds their content and embeds without this. Asking for it
    # here rather than making it a second thing to remember: a watcher that
    # silently sees blank messages is worse than one that fails at login with a
    # message naming the switch to flip.
    if _id_list(os.getenv("DISCORD_WATCH_CHANNEL_IDS")) or _id_list(
        os.getenv("DISCORD_SOP_CHANNEL_IDS")
    ):
        intents.message_content = True
    return intents


def _id_list(raw: str | None) -> list[str]:
    return [part.strip() for part in (raw or "").split(",") if part.strip()]


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
        self.publisher_task: asyncio.Task | None = None
        self.updater_task: asyncio.Task | None = None
        self.recordings_task: asyncio.Task | None = None

    async def setup_hook(self) -> None:
        register_commands(self)
        raw_guild_id = self.config.secrets.discord_guild_id
        guild_id = parse_guild_id(raw_guild_id)

        if guild_id:
            # Guild-scoped commands appear immediately; global ones take ~1 hour.
            guild = discord.Object(id=guild_id)
            self.tree.copy_global_to(guild=guild)
            try:
                await self.tree.sync(guild=guild)
            except discord.HTTPException as exc:
                # An *optional* setting must never take the bot down, and this
                # one did: a server id RYTE has no access to raised out of
                # setup_hook and killed the login. Slash commands are a
                # convenience; @mentions are the way in that matters.
                log.error(
                    "Couldn't register commands in server %s: %s. Either RYTE isn't "
                    "in that server, or it was invited without the "
                    "applications.commands scope - re-invite it from the developer "
                    "portal with both `bot` and `applications.commands` ticked. "
                    "Falling back to a global sync; everything else works as normal.",
                    guild_id,
                    exc,
                )
            else:
                log.info("Slash commands synced to guild %s", guild_id)
                return

        if raw_guild_id and not guild_id:
            log.error(
                "DISCORD_GUILD_ID is not a server id: %r. It should be ~18 digits, "
                "copied from Discord with right-click your server -> Copy Server ID "
                "(User Settings -> Advanced -> Developer Mode must be on). The bot "
                "invite URL is a different thing - that goes in a browser, not here.",
                _clip(raw_guild_id),
            )
            log.warning("Falling back to a global command sync for now.")

        try:
            await self.tree.sync()
        except discord.HTTPException as exc:
            # Same reasoning: RYTE answers @mentions whether or not Discord
            # accepted a single slash command.
            log.error("Couldn't register slash commands at all: %s", exc)
            return
        log.info("Slash commands synced globally (may take up to an hour to appear)")

    async def on_ready(self) -> None:
        log.info("Connected as %s — running %s", self.user, version.code_version())
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching, name="YouTube so you don't have to"
            )
        )
        # on_ready fires again after a reconnect, so guard it - two publisher
        # loops would race each other for the same posts.
        if self.publisher_task is None or self.publisher_task.done():
            self.publisher_task = self.loop.create_task(publisher_loop(self))
        if self.updater_task is None or self.updater_task.done():
            self.updater_task = self.loop.create_task(updater_loop(self))
        # Only when asked for. Calls are reviewed before they earn a card, so
        # filing everything found would fill the gallery with the ones that
        # were looked at and turned down.
        if self.config.secrets.recordings_autofile and (
            self.recordings_task is None or self.recordings_task.done()
        ):
            self.recordings_task = self.loop.create_task(recordings_loop(self))
        # Fill the call list before anyone types into it. Discord allows an
        # autocomplete three seconds, which is not enough to ask Zoom.
        self.loop.create_task(asyncio.to_thread(jobs.call_choices, self.config))
        await _warn_if_stale(self)

    async def on_message(self, message: discord.Message) -> None:
        if self.user is not None and message.author.id == self.user.id:
            return
        if is_direct_mention(message, self.user):
            await handle_mention(self, message)
            return
        if is_watched(message, self.config):
            await handle_watched(self, message)
            return
        if is_sop_channel(message, self.config):
            await handle_sop_post(self, message)


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


# ------------------------------------------------------------- watching a channel


def is_watched(message, config: Config) -> bool:
    """True for a message in a channel RYTE was told to watch.

    Deliberately not filtered by author. The whole point is that the video
    announcement is posted by another bot, which `is_direct_mention` rejects on
    purpose - a bot must not be able to make RYTE do things by mentioning it.
    An allow-listed channel is a different kind of permission: someone chose
    that channel, and everything in it is meant to be acted on.
    """
    watched = config.secrets.discord_watch_channel_ids
    return bool(watched) and str(getattr(message, "channel", None) and message.channel.id) in watched


def is_sop_channel(message, config: Config) -> bool:
    """True for a message in a channel that feeds the SOP library.

    Unlike the watched announcement channel, this one is ours: RYTE files what
    lands here and says so, because a library nobody can see being filled is
    one nobody trusts.
    """
    channels = config.secrets.discord_sop_channel_ids
    return bool(channels) and str(
        getattr(message, "channel", None) and message.channel.id
    ) in channels


def message_files(message) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(images, audio) attached to a message, by what Discord says they are."""
    images: list[str] = []
    audio: list[str] = []
    for attachment in getattr(message, "attachments", None) or []:
        kind = str(getattr(attachment, "content_type", "") or "").casefold()
        url = str(getattr(attachment, "url", "") or "")
        if not url:
            continue
        if kind.startswith("image/"):
            images.append(url)
        elif kind.startswith("audio/") or url.endswith(".ogg"):
            audio.append(url)
    return tuple(images), tuple(audio)


async def handle_sop_post(bot: WilByteBot, message) -> None:
    """File what somebody posted in the SOP channel."""
    from .. import sops

    if getattr(message.author, "bot", False):
        return

    if sops.already_filed(getattr(message, "id", "")):
        return

    images, audio = message_files(message)
    sop = sops.find_sop(message.content or "", images=images, audio=audio)
    if sop is None:
        # Chatter. Filing it is how a library stops being worth searching.
        return

    sop.posted_by = getattr(message.author, "display_name", "") or ""
    posted = getattr(message, "created_at", None)
    sop.posted_on = posted.date() if posted is not None else None

    responder = MessageResponder(message)
    async with message.channel.typing():
        summary = ""
        try:
            summary = await asyncio.to_thread(jobs.sop_summary, bot.config, sop)
        except PIPELINE_ERRORS as exc:
            log.warning("Couldn't read the SOP %s: %s", sop.title, exc)
            sop.note = sop.note or f"No summary — {exc}"

        try:
            title, url = await asyncio.to_thread(
                jobs.file_sop, bot.config, sop, summary=summary
            )
        except PIPELINE_ERRORS as exc:
            await responder.send(embed=embeds.error(f"Couldn't file that SOP\n{exc}"))
            return

    sops.remember(getattr(message, "id", ""))
    tail = " with a summary" if summary else (f"\n⚠ {sop.note}" if sop.note else "")
    await responder.send(f"📘 **{title}** filed{tail}\n{url}")


def watched_links(message) -> tuple[str, ...]:
    """Every YouTube link in a message, text and embeds alike.

    An announcement bot posts a line of text and lets Discord unfurl the video,
    so the link is often only in the embed - and a rich embed can carry it on
    the embed url, its title link, or in the description.
    """
    parts = [getattr(message, "content", "") or ""]
    for embed in getattr(message, "embeds", []) or []:
        for value in (
            getattr(embed, "url", None),
            getattr(embed, "title", None),
            getattr(embed, "description", None),
        ):
            if isinstance(value, str):
                parts.append(value)
        author = getattr(embed, "author", None)
        if author is not None and isinstance(getattr(author, "url", None), str):
            parts.append(author.url)
    return mentions.find_sources("\n".join(parts))


async def handle_watched(bot: "WilByteBot", message: discord.Message) -> None:
    """A video was announced. Write the post for it.

    Sends the review card to the working channel rather than answering in the
    announcements feed, which is a broadcast channel and not somewhere to hold
    a conversation about a draft.
    """
    links = watched_links(message)
    if not links:
        return

    # Never fall back to the channel being watched. RYTE is a guest there: it
    # reads the feed and says nothing, and a missing setting must not turn it
    # into something that talks in front of clients.
    channel = _post_channel(bot)
    if channel is None:
        log.error(
            "A video was posted in %s but there is nowhere to send the review card. "
            "Set DISCORD_POST_CHANNEL_ID to the channel RYTE should work in.",
            message.channel.id,
        )
        return
    responder = ChannelResponder(channel)

    if bot.run_lock.locked():
        await responder.send(
            "A new video just landed but I'm mid-run — I'll need telling again "
            f"once this batch is done:\n```\n@RYTE {' '.join(links)}\n```"
        )
        return

    await responder.send(f"📺 New video posted in <#{message.channel.id}> — writing it up.")
    async with bot.run_lock:
        await _execute_run(
            bot, responder, links, len(links), "scheduled", force=False
        )


def _post_channel(bot: "WilByteBot"):
    """Where the watcher works: the configured channel, else the first allowed one."""
    configured = bot.config.secrets.discord_post_channel_id
    for raw in ([configured] if configured else []) + list(
        bot.config.secrets.discord_channel_ids
    ):
        try:
            channel = bot.get_channel(int(raw))
        except (TypeError, ValueError):
            continue
        if channel is not None:
            return channel
    return None


# ------------------------------------------------------------------ permissions


def is_allowed(*, channel_id: int | None, user, config: Config) -> tuple[bool, str]:
    """Channel and role gating. An empty allowlist means 'no restriction'."""
    channels = config.secrets.discord_channel_ids
    # A channel named as an SOP source is already an explicit permission for
    # that channel - RYTE files what lands in it. Making somebody add it to a
    # second list as well is a trap: he answers everywhere except the one room
    # the library is kept in.
    allowed_anyway = set(config.secrets.discord_sop_channel_ids)
    if channels and str(channel_id) not in set(channels) | allowed_anyway:
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

    # A watched channel is read-only, always. Not "unless the allowlist is
    # empty", not "unless someone asks nicely" - RYTE is in that server to see
    # when a video goes up and for nothing else, and this is the one rule that
    # cannot depend on a setting being filled in correctly.
    if is_watched(message, config):
        log.info("Mentioned in watched channel %s — staying quiet", message.channel.id)
        return

    allowed, reason = is_allowed(
        channel_id=message.channel.id, user=message.author, config=config
    )
    if not allowed:
        # Silent where a channel allowlist exists, because RYTE now sits in a
        # server it is only there to read - an announcements feed with clients
        # in it. "RYTE isn't enabled in this channel" is still chatter, and it
        # would be posted in front of them. Where the refusal is about the
        # person rather than the place, say so: they are somewhere RYTE speaks.
        if config.secrets.discord_channel_ids and "channel" in reason:
            log.info("Ignoring a mention in channel %s", message.channel.id)
            return
        await message.reply(reason, mention_author=False)
        return

    request = mentions.parse(message.content, max_batch=config.discord.max_batch)
    responder = MessageResponder(message)

    if request.action == "help":
        # The version goes on the help text specifically, because this is the
        # message you get when RYTE doesn't recognise a word - and "that word
        # is new, this copy is old" is the most likely reason why.
        await responder.send(f"{mentions.HELP_TEXT}\n\n-# Running `{version.code_version()}`")
        return

    async with message.channel.typing():
        try:
            if request.action == "status":
                await _send_status(responder, config)
                return

            if request.action == "schedule":
                await _send_schedule(responder, config)
                return

            if request.action == "corpus":
                await _send_corpus(responder)
                return

            if request.action == "fields":
                await _send_fields(responder, config, message.id)
                return

            if request.action == "start":
                await _set_earliest_day(responder, config, request.brief or "")
                return

            if request.action == "host":
                await _host_images(responder, config, message)
                return

            if request.action == "recording":
                await _file_recording(responder, config, message)
                return

            if request.action == "missed":
                await _send_missed(responder, config)
                return

            if request.action == "reconcile":
                await _send_reconcile(responder, config)
                return

            if request.action == "datetest":
                await _send_date_test(
                    responder, config, undo="undo" in (request.brief or "").lower()
                )
                return

            if request.action == "check":
                await _send_check(responder, config, request.source)
                return

            if request.action == "sweep":
                await responder.send("Checking Zoom and Fathom for new calls…")
                before = await asyncio.to_thread(jobs.new_recordings, config)
                if not before:
                    await responder.send("Nothing new — everything recent is already filed.")
                    return
                await _file_new_recordings(bot)
                return

            if request.action == "backfill":
                await _backfill_sops(bot, responder, message)
                return

            if request.action == "findsop":
                await _send_sops(responder, config, request.brief or "")
                return

            if request.action == "board":
                lines = await asyncio.to_thread(jobs.board_today, config)
                await responder.send("\n".join(lines) or "The board is empty.")
                return

            if request.action == "rollover":
                await responder.send("Reading the board — nothing will move.")
                report = await asyncio.to_thread(jobs.rollover_plan, config)
                await responder.send(report)
                return

            if request.action == "findcall":
                await _send_cards(responder, config, request.brief or "")
                return

            if request.action == "calls":
                await _send_visible_calls(responder, config, link=request.brief or "")
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
            bot, responder, request.sources, request.limit, request.mode, request.force,
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


async def _warn_if_stale(bot: "WilByteBot") -> None:
    """Say in Discord when the launcher couldn't update.

    Twice now a fix has looked like it didn't work because RYTE was answering
    out of an older checkout. The launcher does print it, but that window
    scrolls past in a second and nobody is watching it. This is the one place
    the answer is actually read.
    """
    from ..state import _state_dir

    marker = _state_dir() / "update-blocked"
    if not marker.exists():
        return

    try:
        reason = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return

    log.warning("%s", reason)
    channel = _announce_channel(bot)
    if channel is not None:
        await channel.send(
            f"⚠ **I couldn't update myself.** {reason}\n"
            f"-# Running `{version.code_version()}` — fixes pushed since then aren't in yet."
        )


# Exit code the launcher watches for. Any other exit means RYTE stopped or
# crashed, and the launcher deliberately does not loop on those.
RESTART_EXIT_CODE = 42

# Often enough that a fix lands the same morning, rarely enough that it isn't
# a git fetch every minute for the rest of the year.
UPDATE_CHECK_SECONDS = 900


async def updater_loop(bot: "WilByteBot") -> None:
    """Restart onto a new version, but never in the middle of something.

    Updates have been landing several times a day, and each one needed someone
    to notice and restart by hand - which is how RYTE spent a day answering out
    of a checkout five commits old.

    It waits for a quiet moment. A run holds the lock through every approval
    click, so restarting while one is open would drop posts a person is halfway
    through reviewing.
    """
    while not bot.is_closed():
        await asyncio.sleep(UPDATE_CHECK_SECONDS)
        try:
            waiting = await asyncio.to_thread(version.update_waiting)
        except Exception:
            log.exception("Update check failed; trying again later")
            continue
        if not waiting:
            continue

        if bot.run_lock.locked():
            log.info("Update %s is waiting, but a run is open - leaving it", waiting)
            continue

        channel = _announce_channel(bot)
        if channel is not None:
            await channel.send(f"🔄 Updating myself — back in a moment.\n-# {waiting}")
        log.info("Restarting onto %s", waiting)
        await bot.close()
        os._exit(RESTART_EXIT_CODE)


# ------------------------------------------------------------------- publishing

# GHL publishes on the minute, so checking more often buys nothing; checking
# much less often would let a 10:00 post go out at 10:20.
PUBLISH_CHECK_SECONDS = 60


async def publisher_loop(bot: WilByteBot) -> None:
    """Publish scheduled posts when their slot arrives, for as long as RYTE runs.

    GoHighLevel's scheduler is driven by a background task its API doesn't
    create, so a post RYTE schedules sits there forever unless something
    publishes it. That something is this.

    It catches up rather than skipping: whatever is overdue goes out on the
    next check. If RYTE was asleep at 10am the post goes out late, which is the
    honest trade and still better than the alternative, which is never.
    """
    while not bot.is_closed():
        try:
            await _publish_due(bot)
        except asyncio.CancelledError:
            raise
        except Exception:  # a bad tick must not take the loop down for good
            log.exception("Publisher check failed; will try again next minute")
        await asyncio.sleep(PUBLISH_CHECK_SECONDS)


async def _publish_due(bot: WilByteBot) -> None:
    ledger = await asyncio.to_thread(Ledger.load)
    if not publisher.due(ledger):
        return

    published, problems = await asyncio.to_thread(
        publisher.publish_due, bot.config, ledger
    )
    for entry in published:
        log.info("Published %s (%s)", entry.title, entry.url_slug)
    for problem in problems:
        log.error("Could not publish: %s", problem)

    channel = _announce_channel(bot)
    if channel is None:
        return
    for entry in published:
        link = bot.config.brand.canonical_link(entry.url_slug)
        await channel.send(f"📣 **{entry.title}** is live — {link}")
    for problem in problems:
        await channel.send(f"⚠ Couldn't publish a post that was due — {problem}")


# Zoom and Fathom both know what was recorded and when, so nobody should have
# to tell RYTE. Checked on this cadence, which is well inside "before anyone
# goes looking for it" and nowhere near either platform's rate limit.
RECORDING_CHECK_SECONDS = 900


async def recordings_loop(bot: WilByteBot) -> None:
    """File new sales calls without being asked, for as long as RYTE runs."""
    # Let the first cache fill and the bot settle before the first sweep.
    await asyncio.sleep(60)
    while not bot.is_closed():
        try:
            await _file_new_recordings(bot)
        except asyncio.CancelledError:
            raise
        except Exception:  # a bad tick must not take the loop down for good
            log.exception("Recording sweep failed; will try again later")
        await asyncio.sleep(RECORDING_CHECK_SECONDS)


async def _file_new_recordings(bot: WilByteBot) -> None:
    found = await asyncio.to_thread(jobs.new_recordings, bot.config)
    if not found:
        return

    channel = _recordings_channel(bot)
    for call in found:
        try:
            title, url, note = await asyncio.to_thread(jobs.file_call, bot.config, call)
        except Exception as exc:  # one bad call must not stop the rest
            log.exception("Couldn't file %s", call.topic)
            if channel is not None:
                await channel.send(f"⚠ Couldn't file **{call.topic}** — {jobs._short(exc)}")
            continue

        log.info("Filed %s", title)
        if channel is not None:
            tail = f"\n⚠ {note}" if note else " with a summary"
            await channel.send(f"📁 **{title}** filed in Notion{tail}\n{url}")


def _recordings_channel(bot: WilByteBot):
    """Where new cards are announced. Its own channel if one is set."""
    configured = bot.config.secrets.discord_recordings_channel_id
    if configured:
        try:
            channel = bot.get_channel(int(configured))
        except (TypeError, ValueError):
            channel = None
        if channel is not None:
            return channel
    return _announce_channel(bot)


def _announce_channel(bot: WilByteBot):
    """Where to say a post went live: the first allowed channel, if there is one."""
    for raw in bot.config.secrets.discord_channel_ids:
        try:
            channel = bot.get_channel(int(raw))
        except (TypeError, ValueError):
            continue
        if channel is not None:
            return channel
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
            # The field takes several links too, space- or newline-separated.
            await _execute_run(
                bot, InteractionResponder(interaction),
                mentions.find_sources(playlist) or (playlist,),
                limit, mode_value, force,
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
        slots = await asyncio.to_thread(jobs.plan_slots, videos, context, config, ledger)
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
    booked: set[date] | None = None
    try:
        if context:
            booked = await asyncio.to_thread(jobs.taken_days, context, config, ledger)
            slots = jobs.open_slots(booked, 3, config)
        else:
            slots = jobs.open_slots(set(), 3, config)
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


async def _send_schedule(responder: Responder, config: Config) -> None:
    """The posting calendar: what goes out, on what day, soonest first."""
    ledger = await asyncio.to_thread(Ledger.load)
    context = await _maybe_open_ghl(config)
    try:
        posts = await asyncio.to_thread(jobs.upcoming_posts, context, config, ledger)
        booked = await asyncio.to_thread(jobs.taken_days, context, config, ledger)
        slots = jobs.open_slots(booked, 3, config)
    finally:
        if context:
            await asyncio.to_thread(context.close)

    await responder.send(
        embed=embeds.upcoming_summary(posts, next_slots=slots, reachable=context is not None)
    )

    # RYTE publishes these itself, so whether it is running is part of the
    # answer to "is this going out?" - and that isn't visible from GHL.
    waiting = len(
        [e for e in ledger.entries.values() if e.scheduled_at and not e.published_at]
    )
    for title, why in jobs.stuck_posts(ledger):
        await responder.send(f"⚠ **{title}** won't go out — {why}.")

    if waiting:
        moment = publisher.next_due(ledger)
        when = f" Next one: {moment.astimezone(ZoneInfo(config.schedule.timezone)):%a %b %d at %I:%M %p}." if moment else ""
        await responder.send(
            f"-# I publish these myself — GoHighLevel's scheduler doesn't fire on "
            f"posts made through its API. {waiting} waiting.{when} Keep me running "
            f"and they go out on time."
        )


async def _send_check(responder: Responder, config: Config, source: str | None) -> None:
    """Everything a real run needs, tested from where the bot actually runs."""
    credentials = [
        (bool(getattr(config.secrets, attr)), f"{env}{'' if getattr(config.secrets, attr) else ' is not set'}")
        for attr, env, required, _ in _PREFLIGHT
        if required or attr != "discord_guild_id"
    ]
    claude_rows = await asyncio.to_thread(jobs.check_anthropic, config)
    ghl_rows = await asyncio.to_thread(jobs.check_ghl, config)
    youtube_rows = await asyncio.to_thread(jobs.check_youtube, source)
    recording_rows = await asyncio.to_thread(jobs.check_recordings, config)

    await responder.send(
        embed=embeds.check_report(
            credentials=credentials, claude=claude_rows,
            ghl=ghl_rows, youtube=youtube_rows,
        )
    )
    lines = [
        f"{'✅' if ok else ('⚠' if ok is None else '❌')} {note}"
        for ok, note in recording_rows
    ]
    await responder.send("**RYTE Closer**\n" + "\n".join(lines))


async def _send_visible_calls(responder: Responder, config: Config, *, link: str = "") -> None:
    """What Zoom and Fathom will actually hand over, listed.

    A recording that was shared with the account rather than recorded on it is
    invisible to the API, and from Discord that is indistinguishable from a
    broken app. Seeing the list is what tells them apart.
    """
    await responder.send(
        "Checking that link against Zoom…" if link
        else "Asking Zoom and Fathom what they'll show me…"
    )
    try:
        if link:
            lines = await asyncio.to_thread(jobs.diagnose_link, config, link)
        else:
            lines = await asyncio.to_thread(jobs.visible_calls, config)
    except PIPELINE_ERRORS as exc:
        await responder.send(embed=embeds.error(str(exc)))
        return

    # Discord refuses anything over 2000 characters, and a busy account's list
    # goes past that - so send it in pieces rather than losing the tail.
    chunk: list[str] = []
    size = 0
    for line in lines:
        if size + len(line) > 1800 and chunk:
            await responder.send("\n".join(chunk))
            chunk, size = [], 0
        chunk.append(line)
        size += len(line) + 1
    if chunk:
        await responder.send("\n".join(chunk))


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


async def _send_fields(responder: Responder, config: Config, token: int) -> None:
    """Dump what GHL actually stores on each post, as an attached file.

    Three publish days have now been missed to a schedule date GHL accepts and
    then doesn't keep, and four guessed field names haven't found it. This
    prints the real objects side by side so the next change is based on the
    schema rather than another guess.
    """
    import json

    posts = await asyncio.to_thread(jobs.raw_post_fields, config)
    if not posts:
        await responder.send("GHL returned no posts at all — that's its own problem.")
        return

    path = DEFAULT_OUTPUT_DIR / "diagnostics" / f"ghl-posts-{token}.json"
    await asyncio.to_thread(
        _write_file, path, json.dumps(posts, indent=2, default=str)
    )

    lines = jobs.field_lines(posts, config)
    dateless = sum(1 for line in lines if "NO DATE" in line)
    head = (
        f"**What GHL is actually holding** — {len(posts)} post(s), "
        f"{dateless} with no date at all.\n"
    )
    # 25 lines is about the most that fits before Discord truncates; the file
    # has all of them either way.
    body = "\n".join(lines[:25])
    await responder.send(head + body, file=discord.File(str(path)))


async def _send_date_test(responder: Responder, config: Config, undo: bool) -> None:
    """Find out whether a future date alone is enough to schedule a post.

    If GHL's blog hides a PUBLISHED post dated ahead of now, the date does the
    scheduling and RYTE need not be awake at 10am at all. That is worth one
    controlled experiment, because the alternative is a laptop that has to stay
    running every night.
    """
    ledger = await asyncio.to_thread(Ledger.load)
    entry = jobs.next_pending(ledger)
    if entry is None:
        stuck = jobs.stuck_posts(ledger)
        if stuck:
            # Never just say "nothing pending" when the truth is "something is
            # pending and I can't publish it" - that is the failure we are here
            # to stop happening quietly.
            lines = [f"• **{title}** — {why}" for title, why in stuck]
            await responder.send(
                "I can't publish these, so they won't go out on their day:\n"
                + "\n".join(lines)
                + "\n\nPublish them by hand in GHL for now, and re-run them "
                "after the next update so I can track them properly."
            )
            return
        await responder.send("Nothing pending to test with — schedule a post first.")
        return

    context = await _maybe_open_ghl(config)
    if context is None:
        await responder.send("I can't reach GoHighLevel right now.")
        return

    status = ghl.STATUS_SCHEDULED if undo else ghl.STATUS_PUBLISHED
    try:
        slot = await asyncio.to_thread(jobs.set_status, context, entry, status)
    except PIPELINE_ERRORS as exc:
        await responder.send(embed=embeds.error(f"Couldn't change the post: {exc}"))
        return
    finally:
        await asyncio.to_thread(context.close)

    link = config.brand.canonical_link(entry.url_slug)
    if undo:
        await responder.send(
            f"Put **{entry.title}** back to scheduled. I'll publish it at "
            f"{slot.astimezone(ZoneInfo(config.schedule.timezone)):%a %b %d at %I:%M %p} as before."
        )
        return

    await responder.send(
        f"Marked **{entry.title}** as published, dated "
        f"{slot.astimezone(ZoneInfo(config.schedule.timezone)):%a %b %d at %I:%M %p} — "
        f"which is still in the future.\n\n"
        f"**Now open {link}**\n"
        f"• **404 / not found** → GoHighLevel hides future-dated posts. The date "
        f"alone schedules it, and I don't need to be running at 10am at all. Tell me "
        f"and I'll make that the default.\n"
        f"• **The post is live** → it doesn't, so this went out early. Say "
        f"`@RYTE datetest undo` and I'll put it straight back to its Aug 18 slot."
    )


# Images only, and small ones - this is for logos and banners, not video.
HOST_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp")
MAX_HOST_BYTES = 10_000_000


async def _host_images(responder: Responder, config: Config, message) -> None:
    """Give an attached image a permanent public URL, via the GHL media library.

    Notion only takes an external URL for a cover or icon and never re-hosts
    it, so the link has to outlive the message. Discord's own attachment URLs
    and Notion's S3 links both expire; GHL's media library is already
    connected, already public, and already where the blog covers live.
    """
    attachments = [
        a for a in getattr(message, "attachments", []) or []
        if str(a.filename).lower().endswith(HOST_SUFFIXES)
    ]
    if not attachments:
        await responder.send("Attach a PNG or JPG with `@RYTE host` and I'll give you a link.")
        return

    for attachment in attachments:
        if attachment.size and attachment.size > MAX_HOST_BYTES:
            await responder.send(f"`{attachment.filename}` is too big — 10MB max.")
            continue

        path = DEFAULT_OUTPUT_DIR / "hosted" / attachment.filename
        path.parent.mkdir(parents=True, exist_ok=True)
        await attachment.save(path)
        try:
            url = await asyncio.to_thread(
                jobs.host_image, config, path, name=attachment.filename
            )
        except PIPELINE_ERRORS as exc:
            await responder.send(embed=embeds.error(f"Couldn't host {attachment.filename}\n{exc}"))
            continue
        await responder.send(f"🔗 `{attachment.filename}`\n{url}")


# One run reads this far back and files at most this many. A channel with a
# year in it would otherwise be one enormous unattended spend, and the tally
# says what was left so it can be run again.
BACKFILL_SCAN = 500
BACKFILL_FILE = 40


async def _backfill_sops(bot: WilByteBot, responder: Responder, message) -> None:
    """File what was posted in the SOP channel before RYTE was watching it."""
    from .. import sops

    channel = message.channel
    if not is_sop_channel(message, bot.config):
        channel = _first_sop_channel(bot)
        if channel is None:
            await responder.send(
                "I don't have an SOP channel set, so there's nothing to backfill."
            )
            return

    await responder.send(f"Reading back through {channel.mention} — this takes a minute.")

    filed: list[str] = []
    skipped = 0
    problems: list[str] = []
    seen = 0

    # Oldest first, so the library ends up in the order things happened.
    async for old in channel.history(limit=BACKFILL_SCAN, oldest_first=True):
        if len(filed) >= BACKFILL_FILE:
            break
        seen += 1
        if getattr(old.author, "bot", False) or sops.already_filed(old.id):
            continue

        images, audio = message_files(old)
        sop = sops.find_sop(old.content or "", images=images, audio=audio)
        if sop is None:
            skipped += 1
            continue

        sop.posted_by = getattr(old.author, "display_name", "") or ""
        sop.posted_on = old.created_at.date() if old.created_at else None

        summary = ""
        try:
            summary = await asyncio.to_thread(jobs.sop_summary, bot.config, sop)
        except PIPELINE_ERRORS as exc:
            log.warning("Backfill couldn't read %s: %s", sop.title, exc)
            sop.note = sop.note or f"No summary — {exc}"
        try:
            title, _ = await asyncio.to_thread(jobs.file_sop, bot.config, sop, summary=summary)
        except PIPELINE_ERRORS as exc:
            problems.append(f"{sop.title}: {jobs._short(exc)}")
            continue

        sops.remember(old.id)
        filed.append(title)

    lines = [f"📘 Filed {len(filed)} of {seen} message(s) read."]
    lines += [f"· {title}" for title in filed[:20]]
    if len(filed) > 20:
        lines.append(f"-# …and {len(filed) - 20} more")
    if skipped:
        lines.append(f"-# {skipped} skipped as chatter — no link, no file, nothing written.")
    if len(filed) >= BACKFILL_FILE:
        lines.append(
            f"-# Stopped at {BACKFILL_FILE} for one run. Say `backfill` again to carry on."
        )
    for problem in problems[:5]:
        lines.append(f"⚠ {problem}")

    await responder.send("\n".join(lines))


def _first_sop_channel(bot: WilByteBot):
    for raw in bot.config.secrets.discord_sop_channel_ids:
        try:
            found = bot.get_channel(int(raw))
        except (TypeError, ValueError):
            continue
        if found is not None:
            return found
    return None


async def _send_sops(responder: Responder, config: Config, asked: str) -> None:
    """Answer "do we have an SOP for X" out of the Notion library."""
    from .. import sops

    topic = sops.wanted_topic(asked)
    try:
        found = await asyncio.to_thread(jobs.find_sops, config, topic)
    except PIPELINE_ERRORS as exc:
        await responder.send(embed=embeds.error(f"Couldn't read the SOP library\n{exc}"))
        return

    if not found:
        await responder.send(
            f"Nothing in the SOP library for “{topic}” yet." if topic
            else "The SOP library is empty so far."
        )
        return

    lines = [f"📘 **{title}**\n{link or card}" for title, card, link in found]
    head = f"{len(found)} SOP(s) for “{topic}”:" if topic else "The most recent:"
    await responder.send(f"{head}\n" + "\n".join(lines))


async def _send_cards(responder: Responder, config: Config, asked: str) -> None:
    """Hand back the gallery card somebody asked for."""
    from .. import recordings

    name = recordings.wanted_name(asked)
    try:
        found = await asyncio.to_thread(jobs.find_cards, config, name)
    except PIPELINE_ERRORS as exc:
        await responder.send(embed=embeds.error(f"Couldn't read the Notion gallery\n{exc}"))
        return

    if not found:
        await responder.send(
            f"Nothing in the gallery for “{name}”." if name
            else "There's nothing in the recordings gallery yet."
        )
        return

    if len(found) == 1:
        title, card, link = found[0]
        # The recording first: "need the video of Derrick" wants the video, and
        # sending only the page makes somebody open it and click again.
        body = f"📁 **{title}**"
        if link:
            body += f"\n{link}"
        await responder.send(f"{body}\n-# Card: {card}" if card else body)
        return

    lines = [
        f"· **{title}**\n{link or card}" for title, card, link in found
    ]
    await responder.send(
        (f"{len(found)} for “{name}”:" if name else "The most recent:") + "\n" + "\n".join(lines)
    )


def _words_beside_link(text: str, url: str) -> str:
    """Whatever was typed around the link, minus the link and the passcode line.

    "@RYTE <link> derrick" leaves "derrick", which is a search. The command
    words themselves are dropped so `recording` isn't hunted for as a name.
    """
    kept: list[str] = []
    for line in (text or "").splitlines():
        if line.strip().casefold().startswith(("passcode", "password", "pwd", "code")):
            continue
        kept.append(line)
    remainder = " ".join(kept)
    # Guarded: "".replace("", " ") inserts a space between every character, so
    # an answer with no link in it came back as nothing at all.
    if url:
        remainder = remainder.replace(url, " ")
    remainder = mentions.ANY_URL_RE.sub(" ", remainder)
    remainder = mentions.MENTION_RE.sub(" ", mentions.ROLE_MENTION_RE.sub(" ", remainder))
    words = [
        word for word in remainder.split()
        if word.casefold() not in mentions.RECORDING_WORDS and len(word) > 1
    ]
    return " ".join(words).strip()


async def _key_from_words(config: Config, typed: str) -> str | None:
    """The one call those words name, or None if they name none or several."""
    if not typed:
        return None
    try:
        matches = await asyncio.to_thread(jobs.search_calls, config, typed)
    except PIPELINE_ERRORS as exc:
        log.warning("Couldn't search the call list: %s", exc)
        return None
    return matches[0].key if len(matches) == 1 else None


async def _ask_which_call(responder: Responder, config: Config, message, rec, typed: str) -> str | None:
    """Show the recordings and let them pick. Returns the chosen call's key.

    Zoom's API cannot resolve a share link to a meeting, so something has to
    say which call it is. Everything cleverer than asking has been tried: three
    goes at the token format, then recency, which filed two cards carrying
    another client's summary. Asking is the only one that is never wrong.

    A name typed beside the link filters the list, so the common case is a
    short list rather than a scroll through ninety.
    """
    try:
        near = await asyncio.to_thread(jobs.search_calls, config, typed or "", limit=25)
        if not near:
            near = await asyncio.to_thread(jobs.picker_choices, config)
    except PIPELINE_ERRORS as exc:
        log.warning("Couldn't list calls to ask about: %s", exc)
        return None
    if not near:
        return None

    view = RecordingPicker(
        [
            (
                item.topic or "(no topic)",
                f"{item.when[:10]} · {item.who}"
                + ("" if item.platform == "fathom" else ""),
                item.key,
            )
            for item in near
        ],
        requester_id=getattr(getattr(message, "author", None), "id", None),
        timeout=600,
    )
    # The names go in the message as well as in the menu. A collapsed dropdown
    # shows its placeholder and nothing else, so the one thing somebody needs
    # in order to answer was the one thing they had to click to see.
    shown = "\n".join(
        f"· **{item.topic or '(no topic)'}** — {item.when[:10]}" for item in near[:10]
    )
    more = f"\n-# …and {len(near) - 10} more in the menu" if len(near) > 10 else ""
    await responder.send(f"Which call is this?\n{shown}{more}", view=view)
    await view.wait()
    return view.chosen


async def _file_recording(
    responder: Responder, config: Config, message, *, chosen_key: str | None = None
) -> None:
    """File a posted sales call in the Notion gallery.

    Reads the message that was replied to when the mention carries no link,
    because the natural way to do this is to reply to whoever posted the
    recording rather than paste their link again underneath it.
    """
    from .. import recordings

    text = message.content or ""
    found = recordings.find_recording(text)
    poster = getattr(getattr(message, "author", None), "display_name", "") or ""

    if found is None:
        replied = await _replied_to(message)
        if replied is not None:
            found = recordings.find_recording(replied.content or "")
            poster = getattr(getattr(replied, "author", None), "display_name", "") or poster

    if found is None:
        await responder.send(
            "I can't see a recording link. Paste a Zoom, Fathom or YouTube link "
            "after `recording`, or reply to the message that has it."
        )
        return

    found.posted_by = poster
    found.posted_on = getattr(message, "created_at", None)
    if found.posted_on is not None:
        found.posted_on = found.posted_on.date()

    # "Sales: Derrick Robison <link>" settles it before anything is worked out.
    # Zoom titles a recording after whoever was on it, so the name somebody
    # typed to label the card is also the name that finds the call.
    typed = found.client_hint or _words_beside_link(text, found.url)
    if not chosen_key:
        chosen_key = await _key_from_words(config, typed)

    summary = ""
    if not chosen_key and found.transcribable(config):
        await responder.send(f"Filing the {found.platform} recording — reading it first.")
        try:
            summary = await asyncio.to_thread(jobs.summarise_call, config, found)
        except PIPELINE_ERRORS as exc:
            # A missing summary is a worse entry, not a failed one - so file it,
            # but say why. Three cards have now been filed silently without one,
            # and each time the reason turned out to be somewhere else.
            log.warning("Could not summarise %s: %s", found.url, exc)
            found.note = found.note or f"No summary — {exc}"

    # Nothing identified it, so ask - and wait. Filing first and asking after
    # posts a card that is already wrong, which somebody then has to notice.
    if not summary and not chosen_key and found.note and found.platform in ("Zoom", "Fathom"):
        chosen_key = await _ask_which_call(responder, config, message, found, typed)

    if chosen_key:
        try:
            picked = await asyncio.to_thread(jobs.find_choice, config, chosen_key)
            if picked is None:
                await responder.send("I've lost track of that one — post the link again.")
                return
            # Chosen by hand, so the complaint about not identifying it is stale.
            found.note = ""
            read = await asyncio.to_thread(jobs.read_chosen, config, found, picked)
            summary = found.fathom_summary or await asyncio.to_thread(
                jobs.summarise_text, config, read
            )
        except PIPELINE_ERRORS as exc:
            log.warning("Could not read the chosen call: %s", exc)
            found.note = f"No summary — {exc}"

    try:
        title, url = await asyncio.to_thread(jobs.file_recording, config, found, summary=summary)
    except PIPELINE_ERRORS as exc:
        await responder.send(embed=embeds.error(f"Couldn't file it in Notion\n{exc}"))
        return

    if summary:
        tail = " with a summary"
    elif found.note:
        # Filed without a summary *and why*. Silence here reads as "nothing to
        # say about the call" rather than "I never found it".
        tail = f"\n⚠ {found.note}"
    elif not found.transcribable(config):
        tail = f" — {found.platform} recordings can't be read from here, so no summary"
    else:
        tail = " — no transcript was available"

    # Zoom's API returns a different share token than its website does, so a
    # pasted link can't be resolved to a recording. When the call was worked
    # out some other way, say which - a guess nobody can see is a guess nobody
    # can correct.
    aside = ""
    if summary and found.matched_by and found.matched_by != "the link":
        # Name the call, not just the method. "Matched by recency" gave no way
        # to notice that a link saying Derrick had filed a call with Arlene.
        which = f" — read **{found.topic}**" if found.topic else ""
        aside = (
            f"\n-# Identified by {found.matched_by}{which}. "
            "Wrong call? Delete the card and post the link again with its passcode."
        )
    await responder.send(f"📁 **{title}** filed in Notion{tail}{aside}\n{url}")


async def _replied_to(message):
    """The message this one is a reply to, if it is one."""
    reference = getattr(message, "reference", None)
    if reference is None:
        return None
    resolved = getattr(reference, "resolved", None)
    if resolved is not None and getattr(resolved, "content", None) is not None:
        return resolved
    message_id = getattr(reference, "message_id", None)
    if message_id is None:
        return None
    try:
        return await message.channel.fetch_message(message_id)
    except Exception:  # deleted, or in a channel RYTE can't read back
        return None


async def _send_missed(responder: Responder, config: Config) -> None:
    """The posts RYTE wrote that never reached GHL, and the line to redo them."""
    ledger = await asyncio.to_thread(Ledger.load)
    leftovers = await asyncio.to_thread(
        jobs.built_but_not_posted, DEFAULT_OUTPUT_DIR, ledger
    )
    if not leftovers:
        await responder.send("Nothing missed — every post I've written made it to GHL.")
        return

    listed = "\n".join(f"• {title}" for title, _link in leftovers)
    links = " ".join(link for _title, link in leftovers)
    await responder.send(
        f"**{len(leftovers)} written but never posted:**\n{listed}\n\n"
        f"Send this to redo them:\n```\n@RYTE {links} force\n```"
    )


async def _send_reconcile(responder: Responder, config: Config) -> None:
    """Free up days RYTE is holding for posts that no longer exist in GHL."""
    ledger = await asyncio.to_thread(Ledger.load)
    context = await _maybe_open_ghl(config)
    if context is None:
        await responder.send("I can't reach GoHighLevel, so I can't check what's still there.")
        return

    try:
        gone, kept, problems = await asyncio.to_thread(jobs.reconcile, context, ledger)
        booked = await asyncio.to_thread(jobs.taken_days, context, config, ledger)
        slots = jobs.open_slots(booked, 3, config)
    finally:
        await asyncio.to_thread(context.close)

    if not gone and not problems:
        await responder.send(
            f"Nothing to tidy — all {len(kept)} post(s) I'm tracking are still in GHL."
        )
        return

    lines = [f"Freed up {len(gone)} day(s) — these posts aren't in GHL any more:"]
    lines += [f"• **{e.title or e.url_slug}** — was {_slot_day(e, config)}" for e in gone]
    lines += [f"⚠ Couldn't check {p}" for p in problems]
    if slots:
        lines.append("\nNext posts land: " + ", ".join(f"{s:%a %b %d}" for s in slots))
    await responder.send("\n".join(lines))


def _slot_day(entry, config: Config) -> str:
    from ..scheduler import parse_timestamp

    slot = parse_timestamp(entry.scheduled_at) if entry.scheduled_at else None
    if slot is None:
        return "a draft"
    return f"{slot.astimezone(ZoneInfo(config.schedule.timezone)):%a %b %d}"


async def _set_earliest_day(responder: Responder, config: Config, text: str) -> None:
    """Move the calendar's starting point, and show what it means in practice.

    Saved outside the tracked config on purpose: editing `config/wilbyte.toml`
    on the Mac would stop the auto-update fast-forwarding, and a RYTE stuck on
    old code is a worse problem than a wrong start date.
    """
    if text.strip().lower() in ("clear", "off", "none", "reset"):
        await asyncio.to_thread(prefs.clear_earliest_day)
        await responder.send("Cleared — I'll just use the next free weekday.")
        return

    if not text.strip():
        await responder.send(
            f"{prefs.describe(config)}\nSet it with `@RYTE start Aug 18`, or "
            f"`@RYTE start clear` to drop it."
        )
        return

    try:
        day = prefs.parse_day(text)
    except prefs.PrefsError as exc:
        await responder.send(str(exc))
        return

    await asyncio.to_thread(prefs.set_earliest_day, day)

    ledger = await asyncio.to_thread(Ledger.load)
    context = await _maybe_open_ghl(config)
    try:
        booked = await asyncio.to_thread(jobs.taken_days, context, config, ledger)
        slots = jobs.open_slots(booked, 3, config)
    finally:
        if context:
            await asyncio.to_thread(context.close)

    listed = ", ".join(f"{s:%a %b %d}" for s in slots) or "(nothing free — the calendar is full)"
    await responder.send(
        f"Got it — nothing before **{day:%a %b %d, %Y}**.\nNext posts land: {listed}"
    )


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


def publish_status(decision: Decision) -> str:
    """Which GHL status a reviewed post gets.

    The button is the whole decision. The run mode only picks the default the
    review returns when approval is off - a card that says "Schedule it" above
    a date has to schedule when clicked, whatever word started the run.
    """
    return ghl.STATUS_DRAFT if decision is Decision.DRAFT else ghl.STATUS_SCHEDULED


async def _offer_retry(responder: Responder, unfinished: list) -> None:
    """Hand back a line that redoes exactly the videos that didn't make it.

    "Created 1, Skipped 4, Failed 1" says what happened and nothing about what
    to do next - the links are somewhere up the channel, and matching four
    titles back to sixteen URLs by hand is the sort of thing that gets skipped
    and then forgotten about.
    """
    if not unfinished:
        return

    seen: set[str] = set()
    links = []
    for video in unfinished:
        if video.video_id not in seen:
            seen.add(video.video_id)
            links.append(video.short_url)

    await responder.send(
        f"**{len(links)} didn't get posted.** Send this to do just those:\n"
        f"```\n@RYTE {' '.join(links)} force\n```"
    )


async def _execute_run(
    bot: WilByteBot,
    responder: Responder,
    sources: tuple[str, ...] | list[str],
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
            jobs.resolve_many, sources, ledger,
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
    # Which videos didn't make it, so the retry doesn't mean hunting through
    # the original message for the links that got no answer.
    unfinished: list = []
    try:
        slot_pool = await asyncio.to_thread(jobs.plan_slots, videos, context, config, ledger)
        # Say what was left out. Ten links in and eight posts back looks like a
        # bug unless the reason is on screen.
        trimmed = max(0, len(sources) - len(videos) - already_done)
        await responder.send(
            f"On it — building {len(videos)} post(s) in **{mode}** mode."
            + (f" Skipping {already_done} already done." if already_done else "")
            + (
                f" That's my {config.discord.max_batch}-per-run cap — send the "
                f"other {trimmed} link(s) after and I'll carry on from there."
                if trimmed else ""
            )
        )

        for index, video in enumerate(videos, start=1):
            try:
                post = await asyncio.to_thread(
                    jobs.build, video, config, output_dir,
                    transcript_text=transcript_text if len(videos) == 1 else None,
                )
            except PIPELINE_ERRORS as exc:
                failed += 1
                unfinished.append(video)
                await responder.send(
                    embed=embeds.error(f"{video.title or video.short_url}\n{exc}")
                )
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
                unfinished.append(video)
                await responder.send(
                    f"No answer on **{post.title}** — skipped it. Files are in "
                    f"`{output_dir / post.url_slug}` if you want them."
                )
                continue
            if decision is Decision.STOP:
                skipped += 1
                # Everything from here on was never looked at, so it all needs
                # offering back - not just the post that was on screen.
                unfinished.extend(videos[index - 1:])
                await responder.send("Stopped.")
                break

            if mode == "preview":
                created += 1
                continue

            status = publish_status(decision)
            to_draft = status == ghl.STATUS_DRAFT
            if to_draft:
                post.scheduled_at = None
            elif slot_pool:
                slot_pool.pop(0)

            try:
                await asyncio.to_thread(jobs.publish, post, config, context, status=status)
                await asyncio.to_thread(jobs.record, ledger, post)
                created += 1
                # A post GHL accepted but didn't date will never publish, and a
                # post with no markup publishes as a wall of tags. Both have to
                # land next to the tick, not in a log. Headline and cover notes
                # don't - the post still works, and they'd bury these.
                trouble = "\n".join(
                    f"⚠ {w}" for w in post.warnings if "GHL" in w or "publish" in w
                )
                await responder.send(
                    f"✅ **{post.title}** → `{post.url_slug}` "
                    + (
                        f"scheduled for {post.scheduled_at:%a %b %d at %I:%M %p}"
                        if post.scheduled_at
                        else "saved as a draft"
                    )
                    + (f"\n{trouble}" if trouble else "")
                )
            except PIPELINE_ERRORS as exc:
                failed += 1
                unfinished.append(video)
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

    await _offer_retry(responder, unfinished)

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
