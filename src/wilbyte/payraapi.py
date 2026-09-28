"""Payra's invoices and payments, read through its API and kept in step.

`@RYTE payra test` showed what the token reads: every invoice with its
customer (name, phone, email), its total, its dates and the payments made on
it; every payment with its amount, status, date and invoice. The lists give
what changed after a time, a page at a time, and say where to carry on
("next_updated_after") - so RYTE keeps a copy, and asks only for what has
changed since.

Read only. The client here has one method, and it is a GET.

What is kept is what a reply to an agent needs - who, how much, when, paid or
not - not Payra's whole record.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from .state import _state_dir

API_PATH = _state_dir() / "payra-api.json"
BASE = "https://api.payra.com/api/v3.1"

#: How far back the first read goes.
FIRST_DAYS = 365

#: Pages read per kind in one pass. A year of invoices is a few pages.
MOST_PAGES = 20

#: What is kept of each record. Raised when more is kept, so everything
#: already held is read again with it - a rebuttal wants the card's last four
#: and the transaction id, which the first copy did not keep.
SHAPE = 2


class PayraError(RuntimeError):
    pass


def when(moment: datetime) -> str:
    """A time as Payra takes it: "2026-08-26T23:02:07.316Z"."""
    moment = moment.astimezone(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


class PayraClient:
    """The site's lists, read. Nothing else - there is no method that writes."""

    def __init__(self, token: str, site_id: str, *, timeout: float = 60.0):
        import httpx

        if not token or not site_id:
            raise PayraError("PAYRA_API_TOKEN and PAYRA_SITE_ID both need to be in .env.")
        self.site_id = site_id
        self._http = httpx.Client(timeout=timeout, headers={
            "x-access-token": token, "Accept": "application/json",
        })

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._http.close()

    def changed(self, kind: str, after: str) -> dict:
        """One page of `kind` ("invoices", "payments") changed after a time."""
        url = f"{BASE}/site/{self.site_id}/{kind}?updated_after={quote(after, safe=':')}"
        got = self._http.get(url)
        if got.status_code in (401, 403):
            raise PayraError("Payra didn't accept the token - check PAYRA_API_TOKEN in .env.")
        if got.status_code >= 400:
            try:
                said = "; ".join(str(one) for one in (got.json().get("errors") or []))
            except ValueError:
                said = got.text[:200]
            raise PayraError(f"Payra said HTTP {got.status_code}: {said}")
        return got.json()


