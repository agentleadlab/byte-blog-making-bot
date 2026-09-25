"""Closing an agent down: their sheet kept, their conversation kept, then gone.

The process, as it is actually done by hand:

    1. Punta ako sa all clients search ko yung name
    2. Dito ko nilalagay sheet nila under "ALL CLIENTS" tab
    3. Punta ulit sa channel and screenshot a portion sa convo and dito ko
       inupload
    4. And delete the channel once saved na lahat

Everything here is pure: matching a name to a channel, deciding what a row
says, and laying the conversation out to be posted back. Finding the channel,
writing the row, banning anybody and deleting anything all live in `bot.jobs`
and `bot.client`, behind their own buttons.

RYTE photographed the conversation into Drive for a while. It was not good
enough - "just forward them to me, then ill screenshot then you collect sheet
and delete" - and the person doing the screenshotting knows what is worth
keeping in a way a rule about the last forty messages never will.

The order matters and is not negotiable: the sheet is written and the
conversation is posted *before* the channel goes, because a channel deleted
with the sheet link still only in it is a client nobody can prove anything
about afterwards.
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

#: How far back to look in a channel, for the handful of things this one
#: client said in it. A thousand because three hundred was not enough:
#: ring-da-bell carries the whole server's wins, and a client who stopped
#: buying four months ago is a long way down it.
LOOK_BACK = 1000

#: How much of the *start* of a channel to read. A client channel opens with
#: the conversation - the welcome, the questions, what they wanted - and then
#: a year of the lead feed buries it. Reading backwards from today never
#: reaches that however far back it goes, and it is the part somebody would
#: screenshot by hand.
FIRST_OF_IT = 60

#: How much of the lead feed to post when there is nothing else. Enough to
#: show what was delivered and that the sheet link was in every one of them,
#: few enough to screenshot in one go.
FEED_SHOWN = 4

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
    # The wins channel. Everybody posts their sales in it, so it is nobody's
    # to delete - and it is where a client's own words about the leads are,
    # which is what the picture before a clear-out is for.
    "ring", "da", "bell", "wins", "win", "vault", "the",
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
    #: Whether a bot wrote it. The lead feeds post every lead into the
    #: client's own channel, so the last forty messages there are forty lead
    #: records rather than anything the client said - "only the human
    #: messages, not the bot feed". Kept on the message rather than filtered
    #: on the way in, because the sheet link is only ever in a bot's post and
    #: that still has to be read.
    by_bot: bool = False
    #: When it was said, for putting messages from several channels in order.
    at: datetime | None = None
    #: Which channel it was said in, named on the line when it was not this
    #: client's own - a sale posted in ring-da-bell is worth showing as having
    #: been posted there.
    where: str = ""
    #: The reactions on it, as "❤️ 4 · 🔥 2". Half of what a sale looks like
    #: in ring-da-bell is the team piling onto it, and a picture of the post
    #: without them is not what was sent.
    reactions: str = ""
    #: Their profile picture, as a url Chromium can fetch. A Discord message
    #: without one does not look like a Discord message.
    avatar: str = ""
    #: Who wrote it, and who it tagged, by Discord id - for knowing who the
    #: client actually is without going by names at all.
    author_id: str = ""
    mentions: tuple = ()


@dataclass
class Plan:
    """One agent being closed down, and what is known about them."""

    name: str
    channel: Channel | None = None
    sheet: str = ""
    member_id: str = ""
    member_name: str = ""
    #: The server the channel is in, for the link that opens it. Kept on the
    #: plan rather than built where it is shown: a jump link needs the server
    #: id as well as the channel's, and the one place that knows both is where
    #: the channel was found.
    guild_id: str = ""
    #: Worth saying, not in the way: a different sheet on their Trello card.
    notes: list = field(default_factory=list)
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


#: What a client channel's name carries after the person: "justin_schwartz-
#: otp-vet", "mujeeb_anwari-standard-vet", "joseph_temple-fb-iul".
LEAD_ENDINGS = frozenset(
    "vet vets veteran fex iul otp standard std mtg mp mortgage widows widow "
    "spanish trucker fb aged fresh plus phnx phoenix uprise ta wle term life "
    "leads lead".split()
)


def words_of(name: str) -> list[str]:
    """A name as its words, lowercased, the lead-type words off the end.
    "dylan_rankin-vet" -> ["dylan", "rankin"]; "🔥 Dylan Rankin" the same."""
    words = [re.sub(r"[^a-z0-9]", "", one) for one in tidy_words(name).split()]
    words = [one for one in words if one]
    while len(words) > 1 and words[-1] in LEAD_ENDINGS:
        words.pop()
    return words


def person_in(name: str) -> str:
    """The person's name in a client channel's name, tidied - the lead-type
    words off the end. "dylan_rankin-vet" -> "dylanrankin"."""
    return "".join(words_of(name))


def _run(part: list, whole: list) -> bool:
    """Whether `part` sits in `whole` word for word, in one piece."""
    return any(whole[at:at + len(part)] == part for at in range(len(whole) - len(part) + 1))


def name_match(wanted: str, said: str) -> int:
    """How well a name matches the one wanted, as written. 0 is not a match.

    Word by word, never letters inside a word. 3 the same name; 2 theirs
    holds the whole of it - "Dylan Rankin Jr", or the username
    "dylanrankin_0523" that is the name run together with digits on; 1 it
    holds theirs, when theirs is two words or more and most of it.

    Never a scrap and never one word typed alone. A member called "D" is
    inside "dylanrankin" and "demetriosbrooks" both, and was taken for each
    of them - his messages from 2024 kept as theirs, Dylan's own sales never
    looked for. "Ann Smith" is not "Joann Smith", and "Jay" is not every Jay.
    """
    w, s = words_of(wanted), words_of(said)
    if not w or not s:
        return 0
    jw, js = "".join(w), "".join(s)
    if jw == js:
        return 3
    if len(w) >= 2 and (_run(w, s) or (js.startswith(jw) and js[len(jw):].isdigit())):
        return 2
    if len(s) >= 2 and _run(s, w) and len(js) * 3 >= len(jw) * 2:
        return 1
    return 0


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
    wanted, person = tidy(name), person_in(name)
    open_ = [one for one in channels or [] if not off_limits(one)]
    # In tiers, and only the best tier that has anything in it: the channel
    # called exactly that; then the channels of that person, whatever lead
    # type is on the end; then a close match - never a scrap of a name. A run
    # through the quiet list names the channel exactly, so it gets exactly
    # that one, not every other channel the same person has.
    exact = [one for one in open_ if tidy(one.name) == wanted]
    # A channel's own name - no spaces, as the quiet list and the picker pass
    # it - is that channel and nothing else. A person's name typed out is
    # every channel of theirs, and two of them is a question for whoever
    # typed it, not for RYTE.
    if exact and not re.search(r"\s", (name or "").strip()):
        return exact
    theirs = [one for one in open_ if person and person_in(one.name) == person]
    for tier in (
        exact + [one for one in theirs if one not in exact],
        [one for one in open_ if name_match(name, one.name)],
    ):
        if tier:
            return sorted(tier, key=lambda one: (tidy(one.name) != wanted, len(one.name)))
    return []


def off_limits(channel: Channel) -> bool:
    """Whether this channel is one nobody's clear-out should ever touch."""
    words = tidy_words(channel.name).split()
    return bool(words) and all(word in NEVER for word in words)


