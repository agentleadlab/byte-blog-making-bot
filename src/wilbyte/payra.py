"""Every Payra payment RYTE has seen, remembered, for looking an agent up.

"we use stripe and payra". Payra announces each payment in the payments
channel - name, email, phone, amount, product - and RYTE read those only for
the Levinson tracker, keeping nothing. An agent texting "did my payment go
through?" is a question the channel has already answered; this is where the
answer is kept, so the RingCentral drafts can check it.

Found by phone (the last ten digits), by email, or by the whole name - never
by a part of one: a payment that is somebody else's is worse than none.

Bounded, and never the only copy: the channel is still there to read again.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .state import _state_dir

PAYRA_PATH = _state_dir() / "payra-payments.json"

#: How many payments are kept, newest. Years of them.
KEEP = 20000


def load(path: Path | None = None) -> dict:
    """{"newest": message id, "payments": {message id: {...}}}."""
    where = path or PAYRA_PATH
    try:
        data = json.loads(where.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("newest", "")
    data.setdefault("payments", {})
    return data


def save(data: dict, path: Path | None = None) -> None:
    where = path or PAYRA_PATH
    try:
        where.parent.mkdir(parents=True, exist_ok=True)
        where.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def remember(data: dict, message_id, paid) -> None:
    """One payment, by the message that announced it - seen twice, kept once."""
    key = str(message_id)
    data["payments"][key] = {
        "name": paid.name, "email": paid.email, "phone": paid.phone,
        "cents": paid.cents, "product": paid.product,
        "at": paid.paid_at.isoformat() if isinstance(paid.paid_at, datetime) else str(paid.paid_at),
    }
    if key.isdigit() and (not str(data.get("newest") or "").isdigit() or int(key) > int(data["newest"])):
        data["newest"] = key
    if len(data["payments"]) > KEEP:
        for old in sorted(data["payments"], key=lambda one: str(data["payments"][one].get("at")))[:-KEEP]:
            data["payments"].pop(old, None)


def _ten(number) -> str:
    only = "".join(one for one in str(number or "") if one.isdigit())
    return only[-10:] if len(only) >= 10 else ""


def find(data: dict, who: str) -> list[dict]:
    """Their payments, newest first - by phone, email, or whole name."""
    from .clearout import name_match

    who = " ".join(str(who or "").split())
    if not who:
        return []
    phone, email = _ten(who), who.casefold() if "@" in who else ""
    found = []
    for one in (data.get("payments") or {}).values():
        if phone and _ten(one.get("phone")) == phone:
            found.append(one)
        elif email and str(one.get("email") or "").casefold() == email:
            found.append(one)
        elif not phone and not email and name_match(who, str(one.get("name") or "")) >= 2:
            found.append(one)
    return sorted(found, key=lambda one: str(one.get("at") or ""), reverse=True)


def described(payments: list[dict], *, most: int = 8) -> str:
    """As lines for a draft to read."""
    lines = []
    for one in payments[:most]:
        dollars = f"${int(one.get('cents') or 0) / 100:,.2f}"
        lines.append(
            f"- {str(one.get('at') or '?')[:10]} {dollars}"
            + (f" for {one['product']}" if one.get("product") else "")
            + f" - {one.get('name') or '?'} <{one.get('email') or '?'}>"
        )
    return "\n".join(lines)
