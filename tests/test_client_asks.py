"""What the client asked for on their Trello card since paying is held to -
Connor Swartz's "Can we take PA off my fresh pack", put on his card by Ryte
itself, and Ryte then had Nova put PA back."""

from datetime import datetime, timezone
from types import SimpleNamespace

from wilbyte import rookqc, rytevoice

CONNOR = {"when": datetime(2026, 11, 9, 14, 27, tzinfo=timezone.utc), "card": "https://trello.com/c/x",
          "text": "@nic0l3 Swartzy: @Kharyl Can we take PA off my fresh pack. My license has not tied to carriers "
                  "yet\n\n— via Ryte, asked by Kharyl on Discord"}


def _result(problems, hub_states):
    return SimpleNamespace(problems=list(problems), notes=[], hub=SimpleNamespace(states=tuple(hub_states)))


def test_a_state_the_client_asked_off_isnt_missing():
    result = _result(["The hub states don't match the payment - missing PA"],
                     ["AZ", "FL", "HI", "MA", "MD", "NC", "NM", "SC", "TX", "VA"])
    rookqc.apply_client_asks(result, [CONNOR])
    assert result.problems == []
    assert any(one.startswith("PA off - the client asked (Nov 9, Trello)") for one in result.notes)
    assert result.client_asks and "take PA off" in result.client_asks[0]


def test_put_back_against_what_they_asked_is_the_problem():
    # Nova "added PA to Connor's Trucker IUL order".
    result = _result([], ["AZ", "FL", "PA", "TX"])
    rookqc.apply_client_asks(result, [CONNOR])
    assert result.problems == ["The client asked to take PA off (Nov 9, Trello) - the hub still has PA"]


def test_only_what_they_asked_comes_off_the_problem():
    result = _result(["The hub states don't match the payment - has WA not ordered - missing PA, OH"], ["WA"])
    rookqc.apply_client_asks(result, [CONNOR])
    assert result.problems == ["The hub states don't match the payment - has WA not ordered - missing OH"]


def test_words_that_look_like_states_arent():
    off, on = rookqc.client_state_changes([{"when": CONNOR["when"], "text": "hi ok can you remove me from the "
                                                                                 "texts or pause it"}])
    assert off == {} and on == {}


def test_the_newest_word_on_a_state_wins():
    later = {"when": datetime(2026, 11, 10, tzinfo=timezone.utc), "text": "actually add PA back please"}
    off, on = rookqc.client_state_changes([CONNOR, later])
    assert "PA" in on and "PA" not in off


def test_ryte_s_question_sees_what_the_client_asked():
    result = SimpleNamespace(client="Connor Swartz", problems=[], todo=[], notes=[], hub=None, setup_text="",
                             client_asks=["Nov 9: Can we take PA off my fresh pack"])
    assert rytevoice.facts(result)["client_asked_since_paying_on_trello"] == ["Nov 9: Can we take PA off my fresh pack"]
    assert "never tell anyone to undo it" in rytevoice._WHO
