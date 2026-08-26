"""New agents, from the form that made the card to the day they go live.

The description is written by a landing-page form and read by nobody in a
hurry. Everything here is checked against the real one - Gustin Elrod's card,
verbatim - because a lead type read wrong sends somebody's leads to the wrong
place and nothing about the board looks different afterwards.
"""

from __future__ import annotations

from datetime import date, timedelta

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


def test_a_setup_card_is_told_apart_from_a_daily_one():
    assert agents.is_setup_card("Agent Setup Going Live Wednesday 08/26") is True
    assert agents.is_setup_card("💎 General 08/26/26") is False
    assert agents.is_setup_card("New Agent - Gustin Elrod") is False


def test_a_setup_card_is_worked_the_day_before_its_agents_go_live():
    """That is the whole shape of it: Thursday's card is Wednesday's work."""
    title = "Agent Setup Going Live Thursday 08/27"

    assert agents.setup_starts(title, date(2026, 8, 25)) == date(2026, 8, 27)
    assert agents.setup_worked_on(title, date(2026, 8, 25)) == date(2026, 8, 26)


def test_the_weekend_card_is_worked_on_the_friday():
    """One card covers Saturday to Monday because nobody is making one on
    Saturday - so the whole weekend gets set up before the Saturday."""
    title = "Agent Setup Going Live Saturday-Monday 08/22-08/24"

    assert agents.setup_starts(title, date(2026, 8, 20)) == date(2026, 8, 22)
    assert agents.setup_worked_on(title, date(2026, 8, 20)) == date(2026, 8, 21)


def test_a_setup_card_over_new_year_is_next_month_not_last_year():
    """The titles carry no year, so 01/02 read on the 30th of December has to
    come out as three days away rather than eleven months behind."""
    title = "Agent Setup Going Live Friday 01/02"

    assert agents.setup_worked_on(title, date(2026, 12, 30)) == date(2027, 1, 1)


def test_a_setup_card_with_no_date_on_it_has_no_working_day():
    assert agents.setup_starts("Agent Setup Going Live", date(2026, 8, 25)) is None
    assert agents.setup_worked_on("Agent Setup Going Live", date(2026, 8, 25)) is None


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


SEBASTIAN = """First Name: Sebastian
Last Name: Espinoza
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
Package Selected: Apex Client
Lead Type: VETS
Target Areas for Marketing: All states besides CA

Live fri, aug 28

$1050/WEEK- UPRISE PHX PLUS

RINGY INTEGRATION
"""

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


# --------------------------- the same line, to three people, on one card

# All four agents landed on Therese and Kathleen and Nicole got nothing. The
# guard against adding a line twice was looking at every item on the whole
# card, so the moment Daniella's line was on Therese it looked like a
# duplicate for the other two.


class Writable:
    """A board that records what was written and answers what it holds."""

    def __init__(self, checklists):
        self.lists = {
            name: {"id": f"l-{name}", "name": name, "checkItems": []}
            for name in checklists
        }
        self.added = []
        self.made = []
        self.moved = []

    def card_checklists(self, card_id):
        return list(self.lists.values())

    def create_checklist(self, card_id, name):
        self.made.append(name)
        self.lists[name] = {"id": f"l-{name}", "name": name, "checkItems": []}
        return self.lists[name]

    def add_check_item(self, checklist_id, name, *, checked=False):
        which = next(c for c in self.lists.values() if c["id"] == checklist_id)
        which["checkItems"].append({"name": name, "state": "incomplete"})
        self.added.append((which["name"], name))
        return {}

    def move_card(self, card_id, list_id, *, position="top"):
        self.moved.append((card_id, list_id, position))
        return {}

    def close(self):
        pass


def file_them(monkeypatch, config, plans, board):
    from wilbyte.bot import jobs

    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)
    return jobs.apply_agents(config, plans, {agents.DONE: "done-list"})