def the_one_to_delete(plan: Plan, channel, *, guild_id) -> str:
    """"" when this channel may be deleted, or why it may not.

    Checked again here, at the moment of deleting, rather than trusted from
    the looking-up half an hour earlier: "MAKE SURE HE DOESNT DELETING
    ANYTHING ELSE OUTSIDE THE CLIENTS CHANNEL".

    Four things, and all four have to hold: there is a plan with a channel on
    it; this is that exact channel by id; it is in the clients server; and its
    name is not one of the server's own. Nothing about what was read - RYTE
    now reads ring-da-bell and the other shared channels to find what the
    client said in them, and reading a channel must never be a step towards
    deleting it.
    """
    if plan.channel is None:
        return "there is no channel on the plan"
    if channel is None:
        return "that channel is not there any more"

    was, now = str(plan.channel.channel_id or ""), str(getattr(channel, "id", "") or "")
    if not was or was != now:
        return f"#{getattr(channel, 'name', '?')} is not the channel this was for"

    where = str(getattr(getattr(channel, "guild", None), "id", "") or "")
    if where and str(guild_id or "") and where != str(guild_id):
        return f"#{getattr(channel, 'name', '?')} is in another server"

    called = str(getattr(channel, "name", "") or "")
    if off_limits(Channel(channel_id=now, name=called)):
        return f"#{called} is one of the server's own"
    return ""


