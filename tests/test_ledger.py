"""Every payment followed until it's set up, live and delivered - "by the
end of the day, hey, this is all the person you didn't set up"."""

import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from wilbyte import ledger, rookqc

NOW = datetime(2026, 11, 3, 22, 0, tzinfo=timezone.utc)  # 5 PM Eastern, a Tuesday
TODAY = date(2026, 11, 3)


def _row(**over):
    return SimpleNamespace(**{"id": 2070, "name": "Aaron Crane", "status": "live", "ordered": 30, "delivered": 4,
                              "start_date": date(2026, 11, 2), "lead_type": "Text Verified Veteran Plus", **over})


def _result(hub=None, **over):
    return SimpleNamespace(**{"hub": hub, "overflow": False, "cant": "", "ok": [], "problems": [], **over})


def test_where_a_payment_stands_comes_from_the_hub():
    assert ledger.stage(_result())["stage"] == ledger.NOT_SET_UP
    assert ledger.stage(_result(cant="Nova: couldn't set"))["stage"] == ledger.CANT
    assert ledger.stage(_result(_row(status="unsigned", delivered=0)))["stage"] == ledger.SET_UP
    assert ledger.stage(_result(_row(status="scheduled", delivered=0)))["stage"] == ledger.SET_UP
    assert ledger.stage(_result(_row(delivered=0)))["stage"] == ledger.LIVE
    kept = ledger.stage(_result(_row()))
    assert kept["stage"] == ledger.DELIVERING and kept["hub_id"] == "2070" and kept["start"] == "2026-11-02"
    assert ledger.stage(_result(_row(status="done", delivered=30)))["stage"] == ledger.FULFILLED
    assert ledger.stage(_result(_row(status="ended", delivered=20)))["stage"] == ledger.SHORT
    assert ledger.stage(_result(overflow=True))["stage"] == ledger.OVERFLOW
    assert ledger.stage(_result(overflow=True, ok=["Delivered 15 of 15"]))["stage"] == ledger.OVERFLOW_DONE


def _kept(name, stage, *, hours=5, problems=(), start="", status="", todo=(), **over):
    return {"client": name, "amount": "993.60", "posted": (NOW - timedelta(hours=hours)).isoformat(),
            "stage": stage, "open_problems": list(problems), "start": start, "status": status, "todo": list(todo),
            "link": f"https://discord.com/channels/1/2/{abs(hash(name)) % 1000}", **over}


def test_the_days_list():
    records = {
        "1": _kept("Charles Sellers", ledger.NOT_SET_UP, problems=["Nothing on the hub for this order yet"]),
        "2": _kept("Kyle Moffitt", ledger.LIVE, problems=["The hub states don't match the payment - has WA"],
                   start="2026-11-02", status="live"),
        "3": _kept("James Wyman", ledger.SET_UP, start="2026-11-03", status="unsigned"),
        "4": _kept("Sarra El Badri", ledger.SET_UP, start="2026-11-04", status="scheduled"),
        "5": _kept("Ian Done", ledger.FULFILLED, status="done"),
        "6": _kept("Pat Okd", ledger.LIVE, problems=["The hub has VT not ordered"], ok_problems=[
            "The hub has VT not ordered"]),
        "7": _kept("Old Order", ledger.NOT_SET_UP, hours=24 * 30),
        "8": _kept("Antonio Brancato", ledger.CANT, cant="Nova: couldn't set Antonio up"),
        "9": _kept("Bram Gutierrez", ledger.SET_UP, start="2026-11-04", status="unsigned"),
    }
    said = ledger.daily(records, now=NOW, today=TODAY, through=date(2026, 11, 4),
                        payra_missing=["• **Nobody Posted** $500 · INV-21300 · 3:10 PM"])
    assert "❌ Paid, not set up** (1)" in said and "Charles Sellers" in said
    assert "⚠️ Set up wrong** (1)" in said and "Kyle Moffitt" in said and "has WA" in said
    assert "Should be live" not in said, "starting today isn't late at 5 PM"
    assert "🚀 Starting today** (1)" in said and "James Wyman" in said
    assert "📅 Going live by Wednesday** (2)" in said
    assert "Sarra El Badri" in said and "✅ ready" in said and "contract not signed yet" in said
    assert "🃏" in said and "Antonio Brancato" in said
    assert "Paid in Payra, never posted" in said and "INV-21300" in said
    for gone in ("Ian Done", "Pat Okd", "Old Order"):
        assert gone not in said, gone
    morning = ledger.daily(records, now=NOW - timedelta(hours=8), today=TODAY, through=date(2026, 11, 4),
                           morning=True)
    assert "🚀 Going live today** (1)" in morning and "James Wyman" in morning
    assert "contract not signed yet" in morning


def test_the_lists_are_said_once_each_at_nine_and_five_six_seven():
    at = lambda hour, minute=0: datetime(2026, 11, 3, 5, tzinfo=timezone.utc) + timedelta(hours=hour, minutes=minute)
    assert ledger.due_lists(at(8, 59), []) == []
    assert ledger.due_lists(at(9, 1), []) == ["morning"]
    assert ledger.due_lists(at(9, 31), ["morning"]) == []
    assert ledger.due_lists(at(17, 5), ["morning"]) == ["17:00"]
    assert ledger.due_lists(at(18, 29), ["morning", "17:00"]) == ["18:00"]
    assert ledger.due_lists(at(19, 40), ["morning", "17:00"]) == ["19:00"], "only the latest when behind"
    assert ledger.due_lists(at(19, 40), ["morning", "17:00", "18:00", "19:00"]) == []
    assert ledger.due_lists(at(22, 0), []) == []
    ledger.said_on(TODAY, "morning")
    assert ledger.said_today(TODAY) == ["morning"]


def test_payra_payments_never_posted_in_new_payment():
    data = {"payments": {
        "a": {"invoice_number": "INV-21170", "paid_on": "2026-11-03T17:24:00+00:00", "status": "paid",
              "amount": 993.6, "customer": "Aaron Crane"},
        "b": {"invoice_number": "INV-21300", "paid_on": "2026-11-03T20:10:00+00:00", "status": "paid",
              "amount": 500, "customer": "Nobody Posted"},
        "c": {"invoice_number": "INV-21301", "paid_on": "2026-11-03T20:10:00+00:00", "status": "failed",
              "amount": 500, "customer": "Declined"},
        "d": {"invoice_number": "INV-21000", "paid_on": "2026-11-01T20:10:00+00:00", "status": "paid",
              "amount": 500, "customer": "Another Day"}}}
    posted = "New Payment | Payra\nNAME: Aaron Crane\nINVOICE: INV-21170 · card"
    missing = ledger.payra_missing(data, posted, day=TODAY)
    assert len(missing) == 1 and "Nobody Posted" in missing[0] and "INV-21300" in missing[0]