def setup_plan(agent, card_id="setup-thu"):
    plan = agents.AgentPlan(agent=agent, when="tomorrow", move_to=agents.DONE)
    for person in agents.SETUP_PEOPLE:
        plan.steps.append(agents.Step(
            "Agent Setup Going Live Thursday 08/27", card_id, person,
            agents.checklist_item(agent.url, agent.stated),
        ))
    return plan


def test_every_person_named_gets_the_line(monkeypatch, config):
    board = Writable(agents.SETUP_PEOPLE)
    agent = read("Lead Type: OTP VET Plus\nlive thu, aug 27")

    filed, problems = file_them(monkeypatch, config, [setup_plan(agent)], board)

    assert filed == 1 and problems == []
    assert [person for person, _ in board.added] == list(agents.SETUP_PEOPLE)


def test_four_agents_reach_all_three_people(monkeypatch, config):
    """What actually happened: four on Therese, nothing on the other two."""
    board = Writable(agents.SETUP_PEOPLE)
    plans = [
        setup_plan(agents.Agent(
            name=name, card_id=f"c{i}", url=f"https://trello.com/c/x{i}",
            lead_type="OTP VET Plus", stated="OTP VET Plus",
            launch=date(2026, 8, 27),
        ))
        for i, name in enumerate(("Daniella", "Benjamin", "Fabiana", "Vicente"))
    ]

    file_them(monkeypatch, config, plans, board)

    for person in agents.SETUP_PEOPLE:
        assert len([p for p, _ in board.added if p == person]) == 4, person


def test_running_it_again_adds_nothing(monkeypatch, config):
    """The guard still has to guard - just per checklist rather than per card."""
    board = Writable(agents.SETUP_PEOPLE)
    agent = read("Lead Type: OTP VET Plus\nlive thu, aug 27")

    file_them(monkeypatch, config, [setup_plan(agent)], board)
    before = len(board.added)
    file_them(monkeypatch, config, [setup_plan(agent)], board)

    assert len(board.added) == before


def test_two_agents_with_the_same_leads_both_go_on(monkeypatch, config):
    """Their lines differ by the card link, which is the point of it being
    there - matching on the lead type alone would drop the second one."""
    board = Writable(agents.SETUP_PEOPLE)
    plans = [
        setup_plan(agents.Agent(
            name=name, card_id=name, url=f"https://trello.com/c/{name}",
            lead_type="OTP VET Plus", stated="OTP VET Plus",
            launch=date(2026, 8, 27),
        ))
        for name in ("first", "second")
    ]

    file_them(monkeypatch, config, plans, board)

    assert len([p for p, _ in board.added if p == "Therese"]) == 2


# ------------------------ the tier is on the card even when the field omits it

# "Lead Type: vets" with "30 otp vtes" below it. The otp is right there and
# filing him as plain vets throws away the half that says which vets.

BENJAMIN = """-- New Client Onboarded --

First Name: Benjamin
Last Name: Zuniga
Phone: 954-882-8608
Email: benjaminzunigafinancial@gmail.com
Package Selected: Text Verified
Lead Type: vets
Target Areas for Marketing:  VA, NM, FL, TX

30 otp vtes

Live thurs, aug 27

AEP
"""


def test_the_otp_on_the_card_qualifies_the_lead_type():
    assert agents.stated_lead_type(BENJAMIN) == "otp vets"


def test_the_card_s_own_word_is_the_one_repeated_back():
    """"otp" and "text verified" are one tier and two words. Swapping theirs
    for the other reads as RYTE having decided something."""
    assert agents.tier_word(BENJAMIN) == "otp"


def test_the_form_s_own_field_is_not_where_the_order_is_written():
    """"Package Selected: Text Verified" says the tier in the form's words.
    "30 otp vtes" is somebody writing down what was bought."""
    assert "Text Verified" not in agents.stated_lead_type(BENJAMIN)