def tidy_words(name: str) -> str:
    """A channel name without emoji or separators - "📋 general" -> "general"."""
    said = re.sub(r"[^\w\s-]", " ", (name or ""))
    return " ".join(said.replace("-", " ").replace("_", " ").split()).casefold()


#: A Google Sheet, in the words of a message rather than in a Trello comment.
_A_SHEET = re.compile(
    r"https?://docs\.google\.com/spreadsheets/d/[A-Za-z0-9_-]{20,}[^\s<>\])]*"
)


def same_sheet(one: str, other: str) -> bool:
    """Whether two sheet links are the same spreadsheet, whatever tab or
    tracking tail each carries."""
    ids = [re.search(r"/spreadsheets/d/([A-Za-z0-9_-]{20,})", str(link or "")) for link in (one, other)]
    return all(ids) and ids[0].group(1) == ids[1].group(1)


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


#: The tab a clear-out writes to, by name. Named for RYTE on purpose - "that
#: way we know if hes the one deleting stuff" - so a row in it is a row RYTE
#: put there and a row anywhere else in that spreadsheet is somebody's own.
COLLECTION_TAB = "Ryte Collection"

#: What each column of it wants, by what its heading sounds like. Against the
#: tab's own headings rather than in a fixed order: the chargeback tracker
#: taught that lesson the expensive way, writing a name under "Closer".
COLLECTS = (
    ("name", r"name|client|agent|who"),
    ("sheet", r"sheet|link|drive|doc"),
    ("channel", r"channel|discord"),
    ("when", r"date|closed|cleared|removed|when|added"),
)


def collected(plan: Plan, *, when: datetime) -> dict:
    """What is known about one clear-out, by what each thing is.

    The sheet link is the point of it: after the channel is deleted that link
    is the only way back to what was delivered, and it lives in a comment on a
    Trello card that nobody is going to think to look at.
    """
    return {
        "name": plan.name,
        "sheet": plan.sheet,
        "channel": plan.channel.name if plan.channel else "",
        "when": f"{when:%Y-%m-%d}",
    }


def read_rows(rows: list) -> list[dict]:
    """The Ryte Collection tab back as clear-outs, by its own headings - the
    same patterns it was written by. A tab with no recognised headings is
    read in the order rows have always been written in."""
    head = [" ".join(str(one or "").split()).casefold() for one in rows[0]] if rows else []
    where = {}
    for at, heading in enumerate(head):
        for name, pattern in COLLECTS:
            if heading and name not in where and re.search(pattern, heading, re.IGNORECASE):
                where[name] = at
                break
    body = rows[1:] if where else rows
    if not where:
        where = {name: at for at, (name, _pattern) in enumerate(COLLECTS)}
    found = []
    for row in body:
        got = {name: (str(row[at]).strip() if at < len(row) else "") for name, at in where.items()}
        if got.get("name") or got.get("channel"):
            found.append(got)
    return found


def seen_in(messages, member_id) -> bool:
    """Whether this member wrote in the client's channel or was tagged in it.

    The channel is the client's by definition, and Faith's welcome tags them
    in it - "Hey @Dylan Rankin this will be the primary channel". A member
    found by name who never appears there is a name that happens to fit, and
    what they said elsewhere is not kept as the client's.
    """
    wanted = str(member_id or "")
    if not wanted:
        return False
    return any(
        str(getattr(one, "author_id", "") or "") == wanted
        or wanted in {str(tag) for tag in getattr(one, "mentions", ()) or ()}
        for one in messages or []
    )


