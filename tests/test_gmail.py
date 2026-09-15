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
        if url.endswith("/messages/payra"):
            return SimpleNamespace(status_code=200, json=lambda: {
                "payload": {
                    "headers": [
                        {"name": "Subject", "value": PAYRA_SUBJECT},
                        {"name": "Date", "value": "Mon, 14 Sep 2026 19:06:00 -0400"},
                    ],
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {"mimeType": "text/plain", "body": {"data": _packed("")}},
                        {"mimeType": "text/html", "body": {"data": _packed(PAYRA_HTML)}},
                    ],
                },
            })
        return SimpleNamespace(status_code=200, json=lambda: {
            "data": base64.urlsafe_b64encode(self.INVOICE).decode().rstrip("="),
        })

    def close(self):
        pass


# The real one, off the inbox. There is no attachment: the receipt is the
# email, and the invoice number in it is a link to a page rather than a file.
PAYRA_SUBJECT = (
    "Agent Lead Lab | Payment confirmation [Invoice #INV-18490] - Jay Rodriguez"
)
PAYRA_HTML = """<html><body><h1>You Just Got Paid!</h1>
<table>
<tr><td>Reference #</td><td>FJZ3FXTG3C2U-PNJ2</td></tr>
<tr><td>Paid</td><td>September 14, 2026</td></tr>
<tr><td>Customer</td><td>Jay Rodriguez</td></tr>
<tr><td>Payment Method</td><td>Mastercard **** 1096</td></tr>
<tr><td>Invoice #</td><td><a href="https://payra.com/i/18490">INV-18490</a></td></tr>
<tr><td>Total Paid</td><td>$983.25</td></tr>
</table></body></html>"""


def _packed(text):
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def client(monkeypatch, *, sender="AgentLeadLab@payra.com"):
    fake = FakeGmail()
    monkeypatch.setattr(gmail.httpx, "Client", lambda **kw: fake)
    creds = gmail.Credentials("id", "secret", "refresh")
    return gmail.GmailClient(creds, sender=sender), fake


def test_every_search_is_pinned_to_the_one_sender(monkeypatch):
    """The boundary of what RYTE can read, not a convenience."""
    one, fake = client(monkeypatch)

    one.invoices_for("Jose Zambrano")

    query = fake.asked[0][1]["q"]
    assert "from:AgentLeadLab@payra.com" in query
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


def test_a_different_account_signs_in_with_its_own_token(monkeypatch):
    """"its a different gmail account is that okay?" — a refresh token belongs
    to one account, so the invoice inbox needs its own."""
    used = {}

    class Tokens(FakeGmail):
        def post(self, url, data=None, **kwargs):
            used.update(data or {})
            return super().post(url, data=data, **kwargs)

    fake = Tokens()
    monkeypatch.setattr(gmail.httpx, "Client", lambda **kw: fake)
    one = gmail.open_gmail(SimpleNamespace(
        google_client_id="id", google_client_secret="secret",
        google_refresh_token="the-sheets-one",
        gmail_refresh_token="the-invoice-one",
        gmail_invoice_sender="no-reply@summitpay.co",
    ))
    one.invoices_for("Jose")

    # The same app, a different person.
    assert used["client_id"] == "id"
    assert used["refresh_token"] == "the-invoice-one"


def test_one_address_for_everything_needs_no_second_token(monkeypatch):
    used = {}

    class Tokens(FakeGmail):
        def post(self, url, data=None, **kwargs):
            used.update(data or {})
            return super().post(url, data=data, **kwargs)

    fake = Tokens()
    monkeypatch.setattr(gmail.httpx, "Client", lambda **kw: fake)
    one = gmail.open_gmail(SimpleNamespace(
        google_client_id="id", google_client_secret="secret",
        google_refresh_token="the-sheets-one",
        gmail_refresh_token="",
        gmail_invoice_sender="no-reply@summitpay.co",
    ))
    one.invoices_for("Jose")

    assert used["refresh_token"] == "the-sheets-one"


def test_an_invoice_with_no_attachment_is_still_found(monkeypatch):
    """Payra's confirmation carries none, and asking for one found nothing at
    all — which is the whole of what is being looked for."""
    one, fake = client(monkeypatch)

    one.invoices_for("Jay Rodriguez")

    assert "has:attachment" not in fake.asked[0][1]["q"]


def test_the_receipt_is_the_email_itself(monkeypatch):
    """The reference, who paid, the card and the total are a table in the
    body. That is the exhibit."""
    one, fake = client(monkeypatch)
    fake.get = lambda url, params=None, headers=None: (
        SimpleNamespace(status_code=200, json=lambda: {"messages": [{"id": "payra"}]})
        if url.endswith("/messages")
        else FakeGmail.get(fake, url, params, headers)
    )

    (found,) = one.invoices_for("Jay Rodriguez")

    assert "Jay Rodriguez" in found.subject
    assert "FJZ3FXTG3C2U-PNJ2" in found.body
    assert "$983.25" in found.body
    assert "Mastercard **** 1096" in found.body
    # And no markup left in it.
    assert "<td>" not in found.body
    assert "<a href" not in found.body


