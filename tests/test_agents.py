"""New agents, from the form that made the card to the day they go live.

The description is written by a landing-page form and read by nobody in a
hurry. Everything here is checked against the real one - Gustin Elrod's card,
verbatim - because a lead type read wrong sends somebody's leads to the wrong
place and nothing about the board looks different afterwards.
"""

from __future__ import annotations

from datetime import date

import pytest

from wilbyte import agents

TUESDAY = date(2026, 8, 25)

REAL = """-- New Client Onboarded --

First Name: Gustin
Last Name: Elrod
Phone: +12103220810
Email: gustin@elrodfinancial.com
Package Selected: Text Verified
Lead Type: Text Verified IUL Plus
Target Areas for Marketing: States: Alabama, Arkansas, AZ, CA, CT, Florida,
Georgia, Iowa, Idaho, Illinois, Indiana, Kansas, Louisiana, Maryland, Michigan,
Mississippi, NC, New Mexico, Nevada, Ohio, Oregon, PA, SC, SD, TN, Texas,
Utah, Virginia, Washington, Wisconsin

Order 25 Leads
Gustin Elrod paid for OTP IUL leads and with Don A setup for immediate launch.
Launch date is today, Tuesday, August 25

He also has a CRM so lets integrate that
"""


def card(name="New Agent - Gustin Elrod", **extra):
    return {"id": "c1", "name": name, "shortUrl": "https://trello.com/c/AbCd1234", **extra}


# --------------------------------------------- telling an agent card apart


@pytest.mark.parametrize(
    "title",
    ["New Agent - Gustin Elrod", "NEW AGENT- Jeffrey Boyd", "New Agent - Everlife"],
)
def test_the_cards_the_form_makes_are_recognised(title):
    assert agents.is_agent_card(title) is True


@pytest.mark.parametrize(
    "title",
    ["💎 General 08/25/26", "Lead Order 08/26/26", "Agent Setup Going Live Wednesday 08/26",
     "Hyros - UTM Code", "New Agent Onboarding SOP"],
)
def test_everything_else_in_in_que_is_left_alone(title):
    """In Que holds the daily four as well, and they are not agents."""
    assert agents.is_agent_card(title) is False


@pytest.mark.parametrize(
    "title,name",
    [
        ("New Agent - Gustin Elrod", "Gustin Elrod"),
        ("NEW AGENT- Jeffrey Boyd", "Jeffrey Boyd"),
        ("New Agent  -  Waldt Family Insurance", "Waldt Family Insurance"),
    ],
)
def test_the_name_comes_off_the_front_of_the_title(title, name):
    assert agents.agent_name(title) == name


# ------------------------------------------------------- reading the card


def test_the_lead_type_line_is_the_lead_type():
    assert agents.find_lead_type(REAL) == "Text Verified IUL Plus"


def test_the_body_mentioning_leads_is_not_the_lead_type():
    """"paid for OTP IUL leads" is prose. The labelled line is the field."""
    assert "paid" not in agents.find_lead_type(REAL)
    assert agents.find_lead_type("Gustin paid for OTP IUL leads") == ""


def test_a_card_with_no_lead_type_line_says_so():
    assert agents.find_lead_type("First Name: Gustin\nPhone: +1") == ""


def test_the_launch_date_is_read_out_of_the_sentence_that_says_it():
    assert agents.find_launch(REAL, today=TUESDAY) == TUESDAY


def test_a_written_date_wins_over_the_word_today():
    """They agree when the card is read the day it was written, and disagree
    when it isn't. The calendar date is the one that stays true."""
    said = "Launch date is today, Tuesday, August 25"

    assert agents.find_launch(said, today=date(2026, 8, 27)) == date(2026, 8, 25)


@pytest.mark.parametrize(
    "said,when",
    [
        ("Launch date is Thursday, August 27", date(2026, 8, 27)),
        ("launching 8/27", date(2026, 8, 27)),
        ("Launch date: 08/27/26", date(2026, 8, 27)),
        ("launch date is tomorrow", date(2026, 8, 26)),
        ("setup for immediate launch", TUESDAY),
        ("Launching Thursday", date(2026, 8, 27)),
    ],
)
def test_the_ways_people_write_a_launch_date(said, when):
    assert agents.find_launch(said, today=TUESDAY) == when


