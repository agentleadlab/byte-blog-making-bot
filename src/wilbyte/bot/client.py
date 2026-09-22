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
from datetime import date, datetime, timedelta, timezone
from contextlib import asynccontextmanager
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
    if (
        _id_list(os.getenv("DISCORD_WATCH_CHANNEL_IDS"))
        or _id_list(os.getenv("DISCORD_SOP_CHANNEL_IDS"))
        # Payra's payments arrive as another bot's embed in a channel that
        # never mentions RYTE. Without this the message turns up with its
        # fields blank, which reads exactly like a month with no sales.
        or _id_list(os.getenv("DISCORD_PAYMENT_CHANNEL_ID"))
        # And the chargeback notices are the same shape - "@here" and an
        # embed, from another bot. This one worked only because the payment
        # channel happened to be set, which is not a thing to rely on.
        or _id_list(os.getenv("DISCORD_DISPUTE_CHANNEL_ID"))
    ):
        intents.message_content = True

    # Members, for the agents' own server. Finding somebody by name is the
    # whole of removing them, and the member list is not sent without this.
    #
    # Asked for only when that server is named, because asking for a
    # privileged intent the portal has not granted does not degrade - Discord
    # refuses the login outright, and RYTE stops doing everything else too.
    if os.getenv("DISCORD_CLIENTS_GUILD_ID", "").strip():
        intents.members = True
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
        self.day_task: asyncio.Task | None = None
        self.tags_task: asyncio.Task | None = None
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
        # And the same switch again for lines on a Lead Order card for a day
        # the agent is not live: it is the same question asked of the other
        # end of the same journey, and it only ever reports.
        if self.config.secrets.trello_agents_auto and (
            self.day_task is None or self.day_task.done()
        ):
            self.day_task = self.loop.create_task(day_check_loop(self))
        # A comment is somebody handing over a job, which happens all day and
        # on nobody's schedule. Its own switch, because it writes onto four
        # people's live checklists rather than reporting.
        if self.config.secrets.trello_tags_auto and (
            self.tags_task is None or self.tags_task.done()
        ):
            self.tags_task = self.loop.create_task(tags_loop(self))
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
            # Somebody is waiting on this one. discord.py logs an exception in
            # an event handler and says nothing, so a bug leaves them looking
            # at "Reading the board —" with no second message ever coming, and
            # no way to tell that from RYTE being slow.
            try:
                await handle_mention(self, message)
            except Exception:
                log.exception("That mention broke something")
                try:
                    await message.reply(
                        "Something broke while I was doing that — the details are "
                        "in the terminal window. Have a look at the board before "
                        "running it again, in case it stopped part way.",
                        mention_author=False,
                    )
                except Exception:  # Discord unreachable as well; the log has it
                    log.exception("...and I couldn't say so either")
            return
        # Somebody telling the room to file what was just said, without
        # telling RYTE. He offers; he never files it on his own - nobody
        # addressed him, and writing to the board off a conversation he was
        # not in is not something to do without a button.
        where = mentions.put_on_board(message.content or "")
        if where is not None:
            try:
                await _offer_to_file_it(self, message, where)
            except Exception:
                log.exception("Couldn't offer to put that on the board")
            return
        if is_dispute(message, self.config):
            await handle_dispute(self, message)
            return
        if is_payment(message, self.config):
            await handle_payment(self, message)
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


def is_payment(message, config: Config) -> bool:
    """True for a message in the channel Payra announces payments in.

    Not filtered by author, for the same reason a watched channel isn't: the
    payments are posted by another bot, and somebody chose that channel for
    exactly this.
    """
    where = config.secrets.discord_payment_channel_id
    channel = getattr(message, "channel", None)
    return bool(where) and str(getattr(channel, "id", "")) == str(where)


def is_dispute(message, config: Config) -> bool:
    """True for a message in the channel chargeback notifications land in.

    Not filtered by author, the same as the payments channel: the acquirer's
    notices arrive through something else's account, and somebody chose that
    channel for exactly this.
    """
    where = config.secrets.discord_dispute_channel_id
    channel = getattr(message, "channel", None)
    return bool(where) and str(getattr(channel, "id", "")) == str(where)


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


@asynccontextmanager
async def _typing(channel):
    """Show "RYTE is typing…" if Discord will have it, and work either way.

    The indicator is decoration. Discord's own API has bad minutes - on the
    morning of the 15th it answered `send_typing` with a 500 behind a
    Cloudflare challenge page - and `channel.typing()` raises that out of
    `__aenter__`, which took the whole of `trello tags` with it before a
    single card had been read. Franklin got "something broke" for a request
    that never started.

    So the indicator is entered on its own and its failure is a log line.
    Nothing decorative is allowed to stand between a person asking for
    something and it happening. The same reasoning as `RESTART_PAUSES`: the
    other side is allowed to have a bad minute, and RYTE is not allowed to
    fall over when it does.
    """
    showing, stopping = None, None
    try:
        showing = channel.typing()
        # Bounded, because a rate-limited send_typing does not raise - the
        # library sleeps until the bucket clears, and an indicator nobody can
        # see is not worth waiting on. This is the half that made RYTE go
        # silent rather than merely undecorated.
        await asyncio.wait_for(showing.__aenter__(), TYPING_STARTS_IN)
    except Exception:
        log.info("Couldn't show the typing indicator; carrying on", exc_info=True)
        showing = None

    if showing is not None:
        stopping = asyncio.create_task(_stop_typing_later(showing))
    try:
        yield
    finally:
        if stopping is not None:
            stopping.cancel()
        if showing is not None:
            await _stop_typing(showing)


#: Longest to wait for Discord to start the indicator. It is decoration, and
#: waiting on it is the opposite of what it is for.
TYPING_STARTS_IN = 5.0

#: And the longest to leave it running. Work takes seconds; a button waits up
#: to `approval_timeout_minutes`, which is 720 - twelve hours. The indicator
#: used to run for the whole of that, re-sending every five seconds, which is
#: eight thousand requests on one channel for one button nobody pressed. That
#: is what rate-limited the channel and took the next mention down with it.
TYPING_AT_MOST = 60.0


async def _stop_typing_later(showing) -> None:
    """Take the indicator down once the work is clearly not seconds away."""
    try:
        await asyncio.sleep(TYPING_AT_MOST)
    except asyncio.CancelledError:
        return
    await _stop_typing(showing)


async def _stop_typing(showing) -> None:
    """Stop it, and never let stopping it be the thing that fails."""
    try:
        await asyncio.wait_for(
            showing.__aexit__(None, None, None), TYPING_STARTS_IN
        )
    except Exception:
        log.debug("Couldn't stop the typing indicator", exc_info=True)


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

    # Replying to a command and saying nothing but "@RYTE" means run that
    # again. Somebody who pasted a dispute notice with six screenshots on it
    # is not going to paste it a second time, and the message RYTE gets on a
    # reply carries none of it - so the one being answered is where to look.
    # Only for a bare mention: a reply that says something is asking for that
    # something, not for a repeat.
    if request.action == "help" and not mentions.said_anything(message.content):
        replied = await _replied_to(message)
        if replied is not None and (replied.content or "").strip():
            again = mentions.parse(
                replied.content, max_batch=config.discord.max_batch
            )
            if again.action != "help":
                request = again
                message = replied

    if request.action == "help":
        # The version goes on the help text specifically, because this is the
        # message you get when RYTE doesn't recognise a word - and "that word
        # is new, this copy is old" is the most likely reason why.
        await responder.send(f"{mentions.HELP_TEXT}\n\n-# Running `{version.code_version()}`")
        return

    async with _typing(message.channel):
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
                await _comment_on_card(
                    responder, config, request.brief or "", message,
                )
                return

            if request.action == "unspread":
                await _unspread(responder, config, request.brief or "")
                return

            if request.action == "spread":
                await _spread_setup(responder, config, request.brief or "")
                return

            if request.action == "clearout":
                await _clear_out(bot, responder, config, request.brief or "")
                return

            if request.action == "blacklist":
                await _blacklist_them(responder, config, request.brief or "")
                return

            if request.action == "golive":
                await _who_goes_live(responder, config, request.brief or "")
                return

            if request.action == "quiet":
                await _quiet_channels(bot, responder, config, request.brief or "")
                return

            if request.action == "access":
                await _what_i_can_do(responder, bot)
                return

            if request.action == "daycheck":
                await _wrong_days(responder, config)
                return

            if request.action == "tags":
                await _tagged_tasks(responder, config, request.brief or "")
                return

            if request.action == "noticed":
                await _what_i_noticed(responder, config, request.brief or "")
                return

            if request.action == "rebuttal":
                await _rebuttal(responder, config, message, request.brief or "")
                return

            if request.action == "archive":
                await _archive_aged(responder, config)
                return

            if request.action == "agentsheet":
                await _send_sheet(responder, config, request.brief or "")
                return

            if request.action == "whenlive":
                await _when_live(responder, config, request.brief or "")
                return

            if request.action == "words":
                await _lead_words(responder, request.brief or "")
                return

            if request.action == "unticked":
                await _send_unticked(responder, config, request.brief or "")
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


def _unmarked_card(found: list[dict], *, step: str = "", days: str = ""):
    """The look at Done, as something to read.

    The time is named only when the clock decided it. Somebody who just typed
    the command knows what time it is.
    """
    from .. import dailyops

    return embeds.unticked_agents(
        found, said_at=dailyops.said_at(step) if step else "",
        days=days, shown=UNMARKED_SHOWN,
    )


# "words STNDRD = standard", or "words STNDRD means standard". The word is
# whatever is on the left, so a phrase works as well as a word.
_TAUGHT = re.compile(
    r"^\s*(?P<word>.+?)\s*(?:=|:|\bmeans\b|\bis\b)\s*(?P<means>.+?)\s*$",
    re.IGNORECASE,
)


def _said_sheet(one: dict) -> str:
    """One agent's setup sheet, or why there isn't one to give."""
    name = str(one.get("agent") or "?")
    link = str(one.get("url") or one.get("shortUrl") or "")
    said = f"[**{name}**]({link})" if link else f"**{name}**"
    sheets = list(one.get("sheets") or [])
    if not sheets:
        return f"{said} — no sheet link on the card yet, so the setup isn't finished."

    # The last one: a setup gets redone and each round leaves its own link, so
    # the newest is the sheet the leads are actually flowing into.
    label, url = sheets[-1]
    line = f"{said} — [{label or 'setup sheet'}]({url})"
    if len(sheets) > 1:
        line += f"\n  *(the newest of {len(sheets)} — the setup was redone)*"
    return line


async def _send_sheet(responder: Responder, config: Config, said: str) -> None:
    """Answer "sheet for Faith" with the Google Sheet off her card."""
    try:
        found, problems = await asyncio.to_thread(jobs.agent_sheet, config, said)
    except PIPELINE_ERRORS as exc:
        await responder.send(embed=embeds.error(f"Couldn't read the board\n{exc}"))
        return

    if problems:
        await responder.send("\n".join(problems))
        return
    if len(found) == 1:
        await responder.send(_said_sheet(found[0]))
        return
    await responder.send(
        f"{len(found)} agents match that:\n"
        + "\n".join(f"• {_said_sheet(one)}" for one in found[:8])
        + (f"\n…and {len(found) - 8} more." if len(found) > 8 else "")
    )


def _said_launch(one: dict, today) -> str:
    """One agent's launch, in the tense the day it falls on deserves."""
    launch = one.get("launch")
    name = str(one.get("agent") or "?")
    link = str(one.get("url") or one.get("shortUrl") or "")
    said = f"[**{name}**]({link})" if link else f"**{name}**"
    if launch is None:
        return f"{said} — I can't find a launch date on the card."
    if launch < today:
        return f"{said} went live **{launch:%A, %B %-d}**."
    if launch == today:
        return f"{said} goes live **today** — {launch:%A, %B %-d}."
    days = (launch - today).days
    return (
        f"{said} goes live **{launch:%A, %B %-d}** — "
        + ("tomorrow." if days == 1 else f"in {days} days.")
    )


async def _when_live(responder: Responder, config: Config, said: str) -> None:
    """Answer "when did Faith go live", past or future.

    The board already holds the answer on every agent's card; until now it
    could only be got at by opening the card, and only if you knew which list
    it had ended up in.
    """
    try:
        found, problems = await asyncio.to_thread(jobs.agent_launch, config, said)
    except PIPELINE_ERRORS as exc:
        await responder.send(embed=embeds.error(f"Couldn't read the board\n{exc}"))
        return

    if problems:
        await responder.send("\n".join(problems))
        return

    today = _today(config)
    if len(found) == 1:
        await responder.send(_said_launch(found[0], today))
        return

    # More than one agent answers to the name, so all of them are given rather
    # than one of them guessed at.
    await responder.send(
        f"{len(found)} agents match that:\n"
        + "\n".join(f"• {_said_launch(one, today)}" for one in found[:8])
        + (f"\n…and {len(found) - 8} more." if len(found) > 8 else "")
    )


