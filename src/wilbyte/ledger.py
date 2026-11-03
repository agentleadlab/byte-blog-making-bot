"""Every payment followed until it's set up, live and delivered.

"I want to be able to not think about it like by the end of the day, hey,
this is all the person you didn't set up. This is the person you
incorrectly set up, or Rook and Nova made a mistake here". The setup QC
checks a payment; this keeps where each one stands - from the hub, not from
anyone saying so - and says the day's list:

    09:00 ET        who goes live today, and whether they're ready
    17/18/19:00 ET  who isn't set up, who's set up wrong, who should be live
                    and isn't, who goes live next and isn't ready, what's
                    waiting on an answer, and payments Payra has that
                    #new-payment doesn't

A payment comes off the list only once the hub shows it delivered, or
somebody OK'd what's left. Reads only.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone

from .state import _state_dir

#: Which of the day's lists have been said: {"2026-11-03": ["09:00", "17:00"]}.
SAID_PATH = _state_dir() / "ledger-said.json"

#: A payment is followed this long - longer than any order takes to deliver.
OPEN_DAYS = 21

#: When the lists are said, Eastern: the morning's, then the evening's three.
MORNING = (9, 0)
EVENINGS = ((17, 0), (18, 0), (19, 0))

#: Where a payment stands, in the order they're worked through.
NOT_SET_UP, CANT, SET_UP, LIVE, DELIVERING, FULFILLED, SHORT, OVERFLOW, OVERFLOW_DONE, PAUSED = (
    "not_set_up", "cant", "set_up", "live", "delivering", "fulfilled", "short", "overflow", "overflow_done",
    "paused")
#: Off the list once it's here with nothing wrong.
CLOSED = (FULFILLED, OVERFLOW_DONE)

_FINISHED = ("ended", "done", "fulfilled", "complete", "completed", "cancelled", "canceled")
_LIVE = ("live", "active")


def stage(result) -> dict:
    """Where the payment stands by its QC: {"stage", and the hub row's
    "hub_id", "hub_name", "status", "start", "ordered", "delivered"}."""
    hub = getattr(result, "hub", None)
    kept = {"stage": NOT_SET_UP, "hub_id": "", "hub_name": "", "status": "", "start": "", "ordered": None,
            "delivered": None}
    if getattr(result, "overflow", False):
        done = any(one.startswith("Delivered") for one in result.ok) and not any(
            one.startswith("Delivered") for one in result.problems)
        kept["stage"] = OVERFLOW_DONE if done else OVERFLOW
        return kept
    if hub is not None:
        start = getattr(hub, "start_date", None)
        kept.update(hub_id=str(getattr(hub, "id", "") or ""), hub_name=str(getattr(hub, "name", "") or ""),
                    status=str(getattr(hub, "status", "") or ""), start=start.isoformat() if start else "",
                    ordered=getattr(hub, "ordered", None), delivered=getattr(hub, "delivered", None))
    if getattr(result, "cant", ""):
        kept["stage"] = CANT
        return kept
    if hub is None:
        return kept
    status = kept["status"].casefold()
    ordered, delivered = kept["ordered"], kept["delivered"] or 0
    if ordered and delivered >= ordered:
        kept["stage"] = FULFILLED
    elif status in _FINISHED:
        kept["stage"] = FULFILLED if ordered is None or delivered >= ordered else SHORT
    elif status in _LIVE:
        kept["stage"] = DELIVERING if delivered else LIVE
    elif delivered:
        # Paused partway - Camden Misquez's, started Oct 7 - not one that
        # never started: "should be live and isn't" was 29 long with them.
        kept["stage"] = PAUSED
    else:
        kept["stage"] = SET_UP
    return kept


def _day(said) -> date | None:
    try:
        return date.fromisoformat(str(said or "")[:10])
    except ValueError:
        return None


def _moment(said) -> datetime | None:
    try:
        at = datetime.fromisoformat(str(said or ""))
    except ValueError:
        return None
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


def _ago(at: datetime | None, now: datetime) -> str:
    if at is None:
        return ""
    hours = (now - at).total_seconds() / 3600
    if hours < 1:
        return f"{max(1, int(hours * 60))}m ago"
    if hours < 48:
        return f"{int(hours)}h ago"
    return f"{int(hours // 24)} days ago"


def open_ones(records: dict, *, now: datetime) -> dict:
    """The payments still followed: paid in the last three weeks, checked at
    least once, and not done with."""
    found = {}
    for key, one in records.items():
        posted = _moment(one.get("posted"))
        if posted is None or now - posted > timedelta(days=OPEN_DAYS) or one.get("aged"):
            continue
        if not one.get("stage"):
            continue
        try:
            if float(str(one.get("amount") or "0").replace(",", "")) and float(
                    str(one.get("amount")).replace(",", "")) < 25:
                continue  # a $1 card check, a fee
        except ValueError:
            pass
        if one["stage"] in CLOSED and not _still_wrong(one):
            continue
        found[key] = one
    return found


def _still_wrong(one: dict) -> list:
    """Its problems, less what somebody OK'd."""
    from .rookqc import among

    okd = one.get("ok_problems") or []
    return [problem for problem in one.get("open_problems") or [] if not among(problem, okd)]


