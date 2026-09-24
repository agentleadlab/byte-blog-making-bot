"""A second screen: everything RYTE posts in a channel, posted again in its copy.

"Full copy" - every channel RYTE talks in can have a twin in Franklin's own
server, Ryte The Goat: the new-video blog cards, the board walk, the spread,
disputes, recordings, the channel clear-out, the RingCentral cards. What is
posted in the original is posted in the copy; what is edited is edited; and
a message with buttons has them in both, working in both - a press in either
answers it, and afterwards both are greyed out.

Tagging happens in one place only. "Ryte The Goat only": with
DISCORD_TAG_IN_COPIES_ONLY on, a message still reads "@Luna" in the original
but notifies nobody there, and the copy is the one that pings.

Hooked in where every message RYTE sends goes out - `Messageable.send` and
`Message.edit` - rather than at each of the hundred places that post, so a
new feature cannot forget to be copied. Nothing is copied when no pairs are
set, and a copy that fails is logged and never costs the original.
"""

from __future__ import annotations

import asyncio
import io
import logging
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar

import discord

log = logging.getLogger("wilbyte.bot")

#: Original channel id -> the id of its copy.
PAIRS: dict[int, int] = {}
#: Whether the originals stop notifying anybody, the copies doing it instead.
TAGS_IN_COPIES_ONLY = False
#: The bot, for finding the copy channels.
_CLIENT = None
#: Where RYTE's own announcements are copied - updates, "is live" - when
#: that is not where the rest of their channel goes.
ANNOUNCE_INTO: int | None = None
#: Set around one send to copy it somewhere other than its channel's twin.
_INTO: ContextVar = ContextVar("copy_into", default=None)

#: Original message id -> its copy, for edits. The newest few hundred.
_COPIES: "OrderedDict[int, discord.Message]" = OrderedDict()
KEEP_COPIES = 500

_SEND = None
_EDIT = None

#: What a copy leaves out. A reply points at a message in the original
#: channel, which the copy's channel has never heard of.
_NOT_COPIED = ("reference", "mention_author", "nonce", "stickers", "allowed_mentions")


def parse_pairs(said: str) -> dict[int, int]:
    """"111:222, 333:444" as {111: 222, 333: 444}. Anything else is skipped.

    A channel copied into itself, or into a channel that is itself an
    original, is refused - the first posts everything twice in one place,
    and the second is how a copy ends up copying a copy.
    """
    pairs: dict[int, int] = {}
    for part in str(said or "").replace(";", ",").split(","):
        bits = [bit.strip().lstrip("#") for bit in part.replace(">", ":").split(":")]
        if len(bits) != 2 or not all(bit.isdigit() for bit in bits):
            continue
        pairs[int(bits[0])] = int(bits[1])
    # A channel copied into itself is copied into an original - its own.
    return {
        original: copy for original, copy in pairs.items() if copy not in pairs
    }


def configure(client, pairs: dict[int, int], *, tags_in_copies_only: bool = False,
              announce_into=None) -> None:
    global _CLIENT, TAGS_IN_COPIES_ONLY, ANNOUNCE_INTO
    _CLIENT = client
    PAIRS.clear()
    PAIRS.update(pairs)
    TAGS_IN_COPIES_ONLY = bool(tags_in_copies_only and pairs)
    said = str(announce_into or "").strip().lstrip("#")
    ANNOUNCE_INTO = int(said) if said.isdigit() else None
    if pairs or ANNOUNCE_INTO:
        install()


@contextmanager
def copying_into(channel_id):
    """Copy whatever is sent inside this into `channel_id`, instead of into
    the twin of the channel it is sent in. Nothing, when it is None."""
    token = _INTO.set(channel_id)
    try:
        yield
    finally:
        _INTO.reset(token)


def copy_of_message(message_id) -> "discord.Message | None":
    """The copy of one of RYTE's messages, if it was copied."""
    return _COPIES.get(message_id)


def copy_of(channel_id) -> int | None:
    try:
        return PAIRS.get(int(channel_id))
    except (TypeError, ValueError):
        return None


