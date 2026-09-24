"""Faith's texts with the agents, as the exchanges they were.

What an agent texted, and what Faith sent back - the pair is the lesson.
Her reply on its own says how she writes; beside what it answered, it says
how she handles "can you pause my leads", "when do I go live", "these leads
are bad", which is the part worth learning: what she asks for, what she
promises and what she will not, when she says she will check with the team.

No network and no Claude in here. This is the reading of what RingCentral
handed back, so it can be tested against real shapes without either.
"""

from __future__ import annotations

import difflib
import math
import re
from dataclasses import dataclass, field


@dataclass
class Text:
    """One SMS, from either side."""

    id: str
    at: str                 # ISO, as RingCentral gave it
    inbound: bool           # the agent writing to Faith
    agent: str              # the agent's number, digits only
    name: str               # the agent's name when RingCentral knows it
    said: str
    #: RingCentral's id for the thread. Most of these are group texts - "Jay
    #: Rodriguez, Arnold Tarpley", three members, Arnold in it twice - and in
    #: a group the number a reply went to first can be Arnold's other line
    #: rather than the agent's. Matched by phone number, Faith's reply to Jay
    #: was filed under nobody: not learned from, and Jay left looking as if he
    #: were still waiting.
    conversation: str = ""
    #: Somebody on the team rather than an agent or Faith. Tre hands agents
    #: over from his cell, (412) - "With Wolfpack so take care of him", "Hi
    #: faith this is Adrian Pacheco paid for OTP Trucker IUL leads" - and
    #: RingCentral labels that number "Arnold Tarpley (me)", the same as
    #: Faith's (878). Read as an agent, that is Franklin pinged to reply like
    #: Faith to Tre; read as Faith, it is Tre's way of writing learned as
    #: hers. Kept, for what it tells the draft; never an agent waiting, never
    #: a question Faith answered, and never Faith's answer.
    team: bool = False
    #: The number an outgoing text was sent from, digits only. Two numbers
    #: send from this line and only one of them is Faith.
    sender: str = ""

    @property
    def key(self) -> str:
        """Which conversation this belongs to: the thread, or failing that
        the agent's number."""
        return self.conversation or self.agent

    def as_dict(self) -> dict:
        return {"id": self.id, "at": self.at, "inbound": self.inbound,
                "agent": self.agent, "name": self.name, "said": self.said,
                "conversation": self.conversation, "sender": self.sender}

    @classmethod
    def from_dict(cls, held: dict) -> "Text":
        return cls(
            id=str(held.get("id") or ""), at=str(held.get("at") or ""),
            inbound=bool(held.get("inbound")), agent=str(held.get("agent") or ""),
            name=str(held.get("name") or ""), said=str(held.get("said") or ""),
            conversation=str(held.get("conversation") or ""),
            sender=str(held.get("sender") or ""),
        )


@dataclass
class Exchange:
    """What an agent sent, and what Faith sent back."""

    agent: str
    name: str
    asked: str
    answered: str
    at: str                 # when she answered
    words: frozenset = field(default_factory=frozenset)
    key: str = ""           # the conversation it happened in
    #: Why she answered the way she did, as RYTE worked it out from the
    #: conversation around it. "" until studied.
    why: str = ""

    @property
    def id(self) -> str:
        """Which exchange this is, for remembering what was learned from it.
        Her first text of the answer does not move once sent."""
        return f"{self.key}|{self.at}"


def digits(number: str) -> str:
    """A phone number to compare: the last ten digits, the way the US writes
    it. "+1 (801) 555-0142" and "8015550142" are one agent."""
    only = re.sub(r"\D", "", str(number or ""))
    return only[-10:] if len(only) > 10 else only


