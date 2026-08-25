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
    """Records what a rollover would send, and can be told to refuse."""

    def __init__(self, *, fail_on=None):
        self.checklists_made = []
        self.items_added = []
        self.fail_on = fail_on

    def create_checklist(self, card_id, name):
        self.checklists_made.append((card_id, name))
        return {"id": f"list-{name}"}

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


def apply_with(monkeypatch, config, plan, targets, *, fail_on=None, store=None):
    from datetime import date as _date

    from wilbyte import carried
    from wilbyte.bot import jobs

    board = FakeTrello(fail_on=fail_on)
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
