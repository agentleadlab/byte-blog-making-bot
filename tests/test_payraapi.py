"""The copy of Payra's invoices and payments: read in pages, kept in step,
looked up by the whole person."""

from datetime import date, datetime, timezone

import pytest

from wilbyte import payraapi

NOW = datetime(2026, 9, 25, 12, 0, 0, 316000, tzinfo=timezone.utc)


def _invoice(n, name="Adrian Pacheco", phone="+1 (312) 555-0188", email="A@X.com", total=700,
             updated="2026-09-20T00:00:00.000Z"):
    first, _, last = name.partition(" ")
    return {
        "_id": f"inv{n}", "invoice_number": str(1000 + n), "totals": {"total": total, "tax": 0},
        "description": "OTP VETS", "invoice_date": "2026-09-19T00:00:00.000Z",
        "due_date": "2026-09-26T00:00:00.000Z", "active": True, "updated_at": updated,
        "customer": {"first_name": first, "last_name": last, "mobile_phone": phone, "email": email},
    }


def _payment(n, invoice, amount=700, status="Settled", refund=False):
    return {
        "_id": f"pay{n}", "amount": amount, "status": status, "is_refund": refund,
        "paid_on": "2026-09-20T01:00:00.000Z", "display_name": "Visa 4242",
        "customer_display": "Adrian Pacheco", "invoice_number": "", "invoice": {"_id": invoice},
    }


class Pages:
    """Payra, as far as sync can tell: pages handed out in order."""

    def __init__(self, **pages):
        self.pages = {kind: list(got) for kind, got in pages.items()}
        self.asked = []

    def changed(self, kind, after):
        self.asked.append((kind, after))
        got = self.pages.get(kind) or []
        if not got:
            return {"records": [], "limit": 2, "next_updated_after": after}
        one = got.pop(0)
        if isinstance(one, Exception):
            raise one
        return one


def test_times_are_written_the_way_payra_takes_them():
    assert payraapi.when(NOW) == "2026-09-25T12:00:00.316Z"


def test_the_first_read_goes_back_a_year_then_carries_on_from_where_it_got_to():
    data = payraapi.load()
    payra = Pages(invoices=[
        {"records": [_invoice(1), _invoice(2)], "limit": 2, "next_updated_after": "T2"},
        {"records": [_invoice(3)], "limit": 2, "next_updated_after": "T3"},
    ])

    counts = payraapi.sync(data, payra, now=NOW)

    assert payra.asked[0] == ("invoices", "2025-09-25T12:00:00.316Z")
    assert payra.asked[1] == ("invoices", "T2")
    assert ("invoices", "T3") not in payra.asked, "a short page is the last one"
    assert counts == {"invoices": 3, "payments": 0}
    assert set(data["invoices"]) == {"inv1", "inv2", "inv3"}
    assert data["cursors"]["invoices"] == "T3"

    payra.asked.clear()
    payraapi.sync(data, payra, now=NOW)
    assert payra.asked[0] == ("invoices", "T3")


def test_a_changed_record_replaces_the_old_one():
    data = payraapi.load()
    payraapi.sync(data, Pages(invoices=[{"records": [_invoice(1, total=700)], "limit": 5,
                                         "next_updated_after": "T1"}]), now=NOW)
    payraapi.sync(data, Pages(invoices=[{"records": [_invoice(1, total=650)], "limit": 5,
                                         "next_updated_after": "T2"}]), now=NOW)
    assert len(data["invoices"]) == 1
    assert data["invoices"]["inv1"]["total"] == 650


def test_a_record_without_an_id_is_not_kept():
    data = payraapi.load()
    nameless = _invoice(1)
    del nameless["_id"]
    payraapi.sync(data, Pages(invoices=[{"records": [nameless], "limit": 5,
                                         "next_updated_after": "T1"}]), now=NOW)
    assert data["invoices"] == {}


def test_a_page_that_goes_nowhere_does_not_loop():
    data = payraapi.load()
    stuck = [{"records": [_invoice(n)], "limit": 1, "next_updated_after": "SAME"} for n in range(5)]
    payra = Pages(invoices=stuck)
    data["cursors"]["invoices"] = "SAME"
    data["shape"] = payraapi.SHAPE

    payraapi.sync(data, payra, now=NOW)

    assert [one for one in payra.asked if one[0] == "invoices"] == [("invoices", "SAME")]


def test_a_failure_keeps_what_was_read_and_where_it_got_to():
    data = payraapi.load()
    payra = Pages(invoices=[
        {"records": [_invoice(1)], "limit": 1, "next_updated_after": "T1"},
        payraapi.PayraError("Payra said HTTP 500"),
    ])

    with pytest.raises(payraapi.PayraError):
        payraapi.sync(data, payra, now=NOW)

    assert "inv1" in data["invoices"]
    assert data["cursors"]["invoices"] == "T1"


def test_kept_is_only_what_a_reply_needs():
    one = payraapi.slim_invoice(_invoice(1))
    assert one == {
        "id": "inv1", "number": "1001", "total": 700, "description": "OTP VETS",
        "invoice_date": "2026-09-19", "due_date": "2026-09-26", "active": True,
        "name": "Adrian Pacheco", "email": "a@x.com", "phone": "3125550188",
        "lines": [], "payment_ids": [], "paid_here": [], "sent": [], "created": "",
        "updated": "2026-09-20T00:00:00.000Z",
    }
    paid = payraapi.slim_payment(_payment(1, "inv1", refund=True))
    assert paid["refund"] is True and paid["invoice_id"] == "inv1" and paid["how"] == "Visa 4242"


