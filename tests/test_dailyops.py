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
