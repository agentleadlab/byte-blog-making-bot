"""Proof of delivery - "ordered 25, delivered 27 by Oct 2"."""

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from wilbyte import delivery
from wilbyte.bot import jobs

TODAY = date(2026, 10, 5)


@pytest.mark.parametrize("phrase, many", [
    ("25 Text Verified Veteran Leads", 25),
    ("30 more OTP vets", 30),
    ("Fresh Veterans: 35", 35),
    ("OTP FEX: 20 leads", 20),
    ("12 Trucker IUL leads", 12),
    ("Uprise vets $350/week", None),
    ("$350/week standard", None),
    ("350/week standard vets", None),
    ("3 states only", None),
    ("Text Verified Spanish IUL", None),
    ("", None),
])
def test_how_many_reads_only_a_count_of_leads(phrase, many):
    """A guessed number turns into a short sheet that isn't."""
    assert delivery.how_many(phrase) is many or delivery.how_many(phrase) == many


def _sheet(*dates, heading="Date Received"):
    rows = [["Name", "Phone", heading]]
    rows += [[f"Lead {n}", "555-0100", when] for n, when in enumerate(dates)]
    return rows


def test_a_sheet_with_a_date_column_is_counted_by_day():
    counted = delivery.count_rows(_sheet("09/28/2026", "09/28/2026", "10/02/2026 3:14 PM"))
    assert counted.rows == 3
    assert counted.by_day == {date(2026, 9, 28): 2, date(2026, 10, 2): 1}
    assert counted.last == date(2026, 10, 2)
    assert counted.since(date(2026, 9, 29)) == 1


def test_a_sheet_without_dates_is_just_its_rows():
    counted = delivery.count_rows([["Name", "Phone"], ["A", "1"], ["", ""], ["B", "2"]])
    assert counted.rows == 2 and counted.by_day == {} and counted.since(TODAY) is None


def test_a_column_headed_date_that_holds_no_dates_isnt_one():
    counted = delivery.count_rows(_sheet("called back", "voicemail", heading="Date notes"))
    assert counted.by_day == {}


def test_a_notes_column_with_the_odd_date_in_it_isnt_one():
    """Read as one, a full order would count as the one row that had a date."""
    counted = delivery.count_rows(_sheet("called back", "voicemail", "10/01/2026", "no answer",
                                         heading="Date / notes"))
    assert counted.by_day == {} and counted.since(date(2026, 9, 1)) is None


def _order(many, dates, *, launch=date(2026, 9, 26), ordered=None):
    return delivery.Delivery(
        agent="Jay Rodriguez", card_id="c1", card_url="https://trello.com/c/1",
        ordered=ordered if ordered is not None else f"{many} OTP Vets", many=many,
        launch=launch, sheet="https://docs.google.com/spreadsheets/d/S/edit",
        counted=delivery.count_rows(_sheet(*dates)),
    )


def test_only_this_orders_rows_count_where_the_sheet_dates_them():
    """A sheet carried on from the last order holds that order's leads too."""
    one = _order(3, ["08/01/2026", "08/02/2026", "09/27/2026", "09/28/2026"])
    assert one.delivered == 2 and one.short_by() == 1


def test_short_and_due_is_a_warning():
    one = _order(25, ["09/28/2026"] * 18)
    line = delivery.describe(one, today=TODAY)
    assert line.startswith("⚠ **Jay Rodriguez**")
    assert "ordered **25**" in line and "delivered **18** by Sep 28" in line
    assert "**7 short**" in line and "live Sep 26 (9 days ago)" in line


def test_short_in_the_first_week_is_leads_still_arriving():
    one = _order(25, ["10/02/2026"] * 10, launch=date(2026, 10, 1))
    line = delivery.describe(one, today=TODAY)
    assert line.startswith("⏳") and "still within the first week" in line
    assert not one.due(TODAY)


def test_all_in_is_a_tick_and_the_dispute_line_says_more_than_ordered():
    one = _order(25, ["09/28/2026"] * 25 + ["10/02/2026"] * 2)
    assert delivery.describe(one, today=TODAY).startswith("✅")
    assert delivery.for_a_dispute(one) == "Ordered 25, delivered 27 leads by 10/02/2026 (2 more than ordered)."


def test_no_count_on_the_card_is_never_called_short():
    one = _order(None, ["09/28/2026"] * 3, ordered="Uprise vets $350/week")
    assert one.short_by() == 0
    assert "no count on the card" in delivery.describe(one, today=TODAY)
    assert delivery.for_a_dispute(one) == "Delivered 3 leads by 09/28/2026."


# ------------------------------------------------------------- off the board


