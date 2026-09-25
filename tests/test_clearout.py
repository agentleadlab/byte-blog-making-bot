"""Closing an agent down: their sheet kept, their conversation kept, then gone."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

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
    kwargs.setdefault("guild_id", "g9")
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


# ------------------------------- the tab that says RYTE did it


def test_the_row_is_laid_out_against_the_tabs_own_headings():
    """The chargeback tracker taught this the expensive way, writing a name
    under "Closer"."""
    row = clearout.row_for(
        _plan(sheet="https://sheet"), when=datetime(2026, 9, 15),
        headings=["Date Cleared", "Client", "Discord Channel", "Sheet Link", "Notes"],
    )

    assert row == [
        "2026-09-15", "Jay Rodriguez", "jay-rodriguez", "https://sheet", "",
    ]


@pytest.mark.parametrize("headings", [[], ["", "", ""], ["a", "b", "c", "d"]])
def test_headings_nobody_recognises_fall_back_to_the_order_it_always_used(headings):
    """A tab with nothing at the top of it is still a tab somebody reads, and
    refusing to write is worse than four cells in the obvious order."""
    row = clearout.row_for(
        _plan(sheet="https://sheet"), when=datetime(2026, 9, 15), headings=headings,
    )

    assert row == ["Jay Rodriguez", "https://sheet", "jay-rodriguez", "2026-09-15"]


def test_the_tab_is_the_one_named_for_ryte():
    """"that way we know if hes the one deleting stuff" - a row in it is a row
    RYTE put there, and a row anywhere else is somebody's own."""
    assert clearout.COLLECTION_TAB == "Ryte Collection"


# --------------------------------------- what comes back to be screenshotted


def test_the_conversation_comes_back_as_messages_to_screenshot():
    """RYTE photographed this into Drive for a while. Screenshotting it is
    somebody's own job now, and this is what they screenshot."""
    pages = clearout.to_screenshot(_plan(), [
        clearout.Said(who="Jay Rodriguez", when="Sep 3", text="got the leads thanks"),
        clearout.Said(who="Therese", when="Sep 3", text="great", attachments=2),
    ])
    whole = "\n".join(pages)

    assert "got the leads thanks" in whole
    assert "2 attachments" in whole
    assert "#jay-rodriguez" in whole
    assert "Screenshot what you want" in whole


def test_each_message_is_its_own_quote():
    """Discord runs consecutive quoted lines into one block, and the whole
    conversation then reads as having been said by whoever is at the top."""
    whole = "\n".join(clearout.to_screenshot(_plan(), [
        clearout.Said(who="Jay Rodriguez", when="Sep 3", text="first"),
        clearout.Said(who="Therese", when="Sep 3", text="second"),
    ]))

    assert "\n\n> **Therese**" in whole


def test_a_message_from_another_channel_says_where():
    whole = "\n".join(clearout.to_screenshot(_plan(), [
        clearout.Said(who="Artur | NOVA |", when="May 8",
                      text="$1548 ethos aged 6/7", where="ring-da-bell"),
        clearout.Said(who="Jay Rodriguez", when="Sep 3", text="thanks",
                      where="jay-rodriguez"),
    ]))

    assert "#ring-da-bell" in whole
    assert whole.count("#jay-rodriguez") == 1, "their own channel, once, at the top"


def test_a_channel_of_nothing_but_the_lead_feed_says_so():
    """Not an empty message with a button under it: there is genuinely
    nothing to post, and that is worth being told - with the channel to go
    and check in, because RYTE finding none is not the same as there being
    none."""
    pages = clearout.to_screenshot(_plan(), [])

    assert len(pages) == 1
    assert "nothing to post" in pages[0]
    assert "Open it and look before deleting it" in pages[0]
    assert "discord.com/channels/g9/c1" in pages[0], "no way to go and look"
    assert str(clearout.LOOK_BACK) in pages[0]
    assert str(clearout.FIRST_OF_IT) in pages[0]


def test_a_long_conversation_is_paged_rather_than_cut_off():
    pages = clearout.to_screenshot(_plan(), [
        clearout.Said(who="Jay Rodriguez", when="Sep 3", text="x" * 300)
        for _ in range(20)
    ])

    assert len(pages) > 1
    assert all(len(one) <= 2000 for one in pages)


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

    assert "none found" in said
    assert "not in the channel" in said, "where else it looked"


def test_a_sheet_found_in_the_channel_says_so():
    """It means the link is about to be deleted with the channel, and the row
    in ALL CLIENTS is the only place it will live afterwards."""
    said = clearout.describe(_plan(sheet="https://docs.google.com/spreadsheets/d/abc",
                                   from_channel=True))

    assert "spreadsheets/d/abc" in said
    assert "out of the channel" in said


def test_a_sheet_off_a_card_does_not_say_that():
    said = clearout.describe(_plan(sheet="https://docs.google.com/spreadsheets/d/abc"))

    assert "out of the channel" not in said


# ------------------------------- the link the channel is carrying


LEAD_POST = """--New VET Lead--

Name: Henri Harper
Age: 70
State: IL

Check it here:
https://docs.google.com/spreadsheets/d/1pX9NheBB7CQdoPNpOrjJjBI8saBil11or8JB9OdnXcQ/edit?usp=sharing"""


def test_the_sheet_is_found_in_the_channel_when_no_card_has_it():
    """Artur Rushiti has no New Agent card anywhere on the board, so the
    clear-out said "none found on their card" and offered the buttons anyway -
    while every lead in his channel ends "Check it here:" and the link."""
    found = clearout.sheet_in([
        clearout.Said(who="Artur_Rushiti BOT", when="May 24", text=LEAD_POST),
    ])

    assert found == (
        "https://docs.google.com/spreadsheets/d/"
        "1pX9NheBB7CQdoPNpOrjJjBI8saBil11or8JB9OdnXcQ/edit?usp=sharing"
    )


def test_the_newest_sheet_in_the_channel_wins():
    """A client set up twice has two, and the one that matters is the round
    they were on when they stopped."""
    found = clearout.sheet_in([
        clearout.Said(who="bot", when="Jan", text="old https://docs.google.com/spreadsheets/d/aaaaaaaaaaaaaaaaaaaaaa/edit"),
        clearout.Said(who="bot", when="May", text="new https://docs.google.com/spreadsheets/d/bbbbbbbbbbbbbbbbbbbbbb/edit"),
    ])

    assert "bbbbbbbbbbbbbbbbbbbbbb" in found


@pytest.mark.parametrize(
    "said", ["", "nothing here", "https://trello.com/c/abc", "a doc: https://docs.google.com/document/d/abcdefghijklmnopqrstuv/edit"],
)
def test_something_that_is_not_a_sheet_is_not_read_as_one(said):
    assert clearout.sheet_in([clearout.Said(who="x", when="", text=said)]) == ""


def test_no_messages_at_all_is_no_sheet():
    assert clearout.sheet_in([]) == ""


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

    #: Whether anybody pressed anything. `None` in `says` is nobody: the
    #: view timed out, which is not the same answer as "leave it".
    def __init__(self, **kw):
        told = type(self).says
        heard = told.pop(0) if told else False
        self.confirmed = bool(heard)
        self.answered = heard is not None
        self.stopped = heard == "stop"
        if self.stopped:
            self.confirmed = False
        self.label = kw.get("label", "")
        self.danger = kw.get("danger", False)
        self.stoppable = kw.get("stoppable", False)

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
             name="Jay Rodriguez", called="jay-rodriguez", member=True,
             on_card="https://sheet", in_channel="", rows=None, unread=False):
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
        bot_client.jobs, "sheet_for_agent",
        lambda config, who: (on_card, [] if on_card else ["No New Agent card"]),
    )

    async def said_in_there(_channel):
        if unread:
            return [], ["Couldn't read that channel's history: 403 Forbidden"]
        if not in_channel:
            return [], []
        return [clearout.Said(who="bot", when="May 24", text=in_channel)], []

    monkeypatch.setattr(bot_client, "_last_said", said_in_there)

    def collecting(config, plan, *, when):
        if rows is not None:
            rows.append(clearout.row_for(plan, when=when))
        return tab, [] if tab else ["the sheet refused that"]

    monkeypatch.setattr(bot_client.jobs, "collect_client", collecting)
    if not picture:
        async def refuse(content=None, **kw):
            raise RuntimeError("Discord said no")

        heard_send = refuse
    else:
        heard_send = None

    heard = SimpleNamespace(requester_id=1, messages=[])

    async def send(content=None, *, embed=None, file=None, view=None):
        if heard_send is not None and str(content or "").startswith("🧹 **#"):
            await heard_send(content)
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


def test_the_second_button_says_what_before_it_is_pressed(monkeypatch):
    _guild, _channel, said, _buttons = _closing(monkeypatch, says=[True, False])
    red = next(one for one in said if "cannot be undone" in one)

    assert "Delete **#jay-rodriguez**" in red


def test_nobody_is_banned_by_a_clear_out(monkeypatch):
    """"WERE ONLY BANNING PEOPLE IF THEY DISPUTED" - an agent whose channel
    has gone quiet has simply stopped buying, and banning them from the server
    for it is a different thing entirely."""
    guild, channel, said, _buttons = _closing(monkeypatch, says=[True, True])
    whole = "\n".join(said)

    assert guild.banned == [], "it banned somebody for going quiet"
    assert channel.deleted is True
    assert "Ban" not in whole.replace("Nobody is banned", "")


def test_both_presses_delete_the_channel(monkeypatch):
    guild, channel, said, _buttons = _closing(monkeypatch, says=[True, True])

    assert channel.deleted is True
    assert guild.banned == []
    assert "🗑 Deleted **#jay-rodriguez**" in "\n".join(said)


def test_somebody_who_already_left_changes_nothing(monkeypatch):
    """Nothing here was ever about the member, now that nobody is banned."""
    guild, channel, said, _buttons = _closing(monkeypatch, says=[True, True], member=False)

    assert channel.deleted is True
    assert guild.banned == []


def test_a_sheet_that_did_not_save_never_offers_the_delete(monkeypatch):
    """The channel is the only copy of what the keeping failed to keep."""
    guild, channel, said, buttons = _closing(monkeypatch, says=[True, True], tab="")

    assert len(buttons) == 1, "the second button was offered anyway"
    assert guild.banned == []
    assert channel.deleted is False
    assert "**Nothing deleted.**" in said[-1]


def test_a_conversation_that_would_not_post_never_offers_the_delete(monkeypatch):
    guild, channel, said, buttons = _closing(monkeypatch, says=[True, True], picture="")

    assert len(buttons) == 1
    assert guild.banned == []
    assert channel.deleted is False
    assert "**Nothing deleted.**" in said[-1]


def test_both_presses_delete_the_channel_and_nothing_else(monkeypatch):
    """And the one path that does go through, so the asking isn't just a wall."""
    guild, channel, said, _ = _closing(monkeypatch, says=[True, True])

    assert guild.banned == []
    assert channel.deleted is True
    assert "Deleted **#jay-rodriguez**" in said[-1]


def test_saying_no_still_leaves_the_channel_there(monkeypatch):
    guild, channel, said, buttons = _closing(monkeypatch, says=[True, False], member=False)

    assert channel.deleted is False
    assert guild.banned == []


# -------------------------------------------- which channels have gone quiet

from datetime import timedelta  # noqa: E402

NOW = datetime(2026, 9, 15)


def quiet_channel(name, *, days=None, category="", used=True):
    return clearout.Channel(
        channel_id=name, name=name, category=category,
        last_active=None if days is None else NOW - timedelta(days=days),
        ever_used=used,
    )


@pytest.mark.parametrize(
    "typed, days",
    [
        ("", 60),
        ("quiet", 60),
        ("3 months", 90),
        ("3months", 90),
        ("1 month", 30),
        ("90 days", 90),
        ("2 weeks", 14),
        ("6 mo", 180),
    ],
)
def test_how_long_back_however_it_is_typed(typed, days):
    assert clearout.how_far_back(typed) == days


def test_nothing_typed_gives_the_answer_rather_than_a_question():
    """`@RYTE quiet` on its own is the common case and must just work."""
    assert clearout.how_far_back("") == clearout.QUIET_MONTHS * clearout.DAYS_A_MONTH


def test_a_busy_channel_is_not_on_the_list():
    quiet, _, _ = clearout.quiet_ones(
        [quiet_channel("busy", days=3)], since=NOW - timedelta(days=60)
    )

    assert quiet == []


def test_the_quietest_comes_first():
    found, _, _ = clearout.quiet_ones(
        [quiet_channel("recent", days=70), quiet_channel("ancient", days=400),
         quiet_channel("middle", days=120)],
        since=NOW - timedelta(days=60),
    )

    assert [one.name for one in found] == ["ancient", "middle", "recent"]


def test_the_servers_own_channels_are_never_on_the_list():
    """However long #general has been quiet, it is not an agent's."""
    quiet, ours, _ = clearout.quiet_ones(
        [quiet_channel("general", days=400), quiet_channel("admin-team", days=400),
         quiet_channel("jay-rodriguez", days=400)],
        since=NOW - timedelta(days=60),
    )

    assert [one.name for one in quiet] == ["jay-rodriguez"]
    assert {one.name for one in ours} == {"general", "admin-team"}


def test_a_channel_nobody_can_date_is_said_out_loud_rather_than_dropped():
    """A silent skip is the bug. An undatable channel is still a channel."""
    quiet, _, unknown = clearout.quiet_ones(
        [quiet_channel("mystery", days=None)], since=NOW - timedelta(days=60)
    )

    assert quiet == []
    assert [one.name for one in unknown] == ["mystery"]
    said = clearout.describe_quiet(
        quiet, [], unknown, since=NOW - timedelta(days=60), now=NOW
    )
    assert "Couldn't tell when 1 was last used" in said[-1]
    assert "#mystery" in said[-1]


def test_a_channel_nobody_ever_used_is_listed_and_does_not_claim_otherwise():
    quiet, _, _ = clearout.quiet_ones(
        [quiet_channel("empty", days=200, used=False)], since=NOW - timedelta(days=60)
    )

    assert [one.name for one in quiet] == ["empty"]
    assert clearout.how_long(quiet[0], now=NOW) == "nothing ever said, made 6 months ago"


def test_the_list_says_it_deletes_nothing():
    """It only ever reports, and it should be impossible to read otherwise."""
    quiet, _, _ = clearout.quiet_ones(
        [quiet_channel("jay-rodriguez", days=100)], since=NOW - timedelta(days=60)
    )
    said = clearout.describe_quiet(
        quiet, [], [], since=NOW - timedelta(days=60), now=NOW
    )

    assert "Nothing here is deleted" in said[-1]
    assert "**#jay-rodriguez** — 3 months" in said[0]