def test_the_plain_part_wins_when_there_is_one(monkeypatch):
    from wilbyte.gmail import _body_of

    said = _body_of({
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": _packed("the plain one")}},
            {"mimeType": "text/html", "body": {"data": _packed("<p>the html</p>")}},
        ],
    })

    assert said == "the plain one"


def test_gmail_can_have_its_own_oauth_client(monkeypatch):
    """A refresh token belongs to the client it was minted under as much as to
    the person, and this project has several — so "unauthorized_client" with
    nothing else to go on."""
    used = {}

    class Tokens(FakeGmail):
        def post(self, url, data=None, **kwargs):
            used.update(data or {})
            return super().post(url, data=data, **kwargs)

    fake = Tokens()
    monkeypatch.setattr(gmail.httpx, "Client", lambda **kw: fake)
    one = gmail.open_gmail(SimpleNamespace(
        google_client_id="935508900085-sheets",
        google_client_secret="sheets-secret",
        google_refresh_token="the-sheets-one",
        gmail_client_id="965472774442-gmail",
        gmail_client_secret="gmail-secret",
        gmail_refresh_token="the-invoice-one",
        gmail_invoice_sender="AgentLeadLab@payra.com",
    ))
    one.invoices_for("Jay")

    assert used["client_id"] == "965472774442-gmail"
    assert used["client_secret"] == "gmail-secret"
    assert used["refresh_token"] == "the-invoice-one"


def test_one_client_for_everything_still_needs_nothing_extra(monkeypatch):
    used = {}

    class Tokens(FakeGmail):
        def post(self, url, data=None, **kwargs):
            used.update(data or {})
            return super().post(url, data=data, **kwargs)

    fake = Tokens()
    monkeypatch.setattr(gmail.httpx, "Client", lambda **kw: fake)
    one = gmail.open_gmail(SimpleNamespace(
        google_client_id="935508900085-sheets",
        google_client_secret="sheets-secret",
        google_refresh_token="the-sheets-one",
        gmail_client_id="",
        gmail_client_secret="",
        gmail_refresh_token="the-invoice-one",
        gmail_invoice_sender="AgentLeadLab@payra.com",
    ))
    one.invoices_for("Jay")

    assert used["client_id"] == "935508900085-sheets"
    assert used["refresh_token"] == "the-invoice-one"


# ---------------------------- which mailbox the token actually reads

# A refresh token belongs to one account. Minted against the wrong one it looks
# exactly like an empty inbox — every search finds nothing and none of it is an
# error — so the receipt and the contract quietly stop appearing in rebuttals.


class Mailbox:
    """A stubbed Gmail client that knows whose it is."""

    def __init__(self, who="franklinmay@agentleadlab.com", many=10330, blows_up=None):
        self.who, self.many, self.blows_up = who, many, blows_up
        self.searched = []

    def whoami(self):
        if self.blows_up is not None:
            raise self.blows_up
        return self.who, self.many

    def invoices_for(self, *terms, since=None):
        self.searched.append(terms)
        return []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _checking_gmail(monkeypatch, box, *, sender="AgentLeadLab@payra.com"):
    from types import SimpleNamespace

    from wilbyte import gmail
    from wilbyte.bot import jobs

    monkeypatch.setattr(gmail, "open_gmail", lambda secrets: box)
    return jobs._check_gmail(
        SimpleNamespace(secrets=SimpleNamespace(gmail_invoice_sender=sender))
    )


def test_the_check_names_the_mailbox_being_read(monkeypatch):
    """So signing in as the wrong account is visible rather than silent."""
    (ok, said), = _checking_gmail(monkeypatch, Mailbox())

    assert ok is True
    assert "franklinmay@agentleadlab.com" in said
    assert "10,330" in said


def test_a_token_for_the_wrong_account_still_reads_as_a_pass_but_names_it(monkeypatch):
    """There is no way to know from here which account is right — naming it is
    the whole of what can be done, and it is enough."""
    (ok, said), = _checking_gmail(monkeypatch, Mailbox(who="someone.else@gmail.com"))

    assert ok is True
    assert "someone.else@gmail.com" in said


def test_a_missing_scope_is_reported_rather_than_raised(monkeypatch):
    from wilbyte import gmail

    (ok, said), = _checking_gmail(
        monkeypatch, Mailbox(blows_up=gmail.GmailError("minted without the Gmail read scope")),
    )

    assert ok is False
    assert "Gmail read scope" in said


def test_gmail_not_configured_is_neither_pass_nor_fail(monkeypatch):
    (ok, said), = _checking_gmail(monkeypatch, Mailbox(), sender="")

    assert ok is None
    assert "not configured" in said


def test_the_check_reads_nobody_s_mail(monkeypatch):
    """Whose inbox it is, not what is in it."""
    box = Mailbox()
    _checking_gmail(monkeypatch, box)

    assert box.searched == []