def test_a_hub_row_is_pinned_once_found_for_this_order():
    from wilbyte.bot import client

    post = SimpleNamespace(jump_url="https://discord.com/channels/1/2/900", content="AMOUNT: $993.60", embeds=[],
                           message_snapshots=[])
    open_row = _row(status="unsigned", delivered=0)
    result = SimpleNamespace(client="Aaron Crane", problems=[], todo=[], cant="", hub=open_row, overflow=False, ok=[])
    client._ledger_keep("900", post, result, {})
    kept = rookqc.recalled()["900"]
    assert kept["hub_id"] == "2070" and kept["stage"] == ledger.SET_UP and kept["amount"] == "993.60"
    done = _row(id=1516, status="done", delivered=36, ordered=36)
    client._ledger_keep("901", post, SimpleNamespace(**{**result.__dict__, "hub": done}), {})
    assert "hub_id" not in rookqc.recalled()["901"], "a row that's over isn't pinned"


def test_okd_problems_are_set_aside():
    from wilbyte.bot import client

    result = SimpleNamespace(problems=["The hub has VT not ordered", "The hub has no sheet"], notes=[])
    client._okd(result, {"ok_problems": ["The hub has VT not ordered"], "ok_by": "Franklin"})
    assert result.problems == ["The hub has no sheet"]
    assert result.notes == ["👌 OK'd by Franklin: The hub has VT not ordered"]


class _Thread:
    def __init__(self, name, made):
        self.name, self.created_at, self.sent = name, made, []

    async def send(self, text):
        self.sent.append(text)
        return SimpleNamespace(jump_url=f"https://discord.com/channels/1/9/{len(self.sent)}")


def test_ryte_asks_rook_and_nova_once_in_the_clients_thread(monkeypatch):
    """"Ok to communicate with Nova and rook" - what Ryte can't check, asked
    in the payment's thread, once for each problem."""
    from wilbyte.bot import client

    paid = datetime.now(timezone.utc) - timedelta(hours=1)
    theirs = _Thread("Charles Sellers", paid + timedelta(minutes=1))
    other = _Thread("Aiden Brown", paid + timedelta(minutes=3))
    members = [SimpleNamespace(bot=True, display_name="Rook", name="Rook", mention="<@11>"),
               SimpleNamespace(bot=True, display_name="Nova", name="Nova", mention="<@12>")]
    channel = _Thread("rook-nova", paid)
    channel.guild = SimpleNamespace(members=members)

    async def threads(ch):
        return [theirs, other]

    monkeypatch.setattr(client, "_rook_nova_channel", lambda bot: channel)
    monkeypatch.setattr(client, "_rn_threads", threads)
    post = SimpleNamespace(jump_url="https://discord.com/channels/1/2/900", created_at=paid)
    result = SimpleNamespace(client="Charles Sellers", problems=["Nothing on the hub for this order yet"], todo=[],
                             cant="", overflow=False, asked=(), setup_text="New fresh setup: ...")
    rookqc.remember("900", posted=paid.isoformat())
    asyncio.run(client._sqc_auto_ask(SimpleNamespace(), "900", post, result, rookqc.recalled()["900"]))
    assert len(theirs.sent) == 1 and not other.sent and not channel.sent
    assert theirs.sent[0].startswith("<@11> <@12>") and "Nothing on the hub for this order yet" in theirs.sent[0]
    # The same problem isn't asked again; a new one is, once the gap's past.
    asyncio.run(client._sqc_auto_ask(SimpleNamespace(), "900", post, result, rookqc.recalled()["900"]))
    assert len(theirs.sent) == 1
    result.problems.append("The hub has no sheet for them")
    monkeypatch.setattr(client, "SETUP_QC_ASK_AGAIN", timedelta(0))
    asyncio.run(client._sqc_auto_ask(SimpleNamespace(), "900", post, result, rookqc.recalled()["900"]))
    assert len(theirs.sent) == 2 and "no sheet" in theirs.sent[1] and "Nothing on the hub" not in theirs.sent[1]
    # Overflow and a can't-set-up aren't Rook and Nova's to answer.
    result.problems.append("Something else")
    asyncio.run(client._sqc_auto_ask(SimpleNamespace(), "900", post, SimpleNamespace(**{**result.__dict__,
                                                                                        "cant": "Nova: x"}),
                                     rookqc.recalled()["900"]))
    assert len(theirs.sent) == 2


def test_still_open_two_hours_after_asking_goes_to_therese_and_nicole(monkeypatch):
    from wilbyte.bot import client

    monkeypatch.setattr(client, "_work_hours", lambda bot, now=None: True)
    now = datetime.now(timezone.utc)
    result = SimpleNamespace(client="Charles Sellers", problems=["Nothing on the hub for this order yet"], todo=[],
                             cant="", overflow=False)
    held = {"posted": (now - timedelta(hours=3)).isoformat(), "asked_at": (now - timedelta(hours=1)).isoformat(),
            "asked_problems": ["Nothing on the hub for this order yet"]}
    rookqc.remember("900", **held)
    assert client._sqc_escalation(SimpleNamespace(), "900", result, held) == "", "an hour isn't two"
    held["asked_at"] = (now - timedelta(hours=2, minutes=5)).isoformat()
    line = client._sqc_escalation(SimpleNamespace(), "900", result, held)
    assert "Still open" in line and "Rook & Nova were asked" in line and "Nothing on the hub" in line
    assert client._sqc_escalation(SimpleNamespace(), "900", result, rookqc.recalled()["900"]) == "", "once"
    guild = SimpleNamespace(members=[SimpleNamespace(bot=False, display_name="Therese", name="t", mention="<@21>"),
                                     SimpleNamespace(bot=False, display_name="𝒩𝒾𝒸𝑜𝓁𝑒", name="n", mention="<@22>")])
    assert client._team_tags(SimpleNamespace(), guild) == "<@21> <@22>"
    monkeypatch.setattr(client, "_work_hours", lambda bot, now=None: False)
    rookqc.remember("901", **{**held, "escalated": []})
    assert client._sqc_escalation(SimpleNamespace(), "901", result, rookqc.recalled()["901"]) == "", "after hours"