def test_an_empty_list_reads_as_an_answer_not_a_failure():
    said = clearout.describe_quiet([], [], [], since=NOW - timedelta(days=60), now=NOW)

    assert "every channel has been used" in said[0]


def _long_list(many=120):
    # Zero-padded so no name is a prefix of another: "#agent-002" would
    # otherwise be found inside "#agent-0020".
    channels = [quiet_channel(f"agent-{i:03d}", days=100 + i) for i in range(many)]
    quiet, _, _ = clearout.quiet_ones(channels, since=NOW - timedelta(days=60))
    return channels, clearout.describe_quiet(
        quiet, [], [], since=NOW - timedelta(days=60), now=NOW
    )


def test_the_list_offers_as_many_as_can_be_picked_and_counts_the_rest():
    """"then do 25 each quiet? then i run again". Naming a channel the list
    cannot offer is naming one somebody has to type out after all, so the cap
    is the dropdown's own limit rather than the message's."""
    channels, said = _long_list()
    whole = "\n".join(said)

    listed = [one for one in channels if f"#{one.name}" in whole]
    assert len(listed) == clearout.PICKABLE
    assert all(len(one) <= 2000 for one in said)


def test_the_ones_it_did_not_list_are_counted_not_dropped():
    """A list quietly cut short reads as if that was all of them."""
    _channels, said = _long_list()
    whole = "\n".join(said)

    assert f"and {120 - clearout.PICKABLE} more" in whole
    assert "run `@RYTE quiet` again" in whole


def test_a_short_list_says_nothing_about_more():
    channels = [quiet_channel(f"agent-{i}", days=100) for i in range(4)]
    quiet, _, _ = clearout.quiet_ones(channels, since=NOW - timedelta(days=60))
    whole = "\n".join(clearout.describe_quiet(
        quiet, [], [], since=NOW - timedelta(days=60), now=NOW
    ))

    assert "more" not in whole.split("Nothing here is deleted")[0]


def test_what_is_offered_is_exactly_what_was_listed():
    """The dropdown and the lines above it have to be the same channels, or
    the list names one thing and offers another."""
    channels, said = _long_list()
    quiet, _, _ = clearout.quiet_ones(channels, since=NOW - timedelta(days=60))
    whole = "\n".join(said)

    offered = clearout.pick_from(quiet, now=NOW)

    assert len(offered) == clearout.PICKABLE
    for name, note in offered:
        assert f"#{name}" in whole
        assert note, name


def test_quiet_is_a_word_ryte_knows():
    from wilbyte.bot import mentions

    for typed in ("quiet", "inactive 3 months", "unused"):
        assert mentions.parse(f"<@1> {typed}").action == "quiet"


def test_the_command_lists_the_clients_server_and_touches_nothing(monkeypatch):
    """End to end, with Discord stubbed: it reports and it stops there."""
    import asyncio
    from types import SimpleNamespace

    import discord

    from wilbyte.bot import client as bot_client

    def snowflake(when):
        """A Discord id with this time inside it, the way Discord makes them."""
        return (int(when.timestamp() * 1000) - 1420070400000) << 22

    class Text:
        def __init__(self, name, *, last=None, category=""):
            self.id, self.name = abs(hash(name)) % 10**6, name
            self.category = SimpleNamespace(name=category) if category else None
            self.last_message_id = snowflake(last) if last else None
            self.created_at = datetime(2026, 1, 1, tzinfo=discord.utils.utcnow().tzinfo)

    old = datetime(2026, 5, 1, tzinfo=discord.utils.utcnow().tzinfo)
    new = datetime(2026, 9, 14, tzinfo=discord.utils.utcnow().tzinfo)
    guild = SimpleNamespace(
        name="Agent Lead Lab Clients",
        text_channels=[
            Text("jay-rodriguez", last=old, category="ONGOING CLIENTS"),
            Text("connor-knudsen", last=new),
            Text("general", last=old),
            Text("never-used"),
        ],
    )

    said = []

    async def send(content=None, **kw):
        said.append(content or "")

    asked = []
    bot = SimpleNamespace(get_guild=lambda where: (asked.append(where), guild)[1])
    config = SimpleNamespace(
        secrets=SimpleNamespace(discord_clients_guild_id="1291897127882195056"),
        schedule=SimpleNamespace(timezone="America/Chicago"),
        discord=SimpleNamespace(approval_timeout_seconds=1),
    )

    # Nobody picks anything. The list on its own still has to do nothing.
    offered = []

    class Nobody:
        chosen = None
        run = False

        def __init__(self, choices, **kw):
            offered.extend(choices)

        async def wait(self):
            return None

    monkeypatch.setattr(bot_client.views, "ChannelPicker", Nobody)

    cleared = []

    async def never(*args, **kwargs):
        cleared.append(args)

    monkeypatch.setattr(bot_client, "_clear_out", never)

    asyncio.run(bot_client._quiet_channels(
        bot, SimpleNamespace(send=send, requester_id=1), config, "",
    ))

    whole = "\n".join(said)
    assert asked == [1291897127882195056], "it looked at some other server"
    assert "#jay-rodriguez" in whole
    assert "#never-used" in whole, "an empty channel is exactly what this is for"
    assert "#connor-knudsen" not in whole, "a channel used yesterday is not quiet"
    assert "Left out 1 of the server's own" in whole
    assert "Nothing here is deleted" in whole
    assert cleared == [], "the list itself cleared somebody out"
    # Quietest first, the same order the lines above it are in.
    assert [name for name, _note in offered] == ["never-used", "jay-rodriguez"]
    assert "general" not in [name for name, _note in offered]
    assert all(note for _name, note in offered), "no sense of how long"


