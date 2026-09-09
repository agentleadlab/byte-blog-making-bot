"""Tagging somebody in a comment, turned into a line on their checklist.

People tag each other on the day's cards and then forget to put the item on
a checklist, so the ask exists in the comments and nowhere anybody looks. The
comment is the record; the checklist is the work. This closes the gap.

    @franklinmaymaldonado Add in gcalendar lunch + walk

becomes, on Frank's checklist:

    Add in gcalendar lunch + walk
    https://trello.com/c/IU4PM7wJ#comment-6aa172b99d24cc82d714f661

Summary first, then the comment it came from. The link is also how RYTE knows
he has already done one: the comment's id is in it, so a second look at the
same card finds the line and leaves it alone.

Which card it lands on is not the card it was said on. Therese is tagged on
the General card all day and her work is Ops, so it goes on Ops - "if Therese
is tag on a comment on general card, you add them on the OPS card". The rule
that gets there without guessing: a person who keeps a checklist on only one
of the day's cards has no ambiguity to resolve, so their work goes there. Only
somebody who keeps several - Nicole is on Ads and on General - needs the
comment read to decide, and then it is ads work to Ads and admin to General.

Everything here is pure. Reading the board and writing to it live in
`bot.jobs`; this decides what should happen.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# `@card` tags the card rather than a person, and it means everybody on it:
# Faith's "@card add states for Jordan Kissinger" on the Ads card is a job for
# Jenn, Kath and Nicole. No routing for these - the card it was said on is the
# card it belongs to, because that is what tagging the card says.
EVERYONE = "card"

# Tags that name nobody and mean nothing here.
NOT_A_PERSON = {"board", "here", "everyone", "channel"}

# A Trello mention. Usernames are lowercase letters, digits and underscores -
# `@nic0l3`, `@franklinmaymaldonado`, `@jenniferhashisaki2`.
MENTION = re.compile(r"@([a-z0-9_]{2,64})", re.IGNORECASE)

# How long a summary is allowed to get before it stops being one. Franklin's
# own lines are three to six words: "make BC BENEFITS LP", "Add in gcalendar
# lunch + walk", "Blue collar agents go live".
MOST_WORDS = 9
MOST_CHARACTERS = 80

# A comment already this short is its own summary. Rewriting "Add YT channel"
# into something briefer is not possible and not wanted.
ALREADY_BRIEF = 8

# The day's cards a tagged item can land on, and what kind of work each holds.
# Lead Order is not here: what goes on it is what agents bought, written by the
# spread off the setup card, and a task is not an order.
WORK = {
    "ops": "setting agents up, distro hub, sheets, sending leads",
    "ads": "campaigns, creatives, budgets, pausing and unpausing, going live",
    "general": "admin, executive, anything that is not ops or ads work",
}
FALLBACK = "general"


@dataclass(frozen=True)
class Note:
    """One comment on one card, as much of it as the filing needs."""

    comment_id: str
    text: str
    author: str = ""
    card_id: str = ""
    card_short: str = ""
    card_title: str = ""

    def link(self) -> str:
        """The permalink to this comment, the shape Trello's own copy makes."""
        return f"https://trello.com/c/{self.card_short}#comment-{self.comment_id}"


@dataclass(frozen=True)
class Person:
    """A board member and the checklists they keep on the day's cards."""

    username: str
    full_name: str = ""
    #: kind -> the checklist's name on that kind of card, e.g. {"ops": "Therese"}
    keeps: dict = field(default_factory=dict)
    #: The work they actually do - "ops", "ads", or "" for neither.
    home: str = ""

    def first_name(self) -> str:
        return (self.full_name or self.username).split()[0] if (
            self.full_name or self.username
        ).strip() else ""


@dataclass
class Task:
    """One line to add, and the card and checklist it goes on."""

    note: Note
    kind: str
    checklist: str
    card_id: str
    card_title: str
    summary: str = ""
    person: Person | None = None
    #: True when the kind was judged rather than read off a single checklist.
    judged: bool = False
    #: True when it came from `@card` and so belongs to everybody on it.
    everyone: bool = False

    def item(self) -> str:
        """What goes on the checklist, summary then the comment it came from."""
        return f"{self.summary}\n{self.note.link()}".strip()


def mentioned(text: str) -> list[str]:
    """The usernames tagged in a comment, in the order they appear, once each.

    `@card` is not among them - it names everybody rather than somebody, and
    `everyones_job` is the question to ask about it.
    """
    found: list[str] = []
    for name in MENTION.findall(text or ""):
        low = name.casefold()
        if low in NOT_A_PERSON or low == EVERYONE or low in found:
            continue
        found.append(low)
    return found


def everyones_job(text: str) -> bool:
    """Whether the comment tags the card itself, meaning everybody on it."""
    return any(name.casefold() == EVERYONE for name in MENTION.findall(text or ""))


def strip_mentions(text: str) -> str:
    """The comment without its tags, which is the part that says what to do."""
    return " ".join(MENTION.sub(" ", text or "").split())


def checklist_for(full_name: str, names) -> str:
    """The checklist on a card belonging to this person, or "".

    By first name, because that is how the board is written: the checklists
    say Frank, Kath and Jenn and the members are Franklin May Maldonado,
    Kathleen Rabaya and Jennifer Hashisaki. A checklist whose name starts the
    person's first name is theirs. The longest such name wins, so a board with
    both "Kath" and "Kathleen" on it picks the one that was meant.
    """
    first = (full_name or "").split()
    if not first:
        return ""
    lead = first[0].casefold()
    fits = [
        name for name in names
        if name and (lead.startswith(name.strip().casefold())
                     or name.strip().casefold().startswith(lead))
    ]
    return max(fits, key=len) if fits else ""


