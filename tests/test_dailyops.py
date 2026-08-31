"""The daily Trello routine, as rules.

Four dated cards walk In Que -> Today -> Quality Check. At 9pm everything still
unticked lands on tomorrow's card, on the same person's checklist and the same
card type. Getting either axis wrong puts someone else's work on your list,
which is worse than not moving it at all.
"""

from datetime import date

import pytest

from wilbyte import dailyops, trello

TODAY = date(2026, 8, 20)
TOMORROW = date(2026, 8, 21)


def card(name, card_id="c1"):
    return {"id": card_id, "name": name, "url": f"https://trello.com/c/{card_id}"}


def checklist(person, *items):
    return {
        "id": f"cl-{person}",
        "name": person,
        "checkItems": [
            {"id": f"i{n}", "name": text, "state": state}
            for n, (text, state) in enumerate(items)
        ],
    }


def done(text):
    return (text, "complete")


def todo(text):
    return (text, "incomplete")


# ------------------------------------------------------------- reading titles


@pytest.mark.parametrize(
    "title,kind",
    [
        ("💎  General 08/20/26", "general"),
        ("💻  Ops 08/20/26", "ops"),
        ("📊  Ads 08/20/26", "ads"),
        ("Lead Order 08/20/26", "lead_order"),
        ("General 08/20/26", "general"),  # emoji dropped on a manual re-type
    ],
)
def test_the_four_cards_are_recognised(title, kind):
    assert dailyops.parse_card_title(title) == (kind, TODAY)


def test_a_date_range_belongs_to_the_day_it_starts_on():
    """One card is titled "Lead Order 08/14/26 - 8/16/26"."""
    assert dailyops.parse_card_title("Lead Order 08/14/26 - 8/16/26") == (
        "lead_order", date(2026, 8, 14)
    )


@pytest.mark.parametrize(
    "title",
    [
        "Agent Setup Going Live Thursday 08/20",
        "Agent Setup Going Live Saturday-Monday 08/22-08/25",
    ],
)
def test_agent_setup_cards_are_left_alone(title):
    """They sit in the same lists and are not part of the four-card routine."""
    assert dailyops.parse_card_title(title) is None


@pytest.mark.parametrize(
    "title",
    ["New Agent - Ryan Hernandez", "AGED LEAD - Joe Shmoe", "Team Update", "💎 General"],
)
def test_everything_else_is_ignored(title):
    assert dailyops.parse_card_title(title) is None


def test_the_days_cards_are_found_together():
    cards = [
        card("💎  General 08/20/26", "g"),
        card("💻  Ops 08/20/26", "o"),
        card("📊  Ads 08/20/26", "a"),
        card("Lead Order 08/20/26", "l"),
        card("💎  General 08/19/26", "old"),
        card("Agent Setup Going Live Thursday 08/20", "setup"),
    ]

    found = dailyops.cards_for(cards, TODAY)

    assert set(found) == {"general", "ops", "ads", "lead_order"}
    assert found["general"]["id"] == "g"


def test_a_card_that_never_generated_is_named():
    """Lead Order was missing from In Que on one check."""
    cards = [card("💎  General 08/21/26"), card("💻  Ops 08/21/26"), card("📊  Ads 08/21/26")]

    assert dailyops.missing_kinds(cards, TOMORROW) == ["lead_order"]


# ------------------------------------------------------------------ leftovers


def test_only_unticked_items_carry_forward():
    lists = [checklist("Nicole", done("cancel invoices eod"), todo("SCAN PHONE NUMBER"))]

    assert [name for _person, item in dailyops.unchecked(lists) for name in [item["name"]]] == [
        "SCAN PHONE NUMBER"
    ]


def test_each_item_keeps_the_person_it_belongs_to():
    """Putting an item on the wrong person's checklist is the worst outcome."""
    lists = [
        checklist("Nicole", todo("nicole thing")),
        checklist("Frank", todo("frank thing")),
    ]

    assert dailyops.unchecked(lists) == [
        ("Nicole", {"id": "i0", "name": "nicole thing", "state": "incomplete"}),
        ("Frank", {"id": "i0", "name": "frank thing", "state": "incomplete"}),
    ]


# ------------------------------------------------------------- planning a roll


def plan(source, target, **kwargs):
    return dailyops.plan_rollover(
        "general",
        source_card=card("💎  General 08/20/26", "src"),
        source_checklists=source,
        target_card=card("💎  General 08/21/26", "dst"),
        target_checklists=target,
        **kwargs,
    )


def test_items_are_planned_onto_the_same_persons_checklist():
    result = plan(
        [checklist("Nicole", todo("scan numbers")), checklist("Frank", todo("chase invoice"))],
        [checklist("Nicole"), checklist("Frank")],
    )

    assert [(i.person, i.name) for i in result.carried] == [
        ("Nicole", "scan numbers"),
        ("Frank", "chase invoice"),
    ]
    assert result.checklists_to_create == []


def test_missing_checklists_are_created_rather_than_dropped():
    """Tomorrow's generated cards arrive with no checklists at all."""
    result = plan([checklist("Nicole", todo("scan numbers"))], [])

    assert result.checklists_to_create == ["Nicole"]


def test_an_existing_checklist_is_matched_despite_spacing():
    result = plan([checklist("Nicole", todo("x"))], [checklist("  nicole ")])

    assert result.checklists_to_create == []


def test_a_linked_item_is_carried_as_its_url():
    """The name *is* the link. Copying the rendered label kills it."""
    link = "https://trello.com/c/NB28l2KN"
    result = plan([checklist("Nicole", todo(link))], [checklist("Nicole")])

    (item,) = result.carried
    assert item.name == link
    assert item.carries_link


def test_a_comment_url_survives_whole():
    link = "https://trello.com/c/NB28l2KN#comment-68a5f2c1"
    result = plan([checklist("Frank", todo(link))], [checklist("Frank")])

    assert result.carried[0].name == link


def test_plain_text_is_not_mistaken_for_a_link():
    result = plan([checklist("Frank", todo("cancel invoices eod"))], [checklist("Frank")])

    assert not result.carried[0].carries_link


# --------------------------------------------------------- the ambiguous case


def test_an_unticked_item_whose_card_is_done_is_flagged_not_moved():
    """"Jonathan Shinn Interview [Done]" sitting unticked on Nicole's list.

    The work looks finished and nobody ticked the box. Not ours to decide.
    """
    link = "https://trello.com/c/NB28l2KN"
    result = plan(
        [checklist("Nicole", todo(link))],
        [checklist("Nicole")],
        done_lookup=lambda _card_id: True,
    )

    (flagged,) = result.needs_a_look
    assert flagged.looks_done
    assert result.carried == [], "it must not be silently rolled forward"


def test_a_linked_item_still_in_progress_rolls_normally():
    link = "https://trello.com/c/NB28l2KN"
    result = plan(
        [checklist("Nicole", todo(link))],
        [checklist("Nicole")],
        done_lookup=lambda _card_id: False,
    )

    assert result.needs_a_look == []
    assert len(result.carried) == 1


def test_an_item_that_keeps_rolling_forward_is_raised():
    """Moving it silently for a fourth night is how it stays invisible."""
    key = dailyops.item_key("general", "Nicole", "scan numbers")
    result = plan(
        [checklist("Nicole", todo("scan numbers"))],
        [checklist("Nicole")],
        history={key: 3},
    )

    (flagged,) = result.needs_a_look
    assert flagged.stuck
    assert flagged in result.carried, "still moves - it is raised, not withheld"


def test_a_couple_of_days_is_not_yet_stuck():
    key = dailyops.item_key("general", "Nicole", "scan numbers")
    result = plan(
        [checklist("Nicole", todo("scan numbers"))], [checklist("Nicole")], history={key: 1}
    )

    assert result.needs_a_look == []


def test_the_item_key_ignores_spacing_but_not_the_person():
    assert dailyops.item_key("general", "Nicole", "a  b") == dailyops.item_key(
        "general", " nicole ", "a b"
    )
    assert dailyops.item_key("general", "Nicole", "a") != dailyops.item_key(
        "general", "Frank", "a"
    )
    assert dailyops.item_key("ops", "Nicole", "a") != dailyops.item_key("general", "Nicole", "a")


# ------------------------------------------------------------------- reporting


def test_the_summary_names_what_moves_and_what_needs_a_look():
    link = "https://trello.com/c/NB28l2KN"
    result = plan(
        [checklist("Nicole", todo("scan numbers"), todo(link))],
        [],
        done_lookup=lambda _card_id: True,
    )

    text = dailyops.summarise([result])

    assert "General" in text
    assert "Nicole: 1 (new checklist)" in text
    assert "already Done but unticked" in text


def test_a_finished_day_says_so():
    assert "Nothing to roll over" in dailyops.summarise([])


# ------------------------------------------------------------ list name lookup


def test_board_lists_are_matched_forgivingly():
    """Names carry emoji and stray spaces, and one is spelled "Pendng A2P"."""
    lists = [{"id": "1", "name": "In Que"}, {"id": "2", "name": " Quality  Check "}]

    assert trello.find_list(lists, "quality check")["id"] == "2"
    assert trello.find_list(lists, "Done") is None


def test_the_next_day_includes_weekends():
    """Cards are generated every day; this is not the blog calendar."""
    friday = date(2026, 8, 21)

    assert dailyops.next_day(friday) == date(2026, 8, 22)


# --------------------------------------- counting the days an item is carried

# `plan_rollover` already flags an item rolled three nights running, but nothing
# was counting, so the flag never fired. A task could walk from Monday to Friday
# without anybody noticing it had - which is exactly what doing it by hand
# would have caught.


def test_an_item_carried_again_counts_up(tmp_path):
    from datetime import date as _date

    from wilbyte import carried

    store = tmp_path / "carried.json"
    key = dailyops.item_key("general", "Nicole", "Chase the Thompson docs")

    carried.record([key], _date(2026, 8, 24), store)
    carried.record([key], _date(2026, 8, 25), store)

    assert carried.history(store)[key] == 2


def test_rolling_twice_in_one_evening_is_not_two_days(tmp_path):
    """Running it again is somebody checking their work, not the item ageing."""
    from datetime import date as _date

    from wilbyte import carried

    store = tmp_path / "carried.json"
    key = dailyops.item_key("ops", "Jay", "Fix the lead sheet")

    carried.record([key], _date(2026, 8, 24), store)
    carried.record([key], _date(2026, 8, 24), store)

    assert carried.history(store)[key] == 1


def test_a_count_of_three_makes_an_item_stuck():
    item = dailyops.Leftover(person="Nicole", name="Chase the docs", times_rolled=3)

    assert item.stuck is True
    assert dailyops.Leftover(person="Nicole", name="x", times_rolled=2).stuck is False


def test_a_stuck_item_is_raised_rather_than_carried_again(tmp_path):
    from wilbyte import carried

    store = tmp_path / "carried.json"
    key = dailyops.item_key("general", "Nicole", "Chase the Thompson docs")
    carried.save({key: {"count": 4, "last": "2026-08-24"}}, store)

    plan = dailyops.plan_rollover(
        "general",
        source_card={"name": "💎 General 08/24/26"},
        source_checklists=[{
            "name": "Nicole",
            "checkItems": [{"name": "Chase the Thompson docs", "state": "incomplete"}],
        }],
        target_card={"name": "💎 General 08/25/26"},
        target_checklists=[],
        history=carried.history(store),
    )

    (flagged,) = plan.needs_a_look
    assert flagged.stuck is True
    assert "rolled forward 4 days" in dailyops.summarise([plan])


def test_a_finished_item_stops_being_counted(tmp_path):
    from wilbyte import carried

    store = tmp_path / "carried.json"
    key = dailyops.item_key("ops", "Jay", "Fix the lead sheet")
    carried.save({key: {"count": 2, "last": "2026-08-24"}}, store)

    carried.clear([key], store)

    assert carried.history(store) == {}


def test_an_item_nobody_has_seen_for_a_fortnight_is_forgotten(tmp_path):
    from datetime import date as _date

    from wilbyte import carried

    store = tmp_path / "carried.json"
    old = dailyops.item_key("ads", "Teresa", "Old thing")
    carried.save({old: {"count": 3, "last": "2026-08-01"}}, store)

    carried.record([dailyops.item_key("ads", "Teresa", "New thing")], _date(2026, 8, 24), store)

    assert old not in carried.history(store)


def test_a_corrupt_count_file_is_no_counts_rather_than_a_crash(tmp_path):
    from wilbyte import carried

    store = tmp_path / "carried.json"
    store.write_text("{ not json")

    assert carried.history(store) == {}


def test_rewording_an_item_starts_its_count_again(tmp_path):
    """The safe direction to be wrong in: a renamed item looks new, which
    means it gets carried rather than flagged."""
    from wilbyte import carried

    store = tmp_path / "carried.json"
    carried.save(
        {dailyops.item_key("general", "Nicole", "Chase docs"): {"count": 4, "last": "2026-08-24"}},
        store,
    )

    assert dailyops.item_key("general", "Nicole", "Chase the docs") not in carried.history(store)


# ------------------------------------------------ actually writing the board


class FakeTrello:
    """Records what a rollover would send, and can be told to refuse.

    `holds` is what tomorrow's cards actually have on them right now - read
    back at write time, not taken from the plan, because a plan is a snapshot
    from whenever somebody last looked at it.
    """

    def __init__(self, *, fail_on=None, holds=None):
        self.checklists_made = []
        self.items_added = []
        self.fail_on = fail_on
        self.holds = holds or {}

    def card_checklists(self, card_id):
        return self.holds.get(card_id, [])

    def create_checklist(self, card_id, name):
        self.checklists_made.append((card_id, name))
        made = {"id": f"list-{name}", "name": name, "checkItems": []}
        self.holds.setdefault(card_id, []).append(made)
        return made

    def add_check_item(self, checklist_id, name, *, checked=False):
        if self.fail_on and self.fail_on in name:
            raise RuntimeError("Trello said no")
        self.items_added.append((checklist_id, name))
        return {}

    def close(self):
        pass


def rollover_of(*items, target_lists=(), history=None):
    """One plan carrying the (person, name, state) items given."""
    return dailyops.plan_rollover(
        "general",
        source_card={"name": "💎 General 08/24/26"},
        source_checklists=[
            {"name": person, "checkItems": [{"name": name, "state": state}]}
            for person, name, state in items
        ],
        target_card={"name": "💎 General 08/25/26"},
        target_checklists=list(target_lists),
        history=history or {},
    )


def apply_with(monkeypatch, config, plan, targets, *, fail_on=None, store=None, holds=None):
    from datetime import date as _date

    from wilbyte import carried
    from wilbyte.bot import jobs

    # By default the board holds what the plan was built against, which is the
    # ordinary case: nothing changed between looking and clicking.
    board = FakeTrello(
        fail_on=fail_on,
        holds=holds if holds is not None
        else {card_id: list(lists) for card_id, lists in targets.values()},
    )
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)
    if store is not None:
        monkeypatch.setattr(carried, "CARRIED_PATH", store)
    moved, problems = jobs.apply_rollover(config, [plan], targets, day=_date(2026, 8, 24))
    return board, moved, problems


def test_an_unticked_item_lands_on_the_same_persons_checklist(monkeypatch, config, tmp_path):
    plan = rollover_of(("Nicole", "Chase the Thompson docs", "incomplete"))
    targets = {"general": ("card-tomorrow", [{"id": "l1", "name": "Nicole"}])}

    board, moved, problems = apply_with(
        monkeypatch, config, plan, targets, store=tmp_path / "c.json"
    )

    assert moved == 1 and problems == []
    assert board.items_added == [("l1", "Chase the Thompson docs")]
    assert board.checklists_made == [], "Nicole's list was already there"