def load(path: Path | None = None) -> dict:
    where = path or API_PATH
    try:
        data = json.loads(where.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    for key in ("cursors", "invoices", "payments"):
        data.setdefault(key, {})
    return data


def save(data: dict, path: Path | None = None) -> None:
    where = path or API_PATH
    where.parent.mkdir(parents=True, exist_ok=True)
    spare = where.with_suffix(".tmp")
    spare.write_text(json.dumps(data), encoding="utf-8")
    spare.replace(where)


def _digits(number) -> str:
    only = "".join(one for one in str(number or "") if one.isdigit())
    return only[-10:] if len(only) >= 10 else ""


def _scalars(one, *, most: int = 160) -> str:
    """A small record of unknown shape - an invoice line, a notification - as
    one line of its plain values."""
    if not isinstance(one, dict):
        return " ".join(str(one or "").split())[:most]
    said = [
        f"{key}: {value}" for key, value in one.items()
        if isinstance(value, (str, int, float, bool)) and str(value).strip()
        and not str(key).startswith("_") and key not in ("id", "external_id")
    ]
    return "; ".join(said)[:most]


def slim_invoice(record: dict) -> dict:
    who = record.get("customer") or {}
    totals = record.get("totals") or {}
    name = " ".join(bit for bit in (who.get("first_name"), who.get("last_name")) if bit) \
        or who.get("display_name") or who.get("account_name") or ""
    return {
        "id": str(record.get("_id") or record.get("id") or ""),
        "number": str(record.get("invoice_number") or record.get("reference_number") or ""),
        "total": totals.get("total"),
        "description": str(record.get("description") or "")[:120],
        "invoice_date": str(record.get("invoice_date") or "")[:10],
        "due_date": str(record.get("due_date") or "")[:10],
        "active": record.get("active", True),
        "name": str(name).strip(),
        "email": str(who.get("email") or "").strip().casefold(),
        "phone": _digits(who.get("mobile_phone")),
        "lines": [_scalars(one) for one in (record.get("lines") or [])[:6]],
        "sent": [_scalars(one, most=120) for one in (record.get("notifications") or [])[:6]],
        "created": str(record.get("created_at") or ""),
        "updated": str(record.get("updated_at") or ""),
    }


def slim_payment(record: dict) -> dict:
    return {
        "id": str(record.get("_id") or record.get("id") or ""),
        "amount": record.get("amount"),
        "paid_on": str(record.get("paid_on") or record.get("created_at") or ""),
        "status": str(record.get("status") or ""),
        "refund": bool(record.get("is_refund")),
        "how": str(record.get("display_name") or record.get("payment_sub_type") or record.get("payment_type") or ""),
        "customer": str(record.get("customer_display") or ""),
        "invoice_number": str(record.get("invoice_number") or ""),
        "invoice_id": str((record.get("invoice") or {}).get("_id") or ""),
        "last_4": str(record.get("last_4") or ""),
        "transaction": str(record.get("gateway_transaction_id") or ""),
        "reference": str(record.get("reference_number") or ""),
        "kind": " ".join(str(record.get(key) or "") for key in ("payment_type", "payment_sub_type")).strip(),
        "by": str(record.get("initiated_by") or ""),
        "updated": str(record.get("updated_at") or ""),
    }


SLIM = {"invoices": slim_invoice, "payments": slim_payment}


def sync(data: dict, client, *, now: datetime | None = None) -> dict:
    """Read what changed since last time into `data`. {kind: how many}.

    Page after page from where the last one said to carry on, until a page
    comes back short. A page that fails stops that kind for this pass and
    keeps its place, so nothing is skipped - the next pass carries on from it.
    """
    now = now or datetime.now(timezone.utc)
    if data.get("shape") != SHAPE:
        data["cursors"] = {}
        data["shape"] = SHAPE
    counts = {}
    for kind, slim in SLIM.items():
        after = str(data["cursors"].get(kind) or when(now - timedelta(days=FIRST_DAYS)))
        count = 0
        for _page in range(MOST_PAGES):
            got = client.changed(kind, after)
            records = got.get("records") or []
            for record in records:
                one = slim(record)
                if one["id"]:
                    data[kind][one["id"]] = one
            count += len(records)
            onward = str(got.get("next_updated_after") or "")
            stuck = onward == after
            if onward:
                after = onward
            data["cursors"][kind] = after
            limit = got.get("limit")
            if not records or not onward or stuck or (isinstance(limit, int) and len(records) < limit):
                break
        counts[kind] = count
    return counts


def _money(amount) -> str:
    try:
        return f"${float(amount):,.2f}"
    except (TypeError, ValueError):
        return "$?"


def account(data: dict, who: str) -> str:
    """Everything kept about one agent, for a draft to read: their invoices,
    each with what has been paid on it, then their payments. By phone, email
    or the whole name - never part of one."""
    from .clearout import name_match

    who = " ".join(str(who or "").split())
    phone, email = _digits(who), who.casefold() if "@" in who else ""

    def theirs(name, one_email="", one_phone=""):
        if phone:
            return one_phone == phone
        if email:
            return one_email == email
        return name_match(who, name) >= 2

    invoices = [one for one in data.get("invoices", {}).values()
                if theirs(one.get("name", ""), one.get("email", ""), one.get("phone", ""))]
    ids = {one["id"] for one in invoices}
    numbers = {one["number"] for one in invoices if one.get("number")}
    payments = [
        one for one in data.get("payments", {}).values()
        if one.get("invoice_id") in ids or (one.get("invoice_number") and one["invoice_number"] in numbers)
        or (not phone and not email and theirs(one.get("customer", "")))
    ]
    if not invoices and not payments:
        return ""
    by_invoice: dict[str, list] = {}
    for one in payments:
        by_invoice.setdefault(one.get("invoice_id") or "", []).append(one)
    lines = []
    for one in sorted(invoices, key=lambda inv: inv.get("invoice_date") or "", reverse=True)[:8]:
        paid = [
            f"{_money(pay.get('amount'))} {pay.get('status') or '?'}"
            + (" (refund)" if pay.get("refund") else "") + f" {str(pay.get('paid_on'))[:10]}"
            for pay in by_invoice.get(one["id"], [])
        ]
        lines.append(
            f"- Invoice #{one.get('number') or '?'} {one.get('invoice_date') or '?'}: "
            f"{_money(one.get('total'))}"
            + (f" for {one['description']}" if one.get("description") else "")
            + (f", due {one['due_date']}" if one.get("due_date") else "")
            + (" - payments: " + "; ".join(paid) if paid else " - no payment on it")
            + ("" if one.get("active", True) else " (inactive)")
        )
    loose = [pay for pay in payments if pay.get("invoice_id") not in ids]
    for pay in sorted(loose, key=lambda one: one.get("paid_on") or "", reverse=True)[:5]:
        lines.append(
            f"- Payment {str(pay.get('paid_on'))[:10]} {_money(pay.get('amount'))} "
            f"{pay.get('status') or '?'}" + (" (refund)" if pay.get("refund") else "")
            + (f" by {pay['how']}" if pay.get("how") else "")
        )
    name = next((one.get("name") for one in invoices if one.get("name")), "")
    return (f"Payra account{' for ' + name if name else ''}:\n" + "\n".join(lines))


def _ago(stamp: str, now: datetime) -> str:
    try:
        then = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return "at an unknown time"
    minutes = int((now - then).total_seconds() // 60)
    if minutes < 1:
        return "just now"
    if minutes < 90:
        return f"{minutes} minute{'' if minutes == 1 else 's'} ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} hours ago"
    return f"{hours // 24} days ago"


def status(data: dict, *, now: datetime | None = None) -> str:
    """Whether the connection is working, in one or two lines for Discord."""
    now = now or datetime.now(timezone.utc)
    invoices, payments = len(data.get("invoices") or {}), len(data.get("payments") or {})
    synced, failed = str(data.get("synced_at") or ""), str(data.get("failed") or "")
    lines = []
    if synced:
        lines.append(
            f"✅ **Connected** — RYTE has {invoices:,} invoices and {payments:,} payments "
            f"from Payra, last read {_ago(synced, now)}. The Responder checks them when "
            "an agent texts."
        )
    elif not failed:
        lines.append("⏳ RYTE hasn't read Payra yet — the first read runs within a few "
                     "minutes of starting, if PAYRA_API_TOKEN and PAYRA_SITE_ID are in .env.")
    if failed and str(data.get("failed_at") or "") >= synced:
        lines.append(f"⚠ The last read failed ({_ago(str(data.get('failed_at')), now)}): {failed}")
    return "\n".join(lines)


# ------------------------------------------------ the record behind a dispute


def _cents(amount) -> int | None:
    try:
        return round(float(str(amount).replace("$", "").replace(",", "").strip()) * 100)
    except (TypeError, ValueError):
        return None


def _day(stamp) -> "date | None":
    try:
        return datetime.fromisoformat(str(stamp or "").replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _spelled(stamp) -> str:
    day = _day(stamp)
    return f"{day:%B} {day.day}, {day.year}" if day else str(stamp or "?")[:10]


def for_dispute(data: dict, *, name: str, email: str = "", amount: str = "",
                paid_on=None, card: str = "") -> tuple[str, bool]:
    """What Payra holds on a disputed charge. (what it says, certain?).

    The payment that was disputed - matched on the amount and the day, as the
    acquirer's notice gives them - and the invoice it paid: what it was for,
    when it was sent to the customer, and the customer on it. Their other
    payments with it, and any refund, which the rebuttal must not contradict.

    The customer by email or whole name only; the card's last four, when the
    notice has them, must agree. "" when Payra has nothing on them.
    """
    from .clearout import name_match

    email = str(email or "").strip().casefold()
    four = "".join(ch for ch in str(card or "") if ch.isdigit())[-4:]

    def theirs_invoice(one):
        return bool(email and one.get("email") == email) or name_match(name, one.get("name", "")) >= 2

    invoices = {one["id"]: one for one in data.get("invoices", {}).values() if theirs_invoice(one)}
    payments = [
        one for one in data.get("payments", {}).values()
        if one.get("invoice_id") in invoices or name_match(name, one.get("customer", "")) >= 2
    ]
    if four:
        payments = [one for one in payments if not one.get("last_4") or one["last_4"] == four]
    if not payments and not invoices:
        return "", False

    charges = sorted((one for one in payments if not one.get("refund")),
                     key=lambda one: str(one.get("paid_on") or ""), reverse=True)
    refunds = [one for one in payments if one.get("refund")]
    wanted = _cents(amount)

    def same_money(one):
        return wanted is not None and _cents(one.get("amount")) == wanted

    def same_day(one):
        day = _day(one.get("paid_on"))
        return bool(paid_on and day) and abs((day - paid_on).days) <= 2

    disputed = (next((one for one in charges if same_money(one) and same_day(one)), None)
                or next((one for one in charges if same_money(one)), None)
                or next((one for one in charges if same_day(one)), None))
    sure = disputed is not None
    disputed = disputed or (charges[0] if charges else None)

    lines = ["From Payra, read through its API:"]
    if disputed:
        lines.append(
            ("Payment disputed: " if sure else "Most recent payment (not matched to the dispute): ")
            + f"{_money(disputed.get('amount'))} on {_spelled(disputed.get('paid_on'))}"
            + f" — {disputed.get('status') or 'status unknown'}"
            + (f" — card ending {disputed['last_4']}" if disputed.get("last_4") else "")
            + (f" ({disputed['how']})" if disputed.get("how") else "")
            + (f" — transaction {disputed['transaction']}" if disputed.get("transaction") else "")
            + (f" — reference {disputed['reference']}" if disputed.get("reference") else "")
        )
    invoice = invoices.get(str((disputed or {}).get("invoice_id") or ""))
    if invoice is None and disputed and disputed.get("invoice_number"):
        invoice = next((one for one in invoices.values()
                        if one.get("number") == disputed["invoice_number"]), None)
    if invoice:
        lines.append(
            f"Invoice #{invoice.get('number') or '?'}"
            + (f", dated {_spelled(invoice['invoice_date'])}" if invoice.get("invoice_date") else "")
            + (f", due {_spelled(invoice['due_date'])}" if invoice.get("due_date") else "")
            + f": {_money(invoice.get('total'))}"
            + (f" — {invoice['description']}" if invoice.get("description") else "")
        )
        for one in invoice.get("lines") or []:
            lines.append(f"  For: {one}")
        who = invoice.get("name") or name
        lines.append(f"  Billed to: {who}" + (f" <{invoice['email']}>" if invoice.get("email") else ""))
        for one in invoice.get("sent") or []:
            lines.append(f"  Sent to the customer: {one}")
    others = [one for one in charges if one is not disputed][:6]
    if others:
        lines.append("Their other Payra payments: " + "; ".join(
            f"{_money(one.get('amount'))} {one.get('status') or ''} {_spelled(one.get('paid_on'))}".replace("  ", " ")
            for one in others))
    if refunds:
        lines.append("Refunds on record: " + "; ".join(
            f"{_money(one.get('amount'))} {_spelled(one.get('paid_on'))}" for one in refunds))
    return "\n".join(lines), sure
