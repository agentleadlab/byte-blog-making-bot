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


# ------------------- a card exists for the day, so they can go on it now

# Tuesday, and "Agent Setup Going Live Thursday 08/27" is already in Automation
# Department. Waiting until Wednesday to put Thursday's agents on it would be
# waiting for nothing: the card they go on is already there.


class Stub:
    """A board that answers what a plan needs to ask it."""

    def __init__(self, held=None):
        self.held = held or []

    def card_checklists(self, card_id):
        return self.held


def plan_for(agent, *, every_card, day=TUESDAY, parked=False, dated=None, held=None):
    from datetime import timedelta

    from wilbyte.bot import jobs

    return jobs._plan_for(
        Stub(held), agent, day=day, tomorrow=day + timedelta(days=1),
        dated=dated or {}, every_card=every_card, parked=parked,
    )


THURSDAY_CARD = {"id": "setup-thu", "name": "Agent Setup Going Live Thursday 08/27"}


def thursday_agent(text=None):
    return read(text or "Lead Type: OTP VET Plus\nLaunch date is Thursday, August 27")


def test_an_agent_for_thursday_goes_on_thursdays_card_on_tuesday():
    plan = plan_for(thursday_agent(), every_card=[THURSDAY_CARD])

    assert plan.move_to == agents.DONE
    assert plan.make_card == ""
    assert [step.checklist for step in plan.steps] == list(agents.SETUP_PEOPLE)
    assert all(step.card_id == "setup-thu" for step in plan.steps)


def test_with_no_card_for_that_day_it_waits_in_franklins_list():
    plan = plan_for(thursday_agent(), every_card=[])

    assert plan.move_to == agents.PARKED
    assert plan.steps == []


def test_one_already_waiting_is_left_where_it_is():
    plan = plan_for(thursday_agent(), every_card=[], parked=True)

    assert plan.move_to == ""


def test_tomorrows_card_is_made_when_it_is_not_there():
    """The only day RYTE makes one. Any further out, Franklin makes it."""
    agent = read("Lead Type: OTP VET Plus\nLaunch date is Wednesday, August 26")

    plan = plan_for(agent, every_card=[])

    assert plan.make_card == "Agent Setup Going Live Wednesday 08/26"
    assert plan.move_to == agents.DONE


def test_the_weekend_card_takes_sundays_agents_too():
    weekend = {"id": "w", "name": "Agent Setup Going Live Saturday-Monday 08/29-08/31"}
    agent = read("Lead Type: OTP FEX\nLaunch date is Sunday, August 30")

    plan = plan_for(agent, every_card=[weekend], day=date(2026, 8, 28))

    assert all(step.card_id == "w" for step in plan.steps)
    assert plan.move_to == agents.DONE


def test_a_lead_type_is_needed_once_there_is_a_card_to_go_on():
    """Parked, it can wait. About to be filed, it cannot."""
    agent = thursday_agent("Launch date is Thursday, August 27")

    waiting = plan_for(agent, every_card=[])
    filing = plan_for(agent, every_card=[THURSDAY_CARD])

    assert waiting.problems == [] and waiting.move_to == agents.PARKED
    assert "lead type" in filing.problems[0]


def test_the_agent_card_ends_up_at_the_top_of_done(monkeypatch, config):
    """"Moved to done on top position" - under forty-nine other cards is not
    where anybody looks."""
    from wilbyte.bot import jobs

    moved = []

    class Board(Stub):
        checklists_made: list = []

        def card_checklists(self, card_id):
            return [{"id": "l1", "name": p, "checkItems": []} for p in agents.SETUP_PEOPLE]

        def add_check_item(self, checklist_id, name, *, checked=False):
            return {}

        def move_card(self, card_id, list_id, *, position="top"):
            moved.append((card_id, list_id, position))
            return {}

        def close(self):
            pass

    monkeypatch.setattr(jobs, "open_trello", lambda cfg: Board())
    plan = plan_for(thursday_agent(), every_card=[THURSDAY_CARD])

    jobs.apply_agents(config, [plan], {agents.DONE: "done-list"})

    assert moved == [(plan.agent.card_id, "done-list", "top")]


# ------------------------------------- the ways the real cards actually read

# Three cards in Franklin's list said "no launch date" and all three had one.
# They just don't use the word launch.

SEBASTIAN = """First Name: Sebastian
Last Name: Espinoza
Phone: (847) 775-9758
Email: seb.esp6@gmail.com
Package Selected: Text Verified
Lead Type: Phoenix Campaign
States: all states except for FL and CA.

Veteran

Uprise

Phoenix Standard
$350

live fri, aug 28
"""

TAYLER = """First Name: Tayler
Last Name: Collins
Phone: +1904-814-3494
Email: taylercollins949@yahoo.com
Package Selected: Apex Client
Lead Type: VETS
Target Areas for Marketing: All states besides CA

Live fri, aug 28

$1050/WEEK- UPRISE PHX PLUS

RINGY INTEGRATION
"""


@pytest.mark.parametrize("said", [SEBASTIAN, TAYLER])
def test_live_fri_aug_28_is_a_launch_date(said):
    """No word "launch" anywhere on either card."""
    assert agents.find_launch(said, today=TUESDAY) == date(2026, 8, 28)


@pytest.mark.parametrize(
    "said,when",
    [
        ("live fri, aug 28", date(2026, 8, 28)),
        ("Live fri, aug 28", date(2026, 8, 28)),
        ("going live Thursday, August 27", date(2026, 8, 27)),
        ("goes live 8/27", date(2026, 8, 27)),
        ("go live wed", date(2026, 8, 26)),
    ],
)
def test_the_other_ways_of_saying_when(said, when):
    assert agents.find_launch(said, today=TUESDAY) == when


