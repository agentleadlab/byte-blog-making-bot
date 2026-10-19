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
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import discord
from discord import app_commands

from .. import corpus
from .. import waiting
from .. import mentionseen
from .. import watchedseen
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
from . import embeds, jobs, mentions, mirror
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
        # What each job spends on Claude, counted as the answers come back.
        from .. import usage

        usage.install()
        # Ryte The Goat: every channel's twin, when any are set.
        from . import mirror

        mirror.configure(
            self, mirror.parse_pairs(
                getattr(config.secrets, "discord_copies", "") or "", config.secrets,
            ),
            tags_in_copies_only=getattr(config.secrets, "discord_tag_in_copies_only", False),
            announce_into=getattr(config.secrets, "discord_announce_copy_channel_id", None),
            clearout_into=getattr(config.secrets, "discord_clearout_copy_channel_id", None),
        )
        self.tree = app_commands.CommandTree(self)
        self.run_lock = asyncio.Lock()
        self.publisher_task: asyncio.Task | None = None
        self.updater_task: asyncio.Task | None = None
        self.caption_task: asyncio.Task | None = None
        self.watch_task: asyncio.Task | None = None
        self.board_task: asyncio.Task | None = None
        self.agent_task: asyncio.Task | None = None
        self.float_task: asyncio.Task | None = None
        self.setup_task: asyncio.Task | None = None
        self.day_task: asyncio.Task | None = None
        self.contract_task: asyncio.Task | None = None
        self.delivery_task: asyncio.Task | None = None
        self.paid_task: asyncio.Task | None = None
        self.live_task: asyncio.Task | None = None
        self.tags_task: asyncio.Task | None = None
        self.ring_task: asyncio.Task | None = None
        self.offered_the_run = False
        self.recordings_task: asyncio.Task | None = None
        self.catchup_task: asyncio.Task | None = None
        # Mentions typed while RYTE was away: read back for on every connect.
        self.mention_task: asyncio.Task | None = None
        self.alive_task: asyncio.Task | None = None
        #: When the connection to Discord last dropped, while this copy kept
        #: running. A restart is known from the file; a dropped connection only
        #: from here.
        self.gone_since: datetime | None = None

    async def setup_hook(self) -> None:
        register_commands(self)
        # Held orders' buttons, answered after a restart too.
        views.HeldButton.on_press = lambda interaction, key, act: _held_pressed(self, interaction, key, act)
        try:
            self.add_dynamic_items(views.HeldButton)
        except Exception:
            log.warning("Couldn't set up the held orders' buttons", exc_info=True)
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
        # The page scripts the GHL pages load from leadlabcrm.com, kept the
        # same as the copies here - so an update reaches every page by itself.
        if getattr(self.config.secrets, "wordpress_app_password", None) and not getattr(self, "site_code_sent", False):
            self.site_code_sent = True
            self.loop.create_task(_site_code_once(self.config))
        if self.updater_task is None or self.updater_task.done():
            self.updater_task = self.loop.create_task(updater_loop(self))
        # Videos announced before YouTube had finished captioning them. Also
        # safe every start: the list is on disk, so a restart mid-wait picks
        # up where it left off.
        if self.caption_task is None or self.caption_task.done():
            self.caption_task = self.loop.create_task(caption_loop(self))
        # Videos announced while RYTE was offline - no wifi, the Mac asleep.
        if self.config.secrets.discord_watch_channel_ids and (
            self.watch_task is None or self.watch_task.done()
        ):
            self.watch_task = self.loop.create_task(watch_catch_up_loop(self))
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
        # Unticked agents kept on top of Done - the same switch: it is about
        # the agents, and it moves cards.
        if self.config.secrets.trello_agents_auto and (
            self.float_task is None or self.float_task.done()
        ):
            self.float_task = self.loop.create_task(float_loop(self))
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
        # Every order going live needs a signed contract of its own. Only once
        # PandaDoc is set up, and on the agents switch - it is about them.
        if (
            self.config.secrets.trello_agents_auto
            and getattr(self.config.secrets, "pandadoc_api_key", None)
            and (self.contract_task is None or self.contract_task.done())
        ):
            self.contract_task = self.loop.create_task(contract_check_loop(self))
        # Paid in Payra with nothing on the board for it: only when asked,
        # `@RYTE not set up`. Its first run on its own posted every aged-lead
        # order of the last two weeks at once - "WTH IS THIS SPAMMMINGG".
        # A go-live day changed after the agent was filed. On the agents
        # switch - it is about them - and reads only.
        if self.config.secrets.trello_agents_auto and (
            self.live_task is None or self.live_task.done()
        ):
            self.live_task = self.loop.create_task(live_moved_loop(self))
        # Proof of delivery, each morning: short orders said once. On the
        # agents switch - it is about them - and reads only.
        if self.config.secrets.trello_agents_auto and (
            self.delivery_task is None or self.delivery_task.done()
        ):
            self.delivery_task = self.loop.create_task(delivery_check_loop(self))
        # A comment is somebody handing over a job, which happens all day and
        # on nobody's schedule. Its own switch, because it writes onto four
        # people's live checklists rather than reporting.
        if self.config.secrets.trello_tags_auto and (
            self.tags_task is None or self.tags_task.done()
        ):
            self.tags_task = self.loop.create_task(tags_loop(self))
        # Faith's texts, read so Franklin can be told how she would answer.
        # Always on once RingCentral is set up: it only ever reads, and all it
        # produces is a message to him. Idle until .env has the keys.
        if self.ring_task is None or self.ring_task.done():
            self.ring_task = self.loop.create_task(ring_loop(self))
        # A quiet run the restart cut short. Once a start, not once a
        # reconnect: on_ready fires again after every dropped connection.
        if not self.offered_the_run:
            self.offered_the_run = True
            _also_running(self.loop.create_task(_offer_the_run(self)))
            _also_running(self.loop.create_task(_catch_up_payra(self)))
            # Gavels dropped while RYTE was down.
            _last_alive(self)
            _also_running(self.loop.create_task(_catch_up_gavels(self)))
            # The down alarm: say how long RYTE was gone, then check in
            # every five minutes for as long as it's up.
            _also_running(self.loop.create_task(alive_loop(self)))
            # The hub's fulfilled orders against the agents' sheets - only
            # once Nova's read-only key is in .env.
            if getattr(self.config.secrets, "hub_api_token", None):
                _also_running(self.loop.create_task(hub_check_loop(self)))
                # The setups going live, on the Ryte Checklist card.
                if getattr(self.config.secrets, "trello_key", None) and not CHECKLIST_PAUSED:
                    _also_running(self.loop.create_task(setup_check_pass_loop(self)))
            if getattr(self.config.secrets, "trello_key", None) and TEAM_TASKS_ON:
                _also_running(self.loop.create_task(team_tasks_loop(self)))
            # Notes in an order's thread the card missed - a restart in the
            # two minutes after one is posted loses its update otherwise.
            if getattr(self.config.secrets, "trello_key", None):
                _also_running(self.loop.create_task(order_sweep_loop(self)))
                _also_running(self.loop.create_task(agency_fill_loop(self)))
                _also_running(self.loop.create_task(tag_items_loop(self)))
            if getattr(self.config.secrets, "payra_api_token", None) and getattr(
                self.config.secrets, "payra_site_id", None
            ):
                _also_running(self.loop.create_task(payra_loop(self)))
                # Aged cards waiting on a bank payment to settle.
                if getattr(self.config.secrets, "trello_key", None):
                    _also_running(self.loop.create_task(bank_hold_loop(self)))
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
        # Anything @RYTE said while he was restarting or offline. On every
        # connect, not once a start: a dropped connection loses messages the
        # same way a restart does.
        if self.mention_task is None or self.mention_task.done():
            self.mention_task = self.loop.create_task(mention_catch_up(self))
        await _warn_if_stale(self)

    async def on_disconnect(self) -> None:
        if self.gone_since is None:
            self.gone_since = datetime.now(timezone.utc)

    async def on_raw_reaction_remove(self, payload) -> None:
        """The gavel taken off an order by somebody whose gavel counts: its
        card forgotten, so the next gavel makes a fresh one."""
        from .. import ordercards

        if not ordercards.is_gavel(payload.emoji) or not _gavel_counts(self, payload):
            return
        try:
            await _order_ungavelled(self, payload)
        except Exception:
            log.exception("Couldn't forget that order")

    async def on_raw_reaction_add(self, payload) -> None:
        """An emoji on a request in the Growth server, from the one it was
        asked of: done."""
        if self.user is not None and payload.user_id == self.user.id:
            return
        from .. import ordercards

        if ordercards.is_gavel(payload.emoji):
            log.info("Gavel on message %s by %s", payload.message_id, payload.user_id)
            if _gavel_counts(self, payload):
                _also_running(self.loop.create_task(_order_gavelled(self, payload)))
            else:
                _also_running(self.loop.create_task(_order_gavel_refused(self, payload)))
            return
        if not TEAM_TASKS_ON:
            return
        if not _in_setup_guild(self, self.get_guild(payload.guild_id) if payload.guild_id else None):
            return
        try:
            who = getattr(payload.member, "display_name", None) or str(payload.user_id)
            await _team_task_closes(self, str(payload.message_id), payload.user_id, who, emoji=payload.emoji)
        except Exception:
            log.exception("Couldn't tick off that request")

    async def on_raw_message_edit(self, payload) -> None:
        """A note in an order's thread edited after its card was made: the card
        brought up to date the same as for a new note - Kharyl added "LIVE
        TOM, OCT 16" to Trey Autry's by editing, and the card never heard."""
        author = (getattr(payload, "data", None) or {}).get("author") or {}
        if author.get("bot") or (self.user is not None and str(author.get("id")) == str(self.user.id)):
            return
        guild = self.get_guild(payload.guild_id) if getattr(payload, "guild_id", None) else None
        if not _in_setup_guild(self, guild):
            return
        channel = self.get_channel(payload.channel_id)
        if channel is None:
            return
        try:
            _order_note_posted(self, SimpleNamespace(channel=channel))
        except Exception:
            log.exception("Couldn't follow up an edited note")

    async def on_message(self, message: discord.Message) -> None:
        if self.user is not None and message.author.id == self.user.id:
            return
        if is_direct_mention(message, self.user):
            # In the Growth server a tag with no command in it is somebody
            # pointing RYTE at an order - the gavel does the rest, and RYTE
            # has nothing to say there.
            if _in_setup_guild(self, getattr(message, "guild", None)) and mentions.parse(
                    message.content or "").action == "help":
                if not getattr(message.author, "bot", False):
                    _order_note_posted(self, message)
                return
            await answer_mention(self, message)
            return
        if not getattr(message.author, "bot", False) and _in_setup_guild(self, getattr(message, "guild", None)):
            _order_note_posted(self, message)
        # An aged order's payment: its card made from the Payra invoice's note.
        if (AGED_FROM_INVOICE and getattr(message.author, "bot", False)
                and _in_setup_guild(self, getattr(message, "guild", None)) and _aged_payment(message)):
            _also_running(self.loop.create_task(_aged_from_invoice(self, message)))
        # The Growth server: somebody asking a teammate to do something, or
        # saying it's done. Read only - nothing is said there.
        if (TEAM_TASKS_ON and not getattr(message.author, "bot", False)
                and _in_setup_guild(self, getattr(message, "guild", None))):
            _also_running(self.loop.create_task(_team_message(self, message)))
        # Franklin replying to a suggestion or a lesson: that is him saying
        # what RYTE got wrong, and the best teacher there is.
        if _is_ring_reply(message):
            try:
                with mirror.answering():
                    await _ring_told(message)
            except Exception:
                log.exception("Couldn't take that correction")
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
            return
        # Ryte The Goat: no tag needed. Only there.
        if talks_to_ryte(message, self.config, self.user):
            await answer_mention(self, message, untagged=True)


#: How recently somebody has to have tagged RYTE for a restart to be said.
#: "no need for ryte to show updates here unless he was tag".
TAGGED_LATELY = timedelta(minutes=10)

#: (when, channel) of the last @RYTE - where to say "back in a moment".
_LAST_TAGGED: list = []

#: {(channel, person): (when, what they asked)} - for "how about today".
_LAST_ASKED: dict = {}


def _tagged_lately(now: datetime):
    """The channel somebody tagged RYTE in within the last ten minutes, or None."""
    if not _LAST_TAGGED:
        return None
    when, channel = _LAST_TAGGED[-1]
    return channel if now - when <= TAGGED_LATELY else None


async def _chat(responder: Responder, config: Config, message) -> None:
    """Answer somebody who talked to RYTE rather than giving it a command -
    from what it knows, with some attitude. Talks only."""
    from .. import chat, sops

    question = mentions.MENTION_RE.sub(" ", str(message.content or ""))
    question = " ".join(mentions.ROLE_MENTION_RE.sub(" ", question).split())
    history = []
    try:
        async for one in message.channel.history(limit=chat.HISTORY, before=message):
            said = " ".join(str(getattr(one, "content", "") or "").split())
            if not said:
                continue
            who = getattr(one.author, "display_name", None) or getattr(one.author, "name", "?")
            if getattr(one.author, "bot", False):
                who = f"{who} (bot)"
            history.append(f"{who}: {said[:400]}")
        history.reverse()
    except Exception:  # no history is a worse answer, not no answer
        log.info("Couldn't read the channel before answering", exc_info=True)
    try:
        index = await asyncio.to_thread(sops.load_index)
        titles = {hit.title for hit in sops.index_matches(index, sops.wanted_topic(question), limit=3)}
        matched = [one for one in index if one.get("title") in titles]
    except Exception:
        matched = []
    said = await asyncio.to_thread(
        chat.answer, config, question,
        who=str(getattr(message.author, "display_name", "") or ""),
        today=_today(config), history=history, sops=matched, commands=mentions.HELP_TEXT,
    )
    await responder.send(said[:1900], quiet=True)


async def answer_mention(bot: "WilByteBot", message, *, untagged: bool = False) -> None:
    """Answer one @RYTE, and write down that it was answered. `untagged`: a
    message in Ryte The Goat, answered without the tag."""
    _LAST_TAGGED[:] = [(datetime.now(timezone.utc), getattr(message, "channel", None))]
    said = getattr(message, "id", None)
    try:
        if said is not None:
            await asyncio.to_thread(mentionseen.add, said)
    except Exception:  # remembering it is for the catch-up; answering comes first
        log.warning("Couldn't note mention %s as answered", said, exc_info=True)
    # Somebody is waiting on this one. discord.py logs an exception in an
    # event handler and says nothing, so a bug leaves them looking at "Reading
    # the board —" with no second message ever coming, and no way to tell that
    # from RYTE being slow.
    try:
        # Spoken to: the answer goes where it was asked, Growth server or not.
        with mirror.answering():
            await handle_mention(bot, message, untagged=untagged)
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


# ------------------------------------------------ mentions made while away

#: The furthest back a start reads for mentions it missed. A restart is
#: seconds; this is for the wifi going for an afternoon. Yesterday's command
#: is not something to start doing today.
MENTION_CATCH_UP_HOURS = 12

#: Younger than this and it is simply done: somebody typed it just before an
#: update and is still sitting there waiting for the answer. Older, and it is
#: asked about first - an hour on, it may have been done by hand, or not be
#: wanted any more.
MENTION_ASK_AFTER_MINUTES = 10

#: How often RYTE writes down that it is still listening.
MENTION_ALIVE_SECONDS = 60

#: A little before the last time it was known to be listening, so a mention
#: typed in the same minute as the restart is not lost in the gap.
MENTION_OVERLAP = timedelta(minutes=2)


def _where_mentions_are_answered(bot: "WilByteBot", since: datetime) -> list:
    """Every text channel RYTE answers in that has had a message since then.

    The last message's id carries its time, so the channels nobody has
    written in are passed over without a request - the clients server alone
    is a hundred and eighty channels.
    """
    found = []
    for channel in bot.get_all_channels():
        if not isinstance(channel, discord.TextChannel):
            continue
        last = getattr(channel, "last_message_id", None)
        if not last or discord.utils.snowflake_time(last) < since:
            continue
        if _never_speaks_in(bot, channel):
            continue
        found.append(channel)
    return found


def _never_speaks_in(bot: "WilByteBot", channel) -> bool:
    """A channel RYTE only reads, or one it isn't enabled in."""
    if is_watched(SimpleNamespace(channel=channel), bot.config):
        return True
    allowed, reason = is_allowed(
        channel_id=channel.id, user=SimpleNamespace(roles=[]), config=bot.config,
        channel_name=str(getattr(channel, "name", "") or ""),
        guild_id=getattr(getattr(channel, "guild", None), "id", None),
    )
    # The Growth server's every channel, as when RYTE is there to hear it:
    # Kharyl's "add to trello and tag therese" in #internal-team, sent while
    # RYTE was restarting, was never answered.
    if not allowed and "channel" in reason and _in_setup_guild(bot, getattr(channel, "guild", None)):
        return False
    return not allowed and "channel" in reason


def _asked_the_same(one, other) -> bool:
    return one.author.id == other.author.id and (
        " ".join((one.content or "").split()).casefold()
        == " ".join((other.content or "").split()).casefold()
    )


async def missed_mentions(bot: "WilByteBot", *, since: datetime, until: datetime,
                          channels=None) -> list:
    """The mentions nobody answered, oldest first.

    Typed after `since` and before `until` - the moment RYTE was back, after
    which they come in the ordinary way. Not ones already answered; not ones
    RYTE has already replied to; and not ones the same person typed again
    later, because they got tired of waiting and the second is the one that
    counts.
    """
    done = await asyncio.to_thread(mentionseen.answered)
    missed = []
    for channel in channels if channels is not None else _where_mentions_are_answered(bot, since):
        try:
            said = [one async for one in channel.history(limit=200, after=since, oldest_first=True)]
        except discord.HTTPException:
            log.warning("Couldn't read back through %s for missed mentions", channel.id, exc_info=True)
            continue
        replied = {
            getattr(one.reference, "message_id", None)
            for one in said
            if bot.user is not None and one.author.id == bot.user.id and one.reference
        }
        asks = [one for one in said if is_direct_mention(one, bot.user)]
        for at, one in enumerate(asks):
            if one.created_at >= until:
                continue
            if str(one.id) in done or one.id in replied:
                continue
            if any(_asked_the_same(one, later) for later in asks[at + 1:]):
                continue
            missed.append(one)
    return sorted(missed, key=lambda one: one.created_at)


async def _ask_about_missed(bot: "WilByteBot", message) -> None:
    """A mention from a while ago: ask before doing it."""
    view = views.ConfirmView(
        requester_id=message.author.id,
        timeout=bot.config.discord.approval_timeout_seconds,
        label="Do it now",
        emoji="↩️",
    )
    stamp = int(message.created_at.timestamp())
    await message.reply(
        f"↩️ I was offline when you sent this (<t:{stamp}:R>), so it never got "
        "an answer. Still want it?",
        view=view, mention_author=False,
    )
    await view.wait()
    if view.confirmed:
        await answer_mention(bot, message)


async def mention_catch_up(bot: "WilByteBot", *, now: datetime | None = None) -> None:
    """Answer what was said to RYTE while he wasn't there to hear it.

    Only from the last time he is known to have been listening. The first
    start ever has nothing to go back to, and reading back a day of the
    team's messages then would answer things long since dealt with.
    """
    now = now or datetime.now(timezone.utc)
    gone, bot.gone_since = bot.gone_since, None
    try:
        since = gone or await asyncio.to_thread(mentionseen.listening_at)
    except Exception:
        log.exception("Couldn't tell when I was last listening")
        since = None
    # Read before this starts writing, and started before any answering: a
    # blog run answered here can take ten minutes, and those are minutes
    # RYTE was listening.
    if bot.alive_task is None or bot.alive_task.done():
        bot.alive_task = asyncio.create_task(listening_loop(bot))
    try:
        if since is not None:
            since = max(since - MENTION_OVERLAP, now - timedelta(hours=MENTION_CATCH_UP_HOURS))
            for message in await missed_mentions(bot, since=since, until=now):
                allowed, reason = is_allowed(
                    channel_id=message.channel.id, user=message.author, config=bot.config,
                    channel_name=str(getattr(message.channel, "name", "") or ""),
                    guild_id=getattr(getattr(message, "guild", None), "id", None),
                )
                if not allowed and not ("channel" in reason
                                        and _in_setup_guild(bot, getattr(message, "guild", None))):
                    continue
                # Taken in hand before anything is said, so a second connect
                # in the middle of this does not answer it twice.
                await asyncio.to_thread(mentionseen.add, message.id)
                log.info("Answering a mention made while I was away: %s", message.id)
                if now - message.created_at <= timedelta(minutes=MENTION_ASK_AFTER_MINUTES):
                    await message.reply(
                        "↩️ I was restarting when you sent this — on it now.",
                        mention_author=False,
                    )
                    await answer_mention(bot, message)
                else:
                    _also_running(asyncio.create_task(_ask_about_missed(bot, message)))
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Catching up on missed mentions failed")


async def listening_loop(bot: "WilByteBot") -> None:
    """Write down, every minute, that RYTE is still here to hear a mention.

    Not while the connection is down: those minutes were not listened to,
    and the next connect has to read back over them.
    """
    while not bot.is_closed():
        if bot.gone_since is None:
            try:
                await asyncio.to_thread(mentionseen.listening, datetime.now(timezone.utc))
            except Exception:
                log.warning("Couldn't note that I'm listening", exc_info=True)
        await asyncio.sleep(MENTION_ALIVE_SECONDS)


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
    return bool(where) and str(where) in _channels_of(message)


def is_dispute(message, config: Config) -> bool:
    """True for a message in the channel chargeback notifications land in.

    Not filtered by author, the same as the payments channel: the acquirer's
    notices arrive through something else's account, and somebody chose that
    channel for exactly this.
    """
    where = config.secrets.discord_dispute_channel_id
    return bool(where) and str(where) in _channels_of(message)


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


def _talk_guild(guild, config: Config) -> bool:
    """Whether this is Ryte The Goat - DISCORD_TALK_GUILD_ID, else by name."""
    if guild is None:
        return False
    wanted = (getattr(config.secrets, "discord_talk_guild_id", "") or "").strip()
    if wanted.isdigit():
        return str(getattr(guild, "id", "")) == wanted
    return "rytethegoat" in _plain_letters(getattr(guild, "name", ""))


def talks_to_ryte(message, config: Config, bot_user) -> bool:
    """"on ryte the goat, can ryte automatically respond even when no
    tagging? just on that server". A person's message there is for RYTE -
    unless it's for somebody else: tagging another person, @everyone or a
    role, or a reply to somebody who isn't RYTE. Everywhere else, a tag."""
    if bot_user is None or getattr(message.author, "bot", False):
        return False
    if not _talk_guild(getattr(message, "guild", None), config):
        return False
    content = str(getattr(message, "content", "") or "")
    if not content.strip() and not getattr(message, "attachments", None):
        return False
    if getattr(message, "mention_everyone", False) or getattr(message, "role_mentions", None):
        return False
    typed = set(re.findall(r"<@!?(\d+)>", content))
    if typed - {str(bot_user.id)}:
        return False
    ref = getattr(message, "reference", None)
    replied = getattr(ref, "resolved", None) if ref is not None else None
    if replied is not None and getattr(getattr(replied, "author", None), "id", None) not in (None, bot_user.id):
        return False
    return True


def is_sop_channel(message, config: Config) -> bool:
    """True for a message in a channel that feeds the SOP library.

    Unlike the watched announcement channel, this one is ours: RYTE files what
    lands here and says so, because a library nobody can see being filled is
    one nobody trusts.
    """
    channels = config.secrets.discord_sop_channel_ids
    return bool(channels) and bool(_channels_of(message) & set(channels))


def _channels_of(message) -> set[str]:
    """The channel a message is in - and, in a Ryte The Goat copy, the channel
    it is a copy of, so what works in one works in its twin."""
    from . import mirror

    here = getattr(getattr(message, "channel", None), "id", None)
    return {str(one) for one in (here, mirror.original_of(here)) if one is not None}


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

#: Files read as an SOP's contents: "Live Call Battle Sheet.pdf" came back
#: "There's nothing in that to file".
SOP_DOCUMENTS = (".pdf", ".docx", ".txt", ".md")
MOST_SOP_DOCUMENT_BYTES = 15 * 1024 * 1024


def _document_text(name: str, data: bytes) -> str:
    """The words in a PDF, Word file or text file - "" if none come out."""
    low = name.casefold()
    if low.endswith(".pdf"):
        return jobs._pdf_text(data)
    if low.endswith(".docx"):
        try:
            import io

            import docx

            return "\n".join(one.text for one in docx.Document(io.BytesIO(data)).paragraphs)
        except Exception:
            log.warning("Couldn't read %s", name, exc_info=True)
            return ""
    return data.decode("utf-8", errors="replace")


async def _with_documents(message, text: str, sop):
    """The SOP, with any document attached to the message read into it - or
    one made from the document alone. None when there's still nothing."""
    from .. import sops

    read = []
    for one in getattr(message, "attachments", None) or []:
        name = str(getattr(one, "filename", "") or "")
        if not name.casefold().endswith(SOP_DOCUMENTS) or (getattr(one, "size", 0) or 0) > MOST_SOP_DOCUMENT_BYTES:
            continue
        try:
            words = _document_text(name, await one.read())
        except Exception:
            log.warning("Couldn't download %s", name, exc_info=True)
            continue
        words = "\n".join(line.strip() for line in words.splitlines() if line.strip())
        if words:
            read.append((name, words))
    if not read:
        return sop
    written = "\n\n".join(f"{name}\n{words}" for name, words in read)
    if sop is not None:
        sop.body = (sop.body + "\n\n" + written).strip()
        sop.kind = sop.kind or "Document"
        return sop
    title = sops.find_title(text or "") or Path(read[0][0]).stem.replace("_", " ")[:120]
    return sops.Sop(
        title=title, named_by_hand=True, kind="Document",
        # The card points back at the message the file is on - Discord's own
        # link to the file stops working after a day.
        url=str(getattr(message, "jump_url", "") or ""), body=written,
    )


async def handle_sop_post(bot: WilByteBot, message) -> None:
    """File what somebody posted in the SOP channel."""
    from .. import sops

    if getattr(message.author, "bot", False):
        return

    if sops.already_filed(getattr(message, "id", "")):
        return

    images, audio = message_files(message)
    sop = sops.find_sop(message.content or "", images=images, audio=audio)
    sop = await _with_documents(message, message.content or "", sop)
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
        # Not taken in hand, so the catch-up has it once this batch is done.
        await responder.send(
            "A new video just landed but I'm mid-run — I'll write it up as soon "
            "as this batch is done."
        )
        return
    # Taken in hand from here - held for captions or written now - and never
    # picked up again by the catch-up, whatever happens next.
    await asyncio.to_thread(watchedseen.add, message.id)

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


#: How far back the announcement channel is read for videos RYTE missed.
WATCH_CATCH_UP_DAYS = 3
#: ...the first time, when nothing is remembered as done yet.
WATCH_FIRST_HOURS = 24
#: And how often, besides every start.
WATCH_CATCH_UP_SECONDS = 900


def _video_ids(links) -> set[str]:
    from .. import youtube

    found = set()
    for link in links:
        try:
            found.add(youtube.extract_video_id(link))
        except Exception:
            continue
    return found


async def missed_announcements(bot: "WilByteBot") -> list:
    """Video announcements RYTE never took in hand, oldest first.

    Not ones it has written, scheduled or published; not ones waiting on
    captions; not ones whose post Franklin deleted - that slot is his to
    rerun; and not ones it has already taken in hand.
    """
    first = not await asyncio.to_thread(watchedseen.exists)
    seen = set(await asyncio.to_thread(watchedseen.load))
    ledger = await asyncio.to_thread(Ledger.load)
    queue = await asyncio.to_thread(waiting.Queue.load)
    queued = _video_ids(queue.items)
    since = datetime.now(timezone.utc) - (
        timedelta(hours=WATCH_FIRST_HOURS) if first else timedelta(days=WATCH_CATCH_UP_DAYS)
    )
    missed = []
    for wanted in bot.config.secrets.discord_watch_channel_ids or ():
        channel = bot.get_channel(int(wanted)) if str(wanted).isdigit() else None
        if channel is None:
            continue
        try:
            async for message in channel.history(limit=50, after=since, oldest_first=True):
                if str(message.id) in seen:
                    continue
                videos = _video_ids(watched_links(message))
                if not videos:
                    continue
                if all(ledger.has(one) or one in ledger.freed or one in queued for one in videos):
                    continue
                missed.append(message)
        except discord.HTTPException:
            log.warning("Couldn't read back through %s for missed videos", wanted, exc_info=True)
    return missed


async def watch_catch_up_loop(bot: "WilByteBot") -> None:
    """Write up any video announced while RYTE wasn't there to see it.

    "i have no wifi right now ... dont want him missing posting". Everything
    else RYTE does looks at the board or the ledger when it wakes and does
    what is overdue; a video announcement is a message, and a message missed
    is gone unless somebody reads back for it. On every start, and every
    quarter of an hour, which also collects one that landed mid-run.
    """
    while not bot.is_closed():
        try:
            for message in await missed_announcements(bot):
                while bot.run_lock.locked():
                    await asyncio.sleep(10)
                log.info("Catching up on a video announced while I was away: %s", message.id)
                await handle_watched(bot, message)
        except asyncio.CancelledError:
            raise
        except Exception:  # a bad tick must not take the loop down for good
            log.exception("Catching up on missed videos failed; will try again")
        await asyncio.sleep(WATCH_CATCH_UP_SECONDS)


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


def is_allowed(
    *, channel_id: int | None, user, config: Config, channel_name: str = "",
    guild_id: int | None = None,
) -> tuple[bool, str]:
    """Channel and role gating. An empty allowlist means 'no restriction'."""
    channels = config.secrets.discord_channel_ids
    # A channel named as an SOP source is already an explicit permission for
    # that channel - RYTE files what lands in it. Making somebody add it to a
    # second list as well is a trap: he answers everywhere except the one room
    # the library is kept in.
    allowed_anyway = set(config.secrets.discord_sop_channel_ids)
    # And the channel the RingCentral suggestions go to, for the same reason:
    # it was named for RYTE to talk in. "@Ryte respond" there went unanswered
    # because it was on one list and not the other.
    ring_here = any(
        bool(ring) and (
            ring == str(channel_id)
            or (not ring.isdigit() and ring.casefold() == str(channel_name or "").casefold())
        )
        for ring in (
            str(getattr(config.secrets, setting, "") or "").lstrip("#").strip()
            for setting in ("ringcentral_channel_id", "ringcentral_shared_channel_id")
        )
    )
    from . import mirror

    # A copy in Ryte The Goat answers the same as the channel it copies.
    # And every channel in the copies' own server.
    a_copy = mirror.original_of(channel_id) is not None or mirror.in_copies_server(guild_id)
    if channels and not ring_here and not a_copy and str(channel_id) not in set(channels) | allowed_anyway:
        return False, "RYTE isn't enabled in this channel."

    roles = config.secrets.discord_role_ids
    if roles:
        member_roles = {str(r.id) for r in getattr(user, "roles", [])}
        if not member_roles & set(roles):
            return False, "You don't have the role required to run this."
    return True, ""


async def guard(interaction: discord.Interaction, config: Config) -> bool:
    allowed, reason = is_allowed(
        channel_id=interaction.channel_id, user=interaction.user, config=config,
        guild_id=interaction.guild_id,
    )
    if not allowed:
        await interaction.response.send_message(reason, ephemeral=True)
    else:
        # A slash command is somebody speaking to RYTE. Each one runs in its
        # own task, so this lasts as long as the command and no longer.
        mirror.spoken_to()
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


async def handle_mention(bot: WilByteBot, message: discord.Message, *, untagged: bool = False) -> None:
    config = bot.config

    # A watched channel is read-only, always. Not "unless the allowlist is
    # empty", not "unless someone asks nicely" - RYTE is in that server to see
    # when a video goes up and for nothing else, and this is the one rule that
    # cannot depend on a setting being filled in correctly.
    if is_watched(message, config):
        log.info("Mentioned in watched channel %s — staying quiet", message.channel.id)
        return

    allowed, reason = is_allowed(
        channel_id=message.channel.id, user=message.author, config=config,
        channel_name=str(getattr(message.channel, "name", "") or ""),
        guild_id=getattr(getattr(message, "guild", None), "id", None),
    )
    if not allowed and "channel" in reason and not untagged and _in_setup_guild(bot, getattr(message, "guild", None)):
        # The Growth server is the team's own: tagged in any channel of it,
        # RYTE answers. Faith's "@Ryte add to Trello @Therese ..." in
        # #internal-team went unanswered - the channel wasn't on the list.
        allowed = True
    if not allowed:
        # Silent where a channel allowlist exists, because RYTE now sits in a
        # server it is only there to read - an announcements feed with clients
        # in it. "RYTE isn't enabled in this channel" is still chatter, and it
        # would be posted in front of them. Where the refusal is about the
        # person rather than the place, say so: they are somewhere RYTE speaks.
        if untagged or (config.secrets.discord_channel_ids and "channel" in reason):
            # Nobody asked RYTE by name: not answering isn't worth a word.
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

    # "how about today" after "how many agent going live tomorrow": the same
    # question again for the new day, from the same person in the same place
    # within ten minutes.
    asked_by = (getattr(message.channel, "id", None), getattr(message.author, "id", None))
    now = datetime.now(timezone.utc)
    asked = message.content
    if request.action == "help" and mentions.said_anything(message.content):
        when, before = _LAST_ASKED.get(asked_by, (None, ""))
        again = mentions.follow_up(message.content, before) if when and now - when <= TAGGED_LATELY else None
        if again:
            request = mentions.parse(again, max_batch=config.discord.max_batch)
            asked = again
    if request.action != "help":
        # What was really asked, so "and friday?" after "how about today" works too.
        _LAST_ASKED[asked_by] = (now, asked)

    if request.action == "help":
        if mentions.wants_the_menu(message.content):
            # The version goes on the help text specifically - "that word is
            # new, this copy is old" is the likeliest reason a command wasn't
            # recognised.
            await responder.send(f"{mentions.HELP_TEXT}\n\n-# Running `{version.code_version()}`")
            return
        # Not a command, and not asking for the menu: talked to.
        async with _typing(message.channel):
            await _chat(responder, config, message)
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

            if request.action == "responder":
                await _set_responder(responder, request.brief or "")
                return

            if request.action == "hubcheck":
                await _hub_check(responder, config, request.brief or "", bot=bot)
                return

            if request.action == "update":
                await _update_now(bot, responder)
                return

            if request.action == "restorestate":
                await _restore_state(bot, responder, config, message)
                return

            if request.action == "cost":
                from .. import usage

                await responder.send(usage.report(await asyncio.to_thread(usage.load)))
                return

            if request.action == "payrafind":
                await responder.send(
                    f"🔎 Looking through Payra for “{request.brief}” — a page at a time, "
                    "this can take a few minutes…"
                )
                try:
                    said = await asyncio.to_thread(jobs.payra_find, config, request.brief or "")
                except Exception as exc:
                    said = f"Couldn't look: {_readable(exc)}"
                await responder.send(said)
                return

            if request.action == "payrastatus":
                from .. import payraapi

                await responder.send(payraapi.status(await asyncio.to_thread(payraapi.load)))
                return

            if request.action == "contracts":
                paired, problems = await asyncio.to_thread(jobs.contract_check, config)
                said = describe_contracts(paired, everything=True)
                if not said and not problems:
                    said = "Nobody going live today or tomorrow."
                await responder.send(said + "".join(f"\n⚠ {one}" for one in problems))
                return

            if request.action == "calendar":
                await _itinerary_calendar(responder, config, request.brief or "")
                return

            if request.action == "livemoved":
                await _live_moved_now(responder, config)
                return

            if request.action == "notsetup":
                await _not_set_up(responder, config)
                return

            if request.action == "delivered":
                await _delivered(responder, config, request.brief or "")
                return

            if request.action == "pandadoctest":
                from .. import pandadoc

                await responder.send(await asyncio.to_thread(
                    pandadoc.test, getattr(config.secrets, "pandadoc_api_key", "") or "",
                ))
                return

            if request.action == "payratest":
                await _payra_test(responder, config)
                return

            if request.action == "checkclearouts":
                await _check_clearouts(bot, responder, config)
                return

            if request.action == "redobell":
                with mirror.copying_into(mirror.CLEAROUT_INTO):
                    await _redo_bell(bot, responder, config, request.brief or "")
                return

            if request.action == "clearout":
                with mirror.copying_into(mirror.CLEAROUT_INTO):
                    await _clear_out(bot, responder, config, request.brief or "")
                return

            if request.action == "blacklist":
                await _blacklist_them(responder, config, request.brief or "")
                return

            if request.action == "golive":
                await _who_goes_live(responder, config, request.brief or "")
                return

            if request.action == "quiet":
                # Wherever it was asked, a copy in Channel Deletion - the whole
                # run, buttons and all.
                brief = request.brief or ""
                keeping = re.search(r"\b(unkeep|keep|kept)\b\s*(.*)$", brief, re.IGNORECASE | re.DOTALL)
                if keeping:
                    await _quiet_keep(bot, responder, config, keeping.group(1).casefold(), keeping.group(2))
                    return
                if re.search(r"\b(?:retry|again|redo|reset|forget)\b", brief, re.IGNORECASE):
                    from .. import quietauto

                    many = await asyncio.to_thread(quietauto.forget)
                    await responder.send(
                        f"🔄 The {many} channel(s) I'd left for you are back in the list - "
                        "`@RYTE quiet auto` goes through them again." if many
                        else "Nothing was being left for you - every quiet channel is already in the list.")
                    return
                is_auto = re.search(r"\bauto(?:matic(?:ally)?)?\b", brief, re.IGNORECASE)
                with mirror.copying_into(mirror.CLEAROUT_INTO):
                    if is_auto:
                        await _quiet_auto(bot, responder, config, brief)
                    else:
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

            if request.action == "tracker":
                await _to_the_tracker(responder, config, message, request.brief or "")
                return

            if request.action == "archive":
                await _archive_aged(responder, config)
                return

            if request.action == "agentsheet":
                await _send_sheet(responder, config, request.brief or "")
                return

            if request.action == "testimonial":
                await _website_testimonial(responder, config, request.brief or "")
                return

            if request.action == "postsegments":
                await _post_segments(responder, config, request.brief or "")
                return

            if request.action == "successremove":
                await _success_remove(responder, config, request.brief or "")
                return

            if request.action == "fullinterviews":
                await _full_interviews(responder, config)
                return

            if request.action == "doclayout":
                await _relayout_doc(responder, config, request.brief or "")
                return

            if request.action == "testimonialremove":
                await _website_take_down(responder, config, request.brief or "")
                return

            if request.action == "websitecheck":
                try:
                    said = await asyncio.to_thread(jobs.website_check, config)
                except Exception as exc:
                    said = f"⚠ {_readable(exc)}"
                await responder.send(said)
                return

            if request.action == "invoice":
                await _send_paid_invoice(responder, request.brief or "", config)
                return

            if request.action == "contract":
                await _send_contract(responder, config, request.brief or "")
                return

            if request.action == "respond":
                await _respond_like_faith(responder, config, message, request.brief or "")
                return

            if request.action == "askfaith":
                await _ask_about_faith(responder, config, request.brief or "")
                return

            if CHECKLIST_PAUSED and request.action in ("setupthreads", "rytechecklist"):
                await responder.send("⏸ The Ryte Checklist is paused — the team's back on Trello for setups. "
                                     "Nothing's been deleted; it's ready to switch back on.")
                return

            if request.action == "ordertemplate":
                kind, _, link = (request.brief or "").partition(" ")
                await _order_template_set(responder, kind, link, config)
                return

            if request.action == "orderrefresh":
                await _order_update_now(bot, responder, request.brief or "")
                return

            if request.action == "orderlink":
                await _order_from_link(bot, responder, request.brief or "", getattr(message, "author", None))
                return

            if request.action in ("gavelallow", "gavelremove"):
                await _gavel_allow(bot, responder, message, allowed=request.action == "gavelallow")
                return

            if request.action == "orderaudit":
                await _order_audit(bot, responder, request.brief or "")
                return

            if request.action == "dedupe":
                try:
                    removed, trouble = await asyncio.to_thread(jobs.remove_duplicate_items, config)
                except Exception as exc:
                    await responder.send(f"⚠ Couldn't read the cards: {_readable(exc)}")
                    return
                said = ([f"🧹 Took off {len(removed)} copied line(s):"] + [f"• {one}" for one in removed[:25]]
                        if removed else ["✅ No line is on any checklist twice."])
                said += [f"⚠ {one}" for one in trouble[:5]]
                await responder.send("\n".join(said)[:1950])
                return

            if request.action == "tagcheck":
                await responder.send(await _tag_check(bot))
                return

            if request.action == "setupbalance":
                await responder.send(await _setup_balance(bot, fix=bool(re.search(r"\bfix\b", request.brief or "", re.IGNORECASE))))
                return

            if request.action == "agencylookup":
                await responder.send(await _agency_lookup(bot, request.brief or ""))
                return

            if request.action == "orderbackfill":
                await _order_backfill(bot, responder, getattr(message, "author", None))
                return

            if request.action == "livelist":
                await _live_list(bot, responder)
                return

            if request.action == "invoicenote":
                await _invoice_note(bot, responder, request.brief or "")
                return

            if request.action == "ordercheck":
                await _order_check(bot, responder)
                return

            if request.action == "ordertemplates":
                await _order_templates_said(responder)
                return

            if request.action == "teamtrack":
                await _team_track(bot, responder, request.brief, getattr(message, "author", None))
                return

            if request.action == "hubtasks":
                await _team_hub_check(bot, responder)
                return

            if request.action == "teamtasks":
                await _team_list(responder, bot)
                return

            if request.action == "setupthreads":
                await _setup_threads_for(bot, responder, request.brief)
                return

            if request.action == "rytechecklist":
                await _ryte_checklist(bot, responder)
                return

            if request.action == "hubfields":
                await _hub_fields(responder, config, request.brief or "")
                return

            if request.action == "sitecode":
                await _site_code(responder, config)
                return

            if request.action == "faithvoice":
                await _faith_voice(responder, config, message, request.brief or "")
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

# "can the update be less than 15 minutes?" - a fix asked for in Discord is
# wanted while the person is still looking at the screen. A fetch is cheap,
# and a run that is open still holds the restart off. `@RYTE update` for now.
UPDATE_CHECK_SECONDS = 120


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

        if bot.run_lock.locked() or _clearing_out():
            log.info("Update %s is waiting, but a run is open - leaving it", waiting)
            continue

        # Said nowhere. "why is it sending the updates here again" - a blog
        # run tagged in #blogs-copywriter got "Updating myself" in the
        # middle of it. Only `@RYTE update`, which asked, is answered.
        log.info("Restarting onto %s", waiting)
        await bot.close()
        os._exit(RESTART_EXIT_CODE)


async def _restore_state(bot: "WilByteBot", responder: Responder, config: Config, message) -> None:
    """`@RYTE restore state` with ryte-state.tar.gz attached - the memory the
    Mac kept, unpacked here. Franklin's only, shown before it's written, the
    old memory kept aside, and the file taken out of Discord after."""
    from .. import corpus, statemove
    from ..state import _state_dir

    owner = str(getattr(config.secrets, "discord_notify_user_id", "") or "")
    if not owner or str(getattr(getattr(message, "author", None), "id", "")) != owner:
        await responder.send("Only Franklin can replace my memory.")
        return
    files = [one for one in getattr(message, "attachments", []) or []
             if str(one.filename).endswith((".tar.gz", ".tgz", ".zip"))]
    if not files:
        await responder.send("Attach the **ryte-state.tar.gz** that `scripts/pack-state.sh` made on the Mac.")
        return
    try:
        found = statemove.read(await files[0].read())
    except ValueError as exc:
        await responder.send(f"⚠ Couldn't use that file — {exc}.")
        return
    if not found.files:
        await responder.send("⚠ Nothing in that file is my memory — it should have a `state/` folder in it.")
        return
    view = views.ConfirmView(
        requester_id=responder.requester_id, timeout=config.discord.approval_timeout_seconds,
        label="Restore it", emoji="🧠",
    )
    await responder.send(
        f"🧠 **{found.count('state')}** memory file(s) and **{found.count('corpus')}** copy-library file(s), "
        f"{found.size / 1024 / 1024:.1f} MB."
        + (f"\n-# Left out (not memory): {', '.join(found.refused[:5])}" if found.refused else "")
        + "\nThis replaces what I remember here. What's here now is kept aside first. I restart after.",
        view=view,
    )
    await view.wait()
    if not view.confirmed:
        await responder.send("Left as it was.")
        return
    try:
        kept = await asyncio.to_thread(
            statemove.restore, found, state_dir=_state_dir(), corpus_dir=corpus.corpus_dir(),
            backup_dir=_state_dir().parent / "backups",
        )
    except OSError as exc:
        await responder.send(f"⚠ Couldn't write it: {exc}")
        return
    # Faith's texts and payments are in it - not left sitting in a channel.
    try:
        await message.delete()
        gone = "I've deleted the message with the file."
    except Exception:
        gone = "Delete the message with the file - it has client data in it."
    await responder.send(f"✅ Restored. The old memory is kept as `{kept.name}`. {gone} Restarting now.")
    await bot.close()
    os._exit(RESTART_EXIT_CODE)


async def _update_now(bot: "WilByteBot", responder: Responder) -> None:
    """`@RYTE update` - look for a newer version now rather than at the next
    check, and come back on it. The same rule as the loop: never with a run
    open."""
    try:
        waiting = await asyncio.to_thread(version.update_waiting)
    except Exception as exc:
        await responder.send(f"Couldn't check for an update: {jobs._short(exc, 200)}")
        return
    if not waiting:
        here = version.code_version() or "this version"
        if "not a git checkout" in here:
            # Railway: a push deploys by itself, there's nothing to pull.
            await responder.send("I'm hosted — every update deploys by itself as soon as it's pushed.")
            return
        await responder.send(f"Already on the latest — {here}.")
        return
    if bot.run_lock.locked() or _clearing_out():
        await responder.send(
            f"There's an update ({waiting}), but "
            + ("a clear-out is open - its buttons wouldn't work after a restart. " if _clearing_out()
               else "a run is open — ")
            + "I'll take it as soon as that's done."
        )
        return
    await responder.send(f"🔄 Updating myself — back in a moment.\n-# {waiting}")
    log.info("Restarting onto %s, asked for", waiting)
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
    # Last night's end of day, if RYTE missed it. Restarted over ten o'clock,
    # the Ads and Lead Order carry and move into Done never happened, and the
    # catch-up below only knows today - so the two cards sat in Quality Check
    # the next morning. Each of these is safe to run late: the carry skips
    # whatever is already on the next card, and the move into Done takes only
    # cards dated that day or before.
    # Every weekday evening missed, oldest first - "ryte will be gone for 1
    # and a half days": two evenings' carry-overs, not only last night's. A
    # step is done for a day when it last ran on that day or after it.
    for back in range(OVERNIGHT_LOOK_BACK_DAYS, 0, -1):
        then = today - timedelta(days=back)
        if then.weekday() >= 5 and back > 1:
            continue
        for step in dailyops.OVERNIGHT_CATCH_UP:
            last = await asyncio.to_thread(boardclock.last_run, step)
            if back > 1 and not last:
                continue
            if last and last >= then.isoformat():
                continue
            await _board_step(bot, step, then, day=then)
    due = dailyops.steps_due(
        datetime.now(ZoneInfo(config.schedule.timezone)),
        await asyncio.to_thread(boardclock.done_on, today),
    )
    for step in due:
        await _board_step(bot, step, today)


#: How many evenings back a restart catches the end of day up for.
OVERNIGHT_LOOK_BACK_DAYS = 4


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


async def _website_testimonial(responder: Responder, config: Config, said: str) -> None:
    """A full interview onto the website's video section, once Franklin has
    seen the name and quote and pressed Add."""
    link, name, quote = mentions.testimonial_parts(said)
    # "i want to send him the quote every time" - the quote is always his.
    if not link or not quote:
        await responder.send(
            "Send the link and the quote together: `@RYTE testimonial https://youtu.be/… "
            "quote: …` — add `name: …` too if the video doesn't say who it is."
        )
        return
    about = mentions.testimonial_about(said)
    if not (name and about):
        await responder.send("🎬 Reading the interview…")
    try:
        video = await asyncio.to_thread(
            partial(jobs.testimonial_draft, config, link, name=name, quote=quote, about=about))
    except Exception as exc:
        await responder.send(f"⚠ {_readable(exc)}")
        return
    card = discord.Embed(
        title="🎬 New testimonial for the website",
        description=(f"**{video['name']}**\n{video['quote']}\n\n"
                     f"[{video.get('title') or 'The video'}](https://www.youtube.com/watch?v={video['id']})"),
        colour=discord.Colour.green(),
    )
    # What goes beside the video on the Full Interviews page - read before Add.
    card.add_field(
        name="Full Interviews page",
        value=(video.get("about") or "⚠ No paragraph - I couldn't read the captions. It won't go "
               "on the Full Interviews page unless you send it again with `about: …`.")[:1024],
        inline=False,
    )
    card.set_image(url=f"https://img.youtube.com/vi/{video['id']}/hqdefault.jpg")
    card.set_footer(text="Add makes it the featured interview on leadlabcrm.com and the first on the "
                         "Full Interviews page. Wrong name, quote or paragraph? Send it again with "
                         "name: … / quote: … / about: … after the link.")
    view = views.ConfirmView(
        requester_id=responder.requester_id,
        timeout=config.discord.approval_timeout_seconds,
        label="Add to website",
        emoji="🎬",
    )
    await responder.send(embed=card, view=view)
    await view.wait()
    if not view.confirmed:
        return
    try:
        done = await asyncio.to_thread(jobs.testimonial_publish, config, video)
    except Exception as exc:
        await responder.send(f"⚠ Couldn't put it on the website: {_readable(exc)}")
        return
    await responder.send(done)


async def _post_segments(responder: Responder, config: Config, who: str) -> None:
    """One interview's segments onto the Success Stories page - after the button.

    "i want to tell ryte to post it". Read from the posting doc: each segment
    with a YouTube link goes first in its website section; the ones without
    are named, not guessed at.
    """
    try:
        tab, items, skipped = await asyncio.to_thread(jobs.success_plan, config, who)
    except Exception as exc:
        await responder.send(f"⚠ {_readable(exc)}")
        return
    lines = [f"🎬 **{tab}** — add to the Success Stories page?"]
    # The ones found on the channel carry their link, to be clicked and
    # checked before the button - "it still should ask me".
    lines += [
        f"• {one['what']}: **{one['title']}** → {one['section']}"
        + (f" · found on YouTube: <https://youtu.be/{one['id']}>" if one.get("found") else "")
        for one in items
    ]
    lines += [f"-# Skipped: {one}" for one in skipped]
    if not items:
        await responder.send("\n".join(lines + ["Nothing in there with a YouTube link yet."]))
        return
    view = views.ConfirmView(
        requester_id=responder.requester_id,
        timeout=config.discord.approval_timeout_seconds,
        label=f"Add {len(items)}",
        emoji="🎬",
    )
    await responder.send("\n".join(lines), view=view)
    await view.wait()
    if not view.confirmed:
        return
    try:
        said = await asyncio.to_thread(jobs.success_post, config, items)
    except Exception as exc:
        await responder.send(f"⚠ Couldn't update the page: {_readable(exc)}")
        return
    await responder.send(said)


async def _full_interviews(responder: Responder, config: Config) -> None:
    """`@RYTE sync full interviews` - the Success Stories page's full
    interviews in the website list's order, newest first, after the button."""
    try:
        _held, updated, added, left = await asyncio.to_thread(jobs.full_interviews_plan, config)
    except Exception as exc:
        await responder.send(f"⚠ {_readable(exc)}")
        return
    from .. import segments, successpage

    row = next((one for one in updated.get("sections") or [] if one.get("c") == segments.FULL_INTERVIEWS), {})
    top = [f"{at}. **{v.get('t')}**" + (f" — {v.get('w')}" if v.get("w") else "")
           for at, v in enumerate((row.get("v") or [])[:successpage.SHOWN], start=1)]
    lines = [f"🎬 **Agent Success Full Interviews** would show, newest first:", *top,
             f"-# {len(added)} interview(s) added, the rest re-ordered to match the website's list."]
    lines += [f"-# Left out: {one['what']} — {one['why']}" for one in left[:5]]
    view = views.ConfirmView(requester_id=responder.requester_id,
                             timeout=config.discord.approval_timeout_seconds, label="Update the page", emoji="🎬")
    await responder.send("\n".join(lines), view=view)
    await view.wait()
    if not view.confirmed:
        return
    try:
        await asyncio.to_thread(jobs.full_interviews_save, config, updated)
    except Exception as exc:
        await responder.send(f"⚠ Couldn't update the page: {_readable(exc)}")
        return
    await responder.send("✅ Updated. Reload the Success Stories page - it switches over a few seconds after it opens.")


async def _success_remove(responder: Responder, config: Config, said: str) -> None:
    """One video off the Success Stories page, after the button."""
    try:
        found = await asyncio.to_thread(jobs.success_lookup, config, said)
    except Exception as exc:
        await responder.send(f"⚠ {_readable(exc)}")
        return
    if not found:
        await responder.send("That video isn't on the Success Stories page.")
        return
    view = views.ConfirmView(
        requester_id=responder.requester_id,
        timeout=config.discord.approval_timeout_seconds,
        label="Take it off",
        emoji="🗑",
    )
    await responder.send(
        "Take this off the Success Stories page?\n" + "\n".join(
            f"• **{one['t']}** — {one['w']} ({one['section']})" for one in found
        ),
        view=view,
    )
    await view.wait()
    if not view.confirmed:
        return
    try:
        done = await asyncio.to_thread(jobs.success_take_down, config, said)
    except Exception as exc:
        await responder.send(f"⚠ Couldn't update the page: {_readable(exc)}")
        return
    await responder.send(done)


async def _relayout_doc(responder: Responder, config: Config, who: str, *, again: bool = False) -> None:
    """One old tab of the posting doc, in the layout new interviews get - after
    the button.

    "okay i want him to fix old ones" - "no i want him to correct specific
    only". One tab, by name. It is only redone when every word of it survives
    the move, the YouTube links pasted in by hand included.
    """
    if not who.strip():
        await responder.send(
            "Which tab? `@RYTE fix doc Crystal Clark` — one at a time, by its name in the doc."
        )
        return
    try:
        ready, left = await asyncio.to_thread(jobs.doc_relayout_plan, config, who)
    except Exception as exc:
        await responder.send(f"⚠ Couldn't read the posting doc: {_readable(exc)}")
        return
    if not ready:
        await responder.send("\n".join(left) or f"Nothing to redo in “{who}”.")
        return
    tab, _paras, _text, kept = ready[0]
    view = views.ConfirmView(
        requester_id=responder.requester_id,
        timeout=config.discord.approval_timeout_seconds,
        label="Redo this tab",
        emoji="📄",
    )
    await responder.send(
        f"📄 Put **{tab.title}** into the new layout? {kept}.\n"
        "-# Every word stays - I checked. Google keeps the old version too: "
        "File → Version history.",
        view=view,
    )
    await view.wait()
    if not view.confirmed:
        return
    try:
        done, problems = await asyncio.to_thread(jobs.doc_relayout, config, ready)
    except Exception as exc:
        await responder.send(f"⚠ Couldn't redo them: {_readable(exc)}")
        return
    # Changed between the look and the press - a version restored, a line
    # typed. Looked at afresh and asked again, once, rather than leaving
    # somebody to retype the command.
    if not done and not again and any("changed since" in one for one in problems):
        await responder.send(f"🔄 **{who}** changed since I looked — checking it again.")
        await _relayout_doc(responder, config, who, again=True)
        return
    said = []
    if done:
        said.append("📄 Redone: " + ", ".join(done))
    said += [f"⚠ {one}" for one in problems]
    await responder.send("\n".join(said) or "Nothing was redone.")


async def _website_take_down(responder: Responder, config: Config, said: str) -> None:
    """One interview off the website, after the button."""
    if not said:
        await responder.send("Which one? `@RYTE testimonial remove Raymond` — or the YouTube link.")
        return
    try:
        gone, featured = await asyncio.to_thread(jobs.testimonial_lookup, config, said)
    except Exception as exc:
        await responder.send(f"⚠ {_readable(exc)}")
        return
    view = views.ConfirmView(
        requester_id=responder.requester_id,
        timeout=config.discord.approval_timeout_seconds,
        label="Remove from website",
        emoji="🗑",
    )
    await responder.send(
        f"Take **{gone['name']}** off the website?"
        + (f" **{featured}** would be the featured interview." if featured else ""),
        view=view,
    )
    await view.wait()
    if not view.confirmed:
        return
    try:
        done = await asyncio.to_thread(jobs.testimonial_take_down, config, gone["id"])
    except Exception as exc:
        await responder.send(f"⚠ Couldn't take it off the website: {_readable(exc)}")
        return
    await responder.send(done)


async def _send_paid_invoice(responder: Responder, who: str, config: Config | None = None) -> None:
    """Answer "invoice of David Pereira" with their paid Payra invoices, as a PDF."""
    import io

    from .. import payraapi

    if not who:
        await responder.send("Whose? `@RYTE invoice of David Pereira` — the whole name, "
                             "their email or their phone number.")
        return
    try:
        pdf, found, problem = await asyncio.to_thread(jobs.paid_invoice_pdf, who, config)
    except Exception as exc:
        await responder.send(embed=embeds.error(f"Couldn't make the invoice PDF\n{_readable(exc)}"))
        return
    if not pdf:
        await responder.send(problem)
        return
    if not found:
        # Out of the inbox: older than the week Payra's API lists.
        await responder.send(
            f"🧾 **{who}** — nothing in Payra's last week, so here are Payra's own payment "
            "confirmation emails for them.",
            file=discord.File(io.BytesIO(pdf), filename=f"{who} - Payra payment confirmations.pdf"),
        )
        return
    name = found[0]["invoice"].get("name") or who
    listed = "\n".join(
        f"• #{one['invoice'].get('number') or '?'} — {payraapi._money(one['invoice'].get('total'))}, "
        f"paid {payraapi._spelled(one['payments'][-1].get('paid_on'))}"
        + (" · refunded" if one["refunds"] else "")
        for one in found
    )
    await responder.send(
        f"🧾 **{name}** — {len(found)} paid invoice{'' if len(found) == 1 else 's'} from Payra:\n{listed}",
        file=discord.File(io.BytesIO(pdf), filename=f"{name} - paid invoices.pdf"),
    )


async def _send_contract(responder: Responder, config: Config, said: str) -> None:
    """Answer "contract of David Pereira" with the signed PDF off the inbox."""
    import io

    from .. import agents as rules

    who = rules.who_wants_a_contract(said)
    try:
        says, pdf, called, problem = await asyncio.to_thread(
            jobs.signed_contract_for, config, who
        )
    except PIPELINE_ERRORS as exc:
        await responder.send(embed=embeds.error(f"Couldn't read the inbox\n{exc}"))
        return

    if pdf:
        # When it was signed and to whom, and anything wrong with the copy -
        # a PandaDoc API copy stamped "for demo purposes only" looks like the
        # real contract until somebody reads the footer.
        signed = next((line for line in says.splitlines() if line.startswith("Signed ")), "")
        await responder.send(
            f"📄 **{who}** — signed contract."
            + (f"\n-# {signed}" if signed else "")
            + (f"\n⚠ {problem}" if problem else ""),
            file=discord.File(io.BytesIO(pdf), filename=called or f"{who}.pdf"),
        )
        return
    # The email without its PDF still says who signed and when, which is
    # worth having while somebody goes and downloads the rest.
    lines = [f"📄 **{who}**"] if says else []
    if says:
        lines.append("> " + "\n> ".join(says.splitlines()[:6]))
    if problem:
        lines.append(f"⚠ {problem}")
    await responder.send("\n".join(lines) or "Nothing came back, which shouldn't happen.")


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
    """The cards nobody ticked, then the setup-card lines nobody checked off -
    "yes add the setup card lines too"."""
    from .. import dailyops

    await _send_unticked_cards(responder, config, said)
    today = _today(config)
    named = dailyops.day_named(said, today=today)
    days = [named] if named else []
    day = today
    while day <= dailyops.chased_through(today):
        if day not in days:
            days.append(day)
        day = dailyops.next_day(day)
    try:
        lines, problems = await asyncio.to_thread(jobs.setup_lines_open, config, days)
    except PIPELINE_ERRORS as exc:
        lines, problems = [], [f"Couldn't read the setup cards: {exc}"]
    if lines:
        await responder.send(_setup_lines_said(lines, today=today), quiet=True)
    for one in problems:
        log.info("Setup lines: %s", one)


def _setup_lines_said(lines: list[dict], *, today) -> str:
    """Grouped by whose checklist they are on - that is who to ask."""
    by_whom: dict = {}
    for one in lines:
        by_whom.setdefault(one["setup_by"] or "?", []).append(one)
    said = [f"📋 **{len(lines)} not checked off on the setup card:**"]
    for whom, theirs in by_whom.items():
        names = []
        for one in theirs:
            name = f"[{one['agent']}](<{one['url']}>)" if one["url"] else str(one["agent"])
            when = ("today" if one["day"] == today
                    else "tomorrow" if one["day"] == today + timedelta(days=1)
                    else "yesterday" if one["day"] == today - timedelta(days=1)
                    else f"{one['day']:%a %b %d}")
            names.append(f"{name} ({when})")
        said.append(f"• **{whom}** — " + ", ".join(names))
    return "\n".join(said)


async def _send_unticked_cards(responder: Responder, config: Config, said: str = "") -> None:
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
    if not found and day is not None:
        # The day asked about is clean. Whatever is running out of time is
        # still worth saying, because "all ticked" reads as "all clear".
        # Franklin asked "unticked yesterday" at ten past one in the morning:
        # every Wednesday launch was ticked, so that was the whole answer -
        # while four Thursday launches, hours away, sat unticked on the card
        # he had open.
        try:
            soon, _trouble = await asyncio.to_thread(
                partial(jobs.unmarked_agents, ahead=True), config, day=None,
            )
        except PIPELINE_ERRORS:
            soon = []
        if soon:
            # The problem first. "Every New Agent card in Done has been
            # ticked" opened the first version of this, and it is the line
            # that gets read: Franklin had Brandon Sena and Don Alimi sitting
            # unticked in Done on the screen beside it - "CLEARLY NOT". The
            # day he named being clean is a footnote to that, not the news.
            ahead = dailyops.days_chased(_today(config))
            await responder.send(
                f"⚠ **{len(soon)} going live {ahead} "
                f"{'is' if len(soon) == 1 else 'are'} still unticked.**\n"
                f"-# Everything that went live {covers} is ticked.",
                embed=_unmarked_card(soon, days=ahead),
            )
            await _offer_the_top_ups(responder, config, soon)
            return
    if not found:
        await responder.send(
            f"Every New Agent card in {dailyops.DONE} has been ticked"
            + (f" for {covers}." if covers else ".")
        )
        return
    # No ping: somebody just asked, so they are already looking at it.
    await responder.send(embed=_unmarked_card(found, days=covers))
    if day is not None:
        # A day named is not the only thing worth knowing. "trello unticked
        # yesterday" at 2:59 in the morning is a question about the card just
        # worked - Tuesday's, going live today - and Monday's one leftover
        # was the whole answer while Tuesday's sat unticked.
        try:
            soon, _trouble = await asyncio.to_thread(
                partial(jobs.unmarked_agents, ahead=True), config, day=None,
            )
        except PIPELINE_ERRORS:
            soon = []
        shown = {str(one.get("id") or "") for one in found}
        soon = [one for one in soon if str(one.get("id") or "") not in shown]
        if soon:
            ahead = dailyops.days_chased(_today(config))
            await responder.send(
                f"⚠ And **{len(soon)} going live {ahead}** "
                f"{'is' if len(soon) == 1 else 'are'} unticked too:",
                embed=_unmarked_card(soon, days=ahead),
            )
            found = list(found) + soon
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
    tag = _tagged_in(said, bot_id=getattr(bot, "user", None))
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
            partial(jobs.comment_on_daily, config, kind=kind, day=day, text=text, tag=tag)
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
    tag: list[str] = []
    # Screenshots with it - Faith's Ringy SID and token came with the text
    # message they were in - go on the card as attachments.
    pictures = _request_bits(message)[1] if message is not None else []
    if not text and message is not None:
        replied = await _replied_to(message)
        if replied is not None:
            pictures += _request_bits(replied)[1]
        if replied is not None and (replied.content or "").strip():
            text = _as_somebody_said(replied)
            tag = _tagged_in(replied, bot_id=getattr(getattr(message, "guild", None), "me", None))
            if not kind:
                # Said rather than refused. General is where anything that is
                # not ops or ads work lives, and the message names the card it
                # landed on, so a wrong one is one line away from being right.
                kind, guessed = dailyops.FALLBACK_CARD, True

    # The people tagged on Discord in the ask itself - "@Ryte add to trello
    # @Nicole cap Michael at 10" - tagged on Trello and on their checklists.
    if message is not None:
        for name in _tagged_in(message, bot_id=getattr(getattr(message, "guild", None), "me", None)):
            if name not in tag:
                tag.append(name)
        text = re.sub(r"<@!?\d+>", "", text or "").strip(" -–—:,")
    read_off: dict = {}
    if not kind and (text or pictures):
        # Nobody named a card: "this is ads related so checklist is on ads".
        # Ads work on Ads, setup systems on Ops, the rest on General - and an
        # ask that's only a tag and a screenshot gets its task read off it.
        bare = re.sub(r"@\S+", "", text or "").strip()
        kind, line = await asyncio.to_thread(partial(jobs.trello_ask_kind, config, text or "", pictures,
                                                     found=read_off))
        guessed = True
        if len(bare.split()) < 3 and line:
            text = (text + "\n" + line).strip() if text else line
    if not kind and text:
        # "@Ryte add to trello / put on board tho let's collate all logins" -
        # something to say and no card named. General, said rather than
        # asked: the reply names the card, so a wrong one is one line away.
        kind, guessed = dailyops.FALLBACK_CARD, True
    if not kind:
        await responder.send(
            "Say what to put on it — `@RYTE comment on general card <what to say>`, "
            "or reply to a message with `@RYTE add to trello`. The four are general, "
            "ops, ads and lead order; it's today's unless you say a day."
        )
        return
    if not text:
        await responder.send(
            "Give me something to say on it — or reply to the message you "
            "want on the board and say `@RYTE put on trello`."
        )
        return

    # The client it's about, when they have a card: "add to his current
    # order" goes on Karl Cook's card too, and the day card's line links it.
    theirs = None
    try:
        theirs = await asyncio.to_thread(partial(jobs.client_card_for, config, text or "",
                                                 read_off.get("client") or ""))
    except Exception:
        log.warning("Couldn't look for the client's card", exc_info=True)
    if theirs is not None:
        text = f"{text}\n{theirs.get('shortUrl') or theirs.get('url') or ''}".strip()
    try:
        title, url, problems = await asyncio.to_thread(
            partial(jobs.comment_on_daily, config, kind=kind, day=day, text=text, tag=tag, pictures=pictures)
        )
    except PIPELINE_ERRORS as exc:
        await responder.send(embed=embeds.error(f"Couldn't reach the board\n{exc}"))
        return
    if theirs is not None and url:
        task = re.sub(r"https?://trello\.com/\S+", "", text).strip()
        try:
            on = await asyncio.to_thread(partial(
                jobs.comment_on_client_card, config, theirs, f"📌 {task}\n— also on {title}: {url}", pictures))
            problems.append(f"✅ On **{theirs.get('name')}**'s card too: <{on}>")
        except Exception as exc:
            problems.append(f"Couldn't comment on {theirs.get('name')}'s card: {_readable(exc)}")

    # A url means it went on the board. Anything said alongside one is a
    # warning about part of it - a name nobody on the board answers to - and
    # reporting that as a failure would hide a comment that posted.
    if problems and not url:
        await responder.send(embed=embeds.error("\n".join(problems)))
        return
    note = f"Said it on **{title}** — <{url}>"
    done = [one for one in problems if one.startswith("✅")]
    problems = [one for one in problems if not one.startswith("✅")]
    if done:
        note += "\n" + "\n".join(done)
    if problems:
        note += "\n⚠ " + "\n⚠ ".join(problems)
    if guessed:
        note += ("\n-# Nobody said which card, so it went where it reads as belonging. `on general`, `on ops`, "
                 "`on ads` or `on lead order` puts it there instead.")
    await responder.send(note)


def _plain_name(member) -> str:
    """"Franklin 👩‍💼| General Manager" -> "Franklin": the name, without the
    job title and decoration a Discord nickname carries."""
    import unicodedata

    said = str(getattr(member, "display_name", None) or getattr(member, "name", "") or "")
    # "𝒩𝒾𝒸𝑜𝓁𝑒" is Nicole in a fancy font - letters to Python, so they passed
    # through untouched and "Nobody on the board is called 𝒩𝒾𝒸𝑜𝓁𝑒".
    said = unicodedata.normalize("NFKC", said)
    said = said.split("|")[0]
    said = re.sub(r"[^\w\s'.-]", " ", said)
    return " ".join(said.split())


def _tagged_in(message, *, bot_id=None) -> list[str]:
    """The people a Discord message tags, by name - never RYTE itself."""
    me = getattr(bot_id, "id", bot_id)
    found = []
    for one in getattr(message, "mentions", None) or []:
        if (me is not None and getattr(one, "id", None) == me) or getattr(one, "bot", False):
            continue
        name = _plain_name(one)
        if name and name not in found:
            found.append(name)
    return found


def _as_somebody_said(message) -> str:
    """One person's Discord message, ready to be a Trello comment.

    With their name in front of it. Every comment RYTE writes is signed by
    RYTE's own account, so without this the board says he decided to hold off
    on blue collar till EOD.
    """
    author = getattr(message, "author", None)
    import unicodedata

    who = unicodedata.normalize("NFKC", (
        getattr(author, "display_name", None)
        or getattr(author, "name", "")
        or ""
    )).strip()
    said = str(message.content or "")
    # "<@1234567890>" means nothing on the board - the name does.
    for one in getattr(message, "mentions", None) or []:
        said = re.sub(rf"<@!?{getattr(one, 'id', '')}>", f"@{_plain_name(one)}", said)
    said = " ".join(said.split())
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
        # Payra's paid invoice, as an exhibit - "we fought them using payra
        # time stamps and reference and all of that". The figures are the
        # ones in the document; this is the document they come from. Not when
        # somebody attached an invoice themselves.
        if not any(one.kind == "invoice" for one in exhibits):
            try:
                who = dispute.customer_email or dispute.customer_name
                pdf, _invoices, _problem = await asyncio.to_thread(jobs.paid_invoice_pdf, who, config)
            except Exception:
                log.warning("Couldn't make the Payra invoice exhibit", exc_info=True)
                pdf = b""
            if pdf:
                exhibits.append(rules_doc.Exhibit(
                    name="Payra-paid-invoice-" + (re.sub(r"[^A-Za-z0-9]+", "-", dispute.customer_name
                                                         or "customer").strip("-")) + ".pdf",
                    data=pdf, kind="invoice",
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
        headings, tab, problems, made = await asyncio.to_thread(
            jobs.tracker_headings, config
        )
    except PIPELINE_ERRORS as exc:
        headings, tab, problems, made = [], "", [f"Couldn't read the tracker: {exc}"], []
    if problems:
        await responder.send("⚠ " + "\n⚠ ".join(problems))
        return
    # Before the button, not after. A tab appearing in somebody's spreadsheet
    # is worth reading about while there is still a decision to make.
    for one in made:
        await responder.send(f"📄 {one}")
    if not headings:
        return

    # Once per dispute - the same person for the same amount. Megan Lucas
    # asked for on her own after a rebuttal already offered the row would
    # otherwise be two deductions.
    try:
        already = await asyncio.to_thread(
            jobs.already_tracked, config, tab, dispute.customer_name, len(headings),
            dispute.amount or "",
        )
    except PIPELINE_ERRORS:
        already = 0
    if already:
        await responder.send(
            f"🧾 **{dispute.customer_name}** for {dispute.amount or 'this amount'} is already "
            f"on **{tab}**, row {already} — not adding it twice."
        )
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


async def _to_the_tracker(responder: Responder, config: Config, message, who: str) -> None:
    """`@RYTE add to tracker`, replying to the chargeback card - the row on
    its own. "I've got no tracker command" was the answer Megan Lucas's
    notice got: the row was only ever offered at the end of a rebuttal.

    The dispute comes off the message replied to, or off the nearest notice
    above for the name given - never just the nearest notice. A row for the
    wrong customer in the tracker is a deduction off the wrong closer.
    """
    from .. import rebuttal as rules_doc

    def facts(older):
        if older is None or not worth_reading(older):
            return None
        said, _paid = rules_doc.split_payment((getattr(older, "content", "") or "").strip())
        found = rules_doc.read_facts(said)
        if not found.customer_name:
            found.customer_name = rules_doc.named_in(said)
        return None if found.missing() else found

    def same_person(asked: str, named: str) -> bool:
        asked_words = set(re.findall(r"[a-z]+", asked.casefold()))
        return bool(asked_words) and asked_words <= set(re.findall(r"[a-z]+", (named or "").casefold()))

    replied = await _replied_to(message)
    dispute, source = facts(replied), replied
    if dispute is None and who.strip():
        source = None
        async for older in _said_before(message):
            found = facts(older)
            if found is not None and same_person(who, found.customer_name):
                dispute, source = found, older
                break
    if dispute is None:
        await responder.send(
            "Reply to the ⚖️ Chargeback card with `@RYTE add to tracker` — or say whose: "
            "`@RYTE add to tracker Megan Lucas` and I'll find their notice above."
            if not who.strip() else
            f"I couldn't find a chargeback notice for **{who.strip()}** above. Reply to "
            "their ⚖️ Chargeback card with `@RYTE add to tracker` instead."
        )
        return
    came_from = getattr(source, "jump_url", "")
    if came_from:
        await responder.send(f"-# For **{dispute.customer_name}**, off [the notice](<{came_from}>).")
    await _offer_the_tracker(responder, config, dispute, rules_doc.Gathered())


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


#: Clear-outs open now - an update waits for them. A restart in the middle
#: of one left its Delete button saying "The application didn't respond in
#: time": buttons live in RYTE's memory and don't outlast it.
_CLEAROUTS_OPEN = 0


def _clearing_out() -> bool:
    return _CLEAROUTS_OPEN > 0 or _QUIET_RUNNING


async def _clear_out(
    bot: "WilByteBot", responder: Responder, config: Config, name: str,
    *, run: tuple[int, int] | None = None, auto: bool = False,
) -> str:
    global _CLEAROUTS_OPEN

    _CLEAROUTS_OPEN += 1
    try:
        return await _clear_out_one(bot, responder, config, name, run=run, auto=auto)
    finally:
        _CLEAROUTS_OPEN -= 1


async def _clear_out_one(
    bot: "WilByteBot", responder: Responder, config: Config, name: str,
    *, run: tuple[int, int] | None = None, auto: bool = False,
) -> str:
    """Close an agent down: keep the sheet, keep the conversation, then go.

    Says how it went - "deleted", "left", "stopped" or "trouble" - so a run
    down the list knows whether to carry on. A press on either question is
    an answer; nobody pressing anything is not, and a run stops rather than
    walking a hundred and eighty channels past an empty chair.

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
        return "trouble"

    where = (config.secrets.discord_clients_guild_id or "").strip()
    guild = bot.get_guild(int(where)) if where.isdigit() else None
    if guild is None:
        await responder.send(
            "I'm not in the clients server, or DISCORD_CLIENTS_GUILD_ID in "
            ".env isn't it. `@RYTE access` lists the servers I'm in."
        )
        return "trouble"

    channels = [
        clearout.Channel(
            channel_id=str(one.id),
            name=str(one.name),
            category=str(getattr(one.category, "name", "") or ""),
        )
        for one in guild.text_channels
    ]
    # A run names its channel by id - two can share a name.
    by_id = re.fullmatch(r"<#(\d+)>", name.strip())
    if by_id:
        found = [one for one in channels
                 if one.channel_id == by_id.group(1) and not clearout.off_limits(one)]
        if not found:
            await responder.send("That channel isn't in the clients server any more.")
            return "trouble"
        name = found[0].name
    else:
        found = clearout.channels_for(name, channels)
    if not found:
        await responder.send(
            f"No channel in **{guild.name}** looks like **{name}**'s."
        )
        return "trouble"
    if len(found) > 1:
        await responder.send(
            f"More than one channel could be **{name}**'s, so I've left them "
            "all alone:\n"
            + "\n".join(f"• #{one.name}" for one in found)
            + "\nName the one you mean and I'll do that one."
        )
        return "trouble"

    from .. import keeplist

    if auto and not keeplist.readable():
        # A keep list that can't be read only protects the names it started
        # with - everybody added since by `quiet keep` would be fair game. The
        # ten-at-a-time run refused to start; the full run went on regardless.
        await responder.send("⚠ I couldn't read the keep list, so I'm not clearing anything out on my own.")
        return "trouble"
    if keeplist.kept(found[0].channel_id, found[0].name):
        await responder.send(
            f"🔒 **#{found[0].name}** is on the keep list, so I don't clear it out. "
            f"`@RYTE quiet unkeep {found[0].name}` first if it really should go."
        )
        return "kept"

    plan = clearout.Plan(name=name, channel=found[0], guild_id=str(guild.id))
    member = _member_called(guild, name)
    if member is not None:
        plan.member_id, plan.member_name = str(member.id), str(member)

    from_card, trouble = await asyncio.to_thread(jobs.sheet_for_agent, config, name)

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
    # The name found somebody; the channel says whether it is them. A member
    # who never wrote in the client's own channel and was never tagged in it
    # is a name that happens to fit - their messages elsewhere are not kept
    # as the client's, and the Drive folder is named for the channel instead.
    # Said, and not a reason to leave the channel for a person: it only means
    # nothing of that member's is kept, and nobody is banned either way.
    just_said = []
    if member is not None and not clearout.seen_in(messages, member.id):
        just_said.append(
            f"{member.display_name} has their name but never wrote in or was "
            f"tagged in #{plan.channel.name} — not keeping anything of theirs "
            "from other channels."
        )
        plan.notes.append(just_said[-1])
        member = None
        plan.member_id, plan.member_name = "", ""
    # The card's link first - it is the one the team chose, and the card is
    # only found when exactly one client on the board has this name. The
    # channel's when there is none; and when the two disagree, both are
    # shown, so the one going into ALL CLIENTS is seen before it goes.
    own = clearout.sheet_in(messages)
    if from_card:
        plan.sheet = from_card
        if own and not clearout.same_sheet(from_card, own):
            # Two sheets, and a client with two orders has one per order: the
            # one whose name is this channel's leads is this channel's. Jeremy
            # Fox's FEX channel was collected with his vets sheet off the card.
            titled = await asyncio.to_thread(jobs.sheet_titles, config, [from_card, own])
            fits = clearout.sheet_for_channel(plan.channel.name, titled)
            # Both kept - "or just save both sheet". The one that is this
            # channel's leads goes first; the other has a row of its own, so
            # whichever turns out to be the one wanted, it's there.
            if fits == own:
                plan.sheet, plan.from_channel, plan.also_sheets = own, True, [from_card]
                plan.told.append(
                    f"Their Trello card has a different sheet ({titled.get(from_card) or from_card}) "
                    f"— keeping both: the channel's, “{titled.get(own)}”, is this channel's "
                    "leads; the card's goes in a row of its own."
                )
            else:
                plan.also_sheets = [own]
                plan.told.append(
                    f"Their channel has a different sheet ({titled.get(own) or own}) — keeping "
                    "both: the card's first, the channel's in a row of its own."
                )
    elif own:
        plan.sheet, plan.from_channel, trouble = own, True, []
    plan.problems += trouble

    held = (list(plan.problems) + [one for one in plan.notes if one not in just_said]
            + ([] if plan.sheet else ["no sheet link found"]))
    if auto:
        # Two more a person would think of before deleting on their own -
        # "MAKE SURE RYTE DONT MAKE MISTAKE HERE".
        # Another channel of theirs is only a reason to stop when it is still
        # in use: all of them quiet, each goes on its own with its own sheet.
        # "cant it know?" - Thomas Cordier's quiet IUL channel was held for a
        # VET one just as quiet.
        others = [one for one in clearout.their_channels(plan.channel, channels)
                  if one.channel_id != plan.channel.channel_id]
        if others:
            quiet_since = datetime.now(timezone.utc) - timedelta(days=clearout.quiet_days(""))
            busy = []
            for one in others:
                when, _used = _last_used(guild.get_channel(int(one.channel_id)))
                if when is None or when > quiet_since:
                    busy.append(one)
            if busy:
                held.append(
                    "their other channel" + ("s " if len(busy) > 1 else " ")
                    + ", ".join(f"#{one.name}" for one in busy)
                    + (" are" if len(busy) > 1 else " is") + " still in use — whether this one "
                    "goes is yours to say")
        ordering, said = await asyncio.to_thread(jobs.recent_order, config, plan.channel.name)
        if not ordering:
            held.append(said)
    if auto and held:
        # On its own, anything that needed a person's eye is left for one: a
        # note (two sheets, a name that doesn't fit), a problem, no sheet.
        await responder.send(
            (f"-# {run[0]} of {run[1]}\n" if run else "") + clearout.describe(plan)
            + "\n\n**Left for you** — " + "; ".join(held)
        )
        return "held"
    view = views.ConfirmView(
        requester_id=responder.requester_id,
        timeout=config.discord.approval_timeout_seconds,
        stoppable=run is not None,
        label="Keep the sheet and show me the messages",
        emoji="🧹",
    )
    shown = await responder.send(
        (f"-# {run[0]} of {run[1]}\n" if run else "")
        + clearout.describe(plan)
        + "\n-# Nothing is deleted by this. I'll put the conversation here for "
        "you to screenshot, and ask again before anything goes."
        + (f"\n-# 🤖 RYTE presses this itself in {RYTE_PRESSES_AFTER}s — "
           "**Leave it** or **Stop the run** first to say no." if auto else ""),
        view=view,
    )
    if auto:
        await _ryte_presses(view, shown)
    else:
        await view.wait()
    # A timeout is not an answer. "Leave it" is this client left alone and on
    # to the next; nobody pressing anything is nobody there, and a run walks
    # the rest of the list past an empty chair.
    if getattr(view, "stopped", False) or not view.answered:
        return "stopped"
    if not view.confirmed:
        return "left"

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
    pictures, undrawn = [], []
    for number, (where, group) in enumerate(groups, start=1):
        def take(where=where, group=group, number=number):
            return jobs.keep_the_picture(
                config, clearout.as_page(plan, group),
                clearout.picture_name(plan, when=today, where=where, order=number),
                into=clearout.their_folder(plan),
            )

        picture, trouble = await asyncio.to_thread(take)
        if not picture:
            # Once more - a blip is not a broken Drive.
            picture, trouble = await asyncio.to_thread(take)
        if picture:
            pictures.append((picture, take))
        named = f"#{where}" if where else "Picture"
        if not picture:
            undrawn.append(named)
        kept.append(
            f"✅ {named} → <{picture}>" if picture
            else f"⚠ {named} — " + "; ".join(trouble)
        )
        # Said even when the upload worked. It came back with a link and a
        # complaint, and dropping the complaint because there was a link is
        # how a picture ends up in the wrong folder with nobody told.
        if picture and trouble:
            kept += [f"-# {one}" for one in trouble]
    # Already small print - "-# -# Read 20 new message(s)" was the marker twice.
    kept += [one if one.startswith("-#") else f"-# {one}" for one in looked]

    # Only once both are kept. The whole point of the order is that a channel
    # is never deleted with the only copy of something still inside it - and a
    # channel whose history would not open is one whose picture is of nothing,
    # however cleanly the picture itself was taken.
    if unread:
        await responder.send(
            "\n".join(kept)
            + "\n\n**Nothing deleted.** " + " ".join(unread)
        )
        return "trouble"
    if not forwarded or not tab:
        await responder.send(
            "\n".join(kept)
            + "\n\n**Nothing deleted.** One of those didn't work, and the "
            "channel is the only copy of what it didn't keep."
        )
        return "trouble"
    if auto and undrawn:
        # A picture that never reached Drive is never checked - there is no
        # link to look at - so "every check passed" would be said of a
        # channel whose six-months-on copy doesn't exist. Lydia Richard's went
        # to the 15-second press with Google refusing the Drive sign-in. On
        # its own, RYTE doesn't delete without it; a person still can.
        await responder.send(
            "\n".join(kept)
            + "\n\n**Nothing deleted.** " + ", ".join(undrawn)
            + " didn't reach Drive, and on my own I don't delete a channel "
            "without its picture kept. Fix Drive, then run it again — or "
            f"`@RYTE clearout {name}` to do it yourself."
        )
        return "trouble"

    # Its own work checked before the button, the way a person would before
    # deleting: "check himself if the channel is really 1 month older, then if
    # the screenshots are right and on the folder". Anything it can put right
    # - a row or a picture that didn't land - it puts right once and looks
    # again; anything it can't, and the channel stays.
    checks = await _check_my_work(config, plan, messages, pictures)
    kept.append("\n🔎 **Checked my own work:**\n" + "\n".join(
        f"{'✅' if ok else '❌'} {said}" for ok, said in checks
    ))
    if not all(ok for ok, _said in checks):
        await responder.send(
            "\n".join(kept)
            + "\n\n**Nothing deleted.** Something above isn't right, and the channel "
            "is the only copy until it is."
        )
        return "trouble"

    going = views.ConfirmView(
        requester_id=responder.requester_id,
        timeout=config.discord.approval_timeout_seconds,
        stoppable=run is not None,
        label=f"Delete #{plan.channel.name}",
        emoji="🗑",
        danger=True,
    )
    # Nobody is banned by a clear-out. "WERE ONLY BANNING PEOPLE IF THEY
    # DISPUTED" - an agent whose channel has gone quiet has simply stopped
    # buying, and banning them from the server for it is a different thing
    # entirely. The chargeback run is where a ban belongs.
    asking = await responder.send(
        "\n".join(kept)
        + f"\n\n**This cannot be undone.**\n"
        + ("" if plan.sheet else
           "• ⚠ **No sheet link was found** — not on the board, not in the "
           "channel. The row in Ryte Collection has an empty cell, and "
           "whatever is in the channel goes with it.\n")
        + f"• Delete {clearout.jump_to(plan)}\n"
        + "-# Nobody is banned by this. That is the chargeback run's job."
        # Every check passed - "do 10 per run, check it then delete" - and the
        # button is still there, pressed by RYTE rather than taken away.
        + (f"\n-# 🤖 Every check passed — RYTE presses this itself in "
           f"{RYTE_PRESSES_AFTER}s. **Leave it** or **Stop the run** first to keep it." if auto else ""),
        view=going,
    )
    if auto:
        await _ryte_presses(going, asking)
    else:
        await going.wait()
    if getattr(going, "stopped", False) or not going.answered:
        await responder.send(
            "Left alone. The sheet row and the messages above stay either way."
        )
        return "stopped"
    if not going.confirmed:
        await responder.send(
            "Left alone. The sheet row and the messages above stay either way."
        )
        return "left"
    return await _delete_the_channel(responder, guild, plan, name)


async def _delete_the_channel(responder: Responder, guild, plan, name: str) -> str:
    """The one place a channel is deleted - asked or automatic, the same
    last look first."""
    from .. import clearout

    done, trouble = [], []
    channel = guild.get_channel(int(plan.channel.channel_id))
    # Asked again here rather than trusted from the looking-up: one channel,
    # the one this clear-out was for, in this server, and never one of the
    # server's own.
    refused = clearout.the_one_to_delete(plan, channel, guild_id=guild.id)
    from .. import keeplist

    if not refused and keeplist.kept(plan.channel.channel_id, getattr(channel, "name", plan.channel.name)):
        refused = f"#{plan.channel.name} is on the keep list"
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
    return "deleted" if done else "trouble"


async def _payra_test(responder: Responder, config: Config) -> None:
    """Payra's docs, read from here, and what the token can see. Reads only."""
    import io

    await responder.send("📖 Reading Payra's API docs and trying the token — read-only, a minute or so…")
    try:
        said, docs = await asyncio.to_thread(jobs.payra_probe, config)
    except Exception as exc:
        await responder.send(f"Couldn't finish: {_readable(exc)}")
        return
    from .. import payraapi

    kept = payraapi.status(await asyncio.to_thread(payraapi.load))
    try:
        kept += "\n\n" + await asyncio.to_thread(jobs.payra_reach, config)
    except Exception as exc:
        kept += f"\n\nCouldn't check how far back Payra goes: {_readable(exc)}"
    await responder.send(
        said + "\n\n**The connection:**\n" + kept + "\n-# Payra's docs, attached for reference.",
        file=discord.File(io.BytesIO(docs.encode("utf-8")), filename="payra-docs.md"),
    )


def _clients_guild(bot, config):
    where = (config.secrets.discord_clients_guild_id or "").strip()
    return bot.get_guild(int(where)) if where.isdigit() else None


async def _check_my_work(config: Config, plan, messages, pictures) -> list[tuple[bool, str]]:
    """The clear-out's own work, checked before its channel can go.
    [(fine, said)]. A row or a picture that didn't land is done again once."""
    now = datetime.now(timezone.utc)
    stamps = [one.at for one in messages if getattr(one, "at", None)]
    newest = max(stamps) if stamps else None
    checks = [jobs.quiet_check(newest, now)]

    ok, said, missing = await asyncio.to_thread(jobs.row_check, config, plan)
    if missing:
        await asyncio.to_thread(partial(jobs.collect_client, config, plan, when=now))
        ok, said, _missing = await asyncio.to_thread(jobs.row_check, config, plan)
        said += " (written again)" if ok else ""
    checks.append((ok, said))
    checks.append(await asyncio.to_thread(jobs.sheet_check, config, plan))

    for link, take in pictures:
        ok, said = await asyncio.to_thread(jobs.picture_check, config, plan, link)
        if not ok:
            again, _trouble = await asyncio.to_thread(take)
            if again:
                ok, said = await asyncio.to_thread(jobs.picture_check, config, plan, again)
                said += f" (uploaded again: <{again}>)" if ok else ""
        checks.append((ok, said))
    return checks


async def _check_clearouts(bot, responder: Responder, config: Config) -> None:
    """The clear-outs already done that matched the wrong person. Reads only.

    Until names had to match word for word, a clear-out took the first member
    whose name was inside the client's - a member called "D" for Dylan Rankin
    and for Demetrios Brooks. That member's ring-da-bell messages were drawn
    as the client's, and the Drive folder was named after them. Each clear-out
    in Ryte Collection is matched again both ways; where the two disagree, it
    says whose messages were used and who it should have been.
    """
    from .. import clearout

    guild = _clients_guild(bot, config)
    if guild is None:
        await responder.send("I'm not in the clients server, or DISCORD_CLIENTS_GUILD_ID isn't it.")
        return
    try:
        rows, problems = await asyncio.to_thread(jobs.cleared_out, config)
    except Exception as exc:
        await responder.send(f"Couldn't read Ryte Collection: {_readable(exc)}")
        return
    if problems:
        await responder.send("⚠ " + "\n⚠ ".join(problems))
        return
    wrong = []
    for row in rows:
        who = row.get("channel") or row.get("name")
        # Only the clear-outs done under the old rule. Run over every row, it
        # flagged 69 of 100 - today's three among them, which were matched
        # word for word and never used anybody's messages but their own.
        done = clearout.collected_on(row.get("when"))
        if done is not None and done > clearout.NAMES_MATCHED_SINCE:
            continue
        was = clearout.old_member(guild.members, who)
        right = _member_called(guild, who)
        # Whoever the old rule took, if their whole name is the client's,
        # it was the client - two members with one name is why nobody came
        # back as "right", not a sign the picture is somebody else's.
        # Zenobia Henderson's own picture was listed for deleting.
        if was is not None and right is None and _was_them(was, who):
            continue
        if was is not None and (right is None or was.id != right.id):
            wrong.append((row, was, right))
    if not wrong:
        await responder.send(
            f"✅ Checked all {len(rows)} clear-outs in Ryte Collection — every one "
            "matched the right person, or nobody. Everything cleared after "
            f"{clearout.NAMES_MATCHED_SINCE:%d %b %Y} was matched name for name."
        )
        return
    lines = [f"⚠ **{len(wrong)} of {len(rows)} clear-outs may have used somebody else's messages** — "
             f"the ones done by {clearout.NAMES_MATCHED_SINCE:%d %b %Y}, before names had to match "
             "in full. Everything since then is fine:"]
    for row, was, right in wrong:
        who = row.get("channel") or row.get("name")
        on_the_day = clearout.collected_on(row.get("when")) == clearout.NAMES_MATCHED_SINCE
        lines.append(
            f"• **{who}** ({row.get('when') or '?'}"
            + (", the day it was fixed — look before deleting anything" if on_the_day else "")
            + ") — ring-da-bell picture is "
            f"**{getattr(was, 'display_name', was)}**'s, in the Drive folder "
            f"“{clearout.their_folder(clearout.Plan(name=who, member_name=str(was)))}”. "
            + (f"Should be **{right.display_name}** → `@RYTE redo bell {who}`"
               if right is not None else
               "No member's name matches theirs in full, so there's nothing to "
               "redraw — delete that ring-da-bell picture.")
        )
    lines.append(
        "-# In each of those folders delete **only the ring-da-bell pictures**. "
        "Their own channel's picture is right and is the only copy left - it "
        "sits in the wrongly named folder, so move it to one named for them, "
        "don't delete it. The sheets in Ryte Collection are not affected. I "
        "can't delete or move in Drive, so this is by hand."
    )
    # Split across messages by the responder when it runs long.
    await responder.send("\n".join(lines))


def _was_them(member, channel: str) -> bool:
    """Whether a member's own name is the channel's person, in full."""
    from .. import clearout

    return any(clearout.name_match(channel, called) >= 2 for called in (
        getattr(member, "display_name", ""), getattr(member, "global_name", "") or "",
        getattr(member, "name", "")))


def _corrected(called: str) -> str:
    """"2 — dylan_rankin-vet — ring-da-bell — 2026-09-25 (corrected).png"."""
    stem, dot, kind = str(called).rpartition(".")
    return f"{stem} (corrected).{kind}" if dot else f"{called} (corrected)"


async def _redo_bell(bot, responder: Responder, config: Config, name: str) -> None:
    """Draw a client's ring-da-bell picture again, from their own messages,
    into a Drive folder named for them."""
    from datetime import datetime

    from .. import clearout

    name = " ".join(str(name or "").split())
    if not name:
        await responder.send("Whose? `@RYTE redo bell Dylan Rankin`")
        return
    guild = _clients_guild(bot, config)
    if guild is None:
        await responder.send("I'm not in the clients server, or DISCORD_CLIENTS_GUILD_ID isn't it.")
        return
    member = _member_called(guild, name)
    if member is None:
        await responder.send(
            f"Nobody in the server is **{name}** — not by that whole name, and "
            "never by a part of it. Nothing drawn."
        )
        return
    channels = [
        clearout.Channel(channel_id=str(one.id), name=str(one.name),
                         category=str(getattr(one.category, "name", "") or ""))
        for one in guild.text_channels
    ]
    found, notes = await _also_said(guild, member, channels)
    if not found:
        await responder.send(
            f"Nothing of **{member.display_name}**'s in ring-da-bell or the other "
            "shared channels, so there's no picture to draw."
        )
        return
    plan = clearout.Plan(name=name, member_id=str(member.id), member_name=str(member))
    today = datetime.now(ZoneInfo(config.schedule.timezone))
    kept = [f"🔔 **{member.display_name}** — {len(found)} message(s) of theirs:"]
    for number, (where, group) in enumerate(clearout.by_channel(found), start=2):
        picture, trouble = await asyncio.to_thread(partial(
            jobs.keep_the_picture, config, clearout.as_page(plan, group),
            _corrected(clearout.picture_name(plan, when=today, where=where, order=number)),
            into=clearout.their_folder(plan),
        ))
        kept.append(
            f"✅ #{where} → <{picture}>" if picture else f"⚠ #{where} — " + "; ".join(trouble)
        )
    kept.append(
        f"-# In the folder “{clearout.their_folder(plan)}”. Delete the wrong "
        "picture from the old folder by hand — I can't delete in Drive."
    )
    await responder.send("\n".join(kept))


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
    asks = jobs.blacklist_asks(asked)
    if len(asks) > 1:
        await _blacklist_several(responder, config, asks)
        return
    if len(asks) == 1:
        # One email in the message: that is who, and the words around it -
        # "Customer Name:", "these people" - are not part of a name.
        asked = asks[0][1]
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


async def _blacklist_several(responder: Responder, config: Config, asks) -> None:
    """Several people at once - "blacklist these people" and a name and email
    for each. One list, one button.

    Each by their email. Somebody GHL doesn't have is said and left out; so
    is a name that doesn't match the contact the email belongs to, flagged
    rather than dropped, because a typo and a wrong address look the same
    from here and only the person pressing can tell them apart.
    """
    try:
        looked, tag, problems = await asyncio.to_thread(
            jobs.who_to_blacklist_each, config, asks
        )
    except PIPELINE_ERRORS as exc:
        await responder.send(embed=embeds.error(f"Couldn't read GHL\n{exc}"))
        return
    if problems:
        await responder.send(embed=embeds.error("\n".join(problems)))
        return

    lines, left, missing = [], [], []
    for one in looked:
        asked = one["name"] or one["email"]
        found = one["found"]
        if not found:
            lines.append(f"• **{asked}** ({one['email']}) — ❌ not in GHL, skipped")
            missing.append(f"❌ **{asked}** ({one['email']}) — not in GHL, so not tagged")
            continue
        for contact in found:
            called = " ".join(
                f"{contact.get('firstName') or ''} {contact.get('lastName') or ''}".split()
            ) or str(contact.get("contactName") or "").strip() or "(no name in GHL)"
            line = f"• **{called}** ({one['email']})"
            if _carries(contact, tag):
                lines.append(line + f" — *already {tag}*")
                continue
            given = " ".join(one["name"].split()).casefold()
            if given and given != called.casefold():
                line += f" — ⚠ you wrote **{one['name']}**, GHL has **{called}** for this email"
            lines.append(line)
            left.append(contact)

    if not left:
        await responder.send("Nobody to tag:\n" + "\n".join(lines))
        return
    view = views.ConfirmView(
        requester_id=responder.requester_id,
        timeout=config.discord.approval_timeout_seconds,
        label=f"Tag {len(left)} as {tag}",
        emoji="🚫",
        danger=True,
    )
    await responder.send(f"🚫 Tagging **{tag}** in GHL:\n" + "\n".join(lines), view=view)
    await view.wait()
    if not view.confirmed:
        return
    try:
        done, trouble = await asyncio.to_thread(jobs.blacklist_them, config, left, tag)
    except PIPELINE_ERRORS as exc:
        await responder.send(embed=embeds.error(f"Couldn't tag them\n{exc}"))
        return
    # Said again at the end: pressing the button replaces the list with
    # "Working on it…", and whoever was skipped went with it.
    said = [f"🚫 Tagged **{one}**" for one in done]
    said += [f"⚠ {one}" for one in trouble]
    said += missing
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
    since = now - timedelta(days=clearout.quiet_days(asked or ""))

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
    from .. import keeplist

    keep = keeplist.load()
    held_back = [one for one in quiet if keeplist.kept(one.channel_id, one.name, keep)]
    quiet = [one for one in quiet if one not in held_back]
    pages = clearout.describe_quiet(quiet, ours, unknown, since=since, now=now)
    offered = clearout.pick_from(quiet, now=now)

    if held_back:
        await responder.send(
            "🔒 On the keep list, so not in the list: "
            + ", ".join(f"#{one.name}" for one in held_back)
        )
    if not re.search(r"\b(?:list|pick|show)\b", asked or "", re.IGNORECASE):
        # "prompt ryte quiet then it will automatically go there" - asked,
        # and straight down the list with RYTE pressing the buttons. The
        # list with its dropdown is `@RYTE quiet list`, for doing one by hand.
        from .. import quietauto

        if not keeplist.readable():
            await responder.send("⚠ I couldn't read the keep list, so I'm not clearing anything out on my own.")
            return
        waiting = quietauto.leave_alone(datetime.now(timezone.utc))
        targets = clearout.one_by_one([one for one in quiet if one.channel_id not in waiting])
        if not targets:
            await responder.send(
                f"Nothing has been quiet since **{since:%d %b %Y}** that I haven't already "
                "left for you." if quiet else pages[-1]
            )
            return
        await responder.send(
            f"🧹 **{len(targets)} channels** with nothing said since **{since:%d %b %Y}** — "
            "going through them now. For each: keep the sheet and the screenshots, check my "
            "own work, and press delete only if every check passes. Press **Stop the run** "
            "on any card to stop.\n-# `@RYTE quiet list` shows the list to pick one by hand. "
            "Nobody is banned by this."
        )
        await _all_of_them(bot, responder, config, targets, auto=True)
        return
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
    if picker.run:
        # RYTE presses the buttons - "its not automatic". What was left for a
        # person in the last week isn't taken again; pick it from the list
        # to do it by hand.
        from .. import quietauto

        waiting = quietauto.leave_alone(datetime.now(timezone.utc))
        await _all_of_them(
            bot, responder, config,
            clearout.one_by_one([one for one in quiet if one.channel_id not in waiting]),
            auto=True,
        )
        return
    if not picker.chosen:
        return

    # By name, down the same path as a typed one. A picked channel could be
    # passed by id and skip the looking-up, but then the picked route and the
    # typed route would be two different pieces of code doing the irreversible
    # thing, and only one of them would have been watched.
    await _clear_out(bot, responder, config, picker.chosen)


async def _all_of_them(
    bot: "WilByteBot", responder: Responder, config: Config, names: list,
    *, earlier: dict | None = None, auto: bool = False,
) -> None:
    """Down the whole list, one at a time, asking the same two each time.

    "so i dont have to pick anymore ... then i just confirm if add to sheet,
    conversation added, then delete then move on to next person." The picking
    is what goes; both questions stay, on every single one, because the second
    of them is still the one that cannot be undone.

    Down the same path as a typed name and a picked one. There is one piece of
    code that deletes a channel and it is watched; a run that had its own
    would be a second one that wasn't.

    `auto` is RYTE pressing both buttons itself once its checks pass - "its
    not automatic". Anything that needs a person is left, listed, and not
    taken again for a week; a press of Leave it or Stop the run first wins.
    """
    global _QUIET_RUNNING
    _QUIET_RUNNING = True
    try:
        await _down_the_list(bot, responder, config, names, earlier=earlier, auto=auto)
    finally:
        _QUIET_RUNNING = False


async def _down_the_list(
    bot: "WilByteBot", responder: Responder, config: Config, names: list,
    *, earlier: dict | None = None, auto: bool = False,
) -> None:
    from .. import clearout, quietauto, quietrun

    earlier = earlier or {}
    went = list(earlier.get("went") or [])
    left = list(earlier.get("left") or [])
    trouble = list(earlier.get("trouble") or [])
    held = list(earlier.get("held") or [])
    held_ids = []
    wrong_in_a_row = 0
    stopped = ""
    # Written down at every step, so a restart halfway through is a run that
    # can be carried on rather than one that silently isn't there any more.
    run = {
        "channel_id": getattr(responder, "channel_id", None),
        "requester_id": getattr(responder, "requester_id", None),
        "names": list(names), "at": 0,
        "went": went, "left": left, "trouble": trouble, "held": held,
        "auto": auto,
    }
    for number, target in enumerate(names, start=1):
        run["at"] = number - 1
        _keep_the_run(quietrun, run)
        # Its name now, while the channel is still there to be asked.
        name = _channel_label(bot, target)
        try:
            how = await _clear_out(
                bot, responder, config, target, run=(number, len(names)), auto=auto,
            )
        except Exception as exc:
            # One client's channel breaking something is that client's
            # problem. Raised out of here, it ended the run for everyone after
            # them, and said so only in the terminal.
            log.exception("The quiet run broke on #%s", name)
            await responder.send(
                f"⚠ Something broke on **#{name}**: {_readable(exc)}. Nothing "
                "more was done to it — on to the next."
            )
            how = "trouble"
        if how == "stopped":
            stopped = f"Stopped at **#{name}** — {number} of {len(names)}."
            break
        if how == "deleted":
            went.append(name)
        elif how in ("left", "kept"):
            left.append(name)
        elif how == "held":
            held.append(name)
        else:
            trouble.append(name)
        if auto and how in ("held", "trouble"):
            by_id = re.fullmatch(r"<#(\d+)>", str(target))
            if by_id:
                held_ids.append(by_id.group(1))

        # Three in a row wrong is not three unlucky clients. Something they
        # all depend on is down - the board, the sheet, Drive - and the rest
        # of the list would be a hundred and eighty messages saying so.
        wrong_in_a_row = wrong_in_a_row + 1 if how == "trouble" else 0
        if wrong_in_a_row >= clearout.ENOUGH_WRONG:
            stopped = (
                f"Stopped after {wrong_in_a_row} in a row went wrong. That is "
                "not the clients - something they all need is down. Nothing "
                "was deleted for those."
            )
            break

    quietrun.clear()
    if held_ids:
        try:
            # Not offered to the hourly run again for a week, the same as its own.
            quietauto.left_for_a_person(held_ids, datetime.now(timezone.utc))
        except Exception:
            log.warning("Couldn't write down which channels were left", exc_info=True)
    summary = clearout.how_the_run_went(
        went, left, trouble, held=held,
        over=stopped or f"Went through all {len(names)}.",
    )
    ping = _unmarked_ping(config) if auto and (held or trouble) else ""
    await responder.send(((ping + "\n") if ping else "") + summary)


def _channel_label(bot, target: str) -> str:
    """A run's channel, by name for saying - its id is for finding it."""
    found = re.fullmatch(r"<#(\d+)>", str(target or "").strip())
    if not found:
        return str(target)
    channel = bot.get_channel(int(found.group(1))) if hasattr(bot, "get_channel") else None
    return str(getattr(channel, "name", "") or target)


async def _offer_the_run(bot: "WilByteBot") -> None:
    try:
        from . import mirror

        with mirror.copying_into(mirror.CLEAROUT_INTO):
            await _carry_on_the_run(bot)
    except Exception:
        log.exception("Couldn't offer to carry on the quiet run")


#: Channels one automatic run takes on - "do 10 per run, check it then delete".
AUTO_PER_RUN = 10


#: How long RYTE leaves its own button for a person to say no first.
RYTE_PRESSES_AFTER = 15


async def _ryte_presses(view, message) -> None:
    """Wait a moment for a person, then press the button as RYTE.

    "ryte needs to be the confirming it ... dont remove it if possible but
    ryte will push it". A person's press in the meantime - Leave it, Stop the
    run - is the answer instead.
    """
    try:
        await asyncio.wait_for(asyncio.shield(view.wait()), RYTE_PRESSES_AFTER)
    except asyncio.TimeoutError:
        pass
    if not view.answered:
        await view.pressed_by_ryte(message)


#: Whether a quiet run - by hand or on its own - is going right now. Two at
#: once would be two lists of the same channels.
_QUIET_RUNNING = False


async def _quiet_auto(
    bot: "WilByteBot", responder: Responder, config: Config, asked: str,
) -> None:
    """The quiet run on its own: the ten quietest channels, each kept, checked
    and - only when every check passes - deleted.

    No button: "i have to not click the button no more". A channel that needs
    a person (a note, a problem, no sheet) or fails a check is left as it is,
    listed at the end, and not taken again for a week - otherwise every run
    would start on the same ones and never get further down the list.
    """
    global _QUIET_RUNNING
    from .. import clearout, keeplist, quietauto

    if not keeplist.readable():
        # Without the keep list, there is no knowing who must never go.
        await responder.send("⚠ I couldn't read the keep list, so I'm not clearing anything out on my own.")
        return

    where = (config.secrets.discord_clients_guild_id or "").strip()
    guild = bot.get_guild(int(where)) if where.isdigit() else None
    if guild is None:
        await responder.send("I'm not in the clients server, or DISCORD_CLIENTS_GUILD_ID in .env isn't it.")
        return
    if _QUIET_RUNNING:
        await responder.send("A quiet run is already going — I'll leave this until it's done.")
        return
    now = datetime.now(ZoneInfo(config.schedule.timezone))
    since = now - timedelta(days=clearout.quiet_days(re.sub(r"\bauto\w*\b", "", asked or "")))
    channels = []
    for one in guild.text_channels:
        when, used = _last_used(one)
        channels.append(clearout.Channel(
            channel_id=str(one.id), name=str(one.name),
            category=str(getattr(one.category, "name", "") or ""),
            last_active=when.astimezone(ZoneInfo(config.schedule.timezone)) if when else None,
            ever_used=used, readable=_can_read(guild, one),
        ))
    quiet, _ours, _unknown = clearout.quiet_ones(channels, since=since)
    waiting = quietauto.leave_alone(now)
    keep = keeplist.load()
    ready = [one for one in quiet if one.readable and one.channel_id not in waiting
             and not keeplist.kept(one.channel_id, one.name, keep)]
    picked = ready[:AUTO_PER_RUN]
    batch = [clearout.target(one) for one in picked]
    called = {clearout.target(one): one.name for one in picked}
    ids = {clearout.target(one): one.channel_id for one in picked}
    if not batch:
        await responder.send(
            f"No channel has been quiet since {since:%b %-d, %Y} — nothing to do."
            if not quiet else
            "Every quiet channel left is one I already left for you — nothing new to do."
        )
        return

    _QUIET_RUNNING = True
    try:
        await responder.send(
            f"🧹 **Automatic quiet run** — the {len(batch)} quietest of {len(ready)}:\n"
            + "\n".join(f"• #{called[one]}" for one in batch)
            + "\n\nFor each: keep the sheet and the screenshots, check my own work, and "
            "**press delete only if every check passes**. I press the buttons myself — "
            "press **Leave it** or **Stop the run** before I do to say no. Anything that "
            "needs you, or fails a check, stays and is listed at the end."
            "\n-# Nobody is banned by this."
        )
        went, left, held, trouble = [], [], [], []
        wrong_in_a_row = 0
        for number, target in enumerate(batch, start=1):
            name = called[target]
            try:
                how = await _clear_out(bot, responder, config, target, run=(number, len(batch)), auto=True)
            except Exception as exc:
                log.exception("The automatic quiet run broke on #%s", name)
                await responder.send(f"⚠ Something broke on **#{name}**: {_readable(exc)}. Left as it is.")
                how = "trouble"
            if how == "stopped":
                # Somebody pressed Stop the run before RYTE pressed for them.
                await responder.send("🛑 Stopped — the rest are untouched.")
                break
            if how == "deleted":
                went.append(target)
                wrong_in_a_row = 0
            elif how == "left":
                # A person said "leave it" - their answer, not one to retry.
                left.append(target)
                wrong_in_a_row = 0
            elif how == "kept":
                wrong_in_a_row = 0
            elif how == "held":
                held.append(target)
                wrong_in_a_row = 0
            else:
                trouble.append(target)
                wrong_in_a_row += 1
            # Three in a row wrong is something they all depend on being down.
            if wrong_in_a_row >= 3:
                await responder.send("⚠ Three in a row went wrong, so I've stopped - something they all need is down.")
                break
    finally:
        _QUIET_RUNNING = False

    try:
        # Not taken again for a week. The three that went wrong in a row are
        # the exception - that was something being down, not them.
        again = trouble[-3:] if wrong_in_a_row >= 3 else []
        quietauto.left_for_a_person(
            [ids[one] for one in left + held + trouble if one not in again], now,
        )
    except Exception:
        log.warning("Couldn't write down which channels were left", exc_info=True)

    lines = [f"🧹 **Automatic quiet run done** — {len(went)} deleted."]
    if went:
        lines.append("🗑 " + ", ".join(f"#{called[one]}" for one in went))
    if left:
        lines.append("✖ You said leave it: " + ", ".join(f"#{called[one]}" for one in left))
    if held:
        lines.append("✋ Left for you (needed a look): " + ", ".join(f"#{called[one]}" for one in held))
    if trouble:
        lines.append("❌ Not deleted (a check failed or something broke): "
                     + ", ".join(f"#{called[one]}" for one in trouble))
    if held or trouble:
        lines.append("-# I won't take those again for a week. `@RYTE clearout <name>` to do one by hand.")
    more = len(ready) - len(batch)
    if more > 0:
        lines.append(f"-# {more} more quiet - `@RYTE quiet auto` for the next {AUTO_PER_RUN}.")
    ping = _unmarked_ping(config) if (held or trouble) else ""
    await responder.send(((ping + "\n") if ping else "") + "\n".join(lines))


async def _quiet_keep(bot, responder: Responder, config: Config, how: str, who: str) -> None:
    """`@RYTE quiet keep <name>` / `unkeep <name>` / `kept` - the channels no
    clear-out touches. By the person, so all of their channels at once."""
    from .. import clearout, keeplist

    who = " ".join((who or "").replace("#", " ").split())
    if how == "kept" or not who:
        held = await asyncio.to_thread(keeplist.load)
        people, ids = held.get("people", []), held.get("channels", [])
        await responder.send(
            "🔒 **Keep list** — never cleared out, by hand or on my own:\n"
            + ("\n".join([f"• {one}" for one in people] + [f"• <#{one}>" for one in ids])
               or "• (nobody)")
            + "\n-# `@RYTE quiet keep <name>` adds, `@RYTE quiet unkeep <name>` takes off."
        )
        return
    guild = _clients_guild(bot, config)
    channels = [
        clearout.Channel(channel_id=str(one.id), name=str(one.name))
        for one in (guild.text_channels if guild is not None else [])
    ]
    by_id = re.fullmatch(r"<?#?(\d{6,})>?", who)
    found = ([one for one in channels if one.channel_id == by_id.group(1)] if by_id
             else clearout.channels_for(who, channels))
    if not found and not by_id and how == "keep":
        # Keeping too much only leaves a channel standing, so a first name on
        # its own - "keep mujeeb" - is enough here, where it never is to delete.
        wanted = set(clearout.words_of(who))
        found = [one for one in channels if wanted and wanted <= set(clearout.words_of(one.name))]
    found = [one for one in found if not clearout.off_limits(one)]
    people = sorted({clearout.person_in(one.name) for one in found if clearout.person_in(one.name)})
    if not found and not by_id:
        people = [clearout.person_in(who)] if clearout.person_in(who) else []
    if not people:
        await responder.send(f"I couldn't tell whose channel **{who}** is.")
        return
    named = ", ".join(f"#{one.name}" for one in found) or "no channel right now"
    if how == "keep":
        await asyncio.to_thread(keeplist.keep, people)
        await responder.send(f"🔒 Kept: **{', '.join(people)}** ({named}). No clear-out will touch them.")
    else:
        held = await asyncio.to_thread(keeplist.load)
        typed = clearout.person_in(who)
        gone = [one for one in held.get("people", []) if one in people or (typed and typed in one)]
        if not gone:
            await responder.send(f"**{who}** isn't on the keep list — `@RYTE quiet kept` shows who is.")
            return
        await asyncio.to_thread(keeplist.unkeep, gone)
        await responder.send(f"🔓 Off the keep list: **{', '.join(gone)}** ({named}).")


def _keep_the_run(quietrun, run: dict) -> None:
    """Remember where the run is. Worth doing, not worth stopping it over."""
    try:
        quietrun.save(run)
    except Exception:
        log.warning("Couldn't write down where the quiet run is", exc_info=True)


async def _carry_on_the_run(bot: "WilByteBot") -> None:
    """After a restart, offer to carry on a quiet run that was cut short.

    Asked, not done: it may be hours later and nobody at the desk. Anybody
    the run was for can press it; the two questions still come on every
    channel after that.
    """
    from .. import quietrun

    run = quietrun.load()
    if not quietrun.unfinished(run):
        quietrun.clear()
        return
    channel = bot.get_channel(int(run["channel_id"])) if str(run.get("channel_id") or "").isdigit() else None
    where = (bot.config.secrets.discord_clients_guild_id or "").strip()
    guild = bot.get_guild(int(where)) if where.isdigit() else None
    if channel is None or guild is None:
        log.warning("A quiet run was cut short, but its channel or server is gone")
        quietrun.clear()
        return
    names = list(run["names"])
    at = int(run.get("at") or 0)
    rest = quietrun.still_there(
        names[at:],
        [one.name for one in guild.text_channels]
        + [f"<#{getattr(one, 'id', '')}>" for one in guild.text_channels],
    )
    if not rest:
        quietrun.clear()
        return
    requester = run.get("requester_id")
    responder = ChannelResponder(channel, requester_id=int(requester) if requester else None)
    if run.get("auto"):
        # Asked for, and cut short only by RYTE restarting for an update - so
        # it carries on rather than waiting on a button. Stop the run is how
        # it ends; a stopped run is cleared and never comes back.
        await responder.send(
            f"🧹 I was restarted in the middle of the quiet run — carrying on from "
            f"**#{_channel_label(bot, names[at])}**, {len(rest)} left. "
            "Press **Stop the run** on any card to stop."
        )
        await _all_of_them(bot, responder, bot.config, rest, earlier=run, auto=True)
        return
    view = views.ConfirmView(
        requester_id=responder.requester_id,
        timeout=bot.config.discord.approval_timeout_seconds,
        label="Carry on the run",
        emoji="🧹",
    )
    done = len(run.get("went") or [])
    await responder.send(
        f"🧹 I was restarted in the middle of the quiet run — I'd got to "
        f"**#{_channel_label(bot, names[at])}**, {at + 1} of {len(names)}"
        + (f", with {done} deleted so far" if done else "")
        + f". {len(rest)} left to go. Carry on from there?",
        view=view,
    )
    await view.wait()
    if not view.confirmed:
        quietrun.clear()
        await responder.send("Left it. `@RYTE quiet` starts a fresh list whenever you want.")
        return
    await _all_of_them(bot, responder, bot.config, rest, earlier=run, auto=bool(run.get("auto")))


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

    # Word by word, the lead type off a channel's name - "dylan_rankin-vet"
    # is Dylan Rankin, the "vet" is what he bought.
    if not clearout.words_of(name):
        return None
    best, found = 0, []
    for member in guild.members:
        score = max((
            clearout.name_match(name, called)
            for called in (
                getattr(member, "display_name", ""),
                getattr(member, "global_name", "") or "",
                getattr(member, "name", ""),
            )
        ), default=0)
        if score > best:
            best, found = score, [member]
        elif score and score == best and member not in found:
            found.append(member)
    # Two people as good a match as each other is a question, not an answer:
    # the wrong one's messages kept as the client's are worse than none.
    return found[0] if len(found) == 1 else None


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
        author_id=str(getattr(author, "id", "") or ""),
        mentions=tuple(
            str(getattr(one, "id", "")) for one in getattr(message, "mentions", None) or []
            if getattr(one, "id", None) is not None
        ),
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


async def _board_step(bot: "WilByteBot", step: str, today, *, day=None) -> None:
    """Do one step and say what happened, then never do it again today.

    `day` is the board day the step works on when it isn't today's - last
    night's end of day, caught up the morning after."""
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
            # "do it when we do unticked reminder" - payments with no gavel
            # and no card, a couple of hours old, with the same chase.
            try:
                waiting = await _ungavelled_note(bot)
            except Exception:
                log.warning("Couldn't list the payments nobody's gavelled", exc_info=True)
                waiting = ""
            if waiting:
                note = (note + "\n" if note else (_unmarked_ping(bot.config) + "\n" if _unmarked_ping(bot.config) else "")) + waiting
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
            # ten for Ads and Lead Order - so both read today's cards, or the
            # day being caught up.
            when = day
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
                partial(jobs.walk_board, bot.config, step, day=day)
            )
            note = (
                f"📋 {dailyops.said_at(step)} — moved {moved} card(s) "
                f"{dailyops.STEP_NAMES[step]}."
            ) if moved else ""
        else:
            moved, problems = await asyncio.to_thread(partial(jobs.walk_board, bot.config, step, day=day))
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
    if day is not None and note:
        note = f"⏰ Catching up {day:%m/%d}'s end of day, which I missed:\n{note}"
    # Each branch leaves both empty when its step had nothing to say. A line
    # every morning reporting that nothing happened is a line nobody reads.
    if responder and (note or card):
        await responder.send(note or None, embed=card)
    if responder and top_ups:
        # Posted and let go of, like the tag watcher. This is the board's own
        # clock - six, nine, six and half eight - and awaiting the button
        # held the whole walk up behind it: a top-up offer nobody pressed at
        # six in the morning meant the evening spread and the move into Done
        # never happened, with nothing said about either.
        #
        # Safe to let go of because the step is marked done above, before any
        # of this, so letting go cannot make it run twice.
        _also_running(asyncio.create_task(
            _offer_the_top_ups(responder, bot.config, top_ups)
        ))


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
    """Where to say a post went live: the first allowed channel, if there is one.

    RYTE's own news - updates, "is live" - and so copied into Ryte The Goat's
    announcements rather than wherever the rest of this channel is copied.
    """
    for raw in bot.config.secrets.discord_channel_ids:
        try:
            channel = bot.get_channel(int(raw))
        except (TypeError, ValueError):
            continue
        if channel is not None:
            return Announcing(channel)
    return None


class Announcing:
    """A channel whose messages are copied into the announcements twin."""

    def __init__(self, channel):
        self._channel = channel

    def __getattr__(self, name):
        return getattr(self._channel, name)

    async def send(self, *args, **kw):
        from . import mirror

        with mirror.copying_into(mirror.ANNOUNCE_INTO):
            return await self._channel.send(*args, **kw)


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
        # Laid out once the card exists, so its link can go at the top.
        lay_out=lambda card: segmenting.as_doc(
            payload, keep, short,
            name=segmenting.client_name(searched or title) or "", card=card,
        ),
    )


async def _file_interview(
    responder: Responder, config: Config, keep, *, topic: str, link: str,
    passcode: str, copy: str = "", lay_out=None,
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
            text=(f"{name} — {url}\n\n" if copy else "") + (copy or description),
            paragraphs=lay_out(url) if lay_out else None,
        )
    except PIPELINE_ERRORS as exc:
        where, trouble = "", [f"Couldn't write it into the doc: {jobs._short(exc, 160)}"]
    if where:
        await responder.send(f"📄 Copy in the posting doc — <{where}>")
    for problem in trouble:
        await responder.send(f"⚠ {problem}")

    # And onto the lists of the people who act on it. The card existing is not
    # the same as anybody knowing it exists: it goes on Faith's list for today
    # and on the Videos Need Editing card where the editors are tagged, then into Done,
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
    sop = await _with_documents(message, text, sop)
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
        # And a card still being written is not one to move out from under
        # whoever is writing it. They land finished and wrong - copied from
        # the last agent, then corrected - and the launch date is often the
        # line that gets corrected.
        stuck = [
            plan for plan in stuck
            if not rules.still_being_written(plan.agent, now=now)
        ]

    # The ones that need a person go and wait in Franklin's list. Nothing is
    # filed by this and nothing is marked done; his list is read on every pass
    # like In Que, so a card that gains its launch date is picked up then.
    parked, wouldnt_move = await asyncio.to_thread(
        jobs.park_agents, config, stuck, where
    )
    if parked:
        await responder.send(
            f"📥 Waiting in **{rules.PARKED}**: "
            + ", ".join(f"**{one}**" for one in parked)
            + "\n-# I'll file them as soon as the card says when they go live."
        )
    for one in wouldnt_move:
        await responder.send(f"⚠ {one}")

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
    # "all this time tho Bram has his launch date" - his card came in on the
    # evening before, after the 11:30 step had put tomorrow's setup card on
    # the Ads and Ops checklists, so the setup card he went on was never
    # linked there. Linked again now - safe to run twice.
    from .. import dailyops

    next_worked = dailyops.next_day(jobs.board_day(config))
    if filed and any(plan.when != "today" and getattr(plan.agent, "launch", None) == next_worked for plan in doable):
        now = datetime.now(ZoneInfo(config.schedule.timezone))
        if (now.hour, now.minute) >= LINK_SETUP_AT:
            try:
                linked, link_trouble = await asyncio.to_thread(jobs.link_setup_on_day, config)
                if linked:
                    note += ("\n🔗 Tomorrow's setup card is on " + ", ".join(linked)
                             + " now — it came in after the 11:30 linking.")
                problems = list(problems) + list(link_trouble)
            except Exception:
                log.warning("Couldn't link tomorrow's setup card again", exc_info=True)
    if problems:
        note += "\n⚠ " + "\n⚠ ".join(problems)
    if filed or problems:
        await responder.send(note)
    if silent:
        await _report_stuck(responder, stuck)


#: When tomorrow's setup card goes on the Ads and Ops checklists - the
#: board's 11:30 "link_setup" step.
LINK_SETUP_AT = (11, 30)


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


#: Offers that have been posted and are still waiting on their buttons. Held
#: only so Python does not collect a task mid-flight; nothing reads them.
_OFFERING: set = set()


def _also_running(job) -> None:
    """Keep hold of a background offer, and say so if it breaks.

    A task nobody holds a reference to can be collected while it is still
    waiting, and one that raises with nobody awaiting it raises into nothing -
    which is the same shape as every swallowed error in this file.
    """
    _OFFERING.add(job)

    def done(finished) -> None:
        _OFFERING.discard(finished)
        if finished.cancelled():
            return
        broke = finished.exception()
        if broke is not None:
            log.error("Tag offer failed", exc_info=broke)

    job.add_done_callback(done)


async def tags_loop(bot: "WilByteBot") -> None:
    """Watch the day's cards and offer what was tagged, as it lands.

    Watching, never writing - "from now i dont want it being added
    automatically but a button". The tick brings the work to the button
    instead of the button having to be gone and fetched: every comment and
    every line added to a description shows up here within the minute, and
    stays where it was written until somebody presses it.

    The tick does not wait for the pressing. A button lives for twelve hours
    and most of them are never pressed at all, and a loop that waited on them
    was a loop that stopped for the day at the first one somebody scrolled
    past.

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
                    # Posted, and then let go of. Every flag waits on its own
                    # button for up to the approval timeout, which is twelve
                    # hours - so awaiting them here meant two unanswered
                    # buttons at half past eight in the morning stopped the
                    # watcher until half past eight at night. Everything said
                    # on the board in between was never flagged, and looked
                    # from the outside exactly like RYTE not reading it.
                    #
                    # Safe to let go of, because what has been shown is
                    # written to disk as it is posted rather than after the
                    # button. The next pass offers what is new and never what
                    # is already on the screen waiting.
                    _also_running(asyncio.create_task(
                        _offer_tags_now(responder, bot.config, remember=True)
                    ))
        except asyncio.CancelledError:
            raise
        except Exception:  # a bad tick must not take the loop down for good
            log.exception("Tag check failed; will try again shortly")
        await asyncio.sleep(TAG_CHECK_SECONDS)


def _already_said(task) -> str:
    """The key a line was remembered by before 30 September, still honoured so
    lines shown under it the day this changed are not offered a second time.
    """
    if task.note.comment_id:
        return f"{task.note.comment_id}|{task.kind}|{task.checklist}"
    return f"description|{task.kind}|{task.checklist}|{task.summary}"


def _whose(task) -> str:
    """Who a comment's line is for: the person, not the card it was sent to.

    The card and the checklist come out of reading the comment, and a comment
    is read again whenever the day's checklists change - tomorrow's cards get
    theirs one at a time through the afternoon. Jenn's "Same folder broken
    down ABO setup" was left alone, read again, and came back as a line
    nobody had seen, because the new reading had placed or numbered it
    differently. Who it is for does not move.
    """
    if task.person is not None and getattr(task.person, "username", ""):
        return "@" + str(task.person.username).casefold()
    return "list:" + " ".join(str(task.checklist or "").split()).casefold()


def _words_of(task) -> str:
    """A short fingerprint of the comment's words, so an edited comment can be
    told from the same comment found again."""
    import hashlib

    said = " ".join(str(task.note.text or "").split())
    return hashlib.sha1(said.encode("utf-8")).hexdigest()[:10]


def _said_key(task) -> str:
    """What makes one offered line the same line as another.

    The comment it came from, its words, and whose it is - never the summary.
    The summary is written fresh every run and comes back reworded: the same
    comment gave "Let them know Everlife aged lead 20% off, code everlife20"
    one minute and the same sentence without the comma the next, and a comma
    was enough to make it a line nobody had seen before. Pressing "leave it"
    then meant nothing.

    A description line has no comment to be identified by, so there its own
    words do the job - which is safe, because those go on as they were written
    rather than being summarised.
    """
    if task.note.comment_id:
        return f"comment|{task.note.comment_id}|{_whose(task)}|{_words_of(task)}"
    return f"description|{task.kind}|{task.checklist}|{task.summary}"


def _numbered(bases) -> list:
    counted: dict = {}
    found = []
    for base in bases:
        counted[base] = counted.get(base, 0) + 1
        found.append(f"{base}#{counted[base]}")
    return found


def _said_keys(tasks) -> list:
    """One key per task, numbered where a comment gives somebody several.

    Tre's comment is three jobs for Kath. Without the number all three are the
    same key, so showing the first would silence the other two for the rest of
    the day.
    """
    return _numbered(_said_key(one) for one in tasks)


def _what_was_shown(lately) -> tuple[set, dict]:
    """(comments shown to somebody, as they were worded then; how many lines
    each person was shown from each comment, whatever its wording)."""
    shown, most = set(), {}
    for key in lately:
        base, _, number = str(key).partition("#")
        if not base.startswith("comment|"):
            shown.add(base)
            continue
        parts = base.split("|")
        # comment|id|whose|words - or comment|id|whose, written on the
        # afternoon of 30 September before the words were part of it.
        whose = "|".join(parts[:3])
        shown.add(base if len(parts) > 3 else whose)
        try:
            most[whose] = max(most.get(whose, 0), int(number or 0))
        except ValueError:
            pass
    return shown, most


def _not_shown_yet(tasks, *, today: set, lately: set) -> list:
    """The tasks nobody has been shown.

    A comment is looked up over the last few days, not just today: its id
    belongs to one card, and a line left alone on tomorrow's card yesterday
    afternoon is the same line once that card is today's. A description line
    only today - the same words on each day's card are each day's job.

    And a comment once shown to somebody is shown to them, whatever a later
    pass makes of it. Jenn's "Same folder broken down ABO setup" came back at
    4:44 and again at 4:53 with nothing restarted in between: that pass found
    the comment one more time, which made her line the second of two, and the
    second had never been shown. Only an edit to the comment makes more of it
    new - and then only the lines past the ones already shown.
    """
    shown, most = _what_was_shown(lately)
    old = _numbered(_already_said(one) for one in tasks)
    fresh, taken = [], set()
    for one, key, before in zip(tasks, _said_keys(tasks), old):
        if one.note.comment_id:
            base, _, number = key.partition("#")
            whose = "|".join(base.split("|")[:3])
            if base in shown or whose in shown or before.split("#", 1)[0] in shown:
                continue
            if int(number) <= most.get(whose, 0):
                continue
            # The same line twice in one pass - one comment found on the
            # way to two days' cards - is one line.
            same = (base, " ".join(str(one.summary or "").split()).casefold())
            if same in taken:
                continue
            taken.add(same)
        elif key in today or before in today:
            continue
        fresh.append(one)
    return fresh


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
        lately = await asyncio.to_thread(alreadysaid.said_lately, day)
        tasks = _not_shown_yet(tasks, today=said, lately=lately)
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
        log.info("Tags offered: %s", ", ".join(_said_keys(tasks)))
        # Should a line ever come back, this is what says why: what was on file
        # for the same comment when it was offered again.
        for one in tasks:
            if one.note.comment_id:
                before = sorted(k for k in lately if one.note.comment_id in str(k))
                if before:
                    log.warning("Offered %s again; on file for that comment: %s",
                                _said_key(one), ", ".join(before))

    if remember:
        # "Make sure that they are task, to be done and not updates ... then
        # automatically add them to the checklist without asking me anymore."
        # The watcher files what the reading judged a job, drops what it
        # judged not one, and asks only about a line it has no judgement on.
        sure = [one for one in tasks if one.is_task is True]
        tasks = [one for one in tasks if one.is_task is None]
        if sure:
            await _file_on_its_own(responder, config, sure)
        if not tasks:
            if problems:
                await responder.send("⚠ " + "\n⚠ ".join(problems))
            return

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


async def _file_on_its_own(responder: Responder, config: Config, tasks) -> None:
    """Put the jobs on their checklists, then say what went where."""
    landed, trouble = await asyncio.to_thread(jobs.file_tags, config, tasks)
    if landed:
        said = f"📌 Added {len(landed)} to the checklists:\n" + "\n".join(
            f"• {line}" for line in landed[:TAGS_SHOWN])
        if len(landed) > TAGS_SHOWN:
            said += f"\n…and {len(landed) - TAGS_SHOWN} more."
        await responder.send(said)
    if trouble:
        await responder.send("⚠ " + "\n⚠ ".join(trouble))


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

    backfilled = False
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
                # Set up and on nobody's Ads checklist - Cody Sturma's card
                # had no launch date, so it never went on one. A week back
                # the first time round, to pick up what was already missed.
                hours = jobs.SETUP_HOURS if backfilled else jobs.ADS_BACKFILL_HOURS
                added, trouble = await asyncio.to_thread(jobs.setups_onto_ads, bot.config, hours=hours)
                backfilled = backfilled or not trouble
                note = _ads_added_note(added, trouble, said_already=_ADS_TROUBLE_SAID)
                if note:
                    await responder.send(note)
        except asyncio.CancelledError:
            raise
        except Exception:  # a bad tick must not take the loop down for good
            log.exception("Setup check failed; will try again shortly")
        await asyncio.sleep(SETUP_CHECK_SECONDS)


#: Problems already said in Goat, so a missing checklist is said once and not
#: every ten minutes.
_ADS_TROUBLE_SAID: set[str] = set()


def _ads_added_note(added, trouble, *, said_already: set[str]) -> str:
    """What `setups_onto_ads` did, in a line or a few, or "" for nothing new."""
    trouble = [one for one in trouble if one not in said_already]
    said_already.update(trouble)
    if not added and not trouble:
        return ""
    lines = []
    if added:
        lines.append("📊 **Set up but on no Ads checklist — added:**")
        lines += [f"• {one}" for one in added[:UNMARKED_SHOWN]]
        if len(added) > UNMARKED_SHOWN:
            lines.append(f"…and {len(added) - UNMARKED_SHOWN} more.")
    lines += [f"⚠ {one}" for one in trouble[:5]]
    return "\n".join(lines)


# Fifteen minutes. A line lands on a Lead Order card when somebody runs the
# spread or writes one by hand, and neither happens on a schedule - but neither
# happens every minute either, and this reads every list on the board.
DAY_CHECK_SECONDS = 900

#: How often orders going live are checked for their signed contract.
CONTRACT_CHECK_SECONDS = 1800
#: Not before this hour on the board's clock - a missing contract is a
#: morning job, not a 2am ping.
CONTRACT_CHECK_FROM_HOUR = 7


def _contract_line(one) -> str:
    order = one.order
    when = f"{order.launch:%a %b} {order.launch.day}" if order.launch else "no launch date"
    who = f"[{order.name}](<{order.url}>)" if order.url else f"**{order.name}**"
    return f"• {who} — live {when}"


def describe_contracts(paired, *, everything: bool = False) -> str:
    """What the contract check found, as one message."""
    missing = [one for one in paired if one.contract is None]
    unsure = [one for one in paired if one.how == "name"]
    found = [one for one in paired if one.how == "email"]
    parts = []
    if missing:
        parts.append("📝 **No signed contract in PandaDoc for this order:**\n"
                     + "\n".join(_contract_line(one) for one in missing))
    if unsure:
        parts.append("📝 **A contract by name only - check it's them:**\n" + "\n".join(
            _contract_line(one) + f" — {'; '.join(one.notes)}" for one in unsure
        ))
    if everything and found:
        parts.append("✅ **Signed:**\n" + "\n".join(_contract_line(one) for one in found))
    return "\n\n".join(parts)


async def _delivered(responder: Responder, config: Config, who: str) -> None:
    """`@RYTE delivered <name>` - each of their orders against its sheet - or,
    with no name, every order that should be all in by now and isn't."""
    from .. import delivery

    today = await asyncio.to_thread(jobs.board_day, config)
    if not who.strip():
        short, problems = await asyncio.to_thread(jobs.delivery_check, config, today=today)
        lines = [delivery.describe(one, today=today) for one in short]
        await responder.send(
            ("📦 **Short, a week or more after going live:**\n" + "\n".join(lines) if lines else
             f"📦 Every order live {delivery.DONE_WITHIN_DAYS}–{delivery.LOOK_BACK_DAYS} days ago "
             "with a count on its card has at least that many leads on its sheet.")
            + "".join(f"\n⚠ {one}" for one in problems),
            quiet=True,
        )
        return
    orders, problems = await asyncio.to_thread(jobs.delivered_for, config, who)
    lines = []
    for one in orders:
        lines.append(delivery.describe(one, today=today))
        lines += [f"  ⚠ {problem}" for problem in one.problems]
        proof = delivery.for_a_dispute(one)
        if proof:
            lines.append(f"  -# For a dispute: {proof}")
    if orders:
        lines.append("-# Every row under the heading counts, replacements included. Where the sheet "
                     "dates its leads, only those since this order went live.")
    await responder.send(
        ("📦 **Proof of delivery**\n" + "\n".join(lines) if lines else "")
        + "".join(f"\n⚠ {one}" for one in problems),
        quiet=True,
    )


async def _not_set_up(responder: Responder, config: Config) -> None:
    """`@RYTE not set up` - paid in Payra, nothing (or no launch date) on the board."""
    from .. import paidsetup

    found, problems = await asyncio.to_thread(jobs.paid_not_set_up, config)
    if found:
        said = "💳 **Paid, but not set up:**\n" + "\n".join(paidsetup.describe(one) for one in found)
    else:
        said = (f"💳 Everybody who paid in the last {paidsetup.LOOK_BACK.days} days has a card "
                "with a launch date." if not problems else "")
    await responder.send(said + "".join(f"\n⚠ {one}" for one in problems), quiet=True)


async def alive_loop(bot: "WilByteBot") -> None:
    """"if Ryte goes down, nobody finds out". Back up after more than a
    quarter of an hour gone, say so - and from then on, check in with the
    outside service at HEALTHCHECK_URL every five minutes, so that it is the
    one to tell Franklin when the check-ins stop."""
    from .. import alive

    now = datetime.now(timezone.utc)
    last = await asyncio.to_thread(alive.last_seen)
    if alive.was_down(now, last) is not None:
        channel = _announce_channel(bot)
        if channel is not None:
            try:
                ping = _unmarked_ping(bot.config)
                await channel.send(((ping + "\n") if ping else "")
                                   + alive.back_up(last, now, ZoneInfo(bot.config.schedule.timezone)))
            except Exception:
                log.warning("Couldn't say I'd been down", exc_info=True)
    url = str(getattr(bot.config.secrets, "healthcheck_url", None) or "")
    while not bot.is_closed():
        try:
            await asyncio.to_thread(alive.beat, datetime.now(timezone.utc))
            if url:
                await asyncio.to_thread(alive.check_in, url)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("Down alarm check-in failed", exc_info=True)
        await asyncio.sleep(alive.BEAT_SECONDS)


#: How often filed agents' go-live days are read back against their setup card.
LIVE_MOVED_SECONDS = 900


async def live_moved_loop(bot: "WilByteBot") -> None:
    """"people changes their live date sometimes even they got move to done".
    Every quarter of an hour, the coming week's setup cards against the
    agents on them; a day that moved is said once, Franklin tagged. Reads
    only, and only cards touched since the last look are opened again."""
    from .. import alreadysaid, livemoved

    while not bot.is_closed():
        try:
            responder = _board_responder(bot)
            if responder is not None:
                day = await asyncio.to_thread(jobs.board_day, bot.config)
                found, problems = await asyncio.to_thread(jobs.live_dates_moved, bot.config, today=day)
                said = await asyncio.to_thread(alreadysaid.said_lately, day)
                fresh = [one for one in found if one.key() not in said]
                if fresh:
                    await asyncio.to_thread(alreadysaid.remember, day, [one.key() for one in fresh])
                    ping = _unmarked_ping(bot.config)
                    if ping:
                        await responder.send(ping)
                    # One message and one button per agent, whatever lists
                    # they're on - Emilie Cruz's three lists got three
                    # buttons, and one press moved one line. Let go of: a
                    # button waits hours, and the loop shouldn't.
                    for group in _by_agent(fresh):
                        _also_running(asyncio.create_task(_offer_to_move(responder, bot.config, group)))
                if problems:
                    log.warning("Live-date check: %s", "; ".join(problems))
        except asyncio.CancelledError:
            raise
        except Exception:  # a bad tick must not take the loop down for good
            log.exception("Live-date check failed; will try again shortly")
        await asyncio.sleep(LIVE_MOVED_SECONDS)


async def _itinerary_calendar(responder: Responder, config: Config, said: str) -> None:
    """`@RYTE calendar <sheet link>` - the itinerary as a calendar file, in
    Philippine time, for anybody to import in their own."""
    import io

    from .. import itinerary

    link = next(iter(re.findall(r"https?://docs\.google\.com/spreadsheets/\S+", said or "")), "")
    if not link:
        await responder.send("Which sheet? `@RYTE calendar <Google Sheets link>` - the itinerary, "
                             "shared with my Google account.")
        return
    text, name, found, problems = await asyncio.to_thread(jobs.itinerary_calendar, config, link)
    if not text:
        await responder.send("⚠ " + "\n⚠ ".join(problems))
        return
    events = found.events
    eastern = ZoneInfo("America/New_York")
    manila = ZoneInfo(itinerary.ZONE)
    first = events[0].starts().replace(tzinfo=manila)
    lines = [
        f"📅 **{name}** — {len(events)} event(s), {events[0].day:%b %-d} to {events[-1].day:%b %-d}, "
        "in Philippine time.",
        f"-# Imported, each shows in your own time: the first, {first:%b %-d %-I:%M %p} in Manila, "
        f"is {first.astimezone(eastern):%b %-d %-I:%M %p} Eastern.",
    ]
    odd = found.to_check()
    if odd:
        lines.append("⚠ **Check these in the sheet** — so long it's probably an AM/PM mix-up: " + "; ".join(
            f"row {one.row} “{one.title}” {one.starts():%b %-d %-I:%M %p}–{one.ends():%-I:%M %p}" for one in odd))
    if found.skipped:
        lines.append("Left out: " + "; ".join(
            f"row {row} “{what}” ({why})" for row, what, why in found.skipped[:12]))
    lines.append("**To add it:** download the file, then in Google Calendar on a computer → ⚙️ "
                 "Settings → Import & export → Import → pick the file and the calendar → Import.")
    filename = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-") + ".ics"
    await responder.send("\n".join(lines),
                         file=discord.File(io.BytesIO(text.encode("utf-8")), filename=filename))


async def _live_moved_now(responder: Responder, config: Config) -> None:
    """`@RYTE live changes` - the same look, now, whether or not it was said."""
    found, problems = await asyncio.to_thread(jobs.live_dates_moved, config)
    if not found:
        await responder.send(
            ("📅 Every agent on the coming week's setup cards still goes live on that card's day."
             if not problems else "") + "".join(f"\n⚠ {one}" for one in problems))
        return
    await asyncio.gather(*(_offer_to_move(responder, config, group) for group in _by_agent(found)))
    if problems:
        await responder.send("⚠ " + "\n⚠ ".join(problems))


def _by_agent(found: list) -> list[list]:
    """The moved lines grouped by agent and new day, in the order found."""
    groups: dict = {}
    for one in found:
        groups.setdefault(one.key(), []).append(one)
    return list(groups.values())


async def _offer_to_move(responder: Responder, config: Config, group) -> None:
    """One agent whose day moved, with one button that moves every line of
    theirs - each list they're on - "yes add the button to move it"."""
    from .. import livemoved

    group = group if isinstance(group, list) else [group]
    first = group[0]
    lists = [one.checklist for one in group if one.checklist]
    view = views.ConfirmView(
        requester_id=None,
        timeout=config.discord.approval_timeout_seconds,
        label=f"Move to {first.live:%a %b %d}",
        emoji="📅",
    )
    line = livemoved.one_line(first).split("\n", 1)[-1]
    if len(lists) > 1:
        line = line.replace(f" ({first.checklist}'s list)", f" ({', '.join(lists)}'s lists)")
    await responder.send(
        "📅 **Live date changed after filing:**\n" + line
        + f"\n-# The button moves {'all ' + str(len(group)) + ' of their setup lines' if len(group) > 1 else 'their setup line'}"
        " - and their Lead Order line, if it's been spread.",
        view=view, quiet=True,
    )
    await view.wait()
    if not view.confirmed:
        return
    done, problems = [], []
    for one in group:
        moved, trouble = await asyncio.to_thread(jobs.move_live_line, config, one)
        done += moved
        problems += trouble
    await responder.send("\n".join(done + [f"⚠ {problem}" for problem in problems])
                         or "Nothing needed moving.")


async def paid_check_loop(bot: "WilByteBot") -> None:
    """Every half hour from seven: a payment with no card, or a card with no
    launch date, said once - "money taken with nothing delivered is how a
    chargeback starts". Reads only."""
    from .. import paidsetup

    while not bot.is_closed():
        try:
            responder = _board_responder(bot)
            hour = datetime.now(ZoneInfo(bot.config.schedule.timezone)).hour
            if responder is not None and hour >= CONTRACT_CHECK_FROM_HOUR:
                found, problems = await asyncio.to_thread(jobs.paid_not_set_up, bot.config)
                seen = await asyncio.to_thread(paidsetup.said)
                fresh = [one for one in found if paidsetup.key(one) not in seen]
                if fresh:
                    ping = _unmarked_ping(bot.config)
                    await responder.send(
                        ((ping + "\n") if ping else "")
                        + "💳 **Paid, but not set up:**\n"
                        + "\n".join(paidsetup.describe(one) for one in fresh)
                        + "\n-# `@RYTE not set up` for the whole list.",
                        quiet=True,
                    )
                    await asyncio.to_thread(paidsetup.remember, [paidsetup.key(one) for one in fresh])
                if problems:
                    log.warning("Paid check: %s", "; ".join(problems))
        except asyncio.CancelledError:
            raise
        except Exception:  # a bad tick must not take the loop down for good
            log.exception("Paid check failed; will try again shortly")
        await asyncio.sleep(CONTRACT_CHECK_SECONDS)


#: When the delivery check says its piece, on the board's clock.
DELIVERY_CHECK_HOUR = 9


async def delivery_check_loop(bot: "WilByteBot") -> None:
    """Each morning: every order a week or more live that's short on its
    sheet - "you hear about it before they complain". Each one said once,
    and Franklin tagged. Reads only: the board and the sheets."""
    from .. import delivery

    while not bot.is_closed():
        try:
            responder = _board_responder(bot)
            now = datetime.now(ZoneInfo(bot.config.schedule.timezone))
            if responder is not None and now.hour >= DELIVERY_CHECK_HOUR:
                today = now.date()
                said = await asyncio.to_thread(delivery.flagged)
                if said.get("_day") != today.isoformat():
                    short, problems = await asyncio.to_thread(jobs.delivery_check, bot.config, today=today)
                    fresh = [one for one in short if one.card_id not in said]
                    if fresh:
                        ping = _unmarked_ping(bot.config)
                        await responder.send(
                            ((ping + "\n") if ping else "")
                            + "📦 **Short on their sheet, a week or more after going live:**\n"
                            + "\n".join(delivery.describe(one, today=today) for one in fresh)
                            + "\n-# `@RYTE delivered <name>` for the whole picture.",
                            quiet=True,
                        )
                    # What was said is kept whatever else happened; the day
                    # only once it all worked, so a board that wouldn't read
                    # is tried again in half an hour.
                    if problems:
                        log.warning("Delivery check: %s", "; ".join(problems))
                    await asyncio.to_thread(delivery.remember, {
                        **({} if problems else {"_day": today.isoformat()}),
                        **{one.card_id: one.short_by() for one in fresh},
                    })
        except asyncio.CancelledError:
            raise
        except Exception:  # a bad tick must not take the loop down for good
            log.exception("Delivery check failed; will try again shortly")
        await asyncio.sleep(CONTRACT_CHECK_SECONDS)


#: How often the hub is asked who's newly fulfilled.
HUB_CHECK_SECONDS = 30 * 60


# ------------------------------------------------ the setups, against the hub

#: "we're going back to trello instead, so pause this ... pause but dont
#: delete". The Discord side of setups - the half-hourly check against the
#: hub, the 6pm list, team requests in the Growth server going on the Ryte
#: Checklist card - all of it off while this is True. Nothing is deleted: the
#: card, what RYTE remembers, and the code are all as they were. False turns
#: it back on.
CHECKLIST_PAUSED = True
#: The team's agent-related requests in the Growth server - "@Nicole greff"
#: - on the Ryte Checklist under Concerns, ticked when done, and Franklin
#: pinged in Ryte The Goat about the ones still open: "just ping me on our
#: own server that way ill remind them". On while the setup check is paused.
TEAM_TASKS_ON = True
#: Open this long, and Franklin is pinged about it - once, then once more
#: the next day.
TEAM_REMIND_AFTER = timedelta(hours=4)
TEAM_REMIND_AGAIN = timedelta(hours=20)
TEAM_REMINDERS_MOST = 2
#: The hours Franklin is pinged in, on the board's clock.
TEAM_REMIND_HOURS = (9, 18)
#: How often open requests are looked at again.
TEAM_TASKS_EVERY_SECONDS = 30 * 60
#: The 6pm "🌙 Asked of the team and not done yet" comment on the Ryte
#: Checklist card - "lets stop this". The items stay on the card, ticked as
#: they're done, and the daytime pings in Ryte The Goat carry on.
TEAM_EVENING_NOTE = False
#: The "Still open with the team - worth a nudge" pings: "it should also stop
#: doing this now". The requests are still kept and ticked on the card.
TEAM_NUDGES = False

#: How often everybody scheduled on the hub is checked again.
CHECKLIST_EVERY_SECONDS = 30 * 60
#: When tomorrow's not-ready list goes to Franklin, on the board's clock.
SETUP_EVENING = (18, 0)
#: Bumped when the thread reader changes, so every thread is read again.
SETUP_READ_VERSION = 4
#: How far back an archived thread is still read.
SETUP_THREAD_DAYS = 30
#: The day the team moved order updates from Trello cards to Discord
#: threads. "new orders, new threads" - a thread from before it is an old
#: order's, or a text-inbox one, and nothing to check a new order against.
SETUP_MOVED_ON = date(2026, 10, 12)


def _setup_since(today: date | None = None) -> datetime:
    """The oldest a thread can be and still be a current order's: made since
    the move to Discord, and within the last month."""
    today = today or datetime.now(timezone.utc).date()
    day = max(SETUP_MOVED_ON, today - timedelta(days=SETUP_THREAD_DAYS))
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)


def _started_by_a_person(guild, thread) -> bool:
    """Not a bot's thread: Suki's text-inbox threads ("Gavin · +1 507 ...")
    are texts, not an order. Unknown owner counts as a person."""
    owner = getattr(thread, "owner_id", None)
    if owner is None or guild is None or not hasattr(guild, "get_member"):
        return True
    member = guild.get_member(int(owner))
    return not getattr(member, "bot", False)


def _order_threads(guild, threads, *, today: date | None = None) -> list:
    """The threads and channels that can be a new order's: made since the move
    to Discord, by a person."""
    since = _setup_since(today)
    kept = []
    for one in threads:
        made = getattr(one, "created_at", None)
        if made is None and getattr(one, "id", None):
            try:
                made = discord.utils.snowflake_time(int(one.id))
            except Exception:
                made = None
        if made is not None and made < since:
            continue
        if _started_by_a_person(guild, one):
            kept.append(one)
    return kept


def _setup_guild(bot, config):
    """The Growth Team server - DISCORD_SETUP_GUILD_ID, else by its name."""
    wanted = (getattr(config.secrets, "discord_setup_guild_id", "") or "").strip()
    if wanted.isdigit():
        return bot.get_guild(int(wanted))
    for guild in getattr(bot, "guilds", None) or []:
        if "growth" in _plain_letters(getattr(guild, "name", "")):
            return guild
    return None


async def _setup_threads(guild) -> list:
    """Every thread in the server worth looking in: the open ones, and the
    ones closed in the last month - public and private, in text channels and
    forums (RYTE is an admin there, so it sees them all). Read only."""
    found = {}
    try:
        for one in await guild.active_threads():
            found[one.id] = one
    except Exception:
        log.warning("Couldn't list the open threads", exc_info=True)
    since = _setup_since()
    closed = []
    for channel in getattr(guild, "text_channels", None) or []:
        closed.append(lambda c=channel: c.archived_threads(limit=50))
        closed.append(lambda c=channel: c.archived_threads(private=True, limit=50))
    for channel in getattr(guild, "forums", None) or []:
        closed.append(lambda c=channel: c.archived_threads(limit=50))
    for listing in closed:
        try:
            async for one in listing():
                stamp = getattr(one, "archive_timestamp", None)
                if stamp is not None and stamp < since:
                    break
                found.setdefault(one.id, one)
        except Exception:
            continue
    return list(found.values())


async def _thread_said(thread) -> list[dict]:
    """A thread's messages, oldest first, screenshots as links."""
    said = []
    async for message in thread.history(limit=80):
        images = [str(one.url) for one in getattr(message, "attachments", None) or []
                  if str(getattr(one, "content_type", "") or "").startswith("image/")]
        said.append({
            "id": int(message.id),
            "who": str(getattr(message.author, "display_name", "") or getattr(message.author, "name", "")),
            "when": message.created_at.isoformat(timespec="minutes"),
            "text": _all_of_it(message), "images": images,
        })
    return list(reversed(said))


async def _setup_pass(bot) -> tuple[list, list, list[str]]:
    """Everybody on their way to going live on the hub, checked against their
    thread and their payment, and the Ryte Checklist card kept up to date.
    (results, newly wrong [(title, lines, link)], problems)."""
    from .. import hub, setupcheck

    config = bot.config
    today = _today(config)
    everybody = []
    for status in setupcheck.COMING:
        try:
            everybody += await asyncio.to_thread(hub.agents, config.secrets, status=status)
        except hub.HubError as exc:
            if status == setupcheck.COMING[0]:
                return [], [], [str(exc)]
    agents = setupcheck.coming_up(everybody, today)
    problems = []
    guild = _setup_guild(bot, config)
    if guild is None:
        # Every one of them would read as "no thread" - which isn't so.
        return [], [], ["I'm not in the Growth Team server, so I can't read the threads - "
                        "or set DISCORD_SETUP_GUILD_ID in .env."]
    threads = await _setup_threads(guild)
    # A client with a channel of their own rather than a thread.
    threads += [one for one in getattr(guild, "text_channels", None) or []]
    threads = _order_threads(guild, threads, today=today)
    setup_where = _new_agent_setup_channel(guild)
    posts = await _setup_posts(setup_where, today) if setup_where is not None else None
    if setup_where is None:
        problems.append("No #new-agent-setup channel found in the Growth server, so “setup is done” isn't checked.")
    zelle_where = _zelle_channel(bot)
    zelle = await _zelle_posts(zelle_where) if zelle_where is not None else None
    _SETUP_SEEN.clear()
    _SETUP_SEEN.update(threads=threads, server=str(getattr(guild, "name", "")), posts=len(posts or []),
                       zelle=str(getattr(zelle_where, "name", "")) if zelle_where is not None else "")

    read = setupcheck.load("read")
    results = []
    for agent in agents:
        # Newest talk first: a client back for a second order has the old
        # order's thread too, and that one is no use for this one.
        mine = sorted(setupcheck.threads_for(agent.name, threads),
                      key=lambda one: int(getattr(one, "last_message_id", None) or one.id), reverse=True)[:3]
        asked = None
        if mine:
            said = []
            for thread in mine:
                try:
                    said += await _thread_said(thread)
                except Exception:
                    log.warning("Couldn't read the thread %s", getattr(thread, "name", "?"), exc_info=True)
            said.sort(key=lambda one: one["when"])
            hub_now = "|".join(str(part) for part in (
                agent.lead_type, agent.ordered, agent.daily_cap, ",".join(agent.states),
                agent.schedule, agent.start_date, agent.status))
            sig = f"{SETUP_READ_VERSION}|{max((one['id'] for one in said), default=0)}|{hub_now}"
            key = setupcheck.key_of(agent)
            held = read.get(key) or {}
            got = held.get("got") if held.get("sig") == sig else None
            if got is None and said:
                try:
                    got = await asyncio.to_thread(partial(jobs.read_setup_thread, config, agent, said, today=today))
                    read[key] = {"sig": sig, "got": got}
                except Exception as exc:
                    problems.append(f"Couldn't read {agent.name}'s thread: {_readable(exc)}")
            if got is not None:
                asked = setupcheck.asked_from(got, thread=str(mine[0].name), link=str(getattr(mine[0], "jump_url", "")))
        try:
            paid = await asyncio.to_thread(partial(jobs.paid_for, agent, zelle=zelle))
        except Exception:
            paid = None
        same_client = sum(1 for one in agents if setupcheck.key_of(one) != setupcheck.key_of(agent)
                          and one.name.casefold() == agent.name.casefold())
        results.append(setupcheck.check(
            agent, asked, paid=paid, today=today, zelle=zelle is not None,
            post=setupcheck.post_for(agent.name, posts) if posts is not None else None,
            posts_read=posts is not None, one_order=same_client == 0))
    await asyncio.to_thread(setupcheck.save, "read", read)

    said_before = await asyncio.to_thread(setupcheck.load, "said")
    fresh, said_now, trouble = await asyncio.to_thread(
        partial(jobs.sync_ryte_card, config, results, said_before=said_before))
    problems += trouble
    if not trouble:
        await asyncio.to_thread(setupcheck.save, "said", said_now)
    return results, fresh, problems


def _setup_news(config, fresh) -> str:
    """Newly wrong, said once - for Franklin."""
    ping = _unmarked_ping(config)
    lines = [f"{ping} ⚠ **Setups that don't match** — noted on the Ryte Checklist card:".strip()]
    for title, wrong, link in fresh:
        lines.append(f"**{title}**" + (f" · <{link}>" if link else ""))
        lines += [f"• {line.split(': ', 1)[1]}" for line in wrong]
    return "\n".join(lines)


# ------------------------------------------------ what the team asks each other

#: One at a time: two quick messages mustn't both add or both tick.
_TEAM_LOCK = asyncio.Lock()
_TAGGED = re.compile(r"<@!?(\d+)>")
_MESSAGE_LINK = re.compile(r"discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)")


def _in_setup_guild(bot, guild) -> bool:
    if guild is None:
        return False
    try:
        mine = _setup_guild(bot, bot.config)
    except Exception:
        return False
    return mine is not None and getattr(mine, "id", None) == getattr(guild, "id", None)


def _request_bits(message) -> tuple[str, list[str]]:
    """What a message says and shows - what was forwarded in it too."""
    text = [_all_of_it(message)]
    images = [str(one.url) for one in getattr(message, "attachments", None) or []
              if str(getattr(one, "content_type", "") or "").startswith("image/")]
    for snap in getattr(message, "message_snapshots", None) or []:
        said = _all_of_it(snap)
        if said:
            text.append("(forwarded) " + said)
        images += [str(one.url) for one in getattr(snap, "attachments", None) or []
                   if str(getattr(one, "content_type", "") or "").startswith("image/")]
    return "\n".join(one for one in text if one.strip()), images


def _tagged_people(bot, message) -> list[dict]:
    """The people typed into the message - not a reply's ping, not RYTE, not
    the one asking, not a bot."""
    typed = set(_TAGGED.findall(str(getattr(message, "content", "") or "")))
    me = str(getattr(getattr(bot, "user", None), "id", ""))
    found = []
    for one in getattr(message, "mentions", None) or []:
        key = str(one.id)
        if key in typed and key != me and key != str(message.author.id) and not getattr(one, "bot", False):
            found.append({"id": key, "name": str(getattr(one, "display_name", "") or getattr(one, "name", ""))})
    return found


def _where_said(channel) -> str:
    parent = getattr(channel, "parent", None)
    name = f"the “{getattr(channel, 'name', '')}” thread" if parent is not None else f"#{getattr(channel, 'name', '')}"
    return name + (f" under #{parent.name}" if parent is not None else "")


async def _where_with_context(channel) -> str:
    """Where it was said, and in a thread what the thread hangs off - the
    payment post naming the client. "Set delivery schedule: Mon-Fri 8am-10pm"
    in Killian Cooper's thread is Killian Cooper's schedule."""
    where = _where_said(channel)
    parent = getattr(channel, "parent", None)
    if parent is None:
        return where
    try:
        starter = await parent.fetch_message(channel.id)
    except Exception:
        return where
    said = " ".join(_all_of_it(starter).split())[:400]
    return f"{where} (the thread hangs off this post: {said})" if said else where


async def _team_message(bot, message) -> None:
    """One message in the Growth server: a job for somebody, or a job done."""
    from .. import teamtasks

    try:
        await _team_done_said(bot, message)
        people = _tagged_people(bot, message)
        # "@here Jack Mahn would like to be on a schedule ..." - asked of the
        # room, or of a role: a request too, nobody's in particular.
        to_the_room = bool(getattr(message, "mention_everyone", False) or getattr(message, "role_mentions", None))
        if not people and not to_the_room:
            return
        text, images = _request_bits(message)
        if len(_TAGGED.sub("", text).strip()) < 3 and not images:
            return
        await _team_task_from(bot, message, people, text, images)
    except Exception:
        log.exception("Couldn't read that message for a team request")


async def _team_task_from(bot, message, people, text, images, *, forced=False) -> dict | None:
    """The request on the card, if it is one. The task, or None."""
    from .. import teamtasks

    key = str(message.id)
    async with _TEAM_LOCK:
        held = await asyncio.to_thread(teamtasks.load)
        if key in held:
            return held[key]
    asker = str(getattr(message.author, "display_name", "") or getattr(message.author, "name", ""))
    got = await asyncio.to_thread(partial(
        jobs.read_team_request, bot.config, asker=asker, asked=[one["name"] for one in people],
        text=text, images=images, where=await _where_with_context(message.channel), forced=forced))
    if not got["task"]:
        return None
    task = {
        "id": key, "channel_id": str(message.channel.id), "in_thread": getattr(message.channel, "parent", None) is not None,
        "link": str(getattr(message, "jump_url", "") or ""), "asker": asker, "asker_id": str(message.author.id),
        "assignees": people, "title": got["title"],
        "made": message.created_at.astimezone(timezone.utc).isoformat(timespec="minutes"),
    }
    async with _TEAM_LOCK:
        held = await asyncio.to_thread(teamtasks.load)
        if key in held:
            return held[key]
        task["item_id"] = await asyncio.to_thread(jobs.team_task_add, bot.config, task)
        held[key] = task
        await asyncio.to_thread(teamtasks.save, held)
    log.info("Team request on the card: %s", teamtasks.item_name(task))
    return task


def _says_done_to(task, message) -> bool:
    """A message from somebody it was asked of, saying it's done - anywhere in
    its own thread; in a busy channel only as a reply to the request."""
    from .. import teamtasks

    if not teamtasks.closes_it(task, message.author.id):
        return False
    if not teamtasks.said_done(_all_of_it(message)):
        return False
    if task.get("in_thread"):
        return True
    replied = getattr(getattr(message, "reference", None), "message_id", None)
    return str(replied) == str(task["id"])


async def _team_done_said(bot, message) -> None:
    from .. import teamtasks

    held = await asyncio.to_thread(teamtasks.load)
    for task in teamtasks.in_place(held, message.channel.id):
        if _says_done_to(task, message):
            who = str(getattr(message.author, "display_name", "") or getattr(message.author, "name", ""))
            await _team_task_closes(bot, task["id"], message.author.id, who, when=message.created_at)


async def _team_task_closes(bot, key: str, person_id, who: str, *, when=None, emoji=None) -> bool:
    """Ticked off, if it's open and this is somebody it was asked of - by a
    reaction (`emoji`) or by saying it's done."""
    from .. import teamtasks

    async with _TEAM_LOCK:
        held = await asyncio.to_thread(teamtasks.load)
        task = held.get(str(key))
        if not task or task.get("done_at"):
            return False
        if not (teamtasks.reaction_closes(task, person_id, emoji) if emoji is not None
                else teamtasks.closes_it(task, person_id)):
            return False
        task["done_by"] = who
        task["done_at"] = (when or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="minutes")
        await asyncio.to_thread(jobs.team_task_done, bot.config, task)
        await asyncio.to_thread(teamtasks.save, held)
    log.info("Team request done: %s", teamtasks.item_name(task))
    return True


async def _team_tasks_catch_up(bot) -> None:
    """What was missed while RYTE was down: an emoji on a request, or a "done"
    after it. And done ones from over a month ago forgotten."""
    from .. import teamtasks

    held = await asyncio.to_thread(teamtasks.load)
    for task in teamtasks.open_ones(held):
        channel = bot.get_channel(int(task["channel_id"]))
        if channel is None:
            try:
                channel = await bot.fetch_channel(int(task["channel_id"]))
            except Exception:
                continue
        try:
            asked = await channel.fetch_message(int(task["id"]))
        except Exception:
            continue
        closed = False
        for reaction in getattr(asked, "reactions", None) or []:
            async for person in reaction.users():
                if teamtasks.reaction_closes(task, person.id, reaction.emoji) and not getattr(person, "bot", False):
                    closed = await _team_task_closes(bot, task["id"], person.id, str(
                        getattr(person, "display_name", "") or person.name), emoji=reaction.emoji)
                    break
            if closed:
                break
        if closed:
            continue
        async for later in channel.history(after=asked, limit=100, oldest_first=True):
            if _says_done_to(task, later):
                await _team_task_closes(bot, task["id"], later.author.id, str(
                    getattr(later.author, "display_name", "") or later.author.name), when=later.created_at)
                break
    async with _TEAM_LOCK:
        held = await asyncio.to_thread(teamtasks.load)
        await asyncio.to_thread(teamtasks.save, teamtasks.tidy(held, datetime.now(timezone.utc)))


def _team_reminder_due(task: dict, now: datetime) -> bool:
    """Open long enough, not pinged too often or too lately."""
    def when(said):
        try:
            got = datetime.fromisoformat(str(said))
        except (TypeError, ValueError):
            return None
        return got if got.tzinfo else got.replace(tzinfo=timezone.utc)

    made = when(task.get("made"))
    if made is None or now - made < TEAM_REMIND_AFTER:
        return False
    pinged = [one for one in (when(said) for said in task.get("reminded") or []) if one is not None]
    if len(pinged) >= TEAM_REMINDERS_MOST:
        return False
    return not pinged or now - max(pinged) >= TEAM_REMIND_AGAIN


async def _team_reminders(bot, *, now: datetime | None = None) -> list[dict]:
    """Franklin pinged in Ryte The Goat about requests still open - he
    reminds the team. Nothing is said in the Growth server. The ones pinged."""
    from .. import teamtasks

    now = now or datetime.now(timezone.utc)
    local = now.astimezone(ZoneInfo(bot.config.schedule.timezone))
    if not (TEAM_REMIND_HOURS[0] <= local.hour < TEAM_REMIND_HOURS[1]):
        return []
    async with _TEAM_LOCK:
        held = await asyncio.to_thread(teamtasks.load)
        due = [one for one in teamtasks.open_ones(held) if _team_reminder_due(one, now)]
        if not due:
            return []
        for one in due:
            held[one["id"]].setdefault("reminded", []).append(now.isoformat(timespec="minutes"))
        await asyncio.to_thread(teamtasks.save, held)
    responder = _hub_responder(bot) or _board_responder(bot)
    if responder is None:
        return due
    ping = _unmarked_ping(bot.config)
    lines = [f"{ping} 🕓 **Still open with the team** — worth a nudge:".strip()]
    for one in due:
        again = len(one.get("reminded") or []) > 1
        lines.append(f"• {teamtasks.item_name(one)}" + (" · *second reminder*" if again else "")
                     + (f" · <{one['link']}>" if one.get("link") else ""))
    lines.append("-# Ticked on the Ryte Checklist once they react to it or say it's done.")
    await responder.send("\n".join(lines)[:1900], quiet=True)
    return due


def _client_of(title: str) -> str:
    """"Abdullah Hatem - set lead schedule to 7am-7pm PST" -> "Abdullah Hatem";
    "" when the title doesn't start with a client."""
    parts = re.split(r"\s+[-–—:]\s+", str(title or ""), maxsplit=1)
    return parts[0].strip() if len(parts) == 2 else ""


async def _team_tasks_on_hub(bot, *, every: bool = False) -> list[tuple[dict, dict]]:
    """Each open request held up against the client's record on the hub -
    read only - and ticked off when the hub shows it done. Asked again only
    when their hub record has changed, unless `every`. [(task, verdict)]."""
    import hashlib

    from .. import clearout, hub, teamtasks

    if not getattr(bot.config.secrets, "hub_api_token", None):
        return []
    everybody = await asyncio.to_thread(hub.agents, bot.config.secrets, status="all")
    today = _today(bot.config)
    out = []
    for task in teamtasks.open_ones(await asyncio.to_thread(teamtasks.load)):
        name = _client_of(task.get("title", ""))
        if len(clearout.words_of(name)) < 2:
            continue
        theirs = [one for one in everybody if clearout.name_match(name, one.name) >= 2]
        records = [f"{one.name} · {one.lead_type or '?'} · {one.status or '?'}\n" + "\n".join(hub.fields_of(one))
                   for one in theirs]
        sig = hashlib.sha1("\n".join(records).encode()).hexdigest()[:16]
        if not every and task.get("hub_sig") == sig:
            continue
        try:
            verdict = await asyncio.to_thread(partial(
                jobs.hub_task_verdict, bot.config, task=task.get("title", ""), records=records, today=today))
        except Exception:
            log.warning("Couldn't check %s on the hub", name, exc_info=True)
            continue
        async with _TEAM_LOCK:
            held = await asyncio.to_thread(teamtasks.load)
            if task["id"] in held:
                held[task["id"]].update(hub_sig=sig, hub_verdict=verdict["verdict"], hub_why=verdict["why"])
                await asyncio.to_thread(teamtasks.save, held)
        if verdict["verdict"] == "done":
            await _team_task_done_on_hub(bot, task["id"], verdict["why"])
        out.append((task, verdict))
    return out


async def _team_task_done_on_hub(bot, key: str, why: str) -> None:
    """Ticked off because the hub shows it done - said so on the item."""
    from .. import teamtasks

    async with _TEAM_LOCK:
        held = await asyncio.to_thread(teamtasks.load)
        task = held.get(str(key))
        if not task or task.get("done_at"):
            return
        task["done_by"] = "the hub"
        task["done_at"] = datetime.now(timezone.utc).isoformat(timespec="minutes")
        await asyncio.to_thread(jobs.team_task_done, bot.config, task)
        await asyncio.to_thread(teamtasks.save, held)
    log.info("Team request done on the hub: %s (%s)", teamtasks.item_name(task), why)


async def _team_hub_check(bot, responder: Responder) -> None:
    """"@Ryte hub tasks" - every open request checked on the hub, now."""
    from .. import hub, teamtasks

    try:
        await _team_tasks_from_card(bot)
    except Exception:
        log.warning("Couldn't read the Ryte Checklist card", exc_info=True)
    if not teamtasks.open_ones(teamtasks.load()):
        await responder.send("Nothing asked of the team is open. ✅")
        return
    await responder.send("🔎 Checking each open request on the hub (reading only)…")
    try:
        checked = await _team_tasks_on_hub(bot, every=True)
    except hub.HubError as exc:
        await responder.send(f"⚠ {exc}")
        return
    if not checked:
        await responder.send("None of the open requests name a client I can look up on the hub.")
        return
    marks = {"done": "✅", "not_done": "⏳", "unclear": "❔"}
    lines = [f"**{len(checked)} checked on the hub** — ✅ done there (ticked on the card), ⏳ not yet, ❔ not something the hub shows:"]
    for task, verdict in checked:
        lines.append(f"{marks[verdict['verdict']]} {task.get('title', '')} — {verdict['why']}")
    await responder.send("\n".join(lines)[:3900])


async def team_tasks_loop(bot: "WilByteBot") -> None:
    """Every half hour: what was done while nobody was looking ticked off,
    Franklin pinged about what's still open, and at six the day's open ones
    on the card for him."""
    from .. import teamtasks

    teamtasks.ZONE = bot.config.schedule.timezone or teamtasks.ZONE
    await asyncio.sleep(90)
    while not bot.is_closed():
        try:
            try:
                await _team_tasks_from_card(bot)
            except Exception:
                log.warning("Couldn't read the Ryte Checklist card", exc_info=True)
            await _team_tasks_catch_up(bot)
            try:
                await _team_tasks_on_hub(bot)
            except Exception:
                log.warning("Couldn't check the open requests on the hub", exc_info=True)
            if TEAM_NUDGES:
                await _team_reminders(bot)
            now = datetime.now(ZoneInfo(bot.config.schedule.timezone))
            today = _today(bot.config).isoformat()
            if TEAM_EVENING_NOTE and (now.hour, now.minute) >= SETUP_EVENING and teamtasks.said_today() != today:
                open_now = teamtasks.open_ones(await asyncio.to_thread(teamtasks.load))
                if open_now:
                    text = "🌙 Asked of the team and not done yet:\n" + "\n".join(
                        f"• {teamtasks.item_name(one)}" + (f" · <{one['link']}>" if one.get("link") else "")
                        for one in open_now)
                    await asyncio.to_thread(jobs.ryte_card_note, bot.config, text)
                await asyncio.to_thread(teamtasks.said_on, today)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("The team requests check failed; trying again next time")
        await asyncio.sleep(TEAM_TASKS_EVERY_SECONDS)


async def _team_track(bot, responder: Responder, said: str, author) -> None:
    """"@Ryte track <message link>" - that message on the card as a job."""
    from .. import teamtasks

    found = _MESSAGE_LINK.search(said or "")
    if not found:
        await responder.send("Send me the message's link: right-click it → **Copy Message Link**, then "
                             "`@Ryte track <link>`.")
        return
    _guild, channel_id, message_id = (int(one) for one in found.groups())
    try:
        channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
        message = await channel.fetch_message(message_id)
    except Exception as exc:
        await responder.send(f"⚠ Couldn't open that message: {_readable(exc)}")
        return
    text, images = _request_bits(message)
    people = _tagged_people(bot, message)
    try:
        task = await _team_task_from(bot, message, people, text, images, forced=True)
    except Exception as exc:
        await responder.send(f"⚠ {_readable(exc)}")
        return
    if task.get("done_at"):
        await responder.send(f"Already done: {teamtasks.done_name(task)}")
        return
    who = ", ".join(one["name"] for one in people) or "anybody but the one who asked"
    await responder.send(f"📌 On the Ryte Checklist under **{teamtasks.TASKS_LIST}**: {teamtasks.item_name(task)}\n"
                         f"-# Ticked when {who} reacts to it or says it's done.")


async def _team_tasks_from_card(bot) -> list[dict]:
    """"most of these are done" - ticked on the Ryte Checklist card by hand.
    The card is the record: an item ticked there is done, one deleted there
    is no longer tracked. The ones closed this way."""
    from .. import teamtasks

    states = await asyncio.to_thread(jobs.ryte_card_items, bot.config)
    closed = []
    async with _TEAM_LOCK:
        held = await asyncio.to_thread(teamtasks.load)
        for task in teamtasks.open_ones(held):
            item = str(task.get("item_id") or "")
            if not item:
                continue
            state = states.get(item)
            if state == "complete":
                task["done_by"] = "ticked on the card"
            elif state is None:
                task["done_by"] = "taken off the card"
            else:
                continue
            task["done_at"] = datetime.now(timezone.utc).isoformat(timespec="minutes")
            closed.append(task)
        if closed:
            await asyncio.to_thread(teamtasks.save, held)
    return closed


async def _team_list(responder: Responder, bot=None) -> None:
    """"@Ryte team tasks" - what's still open."""
    from .. import teamtasks

    try:
        if bot is not None:
            await _team_tasks_from_card(bot)
    except Exception:
        log.warning("Couldn't read the Ryte Checklist card", exc_info=True)
    held = await asyncio.to_thread(teamtasks.load)
    open_now = teamtasks.open_ones(held)
    if not open_now:
        await responder.send("Nothing asked of the team is open. ✅")
        return
    await responder.send("\n".join([f"🕓 **{len(open_now)} asked of the team, not done yet:**"] + [
        f"• {teamtasks.item_name(one)}" + (f" · <{one['link']}>" if one.get("link") else "")
        for one in open_now])[:3900])


# ------------------------------------------------ orders made into cards

#: One gavel at a time: a double-click mustn't make two cards.
_ORDER_LOCK = asyncio.Lock()
#: Notes come in bursts. A card is updated this long after the last one -
#: each new note starts it again, so a burst is still one update. Two
#: minutes was too slow: "if it will, it should be faster".
ORDER_NOTES_SETTLE_SECONDS = 30
#: {order: the update waiting for the notes to settle}.
_ORDER_PENDING: dict = {}
#: {order: the "Making the card…" message, to turn into the card's link}.
_ORDER_ACKS: dict = {}
#: How long the Create button on a fresh order waits.
ORDER_ASK_SECONDS = 24 * 60 * 60
#: Whether a new agent's card waits for the Create button. "i want it that
#: when ryte saw that emoji, it creates the card automatically" - off: the
#: gavel is the go-ahead for both kinds.
FRESH_ASKS_FIRST = False
#: "actually focus on new agent setup for now, not aged leads" - an aged
#: order gavelled is left alone, said so. True turns them back on.
AGED_ORDERS_ON = True
#: Orders being read or waiting on their button right now - a second gavel
#: meanwhile isn't a second card. Not kept over a restart, where the button
#: is gone anyway: gavelling it again asks again.
_ORDER_ACTIVE: set = set()


def _said_line(message, tz: str = "") -> str:
    """"[10/18 09:40PM] Kath: launch tomorrow" - stamped on the board's
    clock when it's known: Discord's is UTC, and a Sunday-evening note read
    as Monday's made its "tomorrow" a Tuesday."""
    who = str(getattr(message.author, "display_name", "") or getattr(message.author, "name", ""))
    text, _images = _request_bits(message)
    when = message.created_at
    if tz:
        try:
            from zoneinfo import ZoneInfo

            when = when.astimezone(ZoneInfo(tz))
        except Exception:
            pass
    return f"[{when:%m/%d %I:%M%p}] {who}: {text}"


async def _order_said(bot, message, *, since: datetime | None = None,
                      threads: tuple = ()) -> tuple[str, list[str], int | None, list[str]]:
    """The order: the gavelled message - forwards and screenshots too - and
    every note in its thread, and in `threads` (ids of other threads found
    to be about it). (text, images, the thread's id or None, the team's own
    notes in the thread as they wrote them)."""
    from .. import ordercards

    tz = str(getattr(getattr(getattr(bot, "config", None), "schedule", None), "timezone", "") or "")

    lines, images, notes = [], [], []
    # The team's screenshots first, numbered as they're marked in the notes -
    # "[screenshot 1]" is the first picture Claude is shown.
    note_images = []
    channel = message.channel
    thread = None
    if getattr(channel, "parent", None) is not None:
        # Gavelled inside the thread: the post it hangs off, then the thread.
        thread = channel
        try:
            starter = await channel.parent.fetch_message(channel.id)
            lines.append(_said_line(starter, tz))
            images += _request_bits(starter)[1]
        except Exception:
            pass
    else:
        lines.append(_said_line(message, tz))
        images += _request_bits(message)[1]
        guild = getattr(message, "guild", None)
        thread = getattr(message, "thread", None) or (guild.get_thread(message.id) if guild is not None else None)
        if thread is None and hasattr(bot, "fetch_channel"):
            # Not in Discord's cache - archived for going quiet, or RYTE
            # restarted since: Ava's and Zach's notes went unread that way.
            thread = await _order_thread(bot, str(message.id), {})
    more = []
    for wanted in threads or ():
        if thread is not None and str(wanted) == str(thread.id):
            continue
        found = await _order_thread(bot, str(wanted), {})
        if found is None:
            continue
        more.append(found)
        # A thread started from somebody's own message: that message is the
        # first note in it.
        try:
            starter = await found.parent.fetch_message(found.id)
        except Exception:
            starter = None
        if starter is not None and not getattr(starter.author, "bot", False):
            lines.append(_said_line(starter, tz))
            said = _note_without_tags(_request_bits(starter)[0]).strip()
            if said:
                notes.append(said)
    for reading in ([thread] if thread is not None else []) + more:
        try:
            async for one in reading.history(limit=100, oldest_first=True):
                if (getattr(one.author, "bot", False) and not getattr(one, "embeds", None)
                        and not _payment_details(one)):
                    continue
                lines.append(_said_line(one, tz))
                said, pictures = _request_bits(one)
                if getattr(one.author, "bot", False):
                    images += pictures
                    continue
                marks = []
                for picture in pictures:
                    note_images.append(picture)
                    marks.append(f"[screenshot {len(note_images)}]")
                written = "\n".join(part for part in (_note_without_tags(said).strip(), *marks) if part)
                # After the card was made, "setup done" and "signed @Nicole"
                # are the team talking, not the order.
                after = since is not None and getattr(one, "created_at", None) is not None and one.created_at > since
                if written and not (after and ordercards.is_chatter(written)):
                    notes.append(written)
        except Exception:
            log.warning("Couldn't read the order's thread", exc_info=True)
    return "\n".join(lines), note_images + images, (int(thread.id) if thread is not None else None), notes


def _payment_details(message) -> bool:
    """Nova's "💳 Payment details — from the Payra invoice" in a payment's
    thread: the invoice's notes - states, how many - written out, from a bot
    and with no embed, so it was passed over with the bots' chatter."""
    said = str(getattr(message, "content", "") or "")
    return bool(re.search(r"payment\s+details", said, re.IGNORECASE)) or (
        bool(re.search(r"invoice\W*:", said, re.IGNORECASE)) and bool(re.search(r"\bnotes\W*:", said, re.IGNORECASE)))


def _note_without_tags(text: str) -> str:
    """The team's note without anybody's tag - "@Franklin | General Manager"
    at the end of Samuel Riggins' notes was Kharyl telling Franklin, not the
    order, and on a Trello card "@franklin" pings him. A line that was only
    tags goes."""
    kept = []
    for line in str(text or "").splitlines():
        bare = re.sub(r"<@[!&]?\d+>|<#\d+>|@(?:here|everyone)\b", "", line)
        if line.strip() and not bare.strip(" |·-–—,"):
            continue
        kept.append(" ".join(bare.split()) if bare != line else line)
    return "\n".join(kept).strip()


def _named_tags(message, text: str) -> str:
    """Discord's tags as names - "<@757873984934117396>" is "@Nicole" on a
    Trello card, where the raw tag shows as a number."""
    names = {str(one.id): str(getattr(one, "display_name", "") or getattr(one, "name", ""))
             for one in getattr(message, "mentions", None) or []}
    roles = {str(one.id): str(getattr(one, "name", "")) for one in getattr(message, "role_mentions", None) or []}
    channels = {str(one.id): str(getattr(one, "name", "")) for one in getattr(message, "channel_mentions", None) or []}
    said = re.sub(r"<@!?(\d+)>", lambda m: "@" + (names.get(m.group(1)) or "someone"), str(text or ""))
    said = re.sub(r"<@&(\d+)>", lambda m: "@" + (roles.get(m.group(1)) or "a role"), said)
    return re.sub(r"<#(\d+)>", lambda m: "#" + (channels.get(m.group(1)) or "a channel"), said)


async def _order_examples(config) -> dict:
    found = {}
    for kind in ("fresh", "aged"):
        try:
            found[kind] = await asyncio.to_thread(jobs.order_template, config, kind)
        except Exception as exc:
            found[kind] = None
            log.info("No %s example card: %s", kind, exc)
    return found


async def _order_read(bot, message, *, since: datetime | None = None,
                      extra: tuple = (), threads: tuple = ()) -> tuple[dict, dict, int | None]:
    text, images, thread_id, notes = await _order_said(bot, message, since=since, threads=threads)
    # The Payra invoice's note, for an aged order made from it: read with the
    # order, and on the card first in Additional Notes.
    if extra:
        text += "\n" + "\n".join(extra)
        notes = list(extra) + list(notes)
    # The client's New Lead Request in #lead-lab, for the Agency.
    requests = await _lead_requests(bot)
    request = jobs.lead_request_for(requests, text)
    if not request:
        requests = await _lead_requests(bot, wait=15, fresh_within=LEAD_REQUESTS_RECHECK_SECONDS)
        request = jobs.lead_request_for(requests, text)
    if not request:
        log.info("No New Lead Request in #lead-lab for this order (%d posts read)", len(requests))
        _log_near_miss(requests, text)
    # Every agency #lead-lab has named, so one written alone in the notes is known.
    agencies = tuple(dict.fromkeys(filter(None, (jobs.request_agency(one) for one in requests))))
    card = await asyncio.to_thread(partial(
        jobs.read_order, bot.config, text=text, images=images, today=_today(bot.config), notes=notes,
        **({"request": request} if request else {}), **({"agencies": agencies} if agencies else {})))
    card["lead_request"] = bool(request)
    # The team's screenshots - the first ones in `images`, one a marker.
    marked = sum(len(re.findall(r"\[screenshot \d+\]", one)) for one in notes)
    card["shots"] = images[:marked]
    return card, {}, thread_id


#: #lead-lab as last read: (read at, posts newest first).
_LEAD_REQUESTS: list = [0.0, []]
#: Read again after this long - a request is posted before the payment.
LEAD_REQUESTS_FRESH_SECONDS = 300
#: How many of its posts are looked through - a request can be weeks
#: before the payment.
LEAD_REQUESTS_READ = 1500
#: How many of its archived threads - Nova Cloud posts some requests as a
#: thread ("Riley Noah · Aged IUL - 30-90 days"), the details inside it.
LEAD_THREADS_READ = 300
#: A thread's opening, read once: {thread id: text}. It doesn't change.
_LEAD_THREADS: dict = {}


async def _lead_thread_text(thread) -> str:
    """A request thread as text: its title, then its first few posts."""
    held = _LEAD_THREADS.get(thread.id)
    if held is not None:
        return held
    title = str(getattr(thread, "name", "") or "")
    # "Riley Noah · Aged IUL - 30-90 days" - who it's for, before the dot.
    parts = [title] + ([f"Name: {title.split('·')[0].strip()}"] if "·" in title else [])
    try:
        async for one in thread.history(limit=5, oldest_first=True):
            parts.append(_all_text(one))
    except Exception:
        log.info("Couldn't read the #lead-lab thread %s", getattr(thread, "name", ""), exc_info=True)
        _LEAD_THREADS[thread.id] = "\n".join(parts)
        return _LEAD_THREADS[thread.id]
    text = "\n".join(part for part in parts if part)
    _LEAD_THREADS[thread.id] = text
    return text


#: The read of #lead-lab under way, if one is: cards don't wait on it.
_LEAD_READING: list = [None]
#: How long a card waits for #lead-lab before going ahead with what's held.
LEAD_REQUESTS_WAIT = 5.0
#: ...and when nothing has been read yet.
LEAD_REQUESTS_FIRST_WAIT = 20.0
#: A request not found in a read older than this gets #lead-lab read again
#: - it may have been posted a minute before the payment.
LEAD_REQUESTS_RECHECK_SECONDS = 60
#: How often #lead-lab is kept read, and blank Agencies filled in after.
AGENCY_FILL_SECONDS = 240
#: How long after a card is made its blank Agency is still looked for.
AGENCY_FILL_DAYS = 2
#: New threads opened per read - the rest on the next one, so the first
#: read after a start isn't three hundred requests in a row.
LEAD_THREADS_PER_READ = 40


async def _lead_requests(bot, *, wait: float | None = None, fresh_within: float | None = None) -> list[str]:
    """#lead-lab's posts and request threads, newest first, as text - each
    client's New Lead Request, where the agency they're with is said.

    Never what holds a card up: "it stopped and now not filing cards" - the
    first read after an update went through hundreds of threads while every
    order waited on it. One read at a time, in the background; a card waits a
    few seconds for it and otherwise goes ahead with the last read, or with
    none - a card without an agency is still a card."""
    import time

    held = _LEAD_REQUESTS[1]
    fresh = LEAD_REQUESTS_FRESH_SECONDS if fresh_within is None else fresh_within
    if held and time.monotonic() - _LEAD_REQUESTS[0] < fresh:
        return held
    # Nothing read yet - just after a start - is worth waiting longer for.
    wait = wait if wait is not None else (LEAD_REQUESTS_WAIT if held else LEAD_REQUESTS_FIRST_WAIT)
    task = _LEAD_READING[0]
    if task is None or task.done():
        task = asyncio.ensure_future(_lead_requests_read(bot))
        _LEAD_READING[0] = task
    try:
        return await asyncio.wait_for(asyncio.shield(task), wait)
    except asyncio.TimeoutError:
        log.info("#lead-lab still being read - going ahead with %d posts held", len(held))
        return held
    except Exception:
        log.warning("Couldn't read #lead-lab for the agency", exc_info=True)
        return held


#: #lead-lab's posts as read: {message id: (posted at, text)}.
_LEAD_POSTS: dict = {}

#: Why #lead-lab couldn't be read, said once in Goat; "" when it's fine.
_LEAD_TROUBLE: list = [""]


def _log_near_miss(posts, text: str) -> None:
    """A #lead-lab post that names the client but wasn't taken as their
    request, as it reads - so what it looks like to RYTE can be seen."""
    emails, phones, names = jobs._who_in(text)
    keys = [one for one in (*emails, *names) if one] + list(phones)
    for post in posts or ():
        flat = " ".join(str(post).split()).casefold()
        digits = re.sub(r"\D", "", str(post))
        if any(key in flat for key in emails | names) or any(phone in digits for phone in phones):
            log.info("#lead-lab post naming them, not taken (agency read: %r): %r",
                     jobs.request_agency(post), str(post)[:600])
            return
    log.info("Nothing in #lead-lab names %s", ", ".join(sorted(keys)) or "them")


def _lead_lab_channel(bot):
    """#lead-lab - DISCORD_LEAD_REQUEST_CHANNEL_ID, else found by name in
    any server RYTE is in."""
    secrets = getattr(getattr(bot, "config", None), "secrets", None)
    wanted = str(getattr(secrets, "discord_lead_request_channel_id", "") or "").strip()
    channel = bot.get_channel(int(wanted)) if wanted.isdigit() and hasattr(bot, "get_channel") else None
    if channel is not None and hasattr(channel, "history"):
        return channel
    for guild in getattr(bot, "guilds", None) or []:
        for one in getattr(guild, "text_channels", None) or []:
            if _plain_letters(getattr(one, "name", "")) == "leadlab":
                log.info("#lead-lab found by name: %s (%s)", one.name, one.id)
                return one
    return None


async def _lead_requests_read(bot) -> list[str]:
    import time

    channel = _lead_lab_channel(bot)
    try:
        if channel is None:
            _LEAD_TROUBLE[0] = "I can't find #lead-lab"
            log.warning("Can't see #lead-lab - no agency from New Lead Requests")
            return []
        # The whole channel once, then only what's new since - a full read
        # each time took longer than a card waits ("still being read").
        newest = max((one for one in _LEAD_POSTS if one > 0), default=None)
        if newest is None:
            got = [one async for one in channel.history(limit=LEAD_REQUESTS_READ)]
        else:
            got = [one async for one in channel.history(limit=None, after=discord.Object(id=newest))]
        for one in got:
            _LEAD_POSTS[getattr(one, "id", None) or -len(_LEAD_POSTS) - 1] = (
                getattr(one, "created_at", None), _all_text(one))
        found = list(_LEAD_POSTS.values())
    except Exception as exc:
        _LEAD_TROUBLE[0] = f"I can't read #lead-lab ({_readable(exc)})"
        log.warning("Couldn't read #lead-lab for the agency", exc_info=True)
        return _LEAD_REQUESTS[1]
    _LEAD_TROUBLE[0] = ""
    if found and not any("agency" in text.casefold() for _when, text in found):
        # Posts, but none with a word of what's in them - no message content.
        _LEAD_TROUBLE[0] = (f"#lead-lab reads as {len(found)} posts with nothing in them - "
                            "I may be missing the Message Content permission")
    threads = list(getattr(channel, "threads", None) or [])
    try:
        if hasattr(channel, "archived_threads"):
            threads += [one async for one in channel.archived_threads(limit=LEAD_THREADS_READ)]
    except Exception:
        log.info("Couldn't list #lead-lab's archived threads", exc_info=True)
    seen, opened = set(), 0
    # Newest first, so the ones a payment today is about are read first.
    for thread in sorted(threads, key=lambda one: one.id, reverse=True):
        if thread.id in seen:
            continue
        seen.add(thread.id)
        if thread.id not in _LEAD_THREADS:
            if opened >= LEAD_THREADS_PER_READ:
                continue
            opened += 1
        found.append((discord.utils.snowflake_time(thread.id), await _lead_thread_text(thread)))
    oldest = datetime.min.replace(tzinfo=timezone.utc)
    posts = [text for _when, text in sorted(found, key=lambda one: one[0] or oldest, reverse=True)]
    # Read again soon while threads are still waiting to be opened.
    waiting = any(one.id not in _LEAD_THREADS for one in threads)
    _LEAD_REQUESTS[:] = [time.monotonic() - (LEAD_REQUESTS_FRESH_SECONDS - 30 if waiting else 0), posts]
    return posts


def _order_who(order: dict) -> str:
    """A made order's client, the way `lead_request_for` matches them."""
    fields = order.get("fields") or {}
    return "\n".join(f"{label}: {fields.get(key) or ''}" for key, label in
                     (("name", "NAME"), ("email", "EMAIL"), ("phone", "PHONE")) if fields.get(key))


async def _agency_fill(bot) -> list[str]:
    """Blank Agencies on cards made in the last two days, filled in from
    #lead-lab - Aidan Moskovitz's card was made while #lead-lab was still
    being read, his request (Nonstop Financial) right there. ["name: agency"]."""
    from .. import ordercards

    posts = await _lead_requests(bot, wait=300)
    if not posts:
        return []
    held = await asyncio.to_thread(ordercards.load)
    now = datetime.now(timezone.utc)
    filled = []
    for key, order in list(held.items()):
        fields = order.get("fields") or {}
        if order.get("status") != "made" or not order.get("card_id") or fields.get("agency") \
                or order.get("agency_filled") or "agency" not in fields:
            continue
        try:
            made = datetime.fromisoformat(str(order.get("made_at") or ""))
        except ValueError:
            continue
        if now - made > timedelta(days=AGENCY_FILL_DAYS):
            continue
        agency = jobs.request_agency(jobs.lead_request_for(posts, _order_who(order)))
        if not agency or not jobs.agency_usable(agency):
            continue
        try:
            done = await asyncio.to_thread(jobs.order_card_fill_agency, bot.config, order["card_id"], agency)
        except Exception:
            log.warning("Couldn't fill in %s's agency", order.get("client"), exc_info=True)
            continue
        # Still RYTE's writing, so new notes keep updating it.
        await _order_note(key, agency_filled=agency, fields={**fields, "agency": agency},
                          **({"written": done} if done and ordercards.same_text(
                              str(order.get("written") or ""), re.sub(r"(?im)^([ \t]*Agency[ \t]*:)[ \t]*" + re.escape(agency) + r"[ \t]*$", r"\1", done)) else {}))
        if done:
            filled.append(f"{order.get('client') or order.get('name')}: {agency} — {order.get('url', '')}")
    return filled


_LEAD_TROUBLE_SAID: list = [""]


async def tag_items_loop(bot: "WilByteBot") -> None:
    """Comments RYTE tagged people in, looked at again: whoever the board's
    automation didn't give a checklist item gets one."""
    while not bot.is_closed():
        for check in jobs.tag_checks_due():
            try:
                added = await asyncio.to_thread(jobs.tag_items_fill, bot.config, check)
                if added:
                    log.info("Tagged but no checklist item from the board - added for %s", ", ".join(added))
            except Exception:
                log.warning("Couldn't check the tagged comment's checklist items", exc_info=True)
        await asyncio.sleep(30)


async def agency_fill_loop(bot: "WilByteBot") -> None:
    """#lead-lab kept read, so a card never waits on it, and a card made
    before its request was read gets its Agency filled in after."""
    await asyncio.sleep(30)
    while not bot.is_closed():
        try:
            filled = await _agency_fill(bot)
            trouble = _LEAD_TROUBLE[0]
            if trouble and trouble != _LEAD_TROUBLE_SAID[0]:
                said_to = _order_responder(bot)
                if said_to is not None:
                    await said_to.send(f"🏢 ⚠ {trouble} — so no Agency from New Lead Requests. "
                                       "Check I can see it (View Channel, Read Message History).")
                _LEAD_TROUBLE_SAID[0] = trouble
            responder = _order_responder(bot) if filled else None
            if responder is not None:
                await responder.send("🏢 **Agency filled in from #lead-lab:**\n" + "\n".join(f"• {one}" for one in filled))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Filling in agencies failed; trying again shortly")
        await asyncio.sleep(AGENCY_FILL_SECONDS)


async def _tag_check(bot) -> str:
    """"tag check" - every tag on the day's cards on somebody's checklist."""
    try:
        tasks, problems = await asyncio.to_thread(partial(jobs.tags_to_file, bot.config, everything=True))
    except Exception as exc:
        return f"⚠ Couldn't read the cards: {_readable(exc)}"
    if not tasks and not problems:
        return "✅ Every comment that tags somebody on today's cards is on their checklist."
    landed, trouble = await asyncio.to_thread(jobs.file_tags, bot.config, tasks) if tasks else ([], [])
    out = [f"📌 Missed and now added — {len(landed)}:"] if landed else ["✅ Nothing tagged was missing."]
    out += [f"• {one}" for one in landed[:30]]
    if len(landed) > 30:
        out.append(f"…and {len(landed) - 30} more.")
    out += [f"⚠ {one}" for one in (list(problems) + list(trouble))[:10]]
    return "\n".join(out)[:1950]


async def _setup_balance(bot, *, fix: bool = False) -> str:
    """"setup check" - said."""
    try:
        found = await asyncio.to_thread(partial(jobs.setup_balance, bot.config, fix=fix))
    except Exception as exc:
        return f"⚠ Couldn't read the setup cards: {_readable(exc)}"
    if not found:
        return "No setup card for the coming days with Therese / Kathleen / Nicole checklists on it."
    out = []
    for card in found:
        counts = " · ".join(f"{person} {count}" for person, count in card["counts"].items())
        out.append(f"📋 **{card['name']}** — {counts}\n{card['url']}")
        if not (card["missing"] or card["twice"] or card["loose"]):
            out.append("✅ Every agent is on every checklist, once.")
            continue
        for person, who, _line in card["missing"][:20]:
            out.append(f"• **{who}** — not on **{person}**'s" + (" → added" if (person, who) in card["added"] else ""))
        for person, who in card["twice"][:10]:
            out.append(f"• **{who}** — on **{person}**'s twice")
        for person, line in card["loose"][:10]:
            out.append(f"• {person}'s has a line that isn't an agent: “{line}”")
        if card["missing"] and not fix:
            out.append("-# `@Ryte setup check fix` copies the missing lines over, as the others have them.")
    return "\n".join(out)[:1950]


async def _agency_lookup(bot, name: str) -> str:
    """"agency Heath McGalliard" - what #lead-lab holds for them, and the
    agency a card of theirs would get."""
    name = " ".join(str(name or "").split())
    if not name:
        return "Who? `@Ryte agency <their name>`"
    posts = await _lead_requests(bot, wait=90)
    channel = _lead_lab_channel(bot)
    where = f"#{getattr(channel, 'name', 'lead-lab')} ({getattr(channel, 'id', '?')})" if channel else "#lead-lab"
    if not posts:
        return (f"{_LEAD_TROUBLE[0] or 'I can’t read #lead-lab'} — check I can see it (View Channel and Read "
                "Message History) and that DISCORD_LEAD_REQUEST_CHANNEL_ID is right.")
    with_agency = sum(1 for one in posts if jobs.request_agency(one))
    wanted = name.casefold()
    naming = [one for one in posts if wanted in " ".join(one.split()).casefold()]
    used = jobs.lead_request_for(posts, f"NAME: {name}")
    lines = [f"🔎 **{name}** in {where} — {len(posts)} posts and threads read, {with_agency} with an Agency:"]
    if _LEAD_TROUBLE[0]:
        lines.append(f"⚠ {_LEAD_TROUBLE[0]}")
    if not naming:
        lines.append("Nothing there names them — no request to take an Agency from. If the team writes it "
                     "on its own line in the thread (\"AEP\"), I take it from there.")
        return "\n".join(lines)
    for one in naming[:3]:
        agency = jobs.request_agency(one)
        first = next((line for line in one.splitlines() if line.strip()), "")[:90]
        lines.append(f"• {first} — Agency: **{agency or '(blank)'}**")
    if len(naming) > 3:
        lines.append(f"…and {len(naming) - 3} more.")
    lines.append(f"A card for them gets **Agency: {jobs.request_agency(used)}**" if used else
                 "None of them has the Agency filled in, so the card's stays blank unless the notes name one.")
    return "\n".join(lines)


def _order_preview(card: dict) -> str:
    from .. import ordercards

    lines = [f"**{card['name']}**"]
    if card.get("due"):
        lines.append(f"Due: {card['due']}")
    lines.append("```\n" + (card["desc"] or "(no description)")[:1200] + "\n```")
    if card.get("missing"):
        lines.append("-# Not in the order yet: " + ", ".join(card["missing"][:8]))
    return "\n".join(lines)


#: How far back a start looks for gavels it missed.
GAVEL_CATCH_UP = timedelta(hours=24)
#: The furthest back a start looks, however long RYTE was away.
GAVEL_CATCH_UP_MOST = timedelta(days=4)
#: Messages read back per channel - all of a busy day and a half.
GAVEL_CATCH_UP_READ = 5000
def _last_alive(bot) -> datetime | None:
    """When RYTE was last running before this start - the down alarm's own
    record, read once, before this start's check-in writes over it."""
    from .. import alive

    held = getattr(bot, "_was_alive", "unread")
    if held != "unread":
        return held
    try:
        when = alive.last_seen()
    except Exception:
        when = None
    try:
        bot._was_alive = when
    except Exception:
        pass
    return when
#: "now i just want him to take care those going forward" - nothing gavelled
#: before the gavel went live (6pm Eastern, 10/13) is caught up.
GAVELS_FROM = datetime(2026, 10, 13, 22, 0, tzinfo=timezone.utc)


async def _catch_up_gavels(bot, *, now: datetime | None = None) -> list[str]:
    """Gavels dropped while RYTE was down - an update, the Mac asleep - found
    by looking through the Growth server's last day: every channel and thread
    with anything said in it since. Each one is done as if just dropped,
    unless its card is made or it was left. The messages done."""
    from .. import ordercards

    await asyncio.sleep(5)
    guild = _setup_guild(bot, bot.config)
    if guild is None:
        return []
    now = now or datetime.now(timezone.utc)
    # Back to when RYTE was last running, not just a day - "ryte will be gone
    # for 1 and a half days". Never more than GAVEL_CATCH_UP_MOST.
    away = _last_alive(bot)
    since = now - GAVEL_CATCH_UP
    if away is not None:
        since = min(since, away - timedelta(minutes=30))
    since = max(since, now - GAVEL_CATCH_UP_MOST, GAVELS_FROM)
    places = list(getattr(guild, "text_channels", None) or [])
    try:
        places += list(await guild.active_threads())
    except Exception:
        log.warning("Couldn't list the open threads for gavels", exc_info=True)
    # The busiest first - #new-payment, where the gavels are, isn't kept
    # waiting behind a hundred quiet channels.
    places.sort(key=lambda one: int(getattr(one, "last_message_id", None) or 0), reverse=True)
    done = []
    for place in places:
        last = getattr(place, "last_message_id", None)
        if last is None:
            continue
        try:
            if discord.utils.snowflake_time(int(last)) < since:
                continue
        except Exception:
            pass
        try:
            async for message in place.history(after=since, limit=GAVEL_CATCH_UP_READ):
                # An aged payment with no card - posted while RYTE was down,
                # or its two minutes cut short by a restart.
                if (AGED_FROM_INVOICE and getattr(message.author, "bot", False)
                        and message.created_at >= AGED_AUTO_FROM and _aged_payment(message)
                        and str(message.id) not in _ORDER_ACTIVE
                        and not (await asyncio.to_thread(ordercards.load)).get(str(message.id))):
                    waited = (now - message.created_at).total_seconds()
                    _also_running(asyncio.get_running_loop().create_task(_aged_from_invoice(
                        bot, message, wait=max(0.0, AGED_INVOICE_WAIT_SECONDS - waited), catching_up=True)))
                    done.append(str(message.id))
                    continue
                for reaction in getattr(message, "reactions", None) or []:
                    if not ordercards.is_gavel(reaction.emoji):
                        continue
                    held = (await asyncio.to_thread(ordercards.load)).get(str(message.id)) or {}
                    if held.get("status") in ("made", "left", "held") or str(message.id) in _ORDER_ACTIVE:
                        break
                    async for person in reaction.users():
                        if not getattr(person, "bot", False) and (
                                GAVEL_EVERYONE or ordercards.may_gavel(person.id, bot.config)):
                            log.info("Catching up a gavel on %s", message.id)
                            done.append(str(message.id))
                            await _order_gavelled(bot, SimpleNamespace(
                                message_id=message.id, channel_id=place.id, user_id=person.id,
                                guild_id=guild.id, member=person, catching_up=True))
                            break
                    break
        except Exception:
            continue
    return done


#: "let everyone's gavel count, not just mine" - anybody's gavel in the
#: Growth server, where it's only the team. Elsewhere - Agent Lead Lab
#: Clients, where the emoji is from - only the people allowed.
GAVEL_EVERYONE = True


def _gavel_counts(bot, payload) -> bool:
    """Whether this gavel makes a card: a person's in the Growth server, or
    anywhere from somebody allowed. Never a bot's."""
    from .. import ordercards

    member = getattr(payload, "member", None)
    if getattr(member, "bot", False):
        return False
    if ordercards.may_gavel(payload.user_id, bot.config):
        return True
    guild_id = getattr(payload, "guild_id", None)
    guild = bot.get_guild(guild_id) if guild_id and hasattr(bot, "get_guild") else None
    return GAVEL_EVERYONE and _in_setup_guild(bot, guild)


#: "can ryte access agents state for aged leads on summit pay invoice
#: internal note ... and ryte will just immediately create the card?" - an
#: aged payment posted in #new-payment whose Payra invoice has a note gets
#: its card at once, no thread or gavel.
AGED_FROM_INVOICE = True
#: How long after an aged payment's post its card is made - time for the
#: team to start the thread with the states. Notes after that update it.
AGED_INVOICE_WAIT_SECONDS = 120
#: How long after an aged payment the audit still counts its card as on its way.
AUDIT_GRACE = timedelta(minutes=10)
#: Aged payments posted from here on get their card by themselves - Jackson
#: Ruehl's at 12:05 the first. Earlier ones may have a card made by hand.
AGED_AUTO_FROM = datetime(2026, 10, 14, 16, 0, tzinfo=timezone.utc)


def _aged_payment(message) -> bool:
    """A Payra payment post for aged leads, in #new-payment."""
    if "newpayment" not in _plain_letters(getattr(getattr(message, "channel", None), "name", "")):
        return False
    said = _all_of_it(message)
    # Anywhere on the PRODUCT line: Nova's "Text-Verified Aged IUL Leads"
    # put the word second, and Azriel Deskin's aged payment got no card.
    return "new payment" in said.casefold() and bool(re.search(r"PRODUCT:[^\n]*\baged\b", said, re.IGNORECASE))


_PAID_NAME = re.compile(r"NAME:\**\s*([^\n]+)", re.IGNORECASE)
_PAID_EMAIL = re.compile(r"EMAIL:\**\s*([^\s]+@[^\s]+)", re.IGNORECASE)


async def _aged_from_invoice(bot, message, *, wait: float | None = None, catching_up: bool = False) -> str:
    """The aged order's card from its invoice note - or nothing, when the
    invoice has none (the thread and the gavel still work). What happened."""
    from .. import ordercards

    await asyncio.sleep(AGED_INVOICE_WAIT_SECONDS if wait is None else wait)
    key = str(message.id)
    if (ordercards.load().get(key) or {}).get("status") not in (None, "waiting") or key in _ORDER_ACTIVE:
        return "already"
    said = _all_of_it(message)
    name = (_PAID_NAME.search(said) or [None, ""])[1].strip(" *")
    email = (_PAID_EMAIL.search(said) or [None, ""])[1].strip(" *")
    amount = (_AMOUNT.search(said) or [None, ""])[1]
    product = (_PRODUCT.search(said) or [None, ""])[1].strip(" *")
    try:
        note = await _invoice_for(bot, key, name=name, email=email, amount=amount, product=product, said=said,
                                  paid_at=getattr(message, "created_at", None))
    except Exception:
        # Payra down is no reason for no card: made from the payment and the
        # thread, and said - a silent "failed" left nothing to see.
        log.warning("Couldn't look up %s's invoice note", name, exc_info=True)
        note = None
        responder = _order_responder(bot)
        if responder is not None:
            try:
                await responder.send(f"⚠ Couldn't read **{name or 'an aged order'}**'s Payra invoice note — making the "
                                     f"card from the payment and its thread. Check its states.", quiet=True)
            except Exception:
                pass
    # "aged leads should be automatic add with states, thread or no thread
    # while relying on invoice if it has no states/thread added": the thread
    # first, the invoice's note beside it, and the card made either way.
    log.info("Making %s's aged card%s", name, " with the invoice note" if note else "")
    await _order_gavelled(bot, SimpleNamespace(
        message_id=message.id, channel_id=message.channel.id, user_id=0,
        guild_id=getattr(getattr(message, "guild", None), "id", None), member=None,
        extra_notes=(f"Payra invoice note: {note}",) if note else (), aged_only=True,
        catching_up=catching_up))
    return "made"


def _order_responder(bot):
    """Where orders are said in Ryte The Goat: the board's channel, else
    #hub-agent-fulfillment. Never the Growth server - "why did this ping
    here": the board's channel is Growth's #trello-manager, and an order
    update sent there on its own showed up in front of the team. So its
    Ryte The Goat twin, straight."""
    for found in (_board_responder(bot), _hub_responder(bot)):
        if found is None:
            continue
        channel = getattr(found, "channel", None)
        if channel is None or not _in_setup_guild(bot, getattr(channel, "guild", None)):
            return found
        guild = getattr(getattr(channel, "guild", None), "id", None)
        twin = mirror.copy_of(getattr(channel, "id", None))
        twin = bot.get_channel(twin) if twin else None
        if twin is not None:
            return ChannelResponder(twin)
        log.warning("Channel %s is in the Growth server (%s) with no twin in Ryte The Goat", channel.id, guild)
    log.error("No board channel and no #hub-agent-fulfillment in Ryte The Goat to say orders in")
    return None


async def _order_ungavelled(bot, payload) -> None:
    """"i removed the emoji and will it do again?" - yes: the order's card is
    forgotten (the card itself stays on the board), and gavelling it again
    makes a new one."""
    from .. import ordercards

    key = str(payload.message_id)
    async with _ORDER_LOCK:
        held = await asyncio.to_thread(ordercards.load)
        order = held.pop(key, None)
        if not order:
            return
        # The card it made is remembered: putting the gavel back updates that
        # card rather than making a second - "why did you create two card for
        # diego".
        if order.get("status") == "made" and order.get("card_id"):
            held[key] = {**order, "status": "off"}
        await asyncio.to_thread(ordercards.save, held)
    responder = _order_responder(bot)
    if responder is not None and order.get("status") == "made" and order.get("merged"):
        await responder.send(f"🔨 Gavel taken off **{order.get('client') or order.get('name') or 'that order'}** — "
                             f"it was put with another card ({order.get('url', '')}), which stays as it is. Put the "
                             f"gavel back and I'll read the order afresh and make its own card.", quiet=True)
    elif responder is not None and order.get("status") == "made":
        await responder.send(f"🔨 Gavel taken off **{order.get('client') or order.get('name') or 'that order'}** — "
                             f"put it back and I'll update the card I made with what's in the thread now (delete "
                             f"the card first for a fresh one): {order.get('url', '')}", quiet=True)


async def _order_gavel_refused(bot, payload) -> None:
    """A gavel from somebody whose gavel doesn't make cards - said, so it
    isn't a mystery why nothing happened. Once per message."""
    from .. import ordercards

    key = f"refused:{payload.message_id}:{payload.user_id}"
    if key in _ORDER_ACTIVE:
        return
    _ORDER_ACTIVE.add(key)
    who = getattr(payload.member, "display_name", None) or str(payload.user_id)
    responder = _order_responder(bot)
    if responder is None:
        return
    allowed = ordercards.gavellers(bot.config)
    link = f"https://discord.com/channels/{payload.guild_id}/{payload.channel_id}/{payload.message_id}"
    await responder.send(
        f"🔨 {who} put the gavel on an order, but only "
        + (", ".join(f"<@{one}>" for one in allowed) if allowed else "nobody (DISCORD_NOTIFY_USER_ID isn't set)")
        + f"'s gavel makes cards — nothing made.\n<{link}>\n"
        f"-# To let them: `@Ryte gavel allow {payload.user_id}`. Or `@Ryte order <message link>` makes this one now.",
        quiet=True)


async def _order_gavelled(bot, payload) -> None:
    """🔨 on an order: the card, made - aged straight away, fresh on the
    Create button. Everything said in Ryte The Goat, nothing in Growth."""
    from .. import ordercards

    key = str(payload.message_id)
    responder = _order_responder(bot)
    off = (await asyncio.to_thread(ordercards.load)).get(key) or {}
    if off.get("status") == "off" and key not in _ORDER_ACTIVE and off.get("merged"):
        # Its card was somebody else's it was put with - "another payment for
        # this order", or one found after a restart - so the gavel put back
        # reads it afresh. Abdullah Bahrani's aged payment had been put with
        # his New Agent card, and putting the gavel back wrote the aged order
        # into that card's comments.
        await _order_forget_made(key)
        off = {}
    if off.get("status") == "off" and key not in _ORDER_ACTIVE:
        # The gavel put back on an order whose card is still on the board:
        # that card, brought up to date - not a second one.
        try:
            still = await asyncio.to_thread(jobs.order_card_open, bot.config, off["card_id"])
        except Exception:
            log.warning("Couldn't see whether %s's card is still there", off.get("client"), exc_info=True)
            still = True
        if still:
            await _order_note(key, status="made")
            done = await _order_refresh(bot, key, settle=0)
            await _order_mark(bot, payload.channel_id, key)
            if responder is not None:
                await responder.send(f"🔨 **{off.get('client') or off.get('name') or 'That order'}** already has its "
                                     f"card, so I've brought it up to date instead of making another"
                                     + (" — nothing new in the thread" if done == "same" else "")
                                     + f": {off.get('url', '')}", quiet=True)
            return
        await _order_forget_made(key)
    async with _ORDER_LOCK:
        held = await asyncio.to_thread(ordercards.load)
        if (held.get(key) or {}).get("status") == "made" or key in _ORDER_ACTIVE:
            return
        _ORDER_ACTIVE.add(key)
        # A waiting aged order keeps what it's waited for and whether that
        # was said - it's said once.
        before = held.get(key) if (held.get(key) or {}).get("status") == "waiting" else {}
        held[key] = {**before, "status": "reading", "channel_id": str(payload.channel_id), "by": str(payload.user_id)}
        await asyncio.to_thread(ordercards.save, held)
    try:
        await _order_make(bot, payload, key, responder)
    except Exception as exc:
        # Anything not already said: said, so a gavel never just does nothing.
        log.exception("The gavelled order broke")
        await _order_forget(key)
        if responder is not None:
            try:
                await responder.send(f"🔨 ⚠ Something broke making that order's card: {_readable(exc)}\n"
                                     f"-# Nothing was made. `@Ryte order <message link>` tries again.")
            except Exception:
                log.exception("...and couldn't say so")
    finally:
        _ORDER_ACTIVE.discard(key)
        _ORDER_ACKS.pop(key, None)


async def _order_make(bot, payload, key: str, responder) -> None:
    from .. import ordercards

    link = f"https://discord.com/channels/{getattr(payload, 'guild_id', '')}/{payload.channel_id}/{payload.message_id}"
    # Said at once, so a gavel is never followed by nothing while the order is
    # read - and turned into the card's link when it's made.
    ack = None
    if responder is not None:
        try:
            ack = await responder.send(f"🔨 Making the card for this order… <{link}>", quiet=True)
        except Exception:
            ack = None
    _ORDER_ACKS[key] = ack
    try:
        channel = bot.get_channel(payload.channel_id) or await bot.fetch_channel(payload.channel_id)
        message = await channel.fetch_message(payload.message_id)
        # "the information is on ... invoice": a gavelled order's Payra
        # invoice read with it, new agent or aged, as the invoice path does.
        if not getattr(payload, "extra_notes", ()):
            note = await _order_invoice_note(bot, message)
            if note:
                payload = _payload_copy(payload, extra_notes=(f"Payra invoice note: {note}",))
        card, examples, thread_id = await _order_read(bot, message, extra=getattr(payload, "extra_notes", ()))
    except Exception as exc:
        log.exception("Couldn't read the gavelled order")
        await _order_forget(key)
        again = "didn't answer" in str(exc) and not getattr(payload, "retried", False)
        if ack is not None and hasattr(ack, "delete"):
            try:
                await ack.delete()
            except Exception:
                pass
        if responder is not None:
            await responder.send(f"🔨 ⚠ Couldn't read that order: {_readable(exc)} <{link}>"
                                 + (f"\n-# Trying it again by myself in {ORDER_RETRY_SECONDS // 60} minutes."
                                    if again else ""))
        if again:
            _also_running(asyncio.get_running_loop().create_task(
                _order_again(bot, _payload_copy(payload, retried=True))))
        return
    await _order_same_states(bot, message, card)
    kind, link = card["kind"], str(getattr(message, "jump_url", "") or "")
    if getattr(payload, "aged_only", False) and (kind != "aged" or not AGED_ORDERS_ON):
        # Made from an invoice note with nobody's gavel: aged only, or nothing.
        await _order_forget(key)
        ack = _ORDER_ACKS.pop(key, None)
        if ack is not None and hasattr(ack, "delete"):
            try:
                await ack.delete()
            except Exception:
                pass
        return
    if kind == "aged" and not AGED_ORDERS_ON:
        await _order_forget(key)
        if responder is not None:
            await responder.send(f"🔨 {card['client'] or 'That order'} is aged leads — those aren't made into cards "
                                 f"for now, so I've left it.\n<{link}>", quiet=True)
        return

    if kind == "fresh" and FRESH_ASKS_FIRST:
        if responder is None:
            await _order_forget(key)
            return
        view = views.ConfirmView(requester_id=int(payload.user_id), timeout=ORDER_ASK_SECONDS,
                                 label="Create card", emoji="🔨")
        asking = await responder.send((f"🔨 **New Agent card** for {card['client'] or 'this order'} — goes in the Queue. "
                                       f"<{link}>\n" + _order_preview(card))[:1950], view=view)
        await _order_note(key, status="asked")
        await view.wait()
        if not view.confirmed:
            if getattr(view, "answered", False):
                # "Leave it": not asked again by the catch-up after a restart.
                # A fresh gavel on it still asks.
                await _order_note(key, status="left")
            else:
                await _order_forget(key)
            return
        # Read again: notes may have come in while it waited.
        try:
            card, examples, thread_id = await _order_read(bot, message, extra=getattr(payload, "extra_notes", ()))
            card["kind"] = "fresh"
        except Exception:
            log.warning("Couldn't read the order again; making it as shown", exc_info=True)
    # Caught up after time away: a card the team made by hand meanwhile is
    # the card - not a second one beside it.
    if getattr(payload, "catching_up", False) and card.get("client"):
        try:
            found = await asyncio.to_thread(partial(jobs.card_made_since, bot.config, card["client"],
                                                    since=message.created_at - timedelta(minutes=10), kind=kind))
        except Exception:
            log.warning("Couldn't look for a card made while I was away", exc_info=True)
            found = None
        if found is not None:
            await _order_note(key, status="made", kind=kind, card_id=found["id"],
                              url=str(found.get("shortUrl") or found.get("url") or ""), name=found.get("name"),
                              client=card["client"], merged=True, link=link,
                              message_channel_id=str(payload.channel_id))
            await _order_mark(bot, payload.channel_id, key)
            if responder is not None:
                await responder.send(f"🔨 **{card['client']}** — paid while I was away, and already has a card made "
                                     f"since, so I left it as it is: {found.get('shortUrl') or found.get('url')}",
                                     quiet=True)
            return
    # Aged leads and new-agent leads bought together: a card for each -
    # Patrick Moreno's four aged lines on one Aged Leads card, each new-agent
    # line on a New Agent card of its own.
    subs = jobs.split_order(card, today=_today(bot.config)) or jobs.agent_cards(card, today=_today(bot.config))
    if subs:
        if getattr(payload, "aged_only", False):
            # Made on its own from an invoice: new-agent cards wait for a gavel.
            await _order_forget(key)
            return
        await _order_make_split(bot, payload, key, responder, message, card, subs, link, thread_id)
        return
    # Another payment for an order that has its card already: on that card.
    if card.get("client") and not getattr(payload, "separate", False):
        try:
            existing = await asyncio.to_thread(partial(jobs.recent_card_for, bot.config, card["client"], kind=kind))
        except Exception:
            log.warning("Couldn't look for their card", exc_info=True)
            existing = None
        if existing is not None and (not _paid_together(existing, key) or _own_invoice(existing, key)):
            # "Always make seprate card for now, unless its paid at the same
            # time. if paid separately. create card"
            existing = None
        if existing is not None:
            # Their card, but is it this order? A different lead type or live
            # date is a new order of theirs, with a card of its own.
            try:
                same = await asyncio.to_thread(jobs.same_order, bot.config, existing, card)
            except Exception:
                log.warning("Couldn't read their card to compare", exc_info=True)
                same = True
            if same:
                await _order_onto_card(bot, payload, key, responder, message, card, existing, link)
                return
    # "Be Sure to not fail here": what was read, held up against the order.
    # Anything that doesn't add up and the card waits for a person.
    wrong, missing = ordercards.doubts(card, today=_today(bot.config))
    # An aged card goes out as soon as it's in Aged Leads Order Done - Matt
    # DeFuria's was made with no states and no lead count. Without those
    # (and a lead type) it isn't made: made on its own, it waits for them;
    # gavelled, it's held for a person.
    gaps = _aged_gaps(card) if kind == "aged" else []
    if gaps and not getattr(payload, "confirmed", False):
        if getattr(payload, "aged_only", False):
            await _order_wait_for_info(bot, payload, key, card, gaps, link, responder)
            return
        wrong = list(wrong) + [f"Not in the order: {', '.join(gaps)} — an aged card goes out as soon as it's made"]
    if wrong and not getattr(payload, "confirmed", False):
        # Waited on apart from the gavel, so gavelling it again once the
        # thread is fixed isn't taken for this one still going.
        await _order_forget(key)
        _also_running(asyncio.get_running_loop().create_task(
            _order_held(bot, _payload_copy(payload), key, responder, card, wrong, link)))
        return
    # "when they pay thru bank, dont add the card on the aged leads order
    # card immediately": captured isn't settled, and an aged order's leads go
    # out from Aged Leads Order Done. It waits in Franklin (Admin) instead.
    paid, looked = await _order_payment(bot, message)
    bank = jobs.payment_by_bank(paid)
    hold = bank and kind == "aged" and BANK_HOLD_AGED and not jobs.payment_settled(paid)
    try:
        made = await asyncio.to_thread(partial(jobs.order_card_make, bot.config, kind, card,
                                               list_name=PARKED_LIST if hold else ""))
    except Exception as exc:
        await _order_forget(key)
        if responder is not None:
            await responder.send(f"🔨 ⚠ Couldn't make the card for {card['client'] or 'that order'}: {_readable(exc)}")
        return
    if bank:
        try:
            await asyncio.to_thread(jobs.order_bank_comment, bot.config, made["id"], _bank_comment(paid, hold=hold))
        except Exception:
            log.warning("Couldn't say on the card that they paid by bank", exc_info=True)
    attached = []
    if card.get("shots"):
        try:
            attached = await asyncio.to_thread(jobs.order_card_attach, bot.config, made["id"], card["shots"])
        except Exception:
            log.warning("Couldn't put the screenshots on the card", exc_info=True)
    await _order_note(key, status="made", kind=kind, card_id=made["id"], url=made["url"], name=card["name"],
                      fields=card.get("fields") or {}, launch_iso=card.get("launch_iso") or "",
                      extra_notes=list(getattr(payload, "extra_notes", ()) or ()),
                      made_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                      written=card["desc"], client=card["client"], link=link, attached=attached,
                      thread_id=str(thread_id or ""), message_channel_id=str(payload.channel_id),
                      payment_id=str((paid or {}).get("id") or ""), bank=bank, bank_hold=hold)
    try:
        await _order_seen(bot, key)
    except Exception:
        log.warning("Couldn't see how far the order's thread goes", exc_info=True)
    await _order_mark(bot, payload.channel_id, key)
    if responder is not None:
        said = (f"🔨 ✅ **{ordercards.KINDS[kind]} card made** — {card['name']} · {made['list']}\n{made['url']}"
                + ("\n🏦 **Paid by bank — not settled yet.** The card waits in Franklin (Admin) with a comment; "
                   "I'll move it to Aged Leads Order Done once Payra shows it Settled." if hold else
                   "\n🏦 Paid by bank — captured, not settled yet. Said in a comment on the card." if bank else "")
                + ("\n⚠ Couldn't find this payment in Payra to tell bank from card — check how they paid before "
                   "their leads go out." if kind == "aged" and looked and paid is None else "")
                + ("\n⚠ Made past a check: " + "; ".join(wrong) if wrong else "")
                # No launch date and the agent pass can't put them on the
                # Ads and Ops checklists - Bram's card sat in Franklin (Admin).
                + ("\n⚠ **No launch date in the order** — the card won't go on the Ads and Ops checklists until "
                   "one's posted in the thread (or put on the card)."
                   if kind == "fresh" and not (card.get("fields") or {}).get("launch") else "")
                + (f"\n-# Not in the order yet: {', '.join(card['missing'][:8])}. I'll add them when they're "
                   f"posted in the thread." if card.get("missing") else "")
                # Why the Agency is blank, so it can be put right.
                + ("\n-# Agency blank — no New Lead Request in #lead-lab matches their email, phone or name, "
                   "and the notes don't name one." if "agency" in (card.get("fields") or {})
                   and not (card.get("fields") or {}).get("agency") and not card.get("lead_request") else "")
                + (f"\n⚠ **Agency blank** — their #lead-lab request says just “{card['agency_unclear']}”. "
                   f"Which agency is that? Put it on the card." if card.get("agency_unclear") else ""))
        ack = _ORDER_ACKS.pop(key, None)
        try:
            if ack is None or not hasattr(ack, "edit"):
                raise LookupError
            await ack.edit(content=said)
        except Exception:
            await responder.send(said, quiet=True)


def _order_payer(card: dict) -> str:
    """Who paid, for an order made into several agents' cards."""
    from .. import ordercards

    return ordercards.paid_by(str(card.get("source") or "")) if card.get("agents") else ""


async def _order_make_split(bot, payload, key, responder, message, card, subs, link, thread_id) -> None:
    """An order split into its cards - "yes build it that way": checked line
    by line, held for a person when a line doesn't add up, then made card by
    card, the aged one held in Franklin (Admin) when the bank hasn't settled."""
    wrong = jobs.split_doubts(subs)
    if wrong and not getattr(payload, "confirmed", False):
        preview = {"client": card.get("client"), "missing": [],
                   "name": f"{card.get('client') or 'This order'} — split into {len(subs)} cards",
                   "desc": "\n\n".join(f"── {one['name']} ──\n{one['desc']}" for one in subs)}
        await _order_forget(key)
        _also_running(asyncio.get_running_loop().create_task(
            _order_held(bot, _payload_copy(payload), key, responder, preview, wrong, link)))
        return
    paid, _looked = await _order_payment(bot, message)
    bank = jobs.payment_by_bank(paid)
    parts, held_back, trouble = [], False, []
    for one in subs:
        hold = bank and one["kind"] == "aged" and BANK_HOLD_AGED and not jobs.payment_settled(paid)
        try:
            made = await asyncio.to_thread(partial(jobs.order_card_make, bot.config, one["kind"], one,
                                                   list_name=PARKED_LIST if hold else ""))
        except Exception as exc:
            trouble.append(f"{one['name']} ({one['fields'].get('package')}): {_readable(exc)}")
            continue
        held_back = held_back or hold
        if bank:
            try:
                await asyncio.to_thread(jobs.order_bank_comment, bot.config, made["id"], _bank_comment(paid, hold=hold))
            except Exception:
                log.warning("Couldn't say on the card that they paid by bank", exc_info=True)
        attached = []
        if card.get("shots"):
            try:
                attached = await asyncio.to_thread(jobs.order_card_attach, bot.config, made["id"], card["shots"])
            except Exception:
                log.warning("Couldn't put the screenshots on the card", exc_info=True)
        parts.append({"kind": one["kind"], "card_id": made["id"], "url": made["url"], "name": one["name"],
                      "list": made["list"], "written": one["desc"], "fields": one["fields"],
                      "package": one["fields"].get("package", ""), "attached": attached, "hold": hold})
    if not parts:
        await _order_forget(key)
        if responder is not None:
            await responder.send("🔨 ⚠ Couldn't make the cards for " + (card.get("client") or "that order") + ": "
                                 + "; ".join(trouble))
        return
    first = parts[0]
    await _order_note(key, status="made", split=True, parts=parts, kind=first["kind"], card_id=first["card_id"],
                      url=first["url"], name=first["name"], fields=first["fields"], written=first["written"],
                      extra_notes=list(getattr(payload, "extra_notes", ()) or ()),
                      made_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), client=card.get("client"),
                      link=link, thread_id=str(thread_id or ""), message_channel_id=str(payload.channel_id),
                      payment_id=str((paid or {}).get("id") or ""), bank=bank,
                      bank_hold=bool(first.get("hold")))
    try:
        await _order_seen(bot, key)
    except Exception:
        log.warning("Couldn't see how far the order's thread goes", exc_info=True)
    await _order_mark(bot, payload.channel_id, key)
    if responder is None:
        return
    what = (f"paid once for {len(parts)} agents" if card.get("agents") and not jobs.split_order(card)
            else "buys aged leads and new-agent leads")
    said = (f"🔨 ✅ **{_order_payer(card) or card.get('client') or 'This order'}** {what} — "
            f"split into {len(parts)} cards:\n"
            + "\n".join(f"• {one['name']} · {one['package']} · {one['list']}\n  {one['url']}"
                        + ("" if one["kind"] == "aged" or one["fields"].get("launch") else
                           " — no launch date yet: it waits until one's posted")
                        + ("" if one["kind"] == "aged" or one["fields"].get("states") else " — states to come")
                        for one in parts)
            + ("\n🏦 **Paid by bank — not settled yet.** The aged card waits in Franklin (Admin)." if held_back else
               "\n🏦 Paid by bank — captured, not settled yet. Said in a comment on each card." if bank else "")
            + ("\n⚠ Made past a check: " + "; ".join(wrong) if wrong else "")
            + ("\n⚠ Not made: " + "; ".join(trouble) if trouble else ""))
    ack = _ORDER_ACKS.pop(key, None)
    try:
        if ack is None or not hasattr(ack, "edit"):
            raise LookupError
        await ack.edit(content=said[:1950])
    except Exception:
        await responder.send(said[:1950], quiet=True)


async def _order_refresh_split(bot, key: str, order: dict, card: dict) -> str:
    """New notes on a split order: each of its cards brought up to date -
    the 34 OTP Vets' card gets its states when they're posted."""
    subs = jobs.split_order(card, today=_today(bot.config)) or jobs.agent_cards(card, today=_today(bot.config))
    parts = [dict(one) for one in order.get("parts") or []]
    await _order_seen(bot, key)
    if not subs or [one["kind"] for one in subs] != [one["kind"] for one in parts]:
        lines = len(subs)
        if order.get("split_changed") != lines:
            await _order_note(key, split_changed=lines)
            responder = _order_responder(bot)
            if responder is not None:
                await responder.send(f"⚠ **{order.get('client') or 'A split order'}** — the new notes change the "
                                     f"order's lines, so its cards are left as they are. Check them: "
                                     + " · ".join(one.get("url", "") for one in parts))
        return "changed"
    updated = []
    for part, sub in zip(parts, subs):
        done, said = await asyncio.to_thread(partial(
            jobs.order_card_update, bot.config, part["card_id"], written=part.get("written", ""),
            desc=sub["desc"], name=sub["name"], commented=tuple(part.get("commented") or ())))
        if done == "updated":
            part.update(written=sub["desc"], fields=sub["fields"], package=sub["fields"].get("package", ""))
            updated.append(part)
        elif done == "commented":
            part["commented"] = list(part.get("commented") or []) + said
            updated.append(part)
    if not updated:
        return "same"
    await _order_note(key, parts=parts, written=parts[0].get("written", ""), fields=parts[0].get("fields") or {})
    responder = _order_responder(bot)
    if responder is not None:
        await responder.send(f"📝 {order.get('client') or 'A split order'} — updated from the new notes: "
                             + " · ".join(f"{one['package']} {one['url']}" for one in updated), quiet=True)
    return "updated"


async def _order_mark(bot, channel_id, message_id) -> bool:
    """The gavel on the payment once its card is made: "can it add an emoji
    to that payment that way i know? same emoji as leadlab_hammertime". RYTE's
    own reactions never count as a gavel. Whether it went on."""
    from .. import ordercards

    try:
        channel = bot.get_channel(int(channel_id)) or await bot.fetch_channel(int(channel_id))
        here = getattr(getattr(channel, "guild", None), "id", None)
        emojis = [one for one in getattr(bot, "emojis", None) or [] if ordercards.is_gavel(one)]
        emojis.sort(key=lambda one: getattr(getattr(one, "guild", None), "id", None) != here)
        if not emojis:
            log.warning("No :%s: emoji where RYTE can use it", ordercards.GAVEL)
            return False
        await channel.get_partial_message(int(message_id)).add_reaction(emojis[0])
        return True
    except Exception:
        log.warning("Couldn't put the gavel on the payment", exc_info=True)
        return False


#: "if its an aged leads, and they paid via bank, put the card on Franklin
#: Admin ... then ryte will move the card back to aged lead order done once
#: its settled". New agents aren't held: the launch date allows for it.
BANK_HOLD_AGED = True
PARKED_LIST = "Franklin (Admin)"
#: How often a held card's payment is looked at, and for how long.
BANK_CHECK_SECONDS = 10 * 60
BANK_WATCH_DAYS = 14


async def _order_same_states(bot, message, card: dict) -> None:
    """"if it said same states as last time, just put it as is and accept it
    as states" (Mohammad Kanan) - the line as the team wrote it, in States."""
    from .. import ordercards

    fields = card.get("fields") or {}
    states = str(fields.get("states") or "").strip()
    if states and ordercards.SAME_STATES.search(states) is None and re.search(r"\b[A-Z]{2}\b|\ball\b", states, re.IGNORECASE):
        return
    for line in str(card.get("source") or "").splitlines():
        line = re.sub(r"^\[\d\d/\d\d \d\d:\d\d[AP]M\][^:]*:\s*", "", line).strip()
        found = ordercards.SAME_STATES.search(line)
        if found:
            fields["states"] = line if len(line) <= 60 else found.group(0)
            card["fields"] = fields
            card["desc"] = jobs.order_desc(fields, card.get("kind") or "fresh")
            card["missing"] = [one for one in card.get("missing") or [] if "state" not in str(one).casefold()]
            return


def _aged_gaps(card: dict) -> list[str]:
    """What an aged order can't go out without, that it doesn't say."""
    fields = card.get("fields") or {}
    gaps = []
    if not str(fields.get("states") or "").strip():
        gaps.append("the states")
    if not re.search(r"\d", str(fields.get("leads") or "")):
        gaps.append("how many leads")
    if not str(fields.get("package") or "").strip():
        gaps.append("the lead type")
    return gaps


#: How long an aged order waits for its states and lead count to be posted.
AGED_WAIT_DAYS = 2
#: How often a waiting aged order's invoice is looked at again.
AGED_RETRY_EVERY = timedelta(minutes=30)


async def _order_wait_for_info(bot, payload, key, card, gaps, link, responder) -> None:
    """An aged order made on its own that doesn't say enough: no card, said
    once, and tried again when its thread or invoice says more."""
    from .. import ordercards

    before = (await asyncio.to_thread(ordercards.load)).get(key) or {}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    await _order_note(key, status="waiting", gaps=gaps, client=card.get("client") or "",
                      message_channel_id=str(payload.channel_id), link=link, tried_at=now,
                      waiting_since=before.get("waiting_since") or now, said=before.get("said") or False)
    ack = _ORDER_ACKS.pop(key, None)
    if before.get("said") or responder is None:
        if ack is not None and hasattr(ack, "delete"):
            try:
                await ack.delete()
            except Exception:
                pass
        return
    await _order_note(key, said=True)
    said = (f"⏳ **{card.get('client') or 'An aged order'}** — aged leads, but I can't find {' or '.join(gaps)} "
            f"in the payment, its Payra invoice note or a thread. **No card yet**: an aged card goes out as soon "
            f"as it's made. I'll make it when they're posted in a thread on the payment or put in the invoice "
            f"note — or gavel it once they're there.\n{link}")
    try:
        if ack is not None and hasattr(ack, "edit"):
            await ack.edit(content=said)
        else:
            await responder.send(said)
    except Exception:
        log.warning("Couldn't say the aged order is waiting", exc_info=True)


async def _order_retry(bot, key: str, *, settle: float = 30) -> str:
    """A waiting aged order tried again - a note in its thread, or the
    invoice looked at again."""
    from .. import ordercards

    await asyncio.sleep(settle)
    _ORDER_PENDING.pop(key, None)
    order = (await asyncio.to_thread(ordercards.load)).get(key) or {}
    if order.get("status") != "waiting":
        return "gone"
    try:
        channel = bot.get_channel(int(order["message_channel_id"])) or \
            await bot.fetch_channel(int(order["message_channel_id"]))
        message = await channel.fetch_message(int(key))
    except Exception:
        log.warning("Couldn't fetch the waiting aged order", exc_info=True)
        return "failed"
    await _order_note(key, tried_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    return await _aged_from_invoice(bot, message, wait=0)


#: Picking an invoice for a payment and writing down that it's taken.
_INVOICE_LOCK = asyncio.Lock()


async def _order_invoice_note(bot, message) -> str:
    """The Payra invoice's note behind a payment post, or ""."""
    said = _all_of_it(message)
    if "payra" not in said.casefold():
        return ""
    name = (_PAID_NAME.search(said) or [None, ""])[1].strip(" *")
    email = (_PAID_EMAIL.search(said) or [None, ""])[1].strip(" *")
    amount = (_AMOUNT.search(said) or [None, ""])[1]
    product = (_PRODUCT.search(said) or [None, ""])[1].strip(" *")
    try:
        return await _invoice_for(bot, str(getattr(message, "id", "") or ""), name=name, email=email,
                                  amount=amount, product=product, said=said,
                                  paid_at=getattr(message, "created_at", None))
    except Exception:
        log.warning("Couldn't look up %s's invoice note", name, exc_info=True)
        return ""


async def _invoice_for(bot, key: str, *, name: str, email: str, amount: str, product: str,
                       said: str = "", paid_at=None) -> str:
    """The note on the invoice behind this payment post - one no other
    payment has taken, written down as this one's. Raises when Payra can't be
    read.

    A post that names its invoice and carries its notes - as they all do from
    Oct 19 - needs nothing looked up: "" (the notes are in the post already,
    and read with it), and the invoice written down as this one's. One that
    names it with no notes on it gets that invoice, not the nearest match."""
    from .. import ordercards

    number, notes = ordercards.post_invoice(said)
    if number and notes:
        if key:
            await _order_note(key, invoice_id=number)
        return ""
    # One at a time, so two posts for one charge can't both take one invoice.
    async with _INVOICE_LOCK:
        held = await asyncio.to_thread(ordercards.load)
        taken = {str(one.get("invoice_id")) for other, one in held.items()
                 if other != key and isinstance(one, dict) and one.get("invoice_id")}
        note, invoice_id = await asyncio.to_thread(partial(
            jobs.payra_invoice_pick, bot.config, name=name, email=email, amount=amount, product=product,
            taken=taken, **({"number": number} if number else {}),
            **({"paid_at": paid_at} if paid_at is not None else {})))
        if invoice_id and key:
            await _order_note(key, invoice_id=invoice_id)
        return note


async def _order_payment(bot, message) -> tuple[dict | None, bool]:
    """(the Payra payment behind a payment post, whether there was one to
    look for). A Zelle post has none."""
    said = _all_of_it(message)
    if "payra" not in said.casefold():
        return None, False
    name = (_PAID_NAME.search(said) or [None, ""])[1].strip(" *")
    amount = (_AMOUNT.search(said) or [None, ""])[1]
    try:
        return await asyncio.to_thread(partial(jobs.payra_payment_for, bot.config, name=name, amount=amount)), True
    except Exception:
        log.warning("Couldn't find %s's payment in Payra", name, exc_info=True)
        return None, True


def _bank_comment(paid: dict, *, hold: bool) -> str:
    amount = paid.get("amount")
    money = f"${float(amount):,.2f} " if isinstance(amount, (int, float)) else ""
    status = str(paid.get("status") or "captured").lower()
    if hold:
        return (f"🏦 Paid via bank — {money}{status}, not settled yet. Waiting here until it settles: no leads "
                f"until then. Ryte moves this card to Aged Leads Order Done once Payra shows it Settled.")
    return f"🏦 Paid via bank — {money}{status}, not settled yet."


async def _bank_holds(bot, *, now: datetime | None = None) -> list[str]:
    """Aged cards held for a bank payment, looked at again: settled, moved to
    Aged Leads Order Done; failed, said. The orders settled or failed."""
    from .. import ordercards

    now = now or datetime.now(timezone.utc)
    held = await asyncio.to_thread(ordercards.load)
    done = []
    for key, order in held.items():
        if not isinstance(order, dict) or order.get("bank_hold") is not True or not order.get("payment_id"):
            continue
        try:
            made = datetime.fromisoformat(str(order.get("made_at") or ""))
        except ValueError:
            continue
        if now - made > timedelta(days=BANK_WATCH_DAYS):
            continue
        try:
            paid = await asyncio.to_thread(jobs.payra_payment_now, bot.config, order["payment_id"])
        except Exception:
            log.warning("Couldn't look at %s's bank payment", order.get("name"), exc_info=True)
            continue
        who = order.get("name") or "An aged order"
        responder = _order_responder(bot)
        if jobs.payment_settled(paid):
            try:
                where = await asyncio.to_thread(
                    jobs.order_bank_settled, bot.config, order["card_id"],
                    "✅ Bank payment settled — moved to Aged Leads Order Done.")
            except Exception as exc:
                log.warning("Couldn't move %s's card", who, exc_info=True)
                if responder is not None and not order.get("bank_said"):
                    await _order_note(key, bank_said=True)
                    await responder.send(f"🏦 ⚠ **{who}** — bank payment settled, but I couldn't move the card: "
                                         f"{_readable(exc)}\n{order.get('url', '')}")
                continue
            await _order_note(key, bank_hold=False, bank_settled=now.isoformat(timespec="seconds"))
            done.append(key)
            if responder is not None:
                await responder.send(
                    f"🏦 ✅ **{who}** — bank payment settled; "
                    + ("card moved to Aged Leads Order Done." if where == "moved" else
                       "the card had been moved out of Franklin (Admin) already, so I left it where it is.")
                    + f"\n{order.get('url', '')}", quiet=True)
        elif jobs.payment_failed(paid):
            status = str((paid or {}).get("status") or "failed")
            try:
                await asyncio.to_thread(jobs.order_bank_comment, bot.config, order["card_id"],
                                        f"⚠ Bank payment {status.lower()} — it won't settle. No leads.")
            except Exception:
                log.warning("Couldn't comment on %s's card", who, exc_info=True)
            await _order_note(key, bank_hold="failed")
            done.append(key)
            if responder is not None:
                await responder.send(f"🏦 ⚠ **{who}**'s bank payment is **{status}** — the card stays in "
                                     f"Franklin (Admin), no leads.\n{order.get('url', '')}")
    return done


async def bank_hold_loop(bot: "WilByteBot") -> None:
    await asyncio.sleep(90)
    while not bot.is_closed():
        try:
            await _bank_holds(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Checking the bank payments failed; trying again next time")
        await asyncio.sleep(BANK_CHECK_SECONDS)


#: Payments this close together are one order paid in parts (Bram's two
#: $372.60s five minutes apart); further apart, each gets a card.
PAID_TOGETHER = timedelta(minutes=15)


def _paid_together(existing: dict, key: str) -> bool:
    """Whether this payment came at the same time as one RYTE already put on
    `existing` - within PAID_TOGETHER of it, by the posts' own times. A card
    RYTE didn't make from a payment isn't one to put another on."""
    from .. import ordercards

    try:
        this = discord.utils.snowflake_time(int(key))
    except (TypeError, ValueError):
        return False
    for other, order in ordercards.load().items():
        if other == key or not isinstance(order, dict) or order.get("card_id") != existing.get("id"):
            continue
        try:
            then = discord.utils.snowflake_time(int(other))
        except (TypeError, ValueError):
            continue
        if abs(this - then) <= PAID_TOGETHER:
            return True
    return False


def _own_invoice(existing: dict, key: str) -> bool:
    """Whether this payment is for an invoice of its own, not the one on
    `existing` - "for ahmed, two cards": INV-20039 and INV-20040 paid in one
    charge are two cards, paid together or not."""
    from .. import ordercards

    held = ordercards.load()
    mine = str((held.get(key) or {}).get("invoice_id") or "")
    if not mine:
        return False
    theirs = {str(order.get("invoice_id") or "") for other, order in held.items()
              if other != key and isinstance(order, dict) and order.get("card_id") == existing.get("id")}
    theirs.discard("")
    return bool(theirs) and mine not in theirs


#: How long after Claude didn't answer an order it is read again, once.
ORDER_RETRY_SECONDS = 120


async def _order_again(bot, payload, *, wait: float | None = None) -> None:
    """A gavelled order Claude didn't answer for, tried once more - unless a
    person has made it since."""
    await asyncio.sleep(ORDER_RETRY_SECONDS if wait is None else wait)
    try:
        await _order_gavelled(bot, payload)
    except Exception:
        log.exception("Trying the order again broke")


def _payload_copy(payload, **changed):
    """A gavel's particulars, kept - Discord's own event can't be copied."""
    kept = {name: getattr(payload, name, None) for name in (
        "message_id", "channel_id", "user_id", "guild_id", "member")}
    for name in ("extra_notes", "aged_only", "separate", "confirmed", "catching_up", "retried"):
        if hasattr(payload, name):
            kept[name] = getattr(payload, name)
    kept.update(changed)
    return SimpleNamespace(**kept)


#: Held orders whose Create anyway button RYTE is still waiting on itself.
_HELD_LIVE: set[str] = set()


async def _held_pressed(bot, interaction, key: str, act: str) -> None:
    """Create anyway / Leave it on a held order, pressed after a restart -
    the order made from what was written down when it was held."""
    from .. import ordercards

    if key in _HELD_LIVE:
        return  # the button it was sent with is still waiting, and answers
    held = (await asyncio.to_thread(ordercards.load)).get(key) or {}
    if act == "no" or held.get("status") != "held" or not held.get("message_channel_id"):
        said = ("✖ Left alone — nothing changed." if act == "no" else
                "Already made." if held.get("status") == "made" else "That order isn't waiting any more.")
        try:
            await interaction.response.edit_message(content=f"{interaction.message.content}\n{said}"[:1990], view=None)
        except Exception:
            log.warning("Couldn't answer the held button", exc_info=True)
        return
    try:
        await interaction.response.edit_message(content=f"{interaction.message.content}\n✅ Working on it…"[:1990],
                                                view=None)
    except Exception:
        log.warning("Couldn't answer the held button", exc_info=True)
    payload = SimpleNamespace(
        message_id=int(key), channel_id=int(held["message_channel_id"]), user_id=interaction.user.id,
        guild_id=int(held["guild_id"]) if str(held.get("guild_id") or "").isdigit() else None, member=None,
        confirmed=True, extra_notes=tuple(held.get("held_extra") or ()),
        **({"aged_only": True} if held.get("held_aged_only") else {}))
    async with _ORDER_LOCK:
        if key in _ORDER_ACTIVE or ((await asyncio.to_thread(ordercards.load)).get(key) or {}).get("status") == "made":
            return
        _ORDER_ACTIVE.add(key)
    try:
        await _order_make(bot, payload, key, _order_responder(bot))
    except Exception as exc:
        log.exception("Making the held order broke")
        responder = _order_responder(bot)
        if responder is not None:
            await responder.send(f"🔨 ⚠ Couldn't make that order: {_readable(exc)}")
    finally:
        _ORDER_ACTIVE.discard(key)


async def _order_held(bot, payload, key, responder, card, wrong, link) -> None:
    """A card that would go wrong, not made: what it would be, what doesn't
    add up, and a button for a person who has looked."""
    from .. import ordercards

    if responder is None:
        return
    said = (f"🔨 ⚠ **Not made yet — {card.get('client') or 'this order'}**: what I read doesn't add up.\n"
            + "\n".join(f"• {one}" for one in wrong)
            + f"\n<{link}>\n" + _order_preview(card)
            + "\n-# Fix it in the thread and gavel it again, or press Create anyway if the card is right.")
    view = views.ConfirmView(requester_id=None, timeout=24 * 60 * 60, label="Create anyway", emoji="⚠️",
                             ids=(f"ryte:held:{key}:go", f"ryte:held:{key}:no"))
    ack = _ORDER_ACKS.pop(key, None)
    try:
        if ack is None or not hasattr(ack, "edit"):
            raise LookupError
        await ack.edit(content=said[:1950], view=view)
    except Exception:
        await responder.send(said[:1950], view=view)
    # Remembered as held, so a restart's catch-up doesn't hold it again - it
    # said "Not made yet" twice for Mohammad Kanan. A fresh gavel still reads it.
    # With what it takes to make it after a restart, from the button alone.
    await _order_note(key, status="held", message_channel_id=str(payload.channel_id),
                      guild_id=str(getattr(payload, "guild_id", "") or ""),
                      held_extra=list(getattr(payload, "extra_notes", ()) or ()),
                      held_aged_only=bool(getattr(payload, "aged_only", False)))
    _HELD_LIVE.add(key)
    try:
        await view.wait()
    finally:
        _HELD_LIVE.discard(key)
    if not view.confirmed:
        return
    async with _ORDER_LOCK:
        if key in _ORDER_ACTIVE or ((await asyncio.to_thread(ordercards.load)).get(key) or {}).get("status") == "made":
            return
        _ORDER_ACTIVE.add(key)
    try:
        await _order_make(bot, _payload_copy(payload, confirmed=True), key, responder)
    finally:
        _ORDER_ACTIVE.discard(key)


_AMOUNT = re.compile(r"AMOUNT:\**\s*\$?\s*([\d,]+(?:\.\d+)?)", re.IGNORECASE)
_PRODUCT = re.compile(r"PRODUCT:\**\s*([^\n]*)", re.IGNORECASE)


async def _order_onto_card(bot, payload, key, responder, message, card, existing, link) -> None:
    """"bram paid alot of time for the same order" - another payment from a
    client whose New Agent card is from this week goes on that card as a
    comment, not a second card. A button makes one anyway, for an order that
    really is new."""
    paid = _all_of_it(message)
    amount = _AMOUNT.search(paid)
    product = _PRODUCT.search(paid)
    line = ("💰 Another payment for this order: "
            + (f"${amount.group(1)}" if amount else "amount not given")
            + (f" · {product.group(1).strip()}" if product and product.group(1).strip() else "")
            + f"\n{link}")
    await asyncio.to_thread(jobs.order_payment_comment, bot.config, existing["id"], line)
    await _order_note(key, status="made", kind=card.get("kind", "fresh"), card_id=existing["id"], url=existing["url"],
                      name=existing["name"], client=card.get("client", ""), merged=True, link=link,
                      message_channel_id=str(payload.channel_id))
    await _order_mark(bot, payload.channel_id, key)
    if responder is None:
        return
    when = "today" if card.get("kind") == "aged" else "this week"
    said = (f"🔨 **{card.get('client')}** already has a card from {when} — this payment went on it as a comment, "
            f"no second card.\n{existing['url']}")
    ack = _ORDER_ACKS.pop(key, None)
    view = views.ConfirmView(requester_id=None, timeout=12 * 60 * 60, label="Make a separate card", emoji="🔨")
    try:
        if ack is None or not hasattr(ack, "edit"):
            raise LookupError
        await ack.edit(content=said, view=view)
    except Exception:
        await responder.send(said, view=view, quiet=True)
    await view.wait()
    if not view.confirmed:
        return
    # A new order after all: forgotten as merged, and made as a card of its own.
    await _order_forget_made(key)
    await _order_make(bot, SimpleNamespace(
        message_id=payload.message_id, channel_id=payload.channel_id, user_id=payload.user_id,
        guild_id=getattr(payload, "guild_id", None), member=getattr(payload, "member", None), separate=True),
        key, responder)


async def _order_forget_made(key: str) -> None:
    from .. import ordercards

    async with _ORDER_LOCK:
        held = await asyncio.to_thread(ordercards.load)
        held.pop(key, None)
        await asyncio.to_thread(ordercards.save, held)


async def _order_note(key: str, **fields) -> None:
    from .. import ordercards

    async with _ORDER_LOCK:
        held = await asyncio.to_thread(ordercards.load)
        held.setdefault(key, {}).update(fields)
        await asyncio.to_thread(ordercards.save, held)


async def _order_forget(key: str) -> None:
    """Not made: gavelling it again starts over."""
    from .. import ordercards

    async with _ORDER_LOCK:
        held = await asyncio.to_thread(ordercards.load)
        if (held.get(key) or {}).get("status") != "made":
            held.pop(key, None)
            await asyncio.to_thread(ordercards.save, held)


def _order_note_posted(bot, message) -> None:
    """A note in a made order's thread: its card updated once they settle."""
    from .. import ordercards

    try:
        held = ordercards.load()
    except Exception:
        return
    where = str(getattr(message.channel, "id", ""))
    _NO_THREAD.pop(where, None)
    linked = any(isinstance(order, dict) and where in (order.get("thread_id"), key, *(order.get("threads") or ()))
                 for key, order in held.items())
    if not linked and getattr(message.channel, "parent", None) is not None and getattr(bot, "loop", None):
        # A new thread about an agent whose card is made - not on the payment
        # post: Ava's and Zach's. Theirs by the thread's name.
        if _order_named_thread(held, str(getattr(message.channel, "name", "") or "")) is not None:
            _also_running(bot.loop.create_task(_order_link_thread(bot, message.channel)))
        return
    for key, order in held.items():
        # A thread started from the post has the post's own id - so one
        # started after the card was made is still this order's.
        if (order.get("status") == "made" and not order.get("merged")
                and where and where in (order.get("thread_id"), key, *(order.get("threads") or ()))):
            waiting = _ORDER_PENDING.pop(key, None)
            if waiting is not None:
                waiting.cancel()
            _ORDER_PENDING[key] = bot.loop.create_task(_order_refresh(bot, key))
        elif order.get("status") == "waiting" and where and where in (order.get("thread_id"), key):
            waiting = _ORDER_PENDING.pop(key, None)
            if waiting is not None:
                waiting.cancel()
            _ORDER_PENDING[key] = bot.loop.create_task(_order_retry(bot, key))


#: {channel id: when Discord last said there's no such thread} - most aged
#: orders have none, and asking about each every five minutes is a lot of
#: asking. Asked again after half an hour.
_NO_THREAD: dict = {}
NO_THREAD_RECHECK = timedelta(minutes=30)


#: How recent a card has to be for a new thread with its agent's name to be
#: taken as about it.
THREAD_LINK_DAYS = 14


async def _order_link_thread(bot, thread) -> str | None:
    """A thread named for an agent with a card, taken as about that card and
    read onto it - unless it hangs off a payment post: that's a payment of
    its own, a new order maybe, not a note on the old one."""
    from .. import ordercards

    try:
        starter = await thread.parent.fetch_message(thread.id)
    except Exception:
        starter = None
    if starter is not None and getattr(starter.author, "bot", False) and \
            "new payment" in _all_of_it(starter).casefold():
        return None
    async with _ORDER_LOCK:
        held = await asyncio.to_thread(ordercards.load)
        key = _order_named_thread(held, str(getattr(thread, "name", "") or ""))
        if key is None or str(thread.id) in (held[key].get("threads") or []):
            return key
        held[key]["threads"] = list(held[key].get("threads") or []) + [str(thread.id)]
        await asyncio.to_thread(ordercards.save, held)
    order = held[key]
    responder = _order_responder(bot)
    if responder is not None:
        try:
            await responder.send(f"🧵 New thread “{thread.name}” — it's about **{order.get('client')}**, so it's "
                                 f"read onto their card from now on.\n{order.get('url', '')}", quiet=True)
        except Exception:
            pass
    waiting = _ORDER_PENDING.pop(key, None)
    if waiting is not None:
        waiting.cancel()
    _ORDER_PENDING[key] = bot.loop.create_task(_order_refresh(bot, key))
    return key


def _order_named_thread(held: dict, title: str) -> str | None:
    """The made order a new thread is about, by the agent's whole name in
    its title - their newest card when there's more than one."""
    from .. import clearout

    if not title.strip():
        return None
    now = datetime.now(timezone.utc)
    found = []
    for key, order in held.items():
        if not isinstance(order, dict) or order.get("status") != "made" or order.get("merged"):
            continue
        client = str(order.get("client") or "")
        if len(client.split()) < 2 or clearout.name_match(client, title) < 2:
            continue
        try:
            made = datetime.fromisoformat(str(order.get("made_at") or ""))
        except ValueError:
            continue
        if now - made <= timedelta(days=THREAD_LINK_DAYS):
            found.append((made, key))
    return max(found)[1] if found else None


async def _order_thread(bot, key: str, order: dict):
    """The thread under an order's post - its id is the post's - or None."""
    now = datetime.now(timezone.utc)
    for wanted in dict.fromkeys(one for one in (str(order.get("thread_id") or ""), key) if one):
        found = bot.get_channel(int(wanted))
        if found is None:
            if now - _NO_THREAD.get(wanted, now - NO_THREAD_RECHECK) < NO_THREAD_RECHECK:
                continue
            try:
                found = await bot.fetch_channel(int(wanted))
            except Exception:
                _NO_THREAD[wanted] = now
                found = None
        if found is not None and getattr(found, "parent", None) is not None:
            return found
    return None


async def _order_last(bot, key: str, order: dict) -> str:
    """The newest message in an order's threads - "" without one."""
    newest = 0
    for one in [None] + list(order.get("threads") or []):
        thread = await (_order_thread(bot, key, order) if one is None else _order_thread(bot, str(one), {}))
        last = getattr(thread, "last_message_id", None) if thread is not None else None
        newest = max(newest, int(last or 0))
    return str(newest or "")


async def _order_seen(bot, key: str) -> None:
    """How far into its thread an order's card has read - for the sweep."""
    from .. import ordercards

    last = await _order_last(bot, key, ordercards.load().get(key) or {})
    if last:
        await _order_note(key, seen_upto=last, failed_upto="", fails=0)


#: Tries at one note that won't go on a card before the sweep leaves it.
ORDER_SWEEP_TRIES = 3


#: How often order threads are looked through for notes a card missed.
ORDER_SWEEP_SECONDS = 5 * 60
#: How long an order's thread is watched after its card is made.
ORDER_SWEEP_DAYS = 7


async def _order_sweep(bot) -> list[str]:
    """Notes in an order's thread that its card hasn't read - the two minutes
    after Faith's note on Leo Fonseca were cut short by a restart, and nothing
    came back for it. Each order whose thread has a message past the last
    one read gets its card updated. The orders updated."""
    from .. import ordercards

    held = await asyncio.to_thread(ordercards.load)
    now = datetime.now(timezone.utc)
    due = []
    for key, order in held.items():
        if isinstance(order, dict) and order.get("status") == "waiting" and key not in _ORDER_PENDING:
            try:
                since = datetime.fromisoformat(str(order.get("waiting_since") or ""))
                tried = datetime.fromisoformat(str(order.get("tried_at") or ""))
            except ValueError:
                continue
            if now - since <= timedelta(days=AGED_WAIT_DAYS) and now - tried >= AGED_RETRY_EVERY:
                await _order_retry(bot, key, settle=0)
            continue
        if not isinstance(order, dict) or order.get("status") != "made" or order.get("merged"):
            continue
        try:
            made = datetime.fromisoformat(str(order.get("made_at") or ""))
        except ValueError:
            continue
        if now - made > timedelta(days=ORDER_SWEEP_DAYS) or key in _ORDER_PENDING:
            continue
        last = await _order_last(bot, key, order)
        if not last:
            continue
        if not order.get("seen_upto"):
            # Never looked at before this sweep began: read up to when its
            # card was made. Marking where the thread is now would swallow a
            # note lost to the very restart that brought the sweep in -
            # Louis Mordecai's 8am-8pm M-F.
            order["seen_upto"] = str(discord.utils.time_snowflake(made))
            await _order_note(key, seen_upto=order["seen_upto"])
        if order.get("failed_upto") == last and int(order.get("fails") or 0) >= ORDER_SWEEP_TRIES:
            continue
        if int(last) > int(order["seen_upto"]):
            due.append(key)
    for key in due:
        await _order_refresh(bot, key, settle=0)
    return due


async def order_sweep_loop(bot: "WilByteBot") -> None:
    await asyncio.sleep(60)
    while not bot.is_closed():
        try:
            await _order_sweep(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("The order thread sweep failed; trying again next time")
        await asyncio.sleep(ORDER_SWEEP_SECONDS)


def _order_named(held: dict, said: str) -> tuple[str, dict] | None:
    """The order "recheck Louis Mordecai" means: its link, or the newest
    made card with that name on it."""
    found = _MESSAGE_LINK.search(said or "")
    if found:
        key = found.group(3)
        return (key, held[key]) if key in held else None
    wanted = " ".join(re.sub(r"[^\w\s'-]", " ", str(said or "")).casefold().split())
    if not wanted:
        return None
    named = [(str(one.get("made_at") or ""), key, one) for key, one in held.items()
             if isinstance(one, dict) and one.get("status") == "made" and not one.get("merged")
             and wanted in " ".join(f"{one.get('client', '')} {one.get('name', '')}".casefold().split())]
    if not named:
        return None
    _when, key, one = max(named)
    return key, one


async def _order_update_now(bot, responder: Responder, said: str) -> None:
    """"@Ryte recheck Louis Mordecai" - the order's thread read onto its card now."""
    from .. import ordercards

    found = _order_named(ordercards.load(), said)
    if found is None or found[1].get("status") != "made":
        await responder.send(f"I haven't made a card for “{said}” — `@Ryte recheck <agent's name>` or "
                             f"`@Ryte recheck <the order's link>`.")
        return
    key, order = found
    await responder.send(f"📝 Reading {order.get('name') or 'the order'}'s thread again…")
    done = await _order_refresh(bot, key, settle=0)
    if done == "same":
        await responder.send(f"Nothing new for the card — it already says what the order says.\n{order.get('url', '')}")
    elif done == "failed":
        await responder.send("⚠ Couldn't update it — the reason's in #trello-manager.")


async def _order_refresh(bot, key: str, *, settle: float | None = None) -> str:
    """The card brought up to date with the order's notes."""
    from .. import ordercards

    await asyncio.sleep(ORDER_NOTES_SETTLE_SECONDS if settle is None else settle)
    _ORDER_PENDING.pop(key, None)
    order = (await asyncio.to_thread(ordercards.load)).get(key) or {}
    if order.get("status") != "made":
        return "gone"
    try:
        channel = bot.get_channel(int(order["message_channel_id"])) or await bot.fetch_channel(int(order["message_channel_id"]))
        message = await channel.fetch_message(int(key))
        since = None
        try:
            since = datetime.fromisoformat(order["made_at"]) if order.get("made_at") else None
        except ValueError:
            since = None
        extra = tuple(order.get("extra_notes") or ())
        if not extra and not order.get("invoice_looked"):
            # Made before the invoice was read for every order - Blaze
            # Lubbers - or before its note was written: read now.
            note = await _order_invoice_note(bot, message)
            # Looked once - not on every note in the thread, which is a lot of
            # asking Payra for an invoice that has nothing on it.
            await _order_note(key, invoice_looked=True)
            if note:
                extra = (f"Payra invoice note: {note}",)
                await _order_note(key, extra_notes=list(extra))
        card, _examples, _thread = await _order_read(bot, message, since=since, extra=extra,
                                                     threads=tuple(order.get("threads") or ()))
        # Notes about somebody else don't make the card theirs: Tony
        # Moderno's card was rewritten as Preston Bridges'.
        was, now = str(order.get("client") or ""), str(card.get("client") or "")
        if was and now and not ordercards.same_person(was, now):
            if order.get("other_person") != now:
                await _order_note(key, other_person=now)
                responder = _order_responder(bot)
                if responder is not None:
                    await responder.send(
                        f"⚠ **{order.get('name') or was}** — the new notes in its thread are about **{now}**, not "
                        f"{was}. The card's left as it is: check whether {was} paid for {now}, or the notes "
                        f"belong to another order.\n{order.get('url', '')}")
            await _order_seen(bot, key)
            return "other person"
        if order.get("split"):
            return await _order_refresh_split(bot, key, order, card)
        done, said = await asyncio.to_thread(partial(
            jobs.order_card_update, bot.config, order["card_id"], written=order.get("written", ""),
            desc=card["desc"], name=card["name"], commented=tuple(order.get("commented") or ())))
    except Exception as exc:
        log.exception("Couldn't update the order card")
        # Said, not only logged - "why isnt this being updated then?" - once
        # for each new note; the sweep tries it again quietly.
        try:
            last = await _order_last(bot, key, order)
        except Exception:
            last = ""
        fails = int(order.get("fails") or 0) + 1 if last and order.get("failed_upto") == last else 1
        await _order_note(key, failed_upto=last, fails=fails)
        responder = _order_responder(bot)
        if responder is not None and fails == 1:
            try:
                await responder.send(f"⚠ Couldn't update **{order.get('name') or 'an order card'}** from the new notes: "
                                     f"{_readable(exc)}\n{order.get('url', '')}\n-# `@Ryte recheck <agent's name>` "
                                     f"tries again.")
            except Exception:
                pass
        return "failed"
    await _order_seen(bot, key)
    # Read as a new agent when gavelled - a Zelle post says nothing of what
    # was bought - and the thread then says aged: Declan Connolly's card was
    # renamed Aged Leads but sat in In Que, and was parked in Franklin
    # (Admin) for having no launch date. Its list follows what it is.
    now_kind = card.get("kind") or order.get("kind")
    # Only when the payment itself doesn't say: a Payra post's PRODUCT of
    # "Aged Leads (VET)" is aged and "VET" isn't, whatever one reading of
    # the thread made of it - a misread mustn't carry a card off.
    paid_for = " ".join(((_PRODUCT.search(_all_of_it(message)) or [None, ""])[1]).split())
    settled_by_post = bool(paid_for) and not re.match(r"^[—\-–(]", paid_for)
    if settled_by_post:
        now_kind = order.get("kind")
    if now_kind in ("fresh", "aged") and now_kind != order.get("kind") and order.get("bank_hold") is not True:
        try:
            where = await asyncio.to_thread(jobs.order_card_move, bot.config, order["card_id"], now_kind)
            await _order_note(key, kind=now_kind)
            responder = _order_responder(bot)
            if responder is not None:
                await responder.send(f"🔀 **{card.get('name') or order.get('name')}** — the thread says it's "
                                     f"{'aged leads' if now_kind == 'aged' else 'a new agent'}, so the card's "
                                     f"moved to {where}.\n{order.get('url', '')}", quiet=True)
        except Exception:
            log.warning("Couldn't move the card to its list", exc_info=True)
    if card.get("shots"):
        try:
            more = await asyncio.to_thread(partial(jobs.order_card_attach, bot.config, order["card_id"], card["shots"],
                                                   done=tuple(order.get("attached") or ())))
        except Exception:
            log.warning("Couldn't put the new screenshots on the card", exc_info=True)
            more = []
        if more:
            await _order_note(key, attached=list(order.get("attached") or []) + more)
            if done == "same":
                done = "screenshots"
    if done == "updated":
        await _order_note(key, written=card["desc"], name=card["name"])
        wrong, _missing = ordercards.doubts(card, today=_today(bot.config))
        responder = _order_responder(bot)
        if wrong and responder is not None:
            await responder.send(f"⚠ **{order.get('name') or 'An order card'}** — updated from new notes, and something "
                                 f"doesn't add up: " + "; ".join(wrong) + f" — check the card.\n{order.get('url', '')}")
    elif done == "commented":
        await _order_note(key, commented=list(order.get("commented") or []) + said)
    if done != "same":
        responder = _order_responder(bot)
        if responder is not None:
            await responder.send(f"📝 {order.get('name') or 'An order card'} — "
                                 + ("updated from the new notes" if done == "updated" else
                                    "the new screenshots are on it" if done == "screenshots" else
                                    "the card was edited by hand, so the new notes went in a comment")
                                 + f"\n{order.get('url', '')}", quiet=True)
    return done


async def _order_from_link(bot, responder: Responder, said: str, author) -> None:
    """"@Ryte order <message link>" - the gavel, by asking: for an order
    gavelled while RYTE was down, or to try it on one."""
    from .. import ordercards

    found = _MESSAGE_LINK.search(said or "")
    if not found:
        await responder.send("Send the order's link: right-click the payment post → **Copy Message Link**, "
                             "then `@Ryte order <link>`.")
        return
    if not ordercards.may_gavel(getattr(author, "id", ""), bot.config):
        await responder.send("Only the gavel's owner can make order cards — see `@Ryte order check`.")
        return
    guild_id, channel_id, message_id = (int(one) for one in found.groups())
    made = ordercards.load().get(str(message_id)) or {}
    separate = bool(made.get("merged"))
    if made.get("status") == "made" and not separate:
        await responder.send(f"That one's card is already made — {made.get('url', '')}")
        return
    if separate:
        # Put on somebody's card as another payment - Ryan Leach's VET on his
        # IUL card. Asked for by its link: a card of its own.
        await _order_forget_made(str(message_id))
        await responder.send(f"🔨 That payment went on {made.get('url', 'a card')} as another payment — making it "
                             f"a card of its own.")
    else:
        await responder.send("🔨 On it — the card goes where gavelled orders go.")
    await _order_gavelled(bot, SimpleNamespace(message_id=message_id, channel_id=channel_id,
                                               user_id=author.id, guild_id=guild_id, member=author,
                                               separate=separate))


async def _gavel_allow(bot, responder: Responder, message, *, allowed: bool) -> None:
    """"@Ryte gavel allow @Kharyl" / "gavel remove @Kharyl" - only somebody
    whose own gavel counts can change who else's does."""
    from .. import ordercards

    author = getattr(message, "author", None)
    if not ordercards.may_gavel(getattr(author, "id", ""), bot.config):
        await responder.send("Only somebody whose gavel already counts can change that.")
        return
    me = str(getattr(bot.user, "id", ""))
    people = [one for one in getattr(message, "mentions", None) or [] if str(one.id) != me]
    said = str(getattr(message, "content", "") or "")
    # Somebody who isn't in this server can't be tagged here: their id (RYTE
    # gives it when their gavel doesn't count), or their name in Growth.
    for one in re.findall(r"(?<![<@!\d])(\d{15,21})(?!\d)", said):
        if one != me and one not in {str(p.id) for p in people}:
            people.append(SimpleNamespace(id=int(one), display_name=f"<@{one}>"))
    if not people:
        named = re.sub(r"<@[!&]?\d+>", " ", said)
        named = re.sub(r"(?i)^\s*(?:gavel\s+(?:allow|add|let|remove|stop|disallow|revoke)|let)\b|\bgavel\b", " ", named.strip())
        named = " ".join(named.split()).strip(" .!?")
        guild = _setup_guild(bot, bot.config)
        found = guild.get_member_named(named) if (guild is not None and named and hasattr(guild, "get_member_named")) else None
        if found is None and guild is not None and named:
            wanted = _plain_letters(named)
            found = next((one for one in getattr(guild, "members", None) or []
                          if wanted and _plain_letters(getattr(one, "display_name", "")).startswith(wanted)), None)
        if found is not None:
            people.append(found)
    if not people:
        await responder.send("Who? `@Ryte gavel allow @Kharyl` - or their id, which I give when their gavel "
                             "doesn't count.")
        return
    for one in people:
        await asyncio.to_thread(ordercards.allow_gaveller, one.id, allowed=allowed)
    names = ", ".join(getattr(one, "display_name", None) or str(one.id) for one in people)
    now = ", ".join(f"<@{one}>" for one in ordercards.gavellers(bot.config))
    await responder.send((f"🔨 {names}'s gavel makes cards now." if allowed else f"🔨 {names}'s gavel doesn't count any more.")
                         + f"\n-# Counts: {now or 'nobody'}", quiet=True)


async def _invoice_note(bot, responder: Responder, name: str) -> None:
    """"@Ryte invoice note <name>" - what RYTE reads as their invoice's note."""
    name = " ".join(str(name or "").split())
    if not name:
        await responder.send("Whose? `@Ryte invoice note Aiden Larsen`")
        return
    try:
        found = await asyncio.to_thread(jobs.payra_invoice_note_fields, bot.config, name)
    except Exception as exc:
        await responder.send(f"⚠ Couldn't read Payra: {_readable(exc)}")
        return
    if not found:
        await responder.send(f"No invoice for **{name}** in Payra this week.")
        return
    lines = [f"🧾 **{name}** — note fields on their invoices this week, as Payra has them:"]
    for label, fields in found:
        lines.append(f"**{label}**")
        lines += [f"• `{key}`: {text}" for key, text in fields] or ["• no note"]
    lines.append("-# An aged order's card is made from these. Something here on every invoice (set wording) "
                 "isn't a note - tell Claude which field is the real one.")
    await responder.send("\n".join(lines)[:1900])


async def _order_check(bot, responder: Responder) -> None:
    """"@Ryte order check" - everything the gavel needs, looked at."""
    from .. import ordercards

    lines = ["🔨 **Order cards — the gavel's setup**"]
    allowed = ordercards.gavellers(bot.config)
    if GAVEL_EVERYONE:
        lines.append("✅ Anyone's gavel counts in the Growth server")
    lines.append(("✅ " + ("Elsewhere, from " if GAVEL_EVERYONE else "Gavel counts from ")
                  + ", ".join(f"<@{one}>" for one in allowed)) if allowed else
                 "❌ Nobody's gavel counts — set DISCORD_NOTIFY_USER_ID (yours) or ORDER_GAVEL_USER_IDS in .env.")
    responder_there = _order_responder(bot)
    channel = getattr(responder_there, "channel", None)
    lines.append(f"✅ Said in #{getattr(channel, 'name', '?')}" if responder_there is not None else
                 "❌ No board channel to say orders in — DISCORD_BOARD_CHANNEL_ID.")
    kinds = ("fresh", "aged") if AGED_ORDERS_ON else ("fresh",)
    if not AGED_ORDERS_ON:
        lines.append("⏸ Aged Leads orders: off for now — gavelled ones are left alone.")
    lines.append("✅ Each card is new, no due date, described as: "
                 + " · ".join(label for _key, label in jobs.ORDER_FIELDS))
    try:
        lists = await asyncio.to_thread(_order_board_lists, bot.config)
        for kind in kinds:
            wanted = (bot.config.secrets.order_fresh_list if kind == "fresh" else bot.config.secrets.order_aged_list) or ""
            where = ordercards.list_for(kind, lists, wanted=wanted)
            lines.append(f"✅ {ordercards.KINDS[kind]} cards go in **{where['name']}**" if where else
                         f"❌ No list for {ordercards.KINDS[kind]} cards" + (f" called “{wanted}”" if wanted else ""))
    except Exception as exc:
        lines.append(f"❌ Couldn't read the board's lists: {_readable(exc)}")
    held = ordercards.load()
    made = [one for one in held.values() if isinstance(one, dict) and one.get("status") == "made"]
    lines.append(f"-# {len(made)} order card(s) made so far.")
    await responder.send("\n".join(lines)[:1900])


def _order_board_lists(config) -> list:
    client = jobs.open_trello(config)
    try:
        return client.board_lists(config.secrets.trello_board_id)
    finally:
        client.close()


def _orders_channel(guild):
    """#new-payment - where the payments are posted, a thread under each."""
    for channel in getattr(guild, "text_channels", None) or []:
        if "newpayment" in _plain_letters(getattr(channel, "name", "")):
            return channel
    return None


async def _payments_on(bot, day: date) -> list:
    """The day's Payra posts in #new-payment, oldest first."""
    guild = _setup_guild(bot, bot.config)
    channel = _orders_channel(guild) if guild is not None else None
    if channel is None:
        return []
    zone = ZoneInfo(bot.config.schedule.timezone)
    start = datetime(day.year, day.month, day.day, tzinfo=zone).astimezone(timezone.utc)
    found = []
    async for one in channel.history(after=start, before=start + timedelta(days=1), limit=400, oldest_first=True):
        text = _all_of_it(one)
        if getattr(one.author, "bot", False) and "new payment" in text.casefold() and "name:" in text.casefold():
            found.append(one)
    return found


#: A payment this old with no gavel is chased with the unticked reminder.
UNGAVELLED_AFTER = timedelta(hours=2)


async def _ungavelled_note(bot, *, now: datetime | None = None) -> str:
    """Today's payments nobody has gavelled and that have no card, two hours
    old or more - "paid but not gavelled yet", said with the unticked
    reminder. "" when there are none."""
    from .. import ordercards

    now = now or datetime.now(timezone.utc)
    zone = ZoneInfo(bot.config.schedule.timezone)
    posts = [one for one in await _payments_on(bot, _today(bot.config))
             if now - one.created_at >= UNGAVELLED_AFTER]
    if not posts:
        return ""
    since = datetime.now(zone).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=2)
    cards = await asyncio.to_thread(jobs.order_cards_since, bot.config, since)
    held = await asyncio.to_thread(ordercards.load)
    lines = []
    for post in posts:
        how, line = _audit_line(post, held, cards, zone)
        if how == "waiting" and "not gavelled yet" in line:
            lines.append(line.replace(" — not gavelled yet, no card", ""))
    if not lines:
        return ""
    shown = lines[:20]
    return ("🔨 **Paid, but nobody's gavelled them yet** (no card):\n" + "\n".join(shown)
            + (f"\n-# …and {len(lines) - len(shown)} more — `@Ryte order audit`." if len(lines) > len(shown) else ""))


def _audit_days(said: str, today: date) -> list[date]:
    """"order audit", "... yesterday", "... 3 days" - the days, oldest first."""
    said = str(said or "").casefold()
    if "yesterday" in said:
        return [today - timedelta(days=1)]
    many = re.search(r"(\d+)\s*days?", said)
    n = min(max(int(many.group(1)), 1), 7) if many else 1
    return [today - timedelta(days=back) for back in range(n - 1, -1, -1)]


def _audit_line(post, held: dict, cards: list, zone=timezone.utc) -> tuple[str, str]:
    """(how it stands - "ok", "check", "waiting", "missing" - the line for
    it). `cards`: the board's New Agent and Aged Leads cards, archived too."""
    from .. import clearout, ordercards

    said = _all_of_it(post)
    name = (_PAID_NAME.search(said) or [None, ""])[1].strip(" *") or "?"
    amount = (_AMOUNT.search(said) or [None, ""])[1]
    product = " ".join(((_PRODUCT.search(said) or [None, ""])[1]).split())
    aged = bool(re.search(r"\baged\b", product, re.IGNORECASE))
    zelle = "zelle" in said.casefold()
    bits = " · ".join(bit for bit in (f"${amount}" if amount else "", product or ("Zelle" if zelle else ""),
                                      f"{post.created_at.astimezone(zone):%-I:%M%p}".lower()) if bit)
    order = held.get(str(post.id)) or {}
    status = order.get("status")
    if status == "made" and order.get("merged"):
        return "ok", f"➕ **{name}** ({bits}) — on their card as another payment: <{order.get('url', '')}>"
    if status == "made":
        hold = " · 🏦 waiting on the bank" if order.get("bank_hold") is True else ""
        return "ok", f"✅ **{name}** ({bits}) — <{order.get('url', '')}>{hold}"
    if status == "off":
        return "check", f"🔨 **{name}** ({bits}) — gavel taken off; its card is still there: <{order.get('url', '')}>"
    if status == "left":
        return "ok", f"✋ **{name}** ({bits}) — somebody pressed Leave it: <{post.jump_url}>"
    if status == "asked":
        return "waiting", f"⏳ **{name}** ({bits}) — waiting on a button in Ryte The Goat: <{post.jump_url}>"
    if status == "waiting":
        return "waiting", (f"⏳ **{name}** ({bits}) — aged, no card until somebody posts "
                           f"{' and '.join(order.get('gaps') or ['the details'])}: <{post.jump_url}>")
    # A card for them on the board, archived or not, or one RYTE made for
    # another of their payments - Keegan Sundberg's $79.18 after his $1,240.
    found = jobs.has_new_agent_card(name, cards)
    if found is None:
        mine = [one for one in held.values() if isinstance(one, dict) and one.get("status") == "made"
                and clearout.name_match(name, str(one.get("client") or "")) >= 2]
        found = {"url": mine[-1].get("url", "")} if mine else None
    if found:
        put_away = " (archived)" if found.get("closed") else ""
        return "ok", f"✅ **{name}** ({bits}) — has a card{put_away}: <{found['url']}>"
    gavelled = any(ordercards.is_gavel(one.emoji) for one in getattr(post, "reactions", None) or [])
    if gavelled:
        return "missing", f"❌ **{name}** ({bits}) — gavelled, **no card**: <{post.jump_url}>"
    if aged and "payra" in said.casefold():
        if post.created_at < AGED_AUTO_FROM:
            return "check", (f"🕘 **{name}** ({bits}) — before I made aged cards on my own, and no card on the "
                             f"board: <{post.jump_url}>")
        # Minutes old: its card is made a couple of minutes after the payment
        # lands, once the invoice is read - Jaden Schomer's was "missing" at
        # 7:35 for a 7:34 payment.
        if datetime.now(timezone.utc) - post.created_at < AUDIT_GRACE:
            return "waiting", f"⏳ **{name}** ({bits}) — paid minutes ago, its card is on its way: <{post.jump_url}>"
        return "missing", f"❌ **{name}** ({bits}) — aged, **no card** (should have been made on its own): <{post.jump_url}>"
    # A weekly subscription's charge - eight came in at 10:07 on 10/15 - is
    # an agent already set up being billed again, not an order. A first
    # week ("First week of leads (starts ...)") still waits for a gavel.
    if re.search(r"\bsubscription\b", said, re.IGNORECASE) and not re.search(r"\bfirst\s+week\b", said, re.IGNORECASE):
        return "subs", f"🔁 **{name}** ({bits}): <{post.jump_url}>"
    return "waiting", f"⏳ **{name}** ({bits}) — not gavelled yet, no card: <{post.jump_url}>"


async def _order_audit(bot, responder: Responder, said: str) -> None:
    """"did ryte created all of the cards that he can create already? did
    he not miss anything?" - every payment posted in #new-payment, and its
    card: made, on a card already, left, or missing. Nothing read by Claude
    and nothing made - it's a list to look at."""
    from .. import ordercards

    days = _audit_days(said, _today(bot.config))
    await responder.send(f"🔎 Checking every payment in #new-payment "
                         f"{'today' if days == [_today(bot.config)] else 'from ' + f'{days[0]:%a %m/%d}'}…")
    posts = []
    for day in days:
        posts += await _payments_on(bot, day)
    if not posts:
        await responder.send("No payments in #new-payment for that - or I can't find the channel.")
        return
    zone = ZoneInfo(bot.config.schedule.timezone)
    since = datetime(days[0].year, days[0].month, days[0].day, tzinfo=zone) - timedelta(days=2)
    try:
        cards = await asyncio.to_thread(jobs.order_cards_since, bot.config, since)
    except Exception as exc:
        await responder.send(f"⚠ Couldn't read the board: {_readable(exc)}")
        return
    held = await asyncio.to_thread(ordercards.load)
    sorted_out = {"missing": [], "check": [], "waiting": [], "subs": [], "ok": []}
    for post in posts:
        how, line = _audit_line(post, held, cards, zone)
        sorted_out[how].append(line)
    head = (f"🔎 **{len(posts)} payment(s)** — {len(sorted_out['ok'])} with a card, "
            f"{len(sorted_out['waiting'])} waiting on a gavel or a button, **{len(sorted_out['missing'])} missing**"
            + (f", {len(sorted_out['check'])} to check by hand" if sorted_out["check"] else "")
            + (f", {len(sorted_out['subs'])} subscription charge(s)" if sorted_out["subs"] else "") + ".")
    lines = [head]
    for how, title in (("missing", "**Missing — should have a card:**"),
                       ("check", "**From before aged cards were automatic — no card found, check by hand:**"),
                       ("waiting", "**Waiting — nobody's gavelled these:**"),
                       ("subs", "**Subscription charges — weekly billing, no card unless somebody gavels one:**"),
                       ("ok", "**Done:**")):
        if sorted_out[how]:
            lines += ["", title] + sorted_out[how]
    if not sorted_out["missing"]:
        lines.insert(1, "✅ Nothing missed.")
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) > 1900:
            await responder.send(chunk, quiet=True)
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        await responder.send(chunk, quiet=True)


async def _order_backfill(bot, responder: Responder, author) -> None:
    """"remember not all agent was added as card today? can we backfill it" -
    today's new agent orders that have no card, read like a gavel and made
    on one button."""
    from .. import clearout, ordercards

    if not ordercards.may_gavel(getattr(author, "id", ""), bot.config):
        await responder.send("Only somebody whose gavel counts can make order cards.")
        return
    today = _today(bot.config)
    await responder.send("🔨 Reading today's payments in #new-payment…")
    posts = await _payments_on(bot, today)
    if not posts:
        await responder.send("No payments in #new-payment today - or I can't find that channel.")
        return
    held = await asyncio.to_thread(ordercards.load)
    posts = [one for one in posts if (held.get(str(one.id)) or {}).get("status") != "made"]
    gate = asyncio.Semaphore(4)

    async def reading(one):
        async with gate:
            try:
                card, _e, thread_id = await _order_read(bot, one)
                return one, card, thread_id, None
            except Exception as exc:
                return one, None, None, exc

    read = await asyncio.gather(*(reading(one) for one in posts))
    try:
        on_board = await asyncio.to_thread(jobs.new_agent_cards, bot.config)
    except Exception as exc:
        await responder.send(f"⚠ Couldn't read the board: {_readable(exc)}")
        return
    todo, skipped, seen = [], [], []
    for one, card, thread_id, problem in read:
        if card is None:
            skipped.append(f"⚠ couldn't read <{one.jump_url}>: {_readable(problem)}")
            continue
        who = card["client"] or "?"
        if card["kind"] == "aged":
            continue
        has = jobs.has_new_agent_card(who, on_board)
        if has:
            skipped.append(f"{who} — already has a card: {has['url']}")
            continue
        if any(clearout.name_match(who, other) >= 2 for other in seen):
            skipped.append(f"{who} — a second payment today; one card")
            continue
        wrong, _missing = ordercards.doubts(card, today=today)
        if wrong:
            skipped.append(f"⚠ {who} — not made, doesn't add up: " + "; ".join(wrong) + " — fix the thread and gavel it")
            continue
        seen.append(who)
        todo.append((one, card, thread_id))
    if not todo:
        await responder.send("✅ Every new agent paid today has a card."
                             + ("\n" + "\n".join(f"-# {line}" for line in skipped[:15]) if skipped else ""))
        return
    lines = [f"🔨 **{len(todo)} new agent(s) paid today with no card** — they'd go in the Queue:"]
    for _one, card, _t in todo:
        fields = card["fields"]
        lines.append(f"• **{card['client']}** — {fields.get('package') or '?'} · {fields.get('leads') or '?'} · "
                     f"launch {fields.get('launch') or '?'}")
    lines += [f"-# {line}" for line in skipped[:15]]
    view = views.ConfirmView(requester_id=getattr(author, "id", None), timeout=60 * 60,
                             label=f"Create {len(todo)} card(s)", emoji="🔨")
    await responder.send("\n".join(lines)[:1900], view=view)
    await view.wait()
    if not view.confirmed:
        return
    made_lines = []
    for one, card, thread_id in todo:
        key = str(one.id)
        try:
            made = await asyncio.to_thread(jobs.order_card_make, bot.config, "fresh", card)
            attached = []
            if card.get("shots"):
                try:
                    attached = await asyncio.to_thread(jobs.order_card_attach, bot.config, made["id"], card["shots"])
                except Exception:
                    log.warning("Couldn't put the screenshots on the card", exc_info=True)
            await _order_note(key, status="made", kind="fresh", card_id=made["id"], url=made["url"], name=card["name"],
                              fields=card.get("fields") or {}, launch_iso=card.get("launch_iso") or "",
                              made_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                              written=card["desc"], client=card["client"], link=str(one.jump_url), attached=attached,
                              thread_id=str(thread_id or ""), message_channel_id=str(one.channel.id))
            try:
                await _order_seen(bot, key)
            except Exception:
                log.warning("Couldn't see how far the order's thread goes", exc_info=True)
            await _order_mark(bot, one.channel.id, key)
            made_lines.append(f"✅ {card['name']} — {made['url']}")
        except Exception as exc:
            made_lines.append(f"⚠ {card['client']}: {_readable(exc)}")
    await responder.send("\n".join(made_lines)[:1900])


async def _live_list(bot, responder: Responder) -> None:
    """"give me the list of name going live today and tomorrow with their
    lead type and states" - the hub's dates and lead types; states from the
    hub, else the order's card; orders whose thread says so and that aren't
    on the hub yet, marked."""
    from .. import clearout, hub, ordercards

    today = _today(bot.config)
    days = (today, today + timedelta(days=1))
    agents, problem = [], ""
    if getattr(bot.config.secrets, "hub_api_token", None):
        try:
            agents = await asyncio.to_thread(hub.agents, bot.config.secrets, status="all")
        except hub.HubError as exc:
            problem = str(exc)
    orders = [one for one in (await asyncio.to_thread(ordercards.load)).values()
              if isinstance(one, dict) and one.get("status") == "made"]

    def order_of(name):
        return next((one for one in orders if clearout.name_match(name, one.get("client", "")) >= 2), None)

    def states_line(said):
        said = str(said or "").strip()
        codes = re.findall(r"\b[A-Z]{2}\b", said)
        if len(codes) > 8:
            return f"{', '.join(codes[:8])}… ({len(codes)} states)"
        return said or "states not given"

    lines = []
    for day, label in zip(days, ("Today", "Tomorrow")):
        rows = []
        for agent in sorted((one for one in agents if one.start_date == day), key=lambda one: one.name):
            order = order_of(agent.name)
            states = ", ".join(agent.states) if agent.states else ((order or {}).get("fields") or {}).get("states", "")
            rows.append(f"• **{agent.name}** — {day:%a %m/%d} · {agent.lead_type or '?'} · {states_line(states)}")
        for order in orders:
            if order.get("launch_iso") != day.isoformat():
                continue
            if any(clearout.name_match(order.get("client", ""), one.name) >= 2 for one in agents):
                continue
            fields = order.get("fields") or {}
            rows.append(f"• ⚠ **{order.get('client')}** — {day:%a %m/%d} · {fields.get('package') or '?'} · "
                        f"{states_line(fields.get('states'))} · *not on the hub yet* · {order.get('url', '')}")
        if lines:
            lines.append("")
        lines.append(f"__**{label.upper()} — {day:%A, %B %d}**__ ({len(rows)})")
        lines += rows or ["• nobody"]
    lines.append("")
    lines.append("-# Dates and lead types from the hub; states from the hub, else the order's card. "
                 "⚠ = on a card, not on the hub.")
    if problem:
        lines.append(f"-# ⚠ Couldn't read the hub: {problem}")
    await responder.send("\n".join(lines)[:3900])


async def _order_template_set(responder: Responder, kind: str, link: str, config) -> None:
    """"@Ryte new agent template <link>" / "aged template <link>"."""
    from .. import ordercards

    if not ordercards.card_id_of(link):
        await responder.send("Send the Trello card's link: `@Ryte new agent template https://trello.com/c/…`")
        return
    await asyncio.to_thread(ordercards.set_template, kind, link.strip("<> "))
    try:
        example = await asyncio.to_thread(jobs.order_template, config, kind)
    except Exception as exc:
        await responder.send(f"Saved, but I couldn't open it: {_readable(exc)}")
        return
    await responder.send(f"✅ {ordercards.KINDS[kind]} cards will be made like **{example['name']}** — "
                         f"its checklists, labels and members, and its description's layout.\n{example['url']}")


async def _order_templates_said(responder: Responder) -> None:
    from .. import ordercards

    held = await asyncio.to_thread(ordercards.templates)
    await responder.send("🔨 Order cards are copied from:\n" + "\n".join(
        f"• {ordercards.KINDS[kind]}: " + (held[kind] or "not set") for kind in ("fresh", "aged"))
        + "\n-# Change one with `@Ryte new agent template <link>` or `@Ryte aged template <link>`.")


async def setup_check_pass_loop(bot: "WilByteBot") -> None:
    """Every half hour: the hub's scheduled agents against their threads and
    payments, on the Ryte Checklist card - and at six, tomorrow's not-ready
    ones listed for Franklin, once."""
    from .. import setupcheck

    await asyncio.sleep(120)
    while not bot.is_closed():
        try:
            results, fresh, problems = await _setup_pass(bot)
            responder = _hub_responder(bot)
            if fresh and responder is not None:
                await responder.send(_setup_news(bot.config, fresh), quiet=True)
            for one in problems:
                log.warning("Ryte Checklist: %s", one)
            now = datetime.now(ZoneInfo(bot.config.schedule.timezone))
            today = _today(bot.config)
            said = await asyncio.to_thread(setupcheck.load, "said")
            try:
                await _team_tasks_catch_up(bot)
            except Exception:
                log.exception("Couldn't catch up on the team's requests")
            if (now.hour, now.minute) >= SETUP_EVENING and said.get("_evening") != today.isoformat():
                from .. import teamtasks

                tomorrow = today + timedelta(days=1)
                late = setupcheck.not_ready(results, tomorrow)
                waiting_on = teamtasks.overdue(await asyncio.to_thread(teamtasks.load), datetime.now(timezone.utc))
                parts = []
                if late:
                    parts.append(f"🌙 Going live tomorrow ({tomorrow:%a %m/%d}) and not all checked:\n"
                                 + "\n".join(f"• {setupcheck.item_title(one.agent.name, one.agent.lead_type, one.agent.start_date)}"
                                             f" — {', '.join(f.what for f in one.wrong)}" for one in late))
                if waiting_on:
                    parts.append("🕓 Asked of the team and not done yet:\n"
                                 + "\n".join(f"• {teamtasks.item_name(one)}" + (f" · <{one['link']}>" if one.get("link") else "")
                                             for one in waiting_on))
                if parts:
                    text = "\n\n".join(parts)
                    await asyncio.to_thread(jobs.ryte_card_note, bot.config, text)
                    if responder is not None:
                        ping = _unmarked_ping(bot.config)
                        await responder.send(((ping + " ") if ping else "") + text, quiet=True)
                said["_evening"] = today.isoformat()
                await asyncio.to_thread(setupcheck.save, "said", said)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("The setup check failed; trying again next time")
        await asyncio.sleep(CHECKLIST_EVERY_SECONDS)


async def _ryte_checklist(bot, responder: Responder) -> None:
    """"@Ryte checklist" - the pass, now, and what it found."""
    from .. import setupcheck

    await responder.send("🔎 Checking everybody scheduled on the hub against their threads and payments…")
    try:
        results, fresh, problems = await _setup_pass(bot)
    except Exception as exc:
        await responder.send(f"⚠ {_readable(exc)}")
        return
    if not results:
        await responder.send("\n".join([f"⚠ {one}" for one in problems] or ["Nobody scheduled on the hub right now."]))
        return
    lines = [f"📋 **{len(results)} on their way to going live** — the Ryte Checklist card is up to date:"]
    for one in results:
        title = setupcheck.item_title(one.agent.name, one.agent.lead_type, one.agent.start_date)
        lines.append(("✅ " if one.all_good else "⚠ ") + title
                     + ("" if one.all_good else " — " + "; ".join(f"{f.what}: {f.said}" for f in one.wrong)))
    lines += [f"⚠ {one}" for one in problems]
    seen = _SETUP_SEEN.get("threads") or []
    lines.append(f"-# Read {_SETUP_SEEN.get('posts', 0)} “setup is done” posts in #new-agent-setup and "
                 f"{len(seen)} threads and channels in {_SETUP_SEEN.get('server') or 'the Growth server'}; "
                 + (f"Zelle from #{_SETUP_SEEN['zelle']}." if _SETUP_SEEN.get("zelle") else
                    "no #new-payment channel yet, so Payra only.")
                 + (" No thread for somebody who has one? `@Ryte threads <name>`."
                    if any(f.what == "Thread" for one in results for f in one.wrong) else ""))
    await responder.send("\n".join(lines)[:3900])


async def _setup_threads_for(bot, responder: Responder, name: str) -> None:
    """"@Ryte threads Gavin" - the threads RYTE takes as theirs, and the ones
    with their name in that it doesn't."""
    from .. import setupcheck

    guild = _setup_guild(bot, bot.config)
    if guild is None:
        await responder.send("I can't see the Growth Team server - set DISCORD_SETUP_GUILD_ID in .env.")
        return
    everything = await _setup_threads(guild) + list(getattr(guild, "text_channels", None) or [])
    threads = _order_threads(guild, everything, today=_today(bot.config))
    older = [one for one in setupcheck.threads_for(name, everything) if one not in threads]
    mine = sorted(setupcheck.threads_for(name, threads),
                  key=lambda one: int(getattr(one, "last_message_id", None) or one.id), reverse=True)
    near = [one for one in setupcheck.near_threads(name, threads, most=8)
            if one not in {str(t.name) for t in mine}]
    lines = [f"🔎 **{name}** — out of {len(threads)} threads and channels in {guild.name}:"]
    lines += [("✅ " if at < 3 else "▫️ ") + str(one.name) + (f" · <{one.jump_url}>" if getattr(one, "jump_url", "") else "")
              for at, one in enumerate(mine)] or [
        "No thread I take as theirs."]
    if len(mine) > 3:
        lines.append("-# ✅ the three with the newest messages are read; ▫️ older ones aren't.")
    if older:
        lines.append(f"-# Left out — from before the move to Discord ({SETUP_MOVED_ON:%m/%d}) or started by a bot: "
                     + "; ".join(f"“{one.name}”" for one in older[:5]))
    if near:
        lines.append("Close, but not taken as theirs: " + "; ".join(f"“{one}”" for one in near))
        lines.append("-# A thread is theirs when its title has their whole name, or their first name "
                     "(and last initial) when only one thread fits.")
    await responder.send("\n".join(lines)[:3900])


#: What the last pass looked through, for its reply.
_SETUP_SEEN: dict = {}


def _new_agent_setup_channel(guild):
    """#new-agent-setup in the Growth server, where Nova Cloud posts each
    setup Therese finishes."""
    for channel in getattr(guild, "text_channels", None) or []:
        if "newagentsetup" in _plain_letters(getattr(channel, "name", "")):
            return channel
    return None


async def _setup_posts(channel, today: date) -> list:
    """Every "setup is done" there since the move to Discord."""
    from .. import setupcheck

    found = []
    try:
        async for one in channel.history(limit=500, after=_setup_since(today)):
            post = setupcheck.parse_setup_post(
                _all_of_it(one), today=today, link=str(getattr(one, "jump_url", "") or ""),
                when=one.created_at.astimezone(timezone.utc).isoformat(timespec="minutes"))
            if post is not None:
                found.append(post)
    except Exception:
        log.warning("Couldn't read #new-agent-setup", exc_info=True)
    return found


def _zelle_channel(bot):
    """#new-payment, where Zelle payments are posted - DISCORD_ZELLE_CHANNEL_ID,
    else found by name in any server RYTE is in. None until Tre sets it up."""
    wanted = (getattr(bot.config.secrets, "discord_zelle_channel_id", "") or "").strip()
    if wanted.isdigit():
        return bot.get_channel(int(wanted))
    for guild in getattr(bot, "guilds", None) or []:
        for channel in getattr(guild, "text_channels", None) or []:
            if "newpayment" in _plain_letters(getattr(channel, "name", "")):
                return channel
    return None


async def _zelle_posts(channel) -> list[str]:
    """The last month's posts there, as text - forwards and embeds too."""
    from ..bot.jobs import SETUP_PAID_DAYS

    since = datetime.now(timezone.utc) - timedelta(days=SETUP_PAID_DAYS)
    posts = []
    try:
        async for one in channel.history(limit=500, after=since):
            text, _images = _request_bits(one)
            if text.strip():
                posts.append(text)
    except Exception:
        log.warning("Couldn't read the Zelle channel", exc_info=True)
    return posts


async def hub_check_loop(bot: "WilByteBot") -> None:
    """Every order the hub newly marks fulfilled, against the agent's sheet -
    "if they really received the right amount". Each order checked once;
    only the short ones said, together, in one message. Reads only."""
    from .. import hub

    while not bot.is_closed():
        try:
            responder = _board_responder(bot)
            responder = _hub_responder(bot) or responder
            if responder is not None:
                found = await asyncio.to_thread(hub.fulfilled_tab, bot.config.secrets)
                said = await asyncio.to_thread(hub.checked)
                fresh = hub.due(found, now=datetime.now(timezone.utc), said=said)
                if fresh:
                    done = await asyncio.to_thread(jobs.hub_check, bot.config, fresh)
                    short = [one for one in done if one.short_by()]
                    if short:
                        ping = _unmarked_ping(bot.config)
                        await responder.send(
                            ((ping + "\n") if ping else "")
                            + "📦 **Marked fulfilled on the hub, but short on their sheet:**\n"
                            + "\n".join(hub.describe(one) for one in short)
                            + "\n-# `@RYTE hub check <name>` to look again.",
                            quiet=True,
                        )
                    # Remembered only once the sheet was read, so a sheet
                    # Google wouldn't open is tried again next time.
                    await asyncio.to_thread(hub.remember, {
                        hub.key(one.agent): one.on_sheet for one in done if one.on_sheet is not None
                    })
        except asyncio.CancelledError:
            raise
        except Exception:  # a bad tick must not take the loop down for good
            log.warning("Hub check failed; will try again", exc_info=True)
        await asyncio.sleep(HUB_CHECK_SECONDS)


def _hub_responder(bot: "WilByteBot"):
    """#hub-agent-fulfillment in Ryte The Goat, made for these, or None for
    the board's channel. DISCORD_HUB_CHANNEL_ID if it's set; else found by
    name - letters only, whatever font or separators the name is written in.
    "why is it updating here, iwant it here": the alert went to
    #trello-manager because a plain look for "hub-agent-fulfil" missed it."""
    import unicodedata

    configured = (os.getenv("DISCORD_HUB_CHANNEL_ID") or "").strip()
    if configured.isdigit():
        channel = bot.get_channel(int(configured))
        if channel is not None:
            return ChannelResponder(channel)

    for guild in getattr(bot, "guilds", None) or []:
        for channel in getattr(guild, "text_channels", None) or []:
            if "hubagentfulfil" in _plain_letters(getattr(channel, "name", "")):
                return ChannelResponder(channel)
    # Else wherever `@RYTE hub check` was last asked - that is the channel
    # somebody reads these in.
    from .. import hub

    remembered = hub.remembered_channel()
    channel = bot.get_channel(remembered) if remembered and hasattr(bot, "get_channel") else None
    if channel is not None:
        return ChannelResponder(channel)
    log.warning("No #hub-agent-fulfillment found - hub alerts go to the board's channel")
    return None


def _plain_letters(name) -> str:
    """A name's letters a-z, whatever they're written in: "𝒩𝒾𝒸𝑜𝓁𝑒" and
    "ʜᴜʙ-ᴀɢᴇɴᴛ" (small capitals, which NFKC leaves alone) read as plain
    letters, emoji and separators dropped."""
    import unicodedata

    out = []
    for ch in unicodedata.normalize("NFKC", str(name or "")):
        if ch.isascii():
            out.append(ch.casefold())
            continue
        named = re.match(r"LATIN (?:SMALL |CAPITAL )?LETTER (?:SMALL CAPITAL )?([A-Z])\b",
                         unicodedata.name(ch, ""))
        if named:
            out.append(named.group(1).casefold())
    return re.sub(r"[^a-z]", "", "".join(out))


#: Sheets read for one `@RYTE hub check` of the whole Fulfilled tab.
HUB_CHECK_MOST = 60


async def _hub_check(responder: Responder, config: Config, who: str, *, bot=None) -> None:
    """`@RYTE hub check` - everybody on the hub's Fulfilled tab, the short
    ones listed and the rest counted; `hub check Tavin Dougher` for one,
    shown in full."""
    from .. import hub

    try:
        # The hub's Fulfilled tab only - not its Done tab, nor paused agents
        # who once reached their count.
        found = await asyncio.to_thread(hub.fulfilled_tab, config.secrets)
    except hub.HubError as exc:
        await responder.send(f"⚠ {exc}")
        return
    if who.strip():
        picked = hub.named(found, who)
        if not picked:
            # Not fulfilled - then say where they are, rather than nothing.
            try:
                anywhere = hub.named(await asyncio.to_thread(hub.agents, config.secrets, status="all"), who)
            except hub.HubError:
                anywhere = []
            if anywhere:
                await responder.send(
                    f"**{who.strip()}** isn't fulfilled yet:\n" + "\n".join(
                        f"• {one.name} ({one.lead_type or '?'}) — **{one.status or '?'}**, "
                        f"{one.progress or '?'} sent" + (f", live {one.start_date:%b %-d}" if one.start_date else "")
                        for one in anywhere[:5]
                    )
                )
            else:
                await responder.send(f"Nobody called **{who.strip()}** is on the hub at all.")
            return
    else:
        picked = [one for one in found if one.sheet_url][:HUB_CHECK_MOST]
        if not picked:
            # Said with what the hub did send, so a word for the tab that
            # isn't "fulfilled" or "ended" can be seen rather than guessed.
            from collections import Counter

            try:
                seen = await asyncio.to_thread(hub.agents, config.secrets, status="fulfilled")
            except hub.HubError:
                seen = []
            kinds = Counter((one.status or "?") for one in seen)
            await responder.send(
                "Nobody is on the hub's Fulfilled tab right now."
                + (f"\n-# Asked for fulfilled, the hub sent: "
                   + ", ".join(f"{many} {status}" for status, many in kinds.most_common()) if kinds else "")
            )
            return
    await responder.send(f"Counting {len(picked)} sheet(s) against the hub —")
    done = await asyncio.to_thread(jobs.hub_check, config, picked)
    short = [one for one in done if one.short_by()]
    unread = [one for one in done if one.on_sheet is None]
    behind = [one for one in done if one.counter_off()]
    fine = len(done) - len(short) - len(unread) - len(behind)
    shown = done if who.strip() else short + behind + unread
    # One person asked about: the working too, to hold against the sheet.
    lines = [hub.describe(one) + (("\n" + hub.working(one)) if who.strip() and hub.working(one) else "")
             for one in shown]
    if not who.strip():
        lines.append(f"✅ {fine} other(s) match the hub." if shown else f"✅ All {fine} match the hub.")
    for start in range(0, len(lines), 12):
        await responder.send("\n".join(lines[start:start + 12]))
    # Asked here, so here is where these are read - unless a channel is set
    # or found by name.
    channel_id = getattr(getattr(responder, "channel", None), "id", None) or getattr(responder, "channel_id", None)
    if channel_id:
        hub.remember_channel(channel_id)
    alerts = _hub_responder(bot) if bot is not None else None
    where = f"<#{alerts.channel.id}>" if alerts is not None and getattr(alerts.channel, "id", None) else \
        "the board's channel (no #hub-agent-fulfillment found)"
    await responder.send("-# The sheet's leads against what the hub says it sent. Test leads, the "
                         "colour key and the dispo banners aren't counted. New shorts are posted "
                         f"in {where} by themselves.")


async def contract_check_loop(bot: "WilByteBot") -> None:
    """Flag an order about to go live with no signed contract of its own.

    "every order" - a reorder needs its own contract, so the one from last
    month does not cover it. Today's and tomorrow's go-lives, from seven in
    the morning on the board's clock. Each one said once, and said again only
    to say it has been signed since. "tag me only".

    Reads only: the board, and PandaDoc.
    """
    from .. import alreadysaid

    while not bot.is_closed():
        try:
            responder = _board_responder(bot)
            hour = datetime.now(ZoneInfo(bot.config.schedule.timezone)).hour
            if responder is not None and hour >= CONTRACT_CHECK_FROM_HOUR:
                day = await asyncio.to_thread(jobs.board_day, bot.config)
                paired, problems = await asyncio.to_thread(jobs.contract_check, bot.config, today=day)
                said = await asyncio.to_thread(alreadysaid.said_lately, day)
                fresh, signed, keys = [], [], []
                for one in paired:
                    card = one.order.card_id
                    if one.how == "email":
                        if f"contract|missing|{card}" in said and f"contract|found|{card}" not in said:
                            signed.append(one)
                            keys.append(f"contract|found|{card}")
                        continue
                    key = f"contract|{'unsure' if one.how else 'missing'}|{card}"
                    if key not in said:
                        fresh.append(one)
                        keys.append(key)
                lines = []
                if fresh:
                    lines.append(describe_contracts(fresh))
                if signed:
                    lines.append("✅ **Signed since:**\n" + "\n".join(_contract_line(one) for one in signed))
                if lines:
                    ping = _unmarked_ping(bot.config)
                    await responder.send(((ping + "\n") if ping else "") + "\n\n".join(lines))
                    await asyncio.to_thread(alreadysaid.remember, day, keys)
                if problems:
                    log.warning("Contract check: %s", "; ".join(problems))
        except asyncio.CancelledError:
            raise
        except Exception:  # a bad tick must not take the loop down for good
            log.exception("Contract check failed; will try again shortly")
        await asyncio.sleep(CONTRACT_CHECK_SECONDS)


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


# ------------------------------------------------ replying the way Faith does

#: A minute, like the other watchers. One request while nothing is happening.
RING_CHECK_SECONDS = 60

#: Setup problems already said this run. A bad JWT said once is a job for
#: somebody; said every minute it is a channel nobody reads by lunchtime.
_RING_SAID: set = set()

#: Passes in a row (a minute apart) RingCentral couldn't be reached before it
#: is said. "Couldn't reach RingCentral to sign in: [Errno 8]" was the Mac's
#: network dropping for a minute - news only when it lasts.
RING_UNREACHABLE_SAY_AFTER = 5
_RING_UNREACHABLE: list = [0]

#: Whether this run has said it is connected. Once per start: with no agent
#: waiting, a working watcher and a broken one are otherwise equally silent.
_RING_READY: list = []

#: When writing Faith's playbook last failed. Tried again an hour later rather
#: than every minute - a study of her whole history that keeps failing is a
#: bill, not a retry.
_PLAYBOOK_FAILED: list = []

#: When studying last failed. Tried again ten minutes later, not every minute.
_STUDY_FAILED: list = []

#: The Discord messages that are RYTE's suggestions and lessons, so a reply
#: to one is known for Franklin teaching him without reading the file for
#: every reply in the server.
_RING_POSTS: set = set()

_RING_SETTINGS = (
    ("RINGCENTRAL_CLIENT_ID", "ringcentral_client_id"),
    ("RINGCENTRAL_CLIENT_SECRET", "ringcentral_client_secret"),
    ("RINGCENTRAL_JWT", "ringcentral_jwt"),
    ("RINGCENTRAL_EXTENSION", "ringcentral_extension"),
)


def _ring_half_set(secrets) -> list:
    """The settings missing when some are there. [] when none or all are.

    None set is somebody not using this, and is nobody's business. Some set
    is somebody who meant to and got a name wrong - and that watcher idles
    in exactly the silence a working one keeps when no agent is waiting.
    """
    have = [env for env, attr in _RING_SETTINGS
            if (getattr(secrets, attr, "") or "").strip()]
    if not have or len(have) == len(_RING_SETTINGS):
        return []
    return [env for env, _attr in _RING_SETTINGS if env not in have]


async def ring_loop(bot: "WilByteBot") -> None:
    """Watch Faith's texts and ping Franklin with how she would answer.

    "i dont need RYte to send the messages, i need him to ping me on discord
    on how to respond to their message the way how Faith respond." It cannot
    send - `ringcentral` has no way to - so every one of these is a message
    to Franklin, and the words reach an agent only if he sends them himself.

    Nothing here waits on a button. The tag watcher did, and one unanswered
    button at half eight stopped it seeing anything for the rest of the day.
    """
    from .. import ringcentral

    while not bot.is_closed():
        try:
            if ringcentral.configured(bot.config.secrets):
                await _ring_once(bot)
            else:
                missing = _ring_half_set(bot.config.secrets)
                said = "RingCentral is half set up — missing " + ", ".join(missing)
                if missing and said not in _RING_SAID:
                    _RING_SAID.add(said)
                    responder = _ring_responder(bot)
                    if responder is not None:
                        await responder.send(f"⚠ {said} in .env.")
        except asyncio.CancelledError:
            raise
        except Exception:  # a bad tick must not take the loop down for good
            log.exception("RingCentral check failed; will try again shortly")
        await asyncio.sleep(RING_CHECK_SECONDS)


def _ring_channel(bot: "WilByteBot", setting: str = "ringcentral_channel_id") -> tuple:
    """(the channel the suggestions go to or None, what is wrong with it or "").

    By id or by name. "#ryte-responder" is what Franklin sees; an id means
    turning on Developer Mode to go and find one, and the name is enough to
    find it by when only one channel RYTE is in carries it.

    Checked for RYTE being allowed to post there. The channel is private, and
    a channel RYTE cannot see is one every ping silently fails to reach - the
    loop logs it and Discord shows nothing, which from where Franklin sits is
    exactly the same as no agent having texted.
    """
    env = setting.upper()
    wanted = str(getattr(bot.config.secrets, setting, "") or "").strip()
    wanted = wanted.lstrip("#").strip()
    if not wanted:
        return None, ""
    if wanted.isdigit():
        channel = bot.get_channel(int(wanted))
        if channel is None:
            return None, (
                f"I can't find channel {wanted} for the RingCentral suggestions, "
                "or I'm not allowed to see it. Posting them here instead."
            )
    else:
        named = [
            one for one in bot.get_all_channels()
            if getattr(one, "type", None) == discord.ChannelType.text
            and str(one.name).casefold() == wanted.casefold()
        ]
        if len(named) != 1:
            return None, (
                f"I can see {'no' if not named else len(named)} channel"
                f"{'' if len(named) == 1 else 's'} called #{wanted}, so the "
                "RingCentral suggestions are coming here instead. "
                + ("If it's private, give my role access to it."
                   if not named else f"Put its id in {env}.")
            )
        channel = named[0]
    me = getattr(getattr(channel, "guild", None), "me", None)
    if me is not None:
        allowed = channel.permissions_for(me)
        if not (allowed.view_channel and allowed.send_messages):
            return None, (
                f"I'm not allowed to post in #{channel.name}, so the RingCentral "
                "suggestions are coming here instead. Give my role View Channel "
                "and Send Messages there."
            )
    return channel, ""


def _ring_responder(bot: "WilByteBot"):
    """Its own channel if it can be posted in, or the board's - and the
    team's copy of it alongside, when there is one."""
    channel, _trouble = _ring_channel(bot)
    main = ChannelResponder(channel) if channel is not None else _board_responder(bot)
    shared, _trouble = _ring_channel(bot, "ringcentral_shared_channel_id")
    if main is None or shared is None or shared.id == getattr(main, "channel_id", None):
        return main
    return AlsoThere(main, ChannelResponder(shared))


_TAGS = re.compile(r"<@[!&]?\d+>|@everyone|@here")


def _untagged(content):
    """What a message says with nobody tagged in it - None if that is nothing."""
    if content is None:
        return None
    left = " ".join(_TAGS.sub(" ", str(content)).split(" ")).strip()
    return left or None


class AlsoThere:
    """Franklin's channel, and the same again on the team's second screen.

    "its juts like a second screen, that one where everyone can access ryte"
    - the cards, the lessons, the playbook, all of it, except that nobody is
    tagged there: "Ryte wont press me or anyone unlike on my server". His
    channel is the one that matters; the copy failing is logged and never
    costs him his ping.
    """

    def __init__(self, main, shared):
        self.main, self.shared = main, shared
        self.requester_id = getattr(main, "requester_id", None)
        self.channel_id = getattr(main, "channel_id", None)
        #: The copy of the last thing sent, so a reply to it can teach RYTE
        #: the same as a reply to the original.
        self.last_echo = None

    async def send(self, content=None, *, embed=None, file=None, view=None, quiet=False):
        import io

        data = None
        if file is not None:
            # A file can be sent once; each channel gets its own.
            file.fp.seek(0)
            data = file.fp.read()
            file = discord.File(io.BytesIO(data), filename=file.filename)
        sent = await self.main.send(content, embed=embed, file=file, view=view, quiet=quiet)
        self.last_echo = None
        said = _untagged(content)
        if said is None and embed is None and data is None:
            return sent
        try:
            self.last_echo = await self.shared.send(
                said, embed=embed, quiet=quiet,
                file=discord.File(io.BytesIO(data), filename=file.filename) if data is not None else None,
            )
        except Exception:
            log.warning("Couldn't post the copy on the team's screen", exc_info=True)
        return sent


async def _ring_once(bot: "WilByteBot") -> None:
    from .. import smsreplies

    found, data, problems = await asyncio.to_thread(jobs.ring_waiting, bot.config)
    responder = _ring_responder(bot)
    # Where they are going wrong is said where they are going instead, once.
    _where, trouble = _ring_channel(bot)
    if trouble:
        problems = [trouble] + list(problems)
    blips = [one for one in problems if "Couldn't reach RingCentral" in one]
    _RING_UNREACHABLE[0] = _RING_UNREACHABLE[0] + 1 if blips else 0
    lasting = _RING_UNREACHABLE[0] >= RING_UNREACHABLE_SAY_AFTER
    fresh = [one for one in problems if one not in _RING_SAID and (lasting or one not in blips)]
    if fresh and responder is not None:
        _RING_SAID.update(fresh)
        await responder.send("⚠ RingCentral: " + "\n⚠ ".join(fresh))
    # The team's copy going wrong is said once, in Franklin's channel, and
    # stops nothing: his pings do not depend on it.
    _shared, elsewhere = _ring_channel(bot, "ringcentral_shared_channel_id")
    if elsewhere and elsewhere not in _RING_SAID and responder is not None:
        _RING_SAID.add(elsewhere)
        await getattr(responder, "main", responder).send(
            "⚠ The team's copy: " + elsewhere.replace(
                "so the RingCentral suggestions are coming here instead",
                "so the team's copy isn't going anywhere",
            ).replace(" Posting them here instead.", "")
        )
    texts = jobs.ring_texts(bot.config, data)
    done = smsreplies.give_reasons(smsreplies.exchanges(texts), data.get("reasons") or {})
    _RING_POSTS.update(jobs.ring_posts(data))
    if not problems and not _RING_READY and responder is not None:
        _RING_READY.append(True)
        # Not every start - RYTE restarts onto every update, and the channel
        # filled with "Connected to RingCentral". Every few days, or to say
        # it's back after a problem was said.
        from .. import alreadysaid

        today = date.today()
        if _RING_SAID or "ring-connected" not in alreadysaid.said_lately(today):
            alreadysaid.remember(today, ["ring-connected"])
            await responder.send(_ring_ready(len(texts), len(done)))
    if responder is not None and done and not problems:
        await _write_the_playbook(bot, responder, data, done)
    if responder is None:
        return

    # Switched off while Faith is around: nothing drafted, but everything
    # above and the studying below still happen - it reads every text and
    # keeps learning from how she answers them.
    for agent, name, tail in found:
        # Asked again for every text, and again before posting: a pass with
        # several agents waiting takes minutes, and Max Wilson's suggestion
        # was posted after "responder off" because the pass had started.
        if not await asyncio.to_thread(prefs.responder_on):
            break
        asked = "\n".join(one.said for one in tail)
        try:
            known = await asyncio.to_thread(partial(
                jobs.ring_context, bot.config, data, texts, agent=agent,
                name=name, key=tail[-1].key, asked=asked,
            ))
            drafted = await asyncio.to_thread(
                partial(
                    jobs.draft_like_faith, bot.config, asked=asked, done=done,
                    thread=smsreplies.thread(texts, tail[-1].key), name=name,
                    agent=agent, **known,
                )
            )
        except Exception:
            # Not remembered as pinged, so the next minute tries again rather
            # than the text going unanswered because Claude had a bad moment.
            log.exception("Couldn't draft a reply for %s", agent)
            continue
        if not await asyncio.to_thread(prefs.responder_on):
            break
        # Remembered before the ping rather than after, so a restart between
        # the two cannot post the same suggestion twice.
        await asyncio.to_thread(partial(
            jobs.ring_pinged, tail[-1].id, pending={
                "id": tail[-1].id, "key": tail[-1].key, "at": tail[-1].at,
                "asked": asked, "draft": drafted["reply"], "agent": agent,
                "name": name,
            },
        ))
        content, card = _ring_card(
            bot.config, agent, name, tail, drafted, card=known.get("card"),
        )
        sent = await responder.send(content or None, embed=card)
        await _remember_post(sent, tail[-1].id, also=getattr(responder, "last_echo", None))

    if done and not problems:
        await _study(bot, responder)


async def _set_responder(responder: Responder, wanted: str) -> None:
    """Switch the responder on while Faith is away and off when she's back.

    Off, it still reads every text and learns why she answered as she did,
    and her playbook is written again from all of her replies - so what it
    drafts next time she is away is what she has been doing since.
    """
    if wanted not in ("on", "off"):
        on = await asyncio.to_thread(prefs.responder_on)
        await responder.send(
            f"Responder is **{'on' if on else 'off'}**. "
            "`@RYTE responder on` when Faith is away, `@RYTE responder off` when she's back."
        )
        return
    await asyncio.to_thread(prefs.set_responder, wanted == "on")
    if wanted == "on":
        await responder.send(
            "🔔 **Responder on.** I'll draft replies to agents' texts again, "
            "starting with any from the last 12 hours nobody has answered."
        )
        return
    await asyncio.to_thread(jobs.ring_playbook_stale)
    await responder.send(
        "🔕 **Responder off** — Faith's got it. I'm still reading every text and "
        "learning from how she answers, and I'm rewriting her playbook from all of "
        "her replies now. `@RYTE responder on` when she's away again."
    )


async def _remember_post(sent, ring_id: str, *, lesson: str = "", also=None) -> None:
    """Which Discord message this went out as - and its copy on the team's
    screen - for replies to either."""
    from . import mirror

    posted = getattr(sent, "id", None)
    if not isinstance(posted, int):
        return
    _RING_POSTS.add(posted)
    # Its copy in Ryte The Goat, when there is one, before the team's.
    twin = getattr(mirror.copy_of_message(posted), "id", None)
    if isinstance(twin, int):
        _RING_POSTS.add(twin)
        try:
            await asyncio.to_thread(partial(jobs.ring_posted, ring_id, twin, lesson=lesson, echo=True))
        except Exception:
            log.exception("Couldn't remember the copy of that message")
    echo = getattr(also, "id", None)
    try:
        await asyncio.to_thread(partial(jobs.ring_posted, ring_id, posted, lesson=lesson))
        if isinstance(echo, int):
            _RING_POSTS.add(echo)
            await asyncio.to_thread(partial(
                jobs.ring_posted, ring_id, echo, lesson=lesson, echo=True,
            ))
    except Exception:
        log.exception("Couldn't remember which message that was")


async def _study(bot, responder) -> None:
    """Learn a little more - why suggestions were changed, why she answered
    the way she did - and tell Franklin each lesson, so he can say where the
    reason is wrong."""
    import time

    from .. import ringtexts, smsreplies

    if _STUDY_FAILED and time.time() - _STUDY_FAILED[-1] < 600:
        return
    try:
        got = await asyncio.to_thread(jobs.ring_study, bot.config)
    except Exception:
        _STUDY_FAILED.append(time.time())
        log.exception("Couldn't study Faith's replies")
        return
    if got.get("problems"):
        _STUDY_FAILED.append(time.time())
        for one in got["problems"]:
            log.warning("%s", one)
    if not got.get("explained"):
        return
    lessons = list((await asyncio.to_thread(ringtexts.load)).get("lessons") or [])
    used, out_of = smsreplies.used_count(lessons)
    for lesson in got["explained"]:
        sent = await responder.send(embed=_ring_lesson(lesson, used=used, out_of=out_of))
        await _remember_post(
            sent, "", lesson=jobs._lesson_id(lesson), also=getattr(responder, "last_echo", None),
        )



def _ring_lesson(lesson: dict, *, used: int = 0, out_of: int = 0):
    """What was learned from one suggestion that was not sent as written."""
    who = lesson.get("name") or _ring_number(str(lesson.get("agent") or "")) or "an agent"
    return embeds.ring_lesson(lesson, who=f"From {who}", used=used, out_of=out_of)


def _is_ring_reply(message) -> bool:
    """A person replying to one of RYTE's suggestions or lessons."""
    if getattr(message.author, "bot", False):
        return False
    reference = getattr(message, "reference", None)
    return bool(reference) and getattr(reference, "message_id", None) in _RING_POSTS


async def _ring_told(message) -> None:
    """Take what Franklin said about a suggestion or a lesson as the reason."""
    text = (message.content or "").strip()
    if not text:
        return
    where = await asyncio.to_thread(
        jobs.ring_told, message.reference.message_id, text,
    )
    if where == "lesson":
        answer = "📝 Got it — I'll work that lesson out again your way and use it from now on."
    elif where == "ping":
        answer = "📝 Got it — I'll learn from that alongside whatever gets sent."
    else:
        return
    await message.reply(answer, mention_author=False)


def _ring_ready(texts: int, replies: int) -> str:
    """The line that says it is connected, once a run.

    Says what it learned from, because a connection to the wrong extension
    connects perfectly well: somebody with no texts to agents reads as zero
    replies, and that is worth saying rather than finding out in a week.
    """
    if not replies:
        return (
            f"📱 Connected to RingCentral and read {texts} text"
            f"{'' if texts == 1 else 's'} — but none of them are Faith answering "
            "an agent, so there's nothing to learn her replies from. Check "
            "RINGCENTRAL_EXTENSION is the line she texts from."
        )
    return (
        f"📱 Connected to RingCentral — learning from {replies} of Faith's "
        f"replies ({texts} texts). I'll ping here when an agent is waiting. "
        "Read-only: I can't text anybody."
    )


def _ring_number(agent: str) -> str:
    """"8015550142" as "(801) 555-0142"."""
    return (
        f"({agent[:3]}) {agent[3:6]}-{agent[6:]}" if len(agent) == 10 else agent
    )




async def _write_the_playbook(bot, responder, data: dict, done: list) -> None:
    """Study her replies and write down how she handles agents, when it's due.

    Posted when written, as a file: it is what every suggestion leans on, and
    the person who knows whether it is right is the one reading this channel.
    """
    import io
    import time

    from .. import smslinks, smsreplies

    if not jobs.ring_playbook_due(data):
        return
    if _PLAYBOOK_FAILED and time.time() - _PLAYBOOK_FAILED[-1] < 3600:
        return
    try:
        text = await asyncio.to_thread(
            jobs.faith_playbook, bot.config, done,
            smsreplies.rules_from(list(data.get("lessons") or [])),
            smslinks.catalog(jobs.ring_texts(bot.config, data)),
        )
    except Exception:
        _PLAYBOOK_FAILED.append(time.time())
        log.exception("Couldn't write Faith's playbook")
        return
    await asyncio.to_thread(jobs.ring_keep_playbook, text, len(done))
    data["playbook"] = {"text": text}
    await responder.send(
        f"📘 **Faith's playbook** — how she handles agents, studied from "
        f"{len(done)} of her replies. Every suggestion uses it. If anything in "
        "it is wrong, tell me and it gets fixed.",
        file=discord.File(io.BytesIO(text.encode("utf-8")), filename="faith-playbook.md"),
    )


def _ring_card(
    config: Config, agent: str, name: str, tail: list, drafted: dict,
    *, card: dict | None = None,
):
    """The ping: (who to tap on the shoulder, the card).

    The mention outside the card, where Discord notifies for it; inside an
    embed it renders and nobody hears it.
    """
    when = jobs._ring_when(tail[-1].at) if tail else None
    who = f"{name} · {_ring_number(agent)}" if name else _ring_number(agent)
    return _unmarked_ping(config), embeds.ring_ping(
        who=who, said="\n".join(one.said for one in tail), drafted=drafted,
        when=when, card=card,
    )


async def _respond_like_faith(
    responder: Responder, config: Config, message, said: str,
) -> None:
    """"@Ryte respond" with a screenshot of a thread - Faith's reply to it now.

    The pings come on their own; this is for the conversation Franklin is
    already looking at. A screenshot is read for who said what, pasted words
    are taken as the agent's, and the draft is written from the same 1,600 of
    her replies the pings are.
    """
    from .. import ringcentral, ringtexts, smsreplies

    shots, skipped = [], []
    for attachment in getattr(message, "attachments", []) or []:
        name = str(getattr(attachment, "filename", "") or "")
        if not re.search(r"\.(png|jpe?g|gif|webp)$", name, re.IGNORECASE):
            continue
        if (getattr(attachment, "size", 0) or 0) > jobs.RING_SHOT_BYTES:
            skipped.append(f"{name} is over 5MB")
            continue
        try:
            shots.append((name, await attachment.read()))
        except Exception as exc:
            skipped.append(f"{name} ({jobs._short(exc, 60)})")
    typed = " ".join(str(said or "").split())
    if not shots and not typed:
        await responder.send(
            "Send a screenshot of the conversation with `@Ryte respond`, or "
            "paste what the agent said after it — I'll write Faith's reply."
            + "".join(f"\n⚠ {one}" for one in skipped)
        )
        return

    problems = []
    if ringcentral.configured(config.secrets):
        data, problems = await asyncio.to_thread(jobs.ring_catch_up, config)
    else:
        data = await asyncio.to_thread(ringtexts.load)
    done = smsreplies.give_reasons(
        smsreplies.exchanges(jobs.ring_texts(config, data)), data.get("reasons") or {},
    )
    if not done:
        await responder.send(
            "I have none of Faith's replies to learn from yet, so anything I "
            "wrote would be my voice, not hers."
            + "".join(f"\n⚠ {one}" for one in problems)
        )
        return

    thread, name = [], ""
    if shots:
        try:
            lines = await asyncio.to_thread(
                partial(jobs.read_texts_off, config, shots,
                        line_name=str(data.get("owner") or ""))
            )
        except Exception as exc:
            await responder.send(f"Couldn't read that screenshot: {_readable(exc)}")
            return
        thread = [
            smsreplies.Text(
                id=str(at), at=f"{at:05d}", inbound=one["side"] != "faith",
                agent="screenshot", name=one["name"], said=one["text"],
                team=one["side"] == "team",
            )
            for at, one in enumerate(lines)
        ]
        # What the agent has said since Faith last did - the same rule the
        # pings use, colleagues passed over.
        tail = []
        for one in reversed(thread):
            if one.team:
                continue
            if not one.inbound:
                break
            tail.insert(0, one)
        if not tail:
            await responder.send(
                "Faith has already answered the last thing the agent said in "
                "that screenshot — there's nothing waiting on a reply."
            )
            return
        asked = "\n".join(one.said for one in tail)
        name = next((one.name for one in reversed(tail) if one.name), "")
    else:
        asked = typed

    # Who this is, from the texts already read: the screenshot shows a name,
    # and the name is how to find their number, their history and their card.
    texts = jobs.ring_texts(config, data)
    agent = next(
        (one.agent for one in reversed(texts)
         if name and one.inbound and one.name.casefold() == name.casefold()),
        "",
    )
    try:
        known = await asyncio.to_thread(partial(
            jobs.ring_context, config, data, texts, agent=agent, name=name,
            asked=asked,
        ))
        drafted = await asyncio.to_thread(
            partial(jobs.draft_like_faith, config, asked=asked, done=done,
                    thread=thread, name=name, agent=agent, **known)
        )
    except Exception as exc:
        await responder.send(f"Couldn't draft that: {_readable(exc)}")
        return
    await responder.send(
        "\n".join(f"⚠ {one}" for one in skipped) or None,
        embed=embeds.ring_ping(
            who=f"Reply like Faith to {name}" if name else "Reply like Faith",
            said="", drafted=drafted, card=known.get("card"),
        ),
    )


#: How the verdict is said.
_VOICE_VERDICT = {"yes": "✅ Yes — that sounds like Faith.",
                  "close": "🟡 Close — a few things she'd do differently.",
                  "no": "🔴 Not really — it doesn't read like her."}


async def _faith_voice(responder: Responder, config: Config, message, draft: str) -> None:
    """"@Ryte does this sound like faith?" with a reply under it - held up to
    her real texts, and given back in her voice. Nothing is sent."""
    from .. import ringcentral, ringtexts, smsreplies

    if not draft.strip():
        # Asked as a reply to the draft itself.
        replied = getattr(getattr(message, "reference", None), "resolved", None)
        draft = str(getattr(replied, "content", "") or "").strip()
    if not draft.strip():
        await responder.send("Put the reply under the question: `@Ryte does this sound like Faith?` "
                             "then your message — or ask it as a reply to the message.")
        return
    problems: list[str] = []
    if ringcentral.configured(config.secrets):
        data, problems = await asyncio.to_thread(jobs.ring_catch_up, config)
    else:
        data = await asyncio.to_thread(ringtexts.load)
    done = smsreplies.give_reasons(
        smsreplies.exchanges(jobs.ring_texts(config, data)), data.get("reasons") or {},
    )
    if not done:
        await responder.send("I haven't read any of Faith's replies yet, so I can't tell what she sounds like"
                             + (f" — {problems[0]}" if problems else "."))
        return
    try:
        got = await asyncio.to_thread(partial(
            jobs.faith_voice_check, config, draft, done,
            playbook=str((data.get("playbook") or {}).get("text") or ""),
            learned=smsreplies.rules_from(list(data.get("lessons") or [])),
        ))
    except Exception as exc:
        await responder.send(f"Couldn't check that: {_readable(exc)}")
        return
    await responder.send(faith_voice_said(got, draft))


def faith_voice_said(got: dict, draft: str) -> str:
    """The check, as one Discord message: the verdict, what's off, anything
    her replies say differently, and the reply in her voice to copy."""
    lines = [_VOICE_VERDICT.get(got.get("verdict"), _VOICE_VERDICT["close"])]
    lines += [f"• {one[:220]}" for one in got.get("differences") or []]
    lines += [f"⚠ Her replies say otherwise: {one[:300]}" for one in got.get("facts") or []]
    rewrite = str(got.get("rewrite") or "").strip()
    if rewrite and (got.get("verdict") != "yes" or rewrite != draft.strip()):
        fence = "`" * 3
        lines += ["", "**As Faith would send it:**", fence, rewrite.replace(fence, "'''")[:1100], fence]
    lines.append(f"-# Held up to {got.get('examples', 0)} of her replies. Nothing was sent.")
    return "\n".join(lines)[:1995]


async def _hub_fields(responder: Responder, config: Config, who: str) -> None:
    """"@Ryte hub fields Gavin Littledike" - everything the hub gives for
    them, a line a field. Reads only; emails and phone numbers blanked."""
    from .. import clearout, hub

    try:
        everybody = await asyncio.to_thread(hub.agents, config.secrets, status="all")
    except hub.HubError as exc:
        await responder.send(f"⚠ {exc}")
        return
    found = [one for one in everybody if clearout.name_match(who, one.name) >= 2]
    if not found:
        await responder.send(f"Nobody on the hub called “{who}”.")
        return
    for one in found[:3]:
        await responder.send(f"**{one.name}** — {one.lead_type or '?'} · {one.status or '?'}\n"
                             + "\n".join(hub.fields_of(one)))
    if len(found) > 3:
        await responder.send(f"-# and {len(found) - 3} more with that name.")


async def _site_code_once(config: Config) -> None:
    try:
        for line in await asyncio.to_thread(jobs.site_code_publish, config):
            log.info("Site code: %s", line)
    except Exception:
        log.warning("Couldn't put the page scripts on the site", exc_info=True)


async def _site_code(responder: Responder, config: Config) -> None:
    """"@Ryte site code" - the page scripts on leadlabcrm.com, brought up to date now."""
    try:
        lines = await asyncio.to_thread(jobs.site_code_publish, config)
    except Exception as exc:
        await responder.send(f"⚠ Couldn't update the page scripts: {_readable(exc)}")
        return
    await responder.send("\n".join(lines) or "No page scripts to keep.")


async def _ask_about_faith(responder: Responder, config: Config, question: str) -> None:
    """"@Ryte what does Faith send when…" - answered from her texts.

    Read fresh from RingCentral first, so a link she started sending this
    morning is in it. The links are counted, not summarised.
    """
    from .. import ringcentral, ringtexts, smsreplies

    problems: list[str] = []
    if ringcentral.configured(config.secrets):
        data, problems = await asyncio.to_thread(jobs.ring_catch_up, config)
    else:
        data = await asyncio.to_thread(ringtexts.load)
    done = smsreplies.give_reasons(
        smsreplies.exchanges(jobs.ring_texts(config, data)), data.get("reasons") or {},
    )
    if not done:
        await responder.send(
            "I haven't read any of Faith's replies yet"
            + (f" — {problems[0]}" if problems else " — is RingCentral set up in .env?")
        )
        return
    try:
        got = await asyncio.to_thread(jobs.faith_answers, config, done, question)
    except Exception as exc:
        await responder.send(f"Couldn't answer that: {_readable(exc)}")
        return
    stale = (
        " · ⚠ couldn't reach RingCentral just now, so this is as of the last read"
        if problems else ""
    )
    if not got["matched"]:
        await responder.send(
            f"I looked through {len(done)} of Faith's replies for "
            f"{', '.join(got['terms'][:6]) or 'that'} and none of them are about it. "
            f"Try asking it another way.{stale}"
        )
        return
    await responder.send(embed=embeds.ring_answer(
        question, got["answer"], links=got["links"], matched=len(got["matched"]),
        read=len(done), stale=bool(problems),
    ))


#: How often Done is checked for unticked cards sunk below ticked ones. One
#: request when nothing is out of place.
FLOAT_CHECK_SECONDS = 120


async def float_loop(bot: "WilByteBot") -> None:
    """Keep the unticked New Agent cards on top of Done, all day. Quietly -
    it is tidying, and a message every time a card floats is noise."""
    while not bot.is_closed():
        try:
            moved, problems = await asyncio.to_thread(jobs.float_unticked, bot.config)
            if moved:
                log.info("Floated to the top of Done: %s", ", ".join(moved))
            for one in problems:
                log.warning("%s", one)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Couldn't keep Done in order; will try again shortly")
        await asyncio.sleep(FLOAT_CHECK_SECONDS)


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


#: How often Payra is asked what changed.
PAYRA_SYNC_SECONDS = 600


async def payra_loop(bot: "WilByteBot") -> None:
    """Keep the copy of Payra's invoices and payments up to date, all day.
    A failure is logged and the next pass carries on from where it stopped."""
    while not bot.is_closed():
        try:
            counts = await asyncio.to_thread(jobs.payra_sync, bot.config)
            if any(counts.values()):
                log.info("Payra: %s", ", ".join(f"{n} {kind}" for kind, n in counts.items()))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("Couldn't read Payra; trying again shortly", exc_info=True)
        await asyncio.sleep(PAYRA_SYNC_SECONDS)


def _keep_payment(message_id, paid) -> None:
    from .. import payra

    data = payra.load()
    payra.remember(data, message_id, paid)
    payra.save(data)


#: How far back the first read of the payments channel goes.
PAYRA_FIRST_DAYS = 365


async def _catch_up_payra(bot: "WilByteBot") -> None:
    """Read what Payra posted while RYTE wasn't listening - the channel's last
    year the first time, then only what is new. Once a start."""
    from datetime import timedelta

    from .. import levinson, payra

    where = str(bot.config.secrets.discord_payment_channel_id or "").strip()
    channel = bot.get_channel(int(where)) if where.isdigit() else None
    if channel is None:
        return
    zone = ZoneInfo(bot.config.schedule.timezone)
    data = await asyncio.to_thread(payra.load)
    newest = str(data.get("newest") or "")
    how = (
        {"after": discord.Object(id=int(newest)), "oldest_first": True}
        if newest.isdigit() else
        {"after": datetime.now(zone) - timedelta(days=PAYRA_FIRST_DAYS)}
    )
    count = 0
    try:
        async for old in channel.history(limit=PAYMENT_SCAN * 5, **how):
            paid = levinson.read_payment(_all_text(old), paid_at=old.created_at.astimezone(zone))
            if paid is not None:
                payra.remember(data, old.id, paid)
                count += 1
    except Exception:
        log.warning("Couldn't read back the payments channel", exc_info=True)
    if count:
        await asyncio.to_thread(payra.save, data)
        log.info("Remembered %d Payra payment(s)", count)


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


#: Tries at reading the Levinson members, and the wait before the next one.
LEVINSON_TRIES, LEVINSON_WAIT = 3, 5


async def _levinson_members(bot: "WilByteBot") -> tuple[list, list[str]]:
    """The member list, from memory when it was read recently enough."""
    held = getattr(bot, "_levinson_members", None)
    read_at = getattr(bot, "_levinson_members_at", 0.0)
    if held is not None and time.time() - read_at < MEMBERS_GOOD_FOR:
        return held, []

    # Three tries a few seconds apart. "[Errno 8] nodename nor servname
    # provided" is the Mac's network blinking, not GHL saying no - and a red
    # "Something went wrong" for it is a false alarm. Still failing, the list
    # read earlier is used if there is one; only with nothing to go on is it
    # an error.
    error = None
    for attempt in range(LEVINSON_TRIES):
        try:
            members, notes = await asyncio.to_thread(jobs.levinson_members, bot.config)
            break
        except Exception as exc:
            error = exc
            if attempt < LEVINSON_TRIES - 1:
                await asyncio.sleep(LEVINSON_WAIT * (attempt + 1))
    else:
        if held is not None:
            log.warning("Couldn't refresh the Levinson members, using the last copy: %s", error)
            return held, []
        raise error
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
    # Every payment, Levinson's or not - for the RingCentral drafts to check
    # when an agent asks whether theirs went through.
    try:
        await asyncio.to_thread(_keep_payment, getattr(message, "id", ""), paid)
    except Exception:
        log.warning("Couldn't remember that payment", exc_info=True)

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

    try:
        ledger = await asyncio.to_thread(Ledger.load)
        # A post deleted in GHL to be redone: forgotten, with its day kept for
        # it, before anything decides the video is already done.
        if context is not None:
            await asyncio.to_thread(jobs.deleted_since, context, ledger, sources)
        videos, already_done = await asyncio.to_thread(
            jobs.resolve_many, sources, ledger,
            limit=limit, force=force, offline=bool(transcript_text),
        )
    except PIPELINE_ERRORS as exc:
        if context:
            await asyncio.to_thread(context.close)
        await responder.send(embed=embeds.error(str(exc)))
        return

    if not videos:
        if context:
            await asyncio.to_thread(context.close)
        await responder.send(
            f"Nothing pending — all {already_done} video(s) are already processed. "
            "Add **force** to redo one."
        )
        return

    created = skipped = failed = 0
    # Which videos didn't make it, so the retry doesn't mean hunting through
    # the original message for the links that got no answer.
    unfinished: list = []
    try:
        slot_pool, held = await asyncio.to_thread(
            partial(
                jobs.run_slots, videos, context, config, ledger,
                include_today=include_today,
            )
        )
        # The blog's live posts, once a run: what each article may link to.
        related = await asyncio.to_thread(jobs.link_targets, context, config, ledger)
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
            + "".join(
                f" Putting it back on **{slot:%a %b %d}**, the day its deleted post held."
                for slot in held.values()
            )
            + (
                f" That's my {config.discord.max_batch}-per-run cap — send the "
                f"other {trimmed} link(s) after and I'll carry on from there."
                if trimmed else ""
            )
        )

        for index, video in enumerate(videos, start=1):
            try:
                post = await asyncio.to_thread(
                    partial(
                        jobs.build, video, config, output_dir,
                        transcript_text=transcript_text if len(videos) == 1 else None,
                        related=related,
                    )
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
            # skip leaves the day free for the next post in the batch. A rerun
            # of a deleted post has its own day, and takes nothing from the pool.
            own = held.get(video.video_id)
            post.scheduled_at = own or (slot_pool[0] if slot_pool else None)

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
            elif own is None and slot_pool:
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
                if status == ghl.STATUS_SCHEDULED and post.scheduled_at and own is None:
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