def test_the_command_will_not_list_a_server_that_is_not_the_clients_one(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from wilbyte.bot import client as bot_client

    said = []

    async def send(content=None, **kw):
        said.append(content or "")

    asyncio.run(bot_client._quiet_channels(
        SimpleNamespace(get_guild=lambda where: None),
        SimpleNamespace(send=send, requester_id=1),
        SimpleNamespace(
            secrets=SimpleNamespace(discord_clients_guild_id=""),
            schedule=SimpleNamespace(timezone="America/Chicago"),
        ),
        "",
    ))

    assert "DISCORD_CLIENTS_GUILD_ID" in said[0]


def test_picking_one_starts_that_one_clear_out(monkeypatch):
    """"then i run again that way we have button". Picking is the same path
    as typing the name, so the irreversible half is the one that has already
    been watched rather than a second copy of it."""
    import asyncio
    from types import SimpleNamespace

    import discord

    from wilbyte.bot import client as bot_client

    def snowflake(when):
        return (int(when.timestamp() * 1000) - 1420070400000) << 22

    old = datetime(2026, 5, 1, tzinfo=discord.utils.utcnow().tzinfo)
    guild = SimpleNamespace(
        name="Clients",
        text_channels=[SimpleNamespace(
            id=1, name="jay-rodriguez", category=None,
            last_message_id=snowflake(old), created_at=old,
        )],
    )

    class Picked:
        chosen = "jay-rodriguez"
        run = False

        def __init__(self, choices, **kw):
            pass

        async def wait(self):
            return None

    monkeypatch.setattr(bot_client.views, "ChannelPicker", Picked)

    cleared = []

    async def clearing(bot, responder, config, name):
        cleared.append(name)

    monkeypatch.setattr(bot_client, "_clear_out", clearing)

    async def send(content=None, **kw):
        return None

    config = SimpleNamespace(
        secrets=SimpleNamespace(discord_clients_guild_id="7"),
        schedule=SimpleNamespace(timezone="America/Chicago"),
        discord=SimpleNamespace(approval_timeout_seconds=1),
    )
    asyncio.run(bot_client._quiet_channels(
        SimpleNamespace(get_guild=lambda where: guild),
        SimpleNamespace(send=send, requester_id=1), config, "",
    ))

    assert cleared == ["jay-rodriguez"], "one channel, and only the one picked"


def test_the_picker_is_only_for_whoever_asked():
    """A list of things to delete is not a thing for anybody passing to press."""
    import inspect

    from wilbyte.bot import views

    source = inspect.getsource(views.ChannelPicker)

    assert "interaction_check" in source
    assert "requester_id" in source


def test_the_picker_holds_no_more_than_discord_allows():
    from wilbyte.bot import views

    picker = views.ChannelPicker(
        [(f"agent-{i:03d}", "3 months") for i in range(60)],
        requester_id=1, timeout=1,
    )

    assert len(picker._select.options) == 25


def test_the_channels_own_link_is_what_gets_kept(monkeypatch):
    """No card on the board, and the link in every lead the channel carries.
    Writing an empty cell into ALL CLIENTS and then deleting the only copy is
    the exact thing this is built to prevent."""
    rows = []
    _guild, _channel, said, _buttons = _closing(
        monkeypatch, says=[True, False],
        on_card="", in_channel=LEAD_POST, rows=rows,
        name="artur_rushiti-vet", called="artur_rushiti-vet",
    )

    assert rows, "nothing was written to ALL CLIENTS"
    assert "1pX9NheBB7CQdoPNpOrjJjBI8saBil11or8JB9OdnXcQ" in rows[0][1]
    assert "out of the channel" in "\n".join(said)


def test_a_card_link_still_wins_over_the_channels(monkeypatch):
    """A link somebody put on the card is the one they chose."""
    rows = []
    _closing(
        monkeypatch, says=[True, False],
        on_card="https://docs.google.com/spreadsheets/d/onthecard", in_channel=LEAD_POST,
        rows=rows,
    )

    assert rows[0][1] == "https://docs.google.com/spreadsheets/d/onthecard"


def test_no_sheet_anywhere_is_said_at_the_button_that_cannot_be_undone(monkeypatch):
    """The row has an empty cell and the channel is about to go. That belongs
    next to the delete, not three messages earlier."""
    _guild, _channel, said, _buttons = _closing(
        monkeypatch, says=[True, False], on_card="", in_channel="",
    )
    red = next(one for one in said if "cannot be undone" in one)

    assert "No sheet link was found" in red
    assert "not in the channel" in red


def test_a_sheet_that_was_found_says_nothing_extra_at_the_button(monkeypatch):
    _guild, _channel, said, _buttons = _closing(monkeypatch, says=[True, False])
    red = next(one for one in said if "cannot be undone" in one)

    assert "No sheet link was found" not in red


# ------------------------- what the lead bots actually post


class Embed:
    def __init__(self, description="", title="", url="", fields=()):
        self.description, self.title, self.url = description, title, url
        self.fields = [
            SimpleNamespace(name=name, value=value) for name, value in fields
        ]
        self.footer = SimpleNamespace(text="")
        self.author = SimpleNamespace(name="")


class Posted:
    def __init__(self, content="", embeds=(), who="Artur_Rushiti BOT", bot=None):
        self.content, self.embeds = content, list(embeds)
        # As Discord marks them: the lead feeds are applications.
        self.author = SimpleNamespace(
            display_name=who,
            bot=who.strip().upper().endswith("BOT") if bot is None else bot,
            id=7,
        )
        self.created_at = datetime(2026, 5, 24, 0, 7)
        self.attachments = []


def test_a_lead_posted_as_an_embed_is_not_a_blank_message():
    """The lead bots put the whole lead in an embed, so `content` is empty.
    Reading only `content` made those channels look like forty blank messages:
    no sheet link to find, and a picture of nothing to keep before deleting
    them."""
    from wilbyte.bot import client as bot_client

    said = bot_client._all_of_it(Posted(embeds=[Embed(
        title="--New VET Lead--",
        description=LEAD_POST,
        url="https://docs.google.com/spreadsheets/d/1pX9NheBB7CQdoPNpOrjJjBI8saBil11or8JB9OdnXcQ/edit",
    )]))

    assert "Henri Harper" in said
    assert "1pX9NheBB7CQdoPNpOrjJjBI8saBil11or8JB9OdnXcQ" in said
    assert clearout.sheet_in([clearout.Said(who="bot", when="", text=said)])


def test_an_embeds_fields_are_read_too():
    from wilbyte.bot import client as bot_client

    said = bot_client._all_of_it(Posted(embeds=[Embed(
        fields=(("Sheet", "https://docs.google.com/spreadsheets/d/aaaaaaaaaaaaaaaaaaaaaa/edit"),),
    )]))

    assert "aaaaaaaaaaaaaaaaaaaaaa" in said


def test_an_ordinary_message_still_reads_as_itself():
    from wilbyte.bot import client as bot_client

    assert bot_client._all_of_it(Posted(content="got the leads thanks")) == (
        "got the leads thanks"
    )


def test_a_channel_that_would_not_open_is_not_an_empty_one(monkeypatch):
    """A channel RYTE is not allowed to read looked exactly like an empty one,
    so the picture kept before a delete was a picture of nothing and was
    reported as kept."""
    guild, channel, said, _buttons = _closing(
        monkeypatch, says=[True, True], unread=True,
    )
    whole = "\n".join(said)

    assert channel.deleted is False, "it deleted a channel it could not read"
    assert guild.banned == []
    assert "Nothing deleted" in whole
    assert "Couldn't read that channel's history" in whole


def test_reading_a_channel_reports_why_it_could_not(monkeypatch):
    """Logged and swallowed, a 403 came back as an empty list and was
    indistinguishable from a channel nobody ever used."""
    import asyncio

    from wilbyte.bot import client as bot_client

    class Refused:
        def history(self, limit=0, oldest_first=False):
            raise PermissionError("403 Forbidden (Missing Access)")

    found, trouble = asyncio.run(bot_client._last_said(Refused()))

    assert found == []
    assert trouble and "403" in trouble[0]
    assert "nothing to keep" in trouble[0]


def test_an_empty_channel_is_not_a_failure():
    """A channel nobody ever used is a real answer, and a clear-out of one is
    allowed to go ahead."""
    import asyncio

    from wilbyte.bot import client as bot_client

    class Quiet:
        async def history(self, limit=0, oldest_first=False):
            return
            yield

    found, trouble = asyncio.run(bot_client._last_said(Quiet()))

    assert found == [] and trouble == []


def test_an_embed_only_channel_reads_through_to_the_end(monkeypatch):
    """The real shape: history of embed posts, read for the sheet."""
    import asyncio

    from wilbyte.bot import client as bot_client

    class Feed:
        async def history(self, limit=0, oldest_first=False):
            if oldest_first:
                return
            yield Posted(embeds=[Embed(description=LEAD_POST)])

    found, trouble = asyncio.run(bot_client._last_said(Feed()))

    assert not trouble
    assert clearout.sheet_in(found).endswith("edit?usp=sharing")


# ------------------------- the ones Discord will not let RYTE open


def test_a_channel_that_cannot_be_opened_is_marked_in_the_list():
    """403 Forbidden (error code: 50001): Missing Access. Discord hands over
    every channel in the server whether or not it can be read, so a channel
    can be listed as quiet and still be one nothing can be kept out of."""
    channels = [
        quiet_channel("artur_rushiti-vet", days=120),
        quiet_channel("jay-rodriguez", days=100),
    ]
    channels[0].readable = False
    quiet, _, _ = clearout.quiet_ones(channels, since=NOW - timedelta(days=60))
    whole = "\n".join(clearout.describe_quiet(
        quiet, [], [], since=NOW - timedelta(days=60), now=NOW
    ))

    assert "#artur_rushiti-vet" in whole
    assert "can't read it" in whole
    assert "1 of these can't be opened" in whole
    assert "Read Message History" in whole, "what to actually change"


def test_one_that_cannot_be_opened_is_not_offered():
    """A clear-out of one stops at the first button with nothing kept, so
    offering it is offering a press that cannot go anywhere."""
    channels = [
        quiet_channel("artur_rushiti-vet", days=120),
        quiet_channel("jay-rodriguez", days=100),
    ]
    channels[0].readable = False
    quiet, _, _ = clearout.quiet_ones(channels, since=NOW - timedelta(days=60))

    assert [name for name, _note in clearout.pick_from(quiet, now=NOW)] == [
        "jay-rodriguez",
    ]


def test_nothing_is_said_about_locks_when_there_are_none():
    channels = [quiet_channel("jay-rodriguez", days=100)]
    quiet, _, _ = clearout.quiet_ones(channels, since=NOW - timedelta(days=60))
    whole = "\n".join(clearout.describe_quiet(
        quiet, [], [], since=NOW - timedelta(days=60), now=NOW
    ))

    assert "can't be opened" not in whole


def test_the_permission_is_read_rather_than_found_out_by_asking():
    """One request per channel and a 403 at the end of most of them is not a
    way to build a list of a hundred and eighty."""
    from types import SimpleNamespace

    from wilbyte.bot import client as bot_client

    def channel(view, history):
        return SimpleNamespace(permissions_for=lambda who: SimpleNamespace(
            view_channel=view, read_message_history=history,
        ))

    guild = SimpleNamespace(me=object())

    assert bot_client._can_read(guild, channel(True, True)) is True
    assert bot_client._can_read(guild, channel(True, False)) is False
    assert bot_client._can_read(guild, channel(False, True)) is False


def test_a_permission_that_cannot_be_read_counts_as_readable():
    """Being told a channel cannot be cleared when it can is worse than
    finding out at the first button, which now says so plainly."""
    from types import SimpleNamespace

    from wilbyte.bot import client as bot_client

    def boom(who):
        raise RuntimeError("discord.py changed shape")

    assert bot_client._can_read(
        SimpleNamespace(me=object()), SimpleNamespace(permissions_for=boom)
    ) is True
    assert bot_client._can_read(SimpleNamespace(me=None), object()) is True


def test_the_small_note_is_on_a_line_of_its_own():
    """"-#" is Discord's small text only at the start of a line. In the
    middle of the sheet line it rendered as the two characters."""
    said = clearout.describe(_plan(
        sheet="https://docs.google.com/spreadsheets/d/abc", from_channel=True,
    ))

    assert not any(
        "-#" in line and not line.startswith("-#") for line in said.splitlines()
    ), said
    assert any(line.startswith("-#") for line in said.splitlines())


def _collecting(monkeypatch, *, titles, gid_tab="Masterlist", headings=None, link="x"):
    """One `collect_client`, with Sheets stubbed. Returns (tab, rows, problems)."""
    from wilbyte import gsheets
    from wilbyte.bot import jobs

    written, styled = [], []

    class Sheet:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def tabs(self, sheet_id):
            return [{"title": one, "sheetId": i} for i, one in enumerate(titles)]

        def tab_named(self, sheet_id, gid):
            return gid_tab

        def rows(self, sheet_id, span):
            return [list(headings)] if headings else []

        def append(self, sheet_id, tab, rows):
            written.append((tab, rows[0]))
            return f"'{tab}'!A2:D2"

        def restyle(self, sheet_id, tab_id, first, last, *, bold, wrap=""):
            styled.append((tab_id, first, last, bold, wrap))

    monkeypatch.setattr(gsheets, "SheetsClient", lambda creds, **kw: Sheet())
    monkeypatch.setattr(gsheets, "credentials", lambda secrets: None)
    monkeypatch.setattr(gsheets, "sheet_id_in", lambda one: "sid" if link else "")
    monkeypatch.setattr(gsheets, "gid_in", lambda one: "1")

    from types import SimpleNamespace as NS

    tab, problems = jobs.collect_client(
        NS(secrets=NS(clients_sheet_link=link)),
        _plan(sheet="https://sheet"), when=datetime(2026, 9, 15),
    )
    return tab, written, problems, styled


def test_it_writes_to_the_ryte_tab_not_whichever_one_the_link_had_open(monkeypatch):
    """The gid in a pasted link is exactly the sort of thing that quietly
    points at the Masterlist instead."""
    tab, written, problems, _styled = _collecting(
        monkeypatch, titles=["Ryte Collection", "Masterlist", "UPRISE"],
    )

    assert not problems, problems
    assert tab == "Ryte Collection"
    assert written[0][0] == "Ryte Collection"


def test_the_tab_is_found_however_it_is_spaced(monkeypatch):
    tab, _written, _problems, _styled = _collecting(
        monkeypatch, titles=["Masterlist", " ryte   collection "],
    )

    assert tab.strip().casefold().startswith("ryte")


def test_without_that_tab_it_falls_back_to_the_link(monkeypatch):
    """A spreadsheet set up before the tab was named should still collect
    rather than refuse."""
    tab, written, problems, _styled = _collecting(
        monkeypatch, titles=["Masterlist", "UPRISE"], gid_tab="Masterlist",
    )

    assert not problems
    assert tab == "Masterlist" and written[0][0] == "Masterlist"


def test_no_tab_at_all_says_which_ones_there_are(monkeypatch):
    tab, written, problems, _styled = _collecting(
        monkeypatch, titles=["Masterlist", "UPRISE"], gid_tab="",
    )

    assert tab == "" and written == []
    assert "Ryte Collection" in problems[0]
    assert "Masterlist" in problems[0] and "UPRISE" in problems[0]


def test_the_row_goes_in_under_the_headings_that_are_there(monkeypatch):
    _tab, written, _problems, _styled = _collecting(
        monkeypatch, titles=["Ryte Collection"],
        headings=["Date Cleared", "Client", "Discord Channel", "Sheet Link"],
    )

    assert written[0][1] == [
        "2026-09-15", "Jay Rodriguez", "jay-rodriguez", "https://sheet",
    ]


# ------------- nothing outside the one client channel, ever

# "MAKE SURE HE DOESNT DELETING ANYTHING ELSE OUTSIDE THE CLIENTS CHANNEL."
# RYTE now reads the shared channels to find what the client said in them, and
# reading a channel must never be a step towards deleting it.


def _theirs(channel_id="11", name="jay-rodriguez"):
    return clearout.Plan(
        name="Jay Rodriguez",
        channel=clearout.Channel(channel_id=channel_id, name=name),
    )


def _real(channel_id=11, name="jay-rodriguez", guild=3):
    return SimpleNamespace(id=channel_id, name=name, guild=SimpleNamespace(id=guild))


def test_the_one_channel_it_was_for_may_be_deleted():
    assert clearout.the_one_to_delete(_theirs(), _real(), guild_id=3) == ""


def test_a_different_channel_may_not_be():
    """Not by name, not by being nearby - by id, and only the one."""
    said = clearout.the_one_to_delete(_theirs(), _real(channel_id=99), guild_id=3)

    assert "not the channel this was for" in said


def test_ring_da_bell_may_never_be_deleted():
    """It is where the client's own words about the leads are, which is what
    RYTE reads it for. Reading it must not make it deletable."""
    said = clearout.the_one_to_delete(
        _theirs(channel_id="11", name="ring-da-bell"),
        _real(name="ring-da-bell"), guild_id=3,
    )

    assert "one of the server's own" in said
    assert clearout.off_limits(channel("ring-da-bell")) is True
    assert clearout.channels_for("ring da bell", [channel("ring-da-bell")]) == []


@pytest.mark.parametrize(
    "called", ["ring-da-bell", "🔔│ring-da-bell", "the-vault", "wins", "general-chat"],
)
def test_the_shared_channels_are_nobodys_to_clear(called):
    assert clearout.off_limits(channel(called)) is True


def test_a_channel_in_another_server_may_not_be():
    said = clearout.the_one_to_delete(_theirs(), _real(guild=999), guild_id=3)

    assert "in another server" in said


def test_a_channel_that_went_away_is_not_deleted():
    assert "not there any more" in clearout.the_one_to_delete(
        _theirs(), None, guild_id=3
    )


def test_a_plan_with_no_channel_deletes_nothing():
    assert "no channel on the plan" in clearout.the_one_to_delete(
        clearout.Plan(name="Jay Rodriguez"), _real(), guild_id=3
    )


def test_the_delete_is_refused_out_loud_rather_than_skipped(monkeypatch):
    """A silent skip on the irreversible step is the one nobody notices."""
    import asyncio
    from types import SimpleNamespace as NS

    from wilbyte.bot import client as bot_client

    Pressed.says = [True, True]
    channel = Channel("ring-da-bell")
    guild = Guild(channel, Member("Jay Rodriguez"))

    monkeypatch.setattr(bot_client.views, "ConfirmView", Pressed)
    monkeypatch.setattr(
        bot_client.jobs, "sheet_for_agent", lambda config, who: ("https://s", [])
    )
    monkeypatch.setattr(
        bot_client.jobs, "collect_client",
        lambda config, plan, *, when: ("ALL CLIENTS", []),
    )

    async def nothing(_channel):
        return [], []

    monkeypatch.setattr(bot_client, "_last_said", nothing)

    async def none_elsewhere(*a, **kw):
        return [], []

    monkeypatch.setattr(bot_client, "_also_said", none_elsewhere)

    said = []

    async def send(content=None, **kw):
        said.append(content or "")

    asyncio.run(bot_client._clear_out(
        NS(get_guild=lambda where: guild), NS(send=send, requester_id=1),
        NS(secrets=NS(discord_clients_guild_id="3"),
           discord=NS(approval_timeout_seconds=1),
           schedule=NS(timezone="America/Chicago")),
        "ring da bell",
    ))

    assert channel.deleted is False
    whole = "\n".join(said)
    assert "looks like" in whole or "Didn't delete" in whole, whole


def test_a_channel_that_changed_under_it_is_not_deleted(monkeypatch):
    """The look-up and the press are minutes apart, and a channel can be
    renamed or replaced in between. The check at the press is what makes that
    safe rather than a race."""
    import asyncio
    from types import SimpleNamespace as NS

    from wilbyte.bot import client as bot_client

    Pressed.says = [True, True]
    theirs = Channel("jay-rodriguez")
    guild = Guild(theirs, Member("Jay Rodriguez"))

    # By the time the red button is pressed, that id is a different channel.
    somebody_elses = Channel("general-chat")
    somebody_elses.id = 404
    guild.get_channel = lambda channel_id: somebody_elses

    monkeypatch.setattr(bot_client.views, "ConfirmView", Pressed)
    monkeypatch.setattr(
        bot_client.jobs, "sheet_for_agent", lambda config, who: ("https://s", [])
    )
    monkeypatch.setattr(
        bot_client.jobs, "collect_client",
        lambda config, plan, *, when: ("ALL CLIENTS", []),
    )

    async def nothing(_channel):
        return [], []

    async def none_elsewhere(*a, **kw):
        return [], []

    monkeypatch.setattr(bot_client, "_last_said", nothing)
    monkeypatch.setattr(bot_client, "_also_said", none_elsewhere)

    said = []

    async def send(content=None, **kw):
        said.append(content or "")

    asyncio.run(bot_client._clear_out(
        NS(get_guild=lambda where: guild), NS(send=send, requester_id=1),
        NS(secrets=NS(discord_clients_guild_id="3"),
           discord=NS(approval_timeout_seconds=1),
           schedule=NS(timezone="America/Chicago")),
        "Jay Rodriguez",
    ))

    assert somebody_elses.deleted is False, "it deleted a channel it was not for"
    assert theirs.deleted is False
    assert "Didn't delete anything" in "\n".join(said)


# ------------------------------- what goes in the picture


def _bot_said(text, at=None):
    return clearout.Said(who="Artur_Rushiti BOT", when="May 24", text=text,
                         by_bot=True, at=at, where="artur_rushiti-vet")


def _person_said(text, at=None, where="artur_rushiti-vet"):
    return clearout.Said(who="artur.rushiti", when="May 25", text=text,
                         at=at, where=where)


def test_the_bot_feed_is_not_the_conversation():
    """"only the human messages, not the bot feed". A picture of the last
    forty messages in a lead channel was forty lead records - the goods, not
    the conversation, and none of it in their words."""
    kept = clearout.for_the_picture([
        _bot_said("--New VET Lead--\nName: Henri Harper"),
        _person_said("got them, thanks"),
        _bot_said("--New VET Lead--\nName: Dolores Ramirez"),
    ])

    assert [one.text for one in kept] == ["got them, thanks"]


def test_what_they_said_elsewhere_goes_in_too():
    """A sale posted in ring-da-bell is the client saying the leads worked."""
    kept = clearout.for_the_picture(
        [_person_said("got them, thanks", at=datetime(2026, 5, 25))],
        [_person_said("$1548 ethos aged 6/7", at=datetime(2026, 5, 8),
                      where="ring-da-bell")],
    )

    assert [one.text for one in kept] == ["$1548 ethos aged 6/7", "got them, thanks"]


def test_the_picture_is_still_capped():
    kept = clearout.for_the_picture([
        _person_said(f"message {i}", at=datetime(2026, 5, 1, 0, i))
        for i in range(60)
    ])

    assert len(kept) == clearout.KEEP_MESSAGES
    assert kept[-1].text == "message 59", "it kept the oldest instead of the newest"


def test_a_message_said_elsewhere_is_labelled_with_where():
    plan = _plan(channel=channel("artur_rushiti-vet"))
    whole = "\n".join(clearout.to_screenshot(plan, [
        _person_said("$1548 ethos aged 6/7", where="ring-da-bell"),
        _person_said("got them, thanks"),
    ]))

    assert "· #ring-da-bell" in whole
    assert whole.count("· #") == 1, "their own channel is not labelled on each line"


def test_the_messages_are_posted_before_the_delete_is_offered(monkeypatch):
    """"just forward them to me, then ill screenshot then you collect sheet
    and delete" - the screenshotting happens between the two buttons, so the
    conversation has to be on screen before the red one appears."""
    _guild, _channel, said, _buttons = _closing(monkeypatch, says=[True, False])

    posted = next(i for i, one in enumerate(said) if str(one).startswith("🧹 **#"))
    red = next(i for i, one in enumerate(said) if "cannot be undone" in str(one))

    assert posted < red, "the delete was offered before the messages were up"


def test_it_reads_past_the_lead_feed_to_find_the_conversation(monkeypatch):
    """"HE HAS CONVO". Forty was how many messages were wanted, and in a
    channel carrying a lead feed all forty are the bot - so it came back as
    "nothing anybody said" while the conversation sat just above the window.
    What is wanted is forty of what people said."""
    import asyncio

    from wilbyte.bot import client as bot_client

    asked = {}

    class Feed:
        async def history(self, limit=0, oldest_first=False):
            asked.setdefault("limits", []).append((limit, oldest_first))
            if oldest_first:
                # The start of the channel: the welcome, buried since.
                yield Posted(content="hey Artur, welcome aboard",
                             who="Therese", bot=False)
                return
            yield Posted(content="", embeds=[Embed(description=LEAD_POST)])
            for _ in range(60):
                yield Posted(content="--New VET Lead--", embeds=[])
            yield Posted(content="got them, thanks", who="artur.rushiti")

    found, trouble = asyncio.run(bot_client._last_said(Feed()))

    assert not trouble
    assert (clearout.LOOK_BACK, False) in asked["limits"]
    assert (clearout.FIRST_OF_IT, True) in asked["limits"], "it never read the start"
    kept = clearout.for_the_picture(found)
    assert "got them, thanks" in [one.text for one in kept]
    assert "hey Artur, welcome aboard" in [one.text for one in kept]


def test_a_channel_with_genuinely_nothing_says_how_far_it_looked():
    """"nothing anybody said" is a strong claim, and one somebody should be
    able to check."""
    pages = clearout.to_screenshot(_plan(), [])

    assert str(clearout.LOOK_BACK) in pages[0]


def test_the_written_row_is_tidied_up_after_it_lands(monkeypatch):
    """"cant this look good any more?" A row appended under a heading row
    arrives wearing the heading's clothes - bold and centred - and the sheet
    link wraps to six lines, which takes the whole row with it."""
    _tab, _written, problems, styled = _collecting(
        monkeypatch, titles=["Ryte Collection"],
    )

    assert not problems
    assert styled, "the row was left in the heading's clothes"
    _tab_id, first, last, bold, wrap = styled[0]
    assert (first, last) == (2, 2), "it restyled a row it did not write"
    assert bold is False
    assert wrap == "CLIP"


def test_the_shared_channels_it_could_not_open_are_named(monkeypatch, tmp_path):
    """Skipping a channel it is not allowed to open, and saying nothing, is
    how "nothing anybody said" gets reported about somebody who has been
    ringing the bell all year."""
    import asyncio
    from types import SimpleNamespace as NS

    from wilbyte import bell
    from wilbyte.bot import client as bot_client

    monkeypatch.setattr(bell, "BELL_PATH", tmp_path / "bell.json")

    shut = NS(id=1, name="ring-da-bell", guild=NS(id=3))

    class Chat:
        async def history(self, **how):
            return
            yield

    monkeypatch.setattr(bot_client, "_can_read", lambda guild, ch: ch is not shut)
    guild = NS(id=3, me=object(), get_channel=lambda cid: {1: shut}.get(cid, Chat()))

    found, notes = asyncio.run(bot_client._also_said(
        guild, NS(id=7),
        [clearout.Channel(channel_id="1", name="ring-da-bell"),
         clearout.Channel(channel_id="2", name="general-chat")],
    ))

    assert found == []
    assert "#ring-da-bell" in "\n".join(notes)
    assert "Can't open" in "\n".join(notes)


def test_what_was_rung_in_the_bell_is_remembered_and_looked_up(monkeypatch, tmp_path):
    """A thousand messages of ring-da-bell is thirteen days, and the client
    stopped buying in May. Read once, remembered by who said it, and a
    clear-out is a lookup after that."""
    import asyncio
    from types import SimpleNamespace as NS

    from wilbyte import bell
    from wilbyte.bot import client as bot_client

    where = tmp_path / "bell.json"
    monkeypatch.setattr(bell, "BELL_PATH", where)

    reads = []

    class Bell:
        async def history(self, **how):
            reads.append(how)
            if how.get("after") is not None:
                return
            for mark, when, text in (
                (11, datetime(2026, 5, 8, 14, 9), "$1548 ethos aged 6/7"),
                (12, datetime(2026, 5, 16, 17, 51), "$1440 trans aged lead"),
                (13, datetime(2026, 5, 25, 18, 46), "$1960 fresh vet lead"),
            ):
                yield NS(
                    id=mark, created_at=when, content=text, embeds=[],
                    attachments=[],
                    author=NS(id=7, display_name="Artur | NOVA |", bot=False),
                )

    monkeypatch.setattr(bot_client, "_can_read", lambda guild, ch: True)
    guild = NS(id=3, me=object(), get_channel=lambda cid: Bell())
    channels = [clearout.Channel(channel_id="1", name="ring-da-bell")]

    found, _notes = asyncio.run(bot_client._also_said(guild, NS(id=7), channels))

    assert [one.text for one in found] == [
        "$1548 ethos aged 6/7", "$1440 trans aged lead", "$1960 fresh vet lead",
    ]
    assert found[0].where == "ring-da-bell"
    assert reads[0].get("limit") == bot_client.FIRST_READ

    # And the next clear-out does not read it all again.
    again, _ = asyncio.run(bot_client._also_said(guild, NS(id=7), channels))

    assert len(again) == 3
    assert reads[1].get("after") is not None, "it read the whole channel twice"


def test_somebody_else_s_sales_are_not_this_client_s(monkeypatch, tmp_path):
    import asyncio
    from types import SimpleNamespace as NS

    from wilbyte import bell
    from wilbyte.bot import client as bot_client

    monkeypatch.setattr(bell, "BELL_PATH", tmp_path / "bell.json")

    class Bell:
        async def history(self, **how):
            yield NS(
                id=11, created_at=datetime(2026, 5, 8), content="$1,259.28 Combined",
                embeds=[], attachments=[],
                author=NS(id=99, display_name="Riley Shaffer", bot=False),
            )

    monkeypatch.setattr(bot_client, "_can_read", lambda guild, ch: True)
    found, notes = asyncio.run(bot_client._also_said(
        NS(id=3, me=object(), get_channel=lambda cid: Bell()), NS(id=7),
        [clearout.Channel(channel_id="1", name="ring-da-bell")],
    ))

    assert found == []
    assert "Nothing of theirs" in "\n".join(notes)


def test_nobody_in_the_server_is_said_rather_than_skipped(monkeypatch):
    import asyncio

    from wilbyte.bot import client as bot_client

    found, notes = asyncio.run(bot_client._also_said(None, None, []))

    assert found == []
    assert "Nobody by that name" in "\n".join(notes)


def test_what_it_searched_is_said_where_the_result_is(monkeypatch):
    """"nothing anybody said" is only checkable next to where it looked."""
    from wilbyte.bot import client as bot_client

    async def some_notes(*a, **kw):
        return [], ["Also read 2 shared channel(s) for what they said: #ring-da-bell"]

    monkeypatch.setattr(bot_client, "_also_said", some_notes)
    _guild, _channel, said, _buttons = _closing(monkeypatch, says=[True, False])

    assert "#ring-da-bell" in "\n".join(said)


def test_a_message_read_from_both_ends_is_only_kept_once():
    """A short channel is read twice over - newest first and oldest first -
    and every message in it would otherwise be in the picture twice."""
    import asyncio

    from wilbyte.bot import client as bot_client

    only = Posted(content="got them, thanks", who="artur.rushiti", bot=False)
    only.id = 99

    class Short:
        async def history(self, limit=0, oldest_first=False):
            yield only

    found, trouble = asyncio.run(bot_client._last_said(Short()))

    assert not trouble
    assert len(found) == 1, [one.text for one in found]


def test_the_first_read_and_the_catch_up_are_both_said(monkeypatch, tmp_path):
    """A first read of twenty-five thousand messages is worth knowing about,
    and so is a later one that found none."""
    import asyncio
    from types import SimpleNamespace as NS

    from wilbyte import bell
    from wilbyte.bot import client as bot_client

    monkeypatch.setattr(bell, "BELL_PATH", tmp_path / "bell.json")

    class Bell:
        async def history(self, **how):
            if how.get("after") is not None:
                return
            yield NS(
                id=11, created_at=datetime(2026, 5, 8), content="$1548 ethos",
                embeds=[], attachments=[],
                author=NS(id=7, display_name="Artur", bot=False),
            )

    monkeypatch.setattr(bot_client, "_can_read", lambda guild, ch: True)
    guild = NS(id=3, me=object(), get_channel=lambda cid: Bell())
    channels = [clearout.Channel(channel_id="1", name="ring-da-bell")]

    _found, notes = asyncio.run(bot_client._also_said(guild, NS(id=7), channels))
    assert "First read of #ring-da-bell" in "\n".join(notes)

    _again, later = asyncio.run(bot_client._also_said(guild, NS(id=7), channels))
    assert "First read" not in "\n".join(later), "it read the whole thing twice"


def test_what_it_searched_is_said_where_the_result_is(monkeypatch):
    """"nothing anybody said" is only checkable next to where it looked."""
    from wilbyte.bot import client as bot_client

    async def some_notes(*a, **kw):
        return [], ["Also read 2 shared channel(s) for what they said: #ring-da-bell"]

    monkeypatch.setattr(bot_client, "_also_said", some_notes)
    _guild, _channel, said, _buttons = _closing(monkeypatch, says=[True, False])

    assert "#ring-da-bell" in "\n".join(said)


def test_a_message_read_from_both_ends_is_only_kept_once():
    """A short channel is read twice over - newest first and oldest first -
    and every message in it would otherwise be in the picture twice."""
    import asyncio

    from wilbyte.bot import client as bot_client

    only = Posted(content="got them, thanks", who="artur.rushiti", bot=False)
    only.id = 99

    class Short:
        async def history(self, limit=0, oldest_first=False):
            yield only

    found, trouble = asyncio.run(bot_client._last_said(Short()))

    assert not trouble
    assert len(found) == 1, [one.text for one in found]


def _fed(text=LEAD_POST, at=None):
    return clearout.Said(who="Artur_Rushiti BOT", when="May 24", text=text,
                         by_bot=True, at=at, where="artur_rushiti-vet")


def test_the_feed_is_posted_when_nobody_said_anything():
    """Filtering the bots out is right when there is a conversation
    underneath them. When there is not, it left nothing at all - and the feed
    is what was delivered, which is the thing worth keeping about a channel
    like that."""
    pages = clearout.to_screenshot(_plan(), [], clearout.the_feed([_fed(), _fed()]))
    whole = "\n".join(pages)

    assert "nobody said anything in it" in whole
    assert "what was delivered" in whole
    assert "Henri Harper" in whole
    assert "1pX9NheBB7CQdoPNpOrjJjBI8saBil11or8JB9OdnXcQ" in whole


def test_what_people_said_still_wins_over_the_feed():
    """The feed is the fallback, not the answer."""
    whole = "\n".join(clearout.to_screenshot(
        _plan(),
        [clearout.Said(who="artur.rushiti", when="May 25", text="got them, thanks")],
        clearout.the_feed([_fed()]),
    ))

    assert "got them, thanks" in whole
    assert "Henri Harper" not in whole


def test_only_the_last_few_of_the_feed():
    """Enough to show what was delivered, few enough to screenshot in one go."""
    kept = clearout.the_feed([_fed(f"lead {i}") for i in range(20)])

    assert len(kept) == clearout.FEED_SHOWN
    assert kept[-1].text == "lead 19"


def test_a_channel_with_neither_still_says_to_go_and_look():
    pages = clearout.to_screenshot(_plan(), [], [])

    assert "nothing to post" in pages[0]
    assert "discord.com/channels/g9/c1" in pages[0]


def test_the_feed_is_only_the_bots():
    """What people said is not the feed, whichever list it arrives in."""
    kept = clearout.the_feed([
        _fed("a lead"),
        clearout.Said(who="artur.rushiti", when="May 25", text="thanks"),
    ])

    assert [one.text for one in kept] == ["a lead"]


def test_the_feed_reaches_the_handler(monkeypatch):
    """The fallback exists; this is it being handed the feed to fall back to.
    Without it the channel that most needs it gets nothing."""
    import asyncio
    from types import SimpleNamespace as NS

    from wilbyte.bot import client as bot_client

    Pressed.says = [True, False]
    channel = Channel("artur_rushiti-vet")
    guild = Guild(channel, Member("artur.rushiti"))

    monkeypatch.setattr(bot_client.views, "ConfirmView", Pressed)
    monkeypatch.setattr(
        bot_client.jobs, "sheet_for_agent", lambda config, who: ("https://s", [])
    )
    monkeypatch.setattr(
        bot_client.jobs, "collect_client",
        lambda config, plan, *, when: ("Ryte Collection", []),
    )

    async def only_the_feed(_channel, **kw):
        return [_fed(), _fed()], []

    async def none_elsewhere(*a, **kw):
        return [], []

    monkeypatch.setattr(bot_client, "_last_said", only_the_feed)
    monkeypatch.setattr(bot_client, "_also_said", none_elsewhere)

    said = []

    async def send(content=None, **kw):
        said.append(str(content or ""))

    asyncio.run(bot_client._clear_out(
        NS(get_guild=lambda where: guild), NS(send=send, requester_id=1),
        NS(secrets=NS(discord_clients_guild_id="3"),
           discord=NS(approval_timeout_seconds=1),
           schedule=NS(timezone="America/Chicago")),
        "artur_rushiti-vet",
    ))
    whole = "\n".join(said)

    assert "nobody said anything in it" in whole
    assert "Henri Harper" in whole, "the channel with nothing else got nothing"


def test_a_bot_in_a_shared_channel_is_not_somebody_selling(monkeypatch, tmp_path):
    """A bot's post in a shared channel is an announcement. This is a record
    of who sold what."""
    import asyncio
    from types import SimpleNamespace as NS

    from wilbyte import bell
    from wilbyte.bot import client as bot_client

    monkeypatch.setattr(bell, "BELL_PATH", tmp_path / "bell.json")

    class Bell:
        async def history(self, **how):
            yield NS(
                id=11, created_at=datetime(2026, 5, 8),
                content="Glad you're here, Cameron.", embeds=[], attachments=[],
                author=NS(id=7, display_name="Server", bot=True),
            )

    monkeypatch.setattr(bot_client, "_can_read", lambda guild, ch: True)
    found, _notes = asyncio.run(bot_client._also_said(
        NS(id=3, me=object(), get_channel=lambda cid: Bell()), NS(id=7),
        [clearout.Channel(channel_id="1", name="ring-da-bell")],
    ))

    assert found == [], "a welcome message was kept as one of their sales"


# --------------- the picture, back, with what the agents actually sent


def test_the_reactions_are_on_the_picture():
    """Half of what a sale looks like in ring-da-bell is the team piling onto
    it, and a picture of the post without them is not what was sent."""
    page = clearout.as_page(_plan(), [
        clearout.Said(who="Artur | NOVA |", when="May 08", text="$1548 ethos aged 6/7",
                      where="ring-da-bell", reactions="❤️ 4 · 🔥 2"),
    ])

    assert "$1548 ethos aged 6/7" in page
    assert "❤️ 4" in page and "🔥 2" in page
    assert "#ring-da-bell" in page


def test_a_message_with_no_reactions_gets_no_empty_box():
    page = clearout.as_page(_plan(), [
        clearout.Said(who="artur.rushiti", when="May 24", text="got them, thanks"),
    ])

    assert "class='react'" not in page


def test_what_somebody_typed_still_cannot_become_markup():
    """A client who writes "<script>" does not get to write the evidence of
    their own conversation."""
    page = clearout.as_page(_plan(), [
        clearout.Said(who="<b>Jay", when="", text="<script>alert(1)</script>",
                      reactions="<img onerror=x> 2"),
    ])

    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page
    assert "<img onerror=x>" not in page


def test_the_picture_is_named_after_them_and_the_day():
    said = clearout.picture_name(_plan(), when=datetime(2026, 9, 22))

    assert said == "Jay Rodriguez — 2026-09-22.png"


def test_a_name_with_awkward_characters_still_makes_a_filename():
    said = clearout.picture_name(
        clearout.Plan(name="Jay / Rodriguez: the 2nd"), when=datetime(2026, 9, 22),
    )

    assert "/" not in said and said.endswith(".png")


def test_the_picture_reaches_drive_and_is_reported(monkeypatch):
    """The messages above are for screenshotting now; this is the copy that
    is still there in six months when the channel is not."""
    taken = {}

    def photographing(config, page, called, *, into=""):
        taken["page"], taken["called"], taken["into"] = page, called, into
        return "https://drive.google.com/file/d/abc", []

    monkeypatch.setattr(
        __import__("wilbyte.bot.jobs", fromlist=["x"]), "keep_the_picture",
        photographing,
    )
    _guild, _channel, said, _buttons = _closing(
        monkeypatch, says=[True, False], in_channel="got them, thanks",
    )

    assert "https://drive.google.com/file/d/abc" in "\n".join(said)
    assert taken["called"].endswith(".png")


def test_a_picture_that_would_not_upload_does_not_stop_the_delete(monkeypatch):
    """The messages are posted and the row is written either way, and those
    are what the order of this exists to protect."""
    monkeypatch.setattr(
        __import__("wilbyte.bot.jobs", fromlist=["x"]), "keep_the_picture",
        lambda config, page, called, **kw: ("", ["Chromium isn't installed"]),
    )
    _guild, channel, said, _buttons = _closing(
        monkeypatch, says=[True, True], in_channel="got them, thanks",
    )

    assert channel.deleted is True
    assert "Chromium isn't installed" in "\n".join(said)


# --------------- whose face is on it


def test_their_profile_picture_comes_off_the_message():
    """A Discord message without one does not look like a Discord message."""
    from types import SimpleNamespace as NS

    from wilbyte.bot import client as bot_client

    said = bot_client._as_said(
        NS(id=11, created_at=datetime(2026, 5, 8), content="$1548 ethos aged 6/7",
           embeds=[], attachments=[], reactions=[],
           author=NS(display_name="Artur | NOVA |", bot=False,
                     display_avatar=NS(url="https://cdn.discordapp.com/a/7.png"))),
        "ring-da-bell",
    )

    assert said.avatar == "https://cdn.discordapp.com/a/7.png"


def test_somebody_with_no_picture_is_not_an_error():
    from types import SimpleNamespace as NS

    from wilbyte.bot import client as bot_client

    assert bot_client._face(NS()) == ""
    assert bot_client._face(None) == ""


def test_the_face_is_remembered_and_comes_back_with_them(monkeypatch, tmp_path):
    """The bell is read once and looked up afterwards, so a face that is not
    kept in it is a face the picture never has."""
    import asyncio
    from types import SimpleNamespace as NS

    from wilbyte import bell
    from wilbyte.bot import client as bot_client

    monkeypatch.setattr(bell, "BELL_PATH", tmp_path / "bell.json")

    class Bell:
        async def history(self, **how):
            yield NS(
                id=11, created_at=datetime(2026, 5, 8),
                content="$1548 ethos aged 6/7", embeds=[], attachments=[],
                reactions=[],
                author=NS(id=7, display_name="Artur | NOVA |", bot=False,
                          display_avatar=NS(url="https://cdn.discordapp.com/a/7.png")),
            )

    monkeypatch.setattr(bot_client, "_can_read", lambda guild, ch: True)
    found, _notes = asyncio.run(bot_client._also_said(
        NS(id=3, me=object(), get_channel=lambda cid: Bell()), NS(id=7),
        [clearout.Channel(channel_id="1", name="ring-da-bell")],
    ))

    assert [one.avatar for one in found] == ["https://cdn.discordapp.com/a/7.png"]


def test_the_face_is_on_the_page():
    page = clearout.as_page(_plan(), [
        clearout.Said(who="Artur | NOVA |", when="May 08", text="$1548 ethos",
                      avatar="https://cdn.discordapp.com/a/7.png"),
    ])

    assert "src='https://cdn.discordapp.com/a/7.png'" in page
    assert "class='pfp blank'" not in page


def test_no_face_is_a_plain_circle_rather_than_a_gap():
    page = clearout.as_page(_plan(), [
        clearout.Said(who="artur.rushiti", when="May 24", text="got them, thanks"),
    ])

    assert "class='pfp blank'" in page
    assert "<img class='pfp'" not in page


def test_a_face_that_will_not_load_becomes_the_plain_circle():
    """An avatar url remembered in May is gone the moment they change their
    picture, and a broken-image mark in the middle of the screenshot is worse
    than no picture at all."""
    page = clearout.as_page(_plan(), [
        clearout.Said(who="Artur", when="May 08", text="hi", avatar="https://x/a.png"),
    ])

    assert "onerror" in page
    assert "classList.add('blank')" in page


def test_an_avatar_url_cannot_break_out_of_the_tag():
    page = clearout.as_page(_plan(), [
        clearout.Said(who="Jay", when="", text="hi",
                      avatar="x' onerror='alert(1)"),
    ])

    assert "onerror='alert(1)" not in page


# --------------- one picture per channel, not one tall one


def test_the_messages_are_split_by_the_channel_they_were_said_in():
    """A client's own channel and the sales they rang in ring-da-bell are two
    different screenshots to the person who would have taken them by hand."""
    groups = clearout.by_channel([
        clearout.Said(who="artur", when="May 08", text="$1548",
                      where="ring-da-bell"),
        clearout.Said(who="artur", when="May 24", text="thanks",
                      where="artur_rushiti-vet"),
        clearout.Said(who="artur", when="May 25", text="$1440",
                      where="ring-da-bell"),
    ])

    assert [where for where, _ in groups] == ["ring-da-bell", "artur_rushiti-vet"]
    assert [one.text for one in groups[0][1]] == ["$1548", "$1440"]
    assert [one.text for one in groups[1][1]] == ["thanks"]


def test_messages_from_nowhere_in_particular_are_still_one_picture():
    groups = clearout.by_channel([
        clearout.Said(who="artur", when="May 24", text="thanks"),
    ])

    assert [where for where, _ in groups] == [""]


def test_nothing_said_is_no_pictures_rather_than_an_empty_one():
    assert clearout.by_channel([]) == []


def test_the_channel_is_in_the_picture_name():
    """Two files called the same thing on the same day are two files nobody
    can tell apart."""
    said = clearout.picture_name(
        _plan(), when=datetime(2026, 9, 22), where="ring-da-bell",
    )

    assert said == "Jay Rodriguez — ring-da-bell — 2026-09-22.png"


def test_a_channel_name_with_awkward_characters_still_makes_a_filename():
    said = clearout.picture_name(
        _plan(), when=datetime(2026, 9, 22), where="wins/vault: the 2nd",
    )

    assert "/" not in said and said.endswith(".png")


def test_one_picture_goes_to_drive_for_each_channel(monkeypatch):
    """"so itll be like 3 ss in total or smthing like that"."""
    import asyncio
    from types import SimpleNamespace as NS

    from wilbyte.bot import client as bot_client

    Pressed.says = [True, False]
    channel = Channel("artur_rushiti-vet")
    guild = Guild(channel, Member("artur.rushiti"))
    taken = []

    monkeypatch.setattr(bot_client.views, "ConfirmView", Pressed)
    monkeypatch.setattr(
        bot_client.jobs, "sheet_for_agent", lambda config, who: ("https://s", [])
    )
    monkeypatch.setattr(
        bot_client.jobs, "collect_client",
        lambda config, plan, *, when: ("Ryte Collection", []),
    )

    def photographing(config, page, called, *, into=""):
        taken.append((called, page, into))
        return f"https://drive/{len(taken)}.png", []

    monkeypatch.setattr(bot_client.jobs, "keep_the_picture", photographing)

    async def theirs(_channel, **kw):
        return [clearout.Said(who="artur.rushiti", when="May 24",
                              text="got them, thanks",
                              at=datetime(2026, 5, 24),
                              where="artur_rushiti-vet")], []

    async def elsewhere(*a, **kw):
        return [clearout.Said(who="Artur | NOVA |", when="May 08",
                              text="$1548 ethos aged 6/7",
                              at=datetime(2026, 5, 8), where="ring-da-bell")], []

    monkeypatch.setattr(bot_client, "_last_said", theirs)
    monkeypatch.setattr(bot_client, "_also_said", elsewhere)

    said = []

    async def send(content=None, **kw):
        said.append(str(content or ""))

    asyncio.run(bot_client._clear_out(
        NS(get_guild=lambda where: guild), NS(send=send, requester_id=1),
        NS(secrets=NS(discord_clients_guild_id="3"),
           discord=NS(approval_timeout_seconds=1),
           schedule=NS(timezone="America/Chicago")),
        "artur_rushiti-vet",
    ))
    whole = "\n".join(said)

    assert len(taken) == 2, "one tall picture of two channels is a picture of neither"
    names = [one for one, _, _ in taken]
    assert "artur_rushiti-vet" in names[0] and names[0].endswith(".png")
    assert "ring-da-bell" in names[1] and names[1].endswith(".png")
    assert names[0] != names[1], "two files nobody can tell apart"
    assert "$1548 ethos aged 6/7" in taken[1][1]
    assert {one for _, _, one in taken} == {"artur.rushiti"}, (
        "the pictures went anywhere but the folder that is theirs"
    )
    assert names[0].startswith("1 — ") and names[1].startswith("2 — "), (
        "Drive sorts by name, and a conversation out of order is not one"
    )
    assert "$1548" not in taken[0][1], "the sale ended up in the wrong picture"
    assert "#artur_rushiti-vet → <https://drive/1.png>" in whole
    assert "#ring-da-bell → <https://drive/2.png>" in whole


def test_a_channel_with_nothing_in_it_says_so_rather_than_going_quiet(monkeypatch):
    """A run with no picture line at all reads exactly like one where the
    upload quietly failed."""
    monkeypatch.setattr(
        __import__("wilbyte.bot.jobs", fromlist=["x"]), "keep_the_picture",
        lambda config, page, called, **kw: ("https://drive/p.png", []),
    )
    _guild, _channel, said, _buttons = _closing(monkeypatch, says=[True, False])
    whole = "\n".join(said)

    assert "nothing in the channel to draw" in whole
    assert "https://drive/p.png" not in whole


# --------------- a folder each, and a fallback when there isn't one


def test_the_folder_is_named_after_who_they_are_not_what_was_typed():
    """"artur rushiti", "Artur Rushiti" and "artur_rushiti-vet" all find the
    same person, and three spellings of one name is three folders."""
    plan = clearout.Plan(name="artur_rushiti-vet", member_name="artur.rushiti")

    assert clearout.their_folder(plan) == "artur.rushiti"


def test_a_client_who_is_not_in_the_server_still_gets_a_folder():
    assert clearout.their_folder(clearout.Plan(name="artur rushiti")) == "artur rushiti"


def test_a_folder_name_is_never_empty_and_never_a_path():
    assert clearout.their_folder(clearout.Plan(name="///")) == "agent"
    assert "/" not in clearout.their_folder(clearout.Plan(name="Jay / Rodriguez"))


def test_a_picture_is_numbered_so_the_conversation_is_in_order():
    said = clearout.picture_name(
        _plan(), when=datetime(2026, 9, 22), where="ring-da-bell", order=2,
    )

    assert said == "2 — Jay Rodriguez — ring-da-bell — 2026-09-22.png"


def test_their_name_stays_on_the_file_as_well_as_the_folder():
    """When their folder could not be made the picture goes in the top one
    instead, and a file that says only "1 — ring-da-bell" is unplaceable."""
    said = clearout.picture_name(_plan(), when=datetime(2026, 9, 22), order=1)

    assert "Jay Rodriguez" in said


def test_a_folder_that_cannot_be_made_does_not_lose_the_picture(monkeypatch):
    """A picture in the wrong place is something to tidy up; a channel
    deleted without one is gone."""
    from wilbyte import drive
    from wilbyte.bot import jobs

    put = {}

    class Refusing:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def folder_named(self, name):
            raise drive.DriveError("Drive refused that")

        def put(self, path, *, name="", into=""):
            put["into"] = into
            return drive.Uploaded(file_id="f1", name=name)

    monkeypatch.setattr(jobs, "_photograph", lambda html, png: png.write_bytes(b"x"))
    monkeypatch.setattr(drive, "open_drive", lambda secrets: Refusing())

    link, trouble = jobs.keep_the_picture(
        SimpleNamespace(secrets=None), "<html>", "1 — Artur.png", into="Artur",
    )

    assert link, "the picture was thrown away because a folder could not be made"
    assert put["into"] == ""
    assert any("top one" in one for one in trouble)


def test_a_picture_that_landed_in_the_wrong_folder_is_still_reported(monkeypatch):
    """It came back with a link and a complaint, and dropping the complaint
    because there was a link is how a picture ends up somewhere nobody looks."""
    monkeypatch.setattr(
        __import__("wilbyte.bot.jobs", fromlist=["x"]), "keep_the_picture",
        lambda config, page, called, **kw: (
            "https://drive/p.png", ["Couldn't make their folder, so this went "
                                    "in the top one: Drive refused that"]
        ),
    )
    _guild, _channel, said, _buttons = _closing(
        monkeypatch, says=[True, False], in_channel="got them, thanks",
    )
    whole = "\n".join(said)

    assert "https://drive/p.png" in whole
    assert "went in the top one" in whole


# --------------- the channel being deleted is the one that needs the picture


def test_their_own_channel_gets_a_picture_even_with_only_the_feed_in_it():
    """It is the channel being deleted - ring-da-bell is not - so it is the
    one that ends up with no record at all otherwise."""
    groups = clearout.to_draw(
        _plan(),
        [_fed("Henri Harper"), _fed("Rosa Vega")],
        [clearout.Said(who="Jay", when="May 08", text="$1548",
                       at=datetime(2026, 5, 8), where="ring-da-bell")],
    )

    assert [where for where, _ in groups] == ["jay-rodriguez", "ring-da-bell"]
    assert [one.text for one in groups[0][1]] == ["Henri Harper", "Rosa Vega"]


def test_their_own_channel_comes_first():
    """Picture 1 is always the channel this clear-out is about."""
    groups = clearout.to_draw(
        _plan(),
        [clearout.Said(who="Jay", when="May 24", text="thanks",
                       at=datetime(2026, 5, 24), where="jay-rodriguez")],
        [clearout.Said(who="Jay", when="May 08", text="$1548",
                       at=datetime(2026, 5, 8), where="ring-da-bell")],
    )

    assert [where for where, _ in groups] == ["jay-rodriguez", "ring-da-bell"]


def test_what_they_said_in_their_channel_wins_over_its_feed():
    """The feed is the fallback, not the answer."""
    groups = clearout.to_draw(
        _plan(),
        [_fed("Henri Harper"),
         clearout.Said(who="Jay", when="May 24", text="thanks",
                       at=datetime(2026, 5, 24), where="jay-rodriguez")],
        [],
    )

    assert [one.text for one in groups[0][1]] == ["thanks"]


def test_a_channel_with_nothing_at_all_in_it_is_no_picture():
    assert clearout.to_draw(_plan(), [], []) == []


def test_a_client_with_no_channel_still_gets_their_sales():
    groups = clearout.to_draw(
        clearout.Plan(name="Jay Rodriguez"), [],
        [clearout.Said(who="Jay", when="May 08", text="$1548",
                       at=datetime(2026, 5, 8), where="ring-da-bell")],
    )

    assert [where for where, _ in groups] == ["ring-da-bell"]


def test_a_face_remembered_before_there_was_one_is_filled_in_now(monkeypatch, tmp_path):
    """The bell is only ever read forwards, so without this the oldest
    messages - the ones worth keeping - stay faceless for good."""
    import asyncio
    from types import SimpleNamespace as NS

    from wilbyte import bell
    from wilbyte.bot import client as bot_client

    monkeypatch.setattr(bell, "BELL_PATH", tmp_path / "bell.json")
    data = {"channels": {"1": "11"}, "said": {"7": [{
        "id": "11", "who": "Artur | NOVA |", "when": "May 08",
        "text": "$1548 ethos aged 6/7", "where": "ring-da-bell",
        "at": "2026-05-08T18:09:00", "reactions": "",
    }]}}
    bell.save(data, tmp_path / "bell.json")

    class Quiet:
        async def history(self, **how):
            return
            yield

    monkeypatch.setattr(bot_client, "_can_read", lambda guild, ch: True)
    found, _notes = asyncio.run(bot_client._also_said(
        NS(id=3, me=object(), get_channel=lambda cid: Quiet()),
        NS(id=7, display_avatar=NS(url="https://cdn.discordapp.com/a/now.png")),
        [clearout.Channel(channel_id="1", name="ring-da-bell")],
    ))

    assert [one.avatar for one in found] == ["https://cdn.discordapp.com/a/now.png"]


def test_the_face_on_the_message_wins_over_the_one_they_have_now(monkeypatch, tmp_path):
    """What was remembered is what it looked like when they said it."""
    import asyncio
    from types import SimpleNamespace as NS

    from wilbyte import bell
    from wilbyte.bot import client as bot_client

    monkeypatch.setattr(bell, "BELL_PATH", tmp_path / "bell.json")
    bell.save({"channels": {"1": "11"}, "said": {"7": [{
        "id": "11", "who": "Artur", "when": "May 08", "text": "$1548",
        "where": "ring-da-bell", "at": "2026-05-08T18:09:00",
        "avatar": "https://cdn.discordapp.com/a/then.png",
    }]}}, tmp_path / "bell.json")

    class Quiet:
        async def history(self, **how):
            return
            yield

    monkeypatch.setattr(bot_client, "_can_read", lambda guild, ch: True)
    found, _notes = asyncio.run(bot_client._also_said(
        NS(id=3, me=object(), get_channel=lambda cid: Quiet()),
        NS(id=7, display_avatar=NS(url="https://cdn.discordapp.com/a/now.png")),
        [clearout.Channel(channel_id="1", name="ring-da-bell")],
    ))

    assert [one.avatar for one in found] == ["https://cdn.discordapp.com/a/then.png"]


def test_the_picture_is_cut_to_what_is_on_it(monkeypatch, tmp_path):
    """Three messages came out as three messages and a thousand pixels of
    empty purple underneath them."""
    from types import SimpleNamespace as NS

    from wilbyte.bot import jobs

    sized, asked = [], []

    class Page:
        def goto(self, where):
            pass

        def wait_for_function(self, script, timeout=0):
            pass

        def evaluate(self, script):
            # What Chromium really answers for this page. The document
            # element is never shorter than the viewport, whatever is on it;
            # the body is the content. Measured off the real thing, because
            # reasoning about it is how the empty purple got there twice.
            asked.append(script)
            if "documentElement" in script:
                return 1200
            return 276.4

        def set_viewport_size(self, size):
            sized.append(size)

        def screenshot(self, path="", full_page=False):
            import pathlib

            pathlib.Path(path).write_bytes(b"png")

    class Browser:
        def new_page(self, viewport=None):
            sized.append(viewport)
            return Page()

        def close(self):
            pass

    class Playing:
        chromium = NS(launch=lambda **kw: Browser())

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        jobs, "sync_playwright", lambda: Playing(), raising=False,
    )
    import sys
    monkeypatch.setitem(
        sys.modules, "playwright.sync_api", NS(sync_playwright=lambda: Playing()),
    )

    html = tmp_path / "convo.html"
    html.write_text("<html></html>", encoding="utf-8")
    jobs._photograph(html, tmp_path / "convo.png")

    assert sized[-1] == {"width": jobs.PICTURE_WIDTH, "height": 277}, (
        "the viewport height leaked back into the measurement"
    )
    assert "documentElement" not in " ".join(asked), (
        "asked the one thing that always answers with the viewport height"
    )