async def _lead_words(responder: Responder, said: str) -> None:
    """What RYTE has been taught about lead types, and teaching him more.

    The vocabulary is Franklin's, not the source's. Every product on this board
    gets written half a dozen ways, and each new spelling used to cost a code
    change - which meant the board waited on a developer to learn a word.
    """
    from .. import agents as rules, vocab

    # Only what follows the command word: "words STNDRD = standard".
    asked = re.sub(r"^\s*(?:trello\s+)?(?:words|word|vocab)\b", "", said or "", count=1,
                   flags=re.IGNORECASE).strip()

    held = await asyncio.to_thread(vocab.load)

    if not asked:
        await responder.send(vocab.describe(held))
        return

    forgetting = re.match(r"^(?:forget|drop|remove)\s+(?P<word>.+)$", asked, re.IGNORECASE)
    if forgetting:
        word = forgetting.group("word").strip()
        shorter = vocab.forget(word, held=held)
        if shorter == held:
            await responder.send(f"I wasn't taught “{word}”, so there's nothing to drop.")
            return
        await asyncio.to_thread(vocab.save, shorter)
        rules.taught(shorter)
        await responder.send(f"Forgotten — “{word}” goes back to meaning nothing.")
        return

    found = _TAUGHT.match(asked)
    if not found:
        await responder.send(
            "Tell me a word and what it means, like `@RYTE words STNDRD = standard`.\n"
            f"{vocab.choices()}"
        )
        return

    try:
        fuller = vocab.teach(found.group("word"), found.group("means"), held=held)
    except vocab.VocabError as exc:
        await responder.send(str(exc))
        return

    await asyncio.to_thread(vocab.save, fuller)
    rules.taught(fuller)

    word = vocab.tidy(found.group("word"))
    means = vocab.settle(found.group("means"))
    kind = vocab.kind_of(means)
    await responder.send(
        f"Got it — `{word}` is a {kind} meaning **{means}**. "
        f"I'll read it that way from now on."
    )


async def _send_unticked(responder: Responder, config: Config, said: str = "") -> None:
    """The same look the afternoon takes, when somebody asks for it now.

    A day can be named - "unticked yesterday" - and it used to be dropped on
    the floor: the answer came back about today either way, with nothing in it
    admitting that a different question had been asked.
    """
    from .. import dailyops

    # A named day is a question about that day. Only the afternoon check, which
    # nobody asked for and which is about what is running out of time, looks at
    # tomorrow as well.
    day = dailyops.day_named(said, today=_today(config))
    # A named day is that day; otherwise however far the chase reaches, which
    # is Monday on a Friday and tomorrow the rest of the week.
    covers = f"{day:%a %b %d}" if day else dailyops.days_chased(_today(config))
    try:
        found, problems = await asyncio.to_thread(
            partial(jobs.unmarked_agents, ahead=day is None),
            config, day=day,
        )
    except PIPELINE_ERRORS as exc:
        await responder.send(embed=embeds.error(f"Couldn't read the board\n{exc}"))
        return

    if problems:
        await responder.send(embed=embeds.error("\n".join(problems)))
        return
    if not found:
        await responder.send(
            f"Every New Agent card in {dailyops.DONE} has been ticked"
            + (f" for {covers}." if covers else ".")
        )
        return
    # No ping: somebody just asked, so they are already looking at it.
    await responder.send(embed=_unmarked_card(found, days=covers))
    await _offer_the_top_ups(responder, config, found)


async def _offer_the_top_ups(responder: Responder, config: Config, found) -> None:
    """Offer to tick the ones that were never a setup in the first place.

    "if you see this on the agent going live today and tomorrow and the card
    is unticked, tick it" - the comment being Therese's, saying the order is
    ongoing and Nicole should bump the leads on a drip already running. There
    is no setup on those to do and nobody is ever going to tick them, so they
    are chased every afternoon for work that was finished before the card was
    copied.

    A button rather than done quietly: a tick is somebody saying they did it,
    which is the whole value of the tick, and RYTE putting them on unasked
    would make the green circle mean less on every other card too. It comes
    off again if it is wrong.
    """
    from .. import agents

    try:
        theirs, problems = await asyncio.to_thread(
            partial(jobs.ongoing_to_tick, found=found), config
        )
    except PIPELINE_ERRORS as exc:
        await responder.send(embed=embeds.error(f"Couldn't read the comments\n{exc}"))
        return
    if problems:
        await responder.send(embed=embeds.error("\n".join(problems)))
    if not theirs:
        return

    view = views.ConfirmView(
        requester_id=responder.requester_id,
        timeout=config.discord.approval_timeout_seconds,
        label=f"Tick {len(theirs)} ongoing order" + ("s" if len(theirs) != 1 else ""),
        emoji="✅",
    )
    lines = [
        f"• **{agents.agent_name(str(one.get('name') or ''))}** — live {one.get('when')}"
        f"\n-# {one.get('because') or ''}"
        for one in theirs
    ]
    await responder.send(
        f"{len(theirs)} of those "
        + ("is a top-up" if len(theirs) == 1 else "are top-ups")
        + ", not a setup — the comment says the order is already running:\n"
        + "\n".join(lines),
        view=view,
    )
    await view.wait()
    if not view.confirmed:
        return

    try:
        ticked, trouble = await asyncio.to_thread(jobs.tick_ongoing, config, theirs)
    except PIPELINE_ERRORS as exc:
        await responder.send(embed=embeds.error(f"Couldn't tick them\n{exc}"))
        return
    said = [f"✅ Ticked **{one}**" for one in ticked]
    said += [f"⚠ {one}" for one in trouble]
    await responder.send("\n".join(said) or "Nothing was ticked.")


async def _offer_to_file_it(bot: "WilByteBot", message, where: str) -> None:
    """Offer to put the message above onto the board. Never does it unasked.

    "@Therese get Ryan hernandez truckers live... hold off on blue collar till
    EOD / Put on trello" - the instruction is to the room rather than to RYTE,
    and until now somebody then copied it across by hand.

    The one being filed is the message that instruction answers, or the one
    before it when it answers nothing. That is what "put on trello" means when
    it arrives on its own line.
    """
    from .. import dailyops

    said = await _replied_to(message) or await _the_one_before(message)
    if said is None or not (said.content or "").strip():
        return

    config = bot.config
    text = _as_somebody_said(said)
    kind = (dailyops.kinds_named(where) or [dailyops.FALLBACK_CARD])[0]
    day = _today(config)

    view = views.ConfirmView(
        requester_id=message.author.id,
        timeout=config.discord.approval_timeout_seconds,
        label="Put it on the board",
        emoji="📋",
    )
    shown = text if len(text) <= 300 else text[:300].rsplit(" ", 1)[0] + "…"
    note = (
        f"📋 On **{dailyops.CARD_KINDS.get(kind, kind)} {day:%m/%d/%y}**?\n> {shown}"
    )
    if not where:
        note += "\n-# Say `on ops`, `on ads` or `on lead order` for a different card."
    await message.reply(note, view=view, mention_author=False)
    await view.wait()
    if not view.confirmed:
        return

    try:
        title, url, problems = await asyncio.to_thread(
            jobs.comment_on_daily, config, kind=kind, day=day, text=text
        )
    except PIPELINE_ERRORS as exc:
        await message.reply(
            embed=embeds.error(f"Couldn't reach the board\n{exc}"),
            mention_author=False,
        )
        return
    if problems:
        await message.reply(
            embed=embeds.error("\n".join(problems)), mention_author=False,
        )
        return
    await message.reply(f"Said it on **{title}** — <{url}>", mention_author=False)


def _fuller_dispute(rules_doc, dispute, said: str, paid_with: str, older):
    """(dispute, text, payment record) off an older message, if it says more.

    Only when it genuinely fills more of the notice than what was typed, so a
    message that happens to be nearby cannot overwrite facts somebody gave.

    The reason code is the one thing the older message is least likely to have
    and the newer one most likely to: it lives in the ElevateQS portal rather
    than on the notice, so Franklin types it when he asks. Taking the fuller
    message must not throw away the half he added.
    """
    if not worth_reading(older):
        return None
    text = (getattr(older, "content", "") or "").strip()
    older_said, older_paid = rules_doc.split_payment(text)
    found = rules_doc.read_facts(older_said)
    if len(found.missing()) >= len(dispute.missing()):
        return None
    if not found.code:
        found.raw = f"{found.raw}\n{said}"
    return found, (older_said or said), (paid_with or older_paid)


def worth_reading(older) -> bool:
    """Whether an older message may be read for dispute facts at all.

    Anything a person said, yes. Of RYTE's own messages only the chargeback
    flag card, which is the notice restated - the rest is RYTE talking.

    This is not a tidiness rule. The message RYTE sends when a notice is short
    of facts contains a worked example of a dispute block: a MID, a dispute
    date, an ARN, a card number, a name and an email, all made up. The first
    rebuttal built after RYTE learned to read the channel took the email, the
    card and the dispute date off that example and printed them in the fact
    table of a document addressed to an acquirer - and argued, at length, that
    the cardholder had waited eleven days, counted from a date nobody had ever
    sent.
    """
    text = (getattr(older, "content", "") or "").strip()
    if not text or ASK_FOR_THE_BLOCK.splitlines()[0] in text:
        return False
    if getattr(getattr(older, "author", None), "bot", False):
        return text.startswith(OUR_FLAG)
    return True


#: Which fields identify a dispute. Two messages that carry any of these and
#: agree on all of them are about the same one.
#:
#: Not the card, the email or the MID, which are the fields being filled in
#: and are the ones written inconsistently: a notice says "Card Number (Last
#: 4): 7543" where a portal says "ending in 7543", and reading those as two
#: different disputes would stop the filling this exists to allow.
_IDENTIFYING = ("arn", "customer_name", "amount", "transaction_date")


def _as_written(said: str) -> str:
    """A field flattened for comparing: "$ 129.37" and "$129.37" are one sum."""
    return re.sub(r"[^a-z0-9]", "", str(said or "").casefold())


def same_dispute(dispute, found) -> bool:
    """Whether an older message is about the dispute being answered.

    At least one identifying field in common, and no identifying field they
    disagree on. A different customer, a different amount or a different ARN
    is a different dispute, and a channel that has held two of them will hold
    more.

    This is what makes it safe to look further back. Without it, widening the
    search to find Juliana Hernandez's email four dozen messages up would also
    let the dispute before hers fill in her card number.
    """
    agreed = 0
    for name in _IDENTIFYING:
        mine = _as_written(getattr(dispute, name, ""))
        theirs = _as_written(getattr(found, name, ""))
        if not mine or not theirs:
            continue
        if mine != theirs:
            return False
        agreed += 1
    return agreed > 0


def _fill_the_blanks(rules_doc, dispute, older) -> None:
    """Fill a dispute's empty fields from an older message. Never overwrite.

    The fact table at the top of the rebuttal is the acquirer's index to the
    case, and it prints only the rows it has. Juliana Hernandez's came out
    five rows long - no MID, no card, no email - because the facts were read
    off RYTE's own flag card, which does not carry them, and the notice that
    did was three lines further up.
    """
    if not worth_reading(older):
        return
    text = (getattr(older, "content", "") or "").strip()
    said, _ = rules_doc.split_payment(text)
    found = rules_doc.read_facts(said)
    if not same_dispute(dispute, found):
        return
    for name, _pattern in rules_doc.FIELDS:
        if not getattr(dispute, name, "") and getattr(found, name, ""):
            setattr(dispute, name, getattr(found, name))


async def _said_before(message, *, howmany: int = 100):
    """The messages above this one, newest first - RYTE's own included.

    Unlike `_the_one_before`, this one keeps RYTE's messages: the chargeback
    flag card is RYTE's, it carries every fact off the notice, and it is
    usually the last thing said before somebody asks for the rebuttal.

    A hundred rather than thirty, which is the same one request to Discord.
    Thirty did not reach back past an afternoon of RYTE's own output to the
    notice itself, and the rebuttal came out without the cardholder's email
    or her card. Reaching further is only safe because what is read back has
    to be about the same dispute.
    """
    try:
        async for older in message.channel.history(limit=howmany, before=message):
            yield older
    except Exception:  # no history permission, or Discord having a moment
        return


async def _the_one_before(message):
    """The message just above this one, from anybody but RYTE.

    "Put on trello" on its own line is about what was said a moment ago, and
    on a busy morning that is not always a reply.
    """
    try:
        async for older in message.channel.history(limit=6, before=message):
            if getattr(older, "author", None) is not None and getattr(
                older.author, "bot", False
            ):
                continue
            if (older.content or "").strip():
                return older
    except Exception:  # no history permission, or Discord having a moment
        return None
    return None


async def _comment_on_card(
    responder: Responder, config: Config, said: str, message=None
) -> None:
    """Say something on one of the day's four cards.

    Or say what somebody else said. A decision gets made in the channel and
    then has to be copied onto the board by hand - "@Therese get Ryan
    hernandez truckers live... hold off on blue collar till EOD / Put on
    trello" - so replying to that message with `@RYTE put on trello` posts
    the message itself, with whoever said it in front of it. The board is
    read by people who were not in the channel, and "Therese said" is half
    of what the line means.
    """
    from .. import dailyops

    text, kind, day = dailyops.comment_target(said, today=_today(config))

    # Nothing to say, but a message being answered. That is the one being
    # put on the board - the reply RYTE receives carries none of its words.
    guessed = False
    if not text and message is not None:
        replied = await _replied_to(message)
        if replied is not None and (replied.content or "").strip():
            text = _as_somebody_said(replied)
            if not kind:
                # Said rather than refused. General is where anything that is
                # not ops or ads work lives, and the message names the card it
                # landed on, so a wrong one is one line away from being right.
                kind, guessed = dailyops.FALLBACK_CARD, True

    if not kind:
        await responder.send(
            "Say which card and I'll post it — `@RYTE comment on monday "
            "general card <what to say>`, or put the card at the end instead. "
            "The four are general, ops, ads and lead order; a weekday or a "
            "date picks the day, otherwise it's today's."
        )
        return
    if not text:
        await responder.send(
            "Give me something to say on it — or reply to the message you "
            "want on the board and say `@RYTE put on trello`."
        )
        return

    try:
        title, url, problems = await asyncio.to_thread(
            jobs.comment_on_daily, config, kind=kind, day=day, text=text
        )
    except PIPELINE_ERRORS as exc:
        await responder.send(embed=embeds.error(f"Couldn't reach the board\n{exc}"))
        return

    # A url means it went on the board. Anything said alongside one is a
    # warning about part of it - a name nobody on the board answers to - and
    # reporting that as a failure would hide a comment that posted.
    if problems and not url:
        await responder.send(embed=embeds.error("\n".join(problems)))
        return
    note = f"Said it on **{title}** — <{url}>"
    if problems:
        note += "\n⚠ " + "\n⚠ ".join(problems)
    if guessed:
        note += "\n-# Nobody said which card. `on ops`, `on ads` or `on lead order` puts it there instead."
    await responder.send(note)


