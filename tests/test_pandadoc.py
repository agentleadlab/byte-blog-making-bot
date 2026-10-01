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
    assert sorted(methods) == ["completed", "get"]


def test_asking_for_it():
    assert mentions.parse("pandadoc test", max_batch=5).action == "pandadoctest"
    assert mentions.parse("panda doc check", max_batch=5).action == "pandadoctest"