def test_the_name_is_not_said_twice_when_it_is_the_channels_name():
    """Looking somebody up by their channel name is normal, and
    "1 — artur_rushiti-vet — artur_rushiti-vet" reads like a mistake."""
    said = clearout.picture_name(
        clearout.Plan(name="artur_rushiti-vet"), when=datetime(2026, 9, 22),
        where="artur_rushiti-vet", order=1,
    )

    assert said == "1 — artur_rushiti-vet — 2026-09-22.png"


def test_a_different_channel_is_still_named():
    said = clearout.picture_name(
        clearout.Plan(name="artur_rushiti-vet"), when=datetime(2026, 9, 22),
        where="ring-da-bell", order=2,
    )

    assert said == "2 — artur_rushiti-vet — ring-da-bell — 2026-09-22.png"


# --------------- down the whole list, without picking


def test_a_run_offers_every_readable_one_with_no_cap_of_twenty_five():
    """Twenty-five is Discord's limit on a dropdown, not on how many people
    there are to close down."""
    quiet = [
        clearout.Channel(channel_id=str(i), name=f"agent-{i}", readable=True)
        for i in range(40)
    ]
    quiet.append(clearout.Channel(channel_id="x", name="shut", readable=False))

    names = clearout.one_by_one(quiet)

    assert len(names) == 40
    assert "shut" not in names, "offered one whose history cannot be read"
    assert names[0] == "agent-0", "the order of the list was not kept"


