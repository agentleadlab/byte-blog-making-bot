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
SHAPE = 5


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


def _small(one) -> dict | str:
    """A small record of unknown shape, its plain values only."""
    if not isinstance(one, dict):
        return " ".join(str(one or "").split())[:160]
    return {
        str(key): (value if isinstance(value, (int, float, bool)) else str(value)[:120])
        for key, value in list(one.items())[:16]
        if isinstance(value, (str, int, float, bool)) and str(value).strip()
        and not str(key).startswith("_") and key not in ("id", "external_id")
    }


def _id_of(one) -> str:
    if isinstance(one, str):
        return one
    if isinstance(one, dict):
        return str(one.get("_id") or one.get("id") or "")
    return ""


def _invoices_named(payment: dict) -> list[str]:
    """The invoices a payment record points at, wherever it says so."""
    found = [_id_of(payment.get("invoice") or "")]
    for one in payment.get("allocations") or []:
        if not isinstance(one, dict):
            continue
        for key, value in one.items():
            if "invoice" in str(key).casefold():
                found.append(_id_of(value))
    return [one for at, one in enumerate(found) if one and one not in found[:at]]


def payments_on(data: dict, invoice: dict) -> list[dict]:
    """Every payment on an invoice, however Payra linked the two: the
    payment's invoice or allocations naming it, the same invoice number, or
    the invoice listing the payment. When none of the payment records can be
    found, the payments as the invoice itself lists them."""
    number = invoice.get("number")
    listed = set(invoice.get("payment_ids") or [])
    found = [
        one for one in (data.get("payments") or {}).values()
        if invoice["id"] in (one.get("invoice_ids") or [])
        or one.get("invoice_id") == invoice["id"]
        or (number and one.get("invoice_number") == number)
        or one.get("id") in listed
    ]
    if found:
        return found
    return [_as_payment(one) for one in invoice.get("paid_here") or [] if _as_payment(one).get("amount") is not None]


def _as_payment(one: dict) -> dict:
    """A payment as the invoice lists it, in the shape of a payment record."""
    when = next((value for key, value in one.items()
                 if any(word in key.casefold() for word in ("paid", "date", "_at")) and _day(value)), "")
    status = str(_first(one, "status") or "")
    return {
        "amount": _first(one, "amount", "total", "paid_amount"),
        "paid_on": when, "status": status,
        "refund": bool(_first(one, "is_refund", "refund")),
        "how": str(_first(one, "display_name", "method", "payment_type") or ""),
        "last_4": str(_first(one, "last_4") or ""),
        "transaction": str(_first(one, "gateway_transaction_id") or ""),
        "reference": str(_first(one, "reference_number") or ""),
    }


def _first(one: dict, *names):
    for name in names:
        for key, value in one.items():
            if key.casefold() == name:
                return value
    return None


def line_text(one) -> str:
    """An invoice line as a person writes it: "50 OTP VETS × 1 @ $1,407.60"."""
    if not isinstance(one, dict):
        return str(one or "")
    what = _first(one, "name", "description", "item", "product", "title")
    many = _first(one, "quantity", "qty", "count")
    each = _first(one, "price", "rate", "unit_price", "unit_amount", "amount")
    if what is None:
        return _scalars(one)
    said = str(what)
    if many not in (None, ""):
        said += f" × {many:g}" if isinstance(many, (int, float)) else f" × {many}"
    if each not in (None, ""):
        said += f" @ {_money(each)}"
    return said


