"""The Thanksgiving sale: Lead Type | English's prices changed and put back.

"Tuesday 24 Nov, 1:00 AM ... edit each option's text to the sale price. Change
only the price, leave the wording exactly as it is ... Friday 27 Nov, 11:59 PM
- back to the list you screenshotted Monday." Franklin's out Wednesday to
Friday, and Friday is the one that can't be missed.

So Ryte keeps the field's options exactly as they were before it touches
anything, changes only the price in each, reads the field back after every
change, and puts the saved list back on Friday. Item by item, so an option
somebody adds in between is kept rather than wiped.

Prices:
- **fresh** (English): Franklin's table, as written - $43 and $45 are 25% off,
  the rest 30%. A fresh price not in the table is left alone and said.
- **aged** (English): half of whatever it says.
- anything with **SPANISH** in it, fresh or aged: 10% off.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from .state import _state_dir

PATH = _state_dir() / "sale-prices.json"

FIELD_NAME = "Lead Type | English"
ZONE = ZoneInfo("America/Chicago")  # Memphis, for saying the times
# The same moments as the Black Friday code on leadlabcrm.com, which runs
# 1:00 AM to 11:59:59 PM *Eastern* - so the page and the order form's
# dropdown change together rather than an hour apart.
EASTERN = ZoneInfo("America/New_York")
APPLY_AT = datetime(2026, 11, 24, 1, 0, tzinfo=EASTERN)
RESTORE_AT = datetime(2026, 11, 28, 0, 0, tzinfo=EASTERN)

#: English fresh, as Franklin wrote it.
FRESH = {"28": "19.60", "32": "22.40", "40": "28.00", "43": "32.25", "45": "33.75", "52": "36.40", "60": "42.00"}

_PRICE = re.compile(r"\$\s?(\d+(?:\.\d{1,2})?)")


def _money(value: Decimal, *, whole_if_even: bool) -> str:
    value = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if whole_if_even and value == value.to_integral_value():
        return str(int(value))
    return f"{value:.2f}"


def sale_text(option: str) -> tuple[str | None, str]:
    """(the option with its sale price, "") or (None, why it's left alone)."""
    prices = _PRICE.findall(option or "")
    if len(prices) != 1:
        return None, ("no price in it" if not prices else "more than one price in it")
    was = prices[0]
    said = option.casefold()
    if "spanish" in said:
        now = _money(Decimal(was) * Decimal("0.9"), whole_if_even=False)
    elif "aged" in said:
        now = _money(Decimal(was) / 2, whole_if_even=True)
    else:
        key = was[:-3] if was.endswith(".00") else was
        if key not in FRESH:
            return None, f"fresh ${was} isn't in the sale table"
        now = FRESH[key]
    match = _PRICE.search(option)
    return option[:match.start(1)] + now + option[match.end(1):], ""


def plan(options: list[str]) -> tuple[list[str], list[str]]:
    """(the options at sale prices, in the same order; what was left alone and why)."""
    out, left = [], []
    for option in options:
        new, why = sale_text(option)
        out.append(new if new is not None else option)
        if new is None:
            left.append(f"{option} - {why}")
    return out, left


def _price(option: str) -> str:
    found = _PRICE.findall(option or "")
    return found[0] if len(found) == 1 else ""


def _words(option: str) -> set:
    return set(re.findall(r"[a-z]+", _PRICE.sub(" ", option or "").casefold()))


def renamed_from(option: str, at: int, before: list[str], used: set) -> int | None:
    """Which of `before` this option is, renamed - "Text-Verified IUL Leads"
    made "Text-Verified IUL Plus Leads" during the sale. Same price, and
    either the same place in the list or most of the same words. None if it
    isn't one of them (a new option)."""
    price = _price(option)
    if not price:
        return None
    if at < len(before) and at not in used and _price(before[at]) == price:
        return at
    mine = _words(option)
    best = None
    for index, one in enumerate(before):
        if index in used or _price(one) != price:
            continue
        theirs = _words(one)
        overlap = len(mine & theirs) / max(1, len(mine | theirs))
        if overlap >= 0.6 and (best is None or overlap > best[0]):
            best = (overlap, index)
    return best[1] if best else None


def swap(current: list[str], before: list[str], after: list[str]) -> list[str]:
    """`current` with each option that's in `before` turned into its `after`.
    An option renamed since keeps its new name and gets only its price
    changed; anything else - an option added since - is kept as it is."""
    turn = {one: two for one, two in zip(before, after)}
    used = {before.index(option) for option in current if option in turn}
    out = []
    for at, option in enumerate(current):
        if option in turn:
            out.append(turn[option])
            continue
        index = renamed_from(option, at, before, used)
        if index is None or not _price(after[index]):
            out.append(option)
            continue
        used.add(index)
        match = _PRICE.search(option)
        out.append(option[:match.start(1)] + _price(after[index]) + option[match.end(1):])
    return out


def renames(current: list[str], before: list[str]) -> list[tuple[str, str]]:
    """[(what it was, what it's called now)] - for saying so."""
    used = {before.index(option) for option in current if option in before}
    out = []
    for at, option in enumerate(current):
        if option in before:
            continue
        index = renamed_from(option, at, before, used)
        if index is not None:
            used.add(index)
            out.append((before[index], option))
    return out


# ------------------------------------------------------------------ memory

def load(path: Path | None = None) -> dict:
    try:
        held = json.loads((path or PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return held if isinstance(held, dict) else {}


def save(held: dict, path: Path | None = None) -> None:
    where = path or PATH
    where.parent.mkdir(parents=True, exist_ok=True)
    spare = where.with_suffix(".tmp")
    spare.write_text(json.dumps(held, indent=1), encoding="utf-8")
    spare.replace(where)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


#: Tries at a change before Ryte stops and leaves it to a person.
MOST_TRIES = 3


def due(held: dict, now: datetime) -> str:
    """"apply", "restore" or "" - what the clock says to do now."""
    if not held.get("original") or held.get("cancelled"):
        return ""
    if now >= RESTORE_AT:
        if held.get("applied_at") and not held.get("restored_at") and held.get("tries_restore", 0) < MOST_TRIES:
            return "restore"
        return ""
    if now >= APPLY_AT and not held.get("applied_at") and held.get("tries_apply", 0) < MOST_TRIES:
        return "apply"
    return ""


def looks_on_sale(options: list[str]) -> bool:
    """Already at sale prices - so not a list to restore to."""
    sale = {f"${one}" for one in FRESH.values()}
    return any(price in option for option in options for price in sale)


# ------------------------------------------------------------------- GHL

def options_of(field: dict) -> list[str]:
    raw = field.get("picklistOptions") or field.get("options") or []
    return [str(one.get("label") or one.get("value") or "") if isinstance(one, dict) else str(one) for one in raw]


def find_field(client, name: str = FIELD_NAME) -> tuple[dict | None, list[str]]:
    """(the field, []) or (None, the names that look close)."""
    got = client._request("GET", f"/locations/{client.location_id}/customFields", params={"model": "all"})
    fields = got.get("customFields") or got.get("data") or []
    wanted = " ".join(name.casefold().split())
    for field in fields:
        if " ".join(str(field.get("name") or "").casefold().split()) == wanted:
            return field, []
    close = [str(field.get("name") or "") for field in fields if "lead type" in str(field.get("name") or "").casefold()]
    return None, close


def read_field(client, field_id: str) -> dict:
    got = client._request("GET", f"/locations/{client.location_id}/customFields/{field_id}")
    return got.get("customField") or got


#: The shapes GHL might take new options in. Its documentation lists none for
#: a dropdown, so each is tried and the field read back.
SHAPES = ("options", "picklistOptions")


def write_options(client, field: dict, options: list[str], *, shape: str = "") -> str:
    """Set the field's options, read back. The shape that worked, or raises."""
    from .ghl import GHLError

    keep = {key: field[key] for key in ("name", "placeholder", "position", "model") if field.get(key) not in (None, "")}
    tried = []
    for one in ([shape] if shape else list(SHAPES)):
        try:
            client._request("PUT", f"/locations/{client.location_id}/customFields/{field['id']}",
                            json={**keep, one: options})
        except GHLError as exc:
            tried.append(f"{one}: {str(exc)[:160]}")
            continue
        if options_of(read_field(client, field["id"])) == options:
            return one
        tried.append(f"{one}: accepted, but the field didn't change")
    raise GHLError("GHL wouldn't take the new options - " + "; ".join(tried))


def test_run(client) -> tuple[str, list[str]]:
    """Make a throwaway dropdown, change one option, read it back, delete it.
    (the shape that worked or "", what happened)."""
    from .ghl import GHLError

    said = []
    start = ["RYTE TEST A [FRESH - $40/Lead]", "RYTE TEST B [AGED - $7/Lead]"]
    made = client._request("POST", f"/locations/{client.location_id}/customFields",
                           json={"name": "RYTE TEST - delete me", "dataType": "SINGLE_OPTIONS",
                                 "options": start, "model": "contact"})
    field = made.get("customField") or made
    field_id = str(field.get("id") or "")
    if not field_id:
        return "", ["GHL didn't give the test field back."]
    shape = ""
    try:
        field = read_field(client, field_id)
        said.append(f"Made a test dropdown with {len(options_of(field))} options.")
        new, _ = plan(options_of(field) or start)
        try:
            shape = write_options(client, field, new)
            said.append(f"✅ Changed its prices and read them back: {', '.join(new)}")
        except GHLError as exc:
            said.append(f"❌ {exc}")
    finally:
        try:
            client._request("DELETE", f"/locations/{client.location_id}/customFields/{field_id}")
            said.append("Deleted the test field.")
        except GHLError as exc:
            said.append(f"⚠️ Couldn't delete the test field \"RYTE TEST - delete me\" - delete it in Settings → "
                        f"Custom Fields. ({str(exc)[:120]})")
    return shape, said