def _as_somebody_said(message) -> str:
    """One person's Discord message, ready to be a Trello comment.

    With their name in front of it. Every comment RYTE writes is signed by
    RYTE's own account, so without this the board says he decided to hold off
    on blue collar till EOD.
    """
    author = getattr(message, "author", None)
    who = (
        getattr(author, "display_name", None)
        or getattr(author, "name", "")
        or ""
    ).strip()
    said = " ".join((message.content or "").split())
    return f"{who}: {said}" if who else said


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
        added, conflicts, problems = await asyncio.to_thread(
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
    if conflicts:
        await responder.send(
            embed=embeds.spread_conflicts(conflicts, shown=UNMARKED_SHOWN)
        )
    if not added and not problems:
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


#: How many tagged lines to print before the message stops being readable.
TAGS_SHOWN = 25


#: A screenshot bigger than this is somebody's whole screen, not an exhibit.
MOST_EXHIBIT_BYTES = 20_000_000


#: What RYTE says when the notice is short of facts. One constant, because the
#: message has to be recognisable again later: it is full of labelled fields
#: with made-up values in them, and RYTE now reads the channel.
ASK_FOR_THE_BLOCK = (
    "Paste the block as it comes off the portal:\n"
    "```\n@RYTE rebuttal\nMID: 510200014664\n"
    "Dispute Date: 9/8/2026\nDispute Dollar Amount: $1,552.50\n"
    "Acquirer's Reference Number: 2455640616780894270\n"
    "Card Number: ending in 2610\nTransaction Date: 6/15/2026\n"
    "Customer Name: Jose Zambrano\nCustomer Email: jose@example.com\n```"
)

#: The one thing RYTE says in a channel that is data rather than prose about
#: the work. Everything else - help, status, an example of what to paste - is
#: RYTE talking, and none of it is evidence.
OUR_FLAG = "⚖️ **Chargeback**"


async def _rebuttal(responder: Responder, config: Config, message, said: str) -> None:
    """Build a chargeback rebuttal from the dispute facts and what he can find.

    Franklin pastes the block off the acquirer's notice and attaches the
    contract, the invoice and any screenshots. RYTE reads the board, the
    setup confirmations and the delivered lead sheet, works out what each
    attachment is by looking at it, and writes the document with them
    embedded under the proof each belongs to.

    Nothing is invented. A proof with no evidence behind it is left out and
    named at the top of the file as still needed, because a gap written
    around is one that reaches the acquirer.
    """
    from .. import rebuttal as rules_doc

    # Replying to the block and saying "@RYTE" is how somebody runs it a
    # second time - the notice was pasted once and the screenshots went with
    # it, and nobody wants to paste either again. The message RYTE gets is
    # bare, so the one it answers is where to look. Same as filing a recording
    # off a reply.
    # Anything under a PAYMENT: line is the portal's own record of the charge -
    # AVS, whether the cardholder started it, the invoice's event log - and it
    # is read as itself rather than parsed for labelled fields.
    said, paid_with = rules_doc.split_payment(said)
    dispute = rules_doc.read_facts(said)
    from_elsewhere = None
    replied = await _replied_to(message)
    if dispute.missing():
        fuller = _fuller_dispute(rules_doc, dispute, said, paid_with, replied)
        if fuller is not None:
            dispute, said, paid_with = fuller
            from_elsewhere = replied

    # The exhibits come off whichever message actually carries them, and that
    # is a separate question from where the facts came from. Franklin posted
    # the payment screenshot, then replied to it with the reason code. The
    # screenshot's own message had no text, so there were no facts to read out
    # of it, so it was skipped - and the screenshot was skipped with it. The
    # rebuttal then named the gateway record as still needed while it was
    # sitting one message up, attached to the thing being answered.
    if not getattr(message, "attachments", None) and getattr(
        replied, "attachments", None
    ):
        message = replied

    # Nobody replies to the notice every time. "@RYTE code: 37 - No Cardholder
    # Authorization rebuttal", typed fresh in the dispute channel with the
    # notice and RYTE's own flag card a few lines above it, asked Franklin to
    # paste a block that was already on the screen twice. Both of those parse
    # whole, so read back for them rather than asking for them again.
    if dispute.missing():
        async for older in _said_before(message):
            fuller = _fuller_dispute(rules_doc, dispute, said, paid_with, older)
            if fuller is None:
                continue
            dispute, said, paid_with = fuller
            from_elsewhere = older
            if not getattr(message, "attachments", None) and getattr(
                older, "attachments", None
            ):
                message = older
            if not dispute.missing():
                break

    # And whatever is still blank, off the rest of the channel. The flag card
    # answers `missing()` - name, amount, transaction date - so reading stopped
    # at it, and the notice two lines below it carrying her email, her card's
    # last four and the MID was never opened. Those are fact-table rows, not
    # blockers, so they are filled and never overwritten: a field somebody
    # typed always beats one found lying about.
    async for older in _said_before(message):
        _fill_the_blanks(rules_doc, dispute, older)

    if not dispute.customer_name:
        dispute.customer_name = rules_doc.named_in(said)

    holes = dispute.missing()
    if holes:
        await responder.send(
            "I need a bit more of the dispute notice — missing "
            + ", ".join(f"**{one}**" for one in holes)
            + ".\n" + ASK_FOR_THE_BLOCK
        )
        return

    exhibits = []
    skipped = []
    for attachment in getattr(message, "attachments", []) or []:
        if attachment.size and attachment.size > MOST_EXHIBIT_BYTES:
            skipped.append(f"{attachment.filename} (over 20MB)")
            continue
        try:
            exhibits.append(rules_doc.Exhibit(
                name=attachment.filename, data=await attachment.read()
            ))
        except Exception as exc:
            skipped.append(f"{attachment.filename} ({jobs._short(exc, 60)})")

    # Which message the facts came off, when they did not come off the ask.
    # Reading back is a guess about what somebody meant, and a rebuttal built
    # for the wrong customer is not a mistake anybody wants to find inside the
    # finished document.
    came_from = getattr(from_elsewhere, "jump_url", "") if from_elsewhere else ""
    if came_from:
        await responder.send(f"-# Facts off [the notice above](<{came_from}>).")

    # Before anything looks at them: the same screenshot twice costs a second
    # trip to Claude to be described, and comes back described as a duplicate.
    exhibits = rules_doc.only_once(exhibits)

    await responder.send(
        f"Building the rebuttal for **{dispute.customer_name}** — reading the "
        f"board, their setup and the delivered sheet"
        + (f", and looking at {len(exhibits)} attachment(s)" if exhibits else "")
        + " —"
    )

    try:
        if exhibits:
            exhibits = await asyncio.to_thread(jobs.sort_exhibits, config, exhibits)
        found = await asyncio.to_thread(jobs.rebuttal_evidence, config, dispute)
        # Pasted, or transcribed off a screenshot of the portal - either
        # reaches the same section. A picture of the gateway record is easier
        # to produce than a tidy copy of it, and RYTE reads what is in it.
        if not paid_with:
            paid_with = "\n\n".join(
                one.transcript.strip() for one in exhibits
                if one.kind == "payment" and one.transcript.strip()
            )
        # Pasted, or transcribed off a screenshot of the portal - either
        # reaches the same section. A picture of the gateway record is easier
        # to produce than a tidy copy of it, and RYTE reads what is in it.
        if not paid_with:
            paid_with = "\n\n".join(
                one.transcript.strip() for one in exhibits
                if one.kind == "payment" and one.transcript.strip()
            )
        if paid_with:
            found.payment = paid_with
        # The contract PandaDoc emailed, as an exhibit rather than as a
        # description of one - the no-chargeback clause is in the document.
        # Only when nobody attached one: a contract dragged in by hand is the
        # one somebody chose, and it wins over the one that was found.
        if found.contract_pdf and not any(
            one.kind == "contract" for one in exhibits
        ):
            exhibits.append(rules_doc.Exhibit(
                name=found.contract_name or "contract.pdf",
                data=found.contract_pdf,
                kind="contract",
            ))
        where = Path(DEFAULT_OUTPUT_DIR) / _rebuttal_name(dispute)
        path = await asyncio.to_thread(
            jobs.write_rebuttal, config, dispute, found, exhibits, into=where
        )
    except Exception as exc:
        await responder.send(embed=embeds.error(f"Couldn't build it\n{jobs._short(exc, 400)}"))
        return

    note = [f"📄 **{dispute.customer_name}** — chargeback rebuttal."]
    if exhibits:
        note.append(
            "Attachments filed as: "
            + ", ".join(f"{one.name} → {one.kind}" for one in exhibits)
        )
    still = rules_doc.what_is_missing(found, exhibits, dispute)
    if still:
        note.append(f"⚠ Still needed ({len(still)}) — it's listed at the top of the file.")
    if skipped:
        note.append("⚠ Couldn't read: " + ", ".join(skipped))
    note.append("_Read it before it goes anywhere. Nothing in it is invented, but "
                "nothing in it has been checked by a person either._")
    await responder.send("\n".join(note), file=discord.File(str(path)))
    found.rebuttal_name = path.name
    await _offer_the_tracker(responder, config, dispute, found)


async def _offer_the_tracker(
    responder: Responder, config: Config, dispute, found
) -> None:
    """The last step: one row in the chargeback tracker.

    The row is laid out against the tracker's own headings and shown in full
    before anything is written - a column RYTE does not recognise stays blank
    rather than being guessed at, and the one that gets filled in weeks later
    when the bank decides is not RYTE's to touch.

    Silent when there is no tracker configured. The rebuttal worked before
    there was one.
    """
    from .. import rebuttal as rules_doc

    try:
        headings, tab, problems = await asyncio.to_thread(
            jobs.tracker_headings, config
        )
    except PIPELINE_ERRORS as exc:
        headings, tab, problems = [], "", [f"Couldn't read the tracker: {exc}"]
    if problems:
        await responder.send("⚠ " + "\n⚠ ".join(problems))
        return
    if not headings:
        return

    row = rules_doc.row_for_tracker(
        headings, dispute, found, when=_today(config),
    )
    view = views.ConfirmView(
        requester_id=responder.requester_id,
        timeout=config.discord.approval_timeout_seconds,
        label=f"Add to {tab}",
        emoji="🧾",
    )
    await responder.send(
        f"🧾 One row for **{tab}**:\n"
        + rules_doc.describe_row(headings, row)
        + "\n-# Blank means RYTE doesn't know that column — fill those in yourself.",
        view=view,
    )
    await view.wait()
    if not view.confirmed:
        return

    try:
        where, trouble = await asyncio.to_thread(
            jobs.track_chargeback, config, tab, row
        )
    except PIPELINE_ERRORS as exc:
        where, trouble = "", [f"Couldn't write to the tracker: {exc}"]
    await responder.send(
        f"🧾 Added to **{where}**." if where else "⚠ " + "\n⚠ ".join(trouble)
    )


def _rebuttal_name(dispute) -> str:
    """A filename somebody can find again: surname, and the dispute date."""
    who = re.sub(r"[^A-Za-z0-9]+", "-", dispute.customer_name or "customer").strip("-")
    when = dispute.disputed()
    return f"{who}-Chargeback-Rebuttal{when and f'-{when:%Y-%m-%d}' or ''}.docx"


_HUSH = re.compile(
    r"^\s*(?:forget|drop|hush|ignore|stop|nevermind)\s+(?P<subject>.+)$", re.IGNORECASE
)


async def _what_i_noticed(responder: Responder, config: Config, said: str) -> None:
    """What RYTE has noticed while working, as suggestions.

    He only ever suggests. Nothing here touches the board, and the point of
    the message is the conversation after it rather than the message.
    """
    from .. import noticed

    asked = re.sub(
        r"^\s*(?:trello\s+)?(?:noticed|notice|suggest|suggestions|ideas)\b",
        "", said or "", count=1, flags=re.IGNORECASE,
    ).strip()

    hushing = _HUSH.match(asked)
    if hushing:
        subject = hushing.group("subject").strip().strip("“”\"'")
        done = await asyncio.to_thread(noticed.hush_subject, subject)
        await responder.send(
            f"Right — I'll stop raising “{subject}”."
            if done else
            f"I haven't been raising “{subject}”, so there's nothing to drop."
        )
        return

    if asked.casefold() in ("all", "everything", "raw", "list"):
        found = await asyncio.to_thread(noticed.notes)
        if not found:
            await responder.send(noticed.nothing_yet())
            return
        await responder.send(
            f"📓 Everything in the notebook ({len(found)}):\n"
            + "\n".join(f"• {noticed.describe(one)}" for one in found[:30])
        )
        return

    await responder.send("Reading back what I've noticed —")
    try:
        written, found = await asyncio.to_thread(jobs.suggestions, config)
    except PIPELINE_ERRORS as exc:
        await responder.send(embed=embeds.error(f"Couldn't read the notebook\n{exc}"))
        return

    if not found:
        await responder.send(written)
        return
    await responder.send(
        f"💡 **What I've noticed**\n\n{written}\n\n"
        "_Suggestions only — I haven't done any of it. "
        "`@RYTE noticed all` for the raw list, "
        "`@RYTE noticed forget <thing>` to stop me raising one._"
    )


async def _clear_out(
    bot: "WilByteBot", responder: Responder, config: Config, name: str
) -> None:
    """Close an agent down: keep the sheet, keep the conversation, then go.

    Two presses. The first keeps things and is safe - a row in ALL CLIENTS and
    a picture in Drive, neither of which takes anything away. The second is the
    one that cannot be undone, and it only appears once the keeping is done:
    "make it a button for me to confirm before ryte delete and remove them".

    Nothing happens in any server but the one named in .env, whatever a
    command is pointed at. The worst mistake available here is doing the right
    thing in the wrong place.
    """
    from datetime import datetime

    from .. import clearout

    name = " ".join((name or "").split())
    if not name:
        await responder.send(
            "Who? `@RYTE clearout Jay Rodriguez` — I'll find their channel, "
            "keep their sheet and a picture of the conversation, and then ask "
            "before anything goes."
        )
        return

    where = (config.secrets.discord_clients_guild_id or "").strip()
    guild = bot.get_guild(int(where)) if where.isdigit() else None
    if guild is None:
        await responder.send(
            "I'm not in the clients server, or DISCORD_CLIENTS_GUILD_ID in "
            ".env isn't it. `@RYTE access` lists the servers I'm in."
        )
        return

    channels = [
        clearout.Channel(
            channel_id=str(one.id),
            name=str(one.name),
            category=str(getattr(one.category, "name", "") or ""),
        )
        for one in guild.text_channels
    ]
    found = clearout.channels_for(name, channels)
    if not found:
        await responder.send(
            f"No channel in **{guild.name}** looks like **{name}**'s."
        )
        return
    if len(found) > 1:
        await responder.send(
            f"More than one channel could be **{name}**'s, so I've left them "
            "all alone:\n"
            + "\n".join(f"• #{one.name}" for one in found)
            + "\nName the one you mean and I'll do that one."
        )
        return

    plan = clearout.Plan(name=name, channel=found[0])
    member = _member_called(guild, name)
    if member is not None:
        plan.member_id, plan.member_name = str(member.id), str(member)

    plan.sheet, trouble = await asyncio.to_thread(jobs.sheet_for_agent, config, name)

    # Read once, before anything is offered: the picture needs it, and so does
    # the sheet when the board has no card for them. Artur Rushiti has no New
    # Agent card anywhere - the clear-out said "none found on their card" and
    # went on to offer the buttons, while every lead in his channel ends
    # "Check it here:" and the link. Writing an empty cell into ALL CLIENTS
    # and then deleting the only copy is the exact thing this is built to
    # prevent.
    messages, unread = await _last_said(
        guild.get_channel(int(plan.channel.channel_id))
    )
    plan.problems += unread
    if not plan.sheet:
        plan.sheet = clearout.sheet_in(messages)
        if plan.sheet:
            plan.from_channel = True
            trouble = []
    plan.problems += trouble

    view = views.ConfirmView(
        requester_id=responder.requester_id,
        timeout=config.discord.approval_timeout_seconds,
        label="Keep the sheet and show me the messages",
        emoji="🧹",
    )
    await responder.send(
        clearout.describe(plan)
        + "\n-# Nothing is deleted by this. I'll put the conversation here for "
        "you to screenshot, and ask again before anything goes.",
        view=view,
    )
    await view.wait()
    if not view.confirmed:
        return

    today = datetime.now(ZoneInfo(config.schedule.timezone))
    kept = []

    tab, trouble = await asyncio.to_thread(
        partial(jobs.collect_client, config, plan, when=today)
    )
    kept.append(f"✅ Sheet link → **{tab}**" if tab else "❌ " + "; ".join(trouble))

    # What they said, wherever they said it - their own channel and the ones
    # the whole server shares - and not the lead feed. Posted here rather than
    # photographed into Drive: "just forward them to me, then ill screenshot
    # then you collect sheet and delete". The person screenshotting knows what
    # is worth keeping in a way a rule about the last forty messages does not.
    elsewhere, looked = await _also_said(guild, member, channels)
    shown = clearout.for_the_picture(messages, elsewhere)
    forwarded = True
    try:
        for page in clearout.to_screenshot(
            plan, shown, clearout.the_feed(messages)
        ):
            await responder.send(page)
    except Exception as exc:
        forwarded = False
        kept.append(f"❌ Couldn't post the conversation: {_readable(exc)}")
    if forwarded:
        kept.append(f"✅ {len(shown)} message(s) posted above.")

    # And pictures of them in Drive, drawn the way Discord draws them. The
    # messages above are for screenshotting now; these are the copy that is
    # still there in six months when the channel is not.
    #
    # One per channel - "so itll be like 3 ss in total or smthing like that".
    # Their own channel and the sales they rang in ring-da-bell are two
    # different screenshots to the person who would have taken them by hand.
    groups = clearout.to_draw(plan, messages, elsewhere)
    if not groups:
        # Said rather than left out. A run with no picture line at all reads
        # exactly like one where the upload quietly failed.
        kept.append("-# No picture — there was nothing in the channel to draw.")
    for number, (where, group) in enumerate(groups, start=1):
        picture, trouble = await asyncio.to_thread(
            partial(
                jobs.keep_the_picture, config,
                clearout.as_page(plan, group),
                clearout.picture_name(
                    plan, when=today, where=where, order=number,
                ),
                into=clearout.their_folder(plan),
            )
        )
        named = f"#{where}" if where else "Picture"
        kept.append(
            f"✅ {named} → <{picture}>" if picture
            else f"⚠ {named} — " + "; ".join(trouble)
        )
        # Said even when the upload worked. It came back with a link and a
        # complaint, and dropping the complaint because there was a link is
        # how a picture ends up in the wrong folder with nobody told.
        if picture and trouble:
            kept += [f"-# {one}" for one in trouble]
    kept += [f"-# {one}" for one in looked]

    # Only once both are kept. The whole point of the order is that a channel
    # is never deleted with the only copy of something still inside it - and a
    # channel whose history would not open is one whose picture is of nothing,
    # however cleanly the picture itself was taken.
    if unread:
        await responder.send(
            "\n".join(kept)
            + "\n\n**Nothing deleted.** " + " ".join(unread)
        )
        return
    if not forwarded or not tab:
        await responder.send(
            "\n".join(kept)
            + "\n\n**Nothing deleted.** One of those didn't work, and the "
            "channel is the only copy of what it didn't keep."
        )
        return

    going = views.ConfirmView(
        requester_id=responder.requester_id,
        timeout=config.discord.approval_timeout_seconds,
        label=f"Delete #{plan.channel.name}",
        emoji="🗑",
        danger=True,
    )
    # Nobody is banned by a clear-out. "WERE ONLY BANNING PEOPLE IF THEY
    # DISPUTED" - an agent whose channel has gone quiet has simply stopped
    # buying, and banning them from the server for it is a different thing
    # entirely. The chargeback run is where a ban belongs.
    await responder.send(
        "\n".join(kept)
        + f"\n\n**This cannot be undone.**\n"
        + ("" if plan.sheet else
           "• ⚠ **No sheet link was found** — not on the board, not in the "
           "channel. The row in Ryte Collection has an empty cell, and "
           "whatever is in the channel goes with it.\n")
        + f"• Delete **#{plan.channel.name}**\n"
        + "-# Nobody is banned by this. That is the chargeback run's job.",
        view=going,
    )
    await going.wait()
    if not going.confirmed:
        await responder.send(
            "Left alone. The sheet row and the messages above stay either way."
        )
        return

    done, trouble = [], []
    channel = guild.get_channel(int(plan.channel.channel_id))
    # Asked again here rather than trusted from the looking-up: one channel,
    # the one this clear-out was for, in this server, and never one of the
    # server's own.
    refused = clearout.the_one_to_delete(plan, channel, guild_id=guild.id)
    if refused:
        trouble.append(f"Didn't delete anything — {refused}.")
    else:
        try:
            await channel.delete(reason=f"Closed down by RYTE for {name}")
            done.append(f"🗑 Deleted **#{plan.channel.name}**")
        except Exception as exc:
            trouble.append(f"Couldn't delete the channel: {_readable(exc)}")

    await responder.send(
        "\n".join(done + [f"⚠ {one}" for one in trouble])
        or "Nothing happened, which shouldn't be possible — check the channel."
    )


async def _blacklist_them(responder: Responder, config: Config, asked: str) -> None:
    """Tag a client blacklisted in GHL, after a chargeback.

    The last step of the chargeback run: "ryte we'll go to GHL and find their
    contact information and put a tag as blacklisted".

    Everything that matched is shown first, and the button names how many it
    will tag. A blacklist tag on the wrong contact is a paying client who
    stops getting leads and is never told why, so more than one match is
    something for a person to look at rather than for RYTE to pick between -
    and a name that matched nobody is said plainly rather than passed over.
    """
    try:
        found, tag, problems = await asyncio.to_thread(
            jobs.who_to_blacklist, config, asked
        )
    except PIPELINE_ERRORS as exc:
        await responder.send(embed=embeds.error(f"Couldn't read GHL\n{exc}"))
        return
    if problems:
        await responder.send(embed=embeds.error("\n".join(problems)))
        return
    if not found:
        await responder.send(
            f"No contact in GHL matches **{' '.join(asked.split())}**. "
            "Their email address matches where a name might not."
        )
        return

    already = [one for one in found if _carries(one, tag)]
    left = [one for one in found if one not in already]
    lines = [
        f"• {jobs._contact_name(one)}"
        + (f" — *already {tag}*" if one in already else "")
        for one in found
    ]
    if not left:
        await responder.send(
            f"All {len(found)} already carry **{tag}**:\n" + "\n".join(lines)
        )
        return

    view = views.ConfirmView(
        requester_id=responder.requester_id,
        timeout=config.discord.approval_timeout_seconds,
        label=f"Tag {len(left)} as {tag}",
        emoji="🚫",
        danger=True,
    )
    await responder.send(
        f"🚫 Tagging **{tag}** in GHL:\n" + "\n".join(lines)
        + ("\n\n**More than one contact matches that name.** Everything listed "
           "gets the tag — say an email address instead if that isn't right."
           if len(left) > 1 else ""),
        view=view,
    )
    await view.wait()
    if not view.confirmed:
        return

    try:
        done, trouble = await asyncio.to_thread(jobs.blacklist_them, config, left, tag)
    except PIPELINE_ERRORS as exc:
        await responder.send(embed=embeds.error(f"Couldn't tag them\n{exc}"))
        return
    said = [f"🚫 Tagged **{one}**" for one in done]
    said += [f"⚠ {one}" for one in trouble]
    await responder.send("\n".join(said) or "Nothing was tagged.")


def _carries(contact: dict, tag: str) -> bool:
    """Whether this contact already has the tag, however it was capitalised."""
    wanted = " ".join((tag or "").split()).casefold()
    return any(
        " ".join(str(one).split()).casefold() == wanted
        for one in (contact.get("tags") or [])
    )


async def _who_goes_live(responder: Responder, config: Config, said: str) -> None:
    """Who is going live on the day somebody asked about.

    "how many people are going live on thursday" used to fall through to the
    help text, which is the answer to a question nobody asked and reads like
    RYTE has never heard of the board he walks three times a day.
    """
    from .. import dailyops

    day = dailyops.day_named(said, today=_today(config)) or _today(config)
    try:
        found, undated, problems = await asyncio.to_thread(
            jobs.going_live_on, config, day
        )
    except PIPELINE_ERRORS as exc:
        await responder.send(embed=embeds.error(f"Couldn't read the board\n{exc}"))
        return
    if problems:
        await responder.send(embed=embeds.error("\n".join(problems)))
        return

    when = f"{day:%A %b %d}"
    if not found:
        await responder.send(
            f"Nobody's card says they go live on **{when}**."
            + (f"\n-# {undated} card(s) carry no launch date at all." if undated else "")
        )
        return

    lines = [
        f"• **{one['agent']}**"
        + (f" — {one['leads']}" if one.get("leads") else "")
        + (f" · {one['setup_by']}" if one.get("setup_by") else "")
        + ("" if one.get("ticked") else " · *card not ticked*")
        for one in found
    ]
    # Whoever set them up, when the setup card said. Two people's checklists on
    # one card is the normal shape of a busy day and worth seeing at a glance.
    note = (
        f"**{len(found)}** going live **{when}**:\n" + "\n".join(lines)
    )
    waiting = [one for one in found if one.get("setup_by") and not one.get("setup_done")]
    if waiting:
        note += (
            f"\n-# {len(waiting)} not ticked off on the setup card yet."
        )
    if undated:
        note += (
            f"\n-# {undated} card(s) carry no launch date in their description, "
            "so they aren't counted either way."
        )
    await responder.send(note)


async def _quiet_channels(
    bot: "WilByteBot", responder: Responder, config: Config, asked: str
) -> None:
    """Which of the clients server's channels nobody has used lately.

    The other end of the clear-out: `clearout` needs a name, and this is how
    you find out whose to clear. The list itself deletes nothing. Picking one
    from it starts a clear-out on that one channel and nothing else, and the
    two questions after it are the same two as when the name is typed by hand
    - the one that cannot be undone is still the second of them.

    Capped at what the dropdown can hold, and the rest counted: "then do 25
    each quiet? then i run again". Clearing some and running it again is how
    the rest are reached, and the channels that went are not in the list the
    second time because they are not there any more.

    One server, the one in DISCORD_CLIENTS_GUILD_ID, because that is the only
    one the agents' own channels are in and listing another server's is how a
    list of things to delete ends up pointing somewhere it shouldn't.
    """
    from .. import clearout

    where = (config.secrets.discord_clients_guild_id or "").strip()
    guild = bot.get_guild(int(where)) if where.isdigit() else None
    if guild is None:
        await responder.send(
            "I'm not in the clients server, or DISCORD_CLIENTS_GUILD_ID in "
            ".env isn't it. `@RYTE access` lists the servers I'm in."
        )
        return

    now = datetime.now(ZoneInfo(config.schedule.timezone))
    since = now - timedelta(days=clearout.how_far_back(asked or ""))

    channels = []
    for one in guild.text_channels:
        when, used = _last_used(one)
        channels.append(clearout.Channel(
            channel_id=str(one.id),
            name=str(one.name),
            category=str(getattr(one.category, "name", "") or ""),
            last_active=when.astimezone(ZoneInfo(config.schedule.timezone)) if when else None,
            ever_used=used,
            readable=_can_read(guild, one),
        ))

    quiet, ours, unknown = clearout.quiet_ones(channels, since=since)
    pages = clearout.describe_quiet(quiet, ours, unknown, since=since, now=now)
    offered = clearout.pick_from(quiet, now=now)

    # The picker goes on the last message, under the names it offers.
    for page in pages[:-1]:
        await responder.send(page)

    if not offered:
        await responder.send(pages[-1])
        return

    picker = views.ChannelPicker(
        offered,
        requester_id=responder.requester_id,
        timeout=config.discord.approval_timeout_seconds,
    )
    await responder.send(pages[-1], view=picker)
    await picker.wait()
    if not picker.chosen:
        return

    # By name, down the same path as a typed one. A picked channel could be
    # passed by id and skip the looking-up, but then the picked route and the
    # typed route would be two different pieces of code doing the irreversible
    # thing, and only one of them would have been watched.
    await _clear_out(bot, responder, config, picker.chosen)


def _can_read(guild, channel) -> bool:
    """Whether RYTE may open this channel's history.

    Asked of the permissions rather than by trying: Discord hands over every
    channel in the server whether or not it can be read, and finding out by
    reading is one request per channel and a 403 at the end of most of them.

    Unknowable counts as readable. Being told a channel cannot be cleared when
    it can is worse than finding out at the first button, which now says so
    plainly.
    """
    try:
        us = getattr(guild, "me", None)
        if us is None:
            return True
        allowed = channel.permissions_for(us)
        return bool(allowed.view_channel and allowed.read_message_history)
    except Exception:
        return True


def _last_used(channel):
    """(when anything was last said in it, whether anything ever was).

    From the id of the last message rather than by reading the channel: a
    Discord id has the time it was made inside it, so a whole server's worth
    of this is no requests at all, where asking each channel for its history
    is one request per channel and a rate limit at the end of it.

    An empty channel dates from when it was made, which is the honest answer
    to how long it has been sitting there.
    """
    try:
        last = getattr(channel, "last_message_id", None)
        if last:
            return discord.utils.snowflake_time(int(last)), True
        return getattr(channel, "created_at", None), False
    except Exception:
        log.exception("Couldn't tell when #%s was last used", getattr(channel, "name", "?"))
        return None, True


def _member_called(guild, name: str):
    """The member whose name looks like this one, or None.

    By what they are called in that server first, because an agent is added
    under their own name and then renames themselves on Discord.
    """
    from .. import clearout

    wanted = clearout.tidy(name)
    if not wanted:
        return None
    for member in guild.members:
        for called in (
            getattr(member, "display_name", ""),
            getattr(member, "global_name", "") or "",
            getattr(member, "name", ""),
        ):
            said = clearout.tidy(called)
            if said and (said == wanted or wanted in said or said in wanted):
                return member
    return None


def _all_of_it(message) -> str:
    """Everything a message says, its embeds included.

    The lead bots post each lead as an embed, so `content` is empty and the
    whole lead - the name, the state, and the "Check it here:" link - is in
    the embed's description. Reading only `content` made those channels look
    like forty blank messages: no sheet link to find, and a picture of
    nothing to keep before deleting them.
    """
    parts = [str(getattr(message, "content", "") or "")]
    for embed in getattr(message, "embeds", None) or []:
        for bit in (getattr(embed, "title", None), getattr(embed, "description", None)):
            if bit:
                parts.append(str(bit))
        for field in getattr(embed, "fields", None) or []:
            said = f"{getattr(field, 'name', '') or ''} {getattr(field, 'value', '') or ''}"
            if said.strip():
                parts.append(said.strip())
        for bit in (
            getattr(embed, "url", None),
            getattr(getattr(embed, "footer", None), "text", None),
            getattr(getattr(embed, "author", None), "name", None),
        ):
            if bit:
                parts.append(str(bit))
    return "\n".join(part for part in parts if str(part).strip())


def _reacted(message) -> str:
    """The reactions on a message, as "❤️ 4 · 🔥 2".

    Half of what a sale looks like in ring-da-bell is the team piling onto
    it, and a picture of the post without them is not what was sent.
    """
    found = []
    for one in getattr(message, "reactions", None) or []:
        mark = getattr(one, "emoji", "")
        name = str(getattr(mark, "name", "") or mark or "").strip()
        count = getattr(one, "count", 0) or 0
        if name and count:
            found.append(f"{name} {count}")
    return " · ".join(found)


def _face(author) -> str:
    """Their profile picture's url, or "".

    `display_avatar` is the one Discord actually shows - their server
    picture, their account picture, or the default one it draws for people
    who never set either - so there is always something to fetch.
    """
    face = getattr(author, "display_avatar", None)
    return str(getattr(face, "url", "") or face or "")


def _as_said(message, where: str):
    """One Discord message as the picture's own shape."""
    from .. import clearout

    author = getattr(message, "author", None)
    return clearout.Said(
        who=str(getattr(author, "display_name", "") or author),
        when=f"{message.created_at:%b %d, %Y %H:%M}" if message.created_at else "",
        text=_all_of_it(message),
        attachments=len(getattr(message, "attachments", []) or []),
        by_bot=bool(getattr(author, "bot", False)),
        at=getattr(message, "created_at", None),
        where=where,
        reactions=_reacted(message),
        avatar=_face(author),
    )


#: How deep the first read of a shared channel goes. Ring-da-bell carries the
#: whole server's wins, so a client who stopped buying in May is months and
#: thousands of messages down. Paid once, not once per clear-out.
FIRST_READ = 25_000

#: How much to pick up on a later pass. Only what has been said since, so
#: this is a fortnight's worth of headroom rather than a second deep read.
CATCH_UP = 2_000


async def _fill_the_bell(guild, channels) -> tuple[dict, list[str]]:
    """Read the shared channels and remember who said what. (data, notes).

    The first pass is deep and slow and happens once. Every pass after it
    reads only what has been said since, which on a busy channel is a few
    hundred messages and on a quiet one is none.

    Only what people said. A bot's post in a shared channel is an
    announcement, and this is a record of who sold what.
    """
    from .. import bell, clearout

    data = await asyncio.to_thread(bell.load)
    notes: list[str] = []
    for one in channels or []:
        if not clearout.off_limits(one):
            continue
        channel = guild.get_channel(int(one.channel_id))
        if channel is None:
            continue
        if not _can_read(guild, channel):
            notes.append(f"⚠ Can't open #{one.name}, so nothing said in it is kept.")
            continue

        after = bell.since(data, one.channel_id)
        how = (
            {"limit": CATCH_UP, "after": discord.Object(id=int(after)),
             "oldest_first": True}
            if after.isdigit() else {"limit": FIRST_READ}
        )
        newest, count = after, 0
        try:
            async for said in channel.history(**how):
                count += 1
                mark = str(getattr(said, "id", "") or "")
                if mark and (not newest.isdigit() or int(mark) > int(newest)):
                    newest = mark
                author = getattr(said, "author", None)
                if getattr(author, "bot", False) or author is None:
                    continue
                text = _all_of_it(said)
                if not text.strip():
                    continue
                at = getattr(said, "created_at", None)
                bell.keep(
                    data, author_id=getattr(author, "id", ""), message_id=mark,
                    who=str(getattr(author, "display_name", "") or author),
                    when=f"{at:%b %d, %Y %H:%M}" if at else "",
                    text=text, where=str(one.name),
                    at=at.isoformat() if at else "",
                    reactions=_reacted(said), avatar=_face(author),
                )
            bell.read_to(data, one.channel_id, newest)
            if count:
                notes.append(
                    f"-# Read {count} new message(s) in #{one.name}."
                    if after.isdigit() else
                    f"-# First read of #{one.name}: {count} message(s)."
                )
        except Exception as exc:
            log.exception("Couldn't read #%s for the bell", one.name)
            notes.append(f"⚠ Couldn't finish reading #{one.name}: {jobs._short(exc, 60)}")

    await asyncio.to_thread(bell.save, data)
    return data, notes


async def _also_said(guild, member, channels) -> tuple[list, list[str]]:
    """What this client said in the channels the whole server shares.

    A sale rung in ring-da-bell is the client saying the leads worked, and it
    is the half of the evidence their own channel does not have - theirs is
    the bot's lead feed and a "thanks".

    Out of what has been read and remembered rather than by reading the
    channel again now. Reading it per clear-out reached thirteen days and
    found nothing of a client who stopped in May.
    """
    from .. import bell, clearout

    if member is None:
        return [], [
            "Nobody by that name is in the server, so only their own channel "
            "was read."
        ]

    data, notes = await _fill_the_bell(guild, channels)
    # Their face as it is now, for anything remembered before there was
    # somewhere to keep one. The bell is only ever read forwards, so without
    # this those messages stay faceless for good - and they are the oldest,
    # which is to say the ones worth keeping.
    face = _face(member)
    found = [
        clearout.Said(
            who=str(one.get("who") or ""), when=str(one.get("when") or ""),
            text=str(one.get("text") or ""), where=str(one.get("where") or ""),
            at=_as_when(one.get("at")),
            reactions=str(one.get("reactions") or ""),
            avatar=str(one.get("avatar") or "") or face,
        )
        for one in bell.theirs(data, getattr(member, "id", ""))
    ]
    if not found:
        notes.append(
            "-# Nothing of theirs in the shared channels that have been read."
        )
    return found, notes


def _as_when(said):
    """An ISO string back into a datetime, or None."""
    try:
        return datetime.fromisoformat(str(said)) if said else None
    except ValueError:
        return None


async def _last_said(channel, *, howmany: int = 0) -> tuple[list, list[str]]:
    """The last of the conversation, oldest first. (messages, problems).

    The problem comes back rather than being logged and swallowed. A channel
    RYTE is not allowed to read looked exactly like an empty one, which meant
    what was kept before a delete was nothing at all, and was reported as
    kept.

    Three hundred rather than forty. Forty was how many were wanted, and in a
    channel carrying a lead feed all forty are the bot: Artur Rushiti's came
    back as "nothing anybody said" while the conversation sat just above the
    window. What is wanted is forty of what people said, which means reading
    past what they didn't.
    """
    from .. import clearout

    if channel is None:
        return [], ["There is no channel by that id to read."]
    where = str(getattr(channel, "name", "") or "")
    deep = howmany or clearout.LOOK_BACK
    seen, found = set(), []

    async def take(**how):
        async for said in channel.history(**how):
            mark = getattr(said, "id", None)
            if mark is not None and mark in seen:
                continue
            if mark is not None:
                seen.add(mark)
            found.append(_as_said(said, where))

    try:
        await take(limit=deep)
        # And the other end. A client channel opens with the conversation -
        # the welcome, the questions, what they wanted - and then a year of
        # the lead feed buries it. Reading backwards from today never reaches
        # it, however far back it goes, and that conversation is the one
        # somebody would screenshot by hand.
        await take(limit=clearout.FIRST_OF_IT, oldest_first=True)
    except Exception as exc:
        log.exception("Couldn't read that channel's history")
        return [], [
            "Couldn't read that channel's history, so there is nothing to "
            f"keep: {jobs._short(exc, 140)}"
        ]
    return list(reversed(found)), []


async def _what_i_can_do(responder: Responder, bot: "WilByteBot") -> None:
    """Every server RYTE is in, and what he is actually allowed to do in it.

    Asked before anything is built on top of a permission rather than after.
    A tick in the Discord portal and a permission that survived the role
    hierarchy are two different things, and the difference only shows up at
    the moment somebody presses a button expecting an agent to be removed.
    """
    wanted = (
        ("ban_members", "ban"),
        ("kick_members", "kick"),
        ("manage_channels", "delete channels"),
        ("read_message_history", "read history"),
        ("manage_messages", "manage messages"),
    )
    lines = []
    for guild in sorted(bot.guilds, key=lambda one: str(one.name or "")):
        me = guild.me
        held = me.guild_permissions if me is not None else None
        able = [
            said for name, said in wanted
            if held is not None and getattr(held, name, False)
        ]
        missing = [
            said for name, said in wanted
            if held is None or not getattr(held, name, False)
        ]
        lines.append(
            f"**{guild.name}** · `{guild.id}`\n"
            f"  ✅ {', '.join(able) or 'nothing'}"
            + (f"\n  ❌ {', '.join(missing)}" if missing else "")
        )

    intents = bot.intents
    lines.append(
        "\n**Intents** — members "
        + ("✅" if getattr(intents, "members", False) else
           "❌ *(Developer Portal → Bot → Privileged Gateway Intents)*")
        + ", message content "
        + ("✅" if getattr(intents, "message_content", False) else "❌")
    )
    await responder.send("\n".join(lines))


async def _wrong_days(responder: Responder, config: Config) -> None:
    """Every Lead Order line sitting on a day its agent is not live on.

    Reads only, and says so. The spread refuses to write these now; the ones
    written before it started asking are still there, and each one is leads
    ordered for the wrong day.
    """
    try:
        findings, problems = await asyncio.to_thread(jobs.wrong_day_lines, config)
    except PIPELINE_ERRORS as exc:
        await responder.send(embed=embeds.error(f"Couldn't check the days\n{exc}"))
        return

    note = jobs.describe_wrong_days(findings)
    if problems:
        note += "\n⚠ " + "\n⚠ ".join(problems)
    await responder.send(note)


async def _tagged_tasks(responder: Responder, config: Config, said: str) -> None:
    """Comments and descriptions that tagged somebody, turned into checklist lines.

    Shows them and waits for the button, always - "from now i dont want it
    being added automatically but a button". It writes onto four people's live
    lists, and the spread - the other thing that writes checklists - put lines
    on the wrong card before it had been watched for a week.
    """
    from .. import tagged

    try:
        tasks, problems = await asyncio.to_thread(jobs.tags_to_file, config)
    except PIPELINE_ERRORS as exc:
        await responder.send(embed=embeds.error(f"Couldn't read the comments\n{exc}"))
        return

    if not tasks:
        note = "Every tagged comment on today's cards is already on a checklist. 👍"
        if problems:
            note += "\n⚠ " + "\n⚠ ".join(problems)
        await responder.send(note)
        return

    view = views.ConfirmView(
        requester_id=responder.requester_id,
        timeout=config.discord.approval_timeout_seconds,
        label=f"Add {len(tasks)} item(s)",
        emoji="📌",
    )
    listed = "\n".join(f"• {tagged.describe(one)}" for one in tasks[:TAGS_SHOWN])
    if len(tasks) > TAGS_SHOWN:
        listed += f"\n…and {len(tasks) - TAGS_SHOWN} more."
    note = (
        f"📌 {len(tasks)} tagged item(s) that aren't on a checklist yet:\n{listed}\n"
        "A comment gets a summary; a description line goes on as written. "
        "Both carry a link back to where they were said."
    )
    if problems:
        note += "\n⚠ " + "\n⚠ ".join(problems)
    await responder.send(note, view=view)
    await view.wait()
    if not view.confirmed:
        return

    try:
        landed, trouble = await asyncio.to_thread(jobs.file_tags, config, tasks)
    except PIPELINE_ERRORS as exc:
        await responder.send(embed=embeds.error(f"Couldn't write them\n{exc}"))
        return

    said_back = f"📌 Added {len(landed)} item(s)."
    if landed:
        said_back += "\n" + "\n".join(f"• {line}" for line in landed[:TAGS_SHOWN])
    if trouble:
        said_back += "\n⚠ " + "\n⚠ ".join(trouble)
    await responder.send(said_back)


async def _board_step(bot: "WilByteBot", step: str, today) -> None:
    """Do one step and say what happened, then never do it again today."""
    from .. import boardclock, dailyops

    responder = _board_responder(bot)
    card = None
    # The unticked cards that were never a setup, offered under the chase
    # rather than instead of it: they are still unticked and still worth
    # seeing, and the button is what makes them stop coming back.
    top_ups = None
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
            card = _unmarked_card(
                found, step=step, days=dailyops.days_chased(_today(bot.config)),
            ) if found else None
            top_ups = found or None
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
        elif step == "to_lead_order" or step in dailyops.SPREAD:
            added, conflicts, problems = await asyncio.to_thread(
                jobs.spread_to_lead_order, bot.config
            )
            # Silent when it placed nobody, which is what the seven, eight and
            # half-eight sweeps usually do - they exist for the agent whose
            # card lands at ten to eight, not to report four times a night
            # that the six o'clock one already did the work.
            note = (
                f"📋 {dailyops.said_at(step)} — put {len(added) - 1} setup-card "
                f"agent(s) on the Lead Order card.\n" + "\n".join(added)
            ) if added else ""
            card = (
                embeds.spread_conflicts(conflicts, shown=UNMARKED_SHOWN)
                if conflicts else None
            )
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

    # On a Friday, the Lead Order card the weekend's setup card spreads onto.
    # Checked at every step rather than once in the morning: the day's cards
    # land in In Que around eleven, so the one that needs widening is often
    # not there yet at six - and by the evening the spread needs it. It does
    # nothing at all on the other six days, and nothing twice on a Friday.
    try:
        weekend, trouble = await asyncio.to_thread(
            jobs.weekend_order_card, bot.config
        )
    except PIPELINE_ERRORS as exc:
        weekend, trouble = "", [f"Couldn't check the weekend Lead Order card: {exc}"]
    problems = list(problems) + trouble
    if weekend:
        note = (note + "\n" if note else "") + f"📋 Weekend Lead Order — {weekend}."

    if problems:
        note = (note + "\n⚠ " + "\n⚠ ".join(problems)).lstrip("\n")
    # Each branch leaves both empty when its step had nothing to say. A line
    # every morning reporting that nothing happened is a line nobody reads.
    if responder and (note or card):
        await responder.send(note or None, embed=card)
    if responder and top_ups:
        await _offer_the_top_ups(responder, bot.config, top_ups)


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


def _chargeback_responder(bot: "WilByteBot"):
    """Where a chargeback is said. Its own channel, or the board's.

    Its own, because a dispute is money and is nobody's daily routine: on the
    board channel it arrives between a rollover and a list of tagged comments
    and is scrolled past with them.
    """
    configured = getattr(
        bot.config.secrets, "discord_chargeback_channel_id", None
    )
    if configured:
        channel = bot.get_channel(int(configured))
        if channel is not None:
            return ChannelResponder(channel)
    return _board_responder(bot)

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
        # What went to Discord, whole: the opening and then every segment in
        # full. The card gets the index — timestamps and titles, which is what
        # the board is for — and the doc gets the copy, because the copy is
        # what the website and YouTube are posted from.
        copy="\n\n".join([summary] + [one.as_text() for one in keep]),
    )


