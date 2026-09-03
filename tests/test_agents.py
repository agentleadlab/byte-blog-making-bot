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


LEAD_ORDER_LISTS = ["OTP IUL Plus", "OTP Spanish IUL", "PHNX STANDARD", "own setup"]


@pytest.mark.parametrize(
    "leads",
    ["40 Basic FB Spanish IUL", "Basic Spanish IUL", "Instant/Basic IUL Leads",
     "FB Index Universal Life", "20 Spanish Basic 10"],
)
def test_basic_instant_and_fb_set_themselves_up(leads):
    """Every item on the real own-setup checklist is one of these three."""
    assert agents.is_own_setup(leads) is True


@pytest.mark.parametrize(
    "leads",
    ["Phoenix Standard", "PHNX Standard", "UPRISE STANDARDS- $350/WEEK",
     "uprise- $350/week standard", "Ascend Standard"],
)
def test_a_line_product_is_ordered_even_at_the_standard_tier(leads):
    """Standard is a tier of Uprise and Phoenix, not a synonym for self-setup.

    Carsen Anderson landed on own setup beside six Basic Spanish IUL lines and
    he had bought Uprise.
    """
    assert agents.is_own_setup(leads) is False


@pytest.mark.parametrize(
    "leads",
    ["Text Verified IUL Plus", "OTP VET Plus", "UPRISE PHX PLUS", "PHNX Plus"],
)
def test_the_plus_tier_is_ordered_and_keeps_its_own_checklist(leads):
    assert agents.is_own_setup(leads) is False


def test_basic_still_wins_over_the_line_it_is_written_beside():
    """"Basic FB Spanish IUL" is self-setup however the card words it."""
    assert agents.is_own_setup("40 Basic FB Spanish IUL") is True


def test_uprise_and_phnx_are_one_product_under_two_names():
    """"PHNX Standard" and "UPRISE STANDARDS" are the same leads."""
    assert agents.shape_of("PHNX STANDARD") == agents.shape_of("UPRISE STANDARDS")
    assert agents.family_of("UPRISE STANDARDS- $350/WEEK") == "phnx"


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
    assert agents.stated_lead_type("Lead Type: vets\nuprise") == "uprise vets"


def test_a_level_on_its_own_line_is_not_the_lead_type():
    """Sebastian Espinoza's card has "Uprise" sitting alone above "Phoenix
    Standard". It says something about the leads without saying what they
    are, and reading it as the answer would file him as Uprise."""
    said = "Lead Type: Phoenix Campaign\n\nVeteran\n\nUprise\n\nPhoenix Standard\n"

    assert agents.named_lead_types(said) == [
        "Phoenix Campaign", "Veteran", "Phoenix Standard",
    ]
    assert agents.stated_lead_type(said) == "Phoenix Standard"


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
    what these leads are - and Final Expense is what it would have made this."""
    agent = aliana()

    assert "FEX" not in agent.stated and "Final Expense" not in agent.stated
    assert agents.shape_of(agent.stated) == ("iul", "plus", frozenset({"spanish"}))


def test_the_lower_line_is_aliana_s_real_lead_type():
    """The field says "Text Verified Spanish IUL"; four lines down the card
    says "14 Spanish OTP IUL". The lower one is the corrected one."""
    assert aliana().stated == "14 Spanish OTP IUL"


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


def test_a_sentence_about_the_leads_is_not_a_lead_type():
    """Gustin Elrod's card says "Gustin Elrod paid for OTP IUL leads and with
    Don A setup for immediate launch" underneath the field. True, and not the
    name of anything you can buy - so the field stands."""
    assert agents.stated_lead_type(REAL) == "Text Verified IUL Plus"
    assert "paid for" not in agents.stated_lead_type(REAL)


def test_a_states_line_is_not_a_lead_type_either():
    said = (
        "Lead Type: Text Verified Spanish IUL\n"
        "States: FL, SC, MO, VA, TX, NC, NE, PA, OH, WV, WI, TN, NJ, MS, MN, MD.\n"
    )

    assert agents.stated_lead_type(said) == "Text Verified Spanish IUL"


@pytest.mark.parametrize(
    "text,expected",
    [
        # The one the card was corrected to, every time.
        ("Lead Type: vets\n\n14 Spanish OTP IUL", "14 Spanish OTP IUL"),
        ("Lead Type: OTP Widows\n\nLead type: OTP IUL Plus", "OTP IUL Plus"),
        # One mention stays one mention.
        ("Lead Type: OTP Widows", "OTP Widows"),
    ],
)
def test_the_lower_phrase_wins(text, expected):
    assert agents.stated_lead_type(text) == expected


# ----------------------------- set up on leads they did not order


BRODY_ORDERED = "OTP Vets - 30 more OTP vets"
BRODY_SAID = [
    "@card\n✅ Updated previous setup of BRODY SULLIVAN for OTP VET ON DISTRO HUB\n"
    "✅ Used the same sheet with stopper. States updated.\n\nReady to go live WED, AUG 26",
]


@pytest.mark.parametrize(
    "comment,expected",
    [
        ("✅ Updated previous setup of BRODY SULLIVAN for OTP VET ON DISTRO HUB",
         "OTP VET"),
        ("✅ OTP SPANISH FEX ON DISTRO HUB setup is complete for ALIANA AREVALO",
         "OTP SPANISH FEX"),
        ("✅ OTP VET ON DISTRO HUB setup is complete for MILLS FINANCIAL LLC",
         "OTP VET"),
        ("nothing about a hub here", ""),
    ],
)
def test_the_confirmation_says_what_was_set_up(comment, expected):
    """Three real ones. The name in front of the lead type is trimmed off -
    "for BRODY SULLIVAN for OTP VET" must not read as Brody the lead type."""
    assert agents.setup_said([comment]) == expected


def test_the_latest_confirmation_is_the_one_that_counts():
    """A setup gets redone - Brody's comment starts "Updated previous setup"."""
    said = [
        "OTP FEX ON DISTRO HUB setup is complete for BRODY SULLIVAN",
        "Updated previous setup of BRODY SULLIVAN for OTP VET ON DISTRO HUB",
    ]

    assert agents.setup_said(said) == "OTP VET"


def test_brody_as_he_really_is_is_not_flagged():
    """"OTP Vets - 30 more OTP vets" against "OTP VET" is the same leads said
    two ways, and nobody should be bothered about it."""
    assert agents.wrong_setup(BRODY_ORDERED, BRODY_SAID) is None


def test_brody_set_up_on_final_expense_is_flagged():
    """Franklin's example. Somebody's money going to the wrong campaign, with
    the card in Done and nothing on the board looking wrong."""
    wrong = [c.replace("OTP VET ON", "OTP FEX ON") for c in BRODY_SAID]

    assert agents.wrong_setup(BRODY_ORDERED, wrong) == (BRODY_ORDERED, "OTP FEX")


def test_the_qualifier_counts_too():
    """Spanish IUL and plain IUL are two different orders."""
    said = ["OTP SPANISH IUL ON DISTRO HUB setup is complete"]

    assert agents.wrong_setup("OTP IUL Plus", said) == ("OTP IUL Plus", "OTP SPANISH IUL")


def test_the_tier_counts_too():
    """Basic Spanish IUL is a different product from OTP Spanish IUL."""
    said = ["OTP SPANISH IUL ON DISTRO HUB setup is complete"]

    assert agents.wrong_setup("Basic Spanish IUL", said) is not None


@pytest.mark.parametrize(
    "ordered,comments",
    [
        # No confirmation yet is not a wrong setup.
        ("OTP Vets", ["@card informed"]),
        ("OTP Vets", []),
        # A confirmation nobody wrote a lead type into.
        ("OTP Vets", ["setup is complete ON DISTRO HUB"]),
        # Nothing ordered to compare against.
        ("", BRODY_SAID),
    ],
)
def test_nothing_to_compare_is_not_a_complaint(ordered, comments):
    assert agents.wrong_setup(ordered, comments) is None


# --------------------------------------------------- Ascend, the newest one


