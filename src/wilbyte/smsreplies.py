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
    line's numbers can send, and only one of them is her.
    """
    wanted = {" ".join(str(one).split()).casefold() for one in names or () if str(one).strip()}
    ours = {digits(one) for one in numbers or () if digits(one)}
    hers = digits(faith)
    for one in texts:
        if one.inbound:
            one.team = bool(
                (one.name and one.name.casefold() in wanted) or one.agent in ours
            )
        else:
            one.team = bool(hers and one.sender and one.sender != hers)
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
        at=answered[0].at, words=words_in(question),
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


def closest(done: list, message: str, *, most: int = 5, recent: int = 3) -> list:
    """The past exchanges to show Claude: the most like this one, then recent.

    Most alike, because how she handled "pause my leads" last month is how
    she would handle it now. A few of the newest as well, whatever they were
    about, because the way she writes drifts and her newest texts are her.
    """
    wanted = words_in(message)
    scored = []
    for at, one in enumerate(done):
        if not wanted or not one.words:
            continue
        shared = len(wanted & one.words)
        if shared:
            scored.append((shared / math.sqrt(len(wanted) * len(one.words)), at, one))
    scored.sort(key=lambda row: (row[0], row[1]), reverse=True)

    picked, seen = [], set()
    for _score, at, one in scored[:most]:
        picked.append(one)
        seen.add(at)
    for at in range(len(done) - 1, -1, -1):
        if len(picked) >= most + recent:
            break
        if at not in seen:
            picked.append(done[at])
            seen.add(at)
    return picked


def thread(texts: list, key: str, *, most: int = 12) -> list:
    """The last of one conversation, oldest first. `key` is a Text's `key`."""
    theirs = sorted((one for one in texts if one.key == key), key=lambda one: one.at)
    return theirs[-most:]