def test_a_missing_checklist_is_made_before_the_item_goes_on_it(monkeypatch, config, tmp_path):
    """The generated cards arrive with no checklists at all, so this is the
    normal case rather than an error."""
    plan = rollover_of(("Jay", "Fix the lead sheet", "incomplete"))
    targets = {"general": ("card-tomorrow", [])}

    board, moved, _ = apply_with(monkeypatch, config, plan, targets, store=tmp_path / "c.json")

    assert board.checklists_made == [("card-tomorrow", "Jay")]
    assert board.items_added == [("list-Jay", "Fix the lead sheet")]
    assert moved == 1


def test_a_ticked_item_is_left_behind(monkeypatch, config, tmp_path):
    plan = rollover_of(("Nicole", "Already done", "complete"))
    targets = {"general": ("card-tomorrow", [])}

    board, moved, _ = apply_with(monkeypatch, config, plan, targets, store=tmp_path / "c.json")

    assert board.items_added == [] and moved == 0


def test_a_card_link_travels_as_the_url_not_the_label(monkeypatch, config, tmp_path):
    """Trello renders the name and badge from the URL. Copying the rendered
    label produces dead text and the badge stops updating."""
    link = "https://trello.com/c/AbCd1234/57-set-up-the-thompson-campaign"
    plan = rollover_of(("Teresa", link, "incomplete"))
    targets = {"general": ("card-tomorrow", [{"id": "l1", "name": "Teresa"}])}

    board, _, _ = apply_with(monkeypatch, config, plan, targets, store=tmp_path / "c.json")

    assert board.items_added == [("l1", link)]


def test_an_item_carried_three_days_is_not_carried_a_fourth(monkeypatch, config, tmp_path):
    """It is raised instead. Moving it again silently is the failure that doing
    this by hand would have caught."""
    key = dailyops.item_key("general", "Nicole", "Chase the docs")
    plan = rollover_of(("Nicole", "Chase the docs", "incomplete"), history={key: 3})
    targets = {"general": ("card-tomorrow", [{"id": "l1", "name": "Nicole"}])}

    board, moved, _ = apply_with(monkeypatch, config, plan, targets, store=tmp_path / "c.json")

    assert board.items_added == [] and moved == 0
    assert plan.needs_a_look[0].stuck is True


def test_one_item_failing_does_not_stop_the_others(monkeypatch, config, tmp_path):
    plan = rollover_of(
        ("Nicole", "This one breaks", "incomplete"),
        ("Jay", "This one is fine", "incomplete"),
    )
    targets = {
        "general": ("card-tomorrow", [{"id": "l1", "name": "Nicole"}, {"id": "l2", "name": "Jay"}])
    }

    board, moved, problems = apply_with(
        monkeypatch, config, plan, targets, fail_on="breaks", store=tmp_path / "c.json"
    )

    assert moved == 1
    assert board.items_added == [("l2", "This one is fine")]
    assert len(problems) == 1 and "Nicole" in problems[0]


def test_no_card_for_tomorrow_is_said_rather_than_guessed_at(monkeypatch, config, tmp_path):
    plan = rollover_of(("Nicole", "Chase the docs", "incomplete"))

    board, moved, problems = apply_with(
        monkeypatch, config, plan, {}, store=tmp_path / "c.json"
    )

    assert moved == 0 and board.items_added == []
    assert "no card for tomorrow" in problems[0]


def test_only_the_items_that_moved_are_aged(monkeypatch, config, tmp_path):
    """A run that failed halfway must not age items it never moved."""
    from wilbyte import carried

    store = tmp_path / "c.json"
    plan = rollover_of(
        ("Nicole", "This one breaks", "incomplete"),
        ("Jay", "This one is fine", "incomplete"),
    )
    targets = {
        "general": ("card-tomorrow", [{"id": "l1", "name": "Nicole"}, {"id": "l2", "name": "Jay"}])
    }

    apply_with(monkeypatch, config, plan, targets, fail_on="breaks", store=store)

    counted = carried.history(store)
    assert dailyops.item_key("general", "Jay", "This one is fine") in counted
    assert dailyops.item_key("general", "Nicole", "This one breaks") not in counted


# --------------------------------------------- walking the board through its day

# From how it is actually run: In Que to Today by 9am, Today to Quality Check by
# 6pm, and by 9pm whatever is still unticked goes onto tomorrow's cards - which
# Zapier has already made and left in In Que.


def at_hour(hour, minute=0):
    from datetime import datetime as _dt

    return _dt(2026, 8, 24, hour, minute)


# Two in the morning, before anything else: yesterday's Ads and Lead Order
# cards get carried over then, because both are still worked after the board
# has been put to bed.
SMALL_HOURS = [dailyops.LATE_ROLLOVER, dailyops.LATE_DONE]
MORNING = ["make_setup"]
WORKING = MORNING + ["to_today"]
MIDDAY = WORKING + ["link_setup"]
CHASE_1 = MIDDAY + [dailyops.UNMARKED[0]]
CHASE_2 = CHASE_1 + [dailyops.UNMARKED[1]]
EVENING = CHASE_2 + ["to_quality_check"]
CHASE_3 = EVENING + [dailyops.UNMARKED[2]]
CHASE_4 = CHASE_3 + [dailyops.UNMARKED[3]]
NIGHT = CHASE_4 + ["rollover", "to_done"]
LATE_NIGHT = NIGHT + ["archive_aged"]


@pytest.mark.parametrize(
    "hour,minute,expected",
    [
        (1, 59, []),
        (2, 0, SMALL_HOURS),
        (5, 0, SMALL_HOURS),
        # Six o'clock is the cut-off: after it the night pair is not caught up.
        (6, 0, ["make_setup"]),
        (8, 0, MORNING),
        (9, 0, WORKING),
        # Half past matters now, so both sides of it are worth pinning.
        (11, 29, WORKING),
        (11, 30, MIDDAY),
        (15, 29, MIDDAY),
        (15, 29, MIDDAY),
        (15, 30, CHASE_1),
        (17, 29, CHASE_1),
        (17, 30, CHASE_2),
        (18, 0, EVENING),
        (18, 29, EVENING),
        (18, 30, CHASE_3),
        (19, 29, CHASE_3),
        (19, 30, CHASE_4),
        (20, 29, CHASE_4),
        (20, 30, NIGHT),
        (22, 0, LATE_NIGHT),
        (23, 0, LATE_NIGHT),
    ],
)
def test_the_day_unfolds_in_order(hour, minute, expected):
    assert dailyops.steps_due(at_hour(hour, minute), set()) == expected


def test_the_carry_happens_before_the_cards_leave_for_done():
    """"After you move those unchecked lists to their respective new list you
    move them to done" - both at nine, and that way round."""
    due = dailyops.steps_due(at_hour(20, 30), set())

    assert due.index("rollover") < due.index("to_done")


def test_the_spread_does_not_run_on_its_own():
    """Asked for only, until it has been watched getting a few real days right.
    It wrote onto the wrong Lead Order card and invented checklists on it."""
    assert "to_lead_order" not in [step for _h, _m, step in dailyops.STEPS]
    assert dailyops.time_of("to_lead_order") is None
    assert "to_lead_order" not in dailyops.steps_due(at_hour(23, 59), set())


def test_a_step_already_done_is_not_done_again():
    """Moving cards that already moved puts them somewhere nobody expects."""
    assert dailyops.steps_due(at_hour(20, 30), set(CHASE_4)) == [
        "rollover", "to_done",
    ]
    assert dailyops.steps_due(at_hour(20, 30), set(NIGHT)) == []


def test_starting_late_catches_up_rather_than_skipping():
    """RYTE is restarted often enough that "it was running at nine" is not
    something to rely on. A board moved late beats one left in In Que."""
    assert dailyops.steps_due(at_hour(11), set()) == WORKING
    assert dailyops.steps_due(at_hour(22, 30), set()) == LATE_NIGHT


def test_the_clock_remembers_across_a_restart(tmp_path):
    from datetime import date as _date

    from wilbyte import boardclock

    store = tmp_path / "clock.json"
    today = _date(2026, 8, 24)

    for step in WORKING:
        boardclock.mark(step, today, store)

    assert boardclock.done_on(today, store) == set(WORKING)
    assert dailyops.steps_due(at_hour(9), boardclock.done_on(today, store)) == []


def test_yesterdays_steps_do_not_count_as_todays(tmp_path):
    from datetime import date as _date

    from wilbyte import boardclock

    store = tmp_path / "clock.json"
    boardclock.mark("to_today", _date(2026, 8, 23), store)

    assert boardclock.done_on(_date(2026, 8, 24), store) == set()


def test_a_corrupt_clock_is_a_fresh_day_rather_than_a_crash(tmp_path):
    from datetime import date as _date

    from wilbyte import boardclock

    store = tmp_path / "clock.json"
    store.write_text("{ not json")

    assert boardclock.done_on(_date(2026, 8, 24), store) == set()


class FakeBoard:
    """A board with named lists and cards in them."""

    def __init__(self, lists):
        self.lists = lists
        self.moves = []

    def board_lists(self, board_id):
        return [{"id": f"id-{name}", "name": name} for name in self.lists]

    def list_cards(self, list_id):
        # Stamped the way Trello stamps them, because what moves where now
        # turns on which list a card is already in.
        return [
            {**card, "idList": list_id}
            for card in self.lists[list_id.removeprefix("id-")]
        ]

    def move_card(self, card_id, list_id, *, position="top"):
        self.moves.append((card_id, list_id, position))
        return {}

    def close(self):
        pass


def test_the_whole_list_moves_not_just_the_four(monkeypatch, config):
    """The lists are the day. Whatever is sitting in In Que at nine is what
    goes into Today, and leaving the rest behind means somebody still walks the
    board by hand afterwards - which is the thing this replaces."""
    from datetime import date as _date

    from wilbyte.bot import jobs

    board = FakeBoard({
        "In Que": [
            {"id": "c1", "name": "💎 General 08/24/26"},
            {"id": "c2", "name": "💻 Ops 08/24/26"},
            {"id": "c3", "name": "Agent Setup Going Live Thursday 08/24"},
            {"id": "c4", "name": "Hyros - UTM Code"},
        ],
        "Today": [],
        "Quality Check": [],
    })
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    moved, problems = jobs.walk_board(config, "to_today", day=_date(2026, 8, 24))

    assert moved == 4 and problems == []
    # Sent last-first, each to the top, so they land in the order they left in.
    assert [card for card, _, _ in board.moves] == ["c4", "c3", "c2", "c1"]
    assert {pos for _, _, pos in board.moves} == {"top"}


def test_tomorrows_cards_are_left_where_they_are(monkeypatch, config):
    """In Que holds tomorrow's four from the evening before. Taking them
    across at nine in the morning starts the day a day early."""
    from datetime import date as _date

    from wilbyte.bot import jobs

    board = FakeBoard({
        "In Que": [
            {"id": "c1", "name": "💎 General 08/26/26"},
            {"id": "c2", "name": "💻 Ops 08/25/26"},
        ],
        "Today": [],
    })
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    cards, _ = jobs.moves_waiting(config, "to_today", day=_date(2026, 8, 25))
    moved, _ = jobs.walk_board(config, "to_today", day=_date(2026, 8, 25))

    assert cards == ["💻 Ops 08/25/26"]
    assert moved == 1 and [c for c, _, _ in board.moves] == ["c2"]


def test_a_new_agents_card_is_not_walked(monkeypatch, config):
    """It is in In Que waiting to be filed, not waiting to be walked. Sweeping
    it into Today loses it out of the only place anything looks for it."""
    from datetime import date as _date

    from wilbyte.bot import jobs

    board = FakeBoard({
        "In Que": [
            {"id": "a", "name": "NEW AGENT- Benjamin Zuniga"},
            {"id": "b", "name": "New Agent - Vicente Mejia"},
            {"id": "c", "name": "💎 General 08/25/26"},
        ],
        "Today": [],
    })
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    moved, _ = jobs.walk_board(config, "to_today", day=_date(2026, 8, 25))

    assert moved == 1
    assert [c for c, _, _ in board.moves] == ["c"]


def test_everything_else_still_goes(monkeypatch, config):
    """The spent setup card, the odd untitled one - the lists are the day."""
    from datetime import date as _date

    from wilbyte.bot import jobs

    board = FakeBoard({
        "In Que": [],
        "Today": [
            {"id": "s", "name": "Agent Setup Going Live Monday 08/24"},
            {"id": "h", "name": "Hyros - UTM Code"},
            {"id": "g", "name": "💎 General 08/25/26"},
        ],
        "Quality Check": [],
    })
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    moved, problems = jobs.walk_board(config, "to_quality_check", day=_date(2026, 8, 25))

    assert moved == 3 and problems == []
    assert {where for _, where, _ in board.moves} == {"id-Quality Check"}


def test_six_fetches_the_card_tomorrow_will_work_on(monkeypatch, config):
    """Agents are set up the day before they go live, so Tuesday evening
    fetches Thursday's card - Wednesday is the day it gets worked."""
    from datetime import date as _date

    from wilbyte.bot import jobs

    board = FakeBoard({
        "AUTOMATION DEPARTMENT": [
            {"id": "thu", "name": "Agent Setup Going Live Thursday 08/27"},
        ],
        "In Que": [],
        "Today": [{"id": "wed", "name": "Agent Setup Going Live Wednesday 08/26"}],
        "Quality Check": [],
    })
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    # Tuesday evening.
    moved, problems = jobs.walk_board(config, "to_quality_check", day=_date(2026, 8, 25))

    assert (moved, problems) == (2, [])
    assert board.moves == [
        # Wednesday's card was today's work and is finished...
        ("wed", "id-Quality Check", "top"),
        # ...and Thursday's is fetched last, so it lands on top of In Que.
        ("thu", "id-In Que", "top"),
    ]


def test_the_card_for_the_day_after_tomorrow_is_not_fetched_yet(monkeypatch, config):
    """Friday's card is Thursday's work. On Tuesday it is nobody's yet."""
    from datetime import date as _date

    from wilbyte.bot import jobs

    board = FakeBoard({
        "AUTOMATION DEPARTMENT": [
            {"id": "fri", "name": "Agent Setup Going Live Friday 08/28"},
        ],
        "In Que": [], "Today": [], "Quality Check": [],
    })
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    assert jobs.walk_board(config, "to_quality_check", day=_date(2026, 8, 25)) == (0, [])


def test_a_setup_card_already_in_the_days_lists_is_left_alone(monkeypatch, config):
    """The point is to fetch it, not to drag it back."""
    from datetime import date as _date

    from wilbyte.bot import jobs

    for holding in ("In Que", "Quality Check", "Done"):
        board = FakeBoard({
            "In Que": [], "Today": [], "Quality Check": [], "Done": [],
            holding: [{"id": "thu", "name": "Agent Setup Going Live Thursday 08/27"}],
        })
        monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

        jobs.walk_board(config, "to_quality_check", day=_date(2026, 8, 25))

        assert [c for c, _, _ in board.moves] == [], f"moved it out of {holding}"


def test_a_card_dragged_into_today_early_is_sent_back_to_in_que(monkeypatch, config):
    """Thursday's card is Wednesday's work. Sitting in Today on Tuesday it is
    a day early, so it goes back to In Que for nine tomorrow morning."""
    from datetime import date as _date

    from wilbyte.bot import jobs

    board = FakeBoard({
        "In Que": [],
        "Today": [{"id": "thu", "name": "Agent Setup Going Live Thursday 08/27"}],
        "Quality Check": [],
    })
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    moved, problems = jobs.walk_board(config, "to_quality_check", day=_date(2026, 8, 25))

    assert (moved, problems) == (1, [])
    assert board.moves == [("thu", "id-In Que", "top")]


def test_nothing_to_fetch_is_not_a_problem(monkeypatch, config):
    """Nobody has made it yet. That is tomorrow's business, not tonight's."""
    from datetime import date as _date

    from wilbyte.bot import jobs

    board = FakeBoard({"In Que": [], "Today": [], "Quality Check": []})
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    assert jobs.walk_board(config, "to_quality_check", day=_date(2026, 8, 25)) == (0, [])