LARS = """-- New Client Onboarded --

First Name: Lars
Last Name: Christofferson
Phone: +1 406-589-1110
Email: larschristofferson41@gmail.com
Package Selected: Text Verified
Lead Type: IUL
Target Areas for Marketing: same states

$350/week- Ascend Standard
Live Fri, aug 28

connect crm
"""


def test_ascend_standard_is_read_off_the_price_line():
    """"Lead Type: IUL" up top and "$350/week- Ascend Standard" below. The
    price comes off the front; the level and the tier are what he bought."""
    assert agents.stated_lead_type(LARS) == "Ascend Standard"
    assert agents.shape_of("Ascend Standard") == ("iul", "standard", frozenset())


def test_ascend_is_the_name_the_iul_line_is_sold_under():
    """"ascend standard = iul standard". Lars Christofferson's card said both
    halves of one thing - "Lead Type: IUL" up top, "Ascend Standard" below -
    and this was read in August as two products rather than one."""
    assert agents.family_of("Ascend Standard") == "iul"
    assert agents.shape_of("Ascend Standard") == agents.shape_of("OTP IUL Standard")
    assert agents.shape_of("Ascend Plus") == agents.shape_of("OTP IUL Plus")


def test_an_ascend_order_lands_on_the_iul_checklist():
    """Alanna Jackson's, which had nowhere to go while Ascend was its own
    product and the Lead Order card had no checklist by that name."""
    have = ["OTP IUL Standard", "OTP IUL Plus", "Phoenix Standard", "OTP Widows"]

    assert agents.match_checklist(
        "Ascend Standard", have, tier=agents.tier_of("Ascend Standard")
    ) == "OTP IUL Standard"


def test_ascend_is_still_the_level_that_was_ordered():
    """A family it shares with IUL, and a name of its own that stays on the
    line - "Lead Type: vets" with "ascend" below it is still an Ascend order."""
    assert agents.stated_lead_type("Lead Type: IUL\n$350/week- Ascend Plus") == (
        "Ascend Plus"
    )
    # Ordered, not self-setup: Standard is a tier of the line rather than a
    # synonym for setting yourself up, exactly as it is for Phoenix.
    assert agents.is_own_setup("Ascend Standard") is False


def test_ascend_and_phoenix_are_not_the_same_order():
    """Both are Standard tier and neither is the other, which is what keeps
    them on separate checklists."""
    assert agents.shape_of("Ascend Standard") != agents.shape_of("Phoenix Standard")


def test_a_level_order_is_not_compared_against_the_hub():
    """The Distro Hub is organised by level rather than by lead type, so an
    Uprise order is confirmed as "PHX STANDARD" and the two words have nothing
    to do with each other. Landon Brown was set up correctly and raised as
    wrong."""
    assert agents.wrong_setup(
        "Ascend Standard", ["PHOENIX STANDARD ON DISTRO HUB setup is complete"]
    ) is None


def test_ascend_is_ordered_at_either_tier():
    """Ascend is a line like Uprise and Phoenix, so Standard is a tier of it."""
    assert agents.is_own_setup("Ascend Standard") is False
    assert agents.is_own_setup("Ascend Plus") is False


def test_lars_goes_live_on_the_friday():
    assert agents.find_launch(LARS, today=date(2026, 8, 27)) == date(2026, 8, 28)


# --------------------------- the eight correct setups that got raised as wrong


HUB = "✅ {} ON DISTRO HUB setup is complete for SOMEBODY"


@pytest.mark.parametrize(
    "ordered,setup",
    [
        # "OTP WIDOW VET" names two kinds of leads. Whether it is widows,
        # veterans, or widows of veterans as one product is not something to
        # work out from the words.
        ("20 otp widow", "OTP WIDOW VET"),
        ("OTP Widows", "OTP WIDOW VET"),
        ("13 OTP Widow leads", "OTP WIDOW VET"),
        ("paid for OTP Widows and is with Fearless", "OTP WIDOW VET"),
        # "IULs" is IUL leads. The pattern wanted the singular.
        ("OTP IULs - 10 more otp IULs", "OTP IUL"),
    ],
)
def test_these_were_set_up_right_and_must_not_be_flagged(ordered, setup):
    assert agents.wrong_setup(ordered, [HUB.format(setup)]) is None


@pytest.mark.parametrize(
    "said,expected",
    [
        ("OTP IUL", "iul"),
        ("OTP IULs", "iul"),
        ("OTP IULs - 10 more otp IULs", "iul"),
        ("25 Text Verified IUL leads w/discount", "iul"),
    ],
)
def test_iul_is_read_singular_or_plural(said, expected):
    assert agents.family_of(said) == expected


def test_a_phrase_naming_two_kinds_of_leads_is_read_as_both():
    assert agents.families_in("OTP WIDOW VET") == {"widows", "vet"}
    assert agents.families_in("OTP Widows") == {"widows"}
    assert agents.families_in("nothing here") == set()


@pytest.mark.parametrize(
    "ordered,setup",
    [
        ("OTP Vets - 30 more OTP vets", "OTP FEX"),
        ("14 Spanish OTP IUL", "OTP SPANISH FEX"),
        # Same leads, different tier - still a different product.
        ("Basic Spanish IUL", "OTP SPANISH IUL"),
        # Same leads, different qualifier.
        ("OTP IUL Plus", "OTP SPANISH IUL"),
    ],
)
def test_a_real_mismatch_still_fires(ordered, setup):
    assert agents.wrong_setup(ordered, [HUB.format(setup)]) is not None


# ------------------------------ "leads going by tomorrow" - no launch, no live


KARYN = """-- New Client Onboarded --

First Name: Karyn
Last Name: Giles
Phone: +12406024770
Email: karyngilesfflmd@gmail.com
Package Selected: Text Verified
Lead Type: Veteran Final Expense
Target Areas for Marketing:

Same States last time
40 Text Verified Veteran leads

leads going by tomorrow, Friday, August 28
"""


def test_leads_going_by_a_day_is_a_launch_date():
    """Karyn Giles's card never says launch, live or going live. It says
    "leads going by tomorrow, Friday, August 28", which is the same thing."""
    assert agents.find_launch(KARYN, today=date(2026, 8, 27)) == date(2026, 8, 28)


@pytest.mark.parametrize(
    "said",
    [
        "leads going by tomorrow, Friday, August 28",
        "leads go out Friday, August 28",
        "leads going out tomorrow, August 28",
        "leads going tomorrow, August 28",
        "lead going by August 28",
    ],
)
def test_the_ways_people_say_the_leads_start(said):
    assert agents.find_launch(said, today=date(2026, 8, 27)) == date(2026, 8, 28)


def test_karyn_is_read_whole():
    agent = agents.read_agent(
        card("New Agent - Karyn Giles"), text=KARYN, today=date(2026, 8, 27)
    )

    assert agent.stated == "40 Text Verified Veteran leads"
    assert agents.shape_of(agent.stated) == ("vet", "plus", frozenset())
    assert agent.when(date(2026, 8, 27)) == "tomorrow"
    assert agents.cannot_read(agent, needs_lead_type=True) == ""


def test_the_older_wordings_still_read_the_same():
    """Eleven real cards, none of them affected."""
    today = date(2026, 8, 27)

    assert agents.find_launch(REAL, today=today) == date(2026, 8, 25)
    assert agents.find_launch(TAYLER, today=today) == date(2026, 8, 28)
    assert agents.find_launch(SPARK, today=today) == date(2026, 8, 27)
    assert agents.find_launch(EVAN, today=today) == date(2026, 8, 26)
    assert agents.find_launch(LARS, today=today) == date(2026, 8, 28)


# ------------------ three more correct setups that got raised, 27 Aug


@pytest.mark.parametrize(
    "who,ordered,setup",
    [
        # The Distro Hub names the level, not the leads.
        ("Landon Brown", "Uprise vets", "PHX STANDARD"),
        ("Caleb Stewart", "Uprise Phoenix Standards JUST FOR ONE WEEK", "PHX STANDARD"),
        ("Freddy Leon", "phoenix standards", "PHX STANDARD"),
    ],
)
def test_a_level_order_is_never_raised_as_wrong(who, ordered, setup):
    assert agents.wrong_setup(ordered, [HUB.format(setup)]) is None, who


