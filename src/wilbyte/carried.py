"""How many days each unfinished item has been carried forward.

`dailyops` already knows what to do with this - an item rolled three nights
running is flagged rather than moved again - but nothing was counting, so the
flag never fired and a task could walk from Monday to Friday without anybody
noticing it had.

That is the failure the rollover risks introducing. Doing it by hand is slow
and error-prone, but somebody retyping the same item onto tomorrow's card for
the fourth time notices they are doing it. Automating the moving without
counting the moves takes that away and gives nothing back.

The count is keyed by `dailyops.item_key` - card kind, person, item text -
which is stable across days as long as nobody rewrites the wording. When they
do, the count restarts, which is the safe direction to be wrong in.
"""

from __future__ import annotations

import json
from pathlib import Path

from .state import _state_dir

CARRIED_PATH = _state_dir() / "carried-items.json"

# Kept for a fortnight. An item nobody has seen in two weeks is not being
# carried any more - it was finished, or renamed, or the card stopped existing.
KEEP_DAYS = 14


def load(path: Path | None = None) -> dict:
    """{item key: {"count": n, "last": "2026-08-24"}}."""
    where = path or CARRIED_PATH
    if not where.exists():
        return {}
    try:
        data = json.loads(where.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(counts: dict, path: Path | None = None) -> None:
    where = path or CARRIED_PATH
    where.parent.mkdir(parents=True, exist_ok=True)
    where.write_text(json.dumps(counts, indent=2, sort_keys=True), encoding="utf-8")


def history(path: Path | None = None) -> dict[str, int]:
    """Just the counts, in the shape `dailyops.plan_rollover` wants."""
    return {key: int(entry.get("count", 0)) for key, entry in load(path).items()}


def record(keys: list[str], on, path: Path | None = None) -> dict:
    """Count a day's carry for each item, and forget the ones long gone.

    Counted once per day, not once per rollover: running it twice in an
    evening is somebody checking their work, not the item being two days
    older.
    """
    day = on.isoformat() if hasattr(on, "isoformat") else str(on)
    counts = load(path)
    for key in keys:
        entry = counts.get(key) or {"count": 0, "last": ""}
        if entry.get("last") != day:
            entry["count"] = int(entry.get("count", 0)) + 1
            entry["last"] = day
        counts[key] = entry
    counts = _forget_old(counts, on)
    save(counts, path)
    return counts


def clear(keys: list[str], path: Path | None = None) -> None:
    """Stop counting items that got done. A finished task is not carried."""
    counts = load(path)
    for key in keys:
        counts.pop(key, None)
    save(counts, path)


def _forget_old(counts: dict, today) -> dict:
    from datetime import date as _date, timedelta

    if not hasattr(today, "isoformat"):
        return counts
    cutoff = today - timedelta(days=KEEP_DAYS)
    kept = {}
    for key, entry in counts.items():
        try:
            last = _date.fromisoformat(str(entry.get("last") or ""))
        except ValueError:
            continue
        if last >= cutoff:
            kept[key] = entry
    return kept