def _line(one: dict, now: datetime, why: str = "") -> str:
    who = one.get("client") or one.get("name") or "?"
    amount = f" ${one['amount']}" if one.get("amount") else ""
    if one.get("_more"):
        amount += f" ({one['_more']} payments)"
    paid = _ago(_moment(one.get("posted")), now)
    bits = f"**{who}**{amount}" + (f" · paid {paid}" if paid else "")
    if why:
        bits += f" — {why}"
    if one.get("link"):
        bits += f" <{one['link']}>"
    return f"• {bits}"


def _ready(one: dict) -> str:
    """"" when ready to go live, else why not."""
    if one.get("stage") in (NOT_SET_UP, CANT):
        return "not on the hub yet"
    wrong = _still_wrong(one)
    if wrong:
        return wrong[0]
    if str(one.get("status") or "").casefold() == "unsigned":
        return "contract not signed yet"
    return ""


def daily(records: dict, *, now: datetime, today: date, through: date, morning: bool = False,
          payra_missing: list | None = None, aged: list | None = None, board: dict | None = None,
          board_complete: bool = True) -> str:
    """The day's list. `through`: the last launch day looked ahead to
    (tomorrow, or Monday on a Friday). `payra_missing`: lines for payments
    Payra has that #new-payment doesn't; `aged`: lines for aged orders."""
    followed = open_ones(records, now=now)
    sections: list[tuple[str, list[str]]] = []

    def by(test):
        """The payments that pass, one line each for the same order - two
        posts of one payment, or two payments onto one hub row, once."""
        out, seen = [], {}
        for _key, one in sorted(followed.items(), key=lambda item: str(item[1].get("posted") or "")):
            if not test(one):
                continue
            same = (f"hub:{one['hub_id']}" if one.get("hub_id") else
                    f"{str(one.get('client') or '').casefold()}:{one.get('amount')}")
            if same in seen:
                seen[same]["_more"] = seen[same].get("_more", 1) + 1
                continue
            one = dict(one)
            seen[same] = one
            out.append(one)
        return out

    launching = by(lambda one: (_day(one.get("start")) or date.max) <= (today if morning else through)
                   and (_day(one.get("start")) or date.min) >= today and one.get("stage") in (SET_UP, LIVE))
    if morning:
        sections.append(("🚀 Going live today", [_line(one, now, ("❌ " + _ready(one)) if _ready(one) else "✅ ready")
                                                for one in launching if _day(one.get("start")) == today]))
    not_set_up = by(lambda one: one.get("stage") == NOT_SET_UP)
    sections.append(("❌ Paid, not set up", [_line(one, now, (_still_wrong(one) or ["not on the hub"])[0])
                                            for one in not_set_up]))
    sections.append(("🃏 Rook/Nova can't set up - press Make the card on its card",
                     [_line(one, now, one.get("cant") or "") for one in by(lambda one: one.get("stage") == CANT)]))
    wrong = by(lambda one: one.get("stage") not in (NOT_SET_UP, CANT, OVERFLOW, OVERFLOW_DONE) and _still_wrong(one))
    sections.append(("⚠️ Set up wrong", [_line(one, now, "; ".join(_still_wrong(one))[:300]) for one in wrong]))
    late = by(lambda one: one.get("stage") == SET_UP and (_day(one.get("start")) or date.max) < today
              or (one.get("stage") == SET_UP and _day(one.get("start")) == today and not morning))
    sections.append(("⏰ Should be live and isn't", [
        _line(one, now, f"start {one.get('start')}, the hub has them {one.get('status') or '?'}") for one in late]))
    if not morning:
        ahead = [one for one in launching if today < (_day(one.get("start")) or date.min) <= through]
        sections.append((f"📅 Going live by {through:%A}", [
            _line(one, now, (f"{_day(one.get('start')):%a} - " if _day(one.get("start")) else "")
                  + (("❌ " + _ready(one)) if _ready(one) else "✅ ready")) for one in ahead]))
    sections.append(("⏸️ Paused with leads still owed", [
        _line(one, now, f"{one.get('delivered')} of {one.get('ordered')} delivered, the hub has them paused")
        for one in by(lambda one: one.get("stage") == PAUSED)]))
    short = by(lambda one: one.get("stage") == SHORT)
    sections.append(("📉 Ended short", [_line(one, now, f"{one.get('delivered')} of {one.get('ordered')} delivered")
                                        for one in short]))
    waiting = by(lambda one: one.get("todo") and one.get("stage") not in (NOT_SET_UP, CANT) and not _still_wrong(one))
    sections.append(("❓ Waiting on the team or an answer", [_line(one, now, "; ".join(one.get("todo"))[:300])
                                                            for one in waiting]))
    sections.append(("🚚 Overflow not delivered yet", [_line(one, now) for one in by(
        lambda one: one.get("stage") == OVERFLOW)]))
    if board:
        sections.append(("🔀 Nova's #launching board vs the hub",
                         board_vs(records, board, now=now, complete=board_complete)))
    if payra_missing:
        sections.append(("💸 Paid in Payra, never posted in #new-payment", list(payra_missing)))
    if aged:
        sections.append(("🧺 Aged", list(aged)))
    counts = {}
    for one in followed.values():
        counts[one.get("stage")] = counts.get(one.get("stage"), 0) + 1
    head = (f"☀️ **Payments - {today:%a %b} {today.day}, morning**" if morning
            else f"🌙 **Payments - {today:%a %b} {today.day}, {now.astimezone(_ny()):%-I %p}**")
    tally = " · ".join(f"{label} {counts.get(key, 0)}" for key, label in (
        (SET_UP, "set up, not live"), (LIVE, "live"), (DELIVERING, "delivering"), (NOT_SET_UP, "not set up"),
        (CANT, "can't set up")))
    lines = [head, f"-# Following {len(followed)} payment(s): {tally}"]
    said_any = False
    for title, items in sections:
        if not items:
            continue
        said_any = True
        lines.append(f"\n**{title}** ({len(items)})")
        lines += items
    if not said_any:
        lines.append("✅ Nothing to chase - everyone paid is set up as paid for.")
    return "\n".join(lines)