def _kept():
    data = payraapi.load()
    for record in (_invoice(1), _invoice(2, total=300),
                   _invoice(3, name="Adrian Smith", phone="6025550199", email="s@x.com")):
        one = payraapi.slim_invoice(record)
        data["invoices"][one["id"]] = one
    for record in (_payment(1, "inv1"), _payment(2, "inv3", amount=50)):
        one = payraapi.slim_payment(record)
        data["payments"][one["id"]] = one
    return data


def test_an_account_shows_each_invoice_and_what_was_paid_on_it():
    got = payraapi.account(_kept(), "(312) 555-0188")

    assert got.startswith("Payra account for Adrian Pacheco:")
    assert "- Invoice #1001 2026-09-19: $700.00 for OTP VETS, due 2026-09-26 - payments: $700.00 Settled 2026-09-20" in got
    assert "- Invoice #1002 2026-09-19: $300.00 for OTP VETS, due 2026-09-26 - no payment on it" in got
    assert "1003" not in got and "$50.00" not in got, "someone else's"


def test_an_account_is_found_by_the_whole_person_only():
    data = _kept()
    assert "1001" in payraapi.account(data, "a@x.com")
    assert "1001" in payraapi.account(data, "Adrian Pacheco")
    assert payraapi.account(data, "Adrian") == "", "a first name is not a person"
    assert payraapi.account(data, "Adrian Pacheco Jr") == "", "his father's invoices aren't his"
    assert payraapi.account(data, "3125550100") == ""
    assert payraapi.account(data, "nobody@x.com") == ""
    assert payraapi.account(data, "") == ""
    assert "1003" not in payraapi.account(data, "Adrian Pacheco")


def test_a_refund_says_so():
    data = _kept()
    one = payraapi.slim_payment(_payment(9, "inv2", amount=300, refund=True))
    data["payments"][one["id"]] = one
    assert "$300.00 Settled (refund) 2026-09-20" in payraapi.account(data, "a@x.com")


def test_the_copy_survives_a_restart(tmp_path):
    where = tmp_path / "payra.json"
    data = _kept()
    data["cursors"]["invoices"] = "T9"
    payraapi.save(data, where)
    back = payraapi.load(where)
    assert back["cursors"]["invoices"] == "T9" and set(back["invoices"]) == {"inv1", "inv2", "inv3"}
    where.write_text("not json")
    assert payraapi.load(where) == {"cursors": {}, "invoices": {}, "payments": {}}


def test_the_client_only_reads_and_sends_the_token_only_to_payra(monkeypatch):
    import httpx

    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"records": [], "limit": 100})

    real = httpx.Client

    def fake(**kwargs):
        return real(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "Client", fake)
    with payraapi.PayraClient("tok-fake", "site1") as client:
        client.changed("invoices", "2026-09-25T12:00:00.316Z")

    assert [one.method for one in seen] == ["GET"]
    assert str(seen[0].url) == (
        "https://api.payra.com/api/v3.1/site/site1/invoices?updated_after=2026-09-25T12:00:00.316Z")
    assert seen[0].headers["x-access-token"] == "tok-fake"
    assert not [name for name in vars(payraapi.PayraClient) if name in ("post", "put", "patch", "delete")]


def test_a_refused_token_says_what_to_check(monkeypatch):
    import httpx

    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real(
        transport=httpx.MockTransport(lambda r: httpx.Response(401, json={})), **kw))
    with payraapi.PayraClient("tok-fake", "site1") as client, pytest.raises(payraapi.PayraError, match="PAYRA_API_TOKEN"):
        client.changed("invoices", "x")
    with pytest.raises(payraapi.PayraError):
        payraapi.PayraClient("", "site1")


# ------------------------------------------------ "are we good here?"


def test_status_says_it_is_connected_and_how_fresh():
    data = _kept()
    data["synced_at"] = "2026-09-25T11:55:00.316Z"
    said = payraapi.status(data, now=NOW)
    assert said.startswith("✅ **Connected** — RYTE has 3 invoices and 2 payments")
    assert "last read 5 minutes ago" in said
    assert "⚠" not in said


def test_status_says_when_the_last_read_failed():
    data = _kept()
    data.update(synced_at="2026-09-25T09:00:00.000Z", failed="Payra said HTTP 500: ",
                failed_at="2026-09-25T11:59:00.000Z")
    said = payraapi.status(data, now=NOW)
    assert "last read 3 hours ago" in said
    assert "⚠ The last read failed (1 minute ago): Payra said HTTP 500" in said

    data["synced_at"] = "2026-09-25T12:00:00.000Z"
    assert "⚠" not in payraapi.status(data, now=NOW), "an old failure since put right"


def test_status_before_the_first_read():
    assert payraapi.status(payraapi.load(), now=NOW).startswith("⏳ RYTE hasn't read Payra yet")


