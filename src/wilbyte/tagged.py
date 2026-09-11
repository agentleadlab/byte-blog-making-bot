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

# A pasted link is not a summary of anything. One went onto the list as
# "[https://chatgpt.com/s/m_6aa1df...](https://chatgpt.com/s" - Discord had
# made half of it a markdown link and cut the rest.
A_LINK = re.compile(r"https?://\S+", re.IGNORECASE)

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
    """One comment on one card, as much of it as the filing needs.

    Or one line of a card's description, which has no id of its own and so
    links to the card - `described` is which of the two it is.
    """

    comment_id: str
    text: str
    author: str = ""
    card_id: str = ""
    card_short: str = ""
    card_title: str = ""
    #: True when this came out of the card's description rather than a comment.
    described: bool = False

    def link(self) -> str:
        """The permalink to this comment, the shape Trello's own copy makes.

        A description line has no anchor to point at, so it points at the card.
        """
        if self.described or not self.comment_id:
            return f"https://trello.com/c/{self.card_short}"
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


# Somebody named at the start of a line, with what follows theirs to do:
#
#     Jenn = FRIDAY
#     • OTP VET removal and consolidation
#     Kath = FRIDAY
#     • MTG creatives and setting up individual ad sets
#
# Arnold writes the week like this and tags nobody. Only at the start of a
# line and only with a colon, an equals or a dash after it: "ask Nicole about
# the budget" names her in passing and is not a job being handed over.
NAMED = re.compile(r"^\s*([A-Za-z][A-Za-z.'\- ]{1,24}?)\s*[:=–—-]\s*(?=\S)", re.MULTILINE)


def named_without_tagging(text: str, checklists) -> dict:
    """{checklist: what follows their name}, for people named but not tagged.

    The lines under a name are theirs until the next name. Somebody who was
    also properly tagged is left to the tag, which carries the whole comment
    rather than one slice of it.
    """
    known = {
        " ".join(str(name).split()).casefold(): " ".join(str(name).split())
        for name in checklists or [] if str(name).strip()
    }
    if not known:
        return {}

    marks = [
        (found.start(), found.end(), known[found.group(1).strip().casefold()])
        for found in NAMED.finditer(text or "")
        if found.group(1).strip().casefold() in known
    ]
    found: dict[str, str] = {}
    for number, (_start, ends, who) in enumerate(marks):
        until = marks[number + 1][0] if number + 1 < len(marks) else len(text or "")
        said = " ".join((text or "")[ends:until].split())
        if said and who not in found:
            found[who] = said
    return found


# An order already running, being topped up. Therese writes "CONNOR SWARTZ
# has an ongoing order that still need to get fulfilled. @nic0l3 kindly bump
# # of leads to his current setup", and the agent's own card is already on
# Nicole's checklist - "if it says ongoing order specifically, dont add".
#
# The words themselves, not the idea: a comment about an agent already on the
# board is most of what gets written on these cards, and the ones asking to
# pause a drip or fix a schedule are real jobs.
ONGOING = re.compile(r"\bon[\s-]?going\s+orders?\b", re.IGNORECASE)


def an_ongoing_order(text: str) -> bool:
    """Whether the comment says in so many words that the order is ongoing."""
    return bool(ONGOING.search(text or ""))


# A bullet, of any of the shapes Trello writes or a person pastes. The glyphs
# are what the rendered card shows - • then ◦ then ▪ going inwards - and they
# carry the nesting on their own when somebody copies the rendered text back in
# with the indentation lost.
BULLET = re.compile(r"^(\s*)(?:([-*+•◦▪‣·–—])|(\d{1,2}[.)]))\s+(.*)$")
GLYPH_DEPTH = {"•": 0, "◦": 1, "▪": 2, "‣": 3}


@dataclass(frozen=True)
class Row:
    """One line of a description, with how far in it sits."""

    depth: int
    text: str
    tags: tuple = ()

    def only_tags(self) -> bool:
        """Whether the line is nothing but the people it names."""
        return bool(self.tags) and not strip_mentions(self.text)


@dataclass(frozen=True)
class Told:
    """One line of a description and the one person it is for."""

    username: str
    text: str


def _rows(text: str) -> list:
    """The description as lines, bullets stripped, nesting as a number."""
    found = []
    for raw in (text or "").splitlines():
        if not raw.strip():
            continue
        bullet = BULLET.match(raw)
        if bullet:
            indent, glyph, _number, said = bullet.groups()
            depth = len(indent.expandtabs(4)) + GLYPH_DEPTH.get(glyph or "", 0)
        else:
            said = raw.strip()
            depth = len(raw[: len(raw) - len(raw.lstrip())].expandtabs(4))
        said = said.strip()
        if not said:
            continue
        found.append(Row(depth=depth, text=said, tags=tuple(mentioned(said))))
    return found