def original_of(channel_id) -> int | None:
    """The channel a copy is a copy of, or None if it is not one."""
    try:
        wanted = int(channel_id)
    except (TypeError, ValueError):
        return None
    return next((original for original, copy in PAIRS.items() if copy == wanted), None)


def install() -> None:
    """Hook in, once."""
    global _SEND, _EDIT
    if _SEND is None:
        _SEND = discord.abc.Messageable.send
        discord.abc.Messageable.send = _send  # type: ignore[assignment]
    if _EDIT is None:
        _EDIT = discord.Message.edit
        discord.Message.edit = _edit  # type: ignore[assignment]


def _files_twice(kw: dict) -> tuple[dict, dict]:
    """The same files for both sends. A discord.File can be sent once."""
    mine, theirs = dict(kw), dict(kw)
    for key in ("file", "files"):
        if kw.get(key) is None:
            continue
        given = kw[key] if key == "files" else [kw[key]]
        read = []
        for one in given:
            try:
                one.fp.seek(0)
            except Exception:
                pass
            read.append((one.fp.read(), one.filename))
        again = [discord.File(io.BytesIO(data), filename=name) for data, name in read]
        other = [discord.File(io.BytesIO(data), filename=name) for data, name in read]
        mine[key] = again if key == "files" else again[0]
        theirs[key] = other if key == "files" else other[0]
    return mine, theirs


async def _send(self, content=None, **kw):
    try:
        channel = await self._get_channel()
    except Exception:
        channel = None
    here = getattr(channel, "id", None)
    copy_id = _INTO.get() or (copy_of(here) if PAIRS else None)
    if copy_id is None or copy_id == here:
        return await _SEND(self, content, **kw)

    mine, theirs = _files_twice(kw)
    if TAGS_IN_COPIES_ONLY and copy_of(here):
        # Still reads "@Luna" - nobody is told.
        mine["allowed_mentions"] = discord.AllowedMentions.none()
    sent = await _SEND(self, content, **mine)
    try:
        where = _CLIENT.get_channel(copy_id) if _CLIENT is not None else None
        if where is None:
            log.warning("Can't find channel %s to copy into", copy_id)
            return sent
        copy = await _SEND(where, content, **{
            key: value for key, value in theirs.items() if key not in _NOT_COPIED
        })
        _remember(sent, copy)
        view = kw.get("view")
        if view is not None and not view.is_finished():
            asyncio.get_running_loop().create_task(_grey_out_after(view, sent, copy))
    except Exception:
        log.warning("Couldn't copy that into channel %s", copy_id, exc_info=True)
    return sent


def _remember(sent, copy) -> None:
    mark = getattr(sent, "id", None)
    if mark is None or copy is None:
        return
    _COPIES[mark] = copy
    while len(_COPIES) > KEEP_COPIES:
        _COPIES.popitem(last=False)


async def _edit(self, **kw):
    if PAIRS and TAGS_IN_COPIES_ONLY and copy_of(getattr(self.channel, "id", None)):
        kw.setdefault("allowed_mentions", discord.AllowedMentions.none())
    edited = await _EDIT(self, **kw)
    copy = _COPIES.get(getattr(self, "id", None))
    if copy is not None:
        try:
            await _EDIT(copy, **{
                key: value for key, value in kw.items()
                if key not in ("attachments", "allowed_mentions")
            })
        except Exception:
            log.warning("Couldn't edit the copy of that message", exc_info=True)
    return edited


async def _grey_out_after(view, *messages) -> None:
    """Once the buttons are answered - from either screen - or time out, both
    copies show them greyed out. Otherwise the one nobody pressed would sit
    there looking live, and pressing it would fail."""
    try:
        await view.wait()
    except Exception:
        return
    for item in getattr(view, "children", []):
        if hasattr(item, "disabled"):
            item.disabled = True
    for one in messages:
        try:
            await _EDIT(one, view=view)
        except Exception:
            log.debug("Couldn't grey out a message's buttons", exc_info=True)
