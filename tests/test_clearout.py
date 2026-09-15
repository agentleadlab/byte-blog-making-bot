"""Closing an agent down: their sheet kept, their conversation kept, then gone."""

from __future__ import annotations

from datetime import datetime

import pytest

from wilbyte import clearout


def channel(name, **kwargs):
    return clearout.Channel(channel_id=kwargs.pop("channel_id", "c1"), name=name, **kwargs)


# ------------------------------------------------- which channel is theirs


@pytest.mark.parametrize(
    "name, called",
    [
        ("Jay Rodriguez", "jay-rodriguez"),
        ("Jay Rodriguez", "jayrodriguez"),
        ("Jay Rodriguez", "jay-rodriguez-vets"),
        ("Jay Rodriguez", "💎 jay rodriguez"),
        ("jay rodriguez", "Jay-Rodriguez"),
    ],
)
def test_a_name_finds_the_channel_however_discord_wrote_it(name, called):
    assert clearout.matches(name, channel(called)) is True


@pytest.mark.parametrize("called", ["connor-knudsen", "general", "leads-2024"])
def test_somebody_elses_channel_is_not_theirs(called):
    assert clearout.matches("Jay Rodriguez", channel(called)) is False


def test_the_exact_one_comes_first():
    """More than one coming back is the answer rather than a failure — which
    of two is theirs is a question for somebody who knows."""
    found = clearout.channels_for("Jay Rodriguez", [
        channel("jay-rodriguez-vets"),
        channel("jay-rodriguez"),
        channel("connor-knudsen"),
    ])

    assert [one.name for one in found] == ["jay-rodriguez", "jay-rodriguez-vets"]


@pytest.mark.parametrize(
    "called",
    ["general", "announcements", "rules", "welcome", "admin-team", "📋 General"],
)
def test_the_server_s_own_channels_are_never_anybody_s(called):
    """Deleting one of these is the mistake this is built to make
    impossible."""
    assert clearout.off_limits(channel(called)) is True
    assert clearout.channels_for("general", [channel(called)]) == []


def test_an_agent_channel_is_not_off_limits():
    assert clearout.off_limits(channel("jay-rodriguez")) is False


# ------------------------------------------------------ what gets kept


def _plan(**kwargs):
    kwargs.setdefault("name", "Jay Rodriguez")
    kwargs.setdefault("channel", channel("jay-rodriguez"))
    return clearout.Plan(**kwargs)


def test_the_row_keeps_the_sheet_link_above_all():
    """After the channel is deleted that link is the only way back to what was
    delivered, and it lives in a Trello comment nobody will think to open."""
    row = clearout.row_for(
        _plan(sheet="https://docs.google.com/spreadsheets/d/abc"),
        when=datetime(2026, 9, 15, 9, 0),
    )

    assert row == [
        "Jay Rodriguez",
        "https://docs.google.com/spreadsheets/d/abc",
        "jay-rodriguez",
        "2026-09-15",
    ]


def test_the_picture_is_named_after_them_and_the_day():
    said = clearout.picture_name(_plan(), when=datetime(2026, 9, 15))

    assert said == "Jay Rodriguez — 2026-09-15.png"


def test_a_name_with_awkward_characters_still_makes_a_filename():
    said = clearout.picture_name(
        _plan(name="Jay / Rodriguez: the 2nd"), when=datetime(2026, 9, 15),
    )

    assert "/" not in said
    assert said.endswith(".png")


# ------------------------------------------------------- the picture itself


def test_the_conversation_becomes_a_page_that_can_be_photographed():
    page = clearout.as_page(_plan(), [
        clearout.Said(who="Jay Rodriguez", when="Sep 3", text="got the leads thanks"),
        clearout.Said(who="Therese", when="Sep 3", text="great", attachments=2),
    ])

    assert "got the leads thanks" in page
    assert "2 attachments" in page
    assert "#jay-rodriguez" in page
    assert "dataset.ready" in page


def test_what_somebody_typed_cannot_become_markup():
    """A client who writes "<script>" in a channel does not get to write the
    evidence of their own conversation."""
    page = clearout.as_page(_plan(), [
        clearout.Said(who="<b>Jay", when="", text="<script>alert(1)</script>"),
    ])

    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_an_empty_channel_still_makes_a_picture():
    page = clearout.as_page(_plan(), [])

    assert "Nothing was said in this channel" in page


# ---------------------------------------------- what is said before anything


def test_it_says_what_it_found_before_anything_irreversible():
    said = clearout.describe(_plan(
        sheet="https://docs.google.com/spreadsheets/d/abc",
        member_id="42", member_name="Jay Rodriguez#1234",
    ))

    assert "#jay-rodriguez" in said
    assert "spreadsheets/d/abc" in said
    assert "Jay Rodriguez#1234" in said


def test_a_missing_sheet_is_said_plainly_rather_than_left_out():
    said = clearout.describe(_plan())

    assert "none found on their card" in said


def test_somebody_who_already_left_has_nobody_to_ban():
    said = clearout.describe(_plan(sheet="x"))

    assert "not in the server" in said


def test_no_channel_at_all_is_not_a_plan():
    plan = clearout.Plan(name="Jay Rodriguez")

    assert plan.ready is False
    assert "no channel of theirs" in clearout.describe(plan)


def test_a_plan_with_a_problem_on_it_is_not_ready():
    plan = _plan(problems=["two channels match that name"])

    assert plan.ready is False


def test_an_agent_whose_name_is_a_server_word_keeps_their_channel():
    """The rule is about the whole name rather than a match somewhere in it."""
    assert clearout.off_limits(channel("grant-rules")) is False
    assert clearout.off_limits(channel("rule-hernandez")) is False
    assert clearout.matches("Grant Rules", channel("grant-rules")) is True