@pytest.mark.parametrize(
    "said,expected",
    [
        ("PHX STANDARD", "standard"),
        ("phoenix standards", "standard"),
        ("Uprise Phoenix Standards JUST FOR ONE WEEK", "standard"),
        ("20 Spanish Basics", "standard"),
        ("Basic Spanish IUL", "standard"),
    ],
)
def test_standard_is_read_singular_or_plural(said, expected):
    """"phoenix standards" was reading as naming no tier at all, which made it
    disagree with a confirmation that named one."""
    assert agents.tier_of(said) == expected


@pytest.mark.parametrize("said", ["PHX STNDRD", "phx stndrds", "PHNX STNDRD"])
def test_standard_with_the_vowels_dropped_is_still_standard(said):
    """Five agents came through one setup card written "PHX STNDRD". despell
    only reorders letters that are already there, so this was not a typo it
    could put right - and a lead type naming no tier fits Standard and Plus
    equally, so all five were refused instead of filed."""
    assert agents.tier_of(said) == "standard"


def test_the_abbreviated_standard_lands_on_the_standard_checklist():
    have = ["Phoenix Standard", "Phoenix Plus", "OTP VET Plus", "OTP Widows"]

    assert agents.match_checklist(
        "PHX STNDRD", have, tier=agents.tier_of("PHX STNDRD")
    ) == "Phoenix Standard"


def test_iul_written_out_in_full_is_iul():
    """Siona Paradas's card said "Text Verified Index Universal Life" - the
    product every other line on the board abbreviates."""
    assert agents.family_of("Text Verified Index Universal Life") == "iul"
    assert agents.family_of("Indexed Universal Life") == "iul"
    assert agents.shape_of("Text Verified Index Universal Life") == agents.shape_of(
        "OTP IUL Plus"
    )


def test_iul_written_out_lands_where_the_abbreviated_one_does():
    assert agents.match_checklist(
        "Text Verified Index Universal Life", ["OTP IUL Plus", "OTP IUL Standard"],
        tier=agents.tier_of("Text Verified Index Universal Life"),
    ) == "OTP IUL Plus"


@pytest.mark.parametrize("said", ["PHX 2.0", "PHOENIX 2.0", "PHNX 2.0"])
def test_two_point_oh_is_the_plus_tier(said):
    """"phnx plus = phnx 2.0" - the version number is what Plus is called on
    the cards that don't use the word."""
    assert agents.tier_of(said) == "plus"
    assert agents.shape_of(said) == agents.shape_of("Phoenix Plus")


def test_the_version_number_lands_on_the_plus_checklist():
    have = ["Phoenix Standard", "Phoenix Plus", "OTP VET Plus", "OTP Widows"]

    assert agents.match_checklist(
        "PHX 2.0", have, tier=agents.tier_of("PHX 2.0")
    ) == "Phoenix Plus"


def test_a_price_is_not_a_tier():
    """"$2.00/WEEK" is what the leads cost, not which ones they are."""
    assert agents.tier_of("$2.00/WEEK PHX STNDRD") == "standard"
    assert agents.tier_of("$2.0/WEEK PHX") is None


# --------------------------------- two things on one order


CATHERINE = """-- New Client Onboarded --

First Name: Catherine Y
Last Name: Barney
Phone: +14439398109
Email: catcat.barney@gmail.com
Package Selected: Text Verified
Lead Type: Text Verified Veteran Plus
Target Areas for Marketing: Name: Catherine Y Barney
Lead Type: Text-Verified Final Expense and Vets
Number of Requested Leads: 15 and 15
States Leads In: MD
Agency: tagteam
Discount Code: SCREAM

15 OTP VETS
15 OTP FEX
invincible agent
launch date is Saturday, August 29
"""


def test_two_things_ordered_are_both_read():
    """Catherine Y Barney paid for vets and FEX. She was filed under FEX."""
    assert agents.ordered_lead_types(CATHERINE) == ["15 OTP VETS", "15 OTP FEX"]


def test_both_go_on_the_line_that_names_the_order():
    assert agents.stated_orders(CATHERINE) == "15 OTP VETS + 15 OTP FEX"


def test_the_agent_carries_both():
    agent = agents.read_agent(
        card("New Agent - Catherine Y Barney"), text=CATHERINE, today=WEDNESDAY
    )
    assert agent.stated == "15 OTP VETS + 15 OTP FEX"


def test_a_correction_is_still_one_order():
    """Colton's two Lead Type fields name different families and he ordered once.

    The form's own field never stacks - only lines somebody typed do.
    """
    assert agents.ordered_lead_types(COLTON) == []
    assert agents.stated_orders(COLTON) == agents.stated_lead_type(COLTON)


def test_the_same_family_twice_is_a_rewording_not_a_second_purchase():
    """Aliana's card says Spanish IUL twice; the lower one is the current one."""
    assert agents.ordered_lead_types(ALIANA) == []
    assert agents.stated_orders(ALIANA) == "14 Spanish OTP IUL"


def test_one_order_is_unchanged_everywhere():
    for said in (REAL, SPARK, BENJI, EVAN):
        assert agents.ordered_lead_types(said) == []
        assert agents.stated_orders(said) == agents.stated_lead_type(said)


def test_two_orders_land_on_two_lead_order_checklists():
    """One line on the Lead Order card is half an order."""
    from wilbyte.bot import jobs

    agent = agents.read_agent(
        card("New Agent - Catherine Y Barney"), text=CATHERINE, today=date(2026, 8, 29)
    )
    held = [
        {"id": "l0", "name": "OTP VETS", "checkItems": []},
        {"id": "l1", "name": "OTP FEX", "checkItems": []},
    ]
    plan = jobs._plan_for(
        Stub(held), agent, day=date(2026, 8, 29), tomorrow=date(2026, 8, 30),
        dated={
            "lead_order": {"id": "lo", "name": "Lead Order 08/29/26"},
            "ads": {"id": "ad", "name": "📊 Ads 08/29/26"},
            "ops": {"id": "op", "name": "💻 Ops 08/29/26"},
        },
        every_card=[],
    )

    assert plan.problems == []
    landed = {
        step.checklist for step in plan.steps
        if step.card_title == "Lead Order 08/29/26"
    }
    assert landed == {"OTP VETS", "OTP FEX"}


# --------------------------------- the setup card's agents onto the Lead Order


def _item(url, label):
    return {"name": f"{url} {label}"}


def _person(name, items):
    return {"id": name, "name": name, "checkItems": [_item(u, l) for u, l in items]}


# Friday 08/28's setup card, as it actually reads: the same list under each of
# the three people who do the setting up.
FRIDAY = [
    _person(person, [
        ("https://trello.com/c/aaa", "UPRISE PHX PLUS"),
        ("https://trello.com/c/bbb", "Phoenix Standard"),
        ("https://trello.com/c/ccc", "Text Verified Veteran Plus"),
    ])
    for person in agents.SETUP_PEOPLE
]


def test_the_agents_are_read_once_not_once_per_person():
    """Therese, Kathleen and Nicole each get the same list."""
    assert agents.setup_agents(FRIDAY) == [
        ("https://trello.com/c/aaa", "UPRISE PHX PLUS"),
        ("https://trello.com/c/bbb", "Phoenix Standard"),
        ("https://trello.com/c/ccc", "Text Verified Veteran Plus"),
    ]


def test_a_line_splits_into_the_card_and_what_it_says():
    assert agents.split_item("https://trello.com/c/aaa 25 OTP VETS") == (
        "https://trello.com/c/aaa", "25 OTP VETS"
    )
    assert agents.split_item("just words") == ("", "just words")


def test_each_agent_lands_under_the_lead_type_they_bought():
    """Each lands on the checklist for what they bought, Standard included."""
    order = [
        {"id": "1", "name": "PHNX PLUS", "checkItems": []},
        {"id": "2", "name": "PHNX STANDARD", "checkItems": []},
        {"id": "3", "name": "OTP VET Plus", "checkItems": []},
    ]
    spreads, problems = agents.plan_spread(FRIDAY, order)

    assert problems == []
    assert [(s.url[-3:], s.checklist) for s in spreads] == [
        ("aaa", "PHNX PLUS"),
        ("bbb", "PHNX STANDARD"),
        ("ccc", "OTP VET Plus"),
    ]
    assert not any(s.make_checklist for s in spreads)


