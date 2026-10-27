"""Orders in Discord made into Trello cards when Franklin drops the gavel."""

from __future__ import annotations

import asyncio
import re
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
        self.positions = getattr(self, "positions", []) + [position]
        self.name = name
        return {"id": f"c{len(self.made)}", "shortUrl": f"https://trello.com/c/new{len(self.made)}"}

    def card_full(self, card_id):
        return {"id": f"id-{card_id}", "name": "New Agent - Jane Doe 25 VET", "desc": "Name:\nStates:", "shortUrl": card_id}

    def card_detail(self, card_id):
        return {"desc": self.desc, "name": getattr(self, "name", "x")}

    def set_description(self, card_id, text):
        self.descs.append(text)
        self.desc = text

    def rename_card(self, card_id, name):
        self.names.append(name)
        self.name = name

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
                    "Number of Leads Requested: 20\nLaunch Date: Tuesday, October 13\nAgency:\nAdditional Notes:")
    made = jobs.order_card_make(CONFIG, "fresh", {"name": "New Agent - Gregory Stevens", "desc": desc, "due": "2026-10-13"})
    assert made == {"id": "c1", "url": "https://trello.com/c/new1", "list": "In Que"}
    assert board.made == [dict(list="q", name="New Agent - Gregory Stevens")], "copied, not made new"
    assert board.descs == [desc]
    assert board.ticks == [] and board.cleared == [] and board.unticked == []


def test_a_made_card_is_read_back_and_put_right(monkeypatch):
    """"he cant fail this card creation": the description that didn't stick
    put on again, and the card said as made - with a warning - when it
    still won't, never "couldn't make the card" beside a blank one."""
    monkeypatch.setattr(jobs, "DESC_PAUSES", (0, 0))

    class Lossy(Board):
        def set_description(self, card_id, text):
            self.descs.append(text)
            self.desc = "" if len(self.descs) == 1 else text

    board = Lossy()
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)
    made = jobs.order_card_make(CONFIG, "fresh", {"name": "New Agent - Ann Able", "desc": "Name: Ann Able\nStates: TX"})
    assert "trouble" not in made and len(board.descs) == 2, "read back, found blank, put on again"

    class Refusing(Board):
        def set_description(self, card_id, text):
            raise RuntimeError("HTTP 503")

    board = Refusing()
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)
    made = jobs.order_card_make(CONFIG, "fresh", {"name": "New Agent - Ann Able", "desc": "Name: Ann Able"})
    assert made["id"] == "c1" and "description didn't go on" in made["trouble"][0]


def test_every_label_is_there_even_when_the_order_doesnt_say():
    assert jobs.order_desc({"name": "Gregory Stevens"}).splitlines() == [
        "Name: Gregory Stevens", "Email:", "Phone Number:", "States:", "Lead Type/Package Selected:",
        "Number of Leads Requested:", "Launch Date:", "Agency:", "Additional Notes:"]


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
        # What an aged card can't be made without, there to be read - unless
        # a test takes it away.
        product = re.search(r"PRODUCT:\s*([^\n]*)", text)
        fields = dict({"states": "AZ, TX", "leads": "25",
                       "package": product.group(1) if product and not product.group(1).startswith("—") else "Aged MTG"}
                      if held.kind == "aged" else {}, **held.fields)
        read = {"kind": held.kind, "client": "Cole Petersen", "name": f"{held.kind} - Cole Petersen",
                "desc": f"Name: Cole Petersen\nread {len(held.reads)}", "due": "2026-10-14",
                "missing": ["States"]}
        if fields:
            read.update(fields=fields, source=text + "\nAZ TX 25")
        return read

    held.fields = {}

    def making(config, kind, card, list_name=""):
        held.made.append((kind, card["name"], card["desc"], "new"))
        held.lists.append(list_name)
        return {"id": "card1", "url": "https://trello.com/c/new1",
                "list": list_name or ("Aged Leads Order Done" if kind == "aged" else "In Que")}

    held.lists, held.paid, held.bank_comments = [], (None, False), []

    async def paying(bot, message):
        return held.paid

    monkeypatch.setattr(client, "_order_payment", paying)
    monkeypatch.setattr(jobs, "order_bank_comment",
                        lambda config, card_id, text: held.bank_comments.append((card_id, text)))

    class Confirm:
        def __init__(self, **kw):
            self.kw, self.confirmed = kw, False

        async def wait(self):
            self.confirmed = held.pressed

    monkeypatch.setattr(jobs, "read_order", reading)
    monkeypatch.setattr(jobs, "order_card_make", making)
    held.existing, held.comments = None, []
    monkeypatch.setattr(jobs, "recent_card_for", lambda config, name, now=None, kind="fresh": held.existing)
    held.same = True
    monkeypatch.setattr(jobs, "same_order", lambda config, existing, card: held.same)
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
                               channel=SimpleNamespace(id=1, name="Cole Petersen · $51.75",
                                                       parent=SimpleNamespace(name="💸 | new-payment")))

    monkeypatch.setattr(client, "is_direct_mention", lambda message, user: True)
    # In a payment's thread: pointing Ryte at the order.
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
    def broken(config, kind, card, list_name=""):
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


def test_a_break_after_the_card_is_made_keeps_it_made(order, monkeypatch):
    """Something breaking once the card is on the board said "Nothing was
    made" and forgot it - and trying again made a second card."""
    async def broken_mark(*a, **k):
        raise RuntimeError("Discord hiccup")

    monkeypatch.setattr(client, "_order_mark", broken_mark)
    asyncio.run(client._order_gavelled(_bot(_message()), GAVEL))
    said = order.responder.sent[-1][0]
    assert "The card was made, then something broke" in said and "Nothing was made" not in said
    assert ordercards.load()["900"]["status"] == "made"
    assert client._ORDER_JUST_MADE == {}


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


def test_aged_orders_are_on():
    """"i want ryte to do the same thing for the aged leads now"."""
    assert client.AGED_ORDERS_ON is True


def test_taking_the_gavel_off_and_putting_it_back_makes_a_fresh_card(order):
    """"can i try again, i removed the emoji and will it do again?"."""
    order.kind = "fresh"
    message = _message()
    asyncio.run(client._order_gavelled(_bot(message), GAVEL))
    asyncio.run(client._order_gavelled(_bot(message), GAVEL))
    assert len(order.made) == 1
    asyncio.run(client._order_ungavelled(_bot(message), GAVEL))
    assert ordercards.load()["900"]["status"] == "off"
    assert "update the card I made" in order.responder.sent[-1][0]
    assert "https://trello.com/c/new1" in order.responder.sent[-1][0]
    # "why did you create two card for diego": put back, the same card is
    # brought up to date - not a second one.
    open_cards = {"card1"}
    refreshed = []

    async def refresh(bot, key, settle=None):
        refreshed.append(key)
        return "updated"

    orig = client._order_refresh
    client._order_refresh = refresh
    jobs_open = jobs.order_card_open
    jobs.order_card_open = lambda config, card_id: card_id in open_cards
    try:
        asyncio.run(client._order_gavelled(_bot(message), GAVEL))
        assert len(order.made) == 1 and refreshed == ["900"]
        assert ordercards.load()["900"]["status"] == "made"
        assert "brought it up to date instead of making another" in order.responder.sent[-1][0]
        # The card deleted, then the gavel off and on again: a fresh one.
        asyncio.run(client._order_ungavelled(_bot(message), GAVEL))
        open_cards.clear()
        asyncio.run(client._order_gavelled(_bot(message), GAVEL))
        assert len(order.made) == 2
    finally:
        client._order_refresh = orig
        jobs.order_card_open = jobs_open


def test_a_gavel_put_back_on_an_order_put_with_another_card_makes_its_own(order, monkeypatch):
    """Abdullah Bahrani's aged payment had been put with his New Agent card;
    the gavel put back wrote the aged order into that card's comments."""
    order.kind = "fresh"
    message = _message()
    ordercards.save({"900": {"status": "made", "merged": True, "card_id": "fresh1", "client": "Abdullah Bahrani",
                             "url": "https://trello.com/c/FRESH"}})
    monkeypatch.setattr(client, "_order_refresh", lambda *a, **kw: pytest.fail("refreshed the other card"))
    monkeypatch.setattr(jobs, "order_card_open", lambda config, card_id: True)

    asyncio.run(client._order_ungavelled(_bot(message), GAVEL))
    assert "read the order afresh and make its own card" in order.responder.sent[-1][0]
    asyncio.run(client._order_gavelled(_bot(message), GAVEL))
    assert len(order.made) == 1


def test_a_gavel_on_an_aged_order_put_with_a_new_agent_card_makes_its_card(order, monkeypatch):
    """"nothing" - Ryte's own gavel came off Abdullah Bahrani's payment, which
    Ryte doesn't hear, and the gavel put back was taken as the order done."""
    order.kind = "aged"
    message = _message()
    ordercards.save({"900": {"status": "made", "merged": True, "kind": "aged", "card_id": "fresh1",
                             "name": "New Agent - Abdullah Bahrani", "client": "Abdullah Bahrani",
                             "url": "https://trello.com/c/FRESH"}})
    asyncio.run(client._order_gavelled(_bot(message), GAVEL))
    assert len(order.made) == 1


def test_a_gavel_on_an_order_with_its_card_says_where_it_is(order):
    message = _message()
    ordercards.save({"900": {"status": "made", "kind": "fresh", "card_id": "c1", "name": "New Agent - Cole Petersen",
                             "client": "Cole Petersen", "url": "https://trello.com/c/C1"}})
    asyncio.run(client._order_gavelled(_bot(message), GAVEL))
    assert order.made == []
    assert "already has its card: https://trello.com/c/C1" in order.responder.sent[-1][0]


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
    assert desc.endswith("Additional Notes:\n\n\u00a0\n\n" + said), "no gap before the team's notes"
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
    # "why was the thread/note not added?" - Lisasi Dieumerci.
    ("@Nicole @Faith i sent this one via sms", False), ("leads sent", True),
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
                 "fields": {"package": "VET", "leads": "20", "launch": "Tuesday, October 13", "states": "TX, FL"},
                 "source": f"NAME: {who}\nPRODUCT: VET\n20 vets TX, FL live today", "launch_iso": "2026-10-13"},
                {}, None)

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
    # His first payment, moments before this one, made the card.
    ordercards.save({"800": {"status": "made", "card_id": "5f0000000000000000000001"}})
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
    ordercards.save({"800": {"status": "made", "card_id": "5f0000000000000000000001"}})
    asyncio.run(client._order_gavelled(_bot(_message()), GAVEL))
    assert len(order.comments) == 1 and [one[0] for one in order.made] == ["fresh"]
    assert ordercards.load()["900"].get("merged") is None and ordercards.load()["900"]["card_id"] == "card1"


def test_only_this_weeks_card_counts(monkeypatch):
    now = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)

    def made(days_ago):
        return format(int((now - timedelta(days=days_ago)).timestamp()), "x") + "0" * 16

    monkeypatch.setattr(jobs, "new_agent_cards", lambda config, kind="fresh": [
        {"id": made(30), "name": "New Agent - Bram Gutierrez", "url": "old"},
        {"id": made(1), "name": "New Agent - Killian Cooper", "url": "new"}])
    assert jobs.recent_card_for(CONFIG, "Bram Gutierrez", now=now) is None, "last month's order is its own"
    assert jobs.recent_card_for(CONFIG, "Killian Cooper", now=now)["url"] == "new"


# ------------------------------------------------------------- onto the Ads card

def test_the_cards_layout_reads_like_the_old_one_for_the_ads_and_ops_checklists():
    """"bram's trucker card but was not added on the ads? nicole's sides"."""
    from wilbyte import agents

    fields = {"name": "Bram Gutierrez", "email": "a@example.com", "phone": "+15550100000", "states": "TX, FL",
              "package": "Text-Verified Trucker IUL", "leads": "25", "launch": "Wednesday, October 14", "notes": ""}
    desc = jobs.order_desc(fields)
    agent = agents.read_agent({"id": "5f0000000000000000000001", "name": "New Agent - Bram Gutierrez", "desc": desc,
                               "shortUrl": "u"}, text=desc, today=date(2026, 10, 14))
    assert agent.lead_type == "Text-Verified Trucker IUL" and agent.stated == "Text-Verified Trucker IUL"
    assert agent.launch == date(2026, 10, 14)
    assert not agents.cannot_read(agent, needs_lead_type=True)


def test_no_launch_date_is_said_when_the_card_is_made(order, monkeypatch):
    def reading(config, *, text, images, today, notes=()):
        return {"kind": "fresh", "client": "Bram Gutierrez", "name": "New Agent - Bram Gutierrez", "desc": "d",
                "fields": {"launch": "", "package": "VET"}, "missing": ["Launch Date"], "launch_iso": ""}

    monkeypatch.setattr(jobs, "read_order", reading)
    asyncio.run(client._order_gavelled(_bot(_message()), GAVEL))
    said = _said(order)[-1][0]
    assert "No launch date in the order" in said and "Ads and Ops checklists" in said



def test_tags_are_left_out_of_the_teams_notes():
    """"@franklin 🧑| General Manager" at the end of Samuel Riggins' notes."""
    said = ("First Name: Samuel\nCan I start my PHX + 2.0 at $1050\n\nUPRISE LIVE FRI OCT 16\n\n<@822523319986290719>\n"
            "lead sched <@757873984934117396>\n@here")
    assert client._note_without_tags(said) == ("First Name: Samuel\nCan I start my PHX + 2.0 at $1050\n\nUPRISE LIVE FRI OCT 16\n\n"
                                       "lead sched")



# ------------------------------------------------------------- aged leads

def test_an_aged_card_in_franklins_aged_layout():
    """"Aged Leads - Name of Agent / Name / Email / Phone Number / Lead Type /
    Number of Leads Requested / States / Agency / Additional Notes"."""
    fields = jobs.order_fields({"name": "Ayden Escarrega", "email": "ayden@example.com", "phone": "+15550104032",
                                "package": "Aged Leads (MTG)", "leads": "50", "states": "AZ", "agency": "UPRISE",
                                "launch": "Tuesday, October 13"}, ["50 aged MTG, AZ only"])
    assert jobs.order_desc(fields, "aged") == (
        "Name: Ayden Escarrega\nEmail: ayden@example.com\nPhone Number: +15550104032\nLead Type: Aged Leads (MTG)\n"
        "Number of Leads Requested: 50\nStates: AZ\nAgency: UPRISE\nAdditional Notes:\n\n\u00a0\n\n50 aged MTG, AZ only")
    assert "Agency: UPRISE\nAdditional Notes" in jobs.order_desc(fields, "fresh") and "Launch Date: Tuesday, October 13" in jobs.order_desc(fields)


def test_an_aged_order_makes_its_card_in_aged_leads_order_done(order):
    order.kind = "aged"
    asyncio.run(client._order_gavelled(_bot(_message()), GAVEL))
    assert [one[0] for one in order.made] == ["aged"]
    assert "Aged Leads card made" in _said(order)[-1][0] and "No launch date" not in _said(order)[-1][0]


def test_a_second_aged_payment_the_same_day_goes_on_the_card_but_not_the_next(monkeypatch):
    now = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)

    def made(hours_ago):
        return format(int((now - timedelta(hours=hours_ago)).timestamp()), "x") + "0" * 16

    monkeypatch.setattr(jobs, "new_agent_cards", lambda config, kind="fresh": [
        {"id": made(3), "name": "Aged Leads - Ayden Escarrega", "url": "today"},
        {"id": made(30), "name": "Aged Leads - Raymond Vargas", "url": "yesterday"}] if kind == "aged" else [])
    assert jobs.recent_card_for(CONFIG, "Ayden Escarrega", now=now, kind="aged")["url"] == "today"
    assert jobs.recent_card_for(CONFIG, "Raymond Vargas", now=now, kind="aged") is None, "yesterday's is its own order"
    assert jobs.recent_card_for(CONFIG, "Ayden Escarrega", now=now) is None, "an aged card isn't a New Agent card"


# ------------------------------------------------------------- aged, from the invoice note

def test_the_invoices_note_is_kept_whatever_payra_calls_it():
    from wilbyte import payraapi

    record = {"_id": "inv1", "customer": {"first_name": "Aiden", "last_name": "Larsen", "email": "a@example.com"},
              "totals": {"total": 103.5}, "internal_note": "AGED IUL\n180+\nin states IL, SC, AZ, and TX",
              "notes": [{"text": "100 $1 Aged leads in those same states"}], "description": "Aged IUL"}
    slim = payraapi.slim_invoice(record)
    assert slim["notes"] == "AGED IUL\n180+\nin states IL, SC, AZ, and TX\n100 $1 Aged leads in those same states"


def test_the_payments_invoice_note_by_email_and_amount(monkeypatch):
    from wilbyte import payraapi

    now = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)
    data = {"invoices": {
        "a": {"id": "a", "name": "Aiden Larsen", "email": "larsen@example.com", "total": 103.5,
              "notes": "AGED IUL 180+ in states IL, SC, AZ, TX", "created": "2026-10-14T14:40:00Z"},
        "b": {"id": "b", "name": "Aiden Larsen", "email": "larsen@example.com", "total": 500,
              "notes": "a different order", "created": "2026-10-14T14:50:00Z"},
        "c": {"id": "c", "name": "Aiden Larsen", "email": "larsen@example.com", "total": 103.5,
              "notes": "last month's", "created": "2026-09-14T14:40:00Z"},
        "d": {"id": "d", "name": "Somebody Else", "email": "x@example.com", "total": 103.5,
              "notes": "not theirs", "created": "2026-10-14T14:55:00Z"}}}
    class Payra:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def changed(self, kind, after):
            raise payraapi.PayraError("offline")

    monkeypatch.setattr(payraapi, "PayraClient", Payra)
    monkeypatch.setattr(payraapi, "load", lambda *a, **k: data)
    config = SimpleNamespace(secrets=SimpleNamespace(payra_api_token="t", payra_site_id="s"))
    assert jobs.payra_invoice_note(config, name="Aiden Larsen", email="larsen@example.com", amount="$103.5",
                                   now=now) == "AGED IUL 180+ in states IL, SC, AZ, TX"
    assert jobs.payra_invoice_note(config, name="Nobody Here", amount="$1", now=now) == ""


def _aged_post(i=950):
    message = _message("@here\nNew Payment | Payra\nNAME: Aiden Larsen\nEMAIL: larsen@example.com\n"
                       "AMOUNT: $103.5\nPRODUCT: Aged Leads (IUL)")
    message.id, message.channel.name = i, "💸 | new-payment"
    return message


def test_an_aged_payment_with_an_invoice_note_gets_its_card_with_no_gavel(order, monkeypatch):
    """"ryte will just immediately create the card"."""
    _note_is(monkeypatch, lambda config, **kw: "AGED IUL\n180+\nin states IL, SC, AZ, and TX")
    order.fields = {"states": "IL, SC, AZ, TX"}
    message = _aged_post()
    assert client._aged_payment(message)
    assert asyncio.run(client._aged_from_invoice(_bot(message), message, wait=0)) == "made"
    assert [one[0] for one in order.made] == ["aged"]
    assert order.notes[0] == "Payra invoice note: AGED IUL\n180+\nin states IL, SC, AZ, and TX"
    assert "Payra invoice note: AGED IUL" in order.reads[0]
    assert asyncio.run(client._aged_from_invoice(_bot(message), message, wait=0)) == "already"


def test_no_note_the_card_is_made_from_the_thread_anyway(order, monkeypatch):
    """"aged leads should be automatic add with states, thread or no thread
    while relying on invoice if it has no states/thread added" - Jackson
    Ruehl's invoice had no note; Kharyl's thread had his states."""
    _note_is(monkeypatch, lambda config, **kw: "")
    message = _aged_post()
    message.thread = _notes("can i get 10 of the aged vets 30-90 days. OK,TX,AZ,MO,AL", "uprise")
    order.fields = {"states": "OK, TX, AZ, MO, AL", "leads": "10"}
    assert asyncio.run(client._aged_from_invoice(_bot(message), message, wait=0)) == "made"
    assert [one[0] for one in order.made] == ["aged"]
    assert "OK,TX,AZ,MO,AL" in order.reads[0] and "Payra invoice note" not in order.reads[0]
    assert ordercards.load()["950"]["extra_notes"] == []


def test_the_invoice_note_stays_on_the_card_when_the_thread_updates_it(order, monkeypatch):
    _note_is(monkeypatch, lambda config, **kw: "AGED IUL in IL, SC")
    message = _aged_post()
    asyncio.run(client._aged_from_invoice(_bot(message), message, wait=0))
    assert ordercards.load()["950"]["extra_notes"] == ["Payra invoice note: AGED IUL in IL, SC"]
    monkeypatch.setattr(jobs, "order_card_update", lambda config, card_id, **kw: ("same", []))
    order.reads.clear()
    asyncio.run(client._order_refresh(_bot(message), "950", settle=0))
    assert "Payra invoice note: AGED IUL in IL, SC" in order.reads[0]


def test_a_thread_started_after_the_card_still_updates_it(monkeypatch):
    ordercards.save({"950": {"status": "made", "thread_id": "", "card_id": "c"},
                     "951": {"status": "made", "thread_id": "", "merged": True, "card_id": "c2"}})
    started = []

    class Loop:
        def create_task(self, coroutine):
            started.append(coroutine)
            coroutine.close()
            return SimpleNamespace(cancel=lambda: None)

    bot = SimpleNamespace(loop=Loop())
    client._order_note_posted(bot, SimpleNamespace(channel=SimpleNamespace(id=950)))
    client._order_note_posted(bot, SimpleNamespace(channel=SimpleNamespace(id=951)))
    assert len(started) == 1, "a merged payment's thread rewrote the card it was put on"
    client._ORDER_PENDING.clear()


def test_from_a_note_its_aged_or_nothing(order, monkeypatch):
    order.kind = "fresh"
    _note_is(monkeypatch, lambda config, **kw: "25 OTP vets")
    asyncio.run(client._aged_from_invoice(_bot(_aged_post()), _aged_post(), wait=0))
    assert order.made == [], "a New Agent card made with nobody's gavel"


def test_only_aged_payments_in_new_payment():
    vet = _message("New Payment | Payra\nNAME: Fabian\nPRODUCT: VET")
    vet.channel.name = "new-payment"
    assert not client._aged_payment(vet)
    elsewhere = _aged_post()
    elsewhere.channel.name = "team-chat"
    assert not client._aged_payment(elsewhere)



def test_the_note_is_read_straight_from_payras_last_two_days(monkeypatch):
    from wilbyte import payraapi

    now = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)
    asked = []

    class Payra:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def changed(self, kind, after):
            asked.append((kind, after))
            return {"records": [{"_id": "inv9", "customer": {"first_name": "Aiden", "last_name": "Larsen",
                                                              "email": "larsen@example.com"},
                                 "totals": {"total": 103.5}, "notes": "AGED IUL in IL, SC, AZ, TX",
                                 "created_at": "2026-10-14T14:44:00Z"}]}

    monkeypatch.setattr(payraapi, "PayraClient", Payra)
    monkeypatch.setattr(payraapi, "load", lambda *a, **k: {"invoices": {}})
    config = SimpleNamespace(secrets=SimpleNamespace(payra_api_token="t", payra_site_id="s"))
    assert jobs.payra_invoice_note(config, name="Aiden Larsen", email="larsen@example.com", amount="$103.5",
                                   now=now) == "AGED IUL in IL, SC, AZ, TX"
    assert asked[0][0] == "invoices" and asked[0][1].startswith("2026-10-12")


def test_invoice_note_shows_every_note_field_as_payra_has_it(order, monkeypatch):
    monkeypatch.setattr(jobs, "payra_invoice_note_fields", lambda config, name: [
        ("#INV-19860 · $103.5", [("internal_note", "AGED IUL 180+ in states IL, SC, AZ, and TX")])])
    asyncio.run(client._invoice_note(_bot(_message()), order.responder, "Aiden Larsen"))
    said = order.responder.sent[-1][0]
    assert "**#INV-19860 · $103.5**" in said and "`internal_note`: AGED IUL 180+" in said
    assert mentions.parse("<@1> invoice note Aiden Larsen").action == "invoicenote"



def test_an_aged_payment_missed_while_down_gets_its_card_on_start(order, monkeypatch):
    now = datetime(2026, 10, 14, 16, 30, tzinfo=timezone.utc)
    paid = _aged_post(960)
    paid.created_at, paid.reactions = datetime(2026, 10, 14, 16, 10, tzinfo=timezone.utc), []
    morning = _aged_post(961)
    morning.created_at, morning.reactions = datetime(2026, 10, 14, 14, 45, tzinfo=timezone.utc), []

    class Place:
        id = 700
        last_message_id = int(((now.timestamp() - 60) * 1000 - 1420070400000)) << 22

        def history(self, **kw):
            async def gen():
                for one in (paid, morning):
                    yield one
            return gen()

    async def threads():
        return []

    guild = SimpleNamespace(id=1, text_channels=[Place()], active_threads=threads)
    monkeypatch.setattr(client, "_setup_guild", lambda bot, config: guild)
    made = []

    async def making(bot, message, *, wait=None, **kw):
        made.append((message.id, wait))
        return "made"

    async def no_wait(_s):
        return None

    monkeypatch.setattr(client, "_aged_from_invoice", making)
    monkeypatch.setattr(client.asyncio, "sleep", no_wait)

    async def run():
        done = await client._catch_up_gavels(_bot(paid), now=now)
        await asyncio.sleep(0)
        return done

    done = asyncio.run(run())
    assert done == ["960"] and made == [(960, 0.0)], "this morning's may have a card made by hand"


# ------------------------------------------------------------- be sure not to fail here

def _checked(**fields):
    source = ("New Payment | Payra\nNAME: Taylor Bolger\nEMAIL: t@example.com\nPHONE: +16602806422\n"
              "PRODUCT: Text-Verified Trucker IUL\nKharyl: 20 trucker IUL, TX, FL, SC, GA\nlive fri oct 16")
    base = dict(name="Taylor Bolger", email="t@example.com", phone="+16602806422", states="TX, FL, SC, GA",
                package="Text-Verified Trucker IUL", leads="20", launch="Friday, October 16")
    base.update(fields)
    return {"kind": "fresh", "launch_iso": "2026-10-16", "source": source, "fields": base}


def test_a_card_that_matches_the_order_passes():
    """"make sure that this is not failing, we dont want setting up agent with
    wrong states, wrong lead type, wrong cap, etc."."""
    assert ordercards.doubts(_checked(), today=date(2026, 10, 14)) == ([], [])


@pytest.mark.parametrize("fields, said", [
    ({"states": "TX, FL, SC, MO"}, "MO is on the card but isn't in the order"),
    ({"states": "TX, FL, SC"}, "the order also says GA"),
    ({"package": "Text-Verified Veteran"}, "the payment post says “Text-Verified Trucker IUL”"),
    ({"leads": "25"}, "25 isn't in the order"),
    ({"leads": "No cap (ad spend)"}, "doesn't say Phoenix"),
])
def test_what_doesnt_match_the_order_is_caught(fields, said):
    wrong, _missing = ordercards.doubts(_checked(**fields), today=date(2026, 10, 14))
    assert any(said in one for one in wrong), wrong


@pytest.mark.parametrize("fields, said", [
    ({"email": "someone@example.com"}, "check the email (someone@example.com)"),
    ({"phone": "+15550001111"}, "check the phone"),
])
def test_contact_details_not_in_the_text_are_said_not_held_for(fields, said):
    """Mohammad Kanan's phone was read off a screenshot of his texts."""
    wrong, missing = ordercards.doubts(_checked(**fields), today=date(2026, 10, 14))
    assert wrong == [] and any(said in one for one in missing), missing


def test_aged_read_as_fresh_and_a_far_launch_are_caught():
    card = _checked()
    card["source"] = card["source"].replace("Text-Verified Trucker IUL", "Aged Leads (VET)")
    card["fields"]["package"] = "Aged VET"
    assert any("aged - but this reads as a new agent's order" in one
               for one in ordercards.doubts(card, today=date(2026, 10, 14))[0])
    far = _checked()
    far["launch_iso"] = "2027-03-01"
    assert any("too far from today" in one for one in ordercards.doubts(far, today=date(2026, 10, 14))[0])


def test_all_states_except_and_ad_spend_pass_when_the_order_says_so():
    card = {"kind": "fresh", "launch_iso": "2026-10-16",
            "source": "NAME: Samuel Riggins\nPRODUCT:\nPHX + 2.0 at $1050\nALL STATES besides CA, GA, IA, NY\nLIVE FRI OCT 16",
            "fields": {"states": "All states except CA, GA, IA, NY", "package": "PHX + 2.0", "leads": "No cap (ad spend)"}}
    assert ordercards.doubts(card, today=date(2026, 10, 14))[0] == []
    card["fields"]["states"] = "All states except CA, GA, IA, NY, TX"
    assert any("TX is left out" in one for one in ordercards.doubts(card, today=date(2026, 10, 14))[0])


def test_missing_isnt_wrong():
    card = _checked(states="", leads="")
    wrong, missing = ordercards.doubts(card, today=date(2026, 10, 14))
    assert wrong == [] and "no states in the order yet" in missing


def test_a_card_that_doesnt_add_up_isnt_made_until_somebody_says_so(order, monkeypatch):
    def reading(config, *, text, images, today, notes=()):
        order.reads.append(text)
        return {**_checked(states="TX, FL, SC, MO"), "client": "Taylor Bolger", "name": "New Agent - Taylor Bolger",
                "desc": "d", "missing": []}

    monkeypatch.setattr(jobs, "read_order", reading)
    monkeypatch.setattr(client, "_today", lambda config: date(2026, 10, 14))
    order.pressed = True

    async def run():
        await client._order_gavelled(_bot(_message()), GAVEL)
        for _ in range(200):
            if order.made:
                break
            await asyncio.sleep(0.01)

    asyncio.run(run())
    held = [text for text, view in order.responder.sent if view is not None]
    assert held and "Not made yet — Taylor Bolger" in held[0] and "MO is on the card" in held[0]
    assert len(order.made) == 1, "pressing Create anyway made it"
    assert "Made past a check" in _said(order)[-1][0]


def test_held_and_left_makes_nothing_and_can_be_gavelled_again(order, monkeypatch):
    def reading(config, *, text, images, today, notes=()):
        return {**_checked(leads="25"), "client": "Taylor Bolger", "name": "New Agent - Taylor Bolger",
                "desc": "d", "missing": []}

    monkeypatch.setattr(jobs, "read_order", reading)
    monkeypatch.setattr(client, "_today", lambda config: date(2026, 10, 14))
    order.pressed = False

    async def run():
        await client._order_gavelled(_bot(_message()), GAVEL)
        for _ in range(20):
            await asyncio.sleep(0.01)

    asyncio.run(run())
    assert order.made == [] and "900" not in client._ORDER_ACTIVE
    assert (ordercards.load().get("900") or {}).get("status") in (None, "held")



# ------------------------------------------------------------- same name, another order

def _their_card(monkeypatch, desc):
    class Trello:
        def card_detail(self, card_id):
            return {"desc": desc}

        def close(self):
            pass

    monkeypatch.setattr(jobs, "open_trello", lambda cfg: Trello())


def test_another_payment_with_nothing_new_is_the_same_order(monkeypatch):
    _their_card(monkeypatch, "Name: Bram Gutierrez\nLead Type/Package Selected: Text-Verified Veteran Leads\n"
                             "Launch Date: Tuesday, October 13")
    assert jobs.same_order(CONFIG, {"id": "c"}, {"fields": {"package": "VET", "launch": ""}, "launch_iso": ""})
    assert jobs.same_order(CONFIG, {"id": "c"}, {"fields": {"package": "Text-Verified Veteran", "launch": "Oct 13"}})


def test_a_different_live_date_or_lead_type_is_a_different_order(monkeypatch):
    """"same person different orders different live dates"."""
    _their_card(monkeypatch, "Name: Bram Gutierrez\nLead Type/Package Selected: Text-Verified Veteran Leads\n"
                             "Launch Date: Tuesday, October 13")
    assert not jobs.same_order(CONFIG, {"id": "c"}, {"fields": {"package": "VET", "launch": "Wednesday, October 14"}})
    assert not jobs.same_order(CONFIG, {"id": "c"}, {"fields": {"package": "Text-Verified Trucker IUL", "launch": ""}})


def test_a_different_order_from_the_same_person_gets_its_own_card(order):
    order.kind = "fresh"
    order.existing = {"id": "5f0000000000000000000001", "name": "New Agent - Bram Gutierrez",
                      "url": "https://trello.com/c/bram"}
    order.same = False
    asyncio.run(client._order_gavelled(_bot(_message()), GAVEL))
    assert order.comments == [] and [one[0] for one in order.made] == ["fresh"]


# ------------------------------------------------- notes a card missed