def sent_text(one) -> str:
    """A notification as a person writes it: "Email · September 2, 2026"."""
    if not isinstance(one, dict):
        return str(one or "")
    how = _first(one, "type", "method", "channel", "medium", "kind")
    when = next((value for key, value in one.items()
                 if any(word in key.casefold() for word in ("sent", "date", "created", "_at"))
                 and _day(value)), None)
    if how is None and when is None:
        return _scalars(one)
    return " · ".join(bit for bit in (str(how).title() if how else "", _spelled(when) if when else "") if bit)


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
        "lines": [_small(one) for one in (record.get("lines") or [])[:6]],
        # The payments Payra lists on the invoice itself - the way it marks
        # one paid - by id, and as they are, for when the payment's own
        # record doesn't point back.
        "payment_ids": [pid for pid in (_id_of(one) for one in record.get("payments") or []) if pid],
        "paid_here": [_small(one) for one in (record.get("payments") or [])[:10] if isinstance(one, dict)],
        "sent": [_small(one) for one in (record.get("notifications") or [])[:6]],
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
        # Every invoice the payment is put against: its own invoice field,
        # and its allocations - which is where a payment says which invoice
        # it paid when the invoice field is empty.
        "invoice_ids": _invoices_named(record),
        "last_4": str(record.get("last_4") or ""),
        # The card fee charged on top, when there is one: the bank's notice
        # carries the amount with it - David Pereira's $1,360 invoice was
        # disputed as $1,407.60, which is 3.5% more.
        "fee": record.get("fee"),
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
        for page in range(MOST_PAGES):
            asked = after
            got = client.changed(kind, after)
            records = got.get("records") or []
            if page == 0:
                # What Payra said about the ask - whether it read back as far
                # as it was asked to - for "payra status" to show.
                data.setdefault("reads", {})[kind] = {
                    "asked": asked, "applied": str(got.get("updated_after_applied") or ""),
                    "capped": bool(got.get("updated_after_capped")),
                    "matched": got.get("records_matched"), "returned": got.get("records_returned"),
                }
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
            more = got.get("records_matched")
            short = isinstance(limit, int) and len(records) < limit
            if isinstance(more, int) and more > len(records):
                short = False
            if not records or not onward or stuck or short:
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
    by_invoice = {one["id"]: payments_on(data, one) for one in invoices}
    on_theirs = {id(pay) for pays in by_invoice.values() for pay in pays}
    payments = [pay for pays in by_invoice.values() for pay in pays] + [
        one for one in data.get("payments", {}).values()
        if id(one) not in on_theirs and not phone and not email and theirs(one.get("customer", ""))
    ]
    if not invoices and not payments:
        return ""
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
    loose = [pay for pay in payments if id(pay) not in on_theirs]
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
    for kind, read in sorted((data.get("reads") or {}).items()):
        if read.get("capped"):
            lines.append(
                f"⚠ Payra only gave {kind} changed since {_spelled(read.get('applied'))} — "
                "anything older can't be read this way."
            )
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
    paid_on_invoice = {}
    for one in invoices.values():
        for pay in payments_on(data, one):
            paid_on_invoice[id(pay)] = (pay, one)
    payments = [pay for pay, _one in paid_on_invoice.values()] + [
        one for one in data.get("payments", {}).values()
        if id(one) not in paid_on_invoice and name_match(name, one.get("customer", "")) >= 2
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
        paid = _cents(one.get("amount"))
        return wanted is not None and paid is not None and wanted in (
            paid, paid + (_cents(one.get("fee")) or 0))

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
            + f"{_money(disputed.get('amount'))}"
            + (f" + {_money(disputed['fee'])} card fee" if _cents(disputed.get("fee")) else "")
            + f" on {_spelled(disputed.get('paid_on'))}"
            + f" — {disputed.get('status') or 'status unknown'}"
            + (f" — card ending {disputed['last_4']}" if disputed.get("last_4") else "")
            + (f" ({disputed['how']})" if disputed.get("how") else "")
            + (f" — transaction {disputed['transaction']}" if disputed.get("transaction") else "")
            + (f" — reference {disputed['reference']}" if disputed.get("reference") else "")
        )
    invoice = paid_on_invoice.get(id(disputed), (None, None))[1] if disputed else None
    invoice = invoice or invoices.get(str((disputed or {}).get("invoice_id") or ""))
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
            lines.append(f"  For: {line_text(one)}")
        who = invoice.get("name") or name
        lines.append(f"  Billed to: {who}" + (f" <{invoice['email']}>" if invoice.get("email") else ""))
        for one in invoice.get("sent") or []:
            lines.append(f"  Sent to the customer: {sent_text(one)}")
    others = [one for one in charges if one is not disputed][:6]
    if others:
        lines.append("Their other Payra payments: " + "; ".join(
            f"{_money(one.get('amount'))} {one.get('status') or ''} {_spelled(one.get('paid_on'))}".replace("  ", " ")
            for one in others))
    if refunds:
        lines.append("Refunds on record: " + "; ".join(
            f"{_money(one.get('amount'))} {_spelled(one.get('paid_on'))}" for one in refunds))
    return "\n".join(lines), sure


# ------------------------------------------------ a copy of a paid invoice

#: Payment statuses that mean the money never arrived.
_FAILED = ("fail", "declin", "void", "error", "cancel", "reject")


def _went_through(payment: dict) -> bool:
    status = str(payment.get("status") or "").casefold()
    return not payment.get("refund") and not any(word in status for word in _FAILED)