def old_member(members, name: str):
    """Who the clear-out matched before names had to match - the first
    member whose tidied name was inside the one wanted, or the other way
    round. Kept to find the clear-outs it got wrong, and for nothing else."""
    wanted = tidy(name)
    if not wanted:
        return None
    for member in members:
        for called in (
            getattr(member, "display_name", ""),
            getattr(member, "global_name", "") or "",
            getattr(member, "name", ""),
        ):
            said = tidy(called)
            if said and (said == wanted or wanted in said or said in wanted):
                return member
    return None


def row_for(plan: Plan, *, when: datetime, headings=None) -> list:
    """That, as a row laid out for the tab it is going in.

    No headings, or none of them recognised, and it goes in the order it has
    always gone in - a tab with nothing at the top of it is still a tab
    somebody reads, and refusing to write is worse than writing four cells in
    the obvious order.
    """
    have = collected(plan, when=when)
    if not headings:
        return list(have.values())
    row, used = [], False
    for heading in headings:
        said = " ".join(str(heading or "").split()).casefold()
        name = next(
            (name for name, pattern in COLLECTS
             if said and re.search(pattern, said, re.IGNORECASE)),
            "",
        )
        row.append(have.get(name, ""))
        used = used or bool(name)
    return row if used else list(have.values())


def for_the_picture(theirs: list, elsewhere: list = ()) -> list:
    """The messages worth keeping: what people said, in order.

    Bots left out. The lead feeds post every lead into the client's own
    channel, so the last forty messages there were forty lead records - the
    goods, not the conversation, and none of it in their words.

    What they said in the channels the whole server shares goes in too: a sale
    posted in ring-da-bell is the client saying the leads worked, which is
    exactly what is worth keeping before closing them down.
    """
    found = [one for one in list(theirs) + list(elsewhere) if not one.by_bot]
    dated = [one for one in found if one.at is not None]
    undated = [one for one in found if one.at is None]
    dated.sort(key=lambda one: one.at)
    return (undated + dated)[-KEEP_MESSAGES:]


def the_feed(messages: list, *, most: int = FEED_SHOWN) -> list:
    """The last of the lead feed, for a channel that holds nothing else.

    Filtering the bots out is right when there is a conversation underneath
    them. When there is not, it leaves nothing at all - and the feed is what
    was delivered, which is the thing worth keeping about a channel like that.
    """
    return [one for one in list(messages) if one.by_bot][-most:]


def to_screenshot(plan: Plan, messages: list, feed: list = ()) -> list:
    """The conversation as messages to post, for somebody to screenshot.

    RYTE used to photograph this itself, into Drive. It was not good enough -
    "just forward them to me, then ill screenshot then you collect sheet and
    delete" - and the person doing the screenshotting knows what is worth
    keeping in a way a rule about the last forty messages never will.

    Quoted rather than plain, so a screenshot of it reads as a conversation
    that was had rather than as something RYTE wrote.
    """
    where = plan.channel.name if plan.channel else plan.name
    # The link rather than a mention. A mention renders as the channel's name
    # only for somebody who can see it, and the one message that most needs
    # this says "go and look before deleting it".
    link = f" — {jump_to(plan)}" if plan.channel else ""
    head = (
        f"🧹 **#{where}** — {len(messages)} message"
        f"{'s' if len(messages) != 1 else ''}. Screenshot what you want."
    )
    if not messages and feed:
        # Nobody said anything, so the feed is the record. It is what was
        # delivered, and every post in it carries the sheet link - which is
        # the thing worth screenshotting about a channel like this one.
        head = (
            f"🧹 **#{where}**{link} — nobody said anything in it, so here is "
            f"the last of what was delivered. Screenshot what you want."
        )
        messages = feed
    elif not messages:
        # The channel itself, to go and look in. RYTE reading a thousand
        # messages and finding none is not the same as there being none, and
        # the person about to delete it should be able to check rather than
        # take that on trust.
        return [
            f"🧹 **#{where}**{link} — nothing in the {LOOK_BACK} newest or "
            f"the {FIRST_OF_IT} oldest messages, so there is nothing to post. "
            "Open it and look before deleting it."
        ]

    lines = []
    for one in messages:
        said_in = f" · #{one.where}" if one.where and one.where != where else ""
        lines.append(
            f"> **{one.who or 'somebody'}** · {one.when}{said_in}\n"
            + "\n".join(f"> {row}" for row in (one.text or "—").splitlines())
            + (
                f"\n> -# {one.attachments} attachment"
                f"{'s' if one.attachments != 1 else ''}"
                if one.attachments else ""
            )
            # A blank line after each, or Discord runs consecutive quoted
            # lines into one block and the whole conversation reads as having
            # been said by whoever is at the top of it.
            + "\n"
        )
    return _pages(head, lines, [])