def test_a_read_remembers_whether_it_worked(monkeypatch):
    from types import SimpleNamespace as NS

    from wilbyte.bot import jobs

    class Fine:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def changed(self, kind, after):
            return {"records": [], "limit": 5, "next_updated_after": after}

    class Broken(Fine):
        def changed(self, kind, after):
            raise payraapi.PayraError("Payra didn't accept the token - check PAYRA_API_TOKEN in .env.")

    config = NS(secrets=NS(payra_api_token="tok-fake", payra_site_id="site"))
    monkeypatch.setattr(payraapi, "PayraClient", Broken)
    with pytest.raises(payraapi.PayraError):
        jobs.payra_sync(config)
    kept = payraapi.load()
    assert "PAYRA_API_TOKEN" in kept["failed"] and kept["failed_at"] and "synced_at" not in kept

    monkeypatch.setattr(payraapi, "PayraClient", Fine)
    jobs.payra_sync(config)
    kept = payraapi.load()
    assert kept["synced_at"] and "failed" not in kept


def test_payra_status_is_asked_for_plainly():
    from wilbyte.bot import mentions

    assert mentions.parse("<@1> payra status").action == "payrastatus"
    assert mentions.parse("<@1> payra?").action == "payrastatus"
    assert mentions.parse("<@1> payra test").action == "payratest"


def test_keeping_more_reads_everything_again_once():
    data = payraapi.load()
    data["cursors"] = {"invoices": "T9", "payments": "T9"}
    payra = Pages()

    payraapi.sync(data, payra, now=NOW)
    assert payra.asked[0] == ("invoices", "2025-09-25T12:00:00.316Z"), "from a year back"
    assert data["shape"] == payraapi.SHAPE

    payra.asked.clear()
    data["cursors"]["invoices"] = "T5"
    payraapi.sync(data, payra, now=NOW)
    assert payra.asked[0] == ("invoices", "T5"), "and only once"


# ------------------------------------------------ the record behind a dispute


def _disputed_board():
    data = payraapi.load()
    invoice = _invoice(7, name="Jose Zambrano", phone="6025550199", email="jz@example.com", total=1407.60)
    invoice["invoice_date"] = "2026-09-02T00:00:00.000Z"
    invoice["lines"] = [{"name": "50 OTP VETS", "quantity": 1, "price": 1407.60}]
    invoice["notifications"] = [{"type": "email", "sent_at": "2026-09-02T15:00:00.000Z", "_id": "n1"}]
    earlier = _invoice(6, name="Jose Zambrano", phone="6025550199", email="jz@example.com", total=700)
    for record in (invoice, earlier, _invoice(1)):
        one = payraapi.slim_invoice(record)
        data["invoices"][one["id"]] = one
    paid = _payment(7, "inv7", amount=1407.60)
    paid.update(paid_on="2026-09-03T14:00:00.000Z", last_4="4242", gateway_transaction_id="txn-9",
                reference_number="R-77", customer_display="Jose Zambrano")
    before = _payment(6, "inv6", amount=700)
    before.update(paid_on="2026-08-01T14:00:00.000Z", last_4="4242", customer_display="Jose Zambrano")
    for record in (paid, before, _payment(1, "inv1")):
        one = payraapi.slim_payment(record)
        data["payments"][one["id"]] = one
    return data


def test_a_dispute_gets_the_disputed_payment_and_the_invoice_it_paid():
    said, sure = payraapi.for_dispute(
        _disputed_board(), name="Jose Zambrano", amount="$1,407.60",
        paid_on=date(2026, 9, 3), card="XXXX XXXX XXXX 4242",
    )

    assert sure
    assert ("Payment disputed: $1,407.60 on September 3, 2026 — Settled — card ending 4242 "
            "(Visa 4242) — transaction txn-9 — reference R-77") in said
    assert "Invoice #1007, dated September 2, 2026, due September 26, 2026: $1,407.60 — OTP VETS" in said
    assert "  For: 50 OTP VETS × 1 @ $1,407.60" in said
    assert "  Billed to: Jose Zambrano <jz@example.com>" in said
    assert "  Sent to the customer: Email · September 2, 2026" in said
    assert "Their other Payra payments: $700.00 Settled August 1, 2026" in said
    assert "Adrian" not in said and "Refunds" not in said


def test_a_refund_is_never_left_out_of_a_dispute():
    data = _disputed_board()
    back = payraapi.slim_payment({**_payment(9, "inv7", amount=1407.60, refund=True),
                                  "paid_on": "2026-09-10T00:00:00.000Z"})
    data["payments"][back["id"]] = back

    said, sure = payraapi.for_dispute(data, name="Jose Zambrano", amount="1407.60",
                                      paid_on=date(2026, 9, 3))
    assert sure and "Refunds on record: $1,407.60 September 10, 2026" in said
    assert said.index("Payment disputed") < said.index("Refunds"), "the refund is not the disputed charge"


def test_the_disputed_charge_is_matched_on_amount_and_day():
    data = _disputed_board()
    said, sure = payraapi.for_dispute(data, name="Jose Zambrano", amount="$700.00",
                                      paid_on=date(2026, 8, 1))
    assert sure and "Payment disputed: $700.00 on August 1, 2026" in said

    said, sure = payraapi.for_dispute(data, name="Jose Zambrano", amount="$99.00",
                                      paid_on=date(2026, 6, 1))
    assert not sure
    assert "Most recent payment (not matched to the dispute): $1,407.60" in said