async def _file_interview(
    responder: Responder, config: Config, keep, *, topic: str, link: str,
    passcode: str, copy: str = "",
) -> None:
    """Put the cut-up interview on the board, and its copy in the posting doc.

    Two different things want two different shapes of the same work. The card
    is an index — the link, and every segment's timestamps and title — because
    the board is where somebody checks what was cut and whether it is done.
    The doc is the copy itself, titles and descriptions and bullets and
    hashtags and website paragraphs, because that is what gets posted from.

    `copy` is what went to Discord, whole. Without it the doc got the index
    too, which is the one thing nobody needs it for.
    """
    from .. import segments as segmenting

    name = segmenting.card_title(topic)
    description = segmenting.as_card(keep, link=link, passcode=passcode)

    try:
        url, card_id, problems = await asyncio.to_thread(
            jobs.file_interview, config, name=name, description=description,
            ask=segmenting.needs_an_image(topic),
        )
    except PIPELINE_ERRORS as exc:
        await responder.send(embed=embeds.error(f"Couldn't make the board card: {exc}"))
        return

    for problem in problems:
        await responder.send(f"⚠ {problem}")
    if not url:
        return

    await responder.send(f"Filed as **{name}** in Marketing Department — <{url}>")

    # And into the doc the website is posted from, as a tab of its own. The
    # card stays as it is: the board runs on the card, and the copy is pasted
    # from the doc - so it wants to be in both rather than moved.
    try:
        where, trouble = await asyncio.to_thread(
            jobs.copy_into_doc, config,
            # The tabs are named for the person — "Leonardo Lopez", not
            # "Leonardo Lopez Interview", which is the card's title.
            title=segmenting.client_name(topic) or name,
            text=copy or description,
        )
    except PIPELINE_ERRORS as exc:
        where, trouble = "", [f"Couldn't write it into the doc: {jobs._short(exc, 160)}"]
    if where:
        await responder.send(f"📄 Copy in the posting doc — <{where}>")
    for problem in trouble:
        await responder.send(f"⚠ {problem}")

    # And onto the lists of the people who act on it. The card existing is not
    # the same as anybody knowing it exists: it goes on Faith's list for today
    # and on the YT VID card where the editors are tagged, and then into Done,
    # because being cut up is what it was for and that part is finished.
    try:
        did, trouble = await asyncio.to_thread(
            jobs.hand_off_interview, config,
            card_url=url, card_id=card_id, day=_today(config),
        )
    except PIPELINE_ERRORS as exc:
        did, trouble = [], [f"Couldn't hand it off: {jobs._short(exc, 160)}"]
    if did or trouble:
        await responder.send(
            "\n".join(did + [f"⚠ {one}" for one in trouble])
        )
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
    # "calls fields <link>" asks a different question: not whether RYTE can see
    # the call, but what Fathom hands back about it - which is how we find out
    # whether the recording itself can be fetched and put somewhere the team
    # can reach without the Fathom account.
    said = " ".join((link or "").split())
    raw = bool(re.match(r"(?i)^(fields|raw)\b", said))
    if raw:
        link = re.sub(r"(?i)^(fields|raw)\b\s*", "", said)

    await responder.send(
        "Asking Fathom what it returns for that call…" if raw
        else "Checking that link against Zoom…" if link
        else "Asking Zoom and Fathom what they'll show me…"
    )
    try:
        if raw:
            lines = await asyncio.to_thread(jobs.fathom_fields, config, link)
        elif link:
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
        # The board's clock, not the machine's. "start monday" decides which
        # day a blog post may land on, and the posting schedule is Eastern -
        # so reading "monday" off a Mac in Manila picks the wrong Monday for
        # half of every day.
        day = prefs.parse_day(text, today=_today(config))
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
        plans, where, missing, notes = await asyncio.to_thread(
            jobs.read_agents, config
        )
    except PIPELINE_ERRORS as exc:
        if not silent:
            await responder.send(embed=embeds.error(f"Couldn't read the board\n{exc}"))
        return

    if missing:
        await responder.send(embed=embeds.error("\n".join(missing)))
        return
    # Said, and then carried on. A title one letter out is worth fixing and is
    # not a reason to file nobody.
    for note in notes:
        await responder.send(f"⚠ {note}")
    if not plans:
        if not silent:
            await responder.send("No new agents waiting in In Que.")
        return

    doable = [plan for plan in plans if plan.doable]
    stuck = [plan for plan in plans if not plan.doable]

    # Cards somebody is still writing. Only the watcher waits: it files on its
    # own twenty seconds after a card lands, and these cards land finished and
    # wrong - copied from the last agent, then corrected. Asked for by hand,
    # everything is shown and the button is somebody's to press.
    if silent:
        now = datetime.now(timezone.utc)
        settling = [plan for plan in doable if rules.still_being_written(plan.agent, now=now)]
        doable = [plan for plan in doable if plan not in settling]

    if not silent:
        view = None
        if doable:
            view = views.ConfirmView(
                requester_id=responder.requester_id,
                timeout=config.discord.approval_timeout_seconds,
                label=f"File {len(doable)} agent(s)",
                emoji="🧾",
            )
        await responder.send(rules.describe(plans, today=_today(config)), view=view)
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
        note += "\n" + rules.describe(doable, today=_today(config))
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