def test_with_nothing_but_the_field_the_field_is_used():
    """No unlabelled line says a tier, so the labelled one is all there is."""
    said = "Package Selected: Text Verified\nLead Type: vets\n\nlive thu"

    assert agents.stated_lead_type(said) == "Text Verified vets"


def test_a_phrase_that_names_its_own_tier_is_left_alone():
    for card, expected in (
        (SEBASTIAN, "Phoenix Standard"),
        (TAYLER, "UPRISE PHX PLUS"),
        (REAL, "Text Verified IUL Plus"),
        (VICENTE, "Basic Spanish IUL"),
    ):
        assert agents.stated_lead_type(card) == expected


def test_a_card_saying_no_tier_anywhere_says_no_tier():
    assert agents.stated_lead_type("Lead Type: vets\n\nlive thu") == "vets"


def test_the_qualified_type_routes_by_its_tier():
    """"otp vets" is Plus, so it goes to its own checklist. Plain "vets" names
    no tier and would have gone nowhere in particular."""
    assert agents.is_own_setup(agents.stated_lead_type(BENJAMIN)) is False
    assert agents.shape_of(agents.stated_lead_type(BENJAMIN)) == (
        "vet", "plus", frozenset()
    )


# ------------------------------------------- the Spark cards, written a second way

SPARK = """veteran-final-expense — 25 leads · one-time pack
A buyer just purchased on Spark. Fulfill this order from your Spark vendor \
dashboard.

Buyer
Mills Financial LLC

Lead type
veteran-final-expense

Order
25 leads · one-time pack

Price / lead
$38

States
AL, GA, KY, MD, NC, OH, SC, TX

Buyer email
zdmills19@gmail.com

Routing token
lw_ecd0a411ba43c5f6abceddb5b0d7defda796dceba9e2c03e

OTP Vets
Live Thursday, August 27
"""


def test_a_label_on_its_own_line_is_still_the_lead_type_field():
    """Spark writes "Lead type" and puts the value underneath. The order form
    writes "Lead Type: x". Both are the field."""
    assert agents.find_lead_type(SPARK) == "veteran-final-expense"
    assert agents.find_lead_type("Lead Type: OTP IUL Plus") == "OTP IUL Plus"


def test_lead_types_in_prose_is_not_the_field():
    """"we buy several lead types including vets" is somebody talking."""
    assert agents.find_lead_type("we buy several lead types including vets") == ""


def test_an_empty_field_does_not_swallow_the_line_after_the_gap():
    assert agents.find_lead_type("Lead Type:\n\nBuyer\nMills Financial LLC") == ""


def test_the_spark_card_reads_as_the_qualified_type():
    """It names the leads twice - "veteran-final-expense" up top and "OTP Vets"
    at the bottom. The one that says which vets is the one to file."""
    agent = agents.read_agent(
        {"id": "x", "name": "New Agent - Mills Financial LLC",
         "shortUrl": "https://trello.com/c/abc"},
        text=SPARK, today=date(2026, 8, 26),
    )

    assert agent.stated == "OTP Vets"
    assert agents.shape_of(agent.stated) == ("vet", "plus", frozenset())
    assert agent.launch == date(2026, 8, 27)
    assert agent.when(date(2026, 8, 26)) == "tomorrow"
    assert agent.ready is True
    assert agents.cannot_read(agent, needs_lead_type=True) == ""


def test_a_card_that_names_the_leads_without_the_field_is_not_refused():
    """The field is one way of saying it, not the only way. Refusing a card
    that says what it is three times over is refusing to read."""
    agent = agents.Agent(
        name="Somebody", card_id="x", url="", lead_type="", stated="OTP Vets",
        launch=date(2026, 8, 27),
    )

    assert agents.cannot_read(agent, needs_lead_type=True) == ""
    assert agent.ready is True


def test_a_card_that_names_no_leads_at_all_still_needs_a_person():
    agent = agents.Agent(
        name="Somebody", card_id="x", url="", lead_type="", stated="",
        launch=date(2026, 8, 27),
    )

    assert agents.cannot_read(agent, needs_lead_type=True) == (
        "I can't find a lead type on this card."
    )
    assert agent.ready is False