def test_a_dispute_is_only_ever_the_whole_person_and_their_card():
    data = _disputed_board()
    assert payraapi.for_dispute(data, name="Jose", amount="1407.60") == ("", False)
    assert payraapi.for_dispute(data, name="Jose Zambrano Jr", amount="1407.60") == ("", False)
    said, _ = payraapi.for_dispute(data, name="J Z", email="JZ@example.com", amount="1407.60")
    assert "Payment disputed: $1,407.60" in said, "by the email on the notice"
    said, _ = payraapi.for_dispute(data, name="Jose Zambrano", amount="1407.60", card="xxxx1111")
    assert "Payment disputed" not in said and "card ending 4242" not in said, "a different card"


def test_the_rebuttal_says_when_payra_has_nothing_or_is_not_sure():
    from wilbyte import rebuttal
    from wilbyte.bot import jobs

    dispute = rebuttal.Dispute(customer_name="Jose Zambrano", amount="$1,407.60",
                               transaction_date="09/03/2026")
    assert jobs._payra_record(dispute) == ("", ""), "nothing kept: not connected, said nowhere"

    payraapi.save(_disputed_board())
    said, trouble = jobs._payra_record(dispute)
    assert said.startswith("From Payra, read through its API:") and trouble == ""

    nobody = rebuttal.Dispute(customer_name="Nobody Known", amount="$1", transaction_date="09/03/2026")
    assert "Nothing in Payra for “Nobody Known”" in jobs._payra_record(nobody)[1]


def test_a_refund_is_never_taken_for_the_disputed_charge():
    data = _disputed_board()
    back = payraapi.slim_payment({**_payment(9, "inv7", amount=1407.60, refund=True),
                                  "paid_on": "2026-09-10T00:00:00.000Z"})
    data["payments"][back["id"]] = back

    said, sure = payraapi.for_dispute(data, name="Jose Zambrano", amount="1407.60")
    assert sure and "Payment disputed: $1,407.60 on September 3, 2026" in said


# ------------------------------------------------ "send me a copy of his paid invoice"


@pytest.mark.parametrize("said, who", [
    ("<@1> invoice of David Pereira", "David Pereira"),
    ("<@1> paid invoice for David Pereira", "David Pereira"),
    ("<@1> send me copy of David Pereira's paid invoice", "David Pereira"),
    ("<@1> send me a copy of the paid invoice for Max Wilson", "Max Wilson"),
    ("<@1> Max Wilson invoice", "Max Wilson"),
    ("<@1> invoices for jz@example.com", "jz@example.com"),
])
def test_asking_for_a_paid_invoice(said, who):
    from wilbyte.bot import mentions

    got = mentions.parse(said)
    assert (got.action, got.brief) == ("invoice", who)


def test_other_things_that_mention_invoices_are_not_this():
    from wilbyte.bot import mentions

    assert mentions.parse("<@1> what does faith send when agents ask for an invoice?").action != "invoice"
    assert mentions.parse("<@1> contract of David Pereira").action == "contract"


def test_only_invoices_with_money_on_them_and_only_theirs():
    data = _disputed_board()
    unpaid = payraapi.slim_invoice(_invoice(8, name="Jose Zambrano", email="jz@example.com"))
    data["invoices"][unpaid["id"]] = unpaid
    bounced = payraapi.slim_invoice(_invoice(9, name="Jose Zambrano", email="jz@example.com"))
    data["invoices"][bounced["id"]] = bounced
    failed = payraapi.slim_payment(_payment(19, "inv9", status="Declined"))
    data["payments"][failed["id"]] = failed

    found = payraapi.paid_invoices(data, "Jose Zambrano")

    assert [one["invoice"]["number"] for one in found] == ["1006", "1007"], "newest first, paid only"
    assert found[1]["paid_cents"] == 140760
    assert payraapi.paid_invoices(data, "Jose") == []
    assert payraapi.paid_invoices(data, "Jose Zambrano Jr") == [], "his father's invoices aren't his"
    assert payraapi.paid_invoices(data, "jz@example.com") == found
    assert payraapi.paid_invoices(data, "(602) 555-0199") == found
    assert payraapi.paid_invoices(data, "") == []


def test_the_copy_says_what_it_is_and_whether_it_is_paid():
    data = _disputed_board()
    data["invoices"]["inv6"]["total"] = 900
    found = payraapi.paid_invoices(data, "Jose Zambrano")

    page = payraapi.invoice_html(found, made_on="September 28, 2026")

    assert page.count("<section class=page>") == 2
    assert "Invoice record from Payra" in page
    assert "Partly paid — $200.00 still owed" in page and "Paid in full" in page
    assert "50 OTP VETS × 1 @ $1,407.60" in page and "Email · September 2, 2026" in page
    assert "txn-9" in page and "ref R-77" in page
    assert "Visa 4242</td>" in page, "the last four said once"
    assert "Read from Payra's records on September 28, 2026" in page


def test_nothing_in_the_copy_is_taken_as_markup():
    data = _disputed_board()
    data["invoices"]["inv7"]["description"] = "<script>alert(1)</script>"
    page = payraapi.invoice_html(payraapi.paid_invoices(data, "Jose Zambrano"), made_on="x")
    assert "<script>" not in page and "&lt;script&gt;" in page


def test_the_pdf_is_made_or_it_says_why_not(monkeypatch):
    from wilbyte.bot import jobs

    assert "don't have anything from Payra yet" in jobs.paid_invoice_pdf("Jose Zambrano")[2]
    payraapi.save(_disputed_board())
    monkeypatch.setattr(jobs, "_print_pdf", lambda html: b"%PDF-fake " + html[:40].encode())

    pdf, found, problem = jobs.paid_invoice_pdf("Jose Zambrano")
    assert pdf.startswith(b"%PDF") and len(found) == 2 and problem == ""
    assert "No Payra invoice for “Nobody Known” among the 3 I hold" in jobs.paid_invoice_pdf("Nobody Known")[2]


