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
import time
from datetime import date, datetime
from functools import partial
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from discord import app_commands

from .. import corpus
from .. import waiting
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
from . import views
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


# What Discord shows under a bot's name. Empty means none at all.
ACTIVITY_VERBS = {
    "watching": discord.ActivityType.watching,
    "playing": discord.ActivityType.playing,
    "listening": discord.ActivityType.listening,
    "competing": discord.ActivityType.competing,
}


def _activity():
    """The status line, read from DISCORD_ACTIVITY. None when it isn't set.

    Written as "watching: the SOP channel" or just "the SOP channel", which
    Discord shows as "Watching". A status line that has stopped being true is
    worse than none, so having none is the default.
    """
    raw = (os.getenv("DISCORD_ACTIVITY") or "").strip()
    if not raw:
        return None

    verb, _, rest = raw.partition(":")
    label = verb.strip().casefold()
    name = rest.strip() if (label in ACTIVITY_VERBS or label == "custom") else raw

    # A custom status carries no verb, so the line reads as written - "busy
    # being cute" rather than "Playing busy being cute".
    if label == "custom":
        return discord.CustomActivity(name=name[:128])
    kind = ACTIVITY_VERBS.get(label, discord.ActivityType.watching)
    return discord.Activity(type=kind, name=name[:128])


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
        self.caption_task: asyncio.Task | None = None
        self.board_task: asyncio.Task | None = None
        self.agent_task: asyncio.Task | None = None
        self.setup_task: asyncio.Task | None = None
        self.recordings_task: asyncio.Task | None = None
        self.catchup_task: asyncio.Task | None = None

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
        # The "Watching ..." line under his name. Set DISCORD_ACTIVITY to change
        # it; leave it empty and he has none, which is what the profile looked
        # like once the line stopped being true - he does rather more than watch
        # YouTube now.
        await self.change_presence(activity=_activity())
        # on_ready fires again after a reconnect, so guard it - two publisher
        # loops would race each other for the same posts.
        if self.publisher_task is None or self.publisher_task.done():
            self.publisher_task = self.loop.create_task(publisher_loop(self))
        if self.updater_task is None or self.updater_task.done():
            self.updater_task = self.loop.create_task(updater_loop(self))
        # Videos announced before YouTube had finished captioning them. Also
        # safe every start: the list is on disk, so a restart mid-wait picks
        # up where it left off.
        if self.caption_task is None or self.caption_task.done():
            self.caption_task = self.loop.create_task(caption_loop(self))
        # The daily board walks itself only when asked to. It writes to a board
        # four people work off every day, on a timer, whether or not anybody is
        # looking.
        if self.config.secrets.trello_auto and (
            self.board_task is None or self.board_task.done()
        ):
            self.board_task = self.loop.create_task(board_loop(self))
        # New agents arrive whenever a client signs, which is not on any
        # schedule - so this one watches rather than waiting to be asked.
        if self.config.secrets.trello_agents_auto and (
            self.agent_task is None or self.agent_task.done()
        ):
            self.agent_task = self.loop.create_task(agent_loop(self))
        # Same switch: both are about the agents rather than about the board's
        # own routine, and wanting one is wanting the other.
        if self.config.secrets.trello_agents_auto and (
            self.setup_task is None or self.setup_task.done()
        ):
            self.setup_task = self.loop.create_task(setup_check_loop(self))
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
        # Anything posted while the Mac was off. Safe to run every start:
        # what is already filed is passed over.
        if self.config.secrets.discord_sop_channel_ids and (
            self.catchup_task is None or self.catchup_task.done()
        ):
            self.catchup_task = self.loop.create_task(catch_up_sops(self))
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


# What RYTE leaves on a post he has filed, in place of a message.
SOP_FILED_REACTION = "📘"


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
    summary = ""
    try:
        summary = await asyncio.to_thread(jobs.sop_summary, bot.config, sop)
    except PIPELINE_ERRORS as exc:
        log.warning("Couldn't read the SOP %s: %s", sop.title, exc)
        sop.note = sop.note or f"No summary — {exc}"

    try:
        title, url = await asyncio.to_thread(jobs.file_sop, bot.config, sop, summary=summary)
    except PIPELINE_ERRORS as exc:
        # Failure is the one thing worth saying out loud. Filing quietly and
        # failing quietly are not the same promise.
        await responder.send(embed=embeds.error(f"Couldn't file that SOP\n{exc}"))
        return

    sops.remember(getattr(message, "id", ""))
    log.info("Filed %s", title)

    # A reaction rather than a reply. The channel is for procedures, not for
    # RYTE announcing that he noticed one - but a post that filed and a post
    # that was passed over as chatter should not look the same afterwards.
    try:
        await message.add_reaction(SOP_FILED_REACTION)
    except discord.HTTPException:
        # Adding reactions is its own permission, and not having it is not a
        # reason to think the SOP failed.
        pass


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

    # Ask for the transcript before announcing anything. A video announced the
    # minute it goes up has no captions for another while yet, and finding that
    # out by running the whole thing costs three messages in the channel to say
    # that nothing happened.
    ready, problem = await asyncio.to_thread(jobs.waiting_on_captions, links[0])
    if not ready and waiting.not_ready_yet(problem):
        await _hold_for_captions(bot, responder, links[0], message)
        return

    await responder.send(f"📺 New video posted in <#{message.channel.id}> — writing it up.")
    async with bot.run_lock:
        await _execute_run(
            bot, responder, links, len(links), "scheduled", force=False,
            # Already in hand, so the run doesn't spend another minute asking
            # YouTube for what was just fetched.
            transcript_text=ready if len(links) == 1 else None,
        )