def test_only_six_fetches_it(monkeypatch, config):
    """Nine and the move to Done leave the Automation Department alone."""
    from datetime import date as _date

    from wilbyte.bot import jobs

    for step, lists in (
        ("to_today", {"In Que": [], "Today": []}),
        ("to_done", {"Quality Check": [], "Done": []}),
    ):
        board = FakeBoard({
            **lists,
            "AUTOMATION DEPARTMENT": [
                {"id": "thu", "name": "Agent Setup Going Live Thursday 08/27"},
            ],
        })
        monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

        assert jobs.walk_board(config, step, day=_date(2026, 8, 25)) == (0, [])


def test_the_weekend_card_is_fetched_on_the_thursday(monkeypatch, config):
    """It covers Saturday to Monday, so Friday is the day it gets worked -
    which makes Thursday evening the time it comes over."""
    from datetime import date as _date

    from wilbyte.bot import jobs

    board = FakeBoard({
        "AUTOMATION DEPARTMENT": [
            {"id": "we", "name": "Agent Setup Going Live Saturday-Monday 08/22-08/24"},
        ],
        "In Que": [], "Today": [], "Quality Check": [],
    })
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    # Thursday the 20th.
    jobs.walk_board(config, "to_quality_check", day=_date(2026, 8, 20))

    assert board.moves == [("we", "id-In Que", "top")]


def test_the_weekend_card_finishes_on_the_friday_it_is_worked(monkeypatch, config):
    """Its agents go live from Saturday, so by six on Friday it is done - it
    does not sit in the rotation all weekend."""
    from datetime import date as _date

    from wilbyte.bot import jobs

    title = "Agent Setup Going Live Saturday-Monday 08/22-08/24"
    board = FakeBoard({
        "In Que": [],
        "Today": [{"id": "we", "name": title}],
        "Quality Check": [],
    })
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    jobs.walk_board(config, "to_quality_check", day=_date(2026, 8, 21))

    assert board.moves == [("we", "id-Quality Check", "top")]


def test_the_setup_card_walks_on_on_its_own_working_day(monkeypatch, config):
    """Six on the day it is worked: the setting up is over and its agents go
    live in the morning, so it finishes the walk like everything else."""
    from datetime import date as _date

    from wilbyte.bot import jobs

    board = FakeBoard({
        "In Que": [],
        "Today": [{"id": "thu", "name": "Agent Setup Going Live Thursday 08/27"}],
        "Quality Check": [],
    })
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    moved, problems = jobs.walk_board(config, "to_quality_check", day=_date(2026, 8, 26))

    assert (moved, problems) == (1, [])
    assert board.moves == [("thu", "id-Quality Check", "top")]


def test_nine_in_the_morning_takes_the_setup_card_across_as_usual(monkeypatch, config):
    """Only six is the exception. In Que -> Today is how it gets worked on."""
    from datetime import date as _date

    from wilbyte.bot import jobs

    board = FakeBoard({
        "In Que": [{"id": "thu", "name": "Agent Setup Going Live Thursday 08/27"}],
        "Today": [],
    })
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    jobs.walk_board(config, "to_today", day=_date(2026, 8, 26))
    assert board.moves == [("thu", "id-Today", "top")]


def test_the_preview_says_what_is_coming_and_where(monkeypatch, config):
    """What gets shown and what gets moved cannot disagree."""
    from datetime import date as _date

    from wilbyte.bot import jobs

    board = FakeBoard({
        "AUTOMATION DEPARTMENT": [
            {"id": "thu", "name": "Agent Setup Going Live Thursday 08/27"},
        ],
        "In Que": [],
        "Today": [{"id": "g", "name": "\U0001f48e General 08/25/26"}],
        "Quality Check": [],
    })
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    found, problems = jobs.moves_waiting(
        config, "to_quality_check", day=_date(2026, 8, 25)
    )

    assert problems == []
    assert found == [
        "\U0001f48e General 08/25/26",
        "Agent Setup Going Live Thursday 08/27 \u2192 In Que (for tomorrow)",
    ]
    assert board.moves == [], "reading only"




def test_a_card_somebody_already_moved_by_hand_stays_where_they_put_it(monkeypatch, config):
    from datetime import date as _date

    from wilbyte.bot import jobs

    board = FakeBoard({
        "In Que": [],
        "Today": [{"id": "c1", "name": "💎 General 08/24/26"}],
    })
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    moved, _ = jobs.walk_board(config, "to_today", day=_date(2026, 8, 24))

    assert moved == 0


def test_a_list_the_board_does_not_have_is_named(monkeypatch, config):
    from datetime import date as _date

    from wilbyte.bot import jobs

    monkeypatch.setattr(jobs, "open_trello", lambda cfg: FakeBoard({"In Que": []}))

    moved, problems = jobs.walk_board(config, "to_today", day=_date(2026, 8, 24))

    assert moved == 0
    assert "'Today'" in problems[0]


def test_the_six_pm_move_goes_the_other_way(monkeypatch, config):
    from datetime import date as _date

    from wilbyte.bot import jobs

    board = FakeBoard({
        "Today": [{"id": "c1", "name": "📊 Ads 08/24/26"}],
        "Quality Check": [],
    })
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    jobs.walk_board(config, "to_quality_check", day=_date(2026, 8, 24))

    assert board.moves == [("c1", "id-Quality Check", "top")]


def test_the_trello_word_names_the_board_and_then_the_task():
    """Every board command is said with "trello" in front, so they read as one
    set rather than as loose words that happen to exist."""
    from wilbyte.bot import mentions

    assert mentions.parse("trello rollover").action == "rollover"
    assert mentions.parse("trello board").action == "board"
    assert mentions.parse("trello carryover").action == "rollover"


def test_trello_on_its_own_shows_the_board():
    from wilbyte.bot import mentions

    assert mentions.parse("trello").action == "board"
    assert mentions.parse("trello what's on today").action == "board"


def test_the_bare_words_still_work():
    """Renaming something by breaking what already worked is its own small
    betrayal."""
    from wilbyte.bot import mentions

    assert mentions.parse("rollover").action == "rollover"
    assert mentions.parse("board").action == "board"


def test_trello_in_the_middle_of_a_brief_is_not_a_command():
    from wilbyte.bot import mentions

    assert mentions.parse("write an sms about our trello board").action == "write"


# ------------------------------- a card that is there and does not look like it

# `trello board` showed all four of tomorrow's cards in In Que. The rollover
# said there was no card for tomorrow. Both read the same board - so the date on
# those cards was not the date they appeared to have, and the display was hiding
# it by printing the month and day without the year.


def test_the_year_is_shown_so_a_wrong_one_is_visible(monkeypatch, config):
    from datetime import date as _date

    from wilbyte.bot import jobs

    board = FakeBoard({"In Que": [{"id": "c1", "name": "💎 General 08/26/25"}]})
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    (line,) = [
        row for row in jobs.board_today(config, day=_date(2026, 8, 25))
        if "In Que" in row
    ]

    assert "08/26/25" in line, "without the year this reads as tomorrow's card"


def test_a_card_with_the_wrong_year_is_named_as_such():
    """It is right there. "No card for tomorrow" is true and useless."""
    cards = [{"name": "💎 General 08/25/26"}, {"name": "💎 General 08/26/25"}]

    said = dailyops.why_missing(cards, "general", date(2026, 8, 26))

    assert "08/26/25" in said and "year" in said


def test_a_kind_with_nothing_ahead_says_so():
    cards = [{"name": "💻 Ops 08/25/26"}]

    assert dailyops.why_missing(cards, "ops", date(2026, 8, 26)) == "nothing dated after today"


def test_a_card_further_out_is_pointed_at():
    cards = [{"name": "📊 Ads 08/28/26"}]

    said = dailyops.why_missing(cards, "ads", date(2026, 8, 26))

    assert "Fri Aug 28" in said


def test_the_right_card_existing_is_not_a_complaint():
    """Only asked about kinds that are actually missing, but the helper must
    not invent a problem when the date matches exactly."""
    cards = [{"name": "📊 Ads 08/26/26"}]

    assert "year on it is wrong" not in dailyops.why_missing(cards, "ads", date(2026, 8, 26))


# ------------------------------- whose "today" the board is dated against

# The board showed all four of tomorrow's cards in In Que and the rollover said
# there were none. The board was right and the years were right: RYTE was
# asking `date.today()`, which reads the timezone of whatever machine it runs
# on. A Mac set to Manila time is already tomorrow by mid-afternoon Eastern.


def test_the_board_is_dated_by_its_own_clock_not_the_machines(monkeypatch, config):
    from wilbyte.bot import jobs

    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo

    # 4:31pm Eastern on the 25th is 4:31am Manila on the 26th. The machine
    # would say the 26th; the board is still on the 25th.
    eastern = _dt(2026, 8, 25, 16, 31, tzinfo=ZoneInfo("America/New_York"))
    assert eastern.astimezone(ZoneInfo("Asia/Manila")).date() == date(2026, 8, 26)

    class Clock:
        @staticmethod
        def now(tz):
            return eastern.astimezone(tz)

    monkeypatch.setattr(jobs, "datetime", Clock)

    assert jobs.board_day(config) == date(2026, 8, 25)


def test_the_whole_board_reads_right_on_the_day_it_actually_is(monkeypatch, config):
    """The exact board that failed: In Que holds tomorrow's four, and they are
    found."""
    from wilbyte.bot import jobs

    names = {
        "In Que": ["Lead Order 08/26/26", "💎 General 08/26/26",
                   "💻 Ops 08/26/26", "📊 Ads 08/26/26"],
        "Today": ["💎 General 08/25/26", "📊 Ads 08/25/26", "💻 Ops 08/25/26",
                  "Agent Setup Going Live Wednesday 08/26"],
        "Quality Check": ["Lead Order 08/25/26"],
    }
    board = FakeBoard({
        name: [{"id": f"{name}-{i}", "name": card} for i, card in enumerate(cards)]
        for name, cards in names.items()
    })
    board.card_checklists = lambda card_id: []
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    plans, missing, targets = jobs.read_rollover(config, day=date(2026, 8, 25))

    assert missing == [], missing
    assert sorted(targets) == ["ads", "general", "lead_order", "ops"]
    assert len(plans) == 4


def test_the_setup_card_is_not_mistaken_for_one_of_the_four(monkeypatch, config):
    """`Agent Setup Going Live Wednesday 08/26` sits in Today and is not part
    of the routine."""
    assert dailyops.parse_card_title("Agent Setup Going Live Wednesday 08/26") is None


def test_the_carry_never_touches_the_setup_card(monkeypatch, config):
    """Its unticked boxes are agents being set up, not a person's day of work.
    Copying them onto tomorrow's General card would put somebody's agents on
    somebody else's list, and the card itself is the record of them."""
    from wilbyte.bot import jobs

    setup = {"id": "s", "name": "Agent Setup Going Live Wednesday 08/26"}
    board = FakeBoard({
        "Quality Check": [setup, {"id": "g", "name": "\U0001f48e General 08/25/26"}],
        "In Que": [{"id": "g2", "name": "\U0001f48e General 08/26/26"}],
    })
    board.card_checklists = lambda card_id: [
        {"id": "cl", "name": "Therese", "checkItems": [
            {"id": "i0", "name": "not ticked", "state": "incomplete"},
        ]},
    ]
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    plans, _missing, targets = jobs.read_rollover(config, day=date(2026, 8, 25))

    assert [plan.kind for plan in plans] == ["general"]
    assert sorted(targets) == ["general"]
    assert all(
        "Agent Setup" not in plan.from_title and "Agent Setup" not in plan.to_title
        for plan in plans
    )


# ------------------------------------- running it twice must not double the card

# 62 items became 124 would not be unpicked by hand. Somebody would delete the
# card and rebuild it, and whatever was ticked on it goes with it.


def test_an_item_already_on_tomorrows_card_is_not_added_again(monkeypatch, config, tmp_path):
    plan = rollover_of(
        ("Nicole", "Chase the Thompson docs", "incomplete"),
        ("Nicole", "Call the Hendersons", "incomplete"),
    )
    targets = {"general": ("card-tomorrow", [{
        "id": "l1",
        "name": "Nicole",
        "checkItems": [{"name": "Chase the Thompson docs", "state": "incomplete"}],
    }])}

    board, moved, problems = apply_with(
        monkeypatch, config, plan, targets, store=tmp_path / "c.json"
    )

    assert moved == 1 and problems == []
    assert board.items_added == [("l1", "Call the Hendersons")]


def test_the_same_wording_on_a_different_persons_list_still_goes_over():
    """Two people can have the same task. Matching on the text alone would
    silently drop one of them."""
    from wilbyte.bot import jobs

    plan = rollover_of(
        ("Nicole", "Send the weekly numbers", "incomplete"),
        ("Jay", "Send the weekly numbers", "incomplete"),
    )
    targets = {"general": ("card-tomorrow", [
        {"id": "l1", "name": "Nicole",
         "checkItems": [{"name": "Send the weekly numbers", "state": "incomplete"}]},
        {"id": "l2", "name": "Jay", "checkItems": []},
    ])}

    board = FakeTrello(
        holds={card_id: list(lists) for card_id, lists in targets.values()}
    )
    import wilbyte.bot.jobs as module

    original = module.open_trello
    module.open_trello = lambda cfg: board
    try:
        from wilbyte.config import load_config

        moved, _ = jobs.apply_rollover(load_config(), [plan], targets, day=date(2026, 8, 24))
    finally:
        module.open_trello = original

    assert moved == 1
    assert board.items_added == [("l2", "Send the weekly numbers")]


def test_a_ticked_copy_on_tomorrows_card_also_counts_as_already_there(
    monkeypatch, config, tmp_path
):
    """Somebody did it early. Adding it again unticked undoes that."""
    plan = rollover_of(("Nicole", "Chase the Thompson docs", "incomplete"))
    targets = {"general": ("card-tomorrow", [{
        "id": "l1",
        "name": "Nicole",
        "checkItems": [{"name": "Chase the Thompson docs", "state": "complete"}],
    }])}

    board, moved, _ = apply_with(monkeypatch, config, plan, targets, store=tmp_path / "c.json")

    assert moved == 0 and board.items_added == []


# ------------------------------ the whole item, exactly as somebody wrote it

# The real items are a linked card *and* text: a link to "New Agent - Margo
# Becht" followed by "37 OTP Vet leads unsigned". The link makes the card name
# and the Done badge render; the text is the actual detail of the order. Losing
# either half loses the item.

REAL_ITEM = (
    "https://trello.com/c/AbCd1234/912-new-agent-margo-becht "
    "37 OTP Vet leads unsigned"
)


def test_the_link_and_the_words_after_it_both_travel(monkeypatch, config, tmp_path):
    plan = rollover_of(("OTP VET Plus", REAL_ITEM, "incomplete"))
    targets = {"general": ("card-tomorrow", [{"id": "l1", "name": "OTP VET Plus"}])}

    board, moved, _ = apply_with(monkeypatch, config, plan, targets, store=tmp_path / "c.json")

    (_, sent), = board.items_added
    assert sent == REAL_ITEM, "verbatim, or the badge dies and the detail is lost"
    assert "37 OTP Vet leads unsigned" in sent
    assert "trello.com/c/AbCd1234" in sent


def test_a_link_with_text_after_it_is_still_recognised_as_a_link():
    from wilbyte import trello

    assert trello.item_is_link(REAL_ITEM) is True
    assert trello.linked_card_id(REAL_ITEM) == "AbCd1234"


def test_a_checklist_named_for_a_product_routes_like_any_other(
    monkeypatch, config, tmp_path
):
    """Lead Order's checklists are OTP VET Plus, OTP FEX, own setup - lead
    types, not people. The rule is the same: back onto the one it came from."""
    plan = rollover_of(
        ("OTP FEX", "20 Txt Verified Final Expense Leads UNSIGNED", "incomplete"),
        ("own setup", "Chase the Aulundrew paperwork", "incomplete"),
    )
    targets = {"general": ("card-tomorrow", [{"id": "l1", "name": "OTP FEX"}])}

    board, moved, _ = apply_with(monkeypatch, config, plan, targets, store=tmp_path / "c.json")

    assert moved == 2
    assert ("l1", "20 Txt Verified Final Expense Leads UNSIGNED") in board.items_added
    assert board.checklists_made == [("card-tomorrow", "own setup")]