def test_asking_for_the_list():
    from wilbyte.bot import mentions

    for said in ("who's not set up", "eod", "payments list", "end of day", "morning list"):
        assert mentions.parse(said).action == "ledger", said


def test_the_half_hourly_pass_takes_in_missed_payments_and_checks_the_open_ones(monkeypatch):
    from wilbyte.bot import client

    now = datetime.now(timezone.utc)
    watched, ran = [], []
    missed = SimpleNamespace(id=950, created_at=now - timedelta(hours=1))

    async def payments_on(bot, day):
        return [missed] if day == client._today(bot.config) else []

    async def run(bot, key, *, due=False, force=False):
        ran.append((key, due))
        return "problem"

    monkeypatch.setattr(client, "_payments_on", payments_on)
    monkeypatch.setattr(client, "_sqc_watch", lambda bot, post: watched.append(post.id))
    monkeypatch.setattr(client, "_sqc_run", run)
    monkeypatch.setattr(client, "_rook_nova_channel", lambda bot: None)
    rookqc.remember("900", posted=(now - timedelta(days=2)).isoformat(), stage=ledger.SET_UP, due_done=True)
    rookqc.remember("901", posted=(now - timedelta(days=2)).isoformat(), stage=ledger.FULFILLED)
    rookqc.remember("902", posted=(now - timedelta(hours=2)).isoformat())  # its timer lost to a restart
    rookqc.remember("903", posted=(now - timedelta(days=40)).isoformat(), stage=ledger.NOT_SET_UP)
    bot = SimpleNamespace(config=SimpleNamespace(schedule=SimpleNamespace(timezone="America/New_York")))
    assert asyncio.run(client._ledger_pass(bot)) == 2
    assert watched == [950]
    assert sorted(ran) == [("900", False), ("902", True)]


def test_asking_is_kept_to_recent_payments_and_a_few_an_hour(monkeypatch):
    """The first pass after an update reads three weeks of payments -
    #rook-nova isn't to be flooded."""
    from wilbyte.bot import client

    asked = []

    async def ask(bot, key, post, result, *, by="", problems=None):
        asked.append(key)
        return "https://discord.com/x"

    monkeypatch.setattr(client, "_sqc_ask", ask)
    monkeypatch.setattr(client, "SETUP_QC_ASKS_AN_HOUR", 3)
    now = datetime.now(timezone.utc)
    result = SimpleNamespace(client="X", problems=["Not on the hub"], todo=[], cant="", overflow=False, asked=(),
                             setup_text="New fresh setup: ...")
    old = {"posted": (now - timedelta(days=5)).isoformat()}
    asyncio.run(client._sqc_auto_ask(SimpleNamespace(), "1", SimpleNamespace(), result, old))
    assert asked == [], "five days old: on the day's list, not asked"
    for key in "23456":
        asyncio.run(client._sqc_auto_ask(SimpleNamespace(), key, SimpleNamespace(), result,
                                         {"posted": (now - timedelta(hours=1)).isoformat()}))
    assert asked == ["2", "3", "4"], "three an hour"



def test_rook_and_nova_have_longer_before_being_asked_about_one_they_havent_touched(monkeypatch):
    """Vivian Pellegrino: asked at fifteen minutes; Rook's renewal setup came
    two minutes after."""
    from wilbyte.bot import client

    asked = []

    async def ask(bot, key, post, result, *, by="", problems=None):
        asked.append(key)
        return "https://discord.com/x"

    monkeypatch.setattr(client, "_sqc_ask", ask)
    now = datetime.now(timezone.utc)
    untouched = SimpleNamespace(client="V", problems=["Nothing on the hub for this order yet"], todo=[], cant="",
                                overflow=False, asked=(), setup_text="")
    asyncio.run(client._sqc_auto_ask(SimpleNamespace(), "1", SimpleNamespace(), untouched,
                                     {"posted": (now - timedelta(minutes=16)).isoformat()}))
    assert asked == []
    asyncio.run(client._sqc_auto_ask(SimpleNamespace(), "1", SimpleNamespace(), untouched,
                                     {"posted": (now - timedelta(minutes=50)).isoformat()}))
    assert asked == ["1"], "45 minutes and still nothing: asked"
    done_wrong = SimpleNamespace(**{**untouched.__dict__, "setup_text": "New fresh setup: ...",
                                    "problems": ["The hub states don't match the payment - has WA"]})
    asyncio.run(client._sqc_auto_ask(SimpleNamespace(), "2", SimpleNamespace(), done_wrong,
                                     {"posted": (now - timedelta(minutes=16)).isoformat()}))
    assert asked == ["1", "2"], "a setup done wrong is asked about straight away"


BOARD = """Launching — where everyone stands (Tue Nov 3)
❌ **Paid, not set up yet** (needs the order added + a launch date)
 ⬗ Isaiah Wehner  ⬗ Sebastian Salas  ⬗ Charles Sellers
⏸️ **On the hub, paused — payment to check**
 ⬗ Marco Ayala
✍️ **Signed — tap Go live now or Schedule**
 ⬗ Joevanny Astorga  ⬗ Jake Scott
🟢 **Live**
 ⬗ Kyle Cassidy  ⬗ Will Stanger · Text Verified Veteran Plus  (Kyle: contract still unsigned)
@Nicole @Therese"""


def test_novas_launching_board_is_read_and_held_up_against_the_hub():
    """"we have this new channel as well, ryte needs to qc ... doesnt need to
    message there"."""
    board = ledger.board_parse(BOARD)
    assert board["Isaiah Wehner"] == ledger.NOT_SET_UP and board["Charles Sellers"] == ledger.NOT_SET_UP
    assert board["Marco Ayala"] == ledger.SET_UP and board["Jake Scott"] == ledger.SET_UP
    assert board["Kyle Cassidy"] == ledger.LIVE and board["Will Stanger"] == ledger.LIVE
    assert "Nicole" not in " ".join(board)
    records = {
        "1": _kept("Kyle Cassidy", ledger.SET_UP, status="unsigned"),
        "2": _kept("Marco Ayala", ledger.NOT_SET_UP),
        "3": _kept("Isaiah Wehner", ledger.LIVE, status="live"),
        "4": _kept("Will Stanger", ledger.LIVE, status="live"),
        "5": _kept("Nobody Listed", ledger.NOT_SET_UP, hours=3),
    }
    said = "\n".join(ledger.board_vs(records, board, now=NOW))
    assert "Kyle Cassidy" in said and "board says live - the hub has them unsigned" in said
    assert "Marco Ayala" in said and "not for this order" in said
    assert "Isaiah Wehner" in said and "says not set up - the hub has them live" in said
    assert "Will Stanger" not in said
    assert "Nobody Listed" in said and "not on Nova's #launching board" in said
    whole = ledger.daily(records, now=NOW, today=TODAY, through=date(2026, 11, 4), board=board)
    assert "🔀 Nova's #launching board vs the hub" in whole


