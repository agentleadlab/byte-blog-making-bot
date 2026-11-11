"""Christopher Gallion's $100 Zelle: asked about an hour after Nova had pulled
his 100 aged MTG - the pull wasn't in the payment's thread."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from wilbyte import agedqc
from wilbyte.bot import client

POSTED = datetime(2026, 11, 10, 23, 0, tzinfo=timezone.utc)


def _result(**over):
    return SimpleNamespace(**{"client": "Christopher Gallion", "paid": SimpleNamespace(lead_type=""), "talk": [],
                              **over})


def test_an_aged_pull_kept_for_them_that_day_makes_it_aged(monkeypatch):
    monkeypatch.setattr(agedqc, "recalled", lambda: {"t1": {"client": "Christopher Gallion", "invoice": "INV-21985",
                                                            "at": (POSTED - timedelta(hours=2)).isoformat()}})
    said = client._aged_pulled_for(_result(), {"posted": POSTED.isoformat()}, None)
    assert "Christopher Gallion" in said and "INV-21985" in said


def test_a_pull_days_away_or_someone_else_isnt(monkeypatch):
    monkeypatch.setattr(agedqc, "recalled", lambda: {
        "t1": {"client": "Christopher Gallion", "at": (POSTED - timedelta(days=8)).isoformat()},
        "t2": {"client": "Chris Gallo", "at": POSTED.isoformat()}})
    assert client._aged_pulled_for(_result(), {"posted": POSTED.isoformat()}, None) == ""


def test_a_fresh_payment_is_never_taken_for_an_aged_pull(monkeypatch):
    monkeypatch.setattr(agedqc, "recalled", lambda: {"t1": {"client": "Christopher Gallion", "at": POSTED.isoformat()}})
    fresh = _result(paid=SimpleNamespace(lead_type="Text Verified Veteran Plus"))
    assert client._aged_pulled_for(fresh, {"posted": POSTED.isoformat()}, None) == ""