def by_channel(messages: list) -> list:
    """The messages grouped by where they were said. [(channel, messages)].

    One picture per channel rather than one tall picture of everything -
    "so itll be like 3 ss in total or smthing like that". A client's own
    channel and the sales they rang in ring-da-bell are two different
    screenshots to the person who would otherwise have taken them by hand,
    and a single image with both in it is not a picture of either.

    In the order the channels first appear, which - the messages arriving
    sorted by when they were said - is the order they were first used.
    """
    groups: dict[str, list] = {}
    for one in messages:
        groups.setdefault(str(getattr(one, "where", "") or ""), []).append(one)
    return list(groups.items())


def to_draw(plan: Plan, messages: list, elsewhere: list = ()) -> list:
    """What to photograph, as one picture per channel. [(channel, messages)].

    Their own channel always gets one, even when the only thing ever posted
    in it was the lead feed. It is the channel being deleted - ring-da-bell
    is not - so it is the one that ends up with no record at all otherwise,
    and a run that photographed three sales out of a channel nobody is
    touching and nothing out of the one about to go is backwards.

    Theirs first, then wherever else they spoke, oldest first. Picture 1 is
    always the channel this clear-out is about.
    """
    mine = plan.channel.name if plan.channel else ""
    groups = by_channel(for_the_picture(messages, elsewhere))
    if mine and not any(where == mine for where, _ in groups):
        feed = the_feed(messages)
        if feed:
            groups.append((mine, feed))
    groups.sort(key=lambda one: 0 if mine and one[0] == mine else 1)
    return groups


def their_folder(plan: Plan) -> str:
    """The folder in Drive that is theirs.

    A folder each, inside the one from .env - "it should add it here as a
    conversation". Opening it shows one client's conversation rather than a
    heap of loose pictures with every other client's mixed in.

    Named after who they are in the server rather than after what was typed
    to find them. "artur rushiti", "Artur Rushiti" and "artur_rushiti-vet"
    all find the same person, and three spellings of one name is three
    folders.
    """
    who = str(plan.member_name or "").strip() or plan.name
    return re.sub(r"[^\w .-]+", "", who).strip() or "agent"


def picture_name(
    plan: Plan, *, when: datetime, where: str = "", order: int = 0,
) -> str:
    """What the picture is called in Drive.

    Numbered, because Drive sorts by name and a conversation out of order is
    not a conversation. The channel is in it because there is one picture per
    channel, and two files called the same thing on the same day are two
    files nobody can tell apart.

    Their name stays on the file as well as on the folder. When their folder
    could not be made the picture goes in the top one instead, and a file
    that says only "1 — ring-da-bell" is a file nobody can place.
    """
    safe = re.sub(r"[^\w .-]+", "", plan.name).strip() or "agent"
    said = re.sub(r"[^\w .-]+", "", str(where or "")).strip()
    # Once, when they are the same word. Looking them up by their channel
    # name is normal, and "1 - artur_rushiti-vet - artur_rushiti-vet" reads
    # like something went wrong.
    if said.casefold() == safe.casefold():
        said = ""
    parts = ([str(order)] if order else []) + [safe] + ([said] if said else [])
    return " — ".join(parts) + f" — {when:%Y-%m-%d}.png"