def _ran(monkeypatch, answers, *, names=("a", "b", "c")):
    """One run down a list, with each clear-out answering as told."""
    import asyncio
    from types import SimpleNamespace as NS

    from wilbyte.bot import client as bot_client

    asked, said = [], []

    async def clearing(bot, responder, config, name, *, run=None):
        asked.append((name, run))
        return answers.pop(0) if answers else "deleted"

    async def send(content=None, **kw):
        said.append(str(content or ""))

    monkeypatch.setattr(bot_client, "_clear_out", clearing)
    asyncio.run(bot_client._all_of_them(
        None, NS(send=send, requester_id=1), None, list(names),
    ))
    return asked, "\n".join(said)


def test_the_run_goes_to_the_next_one_by_itself(monkeypatch):
    """"so i dont have to pick anymore"."""
    asked, said = _ran(monkeypatch, ["deleted", "deleted", "deleted"])

    assert [one for one, _ in asked] == ["a", "b", "c"]
    assert "Deleted 3" in said


def test_each_one_says_where_it_is_in_the_list(monkeypatch):
    asked, _said = _ran(monkeypatch, ["deleted", "deleted", "deleted"])

    assert [where for _, where in asked] == [(1, 3), (2, 3), (3, 3)]


def test_leaving_one_alone_goes_on_to_the_next(monkeypatch):
    """"Leave it" means leave this one, not stop."""
    asked, said = _ran(monkeypatch, ["left", "deleted", "deleted"])

    assert [one for one, _ in asked] == ["a", "b", "c"]
    assert "Left 1" in said and "#a" in said


