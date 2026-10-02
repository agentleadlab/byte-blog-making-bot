"""PandaDoc, read for signed contracts - and only read."""

import httpx
import pytest

from wilbyte import pandadoc
from wilbyte.bot import mentions


def _answering(monkeypatch, status, body=None):
    asked = []
    real = httpx.Client

    def handler(request):
        asked.append(request)
        return httpx.Response(status, json=body or {})

    monkeypatch.setattr(httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return asked


def test_a_working_key_lists_the_latest_signed(monkeypatch):
    asked = _answering(monkeypatch, 200, {"results": [
        {"name": "Agent Agreement - Jo Example", "date_modified": "2026-09-30T10:00:00Z"},
    ]})
    said = pandadoc.test("k")

    assert said.startswith("✅") and "Jo Example — signed 2026-09-30" in said
    assert asked[0].method == "GET"
    assert asked[0].headers["Authorization"] == "API-Key k"
    assert asked[0].url.params["status"] == "2"


@pytest.mark.parametrize("status, words", [(401, "isn't valid"), (403, "plan")])
def test_a_refusal_is_said_in_words(monkeypatch, status, words):
    _answering(monkeypatch, status)
    assert words in pandadoc.test("k")


def test_no_key_is_said():
    assert "PANDADOC_API_KEY" in pandadoc.test("")


def test_it_only_ever_reads():
    """Nothing in the client can send, change or delete a document."""
    methods = [name for name in dir(pandadoc.PandaDoc) if not name.startswith("_")]
    assert sorted(methods) == ["completed", "completed_since", "details", "get", "pdf", "search"]
    import inspect

    source = inspect.getsource(pandadoc.PandaDoc)
    assert ".post(" not in source and ".put(" not in source and ".delete(" not in source


def test_asking_for_it():
    assert mentions.parse("pandadoc test", max_batch=5).action == "pandadoctest"
    assert mentions.parse("panda doc check", max_batch=5).action == "pandadoctest"


class Reader:
    """A stubbed PandaDoc: one page of signed documents and their recipients."""

    def __init__(self, docs, recipients):
        self.docs, self.recipients, self.asked = docs, recipients, []

    def completed_since(self, since, *, page=1, count=100):
        self.asked.append(since)
        return self.docs if page == 1 else []

    def details(self, doc_id):
        return {"recipients": [{"email": e} for e in self.recipients[doc_id]],
                "date_completed": "2026-10-01T15:00:00Z"}


def test_sync_keeps_who_each_contract_went_to():
    from datetime import datetime, timezone

    reader = Reader(
        [{"id": "d1", "name": "Basic Contract (1x lead order) x Jo Example",
          "date_modified": "2026-10-01T15:00:00Z"}],
        {"d1": ["Jo@Example.com"]},
    )
    data = pandadoc.sync("k", now=datetime(2026, 10, 2, tzinfo=timezone.utc), client=reader)
    [contract] = pandadoc.contracts(data)

    assert contract.person == "Jo Example" and contract.emails == ("jo@example.com",)
    # Next time it reads from a day before the newest one it saw.
    assert data["since"] == "2026-09-30T15:00:00Z"


def test_a_document_whose_recipients_arent_read_yet_is_not_a_contract_yet():
    data = {"docs": {"d1": {"title": "x", "modified": "2026-10-01T00:00:00Z"}}}
    assert pandadoc.contracts(data) == []



# ------------------------------------------------ the contract for a dispute

from datetime import date, datetime, timezone


class Searcher:
    def __init__(self, docs):
        self.docs = docs

    def search(self, words, *, count=50):
        return [{"id": d, "name": title, "date_modified": signed}
                for d, (title, _emails, signed) in self.docs.items()]

    def details(self, doc_id):
        title, emails, signed = self.docs[doc_id]
        return {"recipients": [{"email": e} for e in emails], "date_completed": signed}


DAVID = {
    "first": ("Basic Contract (1x lead order) x David Pereira", ["david@example.com"], "2026-06-01T10:00:00Z"),
    "second": ("Basic Contract (1x lead order) x David Pereira", ["david@example.com"], "2026-08-10T10:00:00Z"),
    "later": ("Basic Contract (1x lead order) x David Pereira", ["david@example.com"], "2026-09-20T10:00:00Z"),
    "someone": ("Basic Contract (1x lead order) x David Perez", ["perez@example.com"], "2026-08-10T10:00:00Z"),
}


def test_the_disputed_orders_contract_is_the_one_signed_before_it_was_paid():
    found, how, problem = pandadoc.find_signed(
        "k", name="David Pereira", email="david@example.com", paid=date(2026, 8, 11),
        client=Searcher(DAVID),
    )
    assert (found.doc_id, how, problem) == ("second", "email", "")


def test_a_contract_signed_just_after_paying_still_counts():
    found, _how, _ = pandadoc.find_signed(
        "k", name="David Pereira", email="david@example.com", paid=date(2026, 8, 8),
        client=Searcher(DAVID),
    )
    assert found.doc_id == "second"


def test_somebody_with_a_similar_name_is_not_them():
    found, _how, problem = pandadoc.find_signed(
        "k", name="David Perez Smith", client=Searcher(DAVID),
    )
    assert found is None and "No signed contract" in problem


def test_by_name_only_says_so():
    found, how, _ = pandadoc.find_signed(
        "k", name="David Pereira", email="other@example.com", paid=date(2026, 8, 11),
        client=Searcher(DAVID),
    )
    assert how == "name"


def test_the_rebuttal_gets_the_pdf_from_pandadoc(monkeypatch):
    from types import SimpleNamespace

    from wilbyte.bot import jobs

    class Reading:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def pdf(self, doc_id):
            return b"%PDF-" + doc_id.encode()

    monkeypatch.setattr(pandadoc, "find_signed", lambda key, **kw: (
        pandadoc_contract("second"), "email", ""))
    monkeypatch.setattr(pandadoc, "PandaDoc", lambda key: Reading())
    config = SimpleNamespace(secrets=SimpleNamespace(pandadoc_api_key="k", gmail_contract_sender=""))
    dispute = SimpleNamespace(customer_name="David Pereira", customer_email="david@example.com",
                              paid=lambda: date(2026, 8, 11))
    said, pdf, called, problem = jobs._signed_contract(config, dispute)

    assert pdf == b"%PDF-second" and problem == ""
    assert called == "Basic Contract (1x lead order) x David Pereira.pdf"
    assert "Signed August 10, 2026 in PandaDoc by david@example.com" in said


def test_contract_of_somebody_uses_pandadoc_too(monkeypatch):
    from types import SimpleNamespace

    from wilbyte.bot import jobs

    monkeypatch.setattr(jobs, "_pandadoc_contract", lambda config, who, **kw: ("says", b"%PDF", "c.pdf", ""))
    config = SimpleNamespace(secrets=SimpleNamespace(pandadoc_api_key="k"))
    assert jobs.signed_contract_for(config, "David Pereira") == ("says", b"%PDF", "c.pdf", "")


def pandadoc_contract(doc_id):
    from wilbyte.contracts import Contract

    title, emails, signed = DAVID[doc_id]
    return Contract(doc_id, title, datetime.fromisoformat(signed.replace("Z", "+00:00")), tuple(emails))


def test_the_one_sent_to_their_email_beats_a_later_one_by_name_only():
    docs = dict(DAVID)
    docs["namesake"] = ("Basic Contract (1x lead order) x David Pereira",
                        ["another.david@example.com"], "2026-08-11T09:00:00Z")
    found, how, _ = pandadoc.find_signed(
        "k", name="David Pereira", email="david@example.com", paid=date(2026, 8, 11),
        client=Searcher(docs),
    )
    assert (found.doc_id, how) == ("second", "email")