def test_launching_is_read_as_talk_and_never_said_in(monkeypatch):
    from wilbyte.bot import client

    paid = datetime.now(timezone.utc) - timedelta(hours=1)
    said_in = []

    class Thread:
        id, name, created_at = 70, "Will Stanger", paid + timedelta(minutes=2)

        def history(self, **kw):
            async def gen():
                yield SimpleNamespace(id=1, created_at=paid + timedelta(minutes=3), content=(
                    "Will Stanger — VET · 15 leads · Set up on hub ✅"), embeds=[], message_snapshots=[],
                    reference=None, author=SimpleNamespace(display_name="Nova", name="Nova", bot=True))
            return gen()

    class Launching:
        name, threads = "📋│launching", [Thread()]

        def history(self, **kw):
            async def gen():
                if False:
                    yield None
            return gen()

        def archived_threads(self, **kw):
            async def gen():
                if False:
                    yield None
            return gen()

        async def send(self, *a, **kw):
            said_in.append(a)

    monkeypatch.setattr(client, "_rook_nova_channel", lambda bot: None)
    monkeypatch.setattr(client, "_launching_channel", lambda bot: Launching())
    talk = asyncio.run(client._sqc_talk(SimpleNamespace(), "900", {"names": ["Will Stanger"]}, paid))
    assert talk == [("Nova", "Will Stanger — VET · 15 leads · Set up on hub ✅")]
    post = SimpleNamespace(jump_url="https://discord.com/x", created_at=paid, content="")
    result = SimpleNamespace(client="Will Stanger", problems=["Not on the hub"], todo=[], wanted=None)
    assert asyncio.run(client._sqc_ask(SimpleNamespace(), "900", post, result)) == ""
    assert said_in == [], "nothing said in #launching"


def test_a_delivered_order_isnt_asked_about(monkeypatch):
    """Jason Lewis: done 25 of 25, asked to "fix what's wrong on the hub"."""
    from wilbyte.bot import client

    asked = []

    async def ask(bot, key, post, result, *, by="", problems=None):
        asked.append(key)
        return "x"

    monkeypatch.setattr(client, "_sqc_ask", ask)
    now = datetime.now(timezone.utc)
    result = SimpleNamespace(client="J", problems=["The hub states don't match the payment - has AR"], todo=[],
                             cant="", overflow=False, asked=(), setup_text="x", hub=_row(status="done", delivered=30))
    asyncio.run(client._sqc_auto_ask(SimpleNamespace(), "1", SimpleNamespace(), result,
                                     {"posted": (now - timedelta(hours=1)).isoformat()}))
    assert asked == []


def test_a_thread_about_somebody_else_lends_only_what_names_them(monkeypatch):
    """David Jacob's card carried Nova's warning about Ryan Gsell."""
    from wilbyte.bot import client

    paid = datetime.now(timezone.utc) - timedelta(hours=1)

    def msg(i, text):
        return SimpleNamespace(id=i, created_at=paid + timedelta(minutes=i), content=text, embeds=[],
                               message_snapshots=[], reference=None,
                               author=SimpleNamespace(display_name="Nova", name="Nova", bot=True))

    class Thread:
        id, name, created_at = 70, "Ryan Gsell", paid + timedelta(minutes=1)

        def history(self, **kw):
            async def gen():
                yield msg(2, "Handed to the team: Ryan's pause is the team's.")
                yield msg(3, "David Jacob's payment is the one after Ryan's.")
            return gen()

    class Channel:
        name, threads = "rook-nova", [Thread()]

        def history(self, **kw):
            async def gen():
                if False:
                    yield None
            return gen()

        def archived_threads(self, **kw):
            return Channel.history(self)

    monkeypatch.setattr(client, "_rook_nova_channel", lambda bot: Channel())
    monkeypatch.setattr(client, "_launching_channel", lambda bot: None)
    talk = asyncio.run(client._sqc_talk(SimpleNamespace(), "900", {"names": ["David Jacob"]}, paid))
    assert talk == [("Nova", "David Jacob's payment is the one after Ryan's.")]


def test_tiny_payments_arent_followed():
    records = {"1": _kept("Sonia M Portell", ledger.NOT_SET_UP, amount="1.00"),
               "2": _kept("Brandon Nguyen", ledger.NOT_SET_UP, amount="20.00"),
               "3": _kept("Sonia M Portell", ledger.NOT_SET_UP, amount="1,012.00")}
    assert list(ledger.open_ones(records, now=NOW)) == ["3"]


def test_two_invoices_of_the_same_amount_arent_one_posted_twice():
    """Ryan Gsell: INV-21095 and INV-21096, $776.25 each."""
    from wilbyte.bot import client

    now = datetime.now(timezone.utc)
    rookqc.remember("900", posted=(now - timedelta(minutes=3)).isoformat(), client="Ryan Gsell", amount="776.25",
                    invoice="INV-21095", link="x")
    post = SimpleNamespace(content="AMOUNT: $776.25\nINVOICE: INV-21096", embeds=[], message_snapshots=[],
                           created_at=now)
    result = SimpleNamespace(client="Ryan Gsell", notes=[], problems=[])
    client._sqc_twin("901", post, result, {"posted": now.isoformat()})
    assert result.problems == []


