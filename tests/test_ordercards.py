"""Orders in Discord made into Trello cards when Franklin drops the gavel."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
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
        self.ticks, self.cleared, self.unticked = [], [], []
        self.desc = desc

    def board_lists(self, board_id):
        return [{"id": "q", "name": "In Que"}, {"id": "a", "name": "Aged Leads Order Done"}]

    def copy_card(self, list_id, source_id, *, name, desc, due=None, **kw):
        self.made.append(dict(list=list_id, source=source_id, name=name, desc=desc, due=due))
        return {"id": f"c{len(self.made)}", "shortUrl": f"https://trello.com/c/new{len(self.made)}"}

    def tick_card(self, card_id, *, done=True):
        self.ticks.append((card_id, done))

    def clear_due(self, card_id):
        self.cleared.append(card_id)

    def card_checklists(self, card_id):
        return [{"id": "cl", "name": "Setup", "checkItems": [
            {"id": "i1", "name": "Sheet made", "state": "complete"},
            {"id": "i2", "name": "Scheduled", "state": "incomplete"}]}]

    def set_check_item(self, card_id, item_id, *, done=None, name=None):
        self.unticked.append((card_id, item_id, done))

    def create_card(self, list_id, name, *, position="top"):
        self.made.append(dict(list=list_id, name=name))
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


def test_a_new_card_in_its_list_with_franklins_layout_and_no_due_date(monkeypatch):
    """"no need to put the duedate that way it wont be red ... also create a
    new card not copy ... Do this template"."""
    board = Board()
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)
    fields = {"name": "Gregory Stevens", "email": "gregory@example.com", "phone": "(555) 010-0027",
              "states": "TX, AL, CA", "package": "Text-Verified Trucker IUL (OTP)", "leads": "20",
              "launch": "Tuesday, October 13", "notes": ""}
    desc = jobs.order_desc(fields)
    assert desc == ("Name: Gregory Stevens\nEmail: gregory@example.com\nPhone Number: (555) 010-0027\n"
                    "States: TX, AL, CA\nLead Type/Package Selected: Text-Verified Trucker IUL (OTP)\n"
                    "Number of Leads Requested: 20\nLaunch Date: Tuesday, October 13\nAdditional Notes:")
    made = jobs.order_card_make(CONFIG, "fresh", {"name": "New Agent - Gregory Stevens", "desc": desc, "due": "2026-10-13"})
    assert made == {"id": "c1", "url": "https://trello.com/c/new1", "list": "In Que"}
    assert board.made == [dict(list="q", name="New Agent - Gregory Stevens")], "copied, not made new"
    assert board.descs == [desc]
    assert board.ticks == [] and board.cleared == [] and board.unticked == []


def test_every_label_is_there_even_when_the_order_doesnt_say():
    assert jobs.order_desc({"name": "Gregory Stevens"}).splitlines() == [
        "Name: Gregory Stevens", "Email:", "Phone Number:", "States:", "Lead Type/Package Selected:",
        "Number of Leads Requested:", "Launch Date:", "Additional Notes:"]


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

    def reading(config, *, text, images, today, notes=()):
        held.reads.append(text)
        held.notes = list(notes)
        return {"kind": held.kind, "client": "Cole Petersen", "name": f"{held.kind} - Cole Petersen",
                "desc": f"Name: Cole Petersen\nread {len(held.reads)}", "due": "2026-10-14",
                "missing": ["States"]}

    def making(config, kind, card):
        held.made.append((kind, card["name"], card["desc"], "new"))
        return {"id": "card1", "url": "https://trello.com/c/new1",
                "list": "Aged Leads Order Done" if kind == "aged" else "In Que"}

    class Confirm:
        def __init__(self, **kw):
            self.kw, self.confirmed = kw, False

        async def wait(self):
            self.confirmed = held.pressed

    monkeypatch.setattr(jobs, "read_order", reading)
    monkeypatch.setattr(jobs, "order_card_make", making)
    held.existing, held.comments = None, []
    monkeypatch.setattr(jobs, "recent_card_for", lambda config, name, now=None: held.existing)
    monkeypatch.setattr(jobs, "order_payment_comment", lambda config, card_id, text: held.comments.append((card_id, text)))
    monkeypatch.setattr(jobs, "order_template", lambda config, kind: {"id": f"TPL-{kind}", "name": "x", "desc": "", "url": "u"})
    monkeypatch.setattr(client, "_board_responder", lambda bot: held.responder)
    monkeypatch.setattr(client, "_today", lambda config: date(2026, 10, 13))
    monkeypatch.setattr(client.views, "ConfirmView", Confirm)
    # The aged machinery is kept, switched off - tested on.
    monkeypatch.setattr(client, "AGED_ORDERS_ON", True)
    return held