def test_stopping_stops(monkeypatch):
    asked, said = _ran(monkeypatch, ["deleted", "stopped", "deleted"])

    assert [one for one, _ in asked] == ["a", "b"], "kept going after stop"
    assert "Stopped at **#b**" in said
    assert "2 of 3" in said


def test_nobody_at_the_keyboard_stops_the_run(monkeypatch):
    """A run that walks a hundred and eighty channels past an empty chair is
    a hundred and eighty channels nobody looked at. A timeout is not a press,
    and "leave it" is."""
    import asyncio

    from wilbyte.bot import client as bot_client

    Pressed.says = [None]
    channel = Channel("jay-rodriguez")
    guild = Guild(channel, Member("Jay Rodriguez"))
    monkeypatch.setattr(bot_client.views, "ConfirmView", Pressed)
    monkeypatch.setattr(
        bot_client.jobs, "sheet_for_agent", lambda config, who: ("https://s", []),
    )

    async def nothing(*a, **kw):
        return [], []

    monkeypatch.setattr(bot_client, "_last_said", nothing)
    monkeypatch.setattr(bot_client, "_also_said", nothing)

    async def send(content=None, **kw):
        return None

    how = asyncio.run(bot_client._clear_out(
        SimpleNamespace(get_guild=lambda where: guild),
        SimpleNamespace(send=send, requester_id=1),
        SimpleNamespace(secrets=SimpleNamespace(discord_clients_guild_id="3"),
                        discord=SimpleNamespace(approval_timeout_seconds=1),
                        schedule=SimpleNamespace(timezone="America/Chicago")),
        "Jay Rodriguez", run=(1, 180),
    ))

    assert how == "stopped", "a timed-out question was read as an answer"
    assert channel.deleted is not True


def test_the_stop_button_stops_it_and_leaves_that_one_alone(monkeypatch):
    import asyncio

    from wilbyte.bot import client as bot_client

    Pressed.says = ["stop"]
    channel = Channel("jay-rodriguez")
    guild = Guild(channel, Member("Jay Rodriguez"))
    monkeypatch.setattr(bot_client.views, "ConfirmView", Pressed)
    monkeypatch.setattr(
        bot_client.jobs, "sheet_for_agent", lambda config, who: ("https://s", []),
    )

    async def nothing(*a, **kw):
        return [], []

    monkeypatch.setattr(bot_client, "_last_said", nothing)
    monkeypatch.setattr(bot_client, "_also_said", nothing)

    async def send(content=None, **kw):
        return None

    how = asyncio.run(bot_client._clear_out(
        SimpleNamespace(get_guild=lambda where: guild),
        SimpleNamespace(send=send, requester_id=1),
        SimpleNamespace(secrets=SimpleNamespace(discord_clients_guild_id="3"),
                        discord=SimpleNamespace(approval_timeout_seconds=1),
                        schedule=SimpleNamespace(timezone="America/Chicago")),
        "Jay Rodriguez", run=(1, 180),
    ))

    assert how == "stopped"
    assert channel.deleted is not True


def test_leaving_one_alone_is_an_answer_and_not_a_stop(monkeypatch):
    """The difference the run turns on: "leave it" is one client, a timeout
    is the whole list."""
    import asyncio

    from wilbyte.bot import client as bot_client

    Pressed.says = [False]
    channel = Channel("jay-rodriguez")
    guild = Guild(channel, Member("Jay Rodriguez"))
    monkeypatch.setattr(bot_client.views, "ConfirmView", Pressed)
    monkeypatch.setattr(
        bot_client.jobs, "sheet_for_agent", lambda config, who: ("https://s", []),
    )

    async def nothing(*a, **kw):
        return [], []

    monkeypatch.setattr(bot_client, "_last_said", nothing)
    monkeypatch.setattr(bot_client, "_also_said", nothing)

    async def send(content=None, **kw):
        return None

    how = asyncio.run(bot_client._clear_out(
        SimpleNamespace(get_guild=lambda where: guild),
        SimpleNamespace(send=send, requester_id=1),
        SimpleNamespace(secrets=SimpleNamespace(discord_clients_guild_id="3"),
                        discord=SimpleNamespace(approval_timeout_seconds=1),
                        schedule=SimpleNamespace(timezone="America/Chicago")),
        "Jay Rodriguez", run=(1, 180),
    ))

    assert how == "left"


def test_the_stop_button_is_only_there_during_a_run(monkeypatch):
    """Nothing to stop when there is one of them."""
    Pressed.says = [True, False]
    _guild, _channel, _said, buttons = _closing(monkeypatch, says=[True, False])

    assert [one.stoppable for one in buttons] == [False, False]


def test_one_client_going_wrong_does_not_stop_the_rest(monkeypatch):
    asked, said = _ran(monkeypatch, ["trouble", "deleted", "deleted"])

    assert [one for one, _ in asked] == ["a", "b", "c"]
    assert "Couldn't finish 1" in said


def test_three_wrong_in_a_row_stops_the_run(monkeypatch):
    """Three in a row is not three unlucky clients - something they all need
    is down, and the rest of the list would say so a hundred more times."""
    asked, said = _ran(
        monkeypatch, ["trouble", "trouble", "trouble", "deleted"],
        names=("a", "b", "c", "d"),
    )

    assert [one for one, _ in asked] == ["a", "b", "c"]
    assert "3 in a row" in said
    assert "something they all need is down" in said


