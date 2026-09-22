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
    assert got["Date Logged"] == "9/17/2026"


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
        ("Chargeback Date", "9/12/2026"),
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
    """The real tracker's shape: a tab per month."""

    MONTHS = ["Aug 2026", "Sept 2026", "Oct 2026", "Nov 2026", "Dec 2026"]

    def __init__(self, headings=None, blows_up=None, titles=None):
        self.headings = HEADS if headings is None else headings
        self.titles = list(self.MONTHS) if titles is None else list(titles)
        self.blows_up, self.written, self.asked = blows_up, [], []
        self.restyled, self.copied, self.cleared = [], [], []

    def tabs(self, sheet_id):
        # `tabs()` hands back the properties already — reaching into them
        # again is what produced 'Sheet1'!1:1.
        return [{"title": one, "sheetId": i} for i, one in enumerate(self.titles)]

    def rows(self, sheet_id, span):
        self.asked.append(span)
        if self.blows_up:
            raise self.blows_up
        return [list(self.headings)]

    def put(self, sheet_id, span, rows):
        self.written.append((span, rows))
        return span

    def match_row_above(self, sheet_id, tab_id, row, wide):
        self.restyled.append((tab_id, row, wide))

    def copy_tab(self, sheet_id, from_id, title):
        if title in self.titles:
            return None
        self.copied.append((from_id, title))
        self.titles.append(title)
        return 900 + len(self.titles)

    def clear(self, sheet_id, span):
        self.cleared.append(span)

    def append(self, sheet_id, tab, rows):  # pragma: no cover - must not run
        raise AssertionError(
            "append lets Google pick the table, and the month tab has two"
        )

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
        schedule=SimpleNamespace(timezone="America/Chicago"),
    )
    monkeypatch.setattr(jobs, "board_day", lambda cfg: date(2026, 9, 17))
    asyncio.run(bot_client._offer_the_tracker(
        SimpleNamespace(send=send, requester_id=1), config, JULIANA, FOUND,
    ))
    return paper, said


def test_pressing_it_writes_one_row(monkeypatch):
    paper, said = _offered(monkeypatch)

    assert len(paper.written) == 1
    span, rows = paper.written[0]
    assert rows[0][1] == "Juliana Hernandez"
    assert "Added to **Sept 2026, row 2**" in said[-1]


def test_the_row_goes_in_the_lists_own_columns(monkeypatch):
    """Google's append was handed the tab and chose the deductions-by-closer
    summary, writing Juliana Hernandez underneath it in that table's
    columns."""
    paper, _ = _offered(monkeypatch)

    span, _rows = paper.written[0]

    assert span == "'Sept 2026'!A2:M2"


def test_the_free_row_is_read_before_it_is_written_to(monkeypatch):
    """`put` overwrites. Where it writes is read first, every time."""
    paper, _ = _offered(monkeypatch)

    assert "'Sept 2026'!A:M" in paper.asked


def test_nothing_is_written_without_the_press(monkeypatch):
    paper, _ = _offered(monkeypatch, press=False)

    assert paper.written == []


def test_the_row_is_shown_before_the_button(monkeypatch):
    _, said = _offered(monkeypatch, press=False)

    assert "One row for **Sept 2026**" in said[0]
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


# ------------------------ checking it without running a whole chargeback


def _checked(monkeypatch, *, sheet_id="1BSU", sheet=None):
    from wilbyte import gsheets

    paper = sheet or Sheet()
    monkeypatch.setattr(gsheets, "SheetsClient", lambda creds, **kw: paper)
    monkeypatch.setattr(gsheets, "credentials", lambda secrets: None)
    monkeypatch.setattr(jobs, "board_day", lambda cfg: date(2026, 9, 17))
    return paper, jobs._check_tracker(SimpleNamespace(
        secrets=SimpleNamespace(tracker_sheet_id=sheet_id),
        schedule=SimpleNamespace(timezone="America/Chicago"),
    ))


def test_the_check_names_the_tab_and_the_columns(monkeypatch):
    _, ((ok, said),) = _checked(monkeypatch)

    assert ok is True
    assert "Sept 2026" in said
    assert "13 column(s)" in said


def test_it_says_which_columns_are_left_for_a_person(monkeypatch):
    """Seeing that before a dispute lands beats finding it during one."""
    _, ((_, said),) = _checked(monkeypatch)

    assert "left for you" in said
    assert "Outcome" in said.split("left for you")[1]
    assert "Notes" in said.split("left for you")[1]


def test_the_ones_ryte_fills_are_named_too(monkeypatch):
    _, ((_, said),) = _checked(monkeypatch)

    filled = said.split("RYTE fills")[1].split("· left for you")[0]
    assert "Client Name" in filled
    assert "Amount" in filled
    assert "Outcome" not in filled