def description_tasks(text: str) -> tuple[list, list[str]]:
    """The jobs written in a card's description. (theirs, nobody's).

    Franklin writes the week into the description rather than the comments,
    with a name and then what that name has to do underneath it:

        @nic0l3 @faithhannahcalla @thereseguba

        • @elisadeko2
          ◦ Aged distro udpate
          ◦ What's login for active campaign
        • @franklinmaymaldonado
          ◦ Continue working on Ai SEO

    The indentation is the ownership. A line that is only a tag opens a block
    and everything under it is that person's; every line in the block is its
    own item, at whatever depth it sits - "on description just add the whole
    thing individually". A sub-point is a job too, and folding it into the line
    above it would lose it.

    Three shapes that are not a job for the person named:

    - The row of tags at the top with nothing under it. That is who the card
      is addressed to, not work for all of them.
    - A tag sitting *underneath* a line, like "@jenniferhashisaki2" under "If
      we're going to turn off, turn off" - the line above is hers.
    - A line with words as well as tags outside any block: the people it names
      have it, the way a comment works.

    Whatever is left over - a line nobody is named for, in a description that
    names people elsewhere - comes back as the second list, to be said out
    loud rather than dropped.
    """
    rows = _rows(text)
    if not any(row.only_tags() for row in rows):
        return [], []

    deeper = [
        number + 1 < len(rows) and rows[number + 1].depth > row.depth
        for number, row in enumerate(rows)
    ]

    owners: list = [()] * len(rows)
    stack: list = []
    for number, row in enumerate(rows):
        while stack and stack[-1][0] >= row.depth:
            stack.pop()
        if row.only_tags():
            if deeper[number]:
                stack.append((row.depth, row.tags))
            continue
        owners[number] = stack[-1][1] if stack else row.tags

    # A tag under a line claims the line above it. Done after the walk because
    # the line was read before its own tag was.
    for number, row in enumerate(rows):
        if not row.only_tags() or deeper[number]:
            continue
        above = next(
            (
                back for back in range(number - 1, -1, -1)
                if rows[back].depth < row.depth and not rows[back].only_tags()
            ),
            None,
        )
        if above is not None:
            owners[above] = row.tags

    theirs, nobody = [], []
    for number, row in enumerate(rows):
        if row.only_tags():
            continue
        said = strip_mentions(row.text)
        if not said:
            continue
        if not owners[number]:
            nobody.append(said)
            continue
        for username in owners[number]:
            theirs.append(Told(username=username, text=said))
    return theirs, nobody


def same_line(one: str, two: str) -> bool:
    """Whether two checklist lines say the same thing.

    Word for word, punctuation and case thrown away. A description line has no
    id to match on the way a comment does, so its own words are what says it
    has been filed already - which is also why they go on the checklist as
    they were written rather than summarised: a summary would come back
    slightly different the next afternoon and file itself twice.
    """
    return _bare(one) == _bare(two) and bool(_bare(one))


def _bare(text: str) -> str:
    return " ".join(re.sub(r"[^0-9a-z]+", " ", (text or "").casefold()).split())


def already_said(said: str, checklists) -> bool:
    """Whether a line with these words is already on one of these checklists."""
    return any(
        same_line(said, _first_line(str(item.get("name") or "")))
        for held in checklists or []
        for item in (held or {}).get("checkItems") or []
    )


def _first_line(text: str) -> str:
    """The words of a checklist item, without the link written under them."""
    return next((line for line in (text or "").splitlines() if line.strip()), "")


def strip_mentions(text: str) -> str:
    """The comment without its tags or its links.

    What is left is the part that says what to do. The link belongs on the
    line - but as the link back to the comment, which is already there, not
    as the words describing the job.
    """
    return " ".join(A_LINK.sub(" ", MENTION.sub(" ", text or "")).split())


def checklist_for(full_name: str, names) -> str:
    """The checklist on a card belonging to this person, or "".

    By first name, because that is how the board is written: the checklists
    say Frank, Kath and Jenn and the members are Franklin May Maldonado,
    Kathleen Rabaya and Jennifer Hashisaki. A checklist whose name starts the
    person's first name is theirs. The longest such name wins, so a board with
    both "Kath" and "Kathleen" on it picks the one that was meant.
    """
    words = (full_name or "").split()
    if not words:
        return ""
    lead = words[0].casefold()
    fits = [
        name for name in names
        if name and (lead.startswith(name.strip().casefold())
                     or name.strip().casefold().startswith(lead))
    ]
    if fits:
        return max(fits, key=len)
    # Or their initials. Kharyl Maye Cañizares keeps a checklist called "KC",
    # which is not the start of her first name and is still hers.
    letters = initials(words)
    return next(
        (name for name in names if name.strip().casefold() in letters), ""
    )


def initials(words) -> set:
    """The short forms of a name somebody might label a checklist with.

    "Kharyl Maye Cañizares" gives kmc and kc - every letter, and the first
    and last. Two letters at least, so a single initial never claims a
    checklist called "K".
    """
    letters = [word[0].casefold() for word in words if word]
    if len(letters) < 2:
        return set()
    found = {"".join(letters)}
    found.add(letters[0] + letters[-1])
    return found


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


def where(person: Person, note: Note | None = None, *, judged: str = "") -> tuple[str, bool]:
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
    """One line for the message that asks whether to write these.

    With a link to the comment it came from. A summary is RYTE's words about
    somebody else's, and "where did you get this" is a fair question to want
    answered before pressing a button rather than after - the filed line
    carries the link and the thing asking permission did not.
    """
    from . import dailyops

    card = dailyops.CARD_KINDS.get(task.kind, task.kind)
    why = ""
    if task.everyone:
        why = " *(@card)*"
    elif task.judged:
        why = " *(my call)*"
    said = "in the description" if task.note.described else "said here"
    where = f" · [{said}]({task.note.link()})" if task.note.card_short else ""
    return f"**{card} · {task.checklist}** — {task.summary}{why}{where}"


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
