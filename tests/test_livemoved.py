""""people changes their live date sometimes even they got move to done"."""

from datetime import date, datetime, timezone
from types import SimpleNamespace

from wilbyte import livemoved
from wilbyte.bot import jobs

TODAY = date(2026, 10, 6)


def _id(when, tail):
    return f"{int(when.timestamp()):08x}" + tail * 16


SETUP = {"id": _id(datetime(2026, 10, 5, tzinfo=timezone.utc), "0"),
         "name": "Agent Setup Going Live Wednesday 10/08"}
ERIC = {"id": _id(datetime(2026, 10, 4, tzinfo=timezone.utc), "1"), "name": "New Agent - Eric Karas",
        "url": "https://trello.com/c/eric", "shortUrl": "https://trello.com/c/eric",
        "desc": "Launch Date: Wednesday, October 8", "dateLastActivity": "2026-10-06T18:10:00Z"}
ANA = {"id": _id(datetime(2026, 10, 4, tzinfo=timezone.utc), "2"), "name": "New Agent - Ana Ruiz",
       "url": "https://trello.com/c/ana", "shortUrl": "https://trello.com/c/ana",
       "desc": "Launch Date: Wednesday, October 8", "dateLastActivity": "2026-10-05T10:00:00Z"}

TRE = {"text": "@nic0l3 he is to go live same day", "author": "Tre Tarpley", "when": "2026-10-06T18:10:00Z"}


class Board:
    def __init__(self, notes):
        self.notes, self.read = notes, []

    def board_cards(self, board_id, archived=False):
        return [SETUP, ERIC, ANA]

    def card_checklists(self, card_id):
        assert card_id == SETUP["id"]
        return [{"name": "Nicole", "checkItems": [
            {"name": f"[New Agent - Eric Karas]({ERIC['url']}) 20 vets"},
            {"name": f"[New Agent - Ana Ruiz]({ANA['url']}) 25 OTP VETS"},
        ]}]

    def card_notes(self, card_id):
        self.read.append(card_id)
        return self.notes.get(card_id, [])

    def close(self):
        pass


def _check(monkeypatch, board):
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)
    config = SimpleNamespace(secrets=SimpleNamespace(trello_board_id="b"),
                             schedule=SimpleNamespace(timezone="America/New_York"))
    return jobs.live_dates_moved(config, today=TODAY)


def test_tres_same_day_after_filing_is_caught(monkeypatch):
    found, problems = _check(monkeypatch, Board({ERIC["id"]: [TRE]}))

    assert problems == [] and len(found) == 1
    one = found[0]
    assert (one.agent, one.live, one.who, one.checklist) == ("Eric Karas", date(2026, 10, 6), "Tre Tarpley", "Nicole")
    said = livemoved.describe(found)
    assert "on **Agent Setup Going Live Wednesday 10/08** (Nicole's list), but now goes live **Tue Oct 06**" in said
    assert "Tre Tarpley, Oct 6 2:10 PM: “@nic0l3 he is to go live same day”" in said


def test_an_agent_whose_day_still_matches_is_left_alone(monkeypatch):
    found, _problems = _check(monkeypatch, Board({}))
    assert found == []


def test_the_newest_comment_wins_over_an_older_one_and_the_description():
    older = {"text": "go live Friday", "author": "Faith", "when": "2026-10-03T15:00:00Z"}
    launch, note = livemoved.current_launch([TRE, older], "Launch Date: Wednesday, October 8",
                                            made=date(2026, 10, 2), timezone="America/New_York")
    assert launch == date(2026, 10, 6) and note is TRE


def test_a_comment_that_names_no_day_doesnt_count():
    chat = {"text": "sheet link sent, all good", "author": "Nicole", "when": "2026-10-06T18:00:00Z"}
    launch, note = livemoved.current_launch([chat], "Launch Date: Wednesday, October 8",
                                            made=date(2026, 10, 2), timezone="America/New_York")
    assert launch == date(2026, 10, 8) and note is None


def test_a_card_nobody_touched_isnt_opened_again(monkeypatch):
    board = Board({ERIC["id"]: [TRE]})
    _check(monkeypatch, board)
    first = list(board.read)
    _check(monkeypatch, board)
    assert board.read == first + [], "read the same untouched cards twice"


def test_the_command_is_heard():
    from wilbyte.bot import mentions

    for text in ("<@1> live changes", "<@1> live date changes?", "<@1> dates moved"):
        assert mentions.parse(text).action == "livemoved"


# ------------------------------------------------ "yes add the button to move it"

NEW_SETUP = {"id": _id(datetime(2026, 10, 5, 12, tzinfo=timezone.utc), "3"),
             "name": "Agent Setup Going Live Tuesday 10/06"}
OLD_ORDER = {"id": "lo8", "name": "Lead Order 10/08/26"}
NEW_ORDER = {"id": "lo6", "name": "Lead Order 10/06/26"}
LINE = f"[New Agent - Eric Karas]({ERIC['url']}) 20 vets"