def _said(order):
    """What Ryte said, less the "Making the card…" that comes first."""
    return [one for one in order.responder.sent if not str(one[0] or "").startswith("🔨 Making the card")]


def _bot(message):
    return SimpleNamespace(config=CONFIG, get_channel=lambda i: message.channel, loop=asyncio.get_event_loop_policy())


GAVEL = SimpleNamespace(message_id=900, channel_id=700, user_id=FRANKLIN)


def test_aged_is_made_straight_away_and_said_in_ryte_the_goat(order):
    message = _message(thread=_notes("25 aged MTG, states AZ TX", "launch tomorrow"))
    asyncio.run(client._order_gavelled(_bot(message), GAVEL))
    assert order.made == [("aged", "aged - Cole Petersen", "Name: Cole Petersen\nread 1", "new")]
    [read] = order.reads
    assert "NAME: Cole Petersen" in read and "Kharyl: 25 aged MTG, states AZ TX" in read and "launch tomorrow" in read
    [(said, view)] = _said(order)
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
    assert order.made == [("fresh", "fresh - Cole Petersen", "Name: Cole Petersen\nread 1", "new")]
    [(said, view)] = _said(order)
    assert "New Agent card made" in said and "In Que" in said and view is None
    assert ordercards.load()["900"]["status"] == "made"


def test_with_the_button_back_fresh_waits_for_it_and_reads_the_notes_again(order, monkeypatch):
    monkeypatch.setattr(client, "FRESH_ASKS_FIRST", True)
    order.kind = "fresh"
    message = _message("New Payment | Payra\nNAME: Fabian Ubilla\nPRODUCT: VET")
    asyncio.run(client._order_gavelled(_bot(message), GAVEL))
    (asked, view), (done, _v) = _said(order)
    assert "New Agent card" in asked and "Queue" in asked and view.kw["requester_id"] == FRANKLIN
    assert len(order.reads) == 2, "the order wasn't read again after the button"
    assert order.made == [("fresh", "fresh - Cole Petersen", "Name: Cole Petersen\nread 2", "new")]
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


def test_no_example_card_is_needed(order, monkeypatch):
    def none(config, kind):
        raise AssertionError("an example card was read")

    monkeypatch.setattr(jobs, "order_template", none)
    order.kind = "fresh"
    asyncio.run(client._order_gavelled(_bot(_message()), GAVEL))
    assert [one[0] for one in order.made] == ["fresh"]


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
    [(said, _view)] = _said(order)
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
    assert "✅ Anyone's gavel counts in the Growth server" in said and f"✅ Elsewhere, from <@{FRANKLIN}>" in said
    assert "✅ Said in #trello-manager" in said
    assert "✅ Each card is new, no due date, described as: Name · Email · Phone Number" in said
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
    assert gavelled == [(901, FRANKLIN), (903, KHARYL)] and done == ["901", "903"], "everyone's gavel counts in Growth"

    ordercards.save({"901": {"status": "made"}, "903": {"status": "made"}})
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