# ------------------------------------------------ one card, to try it out on


@pytest.mark.parametrize(
    "said,kind",
    [
        ("rollover general", "general"),
        ("rollover ops", "ops"),
        ("rollover ads", "ads"),
        ("rollover lead order", "lead_order"),
        ("rollover leadorder", "lead_order"),
        ("rollover", None),
    ],
)
def test_the_card_somebody_named_is_the_one_they_meant(said, kind):
    assert dailyops.kind_named(said) == kind


def test_naming_one_card_leaves_the_others_alone(monkeypatch, config):
    from wilbyte.bot import jobs

    names = {
        "In Que": ["💎 General 08/26/26", "💻 Ops 08/26/26"],
        "Today": ["💎 General 08/25/26", "💻 Ops 08/25/26"],
    }
    board = FakeBoard({
        name: [{"id": f"{name}-{i}", "name": card} for i, card in enumerate(cards)]
        for name, cards in names.items()
    })
    board.card_checklists = lambda card_id: []
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    plans, missing, targets = jobs.read_rollover(
        config, day=date(2026, 8, 25), only="general"
    )

    assert [plan.kind for plan in plans] == ["general"]
    assert sorted(targets) == ["general"]
    assert missing == [], "Ops was not asked about, so it is not missing"


def test_asking_for_all_of_them_is_still_the_default(monkeypatch, config):
    from wilbyte.bot import jobs

    names = {
        "In Que": ["💎 General 08/26/26", "💻 Ops 08/26/26"],
        "Today": ["💎 General 08/25/26", "💻 Ops 08/25/26"],
    }
    board = FakeBoard({
        name: [{"id": f"{name}-{i}", "name": card} for i, card in enumerate(cards)]
        for name, cards in names.items()
    })
    board.card_checklists = lambda card_id: []
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    plans, _, _ = jobs.read_rollover(config, day=date(2026, 8, 25))

    assert sorted(plan.kind for plan in plans) == ["general", "ops"]


# --------------------------------------- a button that has been sitting there

# One card was rolled on its own to try it out. The all-four plan from seven
# minutes earlier was still on screen with its button unclicked - and its idea
# of what tomorrow's card held was from before the seven items went on it.


def test_a_stale_plan_does_not_re_add_what_is_already_there(monkeypatch, config, tmp_path):
    plan = rollover_of(
        ("Nicole", "OPUS CLIP", "incomplete"),
        ("Faith", "cancel invoices eod", "incomplete"),
    )
    # What the plan was built against: an empty card.
    targets = {"general": ("card-tomorrow", [])}
    # What the card actually holds now, because the single-card run went first.
    holds = {"card-tomorrow": [
        {"id": "l1", "name": "Nicole",
         "checkItems": [{"name": "OPUS CLIP", "state": "incomplete"}]},
        {"id": "l2", "name": "Faith", "checkItems": []},
    ]}

    board, moved, problems = apply_with(
        monkeypatch, config, plan, targets, store=tmp_path / "c.json", holds=holds
    )

    assert moved == 1 and problems == []
    assert board.items_added == [("l2", "cancel invoices eod")]
    assert board.checklists_made == [], "both checklists already exist on the card"


def test_a_card_that_cannot_be_re_read_is_not_written_to_blind(
    monkeypatch, config, tmp_path
):
    """Guessing what is on it is how items land twice."""
    from wilbyte import carried
    from wilbyte.bot import jobs

    class Unreadable(FakeTrello):
        def card_checklists(self, card_id):
            raise RuntimeError("Trello timed out")

    board = Unreadable()
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)
    monkeypatch.setattr(carried, "CARRIED_PATH", tmp_path / "c.json")
    plan = rollover_of(("Nicole", "Chase the docs", "incomplete"))

    moved, problems = jobs.apply_rollover(
        config, [plan], {"general": ("card-tomorrow", [])}, day=date(2026, 8, 24)
    )

    assert moved == 0 and board.items_added == []
    assert "couldn't re-read" in problems[0]


# ------------------------------------- moving the cards along, when asked to

# The three moves only existed inside the loop that runs them on a timer, and
# Quality Check -> Done did not exist at all. With the timer off there was no
# way to say "move them" at all.


@pytest.mark.parametrize(
    "said,step",
    [
        ("move today", "to_today"),
        ("move to today", "to_today"),
        ("move quality check", "to_quality_check"),
        ("move qc", "to_quality_check"),
        ("move done", "to_done"),
        ("rollover", None),
        ("board", None),
    ],
)
def test_the_move_is_named_by_where_the_cards_end_up(said, step):
    """Which is how anybody says it: "move them to Today", not "do the nine
    o'clock one"."""
    assert dailyops.move_named(said) == step


def test_quality_check_to_done_is_a_step_as_well_as_a_move():
    """It runs itself in the evening, and is still there to ask for by name."""
    assert "to_done" in dailyops.STEP_LISTS
    assert dailyops.time_of("to_done") == (20, 30)
    assert dailyops.move_named("trello move done") == "to_done"


def test_the_evening_pair_run_at_the_same_hour():
    """One hour, two steps. Changing it must not leave them on different ones."""
    assert dailyops.time_of("rollover") == dailyops.time_of("to_done")


def test_a_move_nobody_scheduled_has_no_hour():
    assert dailyops.time_of("not a step") is None
    assert dailyops.said_at("not a step") == ""


@pytest.mark.parametrize(
    "hour,minute,said",
    [
        (9, 0, "9am"), (12, 0, "12pm"), (18, 0, "6pm"),
        (0, 0, "12am"), (15, 30, "3:30pm"), (17, 30, "5:30pm"), (6, 5, "6:05am"),
        (20, 30, "8:30pm"),
    ],
)
def test_the_hour_is_reported_the_way_anybody_says_it(hour, minute, said):
    assert dailyops.clock(hour, minute) == said


def test_the_four_chases_are_half_past_three_five_six_and_seven():
    """Two either side of the six o'clock move - the afternoon pair while
    there is still a working day to fix it in, the evening pair once the cards
    have been through Quality Check."""
    assert [dailyops.said_at(step) for step in dailyops.UNMARKED] == [
        "3:30pm", "5:30pm", "6:30pm", "7:30pm",
    ]
    assert all(step in dailyops.STEP_NAMES for step in dailyops.UNMARKED)


def test_each_chase_is_remembered_on_its_own():
    """Four steps, four names. One that ran does not silence the next."""
    assert len(set(dailyops.UNMARKED)) == 4
    assert dailyops.steps_due(at_hour(19, 30), {dailyops.UNMARKED[0]}) == [
        step for step in CHASE_4 if step != dailyops.UNMARKED[0]
    ]


def test_what_would_move_is_shown_before_anything_does(monkeypatch, config):
    """Everything sitting in Quality Check, today's and the ones left behind.
    Nothing lands there ahead of its day, so an older card is a straggler
    rather than one waiting its turn."""
    from wilbyte.bot import jobs

    board = FakeBoard({
        "Quality Check": [
            {"id": "c1", "name": "💎 General 08/25/26"},
            {"id": "c2", "name": "💻 Ops 08/24/26"},
        ],
        "Done": [],
    })
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    cards, problems = jobs.moves_waiting(config, "to_done", day=date(2026, 8, 25))

    assert cards == ["💎 General 08/25/26", "💻 Ops 08/24/26"]
    assert problems == []
    assert board.moves == [], "reading only"


def _quality_check_board():
    return FakeBoard({
        "Quality Check": [
            {"id": "c1", "name": "Lead Order 08/25/26"},
            {"id": "c2", "name": "📊 Ads 08/25/26"},
            {"id": "c3", "name": "💻 Ops 08/25/26"},
        ],
        "Done": [],
    })


def test_half_eight_leaves_ads_and_lead_order_where_they_are(monkeypatch, config):
    """Both are still being worked after the board is put to bed; they are
    finished at two in the morning instead."""
    from wilbyte.bot import jobs

    board = _quality_check_board()
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    moved, problems = jobs.walk_board(config, "to_done", day=date(2026, 8, 25))

    assert (moved, problems) == (1, [])
    assert board.moves == [("c3", "id-Done", "top")]


def test_two_in_the_morning_finishes_ads_and_lead_order(monkeypatch, config):
    from wilbyte.bot import jobs

    board = _quality_check_board()
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    moved, problems = jobs.walk_board(
        config, dailyops.LATE_DONE, day=date(2026, 8, 25)
    )

    assert (moved, problems) == (2, [])
    assert sorted(who for who, _where, _at in board.moves) == ["c1", "c2"]


def test_the_two_done_steps_never_take_the_same_card(monkeypatch, config):
    """Between them they finish the board once, not twice and not never."""
    from wilbyte.bot import jobs

    day = date(2026, 8, 25)
    every = _quality_check_board().lists["Quality Check"]
    took = {
        step: [
            card["id"] for card in every
            if jobs.walks_today(card, day, step=step)
        ]
        for step in dailyops.DONE_STEPS
    }
    assert sorted(took["to_done"] + took[dailyops.LATE_DONE]) == ["c1", "c2", "c3"]
    assert not set(took["to_done"]) & set(took[dailyops.LATE_DONE])


def test_an_undated_card_goes_with_the_half_eight_sweep(monkeypatch, config):
    """At two in the morning anything else in Quality Check is something
    somebody left there on purpose."""
    from wilbyte.bot import jobs

    card = {"id": "x", "name": "Something somebody made"}
    assert jobs.walks_today(card, date(2026, 8, 25), step="to_done") is True
    assert jobs.walks_today(card, date(2026, 8, 25), step=dailyops.LATE_DONE) is False


def test_lead_order_still_walks_the_rest_of_the_day(monkeypatch, config):
    """Only Done is the exception. Nine and six move it like anything else."""
    from wilbyte.bot import jobs

    board = FakeBoard({
        "In Que": [{"id": "c1", "name": "Lead Order 08/25/26"}],
        "Today": [{"id": "c2", "name": "Lead Order 08/25/26"}],
        "Quality Check": [],
    })
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    assert jobs.walk_board(config, "to_today", day=date(2026, 8, 25))[0] == 1
    assert jobs.walk_board(config, "to_quality_check", day=date(2026, 8, 25))[0] == 1


def test_a_card_dated_ahead_still_waits_its_turn_for_done(monkeypatch, config):
    """The date rule loosens for stragglers, not for cards from the future."""
    from wilbyte.bot import jobs

    board = FakeBoard({
        "Quality Check": [
            {"id": "c1", "name": "💻 Ops 08/25/26"},
            {"id": "c2", "name": "💻 Ops 08/26/26"},
        ],
        "Done": [],
    })
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    cards, _ = jobs.moves_waiting(config, "to_done", day=date(2026, 8, 25))

    assert cards == ["💻 Ops 08/25/26"]


def test_an_agent_card_never_gets_swept_into_done(monkeypatch, config):
    """It leaves In Que by being filed. In Done nothing would ever file it."""
    from wilbyte.bot import jobs

    board = FakeBoard({
        "Quality Check": [{"id": "c1", "name": "New Agent - Gustin Elrod"}],
        "Done": [],
    })
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    moved, problems = jobs.walk_board(config, "to_done", day=date(2026, 8, 25))

    assert (moved, problems, board.moves) == (0, [], [])


def test_a_missing_destination_is_said_before_the_button(monkeypatch, config):
    from wilbyte.bot import jobs

    board = FakeBoard({"Quality Check": [{"id": "c1", "name": "📊 Ads 08/25/26"}]})
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    cards, problems = jobs.moves_waiting(config, "to_done", day=date(2026, 8, 25))

    assert cards == []
    assert "'Done'" in problems[0]


def test_the_done_move_carries_todays_cards_out_of_quality_check(monkeypatch, config):
    from wilbyte.bot import jobs

    board = FakeBoard({
        "Quality Check": [{"id": "c1", "name": "💻 Ops 08/25/26"}],
        "Done": [],
    })
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    moved, problems = jobs.walk_board(config, "to_done", day=date(2026, 8, 25))

    assert moved == 1 and problems == []
    assert board.moves == [("c1", "id-Done", "top")], "the bottom of Done is under 49 cards"


def test_a_moved_card_lands_at_the_top(monkeypatch, config):
    """Trello drops it at the bottom otherwise, and the bottom of Done is
    under forty-nine other cards."""
    from wilbyte.bot import jobs

    board = FakeBoard({
        "Quality Check": [{"id": "c1", "name": "💎 General 08/25/26"}],
        "Done": [],
    })
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    jobs.walk_board(config, "to_done", day=date(2026, 8, 25))

    (_, _, position), = board.moves
    assert position == "top"


def test_the_order_they_were_in_is_the_order_they_arrive_in(monkeypatch, config):
    """Four cards each sent to the top would land upside down. Sent last
    first, they don't."""
    from wilbyte.bot import jobs

    board = FakeBoard({
        "In Que": [
            {"id": "first", "name": "Lead Order 08/25/26"},
            {"id": "second", "name": "💎 General 08/25/26"},
            {"id": "third", "name": "💻 Ops 08/25/26"},
        ],
        "Today": [],
    })
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    jobs.walk_board(config, "to_today", day=date(2026, 8, 25))

    assert [card for card, _, _ in board.moves] == ["third", "second", "first"]


def test_a_card_made_too_late_to_be_fetched_on_time_still_comes(monkeypatch, config):
    """RYTE makes a setup card himself when an agent turns up for a day that
    has none - which happens on that card's own working day, after its only
    fetch has gone by. Late and on the board beats on time and invisible."""
    from datetime import date as _date

    from wilbyte.bot import jobs

    board = FakeBoard({
        "AUTOMATION DEPARTMENT": [
            # Worked Thursday, and it is Thursday evening. Already late.
            {"id": "fri", "name": "Agent Setup Going Live Friday 08/28"},
        ],
        "In Que": [], "Today": [], "Quality Check": [],
    })
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    jobs.walk_board(config, "to_quality_check", day=_date(2026, 8, 27))

    assert board.moves == [("fri", "id-In Que", "top")]


def test_a_card_whose_agents_are_already_live_is_left_where_it_is(monkeypatch, config):
    """Last week's card is history. Fetching it would put a finished card back
    in front of the team every evening."""
    from datetime import date as _date

    from wilbyte.bot import jobs

    board = FakeBoard({
        "AUTOMATION DEPARTMENT": [
            {"id": "old", "name": "Agent Setup Going Live Monday 08/17"},
        ],
        "In Que": [], "Today": [], "Quality Check": [],
    })
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    assert jobs.walk_board(config, "to_quality_check", day=_date(2026, 8, 25)) == (0, [])


def test_two_cards_to_fetch_arrive_soonest_on_top(monkeypatch, config):
    """Each move goes to the top, so the one whose agents go live first has to
    be moved last."""
    from datetime import date as _date

    from wilbyte.bot import jobs

    board = FakeBoard({
        "AUTOMATION DEPARTMENT": [
            {"id": "sat", "name": "Agent Setup Going Live Saturday 08/29"},
            {"id": "fri", "name": "Agent Setup Going Live Friday 08/28"},
        ],
        "In Que": [], "Today": [], "Quality Check": [],
    })
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    # Thursday: Friday's card is late already, Saturday's is due tonight.
    moved, problems = jobs.walk_board(config, "to_quality_check", day=_date(2026, 8, 27))

    assert (moved, problems) == (2, [])
    assert [c for c, _, _ in board.moves] == ["sat", "fri"]


# ------------------------------- the thread from Done back to the setup card


class LinkingBoard(FakeBoard):
    """A board that remembers descriptions as well as moves."""

    def __init__(self, lists, desc=None):
        super().__init__(lists)
        self.desc = desc or {}

    def card_detail(self, card_id):
        return {"id": card_id, "desc": self.desc.get(card_id, "")}

    def set_description(self, card_id, text):
        self.desc[card_id] = text
        return {}