def test_an_agent_already_on_the_lead_order_card_is_not_doubled():
    """The same-day path may have put them there hours earlier."""
    order = [
        {"id": "1", "name": "PHNX PLUS",
         "checkItems": [_item("https://trello.com/c/aaa", "UPRISE PHX PLUS")]},
        {"id": "3", "name": "OTP VET Plus", "checkItems": []},
    ]
    spreads, _ = agents.plan_spread(FRIDAY, order)

    assert "https://trello.com/c/aaa" not in [s.url for s in spreads]
    assert len(spreads) == 2


def test_a_lead_type_with_no_checklist_yet_gets_one_made():
    order = [{"id": "1", "name": "PHNX PLUS", "checkItems": []}]
    spreads, _ = agents.plan_spread(FRIDAY, order)

    made = {s.checklist: s.make_checklist for s in spreads}
    assert made["PHNX PLUS"] is False
    assert all(value for name, value in made.items() if name != "PHNX PLUS")


def test_nothing_ordered_is_swept_into_own_setup():
    """A card with no checklist for it yet still files by what was bought."""
    spreads, _ = agents.plan_spread(FRIDAY, [])
    landed = {s.url[-3:]: s.checklist for s in spreads}

    assert landed["aaa"] == "UPRISE PHX PLUS"
    assert landed["bbb"] == "Phoenix Standard"
    assert landed["ccc"] == "Text Verified Veteran Plus"


def test_the_self_setup_ones_all_go_on_the_one_checklist():
    setup = [_person("Nicole", [("https://trello.com/c/ddd", "25 Instant FB leads")])]
    order = [{"id": "1", "name": agents.OWN_SETUP, "checkItems": []}]
    spreads, _ = agents.plan_spread(setup, order)

    assert spreads[0].checklist == agents.OWN_SETUP
    assert spreads[0].make_checklist is False


def test_a_line_that_never_said_what_the_leads_are_is_reported():
    """Filing it somewhere invented is worse than saying it can't be placed."""
    setup = [_person("Nicole", [("https://trello.com/c/eee", "")])]
    spreads, problems = agents.plan_spread(setup, [])

    assert spreads == []
    assert len(problems) == 1
    assert "https://trello.com/c/eee" in problems[0]


def test_the_line_written_keeps_what_the_setup_card_said():
    """Not the checklist's name: "25 OTP VETS" says the count, "OTP VETS" doesn't."""
    order = [{"id": "3", "name": "OTP VET Plus", "checkItems": []}]
    setup = [_person("Nicole", [("https://trello.com/c/fff", "25 OTP Vets")])]
    spreads, _ = agents.plan_spread(setup, order)

    assert agents.checklist_item(spreads[0].url, spreads[0].label) == (
        "https://trello.com/c/fff 25 OTP Vets"
    )


# --------------------------------- two orders, spread onto two checklists


CATHERINE_LINE = "https://trello.com/c/ggg"


def test_one_line_carrying_two_orders_becomes_two():
    """The setup card gives an agent one line; the Lead Order card needs two."""
    setup = [
        _person(person, [(CATHERINE_LINE, "15 OTP VETS + 15 OTP FEX")])
        for person in agents.SETUP_PEOPLE
    ]
    order = [
        {"id": "1", "name": "OTP VETS", "checkItems": []},
        {"id": "2", "name": "OTP FEX", "checkItems": []},
    ]
    spreads, problems = agents.plan_spread(setup, order)

    assert problems == []
    assert [(s.checklist, s.label) for s in spreads] == [
        ("OTP VETS", "15 OTP VETS"),
        ("OTP FEX", "15 OTP FEX"),
    ]


def test_each_line_says_only_its_own_order():
    """"15 OTP VETS + 15 OTP FEX" under OTP FEX reads as if she bought 30."""
    setup = [_person("Nicole", [(CATHERINE_LINE, "15 OTP VETS + 15 OTP FEX")])]
    spreads, _ = agents.plan_spread(setup, [])

    assert all(agents.ORDER_JOIN not in s.label for s in spreads)


def test_half_an_order_already_filed_still_gets_its_other_half():
    setup = [_person("Nicole", [(CATHERINE_LINE, "15 OTP VETS + 15 OTP FEX")])]
    order = [
        {"id": "1", "name": "OTP VETS",
         "checkItems": [_item(CATHERINE_LINE, "15 OTP VETS")]},
        {"id": "2", "name": "OTP FEX", "checkItems": []},
    ]
    spreads, _ = agents.plan_spread(setup, order)

    assert [(s.checklist, s.label) for s in spreads] == [("OTP FEX", "15 OTP FEX")]


def test_an_agent_listed_twice_on_the_setup_card_keeps_both():
    """Somebody who bought two things gets two lines as often as one."""
    setup = [_person("Nicole", [
        (CATHERINE_LINE, "15 OTP VETS"),
        (CATHERINE_LINE, "15 OTP FEX"),
    ])]
    assert agents.setup_agents(setup) == [(CATHERINE_LINE, "15 OTP VETS + 15 OTP FEX")]


def test_the_three_copies_still_collapse_to_one_order():
    setup = [
        _person(person, [(CATHERINE_LINE, "15 OTP VETS")])
        for person in agents.SETUP_PEOPLE
    ]
    assert agents.setup_agents(setup) == [(CATHERINE_LINE, "15 OTP VETS")]


def test_a_lead_type_with_and_in_its_name_is_not_split():
    """"Text-Verified Final Expense and Vets" is one thing, not two."""
    assert agents.order_parts("Text-Verified Final Expense and Vets") == [
        "Text-Verified Final Expense and Vets"
    ]


def test_two_orders_needing_two_new_checklists_make_two():
    setup = [_person("Nicole", [(CATHERINE_LINE, "15 OTP VETS + 15 OTP FEX")])]
    spreads, _ = agents.plan_spread(setup, [])

    assert [s.make_checklist for s in spreads] == [True, True]
    assert len({s.checklist for s in spreads}) == 2


def test_two_agents_wanting_the_same_new_checklist_only_make_it_once():
    setup = [_person("Nicole", [
        ("https://trello.com/c/hhh", "25 OTP VETS"),
        ("https://trello.com/c/iii", "25 OTP VETS"),
    ])]
    spreads, _ = agents.plan_spread(setup, [])

    assert [s.make_checklist for s in spreads] == [True, False]


def test_an_agent_who_ordered_two_things_is_not_raised_as_wrong():
    """Catherine Y Barney bought vets and FEX. Set up on vets and compared
    against FEX alone, she was flagged when she was right - and either half
    being set up first is correct, so there is nothing to compare against."""
    ordered = agents.stated_orders(CATHERINE)

    assert ordered == "15 OTP VETS + 15 OTP FEX"
    for confirmed in (
        "OTP VET ON DISTRO HUB setup is complete for CATHERINE BARNEY",
        "OTP FEX ON DISTRO HUB setup is complete for CATHERINE BARNEY",
    ):
        assert agents.wrong_setup(ordered, [confirmed]) is None


def test_a_single_order_is_still_checked():
    """The point of the check survives: one order against a different one."""
    said = ["OTP FEX ON DISTRO HUB setup is complete"]
    assert agents.wrong_setup("30 OTP Vets", said) == ("30 OTP Vets", "OTP FEX")


# --------------------------------- abbreviations that reached the board


LEAD_ORDER_ON_THE_BOARD = [
    "OTP IUL Plus", "OTP VET Plus", "OTP Widows", "OTP FEX", "OTP MTG Plus",
    "OTP Blue Collar IUL", "OTP Spanish IUL", "own setup",
    "PHNX PLUS", "PHNX STANDARD",
]


