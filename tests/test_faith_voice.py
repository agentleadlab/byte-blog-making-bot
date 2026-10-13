""""@Ryte does this sound like faith?" with a reply under it - it went to the
ad writer and came back as three ad options."""

import asyncio
from types import SimpleNamespace as NS

import pytest

from wilbyte import smsreplies
from wilbyte.bot import client, jobs, mentions

FRANKLIN = (
    "<@1> does this sound like faith?\n\n"
    "Hi Jean, I actually just checked that you paid through bank, so you can't actually go live "
    "tomorrow until it's settled. If you still want to go live tomorrow, you can use card instead "
    "or Zelle if you want to avoid the additional charges"
)


def test_franklins_message_is_a_voice_check_not_an_ad():
    got = mentions.parse(FRANKLIN)
    assert got.action == "faithvoice"
    assert got.brief.startswith("Hi Jean, I actually just checked") and got.brief.endswith("additional charges")


@pytest.mark.parametrize("said, draft", [
    ("would faith say this? Hey Jean", "Hey Jean"),
    ("is this how faith would say it: Hey Jean", "Hey Jean"),
    ("how would faith say this - Hey Jean", "Hey Jean"),
    ("make this sound like faith: Hey Jean", "Hey Jean"),
    ("rewrite this in faith's voice: Hey Jean", "Hey Jean"),
    ("Does it sound like Faith?", ""),
])
def test_the_ways_it_is_asked(said, draft):
    got = mentions.parse("<@1> " + said)
    assert (got.action, got.brief) == ("faithvoice", draft)


@pytest.mark.parametrize("said", [
    "what does faith send when agents ask where to submit a sale?",
    "how does faith answer refund requests",
])
def test_asking_how_she_handles_something_is_still_that(said):
    assert mentions.parse("<@1> " + said).action == "askfaith"


class Claude:
    asked: list = []
    said: dict = {}

    def __init__(self, api_key=None):
        self.messages = self

    def create(self, **kw):
        type(self).asked.append(kw)
        return NS(stop_reason="tool_use", content=[NS(type="tool_use", name="voice_check", input=type(self).said)])


def _config(monkeypatch, said):
    import anthropic

    Claude.asked, Claude.said = [], said
    monkeypatch.setattr(anthropic, "Anthropic", Claude)
    return NS(secrets=NS(anthropic_api_key="k", require=lambda *a: None), copy=NS(model="claude-test"))


DONE = [
    smsreplies.Exchange(agent="1", name="", asked="can I pay with ach", at="2026-10-01",
                        answered="Hey! ACH takes 3 business days to settle before we launch 😊"),
    smsreplies.Exchange(agent="2", name="", asked="when do I go live", at="2026-10-02",
                        answered="Hey love! You're set to go live tomorrow 🙌"),
]


def test_her_replies_are_the_yardstick_and_the_rewrite_comes_back(monkeypatch):
    config = _config(monkeypatch, {
        "verdict": "close", "differences": ["She opens with Hey, not Hi"],
        "rewrite": "Hey Jean! Since you paid by bank it has to settle first 😊", "facts": [],
    })
    got = jobs.faith_voice_check(config, "Hi Jean, you paid through bank", DONE,
                                 playbook="Warm and short.", learned=["Never promise a date"])
    assert got["verdict"] == "close" and got["rewrite"].startswith("Hey Jean!")
    prompt = Claude.asked[0]["messages"][0]["content"]
    assert "ACH takes 3 business days" in prompt and "Hi Jean, you paid through bank" in prompt
    assert "Warm and short." in prompt and "Never promise a date" in prompt
    assert Claude.asked[0]["tool_choice"] == {"type": "tool", "name": "voice_check"}


def test_nothing_to_check_or_nothing_to_compare_with_is_said(monkeypatch):
    config = _config(monkeypatch, {})
    with pytest.raises(ValueError, match="Put the reply under the question"):
        jobs.faith_voice_check(config, "  ", DONE)
    with pytest.raises(ValueError, match="none of Faith's replies"):
        jobs.faith_voice_check(config, "Hi Jean", [])


def test_what_franklin_sees():
    said = client.faith_voice_said({
        "verdict": "close", "differences": ["She opens with Hey"],
        "facts": ["She tells agents ACH takes 3 business days"],
        "rewrite": "Hey Jean! ```x```", "examples": 6,
    }, "Hi Jean")
    assert said.startswith("🟡 Close")
    assert "• She opens with Hey" in said and "⚠ Her replies say otherwise: She tells agents ACH" in said
    assert "**As Faith would send it:**\n```\nHey Jean! '''x'''\n```" in said
    assert said.endswith("Nothing was sent.") and len(said) <= 1995
    same = client.faith_voice_said({"verdict": "yes", "differences": [], "facts": [],
                                    "rewrite": "Hi Jean", "examples": 3}, "Hi Jean")
    assert "As Faith would send it" not in same and same.startswith("✅ Yes")


class Heard:
    requester_id = 1

    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kw):
        self.sent.append(content)


def test_asked_as_a_reply_to_the_draft_checks_that_message(monkeypatch):
    from wilbyte import ringcentral, ringtexts

    checked = []
    monkeypatch.setattr(ringcentral, "configured", lambda secrets: False)
    monkeypatch.setattr(ringtexts, "load", lambda: {"playbook": {"text": "p"}, "lessons": []})
    monkeypatch.setattr(jobs, "ring_texts", lambda config, data: [])
    monkeypatch.setattr(smsreplies, "exchanges", lambda texts: DONE)
    monkeypatch.setattr(jobs, "faith_voice_check", lambda config, draft, done, **kw: checked.append(
        (draft, kw["playbook"])) or {"verdict": "yes", "differences": [], "facts": [],
                                     "rewrite": draft, "examples": 2})
    heard = Heard()
    message = NS(reference=NS(resolved=NS(content="Hey Jean! It has to settle first")))
    asyncio.run(client._faith_voice(heard, NS(secrets=NS()), message, ""))
    assert checked == [("Hey Jean! It has to settle first", "p")]
    assert heard.sent[-1].startswith("✅ Yes")

    asyncio.run(client._faith_voice(heard, NS(secrets=NS()), NS(reference=None), ""))
    assert heard.sent[-1].startswith("Put the reply under the question")