# A comment is somebody handing over a job, and the point of this is that they
# do not then have to write it down - so the gap between saying it and seeing
# it on the list is the whole experience. A tick costs one request while
# nothing has happened (see `jobs.tags_stamp`), so a short one is cheap.
TAG_CHECK_SECONDS = 60


async def tags_loop(bot: "WilByteBot") -> None:
    """Watch the day's cards and offer what was tagged, as it lands.

    Watching, never writing - "from now i dont want it being added
    automatically but a button". The tick brings the work to the button
    instead of the button having to be gone and fetched: every comment and
    every line added to a description shows up here within the minute, and
    stays where it was written until somebody presses it.

    Cheap while it is quiet: one request a minute to ask whether any of the
    three cards has been touched at all. Everything else - the comments, the
    descriptions, the checklists, having them read - only happens once one has.

    The stamp is remembered before the asking rather than after, so a pass
    that goes wrong is not retried every minute for the rest of the day, and
    the loop waits on the button rather than stacking a second list on top of
    a first nobody has looked at yet.

    What has already been put in front of somebody is remembered on disk, not
    in this loop. The stamp moves for all sorts of reasons - a card walking to
    the next list, the rollover writing onto tomorrow's, somebody ticking a
    box - and every one of those was re-posting the same twelve lines and the
    same three warnings: "if you already said it dont add it to the next
    update i already said leave it". Holding that in memory was enough until
    the first restart, and RYTE is restarted several times on a busy evening.
    """
    seen = ""
    while not bot.is_closed():
        try:
            stamp = await asyncio.to_thread(jobs.tags_stamp, bot.config)
            if stamp and stamp != seen:
                seen = stamp
                responder = _board_responder(bot)
                if responder is not None:
                    await _offer_tags_now(responder, bot.config, remember=True)
        except asyncio.CancelledError:
            raise
        except Exception:  # a bad tick must not take the loop down for good
            log.exception("Tag check failed; will try again shortly")
        await asyncio.sleep(TAG_CHECK_SECONDS)


