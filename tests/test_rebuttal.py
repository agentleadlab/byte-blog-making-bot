"""A chargeback rebuttal, from the dispute facts and what the board remembers.

The facts here are Jose Zambrano's and Skip Scott's real disputes.
"""

from __future__ import annotations

from datetime import date

import pytest

from wilbyte import rebuttal

JOSE = """MID: 510200014664

DBA Name: AGENT LEAD LAB

Dispute Date: 9/8/2026

Dispute Type: Debited

Dispute Dollar Amount: $1,552.50

Acquirer's Reference Number: 24556406167808942703416

Card Number: ending in 2610

Transaction Date: 6/15/2026

Customer Name: Jose Zambrano

Customer Email: josezagent@gmail.com
"""


# ------------------------------------------------------------- reading it


def test_the_whole_block_is_read():
    one = rebuttal.read_facts(JOSE)

    assert one.mid == "510200014664"
    assert one.dba == "AGENT LEAD LAB"
    assert one.dispute_type == "Debited"
    assert one.arn == "24556406167808942703416"
    assert one.card == "ending in 2610"
    assert one.customer_name == "Jose Zambrano"
    assert one.customer_email == "josezagent@gmail.com"


def test_the_amount_is_normalised():
    assert rebuttal.read_facts(JOSE).amount == "$1,552.50"


@pytest.mark.parametrize(
    "said, expected",
    [("6/15/2026", date(2026, 6, 15)), ("06/15/26", date(2026, 6, 15)),
     ("2026-06-15", date(2026, 6, 15)), ("not a date", None), ("", None)],
)
def test_a_date_is_read_however_it_was_typed(said, expected):
    assert rebuttal.as_date(said) == expected


def test_a_label_he_does_not_know_is_ignored_rather_than_guessed_at():
    """A field RYTE invented is worse than one left blank."""
    one = rebuttal.read_facts("Something Else: 12345\nCustomer Name: Jose Zambrano")

    assert one.customer_name == "Jose Zambrano"
    assert one.mid == ""


def test_the_first_label_that_fits_wins():
    """"Customer Email" and "Email" both match the email field; the block is
    read top down and the more specific one is written first."""
    one = rebuttal.read_facts(
        "Customer Email: jose@example.com\nEmail: someone-else@example.com"
    )

    assert one.customer_email == "jose@example.com"


def test_a_block_missing_what_matters_says_which():
    one = rebuttal.read_facts("MID: 510200014664\nDBA Name: AGENT LEAD LAB")

    assert one.missing() == [
        "Customer Name", "Dispute Dollar Amount", "Transaction Date",
    ]


def test_a_full_block_is_missing_nothing():
    assert rebuttal.read_facts(JOSE).missing() == []


def test_the_name_can_come_off_the_command_line_instead():
    assert rebuttal.named_in("rebuttal Jose Zambrano") == "Jose Zambrano"
    assert rebuttal.named_in("chargeback Skip Scott\nMID: 1") == "Skip Scott"


# ----------------------------------------------------------- how long they waited


def test_the_gap_between_paying_and_disputing():
    """85 days. Somebody who received nothing does not wait three months."""
    assert rebuttal.read_facts(JOSE).days_waited() == 85


def test_the_gap_is_said_out_loud():
    said = rebuttal.waited_line(rebuttal.read_facts(JOSE))

    assert "85 days" in said
    assert "6/15/2026" in said and "9/8/2026" in said


def test_a_dispute_filed_the_same_week_makes_no_such_argument():
    one = rebuttal.read_facts(
        "Customer Name: A\nDispute Dollar Amount: $1\n"
        "Transaction Date: 6/15/2026\nDispute Date: 6/18/2026"
    )

    assert one.days_waited() == 3
    assert rebuttal.waited_line(one) == ""


def test_no_dispute_date_means_no_gap_and_no_line():
    one = rebuttal.read_facts(
        "Customer Name: A\nDispute Dollar Amount: $1\nTransaction Date: 6/15/2026"
    )

    assert one.days_waited() is None
    assert rebuttal.waited_line(one) == ""


# ------------------------------------------------------------------ the header


def test_a_blank_field_is_left_out_of_the_fact_table():
    rows = rebuttal.header(rebuttal.read_facts(
        "Customer Name: Jose Zambrano\nDispute Dollar Amount: $1,552.50\n"
        "Transaction Date: 6/15/2026"
    ))
    labels = [label for label, _value in rows]

    assert "Acquirer Reference Number (ARN)" not in labels
    assert "Dispute Dollar Amount" in labels


def test_the_email_rides_with_the_name():
    (label, value) = rebuttal.header(rebuttal.read_facts(JOSE))[0]

    assert label == "Cardholder"
    assert "Jose Zambrano" in value and "josezagent@gmail.com" in value


# ------------------------------------------------------------------- exhibits


def test_a_file_knows_what_sort_of_file_it_is():
    assert rebuttal.Exhibit("texts.PNG").is_image() is True
    assert rebuttal.Exhibit("contract.pdf").is_pdf() is True
    assert rebuttal.Exhibit("notes.txt").is_image() is False


def test_every_exhibit_kind_has_a_proof_to_sit_under():
    for kind in rebuttal.EXHIBITS:
        assert kind == "other" or kind in rebuttal.UNDER


# --------------------------------------------------------------- what is missing


def test_a_proof_with_nothing_behind_it_is_named_not_written_around():
    """A gap named is a gap somebody fills. A gap written around reaches the
    acquirer."""
    holes = rebuttal.what_is_missing(rebuttal.Gathered(), [])

    assert any("PandaDoc" in one for one in holes)
    assert any("invoice" in one.casefold() for one in holes)
    assert any("texts" in one.casefold() for one in holes)


def test_an_attached_contract_closes_that_hole():
    holes = rebuttal.what_is_missing(
        rebuttal.Gathered(), [rebuttal.Exhibit("c.pdf", kind="contract")]
    )

    assert not any("PandaDoc" in one for one in holes)


def test_the_holes_the_gathering_found_are_carried_through():
    found = rebuttal.Gathered(holes=["No sheet link on their card"])

    assert "No sheet link on their card" in rebuttal.what_is_missing(found, [])


# -------------------------------------------------------------- what it says


def test_the_writing_prompt_carries_only_what_was_gathered():
    """Nothing is invented, so nothing that was not found is described."""
    found = rebuttal.Gathered(delivery="Faith: your launch date is Monday")

    asked = rebuttal.writing_prompt(rebuttal.read_facts(JOSE), found, [])

    assert "delivery" in asked
    assert "### sheet" not in asked
    assert "never infer" in asked.casefold() or "never state" in asked.casefold()


def test_the_demand_names_the_amount():
    assert "$1,552.50" in rebuttal.demand(rebuttal.read_facts(JOSE))


def test_the_demand_still_reads_without_one():
    assert "the disputed amount" in rebuttal.demand(rebuttal.Dispute())
