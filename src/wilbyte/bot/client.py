"""The Discord bot: slash commands over the Wil Byte pipeline.

    /plan    playlist:<url>              what would be posted, and when
    /run     playlist:<url> limit:3      build each post, approve it, schedule it
    /status                              ledger + next open slots
    /cover   kicker:.. headline:..       render a cover image on its own

Runs are serialized behind a lock: slot assignment reads the blog's occupied
days from GHL, so two concurrent runs would hand out the same day twice.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import discord
from discord import app_commands

from .. import cover as cover_mod
from .. import ghl
from ..config import Config, ConfigError, load_config
from ..copywriter import CopywriterError
from ..models import CoverPlan
from ..pipeline import DEFAULT_OUTPUT_DIR, PipelineError
from ..scheduler import SchedulerError, next_open_slots
from ..state import Ledger
from ..youtube import IngestError
from . import embeds, jobs
from .views import ApprovalView, Decision

log = logging.getLogger("wilbyte.bot")

# Errors that mean "this post failed" rather than "the bot is broken".
PIPELINE_ERRORS = (
    IngestError, CopywriterError, cover_mod.CoverError, ghl.GHLError,
    PipelineError, SchedulerError, ConfigError,
)


class WilByteBot(discord.Client):
    def __init__(self, config: Config):
        super().__init__(intents=discord.Intents.default())
        self.config = config
        self.tree = app_commands.CommandTree(self)
        self.run_lock = asyncio.Lock()

    async def setup_hook(self) -> None:
        register_commands(self)
        guild_id = self.config.secrets.discord_guild_id
        if guild_id:
            # Guild-scoped commands appear immediately; global ones take ~1 hour.
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Slash commands synced to guild %s", guild_id)
        else:
            await self.tree.sync()
            log.info("Slash commands synced globally (may take up to an hour to appear)")

    async def on_ready(self) -> None:
        log.info("Connected as %s", self.user)


# ------------------------------------------------------------------ permissions


def is_allowed(interaction: discord.Interaction, config: Config) -> tuple[bool, str]:
    """Channel and role gating. An empty allowlist means 'no restriction'."""
    channels = config.secrets.discord_channel_ids
    if channels and str(interaction.channel_id) not in channels:
        return False, "Wil Byte isn't enabled in this channel."

    roles = config.secrets.discord_role_ids
    if roles:
        member_roles = {str(r.id) for r in getattr(interaction.user, "roles", [])}
        if not member_roles & set(roles):
            return False, "You don't have the role required to run this."
    return True, ""


async def guard(interaction: discord.Interaction, config: Config) -> bool:
    allowed, reason = is_allowed(interaction, config)
    if not allowed:
        await interaction.response.send_message(reason, ephemeral=True)
    return allowed


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

        try:
            limit = max(1, min(limit, config.discord.max_batch))
            ledger = await asyncio.to_thread(Ledger.load)
            videos, skipped = await asyncio.to_thread(
                jobs.resolve_videos, playlist, ledger, limit=limit, force=False
            )
            if not videos:
                await interaction.followup.send(
                    f"Nothing pending — all {skipped} video(s) are already processed."
                )
                return

            context = await _maybe_open_ghl(config)
            try:
                slots = await asyncio.to_thread(jobs.plan_slots, videos, context, config)
            finally:
                if context:
                    await asyncio.to_thread(context.close)

            source = "the playlist" if len(videos) > 1 else "the video"
            await interaction.followup.send(
                embed=embeds.plan_summary(list(zip(videos, slots)), skipped=skipped, source=source)
            )
        except PIPELINE_ERRORS as exc:
            await interaction.followup.send(embed=embeds.error(str(exc)))

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
            await _execute_run(bot, interaction, playlist, limit, mode_value, force)

    @bot.tree.command(name="status", description="What's been posted and what's next")
    async def status(interaction: discord.Interaction):
        if not await guard(interaction, config):
            return
        await interaction.response.defer(thinking=True)

        try:
            ledger = await asyncio.to_thread(Ledger.load)
            entries = sorted(
                ledger.entries.values(), key=lambda e: e.processed_at, reverse=True
            )
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

            await interaction.followup.send(
                embed=embeds.status_summary(
                    processed=len(ledger.entries),
                    recent=recent,
                    next_slots=slots,
                    booked_days=booked,
                )
            )
        except PIPELINE_ERRORS as exc:
            await interaction.followup.send(embed=embeds.error(str(exc)))

    @bot.tree.command(name="cover", description="Render a cover image from two lines of text")
    @app_commands.describe(kicker="The highlighted 3-5 word line", headline="The big line underneath")
    async def cover(interaction: discord.Interaction, kicker: str, headline: str):
        if not await guard(interaction, config):
            return
        await interaction.response.defer(thinking=True)

        try:
            plan_obj = CoverPlan(kicker=kicker.upper(), headline=headline.upper())
            out = DEFAULT_OUTPUT_DIR / "previews" / f"cover-{interaction.id}.png"
            await asyncio.to_thread(cover_mod.render_cover, plan_obj, config, out)
            await interaction.followup.send(file=discord.File(out, filename="cover.png"))
        except PIPELINE_ERRORS as exc:
            await interaction.followup.send(embed=embeds.error(str(exc)))


# ----------------------------------------------------------------------- runner


async def _maybe_open_ghl(config: Config):
    """Open a GHL session if credentials exist; otherwise run against an empty calendar."""
    if not (config.secrets.ghl_api_token and config.secrets.ghl_location_id
            and config.secrets.ghl_blog_id):
        return None
    return await asyncio.to_thread(jobs.open_ghl, config)


async def _execute_run(
    bot: WilByteBot,
    interaction: discord.Interaction,
    source: str,
    limit: int,
    mode: str,
    force: bool,
) -> None:
    config = bot.config
    limit = max(1, min(limit, config.discord.max_batch))
    output_dir = DEFAULT_OUTPUT_DIR

    try:
        ledger = await asyncio.to_thread(Ledger.load)
        videos, already_done = await asyncio.to_thread(
            jobs.resolve_videos, source, ledger, limit=limit, force=force
        )
    except PIPELINE_ERRORS as exc:
        await interaction.followup.send(embed=embeds.error(str(exc)))
        return

    if not videos:
        await interaction.followup.send(
            f"Nothing pending — all {already_done} video(s) are already processed. "
            "Use `force: true` to redo one."
        )
        return

    context = None
    if mode != "preview":
        try:
            context = await _maybe_open_ghl(config)
            if context is None:
                await interaction.followup.send(
                    embed=embeds.error(
                        "No GHL credentials configured, so nothing can be posted. "
                        "Use `mode: preview` to build posts locally instead."
                    )
                )
                return
        except PIPELINE_ERRORS as exc:
            await interaction.followup.send(embed=embeds.error(str(exc)))
            return

    created = skipped = failed = 0
    try:
        slot_pool = await asyncio.to_thread(jobs.plan_slots, videos, context, config)
        await interaction.followup.send(
            f"Building {len(videos)} post(s) in **{mode}** mode."
            + (f" Skipping {already_done} already done." if already_done else "")
        )

        for index, video in enumerate(videos, start=1):
            try:
                post = await asyncio.to_thread(jobs.build, video, config, output_dir)
            except PIPELINE_ERRORS as exc:
                failed += 1
                await interaction.followup.send(
                    embed=embeds.error(f"{video.title}\n{exc}")
                )
                continue

            # Show the slot this post would take without consuming it yet, so a
            # skip leaves the day free for the next post in the batch.
            post.scheduled_at = slot_pool[0] if slot_pool else None

            decision = await _review(
                interaction, post, index=index, total=len(videos), mode=mode, config=config
            )

            if decision is Decision.SKIP:
                skipped += 1
                continue
            if decision is Decision.TIMEOUT:
                skipped += 1
                await interaction.followup.send(
                    f"No response on **{post.title}** — skipped. Files are in "
                    f"`{output_dir / post.url_slug}`."
                )
                continue
            if decision is Decision.STOP:
                skipped += 1
                await interaction.followup.send("Run stopped.")
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
                await interaction.followup.send(
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
                await interaction.followup.send(
                    embed=embeds.error(f"Failed to publish {post.title}\n{exc}")
                )

    except PIPELINE_ERRORS as exc:
        await interaction.followup.send(embed=embeds.error(str(exc)))
    finally:
        if context:
            await asyncio.to_thread(context.close)

    await interaction.followup.send(
        embed=embeds.result_summary(
            created=created, skipped=skipped, failed=failed,
            mode=mode, output_dir=str(output_dir),
        )
    )


async def _review(
    interaction: discord.Interaction,
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
        await interaction.followup.send(embed=embed, file=file)
        return Decision.DRAFT if mode == "draft" else Decision.APPROVE

    view = ApprovalView(
        requester_id=interaction.user.id,
        timeout=config.discord.approval_timeout_seconds,
    )
    await interaction.followup.send(embed=embed, file=file, view=view)
    await view.wait()
    return view.decision


# ------------------------------------------------------------------- entrypoint


def build_bot(config: Config | None = None) -> WilByteBot:
    return WilByteBot(config or load_config())


def run_bot(config: Config | None = None) -> None:
    config = config or load_config()
    config.secrets.require("discord_bot_token")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    build_bot(config).run(config.secrets.discord_bot_token, log_handler=None)