def _made(key, **more):
    from datetime import datetime, timezone
    return {"status": "made", "card_id": f"c{key}", "name": f"Aged Leads - {key}", "url": f"https://trello.com/c/{key}",
            "made_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "thread_id": key, **more}


def _threads(monkeypatch, last):
    async def thread(bot, key, order):
        return SimpleNamespace(parent=object(), last_message_id=last.get(key)) if key in last else None

    monkeypatch.setattr(client, "_order_thread", thread)


def test_a_note_lost_to_a_restart_is_found_by_the_sweep(monkeypatch):
    """Leo Fonseca: Faith's note came in, RYTE restarted inside the two
    minutes, and the card never heard about it."""
    ordercards.save({"950": _made("950", seen_upto="1000"), "951": _made("951", seen_upto="2000"),
                     "952": _made("952", seen_upto="10", merged=True)})
    _threads(monkeypatch, {"950": 1001, "951": 2000, "952": 99})
    refreshed = []

    async def refresh(bot, key, *, settle=None):
        refreshed.append((key, settle))
        return "updated"

    monkeypatch.setattr(client, "_order_refresh", refresh)
    assert asyncio.run(client._order_sweep(SimpleNamespace())) == ["950"]
    assert refreshed == [("950", 0)]


def test_the_first_sweep_reads_from_when_the_card_was_made(monkeypatch):
    """Louis Mordecai: the note came in, RYTE restarted for the update that
    brought the sweep - and the first sweep marked it read."""
    import discord
    from datetime import datetime, timedelta, timezone

    made = datetime.now(timezone.utc) - timedelta(hours=1)
    ordercards.save({"950": _made("950", made_at=made.isoformat(timespec="seconds")),
                     "951": _made("951", made_at=made.isoformat(timespec="seconds"))})
    at_made = discord.utils.time_snowflake(made.replace(microsecond=0))
    _threads(monkeypatch, {"950": discord.utils.time_snowflake(made + timedelta(minutes=30)), "951": at_made - 5})
    refreshed = []

    async def refresh(bot, key, *, settle=None):
        refreshed.append(key)
        return "updated"

    monkeypatch.setattr(client, "_order_refresh", refresh)
    assert asyncio.run(client._order_sweep(SimpleNamespace())) == ["950"]
    assert ordercards.load()["951"]["seen_upto"] == str(at_made)


def test_old_orders_and_ones_already_waiting_are_left_to_themselves(monkeypatch):
    ordercards.save({"950": _made("950", seen_upto="1", made_at="2026-01-01T00:00:00+00:00"),
                     "951": _made("951", seen_upto="1")})
    _threads(monkeypatch, {"950": 5, "951": 5})
    monkeypatch.setitem(client._ORDER_PENDING, "951", SimpleNamespace(cancel=lambda: None))

    async def refresh(bot, key, *, settle=None):
        raise AssertionError(key)

    monkeypatch.setattr(client, "_order_refresh", refresh)
    assert asyncio.run(client._order_sweep(SimpleNamespace())) == []


def test_a_failed_update_is_said_once_and_given_up_after_three_tries(monkeypatch):
    ordercards.save({"950": _made("950", seen_upto="1", message_channel_id="700")})
    _threads(monkeypatch, {"950": 5})
    sent = []

    class Responder:
        async def send(self, text, **kw):
            sent.append(text)

    monkeypatch.setattr(client, "_order_responder", lambda bot: Responder())

    def broken(i):
        raise RuntimeError("Trello said no")

    bot = SimpleNamespace(config=CONFIG, get_channel=broken)
    for _ in range(3):
        assert asyncio.run(client._order_refresh(bot, "950", settle=0)) == "failed"
    assert len(sent) == 1 and "Couldn't update **Aged Leads - 950**" in sent[0] and "recheck" in sent[0]
    assert ordercards.load()["950"]["fails"] == 3

    async def refresh(bot, key, *, settle=None):
        raise AssertionError("tried a fourth time")

    monkeypatch.setattr(client, "_order_refresh", refresh)
    assert asyncio.run(client._order_sweep(SimpleNamespace())) == []
    # A newer note is a fresh try.
    _threads(monkeypatch, {"950": 6})
    monkeypatch.setattr(client, "_order_refresh", lambda bot, key, settle=None: asyncio.sleep(0, "updated"))
    assert asyncio.run(client._order_sweep(SimpleNamespace())) == ["950"]


def test_recheck_reads_the_thread_again(monkeypatch):
    """"youll mess this up if you use same command" - "update" is RYTE
    updating itself; reading an order again is "recheck"."""
    from wilbyte.bot import mentions

    link = "https://discord.com/channels/1/700/950"
    assert mentions.parse("update").action == "update"
    assert mentions.parse("update card").action != "orderrefresh"
    assert mentions.parse(f"recheck {link}").action == "orderrefresh"
    assert mentions.parse("recheck Louis Mordecai").action == "orderrefresh"
    assert mentions.parse("recheck the card for Louis Mordecai").brief == "Louis Mordecai"
    assert mentions.parse(f"order {link}").action == "orderlink"
    ordercards.save({"950": _made("950", client="Louis Mordecai", made_at="2026-10-14T15:00:00+00:00"),
                     "940": _made("940", client="Louis Mordecai", made_at="2026-10-01T15:00:00+00:00"),
                     "941": _made("941", client="Louis Mordecai", merged=True, made_at="2026-10-14T16:00:00+00:00")})
    sent, refreshed = [], []

    class Responder:
        async def send(self, text, **kw):
            sent.append(text)

    async def refresh(bot, key, *, settle=None):
        refreshed.append((key, settle))
        return "same"

    monkeypatch.setattr(client, "_order_refresh", refresh)
    asyncio.run(client._order_update_now(SimpleNamespace(), Responder(), link))
    asyncio.run(client._order_update_now(SimpleNamespace(), Responder(), "louis mordecai"))
    assert refreshed == [("950", 0), ("950", 0)] and "Nothing new" in sent[-1]
    asyncio.run(client._order_update_now(SimpleNamespace(), Responder(), "https://discord.com/channels/1/700/4"))
    asyncio.run(client._order_update_now(SimpleNamespace(), Responder(), "Nobody Here"))
    assert "haven't made a card" in sent[-1] and "haven't made a card" in sent[-2]


def test_a_state_spelled_wrong_in_the_order_is_still_said():
    """Simon Aleman: "Illinoi" in the order held up IL on the card."""
    source = ("Lead Type: Text-Verified Veteran Leads\nStates Leads In: Florida, North Carolina, West Virginia, "
              "California, Hawaii, Ohio, Iowa, Illinoi, Texas, Pensylvania")
    assert ordercards._said_state("IL", source)
    assert ordercards._said_state("PA", source)
    assert not ordercards._said_state("UT", "Utha")  # short names have to be right
    assert not ordercards._said_state("KS", "Arkansas")
    assert not ordercards._said_state("NY", source)


def test_a_forward_with_only_a_tag_on_it_is_a_note(monkeypatch):
    """Kharyl forwarded Lou's "8am - 8pm M-F" with only "@Nicole" on it."""
    from datetime import timedelta

    class Thread:
        id = 900
        parent = SimpleNamespace(name="new-payment")

        def history(self, **kw):
            async def gen():
                snap = SimpleNamespace(content="Hey Kharyl! For the fresh drip could you schedule it from 8am - 8pm "
                                               "M-F please!", embeds=[], attachments=[])
                yield SimpleNamespace(id=1, content="<@757873984934117396>", embeds=[], attachments=[],
                                      message_snapshots=[snap], mentions=[],
                                      author=SimpleNamespace(display_name="Kharyl", name="kharyl", bot=False),
                                      created_at=WHEN + timedelta(hours=2))
            return gen()

    message = _message(thread=Thread())
    _text, _images, _thread, notes = asyncio.run(client._order_said(SimpleNamespace(), message, since=WHEN))
    assert notes == ["(forwarded) Hey Kharyl! For the fresh drip could you schedule it from 8am - 8pm M-F please!"]


def test_payra_down_still_makes_the_aged_card(order, monkeypatch):
    def down(config, **kw):
        raise RuntimeError("Payra is down")

    _note_is(monkeypatch, down)
    assert asyncio.run(client._aged_from_invoice(_bot(_aged_post()), _aged_post(), wait=0)) == "made"
    assert len(order.made) == 1
    assert any("Couldn't read" in said for said, _view in _said(order))


def test_the_gavel_goes_on_the_payment_once_its_card_is_made(order):
    """"once ryte finished creating a card, can it add an emoji to that
    payment that way i know? same emoji as leadlab_hammertime"."""
    message = _message(thread=_notes("25 aged MTG, states AZ TX"))
    reacted = []

    class Partial:
        def __init__(self, i):
            self.i = i

        async def add_reaction(self, emoji):
            reacted.append((self.i, emoji.name))

    message.channel.get_partial_message = Partial
    message.channel.guild = SimpleNamespace(id=1)
    bot = _bot(message)
    bot.emojis = [SimpleNamespace(name="smile", guild=SimpleNamespace(id=1)),
                  SimpleNamespace(name="leadlab_hammertime", guild=SimpleNamespace(id=2)),
                  SimpleNamespace(name="leadlab_hammertime", guild=SimpleNamespace(id=1))]
    asyncio.run(client._order_gavelled(bot, GAVEL))
    assert len(order.made) == 1
    assert reacted == [(900, "leadlab_hammertime")]
    # RYTE's own gavel is never a gavel.
    started = []
    fake = SimpleNamespace(user=SimpleNamespace(id=RYTE), loop=SimpleNamespace(create_task=started.append))
    asyncio.run(client.WilByteBot.on_raw_reaction_add(fake, SimpleNamespace(
        user_id=RYTE, emoji=SimpleNamespace(name="leadlab_hammertime"), message_id=900, channel_id=700)))
    assert started == []


def test_order_news_never_goes_to_the_growth_server(monkeypatch):
    """"why did this ping here" - Growth's #trello-manager, unasked."""
    from wilbyte.bot import mirror
    from wilbyte.bot.responders import ChannelResponder

    growth, goat = SimpleNamespace(id=1), SimpleNamespace(id=2)
    board = SimpleNamespace(id=10, guild=growth)
    twin = SimpleNamespace(id=20, guild=goat)
    monkeypatch.setattr(client, "_in_setup_guild", lambda bot, guild: guild is growth)
    monkeypatch.setattr(client, "_board_responder", lambda bot: ChannelResponder(board))
    monkeypatch.setattr(client, "_hub_responder", lambda bot: None)
    monkeypatch.setitem(mirror.PAIRS, 10, 20)
    bot = SimpleNamespace(get_channel=lambda i: {20: twin}.get(i))
    assert client._order_responder(bot).channel is twin
    monkeypatch.delitem(mirror.PAIRS, 10)
    assert client._order_responder(bot) is None
    monkeypatch.setattr(client, "_board_responder", lambda bot: ChannelResponder(twin))
    assert client._order_responder(bot).channel is twin


# ------------------------------------------------ paid by bank, not settled

def test_bank_or_card_settled_or_not():
    assert jobs.payment_by_bank({"how": "ACH", "kind": "ach"})
    assert jobs.payment_by_bank({"how": "", "kind": "bank_account checking"})
    assert jobs.payment_by_bank({"how": "eCheck"})
    assert not jobs.payment_by_bank({"how": "Visa", "kind": "card credit"})
    assert not jobs.payment_by_bank({"how": "Mastercard", "kind": "card"})
    assert not jobs.payment_by_bank(None)
    assert not jobs.payment_settled({"status": "Captured"}), "captured doesnt mean settled"
    assert jobs.payment_settled({"status": "Settled"})
    assert jobs.payment_failed({"status": "Voided"}) and jobs.payment_failed({"status": "Declined"})
    assert jobs.payment_failed({"status": "Returned"}) and not jobs.payment_failed({"status": "Captured"})


def test_the_payment_found_is_the_one_for_that_amount(monkeypatch):
    """Luke Mertz: $200 by bank voided, then $207 by card - one invoice."""
    from datetime import datetime, timezone
    from wilbyte import payraapi

    records = [
        {"_id": "p1", "amount": 200, "status": "Voided", "customer_display": "Luke Mertz",
         "payment_type": "ach", "display_name": "Bank ****7043", "paid_on": "2026-10-14T14:10:00Z"},
        {"_id": "p2", "amount": 207, "status": "Captured", "customer_display": "Luke Mertz",
         "payment_type": "card", "display_name": "Visa ****8687", "paid_on": "2026-10-14T14:44:00Z"},
        {"_id": "p3", "amount": 200, "status": "Captured", "customer_display": "Luke Mertzger",
         "payment_type": "ach", "paid_on": "2026-10-14T14:50:00Z"},
    ]

    class Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def changed(self, kind, after):
            assert kind == "payments"
            return {"records": records}

    monkeypatch.setattr(payraapi, "PayraClient", Client)
    config = SimpleNamespace(secrets=SimpleNamespace(payra_api_token="t", payra_site_id="s"))
    now = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)
    assert jobs.payra_payment_for(config, name="Luke Mertz", amount="200.00", now=now)["id"] == "p1"
    assert jobs.payra_payment_for(config, name="Luke Mertz", amount="207", now=now)["id"] == "p2"
    assert jobs.payra_payment_for(config, name="Luke Mertz", amount="", now=now)["id"] == "p2"
    assert jobs.payra_payment_for(config, name="Nobody Else", amount="207", now=now) is None


BANK = {"id": "pay9", "amount": 200.0, "status": "Captured", "how": "Bank ****7043", "kind": "ach"}
CARD = {"id": "pay8", "amount": 207.0, "status": "Captured", "how": "Visa ****8687", "kind": "card"}


def test_aged_paid_by_bank_waits_in_franklin_admin(order):
    order.paid = (BANK, True)
    asyncio.run(client._order_gavelled(_bot(_message(thread=_notes("25 aged MTG, states AZ TX"))), GAVEL))
    assert order.lists == ["Franklin (Admin)"]
    [(card_id, comment)] = order.bank_comments
    assert card_id == "card1" and "Paid via bank" in comment and "not settled" in comment and "Settled" in comment
    held = ordercards.load()["900"]
    assert held["bank_hold"] is True and held["payment_id"] == "pay9"
    [(said, _view)] = _said(order)
    assert "Paid by bank — not settled yet" in said and "Franklin (Admin)" in said


def test_aged_paid_by_card_goes_straight_to_aged_leads_order_done(order):
    order.paid = (CARD, True)
    asyncio.run(client._order_gavelled(_bot(_message(thread=_notes("25 aged MTG, states AZ TX"))), GAVEL))
    assert order.lists == [""] and order.bank_comments == []
    assert ordercards.load()["900"]["bank_hold"] is False


def test_aged_already_settled_by_bank_isnt_held(order):
    order.paid = (dict(BANK, status="Settled"), True)
    asyncio.run(client._order_gavelled(_bot(_message(thread=_notes("25 aged MTG, states AZ TX"))), GAVEL))
    assert order.lists == [""]


def test_a_new_agent_paid_by_bank_only_gets_a_comment(order):
    """"for new agent we dont need to worry ... maybe you can add a comment"."""
    order.kind, order.paid = "fresh", (BANK, True)
    asyncio.run(client._order_gavelled(_bot(_message()), GAVEL))
    assert order.lists == [""]
    [(_card, comment)] = order.bank_comments
    assert "Paid via bank" in comment and "Aged Leads Order Done" not in comment
    assert ordercards.load()["900"]["bank_hold"] is False


def test_an_aged_payment_not_found_in_payra_is_said(order):
    order.paid = (None, True)
    asyncio.run(client._order_gavelled(_bot(_message(thread=_notes("25 aged MTG, states AZ TX"))), GAVEL))
    assert "Couldn't find this payment in Payra" in _said(order)[0][0]


def _bank_held(**more):
    from datetime import datetime, timezone
    return {"status": "made", "kind": "aged", "card_id": "c1", "name": "Aged Leads - Luke Mertz",
            "url": "https://trello.com/c/x", "payment_id": "pay9", "bank_hold": True,
            "made_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **more}


def test_a_settled_bank_payment_moves_the_card(monkeypatch):
    ordercards.save({"900": _bank_held(), "901": _bank_held(payment_id="pay10"), "902": _bank_held(bank_hold=False)})
    status = {"pay9": "Settled", "pay10": "Captured"}
    monkeypatch.setattr(jobs, "payra_payment_now", lambda config, pid: dict(BANK, id=pid, status=status[pid]))
    moved, sent = [], []
    monkeypatch.setattr(jobs, "order_bank_settled", lambda config, card_id, text: moved.append(card_id) or "moved")

    class Responder:
        async def send(self, text, **kw):
            sent.append(text)

    monkeypatch.setattr(client, "_order_responder", lambda bot: Responder())
    assert asyncio.run(client._bank_holds(SimpleNamespace(config=CONFIG))) == ["900"]
    assert moved == ["c1"] and "settled" in sent[0] and "Aged Leads Order Done" in sent[0]
    held = ordercards.load()
    assert held["900"]["bank_hold"] is False and held["901"]["bank_hold"] is True


def test_a_failed_bank_payment_is_said_and_left(monkeypatch):
    ordercards.save({"900": _bank_held()})
    monkeypatch.setattr(jobs, "payra_payment_now", lambda config, pid: dict(BANK, status="Voided"))
    comments, sent = [], []
    monkeypatch.setattr(jobs, "order_bank_comment", lambda config, card_id, text: comments.append(text))

    def never(*a, **k):
        raise AssertionError("moved a card whose payment failed")

    monkeypatch.setattr(jobs, "order_bank_settled", never)

    class Responder:
        async def send(self, text, **kw):
            sent.append(text)

    monkeypatch.setattr(client, "_order_responder", lambda bot: Responder())
    assert asyncio.run(client._bank_holds(SimpleNamespace(config=CONFIG))) == ["900"]
    assert "voided" in comments[0] and "No leads" in comments[0] and "Voided" in sent[0]
    assert ordercards.load()["900"]["bank_hold"] == "failed"
    assert asyncio.run(client._bank_holds(SimpleNamespace(config=CONFIG))) == [], "said twice"


def test_settled_moves_only_a_card_still_in_franklin_admin(monkeypatch):
    class Held(Board):
        def __init__(self, here):
            super().__init__()
            self.here, self.moves = here, []

        def board_lists(self, board_id):
            return super().board_lists(board_id) + [{"id": "f", "name": "Franklin (Admin)"}]

        def card_full(self, card_id):
            return {"idList": self.here}

        def move_card(self, card_id, list_id, position="top"):
            self.moves.append((card_id, list_id))

        def add_comment(self, card_id, text):
            self.comments.append(text)

        def close(self):
            pass

    board = Held("f")
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)
    assert jobs.order_bank_settled(CONFIG, "c1", "✅ Bank payment settled — moved to Aged Leads Order Done.") == "moved"
    assert board.moves == [("c1", "a")] and "moved" in board.comments[0]
    board = Held("somewhere-else")
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)
    assert jobs.order_bank_settled(CONFIG, "c1", "✅ Bank payment settled — moved to Aged Leads Order Done.") == "left"
    assert board.moves == [] and board.comments == ["✅ Bank payment settled."]


def test_a_payment_payra_wont_hand_over_by_id_is_read_from_the_saved_copy(monkeypatch):
    from wilbyte import payraapi

    class Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def one(self, kind, pid):
            raise payraapi.PayraError("Payra said HTTP 404")

    monkeypatch.setattr(payraapi, "PayraClient", Client)
    monkeypatch.setattr(payraapi, "load", lambda: {"payments": {"pay9": {"id": "pay9", "status": "Settled"}}})
    config = SimpleNamespace(secrets=SimpleNamespace(payra_api_token="t", payra_site_id="s"))
    assert jobs.payment_settled(jobs.payra_payment_now(config, "pay9"))


def test_a_launch_date_the_order_never_gives_isnt_made_up():
    """Blaze Lubbers: the card said "Wednesday, October 14" - the day paid."""
    paid = "[10/14 03:24PM] LeadLab: New Payment | Payra\nNAME: Blaze Lubbers\nEMAIL: lubbersllc@gmail.com\nPRODUCT: VET"
    assert not ordercards.says_when(paid)
    assert not ordercards.says_when("[10/14 03:24PM] LeadLab: NAME: Monica Sunderland\nEMAIL: keegan.sunny@gmail.com")
    assert ordercards.says_when(paid + "\n[10/14 03:30PM] Kharyl: live tomorrow")
    assert ordercards.says_when(paid + "\nPayra invoice note: 50 VET FL TX, start Fri 10/16")
    assert ordercards.says_when("launch Oct. 16")
    card = {"kind": "fresh", "source": paid, "launch_iso": "2026-10-14",
            "fields": {"states": "", "package": "VET", "leads": "", "launch": "Wednesday, October 14"}}
    wrong, _missing = ordercards.doubts(card, today=date(2026, 10, 14))
    assert any("doesn't say when" in one for one in wrong)


def test_the_invoice_is_read_for_a_gavelled_new_agent(order, monkeypatch):
    """"the information is on ... invoice"."""
    order.kind = "fresh"
    looked = []

    def note(config, *, name, email="", amount="", product="", now=None):
        looked.append((name, amount))
        return "50 VET, FL TX GA, launch Fri 10/16"

    _note_is(monkeypatch, note)
    message = _message("New Payment | Payra\nNAME: Blaze Lubbers\nEMAIL: lubbersllc@example.com\nAMOUNT: $1966.5\n"
                       "PRODUCT: VET")
    asyncio.run(client._order_gavelled(_bot(message), GAVEL))
    assert looked == [("Blaze Lubbers", "1966.5")]
    assert "Payra invoice note: 50 VET, FL TX GA, launch Fri 10/16" in order.reads[0]
    assert ordercards.load()["900"]["extra_notes"] == ["Payra invoice note: 50 VET, FL TX GA, launch Fri 10/16"]


def test_what_an_invoice_says_is_its_note_description_and_lines():
    said = jobs.invoice_said({"notes": "FL TX GA", "description": "Launch Friday",
                              "lines": [{"name": "VET Leads", "quantity": 50, "price": 39.33}]})
    assert said == "FL TX GA\nDescription: Launch Friday\nLines: VET Leads × 50 @ $39.33"
    assert jobs.invoice_said({"notes": "", "description": "", "lines": [{"name": "VET"}]}) == ""


def test_recheck_reads_an_invoice_the_card_was_made_without(order, monkeypatch):
    order.kind = "fresh"
    message = _message("New Payment | Payra\nNAME: Blaze Lubbers\nAMOUNT: $1966.5\nPRODUCT: VET")
    _note_is(monkeypatch, lambda config, **kw: "")
    asyncio.run(client._order_gavelled(_bot(message), GAVEL))
    _note_is(monkeypatch, lambda config, **kw: "50 VET FL TX")
    monkeypatch.setattr(jobs, "order_card_update", lambda config, card_id, **kw: ("updated", []))
    asyncio.run(client._order_refresh(_bot(message), "900", settle=0))
    assert "Payra invoice note: 50 VET FL TX" in order.reads[-1]
    assert ordercards.load()["900"]["extra_notes"] == ["Payra invoice note: 50 VET FL TX"]


def test_a_card_that_turns_out_aged_moves_to_aged_leads_order_done(order, monkeypatch):
    """Declan Connolly: a Zelle post, gavelled before the thread said aged -
    made in In Que, parked in Franklin (Admin), then renamed Aged Leads."""
    order.kind = "fresh"
    message = _message("New Payment | Zelle\nNAME: Declan Connolly\nAMOUNT: $400.00\nPRODUCT: — (match to their order)")
    asyncio.run(client._order_gavelled(_bot(message), GAVEL))
    assert ordercards.load()["900"]["kind"] == "fresh"
    moved = []
    monkeypatch.setattr(jobs, "order_card_move", lambda config, card_id, kind: moved.append((card_id, kind)) or
                        "Aged Leads Order Done")
    monkeypatch.setattr(jobs, "order_card_update", lambda config, card_id, **kw: ("updated", []))
    order.kind = "aged"
    asyncio.run(client._order_refresh(_bot(message), "900", settle=0))
    assert moved == [("card1", "aged")] and ordercards.load()["900"]["kind"] == "aged"
    assert any("moved to Aged Leads Order Done" in said for said, _v in order.responder.sent)
    asyncio.run(client._order_refresh(_bot(message), "900", settle=0))
    assert len(moved) == 1, "moved again"


def test_a_503_making_a_card_is_tried_again_without_making_two(monkeypatch):
    """Blake Settle: POST /cards -> HTTP 503, and no card."""
    from datetime import datetime, timezone
    from wilbyte import trello

    monkeypatch.setattr(jobs, "CREATE_PAUSES", (0.0, 0.0, 0.0))
    now = datetime(2026, 10, 14, 23, 40, tzinfo=timezone.utc)
    stamp = f"{int(now.timestamp()):08x}" + "0" * 16

    class Flaky:
        def __init__(self, made_anyway):
            self.tries, self.made_anyway = 0, made_anyway

        def create_card(self, list_id, name, position="top"):
            self.tries += 1
            if self.tries == 1:
                raise trello.TrelloError("POST /cards -> HTTP 503: ")
            return {"id": "fresh", "shortUrl": "u2"}

        def list_cards(self, list_id):
            return [{"id": stamp, "name": "New Agent - Blake Settle", "shortUrl": "u1"}] if self.made_anyway else \
                [{"id": "0" * 24, "name": "New Agent - Blake Settle"}]

    retried = Flaky(made_anyway=False)
    assert jobs._create_once(retried, "q", "New Agent - Blake Settle", now=now)["id"] == "fresh"
    assert retried.tries == 2
    there = Flaky(made_anyway=True)
    assert jobs._create_once(there, "q", "New Agent - Blake Settle", now=now)["id"] == stamp
    assert there.tries == 1, "made a second card"

    class Refused(Flaky):
        def create_card(self, list_id, name, position="top"):
            raise trello.TrelloError("POST /cards -> HTTP 400: invalid")

    with pytest.raises(trello.TrelloError):
        jobs._create_once(Refused(False), "q", "x", now=now)


def test_notes_dont_repeat_the_fields_above_them():
    """Jad Nour Samman: "why the too much repitative" - his invoice, twice,
    under Additional Notes."""
    invoice = ("Payra invoice note: Name: Jad Nour Samman\nEmail: jsamman19@example.com\nPhone Number: +15550100019\n"
               "Lead Type: Text-Verified Veteran Leads [FRESH - $45/Lead]\nStates Leads In: Arizona (AZ) Arkansas (AR)\n"
               "Number of Requested Leads: 50\nLines: This package focuses on generating high-intent leads × 50 @ $45.00")
    thread = ("Name: Jad Nour Samman\nEmail: jsamman19@example.com\nPhone Number: +15550100019\n"
              "Lead Type: Text-Verified Veteran Leads [FRESH - $45/Lead]\nNumber of Requested Leads: 50")
    got = {"name": "Jad Nour Samman", "email": "jsamman19@example.com", "phone": "+15550100019",
           "package": "Text-Verified Veteran Leads", "leads": "50", "states": "AZ, AR"}
    assert jobs.order_fields(dict(got, notes="Name: Jad Nour Samman ..."), [invoice, thread])["notes"] == ""
    declan = "Declan Connolly\n(555) 010-3388\nCan I do an order of $400 worth of veteran aged 30 days az, mi, oh, fl\n100 Aged Vet"
    got = {"name": "Declan Connolly", "phone": "(555) 010-3388"}
    # "as it is": what the team wrote, every line of it - only a form's
    # labelled lines are the card twice.
    assert jobs.order_fields(got, [declan])["notes"] == declan
    aged = "Payra invoice note: AGED IUL / 180+ / in states IL, SC, AZ, and TX"
    assert jobs.order_fields({}, [aged])["notes"] == "From the Payra invoice: AGED IUL / 180+ / in states IL, SC, AZ, and TX"


def test_the_audit_says_what_has_a_card_and_what_was_missed(monkeypatch):
    """"did ryte created all of the cards that he can create already? did he
    not miss anything?"."""
    from datetime import datetime, timezone

    at = datetime(2026, 10, 14, 19, 0, tzinfo=timezone.utc)

    def post(i, text, gavel=False, at=at):
        return SimpleNamespace(id=i, created_at=at, jump_url=f"https://discord.com/channels/1/700/{i}",
                               content=text, embeds=[], message_snapshots=[],
                               author=SimpleNamespace(bot=True),
                               reactions=[SimpleNamespace(emoji=SimpleNamespace(name="leadlab_hammertime"))] if gavel else [])

    posts = [
        post(1, "New Payment | Payra\nNAME: Emilie Cruz\nAMOUNT: $900\nPRODUCT: VET", gavel=True),
        post(2, "New Payment | Payra\nNAME: Blake Settle\nAMOUNT: $500\nPRODUCT: VET", gavel=True),
        post(3, "New Payment | Payra\nNAME: Ryan Leach\nAMOUNT: $100\nPRODUCT: Aged Leads (VET)"),
        post(4, "New Payment | Payra\nNAME: Tee Lam\nAMOUNT: $300\nPRODUCT: IUL"),
        post(5, "New Payment | Zelle\nNAME: Declan Connolly\nAMOUNT: $400\nPRODUCT: — (match to their order)"),
        post(6, "New Payment | Payra\nNAME: Bram Gutierrez\nAMOUNT: $372\nPRODUCT: Trucker", gavel=True),
    ]
    ordercards.save({"1": {"status": "made", "url": "https://trello.com/c/emilie"},
                     "6": {"status": "made", "merged": True, "url": "https://trello.com/c/bram"}})
    cards = [{"name": "Aged Leads - Declan Connolly", "url": "https://trello.com/c/declan"}]
    lines = {i: client._audit_line(one, ordercards.load(), cards) for i, one in enumerate(posts, 1)}
    assert lines[1][0] == "ok" and "trello.com/c/emilie" in lines[1][1]
    assert lines[2][0] == "missing" and "gavelled, **no card**" in lines[2][1]
    assert lines[3][0] == "missing" and "aged" in lines[3][1]
    assert lines[4][0] == "waiting" and "not gavelled yet" in lines[4][1]
    assert lines[5][0] == "ok" and "trello.com/c/declan" in lines[5][1]
    assert lines[6][0] == "ok" and "another payment" in lines[6][1]
    # Boston Farr: aged, paid before aged cards were made automatically.
    early = post(7, "New Payment | Payra\nNAME: Boston Farr\nAMOUNT: $445.05\nPRODUCT: Aged Leads (VET)",
                 at=datetime(2026, 10, 14, 14, 23, tzinfo=timezone.utc))
    assert client._audit_line(early, ordercards.load(), cards)[0] == "check"
    archived = [{"name": "AGED LEADS - Boston Farr", "url": "https://trello.com/c/boston", "closed": True}]
    how, line = client._audit_line(early, ordercards.load(), archived)
    assert how == "ok" and "(archived)" in line
    # Keegan Sundberg's $79.18 after the $1,240 that has its card.
    ordercards.save(dict(ordercards.load(), **{"8": {"status": "made", "client": "Keegan Sundberg",
                                                      "url": "https://trello.com/c/keegan",
                                                      "made_at": "2026-10-14T18:30:00+00:00"}}))
    second = post(9, "New Payment | Payra\nNAME: Keegan Sundberg\nAMOUNT: $79.18\nPRODUCT: Text-Verified Trucker IUL")
    how, line = client._audit_line(second, ordercards.load(), [])
    assert how == "ok" and "trello.com/c/keegan" in line
    # Hayden Keltner's payment a week on: his old order's card isn't this one's.
    later = post(10, "New Payment | Payra\nNAME: Keegan Sundberg\nAMOUNT: $809.37\nPRODUCT: VET",
                 at=datetime(2026, 10, 21, 16, 23, tzinfo=timezone.utc))
    assert client._audit_line(later, ordercards.load(), [])[0] == "waiting"
    old_card = [{"id": "6ada0000" + "a" * 16, "name": "New Agent - Keegan Sundberg", "url": "https://trello.com/c/old"}]
    old_card[0]["id"] = format(int(datetime(2026, 10, 14, tzinfo=timezone.utc).timestamp()), "08x") + "a" * 16
    assert client._audit_line(later, ordercards.load(), old_card)[0] == "waiting"
    today = date(2026, 10, 15)
    assert client._audit_days("order audit", today) == [today]
    assert client._audit_days("order audit yesterday", today) == [date(2026, 10, 14)]
    assert client._audit_days("order audit 3 days", today) == [date(2026, 10, 13), date(2026, 10, 14), today]


def test_ryan_leachs_vet_payment_isnt_his_iul_order(monkeypatch):
    """Two $103.50 aged orders at 7:31 and 7:35 - VET and IUL. The VET
    payment was read off the IUL invoice and put on the IUL card."""
    from wilbyte import payraapi

    now = datetime(2026, 10, 15, 0, 0, tzinfo=timezone.utc)

    class Payra:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def changed(self, kind, after):
            who = {"first_name": "Ryan", "last_name": "Leach", "email": "ryan@example.com"}
            return {"records": [
                {"_id": "vet", "customer": who, "totals": {"total": 103.5}, "notes": "Aged VET 180+ in PA, NV",
                 "created_at": "2026-10-14T23:30:00Z"},
                {"_id": "iul", "customer": who, "totals": {"total": 103.5},
                 "notes": "Lead Type: Aged IUL - 180+ days [AGED - $1/Lead]\nStates Leads In: PA,NV",
                 "created_at": "2026-10-14T23:34:00Z"}]}

    monkeypatch.setattr(payraapi, "PayraClient", Payra)
    config = SimpleNamespace(secrets=SimpleNamespace(payra_api_token="t", payra_site_id="s"))
    vet = jobs.payra_invoice_note(config, name="Ryan Leach", email="ryan@example.com", amount="103.5",
                                  product="Aged Leads (VET)", now=now)
    iul = jobs.payra_invoice_note(config, name="Ryan Leach", email="ryan@example.com", amount="103.5",
                                  product="Aged Leads (IUL)", now=now)
    assert "VET" in vet and "IUL" not in vet
    assert "IUL" in iul
    # Not "another payment for this order" when the post says another product.
    board = Board(desc="Name: Ryan Leach\nLead Type: Aged Leads (IUL) - Aged IUL 180+ days\nStates: PA, NV")
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)
    card = {"fields": {"package": ""}, "source": "New Payment | Payra\nNAME: Ryan Leach\nPRODUCT: Aged Leads (VET)"}
    assert jobs.same_order(CONFIG, {"id": "c1"}, card) is False
    card["source"] = card["source"].replace("(VET)", "(IUL)")
    assert jobs.same_order(CONFIG, {"id": "c1"}, card) is True


def test_ordering_a_merged_payment_by_its_link_gives_it_its_own_card(order, monkeypatch):
    ordercards.save({"900": {"status": "made", "merged": True, "url": "https://trello.com/c/iul"}})
    order.existing = {"id": "iul", "name": "Aged Leads - Cole Petersen", "url": "https://trello.com/c/iul"}
    author = SimpleNamespace(id=FRANKLIN)
    monkeypatch.setattr(ordercards, "may_gavel", lambda who, config: True)
    asyncio.run(client._order_from_link(_bot(_message(thread=_notes("25 aged MTG, states AZ TX"))), order.responder,
                                        "order https://discord.com/channels/1/700/900", author))
    assert len(order.made) == 1 and order.comments == []
    held = ordercards.load()["900"]
    assert held["status"] == "made" and not held.get("merged")


def test_a_misread_doesnt_move_a_card_the_payment_says_is_aged(order, monkeypatch):
    message = _message("New Payment | Payra\nNAME: Cole Petersen\nAMOUNT: $103.5\nPRODUCT: Aged Leads (VET)",
                       thread=_notes("25 aged VET, states AZ TX"))
    asyncio.run(client._order_gavelled(_bot(message), GAVEL))
    monkeypatch.setattr(jobs, "order_card_move", lambda *a, **k: (_ for _ in ()).throw(AssertionError("moved")))
    monkeypatch.setattr(jobs, "order_card_update", lambda config, card_id, **kw: ("same", []))
    order.kind = "fresh"
    asyncio.run(client._order_refresh(_bot(message), "900", settle=0))
    assert ordercards.load()["900"]["kind"] == "aged"


