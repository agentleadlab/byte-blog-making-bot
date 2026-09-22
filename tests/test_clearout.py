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
    assert "<#c1>" in pages[0], "no way to go and look"
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
    assert "<#c1>" in pages[0]


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

    def photographing(config, page, called):
        taken["page"], taken["called"] = page, called
        return "https://drive.google.com/file/d/abc", []

    monkeypatch.setattr(
        __import__("wilbyte.bot.jobs", fromlist=["x"]), "keep_the_picture",
        photographing,
    )
    _guild, _channel, said, _buttons = _closing(monkeypatch, says=[True, False])

    assert "https://drive.google.com/file/d/abc" in "\n".join(said)
    assert taken["called"].endswith(".png")


def test_a_picture_that_would_not_upload_does_not_stop_the_delete(monkeypatch):
    """The messages are posted and the row is written either way, and those
    are what the order of this exists to protect."""
    monkeypatch.setattr(
        __import__("wilbyte.bot.jobs", fromlist=["x"]), "keep_the_picture",
        lambda config, page, called: ("", ["Chromium isn't installed"]),
    )
    _guild, channel, said, _buttons = _closing(monkeypatch, says=[True, True])

    assert channel.deleted is True
    assert "Chromium isn't installed" in "\n".join(said)
