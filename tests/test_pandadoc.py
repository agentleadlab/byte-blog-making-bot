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
    assert sorted(methods) == ["completed", "completed_since", "details", "get"]
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