def _card_made(when, title, desc, card_id_tail="0" * 16):
    return {"id": f"{int(when.timestamp()):08x}" + card_id_tail, "name": title, "desc": desc,
            "shortUrl": f"https://trello.com/c/{title[-3:]}"}


SHEET_NEW = "https://docs.google.com/spreadsheets/d/1NEWNEWNEWNEWNEWNEWNEW/edit"
SHEET_OLD = "https://docs.google.com/spreadsheets/d/1OLDOLDOLDOLDOLDOLDOLD/edit"


class Board:
    def __init__(self, cards, comments):
        self.cards, self.comments = cards, comments

    def board_cards(self, board_id, archived=False):
        return self.cards

    def card_comments(self, card_id):
        return self.comments.get(card_id, [])

    def close(self):
        pass


def _config():
    return SimpleNamespace(secrets=SimpleNamespace(trello_board_id="b"),
                           schedule=SimpleNamespace(timezone="America/New_York"))


def _board(monkeypatch, cards, comments, sheets):
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: Board(cards, comments))
    monkeypatch.setattr(jobs, "board_day", lambda cfg: TODAY)
    read = []

    def count(config, link):
        read.append(link)
        return delivery.count_rows(sheets[link]), ""

    monkeypatch.setattr(jobs, "_count_sheet", count)
    return read


def test_delivered_for_reads_the_newest_sheet_on_the_card(monkeypatch):
    """Trello hands comments over newest first; a redone setup's new sheet
    is the one the leads are going into."""
    card = _card_made(datetime(2026, 9, 20, tzinfo=timezone.utc), "New Agent - Jay Rodriguez",
                      "25 Text Verified Veteran Leads\nLaunch Date: Saturday, September 26")
    read = _board(monkeypatch, [card], {card["id"]: [
        f"Updated previous setup.\nSheet link: {SHEET_NEW}", f"Sheet link: {SHEET_OLD}",
    ]}, {SHEET_NEW: _sheet(*["09/28/2026"] * 25), SHEET_OLD: _sheet()})

    orders, problems = jobs.delivered_for(_config(), "Jay Rodriguez")
    assert problems == [] and read == [SHEET_NEW]
    assert orders[0].many == 25 and orders[0].delivered == 25
    assert orders[0].launch == date(2026, 9, 26)


def test_a_reorder_with_no_link_uses_the_sheet_on_their_earlier_card(monkeypatch):
    first = _card_made(datetime(2026, 8, 1, tzinfo=timezone.utc), "New Agent - Jay Rodriguez",
                       "10 OTP Vets\nLaunch Date: Monday, August 3")
    again = _card_made(datetime(2026, 9, 20, tzinfo=timezone.utc), "AGED LEAD - Jay Rodriguez",
                       "20 Aged Vets\nLaunch Date: Saturday, September 26", "1" * 16)
    _board(monkeypatch, [first, again], {first["id"]: [f"Sheet link: {SHEET_OLD}"]},
           {SHEET_OLD: _sheet(*(["08/04/2026"] * 10 + ["09/27/2026"] * 12))})

    orders, _problems = jobs.delivered_for(_config(), "Jay Rodriguez")
    newest = orders[0]
    assert newest.card_id == again["id"] and newest.sheet == SHEET_OLD
    assert newest.many == 20 and newest.delivered == 12, "the first order's rows were counted"
    assert newest.short_by() == 8


def test_two_people_with_one_name_are_not_guessed_between(monkeypatch):
    one = _card_made(datetime(2026, 9, 1, tzinfo=timezone.utc), "New Agent - Maria Lopez", "Phone: 555-010-0001")
    two = _card_made(datetime(2026, 9, 2, tzinfo=timezone.utc), "New Agent - Maria Lopez",
                     "Phone: 555-010-0002", "1" * 16)
    _board(monkeypatch, [one, two], {}, {})
    orders, problems = jobs.delivered_for(_config(), "Maria Lopez")
    assert orders == [] and "not guessing" in problems[0]


def test_the_daily_check_is_only_orders_a_week_to_three_weeks_live_and_short(monkeypatch):
    def card(name, launch_said, made, tail):
        return _card_made(made, f"New Agent - {name}", f"25 OTP Vets\nLaunch Date: {launch_said}", tail)

    short = card("Jay Rodriguez", "Saturday, September 26", datetime(2026, 9, 20, tzinfo=timezone.utc), "1" * 16)
    full = card("Ana Ruiz", "Saturday, September 26", datetime(2026, 9, 20, tzinfo=timezone.utc), "2" * 16)
    fresh = card("Bo Diaz", "Thursday, October 1", datetime(2026, 9, 28, tzinfo=timezone.utc), "3" * 16)
    sheet = {one["id"]: f"https://docs.google.com/spreadsheets/d/1{n}{'x' * 20}/edit"
             for n, one in enumerate([short, full, fresh])}
    read = _board(
        monkeypatch, [short, full, fresh],
        {cid: [f"Sheet link: {link}"] for cid, link in sheet.items()},
        {sheet[short["id"]]: _sheet(*["09/28/2026"] * 18),
         sheet[full["id"]]: _sheet(*["09/28/2026"] * 25),
         sheet[fresh["id"]]: _sheet(*["10/02/2026"] * 3)},
    )

    found, problems = jobs.delivery_check(_config(), today=TODAY)
    assert problems == []
    assert [one.agent for one in found] == ["Jay Rodriguez"]
    assert sheet[fresh["id"]] not in read, "an order in its first week was opened"