def from_record(record: dict) -> Text | None:
    """One message-store record as a Text, or None if there is nothing in it.

    The agent is whoever is not Faith: the sender of an inbound text, the
    first recipient of an outbound one.
    """
    inbound = str(record.get("direction") or "").casefold() == "inbound"
    if inbound:
        other = record.get("from") or {}
    else:
        other = (record.get("to") or [{}])[0] or {}
    said = " ".join(str(record.get("subject") or "").split())
    if not said:
        # A picture with no words. Kept as what it was rather than dropped:
        # an agent sending a screenshot is still a message waiting on a reply.
        kinds = {str(one.get("type") or "") for one in record.get("attachments") or []}
        said = "[sent a picture]" if kinds - {"Text", ""} else ""
    if not said:
        return None
    return Text(
        id=str(record.get("id") or ""),
        at=str(record.get("creationTime") or ""),
        inbound=inbound,
        agent=digits(other.get("phoneNumber") or ""),
        name=" ".join(str(other.get("name") or "").split()),
        said=said,
        conversation=str(
            record.get("conversationId")
            or (record.get("conversation") or {}).get("id") or ""
        ),
        sender="" if inbound else digits((record.get("from") or {}).get("phoneNumber") or ""),
    )


# Words that say nothing about what a text is about.
_QUIET = frozenset("""
a an the and or but if so to of in on at by for with from up out as is are was
were be been am do does did have has had i im i'm you your yours we our us me my
it its this that these those there here what when where who how why can could
would will just get got ok okay yes no hi hey hello thanks thank please pls
""".split())


def words_in(text: str) -> frozenset:
    """What a text is about, as a set of words."""
    return frozenset(
        one for one in re.findall(r"[a-z0-9']+", str(text or "").casefold())
        if len(one) > 2 and one not in _QUIET
    )


def faiths_number(texts: list) -> str:
    """The number nearly everything sent from this line goes out from.

    Faith answers from (878) all day; Tre's (412) sends now and then. Worked
    out rather than asked for - RINGCENTRAL_FAITH_NUMBER says it outright
    when it should be - and "" when nothing has gone out yet.
    """
    counted: dict[str, int] = {}
    for one in texts:
        if not one.inbound and one.sender:
            counted[one.sender] = counted.get(one.sender, 0) + 1
    return max(counted, key=counted.get) if counted else ""


def mark_team(texts: list, names, numbers=(), faith: str = "") -> list:
    """Mark what the team sent, coming in or going out.

    Coming in: by the name RingCentral shows - Tre's (412) arrives as "Arnold
    Tarpley", the same as the line - or by the line's own numbers, for a
    text that comes back without a name.

    Going out: anything sent from a number that is not Faith's. Both of the
    line's numbers can send, and only one of them is her. `faith` can name
    several, comma-separated - Franklin answering from the line in her place
    is answering as her, and is worth learning from as much as she is.
    """
    wanted = {" ".join(str(one).split()).casefold() for one in names or () if str(one).strip()}
    ours = {digits(one) for one in numbers or () if digits(one)}
    hers = {digits(one) for one in str(faith or "").split(",") if digits(one)}
    for one in texts:
        if one.inbound:
            one.team = bool(
                (one.name and one.name.casefold() in wanted) or one.agent in ours
            )
        else:
            one.team = bool(hers and one.sender and one.sender not in hers)
    return texts


def exchanges(texts: list) -> list:
    """Every time an agent texted and Faith answered, oldest first.

    Several texts in a row from either side are one turn - agents send "hey"
    then the question then "?" - and are joined. A text Faith sent first,
    with nothing before it from the agent, is her starting a conversation
    rather than answering one, and is not an exchange.
    """
    found = []
    for theirs in _by_conversation(texts).values():
        asked, answered = [], []
        for one in theirs:
            if one.team:
                continue
            if one.inbound:
                if answered:
                    found.append(_exchange(asked, answered))
                    asked, answered = [], []
                asked.append(one)
            elif asked:
                answered.append(one)
        if asked and answered:
            found.append(_exchange(asked, answered))
    found.sort(key=lambda one: one.at)
    return found


def _by_conversation(texts: list) -> dict:
    """{conversation: [texts, oldest first]}."""
    held: dict[str, list] = {}
    for one in texts:
        if one.key:
            held.setdefault(one.key, []).append(one)
    for theirs in held.values():
        theirs.sort(key=lambda one: one.at)
    return held


