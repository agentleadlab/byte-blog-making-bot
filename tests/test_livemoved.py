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