def test_the_invoice_is_sent_as_a_file(monkeypatch):
    import asyncio

    from wilbyte.bot import client, jobs

    payraapi.save(_disputed_board())
    monkeypatch.setattr(jobs, "_print_pdf", lambda html: b"%PDF-fake")
    sent = []

    class Heard:
        async def send(self, content=None, **kw):
            sent.append((content, kw.get("file")))

    asyncio.run(client._send_paid_invoice(Heard(), "Jose Zambrano"))

    words, file = sent[0]
    assert words.startswith("🧾 **Jose Zambrano** — 2 paid invoices from Payra:")
    assert "• #1007 — $1,407.60, paid September 3, 2026" in words
    assert file.filename == "Jose Zambrano - paid invoices.pdf"

    asyncio.run(client._send_paid_invoice(Heard(), ""))
    assert sent[-1][0].startswith("Whose?")


def test_a_brief_that_mentions_an_invoice_is_still_a_brief():
    from wilbyte.bot import mentions

    assert mentions.parse("<@1> write a paid ad about how easy our invoices are for agents").action == "write"
    assert mentions.parse("<@1> David Pereira paid invoice").action == "invoice"


# ------------------------------------------------ however Payra links a payment to its invoice
#
# David Pereira's INV-18089, $1,360, paid 09/04 in Payra, and RYTE said he had
# no paid invoice: only the payment's own invoice field was read.


def _pereira(**payment_fields):
    data = payraapi.load()
    invoice = _invoice(89, name="David Pereira", phone="4045550111", email="dp@example.com", total=1360)
    invoice.update(invoice_number="INV-18089", _id="invDP", payments=payment_fields.pop("listed", []))
    one = payraapi.slim_invoice(invoice)
    data["invoices"][one["id"]] = one
    if payment_fields.pop("record", True):
        paid = {**_payment(89, "", amount=1360), "paid_on": "2026-09-04T16:00:00.000Z",
                "customer_display": "", "invoice": None, **payment_fields}
        pay = payraapi.slim_payment(paid)
        data["payments"][pay["id"]] = pay
    return data


@pytest.mark.parametrize("how", [
    {"invoice": {"_id": "invDP"}},
    {"allocations": [{"invoice": {"_id": "invDP"}, "amount": 1360}]},
    {"allocations": [{"invoice_id": "invDP", "amount": 1360}]},
    {"invoice_number": "INV-18089"},
    {"listed": ["pay89"]},
    {"listed": [{"_id": "pay89", "amount": 1360}]},
])
def test_a_payment_is_found_on_its_invoice_however_payra_links_them(how):
    found = payraapi.paid_invoices(_pereira(**how), "David Pereira")

    assert [one["invoice"]["number"] for one in found] == ["INV-18089"]
    assert found[0]["paid_cents"] == 136000


def test_the_invoices_own_list_of_payments_is_enough_on_its_own():
    data = _pereira(record=False, listed=[{"_id": "gone", "amount": 1360, "status": "Settled",
                                           "paid_on": "2026-09-04T16:00:00.000Z", "last_4": "1111"}])

    found = payraapi.paid_invoices(data, "David Pereira")

    assert found and found[0]["paid_cents"] == 136000
    assert found[0]["payments"][0]["last_4"] == "1111"
    page = payraapi.invoice_html(found, made_on="x")
    assert "Paid in full" in page and "September 4, 2026" in page


def test_the_responder_and_the_rebuttal_see_the_same_payment():
    data = _pereira(allocations=[{"invoice_id": "invDP", "amount": 1360}])

    assert "Invoice #INV-18089 2026-09-19: $1,360.00 for OTP VETS, due 2026-09-26 - payments: $1,360.00 Settled 2026-09-04" in (
        payraapi.account(data, "David Pereira"))
    said, sure = payraapi.for_dispute(data, name="David Pereira", amount="$1,360.00", paid_on=date(2026, 9, 4))
    assert sure and "Payment disputed: $1,360.00 on September 4, 2026" in said
    assert "Invoice #INV-18089" in said


def test_when_none_is_paid_it_says_what_it_did_find(monkeypatch):
    from wilbyte.bot import jobs

    payraapi.save(_pereira(record=False))
    said = jobs.paid_invoice_pdf("David Pereira")[2]
    assert said.startswith("I have 1 Payra invoice for “David Pereira” but can't see a payment on it:")
    assert "• #INV-18089 — $1,360.00, dated September 19, 2026 — no payment I can see on it" in said


def test_the_disputed_amount_can_include_the_card_fee():
    """David Pereira's $1,360 invoice was disputed as $1,407.60 - 3.5% more."""
    data = _pereira(invoice={"_id": "invDP"}, fee=47.60)

    said, sure = payraapi.for_dispute(data, name="David Pereira", amount="$1,407.60")

    assert sure
    assert "Payment disputed: $1,360.00 + $47.60 card fee on September 4, 2026" in said
    page = payraapi.invoice_html(payraapi.paid_invoices(data, "David Pereira"), made_on="x")
    assert "+ $47.60 fee" in page
    assert payraapi.for_dispute(data, name="David Pereira", amount="$1,400.00")[1] is False


