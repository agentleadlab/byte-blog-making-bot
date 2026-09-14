"""The invoice, fetched from the inbox it was emailed to.

Summit Pay has no API worth the name, so the invoice for a disputed
transaction is found where it actually is.
"""

from __future__ import annotations

import base64
from datetime import date
from types import SimpleNamespace

import pytest

from wilbyte import gmail


class FakeGmail:
    """Enough of Gmail to answer a search and hand back one PDF."""

    INVOICE = b"%PDF-1.4 the invoice"

    def __init__(self):
        self.asked = []

    def post(self, url, data=None, **kwargs):
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"access_token": "t", "expires_in": 3600},
        )

    def get(self, url, params=None, headers=None):
        self.asked.append((url, dict(params or {})))
        if url.endswith("/messages"):
            return SimpleNamespace(
                status_code=200, json=lambda: {"messages": [{"id": "m1"}]},
            )
        if url.endswith("/messages/m1"):
            return SimpleNamespace(status_code=200, json=lambda: {
                "payload": {
                    "headers": [
                        {"name": "Subject", "value": "Invoice 1042"},
                        {"name": "Date", "value": "Mon, 15 Jun 2026 09:02:00 -0400"},
                    ],
                    "parts": [
                        {"filename": "", "body": {}},
                        {"parts": [{
                            "filename": "invoice-1042.pdf",
                            "body": {"attachmentId": "a1"},
                        }]},
                        {"filename": "signature.gif", "body": {"attachmentId": "a2"}},
                    ],
                },
            })
        return SimpleNamespace(status_code=200, json=lambda: {
            "data": base64.urlsafe_b64encode(self.INVOICE).decode().rstrip("="),
        })

    def close(self):
        pass


def client(monkeypatch, *, sender="no-reply@summitpay.co"):
    fake = FakeGmail()
    monkeypatch.setattr(gmail.httpx, "Client", lambda **kw: fake)
    creds = gmail.Credentials("id", "secret", "refresh")
    return gmail.GmailClient(creds, sender=sender), fake


def test_every_search_is_pinned_to_the_one_sender(monkeypatch):
    """The boundary of what RYTE can read, not a convenience."""
    one, fake = client(monkeypatch)

    one.invoices_for("Jose Zambrano")

    query = fake.asked[0][1]["q"]
    assert "from:no-reply@summitpay.co" in query
    assert '"Jose Zambrano"' in query


def test_the_search_terms_go_in_as_written(monkeypatch):
    one, fake = client(monkeypatch)

    one.invoices_for("Jose Zambrano", "1,552.50")

    query = fake.asked[0][1]["q"]
    assert '"1,552.50"' in query


def test_it_looks_back_far_enough_for_an_old_transaction(monkeypatch):
    """Jose Zambrano's was June, disputed in September."""
    one, fake = client(monkeypatch)

    one.invoices_for("Jose", since=date(2026, 6, 1))

    assert "after:2026/06/01" in fake.asked[0][1]["q"]


def test_what_comes_back_is_the_email_and_its_files(monkeypatch):
    one, _fake = client(monkeypatch)

    (found,) = one.invoices_for("Jose Zambrano")

    assert found.subject == "Invoice 1042"
    assert found.files == [("invoice-1042.pdf", "a1")]


def test_a_signature_image_is_not_evidence(monkeypatch):
    """An invoice is a PDF and a receipt is sometimes a screenshot; a gif in
    somebody's signature is neither."""
    one, _fake = client(monkeypatch)

    (found,) = one.invoices_for("Jose")

    assert "signature.gif" not in [name for name, _id in found.files]


def test_the_attachment_comes_back_as_bytes(monkeypatch):
    one, _fake = client(monkeypatch)

    assert one.download("m1", "a1") == FakeGmail.INVOICE


def test_without_a_sender_there_is_nothing_to_search(monkeypatch):
    """No sender is not "search everything" — it is a refusal."""
    with pytest.raises(gmail.GmailError) as raised:
        client(monkeypatch, sender="")

    assert "GMAIL_INVOICE_SENDER" in str(raised.value)


def test_a_token_without_the_scope_says_which_scope(monkeypatch):
    fake = FakeGmail()
    fake.get = lambda *a, **k: SimpleNamespace(status_code=403, text="denied")
    monkeypatch.setattr(gmail.httpx, "Client", lambda **kw: fake)
    one = gmail.GmailClient(gmail.Credentials("i", "s", "r"), sender="x@y.co")

    with pytest.raises(gmail.GmailError) as raised:
        one.invoices_for("Jose")

    assert "gmail.readonly" in str(raised.value)