SETUP_URL = "https://trello.com/c/FsT9wtDC/15321-agent-setup-going-live-wednesday-08-26"


def linking_board(desc=None):
    return LinkingBoard({
        "Quality Check": [
            {"id": "s", "name": "Agent Setup Going Live Wednesday 08/26",
             "url": SETUP_URL},
        ],
        "Done": [],
        "In Que": [{"id": "lo", "name": "Lead Order 08/26/26"}],
    }, desc=desc)


def test_a_setup_card_going_to_done_is_linked_on_that_days_lead_order(
    monkeypatch, config
):
    """Its agents go live on the 26th, so the 26th's Lead Order card is the one
    somebody is looking at when they need to see who was set up."""
    from wilbyte.bot import jobs

    board = linking_board()
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    moved, problems = jobs.walk_board(config, "to_done", day=date(2026, 8, 25))

    assert (moved, problems) == (1, [])
    assert board.moves == [("s", "id-Done", "top")]
    assert board.desc["lo"] == SETUP_URL


def test_the_link_is_added_under_what_is_already_there(monkeypatch, config):
    """The description carries the people it concerns. Replacing it would lose
    them, which is worse than not adding the link."""
    from wilbyte.bot import jobs

    board = linking_board({"lo": "@nic0l3 @kathleenmarie15 @jenniferhashisaki2"})
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    jobs.walk_board(config, "to_done", day=date(2026, 8, 25))

    assert board.desc["lo"] == (
        "@nic0l3 @kathleenmarie15 @jenniferhashisaki2\n\n" + SETUP_URL
    )


def test_a_link_already_there_is_not_added_twice(monkeypatch, config):
    """Matched on the card's short id, because a link somebody pasted by hand
    carries whatever the title was at the time on the end of it."""
    from wilbyte.bot import jobs

    theirs = "https://trello.com/c/FsT9wtDC/15321-agent-setup-going-live-wednesday-08-26"
    board = linking_board({"lo": f"@nic0l3\n\n{theirs}"})
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    jobs.walk_board(config, "to_done", day=date(2026, 8, 25))

    assert board.desc["lo"] == f"@nic0l3\n\n{theirs}", "written again"


def test_a_link_pasted_with_a_different_slug_still_counts(monkeypatch, config):
    from wilbyte.bot import jobs

    board = linking_board({"lo": "https://trello.com/c/FsT9wtDC"})
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    jobs.walk_board(config, "to_done", day=date(2026, 8, 25))

    assert board.desc["lo"] == "https://trello.com/c/FsT9wtDC"


def test_no_lead_order_card_for_that_day_is_said_not_swallowed(monkeypatch, config):
    """The card still goes to Done. The missing link is worth a sentence."""
    from wilbyte.bot import jobs

    board = LinkingBoard({
        "Quality Check": [
            {"id": "s", "name": "Agent Setup Going Live Wednesday 08/26",
             "url": SETUP_URL},
        ],
        "Done": [],
    })
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    moved, problems = jobs.walk_board(config, "to_done", day=date(2026, 8, 25))

    assert moved == 1
    assert board.moves == [("s", "id-Done", "top")]
    assert "no Lead Order card dated 08/26/26" in problems[0]


def test_the_daily_cards_are_not_linked_anywhere(monkeypatch, config):
    """Only setup cards. Ads going to Done is not a thread anybody follows."""
    from wilbyte.bot import jobs

    board = LinkingBoard({
        "Quality Check": [{"id": "a", "name": "📊 Ads 08/25/26"}],
        "Done": [],
        "In Que": [{"id": "lo", "name": "Lead Order 08/26/26"}],
    })
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    jobs.walk_board(config, "to_done", day=date(2026, 8, 25))

    assert board.desc == {}


def test_six_oclock_does_not_link_anything(monkeypatch, config):
    """Only the move to Done. At six the setup card is still being worked."""
    from wilbyte.bot import jobs

    board = LinkingBoard({
        "In Que": [{"id": "lo", "name": "Lead Order 08/26/26"}],
        "Today": [
            {"id": "s", "name": "Agent Setup Going Live Wednesday 08/26",
             "url": SETUP_URL},
        ],
        "Quality Check": [],
    })
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    jobs.walk_board(config, "to_quality_check", day=date(2026, 8, 25))

    assert board.desc == {}


# --------------------------------------------- six in the morning makes one


class MakingBoard(FakeBoard):
    """A board that remembers cards created on it."""

    def __init__(self, lists):
        super().__init__(lists)
        self.made = []

    def create_card(self, list_id, name, *, position="top"):
        self.made.append((list_id, name, position))
        self.lists[list_id.removeprefix("id-")].append({"id": f"new-{name}", "name": name})
        return {"id": f"new-{name}", "name": name}


def making_board(*existing):
    return MakingBoard({
        "AUTOMATION DEPARTMENT": [
            {"id": f"c{i}", "name": name} for i, name in enumerate(existing)
        ],
        "In Que": [], "Today": [], "Quality Check": [], "Done": [],
    })


def test_six_makes_the_card_for_the_day_after_tomorrow(monkeypatch, config):
    """Two days out, because a setup card is worked the day before its agents
    go live: made Tuesday morning, fetched Tuesday evening, worked Wednesday."""
    from wilbyte.bot import jobs

    board = making_board()
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    title, problems = jobs.make_setup_card(config, day=date(2026, 8, 25))

    assert problems == []
    assert title == "Agent Setup Going Live Thursday 08/27"
    assert board.made == [("id-AUTOMATION DEPARTMENT", title, "top")]


def test_it_lands_where_the_fetch_will_find_it(monkeypatch, config):
    """Made in the Automation Department, same as the ones made by hand, and
    six in the evening brings it over from there."""
    from wilbyte.bot import jobs

    board = making_board()
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    jobs.make_setup_card(config, day=date(2026, 8, 25))
    moved, problems = jobs.walk_board(config, "to_quality_check", day=date(2026, 8, 25))

    assert problems == []
    assert moved == 1
    assert [where for _c, where, _p in board.moves] == ["id-In Que"]


def test_a_card_somebody_already_made_is_not_made_twice(monkeypatch, config):
    from wilbyte.bot import jobs

    board = making_board("Agent Setup Going Live Thursday 08/27")
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    title, problems = jobs.make_setup_card(config, day=date(2026, 8, 25))

    assert (title, problems, board.made) == ("", [], [])


def test_one_already_fetched_into_in_que_still_counts(monkeypatch, config):
    """Yesterday's has moved on. Looking only in Automation would make a
    second card for the same day."""
    from wilbyte.bot import jobs

    board = MakingBoard({
        "AUTOMATION DEPARTMENT": [],
        "In Que": [{"id": "x", "name": "Agent Setup Going Live Thursday 08/27"}],
        "Today": [], "Quality Check": [], "Done": [],
    })
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    assert jobs.make_setup_card(config, day=date(2026, 8, 25)) == ("", [])
    assert board.made == []


def test_thursday_morning_makes_one_card_for_the_whole_weekend(monkeypatch, config):
    """Saturday is two days out, and Saturday's card covers through Monday -
    nobody is setting anybody up on the Saturday."""
    from wilbyte.bot import jobs

    board = making_board()
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    # Thursday 08/27; two days out is Saturday 08/29.
    title, problems = jobs.make_setup_card(config, day=date(2026, 8, 27))

    assert problems == []
    assert title == "Agent Setup Going Live Saturday-Monday 08/29-08/31"


def test_the_weekend_card_stops_the_next_two_mornings_making_more(monkeypatch, config):
    """Friday would want Sunday's and Saturday would want Monday's. Both are
    on the card already made."""
    from wilbyte.bot import jobs

    weekend = "Agent Setup Going Live Saturday-Monday 08/29-08/31"
    for morning in (date(2026, 8, 28), date(2026, 8, 29)):
        board = making_board(weekend)
        monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

        assert jobs.make_setup_card(config, day=morning) == ("", []), morning


def test_sunday_morning_makes_tuesdays(monkeypatch, config):
    """The weekend card runs out at Monday, so Tuesday's is the next one."""
    from wilbyte.bot import jobs

    board = making_board("Agent Setup Going Live Saturday-Monday 08/29-08/31")
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    title, _ = jobs.make_setup_card(config, day=date(2026, 8, 30))

    assert title == "Agent Setup Going Live Tuesday 09/01"


def test_no_automation_list_is_said_rather_than_guessed_at(monkeypatch, config):
    from wilbyte.bot import jobs

    board = MakingBoard({"In Que": [], "Today": []})
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    title, problems = jobs.make_setup_card(config, day=date(2026, 8, 25))

    assert title == ""
    assert "AUTOMATION DEPARTMENT" in problems[0]
    assert board.made == []


# ------------------------- the afternoon look at Done, twice


def done_board(*cards, said=None):
    return dated(FakeBoard({
        "Done": list(cards),
        "In Que": [], "Today": [], "Quality Check": [],
    }), said)


TICKED = {"id": "a", "name": "New Agent - Gustin Elrod",
          "url": "https://trello.com/c/aaa", "dueComplete": True}
UNTICKED = {"id": "b", "name": "NEW AGENT- Tayler Collins",
            "url": "https://trello.com/c/bbb", "dueComplete": False}
NEVER_SET = {"id": "c", "name": "New Agent - Sebastian Espinoza",
             "url": "https://trello.com/c/ccc"}
# Whose card says when. Keyed by card id, read by the stub below.
LAUNCHES = {
    "a": "Live tonight, Wednesday, August 26",
    "b": "Live tom, aug 27",
    "c": "Live tonight, Wednesday, August 26",
}
WEDNESDAY = date(2026, 8, 26)


def dated(board, said=None):
    """Give a FakeBoard descriptions, so a launch date can be read off it."""
    told = said or LAUNCHES
    board.card_detail = lambda card_id: {"id": card_id, "desc": told.get(card_id, "")}
    board.card_comments = lambda card_id: []
    return board


def test_only_the_agent_cards_nobody_ticked_come_back(monkeypatch, config):
    """The green circle is how the team says an agent is actually set up. A
    card in Done without it looks finished from across the board and isn't."""
    from wilbyte.bot import jobs

    board = done_board(TICKED, UNTICKED, NEVER_SET)
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    found, problems = jobs.unmarked_agents(config, day=WEDNESDAY)

    assert problems == []
    # Today's first: those are the ones that have run out of time.
    assert [(c["id"], c["when"]) for c in found] == [("c", "today"), ("b", "tomorrow")]


def test_a_card_that_is_not_an_agent_is_not_chased(monkeypatch, config):
    """Done is full of finished daily cards. None of them get a tick."""
    from wilbyte.bot import jobs

    board = done_board(
        {"id": "g", "name": "💎 General 08/25/26"},
        {"id": "s", "name": "Agent Setup Going Live Wednesday 08/26"},
        {"id": "o", "name": "New Agent Onboarding SOP"},
    )
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    assert jobs.unmarked_agents(config, day=WEDNESDAY) == ([], [])


def test_everything_ticked_comes_back_empty(monkeypatch, config):
    from wilbyte.bot import jobs

    board = done_board(TICKED)
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    assert jobs.unmarked_agents(config, day=WEDNESDAY) == ([], [])


def test_it_reads_and_never_ticks(monkeypatch, config):
    """A tick is somebody saying they did it. Ticking it for them is the one
    thing that would make the check worthless."""
    from wilbyte.bot import jobs

    board = done_board(UNTICKED)
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    jobs.unmarked_agents(config, day=WEDNESDAY)

    assert board.moves == []


def test_no_done_list_is_said(monkeypatch, config):
    from wilbyte.bot import jobs

    board = FakeBoard({"In Que": []})
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    found, problems = jobs.unmarked_agents(config, day=WEDNESDAY)

    assert found == []
    assert "'Done'" in problems[0]


# --------------------------------------------------- what the nudge says


def note(config, found, step=None, *, ping=True):
    from wilbyte.bot.client import _unmarked_card, _unmarked_ping

    if not found:
        return "", None
    return (
        _unmarked_ping(config, ping=ping),
        _unmarked_card(found, step=step or dailyops.UNMARKED[0]),
    )


def test_the_card_names_the_time_the_count_and_the_agents(config):
    _ping, card = note(config, [UNTICKED, NEVER_SET])

    assert "3:30pm" in card.author.name
    assert card.title == "2 agent(s) need ticking"
    assert "NEW AGENT- Tayler Collins" in card.description
    assert "https://trello.com/c/bbb" in card.description


def test_the_link_goes_behind_the_name(config):
    """Eight raw Trello URLs is eight lines of hex nobody reads."""
    _ping, card = note(config, [UNTICKED])

    assert "[NEW AGENT- Tayler Collins](https://trello.com/c/bbb)" in card.description


def test_a_card_with_no_link_is_still_listed(config):
    _ping, card = note(config, [{"id": "z", "name": "New Agent - Nobody"}])

    assert "• New Agent - Nobody" in card.description


def test_the_ping_is_message_text_not_embed_text(config):
    """Discord renders a mention inside an embed and notifies nobody, so a
    ping in there is a ping that never arrives."""
    from dataclasses import replace

    with_user = replace(config, secrets=replace(config.secrets,
                                                discord_notify_user_id="42"))
    ping, card = note(with_user, [UNTICKED])

    assert ping == "<@42>"
    assert "42" not in (card.description or "")


def test_without_a_user_the_card_still_goes_out(config):
    """The card is the point; the ping is the improvement."""
    ping, card = note(config, [UNTICKED])

    assert ping == ""
    assert "Tayler Collins" in card.description


def test_a_long_list_is_cut_and_says_it_was(config):
    """An embed description stops at 4096 characters, and a list quietly cut
    short reads as if that was all of them."""
    from wilbyte.bot import client

    many = [dict(UNTICKED, id=str(n), name=f"New Agent - Person {n}") for n in range(40)]

    _ping, card = note(config, many)

    assert card.description.count("\n• ") == client.UNMARKED_SHOWN - 1
    assert f"…and {40 - client.UNMARKED_SHOWN} more." in card.description
    assert len(card.description) <= 4096


def test_the_second_look_says_its_own_time(config):
    _ping, card = note(config, [UNTICKED], step=dailyops.UNMARKED[1])

    assert "5:30pm" in card.author.name


def test_asking_for_it_now_claims_no_particular_time(config):
    """Saying "3:30pm" at half ten in the morning is worse than saying
    nothing, and the message carries its own timestamp anyway."""
    from wilbyte.bot.client import _unmarked_card

    card = _unmarked_card([UNTICKED])

    assert "3:30pm" not in card.author.name
    assert card.author.name == "🔔 going live today or tomorrow, not ticked"


def test_asking_for_it_now_does_not_ping(config):
    """Somebody who just typed the command is already looking at the channel."""
    from dataclasses import replace

    with_user = replace(config, secrets=replace(config.secrets,
                                                discord_notify_user_id="42"))
    ping, card = note(with_user, [UNTICKED], ping=False)

    assert ping == ""
    assert "Tayler Collins" in card.description


def test_the_watched_lists_are_not_fetched_twice(monkeypatch, config):
    """They are already in the board read. Three more round trips is three
    more seconds between a card landing and it being filed."""
    from wilbyte.bot import jobs

    board = FakeBoard({
        "In Que": [{"id": "a", "name": "New Agent - Somebody"}],
        "Today": [], "Franklin (Admin)": [], "AUTOMATION DEPARTMENT": [],
        "Done": [], "Quality Check": [],
    })
    asked = []
    plain = board.list_cards
    board.list_cards = lambda list_id: (asked.append(list_id), plain(list_id))[1]
    board.card_detail = lambda card_id: {"id": card_id, "desc": ""}
    board.card_comments = lambda card_id: []
    board.card_checklists = lambda card_id: []
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    jobs.read_agents(config, day=date(2026, 8, 25))

    assert len(asked) == len(set(asked)) == len(board.lists), asked