# ------------------------------------------------ when a name finds nothing


def test_an_invoice_number_finds_the_invoice_whatever_the_name():
    data = _pereira(invoice={"_id": "invDP"})
    data["invoices"]["invDP"]["name"] = "Dave A. Pereira"

    assert payraapi.paid_invoices(data, "David Pereira") == []
    for asked in ("INV-18089", "inv-18089", "#INV-18089"):
        assert [one["invoice"]["number"] for one in payraapi.paid_invoices(data, asked)] == ["INV-18089"]


def test_asking_by_invoice_number():
    from wilbyte.bot import mentions

    for said in ("<@1> invoice INV-18089", "<@1> invoice #INV-18089", "<@1> paid invoice for INV-18089"):
        got = mentions.parse(said)
        assert (got.action, got.brief.lstrip("#")) == ("invoice", "INV-18089"), said


def test_nothing_found_by_name_says_who_is_close(monkeypatch):
    from wilbyte.bot import jobs

    data = _pereira(invoice={"_id": "invDP"})
    data["invoices"]["invDP"]["name"] = "Dave A. Pereira"
    payraapi.save(data)

    said = jobs.paid_invoice_pdf("David Pereira")[2]

    assert said.startswith("No Payra invoice for “David Pereira” among the 1 I hold")
    assert "• Dave A. Pereira <dp@example.com> — #INV-18089 $1,360.00, dated September 19, 2026" in said
    assert "`@RYTE invoice INV-18089`" in said
    assert payraapi.near(data, "A Smith") == [], "a middle initial is nobody's name"


def test_status_says_when_payra_would_not_read_back_as_far_as_asked():
    data = _kept()
    data["synced_at"] = "2026-09-25T11:55:00.316Z"
    data["reads"] = {"invoices": {"asked": "2025-09-25T12:00:00.316Z",
                                  "applied": "2026-08-26T12:00:00.000Z", "capped": True}}
    assert "⚠ Payra only gave invoices changed since August 26, 2026" in payraapi.status(data, now=NOW)


def test_it_keeps_reading_while_payra_says_more_matched():
    data = payraapi.load()
    payra = Pages(invoices=[
        {"records": [_invoice(1)], "limit": 5, "records_matched": 2, "records_returned": 1,
         "next_updated_after": "T1", "updated_after_applied": "X", "updated_after_capped": False},
        {"records": [_invoice(2)], "limit": 5, "records_matched": 1, "next_updated_after": "T2"},
    ])
    payraapi.sync(data, payra, now=NOW)
    assert set(data["invoices"]) == {"inv1", "inv2"}
    assert data["reads"]["invoices"]["matched"] == 2 and data["reads"]["invoices"]["capped"] is False


def test_invoice_then_the_name_without_of():
    from wilbyte.bot import mentions

    assert mentions.parse("<@1> invoice David Pereira").brief == "David Pereira"


def test_how_far_back_payra_goes_is_read_not_guessed(monkeypatch):
    from types import SimpleNamespace as NS

    from wilbyte.bot import jobs

    class Newest:
        def __init__(self, *a, **k):
            self.pages = [
                {"records": [{"updated_at": "2026-09-20T00:00:00.000Z"}, {"updated_at": "2026-09-01T00:00:00.000Z"}],
                 "records_matched": 5000, "limit": 2, "updated_after_applied": "2025-09-25T12:00:00.316Z",
                 "updated_after_capped": False, "next_updated_after": "2026-09-20T00:00:00.000Z"},
                {"records": [], "records_matched": 0, "limit": 2},
            ]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def changed(self, kind, after):
            return self.pages.pop(0)

    monkeypatch.setattr(payraapi, "PayraClient", Newest)
    said = jobs.payra_reach(NS(secrets=NS(payra_api_token="tok-fake", payra_site_id="s")), now=NOW)

    assert "• Page 1: asked from September 25, 2025, Payra used September 25, 2025 · 5000 matched, 2 returned" in said
    assert "September 1, 2026 to September 20, 2026, newest first · next from September 20, 2026" in said
    assert "• Page 2:" in said and "0 matched, 0 returned" in said


def test_a_full_stop_after_the_number_or_name_is_not_part_of_it():
    """"@Ryte invoice INV-18089." looked for “INV-18089.”."""
    from wilbyte.bot import mentions

    assert mentions.parse("<@1> invoice INV-18089.").brief == "INV-18089"
    assert mentions.parse("<@1> send me a copy of David Pereira's paid invoice.").brief == "David Pereira"
    assert mentions.parse("<@1> invoice of David Pereira!").brief == "David Pereira"
    data = _pereira(invoice={"_id": "invDP"})
    assert payraapi.paid_invoices(data, "INV-18089.") != []


# ------------------------------------------------ "payra find INV-18089"


class _Walk:
    """Payra, a page at a time, oldest first."""

    pages: list = []

    def __init__(self, token, site, *, timeout=60.0):
        self.timeout = timeout
        type(self).made = self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def changed(self, kind, after):
        type(self).asked = getattr(type(self), "asked", []) + [after]
        one = type(self).pages.pop(0)
        if isinstance(one, Exception):
            raise one
        return one


def _dp(n, number="INV-1", name="Someone Else", updated="2026-09-01T00:00:00.000Z"):
    record = _invoice(n, name=name, email=f"x{n}@example.com", updated=updated)
    record["invoice_number"] = number
    return record


