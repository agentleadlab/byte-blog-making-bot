"""Orders that are live on the hub and have stopped getting leads.

"why didnt it update us with this one" - Bryce Evans' recurring Spark vet
order had no leads from Oct 31 to Nov 9, and nothing said so: Ryte follows
payments, and a recurring order has no new payment to follow. This watches
every live row's delivered count instead, and lists the ones that haven't
moved in three days.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .state import _state_dir

STALLS_PATH = _state_dir() / "hub-stalls.json"

#: A live row with no new lead for this long is stalled. Three days, so a
#: Mon-Fri schedule over a weekend isn't one.
STALL_AFTER = timedelta(days=3)

#: What the hub calls a row that should be getting leads.
LIVE_WORDS = ("live", "active", "running", "delivering", "on")

#: A field on the hub row saying when its last lead went, when there is one.
_LAST_LEAD = re.compile(r"last.*(?:lead|deliver|sent)|(?:lead|deliver).*last", re.IGNORECASE)


def _load(path: Path | None = None) -> dict:
    try:
        held = json.loads((path or STALLS_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return held if isinstance(held, dict) else {}


def _save(held: dict, path: Path | None = None) -> None:
    where = path or STALLS_PATH
    where.parent.mkdir(parents=True, exist_ok=True)
    where.write_text(json.dumps(held, indent=1), encoding="utf-8")


def _when(said) -> datetime | None:
    try:
        at = datetime.fromisoformat(str(said).replace("Z", "+00:00"))
    except ValueError:
        return None
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


def _last_lead(agent) -> datetime | None:
    """When the hub says the row's last lead went, if it says."""
    for key, said in (getattr(agent, "raw", None) or {}).items():
        if said and _LAST_LEAD.search(str(key)):
            found = _when(said)
            if found is not None:
                return found
    return None


def live(agent) -> bool:
    status = str(getattr(agent, "status", "") or "").casefold()
    return any(word == status or status.startswith(word + " ") for word in LIVE_WORDS)


def watch(agents, *, now: datetime, today=None, path: Path | None = None) -> list[tuple[object, datetime]]:
    """[(agent, quiet since)] for live rows with leads still to come and none
    for STALL_AFTER. Each look writes down every live row's count, so a row is
    "quiet since" the last time its count moved."""
    held = _load(path)
    seen, out = {}, []
    for agent in agents or []:
        key = str(getattr(agent, "id", "") or "")
        if not key or not live(agent):
            continue
        delivered = getattr(agent, "delivered", None)
        before = held.get(key) or {}
        since = _when(before.get("since")) if before.get("delivered") == delivered else None
        last = _last_lead(agent)
        if last is not None:
            since = last
        since = since or now
        seen[key] = {"delivered": delivered, "since": since.isoformat(timespec="seconds"),
                     "name": getattr(agent, "name", "")}
        ordered = getattr(agent, "ordered", None)
        start = getattr(agent, "start_date", None)
        owed = ordered is None or delivered is None or delivered < ordered
        started = start is None or today is None or start <= today
        if owed and started and now - since >= STALL_AFTER:
            out.append((agent, since))
    _save(seen, path)
    return sorted(out, key=lambda one: one[1])


def lines(stalled, *, now: datetime) -> list[str]:
    """One line a stalled row, the longest quiet first."""
    out = []
    for agent, since in stalled:
        days = (now - since).days
        got = getattr(agent, "delivered", None)
        of = getattr(agent, "ordered", None)
        count = f"{got} of {of} delivered" if got is not None and of is not None else (
            f"{got} delivered" if got is not None else "")
        bits = [one for one in (f"row {agent.id}", getattr(agent, "lead_type", "") or "", count) if one]
        out.append(f"• **{agent.name or '?'}** — live, no new leads in {days} days ({' · '.join(bits)})")
    return out
