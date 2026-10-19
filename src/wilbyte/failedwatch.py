"""Cards already warned about a failed payment, so each is warned once.

"why did this pushed thru if the payment declined/failed" - Joshua Holt's
Payra payment failed, and an Aged Leads card was made for him by hand
anyway. RYTE watches #failed-payment and says so on any card made for that
client after it; this is where it remembers which ones it has said it on.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from .state import _state_dir

WARNED_PATH = _state_dir() / "failed-payments.json"

#: How long a warning is remembered - longer than the payments are watched.
KEEP_FOR = timedelta(days=7)


def load(path: Path | None = None) -> dict:
    try:
        held = json.loads((path or WARNED_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return held if isinstance(held, dict) else {}


def save(held: dict, path: Path | None = None) -> None:
    where = path or WARNED_PATH
    where.parent.mkdir(parents=True, exist_ok=True)
    spare = where.with_suffix(".tmp")
    spare.write_text(json.dumps(held, indent=1), encoding="utf-8")
    spare.replace(where)


def warned(payment_id: str, card_id: str, held: dict | None = None) -> bool:
    held = load() if held is None else held
    return f"{payment_id}:{card_id}" in held


def note_warned(payment_id: str, card_id: str, now: datetime, path: Path | None = None) -> None:
    """Remember the warning, and forget the ones long past."""
    held = load(path)
    keep = {}
    for key, when in held.items():
        try:
            if now - datetime.fromisoformat(str(when)) <= KEEP_FOR:
                keep[key] = when
        except (TypeError, ValueError):
            continue
    keep[f"{payment_id}:{card_id}"] = now.isoformat(timespec="seconds")
    save(keep, path)