def test_a_post_with_no_thread_isnt_asked_about_every_sweep(monkeypatch):
    asked = []

    async def fetch(i):
        asked.append(i)
        raise RuntimeError("404 Unknown Channel")

    client._NO_THREAD.clear()
    bot = SimpleNamespace(get_channel=lambda i: None, fetch_channel=fetch)
    assert asyncio.run(client._order_thread(bot, "950", {})) is None
    assert asyncio.run(client._order_thread(bot, "950", {})) is None
    assert asked == [950]
    client._order_note_posted(SimpleNamespace(loop=None), SimpleNamespace(channel=SimpleNamespace(id=950)))
    asyncio.run(client._order_thread(bot, "950", {}))
    assert asked == [950, 950], "a thread started later is looked for again"
    client._NO_THREAD.clear()



def test_an_aged_order_with_no_states_or_count_isnt_made_on_its_own(order, monkeypatch):
    """Matt DeFuria: an Aged Leads card with no states and no lead count,
    made on its own - and Aged Leads Order Done sends it."""
    _note_is(monkeypatch, lambda config, **kw: "")
    order.fields = {"states": "", "leads": ""}
    message = _aged_post()
    assert asyncio.run(client._aged_from_invoice(_bot(message), message, wait=0)) == "made"
    assert order.made == []
    held = ordercards.load()["950"]
    assert held["status"] == "waiting" and held["gaps"] == ["the states", "how many leads"]
    said = [one for one, _v in order.responder.sent if "No card yet" in str(one)]
    assert len(said) == 1 and "the states or how many leads" in said[0]
    # Tried again, still nothing: not said twice.
    asyncio.run(client._aged_from_invoice(_bot(message), message, wait=0))
    assert len([one for one, _v in order.responder.sent if "No card yet" in str(one)]) == 1
    # The thread says it: the card's made.
    order.fields = {}
    message.thread = _notes("25 aged IUL, AZ TX")
    asyncio.run(client._order_retry(_bot(message), "950", settle=0))
    assert [one[0] for one in order.made] == ["aged"] and ordercards.load()["950"]["status"] == "made"


def test_a_gavelled_aged_order_with_no_states_is_held_for_a_person(order):
    order.fields = {"states": ""}
    asyncio.run(client._order_gavelled(_bot(_message()), GAVEL))
    assert order.made == []
    [(said, view)] = [one for one in _said(order) if "Not made yet" in str(one[0])]
    assert "the states" in said and view is not None


def test_the_sweep_tries_a_waiting_aged_order_again(monkeypatch):
    now = datetime.now(timezone.utc)
    ordercards.save({"950": {"status": "waiting", "message_channel_id": "700", "gaps": ["the states"],
                             "waiting_since": (now - timedelta(hours=1)).isoformat(),
                             "tried_at": (now - timedelta(minutes=31)).isoformat()},
                     "951": {"status": "waiting", "message_channel_id": "700",
                             "waiting_since": (now - timedelta(hours=1)).isoformat(),
                             "tried_at": (now - timedelta(minutes=5)).isoformat()}})
    tried = []

    async def retry(bot, key, *, settle=30):
        tried.append(key)

    monkeypatch.setattr(client, "_order_retry", retry)
    asyncio.run(client._order_sweep(SimpleNamespace()))
    assert tried == ["950"]


def _thread_said(i, name, starter_text, starter_bot=False, history=()):
    starter = SimpleNamespace(id=i, content=starter_text, embeds=[], attachments=[], message_snapshots=[],
                              author=SimpleNamespace(display_name="Kharyl", name="kharyl", bot=starter_bot),
                              created_at=WHEN)

    class Parent:
        async def fetch_message(self, mid):
            return starter

    class Thread:
        def __init__(self):
            self.id, self.name, self.parent, self.last_message_id = i, name, Parent(), 99

        def history(self, **kw):
            async def gen():
                for at, text in enumerate(history):
                    yield SimpleNamespace(id=at, content=text, embeds=[], attachments=[], message_snapshots=[],
                                          author=SimpleNamespace(display_name="Faith", name="faith", bot=False),
                                          created_at=WHEN)
            return gen()
    return Thread()


def test_a_new_thread_about_an_agent_is_read_onto_their_card(order, monkeypatch):
    """"there are new thread on cards that are already setup that ryte didnt
    udpdate - ava and zach for example"."""
    made = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ordercards.save({"900": {"status": "made", "client": "Ava Lindqvist", "made_at": made, "card_id": "c1",
                             "message_channel_id": "700", "url": "https://trello.com/c/ava"},
                     "901": {"status": "made", "client": "Zach Moreno", "made_at": made, "card_id": "c2"}})
    thread = _thread_said(5000, "Ava Lindqvist - add CA", "Ava Lindqvist wants CA added", history=["also no Sundays"])
    refreshed = []

    async def refresh(bot, key, *, settle=None):
        refreshed.append(key)

    monkeypatch.setattr(client, "_order_refresh", refresh)
    bot = _bot(_message())
    bot.loop = asyncio.new_event_loop()
    try:
        assert bot.loop.run_until_complete(client._order_link_thread(bot, thread)) == "900"
        bot.loop.run_until_complete(asyncio.sleep(0))
    finally:
        bot.loop.close()
    assert ordercards.load()["900"]["threads"] == ["5000"] and refreshed == ["900"]
    assert any("New thread" in str(one) for one, _v in order.responder.sent)
    # Read with the order: the thread's first message and what's said in it.
    found = {"5000": thread}

    async def get(bot_, key, o):
        return found.get(key)

    monkeypatch.setattr(client, "_order_thread", get)
    _t, _i, _tid, notes = asyncio.run(client._order_said(bot, _message(), threads=("5000",)))
    assert notes == ["Ava Lindqvist wants CA added", "also no Sundays"]


def test_a_payment_posts_own_thread_isnt_taken_for_an_older_card(order):
    """Same person, another payment - its thread is that order's, not a note
    on the card they already have."""
    made = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ordercards.save({"900": {"status": "made", "client": "Ryan Leach", "made_at": made, "card_id": "c1"}})
    thread = _thread_said(5001, "Ryan Leach", "New Payment | Payra\nNAME: Ryan Leach\nPRODUCT: Aged Leads (VET)",
                          starter_bot=True)
    assert asyncio.run(client._order_link_thread(_bot(_message()), thread)) is None
    assert not ordercards.load()["900"].get("threads")


def test_a_thread_names_their_newest_card_and_needs_their_whole_name():
    now = datetime.now(timezone.utc)
    held = {"1": {"status": "made", "client": "Zach Moreno", "made_at": (now - timedelta(days=3)).isoformat()},
            "2": {"status": "made", "client": "Zach Moreno", "made_at": (now - timedelta(days=1)).isoformat()},
            "3": {"status": "made", "client": "Zach Moreno", "made_at": now.isoformat(), "merged": True},
            "4": {"status": "made", "client": "Pasha", "made_at": now.isoformat()}}
    assert client._order_named_thread(held, "Zach Moreno - schedule change") == "2"
    assert client._order_named_thread(held, "Zach - schedule") is None
    assert client._order_named_thread(held, "Pasha") is None


def test_reach_out_tomorrow_isnt_a_launch_date():
    """Emilie Cruz: "@Therese @Nicole @Faith reach out tomorrow" - her card
    said Launch Date: Thursday, October 15."""
    thread = ("[10/14 07:44PM] LeadLab: New Payment | Payra\nNAME: Emilie Cruz\nPRODUCT: VET\n"
              "[10/14 07:46PM] Tre Tarpley: Name: Emilie Cruz\nNumber of Requested Leads: 25\n"
              "States Leads In: Illinois, South Carolina, Michigan\nreach out tomorrow")
    assert not ordercards.says_when(thread)
    assert ordercards.says_when(thread + "\nlive tomorrow")
    assert ordercards.says_when(thread + "\nLaunch Date: Friday 10/16")
    assert ordercards.says_when("First week of leads (starts Tuesday, Oct 20)")
    assert not ordercards.says_when("call him Monday")


def test_a_weekly_subscription_charge_isnt_waiting_on_a_card():
    from datetime import datetime, timezone

    def post(text):
        return SimpleNamespace(id=1, created_at=datetime(2026, 10, 15, 14, 7, tzinfo=timezone.utc),
                               jump_url="https://discord.com/channels/1/700/1", content=text, embeds=[],
                               message_snapshots=[], author=SimpleNamespace(bot=True), reactions=[])

    renewal = post("New Payment | Spark\nNAME: Mark Medina\nAMOUNT: $350.00\n"
                   "PRODUCT: 1 × Lead Subscription (at $350.00 / week)\nsubscription")
    first = post("New Payment | Spark\nNAME: Colin Briner\nAMOUNT: $700.00\n"
                 "PRODUCT: First week of leads (starts Tuesday, Oct 20)\nsubscription")
    assert client._audit_line(renewal, {}, [])[0] == "subs"
    assert client._audit_line(first, {}, [])[0] == "waiting"


def test_same_states_last_time_goes_on_as_written(order, monkeypatch):
    """"if it said same states as last time, just put it as is and accept
    it as states" - Mohammad Kanan's card was held for having no states."""
    message = _message("New Payment | Zelle\nNAME: Kanan Kapital Llc\nAMOUNT: $1,000.00\nPRODUCT: Aged Leads (VET)",
                       thread=_notes("200 Aged Vets\nSame states last time\n30-90"))

    def reading(config, *, text, images, today, notes=()):
        order.reads.append(text)
        return {"kind": "aged", "client": "Mohammad Kanan", "name": "Aged Leads - Mohammad Kanan", "desc": "d",
                "fields": {"name": "Mohammad Kanan", "states": "", "leads": "200", "package": "Aged Leads (VET)"},
                "source": text + "\n200", "missing": ["States"]}

    monkeypatch.setattr(jobs, "read_order", reading)
    asyncio.run(client._order_gavelled(_bot(message), GAVEL))
    [(kind, name, desc, _new)] = order.made
    assert kind == "aged" and "States: Same states last time" in desc


def test_saying_the_same_states_is_recognised():
    for said in ("Same states last time", "same states as before", "states same as last order",
                 "States: same as last time"):
        assert ordercards.SAME_STATES.search(said), said
    assert not ordercards.SAME_STATES.search("States: TX, FL")


def test_a_new_aged_card_goes_at_the_bottom_of_its_list(monkeypatch):
    """"when creating a new aged leads card, JUST THE AGED LEADS, put them
    below the list"."""
    board = Board()
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)
    jobs.order_card_make(CONFIG, "aged", {"name": "Aged Leads - Cole Petersen", "desc": "d"})
    jobs.order_card_make(CONFIG, "fresh", {"name": "New Agent - Cole Petersen", "desc": "d"})
    assert board.positions == ["bottom", "top"]


def test_a_split_order_adds_up_and_either_lead_type_is_the_payments():
    """Gabriela Samaan: "50 Aged VET 180+ / 50 Aged Mortgage Protection 180+"
    held as "100 isn't in the order"."""
    source = ("[10/15 01:30PM] LeadLab: New Payment | Payra\nNAME: Gabriela Samaan\nAMOUNT: $103.5\n"
              "PRODUCT: Aged Leads (MTG)\n[10/15 01:31PM] Kharyl: 50 Aged VET 180+\n50 Aged Mortgage Protection 180+\nIL")
    card = {"kind": "aged", "source": source,
            "fields": {"states": "IL", "leads": "100 (50 + 50)",
                       "package": "50 Aged VET 180+ + 50 Aged Mortgage Protection 180+"}}
    assert ordercards.doubts(card, today=date(2026, 10, 15))[0] == []
    card["fields"]["leads"] = "120"
    assert any("120 isn't in the order" in one for one in ordercards.doubts(card, today=date(2026, 10, 15))[0])
    card["fields"].update(leads="100", package="50 Aged FEX + 50 Aged IUL")
    assert any("which one did they buy" in one for one in ordercards.doubts(card, today=date(2026, 10, 15))[0])


def test_counts_on_one_line_add_up_too():
    """Kurtis Kunz: "25 LA and 25 PA 90-180 day" held as "50 isn't in the order"."""
    source = ("[10/15 01:38PM] LeadLab: New Payment | Payra\nNAME: Kurtis Kunz\nAMOUNT: $51.75\n"
              "PRODUCT: Aged Leads (VET)\n[10/15 01:39PM] Kharyl: 25 LA and 25 PA 90-180 day\n180+ DAYS:\n"
              "District of Columbia · EverLife agency VET 180+: 3 available\nNew Mexico · EverLife agency VET 180+: "
              "7 available\nTotal: 15\nEVERLIFE")
    card = {"kind": "aged", "source": source,
            "fields": {"states": "LA, PA", "leads": "50", "package": "Aged Leads (VET)"}}
    assert ordercards.doubts(card, today=date(2026, 10, 15))[0] == []
    card["fields"]["leads"] = "10"
    assert any("10 isn't in the order" in one for one in ordercards.doubts(card, today=date(2026, 10, 15))[0]), \
        "3 + 7 from the list of what's available counted as the order"


def test_paid_separately_is_a_card_of_its_own(order):
    """"Always make seprate card for now, unless its paid at the same time. if
    paid separately. create card"."""
    import discord

    order.kind = "fresh"
    order.existing = {"id": "5f0000000000000000000001", "name": "New Agent - Bram Gutierrez",
                      "url": "https://trello.com/c/bram"}
    an_hour_later = discord.utils.time_snowflake(discord.utils.snowflake_time(900) + timedelta(hours=1))
    ordercards.save({str(an_hour_later): {"status": "made", "card_id": "5f0000000000000000000001"}})
    asyncio.run(client._order_gavelled(_bot(_message()), GAVEL))
    assert order.comments == [] and [one[0] for one in order.made] == ["fresh"]
    # A card RYTE didn't make from a payment isn't one to put another on.
    ordercards.save({})
    order.made.clear()
    asyncio.run(client._order_gavelled(_bot(_message()), GAVEL))
    assert order.comments == []


@pytest.mark.parametrize("said, amount, package", [
    ("$1000 ad spend\n1 week only\nLiftoff Ascend\nLive Friday Oct 16", "1000", "Ascend Plus"),
    ("PHX Standard 350 a week", "362.25", "PHNX Standard"),
    ("Spark ad spend $500/week", "517.50", "Spark Standard"),
    ("Phoenix $1,500 ad spend", "1552.50", "PHNX Plus"),
    ("ad spend, starts monday", "700", ""),
])
def test_ad_spend_is_the_agency_and_its_tier(said, amount, package):
    """"for people who's doing ad spend they are usually from Phnx,
    Ascend/Liftoff, and spark ... less than 1000 usd per week ... Standard ...
    1000 and above, it will be plus"."""
    assert jobs.ad_spend_package(said, amount) == package


def test_an_agency_the_order_doesnt_name_is_caught():
    """Matthew Odierno's card said Phoenix; his order said Liftoff Ascend."""
    card = {"kind": "fresh", "launch_iso": "2026-10-16",
            "source": "New Payment | Payra\nNAME: Matthew Odierno\nPRODUCT: —\n$1000 ad spend\nLiftoff Ascend\nlive Friday",
            "fields": {"states": "", "package": "Phoenix ad spend ($1000, 1 week)", "leads": "No cap (ad spend)"}}
    wrong, _missing = ordercards.doubts(card, today=date(2026, 10, 15))
    assert any("the card says PHNX" in one for one in wrong)
    card["fields"]["package"] = "Ascend Plus"
    assert not any("Lead type" in one for one in ordercards.doubts(card, today=date(2026, 10, 15))[0])


def test_the_order_saying_more_than_the_payment_line_isnt_another_product():
    """Leonardo Lopez: "Aged Leads (IUL)" paid, "50 Aged IUL Spanish (30-90)"
    on his invoice - flagged as "which one did they buy?"."""
    card = {"kind": "aged", "source": "New Payment | Payra\nNAME: Leonardo Lopez\nPRODUCT: Aged Leads (IUL)\n"
                                      "Payra invoice note: 50 Aged IUL Spanish (30-90) TX",
            "fields": {"states": "TX", "leads": "50", "package": "50 Aged IUL Spanish (30-90)"}}
    assert not any("Lead type" in one for one in ordercards.doubts(card, today=date(2026, 10, 15))[0])
    card["fields"]["package"] = "50 Aged VET"
    assert any("which one did they buy" in one for one in ordercards.doubts(card, today=date(2026, 10, 15))[0])


def test_an_aged_card_isnt_told_it_has_no_launch_date(order):
    asyncio.run(client._order_gavelled(_bot(_message(thread=_notes("25 aged MTG, states AZ TX"))), GAVEL))
    assert not any("No launch date" in str(one) for one, _v in order.responder.sent)


def test_paid_but_not_gavelled_is_said_with_the_unticked_reminder(monkeypatch):
    """"do it when we do unticked reminder"."""
    from datetime import datetime, timezone

    now = datetime(2026, 10, 15, 21, 30, tzinfo=timezone.utc)

    def post(i, text, at, gavel=False):
        return SimpleNamespace(id=i, created_at=at, jump_url=f"https://discord.com/channels/1/700/{i}", content=text,
                               embeds=[], message_snapshots=[], author=SimpleNamespace(bot=True),
                               reactions=[SimpleNamespace(emoji=SimpleNamespace(name="leadlab_hammertime"))] if gavel else [])

    posts = [post(1, "New Payment | Payra\nNAME: Matthew Haines\nAMOUNT: $828", datetime(2026, 10, 15, 16, 51, tzinfo=timezone.utc)),
             post(2, "New Payment | Payra\nNAME: Seth Moler\nAMOUNT: $362.25", datetime(2026, 10, 15, 20, 43, tzinfo=timezone.utc)),
             post(3, "New Payment | Payra\nNAME: Mark Medina\nPRODUCT: 1 × Lead Subscription\nsubscription",
                  datetime(2026, 10, 15, 14, 7, tzinfo=timezone.utc)),
             post(4, "New Payment | Payra\nNAME: Jace Jensen\nAMOUNT: $2036.88", datetime(2026, 10, 15, 14, 33, tzinfo=timezone.utc))]

    async def payments(bot, day):
        return posts

    monkeypatch.setattr(client, "_payments_on", payments)
    monkeypatch.setattr(jobs, "order_cards_since", lambda config, since: [])
    ordercards.save({"4": {"status": "made", "url": "https://trello.com/c/jace"}})
    bot = SimpleNamespace(config=SimpleNamespace(schedule=SimpleNamespace(timezone="America/New_York")))
    monkeypatch.setattr(client, "_today", lambda cfg: date(2026, 10, 15))
    said = asyncio.run(client._ungavelled_note(bot, now=now))
    assert "Matthew Haines" in said and "Seth Moler" not in said, "under two hours old"
    assert "Mark Medina" not in said and "Jace Jensen" not in said


def test_phx_plus_two_point_oh_is_one_order_phoenix_plus():
    """Samuel Riggins' "PHX + 2.0" was left off the Lead Order card: "“PHX”
    could be Phoenix Standard or Plus" and "“2.0” doesn't match any checklist"."""
    from wilbyte import agents

    assert agents.order_parts("PHX + 2.0") == ["PHX 2.0"]
    assert agents.tier_of("PHX 2.0") == "plus"
    assert agents.tier_of("PHX +") == "plus" and agents.tier_of("Phoenix+") == "plus"
    assert agents.order_parts("15 OTP VETS + 15 OTP FEX") == ["15 OTP VETS", "15 OTP FEX"]
    assert agents.tier_of("Phoenix Standard") == "standard"


def test_an_edited_note_updates_the_card_too(monkeypatch):
    """Trey Autry: "LIVE TOM, OCT 16" added to Kharyl's note by editing it,
    after the card was made - the card never heard."""
    ordercards.save({"950": {"status": "made", "thread_id": "950", "card_id": "c"}})
    started = []

    class Loop:
        def create_task(self, coroutine):
            started.append(coroutine)
            coroutine.close()
            return SimpleNamespace(cancel=lambda: None)

    growth = SimpleNamespace(id=1)
    monkeypatch.setattr(client, "_in_setup_guild", lambda bot, guild: guild is growth)
    fake = SimpleNamespace(user=SimpleNamespace(id=RYTE), loop=Loop(), get_guild=lambda i: growth,
                           get_channel=lambda i: SimpleNamespace(id=950, parent=object(), name="@here"))
    asyncio.run(client.WilByteBot.on_raw_message_edit(fake, SimpleNamespace(
        guild_id=1, channel_id=950, message_id=7, data={"author": {"id": "5", "bot": False}})))
    assert len(started) == 1
    asyncio.run(client.WilByteBot.on_raw_message_edit(fake, SimpleNamespace(
        guild_id=1, channel_id=950, message_id=8, data={"author": {"id": str(RYTE), "bot": True}})))
    assert len(started) == 1, "RYTE's own edits aren't notes"
    client._ORDER_PENDING.clear()


def test_payras_line_blurb_doesnt_make_an_invoice_another_product():
    """Frank Nocera: his FEX invoice's line said "The leads may include people
    interested in Indexed Universal Life, Final Expense" and was taken for IUL."""
    invoice = ("Name: frank nocera\nLead Type: Aged Final Expense - 180+ days [AGED - $1/Lead]\nStates Leads In: TX,FL\n"
               "Number of Requested Leads: 100\nLines: This payment is for a list of aged leads provided by Agent Lead "
               "Lab. The leads may include people interested in Indexed Universal Life, Final Expense × 100 @ $1.00")
    assert not jobs._other_product("Aged Leads (FEX)", invoice)
    assert jobs._other_product("Aged Leads (VET)", invoice)
    assert not jobs._other_product("Aged Leads (IUL)", "Lines: interested in Indexed Universal Life, Final Expense")
    assert jobs._other_product("Aged Leads (VET)", "Aged IUL 180+ in PA, NV")


def test_payra_is_read_once_and_shared_and_left_alone_when_it_says_wait(monkeypatch):
    """"HTTP 429: Access to this resource has been throttled" - every card,
    recheck and bank check was reading Payra afresh."""
    from wilbyte import payraapi

    asked = []

    class Payra:
        throttled = False

        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def changed(self, kind, after):
            asked.append(kind)
            if Payra.throttled:
                raise payraapi.PayraError("Payra said HTTP 429: Access to this resource has been throttled.")
            return {"records": []}

    monkeypatch.setattr(payraapi, "PayraClient", Payra)
    monkeypatch.setattr(payraapi, "load", lambda *a, **k: {"invoices": {"saved": {"id": "saved"}}})
    config = SimpleNamespace(secrets=SimpleNamespace(payra_api_token="t", payra_site_id="s"))
    jobs.payra_recent(config, "invoices")
    jobs.payra_recent(config, "invoices")
    assert asked == ["invoices"], "read twice inside a minute and a half"
    jobs._PAYRA_RECENT.clear()
    Payra.throttled = True
    assert jobs.payra_recent(config, "invoices") == {"saved": {"id": "saved"}}
    Payra.throttled = False
    assert jobs.payra_recent(config, "invoices") == {"saved": {"id": "saved"}}
    assert asked == ["invoices", "invoices"], "asked again while Payra said to wait"


def test_all_of_whats_available_adds_up():
    """Alexis Lesme: "180+ days ALL" over the stock list, and 37 more 90-180."""
    source = ("[10/16 12:18PM] LeadLab: New Payment | Payra\nNAME: Alexis Lesme\nAMOUNT: $308.95\nPRODUCT: Aged Leads (VET)\n"
              "[10/16 12:23PM] Kharyl: Alexis Lesme\n180+ days ALL\nGeorgia · AEP agency VET 180+: 1 available\n"
              "Texas · AEP agency VET 180+: 34 available\nMichigan · AEP agency VET 180+: 4 available\n"
              "Mississippi · AEP agency VET 180+: 130 available\n\n90-180 days-- 37 leads fl, ga, tx, va, oh, mi, ms\nAEP")
    card = {"kind": "aged", "source": source,
            "fields": {"states": "GA, TX, MI, MS, FL, VA, OH", "leads": "206", "package": "Aged VET"}}
    assert not any("Number of leads" in one for one in ordercards.doubts(card, today=date(2026, 10, 16))[0])
    card["fields"]["leads"] = "169"
    assert not any("Number of leads" in one for one in ordercards.doubts(card, today=date(2026, 10, 16))[0])
    card["fields"]["leads"] = "300"
    assert any("Number of leads" in one for one in ordercards.doubts(card, today=date(2026, 10, 16))[0])


def test_all_of_whats_available_is_a_count_on_the_card(monkeypatch):
    """"it says here 37 leads?" - and 169 more of the 180+: no total written
    down, so the count was left blank and the card held."""
    import sys
    import types

    class Anthropic:
        def __init__(self, **kw):
            self.messages = SimpleNamespace(create=lambda **kw: "reply")

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=Anthropic))
    monkeypatch.setattr(jobs, "_tool_input", lambda response, name: {
        "kind": "aged", "name": "Alexis Lesme", "states": "GA, TX, MI, MS, FL, VA, OH",
        "package": "Aged Leads (VET) 180+ and 90-180 days", "leads": ""})
    config = SimpleNamespace(secrets=SimpleNamespace(anthropic_api_key="k", require=lambda *a: None),
                             copy=SimpleNamespace(model="claude-opus-5-5"))
    text = ("[10/16 12:18PM] LeadLab: New Payment | Payra\nNAME: Alexis Lesme\nPHONE: +15550100000\nAMOUNT: $308.95\n"
            "PRODUCT: Aged Leads (VET)\n[10/16 12:23PM] Kharyl: Alexis Lesme\n180+ days ALL\n"
            "Georgia · AEP agency VET 180+: 1 available\nTexas · AEP agency VET 180+: 34 available\n"
            "Michigan · AEP agency VET 180+: 4 available\nMississippi · AEP agency VET 180+: 130 available\n\n"
            "90-180 days-- 37 leads fl, ga, tx, va, oh, mi, ms\nAEP")
    card = jobs.read_order(config, text=text, images=[], today=date(2026, 10, 16))
    assert card["fields"]["leads"] == "206"
    assert not any("Number of leads" in one for one in ordercards.doubts(card, today=date(2026, 10, 16))[0])
    assert ordercards.all_counted("25 VET leads\nTX, FL") is None, "nothing asked for ALL"


def test_the_invoice_form_fills_what_the_reading_left_out(monkeypatch):
    """Mackenzie Ho: "States Leads In: all but new york" and "Lead Type: Aged
    Veteran - 180+ days" on the invoice; the card had no states and the
    product line's "Text-Verified Aged Veteran Leads"."""
    import sys
    import types

    class Anthropic:
        def __init__(self, **kw):
            self.messages = SimpleNamespace(create=lambda **kw: "reply")

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=Anthropic))
    monkeypatch.setattr(jobs, "_tool_input", lambda response, name: {
        "kind": "aged", "name": "Mackenzie Ho", "states": "", "email": "mackenzie@example.com",
        "package": "Text-Verified Aged Veteran Leads", "leads": "300"})
    config = SimpleNamespace(secrets=SimpleNamespace(anthropic_api_key="k", require=lambda *a: None),
                             copy=SimpleNamespace(model="claude-opus-5-5"))
    text = ("[10/20 04:04PM] LeadLab: New Payment | Payra\nNAME: Mackenzie Ho\nEMAIL: mackenzie@example.com\n"
            "AMOUNT: $310.50\nPRODUCT: Text-Verified Aged Veteran Leads\n"
            "**INVOICE:** INV-20278 · card •••• 2146 · 10/20/2026 4:04 PM\n**Subtotal:** $300.00\n**Notes:**\n"
            "> Name: Mackenzie Ho\n> Email: mackenzie@example.com\n> Phone Number: +15555550833\n"
            "> Lead Type: Aged Veteran - 180+ days [AGED - $1/Lead]\n> States Leads In: all but new york\n"
            "> Number of Requested Leads: 300\n**Internal Notes:**\n> (none)")
    card = jobs.read_order(config, text=text, images=[], today=date(2026, 10, 20))
    assert card["fields"]["states"] == "All states except NY"
    assert card["fields"]["package"] == "Aged Veteran - 180+ days"
    assert ordercards.doubts(card, today=date(2026, 10, 20))[0] == []
    assert client._aged_gaps(card) == []


def test_bailey_lawrence_bank_and_two_launch_days(monkeypatch):
    """"so what the heck is this? are we supposed to guess this" - Agency:
    BANK, Tre's "live tomorrow" kept over Faith's later "BANK - live Monday
    October 26", and the thread run into the invoice's Internal Notes with
    its tags."""
    import sys
    import types

    class Anthropic:
        def __init__(self, **kw):
            self.messages = SimpleNamespace(create=lambda **kw: "reply")

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=Anthropic))
    monkeypatch.setattr(jobs, "_tool_input", lambda response, name: {
        "kind": "fresh", "name": "Bailey Lawrence", "states": "IA, SC, TN, WY", "agency": "BANK",
        "package": "Text-Verified Veteran Leads", "leads": "40", "launch": "tomorrow",
        "launch_iso": "2026-10-21"})
    config = SimpleNamespace(secrets=SimpleNamespace(anthropic_api_key="k", require=lambda *a: None),
                             copy=SimpleNamespace(model="claude-opus-5-5"))
    text = ("[10/20 09:02AM] LeadLab: New Payment | Payra\nNAME: Bailey Lawrence\nAMOUNT: $400\n"
            "PRODUCT: Text-Verified Veteran Leads\n**INVOICE:** INV-20111\n**Notes:**\n"
            "> States: IA, SC, TN, WY\n> Number of Requested Leads: 40\n**Internal Notes:**\n> *(none)*\n"
            "[10/20 11:25AM] Tre Tarpley: live tomorrow <@1478905541001613444>\n"
            "[10/20 03:29PM] Faith: <@956658038188683264> <@1413148328375619665>\n"
            "BANK - live Monday October 26")
    notes = ["live tomorrow", "BANK - live Monday October 26", "BANK - live Monday October 26"]
    card = jobs.read_order(config, text=text, images=[], today=date(2026, 10, 20), notes=notes)
    fields = card["fields"]
    assert fields["agency"] == ""
    assert card["launch_iso"] == "2026-10-26" and fields["launch"] == "Monday, October 26"
    assert card["launch_conflict"] == ["2026-10-21", "2026-10-26"]
    assert any("Which is it?" in one for one in ordercards.doubts(card, today=date(2026, 10, 20))[0])
    assert "<@" not in fields["notes"] and "[10/20" not in fields["notes"] and "(none)" not in fields["notes"]
    assert fields["notes"].count("BANK - live Monday October 26") == 1


def test_a_daily_cap_isnt_how_many_they_bought():
    """Paul Miller: "Number of Leads Requested: 5" for the 24 trucker IULs he
    bought - "CAP 5" is his most a day."""
    source = ("[10/21 11:38AM] Nova: New Payment | Payra\nNAME: Paul Miller\nAMOUNT: $1192.32\n"
              "PRODUCT: Text-Verified Trucker IUL Leads\n**Notes:**\n> States: TX, NM\n> CAP 5\n"
              "> Schedule: 8 am to 8 pm EST\n> NO SUNDAYS\n"
              "[10/21 11:39AM] Faith: 24-trucker IULs\nHe wants to have a cap of 5 leads per day")
    card = {"kind": "fresh", "source": source, "launch_iso": "",
            "fields": {"name": "Paul Miller", "states": "TX, NM", "package": "Text-Verified Trucker IUL Leads",
                       "leads": "5"}}
    wrong = ordercards.doubts(card, today=date(2026, 10, 21))[0]
    assert any("5 is the daily cap" in one and "CAP 5" in one for one in wrong)
    card["fields"]["leads"] = "24"
    assert not any("daily cap" in one for one in ordercards.doubts(card, today=date(2026, 10, 21))[0])
    assert ordercards.only_a_cap("10", "Lead Type: 10 OTP VETS\n10/day") == "", "a count said as a count too"


@pytest.mark.parametrize("kind", ["fresh", "aged"])
def test_a_zelle_payment_says_so_in_the_notes(monkeypatch, kind):
    """"add on additional notes on the cards both aged and fresh that if they
    are zelle payments"."""
    import sys
    import types

    class Anthropic:
        def __init__(self, **kw):
            self.messages = SimpleNamespace(create=lambda **kw: "reply")

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=Anthropic))
    monkeypatch.setattr(jobs, "_tool_input", lambda response, name: {
        "kind": kind, "name": "Ramsey Traboulsi", "states": "MO", "package": "VET", "leads": "25"})
    config = SimpleNamespace(secrets=SimpleNamespace(anthropic_api_key="k", require=lambda *a: None),
                             copy=SimpleNamespace(model="claude-opus-5-5"))
    text = ("[10/16 01:46PM] Nova: @Luna Zelle payment received — Ramsey Traboulsi $2,000.00\n"
            "New Payment | Zelle 💰\n**NAME:** Ramsey Traboulsi\n**AMOUNT:** $2,000.00\n**METHOD:** Zelle (Chase)\n"
            "**PRODUCT:** — (match to their order)")
    card = jobs.read_order(config, text=text, images=[], today=date(2026, 10, 16),
                           notes=["he will send schedule"])
    assert card["fields"]["notes"].startswith("Paid via Zelle (Chase)")
    assert "he will send schedule" in card["fields"]["notes"]
    assert "Paid via Zelle (Chase)" in card["desc"]
    payra = text.replace("New Payment | Zelle", "New Payment | Payra").replace("**METHOD:** Zelle (Chase)\n", "")
    assert "Zelle" not in jobs.read_order(config, text=payra, images=[], today=date(2026, 10, 16))["fields"].get("notes", "")


# ------------------------------------------------------- agency from #lead-lab

REQUESTS = [
    "@Faith\nNew Lead Request\n**Name:** Cheikh Niang\n**Email:** cheikh@example.com\n**Phone Number:** +18435550912\n"
    "**Lead Type:** Aged Final Expense - 90-180 days [AGED - $3/Lead]\n**Agency:** Wolfpack\n**Discount Code:** LABAGED30",
    "@Faith\nNew Lead Request\n**Name:** alejandro jimenez\n**Email:** alejandro@example.com\n"
    "**Phone Number:** +15205550342\n**Agency:** FFL\n**Discount Code:**",
    "@Faith\nNew Lead Request\n**Name:** Talen Hinesley\n**Email:** talen@example.com\n**Agency:**\n",
]


