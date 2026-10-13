"""Orders in Discord made into Trello cards when Franklin drops the gavel."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from wilbyte import ordercards
from wilbyte.bot import client, jobs, mentions

FRANKLIN, KHARYL, RYTE = 11, 22, 99
WHEN = datetime(2026, 10, 13, 15, 35, tzinfo=timezone.utc)
CONFIG = SimpleNamespace(secrets=SimpleNamespace(
    discord_notify_user_id=str(FRANKLIN), order_gavel_user_ids="", trello_board_id="b",
    order_fresh_list="", order_aged_list=""))


# ------------------------------------------------------------- the pieces

def test_the_gavel_by_name_from_any_server():
    assert ordercards.is_gavel(SimpleNamespace(name="leadlab_hammertime", id=123))
    assert ordercards.is_gavel(":leadlab_hammertime:")
    assert not ordercards.is_gavel(SimpleNamespace(name="✅")) and not ordercards.is_gavel(None)


def test_only_franklin_or_whoever_is_named_drops_it():
    assert ordercards.may_gavel(FRANKLIN, CONFIG) and not ordercards.may_gavel(KHARYL, CONFIG)
    named = SimpleNamespace(secrets=SimpleNamespace(discord_notify_user_id=str(FRANKLIN), order_gavel_user_ids="22, 33"))
    assert ordercards.may_gavel(KHARYL, named) and not ordercards.may_gavel(FRANKLIN, named)
    nobody = SimpleNamespace(secrets=SimpleNamespace(discord_notify_user_id="", order_gavel_user_ids=""))
    assert not ordercards.may_gavel(FRANKLIN, nobody)


def test_the_lists():
    lists = [{"id": "1", "name": "Tre"}, {"id": "2", "name": "📥 In Que"}, {"id": "3", "name": "Aged Leads Order Done"},
             {"id": "4", "name": "Queue", "closed": True}]
    assert ordercards.list_for("fresh", lists)["id"] == "2"
    assert ordercards.list_for("aged", lists)["id"] == "3"
    assert ordercards.list_for("fresh", lists, wanted="Tre")["id"] == "1"
    assert ordercards.list_for("aged", [{"id": "9", "name": "Aged Leads"}]) is None


def test_card_links_and_changed_lines():
    assert ordercards.card_id_of("https://trello.com/c/Hl06MPly/17397-ryte") == "Hl06MPly"
    assert ordercards.card_id_of("nope") == ""
    assert ordercards.changed_lines("Name: Cole\nStates:", "Name: Cole\nStates: AZ, TX\nLeads: 25") == [
        "States: AZ, TX", "Leads: 25"]


def test_templates_default_and_set():
    assert ordercards.templates() == {"fresh": "https://trello.com/c/Hl06MPly", "aged": ""}
    ordercards.set_template("aged", "https://trello.com/c/AgEd1234")
    assert ordercards.templates()["aged"] == "https://trello.com/c/AgEd1234"


@pytest.mark.parametrize("said, action, brief", [
    ("<@1> new agent template https://trello.com/c/Hl06MPly", "ordertemplate", "fresh https://trello.com/c/Hl06MPly"),
    ("<@1> aged template <https://trello.com/c/AgEd1234/9-x>", "ordertemplate", "aged https://trello.com/c/AgEd1234/9-x"),
    ("<@1> order templates", "ordertemplates", None),
])
def test_commands(said, action, brief):
    got = mentions.parse(said)
    assert got.action == action and (brief is None or got.brief == brief)


# ------------------------------------------------------------- the board

class Board:
    def __init__(self, desc=""):
        self.made, self.comments, self.descs, self.names = [], [], [], []
        self.desc = desc

    def board_lists(self, board_id):
        return [{"id": "q", "name": "In Que"}, {"id": "a", "name": "Aged Leads Order Done"}]

    def copy_card(self, list_id, source_id, *, name, desc, due=None, **kw):
        self.made.append(dict(list=list_id, source=source_id, name=name, desc=desc, due=due))
        return {"id": f"c{len(self.made)}", "shortUrl": f"https://trello.com/c/new{len(self.made)}"}

    def card_full(self, card_id):
        return {"id": f"id-{card_id}", "name": "New Agent - Jane Doe 25 VET", "desc": "Name:\nStates:", "shortUrl": card_id}

    def card_detail(self, card_id):
        return {"desc": self.desc, "name": "x"}

    def set_description(self, card_id, text):
        self.descs.append(text)
        self.desc = text

    def rename_card(self, card_id, name):
        self.names.append(name)

    def add_comment(self, card_id, text):
        self.comments.append(text)

    def close(self):
        pass


def test_a_card_is_a_copy_of_the_example_in_its_list_with_the_due_date(monkeypatch):
    board = Board()
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)
    made = jobs.order_card_make(CONFIG, "aged", {"name": "Aged - Cole Petersen", "desc": "Name: Cole", "due": "2026-10-14"},
                                {"id": "TPL"})
    assert made == {"id": "c1", "url": "https://trello.com/c/new1", "list": "Aged Leads Order Done"}
    assert board.made == [dict(list="a", source="TPL", name="Aged - Cole Petersen", desc="Name: Cole",
                               due="2026-10-14T17:00:00.000Z")]
    jobs.order_card_make(CONFIG, "fresh", {"name": "New Agent - Fabian", "desc": "", "due": "soon"}, {"id": "TPL"})
    assert board.made[1]["list"] == "q" and board.made[1]["due"] is None


def test_new_notes_rewrite_rytes_description_but_only_comment_on_one_edited_by_hand(monkeypatch):
    board = Board(desc="Name: Cole\nStates:")
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)
    done, said = jobs.order_card_update(CONFIG, "c1", written="Name: Cole\nStates:", desc="Name: Cole\nStates: AZ")
    assert (done, said, board.descs) == ("updated", [], ["Name: Cole\nStates: AZ"])

    board.desc = "Name: Cole\nStates: AZ\nKharyl: call him first"  # edited on Trello
    done, said = jobs.order_card_update(CONFIG, "c1", written="Name: Cole\nStates: AZ", desc="Name: Cole\nStates: AZ, TX")
    assert done == "commented" and said == ["States: AZ, TX"] and len(board.descs) == 1
    assert board.comments == ["📝 New in Discord since this card was made:\nStates: AZ, TX"]
    again = jobs.order_card_update(CONFIG, "c1", written="Name: Cole\nStates: AZ", desc="Name: Cole\nStates: AZ, TX",
                                   commented=tuple(said))
    assert again == ("same", []) and len(board.comments) == 1


# ------------------------------------------------------------- the gavel

class Responder:
    def __init__(self):
        self.sent = []

    async def send(self, text=None, **kw):
        self.sent.append((text, kw.get("view")))
        return SimpleNamespace(id=len(self.sent))


def _message(text="New Payment | Payra\nNAME: Cole Petersen\nPRODUCT: Aged Leads (MTG)", thread=None):
    author = SimpleNamespace(display_name="LeadLab", name="leadlab", bot=True, id=5)
    channel = SimpleNamespace(id=700, parent=None, name="new-payment")
    message = SimpleNamespace(id=900, content=text, embeds=[], attachments=[], message_snapshots=[],
                              author=author, created_at=WHEN, channel=channel, thread=thread,
                              jump_url="https://discord.com/channels/1/700/900", guild=None)

    async def fetch(i):
        return message

    channel.fetch_message = fetch
    return message


def _notes(*said):
    class Thread:
        id = 900
        parent = SimpleNamespace(name="new-payment")

        def history(self, **kw):
            async def gen():
                for at, text in enumerate(said):
                    yield SimpleNamespace(id=at, content=text, embeds=[], attachments=[], message_snapshots=[],
                                          author=SimpleNamespace(display_name="Kharyl", name="kharyl", bot=False),
                                          created_at=WHEN)
            return gen()
    return Thread()


@pytest.fixture
def order(monkeypatch):
    """A gavelled order: what Claude makes of it, the board, Ryte The Goat."""
    held = SimpleNamespace(reads=[], made=[], responder=Responder(), kind="aged", pressed=True)

    def reading(config, *, text, images, today, examples):
        held.reads.append(text)
        return {"kind": held.kind, "client": "Cole Petersen", "name": f"{held.kind} - Cole Petersen",
                "desc": f"Name: Cole Petersen\nread {len(held.reads)}", "due": "2026-10-14",
                "missing": ["States"]}

    def making(config, kind, card, template):
        held.made.append((kind, card["name"], card["desc"], template["id"]))
        return {"id": "card1", "url": "https://trello.com/c/new1",
                "list": "Aged Leads Order Done" if kind == "aged" else "In Que"}

    class Confirm:
        def __init__(self, **kw):
            self.kw, self.confirmed = kw, False

        async def wait(self):
            self.confirmed = held.pressed

    monkeypatch.setattr(jobs, "read_order", reading)
    monkeypatch.setattr(jobs, "order_card_make", making)
    monkeypatch.setattr(jobs, "order_template", lambda config, kind: {"id": f"TPL-{kind}", "name": "x", "desc": "", "url": "u"})
    monkeypatch.setattr(client, "_board_responder", lambda bot: held.responder)
    monkeypatch.setattr(client, "_today", lambda config: date(2026, 10, 13))
    monkeypatch.setattr(client.views, "ConfirmView", Confirm)
    return held


def _bot(message):
    return SimpleNamespace(config=CONFIG, get_channel=lambda i: message.channel, loop=asyncio.get_event_loop_policy())


GAVEL = SimpleNamespace(message_id=900, channel_id=700, user_id=FRANKLIN)


def test_aged_is_made_straight_away_and_said_in_ryte_the_goat(order):
    message = _message(thread=_notes("25 aged MTG, states AZ TX", "launch tomorrow"))
    asyncio.run(client._order_gavelled(_bot(message), GAVEL))
    assert order.made == [("aged", "aged - Cole Petersen", "Name: Cole Petersen\nread 1", "TPL-aged")]
    [read] = order.reads
    assert "NAME: Cole Petersen" in read and "Kharyl: 25 aged MTG, states AZ TX" in read and "launch tomorrow" in read
    [(said, view)] = order.responder.sent
    assert "Aged Leads card made" in said and "https://trello.com/c/new1" in said and view is None
    held = ordercards.load()["900"]
    assert held["status"] == "made" and held["card_id"] == "card1" and held["thread_id"] == "900"

    asyncio.run(client._order_gavelled(_bot(message), GAVEL))
    assert len(order.made) == 1, "a second gavel made a second card"


def test_fresh_is_made_straight_away_too(order):
    """"i want it that when ryte saw that emoji, it creates the card automatically"."""
    assert client.FRESH_ASKS_FIRST is False
    order.kind = "fresh"
    message = _message("New Payment | Payra\nNAME: Gregory Stevens\nPRODUCT: Text-Verified Trucker IUL")
    asyncio.run(client._order_gavelled(_bot(message), GAVEL))
    assert order.made == [("fresh", "fresh - Cole Petersen", "Name: Cole Petersen\nread 1", "TPL-fresh")]
    [(said, view)] = order.responder.sent
    assert "New Agent card made" in said and "In Que" in said and view is None
    assert ordercards.load()["900"]["status"] == "made"


def test_with_the_button_back_fresh_waits_for_it_and_reads_the_notes_again(order, monkeypatch):
    monkeypatch.setattr(client, "FRESH_ASKS_FIRST", True)
    order.kind = "fresh"
    message = _message("New Payment | Payra\nNAME: Fabian Ubilla\nPRODUCT: VET")
    asyncio.run(client._order_gavelled(_bot(message), GAVEL))
    (asked, view), (done, _v) = order.responder.sent
    assert "New Agent card" in asked and "Queue" in asked and view.kw["requester_id"] == FRANKLIN
    assert len(order.reads) == 2, "the order wasn't read again after the button"
    assert order.made == [("fresh", "fresh - Cole Petersen", "Name: Cole Petersen\nread 2", "TPL-fresh")]
    assert "New Agent card made" in done


def test_fresh_left_alone_makes_nothing_and_can_be_gavelled_again(order, monkeypatch):
    monkeypatch.setattr(client, "FRESH_ASKS_FIRST", True)
    order.kind, order.pressed = "fresh", False
    message = _message()
    asyncio.run(client._order_gavelled(_bot(message), GAVEL))
    assert order.made == [] and "900" not in ordercards.load()
    order.pressed = True
    asyncio.run(client._order_gavelled(_bot(message), GAVEL))
    assert len(order.made) == 1


def test_no_example_card_says_so_and_makes_nothing(order, monkeypatch):
    def none(config, kind):
        raise ValueError("no example")

    monkeypatch.setattr(jobs, "order_template", none)
    asyncio.run(client._order_gavelled(_bot(_message()), GAVEL))
    assert order.made == [] and "no example Aged Leads card" in order.responder.sent[0][0]
    assert "900" not in ordercards.load()


def test_a_note_after_the_card_updates_it(order, monkeypatch):
    message = _message(thread=_notes("25 aged MTG"))
    asyncio.run(client._order_gavelled(_bot(message), GAVEL))
    calls = []

    def updating(config, card_id, *, written, desc, name="", commented=()):
        calls.append((card_id, written, desc))
        return "updated", []

    monkeypatch.setattr(jobs, "order_card_update", updating)
    done = asyncio.run(client._order_refresh(_bot(message), "900", settle=0))
    assert done == "updated" and calls == [("card1", "Name: Cole Petersen\nread 1", "Name: Cole Petersen\nread 2")]
    assert ordercards.load()["900"]["written"] == "Name: Cole Petersen\nread 2"
    assert "updated from the new notes" in order.responder.sent[-1][0]


def test_a_tag_with_no_command_in_growth_gets_no_answer(monkeypatch):
    answered = []

    async def answer(bot, message, **kw):
        answered.append(message.content)

    monkeypatch.setattr(client, "answer_mention", answer)
    monkeypatch.setattr(client, "_in_setup_guild", lambda bot, guild: guild == "growth")
    monkeypatch.setattr(client, "_order_note_posted", lambda bot, message: None)
    me = SimpleNamespace(id=RYTE)
    fake = SimpleNamespace(user=me, config=CONFIG, loop=None)

    def said(text, guild):
        return SimpleNamespace(content=text, guild=guild, author=SimpleNamespace(id=FRANKLIN, bot=False),
                               mentions=[me], role_mentions=[], mention_everyone=False, reference=None,
                               channel=SimpleNamespace(id=1))

    monkeypatch.setattr(client, "is_direct_mention", lambda message, user: True)
    asyncio.run(client.WilByteBot.on_message(fake, said(f"<@{RYTE}> Cole Petersen 25 aged", "growth")))
    assert answered == []
    asyncio.run(client.WilByteBot.on_message(fake, said(f"<@{RYTE}> checklist", "growth")))
    asyncio.run(client.WilByteBot.on_message(fake, said(f"<@{RYTE}> hi", "goat")))
    assert answered == [f"<@{RYTE}> checklist", f"<@{RYTE}> hi"]


def test_a_second_gavel_while_the_first_waits_on_its_button_is_ignored(order):
    order.kind = "fresh"
    message = _message()
    client._ORDER_ACTIVE.add("900")
    try:
        asyncio.run(client._order_gavelled(_bot(message), GAVEL))
    finally:
        client._ORDER_ACTIVE.discard("900")
    assert order.responder.sent == [] and order.made == []


@pytest.mark.parametrize("said", [
    "<@1> payment link $621 for 40 basic spanish leads",
    "<@1> can you make a stripe link for 25 VET leads $1125",
    "<@1> can I get a klarna link for Cole, 25 OTP vets, $1,125",
    "<@1> make a link 30 trucker iul 1350",
])
def test_a_stripe_link_however_its_asked_is_answered_in_growth(said, monkeypatch):
    """"make sure ryte this respond there for the stripe links" - a tag with
    no command in the Growth server is ignored now, so a link asked for in
    plain words has to read as one."""
    assert mentions.parse(said).action == "payment"
    answered = []

    async def answer(bot, message, **kw):
        answered.append(message.content)

    monkeypatch.setattr(client, "answer_mention", answer)
    monkeypatch.setattr(client, "_in_setup_guild", lambda bot, guild: True)
    monkeypatch.setattr(client, "is_direct_mention", lambda message, user: True)
    me = SimpleNamespace(id=RYTE)
    message = SimpleNamespace(content=said, guild="growth", author=SimpleNamespace(id=KHARYL, bot=False),
                              channel=SimpleNamespace(id=1))
    asyncio.run(client.WilByteBot.on_message(SimpleNamespace(user=me, config=CONFIG, loop=None), message))
    assert answered == [said]


@pytest.mark.parametrize("said", [
    "<@1> send me the link to the sheet", "<@1> Cole Petersen 25 aged MTG", "<@1> what's the link for zoom",
])
def test_other_links_and_order_notes_arent_payment_links(said):
    assert mentions.parse(said).action != "payment"


# ------------------------------------------------------------- nothing happened?

def test_the_gavellers_are_read_whatever_the_ids_look_like():
    config = SimpleNamespace(secrets=SimpleNamespace(discord_notify_user_id="<@111111111>", order_gavel_user_ids=""))
    assert ordercards.gavellers(config) == ["111111111"] and ordercards.may_gavel(111111111, config)


def test_a_gavel_that_doesnt_count_is_said_once(order):
    payload = SimpleNamespace(message_id=900, channel_id=700, guild_id=1, user_id=KHARYL,
                              member=SimpleNamespace(display_name="Kharyl"))
    bot = _bot(_message())
    asyncio.run(client._order_gavel_refused(bot, payload))
    asyncio.run(client._order_gavel_refused(bot, payload))
    [(said, _view)] = order.responder.sent
    assert "Kharyl put the gavel on an order" in said and f"<@{FRANKLIN}>" in said and "nothing made" in said
    assert "`@Ryte gavel allow 22`" in said and order.made == []
    client._ORDER_ACTIVE.clear()


def test_order_by_link_does_what_the_gavel_does(order):
    message = _message(thread=_notes("25 aged MTG"))
    bot = _bot(message)
    me = SimpleNamespace(id=FRANKLIN, display_name="Franklin")
    asyncio.run(client._order_from_link(bot, order.responder, "order https://discord.com/channels/1/700/900", me))
    assert order.made and order.made[0][0] == "aged"
    asyncio.run(client._order_from_link(bot, order.responder, "order https://discord.com/channels/1/700/900", me))
    assert len(order.made) == 1 and "already made" in order.responder.sent[-1][0]
    them = SimpleNamespace(id=KHARYL)
    asyncio.run(client._order_from_link(bot, order.responder, "order https://discord.com/channels/1/700/901", them))
    assert "Only the gavel's owner" in order.responder.sent[-1][0]


def test_order_check_says_whats_missing(order, monkeypatch):
    def none(config, kind):
        if kind == "aged":
            raise ValueError("unset")
        return {"id": "T", "name": "New Agent - Jane Doe", "desc": "", "url": "https://trello.com/c/Hl06MPly"}

    monkeypatch.setattr(jobs, "order_template", none)
    monkeypatch.setattr(client, "_order_board_lists", lambda config: [{"id": "q", "name": "In Que"}])
    order.responder.channel = SimpleNamespace(name="trello-manager")
    asyncio.run(client._order_check(_bot(_message()), order.responder))
    said = order.responder.sent[-1][0]
    assert f"✅ Gavel counts from <@{FRANKLIN}>" in said and "✅ Said in #trello-manager" in said
    assert "✅ New Agent example: New Agent - Jane Doe" in said and "❌ No Aged Leads example card" in said
    assert "✅ New Agent cards go in **In Que**" in said and "❌ No list for Aged Leads cards" in said


@pytest.mark.parametrize("said, action", [
    ("<@1> order https://discord.com/channels/1/2/3", "orderlink"),
    ("<@1> make card <https://discord.com/channels/1/2/3>", "orderlink"),
    ("<@1> order check", "ordercheck"), ("<@1> gavel check", "ordercheck"),
])
def test_order_commands(said, action):
    assert mentions.parse(said).action == action


def test_anything_that_breaks_is_said_and_nothing_is_made(order, monkeypatch):
    def broken(config, kind, card, template):
        raise RuntimeError("Trello said no")

    monkeypatch.setattr(jobs, "order_card_make", broken)
    asyncio.run(client._order_gavelled(_bot(_message()), GAVEL))
    assert "Couldn't make the card" in order.responder.sent[-1][0]

    async def boom(*a, **k):
        raise RuntimeError("Discord hiccup")

    monkeypatch.setattr(client, "_order_make", boom)
    asyncio.run(client._order_gavelled(_bot(_message()), GAVEL))
    assert "Something broke making that order's card: Discord hiccup" in order.responder.sent[-1][0]
    assert "900" not in ordercards.load()


def test_gavels_dropped_while_ryte_was_down_are_found_anywhere(order, monkeypatch):
    """"he has to search everywhere where he can see his dedicated emoji"."""
    now = datetime(2026, 10, 13, 23, 0, tzinfo=timezone.utc)
    gavel = SimpleNamespace(name="leadlab_hammertime")

    def reaction(*people):
        async def users():
            for one in people:
                yield one
        return SimpleNamespace(emoji=gavel, users=users)

    def posted(i, *reactions, channel_id=700):
        message = _message()
        message.id, message.reactions = i, list(reactions)
        message.channel = SimpleNamespace(id=channel_id, parent=None, name="anywhere", fetch_message=None)
        return message

    franklin, kharyl = SimpleNamespace(id=FRANKLIN), SimpleNamespace(id=KHARYL)
    by_place = {
        700: [posted(901, reaction(franklin)), posted(902), posted(903, reaction(kharyl))],
        701: [posted(904, reaction(franklin), channel_id=701)],
    }

    class Place:
        def __init__(self, i, last):
            self.id, self.last_message_id = i, last

        def history(self, **kw):
            async def gen():
                for one in by_place.get(self.id, []):
                    yield one
            return gen()

    recent = int(((now.timestamp() - 3600) * 1000 - 1420070400000)) << 22
    old = int(((now.timestamp() - 3 * 86400) * 1000 - 1420070400000)) << 22
    guild = SimpleNamespace(id=1, text_channels=[Place(700, recent), Place(701, old)])

    async def threads():
        return []

    guild.active_threads = threads
    ordercards.save({"999": {"status": "made"}})
    gavelled = []

    async def gavelling(bot, payload):
        gavelled.append((payload.message_id, payload.user_id))

    monkeypatch.setattr(client, "_order_gavelled", gavelling)
    monkeypatch.setattr(client, "_setup_guild", lambda bot, config: guild)

    async def no_wait(_s):
        return None

    monkeypatch.setattr(client.asyncio, "sleep", no_wait)
    done = asyncio.run(client._catch_up_gavels(_bot(_message()), now=now))
    assert gavelled == [(901, FRANKLIN)] and done == ["901"]

    ordercards.save({"901": {"status": "made"}})
    gavelled.clear()
    asyncio.run(client._catch_up_gavels(_bot(_message()), now=now))
    assert gavelled == [], "a made card was gavelled again"


# ------------------------------------------------------------- whose gavel

def _asked(text, *, author=FRANKLIN, mentions=()):
    return SimpleNamespace(content=text, author=SimpleNamespace(id=author), mentions=list(mentions))


def test_franklin_lets_kharyl_gavel_by_tag_id_or_name(order, monkeypatch):
    bot = SimpleNamespace(config=CONFIG, user=SimpleNamespace(id=RYTE))
    kharyl = SimpleNamespace(id=KHARYL, display_name="Kharyl")
    asyncio.run(client._gavel_allow(bot, order.responder, _asked(f"<@{RYTE}> gavel allow <@{KHARYL}>",
                                                                  mentions=[SimpleNamespace(id=RYTE), kharyl]), allowed=True))
    assert ordercards.may_gavel(KHARYL, CONFIG) and "Kharyl's gavel makes cards now" in order.responder.sent[-1][0]
    asyncio.run(client._gavel_allow(bot, order.responder, _asked("gavel remove <@22>", mentions=[kharyl]), allowed=False))
    assert not ordercards.may_gavel(KHARYL, CONFIG) and ordercards.may_gavel(FRANKLIN, CONFIG)

    asyncio.run(client._gavel_allow(bot, order.responder, _asked("gavel allow 1478905541001613444"), allowed=True))
    assert ordercards.may_gavel(1478905541001613444, CONFIG)

    growth = SimpleNamespace(get_member_named=lambda name: None,
                             members=[SimpleNamespace(id=44, display_name="𝓜𝓪𝓻𝓲𝓼𝓼𝓪 M")])
    monkeypatch.setattr(client, "_setup_guild", lambda bot, config: growth)
    asyncio.run(client._gavel_allow(bot, order.responder, _asked(f"<@{RYTE}> gavel allow Marissa"), allowed=True))
    assert ordercards.may_gavel(44, CONFIG)


def test_only_someone_whose_gavel_counts_can_change_who_else(order):
    bot = SimpleNamespace(config=CONFIG, user=SimpleNamespace(id=RYTE))
    asyncio.run(client._gavel_allow(bot, order.responder, _asked("gavel allow <@22>", author=KHARYL,
                                                                  mentions=[SimpleNamespace(id=KHARYL)]), allowed=True))
    assert not ordercards.may_gavel(KHARYL, CONFIG) and "Only somebody" in order.responder.sent[-1][0]


@pytest.mark.parametrize("said, action", [
    ("<@1> gavel allow <@22>", "gavelallow"), ("<@1> gavel allow 1478905541001613444", "gavelallow"),
    ("<@1> let <@22> gavel", "gavelallow"), ("<@1> gavel remove <@22>", "gavelremove"),
    ("<@1> gavel check", "ordercheck"), ("<@1> let me know", "help"),
])
def test_gavel_commands(said, action):
    assert mentions.parse(said).action == action