def test_aged_orders_are_off_for_now_and_left_alone_said_so(order, monkeypatch):
    """"actually focus on new agent setup for now, not aged leads"."""
    monkeypatch.setattr(client, "AGED_ORDERS_ON", False)
    asyncio.run(client._order_gavelled(_bot(_message()), GAVEL))
    assert order.made == [] and "900" not in ordercards.load()
    [(said, _view)] = _said(order)
    assert "aged leads — those aren't made into cards for now" in said

    order.kind = "fresh"
    asyncio.run(client._order_gavelled(_bot(_message()), GAVEL))
    assert [one[0] for one in order.made] == ["fresh"]


def test_order_check_with_aged_off(order, monkeypatch):
    monkeypatch.setattr(client, "AGED_ORDERS_ON", False)
    monkeypatch.setattr(client, "_order_board_lists", lambda config: [{"id": "q", "name": "In Que"}])
    order.responder.channel = SimpleNamespace(name="trello-manager")
    asyncio.run(client._order_check(_bot(_message()), order.responder))
    said = order.responder.sent[-1][0]
    assert "⏸ Aged Leads orders: off for now" in said and "❌" not in said


def test_aged_orders_are_off_by_default():
    assert client.AGED_ORDERS_ON is False


def test_taking_the_gavel_off_and_putting_it_back_makes_a_fresh_card(order):
    """"can i try again, i removed the emoji and will it do again?"."""
    order.kind = "fresh"
    message = _message()
    asyncio.run(client._order_gavelled(_bot(message), GAVEL))
    asyncio.run(client._order_gavelled(_bot(message), GAVEL))
    assert len(order.made) == 1
    asyncio.run(client._order_ungavelled(_bot(message), GAVEL))
    assert "900" not in ordercards.load()
    assert "put it back and I'll make a fresh card" in order.responder.sent[-1][0]
    assert "https://trello.com/c/new1" in order.responder.sent[-1][0]
    asyncio.run(client._order_gavelled(_bot(message), GAVEL))
    assert len(order.made) == 2


def test_taking_off_a_gavel_that_made_nothing_says_nothing(order):
    before = len(order.responder.sent)
    asyncio.run(client._order_ungavelled(_bot(_message()), GAVEL))
    assert len(order.responder.sent) == before



# ------------------------------------------------------------- Corban Tidwell

def test_an_ad_spend_package_has_no_cap_and_nothing_says_unknown():
    """"it doesnt have nymber of leads because its ads spend - that means no cap"."""
    got = {"name": "Corban Tidwell", "email": "corban@example.com", "phone": "3605550153",
           "states": "all states except NY and FL", "package": "Phx Standard 350", "leads": "<UNKNOWN>",
           "launch": "Friday, October 16", "notes": "UPRISE. Contact info updated"}
    fields = jobs.order_fields(got)
    assert fields["leads"] == "No cap (ad spend)"
    assert jobs.order_fields({**got, "package": "Text-Verified Trucker IUL", "leads": "unknown"})["leads"] == ""
    assert jobs.order_fields({**got, "package": "Phx Standard 350", "leads": "350"})["leads"] == "350"
    assert jobs.order_fields({**got, "email": "N/A"})["email"] == ""


def test_additional_notes_are_the_teams_own_words_from_the_thread():
    """"the thread message replied from the team should be added on the additional notes"."""
    said = "Phx Standard 350 all states except NY and FL\n\ncorban.tidwell@example.com\n3605550153\n\nUPRISE, LIVE DATE FRI OCT 16"
    fields = jobs.order_fields({"name": "Corban Tidwell", "package": "Phx Standard 350",
                                "notes": "UPRISE. Contact info updated in notes"}, [said])
    assert fields["notes"] == said
    desc = jobs.order_desc(fields)
    assert desc.endswith("Additional Notes:\n\n\u00a0\n\n\u00a0\n\n" + said), "no gap before the team's notes"
    assert "Number of Leads Requested: No cap (ad spend)" in desc
    assert jobs.order_fields({"notes": "Same setup as last time"})["notes"] == "Same setup as last time"


