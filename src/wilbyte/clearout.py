"""Closing an agent down: their sheet kept, their conversation kept, then gone.

The process, as it is actually done by hand:

    1. Punta ako sa all clients search ko yung name
    2. Dito ko nilalagay sheet nila under "ALL CLIENTS" tab
    3. Punta ulit sa channel and screenshot a portion sa convo and dito ko
       inupload
    4. And delete the channel once saved na lahat

Everything here is pure: matching a name to a channel, deciding what a row
says, and turning messages into something that can be screenshotted. Finding
the channel, writing the row, uploading the picture, banning anybody and
deleting anything all live in `bot.jobs`, behind their own buttons.

The order matters and is not negotiable: the sheet and the picture are kept
*before* the channel goes, because a channel deleted with the sheet link still
only in it is a client nobody can prove anything about afterwards.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import datetime

#: How much of the conversation the picture keeps. Franklin screenshots "a
#: portion" - enough to show the channel was worked rather than the whole of
#: it, which on a year-old channel is thousands of messages.
KEEP_MESSAGES = 40

#: Words a channel of the server's own is made of. A channel whose name is
#: nothing but these is never an agent's, whatever anybody typed - "admin-team"
#: and "general" alike. Deleting one of them is the mistake this is built to
#: make impossible, so the rule is about the whole name rather than a match
#: somewhere in it: an agent called Rule or Grant keeps their channel.
NEVER = {
    "general", "random", "announcement", "announcements", "rule", "rules",
    "welcome", "admin", "team", "staff", "support", "help", "bot", "bots",
    "test", "testing", "lounge", "off", "topic", "chat", "sales", "marketing",
    "leads", "internal", "customer", "service", "department", "payments",
    "payment", "dispute", "disputed", "new", "failed", "blogs", "records",
}


@dataclass
class Channel:
    """One Discord channel, as much of it as the deciding needs."""

    channel_id: str
    name: str
    category: str = ""
    #: When anything was last said in it, or None when that couldn't be told.
    #: A real date rather than something already formatted, because the whole
    #: point of it is comparing one channel against a cutoff and against
    #: another channel.
    last_active: datetime | None = None
    #: False when nothing was ever said in it, in which case `last_active` is
    #: the day it was made. An empty channel from March is exactly what a
    #: clear-out is looking for, so it belongs in the list rather than in the
    #: couldn't-tell pile - but it should not claim somebody spoke in March.
    ever_used: bool = True
    #: Whether RYTE is allowed to read the conversation in it. Discord hands
    #: over every channel in the server whether or not it can be opened, and
    #: when it cannot the history answers 403 - so a channel can be listed as
    #: quiet and still be one nothing can be kept out of.
    readable: bool = True


@dataclass
class Said:
    """One message in a channel, for the picture."""

    who: str
    when: str
    text: str
    attachments: int = 0


@dataclass
class Plan:
    """One agent being closed down, and what is known about them."""

    name: str
    channel: Channel | None = None
    sheet: str = ""
    member_id: str = ""
    member_name: str = ""
    #: Whether the sheet link came out of the channel rather than off a Trello
    #: card. Worth saying: it means the link is about to be deleted along with
    #: the channel, and the row in ALL CLIENTS is the only place it will live.
    from_channel: bool = False
    problems: list = field(default_factory=list)

    @property
    def ready(self) -> bool:
        """Whether there is enough to act on without guessing."""
        return self.channel is not None and not self.problems


def tidy(name: str) -> str:
    """A name reduced to the letters and digits in it, lowercased.

    Channel names carry what Discord does to them - "jay-rodriguez", "jay
    rodriguez ✅", "jayrodriguez1" - and a person types the name as it is
    written on the card.
    """
    return re.sub(r"[^a-z0-9]+", "", (name or "").casefold())


def matches(name: str, channel: Channel) -> bool:
    """Whether this channel is that agent's.

    Both ways round, because a channel is sometimes the name with something
    after it - "jay-rodriguez-vets" - and sometimes shorter than the full
    name on the card.
    """
    wanted, called = tidy(name), tidy(channel.name)
    if not wanted or not called:
        return False
    return wanted in called or called in wanted


def channels_for(name: str, channels) -> list:
    """Every channel that could be this agent's, best first.

    An exact match first, then the ones that merely contain it. More than one
    coming back is the answer, not a failure: which of two channels is theirs
    is a question for somebody who knows, and guessing deletes the wrong one.
    """
    wanted = tidy(name)
    found = [one for one in channels or [] if matches(name, one) and not off_limits(one)]
    return sorted(found, key=lambda one: (tidy(one.name) != wanted, len(one.name)))


def off_limits(channel: Channel) -> bool:
    """Whether this channel is one nobody's clear-out should ever touch."""
    words = tidy_words(channel.name).split()
    return bool(words) and all(word in NEVER for word in words)


