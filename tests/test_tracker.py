"""The chargeback tracker: one row, laid out against its own columns."""

from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace

import pytest

from wilbyte import rebuttal
from wilbyte.bot import client as bot_client, jobs


JULIANA = rebuttal.read_facts("""Customer Name: Juliana Hernandez
Customer Email: hjuliana650@gmail.com
ARN: 72307626241809574244780
Card Number (Last 4): 7543
Transaction Date: 8/28/2026
Dispute Date: 9/12/2026
Dispute Amount: $ 129.37
Code: 37 - No Cardholder Authorization""")

FOUND = rebuttal.Gathered(
    product="25 Aged Final Expense — Texas",
    rebuttal_name="Juliana-Hernandez-Chargeback-Rebuttal.docx",
)

HEADS = [
    "Date Logged", "Client Name", "Email", "Amount", "ARN", "Card",
    "Transaction Date", "Dispute Date", "Reason Code", "Product", "Status",
    "Outcome", "Notes",
]


def _row(headings=None):
    return rebuttal.row_for_tracker(
        headings or HEADS, JULIANA, FOUND,
        when=date(2026, 9, 17),
    )


def test_each_column_gets_what_its_heading_asked_for():
    got = dict(zip(HEADS, _row()))

    assert got["Client Name"] == "Juliana Hernandez"
    assert got["Email"] == "hjuliana650@gmail.com"
    assert got["Amount"] == "$129.37"
    assert got["ARN"] == "72307626241809574244780"
    assert got["Card"] == "7543"
    assert got["Reason Code"] == "37 — No Cardholder Authorization"
    assert got["Product"] == "25 Aged Final Expense — Texas"
    assert got["Date Logged"] == "2026-09-17"


def test_a_column_ryte_does_not_know_is_left_blank():
    """The outcome is filled in weeks later when the bank decides. Guessing
    at it on the day the dispute lands writes over the answer."""
    got = dict(zip(HEADS, _row()))

    assert got["Outcome"] == ""
    assert got["Notes"] == ""


def test_the_row_is_as_long_as_the_sheet_is_wide():
    """A short row would shunt every later column one to the left."""
    assert len(_row()) == len(HEADS)
    assert len(_row(["Only", "Three", "Columns"])) == 3


def test_the_columns_can_be_in_any_order():
    backwards = list(reversed(HEADS))
    got = dict(zip(backwards, _row(backwards)))

    assert got["Client Name"] == "Juliana Hernandez"
    assert got["Amount"] == "$129.37"


@pytest.mark.parametrize(
    "heading, expected",
    [
        ("Customer", "Juliana Hernandez"),
        ("Cardholder Name", "Juliana Hernandez"),
        ("Agent", "Juliana Hernandez"),
        ("E-mail", "hjuliana650@gmail.com"),
        ("Disputed Amount", "$129.37"),
        ("Acquirer Reference", "72307626241809574244780"),
        ("Last 4", "7543"),
        ("Chargeback Date", "September 12, 2026"),
        ("Lead Type", "25 Aged Final Expense — Texas"),
        ("Stage", "Pending"),
    ],
)
def test_the_headings_are_matched_by_what_they_sound_like(heading, expected):
    """It is somebody's sheet, with their wording."""
    assert rebuttal.row_for_tracker(
        [heading], JULIANA, FOUND, when=date(2026, 9, 17),
    ) == [expected]


def test_an_unnamed_column_gets_nothing():
    assert rebuttal.row_for_tracker(
        ["", "  "], JULIANA, FOUND, when=date(2026, 9, 17),
    ) == ["", ""]


def test_the_row_is_shown_heading_by_heading_before_it_is_written():
    said = rebuttal.describe_row(HEADS, _row())

    assert "**Client Name** — Juliana Hernandez" in said
    assert "**Outcome** — _(blank)_" in said


# --------------------------------------------- the offer, with Sheets stubbed