def test_the_teams_notes_are_gathered_from_the_thread_and_not_the_bots(order):
    message = _message(thread=_notes("Phx Standard 350 all states except NY and FL", "UPRISE, LIVE DATE FRI OCT 16"))
    text, images, thread_id, notes = asyncio.run(client._order_said(_bot(message), message))
    assert notes == ["Phx Standard 350 all states except NY and FL", "UPRISE, LIVE DATE FRI OCT 16"]
    assert "New Payment | Payra" in text and thread_id == 900
    asyncio.run(client._order_gavelled(_bot(message), GAVEL))
    assert order.notes == notes



def test_a_tag_in_the_teams_notes_is_a_name_on_the_card():
    """"@757873984934117396 >" on Killian Cooper's card."""
    message = SimpleNamespace(mentions=[SimpleNamespace(id=757873984934117396, display_name="Nicole")],
                              role_mentions=[SimpleNamespace(id=5, name="Client Advisors")], channel_mentions=[])
    assert client._named_tags(message, "LIVE TODAY OCT 13\n<@757873984934117396> <@&5> <@!9>") == \
        "LIVE TODAY OCT 13\n@Nicole @Client Advisors @someone"


def test_no_notes_no_gap():
    assert jobs.order_desc({"name": "Killian Cooper"}).endswith("Additional Notes:")


# ------------------------------------------------------------- screenshots

def test_a_screenshot_in_the_thread_is_written_out_in_the_notes():
    """"they sometimes add screenshot as additional notes, can ryte read that"."""
    notes = ["Fresh Vets- 20x leads", "[screenshot 1]", "LIVE TODAY OCT 13\n[screenshot 2]"]
    got = {"screenshots": ["Dean Nevarez · States: CA, AZ, TX · 25 OTP VET · Launch Monday Oct 12", ""]}
    fields = jobs.order_fields(got, notes)
    assert fields["notes"] == ("Fresh Vets- 20x leads\n"
                               "📷 Dean Nevarez · States: CA, AZ, TX · 25 OTP VET · Launch Monday Oct 12\n"
                               "LIVE TODAY OCT 13\n📷 (screenshot)")
    assert jobs.order_fields({}, ["[screenshot 1]"])["notes"] == "📷 (screenshot)", "unread, but not dropped"


def test_the_teams_screenshots_are_marked_and_shown_first(order):
    picture = SimpleNamespace(url="https://cdn.discordapp.com/shot.png", content_type="image/png")

    class Thread:
        id = 900
        parent = SimpleNamespace(name="new-payment")

        def history(self, **kw):
            async def gen():
                for at, (text, files) in enumerate([("Killian Cooper", []), ("", [picture])]):
                    yield SimpleNamespace(id=at, content=text, embeds=[], attachments=files, message_snapshots=[],
                                          mentions=[], role_mentions=[], channel_mentions=[],
                                          author=SimpleNamespace(display_name="Kharyl", name="kharyl", bot=False),
                                          created_at=WHEN)
            return gen()

    message = _message(thread=Thread())
    _text, images, _thread, notes = asyncio.run(client._order_said(_bot(message), message))
    assert notes == ["Killian Cooper", "[screenshot 1]"]
    assert images[0] == "https://cdn.discordapp.com/shot.png"