def test_a_date_elsewhere_in_the_description_is_not_the_launch():
    """The states list, the order line, a note about a call next month - the
    launch is the one in the sentence that says launch."""
    said = "Ordered 25 leads on 08/20/26.\nLaunch date is Thursday, August 27.\nCall 9/1"

    assert agents.find_launch(said, today=TUESDAY) == date(2026, 8, 27)


def test_no_launch_sentence_means_no_date():
    assert agents.find_launch("First Name: Gustin\nOrder 25 Leads", today=TUESDAY) is None


# ------------------------------------ which leads these are, however written


@pytest.mark.parametrize(
    "written,shape",
    [
        ("Text Verified IUL Plus", ("iul", "plus", frozenset())),
        ("OTP IUL Plus", ("iul", "plus", frozenset())),
        ("OTP VET Plus", ("vet", "plus", frozenset())),
        ("Standard MTG", ("mtg", "standard", frozenset())),
        ("Basic Mortgage", ("mtg", "standard", frozenset())),
        ("OTP Spanish IUL", ("iul", "plus", frozenset({"spanish"}))),
        ("OTP Blue Collar IUL", ("iul", "plus", frozenset({"blue collar"}))),
        ("Final Expense", ("fex", None, frozenset())),
        ("OTP Widows", ("widows", "plus", frozenset())),
    ],
)
def test_a_lead_type_reduces_to_what_the_leads_actually_are(written, shape):
    """OTP and text-verified are one thing said two ways, so both reduce the
    same. Standard and basic are the other tier."""
    assert agents.shape_of(written) == shape


def test_text_verified_finds_the_otp_checklist():
    """The card says one, the board says the other, and they are the same."""
    existing = [
        "OTP VET Plus", "OTP FEX", "OTP Blue Collar IUL", "OTP IUL Plus",
        "OTP Spanish IUL", "OTP MTG Standard", "OTP Widows", "own setup",
    ]

    assert agents.match_checklist("Text Verified IUL Plus", existing) == "OTP IUL Plus"


def test_a_variant_does_not_match_the_plain_one():
    """OTP Spanish IUL is a different checklist, and filing into the wrong one
    sends somebody's leads to the wrong place."""
    existing = ["OTP IUL Plus", "OTP Spanish IUL", "OTP Blue Collar IUL"]

    assert agents.match_checklist("Spanish IUL", existing) == "OTP Spanish IUL"
    assert agents.match_checklist("Blue Collar IUL", existing) == "OTP Blue Collar IUL"
    assert agents.match_checklist("Text Verified IUL Plus", existing) == "OTP IUL Plus"


def test_standard_and_plus_are_not_each_other():
    existing = ["OTP MTG Standard", "OTP MTG Plus"]

    assert agents.match_checklist("Basic MTG", existing) == "OTP MTG Standard"
    assert agents.match_checklist("Text Verified MTG Plus", existing) == "OTP MTG Plus"


def test_nothing_matching_is_none_rather_than_the_nearest_thing():
    """The caller makes a new checklist. Guessing is how leads go astray."""
    assert agents.match_checklist("OTP Widows", ["OTP IUL Plus", "OTP FEX"]) is None


def test_a_lead_type_that_names_no_leads_matches_nothing():
    assert agents.match_checklist("Premium Package", ["OTP IUL Plus"]) is None


# ------------------------------------------------- what goes on a checklist


def test_the_line_is_the_link_then_the_lead_type():
    assert agents.checklist_item(
        "https://trello.com/c/AbCd1234", "Text Verified IUL Plus"
    ) == "https://trello.com/c/AbCd1234 Text Verified IUL Plus"


# ------------------------------------------------------------ the branches


def read(text=REAL, *, today=TUESDAY, title="New Agent - Gustin Elrod"):
    return agents.read_agent(card(title), text=text, today=today)


def test_an_agent_going_live_today():
    assert read().when(TUESDAY) == "today"


def test_an_agent_going_live_tomorrow():
    said = "Launch date is Wednesday, August 26"

    assert read(said).when(TUESDAY) == "tomorrow"


def test_an_agent_going_live_later():
    said = "Launch date is Thursday, August 27"

    assert read(said).when(TUESDAY) == "later"


