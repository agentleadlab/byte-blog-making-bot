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
    assert "Agency" not in jobs.order_desc(fields, "fresh") and "Launch Date: Tuesday, October 13" in jobs.order_desc(fields)


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
    monkeypatch.setattr(jobs, "payra_invoice_note", lambda config, **kw: "AGED IUL\n180+\nin states IL, SC, AZ, and TX")
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
    monkeypatch.setattr(jobs, "payra_invoice_note", lambda config, **kw: "")
    message = _aged_post()
    message.thread = _notes("can i get 10 of the aged vets 30-90 days. OK,TX,AZ,MO,AL", "uprise")
    order.fields = {"states": "OK, TX, AZ, MO, AL", "leads": "10"}
    assert asyncio.run(client._aged_from_invoice(_bot(message), message, wait=0)) == "made"
    assert [one[0] for one in order.made] == ["aged"]
    assert "OK,TX,AZ,MO,AL" in order.reads[0] and "Payra invoice note" not in order.reads[0]
    assert ordercards.load()["950"]["extra_notes"] == []


def test_the_invoice_note_stays_on_the_card_when_the_thread_updates_it(order, monkeypatch):
    monkeypatch.setattr(jobs, "payra_invoice_note", lambda config, **kw: "AGED IUL in IL, SC")
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
    monkeypatch.setattr(jobs, "payra_invoice_note", lambda config, **kw: "25 OTP vets")
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

    async def making(bot, message, *, wait=None):
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
    ({"email": "someone@example.com"}, "someone@example.com isn't in the order"),
    ({"phone": "+15550001111"}, "isn't in the order"),
])
def test_what_doesnt_match_the_order_is_caught(fields, said):
    wrong, _missing = ordercards.doubts(_checked(**fields), today=date(2026, 10, 14))
    assert any(said in one for one in wrong), wrong


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
    assert order.made == [] and "900" not in ordercards.load() and "900" not in client._ORDER_ACTIVE



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

    monkeypatch.setattr(jobs, "payra_invoice_note", down)
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

    monkeypatch.setattr(jobs, "payra_invoice_note", note)
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
    monkeypatch.setattr(jobs, "payra_invoice_note", lambda config, **kw: "")
    asyncio.run(client._order_gavelled(_bot(message), GAVEL))
    monkeypatch.setattr(jobs, "payra_invoice_note", lambda config, **kw: "50 VET FL TX")
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

        def create_card(self, list_id, name):
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
        def create_card(self, list_id, name):
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
    assert jobs.order_fields(got, [declan])["notes"] == (
        "Can I do an order of $400 worth of veteran aged 30 days az, mi, oh, fl\n100 Aged Vet")
    aged = "Payra invoice note: AGED IUL / 180+ / in states IL, SC, AZ, and TX"
    assert jobs.order_fields({}, [aged])["notes"] == "AGED IUL / 180+ / in states IL, SC, AZ, and TX"


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
                                                      "url": "https://trello.com/c/keegan"}}))
    second = post(9, "New Payment | Payra\nNAME: Keegan Sundberg\nAMOUNT: $79.18\nPRODUCT: Text-Verified Trucker IUL")
    how, line = client._audit_line(second, ordercards.load(), [])
    assert how == "ok" and "trello.com/c/keegan" in line
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
    monkeypatch.setattr(jobs, "payra_invoice_note", lambda config, **kw: "")
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