def test_the_screenshots_are_uploaded_onto_the_card_once_each(monkeypatch):
    """"add the text and the image as well"."""
    import httpx

    uploads = []

    class Trello:
        def attach_file(self, card_id, name, data, mime="image/png"):
            uploads.append((card_id, name, data, mime))

        def close(self):
            pass

    monkeypatch.setattr(jobs, "open_trello", lambda cfg: Trello())
    monkeypatch.setattr(httpx, "get", lambda url, **kw: SimpleNamespace(
        content=b"PNG" + url.encode(), headers={"content-type": "image/png"}, raise_for_status=lambda: None))
    shots = ["https://cdn.discordapp.com/a/1/shot.png?ex=111", "https://cdn.discordapp.com/a/2/notes.jpg?ex=222"]
    done = jobs.order_card_attach(CONFIG, "c1", shots)
    assert done == ["https://cdn.discordapp.com/a/1/shot.png", "https://cdn.discordapp.com/a/2/notes.jpg"]
    assert [(card, name) for card, name, _d, _m in uploads] == [("c1", "shot.png"), ("c1", "notes.jpg")]
    # Discord's link changes; the picture doesn't - not uploaded twice.
    again = jobs.order_card_attach(CONFIG, "c1", ["https://cdn.discordapp.com/a/1/shot.png?ex=999"], done=tuple(done))
    assert again == [] and len(uploads) == 2


def test_a_gavelled_order_with_a_screenshot_gets_it_on_the_card(order, monkeypatch):
    attached = []

    def attaching(config, card_id, urls, done=()):
        attached.append((card_id, list(urls)))
        return [jobs.shot_key(one) for one in urls]

    monkeypatch.setattr(jobs, "order_card_attach", attaching)
    picture = SimpleNamespace(url="https://cdn.discordapp.com/a/1/shot.png?ex=1", content_type="image/png")

    class Thread:
        id = 900
        parent = SimpleNamespace(name="new-payment")

        def history(self, **kw):
            async def gen():
                yield SimpleNamespace(id=1, content="", embeds=[], attachments=[picture], message_snapshots=[],
                                      mentions=[], role_mentions=[], channel_mentions=[],
                                      author=SimpleNamespace(display_name="Kharyl", name="kharyl", bot=False),
                                      created_at=WHEN)
            return gen()

    order.kind = "fresh"
    asyncio.run(client._order_gavelled(_bot(_message(thread=Thread())), GAVEL))
    assert attached == [("card1", ["https://cdn.discordapp.com/a/1/shot.png?ex=1"])]
    assert ordercards.load()["900"]["attached"] == ["https://cdn.discordapp.com/a/1/shot.png"]


# ------------------------------------------------------------- after the card

@pytest.mark.parametrize("said, chatter", [
    ("setup done", True), ("@Kharyl @Nicole setup done", True), ("signed @Nicole", True), ("ok thanks", True),
    ("FEARLESS", True),
    ("Monday-Friday: 8:00am - 10:00pm EST\n\nSaturday 8am-8pm EST\n\nSunday 1:00pm - 5:00pm EST", False),
    ("[screenshot 2]", False), ("add TX and FL to his states please", False),
])
def test_status_chatter_after_the_card_is_kept_off_it(said, chatter):
    assert ordercards.is_chatter(said) is chatter


def test_a_capital_letter_isnt_a_new_line():
    """"Email: killian32cooper@gmail.com" commented as new on Killian Cooper's card."""
    assert ordercards.changed_lines("Email: Killian32cooper@gmail.com", "Email: killian32cooper@gmail.com") == []


def test_notes_after_the_card_drop_the_chatter_and_keep_the_schedule(order):
    late = datetime(2026, 10, 13, 23, 30, tzinfo=timezone.utc)

    class Thread:
        id = 900
        parent = SimpleNamespace(name="new-payment")

        def history(self, **kw):
            async def gen():
                for at, (text, when) in enumerate([("FEARLESS\nLIVE TODAY OCT 13", WHEN), ("setup done", late),
                                                   ("signed", late), ("Monday-Friday: 8:00am - 10:00pm EST", late)]):
                    yield SimpleNamespace(id=at, content=text, embeds=[], attachments=[], message_snapshots=[],
                                          mentions=[], role_mentions=[], channel_mentions=[],
                                          author=SimpleNamespace(display_name="Kharyl", name="kharyl", bot=False),
                                          created_at=when)
            return gen()

    message = _message(thread=Thread())
    made = datetime(2026, 10, 13, 23, 0, tzinfo=timezone.utc)
    _t, _i, _th, notes = asyncio.run(client._order_said(_bot(message), message, since=made))
    assert notes == ["FEARLESS\nLIVE TODAY OCT 13", "Monday-Friday: 8:00am - 10:00pm EST"]
    _t, _i, _th, everything = asyncio.run(client._order_said(_bot(message), message))
    assert len(everything) == 4, "before the card, every note is the order"