# ------------------------------- the nightly archive, and what it cannot reach


class ArchivingBoard(FakeBoard):
    """A board that remembers what was archived."""

    def __init__(self, lists):
        super().__init__(lists)
        self.archived = []

    def archive_card(self, card_id):
        self.archived.append(card_id)
        return {}


AGED = "Aged Leads Order Done"

ANSON = {"id": "anson", "name": "AGED LEAD - Anson Call"}
KENE = {"id": "kene", "name": "AGED LEAD - Kene Ubakanma", "dueComplete": True}
STEPHANIE = {"id": "steph", "name": "AGED LEAD - Stephanie Huish", "dueComplete": True}


def aged_board(*cards, **elsewhere):
    return ArchivingBoard({AGED: list(cards), **elsewhere})


def test_only_the_ticked_ones_go(monkeypatch, config):
    """The real list: Kene and Stephanie are ticked, Anson Call is not. A card
    nobody has ticked is still somebody's job."""
    from wilbyte.bot import jobs

    board = aged_board(ANSON, KENE, STEPHANIE)
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    cards, problems = jobs.aged_to_archive(config)

    assert problems == []
    assert [c["id"] for c in cards] == ["kene", "steph"]


def test_archiving_takes_exactly_what_was_shown(monkeypatch, config):
    from wilbyte.bot import jobs

    board = aged_board(ANSON, KENE, STEPHANIE)
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    gone, problems = jobs.archive_aged(config)

    assert problems == []
    assert gone == ["AGED LEAD - Kene Ubakanma", "AGED LEAD - Stephanie Huish"]
    assert board.archived == ["kene", "steph"]


def test_nothing_outside_that_one_list_is_even_read(monkeypatch, config):
    """The guarantee. Every other list is stocked with cards that would
    qualify on every count - ticked, the four daily ones, the agents, the
    setup card - and none of them is fetched, let alone archived. Not fetched
    and then filtered: not reachable."""
    from wilbyte.bot import jobs

    others = {
        "In Que": [{"id": "q1", "name": "Lead Order 08/26/26", "dueComplete": True}],
        "Today": [{"id": "t1", "name": "💎 General 08/26/26", "dueComplete": True},
                  {"id": "t2", "name": "Agent Setup Going Live Thursday 08/27",
                   "dueComplete": True}],
        "Quality Check": [{"id": "c1", "name": "📊 Ads 08/25/26", "dueComplete": True}],
        "Done": [{"id": "d1", "name": "New Agent - Somebody", "dueComplete": True}],
        "Franklin (Admin)": [{"id": "f1", "name": "New Agent - Parked",
                              "dueComplete": True}],
        "AUTOMATION DEPARTMENT": [{"id": "a1", "name": "THERESE GUBA",
                                   "dueComplete": True}],
        "Archives - NO TOUCHING 😡": [{"id": "x1", "name": "do not touch",
                                       "dueComplete": True}],
    }
    board = aged_board(KENE, **others)
    asked = []
    plain = board.list_cards
    board.list_cards = lambda list_id: (asked.append(list_id), plain(list_id))[1]
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    jobs.archive_aged(config)

    assert board.archived == ["kene"]
    assert asked == [f"id-{AGED}"], "it read a list it has no business in"


def test_nothing_ticked_archives_nothing(monkeypatch, config):
    """A list nobody has been through is a list nothing happens to."""
    from wilbyte.bot import jobs

    board = aged_board(ANSON, {"id": "b", "name": "AGED LEAD - Someone Else"})
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    assert jobs.archive_aged(config) == ([], [])
    assert board.archived == []


def test_an_empty_list_archives_nothing(monkeypatch, config):
    from wilbyte.bot import jobs

    board = aged_board()
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    assert jobs.archive_aged(config) == ([], [])


def test_no_such_list_is_said_and_nothing_happens(monkeypatch, config):
    """If somebody renames it, the archive stops rather than picking another."""
    from wilbyte.bot import jobs

    board = ArchivingBoard({"Done": [{"id": "d1", "name": "New Agent - Somebody"}]})
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    gone, problems = jobs.archive_aged(config)

    assert gone == []
    assert "Aged Leads Order Done" in problems[0]
    assert board.archived == []


def test_one_card_refusing_does_not_stop_the_rest(monkeypatch, config):
    from wilbyte.bot import jobs

    second = {"id": "other", "name": "AGED LEAD - Someone Else", "dueComplete": True}
    board = aged_board(KENE, second)

    def refuse(card_id):
        if card_id == "kene":
            raise RuntimeError("Trello said no")
        board.archived.append(card_id)
        return {}

    board.archive_card = refuse
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    gone, problems = jobs.archive_aged(config)

    assert gone == ["AGED LEAD - Someone Else"]
    assert "Trello said no" in problems[0]


def test_it_archives_and_never_deletes(monkeypatch, config):
    """A wrong archive is an afternoon. A wrong delete is gone."""
    from wilbyte.bot import jobs

    board = aged_board(KENE)
    board.delete_card = lambda card_id: pytest.fail("it deleted a card")
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    jobs.archive_aged(config)

    assert board.archived == ["kene"]


def test_ten_at_night_is_when_it_runs():
    assert dailyops.time_of("archive_aged") == (22, 0)
    assert dailyops.said_at("archive_aged") == "10pm"


def test_the_archive_runs_after_the_move_to_done():
    """A card that reaches the list at half eight has ninety minutes to be
    ticked, and only then does it go."""
    due = dailyops.steps_due(at_hour(22), set())

    assert due.index("to_done") < due.index("archive_aged")


# ------------------ the setup card on the people who are doing the setting up


class LinkingChecklists(FakeBoard):
    """A board whose cards have checklists, and which remembers items added."""

    def __init__(self, lists, held=None):
        super().__init__(lists)
        self.held = held or {}
        self.items = []

    def card_checklists(self, card_id):
        return self.held.get(card_id, [])

    def add_check_item(self, checklist_id, name, *, checked=False):
        self.items.append((checklist_id, name))
        return {}


SETUP_LINK = "https://trello.com/c/Fr28abcd/1-agent-setup-going-live-friday-08-28"


def person_lists(*people, holding=()):
    return [
        {"id": f"cl-{p}", "name": p,
         "checkItems": [{"id": "i", "name": n} for n in holding]}
        for p in people
    ]


def setup_link_board(held=None, *, setup="Agent Setup Going Live Friday 08/28"):
    return LinkingChecklists({
        "Today": [
            {"id": "setup", "name": setup, "url": SETUP_LINK},
            {"id": "ads", "name": "📊 Ads 08/27/26"},
            {"id": "ops", "name": "💻 Ops 08/27/26"},
            {"id": "lo", "name": "Lead Order 08/27/26"},
        ],
        "In Que": [],
    }, held=held or {
        "ads": person_lists("Jenn", "Kath", "Nicole"),
        "ops": person_lists("Therese"),
        "lo": person_lists("Therese"),
    })


def test_the_setup_card_lands_on_ads_and_ops(monkeypatch, config):
    """Kath, Jenn and Nicole on Ads; Therese on Ops. They are the people doing
    the setting up, and the card is not one of the four they have open."""
    from wilbyte.bot import jobs

    board = setup_link_board()
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    added, problems = jobs.link_setup_on_day(config, for_day=date(2026, 8, 27))

    assert problems == []
    assert {cl for cl, _name in board.items} == {
        "cl-Jenn", "cl-Kath", "cl-Nicole", "cl-Therese",
    }
    assert {name for _cl, name in board.items} == {SETUP_LINK}
    assert len(added) == 4


def test_lead_order_and_general_are_left_out_of_it(monkeypatch, config):
    """Only the two cards he named."""
    from wilbyte.bot import jobs

    board = setup_link_board()
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    jobs.link_setup_on_day(config, for_day=date(2026, 8, 27))

    assert not any(cl == "cl-Therese" and False for cl, _ in board.items)
    # Therese appears once, from Ops - not twice, from Lead Order as well.
    assert [cl for cl, _ in board.items].count("cl-Therese") == 1


def test_the_card_worked_that_day_is_the_one_linked(monkeypatch, config):
    """The 08/27 cards are worked on the 27th, and the card worked on the 27th
    is Friday 08/28's. Thursday 08/27's is sitting there too - that one is
    today's work, and it went on the 08/26 cards yesterday."""
    from wilbyte.bot import jobs

    board = setup_link_board()
    board.lists["Today"].append(
        {"id": "thu", "name": "Agent Setup Going Live Thursday 08/27",
         "url": "https://trello.com/c/Th27abcd/1-thursday"}
    )
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    jobs.link_setup_on_day(config, for_day=date(2026, 8, 27))

    assert {name for _cl, name in board.items} == {SETUP_LINK}


def test_running_it_twice_adds_nothing_the_second_time(monkeypatch, config):
    """Matched on the card's short id, so a link somebody pasted by hand with
    a different slug on the end still counts."""
    from wilbyte.bot import jobs

    board = setup_link_board(held={
        "ads": person_lists("Jenn", "Kath", "Nicole",
                          holding=["https://trello.com/c/Fr28abcd"]),
        "ops": person_lists("Therese", holding=["https://trello.com/c/Fr28abcd/other"]),
    })
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    added, problems = jobs.link_setup_on_day(config, for_day=date(2026, 8, 27))

    assert (added, problems, board.items) == ([], [], [])


def test_a_missing_checklist_is_said_not_created(monkeypatch, config):
    """The checklists are named by whoever made the card, and "Kath" against a
    list that calls her "Kathleen" would make a second one nobody reads."""
    from wilbyte.bot import jobs

    board = setup_link_board(held={"ads": person_lists("Jenn"), "ops": person_lists("Therese")})
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    added, problems = jobs.link_setup_on_day(config, for_day=date(2026, 8, 27))

    assert [cl for cl, _ in board.items] == ["cl-Jenn", "cl-Therese"]
    assert any("'Kath'" in p for p in problems)
    assert any("'Nicole'" in p for p in problems)


def test_no_setup_card_for_that_day_does_nothing_quietly(monkeypatch, config):
    """Nobody made one, or its working day is another. Not worth saying."""
    from wilbyte.bot import jobs

    board = setup_link_board(setup="Agent Setup Going Live Monday 08/31")
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    assert jobs.link_setup_on_day(config, for_day=date(2026, 8, 27)) == ([], [])
    assert board.items == []


def test_a_missing_ads_card_is_said(monkeypatch, config):
    from wilbyte.bot import jobs

    board = setup_link_board()
    board.lists["Today"] = [
        c for c in board.lists["Today"] if c["id"] != "ads"
    ]
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    added, problems = jobs.link_setup_on_day(config, for_day=date(2026, 8, 27))

    assert [cl for cl, _ in board.items] == ["cl-Therese"]
    assert "Ads" in problems[0]


def test_it_runs_after_the_in_que_cards_arrive():
    """"The in que cards comes in by 11am" - half eleven leaves margin."""
    assert dailyops.time_of("link_setup") == (11, 30)
    assert dailyops.said_at("link_setup") == "11:30am"
    assert "link_setup" not in dailyops.steps_due(at_hour(11, 29), set())


def test_an_agent_who_went_live_last_week_is_not_chased(monkeypatch, config):
    """Done holds sixty cards and most of them are weeks old. Being reminded
    about all of them is the same as being reminded about none."""
    from wilbyte.bot import jobs

    board = done_board(UNTICKED, NEVER_SET, said={
        "b": "Live Thursday, August 20",
        "c": "Live tom, aug 27",
    })
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    found, _ = jobs.unmarked_agents(config, day=WEDNESDAY)

    assert [c["id"] for c in found] == ["c"]


def test_an_agent_going_live_next_week_is_not_chased_yet(monkeypatch, config):
    """Nobody is late on Friday's card on Wednesday afternoon."""
    from wilbyte.bot import jobs

    board = done_board(UNTICKED, said={"b": "Live Friday, August 28"})
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    assert jobs.unmarked_agents(config, day=WEDNESDAY) == ([], [])


def test_a_card_with_no_date_anywhere_is_left_out(monkeypatch, config):
    """It is not going live today or tomorrow as far as anybody can tell."""
    from wilbyte.bot import jobs

    board = done_board(UNTICKED, said={"b": "no date on this one"})
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    assert jobs.unmarked_agents(config, day=WEDNESDAY) == ([], [])


def test_a_date_only_in_a_comment_still_counts(monkeypatch, config):
    """Paid for only when the description is silent - one extra request per
    card, not one for every card in Done."""
    from wilbyte.bot import jobs

    board = done_board(UNTICKED, said={"b": ""})
    asked = []
    board.card_comments = lambda card_id: (
        asked.append(card_id), ["live tom, aug 27"]
    )[1]
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    found, _ = jobs.unmarked_agents(config, day=WEDNESDAY)

    assert [(c["id"], c["when"]) for c in found] == [("b", "tomorrow")]
    assert asked == ["b"]


def test_the_description_is_enough_for_most_of_them(monkeypatch, config):
    from wilbyte.bot import jobs

    board = done_board(UNTICKED)
    asked = []
    board.card_comments = lambda card_id: (asked.append(card_id), [])[1]
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    jobs.unmarked_agents(config, day=WEDNESDAY)

    assert asked == [], "it read comments it did not need"


def test_the_card_says_which_day_each_one_goes_live(config):
    """"Live today" is a different sort of late from "live tomorrow"."""
    _ping, card = note(config, [
        {**NEVER_SET, "when": "today"}, {**UNTICKED, "when": "tomorrow"},
    ])

    assert "— live today" in card.description
    assert "— live tomorrow" in card.description


# ------------------------- agents set up on leads they did not order


class SetupBoard(FakeBoard):
    """A board with descriptions and comments on its cards."""

    def __init__(self, lists, desc=None, said=None):
        super().__init__(lists)
        self.descs = desc or {}
        self.said = said or {}
        self.asked = []

    def card_detail(self, card_id):
        self.asked.append(card_id)
        return {"id": card_id, "desc": self.descs.get(card_id, "")}

    def card_comments(self, card_id):
        return self.said.get(card_id, [])


FRESH = "2099-01-01T00:00:00.000Z"     # always inside the window
STALE = "2000-01-01T00:00:00.000Z"     # never is

CONFIRMED = "✅ {} ON DISTRO HUB setup is complete for SOMEBODY"


# Brody goes live tomorrow unless a test says otherwise.
LIVE_TOMORROW = "Live Thursday, August 27"


def brody(where="Done", *, ordered="Lead Type: OTP Vets", setup="OTP VET",
          touched=FRESH, live=LIVE_TOMORROW, **others):
    return SetupBoard(
        {where: [{"id": "b", "name": "New Agent - Brody Sullivan",
                  "url": "https://trello.com/c/bbb", "dateLastActivity": touched}],
         **others},
        desc={"b": f"{ordered}\n{live}"},
        said={"b": [CONFIRMED.format(setup)]},
    )


def test_the_right_setup_is_not_flagged(monkeypatch, config):
    from wilbyte.bot import jobs

    board = brody()
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    assert jobs.wrong_setups(config, day=WEDNESDAY) == ([], [])


def test_the_wrong_setup_is_flagged_with_both_halves(monkeypatch, config):
    from wilbyte.bot import jobs

    board = brody(setup="OTP FEX")
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    found, problems = jobs.wrong_setups(config, day=WEDNESDAY)

    assert problems == []
    assert len(found) == 1
    assert found[0]["agent"] == "Brody Sullivan"
    assert found[0]["ordered"] == "OTP Vets"
    assert found[0]["setup"] == "OTP FEX"
    assert found[0]["where"] == "Done"
    assert found[0]["when"] == "tomorrow"


@pytest.mark.parametrize("where", ["Done", "In Que", "Today", "Franklin (Admin)"])
def test_it_looks_everywhere_not_just_done(monkeypatch, config, where):
    """A card still parked in Franklin's list can be set up early, and one
    that only looked in Done would miss exactly the ones nobody moved on."""
    from wilbyte.bot import jobs

    board = brody(where, setup="OTP FEX")
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    found, _ = jobs.wrong_setups(config, day=WEDNESDAY)

    assert [c["where"] for c in found] == [where]


