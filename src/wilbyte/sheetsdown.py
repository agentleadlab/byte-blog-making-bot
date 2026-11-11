"""Google Sheets down, told once - not as a "couldn't open the sheet" on
every payment.

"ryte needs to know that excel is down": Joseph Schuler, Jonathan Carter and
the rest each got "Google said no to that sheet (403)" on their card, with
Therese, Nicole and Franklin tagged on every one. Three different sheets
refused within twenty minutes, and nothing opening in between, is Google (or
Ryte's sign-in to it), not three clients' sheets.
"""

from __future__ import annotations

import time

#: Different sheets refused, inside the window, that make it an outage.
DOWN_AFTER = 3
WINDOW = 20 * 60

#: [(when, sheet id)] refused since the last sheet that opened.
_FAILED: list = []
#: When it went down (monotonic), or 0.0.
_DOWN_SINCE = 0.0
#: (state, when) to say in #agent-setup: ("down", wall time) / ("up", wall time).
_TO_SAY: list = []


def failed(sheet_id: str, *, now: float | None = None) -> None:
    """A sheet that wouldn't open."""
    global _DOWN_SINCE
    now = time.monotonic() if now is None else now
    _FAILED[:] = [one for one in _FAILED if now - one[0] <= WINDOW]
    _FAILED.append((now, str(sheet_id or "")))
    if not _DOWN_SINCE and len({one[1] for one in _FAILED}) >= DOWN_AFTER:
        _DOWN_SINCE = now
        _TO_SAY.append(("down", time.time()))


def opened(*, now: float | None = None) -> None:
    """A sheet that opened: whatever was wrong isn't any more."""
    global _DOWN_SINCE
    _FAILED.clear()
    if _DOWN_SINCE:
        _DOWN_SINCE = 0.0
        _TO_SAY.append(("up", time.time()))


def down() -> bool:
    return bool(_DOWN_SINCE)


def to_say() -> list:
    """What changed since last asked - said once each."""
    out = list(_TO_SAY)
    _TO_SAY.clear()
    return out


def reset() -> None:
    global _DOWN_SINCE
    _FAILED.clear()
    _TO_SAY.clear()
    _DOWN_SINCE = 0.0
