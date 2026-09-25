"""The copy of Payra's invoices and payments: read in pages, kept in step,
looked up by the whole person."""

from datetime import datetime, timezone

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