def paid_invoices(data: dict, who: str, *, most: int = 12) -> list[dict]:
    """Their invoices with money paid on them, newest first:
    [{"invoice", "payments", "refunds", "paid_cents"}].

    The same person `account` finds - by phone, email or the whole name, never
    part of one.
    """
    from .clearout import name_match

    who = " ".join(str(who or "").split())
    phone, email = _digits(who), who.casefold() if "@" in who else ""
    if not who:
        return []

    # An invoice number is the invoice, whoever's name is on it.
    number = who.casefold().strip(" .,!?;:").lstrip("#")
    by_number = [one for one in data.get("invoices", {}).values()
                 if str(one.get("number") or "").casefold() == number]

    def theirs(one):
        if by_number:
            return one in by_number
        if phone:
            return one.get("phone") == phone
        if email:
            return one.get("email") == email
        return name_match(who, one.get("name", "")) >= 2

    found = []
    for invoice in data.get("invoices", {}).values():
        if not theirs(invoice):
            continue
        on_it = payments_on(data, invoice)
        payments = sorted((one for one in on_it if _went_through(one)),
                          key=lambda one: str(one.get("paid_on") or ""))
        if not payments:
            continue
        found.append({
            "invoice": invoice, "payments": payments,
            "refunds": [one for one in on_it if one.get("refund")],
            "paid_cents": sum(_cents(one.get("amount")) or 0 for one in payments),
        })
    found.sort(key=lambda one: str(one["invoice"].get("invoice_date") or ""), reverse=True)
    return found[:most]


def _esc(text) -> str:
    import html

    return html.escape(str(text if text is not None else ""))


def _phone(ten: str) -> str:
    return f"({ten[:3]}) {ten[3:6]}-{ten[6:]}" if len(ten) == 10 else ten


def _payment_row(pay: dict) -> str:
    """One payment on an invoice, as a row of the table."""
    how = _esc(pay.get("how") or pay.get("kind") or "")
    if pay.get("last_4") and pay["last_4"] not in str(pay.get("how") or ""):
        how += f" · ending {_esc(pay['last_4'])}"
    if pay.get("refund"):
        how += " · <b>refund</b>"
    where = _esc(pay.get("transaction") or "")
    if pay.get("reference"):
        where += f"<br>ref {_esc(pay['reference'])}"
    return (
        "<tr>"
        f"<td>{_esc(_spelled(pay.get('paid_on')))}</td>"
        f"<td class=num>{_esc(_money(pay.get('amount')))}"
        + (f"<br><small>+ {_esc(_money(pay['fee']))} fee</small>" if _cents(pay.get("fee")) else "")
        + "</td>"
        f"<td>{how}</td>"
        f"<td>{_esc(pay.get('status') or '')}</td>"
        f"<td class=ref>{where}</td>"
        "</tr>"
    )