def test_a_recognised_column_counts_even_when_this_dispute_leaves_it_blank():
    """The real tracker's "Date of Transaction" was reported as a column RYTE
    could not fill, because the check asked a made-up dispute that had no
    transaction date. It sent somebody looking for a bug that was not there."""
    knows, leaves = rebuttal.columns_known(
        ["Name of Disputer", "Amount", "Status", "Date of Transaction", "Closer"]
    )

    assert knows == ["Name of Disputer", "Amount", "Status", "Date of Transaction"]
    assert leaves == ["Closer"]


def test_an_unnamed_column_is_left_alone_and_named_as_such():
    knows, leaves = rebuttal.columns_known(["Amount", "", "   "])

    assert knows == ["Amount"]
    assert leaves == ["(unnamed)", "(unnamed)"]


def test_a_section_title_in_the_heading_row_is_not_a_column_to_fill():
    """"Sep 2026 chargebacks — deductions by closer" is somebody's heading,
    not a field."""
    knows, leaves = rebuttal.columns_known(
        ["Amount", "Sep 2026 chargebacks — deductions by closer"]
    )

    assert knows == ["Amount"]
    assert len(leaves) == 1


def test_not_configured_is_neither_pass_nor_fail(monkeypatch):
    _, ((ok, said),) = _checked(monkeypatch, sheet_id="")

    assert ok is None
    assert "not configured" in said


def test_a_tracker_it_cannot_read_fails_loudly(monkeypatch):
    _, ((ok, said),) = _checked(monkeypatch, sheet=Sheet(blows_up=RuntimeError("403")))

    assert ok is False
    assert "Chargeback tracker" in said


def test_checking_writes_nothing(monkeypatch):
    paper, _ = _checked(monkeypatch)

    assert paper.written == []


# --------------------------------- one tab a month, and the right one

# Aug 2026, Sept 2026, Oct 2026, Nov 2026, Dec 2026. Writing every chargeback
# into whichever tab came first would pile the year into August.


@pytest.mark.parametrize(
    "when, tab",
    [
        (date(2026, 9, 17), "Sept 2026"),
        (date(2026, 8, 1), "Aug 2026"),
        (date(2026, 12, 31), "Dec 2026"),
    ],
)
def test_the_row_goes_in_its_own_month(when, tab):
    assert jobs._which_tab(Sheet.MONTHS, when) == (tab, "")


def test_sept_and_sep_and_september_are_one_month():
    for said in ("Sep 2026", "Sept 2026", "September 2026", "SEPT 2026"):
        assert rebuttal.monthly_tab([said], date(2026, 9, 17)) == said


def test_the_year_has_to_agree():
    assert rebuttal.monthly_tab(["Sept 2025"], date(2026, 9, 17)) == ""


def test_a_month_with_no_tab_is_not_guessed_at():
    """Filed under the wrong month is worse than not filed — not filed gets
    noticed."""
    tab, trouble = jobs._which_tab(Sheet.MONTHS, date(2027, 1, 5))

    assert tab == ""
    assert "no tab for Jan 2027" in trouble
    assert "Aug 2026" in trouble, "it should say what is there"


def test_a_sheet_not_kept_by_month_uses_its_first_tab():
    assert jobs._which_tab(["Chargebacks", "Notes"], date(2026, 9, 17)) == (
        "Chargebacks", ""
    )


def test_no_tabs_at_all_is_said():
    assert jobs._which_tab([], date(2026, 9, 17))[1] == "The tracker has no tabs."


def test_the_headings_are_read_off_that_month_s_tab(monkeypatch):
    paper, _ = _offered(monkeypatch, press=False)

    assert paper.asked == ["'Sept 2026'!1:1"]  # nothing written, nothing else read


def test_a_missing_month_is_made_by_copying_the_newest(monkeypatch):
    """"once its 2027 ryte will make them." The tracker runs out of tabs, and
    on the first of the month after the last one a chargeback has nowhere to
    go."""
    paper, said = _offered(
        monkeypatch, sheet=Sheet(titles=["Aug 2026", "Jul 2026"]),
    )

    assert paper.copied == [(0, "Sep 2026")], "copied the wrong month, or none"
    assert "Sep 2026" in paper.titles
    assert any("Made **Sep 2026**" in one for one in said)
    assert any("copying **Aug 2026**" in one for one in said)