def test_the_list_without_doubles_overflow_once_and_paused_apart():
    records = {
        "1": _kept("Calvin Child", ledger.SET_UP, start="2026-10-30", status="scheduled", hub_id="77"),
        "2": _kept("Calvin Child", ledger.SET_UP, start="2026-10-30", status="scheduled", hub_id="77"),
        "3": _kept("Karli Kelley", ledger.OVERFLOW, problems=["Overflow order paid 3 days ago, and nothing says"]),
        "4": _kept("Camden Misquez", ledger.PAUSED, start="2026-10-07", status="paused", ordered=239,
                   delivered=147),
    }
    said = ledger.daily(records, now=NOW, today=TODAY, through=date(2026, 11, 4))
    assert said.count("Calvin Child") == 1 and "(2 payments)" in said
    assert "⚠️ Set up wrong" not in said and said.count("Karli Kelley") == 1
    assert "⏸️ Paused with leads still owed** (1)" in said and "147 of 239 delivered" in said
    assert "Camden" not in said.split("⏸️")[0], "not under should be live"
    assert ledger.stage(_result(_row(status="paused", delivered=12)))["stage"] == ledger.PAUSED
    assert ledger.stage(_result(_row(status="paused", delivered=31, ordered=30)))["stage"] == ledger.FULFILLED


def test_a_payment_for_two_invoices_is_posted_when_either_is():
    data = {"payments": {"a": {"invoice_number": "INV-21189, INV-21190", "paid_on": "2026-11-03T05:09:00+00:00",
                               "status": "paid", "amount": 1119.66, "customer": "Sarra El Badri"}}}
    assert ledger.payra_missing(data, "INVOICE: INV-21190 · card", day=TODAY) == []


def test_nobody_is_missing_from_a_board_that_wasnt_all_read():
    records = {"1": _kept("Sebastian Salas", ledger.NOT_SET_UP, hours=3)}
    assert ledger.board_vs(records, {"Someone Else": ledger.LIVE}, now=NOW, complete=False) == []
    assert ledger.board_vs(records, {"Someone Else": ledger.LIVE}, now=NOW)


def test_a_thread_named_for_the_client_not_the_payer_is_theirs(monkeypatch):
    """The Eissa Group Llc paid for Mohammad Kanan: his thread names his
    email, not the business - Rook's setup was left out."""
    from wilbyte.bot import client

    paid = datetime.now(timezone.utc) - timedelta(hours=1)

    def msg(i, text, who="Rook"):
        return SimpleNamespace(id=i, created_at=paid + timedelta(minutes=i), content=text, embeds=[],
                               message_snapshots=[], reference=None,
                               author=SimpleNamespace(display_name=who, name=who, bot=True))

    class Thread:
        id, name, created_at = 70, "Mohammad Kanan", paid + timedelta(minutes=1)

        def history(self, **kw):
            async def gen():
                yield msg(2, "@Nova New fresh setup:\nName: Mohammad Kanan\nEmail: mk@example.com")
                yield msg(3, "Set up ✅", "Nova")
            return gen()

    class Channel:
        name, threads = "rook-nova", [Thread()]

        def history(self, **kw):
            async def gen():
                if False:
                    yield None
            return gen()

        def archived_threads(self, **kw):
            return Channel.history(self)

    monkeypatch.setattr(client, "_rook_nova_channel", lambda bot: Channel())
    monkeypatch.setattr(client, "_launching_channel", lambda bot: None)
    held = {"names": ["The Eissa Group Llc"], "email": "mk@example.com"}
    talk = asyncio.run(client._sqc_talk(SimpleNamespace(), "900", held, paid))
    assert [who for who, _said in talk] == ["Rook", "Nova"]


def test_not_asked_while_rook_and_nova_are_at_it(monkeypatch):
    """Samuel Stapleton-Jones: Rook's setup and Ryte's question in the same
    minute, the payment an hour old."""
    from wilbyte.bot import client

    asked = []

    async def ask(bot, key, post, result, *, by="", problems=None):
        asked.append(key)
        return "x"

    monkeypatch.setattr(client, "_sqc_ask", ask)
    now = datetime.now(timezone.utc)
    result = SimpleNamespace(client="S", problems=["Nova hasn't answered Rook yet - nothing says it's set up",
                                                   "Not on the hub"], todo=[], cant="", overflow=False, asked=(),
                             setup_text="New fresh setup: ...")
    held = {"posted": (now - timedelta(hours=1)).isoformat(), "heard": (now - timedelta(minutes=1)).isoformat()}
    asyncio.run(client._sqc_auto_ask(SimpleNamespace(), "1", SimpleNamespace(), result, held))
    assert asked == []
    held["heard"] = (now - timedelta(minutes=35)).isoformat()
    asyncio.run(client._sqc_auto_ask(SimpleNamespace(), "1", SimpleNamespace(), result, held))
    assert asked == ["1"]


def test_franklin_isnt_tagged_before_rook_and_nova_have_had_their_time(monkeypatch):
    """Anthony Portillo's "Not on the hub" tagged Franklin minutes before Nova
    set him up."""
    from wilbyte.bot import client
    from tests.test_rookqc import _CardChannel

    cards = _CardChannel()
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(client, "_card_ping", lambda bot: "<@1> ")
    monkeypatch.setattr(client, "_sqc_auto_ask", lambda *a, **k: asyncio.sleep(0))
    monkeypatch.setattr(client, "_sqc_escalation", lambda *a, **k: "")
    post = SimpleNamespace(content="AMOUNT: $1164.37", embeds=[], message_snapshots=[], jump_url="https://x/900",
                           created_at=now - timedelta(minutes=16))
    result = rookqc.Checked(client="Anthony Portillo", problems=["Not on the hub"])
    held = {"posted": (now - timedelta(minutes=16)).isoformat()}
    rookqc.remember("900", **held)
    asyncio.run(client._sqc_card(SimpleNamespace(), cards, "900", post, result, held, due=True, waiting=False))
    assert cards.sent[-1].content is None, "not tagged at 16 minutes"
    held = dict(rookqc.recalled()["900"], posted=(now - timedelta(minutes=50)).isoformat())
    rookqc.remember("900", **held)
    result = rookqc.Checked(client="Anthony Portillo", problems=["Not on the hub"])
    asyncio.run(client._sqc_card(SimpleNamespace(), cards, "900", post, result, held, due=False, waiting=False))
    assert cards.sent[-1].content == "<@1>", "tagged once it's due"


