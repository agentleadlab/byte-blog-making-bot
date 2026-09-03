"""Reading Payra's payment notifications, and who they belong to.

Checked against the real messages: Tony Moderno's, which has everything, and
Ryder Hamlin's, which came through with the product line blank.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from wilbyte import levinson

ET = "America/New_York"

TONY = """New Payment | Payra 💰

**NAME:** Tony Moderno
**EMAIL:** tony.moderno@tryeverlife.com
**PHONE:** +18016371314
**AMOUNT:** $1863
**PRODUCT:** Aged Leads (VET)"""

RYDER = """New Payment | Payra 💰

**NAME:** Ryder Hamlin
**EMAIL:** hamlinryder26@gmail.com
**PHONE:** +12178307069
**AMOUNT:** $1086.75
**PRODUCT:** """

WHEN = datetime(2026, 9, 3, 11, 26)


def test_a_payment_is_read_whole():
    paid = levinson.read_payment(TONY, paid_at=WHEN)

    assert paid.name == "Tony Moderno"
    assert paid.email == "tony.moderno@tryeverlife.com"
    assert paid.cents == 186300
    assert paid.product == "Aged Leads (VET)"
    assert paid.day == date(2026, 9, 3)


def test_a_blank_product_is_still_a_payment():
    """Ryder Hamlin's came through with the product line empty. That is a
    payment with a gap in it, not a payment that didn't happen."""
    paid = levinson.read_payment(RYDER, paid_at=WHEN)

    assert paid is not None
    assert paid.cents == 108675
    assert paid.product == ""


def test_the_money_keeps_its_cents():
    assert levinson.as_cents("$1,086.75") == 108675
    assert levinson.as_cents("$1863") == 186300
    assert levinson.as_cents("$0.05") == 5
    assert levinson.as_cents("nothing here") is None


def test_a_message_that_is_not_a_payment_is_not_read():
    assert levinson.read_payment("morning all", paid_at=WHEN) is None


def test_a_payment_with_no_amount_is_not_guessed_at():
    said = TONY.replace("**AMOUNT:** $1863", "**AMOUNT:**")
    assert levinson.read_payment(said, paid_at=WHEN) is None


@pytest.mark.parametrize(
    "typed,wanted",
    [
        ("+1 (801) 637-1314", "8016371314"),
        ("18016371314", "8016371314"),
        ("8016371314", "8016371314"),
        ("", ""),
    ],
)
def test_one_phone_written_four_ways_is_one_phone(typed, wanted):
    assert levinson.digits(typed) == wanted


# --------------------------------- whose payment is it


def members():
    return [
        levinson.Member(name="Tony Moderno", email="tony.moderno@tryeverlife.com"),
        levinson.Member(name="Dawson M Sullivan", email="sullivaninsgroup@gmail.com",
                        phone="18135166229"),
    ]


def test_a_payment_from_a_levinson_agent_is_theirs():
    (line,) = levinson.lines_for([levinson.read_payment(TONY, paid_at=WHEN)], members())

    assert line.name == "Tony Moderno"
    assert line.amount == "$1,863.00"
    assert line.matched_by == "email"


def test_a_payment_from_anybody_else_is_not():
    """Every payment lands in the same channel. Most of them are nothing to do
    with Levinson, and a report that claims them is a report that overpays."""
    assert levinson.lines_for([levinson.read_payment(RYDER, paid_at=WHEN)], members()) == []


def test_the_company_card_still_finds_the_agent():
    """Signed up on a personal address, paid on the company one. The phone is
    what says it is the same person."""
    said = TONY.replace("tony.moderno@tryeverlife.com", "billing@tryeverlife.com")
    said = said.replace("+18016371314", "+1 (813) 516-6229")

    (line,) = levinson.lines_for([levinson.read_payment(said, paid_at=WHEN)], members())

    assert line.name == "Dawson M Sullivan"
    assert line.matched_by == "phone"


def test_three_orders_in_a_month_are_three_lines():
    """"No matter how many times" - that is three times they spent money, and
    a report that folds them into one is a report that pays for one."""
    paid = [
        levinson.read_payment(TONY, paid_at=datetime(2026, 9, day, 12, 0))
        for day in (2, 11, 27)
    ]

    lines = levinson.lines_for(paid, members())

    assert [line.paid_on.day for line in lines] == [2, 11, 27]
    assert levinson.total(lines) == "$5,589.00"


def test_a_name_that_looks_alike_is_never_matched():
    """There are two Antonio Nortons on the opt-in sheet. Handing an agency
    money for somebody they never sent us is the failure this avoids."""
    said = TONY.replace("tony.moderno@tryeverlife.com", "someone.else@gmail.com")
    said = said.replace("+18016371314", "+15550001111")

    assert levinson.lines_for([levinson.read_payment(said, paid_at=WHEN)], members()) == []


# --------------------------------- which month was asked for


TODAY = date(2026, 9, 3)


@pytest.mark.parametrize(
    "said,wanted",
    [
        ("", (2026, 9)),
        ("this month", (2026, 9)),
        ("last month", (2026, 8)),
        ("august", (2026, 8)),
        ("aug", (2026, 8)),
        ("September", (2026, 9)),
        # Not yet been this year, so it means the one that has.
        ("december", (2025, 12)),
        ("december 2026", (2026, 12)),
        ("08/2026", (2026, 8)),
    ],
)
def test_the_month_somebody_asked_for(said, wanted):
    assert levinson.month_named(said, today=TODAY) == wanted


def test_january_asks_backwards_for_last_month():
    assert levinson.month_named("last month", today=date(2026, 1, 9)) == (2025, 12)


def test_a_word_that_only_looks_like_a_month_is_not_one():
    assert levinson.month_named("augment the report", today=TODAY) is None


def test_the_tab_is_named_the_way_it_gets_sent_on():
    assert levinson.tab_for(2026, 9) == "September 2026"


def test_only_that_months_payments_are_counted():
    paid = [
        levinson.read_payment(TONY, paid_at=datetime(2026, 8, 31, 23, 0)),
        levinson.read_payment(TONY, paid_at=datetime(2026, 9, 1, 0, 30)),
    ]

    assert [one.day.month for one in levinson.in_month(paid, 2026, 9)] == [9]