def tidy_words(name: str) -> str:
    """A channel name without emoji or separators - "📋 general" -> "general"."""
    said = re.sub(r"[^\w\s-]", " ", (name or ""))
    return " ".join(said.replace("-", " ").replace("_", " ").split()).casefold()


#: A Google Sheet, in the words of a message rather than in a Trello comment.
_A_SHEET = re.compile(
    r"https?://docs\.google\.com/spreadsheets/d/[A-Za-z0-9_-]{20,}[^\s<>\])]*"
)


def sheet_in(messages) -> str:
    """The delivered-leads sheet out of the channel itself, or "".

    Artur Rushiti has no New Agent card anywhere on the board, so the sheet
    was reported as "none found on their card" - while every lead his channel
    has ever carried ends "Check it here:" and the link. The channel is where
    the link actually lives for these, and it is the thing about to be
    deleted.

    The newest one. A client who was set up twice has two, and the one that
    matters is the round they were on when they stopped.
    """
    for one in reversed(list(messages or [])):
        found = _A_SHEET.search(str(getattr(one, "text", "") or ""))
        if found:
            return found.group(0).rstrip(".,;)")
    return ""


def row_for(plan: Plan, *, when: datetime) -> list:
    """The line that goes in the ALL CLIENTS tab before the channel goes.

    The sheet link is the point of it: after the channel is deleted that link
    is the only way back to what was delivered, and it lives in a comment on a
    Trello card that nobody is going to think to look at.
    """
    return [
        plan.name,
        plan.sheet,
        plan.channel.name if plan.channel else "",
        f"{when:%Y-%m-%d}",
    ]


def picture_name(plan: Plan, *, when: datetime) -> str:
    """What the screenshot is called in Drive."""
    safe = re.sub(r"[^\w .-]+", "", plan.name).strip() or "agent"
    return f"{safe} — {when:%Y-%m-%d}.png"