def test_the_lead_request_is_found_by_email_phone_or_name():
    """"its because he doesnt know where to get them ... they are here on
    the lead lab channel"."""
    by_email = "NAME: Somebody Else\nEMAIL: CHEIKH@example.com\nAMOUNT: $150"
    assert jobs.request_agency(jobs.lead_request_for(REQUESTS, by_email)) == "Wolfpack"
    by_phone = "NAME: A J\nPHONE: +1 (520) 555-0342"
    assert jobs.request_agency(jobs.lead_request_for(REQUESTS, by_phone)) == "FFL"
    by_name = "**NAME:** Alejandro  Jimenez\n**AMOUNT:** $300"
    assert jobs.request_agency(jobs.lead_request_for(REQUESTS, by_name)) == "FFL"
    assert jobs.lead_request_for(REQUESTS, "NAME: Nobody Here\nEMAIL: no@example.com") == ""
    assert jobs.request_agency(jobs.lead_request_for(REQUESTS, "NAME: Talen Hinesley")) == "", "blank says nothing"


@pytest.mark.parametrize("kind", ["fresh", "aged"])
def test_the_agency_goes_on_the_card_from_the_request(monkeypatch, kind):
    import sys
    import types

    class Anthropic:
        def __init__(self, **kw):
            self.messages = SimpleNamespace(create=lambda **kw: "reply")

    said = {"kind": kind, "name": "Cheikh Niang", "states": "SC", "package": "Aged FEX", "leads": "50", "agency": ""}
    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=Anthropic))
    monkeypatch.setattr(jobs, "_tool_input", lambda response, name: dict(said))
    config = SimpleNamespace(secrets=SimpleNamespace(anthropic_api_key="k", require=lambda *a: None),
                             copy=SimpleNamespace(model="claude-opus-5-5"))
    text = "New Payment | Payra\nNAME: Cheikh Niang\nEMAIL: cheikh@example.com\nAMOUNT: $150"
    card = jobs.read_order(config, text=text, images=[], today=date(2026, 10, 16), request=REQUESTS[0])
    assert card["fields"]["agency"] == "Wolfpack" and "Agency: Wolfpack" in card["desc"]
    assert not any("gency" in one for one in ordercards.doubts(card, today=date(2026, 10, 16))[0])
    # What the team wrote in the thread wins.
    said["agency"] = "AEP"
    card = jobs.read_order(config, text=text, images=[], today=date(2026, 10, 16), request=REQUESTS[0])
    assert card["fields"]["agency"] == "AEP"


def test_lead_lab_is_read_once_for_a_few_minutes(monkeypatch):
    read = []

    class Channel:
        def history(self, limit):
            read.append(limit)

            async def posts():
                for one in REQUESTS:
                    yield SimpleNamespace(content=one, embeds=[])
            return posts()

    bot = SimpleNamespace(config=SimpleNamespace(secrets=SimpleNamespace(discord_lead_request_channel_id="123")),
                          get_channel=lambda one: Channel() if one == 123 else None)
    monkeypatch.setattr(client, "_LEAD_REQUESTS", [0.0, []])
    assert asyncio.run(client._lead_requests(bot)) == REQUESTS
    assert asyncio.run(client._lead_requests(bot)) == REQUESTS
    assert read == [client.LEAD_REQUESTS_READ]


def test_an_agency_on_a_line_of_its_own_is_the_agency():
    """"still no agency" - Gavin Mathieu's notes end "aep"."""
    notes = "[10/16 03:10PM] Kharyl: 30-90 Aged Vet Leads\nSC,FL,AL,GA,LA\n100 aged vet\n\naep"
    assert jobs.agency_said(notes) == "AEP"
    assert jobs.agency_said("180+ days ALL\nAEP @Nicole") == "AEP"
    assert jobs.agency_said("Agency: wolfpack") == "Wolfpack"
    assert jobs.agency_said("Navy Mutual", agencies=("Navy Mutual",)) == "Navy Mutual", "one #lead-lab has named"
    assert jobs.agency_said("100 aged vet\nSC,FL") == ""
    assert jobs.agency_said("40 leads\n\nAEP LIVE  MON, OCT 19") == "AEP", "Collin Williams"
    assert jobs.agency_said("Spark is great") == ""
    assert jobs.agency_said("he's with aep but wants ffl leads") == "", "only a line that's the name alone"


def test_the_card_gets_the_agency_the_notes_name_alone(monkeypatch):
    import sys
    import types

    class Anthropic:
        def __init__(self, **kw):
            self.messages = SimpleNamespace(create=lambda **kw: "reply")

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=Anthropic))
    monkeypatch.setattr(jobs, "_tool_input", lambda response, name: {
        "kind": "aged", "name": "Gavin Mathieu", "states": "SC, FL, AL, GA, LA", "package": "Aged Vet Leads 30-90 days",
        "leads": "100", "agency": ""})
    config = SimpleNamespace(secrets=SimpleNamespace(anthropic_api_key="k", require=lambda *a: None),
                             copy=SimpleNamespace(model="claude-opus-5-5"))
    notes = ["30-90 Aged Vet Leads\nSC,FL,AL,GA,LA\n100 aged vet\n\naep"]
    card = jobs.read_order(config, text="NAME: Gavin Mathieu\nPRODUCT: Aged Leads (VET)\n" + notes[0], images=[],
                           today=date(2026, 10, 16), notes=notes, request=REQUESTS[0])
    assert card["fields"]["agency"] == "AEP", "the team's word beats the request"
    assert "Agency: AEP" in card["desc"]
    assert not ordercards.doubts(card, today=date(2026, 10, 16))[0]


@pytest.mark.parametrize("iso", ["2026-10-17", "2026-10-16", ""])
def test_the_launch_days_name_comes_from_the_date(monkeypatch, iso):
    """"tomorrow is saturday" - "lets push for launch tomorrow" on Friday the
    16th was written "Friday, October 17"."""
    import sys
    import types

    class Anthropic:
        def __init__(self, **kw):
            self.messages = SimpleNamespace(create=lambda **kw: "reply")

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=Anthropic))
    monkeypatch.setattr(jobs, "_tool_input", lambda response, name: {
        "kind": "fresh", "name": "Nicolas Sanmiguel", "states": "TX", "package": "VET", "leads": "35",
        "launch": "Friday, October 17", "launch_iso": iso})
    config = SimpleNamespace(secrets=SimpleNamespace(anthropic_api_key="k", require=lambda *a: None),
                             copy=SimpleNamespace(model="claude-opus-5-5"))
    notes = ["figure out when he wants setup - lets push for launch tomorrow if possible"]
    card = jobs.read_order(config, text="[10/16 03:30PM] Faith: " + notes[0], images=[], today=date(2026, 10, 16),
                           notes=notes)
    assert card["fields"]["launch"] == "Saturday, October 17" and card["launch_iso"] == "2026-10-17"
    assert "Launch Date: Saturday, October 17" in card["desc"]


def test_a_request_posted_as_a_thread_is_read_too(monkeypatch):
    """Nova Cloud posts some requests as a thread - "Riley Noah · Aged IUL -
    30-90 days [AGED - $8/Lead]" - with the details inside it."""
    def history_of(posts):
        def history(limit, oldest_first=False):
            async def each():
                for one in posts:
                    yield SimpleNamespace(content=one, embeds=[])
            return each()
        return history

    thread = SimpleNamespace(id=1481313804640981157 + 10, name="Riley Noah · Aged IUL - 30-90 days [AGED - $8/Lead]",
                             history=history_of(["Email: riley@example.com\nAgency: Symmetry"]))

    class Channel:
        threads = [thread]
        history = staticmethod(history_of(["Nova Cloud started a thread"]))

        def archived_threads(self, limit):
            async def each():
                yield thread
            return each()

    bot = SimpleNamespace(config=SimpleNamespace(secrets=SimpleNamespace(discord_lead_request_channel_id="123")),
                          get_channel=lambda one: Channel())
    monkeypatch.setattr(client, "_LEAD_THREADS", {})
    posts = asyncio.run(client._lead_requests(bot))
    assert len(posts) == 2
    request = jobs.lead_request_for(posts, "NAME: Riley Noah\nPRODUCT: Aged Leads (IUL)")
    assert jobs.request_agency(request) == "Symmetry"
    assert jobs.request_agency(jobs.lead_request_for(posts, "EMAIL: RILEY@example.com")) == "Symmetry"


def test_agency_lookup_says_what_lead_lab_has(monkeypatch):
    """"then why isnt he adding the agency on new agent as well?" - ask."""
    from wilbyte.bot import mentions

    async def requests(bot, **kw):
        return REQUESTS

    monkeypatch.setattr(client, "_lead_requests", requests)
    assert mentions.parse("agency Cheikh Niang").action == "agencylookup"
    said = asyncio.run(client._agency_lookup(None, "Cheikh Niang"))
    assert "Wolfpack" in said and "A card for them gets" in said
    assert "Nothing there names them" in asyncio.run(client._agency_lookup(None, "Heath McGalliard"))
    assert "(blank)" in asyncio.run(client._agency_lookup(None, "Talen Hinesley"))


def test_a_slow_lead_lab_read_never_holds_a_card_up(monkeypatch):
    """"it stopped and now not filing cards" - the first read went through
    hundreds of threads while every order waited."""
    started = []

    async def slow(bot):
        started.append(1)
        await asyncio.sleep(30)
        return REQUESTS

    monkeypatch.setattr(client, "_lead_requests_read", slow)
    monkeypatch.setattr(client, "LEAD_REQUESTS_WAIT", 0.05)
    monkeypatch.setattr(client, "LEAD_REQUESTS_FIRST_WAIT", 0.05)

    async def two_orders():
        return await asyncio.gather(client._lead_requests(None), client._lead_requests(None))

    assert asyncio.run(asyncio.wait_for(two_orders(), 2)) == [[], []]
    assert started == [1], "one read, not one per order"


def test_only_the_agency_comes_from_the_request(monkeypatch):
    """Collin Williams: "States: the order also says IN, MD, NC - not on the
    card" - those were in his #lead-lab request, not his order."""
    import sys
    import types

    class Anthropic:
        def __init__(self, **kw):
            self.messages = SimpleNamespace(create=lambda **kw: "reply")

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=Anthropic))
    monkeypatch.setattr(jobs, "_tool_input", lambda response, name: {
        "kind": "fresh", "name": "Collin Williams", "email": "collin@example.com", "phone": "+15155550341",
        "states": "IA, KS, MO, IL, TX, TN, WV", "package": "Text-Verified Fresh Vets", "leads": "40",
        "launch": "Monday, October 19", "launch_iso": "2026-10-19", "agency": ""})
    config = SimpleNamespace(secrets=SimpleNamespace(anthropic_api_key="k", require=lambda *a: None),
                             copy=SimpleNamespace(model="claude-opus-5-5"))
    text = ("New Payment | Payra\nNAME: Collin Williams\nEMAIL: collin@example.com\nPRODUCT: VET\n"
            "[10/16 04:10PM] Kharyl: can i please have 40 text verified fresh vets. Starting Monday the 19th at 8AM.\n"
            "IA, KS, MO, IL, TX, TN, WV\n40 leads")
    request = ("New Lead Request\nName: Collin Williams\nEmail: collin@example.com\n"
               "States Leads In: IN, MD, NC\nAgency: Symmetry")
    card = jobs.read_order(config, text=text, images=[], today=date(2026, 10, 16), request=request)
    assert card["fields"]["agency"] == "Symmetry"
    assert ordercards.doubts(card, today=date(2026, 10, 16))[0] == []


def test_the_invoices_own_wording_doesnt_add_states():
    """Collin Williams, held for "IN, MD, NC" - not in his thread."""
    source = ("New Payment | Payra\nNAME: Collin Williams\nPRODUCT: VET\n"
              "[10/16 04:10PM] Kharyl: can i please have 40 text verified fresh vets. Starting Monday the 19th at 8AM.\n"
              "IA, KS, MO, IL, TX, TN, WV\n40 leads\n\nAEP LIVE  MON, OCT 19\n"
              "Payra invoice note: 40 vets\nDescription: Veteran leads - IN, MD, NC and more\n"
              "Lines: Text-Verified Veteran Leads IN, MD, NC")
    card = {"kind": "fresh", "source": source, "launch_iso": "2026-10-19",
            "fields": {"states": "IA, KS, MO, IL, TX, TN, WV", "package": "Text-Verified Fresh Vets", "leads": "40",
                       "launch": "Monday, October 19"}}
    assert not any("also says" in one for one in ordercards.doubts(card, today=date(2026, 10, 16))[0])
    # An invoice note listing states, and nothing in the thread: it counts.
    card["source"] = "New Payment | Payra\nPRODUCT: VET\nPayra invoice note: IA, KS, MO, IL, TX, TN, WV, NC"
    said = ordercards.doubts(card, today=date(2026, 10, 16))[0]
    assert any("also says NC" in one and "Payra invoice note" in one for one in said)


def test_a_blank_agency_is_filled_in_after(monkeypatch, tmp_path):
    """Aidan Moskovitz: his card was made while #lead-lab was still being
    read, and his request said Nonstop Financial."""
    desc = ("Name: Aidan Moskovitz\nEmail: aidan@example.com\nLead Type: Aged IUL - 30-90 days\n"
            "Number of Leads Requested: 50\nStates: FL, SC\nAgency:\nAdditional Notes:")
    board = {"c1": desc}

    class Board:
        def card_detail(self, card_id):
            return {"desc": board[card_id]}

        def set_description(self, card_id, text):
            board[card_id] = text

        def close(self):
            pass

    monkeypatch.setattr(jobs, "open_trello", lambda cfg: Board())
    held = {"k": {"status": "made", "card_id": "c1", "client": "Aidan Moskovitz", "url": "u", "written": desc,
                  "made_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "fields": {"name": "Aidan Moskovitz", "email": "aidan@example.com", "agency": ""}}}
    monkeypatch.setattr(ordercards, "load", lambda: held)
    monkeypatch.setattr(ordercards, "save", lambda data: held.update(data))
    request = ("New Lead Request\nName: Aidan Moskovitz\nEmail: aidan@example.com\n"
               "States Leads In: FL, SC\nAgency: Nonstop Financial\nDiscount Code: LABAGED30")

    async def requests(bot, **kw):
        return [request]

    monkeypatch.setattr(client, "_lead_requests", requests)
    filled = asyncio.run(client._agency_fill(SimpleNamespace(config=None)))
    assert filled and "Nonstop Financial" in filled[0]
    assert "Agency: Nonstop Financial\nAdditional Notes:" in board["c1"]
    assert held["k"]["written"] == board["c1"], "still RYTE's writing"
    assert asyncio.run(client._agency_fill(SimpleNamespace(config=None))) == [], "once"
    # An agency somebody wrote in isn't changed.
    board["c2"] = desc.replace("Agency:", "Agency: AEP")
    assert jobs.order_card_fill_agency(None, "c2", "Nonstop Financial") == ""


def test_lead_lab_is_found_by_name_when_the_id_doesnt_work():
    """"what are we doing" - Roman Edwards' request (Agency: Genesis) was
    right there and his card came out blank."""
    lead_lab = SimpleNamespace(id=5, name="🧪┃lead-lab", history=lambda **kw: None)
    other = SimpleNamespace(id=6, name="lead-order", history=lambda **kw: None)
    bot = SimpleNamespace(config=SimpleNamespace(secrets=SimpleNamespace(discord_lead_request_channel_id="999")),
                          get_channel=lambda one: None,
                          guilds=[SimpleNamespace(text_channels=[other, lead_lab])])
    assert client._lead_lab_channel(bot) is lead_lab


def test_posts_with_nothing_in_them_are_said(monkeypatch):
    class Channel:
        name, id = "lead-lab", 5

        def history(self, limit):
            async def each():
                for _ in range(3):
                    yield SimpleNamespace(content="", embeds=[])
            return each()

    bot = SimpleNamespace(config=SimpleNamespace(secrets=SimpleNamespace(discord_lead_request_channel_id="5")),
                          get_channel=lambda one: Channel())
    asyncio.run(client._lead_requests_read(bot))
    assert "Message Content" in client._LEAD_TROUBLE[0]


def test_two_invoices_paid_in_one_charge_each_go_to_their_own_payment(monkeypatch):
    """"not right!!!!" - Ahmed Ghazi's INV-20039 (225 leads, $815.06) and
    INV-20040 (200 leads, $724.50), paid together; Payra posted $787.50 and
    $724.5, and both cards read the 200."""
    from wilbyte import payraapi

    now = datetime(2026, 10, 16, 21, 0, tzinfo=timezone.utc)
    data = {"invoices": {
        "i39": {"id": "i39", "name": "Ahmed Ghazi", "email": "ahmed@example.com", "total": 815.06,
                "notes": "Lead Type: Aged Veteran - 30-90 days\nNumber of Requested Leads: 225",
                "created": "2026-10-16T20:30:00Z"},
        "i40": {"id": "i40", "name": "Ahmed Ghazi", "email": "ahmed@example.com", "total": 724.50,
                "notes": "Lead Type: Aged Veteran - 30-90 days\nNumber of Requested Leads: 200",
                "created": "2026-10-16T20:31:00Z"}}}
    monkeypatch.setattr(jobs, "payra_recent", lambda config, kind, now=None: data["invoices"])
    config = SimpleNamespace(secrets=SimpleNamespace(payra_api_token="t", payra_site_id="s"))
    note, first = jobs.payra_invoice_pick(config, name="Ahmed Ghazi", email="ahmed@example.com", amount="$787.50",
                                          product="Aged Leads (VET)", now=now)
    assert first == "i39" and "225" in note, "nearest total, not the newest"
    note, second = jobs.payra_invoice_pick(config, name="Ahmed Ghazi", email="ahmed@example.com", amount="$724.5",
                                           product="Aged Leads (VET)", now=now, taken={first})
    assert second == "i40" and "200" in note
    # Both taken: nothing, rather than one already on a card.
    assert jobs.payra_invoice_pick(config, name="Ahmed Ghazi", email="ahmed@example.com", amount="$724.5",
                                   now=now, taken={"i39", "i40"}) == ("", "")


def _note_is(monkeypatch, note):
    """The invoice note every payment gets, in a test."""
    monkeypatch.setattr(jobs, "payra_invoice_note", note)
    monkeypatch.setattr(jobs, "payra_invoice_pick", lambda config, **kw: (
        note(config, **{key: value for key, value in kw.items() if key not in ("taken", "number", "paid_at")}), ""))


def test_two_invoices_are_two_cards_even_paid_together(monkeypatch):
    """"for ahmed, two cards"."""
    held = {"1560750000000000000": {"status": "made", "card_id": "c1", "invoice_id": "i40"},
            "1560750000000000001": {"invoice_id": "i39"},
            "1560750000000000002": {"invoice_id": "i40"}}
    monkeypatch.setattr(ordercards, "load", lambda: held)
    assert client._own_invoice({"id": "c1"}, "1560750000000000001")
    assert not client._own_invoice({"id": "c1"}, "1560750000000000002"), "the same invoice paid twice"
    assert not client._own_invoice({"id": "c1"}, "1560750000000000009"), "no invoice known: as before"


def test_a_reply_to_a_deleted_message_is_said_in_the_channel():
    import discord as discord_lib

    from wilbyte.bot.responders import MessageResponder

    sent = []

    class Gone(discord_lib.HTTPException):
        def __init__(self):
            self.code, self.status, self.text = 50035, 400, "In message_reference: Unknown message"

        def __str__(self):
            return self.text

    async def reply(**kw):
        raise Gone()

    async def send(**kw):
        sent.append(kw.get("content"))

    message = SimpleNamespace(author=SimpleNamespace(id=1), channel=SimpleNamespace(id=2, send=send), reply=reply)
    asyncio.run(MessageResponder(message).send("done"))
    assert sent == ["done"]


def test_lead_lab_is_read_whole_once_then_only_whats_new(monkeypatch):
    asked = []

    class Channel:
        name, id = "lead-lab", 5

        def history(self, limit=None, after=None):
            asked.append((limit, getattr(after, "id", None)))

            async def each():
                if after is None:
                    yield SimpleNamespace(id=10, created_at=None, content="", embeds=[
                        SimpleNamespace(title="New Lead Request", description="Name: A B\\nAgency: FFL", fields=[],
                                        url=None, footer=None, author=None)])
                else:
                    yield SimpleNamespace(id=11, created_at=None, content="Agency: AEP", embeds=[])
            return each()

    bot = SimpleNamespace(config=SimpleNamespace(secrets=SimpleNamespace(discord_lead_request_channel_id="5")),
                          get_channel=lambda one: Channel())
    asyncio.run(client._lead_requests_read(bot))
    posts = asyncio.run(client._lead_requests_read(bot))
    assert asked == [(client.LEAD_REQUESTS_READ, None), (None, 10)]
    assert len(posts) == 2


def test_the_teams_launch_day_beats_the_invoices(monkeypatch):
    """"where did you get MONDAY???????" - Michael Panza's thread said "Live
    Saturday October 17"."""
    import sys
    import types

    class Anthropic:
        def __init__(self, **kw):
            self.messages = SimpleNamespace(create=lambda **kw: "reply")

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=Anthropic))
    monkeypatch.setattr(jobs, "_tool_input", lambda response, name: {
        "kind": "fresh", "name": "Michael Panza", "states": "AL, AZ", "package": "OTP IUL", "leads": "20",
        "launch": "Monday, October 19", "launch_iso": "2026-10-19"})
    config = SimpleNamespace(secrets=SimpleNamespace(anthropic_api_key="k", require=lambda *a: None),
                             copy=SimpleNamespace(model="claude-opus-5-5"))
    notes = ["Payra invoice note: 20 OTP IUL - start date 10/19/2026",
             "Michael Panza\n20 OTP IUL Leads\nStates: AL, AZ\n\nLive Saturday October 17"]
    card = jobs.read_order(config, text="NAME: Michael Panza\n" + "\n".join(notes), images=[],
                           today=date(2026, 10, 16), notes=notes)
    assert card["fields"]["launch"] == "Saturday, October 17"


@pytest.mark.parametrize("bracket", ["(GA and NJ if available)", "(GA & NJ sold out)", "(GA, NJ"])
def test_states_in_brackets_dont_hold_a_card(bracket):
    """Marcus Uhrich: "States Leads In: FL, ... TX (GA ...". """
    states = "FL, IA, ID, IL, IN, MA, MD, MI, MN, NC, OH, PA, SC, TN, TX"
    card = {"kind": "fresh", "source": f"New Payment | Payra\nNAME: Marcus Uhrich\nStates Leads In: {states} {bracket}\n"
                                       "Live today Friday October 16",
            "launch_iso": "2026-10-16",
            "fields": {"states": states, "package": "Text-Verified Veteran", "leads": "27",
                       "launch": "Friday, October 16"}}
    assert not any("also says" in one for one in ordercards.doubts(card, today=date(2026, 10, 16))[0])


def test_paid_by_somebody_else_is_held():
    """"this was name tony moderno, but the notes said Preston bridges ... why
    didnt ryte flag it"."""
    source = ("[10/16 05:48PM] LeadLab: New Payment | Payra\nNAME: Tony Moderno\nEMAIL: tony@example.com\n"
              "PRODUCT: MTG\n[10/16 05:50PM] Faith: Preston Bridges\nIL, UT, AZ\n54 OTP MTG Standard\nlive monday")
    card = {"kind": "fresh", "source": source, "launch_iso": "2026-10-19",
            "fields": {"name": "Preston Bridges", "states": "IL, UT, AZ", "package": "OTP MTG Standard",
                       "leads": "54", "launch": "Monday, October 19"}}
    wrong = ordercards.doubts(card, today=date(2026, 10, 16))[0]
    assert any("paid by Tony Moderno" in one and "Preston Bridges" in one for one in wrong)
    assert ordercards.same_person("Mike Smith", "Michael Smith")
    assert ordercards.same_person("alejandro jimenez", "Alejandro Jimenez")
    assert not ordercards.same_person("Tony Moderno", "Preston Bridges")


def test_notes_about_somebody_else_dont_rewrite_the_card(order, monkeypatch):
    """Tony Moderno's card was rewritten as Preston Bridges' from the notes."""
    message = _message(thread=_notes("25 aged MTG"))
    asyncio.run(client._order_gavelled(_bot(message), GAVEL))
    calls = []
    monkeypatch.setattr(jobs, "order_card_update", lambda *a, **kw: calls.append(1) or ("updated", []))
    reading = jobs.read_order

    def somebody_else(config, **kw):
        card = reading(config, **kw)
        return {**card, "client": "Preston Bridges", "name": "New Agent - Preston Bridges"}

    monkeypatch.setattr(jobs, "read_order", somebody_else)
    assert asyncio.run(client._order_refresh(_bot(message), "900", settle=0)) == "other person"
    assert calls == []
    assert "about **Preston Bridges**, not Cole Petersen" in order.responder.sent[-1][0]
    asyncio.run(client._order_refresh(_bot(message), "900", settle=0))
    assert sum("Preston Bridges" in one[0] for one in order.responder.sent) == 1, "said once"


# ------------------------------------------------- aged and new-agent together

PATRICK = ("[10/16 05:34PM] LeadLab: New Payment | Payra\nNAME: Patrick Moreno\nEMAIL: ops@example.com\n"
           "AMOUNT: $12091.9\nPRODUCT: Text-Verified Trucker IUL\n"
           "[10/16 06:35PM] Faith: Name: Patrick Moreno\nALL AGED VETS DELIVERED TODAY\n"
           "Lead Type: 5,659 Aged Vets 180+\nStates: NationWide\nLead Type: 11,029 Aged IULs 180+\nStates: Nation Wide\n"
           "Lead Type: 50 Aged Vets 30-90\nStates: TX\nLead Type: 100 Aged IULs 30-90\nStates: 25 NC 25 GA 25 FL 25 TX\n"
           "OTP VETS - waiting for states\nLead Type: 34 OTP Vets\nStates: WAIT FOR STATES IN GROUP MESSAGE\n"
           "Lead Type: 25 OTP IULs\nStates: AL AZ CA CO CT FL GA KY LA MA MD ME NC NM OH OK OR PA SC TN TX VA WA WI\n"
           "Live on Sunday October 18\n@Nicole @Therese")
PATRICK_PARTS = [
    {"kind": "aged", "package": "Aged Vets 180+", "leads": "5659", "states": "Nationwide", "launch_iso": ""},
    {"kind": "aged", "package": "Aged IULs 180+", "leads": "11029", "states": "Nationwide", "launch_iso": ""},
    {"kind": "aged", "package": "Aged Vets 30-90", "leads": "50", "states": "TX", "launch_iso": ""},
    {"kind": "aged", "package": "Aged IULs 30-90", "leads": "100", "states": "25 NC, 25 GA, 25 FL, 25 TX",
     "launch_iso": ""},
    {"kind": "fresh", "package": "OTP Vets", "leads": "34", "states": "", "launch_iso": ""},
    {"kind": "fresh", "package": "25 OTP IULs", "leads": "25",
     "states": "AL, AZ, CA, CO, CT, FL, GA, KY, LA, MA, MD, ME, NC, NM, OH, OK, OR, PA, SC, TN, TX, VA, WA, WI",
     "launch_iso": "2026-10-18"},
]


def _patrick():
    return {"kind": "fresh", "client": "Patrick Moreno", "source": PATRICK, "parts": [dict(one) for one in PATRICK_PARTS],
            "fields": {"name": "Patrick Moreno", "email": "ops@example.com", "phone": "+13055550397",
                       "agency": "", "notes": "ALL AGED VETS DELIVERED TODAY\n\nOTP VETS - waiting for states\n\n"
                                              "Live on Sunday October 18"}}


def test_aged_and_new_agent_leads_split_into_their_cards():
    """"yes build it that way" - one Aged Leads card with every aged line, a
    New Agent card for each new-agent line."""
    from wilbyte import agents

    aged, vets, iuls = jobs.split_order(_patrick())
    assert aged["kind"] == "aged" and aged["name"] == "Aged Leads - Patrick Moreno"
    assert aged["fields"]["leads"] == "16,838 (5,659 + 11,029 + 50 + 100)"
    assert "Aged Vets 30-90: TX" in aged["fields"]["states"]
    # "ON AGED LEADS, DONT ADD THE FRESH ORDER ON THE DESCRIPTION, but on
    # fresh add both".
    assert "• 5,659 Aged Vets 180+ — Nationwide" in aged["desc"] and "OTP Vets" not in aged["desc"]
    for one in (vets, iuls):
        assert agents.AGED_REFERENCE in one["desc"] and "• 5,659 Aged Vets 180+ — Nationwide" in one["desc"]
    assert "ALL AGED VETS DELIVERED TODAY" in aged["desc"]
    assert "• 100 Aged IULs 30-90 — 25 NC, 25 GA, 25 FL, 25 TX" in aged["desc"], "25 each, as Faith wrote it"
    assert "waiting for states" not in aged["desc"] and "Sunday" not in aged["desc"]
    assert "OTP IULs" not in aged["desc"] and "25 25" not in aged["desc"]
    assert vets["kind"] == "fresh" and vets["fields"]["leads"] == "34" and not vets["fields"]["states"]
    assert not vets["fields"]["launch"], "waits for its states and its date"
    assert "No states yet" in vets["desc"]
    assert iuls["fields"]["launch"] == "Sunday, October 18" and iuls["fields"]["states"].startswith("AL, AZ")
    assert "Sunday" not in vets["desc"], "the other line's date isn't this card's"
    # Each New Agent card reads as the one order it is.
    for one, wanted in ((vets, "OTP Vets"), (iuls, "OTP IULs")):
        agent = agents.read_agent({"id": "5f0000000000000000000001", "name": one["name"], "desc": one["desc"],
                                   "shortUrl": "u"}, text=one["desc"], today=date(2026, 10, 16))
        assert agent.lead_type == wanted and agent.stated == wanted
        assert len(agents.dated_orders(one["desc"], today=date(2026, 10, 16))) <= 1
    assert agents.find_launch(iuls["desc"], today=date(2026, 10, 16)) == date(2026, 10, 18)
    assert agents.find_launch(vets["desc"], today=date(2026, 10, 16)) is None
    assert jobs.split_doubts([aged, vets, iuls], today=date(2026, 10, 16)) == []


def test_invoice_notes_on_a_split_card_arent_called_the_threads():
    """Skyler Liljenquist: "From the thread:" over "From the Payra invoice:"."""
    card = _patrick()
    card["fields"]["notes"] = "From the Payra invoice: Florida · EverLife agency MTG: 20 available"
    aged = jobs.split_order(card, today=date(2026, 10, 16))[0]
    assert "From the thread:" not in aged["desc"] and "From the Payra invoice:" in aged["desc"]
    card["fields"]["notes"] = "Kharyl: send them early"
    assert "From the thread:" in jobs.split_order(card, today=date(2026, 10, 16))[0]["desc"]


def test_a_split_new_agent_card_keeps_the_setup_notes():
    """Syed Tahir Ahmed: the GHL location id, the API key and "fresh leads go
    to this pipeline" only on the Aged card - "therese and nicole have
    different setup"."""
    card = {"kind": "fresh", "client": "Syed Tahir Ahmed",
            "source": "NAME: Syed Tahir Ahmed\nPRODUCT: Text-Verified Veteran leads\nTexas, Hawaii, California\n"
                      "85 aged 90-180 veteran leads\n20 fresh OTP VETS\nLive Wednesday Oct 21",
            "launch_iso": "2026-10-21",
            "parts": [{"kind": "aged", "package": "Aged Veteran leads 90-180", "leads": "85", "states": "TX, HI, CA",
                       "launch_iso": ""},
                      {"kind": "fresh", "package": "OTP VETS", "leads": "20", "states": "TX, HI, CA",
                       "launch_iso": "2026-10-21"}],
            "fields": {"name": "Syed Tahir Ahmed", "notes": (
                "From the Payra invoice: abc123 here's the ghl location id\npit-us-0000 That's the api key\n"
                "In the crm\nAs in like 140 aged 90-180 veteran leads. And then 20 fresh\n"
                "Highest intent pipeline for the fresh leads - fresh leads go to this pipeline\n"
                "Texas, Hawaii, California\nabc123 here's the ghl location id\npit-us-0000 That's the api key\n"
                "In the crm\nAs in like 85 aged 90-180 veteran leads\nAnd then 20 fresh OTP VETS\n"
                "Highest intent pipeline for the fresh leads - fresh leads go to this pipeline\n"
                "Live Wednesday Oct 21")}}
    aged, fresh = jobs.split_order(card, today=date(2026, 10, 21))
    notes = fresh["fields"]["notes"]
    assert "ghl location id" in notes and "api key" in notes and "fresh leads go to this pipeline" in notes
    assert notes.count("ghl location id") == 1, "once, not the invoice's and the thread's"
    assert "aged" not in notes.casefold().replace("part of a bigger order", "") or "Aged Leads card" in notes
    assert "140" not in notes and "Live Wednesday" not in notes and "Texas, Hawaii" not in notes
    assert aged["fields"]["notes"].count("ghl location id") == 1


def test_a_lines_count_made_of_its_states_counts_adds_up():
    """Skyler Liljenquist: "32 leads isn't in the order" - FL 18 + VA 14."""
    source = ("NAME: Skyler Liljenquist\nPRODUCT: Aged Leads (MTG)\n"
              "Florida · EverLife agency MTG: 20 available (30-90: 18 · 90-180: 1 · 180+: 1)\n"
              "Virginia · EverLife agency MTG: 14 available (30-90: 14 · 90-180: 0 · 180+: 0)")
    card = {"kind": "aged", "client": "Skyler Liljenquist", "source": source, "launch_iso": "",
            "parts": [{"kind": "aged", "package": "Aged Mortgage Protection 30-90", "leads": "32",
                       "states": "FL 18, VA 14", "launch_iso": ""},
                      {"kind": "aged", "package": "Aged Mortgage Protection 90-180", "leads": "1",
                       "states": "FL 1", "launch_iso": ""},
                      {"kind": "aged", "package": "Aged Mortgage Protection 180+", "leads": "1",
                       "states": "FL 1", "launch_iso": ""}],
            "fields": {"name": "Skyler Liljenquist", "notes": ""}}
    cards = jobs.split_order(card, today=date(2026, 10, 21))
    assert jobs.split_doubts(cards, today=date(2026, 10, 21)) == []
    card["parts"][0]["leads"] = "33"
    assert any("33 leads isn't in the order" in one
               for one in jobs.split_doubts(jobs.split_order(card, today=date(2026, 10, 21)), today=date(2026, 10, 21)))