@pytest.mark.parametrize(
    "said,wanted",
    [
        ("22 OTP BC", "OTP Blue Collar IUL"),
        ("22 OTP BC leads", "OTP Blue Collar IUL"),
        ("Text-Verified Blue Collar IUL Leads", "OTP Blue Collar IUL"),
        ("25 OTP SIUL", "OTP Spanish IUL"),
        ("50 Spanish OTP IUL", "OTP Spanish IUL"),
        ("36 OTP IUL", "OTP IUL Plus"),
        ("30 WIDOW", "OTP Widows"),
        ("Uprise 30 OTP VET PLUS", "OTP VET Plus"),
    ],
)
def test_the_short_ways_of_writing_a_lead_type_all_land(said, wanted):
    """"22 OTP BC" and "25 OTP SIUL" became checklists of their own on the real
    board, because neither abbreviation was known."""
    assert agents.match_checklist(
        said, LEAD_ORDER_ON_THE_BOARD, tier=agents.tier_of(said)
    ) == wanted


def test_bc_and_siul_are_iul_products():
    assert agents.family_of("22 OTP BC") == "iul"
    assert agents.family_of("25 OTP SIUL") == "iul"
    assert "blue collar" in agents.qualifiers_of("22 OTP BC")
    assert "spanish" in agents.qualifiers_of("25 OTP SIUL")


def test_an_unplaceable_agent_is_reported_rather_than_given_a_new_checklist():
    """The checklists on a Lead Order card are put there by hand. A spread that
    makes its own leaves "22 OTP BC" on the board as though it were a product."""
    setup = [_person("Nicole", [("https://trello.com/c/zzz", "40 Something Unheard Of")])]
    spreads, _problems = agents.plan_spread(setup, [{"id": "1", "name": "OTP IUL Plus",
                                                     "checkItems": []}])
    # The plan still names where it *would* go; the writer is what refuses.
    assert spreads[0].make_checklist is True


# --------------------------------- one order written twice, two ways


TYLER = """veteran-widows — 10 leads · one-time pack
A buyer just purchased on Spark. Fulfill this order from your Spark vendor dashboard.

Buyer
Tyler Menge

Lead type
veteran-widows

Order
10 leads · one-time pack

Price / lead
$22

States
AZ, CA, IN, KY, MD, ME, MI, NV, SC, TX, VA, WA, WI

Buyer email
tydmenge@gmail.com

Routing token
lw_3e3db8ccb366c570e7639fd8bb41625ccd033345405de949

OTP Widows
Live Tuesday, September 1
"""


def test_the_same_order_in_two_wordings_is_one_order():
    """Spark writes "veteran-widows"; the team writes "OTP Widows". Ten leads,
    once. It read as two because "veteran-widows" names vet and widows both,
    and one of the two was picked to compare against the other line."""
    assert agents.ordered_lead_types(TYLER) == []
    assert agents.stated_orders(TYLER) == "OTP Widows"


def test_families_overlapping_at_all_is_one_order():
    assert agents.families_in("veteran-widows") == {"vet", "widows"}
    assert agents.families_in("OTP Widows") == {"widows"}


def test_two_genuinely_different_orders_still_stack():
    """Catherine bought vets and FEX - no family in common, so two."""
    assert agents.ordered_lead_types(CATHERINE) == ["15 OTP VETS", "15 OTP FEX"]


def test_the_whole_description_is_read_not_just_the_bottom():
    """The Spark block at the top and the team's line at the bottom are both
    about the same purchase, and the answer has to account for both."""
    agent = agents.read_agent(
        card("New Agent - Tyler Menge"), text=TYLER, today=date(2026, 8, 31)
    )
    assert agent.stated == "OTP Widows"
    assert agent.launch == date(2026, 9, 1)


# --------------------------------- a date somebody declined to pick


MARIA = """-- New Client Onboarded --

First Name: Maria Fernanda
Last Name: Gomez
Phone: +14244404580
Email: mafegomezmacias@hotmail.com
Package Selected: Text Verified
Lead Type: Index Universal Life
Target Areas for Marketing:Maria Fernanda Gomez
40 OTP Spanish IUL
States: All Major except PR, AK, HI
Internal: She's on suscription
Launch Date: As soon as is possibe for the team.
Please mark as priority and deliver all 40 leads within 5-6 days max
"""


def test_as_soon_as_possible_is_today():
    """A date somebody declined to pick means the first day anybody can do it.
    Read as no date at all it parks the agent waiting for a day nobody is ever
    going to write."""
    agent = agents.read_agent(
        card("New Agent - Maria Fernanda Gomez"), text=MARIA, today=WEDNESDAY
    )
    assert agent.launch == WEDNESDAY
    assert agent.stated == "40 OTP Spanish IUL"


@pytest.mark.parametrize(
    "said",
    [
        "Launch Date: As soon as is possible for the team",
        "Launch Date: as soon as is possibe for the team.",
        "Launch Date: ASAP",
        "launch date: as soon as you can",
        "launch date: as soon as we can",
        "launch right away",
    ],
)
def test_every_way_of_declining_to_pick_a_day(said):
    assert agents.find_launch(said, today=WEDNESDAY) == WEDNESDAY


def test_a_real_date_still_wins_over_asap():
    """"ASAP, and definitely by Thursday" names a day, and the day is the answer."""
    said = "Launch Date: ASAP\nLaunch date is Thursday, August 27"
    assert agents.find_launch(said, today=WEDNESDAY) == date(2026, 8, 27)


def test_a_sentence_about_speed_that_is_not_a_launch_date_is_left_alone():
    """"deliver all 40 leads within 5-6 days max" is not a launch date."""
    assert agents.find_launch(
        "Please mark as priority and deliver all 40 leads within 5-6 days max",
        today=WEDNESDAY,
    ) is None


# --------------------------------- one line per agent, whatever the copies say


def test_the_same_card_written_two_ways_is_one_agent():
    """Trello writes the link both with the slug and without, so comparing
    whole links reads one agent as two and files them twice."""
    assert agents.card_key("https://trello.com/c/tIA7tp2B") == (
        agents.card_key("https://trello.com/c/tIA7tp2B/431-new-agent-jorge-flores")
    )


def test_three_copies_of_a_list_still_add_one_line_each():
    """The point of reading all three checklists is not to miss anybody - not
    to file everybody three times."""
    order = [
        {"id": "1", "name": "PHNX PLUS", "checkItems": []},
        {"id": "2", "name": "PHNX STANDARD", "checkItems": []},
        {"id": "3", "name": "OTP VET Plus", "checkItems": []},
    ]
    spreads, _ = agents.plan_spread(FRIDAY, order)

    urls = [agents.card_key(spread.url) for spread in spreads]
    assert len(urls) == len(set(urls)) == 3


def test_an_agent_already_on_the_card_with_a_slugged_link_is_not_added_again():
    """The line RYTE wrote yesterday and the one on the setup card are the same
    card written differently. Before this, a second spread doubled every line."""
    order = [{
        "id": "1", "name": "PHNX PLUS",
        "checkItems": [_item(
            "https://trello.com/c/aaa/12-new-agent-someone", "UPRISE PHX PLUS"
        )],
    }]
    spreads, _ = agents.plan_spread(FRIDAY[:1], order)

    assert agents.card_key("https://trello.com/c/aaa") not in [
        agents.card_key(spread.url) for spread in spreads
    ]


def test_running_the_spread_twice_adds_nothing_the_second_time():
    order = [
        {"id": "1", "name": "PHNX PLUS", "checkItems": []},
        {"id": "2", "name": "PHNX STANDARD", "checkItems": []},
        {"id": "3", "name": "OTP VET Plus", "checkItems": []},
    ]
    first, _ = agents.plan_spread(FRIDAY, order)

    # ...as the card would read afterwards.
    for spread in first:
        for checklist in order:
            if checklist["name"].casefold() == spread.checklist.casefold():
                checklist["checkItems"].append(_item(spread.url, spread.label))

    again, _ = agents.plan_spread(FRIDAY, order)
    assert again == []


def test_a_two_order_agent_still_gets_both_lines():
    """Filed under one of them is not filed."""
    setup = [_person("Nicole", [
        ("https://trello.com/c/ddd", "25 OTP VETS + 15 OTP FEX"),
    ])]
    order = [
        {"id": "1", "name": "OTP VETS", "checkItems": []},
        {"id": "2", "name": "OTP FEX", "checkItems": []},
    ]
    spreads, _ = agents.plan_spread(setup, order)
    assert len(spreads) == 2


