"""Unticked New Agent cards kept on top of Done."""

from __future__ import annotations

from types import SimpleNamespace as NS

from wilbyte.bot import jobs


class Board:
    def __init__(self, cards, *, lists=None):
        self.cards, self.moves = list(cards), []
        self.lists = lists if lists is not None else [{"id": "L-done", "name": "Done"}]

    def board_lists(self, board_id):
        return self.lists

    def list_cards(self, list_id):
        assert list_id == "L-done"
        return list(self.cards)

    def move_card(self, card_id, list_id, *, position="top"):
        self.moves.append((card_id, list_id, position))
        card = next(one for one in self.cards if one["id"] == card_id)
        self.cards.remove(card)
        self.cards.insert(0, card)

    def close(self):
        pass


def card(name, ticked):
    return {"id": name, "name": f"NEW AGENT- {name}", "dueComplete": ticked}


def _floated(monkeypatch, cards, **kw):
    board = Board(cards, **kw)
    monkeypatch.setattr(jobs, "open_trello", lambda config: board)
    moved, problems = jobs.float_unticked(NS(secrets=NS(trello_board_id="b")))
    return board, moved, problems


def _order(board):
    return [one["id"] for one in board.cards]


def test_unticked_cards_under_ticked_ones_go_to_the_top(monkeypatch):
    """"i want ryte to always move unticked new agent card above / on top"."""
    board, moved, problems = _floated(monkeypatch, [
        card("Wesley", False), card("Ramsey", True), card("Cayson", True),
        card("Jareel", False), card("Old", True), card("Abdullah", False),
    ])

    assert problems == []
    assert moved == ["NEW AGENT- Jareel", "NEW AGENT- Abdullah"]
    # the ones floated keep their order, above the one already on top
    assert _order(board) == ["Jareel", "Abdullah", "Wesley", "Ramsey", "Cayson", "Old"]
    assert all(where == "L-done" and pos == "top" for _, where, pos in board.moves)


def test_nothing_moves_when_done_is_already_in_order(monkeypatch):
    board, moved, _ = _floated(monkeypatch, [
        card("Wesley", False), card("Abdullah", False), card("Ramsey", True), card("Old", True),
    ])

    assert moved == [] and board.moves == []


def test_only_new_agent_cards_float(monkeypatch):
    board, moved, _ = _floated(monkeypatch, [
        card("Ramsey", True),
        {"id": "aged", "name": "AGED LEADS - Doughe", "dueComplete": False},
        card("Jareel", False),
    ])

    assert moved == ["NEW AGENT- Jareel"]
    assert _order(board) == ["Jareel", "Ramsey", "aged"]


def test_a_ticked_card_left_on_top_is_passed_by_the_unticked_below_it(monkeypatch):
    """Somebody ticks the top card; it stays put, and the next pass floats the
    unticked ones under it past it."""
    board, moved, _ = _floated(monkeypatch, [
        card("JustTicked", True), card("Wesley", False), card("Abdullah", False),
    ])

    assert _order(board) == ["Wesley", "Abdullah", "JustTicked"]


def test_a_backlog_is_worked_through_a_pass_at_a_time(monkeypatch):
    monkeypatch.setattr(jobs, "FLOAT_AT_MOST", 2)
    cards = [card("Ticked", True)] + [card(f"A{at}", False) for at in range(5)]

    board, moved, _ = _floated(monkeypatch, cards)

    assert len(board.moves) == 2 and moved == ["NEW AGENT- A0", "NEW AGENT- A1"]


def test_a_board_without_done_says_so(monkeypatch):
    board, moved, problems = _floated(monkeypatch, [], lists=[{"id": "x", "name": "In Que"}])

    assert moved == [] and "Done" in problems[0]


def test_floating_an_old_order_to_the_top_does_not_make_it_the_newest_order(monkeypatch):
    """Moving touches a card. The RingCentral lookup picks a repeat client's
    card by when it was made, so the old order floated to the top of Done
    doesn't pass itself off as the current one."""
    from wilbyte import agents

    old_id = "5f000000" + "0" * 16      # made in 2020
    new_id = "68000000" + "0" * 16      # made in 2025
    cards = [
        {"id": old_id, "name": "NEW AGENT - Adrian Pacheco", "idList": "L-done",
         "dateLastActivity": "2026-09-24T12:00", "desc": "Phone: 312-555-0188\n10 OTP VETS"},
        {"id": new_id, "name": "NEW AGENT - Adrian Pacheco", "idList": "L-done",
         "dateLastActivity": "2026-09-20T12:00", "desc": "Phone: 312-555-0188\n25 OTP Trucker IUL"},
    ]

    class Cards:
        def board_cards(self, board_id, *, archived=False):
            return cards

        def board_lists(self, board_id):
            return [{"id": "L-done", "name": "Done"}]

        def card_comments(self, card_id):
            return []

        def close(self):
            pass

    monkeypatch.setattr(jobs, "open_trello", lambda config: Cards())

    found = jobs.agent_on_the_board(NS(secrets=NS(trello_board_id="b")), "3125550188")

    assert agents.made_at(new_id) > agents.made_at(old_id)
    assert "Trucker" in found["desc"]