def _ny():
    from zoneinfo import ZoneInfo

    return ZoneInfo("America/New_York")


def due_lists(now: datetime, said: list) -> list[str]:
    """Which of the day's lists are due by `now` (Eastern) and not said yet:
    "morning", "17:00"... - only the latest evening one when several are."""
    at = now.astimezone(_ny())
    due = []
    if (at.hour, at.minute) >= MORNING and (at.hour, at.minute) < EVENINGS[0] and "morning" not in said:
        due.append("morning")
    evenings = [f"{hour:02d}:{minute:02d}" for hour, minute in EVENINGS if (at.hour, at.minute) >= (hour, minute)]
    if evenings and evenings[-1] not in said and (at.hour, at.minute) < (EVENINGS[-1][0] + 2, 0):
        due.append(evenings[-1])
    return due


def said_today(day: date) -> list:
    try:
        held = json.loads(SAID_PATH.read_text())
    except (OSError, ValueError):
        return []
    return list((held if isinstance(held, dict) else {}).get(day.isoformat()) or [])


def said_on(day: date, which: str) -> None:
    try:
        held = json.loads(SAID_PATH.read_text())
    except (OSError, ValueError):
        held = {}
    held = {one: when for one, when in (held if isinstance(held, dict) else {}).items()
            if _day(one) and (day - _day(one)).days < 14}
    held.setdefault(day.isoformat(), []).append(which)
    SAID_PATH.parent.mkdir(parents=True, exist_ok=True)
    SAID_PATH.write_text(json.dumps(held, indent=1))


