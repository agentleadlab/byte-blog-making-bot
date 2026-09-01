"""Payment links: what Stripe is asked for, and what is never asked twice."""

import httpx
import pytest

from wilbyte import products, stripepay
from wilbyte.stripepay import StripeError


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("STRIPE_API_KEY", "rk_test_abc")


def test_nothing_happens_without_a_key(monkeypatch):
    monkeypatch.delenv("STRIPE_API_KEY", raising=False)
    assert stripepay.configured() is False
    with pytest.raises(StripeError, match="isn't set up"):
        stripepay.api_key()


def test_a_test_key_is_known_from_a_live_one(monkeypatch):
    """The confirm message says so, because one of them takes real money."""
    assert stripepay.live() is False
    monkeypatch.setenv("STRIPE_API_KEY", "rk_live_abc")
    assert stripepay.live() is True


def test_money_is_written_the_way_a_client_reads_it():
    assert stripepay.as_cents(621) == 62100
    assert stripepay.as_cents(1250.5) == 125050
    assert stripepay.dollars(62100) == "$621.00"
    assert stripepay.dollars(125050) == "$1,250.50"


def test_a_third_of_a_cent_does_not_go_missing():
    """Floating point money is how an invoice ends up a penny out."""
    assert stripepay.as_cents(0.1 + 0.2) == 30


def _reply(status, payload):
    return httpx.Response(
        status, json=payload, request=httpx.Request("GET", "https://api.stripe.com/")
    )


def test_an_existing_product_is_found_by_name(monkeypatch):
    """By name, not by a stored id: a product somebody made in the dashboard
    has to be found the same as one RYTE made."""
    monkeypatch.setattr(stripepay.httpx, "request", lambda *a, **k: _reply(200, {
        "data": [
            {"id": "prod_1", "name": "Facebook IUL Leads"},
            {"id": "prod_2", "name": "Text-Verified Widow Leads"},
        ],
        "has_more": False,
    }))
    found = stripepay.find_product("text-verified widow leads")
    assert found["id"] == "prod_2"


def test_a_product_that_is_not_there_is_made_once(monkeypatch):
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url))
        if method == "GET":
            return _reply(200, {"data": [], "has_more": False})
        return _reply(200, {"id": "prod_new", "name": "Spanish Instant IUL Leads"})

    monkeypatch.setattr(stripepay.httpx, "request", request)
    made = stripepay.ensure_product("Spanish Instant IUL Leads", "…")

    assert made["id"] == "prod_new"
    assert [method for method, _url in calls] == ["GET", "POST"]


def test_a_price_that_already_exists_is_reused(monkeypatch):
    """Otherwise the same package collects a new price row per sale."""
    monkeypatch.setattr(stripepay.httpx, "request", lambda *a, **k: _reply(200, {
        "data": [
            {"id": "price_low", "unit_amount": 50000, "currency": "usd"},
            {"id": "price_want", "unit_amount": 62100, "currency": "usd"},
        ],
    }))
    assert stripepay.find_price("prod_1", 62100)["id"] == "price_want"
    assert stripepay.find_price("prod_1", 999) is None


def test_a_subscription_price_is_never_mistaken_for_a_one_off():
    """A recurring price at the same number is a different thing to sell."""
    price = {"id": "p", "unit_amount": 62100, "currency": "usd",
             "recurring": {"interval": "month"}}
    assert price.get("recurring")  # the shape the check looks at


def test_an_existing_link_for_that_price_is_handed_back(monkeypatch):
    monkeypatch.setattr(stripepay.httpx, "request", lambda *a, **k: _reply(200, {
        "data": [
            {"id": "plink_other", "url": "https://buy.stripe.com/other",
             "line_items": {"data": [{"price": {"id": "price_other"}}]}},
            {"id": "plink_want", "url": "https://buy.stripe.com/want",
             "line_items": {"data": [{"price": {"id": "price_want"}}]}},
        ],
        "has_more": False,
    }))
    found = stripepay.find_link("price_want")
    assert found["url"] == "https://buy.stripe.com/want"
    assert stripepay.find_link("price_nobody_has") is None


def test_the_link_carries_who_asked_and_for_what(monkeypatch):
    sent = {}

    def request(method, url, **kwargs):
        sent.update(kwargs.get("data") or {})
        return _reply(200, {"id": "plink", "url": "https://buy.stripe.com/x"})

    monkeypatch.setattr(stripepay.httpx, "request", request)
    stripepay.make_link("price_1", note="40 Spanish Instant IUL Leads")

    assert sent["line_items[0][price]"] == "price_1"
    assert sent["metadata[asked_for]"] == "40 Spanish Instant IUL Leads"


def test_a_key_without_the_right_permission_says_so(monkeypatch):
    said = stripepay.explain(_reply(403, {"error": {"message": "not permitted."}}))
    assert "Write on payment links" in said


def test_a_rejected_key_is_told_apart_from_a_refused_call():
    assert "pasted whole" in stripepay.explain(_reply(401, {"error": {}}))


def test_the_whole_line_a_client_reads_is_settled_before_anything_is_made():
    """The confirm shows this, and it is what the link is labelled with."""
    said = "Can you make me a Klarna link for $621 for 40 basic spanish leads"
    product = products.find(said)

    assert stripepay.dollars(stripepay.as_cents(products.amount_asked(said))) == "$621.00"
    assert products.titled(products.line_for(said, product)) == (
        "40 Spanish Instant IUL Leads"
    )