def _already_said(task) -> str:
    """What makes one offered line the same line as another.

    The comment it came from and whose list it is going on - never the
    summary. The summary is written fresh every run and comes back reworded:
    the same comment gave "Let them know Everlife aged lead 20% off, code
    everlife20" one minute and the same sentence without the comma the next,
    and a comma was enough to make it a line nobody had seen before. Pressing
    "leave it" then meant nothing.

    A description line has no comment to be identified by, so there its own
    words do the job - which is safe, because those go on as they were written
    rather than being summarised.
    """
    if task.note.comment_id:
        return f"{task.note.comment_id}|{task.kind}|{task.checklist}"
    return f"description|{task.kind}|{task.checklist}|{task.summary}"


def _said_keys(tasks) -> list:
    """One key per task, numbered where a comment gives somebody several.

    Tre's comment is three jobs for Kath. Without the number all three are the
    same key, so showing the first would silence the other two for the rest of
    the day.
    """
    counted: dict = {}
    found = []
    for one in tasks:
        base = _already_said(one)
        counted[base] = counted.get(base, 0) + 1
        found.append(f"{base}#{counted[base]}")
    return found


async def _offer_tags_now(
    responder: Responder, config: Config, *, remember: bool = False
) -> None:
    """Show what is new and wait for the button. Writes nothing on its own.

    Silent when there was nothing, because this runs all day: a line every
    minute saying nothing happened is a line nobody reads, and one that
    matters would be lost among them.

    `remember` is the watcher. Only what has not already been shown gets
    posted, and everything posted is written down - so a list that was left
    alone stays left alone, and the next message is the new work rather than
    the old work again with one line added. The warnings are held the same
    way: "@tretarpley has no checklist" is worth saying once a day, not once
    a minute. Somebody typing the command is looking and gets the whole list.

    Anybody on the team can press it. Nobody asked for this one - it came off
    the board rather than out of a message - so there is no "the person who
    asked" to hold it for.
    """
    from .. import tagged

    from .. import alreadysaid

    day = await asyncio.to_thread(jobs.board_day, config)
    tasks, problems = await asyncio.to_thread(jobs.tags_to_file, config)
    if remember:
        said = await asyncio.to_thread(alreadysaid.said_on, day)
        tasks = [
            one for one, key in zip(tasks, _said_keys(tasks)) if key not in said
        ]
        problems = [one for one in problems if one not in said]
    if not tasks:
        # Said out loud even with nothing to file: somebody tagged with no
        # checklist is a person waiting on a job nobody wrote down.
        if problems:
            await responder.send("⚠ " + "\n⚠ ".join(problems))
            if remember:
                await asyncio.to_thread(alreadysaid.remember, day, problems)
        return

    # Written down as it is posted rather than after the button, so a tick
    # while somebody is still looking at it does not post the same list
    # underneath, and neither does a restart.
    if remember:
        await asyncio.to_thread(
            alreadysaid.remember, day, _said_keys(tasks) + list(problems),
        )

    # A description line and a comment are two different things and get asked
    # about separately. The description is the card's own standing list of who
    # is doing what; a comment is somebody handing over a job during the day.
    # Mixed into one list of seven they read as one pile of work, and saying
    # yes to today's handovers meant saying yes to the standing plan as well.
    written = [one for one in tasks if getattr(one.note, "described", False)]
    spoken = [one for one in tasks if not getattr(one.note, "described", False)]
    both = [
        (these, emoji, what)
        for these, emoji, what in (
            (written, "📋", "in the description"),
            (spoken, "💬", "in the comments"),
        )
        if these
    ]
    # Together, not one after the other. Each list waits on its own button and
    # a button waits up to `approval_timeout_minutes` - so asking in sequence
    # would hide the comments until somebody had answered the description,
    # which on a quiet afternoon is half a day.
    await asyncio.gather(*(
        _offer_these(
            responder, config, these, emoji=emoji, what=what,
            # The warnings belong under one list, not repeated under both.
            problems=problems if at == 0 else [],
        )
        for at, (these, emoji, what) in enumerate(both)
    ))


