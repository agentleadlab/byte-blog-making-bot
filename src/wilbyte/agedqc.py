"""Aged orders, checked after Nova pulls them.

"Ryte doesnt need to create aged leads card anymore, everything is automated
on that part going forward, but i want ryte qc-ing it instead". Nova pulls an
aged order's leads into a sheet and says so in the payment's thread:

    @Rook Kory Parent's aged order is done (INV-21087) - let them know their
    sheet is shared with their email:
    ✅ Aged IUL 30-90: 25 pulled (Arizona 5, Louisiana 5, Texas 5, Virginia
    5, Washington 5) · sheet shared with insuredbykory@gmail.com
    https://docs.google.com/spreadsheets/d/...

That is held up against what was paid for - the payment post's own lines,
"Aged IUL 30-90: 25 @ $8.00 — states AZ, LA, TX, VA, WA" - and, when it can
be opened, the sheet itself. Reads only: nothing here writes anywhere.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .ordercards import US_STATES, _codes
from .state import _state_dir

#: What each aged payment's QC came to, by the payment post's id.
PATH = _state_dir() / "aged-qc.json"

DONE = re.compile(r"\baged\s+order\s+is\s+done\b", re.IGNORECASE)

#: A payment post's order line: "Aged IUL 30-90: 25 @ $8.00 — states AZ, LA".
_ORDERED = re.compile(
    r"^[\s>*•-]*(?P<package>[A-Za-z][A-Za-z0-9 +&/().'-]{1,60}?)\s*:\s*(?P<count>\d[\d,]*)\s*(?:x|@)\s*\$?[\d,.]+"
    r"[^\n—–]*?(?:[—–-]\s*states?\s*:?\s*(?P<states>[^\n]+))?$", re.IGNORECASE | re.MULTILINE)
#: Nova's line for what it pulled: "✅ Aged IUL 30-90: 25 pulled (Arizona 5, ...)".
_PULLED = re.compile(
    r"(?P<mark>✅|⚠️?|❌)?\s*(?P<package>[A-Za-z][A-Za-z0-9 +&/().'-]{1,60}?)\s*:\s*\**(?P<count>\d[\d,]*)\**\s+pulled"
    r"(?:\s+of\s+\**(?P<of>\d[\d,]*)\**)?\s*(?:\((?P<states>[^)]*)\))?", re.IGNORECASE)
_SHARED = re.compile(r"\bshared\s+with\s+(?:their\s+email\s*:?\s*)?([\w.+-]+@[\w-]+(?:\.[\w-]+)+)", re.IGNORECASE)
_SHEET = re.compile(r"https://docs\.google\.com/spreadsheets/d/[\w-]+[^\s)>\]]*")
_POST_EMAIL = re.compile(r"^\W*EMAIL:\**\s*([\w.+-]+@[\w-]+(?:\.[\w-]+)+)", re.IGNORECASE | re.MULTILINE)
_POST_INVOICE = re.compile(r"\bINV-?\d+\b", re.IGNORECASE)
_BUCKET = re.compile(r"\d{2,3}\s*(?:\+|[-–]\s*\d{2,3})")
_FAMILIES = (("vet", r"vet"), ("fex", r"fex|final\s*expense"), ("iul", r"iul"), ("mtg", r"mtg|mortgage"),
             ("widow", r"widow"), ("trucker", r"truck"), ("blue", r"blue\s*collar"), ("spanish", r"spanish"))


@dataclass
class Line:
    package: str
    count: int
    states: list[str] = field(default_factory=list)
    by_state: dict = field(default_factory=dict)
    of: int | None = None


@dataclass
class Checked:
    client: str = ""
    invoice: str = ""
    ok: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    sheets: list[str] = field(default_factory=list)
    #: Said, not a problem: the sheet couldn't be opened to count.
    notes: list[str] = field(default_factory=list)


def _count(said) -> int:
    return int(str(said).replace(",", ""))


def _kind(package: str) -> tuple:
    """What a line is, to pair the payment's with Nova's: the families it
    names and its age - "Aged IUL 30-90" and "Aged IUL 30-90 days" alike."""
    said = str(package or "").casefold()
    bucket = _BUCKET.search(said)
    return (frozenset(name for name, pattern in _FAMILIES if re.search(pattern, said)),
            re.sub(r"\s+", "", bucket.group(0)).replace("–", "-") if bucket else "")


def _state_code(name: str) -> str:
    said = " ".join(str(name or "").split()).casefold()
    if said.upper() in US_STATES:
        return said.upper()
    return next((code for code, full in US_STATES.items() if full == said), "")


def ordered(post: str) -> list[Line]:
    """The payment post's own order lines - aged ones only."""
    found = []
    for one in _ORDERED.finditer(str(post or "")):
        package = " ".join(one.group("package").split())
        if not re.search(r"\baged\b", package, re.IGNORECASE):
            continue
        found.append(Line(package=package, count=_count(one.group("count")), states=_codes(one.group("states") or "")))
    return found


def pulled(done: str) -> list[Line]:
    """What Nova says it pulled, a line each."""
    found = []
    for one in _PULLED.finditer(str(done or "")):
        by_state = {}
        for bit in re.split(r"\s*,\s*", one.group("states") or ""):
            named = re.match(r"\s*(.+?)\s+(\d[\d,]*)\s*$", bit)
            code = _state_code(named.group(1)) if named else ""
            if code:
                by_state[code] = by_state.get(code, 0) + _count(named.group(2))
        found.append(Line(package=" ".join(one.group("package").split()), count=_count(one.group("count")),
                          by_state=by_state, of=_count(one.group("of")) if one.group("of") else None))
    return found