def _exchange(asked, answered) -> Exchange:
    """The agent is whoever asked. In a group, the numbers a reply went to
    include the team's own, and none of those is the agent."""
    question = "\n".join(one.said for one in asked)
    return Exchange(
        agent=asked[-1].agent,
        name=next((one.name for one in reversed(asked) if one.name), ""),
        asked=question,
        answered="\n".join(one.said for one in answered),
        at=answered[0].at, words=words_in(question), key=asked[-1].key,
    )


def waiting(texts: list, *, since: str) -> list:
    """Agents whose last word is still unanswered. [(agent, name, [texts])].

    The texts since Faith's last reply to them, and only when the newest of
    them is newer than `since` - so the first run after setting this up does
    not bring back every agent who ever had the last word.
    """
    found = []
    for theirs in _by_conversation(texts).values():
        tail = []
        for one in reversed(theirs):
            # Tre chiming in does not answer the agent, and is not the agent
            # either: passed over, in both directions.
            if one.team:
                continue
            if not one.inbound:
                break
            tail.insert(0, one)
        if tail and tail[-1].at >= since:
            name = next((one.name for one in reversed(tail) if one.name), "")
            found.append((tail[-1].agent, name, tail))
    found.sort(key=lambda one: one[2][-1].at)
    return found


def thread(texts: list, key: str, *, most: int = 12) -> list:
    """The last of one conversation, oldest first. `key` is a Text's `key`."""
    theirs = sorted((one for one in texts if one.key == key), key=lambda one: one.at)
    return theirs[-most:]


# ------------------------------------------------------ context and learning

# Not inside a longer run of digits: an ARN or a card number on the card is
# not the agent's phone, however its last ten digits happen to fall.
_PHONE = re.compile(r"(?<!\d)\+?1?[\s.(-]*\d{3}[\s.)-]*\d{3}[\s.-]*\d{4}(?!\d)")


def phones_in(text: str) -> set:
    """Every phone number written anywhere in some text, as ten digits.

    For finding an agent's New Agent card by the number texting: "Phone:
    +16146033618", "Phone Number: 435-817-3162", "(786) 609-0765" - the card
    is filled in by a form and by hand, and says it every way.
    """
    return {digits(one) for one in _PHONE.findall(str(text or "")) if len(digits(one)) == 10}


def closest(done: list, message: str, *, most: int = 5, recent: int = 3,
            agent: str = "", theirs: int = 3) -> list:
    """The past exchanges to show Claude, best first, never the same twice.

    How she has already talked to this agent, first: the tone she takes with
    somebody she has texted for months is not the one she takes with
    somebody new. Then the most like this message, for how she handles it.
    Then her newest, whatever they were about, to make up the number -
    because the way she writes drifts and her newest texts are her.
    """
    picked, seen = [], set()

    def take(at):
        picked.append(done[at])
        seen.add(at)

    if agent:
        for at in [at for at in range(len(done) - 1, -1, -1)
                   if done[at].agent == agent][:theirs]:
            take(at)
    target = len(picked) + most + recent

    wanted = words_in(message)
    scored = []
    for at, one in enumerate(done):
        if at in seen or not wanted or not one.words:
            continue
        shared = len(wanted & one.words)
        if shared:
            scored.append((shared / math.sqrt(len(wanted) * len(one.words)), at))
    scored.sort(reverse=True)
    for _score, at in scored[:most]:
        take(at)

    for at in range(len(done) - 1, -1, -1):
        if len(picked) >= target:
            break
        if at not in seen:
            take(at)
    return picked


def history(texts: list, agent: str, *, besides: str = "", most: int = 20) -> list:
    """What this agent has said to the line before, in other conversations.

    The current thread goes in whole; this is everything else - the order
    they placed in June, the states they changed last week - newest last.
    """
    theirs = sorted(
        (one for one in texts if one.agent == agent and one.key != besides),
        key=lambda one: one.at,
    )
    return theirs[-most:]