async def _hold_for_captions(bot, responder: Responder, link: str, message) -> None:
    """Say once that the video is early, and let the retry loop have it."""
    queue = await asyncio.to_thread(waiting.Queue.load)
    title = ""
    for embed in getattr(message, "embeds", []) or []:
        if isinstance(getattr(embed, "title", None), str):
            title = embed.title
            break
    item = queue.add(link, title=title, channel_id=responder.channel_id)
    if item.tries > 1:
        return
    await responder.send(
        f"📺 New video posted in <#{message.channel.id}> — no captions on it yet, "
        f"which is normal for a fresh upload. I'll write it up as soon as YouTube "
        f"publishes them."
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

            if request.action == "weekends":
                await _set_weekends(responder, config, request.brief or "")
                return

            if request.action == "filesop":
                await _file_sop(responder, config, message, request.brief or "")
                return

            if request.action == "probe":
                await _probe_update(responder, config)
                return

            if request.action == "rearrange":
                await _rearrange(responder, config, include_today=request.today)
                return

            if request.action == "publish":
                await _publish_now(responder, config, request.brief or "")
                return

            if request.action == "host":
                await _host_images(responder, config, message)
                return

            if request.action == "payment":
                await _payment_link(responder, config, request.brief or "")
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

            if request.action == "segments":
                await _send_segments(
                    responder, config, request.source, message, named=request.brief or ""
                )
                return

            if request.action == "sweep":
                await responder.send("Checking Zoom and Fathom for new calls…")
                before = await asyncio.to_thread(jobs.new_recordings, config)
                if not before:
                    await responder.send("Nothing new — everything recent is already filed.")
                    return
                await _file_new_recordings(bot)
                return

            if request.action == "index":
                await _index_library(responder, config)
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

            if request.action == "agents":
                await _file_agents(responder, config)
                return

            if request.action == "setups":
                await _send_wrong_setups(responder, config)
                return

            if request.action == "comment":
                await _comment_on_card(responder, config, request.brief or "")
                return

            if request.action == "unspread":
                await _unspread(responder, config, request.brief or "")
                return

            if request.action == "spread":
                await _spread_setup(responder, config, request.brief or "")
                return

            if request.action == "archive":
                await _archive_aged(responder, config)
                return

            if request.action == "unticked":
                await _send_unticked(responder, config)
                return

            if request.action == "move":
                await _move_cards(responder, config, request.brief or "")
                return

            if request.action == "levinson":
                await _levinson_report(bot, responder, config, request.brief or "")
                return

            if request.action == "rollover":
                await _rollover(responder, config, named=request.brief or "")
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
            include_today=request.today,
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

# How often the waiting list is looked at. The wait itself is set in
# `waiting`; this only decides how promptly a due one is noticed.
CAPTION_CHECK_SECONDS = 300

# How often the board's clock is looked at. The steps are on the hour, so a
# check every few minutes is plenty and a missed one is caught by the next.
BOARD_CHECK_SECONDS = 240


async def board_loop(bot: "WilByteBot") -> None:
    """Walk the daily board through its day: 6am, 9am, 6pm, 8:30pm.

    In Que to Today, Today to Quality Check, then whatever is still unticked
    onto tomorrow's cards - which Zapier has already made and left in In Que -
    and last, the cards themselves out of Quality Check into Done.
    """
    while not bot.is_closed():
        try:
            await _board_steps(bot)
        except asyncio.CancelledError:
            raise
        except Exception:  # a bad tick must not take the loop down for good
            log.exception("Board check failed; will try again shortly")
        await asyncio.sleep(BOARD_CHECK_SECONDS)


async def _board_steps(bot: "WilByteBot") -> None:
    from .. import boardclock, dailyops

    config = bot.config
    today = _today(config)
    due = dailyops.steps_due(
        datetime.now(ZoneInfo(config.schedule.timezone)),
        await asyncio.to_thread(boardclock.done_on, today),
    )
    for step in due:
        await _board_step(bot, step, today)


# Enough of them to see who is outstanding, not so many that the card is
# scrolled past. An embed description caps at 4096 characters anyway, and a
# list quietly cut short reads as if that was all of them.
UNMARKED_SHOWN = 15


def _unmarked_ping(config: Config, *, ping: bool = True) -> str:
    """Who to tap on the shoulder, as message text rather than embed text.

    Outside the embed on purpose: Discord renders a mention inside one but
    does not notify anybody, so a ping in there is a ping that never arrives.

    This one is a job for a person rather than a report of what RYTE did, and
    a card in a channel nobody has open is not a job anybody does.
    """
    who = config.secrets.discord_notify_user_id if ping else None
    return f"<@{who}>" if who else ""


def _unmarked_card(found: list[dict], *, step: str = ""):
    """The look at Done, as something to read.

    The time is named only when the clock decided it. Somebody who just typed
    the command knows what time it is.
    """
    from .. import dailyops

    return embeds.unticked_agents(
        found, said_at=dailyops.said_at(step) if step else "", shown=UNMARKED_SHOWN
    )


async def _send_unticked(responder: Responder, config: Config) -> None:
    """The same look the afternoon takes, when somebody asks for it now."""
    from .. import dailyops

    try:
        found, problems = await asyncio.to_thread(jobs.unmarked_agents, config)
    except PIPELINE_ERRORS as exc:
        await responder.send(embed=embeds.error(f"Couldn't read the board\n{exc}"))
        return

    if problems:
        await responder.send(embed=embeds.error("\n".join(problems)))
        return
    if not found:
        await responder.send(
            f"Every New Agent card in {dailyops.DONE} has been ticked."
        )
        return
    # No ping: somebody just asked, so they are already looking at it.
    await responder.send(embed=_unmarked_card(found))


async def _comment_on_card(responder: Responder, config: Config, said: str) -> None:
    """Say something on one of the day's four cards."""
    from .. import dailyops

    text, kind, day = dailyops.comment_target(said, today=_today(config))
    if not kind:
        await responder.send(
            "Say which card and I'll post it — `@RYTE comment on monday "
            "general card <what to say>`, or put the card at the end instead. "
            "The four are general, ops, ads and lead order; a weekday or a "
            "date picks the day, otherwise it's today's."
        )
        return
    if not text:
        await responder.send("Give me something to say on it.")
        return

    try:
        title, url, problems = await asyncio.to_thread(
            jobs.comment_on_daily, config, kind=kind, day=day, text=text
        )
    except PIPELINE_ERRORS as exc:
        await responder.send(embed=embeds.error(f"Couldn't reach the board\n{exc}"))
        return

    if problems:
        await responder.send(embed=embeds.error("\n".join(problems)))
        return
    await responder.send(f"Said it on **{title}** — <{url}>")


async def _unspread(responder: Responder, config: Config, said: str) -> None:
    """List, and on a second word remove, the setup-card lines on a Lead Order card."""
    from .. import dailyops

    day = dailyops.day_named(said, today=_today(config))
    sure = "confirm" in (said or "").lower()

    try:
        found, problems = await asyncio.to_thread(
            jobs.unspread_lead_order, config, day=day, dry=not sure
        )
    except PIPELINE_ERRORS as exc:
        await responder.send(embed=embeds.error(f"Couldn't read the board\n{exc}"))
        return

    if problems:
        await responder.send(embed=embeds.error("\n".join(problems)))
    if not found:
        await responder.send(
            "Nothing on that Lead Order card came off a setup card."
        )
        return

    head, *lines = found
    listed = "\n".join(f"· {line}" for line in lines)
    if sure:
        await responder.send(f"Took {len(lines)} line(s) off {head}:\n{listed}")
        return
    await responder.send(
        f"{len(lines)} line(s) on {head} came off a setup card:\n{listed}\n\n"
        "-# Say it again with **confirm** on the end and I'll take them off."
    )


async def _spread_setup(responder: Responder, config: Config, said: str) -> None:
    """Put the setup card's agents onto the Lead Order card, on request."""
    from .. import dailyops

    day = dailyops.day_named(said, today=_today(config))
    try:
        added, problems = await asyncio.to_thread(
            jobs.spread_to_lead_order, config, day=day
        )
    except PIPELINE_ERRORS as exc:
        await responder.send(embed=embeds.error(f"Couldn't read the board\n{exc}"))
        return

    if added:
        head, *lines = added
        await responder.send(
            f"Put {len(lines)} agent(s) on the Lead Order card.\n{head}\n"
            + "\n".join(f"· {line}" for line in lines)
        )
    elif not problems:
        await responder.send(
            "Every agent on the setup card is already on the Lead Order card."
        )
    if problems:
        await responder.send(embed=embeds.error("\n".join(problems)))


async def _send_wrong_setups(responder: Responder, config: Config) -> None:
    """The same look the watcher takes, when somebody asks for it now."""
    try:
        found, problems = await asyncio.to_thread(jobs.wrong_setups, config)
    except PIPELINE_ERRORS as exc:
        await responder.send(embed=embeds.error(f"Couldn't read the board\n{exc}"))
        return

    if problems:
        await responder.send(embed=embeds.error("\n".join(problems)))
        return
    if not found:
        await responder.send(
            "Every agent going live today or tomorrow is set up on what they ordered."
        )
        return
    # No ping: somebody just asked, so they are already looking at it.
    await responder.send(embed=embeds.wrong_setups(found, shown=UNMARKED_SHOWN))


async def _archive_aged(responder: Responder, config: Config) -> None:
    """Show what the nightly archive would take, then take it once approved."""
    from .. import dailyops

    try:
        cards, problems = await asyncio.to_thread(jobs.aged_to_archive, config)
    except PIPELINE_ERRORS as exc:
        await responder.send(embed=embeds.error(f"Couldn't read the board\n{exc}"))
        return

    if problems:
        await responder.send(embed=embeds.error("\n".join(problems)))
        return
    if not cards:
        await responder.send(
            f"Nothing to archive — no card in {dailyops.AGED_DONE} is ticked yet."
        )
        return

    view = views.ConfirmView(
        requester_id=responder.requester_id,
        timeout=config.discord.approval_timeout_seconds,
        label=f"Archive {len(cards)} card(s)",
        emoji="📦",
    )
    listed = "\n".join(f"• {card.get('name')}" for card in cards[:UNMARKED_SHOWN])
    if len(cards) > UNMARKED_SHOWN:
        listed += f"\n…and {len(cards) - UNMARKED_SHOWN} more."
    await responder.send(
        f"**{dailyops.AGED_DONE}** — ticked, so they would be archived:\n{listed}\n"
        "Archived, not deleted. They can be brought back.",
        view=view,
    )
    await view.wait()
    if not view.confirmed:
        return

    try:
        gone, problems = await asyncio.to_thread(jobs.archive_aged, config)
    except PIPELINE_ERRORS as exc:
        await responder.send(embed=embeds.error(f"Couldn't archive them\n{exc}"))
        return

    note = f"📦 Archived {len(gone)} card(s) from {dailyops.AGED_DONE}."
    if problems:
        note += "\n⚠ " + "\n⚠ ".join(problems)
    await responder.send(note)


async def _board_step(bot: "WilByteBot", step: str, today) -> None:
    """Do one step and say what happened, then never do it again today."""
    from .. import boardclock, dailyops

    responder = _board_responder(bot)
    card = None
    try:
        if step == "make_setup":
            title, problems = await asyncio.to_thread(jobs.make_setup_card, bot.config)
            # Silent when one already existed. Most mornings it makes one, and
            # a line every day saying nothing happened is a line nobody reads.
            note = f"📋 {dailyops.said_at(step)} — made `{title}`." if title else ""
        elif step in dailyops.UNMARKED:
            found, problems = await asyncio.to_thread(jobs.unmarked_agents, bot.config)
            # Nothing outstanding says nothing at all. A card every afternoon
            # reporting that all is well is how the one that matters stops
            # being looked at.
            note = _unmarked_ping(bot.config) if found else ""
            card = _unmarked_card(found, step=step) if found else None
        elif step == "link_setup":
            added, problems = await asyncio.to_thread(jobs.link_setup_on_day, bot.config)
            note = (
                f"🔗 {dailyops.said_at(step)} — put the setup card on "
                f"{len(added)} checklist(s)."
            ) if added else ""
        elif step == "archive_aged":
            gone, problems = await asyncio.to_thread(jobs.archive_aged, bot.config)
            note = (
                f"📦 {dailyops.said_at(step)} — archived {len(gone)} card(s) from "
                f"{dailyops.AGED_DONE}."
            ) if gone else ""
        elif step == "to_lead_order":
            added, problems = await asyncio.to_thread(
                jobs.spread_to_lead_order, bot.config
            )
            note = (
                f"📋 {dailyops.said_at(step)} — put {len(added) - 1} setup-card "
                f"agent(s) on the Lead Order card.\n" + "\n".join(added)
            ) if added else ""
        elif step in ("rollover", dailyops.LATE_ROLLOVER):
            late = step == dailyops.LATE_ROLLOVER
            # Both run on the same day now - half eight for General and Ops,
            # ten for Ads and Lead Order - so both read today's cards.
            when = None
            kinds = dailyops.LATE_KINDS if late else dailyops.EVENING_KINDS
            moved, problems, flagged = await asyncio.to_thread(
                partial(jobs.run_rollover, bot.config, day=when, only=kinds)
            )
            note = (
                f"📋 {dailyops.said_at(step)} — carried {moved} "
                f"unfinished {'Ads and Lead Order ' if late else ''}item(s) "
                "onto the next day's cards."
            )
            if flagged:
                note += "\n" + "\n".join(
                    f"⚠ {item.person}: {item.name[:60]} — " + _why_flagged(item)
                    for item in flagged
                )
        elif step == dailyops.LATE_DONE:
            # Ten o'clock, same day: the cards being finished are today's.
            moved, problems = await asyncio.to_thread(
                partial(jobs.walk_board, bot.config, step)
            )
            note = (
                f"📋 {dailyops.said_at(step)} — moved {moved} card(s) "
                f"{dailyops.STEP_NAMES[step]}."
            ) if moved else ""
        else:
            moved, problems = await asyncio.to_thread(jobs.walk_board, bot.config, step)
            # Nothing to move is not news either. Somebody already did it by
            # hand, which is the usual reason.
            note = (
                f"📋 {dailyops.said_at(step)} — moved {moved} card(s) "
                f"{dailyops.STEP_NAMES[step]}."
            ) if moved else ""
    except PIPELINE_ERRORS as exc:
        # Not marked done: the next tick tries again, which is right for a
        # board sitting in the wrong list.
        if responder:
            await responder.send(embed=embeds.error(f"Board step failed\n{exc}"))
        return

    # Marked before the message, because the step happened whether or not
    # Discord hears about it, and doing it twice is the worse mistake.
    await asyncio.to_thread(boardclock.mark, step, today)
    if problems:
        note = (note + "\n⚠ " + "\n⚠ ".join(problems)).lstrip("\n")
    # Each branch leaves both empty when its step had nothing to say. A line
    # every morning reporting that nothing happened is a line nobody reads.
    if responder and (note or card):
        await responder.send(note or None, embed=card)


def _why_flagged(item) -> str:
    """Why a carried-over item is worth a line of its own tonight.

    It moved, like everything else unticked. Saying how long it has been
    moving is the only way anybody finds out it has been waiting a week.
    """
    if item.looks_done:
        return "already Done but unticked"
    return f"carried {item.times_rolled} days running"


def _board_responder(bot: "WilByteBot"):
    """Where the board's own messages go, if anywhere."""
    configured = bot.config.secrets.discord_board_channel_id
    channel = bot.get_channel(int(configured)) if configured else _post_channel(bot)
    return ChannelResponder(channel) if channel is not None else None

# Terminal colour codes out of a subprocess's error output. yt-dlp writes them
# even when nothing is a terminal, and they arrive in Discord as "[0;31mERROR"
# in the middle of the sentence somebody is trying to read.
_ANSI = re.compile(r"\x1b\[[0-9;]*m|\[[0-9];[0-9]{2}m")


def _readable(exc) -> str:
    """An error with the terminal escape codes taken out of it."""
    return " ".join(_ANSI.sub("", str(exc)).split())


async def _wait_for_captions(responder: Responder, video, output_dir) -> None:
    """Put a video on the waiting list and say so, once."""
    queue = await asyncio.to_thread(waiting.Queue.load)
    item = queue.add(
        video.url or video.short_url,
        title=getattr(video, "title", "") or "",
        channel_id=responder.channel_id,
    )
    if item.tries > 1:
        return
    await responder.send(
        f"⏳ **{video.title or video.short_url}** has no captions yet — YouTube "
        f"usually takes a while on a fresh upload. I'll keep checking and write "
        f"it up as soon as they appear."
    )


async def caption_loop(bot: "WilByteBot") -> None:
    """Retry the videos that were announced before YouTube had captioned them.

    The whole point is that nobody has to remember. A video that was early
    when it was announced is written up when it stops being early, without
    anybody pasting the link back in.
    """
    while not bot.is_closed():
        try:
            await _retry_waiting(bot)
        except asyncio.CancelledError:
            raise
        except Exception:  # a bad tick must not take the loop down for good
            log.exception("Caption check failed; will try again shortly")
        await asyncio.sleep(CAPTION_CHECK_SECONDS)


async def _retry_waiting(bot: "WilByteBot") -> None:
    queue = await asyncio.to_thread(waiting.Queue.load)
    if not queue.items:
        return

    for item in queue.expired():
        queue.drop(item.url)
        channel = bot.get_channel(item.channel_id) if item.channel_id else _post_channel(bot)
        if channel is not None:
            await ChannelResponder(channel).send(
                f"⏳ Gave up waiting on **{item.title or item.url}** — still no "
                f"captions after {waiting.GIVE_UP_AFTER.total_seconds() // 3600:.0f} "
                f"hours. Attach a transcript as a .txt with the link and I'll "
                f"write it:\n```\n@RYTE {item.url}\n```"
            )

    due = queue.due()
    if not due or bot.run_lock.locked():
        return

    item = due[0]
    channel = bot.get_channel(item.channel_id) if item.channel_id else _post_channel(bot)
    if channel is None:
        return

    ready, problem = await asyncio.to_thread(jobs.waiting_on_captions, item.url)
    if not ready:
        if waiting.not_ready_yet(problem):
            # Note the try and keep the original first_seen, or the six-hour
            # limit resets every time and this waits for ever.
            queue.add(item.url, title=item.title, channel_id=item.channel_id)
            return
        # Something else is wrong with it now, and that is worth saying.
        queue.drop(item.url)
        await ChannelResponder(channel).send(
            embed=embeds.error(f"{item.title or item.url}\n{problem}")
        )
        return

    # Off the list before the run, not after: a run that ends in a review card
    # sitting unanswered must not be started again five minutes later.
    queue.drop(item.url)
    responder = ChannelResponder(channel)
    await responder.send(
        f"📺 Captions are up on **{item.title or item.url}** — writing it up now."
    )
    async with bot.run_lock:
        await _execute_run(
            bot, responder, [item.url], 1, "scheduled", force=False, transcript_text=ready
        )


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


async def _attached_cues(message) -> list | None:
    """Timed captions attached to the mention, if there are any.

    The escape hatch for when YouTube won't serve captions to the machine RYTE
    runs on. It has to be a `.vtt` or `.srt` here, unlike the blog pipeline: a
    transcript pasted as plain text has no timings in it, and timings are the
    whole point of cutting a video up.
    """
    for attachment in getattr(message, "attachments", []) or []:
        if not attachment.filename.lower().endswith((".vtt", ".srt")):
            continue
        if attachment.size and attachment.size > MAX_LEARN_BYTES:
            continue
        raw = (await attachment.read()).decode("utf-8", errors="replace")
        cues = youtube.parse_timed_captions(raw)
        if cues:
            return cues
    return None


async def _payment_link(responder: Responder, config: Config, said: str) -> None:
    """Make a Stripe payment link for what somebody asked for, once they say so.

    Nothing is created before the confirm: the amount comes from a message
    typed in a hurry, and a wrong one reaches a client as a quote. What the
    button confirms is the exact line the client will read.
    """
    from .. import products, stripepay

    if not stripepay.configured():
        await responder.send(
            "Stripe isn't set up — `STRIPE_API_KEY` is blank in .env. It wants "
            "a restricted key with payment links, products and prices."
        )
        return

    amount = products.amount_asked(said)
    if amount is None:
        await responder.send(
            "How much? Say it with the amount — "
            "`@RYTE payment link $621 for 40 basic spanish leads`."
        )
        return
    if amount <= 0:
        await responder.send("That amount isn't something I can charge for.")
        return

    product = products.find(said)
    if product is None:
        near = products.matches(said)
        if near:
            await responder.send(
                "Which one? " + ", ".join(item.name for item in near[:5])
            )
        else:
            await responder.send(
                "I don't know which package that is. The ones I have: "
                + ", ".join(item.name for item in products.CATALOGUE[:6])
                + ", and nine more — say one of those."
            )
        return

    cents = stripepay.as_cents(amount)
    line = products.titled(products.line_for(said, product))

    # Looked up before the confirm, so the button can say whether this makes a
    # link or hands back one that already exists.
    try:
        existing = await asyncio.to_thread(_existing_link, product, cents)
    except stripepay.StripeError as exc:
        await responder.send(embed=embeds.error(str(exc)))
        return

    where = "" if stripepay.live() else "\n-# Test mode — this link takes no money."
    if existing:
        await responder.send(
            f"**{stripepay.dollars(cents)}** — {line}\n"
            f"There's already a link for this: {existing.get('url')}{where}"
        )
        return

    view = views.ConfirmView(
        requester_id=responder.requester_id,
        timeout=config.discord.approval_timeout_seconds,
        label="Make the link",
        emoji="💳",
    )
    await responder.send(
        f"**{stripepay.dollars(cents)}** — {line}{where}", view=view
    )
    await view.wait()
    if not view.confirmed:
        return

    try:
        made = await asyncio.to_thread(
            partial(_make_payment_link, product, cents, line)
        )
    except stripepay.StripeError as exc:
        await responder.send(embed=embeds.error(str(exc)))
        return

    await responder.send(f"{line} — {stripepay.dollars(cents)}\n{made.get('url')}")


def _existing_link(product, cents: int):
    """The link already selling this package at this price, if there is one."""
    from .. import stripepay

    found = stripepay.find_product(product.name)
    if not found:
        return None
    price = stripepay.find_price(str(found.get("id") or ""), cents)
    if not price:
        return None
    return stripepay.find_link(str(price.get("id") or ""))


def _make_payment_link(product, cents: int, line: str):
    """Product, price, link - each one found before it is made."""
    from .. import stripepay

    made = stripepay.ensure_product(product.name, product.description)
    price = stripepay.ensure_price(str(made.get("id") or ""), cents)
    return stripepay.make_link(str(price.get("id") or ""), note=line)


async def _send_segments(
    responder: Responder, config: Config, source: str | None, message, *, named: str = ""
) -> None:
    """Cut one interview into clips and post them, ready to paste."""
    from .. import recordings
    from .. import segments as segmenting

    cues = await _attached_cues(message)
    title = ""

    # Replying to whoever posted the recording is the natural way to ask for
    # this, and it is the way filing already accepts.
    said = message.content or ""
    if not source:
        replied = await _replied_to(message)
        if replied is not None:
            said = f"{said}\n{replied.content or ''}"
            call = recordings.find_recording(replied.content or "")
            links = mentions.find_sources(replied.content or "")
            source = call.url if call else (links[0] if links else None)

    # A name is the way round a Zoom share link: the web interface and the API
    # hand out different tokens for the same recording, so the link can never
    # be matched - but the topic carries the guest's name on both sides.
    wanted = mentions.ANY_URL_RE.sub(" ", named).strip(" -–—:,")

    if cues is None and not source and not wanted:
        await responder.send(
            "Give me the recording — `@RYTE segment <zoom, fathom or youtube "
            "link>`, or `@RYTE segment <the guest's name>`, or reply to the "
            "message that has the link. You can also attach the `.vtt`."
        )
        return

    link = ""
    passcode = ""
    searched = ""
    if cues is None:
        found = recordings.find_recording(source) if source else None
        try:
            if found is None and not source:
                await responder.send(f"Looking for a recording named “{wanted}”…")
                cues, title, link, passcode = await asyncio.to_thread(
                    jobs.timed_call_by_name, config, wanted
                )
                # What was typed beats what Zoom calls it. Half these topics
                # are "Strategy Session" with the guest's name nowhere in them.
                searched = wanted
            elif found is not None and found.platform in ("Zoom", "Fathom"):
                # A Zoom share link is a door with a passcode on it, and the
                # passcode is how the call gets identified when the link can't.
                found.passcode = recordings.find_passcode(said)
                await responder.send(f"Looking that {found.platform} recording up…")
                cues, title, link, passcode = await asyncio.to_thread(
                    jobs.timed_call_transcript, config, found
                )
            else:
                video = await asyncio.to_thread(youtube.video_from_link, source)
                title = video.title
                await responder.send(
                    f"Reading the transcript for **{title or video.video_id}**…"
                )
                cues = await asyncio.to_thread(
                    youtube.fetch_timed_transcript, video.video_id
                )
        except (youtube.IngestError, segmenting.SegmentError) as exc:
            await responder.send(embed=embeds.error(str(exc)))
            # The evidence goes in its own message rather than the embed: it is
            # a list of what Zoom actually returned, and it is what tells a
            # recording RYTE can't see from one it can't recognise.
            detail = getattr(exc, "detail", "")
            if detail:
                await responder.send(detail)
            return
        except PIPELINE_ERRORS as exc:
            await responder.send(embed=embeds.error(f"Couldn't read that recording: {exc}"))
            return

    runs = youtube.length_of(cues[-1].end)
    await responder.send(f"Got {len(cues)} lines running to {runs}. Cutting it up…")

    try:
        payload, keep, short = await asyncio.to_thread(
            segmenting.generate_segments,
            cues,
            config,
            title=title,
            url=source or "",
        )
    except segmenting.SegmentError as exc:
        await responder.send(embed=embeds.error(str(exc)))
        return

    summary = segmenting.opening(payload, kept=len(keep), short=short)
    await responder.send(summary)
    # One message per segment, in plain text rather than an embed: these get
    # copied straight into the doc, and an embed is not selectable that way.
    # Quiet, because every description ends in four Agent Lead Lab links and
    # four preview cards under each clip bury the text being copied.
    for segment in keep:
        await responder.send(segment.as_text(), quiet=True)

    await _file_interview(
        responder, config, keep,
        topic=searched or title,
        link=link or source or "",
        passcode=passcode,
    )


async def _file_interview(
    responder: Responder, config: Config, keep, *, topic: str, link: str, passcode: str
) -> None:
    """Put the cut-up interview on the board as a card in Marketing Department."""
    from .. import segments as segmenting

    name = segmenting.card_title(topic)
    description = segmenting.as_card(keep, link=link, passcode=passcode)

    try:
        url, problems = await asyncio.to_thread(
            jobs.file_interview, config, name=name, description=description
        )
    except PIPELINE_ERRORS as exc:
        await responder.send(embed=embeds.error(f"Couldn't make the board card: {exc}"))
        return

    for problem in problems:
        await responder.send(f"⚠ {problem}")
    if not url:
        return

    await responder.send(f"Filed as **{name}** in Marketing Department — <{url}>")
    # Zoom only returns a typed passcode on some recordings, and a card with a
    # link nobody can open is worth one line rather than silence. Fathom links
    # have no passcode at all, so saying so there is noise about a setting that
    # doesn't exist.
    if segmenting.wants_a_passcode(link, passcode):
        await responder.send(
            "-# Zoom didn't give me a passcode for that one — add it to the card "
            "if the link needs one."
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


async def catch_up_sops(bot: WilByteBot) -> None:
    """File what was posted while RYTE was off, without being asked.

    The Mac gets turned off at the end of the day and things get posted over a
    weekend. Remembering to say `backfill` on Monday is exactly the kind of
    step this was built to remove - and the message ids mean running it is
    always safe, whether or not anything was actually missed.
    """
    await asyncio.sleep(45)  # let the gateway settle and the caches fill
    channel = _first_sop_channel(bot)
    if channel is None:
        return
    try:
        filed = await _file_channel_history(bot, channel)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Catch-up on the SOP channel failed")
        return
    if filed:
        log.info("Caught up %s SOP(s) posted while RYTE was off", len(filed))


async def _backfill_sops(bot: WilByteBot, responder: Responder, message) -> None:
    """File what was posted in the SOP channel before RYTE was watching it."""
    channel = message.channel
    if not is_sop_channel(message, bot.config):
        channel = _first_sop_channel(bot)
        if channel is None:
            await responder.send(
                "I don't have an SOP channel set, so there's nothing to backfill."
            )
            return

    await responder.send(f"Reading back through {channel.mention} — this takes a minute.")

    filed, skipped, problems, seen = await _file_channel_history(bot, channel, counted=True)

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


async def _file_channel_history(bot: WilByteBot, channel, *, counted: bool = False):
    """Walk a channel oldest-first and file what isn't filed yet.

    Oldest first so the library ends up in the order things happened. Anything
    already recorded is passed over, which is what makes running this twice -
    or on every start-up - cost nothing but a read.
    """
    from .. import sops

    filed: list[str] = []
    skipped = 0
    problems: list[str] = []
    seen = 0

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
            log.warning("Couldn't read %s: %s", sop.title, exc)
            sop.note = sop.note or f"No summary — {exc}"
        try:
            title, _ = await asyncio.to_thread(jobs.file_sop, bot.config, sop, summary=summary)
        except PIPELINE_ERRORS as exc:
            problems.append(f"{sop.title}: {jobs._short(exc)}")
            continue

        sops.remember(old.id)
        filed.append(title)
        try:
            await old.add_reaction(SOP_FILED_REACTION)
        except discord.HTTPException:
            pass

    return (filed, skipped, problems, seen) if counted else filed


def _first_sop_channel(bot: WilByteBot):
    for raw in bot.config.secrets.discord_sop_channel_ids:
        try:
            found = bot.get_channel(int(raw))
        except (TypeError, ValueError):
            continue
        if found is not None:
            return found
    return None


async def _index_library(responder: Responder, config: Config) -> None:
    """Read the old SOP page once, so questions can be answered on it too."""
    page_id = config.secrets.notion_library_page_id or config.secrets.notion_sop_page_id
    if not page_id:
        await responder.send("I don't have a library page set, so there's nothing to index.")
        return

    await responder.send("Reading the SOP library — this takes a few minutes the first time.")
    try:
        indexed, already, remaining = await asyncio.to_thread(
            jobs.index_library, config, page_id
        )
    except PIPELINE_ERRORS as exc:
        await responder.send(embed=embeds.error(f"Couldn't read the library\n{exc}"))
        return

    lines = [f"📚 Read {indexed} page(s)."]
    if already:
        lines.append(f"-# {already} already indexed — skipped.")
    if remaining:
        lines.append(f"-# {remaining} still to go. Say `index` again to carry on.")
    if not indexed and not already:
        lines.append(
            "-# Nothing found. The page has to be shared with RYTE — "
            "open it, `⋯ → Connections`, add Ryte."
        )
    await responder.send("\n".join(lines))


async def _send_sops(responder: Responder, config: Config, asked: str) -> None:
    """Answer "do we have an SOP for X" out of the Notion library."""
    from .. import sops

    topic = sops.wanted_topic(asked)
    try:
        found = await asyncio.to_thread(jobs.find_sops, config, topic)
    except PIPELINE_ERRORS as exc:
        await responder.send(embed=embeds.error(f"Couldn't read the SOP library\n{exc}"))
        return

    # The pages written before RYTE existed are still the answer half the time,
    # and they are read from an index rather than from Notion - so this costs
    # nothing and can't fail.
    seen = {hit.title for hit in found}
    for hit in sops.index_matches(sops.load_index(), topic):
        if hit.title not in seen:
            found.append(hit)

    if not found:
        await responder.send(
            f"Nothing in the SOP library for “{topic}” yet." if topic
            else "The SOP library is empty so far."
        )
        return

    # One side of the library can answer the question outright while the other
    # only comes close. When anything answers it, the near misses are noise.
    if any(hit.exact for hit in found):
        found = [hit for hit in found if hit.exact]
        head = f"{len(found)} SOP(s) for “{topic}”:" if topic else "The most recent:"
    else:
        head = f"Nothing exact for “{topic}” - closest I have:"

    lines = [f"📘 **{hit.title}**\n{hit.link or hit.card}" for hit in found]
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


async def _set_weekends(responder: Responder, config: Config, text: str) -> None:
    """Turn Saturday and Sunday on or off, and offer to re-lay the calendar.

    Widening the week does nothing on its own - everything already booked is
    still sitting on the weekdays it was given, and the new days go by empty.
    So the offer to rearrange comes with the change rather than being something
    to remember afterwards.
    """
    wanted = mentions.weekend_switch(text)
    if wanted is None:
        await responder.send(
            f"{prefs.describe_days(config)}\n"
            "Change it with `@RYTE weekends on` or `@RYTE weekends off`."
        )
        return

    await asyncio.to_thread(prefs.set_weekends, wanted)
    config = prefs.apply(config)
    await responder.send(
        "📅 Weekends are **on** — the blog can go out any day now."
        if wanted else
        "📅 Weekends are **off** — weekdays only again."
    )
    await _rearrange(
        responder, config, offer=True, include_today=mentions.wants_today(text)
    )


async def _file_sop(responder: Responder, config: Config, message, text: str) -> None:
    """File an SOP handed over from outside the SOP channel.

    Posting in #sop files silently, because a channel of procedures is not a
    place for RYTE to announce that it noticed one. Being asked directly is
    the opposite: somebody said do this, so they get told it is done and
    where it went.

    The command words are stripped before the message is read, or "add to sop"
    becomes the title of the card.
    """
    from .. import sops

    images, audio = message_files(message)
    sop = sops.find_sop(text, images=images, audio=audio)
    if sop is None:
        await responder.send(
            "There's nothing in that to file — give me a link, a file, or the "
            "steps written out."
        )
        return

    sop.posted_by = getattr(message.author, "display_name", "") or ""
    posted = getattr(message, "created_at", None)
    sop.posted_on = posted.date() if posted is not None else None

    summary = ""
    try:
        summary = await asyncio.to_thread(jobs.sop_summary, config, sop)
    except PIPELINE_ERRORS as exc:
        log.warning("Couldn't read the SOP %s: %s", sop.title, exc)
        sop.note = sop.note or f"No summary — {exc}"

    try:
        title, url = await asyncio.to_thread(jobs.file_sop, config, sop, summary=summary)
    except PIPELINE_ERRORS as exc:
        await responder.send(embed=embeds.error(f"Couldn't file that SOP\n{exc}"))
        return

    sops.remember(getattr(message, "id", ""))
    await responder.send(f"{jobs.SOP_ICON} Filed **{title}**\n{url}")


async def _file_agents(responder: Responder, config: Config, *, silent: bool = False) -> None:
    """File the new agents waiting in In Que.

    Asked for by hand it shows the plan and waits. Run by the watcher it does
    it and says what it did - a card can land at any minute of the day, and a
    button nobody is sitting next to is a card that stays in In Que.
    """
    from .. import agents as rules

    try:
        plans, where, missing = await asyncio.to_thread(jobs.read_agents, config)
    except PIPELINE_ERRORS as exc:
        if not silent:
            await responder.send(embed=embeds.error(f"Couldn't read the board\n{exc}"))
        return

    if missing:
        await responder.send(embed=embeds.error("\n".join(missing)))
        return
    if not plans:
        if not silent:
            await responder.send("No new agents waiting in In Que.")
        return

    doable = [plan for plan in plans if plan.doable]
    stuck = [plan for plan in plans if not plan.doable]

    if not silent:
        view = None
        if doable:
            view = views.ConfirmView(
                requester_id=responder.requester_id,
                timeout=config.discord.approval_timeout_seconds,
                label=f"File {len(doable)} agent(s)",
                emoji="🧾",
            )
        await responder.send(rules.describe(plans), view=view)
        if view is None:
            return
        await view.wait()
        if not view.confirmed:
            return
    elif not doable:
        # Nothing to do and nobody asked. The ones that need a person are
        # said once, by the watcher, and then left alone.
        await _report_stuck(responder, stuck)
        return

    try:
        filed, problems = await asyncio.to_thread(jobs.apply_agents, config, doable, where)
    except PIPELINE_ERRORS as exc:
        await responder.send(embed=embeds.error(f"Couldn't file them\n{exc}"))
        return

    note = f"🧾 Filed {filed} agent(s)."
    if silent and filed:
        note += "\n" + rules.describe(doable)
    if problems:
        note += "\n⚠ " + "\n⚠ ".join(problems)
    if filed or problems:
        await responder.send(note)
    if silent:
        await _report_stuck(responder, stuck)


# Launches worth being reminded about: today, tomorrow, or a card with no
# date on it at all, which could be any of the three. A launch further out
# than that is said once and left - chasing it today buys nothing.
NAG_ABOUT = ("today", "tomorrow", "unknown")


def _why_waiting(plan) -> str:
    """What is holding this card up, in the one line that gets posted.

    Not every card that stops short has something wrong with it. One waiting
    in Franklin's list for a setup card that hasn't been made yet has no
    problems at all, and a bare name after a dash reads like a complaint
    nobody wrote down.
    """
    if plan.problems:
        return "; ".join(plan.problems)
    if plan.agent.launch is not None:
        return f"nothing to put them on yet — no {plan.agent.launch:%a %b %d} setup card"
    return "nothing to put them on yet"


async def _report_stuck(responder: Responder, stuck) -> None:
    """Name a card that needs a person - not every twenty seconds, but again
    every few hours while it is still sitting there.

    Said once, it lands while everyone is at lunch and the card waits all
    afternoon. Said every pass, nobody reads the channel by Wednesday.

    Only the ones with a launch on top of us are repeated. An agent going
    live next week is named once; when the week turns and the launch is
    tomorrow, it joins the every-few-hours list on its own.
    """
    from .. import agentseen

    seen = await asyncio.to_thread(agentseen.load)
    now = time.time()
    wanted: set[str] = set()
    for plan in stuck:
        every = (
            agentseen.SAY_AGAIN_AFTER if plan.when in NAG_ABOUT else agentseen.ONLY_ONCE
        )
        wanted.update(
            agentseen.due([plan.agent.card_id], held=seen, every=every, now=now)
        )
    fresh = [plan for plan in stuck if plan.agent.card_id in wanted]
    if not fresh:
        return
    lines = [
        f"• **{plan.agent.name}** — {_why_waiting(plan)}\n  {plan.agent.url}"
        for plan in fresh
    ]
    await responder.send("🧾 Waiting on somebody:\n" + "\n".join(lines))
    await asyncio.to_thread(
        agentseen.remember, [plan.agent.card_id for plan in fresh]
    )


# As close to instant as polling gets. A card lands whenever a client signs
# and somebody is watching the board for it to be picked up, so the wait is
# the whole experience of this. Trello allows 100 requests every 10 seconds
# per token and a pass costs about sixteen, so twenty seconds is nowhere near
# the ceiling - the real floor is how long a pass takes to run.
AGENT_CHECK_SECONDS = 20


SETUP_CHECK_SECONDS = 600


async def setup_check_loop(bot: "WilByteBot") -> None:
    """Watch for agents set up on leads they did not order.

    Ten minutes rather than twenty seconds: the confirmation comment lands
    hours after the card is filed, so there is nothing to gain from looking
    more often, and this one reads every list on the board.
    """
    from .. import setupseen

    while not bot.is_closed():
        try:
            responder = _board_responder(bot)
            if responder is not None:
                found, problems = await asyncio.to_thread(jobs.wrong_setups, bot.config)
                marks = [
                    setupseen.mark(str(c.get("id") or ""), c["ordered"], c["setup"])
                    for c in found
                ]
                said = await asyncio.to_thread(setupseen.load)
                fresh = [c for c, held in zip(found, marks) if held not in said]
                if fresh:
                    await responder.send(
                        _unmarked_ping(bot.config) or None,
                        embed=embeds.wrong_setups(fresh, shown=UNMARKED_SHOWN),
                    )
                    await asyncio.to_thread(setupseen.remember, marks)
                if problems:
                    log.warning("Setup check: %s", "; ".join(problems))
        except asyncio.CancelledError:
            raise
        except Exception:  # a bad tick must not take the loop down for good
            log.exception("Setup check failed; will try again shortly")
        await asyncio.sleep(SETUP_CHECK_SECONDS)


async def agent_loop(bot: "WilByteBot") -> None:
    """Watch In Que for new agents, all day.

    A card lands whenever a client signs, which is not on any schedule, so
    this is the one board job that cannot wait for somebody to ask.
    """
    while not bot.is_closed():
        try:
            responder = _board_responder(bot)
            if responder is not None:
                await _file_agents(responder, bot.config, silent=True)
        except asyncio.CancelledError:
            raise
        except Exception:  # a bad tick must not take the loop down for good
            log.exception("Agent check failed; will try again shortly")
        await asyncio.sleep(AGENT_CHECK_SECONDS)


async def _move_cards(responder: Responder, config: Config, named: str) -> None:
    """Walk today's cards from one list to the next, once approved.

    "Move done" means both Done steps. The clock splits them - half eight for
    General and Ops, ten for Ads and Lead Order - because that is when each
    one's work stops, but somebody typing this is asking for the cards to be
    put away, not for step seven of the walk. Asking for it and being told
    nothing was waiting, while two cards sat in Quality Check, is the command
    failing at the only thing it is for.
    """
    from .. import dailyops

    step = dailyops.move_named(named)
    if step is None:
        await responder.send(
            "Which move? `@RYTE trello move today`, `move quality check`, or `move done`."
        )
        return

    steps = dailyops.DONE_STEPS if step in dailyops.DONE_STEPS else (step,)
    where = dailyops.STEP_NAMES[step]
    cards: list[str] = []
    try:
        for one in steps:
            found, problems = await asyncio.to_thread(
                partial(jobs.moves_waiting, config, one)
            )
            if problems:
                await responder.send(embed=embeds.error("\n".join(problems)))
                return
            cards.extend(found)
    except PIPELINE_ERRORS as exc:
        await responder.send(embed=embeds.error(f"Couldn't read the board\n{exc}"))
        return

    if not cards:
        await responder.send(
            f"Nothing to move {where} — today's cards aren't sitting in "
            f"{dailyops.STEP_LISTS[step][0]}."
        )
        return

    view = views.ConfirmView(
        requester_id=responder.requester_id,
        timeout=config.discord.approval_timeout_seconds,
        label=f"Move {len(cards)} card(s)",
        emoji="📋",
    )
    listed = "\n".join(f"• {name}" for name in cards)
    await responder.send(f"**{where}**\n{listed}", view=view)
    await view.wait()
    if not view.confirmed:
        return

    moved, problems = 0, []
    try:
        for one in steps:
            went, trouble = await asyncio.to_thread(jobs.walk_board, config, one)
            moved += went
            problems.extend(trouble)
    except PIPELINE_ERRORS as exc:
        await responder.send(embed=embeds.error(f"Couldn't move them\n{exc}"))
        return

    note = f"📋 Moved {moved} card(s) {where}."
    if problems:
        note += "\n⚠ " + "\n⚠ ".join(problems)
    await responder.send(note)


async def _hold_back(responder: Responder, config: Config, asked) -> None:
    """"rollover skip ads" - keep a card's items off tomorrow's card tonight.

    Recorded rather than run. Dragging the card into another list does not
    stop the carry, because the rollover finds the day's cards by the date in
    the title wherever they are - so saying no has to be an instruction, and
    one that still holds when eight o'clock comes round.
    """
    from .. import dailyops, rollskip

    doing, kinds = asked
    day = await asyncio.to_thread(jobs.board_day, config)

    if doing == "hold" and not kinds:
        held = await asyncio.to_thread(rollskip.for_day, day)
        await responder.send(
            _held_as_words(held)
            + "\nName one: `trello rollover skip ads`, or `general`, `ops`, "
            "`lead order`."
        )
        return

    if doing == "hold":
        held = await asyncio.to_thread(rollskip.hold, day, kinds)
    else:
        held = await asyncio.to_thread(
            rollskip.release, day, kinds if kinds else None
        )
    await responder.send(
        f"📋 {_held_as_words(held)} Everything else carries as usual at "
        f"{dailyops.said_at('rollover')}."
    )


def _held_as_words(held) -> str:
    from .. import dailyops

    if not held:
        return "Nothing is being held back tonight."
    named = ", ".join(dailyops.CARD_KINDS.get(kind, kind) for kind in held)
    whose = "its" if len(held) == 1 else "their"
    return f"Holding back tonight: **{named}** — {whose} unticked items stay put."


async def _rollover(responder: Responder, config: Config, *, named: str = "") -> None:
    """Move today's unfinished items onto tomorrow's cards, once approved.

    The board is the team's day. A rollover that guesses wrong scatters
    somebody's unfinished work across the wrong checklists, and unlike a wrong
    blog date nobody sees it happen - the item just quietly isn't where they
    left it. So the plan is shown and the button is the decision.
    """
    from .. import dailyops, rollskip

    asked = dailyops.skip_asked(named)
    if asked is not None:
        await _hold_back(responder, config, asked)
        return

    only = dailyops.kind_named(named)
    which = dailyops.CARD_KINDS.get(only, "") if only else ""
    today = await asyncio.to_thread(jobs.board_day, config)
    # "rollover yesterday" - the carry always works from a day to the day
    # after it, so running it today reads today's cards and leaves last
    # night's held-back ones exactly where they are.
    asked_for = dailyops.day_named(named, today=today)
    day = asked_for or today
    # A hold was "not on the automatic run". Asking for that day by hand is
    # asking for it anyway, and honouring the hold here would make last
    # night's skip impossible to undo the morning after.
    skip = [] if asked_for else None

    await responder.send(
        f"Reading {which or 'the board'}"
        + (f" for {day:%a %b %d}" if asked_for else "")
        + " — nothing will move yet."
    )
    try:
        plans, missing, targets = await asyncio.to_thread(
            partial(jobs.read_rollover, config, only=only, day=day, skip=skip)
        )
    except PIPELINE_ERRORS as exc:
        await responder.send(embed=embeds.error(f"Couldn't read the board\n{exc}"))
        return

    report = dailyops.summarise(plans, missing=missing)
    if missing:
        report += (
            f"\n⚠ No card for {dailyops.next_day(day):%m/%d/%y} yet: "
            f"{', '.join(missing)}"
        )

    movable = sum(len(plan.carried) for plan in plans)
    if not movable:
        await responder.send(report)
        return

    if which:
        report = f"**{which} only** — the other cards are untouched.\n{report}"

    view = views.ConfirmView(
        requester_id=responder.requester_id,
        timeout=config.discord.approval_timeout_seconds,
        label=f"Trello rollover — {movable} item(s)"[:80],
        emoji="📋",
    )
    await responder.send(report, view=view)
    await view.wait()
    if not view.confirmed:
        return

    try:
        moved, problems = await asyncio.to_thread(
            partial(jobs.apply_rollover, config, plans, targets, day=day)
        )
    except PIPELINE_ERRORS as exc:
        await responder.send(embed=embeds.error(f"Couldn't move them\n{exc}"))
        return

    note = f"📋 Carried {moved} item(s) onto tomorrow's cards."
    flagged = [item for plan in plans for item in plan.needs_a_look]
    if flagged:
        note += (
            f"\n{len(flagged)} of them have been carried for days — "
            f"they're marked in the list above."
        )
    if problems:
        note += "\n⚠ Couldn't move:\n" + "\n".join(f"• {line}" for line in problems)
    await responder.send(note)


# How far back to read the payment channel. A month of payments is a few
# hundred messages; this is the ceiling that stops a first run walking two
# years of history and rate-limiting the bot on start-up.
PAYMENT_SCAN = 4000


async def _payments_in(bot: "WilByteBot", year: int, month: int) -> tuple[list, str]:
    """Every Payra notification in that month. (payments, problem).

    Read from the channel rather than remembered, because the channel is the
    record. Running the report twice costs a read and produces the same answer.
    """
    from .. import levinson

    where = bot.config.secrets.discord_payment_channel_id
    if not where:
        return [], "DISCORD_PAYMENT_CHANNEL_ID isn't set in .env."
    channel = bot.get_channel(int(where))
    if channel is None:
        return [], (
            f"I can't see channel {where}. Add RYTE to it with View Channel "
            "and Read Message History."
        )

    zone = ZoneInfo(bot.config.schedule.timezone)
    since = datetime(year, month, 1, tzinfo=zone)
    until = datetime(year + (month == 12), (month % 12) + 1, 1, tzinfo=zone)

    found = []
    try:
        async for old in channel.history(limit=PAYMENT_SCAN, after=since, before=until):
            paid = levinson.read_payment(_all_text(old), paid_at=old.created_at.astimezone(zone))
            if paid is not None:
                found.append(paid)
    except discord.Forbidden:
        return [], (
            f"RYTE can see #{getattr(channel, 'name', where)} but can't read its "
            "history. Give it Read Message History."
        )
    return found, ""


def _all_text(message) -> str:
    """A message and its embeds as one blob.

    Payra's notification is an embed, and which part of one carries the fields
    is up to whoever built the automation - title, description, or named
    fields. Reading all of it as text means a change of shape at their end
    doesn't stop the report at ours.
    """
    parts = [message.content or ""]
    for embed in getattr(message, "embeds", None) or []:
        parts.extend(
            str(bit) for bit in (embed.title, embed.description) if bit
        )
        for field in getattr(embed, "fields", None) or []:
            parts.append(f"{field.name}: {field.value}")
        footer = getattr(embed, "footer", None)
        if footer is not None and getattr(footer, "text", None):
            parts.append(str(footer.text))
    return "\n".join(part for part in parts if part)


async def _levinson_report(
    bot: "WilByteBot", responder: Responder, config: Config, said: str
) -> None:
    """Who Levinson sent us and what they paid, for one month.

    Read and shown before it is written. The sheet goes to an agency partner
    as a statement of what they are owed, so the numbers get looked at by a
    person once before they land on it.
    """
    from .. import levinson

    today = await asyncio.to_thread(jobs.board_day, config)
    asked = levinson.month_named(said, today=today)
    if asked is None:
        await responder.send(
            "Which month? `@RYTE levinson`, `levinson last month`, or "
            "`levinson august`."
        )
        return
    year, month = asked
    label = levinson.tab_for(year, month)

    await responder.send(f"Reading {label} — nothing will be written yet.")

    payments, problem = await _payments_in(bot, year, month)
    if problem:
        await responder.send(embed=embeds.error(problem))
        return

    members, notes = await asyncio.to_thread(jobs.levinson_members, config)
    lines = levinson.lines_for(payments, members)

    head = (
        f"**Levinson — {label}**\n"
        f"{len(payments)} payment(s) in the channel, "
        f"{len(lines)} from Levinson agents, {levinson.total(lines)} total."
    )
    if notes:
        head += "\n" + "\n".join(f"⚠ {note}" for note in notes)
    if not lines:
        await responder.send(head)
        return

    listed = "\n".join(
        f"• {line.paid_on:%b %d} — **{line.name}** — {line.amount}"
        + (f" — {line.product}" if line.product else "")
        for line in lines[:40]
    )
    if len(lines) > 40:
        listed += f"\n-# and {len(lines) - 40} more"

    view = views.ConfirmView(
        requester_id=responder.requester_id,
        timeout=config.discord.approval_timeout_seconds,
        label=f"Add {len(lines)} to the sheet",
        emoji="📗",
    )
    await responder.send(f"{head}\n{listed}", view=view)
    await view.wait()
    if not view.confirmed:
        return

    written, problems = await asyncio.to_thread(jobs.write_levinson, config, lines)
    note = (
        f"📗 Added {written} row(s) to the tracker."
        if written else
        "📗 Nothing new — every one of those is already on the sheet."
    )
    if problems:
        note += "\n⚠ " + "\n⚠ ".join(problems)
    await responder.send(note)


async def _probe_update(responder: Responder, config: Config) -> None:
    """Ask GHL what it will accept on an update, on a post nobody can see."""
    await responder.send("Making a throwaway draft and trying a few update shapes…")
    try:
        lines = await asyncio.to_thread(jobs.probe_update, config)
    except PIPELINE_ERRORS as exc:
        await responder.send(embed=embeds.error(f"The probe itself failed\n{exc}"))
        return
    await responder.send("\n".join(lines))


async def _rearrange(
    responder: Responder, config: Config, *, offer: bool = False, include_today: bool = False
) -> None:
    """Pull everything already booked onto the earliest slots now available.

    Read-only until the button is pressed. These are live scheduled posts, and
    a wrong date here means an article going out on a day nobody expected -
    which is not something anybody would notice until it had happened.
    """
    from ..rearrange import explain_failures, summarise

    ledger = await asyncio.to_thread(Ledger.load)
    context = await _maybe_open_ghl(config)
    try:
        try:
            moves = await asyncio.to_thread(
                partial(
                    jobs.reschedule_plan, config, ledger,
                    context=context, include_today=include_today,
                )
            )
        except PIPELINE_ERRORS as exc:
            await responder.send(embed=embeds.error(f"Couldn't work out a new schedule\n{exc}"))
            return

        changing = [move for move in moves if move.moved]
        if not changing:
            # After a change of days this is good news, not an answer worth
            # sending: it means nothing needed pulling forward.
            if not offer:
                await responder.send(summarise(moves))
            return

        view = views.ConfirmView(
            requester_id=responder.requester_id,
            timeout=config.discord.approval_timeout_seconds,
            label="Move them",
            emoji="📅",
        )
        await responder.send(summarise(moves), view=view)
        await view.wait()
        if not view.confirmed:
            return

        if context is None:
            await responder.send(
                embed=embeds.error("No GHL credentials, so there's nothing to move them in.")
            )
            return

        try:
            problems = await asyncio.to_thread(jobs.apply_moves, config, ledger, context, moves)
        except PIPELINE_ERRORS as exc:
            await responder.send(embed=embeds.error(f"Couldn't move them\n{exc}"))
            return

        done = len(changing) - len(problems)
        note = f"📅 Moved {done} post(s)."
        if problems:
            note += "\n" + explain_failures(problems)
        await responder.send(note)
    finally:
        if context:
            await asyncio.to_thread(context.close)


async def _publish_now(responder: Responder, config: Config, text: str) -> None:
    """Send a held post out today rather than on the day it was booked for."""
    asked = (text or "").strip()
    if not asked:
        await responder.send(
            "Which one? `@RYTE publish monday` — the day it's currently booked for."
        )
        return

    try:
        day = prefs.parse_day(asked, today=_today(config))
    except prefs.PrefsError as exc:
        await responder.send(str(exc))
        return

    ledger = await asyncio.to_thread(Ledger.load)
    held = await asyncio.to_thread(jobs.held_on, config, ledger, day)
    if not held:
        await responder.send(f"Nothing booked for {day:%a %b %d} that I'm holding.")
        return
    if len(held) > 1:
        titles = "\n".join(f"• {e.title or e.url_slug}" for e in held)
        await responder.send(f"{day:%a %b %d} has more than one post:\n{titles}")
        return

    entry = held[0]
    if not jobs.publishable(entry):
        why = "I never got its post id from GHL" if not entry.ghl_post_id else (
            "I have no saved copy of its body, so re-sending it would empty the post"
        )
        await responder.send(f"Can't publish **{entry.title}** — {why}. Do that one by hand.")
        return

    view = views.ConfirmView(
        requester_id=responder.requester_id,
        timeout=config.discord.approval_timeout_seconds,
        label="Publish now",
        emoji="🚀",
    )
    await responder.send(
        f"**{entry.title}**\nBooked for {day:%a %b %d}. Publishing now puts it live "
        f"immediately and frees that day.",
        view=view,
    )
    await view.wait()
    if not view.confirmed:
        return

    context = await _maybe_open_ghl(config)
    if context is None:
        await responder.send(embed=embeds.error("No GHL credentials, so I can't publish."))
        return
    try:
        await asyncio.to_thread(jobs.publish_now, config, ledger, context, entry)
    except PIPELINE_ERRORS as exc:
        await responder.send(embed=embeds.error(f"Couldn't publish it\n{exc}"))
        return
    finally:
        await asyncio.to_thread(context.close)

    await responder.send(f"🚀 **{entry.title}** is live.")
    # Its day is free now, so whatever was queued behind it can come forward.
    await _rearrange(responder, config, offer=True)


def _today(config: Config):
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo

    return _dt.now(ZoneInfo(config.schedule.timezone)).date()


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
    include_today: bool = False,
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
        slot_pool = await asyncio.to_thread(
            partial(
                jobs.plan_slots, videos, context, config, ledger,
                include_today=include_today,
            )
        )
        # Say what was left out. Ten links in and eight posts back looks like a
        # bug unless the reason is on screen.
        trimmed = max(0, len(sources) - len(videos) - already_done)
        await responder.send(
            f"On it — building {len(videos)} post(s) in **{mode}** mode."
            + (
                f" First one goes out today at {slot_pool[0]:%-I:%M %p}."
                if include_today and slot_pool and slot_pool[0].date() == _today(config)
                else ""
            )
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
                # A video announced the minute it goes up has no captions yet.
                # Nothing is wrong; it is early. Failing it makes writing the
                # post somebody's job to remember, which means it doesn't get
                # written.
                if waiting.not_ready_yet(str(exc)):
                    await _wait_for_captions(responder, video, output_dir)
                    skipped += 1
                    continue
                failed += 1
                unfinished.append(video)
                await responder.send(
                    embed=embeds.error(f"{video.title or video.short_url}\n{_readable(exc)}")
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


# How long to wait before starting again when Discord drops the connection in
# a way discord.py cannot recover from. Their gateway answered 503 one evening,
# the library tried to reconnect to a socket it never had, and RYTE was dead
# until somebody noticed in the morning - a board that walks itself has to
# survive the other side having a bad minute.
RESTART_PAUSES = (5, 15, 30, 60, 120)


def starts_again(exc: BaseException) -> bool:
    """Whether this is worth starting again for, or worth stopping over.

    Anything that looks like the network or Discord is worth another go. What
    is not: a token Discord refused, an intent the portal has switched off, or
    somebody pressing Ctrl-C. Those do not improve by being tried again, and
    looping on them hides the message that says what to fix.
    """
    if isinstance(exc, (discord.LoginFailure, discord.PrivilegedIntentsRequired)):
        return False
    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
        return False
    return True


def run_bot(config: Config | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    try:
        config = config or load_config()
    except ConfigError as exc:
        log.error("Could not load configuration: %s", exc)
        raise SystemExit(1)

    lost = 0
    missing = preflight(config)
    if missing:
        log.error(
            "Cannot start without %s. Set it in your host's environment variables "
            "(on Railway: the service's Variables tab), then redeploy.",
            " and ".join(missing),
        )
        raise SystemExit(1)

    while True:
        try:
            build_bot(config).run(config.secrets.discord_bot_token, log_handler=None)
            return
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
        except KeyboardInterrupt:
            return
        except Exception as exc:  # noqa: BLE001 - narrowed by `starts_again`
            if not starts_again(exc):
                raise
            wait = RESTART_PAUSES[min(lost, len(RESTART_PAUSES) - 1)]
            lost += 1
            log.warning(
                "Lost Discord — %s: %s. Starting again in %ss.",
                type(exc).__name__, exc, wait,
            )
            time.sleep(wait)