def test_launch_date_still_wins_over_a_bare_live():
    """"Live transfer leads" is a product. The sentence that says launch date
    is the one that means it."""
    said = "Live transfer leads ordered 8/20.\nLaunch date is Thursday, August 27."

    assert agents.find_launch(said, today=TUESDAY) == date(2026, 8, 27)


# ------------------------------------------------------------ phoenix leads


@pytest.mark.parametrize(
    "written,shape",
    [
        ("Phoenix Campaign", ("phnx", None, frozenset())),
        ("Phoenix Standard", ("phnx", "standard", frozenset())),
        ("UPRISE PHX PLUS", ("phnx", "plus", frozenset())),
        ("PHNX Plus", ("phnx", "plus", frozenset())),
        ("phnx standard", ("phnx", "standard", frozenset())),
    ],
)
def test_phoenix_however_it_is_abbreviated(written, shape):
    assert agents.shape_of(written) == shape


BOARD = ["PHNX Plus", "PHNX Standard", "OTP IUL Plus", "OTP VET Plus"]


def test_a_lead_type_with_no_tier_is_not_guessed_at():
    """Phoenix leads, and the board has two kinds. Picking one is the guess
    that puts somebody's leads on the wrong order."""
    assert agents.match_checklist("Phoenix Campaign", BOARD) is None
    assert agents.candidates("Phoenix Campaign", BOARD) == ["PHNX Plus", "PHNX Standard"]


def test_the_rest_of_the_card_settles_it():
    """"Lead Type: Phoenix Campaign" with "Phoenix Standard / $350" three
    lines below is one card saying one thing twice."""
    tier = agents.tier_hint(SEBASTIAN)

    assert tier == "standard"
    assert agents.match_checklist("Phoenix Campaign", BOARD, tier=tier) == "PHNX Standard"


def test_a_card_that_names_its_tier_needs_no_help():
    assert agents.match_checklist("UPRISE PHX PLUS", BOARD) == "PHNX Plus"


def test_the_lead_type_field_still_decides_which_leads():
    """Tayler's field says VETS and the body mentions PHX PLUS. The field is
    the field; the body only settles a tier it didn't state."""
    agent = read(TAYLER, title="NEW AGENT- Tayler Collins")

    assert agent.lead_type == "VETS"
    assert agents.match_checklist(agent.lead_type, BOARD, tier=agent.tier) == "OTP VET Plus"


def test_an_agent_reads_its_tier_off_the_whole_card():
    assert read(SEBASTIAN).tier == "standard"


# ------------------- when the body names something better than the field does

# "Lead Type: VETS" up top, "$1050/WEEK- UPRISE PHX PLUS" further down. Both
# are somebody saying what was bought, and the second says which tier as well -
# so if the board has a PHX PLUS, that is where he goes.


def test_the_body_wins_when_it_names_a_tier_and_the_field_does_not():
    said, landed, could = agents.best_lead_type(TAYLER, BOARD)

    assert landed == "PHNX Plus"
    assert said == "UPRISE PHX PLUS"
    assert could == []


def test_the_price_is_not_part_of_what_the_leads_are_called():
    assert agents.tidy_lead_type("$1050/WEEK- UPRISE PHX PLUS") == "UPRISE PHX PLUS"
    assert agents.tidy_lead_type("Phoenix Standard") == "Phoenix Standard"


def test_without_a_phx_plus_on_the_board_he_is_what_the_field_says():
    """"He should be PHX PLUS if there is a PHX PLUS." If there isn't, the
    field stands."""
    board = ["OTP VET Plus", "OTP IUL Plus", "OTP FEX"]

    said, landed, _ = agents.best_lead_type(TAYLER, board)

    assert landed == "OTP VET Plus"


def test_the_body_settles_a_tier_within_the_same_family():
    said, landed, _ = agents.best_lead_type(SEBASTIAN, BOARD)

    assert landed == "PHNX Standard"
    assert said == "Phoenix Standard"


def test_a_field_that_already_names_its_tier_is_left_alone():
    """Gustin's field says everything. The body agreeing changes nothing."""
    said, landed, _ = agents.best_lead_type(REAL, BOARD)

    assert landed == "OTP IUL Plus"
    assert said == "Text Verified IUL Plus"


def test_a_card_disagreeing_with_itself_asks_a_person():
    """Two phrases, both naming a tier, landing somewhere different. That is
    not a thing to pick between."""
    card = "Lead Type: OTP IUL Plus\n\nUPRISE PHX PLUS\n\nlive fri, aug 28"

    said, landed, could = agents.best_lead_type(card, BOARD)

    assert landed is None
    assert could == ["OTP IUL Plus", "PHNX Plus"]


def test_the_line_says_what_it_was_filed_as():
    """Filed under PHX PLUS, the line should say PHX PLUS - not the field it
    overrode."""
    from wilbyte.bot import jobs

    agent = read(TAYLER, title="NEW AGENT- Tayler Collins")
    held = [{"id": "l", "name": name, "checkItems": []} for name in BOARD]
    said, landed, _ = agents.best_lead_type(agent.said, BOARD)

    step = jobs._step("Lead Order", "c", landed, agent, held, exact=True, label=said)

    assert step.item.endswith("UPRISE PHX PLUS")
    assert step.checklist == "PHNX Plus"
    assert step.make_checklist is False