def test_a_cant_with_its_trello_card_made_is_with_the_team():
    """William McClure: Make the card pressed, his card made on Trello."""
    records = {"1": _kept("William McClure", ledger.CANT, cant="Nova: couldn't set up",
                          trello="https://trello.com/c/s2Lf4Y1R"),
               "2": _kept("Charles Sellers", ledger.CANT, cant="Nova: couldn't set")}
    said = ledger.daily(records, now=NOW, today=TODAY, through=date(2026, 11, 4))
    head, _, tail = said.partition("With the team on Trello")
    assert "Charles Sellers" in head and "William McClure" not in head
    assert "William McClure" in tail and "trello.com/c/s2Lf4Y1R" in tail


def test_what_ryte_asked_about_that_is_right_now_is_said(monkeypatch):
    """Justin Techer: "has IN not ordered", asked, forwarded to Nicole to fix,
    then found added in his payment's thread - and nobody told."""
    from wilbyte.bot import client

    replies = []

    class Question:
        async def reply(self, text, **kw):
            replies.append(text)

    class Channel:
        async def fetch_message(self, _id):
            return Question()

    bot = SimpleNamespace(get_channel=lambda _id: Channel())
    asked = "The hub states don't match the payment - has IN not ordered"
    rookqc.remember("899", asked="https://discord.com/channels/1/2/3", asked_problems=[asked])
    asyncio.run(client._sqc_tell_fixed(bot, "899", rookqc.Checked(client="J", problems=[], hub=_row()),
                                       rookqc.recalled()["899"]))
    assert replies == [], "off: nothing said in #rook-nova"
    monkeypatch.setattr(client, "SETUP_QC_TELL_FIXED", True)
    rookqc.remember("900", asked="https://discord.com/channels/1/2/3", asked_problems=[asked])
    result = rookqc.Checked(client="Justin Techer", problems=[], hub=_row(status="live"))
    # Mai Nguyen, William McClure: what was asked went, something else stayed.
    other = rookqc.Checked(client="Mai Nguyen", problems=["Not on the hub"])
    asyncio.run(client._sqc_tell_fixed(bot, "900", other, rookqc.recalled()["900"]))
    assert replies == [], "not while anything else is open"
    asyncio.run(client._sqc_tell_fixed(bot, "900", rookqc.Checked(client="J", problems=[]),
                                       rookqc.recalled()["900"]))
    assert replies == [], "not when it isn't on the hub"
    asyncio.run(client._sqc_tell_fixed(bot, "900", result, rookqc.recalled()["900"]))
    assert len(replies) == 1 and "Justin Techer" in replies[0] and "has IN not ordered" in replies[0]
    assert "checks out on the hub now" in replies[0]
    asyncio.run(client._sqc_tell_fixed(bot, "900", result, rookqc.recalled()["900"]))
    assert len(replies) == 1, "once"
    still = rookqc.Checked(client="X", problems=[asked])
    rookqc.remember("901", asked="https://discord.com/channels/1/2/4", asked_problems=[asked])
    asyncio.run(client._sqc_tell_fixed(bot, "901", still, rookqc.recalled()["901"]))
    assert len(replies) == 1, "not while it's still wrong"


def test_a_problem_is_asked_about_only_once_its_found_again(monkeypatch):
    """"rYTE CANT MAKE MISTAKE HERE BRO, HES SUPPOSED TO QC": a read that's
    wrong once isn't sent out."""
    from wilbyte.bot import client

    monkeypatch.setattr(client, "SETUP_QC_CONFIRM", timedelta(minutes=25))
    asked = []

    async def ask(bot, key, post, result, *, by="", problems=None):
        asked.append(list(problems or []))
        return "x"

    monkeypatch.setattr(client, "_sqc_ask", ask)
    now = datetime.now(timezone.utc)
    result = SimpleNamespace(client="J", problems=["The hub states don't match the payment - has IN not ordered"],
                             todo=[], cant="", overflow=False, asked=(), setup_text="x")
    held = {"posted": (now - timedelta(hours=2)).isoformat(), "seen": {}}
    asyncio.run(client._sqc_auto_ask(SimpleNamespace(), "1", SimpleNamespace(), result, held))
    assert asked == [], "found once"
    key = rookqc.problem_key(result.problems[0])
    held["seen"] = {key: (now - timedelta(minutes=10)).isoformat()}
    asyncio.run(client._sqc_auto_ask(SimpleNamespace(), "1", SimpleNamespace(), result, held))
    assert asked == []
    held["seen"] = {key: (now - timedelta(minutes=30)).isoformat()}
    asyncio.run(client._sqc_auto_ask(SimpleNamespace(), "1", SimpleNamespace(), result, held))
    assert asked == [result.problems]


def test_a_question_goes_in_the_clients_own_thread_only():
    """Noah Anthony Vera's went into Riley Noah's thread."""
    from wilbyte.bot import client

    paid = datetime.now(timezone.utc) - timedelta(hours=1)
    riley = _Thread("Riley Noah", paid + timedelta(minutes=1))
    noah = _Thread("Noah Anthony Vera · setup", paid + timedelta(minutes=2))

    async def threads(channel, store=None):
        return [riley, noah]

    import wilbyte.bot.client as c
    orig = c._rn_threads
    c._rn_threads = threads
    try:
        assert asyncio.run(client._rn_thread_for(SimpleNamespace(), None, "Noah Anthony Vera", paid)) is noah
        assert asyncio.run(client._rn_thread_for(SimpleNamespace(), None, "Noah Vera", paid)) is noah
        assert asyncio.run(client._rn_thread_for(SimpleNamespace(), None, "Riley Smith", paid)) is None
    finally:
        c._rn_threads = orig


def test_a_marketing_package_isnt_a_hub_setup(monkeypatch):
    """Cole Jackson's $1,449 lead-generation marketing package."""
    from wilbyte.bot import client

    later = []
    monkeypatch.setattr(client, "_sqc_later", lambda *a, **k: later.append(a))
    post = SimpleNamespace(id=950, content=("New Payment | Payra\nNAME: Cole Jackson\nAMOUNT: $1449.00\nPRODUCT: This "
                                            "is our lead generation marketing package where we at Agent Lead Lab ..."),
                           embeds=[], message_snapshots=[], channel=SimpleNamespace(id=2),
                           created_at=datetime.now(timezone.utc))
    client._sqc_watch(SimpleNamespace(), post)
    assert later == [] and "950" not in rookqc.recalled()



