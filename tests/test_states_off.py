""""just have ryte not check the states anymore" - Kevin's changed states
insisted on, and his leads to be replaced."""

from types import SimpleNamespace

from wilbyte import agedqc, rookqc, rytevoice


def test_ryte_runs_with_states_off():
    import importlib

    fresh = importlib.reload(rookqc)
    try:
        assert fresh.CHECK_STATES is False
    finally:
        importlib.reload(rookqc)


def test_states_off_flags_nothing(monkeypatch):
    monkeypatch.setattr(rookqc, "CHECK_STATES", False)
    result = SimpleNamespace(problems=[], notes=[], ok=[])
    paid = SimpleNamespace(states=["CA", "TX", "PA"], left_out=[])
    rookqc._compare_states(result, "The hub", ["CA"], [], paid)
    rookqc._compare_states(result, "Rook's setup", ["NY"], [], paid)
    assert result.problems == [] and result.notes == [rookqc.STATES_OFF_NOTE]


def test_a_clients_state_ask_isnt_held_to_either(monkeypatch):
    from datetime import datetime, timezone

    monkeypatch.setattr(rookqc, "CHECK_STATES", False)
    result = SimpleNamespace(problems=[], notes=[], hub=SimpleNamespace(states=("PA",)))
    rookqc.apply_client_asks(result, [{"when": datetime(2026, 11, 9, tzinfo=timezone.utc),
                                        "text": "Can we take PA off my fresh pack"}])
    assert result.problems == [] and result.client_asks


def test_aged_pulls_arent_held_to_states_either(monkeypatch):
    monkeypatch.setattr(rookqc, "CHECK_STATES", False)
    assert agedqc._states_checked() is False


def test_ryte_never_asks_about_states():
    assert "Never raise, question or ask anyone to change a client's STATES" in rytevoice._WHO