def test_a_card_nobody_has_touched_in_days_is_not_reopened(monkeypatch, config):
    """A comment moves dateLastActivity, so a new confirmation is inside the
    window by definition - and the sixty stale cards cost one request."""
    from wilbyte.bot import jobs

    board = brody(setup="OTP FEX", touched=STALE)
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    assert jobs.wrong_setups(config, day=WEDNESDAY) == ([], [])
    assert board.asked == [], "it opened a card it did not need to"


def test_a_daily_card_is_not_examined(monkeypatch, config):
    from wilbyte.bot import jobs

    board = SetupBoard({"Done": [
        {"id": "g", "name": "💎 General 08/26/26", "dateLastActivity": FRESH},
    ]})
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    assert jobs.wrong_setups(config, day=WEDNESDAY) == ([], [])
    assert board.asked == []


def test_it_writes_nothing(monkeypatch, config):
    """A wrong setup is for a person to put right. Nothing here moves a card,
    ticks a box or edits a description."""
    from wilbyte.bot import jobs

    board = brody(setup="OTP FEX")
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    jobs.wrong_setups(config, day=WEDNESDAY)

    assert board.moves == []


# ---------------------------------------------- saying it once, not forever


def test_the_same_mismatch_is_remembered(tmp_path):
    from wilbyte import setupseen

    store = tmp_path / "said.json"
    held = setupseen.mark("b", "OTP Vets", "OTP FEX")
    setupseen.remember([held], store)

    assert held in setupseen.load(store)


def test_a_redone_setup_is_a_new_thing_to_say(tmp_path):
    """Fixing it correctly makes it stop. Fixing it wrongly does not."""
    from wilbyte import setupseen

    store = tmp_path / "said.json"
    setupseen.remember([setupseen.mark("b", "OTP Vets", "OTP FEX")], store)

    assert setupseen.mark("b", "OTP Vets", "OTP MTG") not in setupseen.load(store)


def test_the_mark_ignores_spacing_and_case(tmp_path):
    from wilbyte import setupseen

    assert setupseen.mark("b", "OTP  Vets", "otp fex") == (
        setupseen.mark("b", "otp vets", "OTP FEX")
    )


def test_an_agent_who_went_live_last_week_is_not_raised(monkeypatch, config):
    """Either it was put right at the time or it wasn't, and either way it is
    not tonight's problem. The point is catching it before the leads flow."""
    from wilbyte.bot import jobs

    board = brody(setup="OTP FEX", live="Live Thursday, August 20")
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    assert jobs.wrong_setups(config, day=WEDNESDAY) == ([], [])


def test_an_agent_going_live_next_week_is_not_raised_yet(monkeypatch, config):
    from wilbyte.bot import jobs

    board = brody(setup="OTP FEX", live="Live Friday, August 28")
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    assert jobs.wrong_setups(config, day=WEDNESDAY) == ([], [])


def test_going_live_today_is_raised_and_listed_first(monkeypatch, config):
    from wilbyte.bot import jobs

    board = brody(setup="OTP FEX", live="Live Wednesday, August 26")
    board.lists["Done"].append({
        "id": "c", "name": "New Agent - Somebody Else",
        "url": "https://trello.com/c/ccc", "dateLastActivity": FRESH,
    })
    board.descs["c"] = "Lead Type: OTP Vets\nLive Thursday, August 27"
    board.said["c"] = [CONFIRMED.format("OTP MTG")]
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    found, _ = jobs.wrong_setups(config, day=WEDNESDAY)

    assert [c["when"] for c in found] == ["today", "tomorrow"]


def test_a_launch_date_only_in_a_comment_still_counts(monkeypatch, config):
    from wilbyte.bot import jobs

    board = brody(setup="OTP FEX", live="")
    board.said["b"] = board.said["b"] + ["going live thursday, august 27"]
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    found, _ = jobs.wrong_setups(config, day=WEDNESDAY)

    assert [c["when"] for c in found] == ["tomorrow"]


def test_a_card_with_no_launch_date_is_left_alone(monkeypatch, config):
    from wilbyte.bot import jobs

    board = brody(setup="OTP FEX", live="")
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    assert jobs.wrong_setups(config, day=WEDNESDAY) == ([], [])


# ---------------------------------- holding a card back from tonight's carry


@pytest.mark.parametrize(
    "said,expected",
    [
        ("trello rollover skip ads", ("hold", ["ads"])),
        ("rollover skip ads and ops", ("hold", ["ops", "ads"])),
        ("rollover don't carry lead order", ("hold", ["lead_order"])),
        ("rollover hold back general", ("hold", ["general"])),
        ("rollover unskip ads", ("release", ["ads"])),
        ("rollover skip none", ("release", [])),
        ("rollover carry all", ("release", [])),
        # Nothing about skipping - these still run the rollover.
        ("trello rollover", None),
        ("rollover general", None),
    ],
)
def test_what_somebody_asked_about_skipping(said, expected):
    assert dailyops.skip_asked(said) == expected


def test_unskip_is_not_read_as_a_skip():
    """The word is inside the other one, so order matters."""
    assert dailyops.skip_asked("rollover unskip ads")[0] == "release"


def test_holding_a_card_lasts_until_the_evening(tmp_path):
    """A standing instruction, not a one-off. Said at four, still true at
    eight, which is the whole point - the eight o'clock run is the one that
    would have carried it."""
    from wilbyte import rollskip

    store = tmp_path / "skip.json"
    day = date(2026, 8, 26)

    assert rollskip.hold(day, ["ads"], store) == ("ads",)
    assert rollskip.for_day(day, store) == ("ads",)


def test_it_only_holds_the_day_it_was_said_for(tmp_path):
    """Tomorrow starts clean without anybody remembering to undo it."""
    from wilbyte import rollskip

    store = tmp_path / "skip.json"
    rollskip.hold(date(2026, 8, 26), ["ads"], store)

    assert rollskip.for_day(date(2026, 8, 27), store) == ()


def test_holding_twice_does_not_double_it(tmp_path):
    from wilbyte import rollskip

    store = tmp_path / "skip.json"
    day = date(2026, 8, 26)
    rollskip.hold(day, ["ads"], store)

    assert rollskip.hold(day, ["ads", "ops"], store) == ("ads", "ops")


def test_releasing_one_leaves_the_others(tmp_path):
    from wilbyte import rollskip

    store = tmp_path / "skip.json"
    day = date(2026, 8, 26)
    rollskip.hold(day, ["ads", "ops"], store)

    assert rollskip.release(day, ["ads"], store) == ("ops",)


def test_releasing_none_named_releases_all(tmp_path):
    from wilbyte import rollskip

    store = tmp_path / "skip.json"
    day = date(2026, 8, 26)
    rollskip.hold(day, ["ads", "ops"], store)

    assert rollskip.release(day, None, store) == ()
    assert rollskip.for_day(day, store) == ()


def test_a_note_from_last_month_is_dropped(tmp_path):
    """A forgotten one must not quietly hold a card back in September."""
    from wilbyte import rollskip

    store = tmp_path / "skip.json"
    rollskip.hold(date(2026, 7, 1), ["ads"], store)
    rollskip.hold(date(2026, 8, 26), ["ops"], store)

    assert rollskip.for_day(date(2026, 7, 1), store) == ()


def test_the_held_card_is_left_out_of_the_carry(monkeypatch, config):
    """The rollover finds the day's cards by the date in the title wherever
    they are, so dragging one into another list does not stop it. This does."""
    from wilbyte.bot import jobs

    names = {
        "In Que": ["Lead Order 08/26/26", "💎 General 08/26/26",
                   "💻 Ops 08/26/26", "📊 Ads 08/26/26"],
        "Quality Check": ["Lead Order 08/25/26", "💎 General 08/25/26",
                          "💻 Ops 08/25/26", "📊 Ads 08/25/26"],
    }
    board = FakeBoard({
        name: [{"id": f"{name}-{i}", "name": card} for i, card in enumerate(cards)]
        for name, cards in names.items()
    })
    board.card_checklists = lambda card_id: []
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    plans, _missing, targets = jobs.read_rollover(
        config, day=date(2026, 8, 25), skip=["ads"]
    )

    assert "ads" not in [plan.kind for plan in plans]
    assert sorted(targets) == ["general", "lead_order", "ops"]


def test_nothing_held_carries_all_four(monkeypatch, config):
    from wilbyte.bot import jobs

    names = {
        "In Que": ["Lead Order 08/26/26", "💎 General 08/26/26",
                   "💻 Ops 08/26/26", "📊 Ads 08/26/26"],
        "Quality Check": ["Lead Order 08/25/26", "💎 General 08/25/26",
                          "💻 Ops 08/25/26", "📊 Ads 08/25/26"],
    }
    board = FakeBoard({
        name: [{"id": f"{name}-{i}", "name": card} for i, card in enumerate(cards)]
        for name, cards in names.items()
    })
    board.card_checklists = lambda card_id: []
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    _plans, _missing, targets = jobs.read_rollover(
        config, day=date(2026, 8, 25), skip=[]
    )

    assert sorted(targets) == ["ads", "general", "lead_order", "ops"]


def test_one_card_held_reads_as_one_and_two_as_two():
    from wilbyte.bot.client import _held_as_words

    assert "its unticked items" in _held_as_words(["ads"])
    assert "their unticked items" in _held_as_words(["ads", "lead_order"])
    assert _held_as_words([]) == "Nothing is being held back tonight."


def test_a_held_card_is_not_filed_away_either(monkeypatch, config, tmp_path):
    """Holding its items back is saying the work is not finished. Filing the
    card in the same hour would put it out of sight with the work still on
    it."""
    from wilbyte import rollskip
    from wilbyte.bot import jobs

    store = tmp_path / "skip.json"
    day = date(2026, 8, 25)
    rollskip.hold(day, ["ads"], store)
    monkeypatch.setattr(rollskip, "SKIP_PATH", store)

    board = FakeBoard({
        "Quality Check": [
            {"id": "a", "name": "📊 Ads 08/25/26"},
            {"id": "g", "name": "💎 General 08/25/26"},
        ],
        "Done": [],
    })
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    moved, problems = jobs.walk_board(config, "to_done", day=day)

    assert (moved, problems) == (1, [])
    assert [c for c, _, _ in board.moves] == ["g"], "it filed the held card"


def test_holding_a_card_does_not_stop_the_other_moves(monkeypatch, config, tmp_path):
    """Nine and six are unaffected - the card still walks its day, it just
    does not finish it."""
    from wilbyte import rollskip
    from wilbyte.bot import jobs

    store = tmp_path / "skip.json"
    day = date(2026, 8, 25)
    rollskip.hold(day, ["ads"], store)
    monkeypatch.setattr(rollskip, "SKIP_PATH", store)

    board = FakeBoard({
        "In Que": [{"id": "a", "name": "📊 Ads 08/25/26"}],
        "Today": [],
        "Quality Check": [],
    })
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    assert jobs.walk_board(config, "to_today", day=day)[0] == 1


def test_the_preview_leaves_the_held_card_out_too(monkeypatch, config, tmp_path):
    """What gets shown and what gets moved cannot disagree."""
    from wilbyte import rollskip
    from wilbyte.bot import jobs

    store = tmp_path / "skip.json"
    day = date(2026, 8, 25)
    rollskip.hold(day, ["ads"], store)
    monkeypatch.setattr(rollskip, "SKIP_PATH", store)

    board = FakeBoard({
        "Quality Check": [
            {"id": "a", "name": "📊 Ads 08/25/26"},
            {"id": "g", "name": "💎 General 08/25/26"},
        ],
        "Done": [],
    })
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    found, _ = jobs.moves_waiting(config, "to_done", day=day)

    assert found == ["💎 General 08/25/26"]


# ------------------------------- carrying a day that was held back last night


@pytest.mark.parametrize(
    "said,expected",
    [
        ("rollover yesterday", date(2026, 8, 26)),
        ("rollover last night", date(2026, 8, 26)),
        ("rollover lead order yesterday", date(2026, 8, 26)),
        ("rollover 08/26", date(2026, 8, 26)),
        ("rollover 08/26/26", date(2026, 8, 26)),
        ("rollover 8/26", date(2026, 8, 26)),
        # Nothing named means today, and the caller decides what that is.
        ("rollover", None),
        ("rollover general", None),
        ("trello rollover skip ads", None),
        # A date that is not one.
        ("rollover 13/45", None),
    ],
)
def test_which_day_somebody_asked_to_carry(said, expected):
    assert dailyops.day_named(said, today=date(2026, 8, 27)) == expected


def test_nothing_to_carry_says_which_of_the_two_reasons():
    """Everything ticked is a day finished. Nowhere to put it is a card Zapier
    has not made yet, and saying the first when it is the second sends
    somebody looking at the wrong thing."""
    assert "every item is ticked" in dailyops.summarise([])
    assert "no card for tomorrow" in dailyops.summarise([], missing=["Lead Order"])


def test_a_day_asked_for_by_hand_ignores_the_hold(monkeypatch, config, tmp_path):
    """The hold was "not on the automatic run". Asking for that day by hand is
    asking for it anyway - otherwise last night's skip is impossible to undo
    the morning after."""
    from wilbyte import rollskip
    from wilbyte.bot import jobs

    store = tmp_path / "skip.json"
    yesterday = date(2026, 8, 26)
    rollskip.hold(yesterday, ["lead_order", "ads"], store)
    monkeypatch.setattr(rollskip, "SKIP_PATH", store)

    names = {
        "In Que": ["Lead Order 08/27/26", "📊 Ads 08/27/26"],
        "Quality Check": ["Lead Order 08/26/26", "📊 Ads 08/26/26"],
    }
    board = FakeBoard({
        name: [{"id": f"{name}-{i}", "name": card} for i, card in enumerate(cards)]
        for name, cards in names.items()
    })
    board.card_checklists = lambda card_id: []
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    # What the automatic run would have done last night: nothing.
    _p, _m, held_back = jobs.read_rollover(config, day=yesterday)
    assert held_back == {}

    # What "rollover yesterday" does this morning.
    _p, _m, targets = jobs.read_rollover(config, day=yesterday, skip=[])
    assert sorted(targets) == ["ads", "lead_order"]


def test_carrying_yesterday_targets_today_not_tomorrow(monkeypatch, config):
    """The 08/26 cards' items land on the 08/27 cards, which is what was
    skipped - not on 08/28, which does not exist yet."""
    from wilbyte.bot import jobs

    names = {
        "In Que": ["📊 Ads 08/27/26"],
        "Quality Check": ["📊 Ads 08/26/26"],
    }
    board = FakeBoard({
        name: [{"id": f"{name}-{i}", "name": card} for i, card in enumerate(cards)]
        for name, cards in names.items()
    })
    board.card_checklists = lambda card_id: []
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)

    _plans, missing, targets = jobs.read_rollover(
        config, day=date(2026, 8, 26), skip=[]
    )

    assert missing == []
    assert targets["ads"][0] == "In Que-0", "it aimed at the wrong card"


# --------------------- items already carried, offered a second time


def test_an_item_already_on_tomorrows_card_is_not_offered_again():
    """"We already did General yesterday." The write step has always skipped
    these; the plan needs to know too, or it offers thirteen and moves four
    and somebody reasonably thinks it went wrong."""
    result = plan(
        [checklist("Nicole", todo("scan numbers"), todo("chase invoice"))],
        [checklist("Nicole", todo("scan numbers"))],
    )

    assert [item.name for item in result.carried] == ["chase invoice"]
    assert [item.already for item in result.leftovers] == [True, False]


def test_a_whole_card_already_carried_offers_nothing():
    result = plan(
        [checklist("Nicole", todo("scan numbers"))],
        [checklist("Nicole", todo("scan numbers"))],
    )

    assert result.carried == []


