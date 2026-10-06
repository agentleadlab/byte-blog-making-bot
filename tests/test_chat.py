"""RYTE talking back - "sassy and witty", and only from what he knows."""

from datetime import date
from types import SimpleNamespace

from wilbyte import chat


def _built(**kw):
    defaults = dict(who="Franklin", today=date(2026, 10, 6), history=[], sops=[], commands="@RYTE board")
    return chat.build("who's going live today?", **{**defaults, **kw})


def test_he_is_told_to_be_sassy_and_to_never_make_things_up():
    system, _messages = _built()
    assert "sassy, witty" in system
    assert "Never make up numbers, names, dates" in system
    assert "Never say you did" in system
    assert "@RYTE board" in system


def test_he_hears_the_question_the_conversation_and_the_sops():
    _system, messages = _built(
        history=["Therese: is Eric set up?", "Nicole: not yet"],
        sops=[{"title": "SOP How to Create Internal Ads LeadForm", "summary": "Steps in Ads Manager",
               "url": "https://notion.so/x"}],
    )
    said = messages[0]["content"]
    assert "Tuesday, October 6, 2026" in said
    assert "Therese: is Eric set up?\nNicole: not yet" in said
    assert "Internal Ads LeadForm: Steps in Ads Manager https://notion.so/x" in said
    assert said.endswith("Franklin says to you: who's going live today?")


def test_a_bare_ping_is_still_something_to_answer():
    _system, messages = chat.build("", who="Kath", today=date(2026, 10, 6), history=[], sops=[], commands="")
    assert "just pinged you and said nothing" in messages[0]["content"]


def test_without_a_key_he_says_so_in_character():
    config = SimpleNamespace(secrets=SimpleNamespace(anthropic_api_key=None))
    said = chat.answer(config, "hi", who="", today=date(2026, 10, 6), history=[], sops=[], commands="")
    assert "ANTHROPIC_API_KEY" in said and "`@RYTE commands`" in said


def test_a_failed_call_is_never_silence(monkeypatch):
    import anthropic

    class Broken:
        def __init__(self, **kw):
            self.messages = self

        def create(self, **kw):
            raise RuntimeError("overloaded")

    monkeypatch.setattr(anthropic, "Anthropic", Broken)
    config = SimpleNamespace(secrets=SimpleNamespace(anthropic_api_key="k"), copy=SimpleNamespace(model="m"))
    said = chat.answer(config, "hi", who="", today=date(2026, 10, 6), history=[], sops=[], commands="")
    assert "buffered" in said


def test_the_answer_is_what_claude_wrote(monkeypatch):
    import anthropic

    seen = {}

    class Claude:
        def __init__(self, **kw):
            self.messages = self

        def create(self, **kw):
            seen.update(kw)
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="Awake? Darling, I never sleep.")])

    monkeypatch.setattr(anthropic, "Anthropic", Claude)
    config = SimpleNamespace(secrets=SimpleNamespace(anthropic_api_key="k"), copy=SimpleNamespace(model="m"))
    said = chat.answer(config, "you awake?", who="Franklin", today=date(2026, 10, 6), history=[], sops=[],
                       commands="")
    assert said == "Awake? Darling, I never sleep."
    assert seen["max_tokens"] == 500 and "sassy" in seen["system"]
