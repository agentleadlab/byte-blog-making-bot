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
    last_active: str = ""


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
        lines.append(f"• Last message — {plan.channel.last_active}")
    lines.append(
        f"• Sheet — {plan.sheet}" if plan.sheet
        else "• Sheet — **none found on their card**"
    )
    lines.append(
        f"• Member — {plan.member_name}" if plan.member_id
        else "• Member — **not in the server**, so there is nobody to ban"
    )
    lines += [f"⚠ {one}" for one in plan.problems]
    return "\n".join(lines)
