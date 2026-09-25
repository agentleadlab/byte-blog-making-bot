"""What RYTE spends on Claude, counted by job."""

from datetime import date
from types import SimpleNamespace as NS

import pytest

from wilbyte import usage

DAY = date(2026, 9, 25)


def _used(fresh=0, out=0, wrote=0, read=0):
    return NS(input_tokens=fresh, output_tokens=out,
              cache_creation_input_tokens=wrote, cache_read_input_tokens=read)


def test_prices_follow_the_model_and_the_cache():
    assert usage.price("claude-opus-5", fresh=1_000_000) == pytest.approx(5.0)
    assert usage.price("claude-opus-5", out=1_000_000) == pytest.approx(25.0)
    assert usage.price("claude-opus-5", wrote=1_000_000) == pytest.approx(6.25)
    assert usage.price("claude-opus-5", read=1_000_000) == pytest.approx(0.5)
    assert usage.price("claude-opus-5-5", fresh=1_000_000) == pytest.approx(4.0), "not Opus 5's price"
    assert usage.price("claude-sonnet-5", fresh=1_000_000, out=1_000_000) == pytest.approx(12.0)
    assert usage.price("claude-haiku-4-5-20251001", out=1_000_000) == pytest.approx(5.0)
    assert usage.price("something-new", fresh=1_000_000) == pytest.approx(5.0)


def test_each_call_is_added_to_its_job_for_the_day():
    usage.record("Responder drafts", "claude-opus-5", _used(fresh=10_000, out=200), day=DAY)
    usage.record("Responder drafts", "claude-opus-5", _used(fresh=1_000, read=9_000, out=200), day=DAY)
    usage.record("Blog posts", "claude-opus-5", _used(fresh=20_000, out=4_000), day=DAY)

    kept = usage.load()["days"]["2026-09-25"]
    assert kept["Responder drafts"]["calls"] == 2
    assert kept["Responder drafts"]["read"] == 9_000
    assert kept["Responder drafts"]["usd"] == pytest.approx(0.05 + 0.005 + 0.0045 + 0.005 + 0.005)
    assert kept["Blog posts"]["usd"] == pytest.approx(0.2)
    usage.record("Blog posts", "claude-opus-5", None, day=DAY)
    assert usage.load()["days"]["2026-09-25"]["Blog posts"]["calls"] == 1, "nothing to count"


def test_old_days_are_let_go():
    usage.record("Blog posts", "claude-opus-5", _used(fresh=1), day=date(2026, 1, 1))
    usage.record("Blog posts", "claude-opus-5", _used(fresh=1), day=DAY)
    assert list(usage.load()["days"]) == ["2026-09-25"]


def test_the_report_says_what_the_money_went_on():
    usage.record("Responder drafts", "claude-opus-5", _used(fresh=100_000, out=2_000), day=DAY)
    usage.record("Blog posts", "claude-opus-5", _used(fresh=20_000, out=4_000), day=date(2026, 9, 20))
    usage.record("Blog posts", "claude-opus-5", _used(fresh=1_000_000), day=date(2026, 9, 1))

    said = usage.report(usage.load(), today=DAY)

    assert "**Today: $0.55**" in said
    assert "**Last 7 days: $0.75**" in said
    assert "**Last 30 days: $5.75**" in said
    week = said.split("What the last 7 days went on:")[1]
    assert week.index("Responder drafts") < week.index("Blog posts"), "biggest first"
    assert "• Responder drafts: $0.55 — 1 call, ~$0.550 each (100,000 tokens read, 2,000 written)" in said
    assert "Counting since 2026-09-01" in said
    assert "Nothing counted yet" in usage.report({"days": {}})


def test_the_report_shows_what_the_cache_saved():
    usage.record("Responder drafts", "claude-opus-5", _used(fresh=1_000, read=9_000), day=DAY)
    assert "(10,000 tokens read, 90% from cache, 0 written)" in usage.report(usage.load(), today=DAY)


def draft_like_faith(call):
    return _think_then_reply(call)


def _think_then_reply(call):
    return call()


def test_a_call_is_filed_under_the_job_that_asked(monkeypatch):
    import sys

    monkeypatch.setitem(globals(), "__name__", "wilbyte.bot.jobs")
    try:
        assert draft_like_faith(lambda: usage.job_of(sys._getframe(1))) == "Responder drafts"
    finally:
        monkeypatch.undo()


def test_every_call_through_the_sdk_is_counted(monkeypatch):
    from anthropic.resources.messages import Messages

    answered = NS(model="claude-opus-5", usage=_used(fresh=1_000_000))
    monkeypatch.setattr(Messages, "create", lambda self, *a, **kw: answered)
    usage.install()
    monkeypatch.setattr(usage, "job_of", lambda frame: "Blog posts")

    assert Messages.create(None, model="claude-opus-5") is answered
    assert usage.load()["days"][date.today().isoformat()]["Blog posts"]["usd"] == pytest.approx(5.0)

    usage.install()
    Messages.create(None, model="claude-opus-5")
    assert usage.load()["days"][date.today().isoformat()]["Blog posts"]["calls"] == 2, "counted once"


def test_counting_never_breaks_the_call(monkeypatch):
    from anthropic.resources.messages import Messages

    answered = NS(model="claude-opus-5", usage=_used(fresh=1))
    monkeypatch.setattr(Messages, "create", lambda self, *a, **kw: answered)
    usage.install()
    monkeypatch.setattr(usage, "record", lambda *a, **kw: 1 / 0)

    assert Messages.create(None, model="claude-opus-5") is answered


def test_cost_is_asked_for_in_a_few_words():
    from wilbyte.bot import mentions

    for said in ("<@1> cost", "<@1> spending?", "<@1> claude usage", "<@1> Cost"):
        assert mentions.parse(said).action == "cost", said
    assert mentions.parse("<@1> cost of leads for David Pereira").action != "cost"