def check(post: str, done: str, *, sheet_states: dict | None = None, sheet_rows: int | None = None,
          sheet_problem: str = "") -> Checked:
    """Nova's pull held up against the payment. `sheet_states` / `sheet_rows`:
    what the sheet holds, when it could be read."""
    result = Checked(invoice=(_POST_INVOICE.search(str(done or "")) or _POST_INVOICE.search(str(post or ""))
                              or [""])[0].upper())
    who = re.search(r"^\W*NAME:\**\s*(.+)$", str(post or ""), re.IGNORECASE | re.MULTILINE)
    result.client = " ".join(who.group(1).split()).strip(" *") if who else ""
    result.sheets = list(dict.fromkeys(_SHEET.findall(str(done or ""))))
    bought, got = ordered(post), pulled(done)
    if not bought:
        result.problems.append("I couldn't read the payment's order lines to check against")
    if not got:
        result.problems.append("I couldn't read what Nova says it pulled")
    left = list(got)
    for line in bought:
        match = next((one for one in left if _kind(one.package) == _kind(line.package)), None)
        if match is None and len(bought) == 1 and len(left) == 1:
            match = left[0]
        if match is None:
            result.problems.append(f"{line.count} {line.package} was paid for and Nova pulled none of it")
            continue
        left.remove(match)
        if match.count != line.count:
            result.problems.append(f"{line.package}: paid for {line.count}, Nova pulled {match.count}")
        else:
            result.ok.append(f"{line.package}: {match.count} pulled, as paid for")
        if line.states:
            outside = [code for code in match.by_state if code not in line.states]
            if outside:
                result.problems.append(f"{line.package}: pulled in {', '.join(outside)}, which they didn't order "
                                       f"(ordered {', '.join(line.states)})")
            elif match.by_state:
                result.ok.append(f"{line.package}: every state pulled is one they ordered")
        if match.by_state and sum(match.by_state.values()) != match.count:
            result.problems.append(f"{line.package}: the states add up to {sum(match.by_state.values())}, "
                                   f"not the {match.count} said")
    for one in left:
        result.problems.append(f"Nova pulled {one.count} {one.package}, which isn't on the payment")
    email = _POST_EMAIL.search(str(post or ""))
    shared = _SHARED.search(str(done or ""))
    if email and shared:
        if shared.group(1).casefold() != email.group(1).casefold():
            result.problems.append(f"The sheet was shared with {shared.group(1)}, but the payment's email is "
                                   f"{email.group(1)}")
        else:
            result.ok.append(f"Shared with {shared.group(1)}, the payment's email")
    elif email and not shared:
        result.problems.append("Nova didn't say who the sheet was shared with")
    if not result.sheets:
        result.problems.append("No sheet link in Nova's message")
    if sheet_problem:
        result.notes.append(f"The sheet itself wasn't checked: {sheet_problem}")
    if sheet_rows is not None:
        want = sum(one.count for one in got)
        if sheet_rows != want:
            result.problems.append(f"The sheet has {sheet_rows} leads, and Nova says it pulled {want}")
        else:
            result.ok.append(f"The sheet has {sheet_rows} leads")
    if sheet_states:
        allowed = {code for one in bought for code in one.states}
        stray = sorted(code for code in sheet_states if allowed and code not in allowed)
        if stray:
            result.problems.append(f"The sheet has leads in {', '.join(stray)}, which they didn't order")
    return result


def sheet_states(rows: list) -> tuple[int, dict]:
    """(leads on the sheet, how many in each state). The state column is the
    one whose cells are states - whatever its heading says, with banners and
    link rows above it: Kory Parent's sheet went "wasn't checked" looking for
    a heading called State. A lead is a row with a state in that column."""
    rows = [list(one) for one in rows or []]
    hits: dict = {}
    for row in rows:
        for at, cell in enumerate(row):
            if _cell_state(cell):
                hits[at] = hits.get(at, 0) + 1
    if not hits:
        return 0, {}
    column = max(hits, key=lambda at: hits[at])
    counts, leads = {}, 0
    for row in rows:
        code = _cell_state(row[column] if column < len(row) else "")
        if code:
            leads += 1
            counts[code] = counts.get(code, 0) + 1
    return leads, counts


def _cell_state(cell) -> str:
    """The state a cell holds - "AZ", "Arizona", "arizona " - or ""."""
    said = " ".join(str(cell or "").split())
    if not said or len(said) > 20:
        return ""
    return said.upper() if said.upper() in US_STATES and said.isupper() else _state_code(said)


def said(result: Checked) -> str:
    """The QC as one message for the board channel."""
    head = (f"🔎 **Aged QC — {result.client or 'an aged order'}**" + (f" ({result.invoice})" if result.invoice else ""))
    if result.problems:
        lines = [head + " — ⚠ doesn't match:", *(f"• {one}" for one in result.problems)]
        lines += [f"-# ✓ {one}" for one in result.ok]
    else:
        lines = [head + " — ✅ matches the payment", *(f"• {one}" for one in result.ok)]
    lines += [f"-# {one}" for one in result.notes]
    lines += [f"<{one}>" for one in result.sheets[:2]]
    return "\n".join(lines)


def recalled() -> dict:
    import json

    try:
        held = json.loads(PATH.read_text())
    except (OSError, ValueError):
        return {}
    return held if isinstance(held, dict) else {}


def remember(post_id: str, result: Checked) -> None:
    """The QC kept by its payment, for the order audit."""
    import json
    from datetime import datetime, timezone

    held = recalled()
    held[str(post_id)] = {"client": result.client, "invoice": result.invoice, "problems": result.problems,
                          "sheets": result.sheets, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(held, indent=1))