# --------------------------------- a note about the agent is not an order


@pytest.mark.parametrize(
    "said,wanted",
    [
        ("Uprise Agent", False),
        ("uprise agents", False),
        ("Ascend Agency", False),
        ("Imperial Financial", False),
        ("5 OTP VETS", True),
        ("OTP VET Plus", True),
        # Names leads, so it is an order however it is written.
        ("Uprise 5 OTP VETS", True),
    ],
)
def test_what_counts_as_something_bought(said, wanted):
    assert agents.an_order(said) is wanted


def test_an_agency_note_beside_a_real_order_is_dropped():
    """Jack Duval's card says "5 OTP VETS" and "Uprise Agent". The second is
    who he is under, and filing it put an unplaceable line on the board."""
    assert agents.order_parts("5 OTP VETS + Uprise Agent") == ["5 OTP VETS"]


def test_a_line_that_is_only_a_note_is_still_reported():
    """Dropping it silently would lose an agent nobody could then place."""
    assert agents.order_parts("Uprise Agent") == ["Uprise Agent"]


def test_the_agent_gets_one_line_not_two():
    setup = [_person("Nicole", [
        ("https://trello.com/c/jack", "5 OTP VETS + Uprise Agent"),
    ])]
    order = [{"id": "1", "name": "OTP VETS", "checkItems": []}]

    spreads, problems = agents.plan_spread(setup, order)

    assert [spread.checklist for spread in spreads] == ["OTP VETS"]
    assert problems == []


# --------------------------------- the line reads like an order, not a sentence


@pytest.mark.parametrize(
    "typed,wanted",
    [
        # Real lines off this week's cards.
        ("let's do 40 Text-Verified Veteran Leads", "40 Text-Verified Veteran Leads"),
        ("40 OTP Vets - RECORD discount", "40 OTP Vets"),
        ("OTP Vets - 25 OTP Vets", "25 OTP Vets"),
        ("30 OTP Blue Collar IUL UNSIGNED", "30 OTP Blue Collar IUL"),
        ("we'll do 20 Basic Spanish IUL", "20 Basic Spanish IUL"),
        # The count stays: it is what whoever loads the leads needs.
        ("15 OTP VETS", "15 OTP VETS"),
        # Uprise says which product it is, so it survives everything.
        ("Uprise 5 OTP VETS", "Uprise 5 OTP VETS"),
        ("30 OTP VET PLUS - uprise", "30 OTP VET PLUS - uprise"),
        # The form's own label still comes off, as before.
        ("Lead Type: OTP VETS", "OTP VETS"),
    ],
)
def test_the_sales_talk_comes_off_the_line(typed, wanted):
    assert agents.tidy_lead_type(typed) == wanted


def test_a_note_never_eats_the_whole_order():
    """Stripping is only ever allowed to leave something behind."""
    assert agents.tidy_lead_type("unsigned") == "unsigned"
    assert agents.tidy_lead_type("RECORD discount") == "RECORD discount"


def test_the_tidied_line_is_what_lands_on_the_checklist():
    card = """-- New Client Onboarded --

Name: Jonathan Carlos
Lead Type: let's do 40 Text-Verified Veteran Leads

Live Wednesday, September 2
"""
    assert agents.stated_lead_type(card) == "40 Text-Verified Veteran Leads"


@pytest.mark.parametrize(
    "typed,wanted",
    [
        # Whoever typed it introduced themselves first.
        ("Tommy Vereau here paid for OTP vets", "OTP vets"),
        ("Julio wants 25 OTP Spanish IUL", "25 OTP Spanish IUL"),
        ("Dapper Life bought OTP Widows", "OTP Widows"),
        ("ordered 40 OTP Vets", "40 OTP Vets"),
        # Nothing to introduce, nothing taken off.
        ("Text-Verified Veteran Leads", "Text-Verified Veteran Leads"),
        ("25 OTP Vets", "25 OTP Vets"),
    ],
)
def test_who_bought_it_comes_off_the_line(typed, wanted):
    assert agents.tidy_lead_type(typed) == wanted


def test_the_agent_is_still_filed_under_the_right_leads():
    card = """-- New Client Onboarded --

Name: Tommy Vereau
Lead Type: Tommy Vereau here paid for OTP vets

Live Thursday, September 3
"""
    said, landed, _ = agents.best_lead_type(card, ["OTP VETS", "OTP FEX"])
    assert said == "OTP vets"
    assert landed == "OTP VETS"


# --------------------------------- a top-up has no launch date, and goes today


SALAS = """-- New Client Onboarded --

First Name: Sebastian
Last Name: Salas
Phone: +954-907-1479
Package Selected: Text Verified
Lead Type: Vets
Target Areas for Marketing: TX, TN, OH, VA, CO, AZ, NM, FL, NC

50 Text Verified Veteran leads

add it to his current once fulfilled

AEP SERVER
"""


def test_a_top_up_goes_on_todays_cards():
    """"add it to his current once fulfilled" says when: as soon as it is set
    up. Read as a missing date, it left fifty veteran leads waiting."""
    assert agents.find_launch(SALAS, today=date(2026, 9, 1)) == date(2026, 9, 1)


@pytest.mark.parametrize(
    "said",
    [
        "add it to his current once fulfilled",
        "add these to his active order",
        "add to her existing batch",
        "once fulfilled add to his current",
        "after his current is done",
    ],
)
def test_the_ways_the_team_writes_a_top_up(said):
    assert agents.find_launch(f"50 OTP VETS\n\n{said}\n", today=date(2026, 9, 1)) == (
        date(2026, 9, 1)
    )


def test_a_card_with_no_date_and_no_top_up_still_asks():
    """A date somebody forgot is not the same as one that doesn't exist. That
    one is still a question."""
    card = "50 OTP VETS\n\nTarget Areas: TX, FL\n"
    assert agents.find_launch(card, today=date(2026, 9, 1)) is None


def test_a_written_date_still_beats_the_top_up_wording():
    card = "50 OTP VETS\n\nadd to his current\n\nLive Friday, September 4\n"
    assert agents.find_launch(card, today=date(2026, 9, 1)) == date(2026, 9, 4)


# --------------------------------- a trucker is its own product


MARCEL = """-- New Client Onboarded --

First Name: Marcel
Last Name: Trifan
Package Selected: Text Verified
Lead Type: Indexed Universal Life
Target Areas for Marketing:

GA,MO,NC,AL,KS,KY,LA,MA,MD,MI,MS,NV,OK,PA,SC,TN,TX,UT,VA,WA,WI,OR
30 LEADS
TEXT VERIFIED TRUCKER IUL

Marcel got the OTP Truckers with early access discount as well
Get him started later today please - Launch date is later today, September 2
"""

TRUCKER_BOARD = ["OTP IUL Plus", "OTP IUL TRUCKER", "OTP Blue Collar IUL", "own setup"]


def test_a_trucker_lands_on_the_trucker_checklist():
    """"TEXT VERIFIED TRUCKER IUL" read as either IUL Plus or IUL TRUCKER and
    had to be asked about. It is always the trucker one."""
    _said, landed, _could = agents.best_lead_type(MARCEL, TRUCKER_BOARD)
    assert landed == "OTP IUL TRUCKER"


@pytest.mark.parametrize(
    "said",
    ["TEXT VERIFIED TRUCKER IUL", "30 OTP Truckers", "trucker iul", "40 OTP TRUCKER IUL"],
)
def test_the_ways_a_trucker_order_is_written(said):
    assert agents.match_checklist(
        said, TRUCKER_BOARD, tier=agents.tier_of(said)
    ) == "OTP IUL TRUCKER"


def test_a_trucker_is_an_iul_even_when_the_card_never_says_iul():
    """"OTP Truckers" names the family on its own, the way SIUL and BC do."""
    assert "iul" in agents.families_in("30 OTP Truckers")


def test_plain_iul_is_still_plain_iul():
    """The point of the qualifier is that it separates two checklists, not
    that it swallows one."""
    assert agents.match_checklist(
        "25 text verified iul", TRUCKER_BOARD, tier="plus"
    ) == "OTP IUL Plus"