def _find(monkeypatch, pages, target="INV-18089"):
    from types import SimpleNamespace as NS

    from wilbyte.bot import jobs

    _Walk.pages, _Walk.asked = list(pages), []
    monkeypatch.setattr(payraapi, "PayraClient", _Walk)
    ticks = iter(range(0, 1000, 3))
    return jobs.payra_find(NS(secrets=NS(payra_api_token="tok-fake", payra_site_id="s")), target,
                           now=NOW, clock=lambda: next(ticks))


def test_it_walks_payra_until_it_finds_the_invoice_and_keeps_what_it_read(monkeypatch):
    said = _find(monkeypatch, [
        {"records": [_dp(1), _dp(2)], "records_matched": 900, "limit": 2, "next_updated_after": "T1"},
        {"records": [_dp(3), _dp(4, "INV-18089", "David Pereira", "2026-09-04T16:00:00.000Z")],
         "records_matched": 898, "limit": 2, "next_updated_after": "T2"},
        {"records": [_dp(5)], "records_matched": 1, "limit": 2},
    ])

    assert _Walk.asked == ["2026-04-28T12:00:00.316Z", "T1"], "stops once found"
    assert _Walk.made.timeout == 240.0
    assert "• Page 1: from April 28, 2026 · 900 matched, 2 returned (limit 2) · September 1, 2026 to September 1, 2026 · 3s" in said
    assert "✅ **Found:** #INV-18089 David Pereira $700.00, dated September 19, 2026" in said
    assert "`@RYTE invoice INV-18089` sends it" in said
    assert set(payraapi.load()["invoices"]) == {"inv1", "inv2", "inv3", "inv4"}


def test_by_whole_name_or_email_too(monkeypatch):
    said = _find(monkeypatch, [{"records": [_dp(4, "INV-18089", "David Pereira")], "limit": 5}],
                 target="David Pereira")
    assert "✅ **Found:** #INV-18089" in said
    said = _find(monkeypatch, [{"records": [_dp(4, "INV-18089", "David Pereira")], "limit": 5}],
                 target="David")
    assert "isn't in them" in said


def test_a_page_that_times_out_keeps_what_came_before(monkeypatch):
    said = _find(monkeypatch, [
        {"records": [_dp(1)], "records_matched": 5, "limit": 1, "next_updated_after": "T1"},
        TimeoutError("The read operation timed out"),
    ])
    assert "• Stopped: The read operation timed out" in said
    assert "**“INV-18089” isn't in them.**" in said
    assert "inv1" in payraapi.load()["invoices"]


def test_asking_to_find():
    from wilbyte.bot import mentions

    got = mentions.parse("<@1> payra find INV-18089")
    assert (got.action, got.brief) == ("payrafind", "INV-18089")
    assert mentions.parse("<@1> payra search David Pereira").brief == "David Pereira"
    assert mentions.parse("<@1> payra status").action == "payrastatus"


# ------------------------------------------------ older than Payra's week


def test_find_asks_for_the_invoice_by_id_when_the_list_has_not_got_it(monkeypatch):
    class WithOne(_Walk):
        def one(self, kind, key):
            assert (kind, key) == ("invoice", "INV-18089")
            return _dp(9, "INV-18089", "David Pereira")

    from types import SimpleNamespace as NS

    from wilbyte.bot import jobs

    WithOne.pages, WithOne.asked = [{"records": [_dp(1)], "limit": 5}], []
    monkeypatch.setattr(payraapi, "PayraClient", WithOne)
    said = jobs.payra_find(NS(secrets=NS(payra_api_token="t", payra_site_id="s")), "INV-18089",
                           now=NOW, clock=lambda: 0)
    assert "• Asked for “INV-18089” by id: found" in said and "✅ **Found:** #INV-18089" in said
    assert "inv9" in payraapi.load()["invoices"]


def test_the_client_asks_for_one_invoice(monkeypatch):
    import httpx

    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path.endswith("/NOPE"):
            return httpx.Response(404, json={})
        return httpx.Response(200, json={"record": {"_id": "x1", "invoice_number": "INV-1"}})

    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    with payraapi.PayraClient("tok-fake", "site1") as client:
        assert client.one("invoice", "INV-1")["_id"] == "x1"
        assert client.one("invoice", "NOPE") is None
    assert str(seen[0].url) == "https://api.payra.com/api/v3.1/site/site1/invoice/INV-1"
    assert {one.method for one in seen} == {"GET"}


def _inbox(monkeypatch, emails):
    from types import SimpleNamespace as NS

    from wilbyte import gmail

    asked = []

    class Reading:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def invoices_for(self, *terms, since=None):
            asked.append(terms)
            return emails

    monkeypatch.setattr(gmail, "open_gmail", lambda secrets: Reading())
    return asked, NS(secrets=NS(gmail_invoice_sender="payra@example.com"))


def test_older_than_payras_week_it_sends_payras_own_emails(monkeypatch):
    from wilbyte import gmail
    from wilbyte.bot import jobs

    payraapi.save(_disputed_board())
    asked, config = _inbox(monkeypatch, [
        gmail.Found("m1", subject="Payment Confirmation - David Pereira", when="Thu, 4 Sep 2026",
                    body="Reference R-1\nTotal $1,407.60"),
        gmail.Found("m2", subject="Payment Error - David Pereira", when="Wed, 3 Sep 2026", body="declined"),
    ])
    printed = []
    monkeypatch.setattr(jobs, "_print_pdf", lambda html: printed.append(html) or b"%PDF-mail")

    pdf, found, problem = jobs.paid_invoice_pdf("David Pereira", config)

    assert pdf == b"%PDF-mail" and found == [] and problem == ""
    assert asked == [("David Pereira",)]
    assert "Payment Confirmation - David Pereira" in printed[0] and "Total $1,407.60" in printed[0]
    assert "Payment Error" not in printed[0], "a failed payment isn't proof of one"
    assert "Payment confirmation from Payra — as emailed" in printed[0]