class MovingBoard:
    def __init__(self, *, new_setup=True, nicole_there=True, new_order=True, spread=True):
        self.cards = [SETUP, ERIC, OLD_ORDER] + ([NEW_SETUP] if new_setup else []) + ([NEW_ORDER] if new_order else [])
        self.lists = {
            SETUP["id"]: [{"id": "old-nicole", "name": "Nicole", "checkItems": [
                {"id": "i1", "name": LINE, "state": "complete"}]}],
            NEW_SETUP["id"]: [{"id": "new-nicole" if nicole_there else "new-kath",
                               "name": "Nicole" if nicole_there else "Kathleen", "checkItems": []}],
            "lo8": [{"id": "old-vets", "name": "OTP VET Plus", "checkItems": (
                [{"id": "o1", "name": f"{ERIC['url']} 20 vets", "state": "incomplete"}] if spread else [])}],
            "lo6": [{"id": "new-vets", "name": "OTP VET Plus", "checkItems": []}],
        }
        self.added, self.removed = [], []

    def board_cards(self, board_id, archived=False):
        return self.cards

    def card_checklists(self, card_id):
        return self.lists.get(card_id, [])

    def add_check_item(self, checklist_id, name, *, checked=False):
        self.added.append((checklist_id, name, checked))
        return {}

    def remove_check_item(self, checklist_id, item_id):
        self.removed.append((checklist_id, item_id))
        return {}

    def close(self):
        pass


def _eric_moved():
    return livemoved.Moved(
        agent="Eric Karas", card_url=ERIC["url"], setup_card=SETUP["name"], checklist="Nicole",
        live=date(2026, 10, 6), setup_card_id=SETUP["id"], checklist_id="old-nicole",
        item_id="i1", item_name=LINE, ticked=True,
    )


def _move(monkeypatch, board):
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)
    return jobs.move_live_line(SimpleNamespace(secrets=SimpleNamespace(trello_board_id="b")), _eric_moved())


def test_the_button_moves_the_setup_line_and_the_lead_order_line(monkeypatch):
    board = MovingBoard()
    done, problems = _move(monkeypatch, board)

    assert problems == []
    assert board.added == [("new-nicole", LINE, True), ("new-vets", f"{ERIC['url']} 20 vets", False)]
    assert board.removed == [("old-nicole", "i1"), ("old-vets", "o1")]
    assert "Agent Setup Going Live Tuesday 10/06" in done[0] and "Lead Order 10/06/26" in done[1]


def test_not_spread_yet_moves_just_the_setup_line(monkeypatch):
    board = MovingBoard(spread=False)
    done, problems = _move(monkeypatch, board)
    assert problems == [] and len(done) == 1 and board.removed == [("old-nicole", "i1")]


def test_no_setup_card_for_the_new_day_moves_nothing(monkeypatch):
    board = MovingBoard(new_setup=False)
    done, problems = _move(monkeypatch, board)
    assert done == [] and board.added == [] and board.removed == []
    assert "no setup card for Tue Oct 06" in problems[0]


def test_no_list_of_hers_on_the_new_card_moves_nothing(monkeypatch):
    """Never made up - Nicole's line doesn't go on Kathleen's list."""
    board = MovingBoard(nicole_there=False)
    done, problems = _move(monkeypatch, board)
    assert board.added == [] and board.removed == [] and "no Nicole list" in problems[0]


def test_already_moved_by_somebody_is_left(monkeypatch):
    board = MovingBoard()
    board.lists[SETUP["id"]][0]["checkItems"] = []
    done, problems = _move(monkeypatch, board)
    assert board.added == [] and "moved them already" in problems[0]


def test_no_lead_order_card_for_the_new_day_leaves_that_line_and_says_so(monkeypatch):
    board = MovingBoard(new_order=False)
    done, problems = _move(monkeypatch, board)
    assert board.removed == [("old-nicole", "i1")]
    assert "No Lead Order card for Tue Oct 06" in problems[0]


def test_each_change_comes_with_its_own_button(monkeypatch):
    import asyncio

    from wilbyte.bot import client

    labels, said = [], []

    class Button:
        def __init__(self, **kw):
            labels.append(kw["label"])
            self.confirmed = True

        async def wait(self):
            return None

    class Heard:
        async def send(self, text=None, **kw):
            said.append(text)

    monkeypatch.setattr(client.views, "ConfirmView", Button)
    monkeypatch.setattr(jobs, "move_live_line", lambda config, one: (["✅ Setup: moved"], []))
    config = SimpleNamespace(discord=SimpleNamespace(approval_timeout_seconds=1))
    asyncio.run(client._offer_to_move(Heard(), config, _eric_moved()))
    assert labels == ["Move to Tue Oct 06"]
    assert "Eric Karas" in said[0] and said[-1] == "✅ Setup: moved"


def test_only_their_line_moves_never_somebody_elses(monkeypatch):
    board = MovingBoard()
    board.lists["lo8"][0]["checkItems"].append(
        {"id": "o2", "name": "https://trello.com/c/somebodyelse 25 OTP VETS", "state": "incomplete"})
    _move(monkeypatch, board)
    assert ("old-vets", "o2") not in board.removed
    assert all("somebodyelse" not in name for _where, name, _ticked in board.added)