# ------------------------------------------------------------- quick

def test_orders_are_read_with_the_fast_model_and_the_main_one_if_that_fails(monkeypatch):
    """"it should not take too long for it to add and create the card"."""
    import sys
    import types

    asked = []

    class Messages:
        def create(self, *, model, **kw):
            asked.append(model)
            if model == jobs.ORDER_MODEL and len(asked) == 1 and fail:
                raise RuntimeError("model not available")
            return "reply"

    class Anthropic:
        def __init__(self, **kw):
            self.messages = Messages()

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=Anthropic))
    monkeypatch.setattr(jobs, "_tool_input", lambda response, name: {"kind": "fresh", "name": "Bram Gutierrez"})
    config = SimpleNamespace(secrets=SimpleNamespace(anthropic_api_key="k", require=lambda *a: None),
                             copy=SimpleNamespace(model="claude-opus-5-5"))
    fail = False
    card = jobs.read_order(config, text="NAME: Bram Gutierrez", images=[], today=date(2026, 10, 13))
    assert asked == [jobs.ORDER_MODEL] and card["name"] == "New Agent - Bram Gutierrez"
    asked.clear()
    fail = True
    jobs.read_order(config, text="NAME: Bram Gutierrez", images=[], today=date(2026, 10, 13))
    assert asked == [jobs.ORDER_MODEL, "claude-opus-5-5"]


def test_the_gavel_is_answered_at_once_and_that_message_becomes_the_card(order):
    edits = []

    class Sent:
        async def edit(self, content=None, **kw):
            edits.append(content)

    async def send(text=None, **kw):
        order.responder.sent.append((text, kw.get("view")))
        return Sent()

    order.responder.send = send
    order.kind = "fresh"
    asyncio.run(client._order_gavelled(_bot(_message()), GAVEL))
    [(first, _v)] = order.responder.sent
    assert first.startswith("🔨 Making the card for this order…")
    assert len(edits) == 1 and "New Agent card made" in edits[0] and "https://trello.com/c/new1" in edits[0]


# ------------------------------------------------------------- backfill and the live list

def _payment(i, name):
    message = _message(f"New Payment | Payra\nNAME: {name}\nPRODUCT: VET")
    message.id, message.jump_url = i, f"https://discord.com/channels/1/700/{i}"
    return message