def test_one_kind_isnt_split_but_aged_lines_keep_their_own_states():
    card = _patrick()
    card["parts"] = [one for one in card["parts"] if one["kind"] == "aged"]
    made = jobs.split_order(card)
    assert [one["kind"] for one in made] == ["aged"], "one Aged Leads card, each line with its own states"
    card["parts"] = [one for one in _patrick()["parts"] if one["kind"] == "fresh"]
    assert jobs.split_order(card) == []
    card["parts"] = []
    assert jobs.split_order(card) == []


def test_a_split_line_that_isnt_in_the_order_is_held():
    card = _patrick()
    card["parts"][4]["leads"] = "43"
    assert any("43 leads isn't in the order" in one for one in jobs.split_doubts(jobs.split_order(card), today=date(2026, 10, 16)))


def test_a_split_order_gets_a_single_cards_checks():
    """"make sure that this task ryte doing is not gonna cost us anymore
    money": split cards went round the lead type, launch and agency checks,
    and an aged line with no states went out with none."""
    card = _patrick()
    card["parts"][2]["states"] = ""
    wrong = jobs.split_doubts(jobs.split_order(card, today=date(2026, 10, 16)), today=date(2026, 10, 16))
    assert any("no states for Aged Vets 30-90" in one for one in wrong)

    card = _patrick()
    card["parts"][5]["package"] = "25 OTP IULs PHNX"
    wrong = jobs.split_doubts(jobs.split_order(card, today=date(2026, 10, 16)), today=date(2026, 10, 16))
    assert any("the card says PHNX but the order doesn't name PHNX" in one for one in wrong)

    cards = jobs.split_order(_patrick(), today=date(2026, 10, 16))
    assert any("too far from today" in one for one in jobs.split_doubts(cards, today=date(2026, 12, 20)))

    card = _patrick()
    card["source"] = card["source"].replace("PRODUCT: Text-Verified Trucker IUL", "PRODUCT: Aged Leads (VET)")
    card["parts"] = [one for one in card["parts"] if one["kind"] == "fresh"]
    card["parts"].append(dict(card["parts"][0]))
    assert jobs.split_order(card) == [], "two fresh lines aren't split"
    fresh_only = [dict(one, kind="fresh") for one in jobs.split_order(_patrick(), today=date(2026, 10, 16))[1:]]
    for one in fresh_only:
        one["source"] = card["source"]
    assert any("aged - but no card here is aged" in one for one in jobs.split_doubts(fresh_only, today=date(2026, 10, 16)))


def test_a_gavelled_split_order_makes_each_card(order, monkeypatch):
    order.kind = "fresh"

    def reading(config, **kw):
        return {**_patrick(), "name": "New Agent - Patrick Moreno", "desc": "d", "missing": [], "launch_iso": ""}

    monkeypatch.setattr(jobs, "read_order", reading)
    asyncio.run(client._order_gavelled(_bot(_message()), GAVEL))
    assert [one[0] for one in order.made] == ["aged", "fresh", "fresh"]
    held = ordercards.load()["900"]
    assert held["split"] and len(held["parts"]) == 3 and held["status"] == "made"
    said = order.responder.sent[-1][0]
    assert "split into 3 cards" in said and "states to come" in said


def test_new_notes_on_a_split_order_update_each_card(order, monkeypatch):
    """The 34 OTP Vets' states posted in the thread: onto their card."""
    order.kind = "fresh"
    reads = [_patrick()]

    def reading(config, **kw):
        return {**reads[-1], "name": "New Agent - Patrick Moreno", "desc": "d", "missing": [], "launch_iso": ""}

    monkeypatch.setattr(jobs, "read_order", reading)
    message = _message(thread=_notes("states for the vets: TX, FL"))
    asyncio.run(client._order_gavelled(_bot(message), GAVEL))
    later = _patrick()
    later["parts"][4]["states"] = "TX, FL"
    later["source"] += "\nstates for the vets: TX, FL"
    reads.append(later)
    calls = []

    def updating(config, card_id, *, written, desc, name="", commented=()):
        calls.append(desc)
        return ("updated", []) if written != desc else ("same", [])

    monkeypatch.setattr(jobs, "order_card_update", updating)
    assert asyncio.run(client._order_refresh(_bot(message), "900", settle=0)) == "updated"
    assert len(calls) == 3
    held = ordercards.load()["900"]
    assert held["parts"][1]["fields"]["states"] == "TX, FL"
    assert "OTP Vets" in order.responder.sent[-1][0]


def test_an_aged_payment_minutes_old_is_on_its_way_not_missing():
    """Jaden Schomer: "missing" at 7:35 for a 7:34 payment."""
    from datetime import datetime, timezone

    just = SimpleNamespace(id=9, created_at=datetime.now(timezone.utc), jump_url="https://discord.com/channels/1/700/9",
                           content="New Payment | Payra\nNAME: Jaden Schomer\nAMOUNT: $103.5\nPRODUCT: Aged Leads (IUL)",
                           embeds=[], message_snapshots=[], author=SimpleNamespace(bot=True), reactions=[])
    how, line = client._audit_line(just, {}, [])
    assert how == "waiting" and "on its way" in line


def test_a_new_agent_payment_isnt_read_off_an_aged_invoice():
    """"is this really aged leads?" - Riley Noah's Trucker IUL payment and his
    Aged IUL invoice from the day before."""
    aged = "Name: Riley Noah\nLead Type: Aged IUL - 30-90 days [AGED - $8/Lead]\nNumber of Requested Leads: 40"
    assert jobs._other_product("Text-Verified Trucker IUL", aged)
    assert not jobs._other_product("Aged Leads (IUL)", aged)
    fresh = "Lead Type: Text-Verified Veteran Leads [FRESH - $45/Lead]"
    assert jobs._other_product("Aged Leads (VET)", fresh)
    assert not jobs._other_product("VET", fresh)
    assert not jobs._other_product("Aged Leads (VET)", "180+ vets TX FL"), "an aged note needn't say aged"


# ------------------------------------------- "gone for 1 and a half days"

def test_the_catch_up_goes_back_to_when_ryte_was_last_running(order, monkeypatch):
    now = datetime(2026, 10, 19, 21, 0, tzinfo=timezone.utc)
    asked = []

    class Place:
        id = 700
        last_message_id = int(((now.timestamp() - 60) * 1000 - 1420070400000)) << 22

        def history(self, **kw):
            asked.append(kw)

            async def gen():
                return
                yield
            return gen()

    async def threads():
        return []

    guild = SimpleNamespace(id=1, text_channels=[Place()], active_threads=threads)
    monkeypatch.setattr(client, "_setup_guild", lambda bot, config: guild)

    async def no_wait(_s):
        return None

    monkeypatch.setattr(client.asyncio, "sleep", no_wait)
    bot = _bot(_message())
    bot._was_alive = now - timedelta(hours=36)
    asyncio.run(client._catch_up_gavels(bot, now=now))
    assert asked[0]["after"] == now - timedelta(hours=36, minutes=30)
    assert asked[0]["limit"] >= 5000
    bot._was_alive = now - timedelta(days=9)
    asked.clear()
    asyncio.run(client._catch_up_gavels(bot, now=now))
    assert asked[0]["after"] == now - client.GAVEL_CATCH_UP_MOST, "never more than four days"
    # Away mid-run - the Mac asleep - for longer than a day: from then.
    bot._was_alive = now - timedelta(days=2, hours=1)
    asked.clear()
    asyncio.run(client._catch_up_gavels(bot, now=now, gone=now - timedelta(days=3)))
    assert asked[0]["after"] == now - timedelta(days=3, minutes=30)


def test_an_aged_payment_read_back_twice_waits_once(order, monkeypatch):
    """A reconnect reads back over the same aged payment while its two
    minutes are still running: one card, not two."""
    made = []

    async def gavelled(bot, payload):
        made.append(payload.message_id)

    async def invoice(*a, **k):
        return None

    monkeypatch.setattr(client, "_order_gavelled", gavelled)
    monkeypatch.setattr(client, "_invoice_for", invoice)
    message = _message()

    async def both():
        return await asyncio.gather(client._aged_from_invoice(_bot(message), message, wait=0.01),
                                    client._aged_from_invoice(_bot(message), message, wait=0.01))

    assert sorted(asyncio.run(both())) == ["already", "made"]
    assert len(made) == 1


def test_a_card_made_by_hand_while_away_is_used_not_doubled(order, monkeypatch):
    order.kind = "fresh"
    hand = {"id": "6a00ffff00000000000000aa", "name": "New Agent - Cole Petersen", "shortUrl": "https://trello.com/c/HAND"}
    monkeypatch.setattr(jobs, "card_made_since", lambda config, client_name, since, kind="": hand)
    payload = SimpleNamespace(**{**vars(GAVEL), "catching_up": True})
    asyncio.run(client._order_gavelled(_bot(_message()), payload))
    assert order.made == []
    held = ordercards.load()["900"]
    assert held["status"] == "made" and held["card_id"] == hand["id"]
    assert "already has a card made since" in order.responder.sent[-1][0]


def test_cards_made_since_a_time(monkeypatch):
    class Board:
        def board_lists(self, board_id):
            return [{"id": "L"}]

        def list_cards(self, list_id):
            return [{"id": f"{int(datetime(2026, 10, 17, tzinfo=timezone.utc).timestamp()):08x}0000000000000001",
                     "name": "New Agent - Karl Cook"},
                    {"id": f"{int(datetime(2026, 10, 19, tzinfo=timezone.utc).timestamp()):08x}0000000000000002",
                     "name": "New Agent - Karl Cook"}]

        def close(self):
            pass

    monkeypatch.setattr(jobs, "open_trello", lambda cfg: Board())
    config = SimpleNamespace(secrets=SimpleNamespace(trello_board_id="b"))
    found = jobs.card_made_since(config, "Karl Cook", since=datetime(2026, 10, 18, tzinfo=timezone.utc))
    assert found["id"].endswith("2")
    assert jobs.card_made_since(config, "Karl Cook", since=datetime(2026, 10, 20, tzinfo=timezone.utc)) is None


def test_a_new_agent_card_isnt_an_aged_orders_card(monkeypatch):
    """"where is the card of this" - Abdullah Bahrani's aged payment, caught
    up after a restart, was taken as made by his New Agent card from that
    morning: Ryte's gavel on it, and no Aged Leads card anywhere."""
    made = f"{int(datetime(2026, 10, 19, 14, tzinfo=timezone.utc).timestamp()):08x}"

    class Board:
        def board_lists(self, board_id):
            return [{"id": "L"}]

        def list_cards(self, list_id):
            return [{"id": made + "0000000000000001", "name": "New Agent - Abdullah Bahrani"}]

        def close(self):
            pass

    monkeypatch.setattr(jobs, "open_trello", lambda cfg: Board())
    config = SimpleNamespace(secrets=SimpleNamespace(trello_board_id="b"))
    since = datetime(2026, 10, 19, 13, tzinfo=timezone.utc)
    assert jobs.card_made_since(config, "Abdullah Bahrani", since=since, kind="aged") is None
    assert jobs.card_made_since(config, "Abdullah Bahrani", since=since, kind="fresh") is not None


# ------------------------------------------ the payment post from Oct 19 on


HAYDEN = """<@&1> <@2>

**New Payment | Payra** ✅

**NAME:** Hayden Keltner
**EMAIL:** hayden@example.com
**PHONE:** +18125550193
**AMOUNT:** $1020.51
**PRODUCT:** Text-Verified Veteran leads
**INVOICE:** INV-20162 · card •••• 7872 · 10/19/2026 8:50 AM

**Subtotal:** $1,305.00
**$11/lead OFF:** -$319.00
**Automatic Dual Pricing:** $34.51
**Total:** $1,020.51
**Notes:**
> Name: Hayden Keltner
> Email: hayden@example.com
> Phone Number: 812-555-0193
> Lead Type: OTP VET - Text-Verified Veteran Leads (fresh) - invoice 2 of 2, for his agent Matthew Stewart (stewart@example.com, 720-555-0107); leads, lead texts and emails go to Matthew directly
> Number of Requested Leads: 29
**Internal Notes:**
> *(none)*"""


def test_the_new_post_names_its_invoice_and_carries_its_notes():
    number, notes = ordercards.post_invoice(HAYDEN)

    assert number == "INV-20162"
    assert notes.startswith("Name: Hayden Keltner") and notes.endswith("Number of Requested Leads: 29")
    assert "none" not in notes and ">" not in notes


def test_internal_notes_are_kept_when_there_are_some():
    _number, notes = ordercards.post_invoice(HAYDEN.replace("> *(none)*", "> states: TX, FL, GA"))

    assert notes.endswith("Internal notes: states: TX, FL, GA")


def test_the_old_post_has_neither():
    assert ordercards.post_invoice("New Payment | Payra\n**NAME:** X\n**PRODUCT:** VET") == ("", "")


def test_paying_for_their_agent_is_said_not_held():
    """Hayden Keltner paying "for his agent Matthew Stewart": the order says
    so - not Tony Moderno's card rewritten for Preston Bridges."""
    card = {"kind": "fresh", "source": HAYDEN,
            "fields": {"name": "Matthew Stewart", "states": "TX", "package": "OTP VET"}}
    wrong, missing = ordercards.doubts(card, today=datetime(2026, 10, 19).date())

    assert not any("paid by" in one for one in wrong)
    assert any("paid by Hayden Keltner for Matthew Stewart" in one for one in missing)


def test_somebody_else_named_nowhere_is_still_held():
    card = {"kind": "fresh", "source": HAYDEN,
            "fields": {"name": "Preston Bridges", "states": "TX", "package": "OTP VET"}}
    wrong, _missing = ordercards.doubts(card, today=datetime(2026, 10, 19).date())

    assert any("paid by Hayden Keltner" in one for one in wrong)


def test_a_post_carrying_its_notes_looks_nothing_up(monkeypatch):
    noted = {}

    async def note(key, **kw):
        noted.update(kw)

    monkeypatch.setattr(client, "_order_note", note)
    monkeypatch.setattr(jobs, "payra_invoice_pick", lambda *a, **kw: pytest.fail("looked Payra up"))
    got = asyncio.run(client._invoice_for(SimpleNamespace(config=None), "k1", name="Hayden Keltner", email="",
                                          amount="1020.51", product="VET", said=HAYDEN))

    assert got == "" and noted == {"invoice_id": "INV-20162"}


def test_a_post_naming_its_invoice_gets_that_one(monkeypatch):
    now = datetime(2026, 10, 19, 14, 0, tzinfo=timezone.utc)
    invoices = {
        "a": {"id": "a", "number": "INV-20161", "name": "Hayden Keltner", "total": 1020.51,
              "notes": "Lead Type: OTP FEX", "created": "2026-10-19T13:00:00Z"},
        "b": {"id": "b", "number": "INV-20162", "name": "Hayden Keltner", "total": 900.00,
              "notes": "Lead Type: OTP VET", "created": "2026-10-19T12:00:00Z"}}
    monkeypatch.setattr(jobs, "payra_recent", lambda config, kind, now=None: invoices)
    config = SimpleNamespace(secrets=SimpleNamespace(payra_api_token="t", payra_site_id="s"))

    note, which = jobs.payra_invoice_pick(config, name="Hayden Keltner", amount="1020.51", now=now,
                                          number="INV-20162")
    assert which == "INV-20162" and "OTP VET" in note
    # And a payment without a number doesn't take the one a post named.
    note, which = jobs.payra_invoice_pick(config, name="Hayden Keltner", amount="900", now=now,
                                          taken={"INV-20162"})
    assert which == "a"


def _busy_claude(monkeypatch, failures):
    """Anthropic that fails with each of `failures` in turn, then answers."""
    import sys
    import types

    asked = []

    class Messages:
        def create(self, *, model, **kw):
            asked.append(model)
            if failures:
                raise failures.pop(0)
            return "reply"

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=lambda **kw: SimpleNamespace(
        messages=Messages())))
    monkeypatch.setattr(jobs, "_tool_input", lambda response, name: {"kind": "fresh", "name": "Bram Gutierrez"})
    monkeypatch.setattr(jobs, "ORDER_READ_RETRY_WAITS", (0.001, 0.001))
    config = SimpleNamespace(secrets=SimpleNamespace(anthropic_api_key="k", require=lambda *a: None),
                             copy=SimpleNamespace(model="claude-opus-5-5"))
    return asked, config


class Overloaded(Exception):
    status_code = 529


class Refused(Exception):
    status_code = 400


def test_claude_busy_is_asked_again_after_a_wait(monkeypatch):
    """"Couldn't read the order - Claude didn't answer", both models in the
    same second, Oct 19."""
    asked, config = _busy_claude(monkeypatch, [Overloaded(), Overloaded()])
    card = jobs.read_order(config, text="NAME: Bram Gutierrez", images=[], today=date(2026, 10, 19))

    assert card["name"] == "New Agent - Bram Gutierrez"
    assert asked == [jobs.ORDER_MODEL] * 3


def test_a_refused_request_isnt_asked_again_and_says_why(monkeypatch):
    asked, config = _busy_claude(monkeypatch, [Refused("bad image"), Refused("bad image")])
    with pytest.raises(RuntimeError, match=r"didn't answer \(.*bad image"):
        jobs.read_order(config, text="NAME: Bram Gutierrez", images=[], today=date(2026, 10, 19))

    assert asked == [jobs.ORDER_MODEL, "claude-opus-5-5"]


@pytest.mark.parametrize("exc, busy", [
    (Overloaded(), True), (Refused(), False), (type("APIConnectionError", (Exception,), {})(), True),
    (type("APITimeoutError", (Exception,), {})(), True), (ValueError("no"), False),
])
def test_what_is_worth_asking_again(exc, busy):
    assert jobs.claude_busy(exc) is busy


def test_an_order_claude_didnt_answer_is_tried_again_once(monkeypatch):
    tried = []

    async def gavelled(bot, payload):
        tried.append(getattr(payload, "retried", False))

    monkeypatch.setattr(client, "_order_gavelled", gavelled)
    asyncio.run(client._order_again(None, client._payload_copy(
        SimpleNamespace(message_id=1, channel_id=2, user_id=3, guild_id=4, member=None), retried=True), wait=0))

    assert tried == [True]


DRUCILLA = """New Payment | Payra
**NAME:** Drucilla Bell
**PRODUCT:** Aged Leads (IUL)
Payra invoice note: Name: Drucilla Bell
Lead Type: Aged IUL - 30-90 days [AGED - $8/Lead]
States Leads In:
md,dc,va,sc,mo,tx,ok,or,west va,nc,tn
Number of Requested Leads: 50"""


def test_west_va_is_west_virginia():
    """Drucilla Bell's aged card was held: "WV is on the card but isn't in
    the order", from "west va"."""
    card = {"kind": "aged", "source": DRUCILLA, "fields": {
        "name": "Drucilla Bell", "states": "MD, DC, VA, SC, MO, TX, OK, OR, WV, NC, TN",
        "package": "Aged IUL 30-90", "leads": "50"}}
    wrong, _missing = ordercards.doubts(card, today=date(2026, 10, 18))

    assert wrong == []


@pytest.mark.parametrize("code, said", [
    ("WV", "W. Va."), ("WV", "W.Va"), ("NC", "N.C. and S.C."), ("SC", "N.C. and S.C."), ("DC", "D.C."),
    ("CA", "Calif"), ("TN", "tenn"), ("ND", "N Dakota"),
])
def test_short_ways_of_writing_a_state(code, said):
    assert ordercards._said_state(code, said)


@pytest.mark.parametrize("code, said", [("WV", "virginia only"), ("KS", "arkansas"), ("NC", "sc only")])
def test_not_a_state_that_isnt_written(code, said):
    assert not ordercards._said_state(code, said)


@pytest.mark.parametrize("said, usable", [
    ("F", False), ("-", False), ("FFL", True), ("Symmetry Financial", True), ("AEP", True), ("FE", True),
])
def test_an_agency_that_is_a_letter_isnt_one(said, usable):
    """Bram Gutierrez's card: "Agency: F" - "what is the agency of this"."""
    assert jobs.agency_usable(said) is usable


def test_a_letter_or_a_sentence_from_lead_lab_isnt_matched_in_the_notes():
    agencies = ("F", "i am leaving my current agency at this time", "Symmetry Financial")

    assert jobs.agency_said("Notes\nf", agencies) == ""
    assert jobs.agency_said("i am leaving my current agency at this time", agencies) == ""
    assert jobs.agency_said("symmetry financial", agencies) == "Symmetry Financial"


def test_a_count_after_the_lead_type_is_the_count():
    """Aaron Prum's "Text Verified Fresh Vets:30" - held for "30 isn't in
    the order"."""
    card = {"kind": "fresh", "source": "GA, FL, SC, AL, LA\nText Verified Fresh Vets:30\n\nAEP\n\nLIVE TODAY OCT 19",
            "fields": {"name": "Aaron Prum", "states": "GA, FL, SC, AL, LA",
                       "package": "Text-Verified Veteran leads", "leads": "30"}}
    assert ordercards.doubts(card, today=date(2026, 10, 19))[0] == []


def test_a_time_is_still_not_a_count():
    card = {"kind": "fresh", "source": "live at 10:30 today, GA FL SC AL LA vets",
            "fields": {"name": "Aaron Prum", "states": "GA, FL, SC, AL, LA",
                       "package": "Text-Verified Veteran leads", "leads": "30"}}
    assert any("30 isn't in the order" in one for one in ordercards.doubts(card, today=date(2026, 10, 19))[0])


@pytest.mark.parametrize("said, details", [
    ("💳 **Payment details** — from the Payra invoice\n**Name:** Drucilla Bell", True),
    ("**Invoice:** INV-20112\n**Notes:**\n> States Leads In: md,dc", True),
    ("✅ Aged Leads card made", False),
])
def test_novas_payment_details_in_the_thread_are_read(said, details):
    """Drucilla Bell's states were in Nova's thread message - a bot's, with
    no embed, so passed over."""
    assert client._payment_details(SimpleNamespace(content=said)) is details


def test_two_invoices_of_the_same_total_each_go_to_the_payment_that_paid_it(monkeypatch):
    """"WHAT ARE WE DOING??????" - Wiser Charles paid $103.50 twice on Oct
    16: INV-20057, 100 VET California, and INV-20058, 100 VET in eight
    states. Both cards got the eight. Payra's payment says which invoice it
    paid."""
    invoices = {
        "i57": {"id": "i57", "number": "INV-20057", "name": "Wiser Charles", "total": 103.50,
                "notes": "Lead Type: Aged Veteran - 180+ days\nStates Leads In: California\nNumber of Requested Leads: 100",
                "created": "2026-10-16T20:40:00Z"},
        "i58": {"id": "i58", "number": "INV-20058", "name": "Wiser Charles", "total": 103.50,
                "notes": "Lead Type: Aged Veteran - 180+ days\nStates Leads In: CA FL MI LA NC SC NJ OH\n"
                         "Number of Requested Leads: 100",
                "created": "2026-10-16T20:41:00Z"}}
    payments = {
        "p1": {"id": "p1", "customer": "Wiser Charles", "amount": 103.5, "paid_on": "2026-10-16T20:50:00Z",
               "invoice_ids": ["i57"]},
        "p2": {"id": "p2", "customer": "Wiser Charles", "amount": 103.5, "paid_on": "2026-10-16T20:53:00Z",
               "invoice_ids": ["i58"]}}
    monkeypatch.setattr(jobs, "payra_recent", lambda config, kind, now=None: invoices if kind == "invoices" else payments)
    config = SimpleNamespace(secrets=SimpleNamespace(payra_api_token="t", payra_site_id="s"))
    now = datetime(2026, 10, 16, 21, 0, tzinfo=timezone.utc)

    first, first_id = jobs.payra_invoice_pick(config, name="Wiser Charles", amount="$103.5", now=now,
                                              paid_at=datetime(2026, 10, 16, 20, 50, 20, tzinfo=timezone.utc))
    second, second_id = jobs.payra_invoice_pick(config, name="Wiser Charles", amount="$103.5", now=now,
                                                paid_at=datetime(2026, 10, 16, 20, 53, 15, tzinfo=timezone.utc),
                                                taken={first_id})
    assert first_id == "i57" and "California" in first and "CA FL" not in first
    assert second_id == "i58" and "CA FL MI" in second


def test_without_the_posts_time_it_is_matched_as_before(monkeypatch):
    invoices = {"i1": {"id": "i1", "number": "INV-1", "name": "Wiser Charles", "total": 103.50,
                       "notes": "Lead Type: Aged VET", "created": "2026-10-16T20:40:00Z"}}
    monkeypatch.setattr(jobs, "payra_recent", lambda config, kind, now=None: invoices if kind == "invoices" else {})
    config = SimpleNamespace(secrets=SimpleNamespace(payra_api_token="t", payra_site_id="s"))
    _note, which = jobs.payra_invoice_pick(config, name="Wiser Charles", amount="$103.5",
                                           now=datetime(2026, 10, 16, 21, 0, tzinfo=timezone.utc))
    assert which == "i1"


TRAVIS = """**New Payment | Payra** ✅
**NAME:** Travis Compere
**AMOUNT:** $1000
**PRODUCT:** VET
**INVOICE:** INV-20170 · card •••• 1234 · 10/19/2026 10:20 AM
[10/19 10:25AM] Kath: not sure who this is
but they aren't setup"""


def test_a_launch_day_nothing_names_isnt_taken_from_claude():
    """"why is this tuesday????" - Travis Compere's card: Tuesday, October 20,
    from nothing in his order."""
    assert not jobs.launch_written(date(2026, 10, 20), TRAVIS, today=date(2026, 10, 19))
    assert not ordercards.says_when(TRAVIS)


@pytest.mark.parametrize("said", ["Live Tuesday", "launch tues", "go live Oct 20", "start 10/20", "live tomorrow"])
def test_a_launch_day_the_order_names_is(said):
    assert jobs.launch_written(date(2026, 10, 20), said, today=date(2026, 10, 19))


def test_claudes_day_is_dropped_when_the_order_doesnt_say_it(monkeypatch):
    import sys
    import types

    class Messages:
        def create(self, **kw):
            return "reply"

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(
        Anthropic=lambda **kw: SimpleNamespace(messages=Messages())))
    monkeypatch.setattr(jobs, "_tool_input", lambda response, name: {
        "kind": "fresh", "name": "Travis Compere", "launch": "Tuesday, October 20", "launch_iso": "2026-10-20"})
    config = SimpleNamespace(secrets=SimpleNamespace(anthropic_api_key="k", require=lambda *a: None),
                             copy=SimpleNamespace(model="m"))
    # Payra's invoice header reads as "when" to the loose check.
    card = jobs.read_order(config, text=TRAVIS + "\nIF YOU USE CC/DEBIT WE CAN START YOUR ORDER TODAY", images=[],
                           today=date(2026, 10, 19))

    assert card["fields"]["launch"] == "" and card["launch_iso"] == ""


TARAFA = """[10/18 09:40PM] Nova: **New Payment | Payra** ✅
**NAME:** Matthew Tarafa
**PRODUCT:** Text-Verified Trucker IUL
[10/18 09:45PM] Kath: 40 OTP Trucker IUL Leads
Launch tomorrow 1am EST"""


def test_tomorrow_is_from_the_day_it_was_written():
    """"AND THIS ONE??????????" - Matthew Tarafa's "Launch tomorrow 1am
    EST", written on the Sunday, went on his card as Tuesday."""
    from wilbyte import agents

    named = jobs._named_days_by_stamp(TARAFA, date(2026, 10, 19))
    assert agents.find_launch(named, today=date(2026, 10, 19)) == date(2026, 10, 19)
    assert jobs.launch_written(date(2026, 10, 19), TARAFA, today=date(2026, 10, 19))
    assert not jobs.launch_written(date(2026, 10, 20), TARAFA, today=date(2026, 10, 19))


def test_a_note_is_dated_by_its_own_stamp():
    note = "40 OTP Trucker IUL Leads\nLaunch tomorrow 1am EST"
    assert jobs._written_on(TARAFA, note, date(2026, 10, 19)) == date(2026, 10, 18)
    assert jobs._named_days(note, date(2026, 10, 18)).endswith("Launch October 19 1am EST")


def test_not_today_stays_not_today():
    from wilbyte import agents

    said = jobs._named_days("launch not today, Tuesday", date(2026, 10, 19))
    assert agents.find_launch(said, today=date(2026, 10, 19)) == date(2026, 10, 20)


def test_the_invoice_header_isnt_today_once_named():
    said = jobs._named_days("IF YOU USE CC/DEBIT WE CAN START YOUR ORDER TODAY", date(2026, 10, 19))
    assert "October" not in said


def test_a_note_in_december_read_in_january_is_last_year():
    days = jobs._days_said("[12/31 11:00PM] K: launch tomorrow", date(2027, 1, 1))
    assert days[0][1] == date(2026, 12, 31)


def test_notes_are_stamped_on_the_boards_clock():
    message = SimpleNamespace(author=SimpleNamespace(display_name="Kath"), content="launch tomorrow",
                              embeds=[], attachments=[], created_at=datetime(2026, 10, 19, 1, 45, tzinfo=timezone.utc))
    assert client._said_line(message, "America/New_York").startswith("[10/18 09:45PM] Kath:")


@pytest.mark.parametrize("product, aged", [
    ("Text-Veified Aged IUL Leads", True), ("Text-Verified Aged Veteran Leads", True), ("Aged Leads (VET)", True),
    ("Text-Verified Veteran leads", False), ("VET", False),
])
def test_aged_anywhere_on_the_product_line(product, aged):
    """"WHY didnt it emojji this? its aged" - Azriel Deskin's "Text-Veified
    Aged IUL Leads": the word second, and no card."""
    message = _message(f"New Payment | Payra\n**NAME:** Azriel Deskin\n**PRODUCT:** {product}\n**INVOICE:** INV-20168")
    message.channel.name = "new-payment"
    assert client._aged_payment(message) is aged


def test_aged_on_another_line_isnt_the_product():
    message = _message("New Payment | Payra\n**PRODUCT:** VET\n**Notes:**\n> Lead type: Aged vets 180+")
    message.channel.name = "new-payment"
    assert not client._aged_payment(message)


def test_a_split_orders_new_agent_line_takes_the_orders_launch_day():
    """"??????????????????" - Jack Collins: "60 aged vet 30-90 / 20 fresh vet
    / ROADMAP AGENCY LIVE OCT 19", and his New Agent card had no launch date."""
    source = ("New Payment | Zelle\nNAME: Jack Collins\n[10/19 11:28AM] Kharyl: Jack Collins\nNC,TX,TN,CO\n"
              "60 aged vet 30-90\n20 fresh vet\n\nROADMAP AGENCY  LIVE OCT 19")
    card = {"client": "Jack Collins", "source": source, "launch_iso": "2026-10-19",
            "fields": {"name": "Jack Collins", "agency": "ROADMAP"},
            "parts": [{"kind": "aged", "package": "aged vet 30-90", "leads": "60", "states": "NC, TX, TN, CO",
                       "launch_iso": ""},
                      {"kind": "fresh", "package": "fresh vet", "leads": "20", "states": "NC, TX, TN, CO",
                       "launch_iso": ""}]}
    cards = jobs.split_order(card, today=date(2026, 10, 19))
    fresh = [one for one in cards if one["kind"] == "fresh"]

    assert fresh[0]["fields"]["launch"] == "Monday, October 19" and fresh[0]["launch_iso"] == "2026-10-19"


def test_a_line_without_a_day_beside_one_with_stays_blank():
    source = "NAME: Patrick Moreno\n34 OTP Vets waiting on states\n25 OTP IULs live Sunday\n100 Aged Vets 180+ GA"
    card = {"client": "Patrick Moreno", "source": source, "launch_iso": "2026-10-25",
            "fields": {"name": "Patrick Moreno"},
            "parts": [{"kind": "aged", "package": "Aged Vets 180+", "leads": "100", "states": "GA", "launch_iso": ""},
                      {"kind": "fresh", "package": "OTP Vets", "leads": "34", "states": "", "launch_iso": ""},
                      {"kind": "fresh", "package": "OTP IULs", "leads": "25", "states": "", "launch_iso": "2026-10-25"}]}
    fresh = [one for one in jobs.split_order(card, today=date(2026, 10, 22)) if one["kind"] == "fresh"]

    assert [one["launch_iso"] for one in fresh] == ["", "2026-10-25"]


def test_paid_through_their_own_business_is_said_not_held():
    """"bro" - Montaser Aidah paid as "Victory Freedom Llc", his own
    company, and the card was held as somebody else paying."""
    source = "New Payment | Zelle\nNAME: Victory Freedom Llc\nAMOUNT: $1100\n[10/19 11:30AM] K: Montaser Aidah setup"
    card = {"kind": "fresh", "source": source,
            "fields": {"name": "Montaser Aidah", "states": "", "package": "VET", "leads": ""}}
    wrong, missing = ordercards.doubts(card, today=date(2026, 10, 19))

    assert not any("paid by" in one for one in wrong)
    assert any("Victory Freedom Llc - their business?" in one for one in missing)


def test_held_buttons_carry_the_order_so_a_restart_can_answer_them():
    from wilbyte.bot import views

    item = views.HeldButton("1561758162590244915", "go")
    assert item.item.custom_id == "ryte:held:1561758162590244915:go"
    assert views.HeldButton.__discord_ui_compiled_template__.fullmatch(item.item.custom_id)


def test_a_held_button_from_before_a_restart_makes_the_order(monkeypatch):
    made, edits = [], []
    held = {"156": {"status": "held", "message_channel_id": "77", "guild_id": "88", "held_extra": ["Payra invoice note: x"]}}
    monkeypatch.setattr(ordercards, "load", lambda: held)

    async def making(bot, payload, key, responder):
        made.append((key, payload.channel_id, payload.guild_id, payload.confirmed, payload.extra_notes))

    monkeypatch.setattr(client, "_order_make", making)
    monkeypatch.setattr(client, "_order_responder", lambda bot: None)

    class Response:
        async def edit_message(self, **kw):
            edits.append(kw)

    press = SimpleNamespace(user=SimpleNamespace(id=5), message=SimpleNamespace(content="Not made yet"),
                            response=Response())
    asyncio.run(client._held_pressed(None, press, "156", "go"))

    assert made == [("156", 77, 88, True, ("Payra invoice note: x",))]
    assert "Working on it" in edits[0]["content"]