def invoice_html(found: list[dict], *, made_on: str) -> str:
    """The invoices as a printable page, one to a sheet of paper.

    Plainly a record read from Payra rather than Payra's own invoice: every
    figure on it is Payra's, and it says where it came from and when.
    """
    pages = []
    for one in found:
        invoice = one["invoice"]
        total = _cents(invoice.get("total")) or 0
        left = total - one["paid_cents"] + sum(_cents(r.get("amount")) or 0 for r in one["refunds"])
        standing = ("Paid in full" if left <= 0 else f"Partly paid — {_money(left / 100)} still owed")
        if one["refunds"]:
            standing += " · refunded " + ", ".join(_money(r.get("amount")) for r in one["refunds"])
        rows = "".join(_payment_row(pay) for pay in one["payments"] + one["refunds"])
        lines = "".join(f"<li>{_esc(line_text(line))}</li>" for line in invoice.get("lines") or [])
        sent = "".join(f"<li>{_esc(sent_text(line))}</li>" for line in invoice.get("sent") or [])
        billed = [_esc(invoice.get("name") or "")]
        if invoice.get("email"):
            billed.append(_esc(invoice["email"]))
        if invoice.get("phone"):
            billed.append(_esc(_phone(invoice["phone"])))
        pages.append(f"""
<section class=page>
  <header>
    <div><div class=label>Invoice record from Payra</div>
      <h1>Invoice #{_esc(invoice.get('number') or '?')}</h1></div>
    <div class="stamp{' part' if left > 0 else ''}">{_esc(standing)}</div>
  </header>
  <div class=grid>
    <div><div class=label>Billed to</div><div>{'<br>'.join(billed)}</div></div>
    <div><div class=label>Invoice date</div><div>{_esc(_spelled(invoice.get('invoice_date')))}</div>
      <div class=label>Due</div><div>{_esc(_spelled(invoice.get('due_date')) if invoice.get('due_date') else '—')}</div></div>
    <div><div class=label>Total</div><div class=big>{_esc(_money(invoice.get('total')))}</div></div>
  </div>
  <div class=label>For</div>
  <p>{_esc(invoice.get('description') or '')}</p>
  {f'<ul>{lines}</ul>' if lines else ''}
  <div class=label>Payments on this invoice</div>
  <table><thead><tr><th>Date</th><th class=num>Amount</th><th>Method</th><th>Status</th><th>Transaction</th></tr></thead>
  <tbody>{rows}</tbody></table>
  {f'<div class=label>Sent to the customer</div><ul>{sent}</ul>' if sent else ''}
  <footer>Read from Payra's records on {_esc(made_on)} · Payra invoice ID {_esc(invoice.get('id') or '')}</footer>
</section>""")
    return """<!doctype html><html><head><meta charset=utf-8><style>
@page { size: Letter; margin: 0.6in; }
body { font: 11pt/1.45 -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; color: #1b1b1f; margin: 0; }
.page { page-break-after: always; }
.page:last-child { page-break-after: auto; }
header { display: flex; justify-content: space-between; align-items: flex-start;
  border-bottom: 2px solid #1b1b1f; padding-bottom: 10px; margin-bottom: 18px; }
h1 { font-size: 22pt; margin: 2px 0 0; }
.label { font-size: 8.5pt; text-transform: uppercase; letter-spacing: .06em; color: #666; margin-top: 12px; }
.stamp { border: 2px solid #1d7a3a; color: #1d7a3a; font-weight: 700; padding: 6px 10px; border-radius: 4px; }
.stamp.part { border-color: #a15c00; color: #a15c00; }
.grid { display: grid; grid-template-columns: 1.4fr 1fr 1fr; gap: 16px; }
.big { font-size: 16pt; font-weight: 700; }
table { width: 100%; border-collapse: collapse; margin-top: 6px; font-size: 10pt; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #ddd; vertical-align: top; }
th { font-size: 8.5pt; text-transform: uppercase; letter-spacing: .05em; color: #666; }
.num { text-align: right; } .ref { font-family: Menlo, Consolas, monospace; font-size: 8.5pt; word-break: break-all; }
ul { margin: 4px 0 0; padding-left: 18px; }
footer { margin-top: 28px; font-size: 8.5pt; color: #777; border-top: 1px solid #ddd; padding-top: 8px; }
</style></head><body>""" + "".join(pages) + "</body></html>"


def theirs_listed(data: dict, who: str, *, most: int = 6) -> list[dict]:
    """Every invoice of theirs, paid or not, newest first - for saying what
    was found when none of it is paid."""
    from .clearout import name_match

    who = " ".join(str(who or "").split())
    phone, email = _digits(who), who.casefold() if "@" in who else ""
    if not who:
        return []
    found = [
        one for one in data.get("invoices", {}).values()
        if (one.get("phone") == phone if phone else one.get("email") == email if email
            else name_match(who, one.get("name", "")) >= 2)
    ]
    return sorted(found, key=lambda one: str(one.get("invoice_date") or ""), reverse=True)[:most]


def near(data: dict, who: str, *, most: int = 6) -> list[str]:
    """Names RYTE holds that share a whole word with the one asked for - never
    used to find anybody, only to say who else there is, so the right one can
    be asked for by email or invoice number."""
    from .clearout import words_of

    wanted = {one for one in words_of(who) if len(one) >= 3}
    if not wanted:
        return []
    said = []
    for one in sorted(data.get("invoices", {}).values(),
                      key=lambda inv: str(inv.get("invoice_date") or ""), reverse=True):
        mine = set(words_of(one.get("name", ""))) | set(words_of(str(one.get("email") or "").split("@")[0]))
        if not wanted & mine:
            continue
        line = (f"{one.get('name') or '?'}" + (f" <{one['email']}>" if one.get("email") else "")
                + f" — #{one.get('number') or '?'} {_money(one.get('total'))}, dated {_spelled(one.get('invoice_date'))}")
        if line not in said:
            said.append(line)
        if len(said) >= most:
            break
    return said