def test_payras_copy_comes_first_and_the_inbox_is_not_asked(monkeypatch):
    from wilbyte.bot import jobs

    payraapi.save(_disputed_board())
    asked, config = _inbox(monkeypatch, [])
    monkeypatch.setattr(jobs, "_print_pdf", lambda html: b"%PDF-api")
    pdf, found, _ = jobs.paid_invoice_pdf("Jose Zambrano", config)
    assert found and asked == []


def test_nothing_in_either_says_what_it_found(monkeypatch):
    from wilbyte.bot import jobs

    payraapi.save(_disputed_board())
    _asked, config = _inbox(monkeypatch, [])
    assert "No Payra invoice for “David Pereira”" in jobs.paid_invoice_pdf("David Pereira", config)[2]


def test_the_emails_are_sent_as_their_own_file(monkeypatch):
    import asyncio

    from wilbyte import gmail
    from wilbyte.bot import client, jobs

    _asked, config = _inbox(monkeypatch, [gmail.Found("m1", subject="Payment Confirmation", body="x")])
    monkeypatch.setattr(jobs, "_print_pdf", lambda html: b"%PDF-mail")
    sent = []

    class Heard:
        async def send(self, content=None, **kw):
            sent.append((content, kw.get("file")))

    asyncio.run(client._send_paid_invoice(Heard(), "David Pereira", config))
    words, file = sent[0]
    assert "Payra's own payment confirmation emails" in words
    assert file.filename == "David Pereira - Payra payment confirmations.pdf"


PEREIRA_EMAIL = (
    "You Just Got Paid!      Reference #   FJZ3FZZHJC7N-PU58     Paid   September 4, 2026     Customer    David "
    "Pereira      Payment Method   Visa **** 0000     Invoice #    INV-18089      Subtotal   $1,600.00     Discounts   "
    "-$240.00     Total   $1,360.00     Other Fees   $47.60     Total Paid   $1,407.60     Reference #   FJZ3FZZHJC7N-"
    "PU58        View Payment     Powered by PAYRA     Own a business and want to get paid faster?"
)


def test_the_confirmation_is_laid_out_field_by_field():
    """It arrived as one run-on paragraph with Payra's advert on the end."""
    from wilbyte.bot import jobs

    fields = dict(jobs.receipt_fields(PEREIRA_EMAIL))
    assert fields["Invoice #"] == "INV-18089"
    assert fields["Paid"] == "September 4, 2026"
    assert fields["Customer"] == "David Pereira"
    assert fields["Total"] == "$1,360.00" and fields["Other Fees"] == "$47.60"
    assert fields["Total Paid"] == "$1,407.60"
    assert fields["Reference #"] == "FJZ3FZZHJC7N-PU58"

    from wilbyte import gmail

    page = jobs._emails_html([gmail.Found("m", subject="Payment confirmation", when="Fri", body=PEREIRA_EMAIL)], "x")
    assert "<tr><th>Total Paid</th><td>$1,407.60</td></tr>" in page, "no designed version: the table"
    assert "Own a business" not in page, "Payra's advert left off"
    assert "The email as written" in page


PAYRA_DESIGN = (
    "<html><head><style>.paid{color:#1d7a3a}</style><script>alert(1)</script></head>"
    "<body><img src='https://example.com/payra-logo.png'><h1 class=paid>You Just Got Paid!</h1>"
    "<table><tr><td>Total Paid</td><td>$1,407.60</td></tr></table></body></html>"
)


def test_the_email_is_copied_as_payra_designed_it():
    """"yes do option 2" - Payra's own layout, logo and all."""
    from wilbyte import gmail
    from wilbyte.bot import jobs

    page = jobs._emails_html([gmail.Found("m", subject="Payment confirmation [Invoice #INV-18089]",
                                          when="Fri, 4 Sep 2026", body=PEREIRA_EMAIL, html=PAYRA_DESIGN)], "x")

    assert "<h1 class=paid>You Just Got Paid!</h1>" in page and "payra-logo.png" in page
    assert "<style>.paid{color:#1d7a3a}</style>" in page, "Payra's own styles kept"
    assert "<script" not in page
    assert "Payment confirmation from Payra — as emailed · Payment confirmation [Invoice #INV-18089] · sent Fri, 4 Sep 2026" in page
    assert "<tr><th>" not in page, "the table only when there is no designed email"


def test_the_designed_email_is_read_off_gmail():
    import base64

    from wilbyte import gmail

    def part(kind, text):
        return {"mimeType": kind, "body": {"data": base64.urlsafe_b64encode(text.encode()).decode()}}

    payload = {"mimeType": "multipart/alternative", "parts": [part("text/plain", "Total Paid $1"),
                                                              part("text/html", "<b>Total Paid</b> $1")]}
    assert gmail._first_part(payload, "text/html") == "<b>Total Paid</b> $1"
