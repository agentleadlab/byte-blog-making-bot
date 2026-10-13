"""Each agent going live, held up against what was asked for.

"we are now doing discord threads and that's where we do updates going
forward". The team's updates - what was bought, the hours, the states, "this
is live" - are in threads all over the Growth server; the setup itself is on
the hub. RYTE reads both, and Payra for the payment, and keeps the Ryte
Checklist card in Franklin (Admin): an item a client under Agent Setup,
ticked when everything matches, and anything that doesn't under Concerns.

Nothing here writes anywhere. It works out what is right and what isn't;
`jobs` reads and writes the board, and nothing anywhere writes to the hub -
"make sure ryte is not deleting anyone on hub or changing states and caps".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

#: The checklists on the Ryte Checklist card, by name.
SETUP_LIST = "Agent Setup"
CONCERNS_LIST = "Concerns Needs to Be Addressed"
CARD_NAME = "Ryte Checklist"

#: Hub statuses that are an agent on their way to going live.
COMING = ("scheduled", "pending")


# ------------------------------------------------------------- the item

def item_title(name: str, lead_type: str, start: date | None) -> str:
    """"Gavin Littledike (FEX) - 10/14 - Wednesday" - the card's own pattern,
    "Name of Agent - Live Date - Day of Week", with the lead type because one
    client can go live twice in a day: Gavin's FEX and his MTG_PLUS."""
    kind = f" ({lead_type})" if lead_type else ""
    if start is None:
        return f"{name}{kind} - no live date"
    return f"{name}{kind} - {start:%m/%d} - {WEEKDAYS[start.weekday()]}"


def key_of(agent) -> str:
    """One order, however its item is worded: the hub's id, else name and type."""
    return str(getattr(agent, "id", "") or "") or f"{agent.name}|{agent.lead_type}"


# ------------------------------------------------------------- products

_SUBTYPES = (
    ("trucker", re.compile(r"\btruck(?:er|ing|s)?\b", re.IGNORECASE)),
    ("blue collar", re.compile(r"\bblue\s*collar\b|\bbc\b", re.IGNORECASE)),
    ("spanish", re.compile(r"\bspanish\b|\bsiul\b|\bespa[nñ]ol\b", re.IGNORECASE)),
)


def product_of(said: str) -> tuple[str | None, frozenset]:
    """(family, subtypes) - "TRUCKER_IUL" is ("iul", {"trucker"}). Underscores
    are spaces: "MTG_PLUS" is mortgage."""
    from . import agents

    text = re.sub(r"[_\-/]+", " ", str(said or ""))
    return agents.family_of(text), frozenset(name for name, pattern in _SUBTYPES if pattern.search(text))


def same_product(asked: str, hub: str) -> bool | None:
    """Whether what was asked for is what the hub has. None when either side
    doesn't say. A subtype the thread names has to be on the hub too; one the
    hub has that the thread doesn't mention is the thread being brief."""
    want, want_kinds = product_of(asked)
    have, have_kinds = product_of(hub)
    if not want or not have:
        return None
    if want != have:
        return False
    return want_kinds <= have_kinds


# ------------------------------------------------------------- the thread

@dataclass
class Asked:
    """What a client's thread says they bought and how it is set up."""

    lead_type: str = ""
    leads: int | None = None
    daily_cap: int | None = None
    states: tuple = ()
    hours: str = ""
    start: date | None = None
    same_as_before: bool = False
    #: Whether the hub's hours fit what was asked, as judged reading both:
    #: "yes", "no", or "" when either side doesn't say.
    hours_match: str = ""
    hours_why: str = ""
    live: bool = False
    thread: str = ""
    link: str = ""
    #: False when their thread only talks about their other orders.
    this_order: bool = True
    #: What the thread is about instead, when it isn't this order.
    about: str = ""


_STATE = re.compile(r"\b[A-Z]{2}\b")


def asked_from(got: dict, *, thread: str = "", link: str = "") -> Asked:
    """The thread reader's answer, tidied. A thread that isn't about this
    order says nothing about it - an earlier order's 25 OTP Vet leads from
    09/24 aren't what this one should be."""
    if got.get("this_order") is False:
        return Asked(thread=thread, link=link, this_order=False,
                     about=" ".join(str(got.get("thread_about") or "").split())[:120])
    def number(said):
        try:
            value = int(str(said).strip())
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    start = None
    try:
        start = date.fromisoformat(str(got.get("start_date") or "")[:10])
    except ValueError:
        start = None
    states = got.get("states") or []
    if isinstance(states, str):
        states = _STATE.findall(states.upper())
    return Asked(
        lead_type=" ".join(str(got.get("lead_type") or "").split()),
        leads=number(got.get("leads")), daily_cap=number(got.get("daily_cap")),
        states=tuple(dict.fromkeys(str(one).strip().upper() for one in states if str(one).strip())),
        hours=" ".join(str(got.get("hours") or "").split()), start=start,
        same_as_before=bool(got.get("same_as_before")),
        hours_match=str(got.get("hours_match") or "").casefold() if str(got.get("hours_match") or "").casefold() in ("yes", "no") else "",
        hours_why=" ".join(str(got.get("hours_why") or "").split()),
        live=bool(got.get("live")), thread=thread, link=link,
    )