def payra_missing(data: dict, posted_text: str, *, day: date) -> list[str]:
    """Payra payments that went through on `day` (Eastern) whose invoice no
    payment post in #new-payment names - the ones nobody would ever see."""
    from .payraapi import _went_through

    seen = {one.upper().replace("INV", "INV-").replace("--", "-")
            for one in re.findall(r"\bINV-?\d+\b", posted_text or "", re.IGNORECASE)}
    lines = []
    for one in (data.get("payments") or {}).values():
        number = str(one.get("invoice_number") or "").upper()
        paid = _moment(one.get("paid_on"))
        if not number or paid is None or paid.astimezone(_ny()).date() != day or not _went_through(one):
            continue
        # One payment for two invoices - Sarra El Badri's "INV-21189,
        # INV-21190" - is posted when either is.
        numbers = {f"INV-{digits}" for digits in re.findall(r"\d+", number)}
        if numbers & seen:
            continue
        lines.append(f"• **{one.get('customer') or '?'}** ${one.get('amount')} · {number} · "
                     f"{paid.astimezone(_ny()):%-I:%M %p}")
    return lines


# ------------------------------------------------ Nova's #launching board

#: The board's sections, by the words in their heading: what Nova says of
#: everyone listed under it.
_BOARD = (
    (re.compile(r"\bpaid\b.*\bnot\s+set\s*up\b", re.IGNORECASE), NOT_SET_UP),
    (re.compile(r"\bon\s+the\s+hub\b|\bpaused\b|\bpayment\s+to\s+check\b", re.IGNORECASE), SET_UP),
    (re.compile(r"\bsigned\b|\bgo\s+live\b|\bschedule\b", re.IGNORECASE), SET_UP),
    (re.compile(r"^\W*live\b", re.IGNORECASE), LIVE),
)


def board_parse(text: str) -> dict:
    """Nova's "Launching — where everyone stands": {name: what it says},
    from a message whose thread links have been put as names. Each heading
    line, then the names under it, until the next heading."""
    found, under = {}, None
    for line in str(text or "").splitlines():
        bare = re.sub(r"[*_`]", "", line).strip()
        if not bare:
            continue
        heading = next((kind for pattern, kind in _BOARD if pattern.search(bare.split("(")[0])), None)
        names_part = bare
        if heading is not None and not re.search(r"⬗|«", bare):
            under = heading
            continue
        if under is None:
            continue
        for name in re.split(r"\s*[⬗«]\s*|\s{2,}|\s*,\s*", names_part):
            name = re.sub(r"\(.*?\)", "", name).split("·")[0].strip(" -—")
            if re.fullmatch(r"[A-Z][\w.'’-]*(?:\s+[A-Z][\w.'’-]*){1,3}", name):
                found.setdefault(name, under)
    return found


def board_vs(records: dict, board: dict, *, now: datetime, complete: bool = True) -> list[str]:
    """Where Nova's board and the hub (by Ryte's list) disagree - Kyle
    Cassidy "Live" on the board, the hub still waiting on his signature.
    `complete`: every name on the board was read - only then is somebody
    said to be missing from it."""
    from .rookqc import same_person_strict as same_person

    lines = []
    if not board:
        return lines
    recent = {key: one for key, one in records.items()
              if (_moment(one.get("posted")) or datetime.min.replace(tzinfo=timezone.utc)) > now - timedelta(
                  days=OPEN_DAYS) and one.get("stage") and not one.get("aged")}
    for name, said in board.items():
        mine = [one for one in recent.values() if same_person(str(one.get("client") or ""), name)]
        if not mine:
            continue
        one = max(mine, key=lambda item: str(item.get("posted") or ""))
        stage, status = one.get("stage"), one.get("status") or "?"
        if said == LIVE and stage not in (LIVE, DELIVERING, FULFILLED):
            lines.append(_line(one, now, f"Nova's board says live - the hub has them {status}"
                                         if stage != NOT_SET_UP else "Nova's board says live - not on the hub "
                                                                     "for this order"))
        elif said == NOT_SET_UP and stage in (SET_UP, LIVE, DELIVERING, PAUSED):
            lines.append(_line(one, now, f"Nova's board says not set up - the hub has them {status}"))
        elif said == SET_UP and stage == NOT_SET_UP:
            lines.append(_line(one, now, "Nova's board has them on the hub - not for this order"))
    listed = list(board)
    for one in recent.values() if complete else ():
        if one.get("stage") != NOT_SET_UP:
            continue
        posted = _moment(one.get("posted"))
        if posted is None or now - posted < timedelta(hours=1):
            continue
        if not any(same_person(str(one.get("client") or ""), name) for name in listed):
            lines.append(_line(one, now, "paid, not set up, and not on Nova's #launching board"))
    return lines