def test_a_held_button_still_waiting_isnt_answered_twice(monkeypatch):
    client._HELD_LIVE.add("157")
    try:
        monkeypatch.setattr(ordercards, "load", lambda: pytest.fail("answered twice"))
        asyncio.run(client._held_pressed(None, SimpleNamespace(), "157", "go"))
    finally:
        client._HELD_LIVE.discard("157")


@pytest.mark.parametrize("states, said", [
    ("Same as before", "States: Same as before"), ("Same as Fresh", "States: Same as Fresh"),
    ("States: Same as before", "States: Same as before"),
])
def test_same_as_before_is_taken_as_written(states, said):
    """Shane Suleiman's "States: Same as before" was held: "isn't a list of
    states". "if it said same states as last time, just put it as is"."""
    card = {"kind": "fresh", "source": f"NAME: Shane Suleiman\n142 OTP VET Leads\n{said}\nLive Monday October 19",
            "fields": {"name": "Shane Suleiman", "states": states, "package": "OTP VET", "leads": "142"}}
    wrong, _missing = ordercards.doubts(card, today=date(2026, 10, 19))
    assert not [one for one in wrong if "State" in one]


def test_states_made_up_beside_same_as_before_are_still_held():
    card = {"kind": "fresh", "source": "NAME: Shane Suleiman\nStates: Same as before",
            "fields": {"name": "Shane Suleiman", "states": "TX, FL", "package": "OTP VET", "leads": ""}}
    wrong, _missing = ordercards.doubts(card, today=date(2026, 10, 19))
    assert any("TX is on the card" in one for one in wrong)


def test_one_lead_type_with_an_aged_and_a_fresh_line_is_split(monkeypatch):
    """"no aged leads card???? why?" - Allen Coleman's "20 Fresh OTP Mtg PRO
    + 20 Aged Mtg Pro 30-90" was one New Agent card."""
    import sys
    import types

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(
        Anthropic=lambda **kw: SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: "reply"))))
    monkeypatch.setattr(jobs, "_tool_input", lambda response, name: {
        "kind": "fresh", "name": "Allen Coleman", "package": "20 Fresh OTP Mtg PRO + 20 Aged Mtg Pro 30-90",
        "leads": "40", "states": "CA, AZ, CO, UT", "launch": "Tuesday, October 20", "launch_iso": "2026-10-20",
        "parts": []})
    config = SimpleNamespace(secrets=SimpleNamespace(anthropic_api_key="k", require=lambda *a: None),
                             copy=SimpleNamespace(model="m"))
    text = ("[10/18 04:00PM] LeadLab: New Payment | Payra\nNAME: Allen Coleman\nPRODUCT: Aged Leads (MTG)\n"
            "Payra invoice note: 20 Fresh OTP Mtg PRO\n20 Aged Mtg Pro 30-90\nStates: CA, AZ, CO, UT\n"
            "[10/18 04:05PM] Kath: live Tuesday October 20")
    card = jobs.read_order(config, text=text, images=[], today=date(2026, 10, 19))
    cards = jobs.split_order(card, today=date(2026, 10, 19))

    assert sorted(one["kind"] for one in cards) == ["aged", "fresh"]
    fresh = next(one for one in cards if one["kind"] == "fresh")
    aged = next(one for one in cards if one["kind"] == "aged")
    assert fresh["fields"]["leads"] == "20" and fresh["launch_iso"] == "2026-10-20"
    assert aged["fields"]["leads"] == "20" and "Aged Mtg Pro 30-90" in aged["fields"]["package"]


@pytest.mark.parametrize("package", ["50 Aged VET 180+ + 50 Aged Mortgage Protection", "25 OTP VET", "20 VET + 20 Aged IUL"])
def test_a_lead_type_that_isnt_plainly_both_isnt_split(package):
    assert jobs.parts_from_package({"package": package}) == []


@pytest.mark.parametrize("said, agency", [
    ("ROADMAP AGENCY  LIVE OCT 19", "Roadmap"), ("Roadmap agency", "Roadmap"), ("Agency: Symmetry Financial", "Symmetry Financial"),
    ("LEGADO AGENCY - live monday", "LEGADO"),
    ("[10/19 11:28AM] Kharyl: Benny Beniamin\nNC,TX\n20 fresh vet\n\nROADMAP AGENCY  LIVE OCT 19", "Roadmap"),
])
def test_the_agency_as_the_team_writes_it(said, agency):
    """"ryte didnt add roadmap as an agency to benny beniamin"."""
    assert jobs.agency_said(said) == agency


@pytest.mark.parametrize("said", [
    "i am leaving my current agency at this time", "the agency", "my agency", "Agency: none", "Agency: N/A",
    "Same agency", "he is with the agency now", "Agency: F",
])
def test_not_an_agency_named(said):
    assert jobs.agency_said(said) == ""


def _read_with(monkeypatch, got, text, notes):
    import sys
    import types

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(
        Anthropic=lambda **kw: SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: "reply"))))
    monkeypatch.setattr(jobs, "_tool_input", lambda response, name: got)
    config = SimpleNamespace(secrets=SimpleNamespace(anthropic_api_key="k", require=lambda *a: None),
                             copy=SimpleNamespace(model="m"))
    return jobs.read_order(config, text=text, images=[], today=date(2026, 10, 19), notes=notes)


def test_the_person_the_notes_name_gets_the_card_not_who_paid(monkeypatch):
    """"Why didnt this named Camden?" - Garret Sekelsky paid, the notes said
    "7 OTP VETS / Camden Misquez", and the card was Garret's, with his email."""
    note = "7 OTP VETS\nCamden Misquez\nSame states last time\nLive today Monday October 19"
    text = ("[10/19 02:20PM] LeadLab: New Payment | Payra\nNAME: Garret Sekelsky\nEMAIL: garret@example.com\n"
            "PHONE: +15025550123\nPRODUCT: VET\n[10/19 02:25PM] Kath: " + note)
    card = _read_with(monkeypatch, {"kind": "fresh", "name": "Garret Sekelsky", "email": "garret@example.com",
                                    "phone": "+15025550123", "package": "OTP VETS", "leads": "7"}, text, [note])

    assert card["client"] == "Camden Misquez" and card["name"] == "New Agent - Camden Misquez"
    assert card["fields"]["email"] == "" and card["fields"]["phone"] == ""
    wrong, _missing = ordercards.doubts(card, today=date(2026, 10, 19))
    assert any("paid by Garret Sekelsky" in one and "Camden Misquez" in one for one in wrong)


def test_notes_naming_who_paid_change_nothing(monkeypatch):
    note = "Shane Suleiman\n142 OTP VET Leads"
    text = "[10/19 02:20PM] LeadLab: New Payment | Payra\nNAME: Shane Suleiman\nEMAIL: s@example.com\n" + note
    card = _read_with(monkeypatch, {"kind": "fresh", "name": "Shane Suleiman", "email": "s@example.com",
                                    "package": "OTP VET", "leads": "142"}, text, [note])
    assert card["client"] == "Shane Suleiman" and card["fields"]["email"] == "s@example.com"


@pytest.mark.parametrize("notes, name", [
    (["7 OTP VETS\nCamden Misquez\nSame states last time"], "Camden Misquez"),
    (["Same states last time\nLive Monday"], ""), (["Camden Misquez", "Jack Collins"], ""),
    (["Text Verified Fresh Vets"], ""),
])
def test_a_name_on_a_line_of_its_own(notes, name):
    assert jobs.name_in_notes(notes) == name


GARRET = """[10/19 02:38PM] Nova: <@&1>
**New Payment | Payra** ✅
**NAME:** Garret Sekelsky
**EMAIL:** garret@example.com
**PHONE:** +15025550123
**AMOUNT:** $246.33
**PRODUCT:** Text-Verified Veteran leads
**INVOICE:** INV-20230 · card •••• 0395 · 10/19/2026 1:36 PM
**Total:** $246.33
**Notes:**
> 7 OTP VETS
> Bailey Lawrence
> Same states
**Internal Notes:**
> *(none)*"""


def test_a_name_in_the_posts_own_notes_gets_the_card(monkeypatch):
    """"Same freaking issue" - Garret Sekelsky's "7 OTP VETS / Bailey
    Lawrence / Same states", in the post itself: his card, Bailey Lawrence as
    the Agency, and "> Same states" as the States."""
    card = _read_with(monkeypatch, {"kind": "fresh", "name": "Garret Sekelsky", "email": "garret@example.com",
                                    "phone": "+15025550123", "package": "OTP VETS", "leads": "7",
                                    "agency": "Bailey Lawrence", "states": "Same states"}, GARRET, [])

    assert card["client"] == "Bailey Lawrence"
    assert card["fields"]["agency"] == "" and card["fields"]["email"] == ""
    assert ">" not in card["source"].split("Notes:")[1]
    wrong, _missing = ordercards.doubts(card, today=date(2026, 10, 19))
    assert any("paid by Garret Sekelsky" in one for one in wrong)


SHOT = "https://trello.com/1/cards/c1/attachments/a1/download/image.png"


@pytest.mark.parametrize("text, vague", [
    (f"@nic0l3 @thereseguba can you take a look ![image.png]({SHOT})", True),
    (f"@nic0l3 ![image.png]({SHOT})", True),
    (f"@nic0l3 Michael Greff is capped at 10 a day and got 28 over the weekend ![image.png]({SHOT})", False),
    ("@nic0l3 can you take a look", False),
])
def test_a_picture_with_too_few_words_is_read(text, vague):
    assert jobs._too_vague(text) is vague


def test_a_tagged_picture_is_read_into_the_job(monkeypatch):
    """"BRO IMAGE CONTEXT"."""
    import sys
    import types

    from wilbyte import tagged

    asked = []

    class Messages:
        def create(self, **kw):
            asked.append(kw)
            return SimpleNamespace(content=[SimpleNamespace(
                text="Michael Greff, capped at 10 leads a day, got 28 over the weekend - find out why")])

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(
        Anthropic=lambda **kw: SimpleNamespace(messages=Messages())))
    board = SimpleNamespace(download=lambda url: (b"png", "image/png"))
    config = SimpleNamespace(secrets=SimpleNamespace(anthropic_api_key="k"), copy=SimpleNamespace(model="m"))
    note = tagged.Note(comment_id="x1", text=f"@nic0l3 can you take a look ![image.png]({SHOT})", author="Ryte",
                       card_id="c1", card_short="s")
    jobs._PICTURES_SAID.clear()

    said = jobs._picture_said(config, board, note)
    assert said.startswith("Michael Greff, capped at 10")
    assert jobs._picture_said(config, board, note) == said and len(asked) == 1, "read once a comment"
    assert "can you take a look" in asked[0]["messages"][0]["content"][-1]["text"]


def test_only_trellos_own_files_are_fetched_with_the_boards_key():
    from wilbyte import trello

    client = trello.TrelloClient("key", "token")
    try:
        with pytest.raises(trello.TrelloError, match="Not a Trello attachment"):
            client.download("https://evil.example.com/image.png")
    finally:
        client.close()


def test_one_payment_for_five_agents_is_five_cards(monkeypatch):
    """"how is ryte gonna file this" - Colby Whittaker paid $2550.24 once for
    five agents, each with their own details, states, count and webhook."""
    from pathlib import Path

    notes = (Path(__file__).parent / "colby_order.txt").read_text()
    text = ("[10/19 03:57PM] Nova: New Payment | Payra\nNAME: Colby Whittaker\nEMAIL: —\nAMOUNT: $2550.24\n"
            "PRODUCT: —\n[10/19 03:58PM] Faith: " + notes)
    card = _read_with(monkeypatch, {"kind": "fresh", "name": "Colby Whittaker", "package": "Fresh Vets",
                                    "leads": "154", "launch": "Monday, October 19", "launch_iso": "2026-10-19"},
                      text, [notes])
    cards = jobs.agent_cards(card, today=date(2026, 10, 19))

    assert [one["client"] for one in cards] == ["Gabriel Lucas", "Sawyer Wetzel", "Justin Bailey", "Dillon Willis",
                                                "Colby Whittaker"]
    assert [one["fields"]["leads"] for one in cards] == ["28", "42", "28", "28", "28"]
    assert all(one["launch_iso"] == "2026-10-19" and one["fields"]["launch"] == "Monday, October 19" for one in cards)
    justin = cards[2]["fields"]
    assert justin["email"] == "justinbailey@example.com" and "WV" in justin["states"] and "UT" in justin["states"]
    assert "Webhook URL: https://americanfirstagency.com/api/public/leads/inbound/926b6738" in justin["notes"]
    assert "Paid by Colby Whittaker - one payment for 5 agents ($2550.24)" in justin["notes"]
    assert cards[4]["fields"]["states"].startswith("All but HI")
    assert jobs.split_doubts(cards, today=date(2026, 10, 19)) == []


def test_one_agent_with_labels_is_one_card():
    said = "Name: Shane Suleiman\nEmail: s@example.com\nStates: TX, FL\nOrder Type: 30 Fresh Vets"
    assert jobs.order_agents(said) == []


def test_an_agent_list_said_twice_is_still_one_card_each():
    """"WDYM?????? they're jiust 5 no?" - Colby Whittaker's five agents came
    out as eight cards: the list was in the notes more than once."""
    from pathlib import Path

    notes = (Path(__file__).parent / "colby_order.txt").read_text()
    twice = notes + "\n" + notes.split("* - - -", 2)[-1]
    agents = jobs.order_agents(notes + "\n- - - - - - - - - -\n" + twice)

    assert [one["name"] for one in agents] == ["Gabriel Lucas", "Sawyer Wetzel", "Justin Bailey", "Dillon Willis",
                                               "Colby Whittaker"]


def test_a_name_line_starts_the_next_agent_without_a_line_of_dashes():
    """Gabriel Lucas' block has no "Name:" - without the dashes, Sawyer
    Wetzel's lines were read as more of Gabriel's."""
    from pathlib import Path

    notes = (Path(__file__).parent / "colby_order.txt").read_text()
    no_dashes = "\n".join(line for line in notes.splitlines() if "- - -" not in line)
    assert [one["name"] for one in jobs.order_agents(no_dashes)] == [
        "Gabriel Lucas", "Sawyer Wetzel", "Justin Bailey", "Dillon Willis", "Colby Whittaker"]


def test_agents_counted_against_the_number_the_order_says():
    cards = [{"kind": "fresh", "client": name, "name": f"New Agent - {name}", "agents_of": 3,
              "fields": {"leads": "28", "states": ""}, "source": "5 agents\n28 Fresh Vets"}
             for name in ("A B", "C D", "E F")]
    assert any("says 5 agents, and I read 3" in one for one in jobs.split_doubts(cards, today=date(2026, 10, 19)))
    for one in cards:
        one["source"] = "3 agents\n28 Fresh Vets"
    assert jobs.split_doubts(cards, today=date(2026, 10, 19)) == []


JOSHUA_FAILED = """**Failed Payment | Payra** ❌
**NAME:** joshua holt
**EMAIL:** joshua@example.com
**AMOUNT:** $703.80
**PRODUCT:** Text-Veified Aged IUL Leads
**INVOICE:** INV-20240 · card •••• 8794 · 10/19/2026 3:11 PM"""


def _failed_watch(monkeypatch, *, failed_at, paid=(), cards=(), made_by_ryte=()):
    """A Growth server with #failed-payment and #new-payment, and a board."""
    class Channel:
        def __init__(self, name, posts):
            self.name, self.posts = name, posts

        def history(self, after=None, limit=None):
            async def each():
                for one in self.posts:
                    yield one
            return each()

    def post(text, at, id_=1):
        return SimpleNamespace(id=id_, content=text, embeds=[], created_at=at, jump_url=f"https://discord/{id_}")

    failed = Channel("❌｜failed-payment", [post(JOSHUA_FAILED, failed_at, 11)])
    payments = Channel("new-payment", [post(text, at, 20 + n) for n, (text, at) in enumerate(paid)])
    guild = SimpleNamespace(text_channels=[failed, payments])
    monkeypatch.setattr(client, "_setup_guild", lambda bot, config: guild)
    asked, comments, said = [], [], []

    def cards_since(config, name, since, kind=""):
        asked.append((name, kind))
        return list(cards)

    monkeypatch.setattr(jobs, "cards_made_since", cards_since)
    monkeypatch.setattr(jobs, "order_payment_comment", lambda config, card_id, text: comments.append((card_id, text)))

    class Responder:
        async def send(self, text, **kw):
            said.append(text)

    monkeypatch.setattr(client, "_order_responder", lambda bot: Responder())
    ordercards.save({"x": {"status": "made", "card_id": one} for one in made_by_ryte})
    bot = SimpleNamespace(config=SimpleNamespace(schedule=SimpleNamespace(timezone="America/New_York")))
    return bot, asked, comments, said


def test_a_card_made_for_a_failed_payment_is_warned_once(monkeypatch):
    """"why did this pushed thru if the payment declined/failed" - Joshua
    Holt's aged card, made by hand after his payment failed."""
    failed_at = datetime(2026, 10, 19, 20, 13, tzinfo=timezone.utc)
    card = {"id": "c1", "name": "AGED LEAD - Joshua Holt", "shortUrl": "https://trello.com/c/J"}
    bot, asked, comments, said = _failed_watch(monkeypatch, failed_at=failed_at, cards=[card])
    now = failed_at + timedelta(hours=1)

    asyncio.run(client._failed_payments_once(bot, now=now))
    assert asked == [("joshua holt", "aged")]
    assert comments and comments[0][0] == "c1" and "Payment failed — INV-20240, $703.80" in comments[0][1]
    assert "Payment failed, but there's a card" in said[0] and "https://trello.com/c/J" in said[0]
    asyncio.run(client._failed_payments_once(bot, now=now + timedelta(minutes=5)))
    assert len(comments) == 1, "warned once"


def test_paid_since_the_failure_is_left_alone(monkeypatch):
    failed_at = datetime(2026, 10, 19, 20, 13, tzinfo=timezone.utc)
    card = {"id": "c1", "name": "AGED LEAD - Joshua Holt"}
    paid = [("New Payment | Payra\n**NAME:** Joshua Holt\n**EMAIL:** joshua@example.com", failed_at + timedelta(minutes=20))]
    bot, asked, comments, _said = _failed_watch(monkeypatch, failed_at=failed_at, paid=paid, cards=[card])
    asyncio.run(client._failed_payments_once(bot, now=failed_at + timedelta(hours=1)))
    assert asked == [] and comments == []


def test_a_card_ryte_made_from_a_payment_that_went_through_is_left_alone(monkeypatch):
    failed_at = datetime(2026, 10, 19, 20, 13, tzinfo=timezone.utc)
    card = {"id": "c9", "name": "Aged Leads - Joshua Holt"}
    bot, _asked, comments, _said = _failed_watch(monkeypatch, failed_at=failed_at, cards=[card], made_by_ryte=["c9"])
    asyncio.run(client._failed_payments_once(bot, now=failed_at + timedelta(hours=1)))
    assert comments == []


@pytest.mark.parametrize("text, named", [
    ("Mandatory entire amount needs moved over Kath and Jenn setup a discord call", True),
    ("Faith can you send the new link", True),
    ("@nic0l3 @thereseguba can you take a look", False),
    ("Give Tre admin access to ROADMAP server, add the Nova bot", False),
])
def test_a_job_nobody_tagged_goes_to_somebody_the_comment_names(text, named):
    """"these are not faith's tasks right?" - Tre's Roadmap steps and "Report
    back to Santiago Villegas" went on Faith's checklist, named nowhere."""
    from wilbyte import tagged

    faith = tagged.Person(username="faithhannahcalla", full_name="Faith Hannah Calla")
    kath = tagged.Person(username="kathleenmarie15", full_name="Kathleen Marie")
    who = kath if "Kath" in text else faith
    assert jobs._named_in_comment(text, who) is named


MATEO = """New Payment | Payra
**NAME:** Mateo Lyon
**PRODUCT:** Aged Leads (VET)
Payra invoice note: Name: Mateo Lyon
Lead Type: Aged Vets 30-90
States: AL CA KS KY MI MN NC OH OK SC TN TX
Lead Type: Aged Vets 90-180
States: TX
Lead Type: Aged FEX 180+
States: AZ CA ID MI NV OR SD TX UT WA
[10/19 10:40AM] Kath: 163 AGED VETS
33 AGED VETS
167 AGED FEX"""


def test_several_aged_lines_keep_their_own_states():
    """"this cost us MONEY" - Mateo Lyon's three aged lines went out with
    every state on every line, and his Vets 90-180 were Texas only."""
    card = {"client": "Mateo Lyon", "source": MATEO, "fields": {"name": "Mateo Lyon"},
            "parts": [{"kind": "aged", "package": "Aged Vets 30-90", "leads": "163",
                       "states": "AL, CA, KS, KY, MI, MN, NC, OH, OK, SC, TN, TX", "launch_iso": ""},
                      {"kind": "aged", "package": "Aged Vets 90-180", "leads": "33", "states": "TX", "launch_iso": ""},
                      {"kind": "aged", "package": "Aged FEX 180+", "leads": "167",
                       "states": "AZ, CA, ID, MI, NV, OR, SD, TX, UT, WA", "launch_iso": ""}]}
    cards = jobs.split_order(card, today=date(2026, 10, 19))

    assert len(cards) == 1 and cards[0]["kind"] == "aged"
    fields = cards[0]["fields"]
    assert "Aged Vets 90-180: TX;" in fields["states"]
    assert fields["leads"] == "363 (163 + 33 + 167)"
    assert "• 33 Aged Vets 90-180 — TX" in fields["notes"] and "New-agent leads" not in fields["notes"]
    assert ordercards.doubts({"kind": "aged", "source": MATEO, "fields": {**fields, "name": "Mateo Lyon"}},
                             today=date(2026, 10, 19))[0] == []


def test_lines_with_their_own_states_on_one_list_are_held():
    card = {"kind": "aged", "source": MATEO, "fields": {
        "name": "Mateo Lyon", "package": "50 Aged Vets 30-90 + 33 Aged Vets 90-180 + 167 Aged FEX 180+",
        "leads": "363", "states": "AL, CA, KS, KY, MI, MN, NC, OH, OK, SC, TN, TX, AZ, ID, NV, OR, SD, UT, WA"}}
    wrong, _missing = ordercards.doubts(card, today=date(2026, 10, 19))

    assert any("Lead type: 50" in one for one in wrong)
    assert any("each line of the order has its own states" in one for one in wrong)


def _aged_board(monkeypatch, cards):
    class Board:
        def board_cards(self, board_id, archived=False):
            assert archived, "archived cards too - Aged Leads Order Done is archived each night"
            return cards

        def close(self):
            pass

    monkeypatch.setattr(jobs, "open_trello", lambda cfg: Board())
    return SimpleNamespace(secrets=SimpleNamespace(trello_board_id="b"))


MATEO_INVOICE = {"id": "i130", "number": "INV-20130", "name": "Mateo Lyon", "email": "mateo@example.com",
                 "created": "2026-10-19T14:30:00Z", "description": "", "lines": [],
                 "notes": ("Name: Mateo Lyon\nLead Type: Aged Vets 30-90\nStates: AL CA KS KY MI MN NC OH OK SC TN TX\n"
                           "Lead Type: Aged Vets 90-180\nStates: TX\n"
                           "Lead Type: Aged FEX 180+\nStates: AZ CA ID MI NV OR SD TX UT WA\n163 + 33 + 167")}


def _aged_card(states, package):
    made = int(datetime(2026, 10, 19, 14, 42, tzinfo=timezone.utc).timestamp())
    return {"id": f"{made:08x}0000000000000001", "name": "Aged Leads - Mateo Lyon", "shortUrl": "https://trello.com/c/M",
            "desc": f"Name: Mateo Lyon\nEmail: mateo@example.com\nLead Type: {package}\nStates: {states}\n"}


def test_check_aged_lines_finds_one_list_for_lines_of_their_own(monkeypatch):
    """"yes build the check aged lines" - Mateo Lyon's card, as it went out."""
    config = _aged_board(monkeypatch, [_aged_card(
        "AL, CA, KS, KY, MI, MN, NC, OH, OK, SC, TN, TX, AZ, ID, NV, OR, SD, UT, WA",
        "50 Aged Vets 30-90 + 33 Aged Vets 90-180 + 167 Aged FEX 180+")])
    flagged, looked = jobs.check_aged_lines(config, now=datetime(2026, 10, 19, 23, tzinfo=timezone.utc),
                                            invoices=[MATEO_INVOICE])
    assert looked == 1 and len(flagged) == 1
    assert "one list" in flagged[0] and "50 in the lead type" in flagged[0] and "Aged Vets 90-180: TX" in flagged[0]


def test_check_aged_lines_passes_a_card_right_line_by_line(monkeypatch):
    config = _aged_board(monkeypatch, [_aged_card(
        "Aged Vets 30-90: AL, CA, KS, KY, MI, MN, NC, OH, OK, SC, TN, TX; Aged Vets 90-180: TX; "
        "Aged FEX 180+: AZ, CA, ID, MI, NV, OR, SD, TX, UT, WA",
        "Aged Vets 30-90 + Aged Vets 90-180 + Aged FEX 180+")])
    flagged, looked = jobs.check_aged_lines(config, now=datetime(2026, 10, 19, 23, tzinfo=timezone.utc),
                                            invoices=[MATEO_INVOICE])
    assert looked == 1 and flagged == []


def test_check_aged_lines_is_a_command():
    request = mentions.parse("<@1> check aged lines")
    assert request.action == "agedlines" and request.brief == ""
    assert mentions.parse("<@1> check aged lines 14 days").brief == "14"


def test_check_aged_lines_leaves_the_new_agent_lines_to_their_own_cards(monkeypatch):
    """Patrick Moreno's Aged card was flagged against his OTP lines too."""
    invoice = {"id": "i54", "number": "INV-20054", "name": "Patrick Moreno", "email": "p@example.com",
               "created": "2026-10-19T14:30:00Z", "description": "", "lines": [],
               "notes": ("Lead Type: 50 Aged Vets 30-90\nStates: TX\nLead Type: 100 Aged IULs 30-90\nStates: NC GA FL TX\n"
                         "Lead Type: 34 OTP Vets\nStates: IN\nLead Type: 25 OTP IULs\nStates: AL AZ CA")}
    made = int(datetime(2026, 10, 19, 14, 42, tzinfo=timezone.utc).timestamp())
    card = {"id": f"{made:08x}0000000000000002", "name": "Aged Leads - Patrick Moreno", "shortUrl": "u",
            "desc": ("Name: Patrick Moreno\nEmail: p@example.com\nLead Type: Aged Vets 30-90 + Aged IULs 30-90\n"
                     "States: Aged Vets 30-90: TX; Aged IULs 30-90: NC, GA, FL, TX\n")}
    config = _aged_board(monkeypatch, [card])
    flagged, _looked = jobs.check_aged_lines(config, now=datetime(2026, 10, 19, 23, tzinfo=timezone.utc),
                                             invoices=[invoice])
    assert flagged == []


def test_check_aged_lines_takes_the_invoice_the_card_is_for(monkeypatch):
    """Patrick Moreno had two orders that week; his card matched the other."""
    made = int(datetime(2026, 10, 19, 14, 42, tzinfo=timezone.utc).timestamp())
    near = {"id": "a", "number": "INV-A", "name": "Patrick Moreno", "email": "p@example.com", "lines": [],
            "created": "2026-10-19T14:30:00Z", "description": "",
            "notes": "Lead Type: Aged Vets 30-90\nStates: TX\nLead Type: Aged IULs 30-90\nStates: NC GA FL TX"}
    his = {**near, "id": "b", "number": "INV-B", "created": "2026-10-18T14:30:00Z",
           "notes": "Lead Type: Aged Vets 180+\nStates: GA\nLead Type: Aged MTG 180+\nStates: FL"}
    card = {"id": f"{made:08x}0000000000000003", "name": "Aged Leads - Patrick Moreno", "shortUrl": "u",
            "desc": "Email: p@example.com\nLead Type: Aged Vets 180+ + Aged MTG 180+\nStates: Aged Vets 180+: GA; Aged MTG 180+: FL\n"}
    config = _aged_board(monkeypatch, [card])
    flagged, _ = jobs.check_aged_lines(config, now=datetime(2026, 10, 19, 23, tzinfo=timezone.utc),
                                       invoices=[near, his])
    assert flagged == []
    card["desc"] = card["desc"].replace("FL\n", "TX\n")
    flagged, _ = jobs.check_aged_lines(config, now=datetime(2026, 10, 19, 23, tzinfo=timezone.utc),
                                       invoices=[near, his])
    assert len(flagged) == 1 and "Card has: Aged Vets 180+: GA; Aged MTG 180+: TX" in flagged[0]


HUNTER = """[10/19 05:30PM] Nova: <@&1>
**New Payment | Payra** ✅
**NAME:** Hunter Kahle
**EMAIL:** hunter@example.com
**PHONE:** +19735550199
**AMOUNT:** $507.15
**PRODUCT:** Text-Verified Aged Veteran Leads
**INVOICE:** INV-20260 · card •••• 0509 · 10/19/2026 4:27 PM
**Total:** $507.15
**Notes:**
> Name: Hunter Kahle
> Lead Type: Aged Vets
> States: KY OK SC TN FL NC VA NJ OH MO CO AZ TX
> Notes: Only Motion
**Internal Notes:**
> *(none)*"""


def test_notes_inside_the_notes_dont_cut_them_short():
    """Hunter Kahle's "Notes: Only Motion" read as the post's own heading."""
    _number, notes = ordercards.post_invoice(HUNTER)
    assert notes.startswith("Name: Hunter Kahle") and "States: KY OK" in notes and notes.endswith("Notes: Only Motion")


@pytest.mark.parametrize("named_by_claude", ["Hunter Kahle", "Only Motion"])
def test_the_name_written_in_the_notes_is_who_its_for(monkeypatch, named_by_claude):
    """"what are we doing" - his card went out as "Only Motion", without his
    email and phone, held as somebody else paying."""
    card = _read_with(monkeypatch, {"kind": "aged", "name": named_by_claude, "email": "hunter@example.com",
                                    "phone": "+19735550199", "package": "Aged Vets",
                                    "states": "KY, OK, SC, TN, FL, NC, VA, NJ, OH, MO, CO, AZ, TX"}, HUNTER, [])
    assert card["client"] == "Hunter Kahle"
    assert card["fields"]["email"] == "hunter@example.com"
    assert not any("paid by" in one for one in ordercards.doubts(card, today=date(2026, 10, 19))[0])


def test_a_written_name_doesnt_undo_paying_for_their_agent(monkeypatch):
    card = _read_with(monkeypatch, {"kind": "fresh", "name": "Matthew Stewart", "package": "OTP VET", "leads": "29"},
                      HAYDEN, [])
    assert card["client"] == "Matthew Stewart"


MARIO = """[10/20 08:14AM] Nova: <@&1>
**New Payment | Payra** ✅
**NAME:** Mario Gonzalez
**EMAIL:** mario@example.com
**AMOUNT:** $1490.40
**PRODUCT:** Text-Verified Veteran leads
**INVOICE:** INV-20243 · card •••• 8356 · 10/20/2026 8:14 AM
**Total:** $1,490.40
**Notes:**
> Hey Faith this is Mario and agent under maxout through Mike shaefer,
> Would like to make an order of 40 premium vets
> States are
> Az , IN , KS, MN, OH ,VA,SC, NC, OR, WA
> We are told we have a 20% off code with code (leadlab20)
> Just wanted to place a order! Thank you so much .
> mario@example.com
**Internal Notes:**
> *(none)*"""


def test_the_invoices_own_notes_go_on_the_card_as_written(monkeypatch):
    """"can you add the additional notes as it is? unless its image, if its
    text as it is"."""
    card = _read_with(monkeypatch, {"kind": "fresh", "name": "Mario Gonzalez", "email": "mario@example.com",
                                    "package": "OTP VETS", "leads": "40", "notes": "Client wants 40 premium vets."},
                      MARIO, [])
    notes = card["fields"]["notes"]
    assert notes.startswith("From the Payra invoice: Hey Faith this is Mario and agent under maxout through Mike shaefer,")
    assert "We are told we have a 20% off code with code (leadlab20)" in notes
    assert "Client wants 40 premium vets." not in notes, "the invoice as written, not a summary of it"


def test_the_teams_notes_go_on_whole():
    """Every line, as they wrote it - "50 OTP Vets / Add OR and ND / Live tonight
    Monday October 19 / Marcel Trifan"."""
    note = "50 OTP Vets\nAdd OR and ND\n\nLive tonight Monday October 19\nMarcel Trifan"
    fields = jobs.order_fields({"name": "Marcel Trifan", "package": "OTP Vets", "leads": "50"}, [note])
    assert fields["notes"] == note


def test_the_invoices_form_lines_arent_the_card_twice():
    note = "Payra invoice note: Name: Jad\nEmail: jad@example.com\nStates Leads In: TX\nPlease start Monday morning"
    fields = jobs.order_fields({"name": "Jad", "email": "jad@example.com"}, [note])
    assert fields["notes"] == "From the Payra invoice: Please start Monday morning"


SHELBY = """[10/19 04:47PM] Nova: **New Payment | Payra** ✅
**NAME:** Tony Moderno
**EMAIL:** tony@example.com
**PHONE:** +18015550114
**AMOUNT:** $1852.65
**PRODUCT:** Text-Verified Mortgage Protection Leads
[10/19 04:53PM] Faith: Name: Shelby Guest
Email: Shelby.guest@example.com
Phone number: 435-555-3162
States. UT, AL, AR, AZ, CA
30 fresh mortgage protection plus
100 aged final expense
30-90
Live Tuesday October 20"""

SHELBY_NOTE = SHELBY.split("Faith: ", 1)[1]