def name_in_thread(title: str) -> str:
    """The client's name at the front of a thread's title: "Lauren Huddleston
    · Text-Verified Veteran Leads [FRESH - $45/Lead]", "Edward Conway - Trucker
    IUL 15 leads", "NAME: Madison McGill", "Marissa M"."""
    said = re.sub(r"^\s*(?:name|client|agent)\s*[:\-]\s*", "", str(title or ""), flags=re.IGNORECASE)
    return re.split(r"\s+[·|\-–—]\s+|\s*[\[(]|\s*[·|]\s*", said, maxsplit=1)[0].strip()


def threads_for(name: str, threads) -> list:
    """The threads that are this client's, by their title: the whole name -
    at the front, anywhere in it ("FEX setup - Gavin Littledike"), or last name
    first ("Littledike, Gavin"). Failing that, their first name and the
    initial of their last ("Marissa M"), or their first name alone ("Gavin") -
    those only when it's the one thread that fits."""
    from . import clearout

    whole, initial = [], []
    wanted = clearout.words_of(name)
    # Every thread that starts with their first name: two of those and a
    # lone "Gavin" could be either Gavin.
    namesakes = 0
    for one in threads or []:
        title = getattr(one, "name", "") or ""
        said = name_in_thread(title)
        if wanted and clearout.words_of(said)[:1] == wanted[:1]:
            namesakes += 1
        if (clearout.name_match(name, said) >= 2 or clearout.name_match(name, title) >= 2
                or (len(wanted) >= 2 and clearout.name_match(" ".join(wanted[1:] + wanted[:1]), title) >= 2)):
            whole.append(one)
            continue
        words = clearout.words_of(said)
        if len(wanted) >= 2 and words and words[0] == wanted[0] and (
                words[1:] == [wanted[-1][:1]] or (len(words) == 1 and len(wanted[0]) >= 3)):
            initial.append(one)
    if not whole and len(initial) == 1 and len(clearout.words_of(name_in_thread(initial[0].name))) == 1 and namesakes > 1:
        return []
    return whole or (initial if len(initial) == 1 else [])


def near_threads(name: str, threads, *, most: int = 5) -> list[str]:
    """Thread titles with any of their name's words in - for "@Ryte threads
    Gavin", when RYTE says there's no thread and there is."""
    from . import clearout

    wanted = {word for word in clearout.words_of(name) if len(word) >= 3}
    found = [str(getattr(one, "name", "") or "") for one in threads or []
             if wanted & set(clearout.words_of(getattr(one, "name", "") or ""))]
    return sorted(set(found))[:most]


# ------------------------------------------------------------- #new-agent-setup

@dataclass
class SetupPost:
    """Nova Cloud's "✅ Wesley Zurschmit setup is done (by Therese)" in
    #new-agent-setup - the setup finished and tested, and who did it."""

    name: str
    by: str = ""
    launch: date | None = None
    leads: int | None = None
    #: The test lead, channel by channel: {"sms": True, "email": True, ...}.
    channels: dict = field(default_factory=dict)
    link: str = ""
    when: str = ""


_MONTHS = {name: at for at, name in enumerate(
    ("january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"), start=1)}