# --------------------------------- a typo is not a different order


@pytest.mark.parametrize(
    "typed,meant",
    [
        ("OTP TRCUKER IUL", "OTP Trucker IUL"),
        ("OTP Spansih IUL", "OTP Spanish IUL"),
        ("Blue Colalr IUL", "Blue Collar IUL"),
        ("Text Veriifed VET", "Text Verified VET"),
    ],
)
def test_a_misspelling_is_the_same_order(typed, meant):
    """Tommy Mortillaro was flagged as set up on the wrong leads. He wasn't -
    somebody typed TRCUKER."""
    assert agents.shape_of(typed) == agents.shape_of(meant)


def test_a_different_word_is_still_a_different_order():
    """Correcting letters that were never there is how a real mismatch gets
    read as a typo and nobody hears about it."""
    assert agents.shape_of("OTP VET Plus") != agents.shape_of("OTP FEX Plus")
    assert agents.shape_of("OTP Spanish IUL") != agents.shape_of("OTP Trucker IUL")
    assert agents.shape_of("OTP IUL Plus") != agents.shape_of("OTP IUL Standard")


def test_short_words_are_left_alone():
    """"vet" and "fex" are three letters and one apart from plenty of things."""
    assert agents.despell("OTP VET FEX MTG") == "OTP VET FEX MTG"


def test_only_the_same_letters_rearranged_count_as_a_typo():
    """"Standards" is not a misspelling of "Standard" that needs fixing, and
    "Spaniel" is not "Spanish"."""
    assert agents.despell("Spaniel leads") == "Spaniel leads"


# --------------------------------- two spellings of one checklist


TRUCKER_TWICE = ["OTP IUL TRUCKER", "OTP TRUCKERS", "OTP IUL Plus"]


def test_two_checklists_meaning_the_same_leads_are_not_a_question():
    """Marcel Trifan's card offered "OTP IUL TRUCKER" or "OTP TRUCKERS". They
    are the same product written twice, so asking which only asked somebody to
    pick a spelling."""
    landed = agents.match_checklist(
        "TEXT VERIFIED TRUCKER IUL", TRUCKER_TWICE, tier="plus"
    )
    assert landed == "OTP IUL TRUCKER"


def test_the_one_that_says_more_of_the_order_wins():
    assert agents.match_checklist("30 OTP Truckers", TRUCKER_TWICE, tier="plus") == (
        "OTP TRUCKERS"
    )


FEX_TWICE = ["OTP VETS/FEX", "OTP FEX"]


def test_an_order_that_tells_them_apart_by_nothing_is_a_question():
    """Kara E Williams bought Final Expense. Both of these are Final Expense
    to this module, her card names neither, and taking the first one on the
    board is a coin toss dressed up as a decision."""
    assert agents.match_checklist(
        "Text-Verified Final Expense", FEX_TWICE, tier="plus"
    ) is None


def test_two_names_equally_close_are_a_question_too():
    """The same words in a different order is the same distance from the order
    that was placed. There is nothing here to decide it on."""
    assert agents.match_checklist(
        "25 OTP FEX", ["OTP VETS/FEX", "FEX/VETS OTP"], tier="plus"
    ) is None


def test_naming_one_of_them_still_decides_it():
    """The point is not to stop deciding - it is to decide on evidence. Both
    of these are the order word for word; "OTP FEX" is closer because the
    other one brings vets along."""
    assert agents.match_checklist(
        "25 OTP FEX", FEX_TWICE, tier="plus"
    ) == "OTP FEX"


def test_a_real_choice_is_still_a_question():
    """Standard and Plus are two products. Picking one is somebody's money."""
    assert agents.match_checklist(
        "Phoenix Campaign", ["PHNX PLUS", "PHNX STANDARD"], tier=None
    ) is None


# --------------------------------- saying it again a few hours later


def test_a_waiting_card_is_raised_again_after_a_few_hours(tmp_path):
    """Said once, it lands while everyone is at lunch and the card waits all
    afternoon. Said every pass, nobody reads the channel by Wednesday."""
    from wilbyte import agentseen

    path = tmp_path / "said.json"
    agentseen.remember(["card-1"], path, now=1000.0)

    assert agentseen.due(["card-1"], path=path, now=1000.0) == []
    assert agentseen.due(["card-1"], path=path, now=1000.0 + 3600) == []
    assert agentseen.due(["card-1"], path=path, now=1000.0 + 3 * 3600) == ["card-1"]


def test_a_card_never_mentioned_is_always_due(tmp_path):
    from wilbyte import agentseen

    assert agentseen.due(["new-card"], path=tmp_path / "said.json") == ["new-card"]


def test_the_old_flat_list_does_not_announce_itself_on_upgrade(tmp_path):
    """Every card ever quietened shouting at once is not an upgrade."""
    import json

    from wilbyte import agentseen

    path = tmp_path / "said.json"
    path.write_text(json.dumps(["card-1", "card-2"]), encoding="utf-8")

    assert agentseen.due(["card-1"], path=path) == []


def test_forgetting_a_card_makes_it_due_again(tmp_path):
    from wilbyte import agentseen

    path = tmp_path / "said.json"
    agentseen.remember(["card-1"], path)
    agentseen.forget(["card-1"], path)

    assert agentseen.due(["card-1"], path=path) == ["card-1"]


# --------------------------------- how often a waiting card is raised


class Said:
    """A responder that keeps what it was told."""

    def __init__(self):
        self.messages = []

    async def send(self, content=None, *, embed=None, file=None, view=None):
        self.messages.append(content or "")


def stuck_plan(when, *, problems=(), launch=None, name="Jorge Arce"):
    agent = agents.Agent(
        name=name,
        card_id=f"card-{name.split()[0].lower()}",
        url="https://trello.com/c/MsF6rf9O",
        launch=launch,
    )
    return agents.AgentPlan(agent=agent, when=when, problems=list(problems))


def report(plans, tmp_path, monkeypatch, *, said_hours_ago=None):
    """Run one watcher pass and hand back what went to the channel."""
    import asyncio
    import time as clock

    from wilbyte import agentseen
    from wilbyte.bot import client

    path = tmp_path / "said.json"
    monkeypatch.setattr(agentseen, "SEEN_PATH", path)
    if said_hours_ago is not None:
        agentseen.remember(
            [plan.agent.card_id for plan in plans],
            path,
            now=clock.time() - said_hours_ago * 3600,
        )

    heard = Said()
    asyncio.run(client._report_stuck(heard, plans))
    return heard.messages


def test_a_launch_days_out_is_named_once_and_then_left_alone(tmp_path, monkeypatch):
    """Nothing about next Thursday needs doing this afternoon."""
    plans = [stuck_plan("later", launch=date(2026, 9, 10))]

    assert report(plans, tmp_path, monkeypatch) != []
    assert report(plans, tmp_path, monkeypatch, said_hours_ago=9) == []


def test_a_launch_tomorrow_comes_back_after_a_few_hours(tmp_path, monkeypatch):
    plans = [stuck_plan("tomorrow", problems=["No lead type"], launch=TUESDAY)]

    assert report(plans, tmp_path, monkeypatch, said_hours_ago=1) == []
    assert report(plans, tmp_path, monkeypatch, said_hours_ago=4) != []


def test_a_card_with_no_launch_date_is_chased_like_an_urgent_one(tmp_path, monkeypatch):
    """No date could mean today. That is not something to say once."""
    plans = [stuck_plan("unknown", problems=["No launch date"])]

    assert report(plans, tmp_path, monkeypatch, said_hours_ago=4) != []


def test_an_ahead_card_starts_being_chased_once_its_launch_arrives(
    tmp_path, monkeypatch
):
    """Said on Monday about a Thursday launch, and quiet since. On Wednesday
    the same card is tomorrow's problem and joins the every-few-hours list."""
    plans = [stuck_plan("tomorrow", problems=["No lead type"], launch=TUESDAY)]

    assert report(plans, tmp_path, monkeypatch, said_hours_ago=48) != []