def home_for(checklists) -> str:
    """The work a person does, from the checklists they keep. "" for neither.

    Therese keeps one on General as well as on Ops, so "the only card they are
    on" does not answer it - the board says who does what, in the same names
    the agent filing already uses. Ops beats Ads if somebody is somehow both:
    Ops is a smaller job and a line put there is noticed.
    """
    from . import agents

    held = {str(name).strip().casefold() for name in checklists or []}
    if held & {name.casefold() for name in agents.OPS_PEOPLE}:
        return "ops"
    if held & {name.casefold() for name in agents.ADS_PEOPLE}:
        return "ads"
    return ""


def where(person: Person, note: Note, *, judged: str = "") -> tuple[str, bool]:
    """Which kind of card this task belongs on. (kind, was it my call).

    The work decides, not the card it was said on. Therese does Ops, so
    anything for Therese goes on Ops wherever it was written - "if Therese is
    tag on a comment on general card, you add them on the OPS card not on
    general card check list". Nothing to weigh up there.

    Ads is the one that can go two ways: Nicole does ads work and admin both,
    so "if nicole gets tag on General, if its ads related, it will go to ads /
    if its admin/executive comments it will be on general". Her own card is
    the default and admin is the exception, so the reading only ever has to
    make the case for General.

    Anybody who is neither - Frank, Faith - is on General, which is where
    admin lives.

    `judged` is what reading the comment said, passed in rather than worked
    out here, because reading it is not a pure thing.
    """
    keeps = person.keeps or {}
    if not keeps:
        return "", True

    if person.home == "ops" and "ops" in keeps:
        return "ops", False
    if person.home == "ads" and "ads" in keeps:
        if judged == FALLBACK and FALLBACK in keeps:
            return FALLBACK, True
        return "ads", False

    if len(keeps) == 1:
        return next(iter(keeps)), False
    if judged in keeps:
        return judged, True
    if FALLBACK in keeps:
        return FALLBACK, True
    return next(iter(keeps)), True


def already_on(note: Note, checklist) -> bool:
    """Whether this comment is already a line on one checklist.

    By the comment's id rather than by the summary: the id is exact and the
    wording is not, and somebody who typed the line themselves worded it their
    own way.
    """
    if not note.comment_id:
        return False
    return any(
        note.comment_id in str(item.get("name") or "")
        for item in (checklist or {}).get("checkItems") or []
    )


def already_filed(note: Note, checklists) -> bool:
    """Whether it is on any of these checklists.

    Used for a tag on a person, where the line lands on one checklist out of
    all of today's - so anywhere counts, including a list somebody put it on
    by hand. `@card` asks per checklist instead, because it belongs on all of
    them and finding it on Kath's is no reason to skip Nicole's.
    """
    return any(already_on(note, held) for held in checklists or [])


def trim(text: str) -> str:
    """A summary from the comment itself, for when nothing cleverer is around.

    The first sentence, cut to a line's worth. Deliberately dumb: it is the
    floor under the written summary, not a replacement for it, and a floor
    that invents nothing is worth more than one that guesses.
    """
    said = strip_mentions(text)
    if not said:
        return ""
    first = re.split(r"(?<=[.!?])\s+|\n", said, maxsplit=1)[0].strip()
    words = first.split()
    if len(words) > MOST_WORDS:
        first = " ".join(words[:MOST_WORDS])
    if len(first) > MOST_CHARACTERS:
        first = first[:MOST_CHARACTERS].rsplit(" ", 1)[0]
    return first.strip(" -–—:,")


def brief_already(text: str) -> bool:
    """Whether the comment is short enough to be its own summary."""
    said = strip_mentions(text)
    return bool(said) and len(said.split()) <= ALREADY_BRIEF


def describe(task: Task) -> str:
    """One line for the message that asks whether to write these."""
    from . import dailyops

    card = dailyops.CARD_KINDS.get(task.kind, task.kind)
    why = ""
    if task.everyone:
        why = " *(@card)*"
    elif task.judged:
        why = " *(my call)*"
    return f"**{card} · {task.checklist}** — {task.summary}{why}"


def summary_prompt(notes: list[Note], people: dict) -> str:
    """What to ask Claude about a batch of comments.

    A batch rather than one at a time: the comments on a card are about each
    other, and reading them together is how "same with trucker lp" means
    anything at all.
    """
    kinds = "\n".join(f"- {kind}: {what}" for kind, what in WORK.items())
    lines = []
    for note in notes:
        who = ", ".join(
            people[name].full_name or name
            for name in mentioned(note.text) if name in people
        )
        lines.append(
            f"[{note.comment_id}] on “{note.card_title}”, "
            f"by {note.author or 'somebody'}, tagging {who or 'nobody'}:\n"
            f"{note.text.strip()}"
        )
    return (
        "These are comments on a lead-generation team's daily Trello cards. "
        "Somebody was tagged in each one, which means it is a job for them.\n\n"
        "For each comment give me two things:\n"
        "1. summary — what the tagged person has to do, in their own words "
        f"where possible. At most {MOST_WORDS} words. No trailing full stop. "
        "If the comment is already short, use it as it is.\n"
        "2. kind — which of these the work belongs to:\n" + kinds + "\n\n"
        "Comments:\n\n" + "\n\n".join(lines)
    )