# ------------------------------------------- when the card isn't there yet


def plan_for_launch(text, *, every_card, day=date(2026, 8, 25), parked=False):
    from wilbyte.bot import jobs

    agent = agents.read_agent(card(), text=text, today=day)
    return jobs._plan_for(
        Stub(), agent, day=day, tomorrow=day + timedelta(days=1), dated={},
        every_card=every_card, parked=parked,
    )


def test_no_setup_card_for_tomorrow_parks_rather_than_making_one():
    """The setup cards are made on their own schedule at eleven. A second one
    made here would split a day's agents across two cards."""
    plan = plan_for_launch(
        "Lead Type: OTP Vets\nLive Wednesday, August 26", every_card=[]
    )

    assert plan.move_to == agents.PARKED
    assert plan.steps == []
    assert plan.problems == []


def test_no_setup_card_for_a_day_further_off_parks_too():
    plan = plan_for_launch(
        "Lead Type: OTP Vets\nLive Friday, August 28", every_card=[]
    )

    assert plan.move_to == agents.PARKED


def test_a_card_already_parked_is_left_exactly_where_it_is():
    """Moving it to the list it is already in shuffles Franklin's board every
    five minutes for no reason."""
    plan = plan_for_launch(
        "Lead Type: OTP Vets\nLive Friday, August 28", every_card=[], parked=True
    )

    assert plan.move_to == ""


def test_the_moment_the_card_exists_the_parked_agent_goes_on_it():
    """Franklin's list is a waiting room that gets read every time In Que is."""
    plan = plan_for_launch(
        "Lead Type: OTP Vets\nLive Wednesday, August 26",
        every_card=[{"id": "s", "name": "Agent Setup Going Live Wednesday 08/26"}],
        parked=True,
    )

    assert plan.move_to == agents.DONE
    assert [step.checklist for step in plan.steps] == list(agents.SETUP_PEOPLE)
    assert all(step.card_id == "s" for step in plan.steps)


def test_nothing_in_a_plan_ever_asks_for_a_card_to_be_made():
    assert not hasattr(agents.AgentPlan(agent=None, when="today"), "make_card")


# ------------------------------------------ the customer level, wherever it is


BENJI = """-- New Client Onboarded --

First Name: Benji
Last Name: Missey
Phone: 3148456456
Email: bnmissey05@gmail.com
Package Selected: Text Verified
Lead Type: vets
Target Areas for Marketing:  all states except for FL, CA, IL, GA, IA, KY.

uprise- $350/week standard

live fri, aug 27
"""


def test_the_level_line_is_copied_as_it_was_written():
    """"Lead Type: vets" up top, "uprise- $350/week standard" three lines
    down. The second line is the one that says what the order actually was,
    and it goes down in the words somebody wrote it in - price and all."""
    assert agents.stated_lead_type(BENJI) == "uprise- $350/week standard"


def test_a_level_line_beats_the_lead_type_field_when_it_names_a_tier():
    """The field says "vets" and stops. The line below says which vets."""
    said = "Lead Type: vets\n\nuprise- $350/week standard"

    assert agents.stated_lead_type(said) == "uprise- $350/week standard"


def test_a_price_in_front_still_comes_off():
    """"$1050/WEEK- UPRISE PHX PLUS" is a price and then the leads. Copying
    the line as written does not mean copying the invoice."""
    assert agents.stated_lead_type(TAYLER) == "UPRISE PHX PLUS"


def test_the_level_word_on_its_own_does_not_win():
    """Sebastian's card says "Uprise" on one line and "Phoenix Standard" on
    another. The one naming a tier is the one that says what was bought."""
    assert agents.stated_lead_type(SEBASTIAN) == "Phoenix Standard"


@pytest.mark.parametrize("said", ["uprise", "UPRISE", "Uprise", "phnx", "PHX", "Phoenix"])
def test_every_way_of_writing_the_level_is_found(said):
    assert agents.line_word(f"{said}- $350/week standard") != ""