def as_page(plan: Plan, messages: list) -> str:
    """The conversation as a page Chromium can photograph.

    Discord's own colours, because the picture is evidence of a conversation
    and one that looks like the thing it is a picture of is read without
    anybody having to be told what they are looking at.
    """
    lines = []
    for one in messages:
        text = html.escape(one.text or "")
        if one.attachments:
            text += (
                f"<div class='files'>{one.attachments} attachment"
                f"{'s' if one.attachments != 1 else ''}</div>"
            )
        lines.append(
            "<div class='msg'>"
            f"<div class='who'>{html.escape(one.who or 'somebody')}"
            f"<span class='when'>{html.escape(one.when or '')}</span></div>"
            f"<div class='what'>{text or '<em>no text</em>'}</div>"
            "</div>"
        )

    where = html.escape(plan.channel.name if plan.channel else plan.name)
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
  body {{ margin: 0; background: #313338; color: #dbdee1;
         font: 15px/1.45 "Helvetica Neue", Helvetica, Arial, sans-serif; }}
  .head {{ padding: 14px 20px; background: #2b2d31; color: #f2f3f5;
           font-weight: 600; border-bottom: 1px solid #1f2023; }}
  .head span {{ color: #949ba4; font-weight: 400; margin-left: 8px; }}
  .wrap {{ padding: 12px 20px 20px; }}
  .msg {{ padding: 7px 0; }}
  .who {{ color: #f2f3f5; font-weight: 600; }}
  .when {{ color: #949ba4; font-weight: 400; font-size: 12px; margin-left: 8px; }}
  .what {{ white-space: pre-wrap; word-break: break-word; }}
  .files {{ color: #949ba4; font-size: 13px; font-style: italic; }}
</style></head>
<body>
  <div class="head">#{where}<span>{len(messages)} message(s)</span></div>
  <div class="wrap">{"".join(lines) or "<em>Nothing was said in this channel.</em>"}</div>
  <script>document.documentElement.dataset.ready = '1';</script>
</body></html>"""


def describe(plan: Plan) -> str:
    """What was found, to be read before anything irreversible is pressed."""
    if plan.channel is None:
        return (
            f"**{plan.name}** — no channel of theirs in the clients server.\n"
            + ("\n".join(f"⚠ {one}" for one in plan.problems) if plan.problems else "")
        ).strip()

    lines = [f"🧹 **{plan.name}**", f"• Channel — **#{plan.channel.name}**"]
    if plan.channel.category:
        lines.append(f"• Category — {plan.channel.category}")
    if plan.channel.last_active:
        lines.append(f"• Last message — {plan.channel.last_active:%d %b %Y}")
    if plan.sheet:
        lines.append(
            f"• Sheet — {plan.sheet}"
            + (" -# (out of the channel, not off a card)" if plan.from_channel else "")
        )
    else:
        lines.append(
            "• Sheet — **none found**, not on the board and not in the channel"
        )
    lines.append(
        f"• Member — {plan.member_name}" if plan.member_id
        else "• Member — **not in the server**, so there is nobody to ban"
    )
    lines += [f"⚠ {one}" for one in plan.problems]
    return "\n".join(lines)


# ------------------------------------------------- which channels have gone quiet

#: How long a channel says nothing before it is worth looking at. Franklin's
#: own figure: "give me list of inactive channel for 2-3months".
QUIET_MONTHS = 2

#: A month, for the purpose of "two months ago". Nobody closing a channel down
#: cares whether February was short.
DAYS_A_MONTH = 30

#: "2 months", "3mo", "90 days", "10 weeks" - however it is typed.
HOW_LONG = re.compile(
    r"(\d+)\s*(month|mo|week|wk|day)s?\b", re.IGNORECASE
)

#: Discord's own limit is 2000. Enough room under it for the heading and the
#: footer that go around the lines.
ROOM = 1700


def how_far_back(text: str, *, months: int = QUIET_MONTHS) -> int:
    """How many days back "2 months" means, defaulting when nothing is said.

    Typing nothing is the common case - `@RYTE quiet` - and it should give
    the answer rather than a question about units.
    """
    found = HOW_LONG.search(text or "")
    if not found:
        return months * DAYS_A_MONTH
    many = max(1, int(found.group(1)))
    unit = found.group(2).casefold()
    if unit.startswith("d"):
        return many
    if unit.startswith("w"):
        return many * 7
    return many * DAYS_A_MONTH


def how_long(channel: Channel, *, now: datetime) -> str:
    """How long this channel has been quiet, in the roundest honest terms."""
    if channel.last_active is None:
        return "not known"
    days = max(0, (now - channel.last_active).days)
    if days < 14:
        said = f"{days} day{'s' if days != 1 else ''}"
    elif days < 60:
        said = f"{days // 7} weeks"
    else:
        said = f"{days // DAYS_A_MONTH} months"
    return f"nothing ever said, made {said} ago" if not channel.ever_used else said


def quiet_ones(channels, *, since: datetime) -> tuple:
    """(gone quiet, the server's own, couldn't tell), quietest first.

    Three lists rather than one, because a channel nobody can date is not a
    channel that is busy, and dropping it silently is how a list that looks
    complete stops being one.
    """
    quiet, ours, unknown = [], [], []
    for one in channels or []:
        if off_limits(one):
            ours.append(one)
        elif one.last_active is None:
            unknown.append(one)
        elif one.last_active < since:
            quiet.append(one)
    quiet.sort(key=lambda one: one.last_active)
    return quiet, ours, unknown


#: How many of them can be offered at once. Discord's own limit on a dropdown
#: - and on buttons, which is what it was going to be - so the list is capped
#: to match rather than listing forty and letting fifteen of them sit there
#: unpickable: "then do 25 each quiet? then i run again".
PICKABLE = 25


def pick_from(quiet, *, now: datetime, most: int = PICKABLE) -> list:
    """(channel name, how long it has been quiet) for the ones on offer.

    Only the ones that can be read. A clear-out of a channel whose history
    answers 403 stops at the first button with nothing kept, so offering it is
    offering a press that cannot go anywhere.
    """
    return [
        (one.name, how_long(one, now=now))
        for one in quiet[:most] if one.readable
    ]


def describe_quiet(
    quiet, ours, unknown, *, since: datetime, now: datetime, most: int = PICKABLE
) -> list:
    """The list, as however many messages Discord will take.

    Capped at what can be offered in one go, and the rest counted rather than
    listed. An earlier version listed all of them over several messages, which
    was right when the only way to act on one was to type its name; now that
    the list carries a picker, naming a channel it cannot offer is naming one
    somebody has to type out after all.
    """
    days = max(0, (now - since).days)
    shown, over = list(quiet[:most]), max(0, len(quiet) - most)
    head = (
        f"🕸 **{len(quiet)} channel{'s' if len(quiet) != 1 else ''}** with nothing "
        f"said since **{since:%d %b %Y}** ({days // DAYS_A_MONTH} months)"
    )
    if not quiet:
        head = (
            f"Nothing has been quiet since **{since:%d %b %Y}** "
            f"({days // DAYS_A_MONTH} months) — every channel has been used."
        )

    lines = [
        f"• **#{one.name}** — {how_long(one, now=now)}"
        + (f", {one.category}" if one.category else "")
        + ("" if one.readable else " — 🔒 **can't read it**")
        for one in shown
    ]
    shut = [one for one in shown if not one.readable]

    tail = []
    if shut:
        tail.append(
            f"🔒 **{len(shut)} of these can't be opened** — Discord answers 403 "
            "on their history, so there is no conversation to keep a picture "
            "of and nothing to clear out. They need **View Channel** and "
            "**Read Message History** for RYTE's role, on the channel or on "
            "the category above it."
        )
    if over:
        tail.append(
            f"-# …and {over} more. Clear some of these and run `@RYTE quiet` "
            "again for the rest."
        )
    if ours:
        tail.append(
            f"-# Left out {len(ours)} of the server's own: "
            + ", ".join(f"#{one.name}" for one in ours[:6])
            + (", …" if len(ours) > 6 else "")
        )
    if unknown:
        tail.append(
            f"⚠ Couldn't tell when {len(unknown)} "
            + ("was" if len(unknown) == 1 else "were")
            + " last used: "
            + ", ".join(f"#{one.name}" for one in unknown[:6])
            + (", …" if len(unknown) > 6 else "")
        )
    tail.append(
        "-# Nothing here is deleted. Pick one below, or `@RYTE clearout "
        "<name>` — either way it asks twice."
    )

    return _pages(head, lines, tail)


def _pages(head: str, lines, tail) -> list:
    """One message if it fits, several if it doesn't. Never a cut-off list."""
    out, now_saying = [], head
    for line in lines:
        if len(now_saying) + len(line) + 1 > ROOM:
            out.append(now_saying)
            now_saying = line
        else:
            now_saying = f"{now_saying}\n{line}"
    out.append(now_saying)

    for one in tail:
        if len(out[-1]) + len(one) + 2 > ROOM:
            out.append(one)
        else:
            out[-1] = f"{out[-1]}\n{one}"
    return out