def test_backfill_makes_only_whats_missing_on_one_button(order, monkeypatch):
    """"not all agent was added as card today? can we backfill it"."""
    posts = [_payment(1, "Bram Gutierrez"), _payment(2, "Bram Gutierrez"), _payment(3, "Killian Cooper"),
             _payment(4, "Gregory Stevens"), _payment(5, "Ayden Escarrega")]
    ordercards.save({"4": {"status": "made"}})
    names = {1: "Bram Gutierrez", 2: "Bram Gutierrez", 3: "Killian Cooper", 5: "Ayden Escarrega"}

    async def reading(bot, message, **kw):
        who = names[message.id]
        kind = "aged" if who == "Ayden Escarrega" else "fresh"
        return ({"kind": kind, "client": who, "name": f"New Agent - {who}", "desc": "d", "shots": [],
                 "fields": {"package": "VET", "leads": "20", "launch": "Tuesday, October 13"},
                 "launch_iso": "2026-10-13"}, {}, None)

    async def payments(bot, day):
        return posts

    monkeypatch.setattr(client, "_order_read", reading)
    monkeypatch.setattr(client, "_payments_on", payments)
    monkeypatch.setattr(jobs, "new_agent_cards", lambda config: [
        {"name": "NEW AGENT - Killian Cooper", "url": "https://trello.com/c/kc"}])
    asyncio.run(client._order_backfill(_bot(posts[0]), order.responder, SimpleNamespace(id=FRANKLIN)))
    listed = [text for text, view in order.responder.sent if view is not None][0]
    assert "1 new agent(s) paid today with no card" in listed and "Bram Gutierrez" in listed
    assert "Killian Cooper — already has a card: https://trello.com/c/kc" in listed
    assert "Bram Gutierrez — a second payment today; one card" in listed
    assert "Ayden" not in listed and "Gregory" not in listed
    assert [one[1] for one in order.made] == ["New Agent - Bram Gutierrez"]
    assert ordercards.load()["1"]["status"] == "made" and ordercards.load()["1"]["launch_iso"] == "2026-10-13"


def test_backfill_is_franklins(order):
    asyncio.run(client._order_backfill(_bot(_message()), order.responder, SimpleNamespace(id=KHARYL)))
    assert "Only somebody whose gavel counts" in order.responder.sent[-1][0]


def test_the_live_list_today_and_tomorrow_with_lead_type_and_states(order, monkeypatch):
    """"give me the list of name going live today and tomorrow with their lead type and states"."""
    from wilbyte import hub

    today = date(2026, 10, 13)
    agents = [hub.HubAgent(id="a", name="Killian Cooper", lead_type="VET", start_date=today,
                           states=("AL", "AZ", "GA")),
              hub.HubAgent(id="b", name="Gregory Stevens", lead_type="TRUCKER_IUL", start_date=today),
              hub.HubAgent(id="c", name="Wesley Zurschmit", lead_type="TRUCKER_IUL", start_date=date(2026, 10, 14)),
              hub.HubAgent(id="d", name="Old Agent", lead_type="FEX", start_date=date(2026, 10, 1))]
    monkeypatch.setattr(hub, "agents", lambda secrets, status="fulfilled": agents)
    ordercards.save({
        "1": {"status": "made", "client": "Gregory Stevens", "fields": {"states": "TX, AL, CA"}, "launch_iso": "2026-10-13"},
        "2": {"status": "made", "client": "Corban Tidwell", "url": "https://trello.com/c/ct",
              "fields": {"states": "all states except NY and FL", "package": "Phx Standard 350"}, "launch_iso": "2026-10-14"},
    })
    bot = SimpleNamespace(config=SimpleNamespace(secrets=SimpleNamespace(hub_api_token="t"),
                                                 schedule=SimpleNamespace(timezone="America/New_York")))
    asyncio.run(client._live_list(bot, order.responder))
    said = order.responder.sent[-1][0]
    """"add their date or atleast separate them for those going live today and tomorrow"."""
    assert "__**TODAY — Tuesday, October 13**__ (2)" in said and "__**TOMORROW — Wednesday, October 14**__ (2)" in said
    assert said.index("TODAY") < said.index("Killian") < said.index("TOMORROW") < said.index("Wesley")
    assert "**Killian Cooper** — Tue 10/13 · VET · AL, AZ, GA" in said
    assert "**Gregory Stevens** — Tue 10/13 · TRUCKER_IUL · TX, AL, CA" in said, "states from the card when the hub has none"
    assert "**Wesley Zurschmit** — Wed 10/14 · TRUCKER_IUL · states not given" in said
    assert "⚠ **Corban Tidwell** — Wed 10/14 · Phx Standard 350 · all states except NY and FL · *not on the hub yet*" in said
    assert "Old Agent" not in said


