"""The Distro Hub's fulfilled agents, against their own lead sheets.

"i want ryte to be able to double check the clients sheet when they are
tagged as fulfilled if they really received the right amount". The hub says
an order is done - 40/40 - when it marks it so or when its own count reaches
the order. The sheet is what the agent actually has. This reads both and says
where they disagree.

Read only, both ways. The hub's API is GET-only by design (Nova built it so),
and the sheets are only read. The token goes in a header, never in a URL and
never in anything RYTE says or logs.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

from .state import _state_dir

#: Under /distro, where the hub itself lives - not /api/agents, which is what
#: was asked for and answers 404.
DEFAULT_URL = "https://hub.agentleadlab.com/distro/api/agents"

#: How far back a fulfilled order is checked on its own. "fulfilled_at" is the
#: row's last save, so an old order touched today comes back as fresh - the
#: id, start and order size are what make it the same one (see `key`).
LOOK_BACK = timedelta(days=3)

#: Sheets read in one pass. Each is a call to Google; the rest wait for the
#: next pass rather than all landing at once.
MOST_PER_PASS = 15

CONNECT_PAUSES = (2.0, 5.0, 10.0)


#: The hub's "Done" tab - orders already closed out and moved on from. Not
#: what's being checked: "joevanny is not even here" - the check is for the
#: hub's Fulfilled tab, the 52 waiting to be closed. The API's own
#: `fulfilled` flag is true for both.
DONE_WORDS = ("done", "complete", "completed", "ended", "archived")


class HubError(RuntimeError):
    pass


@dataclass
class HubAgent:
    id: str
    name: str
    status: str = ""
    fulfilled: bool = False
    lead_type: str = ""
    start_date: date | None = None
    delivered: int | None = None
    ordered: int | None = None
    progress: str = ""
    daily_cap: int | None = None
    sheet_url: str = ""
    fulfilled_at: datetime | None = None
    updated_at: datetime | None = None


def _int(said) -> int | None:
    try:
        return int(str(said).strip())
    except (TypeError, ValueError):
        return None


def _day(said) -> date | None:
    moment = _moment(said)
    if moment is not None:
        return moment.date()
    try:
        return date.fromisoformat(str(said or "").strip()[:10])
    except ValueError:
        return None


def _moment(said) -> datetime | None:
    text = str(said or "").strip()
    if not text or len(text) <= 10:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def read(record: dict) -> HubAgent:
    """One agent as the API gives it, every field optional."""
    delivered, ordered = _int(record.get("delivered")), _int(record.get("ordered"))
    progress = str(record.get("progress") or "")
    if (delivered is None or ordered is None) and "/" in progress:
        got, of = (part.strip() for part in progress.split("/", 1))
        delivered = delivered if delivered is not None else _int(got)
        ordered = ordered if ordered is not None else _int(of)
    return HubAgent(
        id=str(record.get("id") or ""),
        name=" ".join(str(record.get("name") or record.get("client_facing_name") or "").split()),
        status=str(record.get("status") or ""),
        # The Fulfilled tab, not the Done one: the flag is true for both.
        fulfilled=(bool(record.get("fulfilled")) or str(record.get("status") or "").casefold() == "fulfilled")
        and str(record.get("status") or "").casefold() not in DONE_WORDS,
        lead_type=str(record.get("lead_type_label") or record.get("lead_type") or ""),
        start_date=_day(record.get("start_date")),
        delivered=delivered, ordered=ordered, progress=progress,
        daily_cap=_int(record.get("daily_cap")),
        sheet_url=str(record.get("sheet_url") or "").strip(),
        fulfilled_at=_moment(record.get("fulfilled_at")),
        updated_at=_moment(record.get("updated_at")),
    )


def _records(body) -> list[dict]:
    """The list of agents, wherever in the reply it is."""
    if isinstance(body, list):
        return [one for one in body if isinstance(one, dict)]
    if isinstance(body, dict):
        for name in ("agents", "data", "results", "items"):
            if isinstance(body.get(name), list):
                return [one for one in body[name] if isinstance(one, dict)]
    return []


def agents(secrets, *, status: str = "fulfilled", timeout: float = 30.0) -> list[HubAgent]:
    """The hub's agents with that status. Raises HubError with a sentence
    about what to do - never with the token in it."""
    token = (getattr(secrets, "hub_api_token", "") or "").strip()
    if not token:
        raise HubError("No HUB_API_TOKEN in .env - the key Nova made for RYTE goes there.")
    url = (getattr(secrets, "hub_api_url", "") or "").strip() or DEFAULT_URL
    for pause in (*CONNECT_PAUSES, None):
        try:
            reply = httpx.get(
                url, params={"status": status}, timeout=timeout,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
            break
        except httpx.ConnectError as exc:
            if pause is None:
                raise HubError(f"Couldn't reach the hub: {type(exc).__name__}") from None
            time.sleep(pause)
        except httpx.HTTPError as exc:
            raise HubError(f"Couldn't reach the hub: {type(exc).__name__}") from None
    if reply.status_code in (401, 403):
        raise HubError("The hub refused RYTE's key - check HUB_API_TOKEN in .env against "
                       "the one Nova gave.")
    if reply.status_code == 404:
        raise HubError(f"The hub has nothing at {url} (404). Its API is at {DEFAULT_URL} - "
                       "fix HUB_API_URL in .env, or delete that line to use it.")
    if reply.status_code >= 400:
        raise HubError(f"The hub answered {reply.status_code}.")
    try:
        body = reply.json()
    except ValueError:
        raise HubError("The hub's answer wasn't JSON.") from None
    return [read(one) for one in _records(body)]


# --------------------------------------------------------------- the check

@dataclass
class Checked:
    """One fulfilled order against its sheet."""

    agent: HubAgent
    #: The sheet's rows, from `delivery.count_rows`.
    counted: object = None
    problems: list = field(default_factory=list)

    @property
    def on_sheet(self) -> int | None:
        """This order's leads on the sheet: those dated since it started, when
        the sheet dates them, else every lead on it."""
        if self.counted is None:
            return None
        since = self.counted.since(self.agent.start_date)
        return since if since is not None else self.counted.rows

    @property
    def dated(self) -> bool:
        return self.counted is not None and self.counted.since(self.agent.start_date) is not None

    def short_by(self) -> int:
        got, wanted = self.on_sheet, self.agent.ordered
        if got is None or wanted is None:
            return 0
        return max(wanted - got, 0)

    def hub_short(self) -> int:
        """What the hub itself says it never sent - "19/21" marked done."""
        if self.agent.delivered is None or self.agent.ordered is None:
            return 0
        return max(self.agent.ordered - self.agent.delivered, 0)

    def missing_from_sheet(self) -> int:
        """What the hub says it sent that isn't on the sheet."""
        if self.on_sheet is None or self.agent.delivered is None:
            return 0
        return max(self.agent.delivered - self.on_sheet, 0)