def test_the_urgent_and_the_ahead_are_weighed_one_card_at_a_time(
    tmp_path, monkeypatch
):
    """Two cards said at the same time, hours ago: only the near one repeats."""
    plans = [
        stuck_plan("today", problems=["No lead type"], name="Travis Loukusa"),
        stuck_plan("later", launch=date(2026, 9, 10), name="Malcolm Edwards"),
    ]

    (message,) = report(plans, tmp_path, monkeypatch, said_hours_ago=9)

    assert "Travis Loukusa" in message
    assert "Malcolm Edwards" not in message


def test_a_card_waiting_on_a_setup_card_says_so(tmp_path, monkeypatch):
    """It had no problems, so the line was a name, a dash, and nothing."""
    plans = [stuck_plan("later", launch=date(2026, 9, 10))]

    (message,) = report(plans, tmp_path, monkeypatch)

    assert "Jorge Arce** — nothing to put them on yet" in message
    assert "Sep 10 setup card" in message


# --------------------------------- shorthand for a day


PARKER = """Name: Parker Marquis
Phone: +(951)850-2790
Email: Parkermarquis.ins@gmail.com
Lead type: otp widows

States: TN, NC, SC, VA

25 OTP WIDOWS
EVERLIFE LEADS

LIVE TOM. SEPT 3
"""


def test_a_full_stop_in_an_abbreviation_does_not_end_the_sentence():
    """"LIVE TOM. SEPT 3" was read as "LIVE TOM" - the date was sitting right
    there and never got looked at."""
    assert agents.find_launch(PARKER, today=date(2026, 9, 2)) == date(2026, 9, 3)


@pytest.mark.parametrize(
    "said,wanted",
    [
        ("Live tom.", date(2026, 9, 3)),
        ("live tmrw", date(2026, 9, 3)),
        ("Live tmr", date(2026, 9, 3)),
        ("Launch date: Sept. 8", date(2026, 9, 8)),
        ("Live Aug. 28", date(2026, 8, 28)),
        ("Live today", date(2026, 9, 2)),
    ],
)
def test_the_short_ways_of_writing_a_day(said, wanted):
    assert agents.find_launch(said, today=date(2026, 9, 2)) == wanted


def test_the_card_itself_is_not_rewritten():
    """Only the forms that mean a day. A card is somebody's words."""
    assert agents.spelled_out("25 OTP WIDOWS\nEVERLIFE LEADS") == (
        "25 OTP WIDOWS\nEVERLIFE LEADS"
    )
    assert "Tomlinson" in agents.spelled_out("Agent: Tomlinson")


# --------------------------------- the variant named, the family left unsaid


SPANISH_BOARD = ["OTP Spanish IUL", "OTP VET Plus", "OTP FEX", "own setup"]


def test_a_card_naming_only_the_variant_still_files():
    """Jose Darinel Garcia's line reads "25 OTP SPANISH". The checklist was
    sitting right there and the spread said nothing matched."""
    assert agents.match_checklist(
        "25 OTP SPANISH", SPANISH_BOARD, tier="plus"
    ) == "OTP Spanish IUL"


def test_two_of_that_variant_is_a_question_again():
    """Spanish IUL and Spanish FEX are two products. Picking one is a guess."""
    assert agents.match_checklist(
        "25 OTP SPANISH", ["OTP Spanish IUL", "Spanish FEX"], tier="plus"
    ) is None


def test_naming_the_family_still_beats_naming_the_variant():
    assert agents.match_checklist(
        "25 OTP VETS", SPANISH_BOARD, tier="plus"
    ) == "OTP VET Plus"


def test_a_variant_with_no_checklist_for_it_is_still_nothing():
    assert agents.match_checklist("30 blue collar", SPANISH_BOARD, tier="plus") is None


# --------------------------------- a launch date already gone by


def dated_plan(launch):
    agent = agents.Agent(
        name="Eduardo Munoz", card_id="c1", url="https://trello.com/c/x",
        lead_type="Basic/Instant Spanish IUL", launch=launch,
    )
    return agents.AgentPlan(agent=agent, when=agent.when(date(2026, 9, 3)))


def test_a_launch_date_already_past_says_so():
    """Eduardo Munoz's card carried "Ready to go live WED, AUG 26", copied
    from somebody else's card a week earlier. It was read as today, filed
    today, and nothing on the board looked wrong."""
    said = agents.describe([dated_plan(date(2026, 8, 26))], today=date(2026, 9, 3))

    assert "launching today" in said
    assert "Wed Aug 26" in said and "already past" in said


def test_a_launch_date_that_really_is_today_says_only_that():
    said = agents.describe([dated_plan(date(2026, 9, 3))], today=date(2026, 9, 3))

    assert "launching today" in said
    assert "already past" not in said


def test_it_is_still_filed_today_either_way():
    """A card that should have gone live and didn't is due now, not never."""
    assert dated_plan(date(2026, 8, 26)).when == "today"


def test_without_a_today_the_wording_is_unchanged():
    """Every other caller keeps the message it had."""
    said = agents.describe([dated_plan(date(2026, 8, 26))])

    assert "launching today" in said
    assert "already past" not in said


# --------------------------------- a copied card brings the old comments


EDUARDO = """-- New Client Onboarded --

First Name: Eduardo
Last Name: Munoz
Lead Type: Basic/Instant Spanish IUL
40 Spanish Basic Leads
Launch Date: Friday, Sept 04
"""

COPIED = """DEDICATED SPANISH IUL IF setup is complete for EDUARDO MUNOZ
Fired a test in the Discord channel and Google Sheet.
Ready to go live WED, AUG 26"""


def read_eduardo(comments, today=date(2026, 9, 3)):
    return agents.read_agent(
        {"name": "New Agent - Eduardo Munoz", "id": "c1", "shortUrl": "u"},
        text=EDUARDO, comments=comments, today=today,
    )


def test_a_stale_comment_never_beats_the_description():
    """These cards are copied from one agent to the next with the comments
    attached. Eduardo's carried a launch date from somebody else's card a week
    earlier, next to a description plainly saying Friday, Sept 04."""
    assert read_eduardo((COPIED,)).launch == date(2026, 9, 4)


def test_the_comments_are_still_read_when_the_description_is_silent():
    """It turns up in either, and which one is nobody's decision to make -
    but only when the description doesn't say."""
    silent = agents.read_agent(
        {"name": "New Agent - Eduardo Munoz", "id": "c1", "shortUrl": "u"},
        text=EDUARDO.replace("Launch Date: Friday, Sept 04", ""),
        comments=("Going live Sept 9",),
        today=date(2026, 9, 3),
    )

    assert silent.launch == date(2026, 9, 9)


def test_a_weekday_argument_in_a_stale_comment_is_not_raised():
    """The date came off the description, so the description is what gets
    argued with. A copied comment is not a disagreement."""
    assert read_eduardo(("live fri, aug 27",)).note == ""


# --------------------------------- three days on one setup card


SPAN = "Agent Setup Going Live Saturday-Monday 09/05-09/07"
ONE_DAY = "Agent Setup Going Live Thursday 09/03"


def test_a_card_covering_three_days_is_a_span():
    assert agents.spans_days(SPAN) is True
    assert agents.spans_days(ONE_DAY) is False
    assert agents.spans_days("Agent Setup Going Live") is False


def test_a_line_on_a_span_says_which_day():
    """Three days of agents on one checklist all look alike until you open
    each card to find out which is which."""
    said = agents.checklist_item(
        "https://trello.com/c/abc", "OTP Vets",
        day=agents.day_label(date(2026, 9, 5)),
    )

    assert said == "https://trello.com/c/abc OTP Vets SATURDAY"


def test_a_line_on_a_one_day_card_does_not():
    """Everything on Tuesday's card goes live Wednesday. Saying so on every
    line is noise."""
    assert agents.checklist_item("https://trello.com/c/abc", "OTP Vets") == (
        "https://trello.com/c/abc OTP Vets"
    )


def test_the_day_is_left_off_when_there_is_no_launch_date():
    assert agents.day_label(None) == ""
    assert agents.checklist_item("u", "OTP Vets", day="") == "u OTP Vets"
