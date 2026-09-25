"""Reading Payra's API docs, and trying the token on them - read only."""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from wilbyte import payradocs

INTRO = """<html><head><script>var x = 1</script></head><body>
<nav><a href="/docs/3.1/introduction">Intro</a> <a href="/docs/3.1/invoices">Invoices</a>
<a href="https://elsewhere.com/docs/3.1/x">not theirs</a></nav>
<h1>Introduction</h1><p>All requests go to https://api.payra.com/v1 and need your token
in the <code>Authorization: Bearer YOUR_TOKEN</code> header.</p></body></html>"""

INVOICES = """<html><body><h2>List invoices</h2><pre>GET /v1/invoices</pre>
<h2>Get one</h2><pre>GET /v1/invoices/{invoiceId}</pre>
<h2>Create</h2><pre>POST /v1/invoices</pre>
<h2>Refund</h2><pre>DELETE /v1/transactions/{id}</pre>
<h2>Charge a card</h2><pre>POST /v1/charges</pre></body></html>"""


def test_a_docs_page_reads_as_text_and_its_sections_are_found():
    text = payradocs.page_text(INTRO)

    assert "# Introduction" in text and "var x" not in text
    assert "Authorization: Bearer YOUR_TOKEN" in text
    assert payradocs.doc_links(INTRO, payradocs.DOCS_START) == [
        "https://app.payra.com/docs/3.1/introduction", "https://app.payra.com/docs/3.1/invoices"]


def test_only_read_only_calls_with_nothing_to_fill_in_are_tried():
    text = payradocs.page_text(INTRO) + payradocs.page_text(INVOICES)

    calls = payradocs.calls_in(text)
    bases = payradocs.api_bases(text)

    assert bases == ["https://api.payra.com"]
    assert ("POST", "/v1/invoices") in calls and ("DELETE", "/v1/transactions/{id}") in calls
    assert payradocs.worth_trying(calls, bases) == ["https://api.payra.com/v1/invoices"]


def test_a_reply_is_described_by_its_field_names_never_its_values():
    shape = payradocs.shape_of({"data": [{"id": 7, "amount": 70000,
                                          "customer": {"email": "agent@x.com", "phone": "3125550188"}}],
                                "total": 1})

    assert shape == "{data: [1 × {id, amount, customer: {email, phone}}], total}"
    assert "agent@x.com" not in shape and "70000" not in shape


class Web:
    """httpx.Client, as far as the probe goes: pages by address, and every
    request kept."""

    asked: list = []

    def __init__(self, **kw):
        self.kw = kw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def get(self, url, headers=None, **kw):
        type(self).asked.append(("GET", url, dict(headers or {})))
        pages = {
            "https://app.payra.com/docs/3.1/introduction": INTRO,
            "https://app.payra.com/docs/3.1/invoices": INVOICES,
        }
        if url in pages:
            return NS(status_code=200, text=pages[url])
        if url == "https://api.payra.com/v1/invoices":
            return NS(status_code=200, text="", json=lambda: {"data": [{"id": 1, "status": "paid"}]})
        return NS(status_code=404, text="")

    def post(self, *a, **kw):
        raise AssertionError("the probe wrote to Payra")

    put = patch = delete = post


def _probe(monkeypatch, token="tok-SECRET-123"):
    import httpx

    from wilbyte.bot import jobs

    Web.asked = []
    monkeypatch.setattr(httpx, "Client", Web)
    return jobs.payra_probe(NS(secrets=NS(payra_api_token=token)))


def test_the_docs_are_read_and_the_token_tried_on_what_they_name(monkeypatch):
    said, docs = _probe(monkeypatch)

    assert "Read **2** page(s)" in said and "API address: `https://api.payra.com`" in said
    assert "Authorization: Bearer YOUR_TOKEN" in said
    assert "✅ `https://api.payra.com/v1/invoices` — HTTP 200 — {data: [1 × {id, status}]}" in said
    assert "# https://app.payra.com/docs/3.1/invoices" in docs and "List invoices" in docs


def test_the_token_goes_only_to_the_api_never_into_what_is_posted(monkeypatch):
    said, docs = _probe(monkeypatch)

    assert "tok-SECRET-123" not in said and "tok-SECRET-123" not in docs
    for method, url, headers in Web.asked:
        assert method == "GET"
        if "x-access-token" in headers:
            assert url.startswith("https://api.payra.com/")
            assert headers["x-access-token"] == "tok-SECRET-123"


def test_without_a_token_nothing_is_tried(monkeypatch):
    said, _docs = _probe(monkeypatch, token="")

    assert "PAYRA_API_TOKEN isn't in .env" in said
    assert not any("x-access-token" in headers for _m, _u, headers in Web.asked)


def test_the_command_is_understood():
    from wilbyte.bot import mentions

    assert mentions.parse("<@1> payra test").action == "payratest"


# Payra's own pages, as RYTE read them from the office Mac.
PAYRA_TEXT = """Stage Environment
https: / / stage-api.payra.com

Production Environment
https: / / api.payra.com
Endpoint
POST {base_url} / api / v3.1 / site / {site_id} / invoice / addUpdate
POST {base_url} / api / v3.1 / site / {site_id} / invoice / send
GET {base_url} / api / v3.1 / site / {site_id} / invoices
GET {base_url} / api / v3.1 / site / {site_id} / invoice / {external_id}
Every call to the API requires an x-access-token property be added to the request headers."""


def test_payras_spaced_out_addresses_are_read():
    calls = payradocs.calls_in(PAYRA_TEXT)

    assert ("GET", "/api/v3.1/site/{site_id}/invoices") in calls
    assert ("POST", "/api/v3.1/site/{site_id}/invoice/send") in calls
    assert payradocs.api_bases(PAYRA_TEXT) == ["https://api.payra.com"], "not the stage one"


def test_nothing_under_a_site_is_tried_without_the_sites_id():
    calls, bases = payradocs.calls_in(PAYRA_TEXT), payradocs.api_bases(PAYRA_TEXT)

    assert payradocs.worth_trying(calls, bases) == []
    assert payradocs.worth_trying(calls, bases, site_id="s1") == [
        "https://api.payra.com/api/v3.1/site/s1/invoices"]


def test_the_probe_says_it_needs_the_site_id(monkeypatch):
    import httpx

    from wilbyte.bot import jobs

    page = "<html><body><pre>" + PAYRA_TEXT.replace("\n", "<br>") + "</pre></body></html>"

    class Docs(Web):
        def get(self, url, headers=None, **kw):
            type(self).asked.append(("GET", url, dict(headers or {})))
            return NS(status_code=200, text=page) if "docs" in url else NS(status_code=404, text="")

    Docs.asked = []
    monkeypatch.setattr(httpx, "Client", Docs)

    said, _docs = jobs.payra_probe(NS(secrets=NS(payra_api_token="t", payra_site_id="")))

    assert "`GET /api/v3.1/site/{site_id}/invoices`" in said
    assert "PAYRA_SITE_ID isn't in .env" in said and "hyfin_api_access" in said
    assert not any("x-access-token" in headers for _m, _u, headers in Docs.asked)



def test_the_lists_are_asked_for_what_changed_lately():
    calls = [("GET", "/api/v3.1/site/{site_id}/payments?updated_after={updated_after}"),
             ("GET", "/api/v3.1/site/{site_id}/payment/{external_id}")]

    assert payradocs.worth_trying(calls, ["https://api.payra.com"], site_id="s1",
                                  since="2026-08-26T00:00:00Z") == [
        "https://api.payra.com/api/v3.1/site/s1/payments?updated_after=2026-08-26T00:00:00Z"]