class Sheet:
    def __init__(self, headings=None, blows_up=None):
        self.headings = HEADS if headings is None else headings
        self.blows_up, self.written = blows_up, []

    def tabs(self, sheet_id):
        return [{"properties": {"title": "Chargebacks"}}]

    def rows(self, sheet_id, span):
        if self.blows_up:
            raise self.blows_up
        return [list(self.headings)]

    def append(self, sheet_id, tab, rows):
        self.written.append((tab, rows))
        return "ok"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Button:
    press = True

    def __init__(self, **kw):
        self.confirmed = type(self).press
        self.answered = True
        self.label = kw.get("label", "")

    async def wait(self):
        return None


def _offered(monkeypatch, *, sheet_id="1BSU", press=True, sheet=None):
    from wilbyte import gsheets

    paper = sheet or Sheet()
    monkeypatch.setattr(gsheets, "SheetsClient", lambda creds, **kw: paper)
    monkeypatch.setattr(gsheets, "credentials", lambda secrets: None)

    class Pressed(Button):
        pass

    Pressed.press = press
    monkeypatch.setattr(bot_client.views, "ConfirmView", Pressed)
    monkeypatch.setattr(bot_client, "_today", lambda cfg: date(2026, 9, 17))

    said = []

    async def send(content=None, **kw):
        said.append(content or "")

    config = SimpleNamespace(
        secrets=SimpleNamespace(tracker_sheet_id=sheet_id),
        discord=SimpleNamespace(approval_timeout_seconds=1),
    )
    asyncio.run(bot_client._offer_the_tracker(
        SimpleNamespace(send=send, requester_id=1), config, JULIANA, FOUND,
    ))
    return paper, said


def test_pressing_it_writes_one_row(monkeypatch):
    paper, said = _offered(monkeypatch)

    assert len(paper.written) == 1
    tab, rows = paper.written[0]
    assert tab == "Chargebacks"
    assert rows[0][1] == "Juliana Hernandez"
    assert "Added to **Chargebacks**" in said[-1]


def test_nothing_is_written_without_the_press(monkeypatch):
    paper, _ = _offered(monkeypatch, press=False)

    assert paper.written == []


def test_the_row_is_shown_before_the_button(monkeypatch):
    _, said = _offered(monkeypatch, press=False)

    assert "One row for **Chargebacks**" in said[0]
    assert "Juliana Hernandez" in said[0]
    assert "_(blank)_" in said[0]


def test_no_tracker_configured_says_nothing_at_all(monkeypatch):
    """The rebuttal worked before there was one."""
    paper, said = _offered(monkeypatch, sheet_id="")

    assert (paper.written, said) == ([], [])


def test_a_tracker_that_cannot_be_read_is_said_rather_than_skipped(monkeypatch):
    paper, said = _offered(monkeypatch, sheet=Sheet(blows_up=RuntimeError("403")))

    assert paper.written == []
    assert "Couldn't read the tracker" in said[0]


def test_an_empty_first_row_is_said_rather_than_written_into(monkeypatch):
    paper, said = _offered(monkeypatch, sheet=Sheet(headings=[]))

    assert paper.written == []
    assert "first row is empty" in said[0]


def test_a_new_dispute_is_pending():
    """"new dispute is always pending" — and Status is a dropdown of Win,
    Pending and the other one, so anything else fails the sheet's own
    validation and lands in nobody's filter."""
    got = dict(zip(HEADS, _row()))

    assert got["Status"] == "Pending"
    assert rebuttal.NEW_DISPUTE == "Pending"


def test_the_outcome_is_still_not_guessed_at():
    """Pending is the status of a dispute in flight. Win and Lose are what
    somebody writes when the bank has decided."""
    got = dict(zip(HEADS, _row()))

    assert got["Outcome"] == ""


def test_a_column_called_closer_is_left_for_a_person():
    """Tarpley, Villegas — who closed the sale is not something RYTE knows
    from a dispute notice."""
    assert rebuttal.row_for_tracker(
        ["Closer"], JULIANA, FOUND, when=date(2026, 9, 17),
    ) == [""]