def test_a_good_one_in_between_resets_the_count(monkeypatch):
    asked, _said = _ran(
        monkeypatch, ["trouble", "trouble", "deleted", "trouble", "deleted"],
        names=("a", "b", "c", "d", "e"),
    )

    assert [one for one, _ in asked] == ["a", "b", "c", "d", "e"]


def test_the_run_button_starts_it_rather_than_the_dropdown(monkeypatch):
    """The picker still works; this is the other way out of the same list."""
    import asyncio
    from types import SimpleNamespace

    import discord

    from wilbyte.bot import client as bot_client

    def snowflake(when):
        return (int(when.timestamp() * 1000) - 1420070400000) << 22

    class Text:
        def __init__(self, name, *, last=None):
            self.id, self.name = abs(hash(name)) % 10**6, name
            self.category = None
            self.last_message_id = snowflake(last) if last else None
            self.created_at = datetime(2026, 1, 1, tzinfo=discord.utils.utcnow().tzinfo)

    quiet_since = datetime(2026, 5, 1, tzinfo=discord.utils.utcnow().tzinfo)
    guild = SimpleNamespace(
        name="Agent Lead Lab Clients", me=None,
        text_channels=[Text("jay-rodriguez", last=quiet_since),
                       Text("connor-knudsen", last=quiet_since)],
    )

    class Pressed:
        chosen = None
        run = True

        def __init__(self, choices, **kw):
            pass

        async def wait(self):
            return None

    started = []

    async def running(bot, responder, config, names):
        started.append(list(names))

    monkeypatch.setattr(bot_client.views, "ChannelPicker", Pressed)
    monkeypatch.setattr(bot_client, "_all_of_them", running)

    async def send(content=None, **kw):
        return None

    asyncio.run(bot_client._quiet_channels(
        SimpleNamespace(get_guild=lambda where: guild),
        SimpleNamespace(send=send, requester_id=1),
        SimpleNamespace(secrets=SimpleNamespace(discord_clients_guild_id="3"),
                        discord=SimpleNamespace(approval_timeout_seconds=1),
                        schedule=SimpleNamespace(timezone="America/Chicago")),
        "",
    ))

    assert started == [["jay-rodriguez", "connor-knudsen"]]


def test_nobody_at_the_keyboard_for_the_delete_stops_the_run_too(monkeypatch):
    """The second question is the one that cannot be undone, and walking past
    an unanswered one of those is worse than walking past the first."""
    import asyncio

    from wilbyte.bot import client as bot_client

    Pressed.says = [True, None]
    channel = Channel("jay-rodriguez")
    guild = Guild(channel, Member("Jay Rodriguez"))
    monkeypatch.setattr(bot_client.views, "ConfirmView", Pressed)
    monkeypatch.setattr(
        bot_client.jobs, "sheet_for_agent", lambda config, who: ("https://s", []),
    )
    monkeypatch.setattr(
        bot_client.jobs, "collect_client",
        lambda config, plan, *, when: ("Ryte Collection", []),
    )
    monkeypatch.setattr(
        bot_client.jobs, "keep_the_picture",
        lambda config, page, called, **kw: ("https://drive/p.png", []),
    )

    async def theirs(_channel, **kw):
        return [clearout.Said(who="Jay", when="May 24", text="thanks",
                              at=datetime(2026, 5, 24), where="jay-rodriguez")], []

    async def nothing(*a, **kw):
        return [], []

    monkeypatch.setattr(bot_client, "_last_said", theirs)
    monkeypatch.setattr(bot_client, "_also_said", nothing)

    async def send(content=None, **kw):
        return None

    how = asyncio.run(bot_client._clear_out(
        SimpleNamespace(get_guild=lambda where: guild),
        SimpleNamespace(send=send, requester_id=1),
        SimpleNamespace(secrets=SimpleNamespace(discord_clients_guild_id="3"),
                        discord=SimpleNamespace(approval_timeout_seconds=1),
                        schedule=SimpleNamespace(timezone="America/Chicago")),
        "Jay Rodriguez", run=(1, 180),
    ))

    assert how == "stopped", "an unanswered delete was read as an answer"
    assert channel.deleted is not True


def test_both_questions_carry_the_stop_button_during_a_run(monkeypatch):
    """With a hundred and eighty to go there has to be a way to say enough
    that is not walking away from the keyboard."""
    import asyncio

    from wilbyte.bot import client as bot_client

    Pressed.says = [True, False]
    buttons = []

    class Watched(Pressed):
        def __init__(self, **kw):
            super().__init__(**kw)
            buttons.append(self)

    channel = Channel("jay-rodriguez")
    guild = Guild(channel, Member("Jay Rodriguez"))
    monkeypatch.setattr(bot_client.views, "ConfirmView", Watched)
    monkeypatch.setattr(
        bot_client.jobs, "sheet_for_agent", lambda config, who: ("https://s", []),
    )
    monkeypatch.setattr(
        bot_client.jobs, "collect_client",
        lambda config, plan, *, when: ("Ryte Collection", []),
    )
    monkeypatch.setattr(
        bot_client.jobs, "keep_the_picture",
        lambda config, page, called, **kw: ("https://drive/p.png", []),
    )

    async def theirs(_channel, **kw):
        return [clearout.Said(who="Jay", when="May 24", text="thanks",
                              at=datetime(2026, 5, 24), where="jay-rodriguez")], []

    async def nothing(*a, **kw):
        return [], []

    monkeypatch.setattr(bot_client, "_last_said", theirs)
    monkeypatch.setattr(bot_client, "_also_said", nothing)

    async def send(content=None, **kw):
        return None

    asyncio.run(bot_client._clear_out(
        SimpleNamespace(get_guild=lambda where: guild),
        SimpleNamespace(send=send, requester_id=1),
        SimpleNamespace(secrets=SimpleNamespace(discord_clients_guild_id="3"),
                        discord=SimpleNamespace(approval_timeout_seconds=1),
                        schedule=SimpleNamespace(timezone="America/Chicago")),
        "Jay Rodriguez", run=(1, 180),
    ))

    assert [one.stoppable for one in buttons] == [True, True]


def test_the_stop_button_is_really_gone_when_there_is_no_run():
    """Nothing to stop when there is one of them, and a button that says
    "Stop the run" under a single clear-out is a button nobody can read."""
    from wilbyte.bot import views

    alone = views.ConfirmView(
        requester_id=1, timeout=1, label="go", emoji="✅",
    )
    running = views.ConfirmView(
        requester_id=1, timeout=1, label="go", emoji="✅", stoppable=True,
    )

    assert len(alone.children) == 2
    assert len(running.children) == 3
    assert any("Stop" in str(one.label) for one in running.children)
    assert not any("Stop" in str(one.label) for one in alone.children)


# --------------- a link to the channel, to see which one he means


def test_the_channel_is_something_to_click_on():
    """"can it attach the discord link so i can check which one hes talking
    about?" - 179 of them go past, and #malaki-martinez-vet is a name until
    you have opened it."""
    said = clearout.describe(_plan())

    assert "https://discord.com/channels/g9/c1" in said
    assert "#jay-rodriguez" in said, "the name went when the link arrived"


def test_the_link_is_the_server_and_the_channel_both():
    """A channel id on its own opens nothing."""
    said = clearout.jump_to(_plan())

    assert said.endswith("(https://discord.com/channels/g9/c1)")


def test_without_a_server_it_is_still_named():
    """Better a name with no link than a link that goes nowhere."""
    said = clearout.jump_to(clearout.Plan(
        name="Jay", channel=clearout.Channel(channel_id="c1", name="jay"),
    ))

    assert said == "**#jay**"
    assert "discord.com" not in said


def test_nobody_with_a_channel_has_nothing_to_click():
    assert clearout.jump_to(clearout.Plan(name="Jay")) == ""


def test_the_delete_button_names_a_channel_you_can_open(monkeypatch):
    """The last message before the one that cannot be undone."""
    _guild, _channel, said, _buttons = _closing(
        monkeypatch, says=[True, False], in_channel="got them, thanks",
    )
    whole = "\n".join(said)

    assert "This cannot be undone" in whole
    assert whole.count("discord.com/channels/") >= 2, (
        "the delete question named a channel with no way to look at it"
    )


# ------------------------------------------- a restart in the middle of a run


def test_the_run_writes_down_where_it_is_at_every_step(monkeypatch):
    """"it stopped and didnt go thru the list" - a restart for an update ended
    it without a word, and nothing remembered where it had got to."""
    from wilbyte import quietrun
    from wilbyte.bot import client as bot_client

    seen = []

    async def clearing(bot, responder, config, name, *, run=None):
        seen.append(dict(quietrun.load()))
        return "deleted"

    import asyncio
    from types import SimpleNamespace as NS

    async def send(content=None, **kw):
        return None

    monkeypatch.setattr(bot_client, "_clear_out", clearing)
    asyncio.run(bot_client._all_of_them(
        None, NS(send=send, requester_id=7, channel_id=99), None, ["a", "b", "c"],
    ))

    assert [(one["at"], one["went"]) for one in seen] == [(0, []), (1, ["a"]), (2, ["a", "b"])]
    assert seen[0]["channel_id"] == 99 and seen[0]["requester_id"] == 7
    assert quietrun.load() is None, "a finished run was left to be offered back"


def test_a_stopped_run_is_not_offered_back(monkeypatch):
    from wilbyte import quietrun

    _ran(monkeypatch, ["deleted", "stopped"])

    assert quietrun.load() is None


def test_one_channel_breaking_does_not_end_the_run(monkeypatch):
    import asyncio
    from types import SimpleNamespace as NS

    from wilbyte.bot import client as bot_client

    asked, said = [], []

    async def clearing(bot, responder, config, name, *, run=None):
        asked.append(name)
        if name == "b":
            raise RuntimeError("Discord had a moment")
        return "deleted"

    async def send(content=None, **kw):
        said.append(str(content or ""))

    monkeypatch.setattr(bot_client, "_clear_out", clearing)
    asyncio.run(bot_client._all_of_them(None, NS(send=send, requester_id=1), None, ["a", "b", "c"]))

    assert asked == ["a", "b", "c"]
    assert "Something broke on **#b**: Discord had a moment" in said[0]
    assert "Couldn't finish 1 — #b" in said[-1]


def test_a_run_carried_on_counts_what_it_did_before(monkeypatch):
    import asyncio
    from types import SimpleNamespace as NS

    from wilbyte.bot import client as bot_client

    out = []

    async def clearing(bot, responder, config, name, *, run=None):
        return "deleted"

    async def send(content=None, **kw):
        out.append(str(content or ""))

    monkeypatch.setattr(bot_client, "_clear_out", clearing)
    asyncio.run(bot_client._all_of_them(
        None, NS(send=send, requester_id=1), None, ["c"],
        earlier={"went": ["a", "b"], "left": ["x"]},
    ))

    assert "Deleted 3 — #a, #b, #c" in out[-1] and "Left 1 — #x" in out[-1]


def test_an_unfinished_run_is_recent_and_has_somewhere_left_to_go():
    from datetime import datetime, timedelta, timezone

    from wilbyte import quietrun

    now = datetime(2026, 9, 24, 15, tzinfo=timezone.utc)
    run = {"names": ["a", "b"], "at": 1, "saved": (now - timedelta(hours=3)).isoformat()}

    assert quietrun.unfinished(run, now=now)
    assert not quietrun.unfinished({**run, "at": 2}, now=now)
    assert not quietrun.unfinished({**run, "saved": (now - timedelta(days=3)).isoformat()}, now=now)
    assert not quietrun.unfinished(None, now=now)
    assert quietrun.still_there(["a", "Gone", "b"], ["b", "A"]) == ["a", "b"]


class _Place:
    def __init__(self, names, channel):
        self.text_channels = [NS_(name=one) for one in names]
        self.channel = channel


def NS_(**kw):
    from types import SimpleNamespace

    return SimpleNamespace(**kw)


def _restarted(monkeypatch, run, *, press=True, names=("b", "c")):
    import asyncio

    from wilbyte import quietrun
    from wilbyte.bot import client as bot_client

    if run is not None:
        quietrun.save(run)
    said, carried = [], []

    class Here:
        id = 99

        async def send(self, content=None, **kw):
            said.append(str(content or ""))

    class Press:
        def __init__(self, **kw):
            self.kw, self.confirmed = kw, press

        async def wait(self):
            pass

    async def all_of_them(bot, responder, config, names, *, earlier=None):
        carried.append((list(names), earlier, responder.requester_id))

    monkeypatch.setattr(bot_client.views, "ConfirmView", Press)
    monkeypatch.setattr(bot_client, "_all_of_them", all_of_them)
    place = _Place(names, Here())
    bot = NS_(
        get_channel=lambda cid: place.channel if cid == 99 else None,
        get_guild=lambda gid: place if gid == 5 else None,
        config=NS_(secrets=NS_(discord_clients_guild_id="5"),
                   discord=NS_(approval_timeout_seconds=60)),
    )
    asyncio.run(bot_client._carry_on_the_run(bot))
    return said, carried


RUN = {"channel_id": 99, "requester_id": 7, "names": ["a", "b", "gone", "c"], "at": 1,
       "went": ["a"], "left": [], "trouble": []}


def test_after_a_restart_it_offers_to_carry_on_from_where_it_was(monkeypatch):
    said, carried = _restarted(monkeypatch, RUN)

    assert said[0] == ("🧹 I was restarted in the middle of the quiet run — I'd got to "
                       "**#b**, 2 of 4, with 1 deleted so far. 2 left to go. Carry on from there?")
    # the one deleted before the restart is not in the list any more
    assert carried == [(["b", "c"], {**RUN, "saved": carried[0][1]["saved"]}, 7)]


def test_saying_no_forgets_the_run(monkeypatch):
    from wilbyte import quietrun

    said, carried = _restarted(monkeypatch, RUN, press=False)

    assert carried == [] and quietrun.load() is None
    assert "Left it" in said[-1]


def test_nothing_is_offered_when_no_run_was_cut_short(monkeypatch):
    said, carried = _restarted(monkeypatch, None)

    assert said == [] and carried == []


def test_a_run_whose_channels_are_all_gone_is_not_offered(monkeypatch):
    from wilbyte import quietrun

    said, carried = _restarted(monkeypatch, RUN, names=())

    assert said == [] and quietrun.load() is None


def test_a_press_counts_even_when_discord_wont_redraw_the_message():
    """A press acknowledged too late makes the edit fail. Stopping only after
    it left whatever waited on the press waiting twelve hours."""
    import asyncio

    import pytest as _pytest

    from wilbyte.bot import views

    async def go():
        view = views.ConfirmView(requester_id=None, timeout=60, label="x", emoji="🧹")

        async def failing(**kw):
            raise RuntimeError("Unknown interaction")

        interaction = NS_(response=NS_(edit_message=failing))
        with _pytest.raises(RuntimeError):
            await view._close(interaction, "note")
        await asyncio.wait_for(view.wait(), 1)
        return view

    view = asyncio.run(go())
    assert view.answered