def test_a_launch_date_that_has_gone_by_is_treated_as_today():
    """Nobody wants it parked. It is late already."""
    said = "Launch date is Monday, August 24"

    assert read(said, today=TUESDAY).when(TUESDAY) == "today"


def test_a_card_that_cannot_be_read_says_which_half_is_missing():
    agent = read("First Name: Gustin\nOrder 25 Leads")

    assert agent.ready is False
    said = agents.cannot_read(agent, needs_lead_type=True)
    assert "lead type" in said and "launch date" in said


def test_a_card_missing_only_the_date_says_only_that():
    agent = read("Lead Type: Text Verified IUL Plus")

    said = agents.cannot_read(agent, needs_lead_type=True)
    assert "launch date" in said
    assert "lead type" not in said


def test_a_card_that_is_not_its_turn_yet_is_not_nagged_about_its_lead_type():
    """Somebody fills it in before Thursday. Saying it is missing on Tuesday,
    every five minutes, is how a warning stops being read."""
    agent = read("Launch date is Friday, August 28")

    assert agent.lead_type == ""
    assert agents.cannot_read(agent, needs_lead_type=False) == ""


def test_a_card_with_no_launch_date_always_needs_a_person():
    """There is nothing to decide without it, and parking it would be a guess."""
    agent = read("Lead Type: Text Verified IUL Plus")

    assert "launch date" in agents.cannot_read(agent, needs_lead_type=False)


def test_a_card_that_is_not_an_agent_is_not_read():
    assert agents.read_agent(card("💎 General 08/25/26"), text=REAL, today=TUESDAY) is None


# ------------------------------------------- tomorrow's setup card


def test_the_setup_card_is_named_for_the_day_it_covers():
    assert agents.setup_title(date(2026, 8, 26)) == "Agent Setup Going Live Wednesday 08/26"


def test_an_existing_setup_card_is_found_however_it_was_typed():
    """`Agent Setup Going Live Wednesday08/26` - no space - is on the board."""
    cards = [
        {"id": "a", "name": "Agent Setup Going Live Wednesday08/26"},
        {"id": "b", "name": "💎 General 08/26/26"},
    ]

    found = agents.find_setup_card(cards, date(2026, 8, 26))

    assert found["id"] == "a"


def test_a_setup_card_for_another_day_is_not_it():
    cards = [{"id": "a", "name": "Agent Setup Going Live Thursday 08/27"}]

    assert agents.find_setup_card(cards, date(2026, 8, 26)) is None


def test_no_setup_card_yet_is_none_so_one_gets_made():
    assert agents.find_setup_card([], date(2026, 8, 26)) is None


# ------------------------------------------------------- what it will say


def test_the_plan_names_the_agent_the_leads_and_the_day():
    plan = agents.AgentPlan(agent=read(), when="today")
    plan.steps.append(agents.Step("Lead Order 08/25/26", "x", "OTP IUL Plus", "item"))

    said = agents.describe([plan])

    assert "Gustin Elrod" in said
    assert "Text Verified IUL Plus" in said
    assert "Lead Order 08/25/26 · OTP IUL Plus" in said


def test_a_new_checklist_is_flagged_in_the_plan():
    plan = agents.AgentPlan(agent=read(), when="today")
    plan.steps.append(
        agents.Step("Lead Order 08/25/26", "x", "OTP Widows", "item", make_checklist=True)
    )

    assert "(new checklist)" in agents.describe([plan])


def test_a_card_needing_a_person_shows_the_reason_not_the_steps():
    agent = read("First Name: Gustin")
    plan = agents.AgentPlan(agent=agent, when="unknown", problems=[agent.note])

    said = agents.describe([plan])

    assert "⚠" in said and "→" not in said


def test_nothing_waiting_says_so():
    assert "No new agents" in agents.describe([])


# ---------------------------------------- the waiting room, and getting out of it

# Done means finished with. Franklin's list means waiting. A card parked on
# Tuesday because it launches Thursday has to be looked at again on Wednesday,
# when Thursday has become tomorrow - a waiting room nobody goes back to is a
# place things get lost.


def test_a_card_parked_on_tuesday_is_tomorrows_on_wednesday():
    agent = read("Launch date is Thursday, August 27")

    assert agent.when(date(2026, 8, 25)) == "later"
    assert agent.when(date(2026, 8, 26)) == "tomorrow"
    assert agent.when(date(2026, 8, 27)) == "today"


