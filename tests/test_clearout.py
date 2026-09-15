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


# ---------------------------------------- nothing goes until it has been asked

# Franklin: "make sure it ask me first before delting and kicking people".
# Reading the code and seeing that it asks is not the same as the suite
# refusing to let it stop asking, which is what these are for.


class Pressed:
    """A stubbed ConfirmView. Answers the presses in `says`, in order."""

    says = [True, True]

    def __init__(self, **kw):
        told = type(self).says
        self.confirmed = told.pop(0) if told else False
        self.answered = True
        self.label = kw.get("label", "")
        self.danger = kw.get("danger", False)

    async def wait(self):
        return None


class Member:
    def __init__(self, name):
        self.id, self.display_name, self.name = 7, name, name

    def __str__(self):
        return self.display_name


class Channel:
    def __init__(self, name):
        self.id, self.name, self.category = 11, name, None
        self.deleted = False

    async def history(self, limit=0):
        for one in ():
            yield one

    async def delete(self, reason=""):
        self.deleted = True


class Guild:
    def __init__(self, channel, member):
        self.id, self.name = 3, "Agent Lead Lab Clients"
        self.text_channels = [channel] if channel else []
        self.members = [member] if member else []
        self.banned = []

    def get_channel(self, channel_id):
        return next((one for one in self.text_channels if one.id == channel_id), None)

    async def ban(self, member, reason="", delete_message_days=0):
        self.banned.append(member)


def _closing(monkeypatch, *, says, tab="ALL CLIENTS", picture="https://drive/p.png",
             name="Jay Rodriguez", called="jay-rodriguez", member=True):
    """One `@RYTE clearout <name>`, with the board, Drive and buttons stubbed."""
    import asyncio
    from types import SimpleNamespace

    from wilbyte.bot import client as bot_client

    Pressed.says = list(says)
    buttons = []

    class Watched(Pressed):
        def __init__(self, **kw):
            super().__init__(**kw)
            buttons.append(self)

    channel = Channel(called)
    guild = Guild(channel, Member(name) if member else None)

    monkeypatch.setattr(bot_client.views, "ConfirmView", Watched)
    monkeypatch.setattr(
        bot_client.jobs, "sheet_for_agent", lambda config, who: ("https://sheet", [])
    )
    monkeypatch.setattr(
        bot_client.jobs, "collect_client",
        lambda config, row: (tab, [] if tab else ["the sheet refused that"]),
    )
    monkeypatch.setattr(
        bot_client.jobs, "keep_the_picture",
        lambda config, page, called: (
            picture, [] if picture else ["Chromium isn't installed"]
        ),
    )

    heard = SimpleNamespace(requester_id=1, messages=[])

    async def send(content=None, *, embed=None, file=None, view=None):
        heard.messages.append(content or "")

    heard.send = send
    bot = SimpleNamespace(get_guild=lambda where: guild)
    config = SimpleNamespace(
        secrets=SimpleNamespace(discord_clients_guild_id="3"),
        discord=SimpleNamespace(approval_timeout_seconds=1),
        schedule=SimpleNamespace(timezone="America/Chicago"),
    )

    asyncio.run(bot_client._clear_out(bot, heard, config, name))
    return guild, channel, heard.messages, buttons


def test_the_first_button_is_only_ever_offered_before_anything_happens(monkeypatch):
    """Shown the plan, RYTE has done nothing yet - not even kept the sheet."""
    guild, channel, said, _ = _closing(monkeypatch, says=[False])

    assert guild.banned == []
    assert channel.deleted is False
    assert "Nothing is deleted by this" in said[0]


def test_saying_no_to_the_second_bans_nobody_and_deletes_nothing(monkeypatch):
    """The sheet and the picture are kept. The channel and the person stay."""
    guild, channel, said, _ = _closing(monkeypatch, says=[True, False])

    assert guild.banned == []
    assert channel.deleted is False
    assert "Left alone" in said[-1]


def test_a_button_nobody_pressed_reads_as_a_no():
    """A view that timed out is a view nobody saw, and must not count as yes.

    The real one rather than the stub, because this is the only thing standing
    between walking away from the message and coming back to a deleted channel.
    """
    import asyncio

    from wilbyte.bot import views

    view = views.ConfirmView(
        requester_id=1, timeout=1, label="Ban and delete #x", emoji="⛔", danger=True
    )

    assert view.confirmed is False, "unpressed is a yes before it even times out"
    asyncio.run(view.on_timeout())
    assert view.confirmed is False
    assert view.answered is False, "a timeout is not somebody answering"


def test_the_second_button_says_who_and_what_before_it_is_pressed(monkeypatch):
    _, _, said, buttons = _closing(monkeypatch, says=[True, True])

    asked = said[-2]
    assert "**This cannot be undone.**" in asked
    assert "Ban **Jay Rodriguez**" in asked
    assert "Delete **#jay-rodriguez**" in asked
    assert buttons[-1].label == "Ban and delete #jay-rodriguez"


def test_the_irreversible_button_is_red_and_the_safe_one_is_not(monkeypatch):
    """It should not look like the button that moves a blog post."""
    _, _, _, buttons = _closing(monkeypatch, says=[True, True])

    assert buttons[0].danger is False
    assert buttons[-1].danger is True


def test_a_sheet_that_did_not_save_never_offers_the_delete(monkeypatch):
    """The channel is the only copy of what the keeping failed to keep."""
    guild, channel, said, buttons = _closing(monkeypatch, says=[True, True], tab="")

    assert len(buttons) == 1, "the second button was offered anyway"
    assert guild.banned == []
    assert channel.deleted is False
    assert "**Nothing deleted.**" in said[-1]


def test_a_picture_that_did_not_upload_never_offers_the_delete(monkeypatch):
    guild, channel, said, buttons = _closing(monkeypatch, says=[True, True], picture="")

    assert len(buttons) == 1
    assert guild.banned == []
    assert channel.deleted is False
    assert "**Nothing deleted.**" in said[-1]


def test_both_presses_ban_them_and_delete_the_channel(monkeypatch):
    """And the one path that does go through, so the asking isn't just a wall."""
    guild, channel, said, _ = _closing(monkeypatch, says=[True, True])

    assert [str(one) for one in guild.banned] == ["Jay Rodriguez"]
    assert channel.deleted is True
    assert "Deleted **#jay-rodriguez**" in said[-1]


def test_nobody_left_to_ban_still_asks_before_the_channel_goes(monkeypatch):
    guild, channel, said, buttons = _closing(monkeypatch, says=[True, False], member=False)

    assert "Nobody to ban" in said[-2]
    assert channel.deleted is False