def test_the_card_s_own_word_is_the_one_repeated_back():
    """Not a tidied one. Inventing a spelling reads as RYTE having decided
    something about somebody's order."""
    assert agents.line_word("UPRISE- $350/week") == "UPRISE"
    assert agents.line_word("phnx plus") == "phnx"


def test_a_phrase_that_already_names_its_level_is_left_alone():
    """"Phoenix Standard" on a card that also says "Uprise" is naming one
    thing twice, not two things."""
    assert agents.stated_lead_type(SEBASTIAN) == "Phoenix Standard"
    assert agents.with_line("Phoenix Standard", "uprise phoenix") == "Phoenix Standard"


def test_a_card_naming_no_level_gains_nothing():
    assert agents.stated_lead_type(REAL) == "Text Verified IUL Plus"
    assert agents.stated_lead_type(BENJAMIN) == "otp vets"


def test_the_level_does_not_change_which_checklist_it_matches():
    """It is part of what to write down, not part of what the leads are for
    matching - "uprise standard vets" is still standard vets."""
    assert agents.shape_of("uprise standard vets") == agents.shape_of("standard vets")


def test_it_is_carried_onto_a_bare_lead_type_line_too():
    """The branch where the card names nothing but the field."""
    assert agents.stated_lead_type("Lead Type: whatever\nuprise- $350") == (
        "uprise whatever"
    )


# ------------------------------- when the card argues with itself about the day


def test_a_weekday_that_is_not_that_date_is_raised_not_resolved():
    """Benji Missey's card says "live fri, aug 27". August 27 is a Thursday.
    Taking the date puts him live a day early if the writer meant the Friday;
    taking the weekday puts him live a day late if they meant the date."""
    said = agents.launch_conflict("live fri, aug 27", today=TUESDAY)

    assert "Friday" in said and "August 27" in said and "Thursday" in said


@pytest.mark.parametrize(
    "said",
    [
        "live thu, aug 27",
        "Live Thursday, August 27",
        "Launch date is today, Tuesday, August 25",
        "live fri, aug 28",
        # One or the other on its own cannot disagree with anything.
        "launch date is friday",
        "Launch date is August 27",
        "",
    ],
)
def test_a_card_that_agrees_with_itself_says_nothing(said):
    assert agents.launch_conflict(said, today=TUESDAY) == ""


def test_a_friday_mentioned_elsewhere_is_not_an_argument():
    """Only the sentence the launch date was read out of."""
    said = "Launch date is Thursday, August 27\n\nHe is away from Friday."

    assert agents.launch_conflict(said, today=TUESDAY) == ""


def test_the_conflict_travels_on_the_agent():
    agent = read("Lead Type: vets\nlive fri, aug 27")

    assert "Thursday" in agent.note


def test_a_card_that_agrees_carries_no_note():
    assert read("Lead Type: vets\nlive thu, aug 27").note == ""


def test_the_conflict_stops_the_agent_being_filed_anywhere():
    """Everything turns on the date - which setup card they go on, and whether
    they park or get filed now. Guessing it wrong is not recoverable by
    looking at the board."""
    from wilbyte.bot import jobs

    agent = read("Lead Type: vets\nlive fri, aug 27")
    plan = jobs._plan_for(
        Stub(), agent, day=TUESDAY, tomorrow=date(2026, 8, 26), dated={},
        every_card=[{"id": "s", "name": "Agent Setup Going Live Thursday 08/27"}],
    )

    assert plan.doable is False
    assert plan.steps == []
    assert plan.move_to == ""
    assert "Which day do they go live?" in plan.problems[0]


# ---------------------------------- an existing agent adding to what they have


EVAN = """-- New Client Onboarded --

First Name: Evan
Last Name: Scott
Phone: +14049901006
Email: ecscott27@gmail.com
Package Selected: Text Verified
Lead Type: Veteran
Target Areas for Marketing:

Need 25 OTP Vets
with Fearless Shepherds
same states last time

add to his active order - Wednesday, August 26
"""