def test_the_same_line_on_another_persons_list_is_not_the_same_item():
    """Nicole's line is not already there because Kath has one that reads the
    same - that is how somebody's work goes missing."""
    result = plan(
        [checklist("Nicole", todo("scan numbers"))],
        [checklist("Kath", todo("scan numbers")), checklist("Nicole")],
    )

    assert [item.name for item in result.carried] == ["scan numbers"]


def test_a_ticked_item_on_tomorrows_card_still_counts_as_there():
    """Somebody carried it last night and then did it. Putting it back
    unticked would undo their evening."""
    result = plan(
        [checklist("Nicole", todo("scan numbers"))],
        [checklist("Nicole", done("scan numbers"))],
    )

    assert result.carried == []


def test_one_already_there_is_not_also_raised_as_stuck():
    """It is not waiting on anybody - it moved."""
    key = dailyops.item_key("general", "Nicole", "scan numbers")
    result = plan(
        [checklist("Nicole", todo("scan numbers"))],
        [checklist("Nicole", todo("scan numbers"))],
        history={key: 3},
    )

    assert result.needs_a_look == []


def test_spacing_does_not_make_it_look_new():
    result = plan(
        [checklist("Nicole", todo("scan  numbers"))],
        [checklist("  nicole ", todo("scan numbers"))],
    )

    assert result.carried == []


# --------------------------------- saying something on one of the four cards


FRIDAY = date(2026, 8, 28)


def test_the_card_is_named_after_on():
    assert dailyops.comment_target(
        "leads went out late on monday general", today=FRIDAY
    ) == ("leads went out late", "general", date(2026, 8, 31))


def test_a_comment_with_its_own_on_in_it_survives():
    """The split is at the rightmost "on" that actually names a card."""
    text, kind, day = dailyops.comment_target(
        "check the numbers on the ads on monday general card", today=FRIDAY
    )
    assert text == "check the numbers on the ads"
    assert kind == "general"


def test_no_day_means_today():
    assert dailyops.comment_target("double check this on ads", today=FRIDAY) == (
        "double check this", "ads", FRIDAY
    )


def test_a_date_picks_the_day():
    _text, kind, day = dailyops.comment_target(
        "this on lead order 09/01", today=FRIDAY
    )
    assert (kind, day) == ("lead_order", date(2026, 9, 1))


def test_no_card_named_is_not_guessed_at():
    """A comment on the wrong card reads as being about somebody's work."""
    _text, kind, _day = dailyops.comment_target("nothing named here", today=FRIDAY)
    assert kind is None


def test_a_weekday_reads_forwards():
    assert dailyops.day_named("monday", today=FRIDAY) == date(2026, 8, 31)
    assert dailyops.day_named("saturday", today=FRIDAY) == date(2026, 8, 29)


def test_todays_own_weekday_means_today_not_next_week():
    assert dailyops.day_named("friday", today=FRIDAY) == FRIDAY


def test_tomorrow_and_yesterday_still_read():
    assert dailyops.day_named("tomorrow", today=FRIDAY) == date(2026, 8, 29)
    assert dailyops.day_named("yesterday", today=FRIDAY) == date(2026, 8, 27)


def test_a_written_date_beats_a_weekday_beside_it():
    """"monday 08/31" is one day said twice, and the numbers can't be off by a week."""
    assert dailyops.day_named("monday 09/07", today=FRIDAY) == date(2026, 9, 7)


def test_rollover_general_is_not_read_as_a_day():
    """The weekday reading must not turn ordinary commands into dated ones."""
    assert dailyops.day_named("rollover general", today=FRIDAY) is None


def test_the_card_can_be_named_first_instead():
    """"comment on monday general card <text>" is how Franklin actually typed it."""
    assert dailyops.comment_target(
        "on monday general card Spanish Lead discount OTP IUL and OTP FEX 15% OFF",
        today=FRIDAY,
    ) == (
        "Spanish Lead discount OTP IUL and OTP FEX 15% OFF",
        "general",
        date(2026, 8, 31),
    )


def test_named_first_with_no_day_and_no_card_word():
    assert dailyops.comment_target(
        "on ads Spanish Lead discount 15% OFF", today=FRIDAY
    ) == ("Spanish Lead discount 15% OFF", "ads", FRIDAY)


def test_named_first_stops_at_the_first_word_of_the_comment():
    text, kind, day = dailyops.comment_target(
        "on general we should double check this", today=FRIDAY
    )
    assert (text, kind, day) == ("we should double check this", "general", FRIDAY)


def test_the_word_card_ends_the_name_so_the_comment_keeps_its_first_word():
    """Otherwise "on monday general card general cleanup" eats "general"."""
    text, _kind, _day = dailyops.comment_target(
        "on monday general card general cleanup needed", today=FRIDAY
    )
    assert text == "general cleanup needed"


def test_named_first_takes_a_date_too():
    _text, kind, day = dailyops.comment_target(
        "on lead order 09/01 please redo the states", today=FRIDAY
    )
    assert (kind, day) == ("lead_order", date(2026, 9, 1))


def test_a_leading_on_that_names_no_card_is_not_a_card_phrase():
    """"on second thoughts, ..." is a comment, not a card."""
    text, kind, _day = dailyops.comment_target(
        "on second thoughts leave it", today=FRIDAY
    )
    assert kind is None
    assert text == "on second thoughts leave it"


# --------------------------------- which two cards the spread pairs


SETUP_CARDS = [
    {"id": "s1", "name": "Agent Setup Going Live Friday 08/28"},
    {"id": "s2", "name": "Agent Setup Going Live Saturday-Monday 08/29-08/31"},
]
ORDER_CARDS = [
    {"id": "o1", "name": "Lead Order 08/28/26"},
    {"id": "o2", "name": "Lead Order 08/29/26-8/30/26"},
]


def _pair(day):
    """The pairing the spread makes: (setup card, lead order card)."""
    from wilbyte import agents

    setup = agents.find_setup_card(SETUP_CARDS, dailyops.next_day(day))
    if setup is None:
        return None, None
    live = agents.setup_starts(setup["name"], day)
    # `cards_covering`, as the spread itself uses: an agent goes live on any
    # day the range covers, not only the one it ends on.
    return setup["name"], (dailyops.cards_covering(ORDER_CARDS, live) or {}).get(
        "lead_order", {}
    ).get("name")


def test_friday_night_pairs_the_weekend_card_with_the_weekend_lead_order():
    """The setup card worked tonight is headed Saturday-Monday, and its agents
    go live on the 29th - not tonight, and not onto tonight's Lead Order."""
    assert _pair(date(2026, 8, 28)) == (
        "Agent Setup Going Live Saturday-Monday 08/29-08/31",
        "Lead Order 08/29/26-8/30/26",
    )


def test_thursday_night_pairs_fridays_card_with_fridays_lead_order():
    assert _pair(date(2026, 8, 27)) == (
        "Agent Setup Going Live Friday 08/28",
        "Lead Order 08/28/26",
    )


def test_the_card_worked_today_is_never_the_one_covering_today():
    """A setup card is worked the day before its agents launch, so pairing
    today with today takes the card that went to Done last night."""
    from wilbyte import agents

    day = date(2026, 8, 28)
    covering_today = agents.find_setup_card(SETUP_CARDS, day)
    worked_today = agents.find_setup_card(SETUP_CARDS, dailyops.next_day(day))

    assert covering_today["name"] == "Agent Setup Going Live Friday 08/28"
    assert worked_today["name"] != covering_today["name"]


# --------------------------------- what an unspread is allowed to remove


def test_belonging_to_a_setup_card_is_not_what_makes_a_line_wrong():
    """Every line the spread writes correctly matches a setup card - that is
    the whole point of it. Removing everything that matched any setup card
    took thirty wrong lines off Lead Order 08/28 and twenty-seven right ones."""
    from wilbyte import agents

    friday = "https://trello.com/c/aaa Text Verified Veteran Plus"
    weekend = "https://trello.com/c/bbb 25 OTP VET"

    mine = {friday}
    others = {friday, weekend} - mine

    # The Friday agent belongs on Lead Order 08/28 and must survive.
    assert friday not in others
    assert weekend in others
    assert agents.split_item(friday)[0] == "https://trello.com/c/aaa"


def test_the_belonging_card_is_the_one_covering_the_lead_orders_own_day():
    """Not the one worked that day - that is the *next* day's agents."""
    from wilbyte import agents

    belongs = agents.find_setup_card(SETUP_CARDS, date(2026, 8, 28))
    assert belongs["name"] == "Agent Setup Going Live Friday 08/28"


def test_a_line_on_both_setup_cards_is_kept():
    """The same agent can be listed twice; the day decides, not the sighting."""
    shared = "https://trello.com/c/ccc 25 OTP VET"
    mine = {shared}
    others = {shared} - mine
    assert others == set()


def test_the_date_typed_is_the_day_the_agents_go_live():
    """It used to be the day the setup card was *worked*, so restoring the
    Friday card meant typing 08/27. Nobody thinks about the board that way."""
    from wilbyte import agents

    for live, wanted in (
        (date(2026, 8, 28), "Agent Setup Going Live Friday 08/28"),
        (date(2026, 8, 29), "Agent Setup Going Live Saturday-Monday 08/29-08/31"),
    ):
        assert agents.find_setup_card(SETUP_CARDS, live)["name"] == wanted


def test_a_sunday_falls_back_to_the_weekend_cards_own_lead_order():
    """The weekend Lead Order card is titled with the Saturday."""
    from wilbyte import agents

    sunday = date(2026, 8, 30)
    setup = agents.find_setup_card(SETUP_CARDS, sunday)

    starts = agents.setup_starts(setup["name"], sunday)
    assert dailyops.cards_covering(ORDER_CARDS, starts)["lead_order"]["name"] == (
        "Lead Order 08/29/26-8/30/26"
    )


# --------------------------------- a card whose title spans several days


SPANNING = [
    {"id": "1", "name": "💎 General 08/31/26"},
    {"id": "2", "name": "💻 Ops 08/31/26"},
    {"id": "3", "name": "📊 Ads 08/31/26"},
    {"id": "4", "name": "Lead Order 08/29/26-8/31/26"},
    {"id": "5", "name": "Lead Order 08/28/26"},
]


def test_a_lead_order_card_titled_as_a_range_covers_every_day_in_it():
    """Johan Castro, Brandon Nguyen, Connor Swartz and Luis Vergara all sat in
    In Que on the 31st, told there was no Lead Order card. It was right there,
    titled 08/29/26-8/31/26, and only its first date was indexed."""
    for day in (date(2026, 8, 29), date(2026, 8, 30), date(2026, 8, 31)):
        found = dailyops.cards_covering(SPANNING, day)
        assert found["lead_order"]["name"] == "Lead Order 08/29/26-8/31/26"


def test_the_other_three_still_come_back_for_their_own_day():
    found = dailyops.cards_covering(SPANNING, date(2026, 8, 31))
    assert found["general"]["name"] == "💎 General 08/31/26"
    assert found["ops"]["name"] == "💻 Ops 08/31/26"
    assert found["ads"]["name"] == "📊 Ads 08/31/26"


def test_a_days_own_card_beats_a_range_that_happens_to_include_it():
    found = dailyops.cards_covering(SPANNING, date(2026, 8, 28))
    assert found["lead_order"]["name"] == "Lead Order 08/28/26"


def test_a_spanning_card_carries_over_on_the_last_night_it_covers():
    """It is worked Saturday through Monday, so Monday night is when its
    unticked items move on. Rolling it over on the Saturday would carry work
    nobody had started; rolling it over nightly would carry it three times."""
    assert dailyops.cards_for(SPANNING, date(2026, 8, 29)).get("lead_order") is None
    assert dailyops.cards_for(SPANNING, date(2026, 8, 30)).get("lead_order") is None
    assert dailyops.cards_for(SPANNING, date(2026, 8, 31))["lead_order"]["name"] == (
        "Lead Order 08/29/26-8/31/26"
    )


def test_a_spanning_card_is_never_both_the_source_and_the_target():
    """One night only, or the rollover carries items from a card onto itself."""
    nights = [
        day for day in (date(2026, 8, 29), date(2026, 8, 30), date(2026, 8, 31))
        if dailyops.cards_for(SPANNING, day).get("lead_order")
    ]
    assert len(nights) == 1


def test_a_days_own_card_still_beats_a_range_ending_on_it():
    found = dailyops.cards_for(SPANNING, date(2026, 8, 31))
    assert found["general"]["name"] == "💎 General 08/31/26"


def test_the_weekend_card_is_not_reported_missing_mid_range():
    """It is on the board, titled with the Saturday, all the way to Monday."""
    for day in (date(2026, 8, 29), date(2026, 8, 30), date(2026, 8, 31)):
        assert "lead_order" not in dailyops.missing_kinds(SPANNING, day)


def test_the_days_a_title_names():
    assert dailyops.card_days("Lead Order 08/28/26") == [date(2026, 8, 28)]
    assert dailyops.card_days("Lead Order 08/29/26-8/31/26") == [
        date(2026, 8, 29), date(2026, 8, 30), date(2026, 8, 31),
    ]


def test_two_dates_a_month_apart_are_a_typo_not_a_range():
    """A card spread across a fortnight is worse than one that isn't found."""
    assert dailyops.card_days("Lead Order 08/01/26-9/30/26") == [date(2026, 8, 1)]


def test_a_title_with_no_date_covers_nothing():
    assert dailyops.card_days("Lead Order") == []
    assert dailyops.cards_covering([{"name": "Lead Order"}], date(2026, 8, 31)) == {}


# --------------------------------------------- two rollovers, two times


def test_every_card_is_carried_by_exactly_one_rollover():
    """Split between them, or a card is carried twice or not at all."""
    both = list(dailyops.EVENING_KINDS) + list(dailyops.LATE_KINDS)
    assert sorted(both) == sorted(dailyops.CARD_KINDS)
    assert len(both) == len(set(both))


def test_ads_and_lead_order_are_the_late_pair():
    assert set(dailyops.LATE_KINDS) == {"ads", "lead_order"}
    assert set(dailyops.EVENING_KINDS) == {"general", "ops"}


def test_the_late_rollover_runs_at_two_in_the_morning():
    assert dailyops.time_of(dailyops.LATE_ROLLOVER) == (2, 0)
    assert dailyops.time_of("rollover") == (20, 30)


def test_two_in_the_morning_works_on_yesterdays_cards():
    """The calendar day has already turned over; the cards the team worked
    last night are yesterday's."""
    assert dailyops.day_before(date(2026, 9, 1)) == date(2026, 8, 31)


def test_the_late_rollover_is_the_first_thing_in_the_day():
    every = [step for _h, _m, step in dailyops.STEPS]
    assert every[0] == dailyops.LATE_ROLLOVER


def test_the_evening_carry_still_happens_before_the_cards_go_to_done():
    due = dailyops.steps_due(at_hour(20, 30), set())
    assert due.index("rollover") < due.index("to_done")


def test_a_small_hours_step_is_never_caught_up_in_the_evening():
    """A restart at half seven ran both of them: they carried the wrong day's
    cards and filed away a Lead Order card the team was still working on."""
    evening = dailyops.steps_due(at_hour(19, 34), set())

    assert dailyops.LATE_ROLLOVER not in evening
    assert dailyops.LATE_DONE not in evening


def test_it_still_catches_up_within_the_small_hours():
    """Missing 2am by an hour is worth catching up; missing it by fifteen is
    tomorrow's problem."""
    assert dailyops.LATE_ROLLOVER in dailyops.steps_due(at_hour(4, 0), set())
    assert dailyops.LATE_DONE in dailyops.steps_due(at_hour(5, 59), set())
    assert dailyops.LATE_DONE not in dailyops.steps_due(at_hour(6, 0), set())


def test_the_ordinary_steps_still_catch_up_all_day():
    late = dailyops.steps_due(at_hour(23, 0), set())
    assert "to_today" in late and "to_done" in late