def test_the_clear_outs_sheet_is_the_newest_on_the_card(monkeypatch):
    """The bug found on the way: the last link of a newest-first list was
    the oldest sheet."""
    card = _card_made(datetime(2026, 9, 20, tzinfo=timezone.utc), "New Agent - Jay Rodriguez", "")
    _board(monkeypatch, [card], {card["id"]: [
        f"Updated previous setup.\nSheet link: {SHEET_NEW}", f"Sheet link: {SHEET_OLD}",
    ]}, {})
    link, problems = jobs.sheet_for_agent(_config(), "Jay Rodriguez")
    assert problems == [] and link == SHEET_NEW


def test_the_command_lists_their_orders_with_the_dispute_line(monkeypatch):
    import asyncio

    from wilbyte.bot import client

    monkeypatch.setattr(jobs, "board_day", lambda cfg: TODAY)
    monkeypatch.setattr(jobs, "delivered_for", lambda config, who: (
        [_order(25, ["09/28/2026"] * 25 + ["10/02/2026"] * 2)], []))
    said = []

    class Heard:
        async def send(self, text=None, **kw):
            said.append(text)

    asyncio.run(client._delivered(Heard(), _config(), "Jay Rodriguez"))
    assert "Proof of delivery" in said[0]
    assert "For a dispute: Ordered 25, delivered 27 leads by 10/02/2026" in said[0]


def test_the_command_with_no_name_says_who_is_short(monkeypatch):
    import asyncio

    from wilbyte.bot import client

    monkeypatch.setattr(jobs, "board_day", lambda cfg: TODAY)
    monkeypatch.setattr(jobs, "delivery_check", lambda config, today: ([_order(25, ["09/28/2026"] * 18)], []))
    said = []

    class Heard:
        async def send(self, text=None, **kw):
            said.append(text)

    asyncio.run(client._delivered(Heard(), _config(), ""))
    assert "Short" in said[0] and "7 short" in said[0]


def test_the_command_is_heard():
    from wilbyte.bot import mentions

    for text, who in [("<@1> delivered Jay Rodriguez", "Jay Rodriguez"), ("<@1> delivered", ""),
                      ("<@1> proof of delivery for Jay Rodriguez?", "Jay Rodriguez")]:
        asked = mentions.parse(text)
        assert asked.action == "delivered" and asked.brief == who


def test_each_short_order_is_said_once():
    delivery.remember({"c1": 7})
    assert delivery.flagged()["c1"] == 7
    delivery.remember({"c2": 3})
    assert set(delivery.flagged()) == {"c1", "c2"}


def test_a_sheet_with_a_colour_key_above_counts_from_its_heading_row():
    """Tavin Dougher's sheet: a black bar, a banner, the colour key, the banner
    again, then "Name | Email | Phone Number" on row 10 and two test leads.
    Counting from row 1 made eleven lines that aren't leads into leads."""
    banner = "When YOU MAKE A SALE HERE IS THE LINK TO DISPO THE LEAD ---- LINK ---- https://x"
    rows = [
        [""],
        [banner],
        ["Green", "Sold"], ["Purple", "Call Disconnected"], ["Blue", "Call Blocker"],
        ["Yellow", "Booked Appointment"], ["Red", "Not Interested"], ["Teal", "Contact Attempted"],
        [banner],
        ["Name", "Email", "Phone Number", "Age", "State", "Branch of Service"],
        ["Test Lead", "test@agentleadlab.com", "214-555-0100", "45", "Texas", "TEST"],
        ["Test Lead", "test@agentleadlab.com", "214-555-0100", "45", "Alabama", "TEST"],
        ["Leroy Example", "leroy@example.com", "555-010-0001", "65", "Texas", "Army"],
        ["Danny Example", "danny@example.com", "555-010-0002", "83", "Indiana", "Navy"],
        ["Marion Example", "marion@example.com", "555-010-0003", "75", "Georgia", "Army"],
    ]
    assert delivery.count_rows(rows).rows == 3