def _plain(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9']+", str(text or "").casefold()))


def kept(suggested: str, sent: str) -> float:
    """How much of a suggestion made it into what was sent, 0 to 1.

    "Hi Shelby! Yes they're paused 😊" sent as "Yes they're paused Shelby!"
    is the suggestion used, not a correction; a different answer altogether
    is. Only the words count - a changed emoji is not a lesson.
    """
    a, b = _plain(suggested), _plain(sent)
    if not a or not b:
        return 0.0
    return round(difflib.SequenceMatcher(None, a.split(), b.split()).ratio(), 2)


#: Kept at least this much, a suggestion counts as used.
USED = 0.8


def lessons_from(pending: list, texts: list, *, now: str, gone_after: str) -> tuple:
    """What was actually sent after each suggestion. (lessons, still pending).

    Every ping is a guess about what Faith would say, and a minute later she
    says it. The first thing sent from her number in that conversation after
    the agent's text is the answer to the guess - kept beside what RYTE
    suggested, so the next draft can see where it was wrong. A suggestion
    nobody answered by `gone_after` is dropped: the agent got a call, or it
    needed no reply, and neither says anything about how she writes.
    """
    lessons, waiting = [], []
    by_key: dict[str, list] = {}
    for one in texts:
        if not one.inbound and not one.team:
            by_key.setdefault(one.key, []).append(one)
    for held in pending:
        after = sorted(
            (one for one in by_key.get(str(held.get("key") or ""), [])
             if one.at > str(held.get("at") or "")),
            key=lambda one: one.at,
        )
        if after and _plus_minutes(after[0].at, 5) > now:
            # Still answering, perhaps: graded once she has had a few minutes
            # to send the rest of it.
            waiting.append(held)
        elif after:
            # Her whole answer, not its first line: a reply sent as three
            # texts a few seconds apart is one reply.
            sent = "\n".join(
                one.said for one in after if one.at <= _plus_minutes(after[0].at, 5)
            )
            lessons.append(_lesson(held, sent, after[0].at))
        elif str(held.get("at") or "") >= gone_after:
            waiting.append(held)
        elif held.get("note"):
            # Nothing texted back, but Franklin said what was wrong with it -
            # that is the lesson, and too good to drop with the suggestion.
            lessons.append(_lesson(held, "", str(held.get("at") or "")))
    return lessons, waiting


def _lesson(held: dict, sent: str, at: str) -> dict:
    suggested = str(held.get("draft") or "")
    lesson = {
        "asked": str(held.get("asked") or ""),
        "suggested": suggested,
        "sent": sent,
        "at": at,
        "asked_at": str(held.get("at") or ""),
        "agent": str(held.get("agent") or ""),
        "key": str(held.get("key") or ""),
        "same": bool(sent) and _plain(sent) == _plain(suggested),
        "kept": kept(suggested, sent),
    }
    for carried in ("posted", "note", "name"):
        if held.get(carried):
            lesson[carried] = held[carried]
    return lesson


def _plus_minutes(at: str, minutes: int) -> str:
    """An ISO time a few minutes later, as a string that still compares."""
    from datetime import datetime, timedelta

    try:
        when = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
    except ValueError:
        return str(at)
    later = when + timedelta(minutes=minutes)
    return later.strftime("%Y-%m-%dT%H:%M:%S.000Z") if str(at).endswith("Z") else later.isoformat()


def pick_lessons(lessons: list, message: str, *, most: int = 4) -> list:
    """The corrections worth showing: the most like this message, then the
    newest. Only where she wrote something different - a suggestion she sent
    word for word teaches nothing the examples do not."""
    wrong = [one for one in lessons if not _used(one)]
    wanted = words_in(message)
    scored = sorted(
        range(len(wrong)),
        key=lambda at: (
            len(wanted & words_in(wrong[at].get("asked"))),
            bool(wrong[at].get("note")),
            at,
        ),
        reverse=True,
    )
    picked = [wrong[at] for at in scored[: max(0, most - 1)]]
    for one in reversed(wrong):
        if len(picked) >= most:
            break
        if one not in picked:
            picked.append(one)
    return picked


def _used(lesson: dict) -> bool:
    """Whether a suggestion went out as written, or near enough - and nobody
    said anything was wrong with it."""
    if lesson.get("note"):
        return False
    if lesson.get("same"):
        return True
    return bool(lesson.get("sent")) and float(lesson.get("kept") or 0) >= USED


def used_count(lessons: list, *, last: int = 20) -> tuple:
    """(used, out of) for the newest suggestions - is it getting better."""
    newest = list(lessons)[-last:]
    return sum(1 for one in newest if _used(one)), len(newest)


def needs_explaining(lessons: list, *, tries: int = 3) -> list:
    """Corrections nobody has worked out the reason for yet, oldest first."""
    return [
        one for one in lessons
        if not _used(one) and not one.get("why") and int(one.get("tries") or 0) < tries
    ]


def rules_from(lessons: list, *, most: int = 15) -> list:
    """What the corrections add up to, newest last, each said once.

    Franklin's own words first among equals: when he says why, that is the
    reason, not RYTE's guess at it.
    """
    rules, seen = [], set()
    for one in reversed(list(lessons)):
        rule = " ".join(str(one.get("rule") or "").split())
        if not rule or _plain(rule) in seen:
            continue
        seen.add(_plain(rule))
        rules.append(rule)
        if len(rules) >= most:
            break
    return list(reversed(rules))


def before(texts: list, key: str, at: str, *, most: int = 8) -> list:
    """The conversation up to a moment, oldest first - what she was looking
    at when she wrote."""
    theirs = sorted(
        (one for one in texts if one.key == key and one.at < at), key=lambda one: one.at
    )
    return theirs[-most:]


def after(texts: list, key: str, at: str, *, most: int = 4) -> list:
    """What came next, oldest first - how her answer landed."""
    return sorted(
        (one for one in texts if one.key == key and one.at > at), key=lambda one: one.at
    )[:most]


def give_reasons(done: list, reasons: dict) -> list:
    """Her exchanges with what was learned about each, in place."""
    for one in done:
        one.why = str((reasons or {}).get(one.id) or "")
    return done


def unexplained(done: list, reasons: dict, *, settled: str, most: int = 20) -> list:
    """Her exchanges not yet studied, newest first, `most` at a time.

    Only answers older than `settled`: she often answers in two or three
    texts, and an exchange studied halfway through is studied wrong.
    """
    found = []
    for one in reversed(done):
        if one.at > settled or one.id in (reasons or {}):
            continue
        found.append(one)
        if len(found) >= most:
            break
    return found


def sample_for_playbook(done: list, *, most: int = 250) -> list:
    """Her exchanges spread across the whole history, for studying.

    Evenly spaced rather than the newest: a playbook written from one week
    is that week's playbook, and misses the pauses and the refunds that come
    round once a month.
    """
    if len(done) <= most:
        return list(done)
    step = len(done) / most
    return [done[int(at * step)] for at in range(most)]


def _stem(word: str) -> str:
    """Near enough to match "sales" to "sale" and "submitted" to "submit"."""
    word = word.casefold().strip()
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        word = word[:-1]
    return word[:5] if len(word) > 5 else word


def about(done: list, terms, *, most: int = 40) -> list:
    """Her exchanges about something, best first: the agent's words count
    double, hers once, and the newest wins a tie.

    `terms` are words or short phrases - "submit", "sale", "sold", "app
    submitted" - matched on their stems, so every way an agent types it
    counts.
    """
    stems = [
        [_stem(word) for word in re.findall(r"[a-z0-9']+", str(term).casefold()) if len(word) > 2]
        for term in terms or ()
    ]
    stems = [one for one in stems if one]
    scored = []
    for at, one in enumerate(done):
        asked, answered = one.asked.casefold(), one.answered.casefold()
        score = 0
        for words in stems:
            if all(word in asked for word in words):
                score += 2
            elif all(word in answered for word in words):
                score += 1
        if score:
            scored.append((score, at))
    scored.sort(reverse=True)
    return [done[at] for _score, at in scored[:most]]