@pytest.mark.parametrize("said, action", [
    ("<@1> backfill orders", "orderbackfill"), ("<@1> backfill", "backfill"),
    ("<@1> going live today and tomorrow", "livelist"), ("<@1> live list", "livelist"),
    ("<@1> who goes live tomorrow", "golive"),
])
def test_backfill_and_live_list_commands(said, action):
    assert mentions.parse(said).action == action


# ------------------------------------------------------------- everyone's gavel

def test_everyones_gavel_counts_in_growth_but_not_elsewhere_and_never_a_bots(monkeypatch):
    """"let everyone's gavel count, not just mine"."""
    growth, clients = SimpleNamespace(id=2), SimpleNamespace(id=3)
    monkeypatch.setattr(client, "_in_setup_guild", lambda bot, guild: guild is growth)
    bot = SimpleNamespace(config=CONFIG, get_guild=lambda i: {2: growth, 3: clients}.get(i))

    def gavel(user, guild_id, *, is_bot=False):
        return SimpleNamespace(user_id=user, guild_id=guild_id, member=SimpleNamespace(bot=is_bot))

    assert client._gavel_counts(bot, gavel(KHARYL, 2)), "Kharyl's gavel in Growth"
    assert not client._gavel_counts(bot, gavel(KHARYL, 3)), "a gavel in the clients' server"
    assert client._gavel_counts(bot, gavel(FRANKLIN, 3)), "Franklin's counts anywhere"
    assert not client._gavel_counts(bot, gavel(77, 2, is_bot=True)), "a bot's"
    monkeypatch.setattr(client, "GAVEL_EVERYONE", False)
    assert not client._gavel_counts(bot, gavel(KHARYL, 2))



# ------------------------------------------------------------- paid more than once

def test_another_payment_for_this_weeks_order_goes_on_its_card(order):
    """"bram paid alot of time for the same order"."""
    order.kind, order.pressed = "fresh", False
    order.existing = {"id": "5f0000000000000000000001", "name": "New Agent - Bram Gutierrez",
                      "url": "https://trello.com/c/bram"}
    message = _message("New Payment | Payra\nNAME: Bram Gutierrez\nAMOUNT: $372.6\nPRODUCT: Text-Verified Trucker IUL")
    asyncio.run(client._order_gavelled(_bot(message), GAVEL))
    assert order.made == [], "a second card for the same order"
    [(card_id, text)] = order.comments
    assert card_id == "5f0000000000000000000001"
    assert text.startswith("💰 Another payment for this order: $372.6 · Text-Verified Trucker IUL")
    said = [one for one in _said(order) if one[1] is not None][0][0]
    assert "already has a card from this week" in said and "https://trello.com/c/bram" in said
    assert ordercards.load()["900"]["merged"] is True


def test_a_separate_card_when_its_a_new_order_after_all(order):
    order.kind, order.pressed = "fresh", True
    order.existing = {"id": "5f0000000000000000000001", "name": "New Agent - Bram Gutierrez",
                      "url": "https://trello.com/c/bram"}
    asyncio.run(client._order_gavelled(_bot(_message()), GAVEL))
    assert len(order.comments) == 1 and [one[0] for one in order.made] == ["fresh"]
    assert ordercards.load()["900"].get("merged") is None and ordercards.load()["900"]["card_id"] == "card1"


def test_only_this_weeks_card_counts(monkeypatch):
    now = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)

    def made(days_ago):
        return format(int((now - timedelta(days=days_ago)).timestamp()), "x") + "0" * 16

    monkeypatch.setattr(jobs, "new_agent_cards", lambda config: [
        {"id": made(30), "name": "New Agent - Bram Gutierrez", "url": "old"},
        {"id": made(1), "name": "New Agent - Killian Cooper", "url": "new"}])
    assert jobs.recent_card_for(CONFIG, "Bram Gutierrez", now=now) is None, "last month's order is its own"
    assert jobs.recent_card_for(CONFIG, "Killian Cooper", now=now)["url"] == "new"