async def _offer_these(
    responder: Responder, config: Config, tasks, *, problems, emoji: str, what: str
) -> None:
    """One item, one button, each on its own.

    "i want this 1 item per flag". A list of four with one button is a yes to
    all four or a no to all four, and the whole reason to look at them is that
    some belong on a checklist and some do not.

    Together rather than one after the other: each waits on its own button for
    up to the approval timeout, so asking in turn would hide the fourth until
    somebody had answered the first.
    """
    if problems:
        await responder.send("⚠ " + "\n⚠ ".join(problems))
    await asyncio.gather(*(
        _offer_one(responder, config, one, emoji=emoji, what=what)
        for one in tasks
    ))


async def _offer_one(
    responder: Responder, config: Config, task, *, emoji: str, what: str
) -> None:
    """One item, one button. Nothing is written until it is pressed."""
    from .. import tagged

    tasks = [task]
    view = views.ConfirmView(
        requester_id=None,
        timeout=config.discord.approval_timeout_seconds,
        label="Add it",
        emoji=emoji,
    )
    note = f"{emoji} New {what}, not on a checklist yet:\n• {tagged.describe(task)}"
    await responder.send(note, view=view)
    await view.wait()
    if not view.confirmed:
        # A list that timed out is a list nobody saw, and the work is still
        # only written where it was written. "Leave it" has been answered
        # already and needs nothing more said about it.
        if not view.answered:
            await responder.send(
                "⏳ Nobody pressed it, so nothing was added — "
                "`@RYTE trello tags` brings the same list back."
            )
        return

    landed, trouble = await asyncio.to_thread(jobs.file_tags, config, tasks)
    said_back = f"📌 Added {len(landed)} item(s)."
    if landed:
        said_back += "\n" + "\n".join(f"• {line}" for line in landed[:TAGS_SHOWN])
        if len(landed) > TAGS_SHOWN:
            said_back += f"\n…and {len(landed) - TAGS_SHOWN} more."
    if trouble:
        said_back += "\n⚠ " + "\n⚠ ".join(trouble)
    await responder.send(said_back)


SETUP_CHECK_SECONDS = 600


async def setup_check_loop(bot: "WilByteBot") -> None:
    """Watch for agents set up on leads they did not order.

    Ten minutes rather than twenty seconds: the confirmation comment lands
    hours after the card is filed, so there is nothing to gain from looking
    more often, and this one reads every list on the board.

    A confirmation that contradicts itself and answers itself is not raised
    here. Therese's on Austin Casares opened "OTP TUCKER IUL" and then gave
    the slug `austin-casares-trucker`, which is what he ordered - so nothing
    was set up wrong and the only thing wrong is a letter in a headline.
    Written down instead, and said if it keeps happening. `@RYTE trello
    setups` still shows them, because that is somebody asking.
    """
    from .. import noticed, setupseen

    while not bot.is_closed():
        try:
            responder = _board_responder(bot)
            if responder is not None:
                found, problems = await asyncio.to_thread(jobs.wrong_setups, bot.config)
                for one in found:
                    if one.get("typo"):
                        await asyncio.to_thread(
                            jobs._jot, noticed, "conflict",
                            str(one.get("setup") or ""),
                            detail=(
                                f"the confirmation says “{one.get('setup')}” and "
                                f"then “{one.get('also')}”, which is what they "
                                "ordered"
                            ),
                        )
                found = [one for one in found if not one.get("typo")]
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


# Fifteen minutes. A line lands on a Lead Order card when somebody runs the
# spread or writes one by hand, and neither happens on a schedule - but neither
# happens every minute either, and this reads every list on the board.
DAY_CHECK_SECONDS = 900


def _wrong_day_key(one: dict) -> str:
    """What makes one wrong-day line the same one as before.

    The card, the checklist and the agent's line. Nothing here is written by
    a model, so it stays the same between runs.
    """
    return "|".join((
        "wrongday", str(one.get("card") or ""), str(one.get("checklist") or ""),
        str(one.get("agent") or ""), str(one.get("label") or ""),
    ))


async def day_check_loop(bot: "WilByteBot") -> None:
    """Watch for lines on a Lead Order card for a day the agent isn't live.

    The same thing the setup check does for leads somebody didn't order:
    nobody has to ask, and it says each one once - "this should automatically
    notify me like when the agent setup doesnt match on what they want and
    what got set up... without repeating what was said".

    Today's card and tomorrow's, and on a Friday the weekend's as well, since
    Saturday, Sunday and Monday are set up together and spread onto one card.
    Not the fortnight: that is `@RYTE trello daycheck`, which is somebody
    asking and wants everything.

    Reads only. It names the lines; moving them is somebody's decision.
    """
    from .. import alreadysaid

    while not bot.is_closed():
        try:
            responder = _board_responder(bot)
            if responder is not None:
                day = await asyncio.to_thread(jobs.board_day, bot.config)
                findings, problems = await asyncio.to_thread(
                    jobs.wrong_day_lines, bot.config, only=jobs.days_watched(day),
                )
                said = await asyncio.to_thread(alreadysaid.said_on, day)
                fresh = [
                    one for one in findings if _wrong_day_key(one) not in said
                ]
                if fresh:
                    await responder.send(jobs.describe_wrong_days(fresh))
                    await asyncio.to_thread(
                        alreadysaid.remember, day,
                        [_wrong_day_key(one) for one in fresh],
                    )
                if problems:
                    log.warning("Day check: %s", "; ".join(problems))
        except asyncio.CancelledError:
            raise
        except Exception:  # a bad tick must not take the loop down for good
            log.exception("Day check failed; will try again shortly")
        await asyncio.sleep(DAY_CHECK_SECONDS)


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


def _rollover_covers(only, which: str) -> str:
    """The line above the plan saying which cards this run is about.

    A bare rollover is the half-eight pair now, and somebody who typed it at
    nine expecting all four should find that out before they press anything -
    not tomorrow, from Lead Order still holding yesterday's lines.
    """
    from .. import dailyops

    if isinstance(only, str):
        return f"**{which} only** — the other cards are untouched."
    kinds = tuple(only or ())
    if set(kinds) == set(dailyops.EVENING_KINDS):
        late = " and ".join(
            dailyops.CARD_KINDS.get(k, k) for k in dailyops.LATE_KINDS
        )
        return (
            f"**{which}** — {late} are carried at "
            f"{dailyops.said_at(dailyops.LATE_ROLLOVER)}, because they're still "
            "being worked. `trello rollover late` does those now; "
            "`trello rollover all` does everything."
        )
    if set(kinds) == set(dailyops.LATE_KINDS):
        return f"**{which}** — the ten o'clock pair. General and Ops are untouched."
    return "**All four cards.**"


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

    only = dailyops.kind_named(named) or dailyops.rollover_kinds(named)
    which = (
        dailyops.CARD_KINDS.get(only, "") if isinstance(only, str)
        else " and ".join(dailyops.CARD_KINDS.get(k, k) for k in only)
    )
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
        plans, missing, targets, ahead = await asyncio.to_thread(
            partial(jobs.read_rollover, config, only=only, day=day, skip=skip)
        )
    except PIPELINE_ERRORS as exc:
        await responder.send(embed=embeds.error(f"Couldn't read the board\n{exc}"))
        return

    report = dailyops.summarise(plans, missing=missing)
    if ahead:
        # Not tomorrow's card. Said before the button rather than after it, so
        # nobody approves a move onto a day they weren't told about.
        report += "\n⚠ No card for tomorrow, so these would go onto the next one: " + ", ".join(
            f"{dailyops.CARD_KINDS.get(kind, kind)} → {when:%a %b %d}"
            for kind, when in sorted(ahead.items())
        )
    if missing:
        report += f"\n⚠ Nowhere to carry: {', '.join(missing)}"

    movable = sum(len(plan.carried) for plan in plans)
    if not movable:
        await responder.send(report)
        return

    report = f"{_rollover_covers(only, which)}\n{report}"

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


# How long the Levinson member list is kept before it is read again. Walking
# GoHighLevel's contacts is two hundred requests, and a payment lands often
# enough that doing it per payment would spend the whole minute on it. Half an
# hour late on an agent who opted in this morning is a row RYTE adds when the
# next payment lands or when the month is run by hand.
MEMBERS_GOOD_FOR = 30 * 60

# Long enough that a broken tag lookup is said once an hour rather than once a
# payment. A warning repeated eleven hundred times is a warning nobody reads.
SAY_AGAIN_AFTER = 60 * 60


async def _levinson_members(bot: "WilByteBot") -> tuple[list, list[str]]:
    """The member list, from memory when it was read recently enough."""
    held = getattr(bot, "_levinson_members", None)
    read_at = getattr(bot, "_levinson_members_at", 0.0)
    if held is not None and time.time() - read_at < MEMBERS_GOOD_FOR:
        return held, []

    members, notes = await asyncio.to_thread(jobs.levinson_members, bot.config)
    if members:
        bot._levinson_members = members
        bot._levinson_members_at = time.time()
    return members, notes