def test_the_three_destinations_are_the_only_three():
    """Filed and finished, or filed and finished, or waiting. Nothing else."""
    for said, when in (
        ("Launch date is today, August 25", "today"),
        ("Launch date is Wednesday, August 26", "tomorrow"),
        ("Launch date is Thursday, August 27", "later"),
    ):
        assert read(said).when(TUESDAY) == when


def test_a_parked_card_still_waiting_is_not_moved_to_where_it_already_is():
    """Moving it to the list it is in would put it at the top every five
    minutes, which reorders somebody's waiting room all day."""
    from wilbyte.bot import jobs

    agent = read("Launch date is Friday, August 28")

    already = jobs._plan_for(
        None, agent, day=TUESDAY, tomorrow=date(2026, 8, 26),
        dated={}, every_card=[], parked=True,
    )
    fresh = jobs._plan_for(
        None, agent, day=TUESDAY, tomorrow=date(2026, 8, 26),
        dated={}, every_card=[], parked=False,
    )
    assert already.problems == [] and fresh.problems == []

    assert already.move_to == ""
    assert fresh.move_to == agents.PARKED


# --------------------------------------------- the card that covers a weekend

# Made every Friday, one card for the whole weekend: "Agent Setup Going Live
# Saturday-Monday 08/22-08/25". Reading only the first date on it would mean an
# agent going live on the Sunday never found it.


WEEKEND = "Agent Setup Going Live Saturday-Monday 08/22-08/25"


@pytest.mark.parametrize("day", [22, 23, 24, 25])
def test_every_day_in_the_span_is_covered(day):
    assert agents.setup_covers(WEEKEND, date(2026, 8, day)) is True


@pytest.mark.parametrize("day", [21, 26])
def test_a_day_outside_the_span_is_not(day):
    assert agents.setup_covers(WEEKEND, date(2026, 8, day)) is False


def test_the_weekend_card_is_found_for_the_sunday(config=None):
    cards = [{"id": "w", "name": WEEKEND}, {"id": "o", "name": "💎 General 08/23/26"}]

    assert agents.find_setup_card(cards, date(2026, 8, 23))["id"] == "w"


def test_a_single_day_card_still_covers_only_that_day():
    single = "Agent Setup Going Live Wednesday 08/26"

    assert agents.setup_covers(single, date(2026, 8, 26)) is True
    assert agents.setup_covers(single, date(2026, 8, 27)) is False


def test_a_span_across_the_turn_of_the_year_is_three_days_not_none():
    span = "Agent Setup Going Live Wednesday-Friday 12/31-01/02"

    assert agents.setup_covers(span, date(2026, 12, 31)) is True
    assert agents.setup_covers(span, date(2027, 1, 1)) is True
    assert agents.setup_covers(span, date(2027, 1, 2)) is True
    assert agents.setup_covers(span, date(2026, 6, 15)) is False


def test_a_card_with_no_date_covers_nothing():
    assert agents.setup_covers("Agent Setup Going Live", date(2026, 8, 26)) is False


# ------------------------------------------------ making one, if it isn't there


def test_a_saturday_gets_the_weekend_shape():
    """Nobody makes a card on Saturday, so Friday's covers until Monday."""
    saturday = date(2026, 8, 29)

    span = agents.weekend_span(saturday)

    assert span == (saturday, date(2026, 8, 31))
    assert agents.setup_title(*span) == (
        "Agent Setup Going Live Saturday-Monday 08/29-08/31"
    )


@pytest.mark.parametrize("day", [24, 25, 26, 27, 28, 30])
def test_every_other_day_is_a_day(day):
    assert agents.weekend_span(date(2026, 8, day)) is None


def test_a_weekday_card_is_named_for_its_one_day():
    assert agents.setup_title(date(2026, 8, 26)) == (
        "Agent Setup Going Live Wednesday 08/26"
    )


def test_one_made_for_the_weekend_is_then_found_by_all_of_it():
    """What it makes on Friday has to be what it looks for on Sunday."""
    saturday = date(2026, 8, 29)
    made = agents.setup_title(*agents.weekend_span(saturday))
    cards = [{"id": "w", "name": made}]

    for day in (29, 30, 31):
        assert agents.find_setup_card(cards, date(2026, 8, day))["id"] == "w"