def test_past_their_start_is_split_by_whose_it_is():
    records = {"1": _kept("Joseph Foreman", ledger.SET_UP, start="2026-11-02", status="scheduled"),
               "2": _kept("Jonathan Flores", ledger.SET_UP, start="2026-10-31", status="unsigned")}
    said = ledger.daily(records, now=NOW, today=TODAY, through=date(2026, 11, 4))
    team, _, client = said.partition("✍️ Should be live - waiting on their signature")
    assert "Joseph Foreman" in team and "Jonathan Flores" in client


def test_payra_by_name_and_amount_when_the_invoice_isnt_named():
    data = {"payments": {"a": {"invoice_number": "SW16RFK-39", "paid_on": "2026-11-03T17:34:00+00:00",
                               "status": "paid", "amount": 1449, "customer": "Cole Jackson"}}}
    assert ledger.payra_missing(data, "NAME: Cole Jackson\nAMOUNT: $1449.00", day=TODAY) == []
    assert ledger.payra_missing(data, "NAME: Someone Else\nAMOUNT: $1449.00", day=TODAY)


def test_aged_cards_waiting_on_a_pull_thats_done_are_brought_up_to_date(monkeypatch):
    """46 read "paid, not pulled yet" - Roman Edwards' pulled that morning."""
    from wilbyte import agedqc, paycards
    from wilbyte.bot import client

    paycards.remember("aged", "900", standing="waiting", title="Roman Edwards (INV-21221)")
    paycards.remember("aged", "901", standing="waiting", title="Kyle Lass (INV-21078)")
    agedqc.remember("900", agedqc.Checked(client="Roman Edwards", problems=[]))

    async def no_thread(bot, key, order):
        return None

    monkeypatch.setattr(client, "_order_thread", no_thread)
    asyncio.run(client._aged_catch_up(SimpleNamespace()))
    assert paycards.where("aged", "900")["standing"] == "ok"
    assert paycards.where("aged", "901")["standing"] == "waiting" and paycards.where("aged", "901").get("looked")


def test_todays_payments_a_line_each():
    """"how can i make sure that people are setup for today's payment"."""
    from wilbyte.bot import mentions

    for said in ("today's payments", "payments today", "todays payments?", "today"):
        assert mentions.parse(said).action == "todays", said
    today = NOW - timedelta(hours=2)
    records = {
        "1": _kept("Kailey Hull", ledger.NOT_SET_UP, problems=["Not on the hub"], hours=2),
        "2": _kept("Raymond Vargas", ledger.SET_UP, status="unsigned", hours=2),
        "3": _kept("Anthony Portillo", ledger.LIVE, status="live", ordered=25, delivered=0, hours=2),
        "4": _kept("Connor Swartz", ledger.LIVE, problems=["The hub states don't match the payment - has MT"],
                   hours=2),
        "5": _kept("Yesterday Person", ledger.NOT_SET_UP, hours=30),
        "6": {"client": "Just Paid", "posted": today.isoformat(), "amount": "500.00"},
    }
    said = ledger.todays(records, now=NOW, day=TODAY, aged=["• **Roman Edwards** - ✅ pulled as paid for"])
    assert said.index("❌ Not on the hub yet") < said.index("✅ Set up and live")
    assert "Kailey Hull" in said and "✍️ Set up - waiting on their signature** (1)" in said
    assert "⚠️ Set up, but not as paid for** (1)" in said and "has MT" in said
    assert "⏳ Just paid" in said and "Yesterday Person" not in said and "Roman Edwards" in said


def test_why_shows_what_ryte_read(monkeypatch):
    from wilbyte.bot import client, mentions

    assert mentions.parse("why Kane Curtis").action == "sqcwhy"
    assert mentions.parse("why Kane Curtis").brief == "Kane Curtis"
    said = []

    class Responder:
        async def send(self, text=None, **kw):
            said.append(text)

    rows = [_row(name="Kailee Hull", id=11, lead_type="Mortgage Protection", status="live"),
            _row(name="Kailey Hull", id=12, status="done", delivered=20, ordered=20)]

    async def hub(bot):
        return rows

    async def checking(bot, key):
        post = SimpleNamespace(content="AMOUNT: $600.00", embeds=[], message_snapshots=[], jump_url="https://x/900")
        return post, rookqc.Checked(client="Kailey Hull", problems=["Not on the hub"],
                                    paid=rookqc.Order(name="Kailey Hull", count=20, lead_type="Mortgage Protection"))

    monkeypatch.setattr(client, "_sqc_hub", hub)
    monkeypatch.setattr(client, "_sqc_check", checking)
    rookqc.remember("900", name="Kailey Hull", posted=NOW.isoformat())
    asyncio.run(client._sqc_why(SimpleNamespace(), Responder(), "Kailey Hull"))
    text = "\n".join(said)
    assert "Kailey Hull (row 12)" in text and "Kailee Hull (row 11)" in text, text
    assert "Problems: Not on the hub" in text


def test_nova_saying_done_isnt_said_as_not_on_the_hub():
    """"some of thse are live, and some of these are aged and delivered"."""
    from tests.test_rookqc import PAYRA, ROOK

    result = rookqc.check(PAYRA, [("Rook", ROOK), ("Nova", "Got it ✅ I set up Jonathan's order: 40 FEX in TX FL GA")],
                          hub=None, hub_rows=[])
    said = " ".join(result.problems)
    assert "Nova says it's done" in said and "What name is it under?" in said and "Not on the hub" not in said


def test_why_takes_a_list_of_names():
    """Franklin's six in one message got "fire them off separately"."""
    from wilbyte.bot import mentions

    said = ("why Kane Curtis\n<@123> why Kailey Hull\n@Ryte why Jensin Philpott\n@Ryte why Wes Stoecklin\n"
            "@Ryte why Killtech Financial\n@Ryte why Airr")
    got = mentions.parse(said)
    assert got.action == "sqcwhy"
    assert got.brief.split("\n") == ["Kane Curtis", "Kailey Hull", "Jensin Philpott", "Wes Stoecklin",
                                     "Killtech Financial", "Airr"], got.brief
    assert mentions.parse("why Kane Curtis").brief == "Kane Curtis"


