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


# ------------------- when the body names something better than the field does

# "Lead Type: VETS" up top, "$1050/WEEK- UPRISE PHX PLUS" further down. Both
# are somebody saying what was bought, and the second says which tier as well -
# so if the board has a PHX PLUS, that is where he goes.


def test_the_price_is_not_part_of_what_the_leads_are_called():
    assert agents.tidy_lead_type("$1050/WEEK- UPRISE PHX PLUS") == "UPRISE PHX PLUS"
    assert agents.tidy_lead_type("Phoenix Standard") == "Phoenix Standard"


# --------------------- what an agent is called, before anything is matched

# A setup card has a checklist per person, not per lead type, so there is no
# board to check a phrase against - and the line still has to say what was
# bought. Same rule without the board: the phrase naming a tier wins.


# ---------------------------------------- dates written by people, not forms


@pytest.mark.parametrize(
    "said,when",
    [
        ("Internal: Launch Date Thursday, August 27th", date(2026, 8, 27)),
        ("Launch Date August 27th", date(2026, 8, 27)),
        ("launch date is Sept 3rd", date(2026, 9, 3)),
        ("Launch date is August 1st", date(2026, 8, 1)),
    ],
)
def test_an_ordinal_suffix_does_not_hide_the_date(said, when):
    """"August 27th" - the th runs into the number, so a word boundary after
    it never matched and the whole date was missed."""
    assert agents.find_launch(said, today=TUESDAY) == when


class Stub:
    """A board that answers the one thing a plan asks it."""

    def __init__(self, held=None):
        self.held = held or []

    def card_checklists(self, card_id):
        return self.held


VICENTE = """Package Selected: Basic Spanish IUL
Lead Type: Index Universal Life

Internal: Launch Date Thursday, August 27th
"""


# ------------------------------------ a new agent goes on own setup, always

# The Lead Order card's lead-type checklists are the bulk orders. Every new
# agent on the real card is under "own setup" - "New Agent - Romy Soto · Done ·
# 40 Basic Spanish IUL" - and the lead type is in the line rather than being
# the checklist it sits on.


def filed_today(agent, board_checklists):
    from wilbyte.bot import jobs

    held = [
        {"id": f"l{i}", "name": name, "checkItems": []}
        for i, name in enumerate(board_checklists)
    ]
    return jobs._plan_for(
        Stub(held), agent, day=date(2026, 8, 25), tomorrow=date(2026, 8, 26),
        dated={
            "lead_order": {"id": "lo", "name": "Lead Order 08/25/26"},
            "ads": {"id": "ad", "name": "📊 Ads 08/25/26"},
            "ops": {"id": "op", "name": "💻 Ops 08/25/26"},
        },
        every_card=[],
    )


LEAD_ORDER_LISTS = ["OTP IUL Plus", "OTP Spanish IUL", "Phoenix Standard", "own setup"]


@pytest.mark.parametrize(
    "leads",
    ["40 Basic FB Spanish IUL", "Basic Spanish IUL", "Instant/Basic IUL Leads",
     "FB Index Universal Life", "20 Spanish Basic 10", "Phoenix Standard",
     "PHNX Standard"],
)
def test_the_standard_tier_is_the_self_setup_half(leads):
    """Every item on the real own-setup checklist is one of these, and Phoenix
    Standard belongs with them - the same tier, not an exception."""
    assert agents.is_own_setup(leads) is True


@pytest.mark.parametrize(
    "leads",
    ["Text Verified IUL Plus", "OTP VET Plus", "UPRISE PHX PLUS", "PHNX Plus"],
)
def test_the_plus_tier_is_ordered_and_keeps_its_own_checklist(leads):
    """PHX Plus is where Phoenix stops being like the rest: a Plus customer
    has a checklist, a Standard one sets themselves up."""
    assert agents.is_own_setup(leads) is False


def test_the_two_phoenix_tiers_go_to_different_places():
    """One rule telling them apart rather than Phoenix being special-cased."""
    assert agents.is_own_setup("Phoenix Standard") is True
    assert agents.is_own_setup("Phoenix Plus") is False


BASIC_TODAY = """Package Selected: Basic Spanish IUL
Lead Type: Index Universal Life

Launch date is today, Tuesday, August 25
"""


def test_a_basic_agent_lands_on_own_setup():
    agent = read(BASIC_TODAY, title="New Agent - Vicente Mejia")

    plan = filed_today(agent, LEAD_ORDER_LISTS)

    (order,) = [s for s in plan.steps if s.card_title.startswith("Lead Order")]
    assert order.checklist == "own setup"
    assert order.make_checklist is False
    assert order.item.endswith("Basic Spanish IUL")


def test_an_otp_agent_lands_on_its_own_lead_type():
    """Gustin bought text-verified IUL. That has a checklist of its own."""
    plan = filed_today(read(), LEAD_ORDER_LISTS)

    (order,) = [s for s in plan.steps if s.card_title.startswith("Lead Order")]
    assert order.checklist == "OTP IUL Plus"


def test_the_line_carries_the_link_and_the_lead_type():
    """"New Agent - Romy Soto · Done · 40 Basic Spanish IUL" - the link renders
    the name and badge, the words say what was bought."""
    plan = filed_today(read(), LEAD_ORDER_LISTS)

    (order,) = [s for s in plan.steps if s.card_title.startswith("Lead Order")]
    assert order.item.endswith("Text Verified IUL Plus")
    assert order.item.startswith("https://trello.com/c/")


def test_a_self_setup_type_is_never_a_question():
    """It goes on own setup whatever the board has, so there is nothing to be
    unsure about."""
    agent = read(BASIC_TODAY, title="New Agent - Vicente Mejia")

    plan = filed_today(agent, ["OTP IUL Plus", "OTP Spanish IUL", "own setup"])

    assert plan.problems == []


def test_own_setup_is_made_if_the_card_has_not_got_one():
    agent = read(BASIC_TODAY, title="New Agent - Vicente Mejia")

    plan = filed_today(agent, ["OTP IUL Plus"])

    (order,) = [s for s in plan.steps if s.card_title.startswith("Lead Order")]
    assert order.checklist == "own setup"
    assert order.make_checklist is True


def test_ads_and_ops_still_go_to_people():
    plan = filed_today(read(), LEAD_ORDER_LISTS)

    ads = [s.checklist for s in plan.steps if "Ads" in s.card_title]
    ops = [s.checklist for s in plan.steps if "Ops" in s.card_title]

    assert ads == list(agents.ADS_PEOPLE)
    assert ops == list(agents.OPS_PEOPLE)