async def handle_dispute(bot: "WilByteBot", message) -> None:
    """One chargeback notification, read and flagged. Nothing is sent.

    The first step of a longer job: a dispute lands, somebody has to notice
    it, gather the agent's sheet and invoice and contract and conversation,
    and write the rebuttal. Until now noticing it meant reading the channel,
    and starting meant pasting the notice back to RYTE by hand.

    So he reads what he can off the notice itself and says what is still
    missing, with the link back to it. Nothing is uploaded, nobody is removed,
    and no chargeback is answered without somebody doing it.

    The flag goes to the board channel rather than under the notice. The
    dispute channel is the acquirer's record of what happened, read by people
    who are not doing anything about it, and a reply under every notice turns
    it into a conversation - "i dont want it responding on the dispute
    channel".
    """
    from .. import rebuttal as rules_doc

    # The notification is an embed - the content of the message itself is
    # "@here" and nothing else - so the whole thing is read, title, description
    # and fields alike. Which part carries them is up to whoever built the
    # automation, and it is the same reason the payments watcher reads it all.
    said = _all_text(message).strip()
    if not said:
        return

    # Read off the labels only. `named_in` is for a command line and would
    # take the first four words of the notice as somebody's name.
    found = rules_doc.read_facts(said)

    # Nothing that looks like a dispute at all. Somebody talking in the
    # channel is not a chargeback, and a flag on every message is a flag
    # nobody reads.
    holes = found.missing()
    if len(holes) >= 3 and not found.arn and not found.mid:
        return

    lines = [
        f"**{label}** — {value}"
        for label, value in (
            ("Customer", found.customer_name),
            ("Amount", found.amount),
            ("Transaction", found.transaction_date),
            ("Dispute date", found.dispute_date),
            ("Reason", found.reason),
            ("ARN", found.arn),
        ) if value
    ]
    where = getattr(message, "jump_url", "")
    note = "⚖️ **Chargeback**\n" + "\n".join(f"• {one}" for one in lines)
    if holes:
        note += "\n⚠ Still needed: " + ", ".join(f"**{one}**" for one in holes)
    if where:
        note += f"\n[The notice]({where})"
    note += (
        "\n-# Reply to it with `@RYTE rebuttal` and the text screenshots "
        "attached. Their card, what they ordered, the setup confirmations and "
        "the delivered lead sheet I find myself. Uploading it to ElevateQS is "
        "still yours."
    )

    responder = _chargeback_responder(bot)
    if responder is None:
        log.warning("A chargeback landed and there is no channel to say so in")
        return
    try:
        await responder.send(note)
    except Exception:
        log.exception("Couldn't flag that chargeback")


async def handle_payment(bot: "WilByteBot", message) -> None:
    """One Payra notification, onto the Levinson tracker if it is theirs.

    Every payment in the channel comes through here and most are nothing to do
    with Levinson - "all payments are notified the same thing so there's no
    identifying if its levinson agent unless you know their contact
    information". The ones that aren't theirs leave no trace at all: no row, no
    message, nothing to scroll past.

    Writing is the same append the monthly command does, so a payment that
    reaches the sheet twice - live now and again when somebody runs the month -
    is added once.
    """
    from .. import levinson

    zone = ZoneInfo(bot.config.schedule.timezone)
    when = getattr(message, "created_at", None)
    paid = levinson.read_payment(
        _all_text(message),
        paid_at=when.astimezone(zone) if when is not None else datetime.now(zone),
    )
    if paid is None:
        return

    # Not knowing who the Levinson agents are is not the same as knowing this
    # isn't one of them, and a payment dropped in silence is the failure this
    # whole report exists to avoid. Both ways of not knowing get said out loud.
    try:
        members, notes = await _levinson_members(bot)
    except Exception as exc:
        log.warning("Couldn't read the Levinson members: %s", exc)
        await _grumble(bot, "levinson-members", (
            "A payment landed and I can't tell whether it's a Levinson agent — "
            f"{_readable(exc)}"
        ))
        return

    if not members:
        await _grumble(bot, "levinson-members", (
            "A payment landed and I can't tell whether it's a Levinson agent — "
            + (notes[0] if notes else "the member list came back empty.")
        ))
        return

    lines = levinson.lines_for([paid], members)
    if not lines:
        return

    (line,) = lines
    written, problems = await asyncio.to_thread(
        jobs.write_levinson, bot.config,
        [((paid.paid_at.year, paid.paid_at.month), lines)],
    )

    responder = _board_responder(bot)
    if responder is None:
        return
    if problems:
        await responder.send(embed=embeds.error(
            f"Couldn't put {line.name}'s payment on the Levinson tracker\n"
            + "\n".join(problems)
        ))
        return
    if not written:
        return
    await responder.send(
        f"📗 **{line.name}** — {line.amount}"
        + (f" — {line.product}" if line.product else "")
        + f" → {levinson.tab_for(paid.paid_at.year, paid.paid_at.month)}"
    )


async def _grumble(bot: "WilByteBot", about: str, said: str) -> None:
    """Say something that is wrong, and not again for an hour.

    The payment channel carries better than a thousand messages a month. A
    warning that repeats per payment is a warning nobody reads by lunchtime.
    """
    last = getattr(bot, "_grumbles", None)
    if last is None:
        last = bot._grumbles = {}
    now = time.time()
    if now - last.get(about, 0.0) < SAY_AGAIN_AFTER:
        return
    last[about] = now
    responder = _board_responder(bot)
    if responder is not None:
        await responder.send(embed=embeds.error(said))


async def _levinson_report(
    bot: "WilByteBot", responder: Responder, config: Config, said: str
) -> None:
    """Who Levinson sent us and what they paid, a month at a time.

    Read and shown before it is written. The sheet goes to an agency partner
    as a statement of what they are owed, so the numbers get looked at by a
    person once before they land on it.
    """
    from .. import levinson

    today = await asyncio.to_thread(jobs.board_day, config)
    asked = levinson.months_named(said, today=today)
    if not asked:
        await responder.send(
            "Which month? `@RYTE levinson`, `levinson last month`, "
            "`levinson august`, or `levinson june and july`."
        )
        return

    named = ", ".join(levinson.tab_for(year, month) for year, month in asked)
    await responder.send(f"Reading {named} — nothing will be written yet.")

    members, notes = await asyncio.to_thread(jobs.levinson_members, config)

    batches, blocks, count, seen = [], [], 0, 0
    for year, month in asked:
        payments, problem = await _payments_in(bot, year, month)
        if problem:
            await responder.send(embed=embeds.error(problem))
            return
        lines = levinson.lines_for(payments, members)
        batches.append(((year, month), lines))
        count += len(lines)
        seen += len(payments)

        head = (
            f"**{levinson.tab_for(year, month)}** — {len(payments)} payment(s) in "
            f"the channel, {len(lines)} from Levinson agents, "
            f"{levinson.total(lines)}."
        )
        listed = "\n".join(
            f"• {line.paid_on:%b %d} — **{line.name}** — {line.amount}"
            + (f" — {line.product}" if line.product else "")
            for line in lines[:40]
        )
        if len(lines) > 40:
            listed += f"\n-# and {len(lines) - 40} more"
        blocks.append(head + ("\n" + listed if listed else ""))

    report = "\n\n".join(blocks)
    if notes:
        report = "\n".join(f"⚠ {note}" for note in notes) + "\n" + report
    if not count:
        await responder.send(report)
        return

    view = views.ConfirmView(
        requester_id=responder.requester_id,
        timeout=config.discord.approval_timeout_seconds,
        label=f"Add {count} to the sheet",
        emoji="📗",
    )
    await responder.send(report, view=view)
    await view.wait()
    if not view.confirmed:
        return

    written, problems = await asyncio.to_thread(jobs.write_levinson, config, batches)
    note = (
        f"📗 Added {written} row(s), each on its own month's tab."
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
    # The live payment watcher has no other sign of life. Everything else here
    # announces itself the first time somebody uses it; this one is silent by
    # design until a Levinson agent pays, and "no line yet" and "never armed"
    # look identical from the outside. So it says at boot which it is.
    (
        "discord_payment_channel_id", "DISCORD_PAYMENT_CHANNEL_ID", False,
        "watch for Levinson payments as they land",
    ),
    ("levinson_sheet_id", "LEVINSON_SHEET_ID", False, "write the Levinson tracker"),
]


def clocks(config: Config) -> tuple[str, str]:
    """(what to say about the two clocks, "info" or "warning").

    RYTE keeps the board on its own timezone whatever the machine is set to,
    so the two disagreeing breaks nothing here - it breaks the reading. Every
    log line, and every timestamp Discord renders, comes out in the machine's
    zone; on a Mac twelve hours ahead, "live tomorrow" and the clock beside it
    describe different days, and the person in the middle does the arithmetic.
    So it is said at boot rather than discovered from a wrong answer.
    """
    board = datetime.now(ZoneInfo(config.schedule.timezone))
    here = datetime.now().astimezone()
    said = f"board {board:%a %b %d %H:%M %Z}"
    if here.utcoffset() == board.utcoffset():
        return f"{said} — this Mac agrees", "info"

    hours = (here.utcoffset() - board.utcoffset()).total_seconds() / 3600
    # The offset rather than the abbreviation: Asia/Manila renders as "PST",
    # which on a line next to EDT reads as US Pacific and sends somebody
    # looking three thousand miles from the problem.
    return (
        f"{said} — this Mac says {here:%a %b %d %H:%M} {_utc_offset(here)}, "
        f"{abs(hours):g} hours {'ahead' if hours > 0 else 'behind'}"
    ), "warning"


def _utc_offset(when: datetime) -> str:
    """"UTC+08" - how far off the machine is, in the one form nobody misreads."""
    minutes = int((when.utcoffset() or timedelta()).total_seconds() // 60)
    sign = "-" if minutes < 0 else "+"
    hours, left = divmod(abs(minutes), 60)
    return f"UTC{sign}{hours:02d}" + (f":{left:02d}" if left else "")


def preflight(config: Config) -> list[str]:
    """Log which credentials are present. Returns the missing required ones."""
    log.info("RYTE starting up")
    log.info("config: %s", config.path)
    log.info("state:  %s", DEFAULT_OUTPUT_DIR)

    # Words somebody taught RYTE about lead types, back into the matching
    # before the first card is read. Without this they last until the Mac is
    # restarted, which is the same as not being learned at all.
    from .. import agents as rules, vocab

    held = vocab.load()
    rules.taught(held)
    if held:
        log.info("words:  %d lead-type word(s) I was taught", len(held))

    said, how = clocks(config)
    log.info("clock:  %s", said) if how == "info" else log.warning("clock:  %s", said)
    if how == "warning":
        log.warning(
            "         RYTE still works off the board's clock; it's the log "
            "timestamps and Discord that will read wrong to you"
        )

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

    # The three watchers, said out loud because none of them has any other
    # sign of life: each is silent until the thing it watches for happens, and
    # "quiet morning" and "never started" look identical from the outside.
    for on, name, what in (
        (config.secrets.trello_auto, "TRELLO_AUTO", "walk the board on the clock"),
        (config.secrets.trello_agents_auto, "TRELLO_AGENTS_AUTO",
         "file new agents, and watch for wrong leads and wrong days"),
        (config.secrets.discord_dispute_channel_id, "DISCORD_DISPUTE_CHANNEL_ID",
         "read and flag chargeback notifications as they land"),
        (config.secrets.discord_chargeback_channel_id, "DISCORD_CHARGEBACK_CHANNEL_ID",
         "say them in their own channel rather than with the board"),
        (config.secrets.discord_clients_guild_id, "DISCORD_CLIENTS_GUILD_ID",
         "the server the agents' own channels live in"),
        # The other half of a clear-out. Without it the first button fails
        # after the picture has been taken, which is safe but is a thing to
        # find out before starting rather than halfway through.
        (config.secrets.clients_sheet_link, "CLIENTS_SHEET_LINK",
         "the ALL CLIENTS tab a closed-down agent's sheet link is kept in"),
        (config.secrets.trello_tags_auto, "TRELLO_TAGS_AUTO",
         "offer new comments and description lines as they are written"),
    ):
        log.info("  [%s]  %-20s %s", " on" if on else "off", name, what)
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


def _quieten_http() -> None:
    """Keep every Trello and Google request out of the window.

    The agent watcher looks at the board every twenty seconds and costs about
    sixteen requests, so httpx's one-line-per-request logging is roughly three
    thousand lines an hour of "200 OK". A window at that volume is one nobody
    reads, and the two real failures so far - a read timeout and a 422 - each
    sat buried in thousands of lines of it.

    Warnings and errors still come through, and RYTE's own lines are
    untouched. Set RYTE_LOG_HTTP=true to put the requests back when a
    misbehaving API is the thing being looked at.
    """
    if os.getenv("RYTE_LOG_HTTP", "").strip().lower() in ("1", "true", "yes"):
        return
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def run_bot(config: Config | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    _quieten_http()

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
                "Discord requires the intents this bot asked for to be enabled in "
                "the developer portal, under Bot -> Privileged Gateway Intents:"
            )
            log.error(
                "  Message Content - for the watched, SOP, payment and dispute "
                "channels. Or unset those; mentions work without it."
            )
            log.error(
                "  Server Members - for DISCORD_CLIENTS_GUILD_ID, so somebody "
                "can be found by name in the agents' server. Or unset that."
            )
            log.error(
                "Nothing else runs until one or the other is settled, which is "
                "why this stops rather than carrying on without them."
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