# ------------------------------------------- whose messages are whose


class _Member:
    """A server member as discord.py has one: printed as their username."""

    def __init__(self, id, display_name, name):
        self.id, self.display_name, self.global_name, self.name = id, display_name, None, name

    def __str__(self):
        return self.name


def _server(*people):
    from types import SimpleNamespace as NS

    return NS(members=[
        _Member(at, display, user) for at, (display, user) in enumerate(people, start=1)
    ])


def test_a_member_called_by_an_initial_is_nobody_elses():
    """"ryte didnt flag these" - a member called "D" is inside "dylanrankin"
    and "demetriosbrooks" both, and his two ring-da-bell messages from 2024
    were kept as Dylan's, and as Demetrios's before him - while Dylan's own
    sales were never looked for."""
    from wilbyte.bot import client

    guild = _server(("D", "d_1234"), ("Dylan Rankin", "dylanrankin_0523"),
                    ("Demetrios Brooks", "dbrooks"))

    assert client._member_called(guild, "dylan_rankin-vet").display_name == "Dylan Rankin"
    assert client._member_called(guild, "Dylan Rankin").display_name == "Dylan Rankin"
    assert client._member_called(guild, "demetrios_brooks-fex").display_name == "Demetrios Brooks"


def test_nobody_by_that_name_is_nobody_not_the_nearest():
    from wilbyte.bot import client

    guild = _server(("D", "d_1234"), ("Dylan", "dylan77"), ("Rankin", "rankin"))

    assert client._member_called(guild, "dylan_rankin-vet") is None


def test_two_people_as_likely_as_each_other_is_nobody():
    from wilbyte.bot import client

    guild = _server(("Jay Rodriguez", "jayrod1"), ("Jay Rodriguez", "jayrod2"))

    assert client._member_called(guild, "jay-rodriguez") is None


def test_the_closest_match_wins_over_a_looser_one():
    from wilbyte.bot import client

    guild = _server(("Dylan Rankin Jr", "djr"), ("Dylan Rankin", "dylanr"))

    assert client._member_called(guild, "Dylan Rankin").name == "dylanr"


def test_their_username_is_them_too():
    from wilbyte.bot import client

    guild = _server(("🔥 closer", "dylanrankin_0523"))

    assert client._member_called(guild, "Dylan Rankin").name == "dylanrankin_0523"


def test_the_lead_type_on_a_channel_name_is_not_part_of_the_person():
    assert clearout.person_in("mujeeb_anwari-standard-vet") == "mujeebanwari"
    assert clearout.person_in("joseph-temple-fb-iul") == "josephtemple"
    assert clearout.person_in("vet") == "vet"


def test_an_initial_alone_is_never_taken_for_the_client():
    from wilbyte.bot import client

    assert client._member_called(_server(("D", "d_1234")), "dylan_rankin-vet") is None


def test_a_long_lead_type_on_the_channel_name_still_finds_them():
    """"seth_essien-standard-vet" is mostly lead type; read whole, Seth's own
    name is too small a part of it to count."""
    from wilbyte.bot import client

    guild = _server(("Seth Essien", "sethe"))

    assert client._member_called(guild, "seth_essien-standard-vet").name == "sethe"


@pytest.mark.parametrize("wanted, said, score", [
    ("dylan_rankin-vet", "D", 0),
    ("demetrios_brooks-fex", "D", 0),
    ("dylan_rankin-vet", "Dylan Rankin", 3),
    ("dylan_rankin-vet", "dylanrankin_0523", 2),
    ("Dylan Rankin", "Dylan Rankin Jr", 2),
    ("Jay Rodriguez Jr", "jay-rodriguez", 1),
    ("Ann Smith", "Joann Smith", 0),
    ("Jay", "Jay Rodriguez", 0),
    ("clearout Jay Rodriguez", "jay-rodriguez", 0),
    ("seth_essien-standard-vet", "Seth Essien", 3),
])
def test_a_name_matches_word_for_word(wanted, said, score):
    assert clearout.name_match(wanted, said) == score


def test_a_channel_picked_from_the_list_is_that_channel_alone():
    """The quiet run names the channel exactly; the same person's other
    channel is not a second candidate for it."""
    both = [channel("dylan_rankin-vet"), channel("dylan_rankin-fex")]

    assert [one.name for one in clearout.channels_for("dylan_rankin-vet", both)] == ["dylan_rankin-vet"]


def test_a_person_typed_out_is_every_channel_of_theirs():
    both = [channel("dylan_rankin-vet"), channel("dylan_rankin-fex"), channel("dylan-rankins")]

    assert [one.name for one in clearout.channels_for("Dylan Rankin", both)] == [
        "dylan_rankin-vet", "dylan_rankin-fex"]


def test_a_first_name_alone_finds_no_channel():
    assert clearout.channels_for("Jay", [channel("jay-rodriguez")]) == []
    assert clearout.channels_for("Ann Smith", [channel("joann-smith")]) == []


def test_their_card_is_found_by_their_whole_name():
    from wilbyte.bot import jobs

    cards = [
        {"id": "1", "name": "New Agent - Dylan Rankin"},
        {"id": "2", "name": "New Agent - Dylan Rankins"},
        {"id": "3", "name": "NEW AGENT- Dylan"},
        {"id": "4", "name": "AGED LEAD - Dylan Rankin"},
    ]

    assert [one["id"] for one in jobs.client_cards("dylan_rankin-vet", cards)] == ["1", "4"]
    assert [one["id"] for one in jobs.client_cards("Dylan", cards)] == ["3"]


def test_two_clients_with_the_name_give_no_sheet(monkeypatch):
    from types import SimpleNamespace as NS

    from wilbyte.bot import jobs

    class Board:
        def board_cards(self, board, *, archived=False):
            return [{"id": "a", "name": "New Agent - Maria Lopez", "desc": "Phone: 312-555-0101"},
                    {"id": "b", "name": "NEW AGENT- maria lopez", "desc": "Phone: (602) 555-0199"}]

        def card_comments(self, card_id):
            raise AssertionError("read a card it couldn't be sure of")

        def close(self):
            pass

    monkeypatch.setattr(jobs, "open_trello", lambda config: Board())
    config = NS(secrets=NS(trello_board_id="b"))

    sheet, problems = jobs.sheet_for_agent(config, "Maria Lopez")

    assert sheet == "" and "not guessing" in problems[0]


def test_a_repeat_clients_newest_card_gives_the_sheet(monkeypatch):
    from types import SimpleNamespace as NS

    from wilbyte.bot import jobs

    old, new = "5f000000" + "0" * 16, "68000000" + "0" * 16
    read = []

    class Board:
        def board_cards(self, board, *, archived=False):
            return [{"id": new, "name": "New Agent - Dylan Rankin"},
                    {"id": old, "name": "New Agent - Dylan Rankin"}]

        def card_comments(self, card_id):
            read.append(card_id)
            return ["Sheet link: https://docs.google.com/spreadsheets/d/" + "n" * 30 + "/edit"]

        def close(self):
            pass

    monkeypatch.setattr(jobs, "open_trello", lambda config: Board())

    jobs.sheet_for_agent(NS(secrets=NS(trello_board_id="b")), "Dylan Rankin")

    assert read == [new]


def test_a_card_and_a_channel_that_disagree_are_both_shown(monkeypatch):
    rows = []
    _guild, _channel, said, _buttons = _closing(
        monkeypatch, says=[True, False],
        on_card="https://docs.google.com/spreadsheets/d/onthecard", in_channel=LEAD_POST,
        rows=rows,
    )

    assert rows[0][1] == "https://docs.google.com/spreadsheets/d/onthecard"
    assert any("Their channel has a different sheet" in one for one in said)


def test_the_same_sheet_whatever_tab_or_tail():
    link = "https://docs.google.com/spreadsheets/d/1pX9NheBB7CQdoPNpOrjJjBI8saBil11or8JB9OdnXcQ"

    assert clearout.same_sheet(link + "/edit?usp=sharing", link + "/edit#gid=77")
    assert not clearout.same_sheet(link, "https://docs.google.com/spreadsheets/d/" + "x" * 30)
    assert not clearout.same_sheet("", link)



def test_one_clients_orders_are_one_person_whatever_the_card_says():
    from wilbyte.bot import jobs

    assert not jobs.different_people([
        {"desc": "Phone: 312-555-0101"}, {"desc": "copied\nPhone: +1 (312) 555-0101"},
        {"desc": "no number on this one"},
        {"desc": "Phone: 312-555-0101\nSpouse: 602-555-0100"},
    ])
    assert jobs.different_people([{"desc": "312-555-0101"}, {"desc": "602-555-0199"}])


# ------------------------------------------- the clear-outs already done


def test_the_collection_tab_is_read_back_by_its_headings():
    rows = [["Client Name", "Sheet Link", "Discord Channel", "Date Removed"],
            ["dylan_rankin-vet", "https://s", "dylan_rankin-vet", "2026-09-25"],
            ["", "", "", ""]]

    assert clearout.read_rows(rows) == [
        {"name": "dylan_rankin-vet", "sheet": "https://s", "channel": "dylan_rankin-vet",
         "when": "2026-09-25"}]


def test_a_tab_with_no_headings_is_read_in_the_order_it_was_written():
    assert clearout.read_rows([["artur_rushiti-vet", "https://s", "artur_rushiti-vet", "2026-09-01"]]) == [
        {"name": "artur_rushiti-vet", "sheet": "https://s", "channel": "artur_rushiti-vet",
         "when": "2026-09-01"}]


def test_the_old_rule_is_kept_to_find_what_it_got_wrong():
    guild = _server(("D", "d_1234"), ("Dylan Rankin", "dylanrankin_0523"))

    assert clearout.old_member(guild.members, "dylan_rankin-vet").display_name == "D"


def _goat(monkeypatch, rows, *people):
    from types import SimpleNamespace as NS

    from wilbyte.bot import client, jobs

    guild = _server(*people)
    guild.text_channels = []
    monkeypatch.setattr(client, "_clients_guild", lambda bot, config: guild)
    monkeypatch.setattr(jobs, "cleared_out", lambda config: (rows, []))
    said = []

    class Here:
        async def send(self, content=None, **kw):
            said.append(str(content or ""))

    return guild, Here(), said


def test_the_clear_outs_that_used_somebody_elses_messages_are_listed(monkeypatch):
    import asyncio

    from wilbyte.bot import client

    rows = [{"name": "dylan_rankin-vet", "channel": "dylan_rankin-vet", "when": "2026-09-25"},
            {"name": "demetrios_brooks-fex", "channel": "demetrios_brooks-fex", "when": "2026-09-24"},
            {"name": "seth_essien-iul", "channel": "seth_essien-iul", "when": "2026-09-23"}]
    _guild, here, said = _goat(monkeypatch, rows, ("D", "d_1234"), ("Dylan Rankin", "dylanr"),
                               ("Seth Essien", "sethe"))

    asyncio.run(client._check_clearouts(None, here, None))

    text = said[0]
    assert "2 of 3 clear-outs" in text
    assert "**dylan_rankin-vet**" in text and "Should be **Dylan Rankin**" in text
    assert "`@RYTE redo bell dylan_rankin-vet`" in text
    assert "**demetrios_brooks-fex**" in text and "Nobody in the server is them" in text
    assert "seth_essien" not in text
    assert "“d_1234”" in text


def test_all_matched_right_is_said(monkeypatch):
    import asyncio

    from wilbyte.bot import client

    rows = [{"name": "seth_essien-iul", "channel": "seth_essien-iul", "when": "x"}]
    _guild, here, said = _goat(monkeypatch, rows, ("Seth Essien", "sethe"))

    asyncio.run(client._check_clearouts(None, here, None))

    assert said[0].startswith("✅ Checked all 1 clear-outs")


def test_redrawing_uses_only_their_own_messages(monkeypatch):
    import asyncio

    from wilbyte.bot import client, jobs

    rows = []
    guild, here, said = _goat(monkeypatch, rows, ("D", "d_1234"), ("Dylan Rankin", "dylanr"))
    asked = []

    async def their(g, member, channels):
        asked.append(member.display_name)
        return [clearout.Said(who="Dylan Rankin", when="Jun 3", text="$1872 Vet", where="ring-da-bell")], []

    kept = []
    monkeypatch.setattr(client, "_also_said", their)
    monkeypatch.setattr(jobs, "keep_the_picture",
                        lambda config, page, called, into="": kept.append((called, into)) or ("https://drive/x", []))
    config = type("C", (), {"schedule": type("S", (), {"timezone": "America/Chicago"})()})()

    asyncio.run(client._redo_bell(None, here, config, "dylan_rankin-vet"))

    assert asked == ["Dylan Rankin"]
    assert kept[0][0].endswith(" (corrected).png") and kept[0][1] == "dylanr"
    assert "✅ #ring-da-bell → <https://drive/x>" in said[0]


def test_nobody_by_that_whole_name_draws_nothing(monkeypatch):
    import asyncio

    from wilbyte.bot import client

    _guild, here, said = _goat(monkeypatch, [], ("D", "d_1234"))

    asyncio.run(client._redo_bell(None, here, None, "dylan_rankin-vet"))

    assert "Nothing drawn" in said[0]


@pytest.mark.parametrize("asked, action, brief", [
    ("check clearouts", "checkclearouts", None),
    ("check clear-outs", "checkclearouts", None),
    ("redo bell Dylan Rankin", "redobell", "Dylan Rankin"),
    ("redo ring da bell for dylan_rankin-vet", "redobell", "dylan_rankin-vet"),
    ("clearout Dylan Rankin", "clearout", None),
])
def test_the_commands_are_told_apart(asked, action, brief):
    from wilbyte.bot import mentions

    got = mentions.parse(f"<@1> {asked}")
    assert got.action == action
    if brief is not None:
        assert got.brief == brief


def test_a_clear_out_that_matched_nobody_then_is_not_listed(monkeypatch):
    import asyncio

    from wilbyte.bot import client

    rows = [{"name": "zora_quill-vet", "channel": "zora_quill-vet", "when": "x"}]
    _guild, here, said = _goat(monkeypatch, rows, ("Seth Essien", "sethe"))

    asyncio.run(client._check_clearouts(None, here, None))

    assert said[0].startswith("✅")


def test_the_channel_is_who_they_were_when_the_name_typed_was_short(monkeypatch):
    """"Dylan" was typed; the channel it deleted says which Dylan."""
    import asyncio

    from wilbyte.bot import client

    rows = [{"name": "Dylan", "channel": "dylan_rankin-vet", "when": "x"}]
    _guild, here, said = _goat(monkeypatch, rows, ("D", "d_1234"), ("Dylan Rankin", "dylanr"))

    asyncio.run(client._check_clearouts(None, here, None))

    assert "Should be **Dylan Rankin**" in said[0]