def test_franklin_is_tagged_once_a_day_for_a_payment(monkeypatch):
    """William J Seitz's and Brandon Nguyen's cards sent again, tagged, for
    the same thing said in new words."""
    from wilbyte.bot import client
    from tests.test_rookqc import _CardChannel

    cards = _CardChannel()
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(client, "_card_ping", lambda bot: "<@1> ")
    monkeypatch.setattr(client, "_sqc_auto_ask", lambda *a, **k: asyncio.sleep(0))
    monkeypatch.setattr(client, "_sqc_escalation", lambda *a, **k: "")
    post = SimpleNamespace(content="AMOUNT: $400.00", embeds=[], message_snapshots=[], jump_url="https://x/900",
                           created_at=now - timedelta(hours=30))
    rookqc.remember("900", posted=(now - timedelta(hours=30)).isoformat())
    first = rookqc.Checked(client="Brandon Nguyen", problems=["Nothing on the hub for this order yet"])
    asyncio.run(client._sqc_card(SimpleNamespace(), cards, "900", post, first, rookqc.recalled()["900"],
                                 due=True, waiting=False))
    assert cards.sent[-1].content == "<@1>"
    again = rookqc.Checked(client="Brandon Nguyen", problems=["Not sure which order this pays for"])
    asyncio.run(client._sqc_card(SimpleNamespace(), cards, "900", post, again, rookqc.recalled()["900"],
                                 due=False, waiting=False))
    assert len(cards.sent) == 1 and cards.sent[0].edits, "edited where it is, not sent again tagged"


def test_a_payment_asked_about_today_isnt_asked_again(monkeypatch):
    """"WE flagged thise alreadyyyy" - asked again when the problem was put
    in new words."""
    from wilbyte.bot import client

    asked = []

    async def ask(bot, key, post, result, *, by="", problems=None):
        asked.append(key)
        return "x"

    monkeypatch.setattr(client, "_sqc_ask", ask)
    now = datetime.now(timezone.utc)
    result = SimpleNamespace(client="B", problems=["Not sure which order this pays for"], todo=[], cant="",
                             overflow=False, asked=(), setup_text="x")
    held = {"posted": (now - timedelta(hours=8)).isoformat(), "asked_at": (now - timedelta(hours=3)).isoformat(),
            "asked_problems": ["Nothing on the hub for this order yet"]}
    asyncio.run(client._sqc_auto_ask(SimpleNamespace(), "1", SimpleNamespace(), result, held))
    assert asked == []
    handed = SimpleNamespace(**{**result.__dict__, "with_team": "Forwarded to Nicole"})
    asyncio.run(client._sqc_auto_ask(SimpleNamespace(), "2", SimpleNamespace(), handed,
                                     {"posted": (now - timedelta(hours=2)).isoformat()}))
    assert asked == [], "Rook's taken it to the team"


def test_clear_them_out_takes_everything_off_until_something_new():
    """"these are all good to go so clear them out"."""
    for key, one in {
        "1": _kept("Keven Mata Hernandez", ledger.NOT_SET_UP, hours=90, problems=["Not on the hub"]),
        "2": _kept("Laura Vargas", ledger.SET_UP, hours=90, start="2026-11-02", status="scheduled"),
        "3": _kept("Dillon Willis", ledger.DELIVERING, hours=90,
                   problems=["The hub states don't match the payment - has NM not ordered"]),
        "4": _kept("Came In Later", ledger.NOT_SET_UP, hours=1, problems=["Not on the hub"]),
    }.items():
        rookqc.remember(key, **one)
    aged = ["• **Rachna (INV-1)** - the pull doesn't match <https://discord.com/x>"]
    keys, lines = ledger.clear(rookqc.recalled(), now=NOW, by="Franklin", before=NOW - timedelta(hours=2),
                               lines=aged)
    assert sorted(keys) == ["1", "2", "3"] and lines == aged
    said = ledger.daily(rookqc.recalled(), now=NOW, today=TODAY, through=TODAY + timedelta(days=1), aged=aged)
    for name in ("Keven Mata Hernandez", "Laura Vargas", "Dillon Willis", "Rachna"):
        assert name not in said
    assert "Came In Later" in said  # paid after the list that was answered
    # OK'd, so Rook and Nova aren't asked about it again.
    assert rookqc.among("The hub states don't match the payment - has NM not ordered",
                        rookqc.recalled()["3"]["ok_problems"])
    # Something new wrong puts it back.
    rookqc.remember("3", open_problems=["The hub states don't match the payment - has NM, TX not ordered"])
    said = ledger.daily(rookqc.recalled(), now=NOW, today=TODAY, through=TODAY + timedelta(days=1))
    assert "Dillon Willis" in said
    # And undo puts the rest back.
    back, _lines = ledger.unclear(now=NOW)
    assert sorted(back) == ["1", "2", "3"]
    said = ledger.daily(rookqc.recalled(), now=NOW, today=TODAY, through=TODAY + timedelta(days=1), aged=aged)
    assert "Keven Mata Hernandez" in said and "Laura Vargas" in said and "Rachna" in said
    assert not rookqc.recalled()["1"].get("ok_problems")


def test_clear_them_out_is_not_a_clearout():
    from wilbyte.bot import mentions

    assert mentions.parse("these are all good to go so clear them out").action == "ledgerclear"
    assert mentions.parse("<@1> clear the list").action == "ledgerclear"
    assert mentions.parse("undo clear").action == "ledgerunclear"
    # Franklin's first try, with nobody after "clear" - read as a clear-out.
    assert mentions.parse("<@1> these are all good to go so clear").action == "ledgerclear"
    assert mentions.parse("clear Jay Rodriguez").action == "clearout"


def test_clearing_answering_rytes_list(monkeypatch):
    from wilbyte.bot import client

    rookqc.remember("1", **_kept("Keven Mata Hernandez", ledger.NOT_SET_UP, hours=90, problems=["Not on the hub"]))
    sent = []

    class Said:
        async def send(self, text="", **_):
            sent.append(text)

    async def text(_bot, _which, parts=None):
        parts["lines"] = []
        return ""

    listed = SimpleNamespace(created_at=datetime.now(timezone.utc), author=SimpleNamespace(bot=True))

    async def replied(_message):
        return listed

    monkeypatch.setattr(client, "_ledger_text", text)
    monkeypatch.setattr(client, "_replied_to", replied)
    asking = SimpleNamespace(author=SimpleNamespace(display_name="Franklin", name="franklin"))
    asyncio.run(client._ledger_clear(SimpleNamespace(), Said(), asking))
    assert "Cleared 1 payment" in sent[0] and "Keven Mata Hernandez" in sent[0]
    assert rookqc.recalled()["1"]["cleared_by"] == "Franklin"