def describe(one: Checked) -> str:
    agent = one.agent
    hub = agent.progress or (
        f"{agent.delivered}/{agent.ordered}" if agent.delivered is not None and agent.ordered is not None
        else "?")
    got = one.on_sheet
    if got is None:
        mark, said = "•", "sheet not read" + (f" ({one.problems[0]})" if one.problems else "")
    else:
        mark = "⚠" if one.short_by() else "✅"
        tab = getattr(one.counted, "tab", "")
        said = (f"the sheet has **{got}**"
                + (f" on “{tab}”" if tab else "")
                + (f" since {agent.start_date:%b %-d}" if one.dated and agent.start_date else "")
                + ("" if one.dated else " (all of it - it doesn't date its leads)"))
        if one.short_by():
            said += f" — **{one.short_by()} short**"
            # Which side it's on: the hub closing an order it never filled,
            # or leads the hub sent that never reached the sheet.
            why = []
            if one.hub_short():
                why.append(f"the hub marked it done having sent {agent.delivered} of {agent.ordered}")
            if one.missing_from_sheet():
                why.append(f"{one.missing_from_sheet()} the hub says it sent aren't on the sheet")
            if why:
                said += " (" + "; ".join(why) + ")"
    line = (f"{mark} **{agent.name or agent.id}**" + (f" ({agent.lead_type})" if agent.lead_type else "")
            + f" — hub says {hub}, {said}")
    if agent.sheet_url:
        line += f" · [sheet](<{agent.sheet_url}>)"
    return line


def key(agent: HubAgent) -> str:
    """Which order this is. Not `fulfilled_at`: that's the row's last save,
    and a later edit would make the same order look new."""
    return f"{agent.id}|{agent.start_date or ''}|{agent.ordered}"


def due(found: list[HubAgent], *, now: datetime, said: dict) -> list[HubAgent]:
    """Fulfilled lately, with a sheet, and not already checked."""
    fresh = []
    for one in found:
        when = one.fulfilled_at or one.updated_at
        if not one.fulfilled or not one.sheet_url or key(one) in said:
            continue
        if when is not None and when < now - LOOK_BACK:
            continue
        fresh.append(one)
    return fresh[:MOST_PER_PASS]


def named(found: list[HubAgent], who: str) -> list[HubAgent]:
    """The agents whose name has every word asked for."""
    words = [one for one in who.casefold().split() if one]
    return [one for one in found if words and all(word in one.name.casefold().split() for word in words)]


SEEN_PATH = _state_dir() / "hub-checked.json"


def checked(path: Path | None = None) -> dict:
    """{order key: leads on the sheet when it was checked}."""
    try:
        held = json.loads((path or SEEN_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return held if isinstance(held, dict) else {}


def remember(keys: dict, path: Path | None = None) -> None:
    where = path or SEEN_PATH
    held = {**checked(where), **{str(k): v for k, v in keys.items()}}
    where.parent.mkdir(parents=True, exist_ok=True)
    spare = where.with_suffix(".tmp")
    spare.write_text(json.dumps(held, indent=1), encoding="utf-8")
    spare.replace(where)