WEDNESDAY = date(2026, 8, 26)


def test_adding_to_an_existing_order_is_a_launch_day():
    """Nothing on Evan Scott's card launches or goes live - he is already
    live. "Add to his active order" is the sentence that says when."""
    assert agents.find_launch(EVAN, today=WEDNESDAY) == WEDNESDAY


def test_it_is_the_same_day_and_so_the_usual_three_cards():
    agent = agents.read_agent(
        card("New Agent - Evan Scott"), text=EVAN, today=WEDNESDAY
    )

    assert agent.when(WEDNESDAY) == "today"
    assert agents.shape_of(agent.stated) == ("vet", "plus", frozenset())
    assert agent.note == ""


@pytest.mark.parametrize(
    "said",
    [
        "add to his active order",
        "added to their current order",
        "add these to the existing order",
        "adding it to her order",
    ],
)
def test_no_date_on_it_means_today(said):
    """Adding to an order that already exists is the same-day job."""
    assert agents.find_launch(said, today=WEDNESDAY) == WEDNESDAY


def test_a_date_on_it_still_wins():
    said = "add to his active order - Friday, August 28"

    assert agents.find_launch(said, today=WEDNESDAY) == date(2026, 8, 28)


def test_a_real_launch_date_is_not_lost_to_it():
    """"Launch date" is read first, so a card carrying both is unaffected."""
    said = "Launch date is Thursday, August 27\nadd to his active order"

    assert agents.find_launch(said, today=WEDNESDAY) == date(2026, 8, 27)


def test_the_three_cards_get_the_line():
    """The usual: that day's Lead Order, Ads and Ops, then the card to Done."""
    from wilbyte.bot import jobs

    agent = agents.read_agent(
        card("New Agent - Evan Scott"), text=EVAN, today=WEDNESDAY
    )
    held = [{"id": "l0", "name": "OTP VET Plus", "checkItems": []}]
    plan = jobs._plan_for(
        Stub(held), agent, day=WEDNESDAY, tomorrow=date(2026, 8, 27),
        dated={
            "lead_order": {"id": "lo", "name": "Lead Order 08/26/26"},
            "ads": {"id": "ad", "name": "📊 Ads 08/26/26"},
            "ops": {"id": "op", "name": "💻 Ops 08/26/26"},
        },
        every_card=[],
    )

    assert plan.problems == []
    assert plan.move_to == agents.DONE
    assert {step.card_title for step in plan.steps} == {
        "Lead Order 08/26/26", "📊 Ads 08/26/26", "💻 Ops 08/26/26",
    }
    assert any(step.checklist == "OTP VET Plus" for step in plan.steps)


# --------------------------------- a card corrected by pasting a block below


COLTON = """-- New Client Onboarded --

First Name: Colton
Last Name: Ramon
Phone: +18018857693
Email: colton.ramon@tryeverlife.com
Package Selected: Text Verified
Lead Type: Text Verified Veteran Plus
Target Areas for Marketing:

Name: Colton Ramon
Lead type: OTP Widows - 50 OTP Widows
States: VA AR MS GA MO AZ IN - States: Same States
Notes: Black Label

Live Friday, August 28
"""


def test_the_lower_lead_type_line_is_the_one_that_counts():
    """A correction gets pasted underneath rather than typed over the top, so
    the block further down is the current one. Colton Ramon is OTP Widows, not
    the Text Verified Veteran Plus the form wrote first."""
    assert agents.find_lead_type(COLTON) == "OTP Widows - 50 OTP Widows"
    assert agents.shape_of(agents.stated_lead_type(COLTON)) == (
        "widows", "plus", frozenset()
    )


def test_one_lead_type_line_is_unaffected():
    assert agents.find_lead_type(REAL) == "Text Verified IUL Plus"
    assert agents.find_lead_type(SPARK) == "veteran-final-expense"