def test_a_split_order_for_somebody_else_is_held_and_theirs(monkeypatch):
    """"what are we doing bro" - Tony Moderno paid, the thread said "Name:
    Shelby Guest", and both her cards went out as Tony's, with his email and
    phone: the split order went round the paid-by check."""
    card = _read_with(monkeypatch, {
        "kind": "fresh", "name": "Tony Moderno", "email": "tony@example.com", "phone": "+18015550114",
        "package": "30 fresh mortgage protection plus + 100 aged final expense 30-90", "leads": "130",
        "states": "UT, AL, AR, AZ, CA", "launch": "Tuesday, October 20", "launch_iso": "2026-10-20",
        "parts": [{"kind": "fresh", "package": "fresh mortgage protection plus", "leads": "30",
                   "states": "UT, AL, AR, AZ, CA", "launch_iso": "2026-10-20"},
                  {"kind": "aged", "package": "aged final expense 30-90", "leads": "100",
                   "states": "UT, AL, AR, AZ, CA", "launch_iso": ""}]}, SHELBY, [SHELBY_NOTE])

    assert card["client"] == "Shelby Guest"
    assert card["fields"]["email"] == "Shelby.guest@example.com" and card["fields"]["phone"] == "435-555-3162"
    cards = jobs.split_order(card, today=date(2026, 10, 19))
    assert {one["client"] for one in cards} == {"Shelby Guest"}
    assert any("paid by Tony Moderno, but the order is for Shelby Guest" in one for one in jobs.split_doubts(cards, today=date(2026, 10, 19)))


def test_several_agents_paid_for_by_one_isnt_held_for_it():
    cards = [{"kind": "fresh", "client": "Gabriel Lucas", "name": "New Agent - Gabriel Lucas", "agents_of": 2,
              "fields": {"leads": "28", "states": ""}, "source": "NAME: Colby Whittaker\n2 agents\n28 Fresh Vets"},
             {"kind": "fresh", "client": "Sawyer Wetzel", "name": "New Agent - Sawyer Wetzel", "agents_of": 2,
              "fields": {"leads": "28", "states": ""}, "source": "NAME: Colby Whittaker\n2 agents\n28 Fresh Vets"}]
    assert jobs.split_doubts(cards, today=date(2026, 10, 19)) == []


def test_a_card_that_cant_be_made_tags_franklin(order, monkeypatch):
    """"can ryte tag me with these? ... that way im aware that a card cannot
    be created" - a new message, since a tag added by an edit notifies
    nobody."""
    monkeypatch.setattr(order.responder, "send", order.responder.send)
    bot = _bot(_message())
    monkeypatch.setattr(bot.config.secrets, "discord_notify_user_id", "424242")
    assert client._card_ping(bot) == "<@424242> "
    assert client._card_ping(SimpleNamespace()) == ""


def _spark_post(i=960):
    fields = [SimpleNamespace(name=name, value=value) for name, value in (
        ("Buyer", "Precision Life Financial"), ("Pool", "Blue Collar IUL"), ("Lead type", "blue-collar-iul"),
        ("Order", "$525/wk · pool membership"), ("CPL range", "$35–$39"), ("Expected leads / wk", "13–15"),
        ("Starts", "October 25, 2026"), ("Paid through", "November 1, 2026"),
        ("States", "AL, AK, AZ, FL, GA"), ("Buyer email", "michael@example.com"),
        ("Membership", "aaa49443-0000"), ("Routing token", "lpm_0000"))]
    message = _message("🛒 New lead pool member on Spark")
    message.embeds = [SimpleNamespace(title="Blue Collar IUL — $525/wk · pool membership",
                                      description="A buyer just joined and paid for your pool on Spark. Fulfill "
                                                  "this order from your Spark vendor dashboard.",
                                      fields=fields, url=None, footer=None, author=None)]
    message.id, message.channel.name = i, "👑 | spark"
    message.author.display_name = "Capture Info"
    return message


def test_a_new_spark_pool_member_gets_its_card_with_no_gavel(order, monkeypatch):
    """"just like aged leads, everytime there's new capture info, auto-hammer
    it" - a New Agent card from Capture Info's post in #spark."""
    order.kind = "fresh"
    message = _spark_post()
    assert client._spark_member(message)
    assert not client._spark_member(_aged_post()), "not a payment post"
    assert asyncio.run(client._spark_from_post(_bot(message), message, wait=0)) == "made"
    assert [one[0] for one in order.made] == ["fresh"]
    note = order.notes[0]
    assert note.startswith("Purchased on Spark") and "Paid through - November 1, 2026" in note
    assert "Routing token - lpm_0000" in note and "Buyer email" not in note
    from wilbyte import agents
    assert agents.from_spark(note), "known as Spark's: no contract to sign"
    assert asyncio.run(client._spark_from_post(_bot(message), message, wait=0)) == "already"


def test_a_question_thread_about_an_agent_isnt_their_order(order):
    """"Nova: Santiago Villegas - Does Allen Coleman have a schedule?" in
    #team-chat, started by Nova: read onto his card, with a false "40 isn't
    in the order"."""
    nova = SimpleNamespace(bot=True)
    asked = SimpleNamespace(name="Nova: Santiago Villegas — Does Allen Coleman have a schedule?", owner=nova)
    assert client._not_an_order_thread(None, asked)
    assert client._not_an_order_thread(None, SimpleNamespace(name="Allen Coleman schedule?", owner=None))
    assert client._not_an_order_thread(None, SimpleNamespace(name="Allen Coleman", owner=nova))
    person = SimpleNamespace(bot=False)
    assert not client._not_an_order_thread(None, SimpleNamespace(name="Allen Coleman - OTP MTG", owner=person))
    assert asyncio.run(client._order_link_thread(_bot(_message()), asked)) is None


def test_a_split_beside_the_states_stays_on_the_card():
    """Torian Lollis: "States Leads In: 80 Louisiana, 20 Texas" for 25 leads,
    and the card said "States: LA, TX"."""
    form = jobs.invoice_form("Lead Type: aged FEX 180+\nNumber of Requested Leads: 25\n"
                             "States Leads In: 80 Louisiana, 20 Texas")
    fields = jobs.with_invoice_form({"states": "LA, TX", "leads": "25", "package": "aged FEX 180+"}, form)
    assert fields["states"] == "LA 80%, TX 20%"
    card = {"kind": "aged", "source": "PRODUCT: Text-Verified Aged Final Expense Leads\n"
                                      "States Leads In: 80 Louisiana, 20 Texas\nNumber of Requested Leads: 25",
            "fields": fields, "launch_iso": ""}
    assert ordercards.doubts(card, today=date(2026, 10, 21))[0] == []
    assert jobs._states_split("FL 18, VA 14", "32") == "FL 18, VA 14", "counts, not shares"
    assert jobs._states_split("Texas, Florida", "10") == ""


def test_a_zelle_posts_own_lines_fill_the_card(monkeypatch):
    """Jordan Holmes: Nova's Zelle post had EMAIL, PHONE and every state, and
    his new card had none of them - and LEADS: 50 against the 21 in the note
    went by unasked."""
    import sys
    import types

    class Anthropic:
        def __init__(self, **kw):
            self.messages = SimpleNamespace(create=lambda **kw: "reply")

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=Anthropic))
    monkeypatch.setattr(jobs, "_tool_input", lambda response, name: {
        "kind": "fresh", "name": "Jordan Holmes", "package": "Trucker IUL", "leads": "21", "agency": "FFL"})
    config = SimpleNamespace(secrets=SimpleNamespace(anthropic_api_key="k", require=lambda *a: None),
                             copy=SimpleNamespace(model="claude-opus-5-5"))
    text = ("[10/21 04:59PM] Nova: @Luna Zelle payment received — Jordan Holmes $1,000.00\nNew Payment | Zelle\n"
            "NAME: Jordan Holmes\nEMAIL: jordan@example.com\nPHONE: +18435550000\nAMOUNT: $1,000.00\n"
            "PRODUCT: Text Verified Trucker IUL Leads [FRESH - $60/Lead]\nLEADS: 50\n"
            "STATES: Alabama Alaska Arizona Texas Wyoming\nAGENCY: FFL\n"
            "[10/21 05:01PM] Faith: 21 Trucker IUL leads\nAdd to current order")
    card = jobs.read_order(config, text=text, images=[], today=date(2026, 10, 21),
                           notes=["21 Trucker IUL leads\nAdd to current order"])
    fields = card["fields"]
    assert fields["email"] == "jordan@example.com" and fields["phone"] == "+18435550000"
    assert fields["states"] == "AL, AK, AZ, TX, WY"
    wrong = ordercards.doubts(card, today=date(2026, 10, 21))[0]
    assert any("the payment says 50, the card 21" in one for one in wrong)
    assert not any("No email, phone or states" in one for one in wrong)
    note_only = {"kind": "fresh", "source": "[10/21 05:01PM] Faith: 21 Trucker IUL leads\nAdd to current order",
                 "fields": {"name": "Jordan Holmes", "leads": "21", "package": "Trucker IUL"}, "launch_iso": ""}
    assert any("was the payment gavelled" in one for one in ordercards.doubts(note_only, today=date(2026, 10, 21))[0])


def test_a_spark_posts_start_is_the_launch_date(monkeypatch):
    """FND Life: "Starts October 25, 2026" on the Spark post, and the card went
    out with no launch date - "I can't find a launch date on this card"."""
    import sys
    import types

    class Anthropic:
        def __init__(self, **kw):
            self.messages = SimpleNamespace(create=lambda **kw: "reply")

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=Anthropic))
    monkeypatch.setattr(jobs, "_tool_input", lambda response, name: {
        "kind": "fresh", "name": "FND Life", "email": "join@example.com", "states": "AL, AZ, AR",
        "package": "Blue Collar IUL", "leads": "17-20"})
    config = SimpleNamespace(secrets=SimpleNamespace(anthropic_api_key="k", require=lambda *a: None),
                             copy=SimpleNamespace(model="claude-opus-5-5"))
    text = ("[10/21 03:50PM] Capture Info: 🛒 New lead pool member on Spark\n"
            "Blue Collar IUL — $700/wk · pool membership\n"
            "A buyer just joined and paid for your pool on Spark. Fulfill this order from your Spark vendor "
            "dashboard.\nBuyer FND Life\nPool Blue Collar IUL\nLead type blue-collar-iul\n"
            "Order $700/wk · pool membership\nCPL range $35–$39\nExpected leads / wk 17–20\n"
            "Starts October 25, 2026\nPaid through November 1, 2026\nStates AL, AZ, AR\n"
            "Buyer email join@example.com")
    card = jobs.read_order(config, text=text, images=[], today=date(2026, 10, 21),
                           notes=["Purchased on Spark - pool membership.\nStarts - October 25, 2026"])
    assert card["launch_iso"] == "2026-10-25" and card["fields"]["launch"] == "Sunday, October 25"
    assert card["fields"]["agency"] == "Spark" and "Agency: Spark" in card["desc"]
    assert not any("Launch date" in one for one in ordercards.doubts(card, today=date(2026, 10, 21))[0])


def _one_line_case(monkeypatch, desc, form):
    made = int(datetime(2026, 10, 19, 14, 42, tzinfo=timezone.utc).timestamp())
    card = {"id": f"{made:08x}0000000000000004", "name": "Aged Leads - Torian Lollis", "shortUrl": "u",
            "desc": "Name: Torian Lollis\nEmail: torian@example.com\n" + desc}
    invoice = {"id": "t", "number": "INV-T", "name": "Torian Lollis", "email": "torian@example.com", "lines": [],
               "created": "2026-10-19T14:30:00Z", "description": "", "notes": "New Lead Request\n" + form}
    config = _aged_board(monkeypatch, [card])
    return jobs.check_aged_lines(config, now=datetime(2026, 10, 19, 23, tzinfo=timezone.utc), invoices=[invoice])


TORIAN_FORM = "Lead Type: aged FEX 180+\nNumber of Requested Leads: 25\nStates Leads In: 80 Louisiana, 20 Texas"


def test_check_aged_lines_finds_a_lost_split_on_one_line(monkeypatch):
    """"make sure ryte dont miss anything" - Torian Lollis's 80 Louisiana / 20
    Texas went out as "LA, TX", and one-line invoices weren't looked at."""
    flagged, looked = _one_line_case(
        monkeypatch, "Lead Type: Aged FEX 180+\nNumber of Leads Requested: 25\nStates: LA, TX\n", TORIAN_FORM)
    assert looked == 1 and len(flagged) == 1
    assert "splits the states (LA 80%, TX 20%)" in flagged[0] and "Should be: LA 80%, TX 20%" in flagged[0]


def test_check_aged_lines_passes_a_one_line_card_thats_right(monkeypatch):
    flagged, _ = _one_line_case(
        monkeypatch, "Lead Type: Aged FEX 180+\nNumber of Leads Requested: 25\nStates: LA 80%, TX 20%\n",
        TORIAN_FORM)
    assert flagged == []


def test_check_aged_lines_finds_wrong_states_count_and_age_on_one_line(monkeypatch):
    flagged, _ = _one_line_case(
        monkeypatch, "Lead Type: Aged Veteran 180+\nNumber of Leads Requested: 20\nStates: AZ, VA\n",
        "Lead Type: Aged Veteran - 30-90 days\nStates Leads In: Arizona Virginia New Mexico\n"
        "Number of Requested Leads: 25")
    assert len(flagged) == 1
    assert "states aren't the invoice's (AZ, NM, VA)" in flagged[0]
    assert "invoice is for 25 leads, the card 20" in flagged[0]
    assert "invoice is for 30-90 days, the card says 180+" in flagged[0]


def test_check_aged_lines_reads_all_but_on_one_line(monkeypatch):
    """Mackenzie Ho: "all but new york"."""
    form = "Lead Type: Aged Veteran - 180+ days\nStates Leads In: all but new york\nNumber of Requested Leads: 300"
    flagged, _ = _one_line_case(
        monkeypatch, "Lead Type: Aged Veteran 180+\nNumber of Leads Requested: 300\nStates: All states except NY\n",
        form)
    assert flagged == []
    flagged, _ = _one_line_case(
        monkeypatch, "Lead Type: Aged Veteran 180+\nNumber of Leads Requested: 300\nStates: All states\n", form)
    assert len(flagged) == 1 and "All states except NY" in flagged[0]


def test_check_aged_lines_leaves_one_line_cards_with_nothing_to_compare(monkeypatch):
    """No form on the invoice, or their New Agent invoice the same week."""
    desc = "Lead Type: Aged FEX 180+\nNumber of Leads Requested: 25\nStates: LA, TX\n"
    assert _one_line_case(monkeypatch, desc, "same as last time")[0] == []
    assert _one_line_case(monkeypatch, desc, "Lead Type: OTP Vets\nStates Leads In: Indiana\n"
                                              "Number of Requested Leads: 34")[0] == []


def test_an_aged_order_waiting_for_its_states_isnt_tagged_straight_away(order, monkeypatch):
    """Thomas Natola: Kharyl's states came seconds after his two minutes, and
    Franklin was tagged "No card yet" for a card made straight after. Tagged
    only when it's still waiting AGED_PING_AFTER on - and once."""
    _note_is(monkeypatch, lambda config, **kw: "")
    order.fields = {"states": "", "leads": ""}
    message = _aged_post()
    asyncio.run(client._aged_from_invoice(_bot(message), message, wait=0))
    [first] = [one for one, _v in order.responder.sent if "No card yet" in str(one)]
    assert f"<@{FRANKLIN}>" not in first and "tag you" in first
    assert ordercards.load()["950"]["said"] == "quiet"

    tried = []

    async def retry(bot, key, *, settle=30):
        tried.append(key)

    monkeypatch.setattr(client, "_order_retry", retry)
    monkeypatch.setattr(client, "_order_responder", lambda bot: order.responder)
    monkeypatch.setattr(client, "_ORDER_PENDING", {})
    held = ordercards.load()
    held["950"]["waiting_since"] = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    ordercards.save(held)
    asyncio.run(client._order_sweep(_bot(message)))
    assert not [one for one, _v in order.responder.sent if f"<@{FRANKLIN}>" in str(one)], "not yet"

    held = ordercards.load()
    held["950"]["waiting_since"] = (datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat()
    ordercards.save(held)
    asyncio.run(client._order_sweep(_bot(message)))
    asyncio.run(client._order_sweep(_bot(message)))
    tagged = [one for one, _v in order.responder.sent if f"<@{FRANKLIN}>" in str(one)]
    assert len(tagged) == 1 and "still no the states or how many leads" in tagged[0] and "No card yet" in tagged[0]
    assert tried == ["950"], "looked at once more before tagging"


def test_an_aged_order_made_before_its_tag_isnt_tagged(order, monkeypatch):
    _note_is(monkeypatch, lambda config, **kw: "")
    order.fields = {"states": "", "leads": ""}
    message = _aged_post()
    asyncio.run(client._aged_from_invoice(_bot(message), message, wait=0))
    order.fields = {}
    message.thread = _notes("25 aged IUL, AZ TX")
    monkeypatch.setattr(client, "_order_responder", lambda bot: order.responder)
    monkeypatch.setattr(client, "_ORDER_PENDING", {})
    held = ordercards.load()
    held["950"]["waiting_since"] = (datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat()
    ordercards.save(held)
    asyncio.run(client._order_sweep(_bot(message)))
    assert ordercards.load()["950"]["status"] == "made"
    assert not [one for one, _v in order.responder.sent if f"<@{FRANKLIN}>" in str(one)]


def test_a_spark_pool_membership_isnt_ad_spend_and_its_discord_dates_are_read(monkeypatch):
    """FND Life's second pool: "Spark Standard", "No cap (ad spend)" and no
    launch date - held for "the order doesn't say Phoenix / ad spend". The
    post's dates came as Discord's <t:1792900800:D>."""
    import sys
    import types

    class Anthropic:
        def __init__(self, **kw):
            self.messages = SimpleNamespace(create=lambda **kw: "reply")

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=Anthropic))
    monkeypatch.setattr(jobs, "_tool_input", lambda response, name: {
        "kind": "fresh", "name": "FND Life", "email": "join@example.com", "states": "AL, AZ, AR",
        "package": "Spark Standard", "leads": "No cap (ad spend)"})
    config = SimpleNamespace(secrets=SimpleNamespace(anthropic_api_key="k", require=lambda *a: None),
                             copy=SimpleNamespace(model="claude-opus-5-5"))
    text = ("[10/22 01:50AM] Capture Info: 🛒 New lead pool member on Spark\n"
            "IUL - Volume — $700/wk · pool membership\n"
            "A buyer just joined and paid for your pool on Spark.\nBuyer FND Life\nPool IUL - Volume\n"
            "Order $700/wk · pool membership\nCPL range $14–$18\nExpected leads / wk 38–50\n"
            "Starts <t:1792900800:D>\nPaid through <t:1793505600:D>\nStates AL, AZ, AR\n"
            "Buyer email join@example.com")
    card = jobs.read_order(config, text=text, images=[], today=date(2026, 10, 22),
                           notes=["Purchased on Spark - pool membership.\nStarts - <t:1792900800:D>"])
    fields = card["fields"]
    assert fields["package"] == "IUL - Volume" and fields["leads"] == "38-50" and fields["agency"] == "Spark"
    assert card["launch_iso"] == "2026-10-25"
    assert "<t:" not in card["desc"] and "October 25, 2026" in card["desc"]
    wrong, _missing = ordercards.doubts(card, today=date(2026, 10, 22))
    assert not any("ad spend" in one or "Launch" in one for one in wrong), wrong


def test_discord_dates_are_written_out():
    assert jobs.discord_times("Starts <t:1792900800:D>") == "Starts October 25, 2026"
    assert jobs.discord_times("at <t:1792900800:t>") == "at 12:00 AM"
    assert jobs.discord_times("no date here") == "no date here"


def test_a_spark_buyer_is_the_name_and_starts_is_the_launch(monkeypatch):
    """"you dont include "buyer" as their name, its just Everlife" - the card
    went out as "New Agent - Buyer Everlife", with no launch date."""
    import sys
    import types

    class Anthropic:
        def __init__(self, **kw):
            self.messages = SimpleNamespace(create=lambda **kw: "reply")

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=Anthropic))
    monkeypatch.setattr(jobs, "_tool_input", lambda response, name: {
        "kind": "fresh", "name": "Buyer Everlife", "email": "", "states": "AZ, AR, CA",
        "package": "Pool Mortgage Protection - Volume", "leads": "", "launch": "Sunday, November 1"})
    config = SimpleNamespace(secrets=SimpleNamespace(anthropic_api_key="k", require=lambda *a: None),
                             copy=SimpleNamespace(model="claude-opus-5-5"))
    text = ("[10/22 09:50AM] Capture Info: 🔁 Lead pool member paid for another week on Spark\n"
            "Mortgage Protection - Volume — $700/wk · pool membership\n"
            "This member paid for another week of your pool on Spark.\nBuyer Everlife\n"
            "Pool Mortgage Protection - Volume\nLead type mortgage-protection-volume\n"
            "Order $700/wk · pool membership\nCPL range $18–$22\nExpected leads / wk 31–38\n"
            "Starts <t:1792900800:D>\nPaid through <t:1793505600:D>\nStates AZ, AR, CA\n"
            "Buyer email everlife@example.com")
    card = jobs.read_order(config, text=text, images=[], today=date(2026, 10, 22), notes=[])
    fields = card["fields"]
    assert fields["name"] == "Everlife" and card["name"] == "New Agent - Everlife"
    assert fields["email"] == "everlife@example.com"
    assert fields["package"] == "Mortgage Protection - Volume" and fields["leads"] == "31-38"
    assert card["launch_iso"] == "2026-10-25", "Starts, not Paid through"


def test_a_payment_not_in_the_shared_copy_is_read_fresh(monkeypatch):
    """Remi Sampson paid by card and was in Payra; the shared copy, read a
    minute before, didn't have it - "Couldn't find this payment in Payra"."""
    from wilbyte import payraapi

    remi = {"_id": "r1", "amount": 315, "status": "Captured", "customer_display": "Remi Sampson",
            "payment_type": "card", "display_name": "Mastercard ****1111", "paid_on": "2026-10-22T14:48:00Z"}
    asked = []

    class Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def changed(self, kind, after):
            asked.append(after)
            return {"records": [remi]}

    monkeypatch.setattr(payraapi, "PayraClient", Client)
    monkeypatch.setattr(jobs, "payra_recent", lambda config, kind, now=None: {"old": {"id": "old"}})
    config = SimpleNamespace(secrets=SimpleNamespace(payra_api_token="t", payra_site_id="s"))
    now = datetime(2026, 10, 22, 14, 50, tzinfo=timezone.utc)
    found = jobs.payra_payment_for(config, name="Remi Sampson", amount="315.00", now=now)
    assert found["id"] == "r1" and found["how"].startswith("Mastercard")
    assert len(asked) == 1


def test_the_recent_read_reaches_past_five_pages(monkeypatch):
    from wilbyte import payraapi

    pages = [{"records": [{"_id": f"p{at}", "paid_on": "2026-10-22T10:00:00Z"}], "next_updated_after": f"t{at}"}
             for at in range(8)] + [{"records": []}]

    class Client:
        def __init__(self, *a, **k):
            self.at = 0

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def changed(self, kind, after):
            self.at += 1
            return pages[self.at - 1]

    monkeypatch.setattr(payraapi, "PayraClient", Client)
    monkeypatch.setattr(jobs, "_PAYRA_RECENT", {})
    config = SimpleNamespace(secrets=SimpleNamespace(payra_api_token="t", payra_site_id="s"))
    assert "p7" in jobs.payra_recent(config, "payments")


def test_check_aged_lines_leaves_another_lead_types_invoice_alone(monkeypatch):
    """Shyanne Tower's Vets card was held up against her FEX invoice."""
    flagged, _ = _one_line_case(
        monkeypatch, "Lead Type: Text-Verified Aged Veteran Leads - Aged Vet 90-180 days\n"
                     "Number of Leads Requested: 50\nStates: WA, TX, LA, TN\n",
        "Lead Type: Aged Final Expense - 90-180+ days\nStates Leads In: WY, TN, SC, TX\n"
        "Number of Requested Leads: 50")
    assert flagged == []


def test_check_aged_lines_takes_their_invoices_counts_together(monkeypatch):
    """Preston Pennington's 200 was two invoices of 100."""
    made = int(datetime(2026, 10, 19, 14, 42, tzinfo=timezone.utc).timestamp())
    card = {"id": f"{made:08x}0000000000000005", "name": "Aged Leads - Preston Pennington", "shortUrl": "u",
            "desc": "Email: p2@example.com\nLead Type: Aged IUL 180+\nNumber of Leads Requested: 200\nStates: VA\n"}
    one = {"id": "a", "number": "INV-A", "name": "Preston Pennington", "email": "p2@example.com", "lines": [],
           "created": "2026-10-19T14:30:00Z", "description": "",
           "notes": "Lead Type: Aged IUL - 180+ days\nStates Leads In: VA\nNumber of Requested Leads: 100"}
    two = dict(one, id="b", number="INV-B", created="2026-10-19T14:35:00Z")
    config = _aged_board(monkeypatch, [card])
    flagged, _ = jobs.check_aged_lines(config, now=datetime(2026, 10, 19, 23, tzinfo=timezone.utc),
                                       invoices=[one, two])
    assert flagged == []


def test_cards_marked_checked_arent_named_again(monkeypatch):
    """"this was done" - Patrick Moreno and Mateo Lyon, named every time."""
    config = _aged_board(monkeypatch, [dict(_aged_card("AL, CA", "Aged Vets 30-90"),
                                            shortUrl="https://trello.com/c/VlGannbA")])
    flagged, _ = jobs.check_aged_lines(config, now=datetime(2026, 10, 19, 23, tzinfo=timezone.utc),
                                       invoices=[MATEO_INVOICE])
    assert len(flagged) == 1
    assert jobs.aged_lines_mark_ok("aged lines ok <https://trello.com/c/VlGannbA>") == ["VlGannbA"]
    flagged, looked = jobs.check_aged_lines(config, now=datetime(2026, 10, 19, 23, tzinfo=timezone.utc),
                                            invoices=[MATEO_INVOICE])
    assert flagged == [] and looked == 0
    assert mentions.parse("<@1> aged lines ok https://trello.com/c/VlGannbA").action == "agedlinesok"
    assert mentions.parse("<@1> check aged lines").action == "agedlines"


def test_an_email_trello_linked_isnt_new():
    """Cole Petersen: "📝 New in Discord since this card was made: Email:
    cole.petersen@..." - the address the card already had, as Trello links it."""
    on_card = "Name: Cole Petersen\nEmail: [cole@example.com](mailto:cole@example.com)\nAgency: everlife"
    ours = "Name: Cole Petersen\nEmail: cole@example.com\nAgency: everlife"
    assert ordercards.changed_lines(on_card, ours) == []
    assert ordercards.same_text(on_card, ours)
    assert ordercards.changed_lines(on_card, ours.replace("everlife", "Spark")) == ["Agency: Spark"]


def test_an_email_and_phone_on_lines_of_their_own_go_on_the_card(monkeypatch):
    """Aiden Adair: his invoice note had "+1 (801) 555-0141" and his email
    on lines of their own, somebody else paid, and the card had neither."""
    import sys
    import types

    class Anthropic:
        def __init__(self, **kw):
            self.messages = SimpleNamespace(create=lambda **kw: "reply")

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=Anthropic))
    monkeypatch.setattr(jobs, "_tool_input", lambda response, name: {
        "kind": "fresh", "name": "Aiden Adair", "email": "boss@example.com", "phone": "+15550109999",
        "states": "AL, AR", "package": "Text-Verified Mortgage Protection Leads", "leads": "50"})
    config = SimpleNamespace(secrets=SimpleNamespace(anthropic_api_key="k", require=lambda *a: None),
                             copy=SimpleNamespace(model="claude-opus-5-5"))
    text = ("[10/22 01:00PM] Nova: New Payment | Payra\nNAME: Boss Person\nEMAIL: boss@example.com\n"
            "PHONE: +15550109999\nAMOUNT: $1,449.00\nPRODUCT: Text-Verified Mortgage Protection Leads\n"
            "Notes:\n> Ok I want 50 fresh mtg Tex verified standard they should be $28 a lead.\n"
            "> Aiden Adair\n> +1 (801) 555-0141\n> Aiden.adair@example.com\n> EVERLIFE\n> Alabama\n"
            "> Arkansas\n> Live Sat\n> Oct 24\nInternal Notes:\n> (none)")
    card = jobs.read_order(config, text=text, images=[], today=date(2026, 10, 22), notes=[])
    fields = card["fields"]
    assert fields["name"] == "Aiden Adair"
    assert fields["email"] == "Aiden.adair@example.com"
    assert fields["phone"] == "+1 (801) 555-0141"


def test_unlabelled_contact_isnt_guessed_from_two():
    fields = jobs.with_unlabelled_contact({"email": "", "phone": ""},
                                          ["a@example.com\nb@example.com\n801-555-0141\n801-555-0142"],
                                          "New Payment\nEMAIL: payer@example.com")
    assert fields == {"email": "", "phone": ""}


def test_an_overflow_order_is_a_new_agents_card_on_aged_leads(monkeypatch):
    """"if it says overflow, take it to aged leads ... it'll be treated as new
    agent but on aged leads" - Christa Arriaga's card went to In Que."""
    import sys
    import types

    class Anthropic:
        def __init__(self, **kw):
            self.messages = SimpleNamespace(create=lambda **kw: "reply")

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=Anthropic))
    monkeypatch.setattr(jobs, "_tool_input", lambda response, name: {
        "kind": "fresh", "name": "Christa Arriaga", "email": "christa@example.com", "phone": "956-555-0160",
        "states": "TX, KS, IL, VA, OH, MI", "package": "Text-Verified IUL leads - Overflow · IUL Standard 20 Leads",
        "leads": "20"})
    config = SimpleNamespace(secrets=SimpleNamespace(anthropic_api_key="k", require=lambda *a: None),
                             copy=SimpleNamespace(model="claude-opus-5-5"))
    text = ("New Payment | Payra\nNAME: Christa Arriaga\nEMAIL: christa@example.com\nPHONE: 9565550160\n"
            "AMOUNT: $400.00\nPRODUCT: Text-Verified IUL leads - Overflow\n"
            "Notes:\n> (no notes on the invoice)\n[10/22 12:40PM] Kharyl: Christa Arriaga\n"
            "TX, KS, IL, VA, OH, MI\nOverflow · IUL Standard 20 Leads\nTAKEOVER")
    card = jobs.read_order(config, text=text, images=[], today=date(2026, 10, 22), notes=[])
    assert card["kind"] == "fresh" and card["name"] == "New Agent - Christa Arriaga" and card["overflow"]

    made = []

    class Board:
        def board_lists(self, board_id):
            return [{"id": "q", "name": "In Que"}, {"id": "a", "name": "Aged Leads Order Done"}]

        def close(self):
            pass

    monkeypatch.setattr(jobs, "open_trello", lambda cfg: Board())
    monkeypatch.setattr(jobs, "_create_once", lambda client, list_id, name, position: (
        made.append((list_id, position)) or {"id": "c1", "shortUrl": "u"}))
    monkeypatch.setattr(jobs, "_described", lambda client, card_id, card: [])
    board_config = SimpleNamespace(secrets=SimpleNamespace(trello_board_id="b", order_fresh_list="",
                                                           order_aged_list=""))
    got = jobs.order_card_make(board_config, "fresh", card)
    assert made == [("a", "bottom")] and got["list"] == "Aged Leads Order Done"



DAWSON = ("[10/21 06:59PM] Nova: @here\nNew Payment | Payra\nNAME: Dawson Clevenger\nEMAIL: dawson@example.com\n"
          "PHONE: +15555550171\nAMOUNT: $279.45\nPRODUCT: Text-Veified Aged IUL Leads\n"
          "INVOICE: INV-20313 · card •••• 8002 · 10/21/2026 5:59 PM\n**Subtotal:** $130.00\n"
          "**Automatic Dual Pricing:** $4.55\n**Total:** $134.55\n**Notes:**\n> Name: Dawson Clevenger\n"
          "> Email: dawson@example.com\n> Phone Number: +15555550171\n"
          "> Lead Type: Aged IUL - 180+ days [AGED - $1/Lead]\n> States Leads In: Idaho\n"
          "> Number of Requested Leads: 130\n**Internal Notes:**\n> (none)")

DAWSON_OTHERS = [
    {"id": "i14", "number": "INV-20314", "name": "Dawson Clevenger", "email": "dawson@example.com", "total": 57.96,
     "notes": "Name: Dawson Clevenger\nLead Type: Aged IUL - 30-90 days [AGED - $8/Lead]\n"
              "States Leads In: Idaho, texas, michigan, ohio, virginia, nevada, minnesota\n"
              "Number of Requested Leads: 10", "created": "2026-10-20T15:00:00Z"},
    {"id": "i15", "number": "INV-20315", "name": "Dawson Clevenger", "email": "dawson@example.com", "total": 86.94,
     "notes": "Name: Dawson Clevenger\nLead Type: Aged IUL - 90-180 days [AGED - $6/Lead]\n"
              "States Leads In: Idaho, ohio, michigan, minnesota, Nevada, Texas, Virginia\n"
              "Number of Requested Leads: 20", "created": "2026-10-20T15:01:00Z"},
]


def test_a_charge_bigger_than_its_invoice_is_seen():
    assert ordercards.post_charge(DAWSON) == (27945, 13455)
    assert ordercards.post_charge("AMOUNT: $152.14\n**Subtotal:** $147.00\n**Total:** $152.14") == (15214, 15214)
    assert ordercards.post_charge("AMOUNT: $50") == (5000, None)


def test_the_other_invoices_one_charge_paid_are_found(monkeypatch):
    now = datetime(2026, 10, 21, 23, 5, tzinfo=timezone.utc)
    own = {"id": "i13", "number": "INV-20313", "name": "Dawson Clevenger", "email": "dawson@example.com",
           "total": 134.55, "notes": "Lead Type: Aged IUL - 180+ days", "created": "2026-10-20T14:59:00Z"}
    stranger = {"id": "x", "number": "INV-20316", "name": "Somebody Else", "total": 57.96,
                "notes": "x", "created": "2026-10-20T15:00:00Z"}
    invoices = {one["id"]: one for one in [own, *DAWSON_OTHERS, stranger]}
    payments = {"p": {"id": "p", "customer": "Dawson Clevenger", "amount": 279.45,
                      "paid_on": "2026-10-21T22:59:00Z", "invoice_ids": ["i13", "i14", "i15"]}}
    monkeypatch.setattr(jobs, "payra_recent", lambda config, kind, now=None: invoices if kind == "invoices" else payments)
    paid_at = datetime(2026, 10, 21, 22, 59, tzinfo=timezone.utc)
    got = jobs.payra_same_charge(CONFIG, name="Dawson Clevenger", email="dawson@example.com", amount="279.45",
                                 number="INV-20313", rest=27945 - 13455, paid_at=paid_at, now=now)
    assert [one["number"] for one in got] == ["INV-20314", "INV-20315"]

    # The payment not listing them: theirs whose totals make up the rest.
    payments.clear()
    got = jobs.payra_same_charge(CONFIG, name="Dawson Clevenger", email="dawson@example.com", amount="279.45",
                                 number="INV-20313", rest=27945 - 13455, paid_at=paid_at, now=now)
    assert [one["number"] for one in got] == ["INV-20314", "INV-20315"]
    assert jobs.payra_same_charge(CONFIG, name="Dawson Clevenger", amount="279.45", number="INV-20313",
                                  rest=5000, paid_at=paid_at, now=now) == []