_DONE_POST = re.compile(r"✅\s*(.+?)\s+setup is done(?:\s*\(by\s+([^)]+)\))?", re.IGNORECASE)
_AGENT_LINE = re.compile(r"^\s*Agent:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_LAUNCH = re.compile(r"Launch(?:\s+date)?:\s*(?:[A-Za-z]+,?\s+)?([A-Za-z]+)\.?\s+(\d{1,2})", re.IGNORECASE)
_LEADS = re.compile(r"Requested\s+Leads:\s*(\d+)", re.IGNORECASE)
_CHANNEL = re.compile(r"\b(sms|email|discord|sheet)\s*([✓✔✅]|[✗✘❌]|\bx\b)", re.IGNORECASE)


def parse_setup_post(text: str, *, today: date, link: str = "", when: str = "") -> SetupPost | None:
    """One #new-agent-setup post, or None when it isn't a "setup is done"."""
    plain = re.sub(r"<@[!&]?\d+>|\*\*|__", "", str(text or ""))
    done = _DONE_POST.search(plain)
    if not done:
        return None
    name = " ".join(done.group(1).split())
    agent = _AGENT_LINE.search(plain)
    if agent and not name:
        name = " ".join(agent.group(1).split())
    launch = None
    found = _LAUNCH.search(plain)
    if found and found.group(1).casefold() in _MONTHS:
        month, day = _MONTHS[found.group(1).casefold()], int(found.group(2))
        try:
            launch = date(today.year, month, day)
            # "January 3" said in late December is next year's.
            if (launch - today).days < -180:
                launch = date(today.year + 1, month, day)
        except ValueError:
            launch = None
    leads = _LEADS.search(plain)
    channels = {}
    for one in _CHANNEL.finditer(plain):
        channels[one.group(1).casefold()] = one.group(2) in "✓✔✅"
    return SetupPost(name=name, by=" ".join((done.group(2) or "").split()), launch=launch,
                     leads=int(leads.group(1)) if leads else None, channels=channels, link=link, when=when)


def post_for(name: str, posts) -> SetupPost | None:
    """Their newest "setup is done", by their whole name."""
    from . import clearout

    mine = [one for one in posts or [] if clearout.name_match(name, one.name) >= 2
            or clearout.name_match(one.name, name) >= 2]
    return max(mine, key=lambda one: one.when) if mine else None


# ------------------------------------------------------------- the checks

@dataclass
class Finding:
    """One check. `ok` is True, False, or None when there was nothing to check
    it against - which never stops the item being ticked."""

    what: str
    ok: bool | None
    said: str = ""


@dataclass
class Result:
    agent: object
    asked: Asked | None
    findings: list = field(default_factory=list)
    post: SetupPost | None = None

    @property
    def link(self) -> str:
        """Where to look: their setup post, else their thread."""
        return (self.post.link if self.post and self.post.link else "") or (self.asked.link if self.asked else "")

    @property
    def wrong(self) -> list:
        return [one for one in self.findings if one.ok is False]

    @property
    def all_good(self) -> bool:
        return not self.wrong


def check(agent, asked: Asked | None, *, paid: bool | None, today: date, zelle: bool = False,
          post: SetupPost | None = None, posts_read: bool = False, one_order: bool = True) -> Result:
    """Every check for one agent on the hub. `zelle`: the Zelle channel was
    read too, so "not paid" is neither Payra nor Zelle. `post`: their "setup
    is done" in #new-agent-setup, when `posts_read`. `one_order`: the only
    order of theirs coming up, so the post's lead count is this order's."""
    found = []
    found.append(Finding("Paid", paid, "" if paid else (
        f"no payment in {'Payra or Zelle' if zelle else 'Payra'} under their name" if paid is False else "Payra not read")))
    if posts_read:
        if post is None:
            found.append(Finding("Setup done", False, "no “setup is done” for them in #new-agent-setup yet"))
        else:
            found.append(Finding("Setup done", True, f"by {post.by}" if post.by else ""))
            failed = sorted(name for name, ok in post.channels.items() if not ok)
            if failed:
                found.append(Finding("Test lead", False, f"didn't arrive by {', '.join(failed)}"))
            if post.launch is not None and agent.start_date is not None and post.launch != agent.start_date:
                found.append(Finding("Launch date", False,
                                     f"#new-agent-setup says {post.launch:%m/%d}, the hub says {agent.start_date:%m/%d}"))
            if one_order and post.leads is not None and agent.ordered is not None and post.leads != agent.ordered:
                found.append(Finding("Requested leads", False,
                                     f"#new-agent-setup says {post.leads}, the hub has {agent.ordered}"))
    # Their thread, when there is one: what it asked for, held against the
    # hub. None isn't a concern - the setup post is what says it's done.
    if asked is None:
        found.append(Finding("Thread", None, "no thread for them in the Growth server"))
    elif not asked.this_order:
        found.append(Finding("Thread", None, f"their thread doesn't mention this order ({agent.lead_type or 'this lead type'})"
                             + (f" - it's about {asked.about}" if asked.about else "")))
    else:
        found.append(Finding("Thread", True, asked.thread))

    found.append(Finding("Live date", agent.start_date is not None,
                         "" if agent.start_date else "no live date on the hub"))
    status = str(agent.status or "").casefold()
    found.append(Finding("Scheduled", status in ("scheduled", "live", "active"),
                         "" if status in ("scheduled", "live", "active") else
                         f"not turned on yet - the hub says “{agent.status or 'nothing'}”"))
    found.append(Finding("Sheet", bool(agent.sheet_url), "" if agent.sheet_url else "no sheet link on the hub"))
    hub_has_states = any(key in (agent.raw or {}) for key in ("states", "state_list", "allowed_states", "licensed_states"))
    if hub_has_states:
        found.append(Finding("States set", bool(agent.states), "" if agent.states else "no states on the hub"))

    if asked is not None:
        same = same_product(asked.lead_type, agent.lead_type)
        found.append(Finding("Lead type", same, "" if same is not False else
                             f"thread asked for {asked.lead_type}, the hub has {agent.lead_type}"))
        if asked.leads is not None and agent.ordered is not None:
            found.append(Finding("Lead count", asked.leads == agent.ordered, "" if asked.leads == agent.ordered else
                                 f"thread asked for {asked.leads}, the hub has {agent.ordered}"))
        if asked.daily_cap is not None:
            hub_cap = agent.daily_cap or 0
            found.append(Finding("Daily cap", hub_cap == asked.daily_cap, "" if hub_cap == asked.daily_cap else
                                 f"thread asked for {asked.daily_cap}/day, the hub has "
                                 + (f"{hub_cap}/day" if hub_cap else "no cap (0 - the hub decides)")))
        if asked.states and agent.states:
            want, have = set(asked.states), set(agent.states)
            missing, extra = sorted(want - have), sorted(have - want)
            found.append(Finding("States", not missing and not extra, "; ".join(filter(None, (
                f"missing on the hub: {', '.join(missing)}" if missing else "",
                f"on the hub but not asked: {', '.join(extra)}" if extra else "")))))
        if asked.start is not None and agent.start_date is not None:
            found.append(Finding("Start date", asked.start == agent.start_date, "" if asked.start == agent.start_date else
                                 f"thread says {asked.start:%m/%d}, the hub says {agent.start_date:%m/%d}"))
        if asked.hours_match:
            found.append(Finding("Hours", asked.hours_match == "yes", "" if asked.hours_match == "yes" else
                                 (asked.hours_why or f"thread asked for {asked.hours}, the hub has {agent.schedule or 'no hours'}")))
    return Result(agent=agent, asked=asked, findings=found, post=post)


def concern_kind(line: str) -> str:
    """"Gavin Littledike (FEX) - 10/14 - Wednesday: Thread" - which order and
    which check, whatever the wording after it."""
    return " ".join(str(line or "").split(" — ", 1)[0].split())


def concern_lines(result: Result) -> list[str]:
    """What's wrong, a line each, for the Concerns checklist."""
    title = item_title(result.agent.name, result.agent.lead_type, result.agent.start_date)
    return [f"{title}: {one.what} — {one.said}" for one in result.wrong]


# ------------------------------------------------------------- remembered

def _path(name: str):
    from .state import _state_dir

    return _state_dir() / name


SAID_PATH = None
READ_PATH = None


def load(which: str) -> dict:
    """"said" - what's been raised with Franklin, per order; "read" - each
    thread's last read, so an unchanged thread isn't read again."""
    import json

    where = (SAID_PATH if which == "said" else READ_PATH) or _path(f"setup-{which}.json")
    try:
        held = json.loads(where.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return held if isinstance(held, dict) else {}


def save(which: str, held: dict) -> None:
    import json

    where = (SAID_PATH if which == "said" else READ_PATH) or _path(f"setup-{which}.json")
    where.parent.mkdir(parents=True, exist_ok=True)
    spare = where.with_suffix(".tmp")
    spare.write_text(json.dumps(held, indent=1, default=str), encoding="utf-8")
    spare.replace(where)


def coming_up(agents, today: date, *, ahead: int = 14) -> list:
    """The hub's agents on their way to going live: scheduled or pending, with
    a live date from yesterday to two weeks out, or none yet."""
    from datetime import timedelta

    found = []
    for one in agents:
        status = str(one.status or "").casefold()
        if status not in COMING:
            continue
        if one.start_date is None or today - timedelta(days=1) <= one.start_date <= today + timedelta(days=ahead):
            found.append(one)
    return sorted(found, key=lambda one: (one.start_date or date.max, one.name))


def not_ready(results, day: date) -> list:
    """The ones going live that day with anything still wrong."""
    return [one for one in results if one.agent.start_date == day and not one.all_good]