def test_a_block_correction_under_a_line_one_still_wins():
    """Spark writes the label on its own line, a person writes it with a
    colon. Whichever is further down is the later word on it."""
    said = "Lead Type: vets\n\nLead type\nOTP Widows\n"

    assert agents.find_lead_type(said) == "OTP Widows"


def test_a_line_correction_under_a_block_one_still_wins():
    said = "Lead type\nvets\n\nLead Type: OTP Widows\n"

    assert agents.find_lead_type(said) == "OTP Widows"


def test_colton_lands_on_the_widows_checklist():
    """The point of reading the lower block: OTP Widows leads on a card filed
    as Veteran Plus go to the wrong person's order."""
    said, landed, could = agents.best_lead_type(
        COLTON, ["OTP VET Plus", "OTP Widows", "own setup"]
    )

    assert landed == "OTP Widows"
    assert could == []


# ------------------------------- comments say when, never what the leads are


STALE = (
    "✅ OTP SPANISH FEX ON DISTRO HUB setup is complete for ALIANA AREVALO\n"
    "✅ Fired a test in the Discord channel and Google Sheet.",
    "SCHEDULE ADDED @card",
)

ALIANA = """-- New Client Onboarded --

First Name: Aliana
Last Name: Arevalo
Phone: +19542983662
Email: alianaarevalo7@gmail.com
Package Selected: Text Verified
Lead Type: Text Verified Spanish IUL
Target Areas for Marketing: Aliana Arevalo
(954) 298-3662
14 Spanish OTP IUL
States: FL, SC, MO, VA, TX, NC, NE, PA, OH, WV, WI, TN, NJ.
Launch Date: Thursday, August 27
"""


def aliana(desc=ALIANA, *, comments=STALE, today=date(2026, 8, 26)):
    return agents.read_agent(
        card("New Agent - Aliana Arevalo"), text=desc, comments=comments, today=today
    )


def test_a_stale_comment_does_not_become_the_lead_type():
    """These cards get copied from one agent to the next with the comments
    attached. A week-old note about somebody else's Distro Hub setup is not
    what these leads are."""
    agent = aliana()

    assert agent.stated == "Text Verified Spanish IUL"
    assert agents.shape_of(agent.stated) == ("iul", "plus", frozenset({"spanish"}))


def test_a_description_naming_nothing_is_held_rather_than_guessed_at():
    """Not fallen back to the comments. A stale note filed onto somebody's
    order leaves nothing on the board looking wrong."""
    thin = "First Name: Aliana\nPackage Selected: Text Verified\nLaunch Date: Thursday, August 27\n"
    agent = aliana(thin)

    assert agent.stated == ""
    assert agents.cannot_read(agent, needs_lead_type=True) == (
        "I can't find a lead type on this card."
    )


def test_the_launch_date_is_still_read_out_of_a_comment():
    """It turns up in either, and which one is nobody's decision to make."""
    agent = aliana(
        "First Name: Aliana\nLead Type: Text Verified Spanish IUL\n",
        comments=("launch date is Thursday, August 27",),
    )

    assert agent.launch == date(2026, 8, 27)
    assert agent.stated == "Text Verified Spanish IUL"


def test_the_description_beats_a_comment_on_the_date():
    agent = aliana(comments=("Launch date is Monday, August 24",) + STALE)

    assert agent.launch == date(2026, 8, 27)


def test_aliana_is_tomorrow_not_today():
    agent = aliana()

    assert agent.when(date(2026, 8, 26)) == "tomorrow"


def test_what_gets_matched_is_the_description_too():
    """`said` is what goes up against the board's checklists, so a comment
    cannot pull it onto the wrong one either."""
    said, landed, could = agents.best_lead_type(
        aliana().said, ["OTP Spanish IUL", "OTP Spanish FEX", "own setup"]
    )

    assert landed == "OTP Spanish IUL"
    assert could == []