def test_dawson_every_invoice_of_the_charge_is_a_line_on_his_aged_card(monkeypatch):
    """"we missed dawasons payment" - $279.45 paid three invoices, and his
    card had only the 130 of the 180+ in Idaho."""
    import sys
    import types

    class Anthropic:
        def __init__(self, **kw):
            self.messages = SimpleNamespace(create=lambda **kw: "reply")

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=Anthropic))
    monkeypatch.setattr(jobs, "_tool_input", lambda response, name: {
        "kind": "aged", "name": "Dawson Clevenger", "states": "ID", "email": "dawson@example.com",
        "package": "Aged IUL - 180+ days", "leads": "130"})
    config = SimpleNamespace(secrets=SimpleNamespace(anthropic_api_key="k", require=lambda *a: None),
                             copy=SimpleNamespace(model="claude-opus-5-5"))
    notes = [jobs.same_charge_said(one) for one in DAWSON_OTHERS]
    card = jobs.read_order(config, text=DAWSON, images=[], today=date(2026, 10, 21), notes=notes)
    assert [(one["package"], one["leads"]) for one in card["parts"]] == [
        ("Aged IUL - 180+ days", "130"), ("Aged IUL - 30-90 days", "10"), ("Aged IUL - 90-180 days", "20")]
    assert card["fields"]["states"] == "ID" and card["fields"]["leads"] == "130"
    [aged] = jobs.split_order(card, today=date(2026, 10, 21))
    fields = aged["fields"]
    assert fields["leads"] == "160 (130 + 10 + 20)"
    assert "Aged IUL - 30-90 days: ID, TX, MI, OH, VA, NV, MN" in fields["states"]
    assert "Aged IUL - 90-180 days: ID, OH, MI, MN, NV, TX, VA" in fields["states"]
    assert jobs.split_doubts([aged], today=date(2026, 10, 21)) == []


def test_dawson_the_aged_card_is_made_with_the_other_invoices(order, monkeypatch):
    _note_is(monkeypatch, lambda config, **kw: "")
    asked = {}

    def same(config, **kw):
        asked.update(kw)
        return DAWSON_OTHERS

    monkeypatch.setattr(jobs, "payra_same_charge", same)
    message = _message(DAWSON)
    assert asyncio.run(client._aged_from_invoice(_bot(message), message, wait=0)) == "made"
    assert asked["number"] == "INV-20313" and asked["rest"] == 27945 - 13455
    assert [one.split(")")[0] for one in order.notes] == [
        "Payra invoice note: (paid in the same charge - INV-20314, $57.96",
        "Payra invoice note: (paid in the same charge - INV-20315, $86.94"]
    assert ordercards.load()["900"]["also_invoices"] == ["INV-20314", "INV-20315"]
    assert not any("can't find" in one[0] for one in order.responder.sent)

    # Nova posting INV-20314 on its own afterwards: no second card.
    later = _message(DAWSON.replace("INV-20313", "INV-20314").replace("$279.45", "$57.96"))
    later.id = 901
    made = len(order.made)
    assert asyncio.run(client._aged_from_invoice(_bot(later), later, wait=0)) == "already"
    assert len(order.made) == made
    assert "already on their card" in order.responder.sent[-1][0]


def test_more_charged_than_ryte_can_find_is_said(order, monkeypatch):
    _note_is(monkeypatch, lambda config, **kw: "")
    monkeypatch.setattr(jobs, "payra_same_charge", lambda config, **kw: [])
    client._SAME_CHARGE_SAID.clear()
    message = _message(DAWSON)
    asyncio.run(client._aged_from_invoice(_bot(message), message, wait=0))
    said = [one[0] for one in order.responder.sent if "can't find" in one[0]]
    assert len(said) == 1 and "$279.45" in said[0] and "$144.90 paid for something" in said[0]


def test_the_audit_finds_invoices_a_shared_charge_left_without_a_card(monkeypatch):
    """"yes, check for other missed ones" - Dawson Clevenger's two, from his
    post; Ahmed Ghazi's two from Payra's payment, each on its own card."""
    own = {"id": "i13", "number": "INV-20313", "name": "Dawson Clevenger", "email": "dawson@example.com",
           "total": 134.55, "notes": "Lead Type: Aged IUL - 180+ days\nStates Leads In: Idaho\n"
                                      "Number of Requested Leads: 130", "created": "2026-10-20T14:59:00Z"}
    ahmed = [{"id": f"a{n}", "number": f"INV-200{n}", "name": "Ahmed Ghazi", "total": total,
              "notes": f"Lead Type: Aged FEX - 180+ days\nStates Leads In: TX\nNumber of Requested Leads: {count}",
              "created": "2026-10-20T10:00:00Z"} for n, total, count in ((39, 225.0, 225), (40, 200.0, 200))]
    invoices = {one["id"]: one for one in [own, *DAWSON_OTHERS, *ahmed]}
    payments = {"pa": {"id": "pa", "customer": "Ahmed Ghazi", "amount": 425.0, "paid_on": "2026-10-21T15:00:00Z",
                       "invoice_ids": ["a39", "a40"]}}
    monkeypatch.setattr(jobs, "payra_kept", lambda config, kind, now=None: invoices if kind == "invoices" else payments)
    monkeypatch.setattr(jobs, "payra_recent", lambda config, kind, now=None: invoices if kind == "invoices" else payments)
    cards = [{"name": "Aged Leads - Dawson Clevenger", "url": "https://trello.com/c/ptI5Z4OV",
              "desc": "Name: Dawson Clevenger\nPhone Number: +15555550171\nLead Type: Text-Veified Aged IUL Leads - "
                      "Aged IUL 180+ days\nNumber of Leads Requested: 130\nStates: ID"},
             {"name": "Aged Leads - Ahmed Ghazi", "url": "https://trello.com/c/a39",
              "desc": "Lead Type: Aged FEX 180+\nNumber of Leads Requested: 225\nStates: TX"},
             {"name": "Aged Leads - Ahmed Ghazi", "url": "https://trello.com/c/a40",
              "desc": "Lead Type: Aged FEX 180+\nNumber of Leads Requested: 200\nStates: TX"}]
    post = _message(DAWSON)
    post.created_at = datetime(2026, 10, 21, 22, 59, tzinfo=timezone.utc)
    since = datetime(2026, 10, 21, 4, 0, tzinfo=timezone.utc)
    missed, lines = asyncio.run(client._audit_several(_bot(post), [post], cards, {}, since))
    said = "\n".join(lines)
    assert missed == 2, said
    assert "INV-20313 ($134.55) 130 · Aged IUL - 180+ days · Idaho — <https://trello.com/c/ptI5Z4OV>" in said
    assert "INV-20314 ($57.96) 10 · Aged IUL - 30-90 days" in said and said.count("**not on any card**") == 2
    assert "✅ **Ahmed Ghazi** — $425.00" in said and "<https://trello.com/c/a40>" in said
    # Its lines put on the card since: nothing missed.
    cards[0]["desc"] += "\nAged IUL - 30-90 days: ID, TX\nAged IUL - 90-180 days: ID, OH\n160 (130 + 10 + 20)"
    assert asyncio.run(client._audit_several(_bot(post), [post], cards, {}, since))[0] == 0


def test_invoices_without_the_card_fee_still_make_up_the_charge(monkeypatch):
    """Payra keeps Dawson's invoices at $130, $56 and $84 - the 3.5% card fee
    goes on at the charge - and the audit said "$144.90 ... no invoice found".
    Gavin Casella's $1,035 named two subscription invoices that don't come to
    it: not one charge for both."""
    own = {"id": "i13", "number": "INV-20313", "name": "Dawson Clevenger", "total": 130.0,
           "notes": "Lead Type: Aged IUL - 180+ days\nNumber of Requested Leads: 130", "created": "2026-10-20T14:59:00Z"}
    others = [dict(DAWSON_OTHERS[0], total=56.0), dict(DAWSON_OTHERS[1], total=84.0)]
    gavin = [{"id": f"g{n}", "number": number, "name": "Gavin Casella", "total": total, "notes": "",
              "created": "2026-10-15T10:00:00Z"} for n, number, total in ((1, "SFVFF9Y-1", 1000.0), (2, "SKURX9G-1", 1050.0))]
    invoices = {one["id"]: one for one in [own, *others, *gavin]}
    payments = {"pg": {"id": "pg", "customer": "Gavin Casella", "amount": 1035.0, "paid_on": "2026-10-22T17:18:00Z",
                       "invoice_ids": ["g1", "g2"]}}
    monkeypatch.setattr(jobs, "payra_kept", lambda config, kind, now=None: invoices if kind == "invoices" else payments)
    paid_at = datetime(2026, 10, 21, 22, 59, tzinfo=timezone.utc)
    got = jobs.charges_of_several(CONFIG, since=datetime(2026, 10, 16, tzinfo=timezone.utc), posts=[
        {"name": "Dawson Clevenger", "email": "dawson@example.com", "amount": "279.45", "amount_cents": 27945,
         "total": 13455, "number": "INV-20313", "paid_at": paid_at, "link": ""}],
        now=datetime(2026, 10, 22, 20, 0, tzinfo=timezone.utc))
    assert [[one["number"] for one in entry["invoices"]] for entry in got] == [["INV-20313", "INV-20314", "INV-20315"]]
    assert got[0]["unplaced"] == 0
    assert jobs.charge_left(14490, 14000) == 0 and jobs.charge_left(14490, 5600) == 14490 - 5796


def test_the_audit_names_a_charge_that_looks_made_twice():
    def post(i, text, minute):
        return SimpleNamespace(id=i, content=text, embeds=[], message_snapshots=[], author=SimpleNamespace(bot=True),
                               created_at=datetime(2026, 10, 22, 22, minute, tzinfo=timezone.utc),
                               jump_url=f"https://discord.com/channels/1/700/{i}", reactions=[])

    posts = [post(1, "New Payment | Payra\nNAME: Gauri Beaston\nAMOUNT: $336.38", 27),
             post(2, "New Payment | Payra\nNAME: Gauri Beaston\nAMOUNT: $336.36", 27),
             post(3, "New Payment | Payra\nNAME: Gauri Beaston\nAMOUNT: $289.80", 30),
             post(4, "New Payment | Payra\nNAME: Somebody Else\nAMOUNT: $336.38", 28)]
    said = "\n".join(client._audit_doubles(posts))
    assert said.count("**Gauri Beaston**") == 1 and "$336.38" in said and "$336.36" in said
    assert "Somebody Else" not in said and "$289.80" not in said
    assert client._audit_doubles(posts[2:]) == []
    # Bram Gutierrez's three: one line. Matthew Odierno's two products and
    # Wiser Charles' two invoices: two orders, not named.
    bram = [post(10 + at, "New Payment | Payra\nNAME: Bram Gutierrez\nAMOUNT: $298.08\nPRODUCT: Trucker", minute)
            for at, minute in enumerate((44, 46, 47))]
    said = "\n".join(client._audit_doubles(bram))
    assert said.count("Bram Gutierrez") == 1 and "3 charges" in said
    odierno = [post(20, "New Payment | Payra\nNAME: Matthew Odierno\nAMOUNT: $1035.00\nPRODUCT: Trucker IUL", 28),
               post(21, "New Payment | Payra\nNAME: Matthew Odierno\nAMOUNT: $1035.00\nPRODUCT: Ad Spend", 32)]
    assert client._audit_doubles(odierno) == []
    wiser = [post(30, "New Payment | Payra\nNAME: Wiser Charles\nAMOUNT: $103.5\nPRODUCT: Aged Leads (VET)", 35),
             post(31, "New Payment | Payra\nNAME: Wiser Charles\nAMOUNT: $103.5\nPRODUCT: Aged Leads (VET)", 38)]
    assert client._audit_doubles(wiser, held={"30": {"invoice_id": "i57"}, "31": {"invoice_id": "i58"}}) == []
    assert len(client._audit_doubles(wiser)) == 3


def test_an_aged_count_off_the_invoice_line(monkeypatch):
    """Lajuana Jackson's INV-20471: "Aged vets 30-90", ten states, no count
    in the notes - and QTY 50 on the invoice. Held for "how many leads"."""
    invoices = {"i": {"id": "i", "number": "INV-20471", "lines": [
        {"name": "Text-Verified Aged Veteran Leads", "quantity": 50, "price": 7.13}]},
        "p": {"id": "p", "number": "INV-20472", "lines": [{"name": "Lead Package", "quantity": 1}]},
        "two": {"id": "two", "number": "INV-20473", "lines": [
            {"name": "Aged Vet Leads", "quantity": 20}, {"name": "Aged FEX Leads", "quantity": 30}]}}
    monkeypatch.setattr(jobs, "payra_kept", lambda config, kind, now=None: invoices)
    count, line = jobs.invoice_line_count(CONFIG, "INV-20471")
    assert count == 50 and "× 50" in line
    assert jobs.invoice_line_count(CONFIG, "INV-20472") is None, "1 package isn't a lead count"
    assert jobs.invoice_line_count(CONFIG, "INV-20473") is None, "two lines - which count?"
    assert jobs.invoice_line_count(CONFIG, "INV-99999") is None

    message = _message("New Payment | Payra\nNAME: Lajuana Jackson\nAMOUNT: $253.57\n"
                       "PRODUCT: Text-Verified Aged Veteran Leads\nINVOICE: INV-20471 · card •••• 1111\n"
                       "**Notes:**\n> Name: Lajuana Jackson\n> Lead type: Aged vets 30-90\n"
                       "> States: GA MD HI TX FL MI OH TN VA SC\n> Notes: GTC")
    card = {"kind": "aged", "fields": {"name": "Lajuana Jackson", "package": "Aged vets 30-90",
                                       "states": "GA, MD, HI, TX, FL, MI, OH, TN, VA, SC", "leads": ""},
            "source": message.content, "missing": ["Number of Leads Requested"]}
    filled = asyncio.run(client._count_off_the_invoice(_bot(message), message, card))
    assert filled["fields"]["leads"] == "50" and client._aged_gaps(filled) == []
    assert "Number of Leads Requested: 50" in filled["desc"] and filled["missing"] == []
    assert not any("50" in one and "isn't" in one for one in ordercards.doubts(filled, today=date(2026, 10, 23))[0])


def test_jake_scotts_count_beside_the_lead_type(monkeypatch):
    """"90, 30-90 aged vets" in the Zelle notes, and the card held for "how
    many leads"."""
    import sys
    import types

    assert jobs.count_beside_lead_type("90, 30-90 aged vets") == 90
    assert jobs.count_beside_lead_type("Aged Vets 30-90") is None, "the age range isn't the count"
    assert jobs.count_beside_lead_type("Lead Type: Aged IUL - 180+ days [AGED - $1/Lead]") is None
    assert jobs.count_beside_lead_type("816-730-0147 vets") is None

    class Anthropic:
        def __init__(self, **kw):
            self.messages = SimpleNamespace(create=lambda **kw: "reply")

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=Anthropic))
    monkeypatch.setattr(jobs, "_tool_input", lambda response, name: {
        "kind": "aged", "name": "Jake Scott", "states": "MD, CA, FL, GA, TX", "agency": "AEP",
        "package": "Aged Vets 30-90", "leads": ""})
    config = SimpleNamespace(secrets=SimpleNamespace(anthropic_api_key="k", require=lambda *a: None),
                             copy=SimpleNamespace(model="claude-opus-5-5"))
    text = ("[10/23 06:50PM] Nova: New Payment | Zelle\nNAME: Jake Scott\nAMOUNT: $300\n"
            "PRODUCT: — (match to their order)\n[10/23 06:50PM] Kharyl: Paid via Zelle (Chase)\nJake scott\n"
            "js.aep@example.com\n555-010-0147\n90, 30-90 aged vets\nMD,CA,FL,GA,TX\naep")
    card = jobs.read_order(config, text=text, images=[], today=date(2026, 10, 23))
    assert card["fields"]["leads"] == "90"
    assert client._aged_gaps(card) == []
    assert not any("90" in one for one in ordercards.doubts(card, today=date(2026, 10, 23))[0])


def test_overflow_typed_in_a_hurry():
    """Karli Kelley's "OVERLFLOW BC" went to the Queue."""
    for typed in ("OVERLFLOW BC", "overflow", "Over flow", "over-flow", "OVERFLW", "overflowed"):
        assert ordercards.OVERFLOW.search(typed), typed
    for other in ("overall flow", "workflow", "over follow", "Flow state"):
        assert not ordercards.OVERFLOW.search(other), other


def test_the_payer_under_a_nickname_is_the_payer(monkeypatch):
    """Jacob Martin paid; the notes say "Name: jacoby" with his own email -
    the card went out as jacoby, no email or phone, and was held."""
    import sys
    import types

    class Anthropic:
        def __init__(self, **kw):
            self.messages = SimpleNamespace(create=lambda **kw: "reply")

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=Anthropic))
    monkeypatch.setattr(jobs, "_tool_input", lambda response, name: {
        "kind": "aged", "name": "jacoby", "states": "MO, KS, OH, TX", "package": "Aged Vets 30-90 days",
        "leads": "20"})
    config = SimpleNamespace(secrets=SimpleNamespace(anthropic_api_key="k", require=lambda *a: None),
                             copy=SimpleNamespace(model="claude-opus-5-5"))
    text = ("[10/24 10:13AM] Nova: New Payment | Payra\nNAME: Jacob Martin\nEMAIL: jmartin@example.com\n"
            "PHONE: +15555550199\nAMOUNT: $101.43\nPRODUCT: Text-Verified Aged Veteran Leads\n"
            "**INVOICE:** INV-20488 · card •••• 8622\n**Notes:**\n> Name: jacoby\n> Email: jmartin@example.com\n"
            "> Lead Type: Aged Vets 30-90 days\n> States Leads In: MO, KS, OH, TX\n"
            "> Number of Requested Leads: 20\n**Internal Notes:**\n> (none)")
    card = jobs.read_order(config, text=text, images=[], today=date(2026, 10, 24))
    fields = card["fields"]
    assert card["client"] == "Jacob Martin" and fields["email"] == "jmartin@example.com"
    assert fields["phone"] == "+15555550199"
    assert ordercards.doubts(card, today=date(2026, 10, 24))[0] == []
    # Somebody else's email under the other name: still somebody else.
    note = "Name: jacoby\nEmail: someone@example.com\nStates Leads In: MO"
    assert not jobs._same_contact_as_payer(text, [note], "jacoby")
    assert jobs._same_contact_as_payer(text, [note.replace("someone", "jmartin")], "jacoby")


def test_an_agency_off_their_earlier_card(monkeypatch):
    """"it has no agency?" - Jacob Martin's re-order: nothing in the notes or
    #lead-lab, his earlier card says AEP."""
    cards = [
        {"id": "6a000000aaaaaaaaaaaaaaaa", "name": "Aged Leads - Jacob Martin",
         "desc": "Name: Jacob Martin\nEmail: jmartin@example.com\nAgency: AEP"},
        {"id": "6a100000bbbbbbbbbbbbbbbb", "name": "New Agent - Jacob Martinez",
         "desc": "Name: Jacob Martinez\nEmail: other@example.com\nAgency: Wolfpack"},
        {"id": "6a200000cccccccccccccccc", "name": "New Agent - J. Martin",
         "desc": "Email: jmartin@example.com\nAgency: F"}]

    class Board:
        def board_cards(self, board_id, archived=False):
            return cards

        def close(self):
            pass

    monkeypatch.setattr(jobs, "open_trello", lambda cfg: Board())
    config = SimpleNamespace(secrets=SimpleNamespace(trello_board_id="b"))
    assert jobs.agency_from_earlier_cards(config, name="Jacob Martin", email="jmartin@example.com") == (
        "AEP", "Aged Leads - Jacob Martin")
    assert jobs.agency_from_earlier_cards(config, name="Nobody Here", email="none@example.com") is None


def test_a_plain_audit_on_monday_takes_in_the_weekend():
    """"when i order audit monday, can it include the weekends"."""
    monday = date(2026, 10, 26)
    assert client._audit_days("order audit", monday) == [date(2026, 10, 24), date(2026, 10, 25), monday]
    assert client._audit_days("order audit", date(2026, 10, 27)) == [date(2026, 10, 27)]
    assert client._audit_days("order audit 2 days", monday) == [date(2026, 10, 25), monday]


def test_a_time_and_today_is_when_they_start():
    """Hutchinson Yang: "25 OTP VETS / 2:30 PM EST today / Same states" -
    parked in Franklin (Admin) with no launch date."""
    from wilbyte import agents

    monday = date(2026, 10, 26)
    assert agents.find_launch("25 OTP VETS\n2:30 PM EST today\nSame states", today=monday) == monday
    assert ordercards.says_when("25 OTP VETS\n2:30 PM EST today")
    assert agents.find_launch("Friday 9am EST", today=monday) == date(2026, 10, 30)
    # A schedule, the payment's own stamp, or a time with no day: no start.
    for not_a_start in ("Schedule: 8 am to 8 pm EST", "every Monday 9am", "call me at 3pm",
                        "INVOICE: INV-20488 · card •••• 8622 · 10/24/2026 9:12 AM"):
        assert not ordercards.says_when(not_a_start), not_a_start


def test_an_order_line_with_who_its_for_after_a_dash(monkeypatch):
    """Garret Sekelsky paid; the invoice said "50 Aged Vets - Santiago
    Basualdo / 30-90" - the card went out as Garret's, with "Agency: Bailey
    Lawrence" off his earlier card (one of his agents)."""
    assert jobs.name_in_notes(["50 Aged Vets - Santiago Basualdo\n30-90"]) == "Santiago Basualdo"
    assert jobs.name_in_notes(["50 Aged Vets - 30-90 days"]) == ""
    cards = [{"id": "6a000000aaaaaaaaaaaaaaaa", "name": "Aged Leads - Garret Sekelsky",
              "desc": "Email: g@example.com\nAgency: Bailey Lawrence"},
             {"id": "6a100000bbbbbbbbbbbbbbbb", "name": "New Agent - Bailey Lawrence", "desc": "Agency: BANK"}]

    class Board:
        def board_cards(self, board_id, archived=False):
            return cards

        def close(self):
            pass

    monkeypatch.setattr(jobs, "open_trello", lambda cfg: Board())
    config = SimpleNamespace(secrets=SimpleNamespace(trello_board_id="b"))
    assert jobs.agency_from_earlier_cards(config, name="Garret Sekelsky", email="g@example.com") is None


def test_asap_written_on_a_day_thats_gone_is_today(monkeypatch):
    """Geno Glaze paid Saturday - "start asap", "once signs he is live today" -
    gavelled Monday and held as "Sat 10/24 is too far from today"."""
    import sys
    import types

    class Anthropic:
        def __init__(self, **kw):
            self.messages = SimpleNamespace(create=lambda **kw: "reply")

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=Anthropic))
    monkeypatch.setattr(jobs, "_tool_input", lambda response, name: {
        "kind": "fresh", "name": "Geno Glaze", "package": "Text-Verified Veteran leads (OTP VET)", "leads": "30",
        "states": "", "launch": "today", "launch_iso": "2026-10-24"})
    config = SimpleNamespace(secrets=SimpleNamespace(anthropic_api_key="k", require=lambda *a: None),
                             copy=SimpleNamespace(model="claude-opus-5-5"))
    text = ("[10/24 09:10AM] Nova: New Payment | Payra\nNAME: Geno Glaze\nAMOUNT: $1,000\n"
            "PRODUCT: Text-Verified Veteran leads\n**INVOICE:** INV-20500\n**Notes:**\n> 5-8\n> start asap\n"
            "> 30 OTP VET + leads\n**Internal Notes:**\n> check schedule. otherwise once signs he is live today")
    monday = date(2026, 10, 26)
    card = jobs.read_order(config, text=text, images=[], today=monday)
    assert card["launch_iso"] == "2026-10-26" and card["fields"]["launch"] == "Monday, October 26"
    assert not any("too far" in one for one in ordercards.doubts(card, today=monday)[0])


def test_an_order_switched_to_another_product_isnt_held_for_it():
    """Ben Jacob paid for Trucker IUL; "Instead of Trucker IUL's / Switch
    order to 45 Text Verified Veteran leads"."""
    source = ("NAME: Ben Jacob\nPRODUCT: Text-Verified Trucker IUL Leads\n"
              "[10/26 01:22PM] Faith: Instead of Trucker IUL's\nSwitch order to 45 Text Verified Veteran leads")
    card = {"kind": "fresh", "client": "Ben Jacob", "source": source, "launch_iso": "2026-11-02",
            "fields": {"name": "Ben Jacob", "package": "Text-Verified Veteran Leads", "leads": "45",
                       "states": "PA, VA", "launch": "Monday, November 2"}}
    assert not any("Lead type" in one for one in ordercards.doubts(card, today=date(2026, 10, 26))[0])
    unswitched = dict(card, source=source.split("\n[")[0])
    assert any("Lead type" in one for one in ordercards.doubts(unswitched, today=date(2026, 10, 26))[0])


def test_each_line_aged_or_not_by_its_own_words(monkeypatch):
    """Skyler Liljenquist paid "Text-Verified Aged Mortgage Protection Leads";
    Kharyl's thread: "100 - $1/lead (180+) Mortgage protection / 10 - Text
    Verified MTG PLUS / EVERLIFE, LIVE TOM OCT 27". Both lines were read as
    aged and went on one Aged Leads card."""
    import sys
    import types

    assert jobs.line_kind("100 - $1/lead (180+) Mortgage protection") == "aged"
    assert jobs.line_kind("10 - Text Verified MTG PLUS") == "fresh"
    assert jobs.line_kind("Text-Verified Aged Veteran Leads") == "aged"
    assert jobs.line_kind("Text Verified Veteran Leads [FRESH - $45/Lead]") == "fresh"
    assert jobs.line_kind("Mortgage Protection") == ""

    class Anthropic:
        def __init__(self, **kw):
            self.messages = SimpleNamespace(create=lambda **kw: "reply")

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=Anthropic))
    monkeypatch.setattr(jobs, "_tool_input", lambda response, name: {
        "kind": "aged", "name": "Skyler Liljenquist", "states": "AL, AZ, CO, FL", "agency": "EVERLIFE",
        "package": "$1/lead (180+) Mortgage protection + Text Verified MTG PLUS", "leads": "110",
        "launch": "Tuesday, October 27", "launch_iso": "2026-10-27",
        "parts": [{"kind": "aged", "package": "$1/lead (180+) Mortgage protection", "leads": "100",
                   "states": "AL, AZ, CO, FL"},
                  {"kind": "aged", "package": "Text Verified MTG PLUS", "leads": "10", "states": "AL, AZ, CO, FL",
                   "launch_iso": "2026-10-27"}]})
    config = SimpleNamespace(secrets=SimpleNamespace(anthropic_api_key="k", require=lambda *a: None),
                             copy=SimpleNamespace(model="claude-opus-5-5"))
    text = ("[10/26 04:16PM] Nova: New Payment | Payra\nNAME: Skyler Liljenquist\nEMAIL: skyler@example.com\n"
            "AMOUNT: $548.55\nPRODUCT: Text-Verified Aged Mortgage Protection Leads\n"
            "[10/26 04:17PM] Kharyl: Skyler Liljenquist\nAL, AZ, CO, FL\n100 - $1/lead (180+) Mortgage protection\n"
            "10 - Text Verified MTG PLUS\nEVERLIFE, LIVE TOM OCT 27")
    card = jobs.read_order(config, text=text, images=[], today=date(2026, 10, 26))
    assert [one["kind"] for one in card["parts"]] == ["aged", "fresh"]
    cards = jobs.split_order(card, today=date(2026, 10, 26))
    assert [one["kind"] for one in cards] == ["aged", "fresh"]
    aged, fresh = cards
    assert aged["fields"]["leads"] == "100" and "MTG PLUS" not in aged["desc"]
    assert fresh["fields"]["leads"] == "10" and fresh["fields"]["launch"] == "Tuesday, October 27"


KANAN = ("New Payment | Zelle\nNAME: Kanan Kapital Llc\nAMOUNT: $2,005.00\nMETHOD: Zelle (Chase)\n"
         "PRODUCT: — (match to their order)\n"
         "[10/26 12:02PM] Faith: 43 OTP Vets to Shahan Uddin\nLive Tuesday\n\n"
         "100 Aged Vets, 30-90 no ny ak no ha to Mohammad Kanan\n"
         "Shahanuddin2005@example.com Email that the high intents no ny ak no ha")


def test_states_the_order_leaves_out_arent_the_states():
    """Shahan Uddin's card went out with "States: NY, AK" off "no ny ak no
    ha" - the states he was NOT to get."""
    assert ordercards.left_out_groups("100 Aged Vets, 30-90 no ny ak no ha to Mohammad Kanan") == [
        (["NY", "AK", "HI"], "no ny ak no ha")]
    assert ordercards.left_out_states("2 others packets all states except ny ak ha") == ["NY", "AK", "HI"]
    assert ordercards.left_out_groups("No leads after 9pm, not in the morning") == []
    assert ordercards.states_left_out_fixed("NY, AK", KANAN) == "All states except NY, AK, HI"
    assert ordercards.states_left_out_fixed("TX, FL", KANAN) == "TX, FL"
    wrong, _ = ordercards.doubts({"kind": "fresh", "source": KANAN, "fields": {
        "name": "Shahan Uddin", "states": "NY, AK", "package": "OTP Vets", "leads": "43"}}, today=date(2026, 10, 26))
    assert any("NY is on the card, but the order says to leave it out" in one for one in wrong)
    wrong, _ = ordercards.doubts({"kind": "fresh", "source": KANAN, "fields": {
        "name": "Shahan Uddin", "states": "All states except NY, AK, HI", "package": "OTP Vets", "leads": "43"}},
        today=date(2026, 10, 26))
    assert not any("States" in one for one in wrong), wrong


def test_each_line_goes_to_the_person_the_order_sends_it_to():
    """"43 OTP Vets to Shahan Uddin ... 100 Aged Vets ... to Mohammad Kanan":
    two cards, each for its own person, and the left-out states kept as left
    out on the New Agent card."""
    card = {"kind": "fresh", "client": "Shahan Uddin", "source": KANAN, "fields": {
        "name": "Shahan Uddin", "email": "Shahanuddin2005@example.com", "phone": "", "agency": "",
        "notes": "Paid via Zelle (Chase)"},
        "parts": [
            {"kind": "fresh", "package": "OTP Vets", "leads": "43", "launch_iso": "2026-10-27",
             "states": "All states except NY, AK, HI", "name": "Shahan Uddin"},
            {"kind": "aged", "package": "Aged Vets 30-90", "leads": "100", "launch_iso": "",
             "states": "All states except NY, AK, HI", "name": "Mohammad Kanan"}]}
    aged, fresh = jobs.split_order(card, today=date(2026, 10, 26))
    assert aged["name"] == "Aged Leads - Mohammad Kanan" and aged["fields"]["email"] == ""
    assert fresh["name"] == "New Agent - Shahan Uddin"
    assert fresh["fields"]["email"] == "Shahanuddin2005@example.com"
    assert fresh["fields"]["states"] == "All states except NY, AK, HI"
    assert not [one for one in jobs.split_doubts([aged, fresh], today=date(2026, 10, 26)) if "isn't in" in one
                or "leave it out" in one]


def test_an_aged_order_in_packets_is_held_as_one_states_list():
    """Mohammad Kanan's 600 Aged Vets in six packets went out as one list of
    every state named - the two "all states except ny ak ha" packets lost."""
    source = ("NAME: Kanan Kapital Llc\n6 batch of orders\n600 Aged Vets\n30-90\nplease see ss for states\n"
              "This is one packet: AR, CA, CO, FL\n2 packets of this: AR, GA, MA\n"
              "And 2 others packets all states except ny ak ha")
    wrong, _ = ordercards.doubts({"kind": "aged", "source": source, "fields": {
        "name": "Mohammad Kanan", "states": "AR, CA, CO, FL, GA, MA", "package": "Aged Vets 30-90",
        "leads": "600"}}, today=date(2026, 10, 26))
    assert any("packets or batches" in one for one in wrong)
    wrong, _ = ordercards.doubts({"kind": "aged", "source": "100 Aged Vets 30-90, TX, one batch", "fields": {
        "name": "Mohammad Kanan", "states": "TX", "package": "Aged Vets 30-90", "leads": "100"}},
        today=date(2026, 10, 26))
    assert not any("packets" in one for one in wrong)


def test_states_check_finds_the_cards_that_cost_money():
    """"were losing moneyyyyyyyy because of this" - every card held up
    against its thread."""
    from wilbyte.bot import mentions as m

    assert m.parse("<@1> states check").action == "statescheck"
    assert m.parse("<@1> check states 60 days").action == "statescheck"
    assert jobs.card_states("Name: Shahan Uddin\nStates: NY, AK\nLead Type: OTP Vets") == "NY, AK"
    found = jobs.states_recheck(KANAN, [
        {"name": "New Agent - Shahan Uddin", "kind": "fresh", "states": "NY, AK", "url": "https://trello.com/c/x"},
        {"name": "Aged Leads - Mohammad Kanan", "kind": "aged", "states": "All states except NY, AK, HI"}])
    assert len(found) == 1 and "Shahan Uddin" in found[0] and "no ny ak no ha" in found[0]
    packets = "6 batch of orders\n600 Aged Vets\n2 packets of this: AR, GA\n2 others packets all states except ny ak ha"
    found = jobs.states_recheck(packets, [{"name": "Aged Leads - Kanan Kapital Llc", "kind": "aged",
                                           "states": "AR, CA, GA"}])
    assert len(found) == 1 and "packets" in found[0]