#: The colours of the real thing, sampled off a screenshot of the channel
#: rather than guessed at - "make it like the real one like this". A purple
#: theme, not Discord's own grey, because that is what these were sent in and
#: a picture in the wrong colours is one somebody has to be told about.
INK_ON_PAGE = {
    "page": "#411A3E",
    "bar": "#381737",
    "said": "#EAE6EB",
    "name": "#FBF6F9",
    "quiet": "#B3A3B5",
    "pill": "#4B2A4B",
    "edge": "#5A3459",
}


def as_page(plan: Plan, messages: list) -> str:
    """The messages as a page Chromium can photograph.

    Made to look like the channel they were said in rather than like a
    report: the avatars, the names, the times, the reactions and the colours
    of the real thing - "replicating as close as possible to how it was sent
    by agents".
    """
    lines = []
    for one in messages:
        text = html.escape(one.text or "")
        if one.attachments:
            text += (
                f"<div class='files'>{one.attachments} attachment"
                f"{'s' if one.attachments != 1 else ''}</div>"
            )
        if one.reactions:
            text += "<div class='react'>" + "".join(
                f"<span>{html.escape(bit.strip())}</span>"
                for bit in one.reactions.split("·") if bit.strip()
            ) + "</div>"
        said_in = ""
        if one.where and plan.channel and one.where != plan.channel.name:
            said_in = f"<span class='where'>#{html.escape(one.where)}</span>"
        face = (
            f"<img class='pfp' src='{html.escape(one.avatar, quote=True)}' alt=''>"
            if one.avatar else "<div class='pfp blank'></div>"
        )
        lines.append(
            f"<div class='msg'>{face}<div class='body'>"
            f"<div class='who'>{html.escape(one.who or 'somebody')}"
            f"<span class='when'>{html.escape(one.when or '')}</span>{said_in}</div>"
            f"<div class='what'>{text or '<em>no text</em>'}</div>"
            "</div></div>"
        )

    where = html.escape(
        (messages[0].where if messages and messages[0].where else "")
        or (plan.channel.name if plan.channel else plan.name)
    )
    paint = INK_ON_PAGE
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
  body {{ margin: 0; background: {paint['page']}; color: {paint['said']};
         font: 15px/1.375 "gg sans", "Noto Color Emoji", "Helvetica Neue",
               Helvetica, Arial, sans-serif; }}
  .head {{ padding: 13px 18px; background: {paint['bar']}; color: {paint['name']};
           font-weight: 600; }}
  .head span {{ color: {paint['quiet']}; font-weight: 400; margin-left: 10px;
                font-size: 13px; }}
  .wrap {{ padding: 10px 18px 16px; }}
  .msg {{ display: flex; gap: 14px; padding: 9px 0; }}
  .pfp {{ width: 40px; height: 40px; border-radius: 50%; flex: 0 0 40px;
          object-fit: cover; }}
  .blank {{ background: {paint['pill']}; }}
  .body {{ min-width: 0; }}
  .who {{ color: {paint['name']}; font-weight: 500; }}
  .when {{ color: {paint['quiet']}; font-weight: 400; font-size: 12px;
           margin-left: 8px; }}
  .where {{ color: {paint['quiet']}; font-weight: 400; font-size: 12px;
            margin-left: 8px; background: {paint['pill']};
            border-radius: 4px; padding: 1px 6px; }}
  .what {{ white-space: pre-wrap; word-break: break-word; margin-top: 2px; }}
  .files {{ color: {paint['quiet']}; font-size: 13px; font-style: italic; }}
  .react {{ margin-top: 7px; }}
  .react span {{ display: inline-block; background: {paint['pill']};
                 border: 1px solid {paint['edge']}; border-radius: 8px;
                 padding: 2px 8px; margin-right: 5px; font-size: 14px;
                 color: {paint['said']}; }}