def test_the_new_months_list_is_emptied_and_nothing_else_is(monkeypatch):
    """The month tab has a second table beside it, and what belongs in
    January's copy of that is the sheet owner's business."""
    paper, _said = _offered(
        monkeypatch, sheet=Sheet(titles=["Aug 2026"]),
    )

    assert paper.cleared == [f"'Sep 2026'!A2:{chr(ord('A') + len(HEADS) - 1)}"]


def test_a_tracker_with_no_tabs_at_all_is_still_refused(monkeypatch):
    """Nothing to copy, and a tab invented out of nothing is a tab that
    matches no other sheet anybody keeps."""
    paper, said = _offered(monkeypatch, sheet=Sheet(titles=[]))

    assert paper.copied == [] and paper.written == []
    assert "no tabs" in said[0]


def test_a_month_that_turned_up_while_we_were_looking_is_not_made_twice(
    monkeypatch
):
    paper = Sheet(titles=["Aug 2026"])
    real = paper.copy_tab

    def once(sheet_id, from_id, title):
        paper.titles.append(title)      # somebody else got there first
        return real(sheet_id, from_id, title)

    paper.copy_tab = once
    _paper, said = _offered(monkeypatch, sheet=paper)

    assert paper.titles.count("Sep 2026") == 1
    assert not any("Made **Sep 2026**" in one for one in said)


# ------------------------------------- the row has to look like the sheet's own


def test_dates_are_written_so_the_sheet_stores_them_as_dates():
    """"August 28, 2026" typed into a cell stays a string: it sorts
    alphabetically, no date filter sees it, and it sits in a column of real
    dates in a different shape from every one of them."""
    got = dict(zip(HEADS, _row()))

    assert got["Transaction Date"] == "8/28/2026"
    assert got["Dispute Date"] == "9/12/2026"
    assert got["Date Logged"] == "9/17/2026"


def test_a_date_that_cannot_be_read_is_kept_as_it_was_written():
    """Losing a date RYTE cannot parse is worse than writing it as text."""
    assert rebuttal.sheet_date("sometime in August") == "sometime in August"
    assert rebuttal.sheet_date("") == ""


def test_the_written_row_is_given_the_look_of_the_one_above(monkeypatch):
    """The sheet is kept in Arial 12. A row in Google's default Calibri 11
    reads as an outsider's row before anybody reads what is in it."""
    paper, _ = _offered(monkeypatch)

    assert paper.restyled == [(1, 2, 13)]


def test_the_heading_rows_clothes_are_never_copied_down():
    """The row above row 2 is the heading. A dispute in bold blue is the older
    bug `restyle` was written for."""
    from wilbyte import gsheets

    asked = []

    class Client(gsheets.SheetsClient):
        def __init__(self):
            pass

        def _request(self, method, path, **kwargs):
            asked.append(path)
            return {}

    Client().match_row_above("sid", 3, 2, 5)

    assert asked == []

    Client().match_row_above("sid", 3, 3, 5)

    assert len(asked) == 1


def test_the_newest_month_is_the_one_copied():
    """A sheet's oldest tab is last year's shape, and whatever was learned
    since is on the newest one."""
    assert rebuttal.latest_month(Sheet.MONTHS) == "Dec 2026"
    assert rebuttal.latest_month(["Dec 2026", "Jan 2027"]) == "Jan 2027"
    assert rebuttal.latest_month(["Notes", "Summary"]) == ""


def test_a_tab_with_no_year_on_it_cannot_be_the_newest():
    """"September" could be any year, and copying it could mean copying a tab
    three years old."""
    assert rebuttal.month_of("September") is None
    assert rebuttal.latest_month(["September", "Aug 2026"]) == "Aug 2026"


def test_the_new_tab_is_named_the_way_the_sheet_names_them():
    """"Sept 2026" rather than "Sep 2026" when that is how the sheet says it.
    A tab that matches none of the eleven beside it looks like somebody else
    made it — which somebody else did."""
    assert rebuttal.month_tab_name(date(2027, 9, 1), Sheet.MONTHS) == "Sept 2027"
    assert rebuttal.month_tab_name(date(2027, 1, 1), Sheet.MONTHS) == "Jan 2027"


def test_a_sheet_with_no_habit_to_copy_gets_the_plain_name():
    assert rebuttal.month_tab_name(date(2027, 1, 1), []) == "Jan 2027"


def test_month_tabs_with_no_year_on_them_are_not_copied_from(monkeypatch):
    """"September" could be any year. Copying it to make a new month could
    mean copying a tab three years old, with three-year-old columns."""
    paper, said = _offered(
        monkeypatch, sheet=Sheet(titles=["July", "August"]),
    )

    assert paper.copied == [], "copied a tab that could be from any year"
    assert paper.written == []
    assert "no tab for Sep 2026" in said[0]