</style></head>
<body>
  <div class="head"># {where}<span>{len(messages)} message(s)</span></div>
  <div class="wrap">{"".join(lines) or "<em>Nothing was said in this channel.</em>"}</div>
  <script>
    // Only once every avatar has loaded or given up, or the picture is taken
    // with holes where the faces go. An avatar url remembered months ago is
    // gone the moment they change their picture, so one that will not load
    // becomes the same plain circle as somebody with no picture at all,
    // rather than a broken-image mark in the middle of the screenshot.
    Promise.all([...document.images].map(one => {{
      const plain = () => {{
        one.classList.add('blank');
        one.removeAttribute('src');
      }};
      if (one.complete) {{
        if (!one.naturalWidth) {{ plain(); }}
        return true;
      }}
      return new Promise(done => {{
        one.onload = () => done(true);
        one.onerror = () => {{ plain(); done(true); }};
      }});
    }})).then(() => {{ document.documentElement.dataset.ready = '1'; }});
  </script>
</body></html>"""


def jump_to(plan: Plan) -> str:
    """The channel, as something to click on and go and look at.

    A mention alone only renders where the reader can see that channel, and
    these are read in whichever channel the clear-out was asked in. The link
    beside it opens the channel wherever it is, so "which one is he talking
    about" is one click rather than a search of the sidebar.
    """
    if plan.channel is None:
        return ""
    said = f"**#{plan.channel.name}**"
    if plan.guild_id and plan.channel.channel_id:
        return (
            f"{said} — [open it](https://discord.com/channels/"
            f"{plan.guild_id}/{plan.channel.channel_id})"
        )
    return said


def describe(plan: Plan) -> str:
    """What was found, to be read before anything irreversible is pressed."""
    if plan.channel is None:
        return (
            f"**{plan.name}** — no channel of theirs in the clients server.\n"
            + ("\n".join(f"⚠ {one}" for one in plan.problems) if plan.problems else "")
        ).strip()

    lines = [f"🧹 **{plan.name}**", f"• Channel — {jump_to(plan)}"]
    if plan.channel.category:
        lines.append(f"• Category — {plan.channel.category}")
    if plan.channel.last_active:
        lines.append(f"• Last message — {plan.channel.last_active:%d %b %Y}")
    if plan.sheet:
        lines.append(f"• Sheet — {plan.sheet}")
    else:
        lines.append(
            "• Sheet — **none found**, not on the board and not in the channel"
        )
    lines.append(
        f"• Member — {plan.member_name}" if plan.member_id
        else "• Member — **not in the server**, so there is nobody to ban"
    )
    lines += [f"⚠ {one}" for one in plan.problems]
    lines += [f"-# {one}" for one in plan.notes]
    # Under the list rather than inside it, and on a line of its own: "-#" is
    # Discord's small text only at the start of one, and in the middle of the
    # sheet line it rendered as the two characters.
    if plan.sheet and plan.from_channel:
        lines.append(
            "-# That sheet link came out of the channel rather than off a "
            "card, so once the channel goes the ALL CLIENTS row is the only "
            "place it lives."
        )
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


def one_by_one(quiet) -> list:
    """Every quiet channel's name, in order, for a run through them.

    The same rule as the dropdown - only the ones that can be read - and no
    cap on how many. Twenty-five is Discord's limit on a dropdown, not on how
    many people there are to close down, and a run does not have to live
    within it: "for every list he gives me, he send them to me one by one, so
    i dont have to pick anymore".
    """
    return [one.name for one in quiet if one.readable]


#: How many in a row can go wrong before a run gives up. One that fails
#: because a client has no sheet is that client's problem; three in a row is
#: Google being down, and grinding through a hundred and eighty of those is
#: a hundred and eighty messages saying the same thing.
ENOUGH_WRONG = 3


def how_the_run_went(went: list, left: list, trouble: list, *, over: str) -> str:
    """What a run through the list did, once it has stopped."""
    lines = [f"🧹 **{over}**"]
    if went:
        lines.append(f"• 🗑 Deleted {len(went)} — " + ", ".join(f"#{one}" for one in went))
    if left:
        lines.append(f"• ✖ Left {len(left)} — " + ", ".join(f"#{one}" for one in left))
    if trouble:
        lines.append(f"• ⚠ Couldn't finish {len(trouble)} — "
                     + ", ".join(f"#{one}" for one in trouble))
    if not (went or left or trouble):
        lines.append("• Nothing was touched.")
    return "\n".join(lines)


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
