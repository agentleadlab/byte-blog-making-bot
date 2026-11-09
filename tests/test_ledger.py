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
    # Nova by Nova Cloud's own id, not the first bot named "Nova" - that was
    # the Mac app's, and nobody listens on it.
    assert theirs.sent[0].startswith("<@11> <@1529479205165858923>")
    assert "<@12>" not in theirs.sent[0] and "Nothing on the hub for this order yet" in theirs.sent[0]
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
    assert cards.sent[-1].content == "@Therese @Nicole <@1>", "tagged once it's due"


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
    assert cards.sent[-1].content == "@Therese @Nicole <@1>"
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
    # Not on the list at all: delivering, nothing wrong.
    rookqc.remember("5", **_kept("Quiet Fine", ledger.DELIVERING, hours=90))
    keys, lines = ledger.clear(rookqc.recalled(), now=NOW, by="Franklin", before=NOW - timedelta(hours=2),
                               lines=aged, today=TODAY, through=TODAY + timedelta(days=1))
    assert sorted(keys) == ["1", "2", "3"] and lines == aged
    assert not rookqc.recalled()["5"].get("cleared_at")  # never on the list, never cleared
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
    # Moving section puts it back too - Laura Vargas paused.
    rookqc.remember("2", stage=ledger.PAUSED, delivered=3, ordered=30)
    said = ledger.daily(rookqc.recalled(), now=NOW, today=TODAY, through=TODAY + timedelta(days=1))
    assert "Laura Vargas" in said
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
    monkeypatch.setattr(client, "_today", lambda _config: TODAY)
    asyncio.run(client._ledger_clear(SimpleNamespace(config=None), Said(), asking))
    assert "Cleared 1 payment" in sent[0] and "Keven Mata Hernandez" in sent[0]
    assert rookqc.recalled()["1"]["cleared_by"] == "Franklin"


def test_the_first_clear_of_171_is_undone_once():
    """Cleared before sections were kept: everything open, OK'd."""
    rookqc.remember("9", **_kept("Scout Allen", ledger.NOT_SET_UP, hours=1, problems=["Not on the hub"]))
    held = ledger.cleared()
    held["last"] = {"at": NOW.isoformat(), "by": "Franklin", "keys": ["9"], "lines": []}
    ledger._cleared_save(held)
    rookqc.remember("9", cleared_at=NOW.isoformat(), cleared_by="Franklin", cleared_problems=["Not on the hub"],
                    ok_before=[], ok_by_before="", ok_problems=["Not on the hub"], ok_by="Franklin - cleared")
    assert ledger.undo_unsectioned_clear() == 1
    assert not rookqc.recalled()["9"].get("ok_problems") and not rookqc.recalled()["9"].get("cleared_at")
    assert ledger.undo_unsectioned_clear() == 0


def test_a_problem_back_after_an_ok_is_not_tagged_again(monkeypatch):
    """Undoing the clear of 171 sent 26 cards again with Franklin tagged:
    their problems had been OK'd off the card, came back, and read as new."""
    from wilbyte.bot import client
    from tests.test_rookqc import _CardChannel

    cards = _CardChannel()
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(client, "_card_ping", lambda bot: "<@1> ")
    monkeypatch.setattr(client, "_sqc_auto_ask", lambda *a, **k: asyncio.sleep(0))
    monkeypatch.setattr(client, "_sqc_escalation", lambda *a, **k: "")
    post = SimpleNamespace(content="AMOUNT: $125.00", embeds=[], message_snapshots=[], jump_url="https://x/901",
                           created_at=now - timedelta(days=4))
    held = {"posted": (now - timedelta(days=4)).isoformat()}
    rookqc.remember("901", **held)

    def card(problems):
        one = rookqc.recalled()["901"]
        before = len(cards.sent)
        asyncio.run(client._sqc_card(SimpleNamespace(), cards, "901", post,
                                     rookqc.Checked(client="Keven Mata Hernandez", problems=list(problems)), one,
                                     due=False, waiting=False))
        return cards.sent[-1].content if len(cards.sent) > before else None

    assert card(["Not on the hub"]) == "@Therese @Nicole <@1>", "tagged the first time"
    card([])  # OK'd off the card
    # A day on, so the once-a-day rule isn't what holds it back.
    rookqc.remember("901", pinged_at=(now - timedelta(days=2)).isoformat())
    assert card(["Not on the hub"]) is None, "back after the OK - not tagged again"
    assert card(["Not on the hub", "The hub says 10 delivered, but the sheet has 0 lead row(s)"]) == "@Therese @Nicole <@1>", \
        "something it never had is"


def test_clear_yesterday_leaves_todays_payments(monkeypatch):
    """"i forgot to clear out payments yesterday"."""
    from wilbyte.bot import client
    from wilbyte.bot import mentions

    assert mentions.parse("<@1> clear yesterday").action == "ledgerclear"
    assert mentions.parse("clear out payments yesterday").action == "ledgerclear"
    rookqc.remember("1", **_kept("Keven Mata Hernandez", ledger.NOT_SET_UP, hours=90, problems=["Not on the hub"]))
    rookqc.remember("2", **_kept("Paid This Morning", ledger.NOT_SET_UP, hours=1, problems=["Not on the hub"]))
    sent = []

    class Said:
        async def send(self, text="", **_):
            sent.append(text)

    async def text(_bot, _which, parts=None):
        parts["lines"] = []
        return ""

    async def replied(_message):
        return None

    monkeypatch.setattr(client, "_ledger_text", text)
    monkeypatch.setattr(client, "_replied_to", replied)
    monkeypatch.setattr(client, "_today", lambda _config: TODAY)
    asking = SimpleNamespace(content="<@1> clear yesterday",
                             author=SimpleNamespace(display_name="Franklin", name="franklin"))
    asyncio.run(client._ledger_clear(SimpleNamespace(config=None), Said(), asking))
    assert rookqc.recalled()["1"].get("cleared_at") and not rookqc.recalled()["2"].get("cleared_at")
    assert "Keven Mata Hernandez" in sent[0] and "Paid This Morning" not in sent[0]


def test_clearing_by_a_time_holds_the_other_lines_to_it_too():
    old = "• **Luke borges (INV-21112)** - the pull doesn't match <https://discord.com/channels/1/2/1566863736684281909>"
    new = "• **Joseph Thomas (INV-21297)** - the pull doesn't match <https://discord.com/channels/1/2/1567292757859639432>"
    cut = ledger._snowflake_time("1567000000000000000")
    _keys, lines = ledger.clear({}, now=NOW, by="Franklin", today=TODAY, through=TODAY, before=cut, lines=[old, new])
    assert lines == [old]


def test_the_team_saying_its_overflow_is_kept(monkeypatch):
    """Therese: "@Ryte Shea ordered overflow. No setup needed on hub" - and
    Ryte answered "Every agent going live today or tomorrow is set up"."""
    from wilbyte.bot import client, mentions

    asked = mentions.parse("<@1> Shea ordered overflow. No setup needed on hub")
    assert asked.action == "sqcsaid"
    kind, name, said = asked.brief.split("\n", 2)
    assert (kind, name) == ("overflow", "Shea")
    now = datetime.now(timezone.utc)
    rookqc.remember("950", **dict(_kept("Shea Scotty", ledger.CANT, hours=2, problems=["Not on the hub"]),
                                  posted=(now - timedelta(hours=2)).isoformat()))
    rookqc.remember("951", **dict(_kept("Kyle Moffitt", ledger.NOT_SET_UP, hours=2, problems=["Not on the hub"]),
                                  posted=(now - timedelta(hours=2)).isoformat()))
    sent, ran = [], []

    class Said:
        async def send(self, text="", **_):
            sent.append(text)

    async def replied(_message):
        return None

    async def run(bot, key, **kw):
        ran.append(key)
        return ""

    monkeypatch.setattr(client, "_replied_to", replied)
    monkeypatch.setattr(client, "_sqc_run", run)
    asking = SimpleNamespace(author=SimpleNamespace(display_name="Therese", name="therese"))
    asyncio.run(client._sqc_team_said(SimpleNamespace(), Said(), asking, kind, name, said))
    assert ran == ["950"] and "Shea Scotty" in sent[0] and "overflow" in sent[0]
    assert rookqc.recalled()["950"]["said_overflow"].startswith("Therese:")
    # Read as overflow from then on.
    result = rookqc.check("AMOUNT: $279.45\nNAME: Shea Scotty\nNotes:\nOverflow - Therese: “Shea ordered overflow”", [])
    assert result.overflow and ledger.stage(result)["stage"] in (ledger.OVERFLOW, ledger.OVERFLOW_DONE)
    # Nobody by that name: said, not guessed.
    asyncio.run(client._sqc_team_said(SimpleNamespace(), Said(), asking, "overflow", "Zed", "Zed is overflow"))
    assert "can't find" in sent[-1]


def test_did_we_miss_anything_is_only_the_misses():
    """"can i ask him did we miss anything and he'll just send me those not
    setup and wrong setup?\""""
    from wilbyte.bot import mentions

    assert mentions.parse("<@1> did we miss anything").action == "missedsetup"
    assert mentions.parse("did rook and nova miss anyone?").action == "missedsetup"
    records = {
        "1": _kept("Keven Mata Hernandez", ledger.NOT_SET_UP, problems=["Not on the hub"]),
        "2": _kept("Dillon Willis", ledger.DELIVERING, problems=["The hub states don't match the payment - has NM not ordered"]),
        "3": _kept("Laura Vargas", ledger.SET_UP, hours=90, start="2026-11-02", status="scheduled"),
        "4": _kept("Riley Noah", ledger.OVERFLOW),
        "5": _kept("Kailey Hull", ledger.NOT_SET_UP, problems=["Not on the hub"], trello="https://trello.com/c/x"),
    }
    said = ledger.daily(records, now=NOW, today=TODAY, through=TODAY, only=ledger.MISSED)
    assert "Keven Mata Hernandez" in said and "Dillon Willis" in said
    for name in ("Laura Vargas", "Riley Noah", "Kailey Hull"):
        assert name not in said
    assert "Nothing missed" in ledger.daily({"3": records["3"]}, now=NOW, today=TODAY, through=TODAY,
                                            only=ledger.MISSED)


def test_the_teams_answers_are_kept_on_each_payment(monkeypatch):
    """Franklin replying "@Ryte" to Therese's answers - "I can't find a New
    Agent card for "Juan Serna - Spark agent, ..." anywhere on the board"."""
    from wilbyte.bot import client

    now = datetime.now(timezone.utc)
    for key, name in (("961", "Juan Serna"), ("962", "Dylan Walker"), ("963", "Jorge Gonzalez-Marin"),
                      ("964", "Mujeeb Anwari"), ("965", "Haroon Anwari"), ("966", "Zachary Johnson")):
        rookqc.remember(key, **dict(_kept(name, ledger.NOT_SET_UP, problems=["Not on the hub"]),
                                    posted=(now - timedelta(hours=3)).isoformat()))
    therese = SimpleNamespace(
        content=("<@2> Juan Serna - Spark agent, has a setup card for Sunday, Nov 8.\n"
                 "Zachary Johnson - Spark agent. Repeat order. Waiting for launch date\n"
                 "Dylan Walker - Aged lead delivered\n"
                 "Jorge Gonzalez-Marin & Anwari - cinonfirm ko pa kay faith ano binayaran, walang details yung zelle"),
        embeds=[], message_snapshots=[], author=SimpleNamespace(display_name="Therese", name="therese", bot=False))
    sent, ran = [], []

    class Said:
        async def send(self, text="", **_):
            sent.append(text)

    async def run(bot, key, **kw):
        ran.append(key)
        held = rookqc.recalled()[key]
        if held.get("team_note"):
            rookqc.remember(key, open_problems=[])
        return ""

    monkeypatch.setattr(client, "_sqc_run", run)
    assert asyncio.run(client._sqc_team_notes(SimpleNamespace(), Said(), therese))
    held = rookqc.recalled()
    assert held["962"]["aged"] is True
    for key in ("961", "963", "964", "965", "966"):
        assert held[key]["team_note"].startswith("Therese:"), key
    said = ledger.daily(held, now=now, today=now.date(), through=now.date())
    head, _, tail = said.partition("Waiting on the team")
    assert "Juan Serna" not in head and "Juan Serna" in tail and "Dylan Walker" not in said
    assert "Nothing missed" in ledger.daily(held, now=now, today=now.date(), through=now.date(), only=ledger.MISSED)
    # A message that isn't about anyone followed isn't taken for one.
    other = SimpleNamespace(content="Nobody Here - hello", embeds=[], message_snapshots=[],
                            author=SimpleNamespace(display_name="Therese", bot=False))
    assert not asyncio.run(client._sqc_team_notes(SimpleNamespace(), Said(), other))


def test_good_to_go_lines_are_the_payments_not_an_ad(monkeypatch):
    """"@Ryte this are good to go: ... Francesca - Spark ad spend agent" was
    written into three ads."""
    from wilbyte.bot import client, mentions

    said = ("<@2> this are good to go:\nAnwari - aged lead orders\n"
            "Juan Serna - Spark agent, has a setup card for Sunday, Nov 8.\n"
            "Zachary Johnson - Spark agent. Already setup under DapperLife name\nDylan Walker - Aged lead\n"
            "Francesca - Spark ad spend agent. Taken care of.\n"
            "Mark Christopherson - Already setup on the hub. 20 OTP VET")
    notes = client.team_notes(said)
    assert len(notes) == 6 and mentions.parse(said).action != "comment"
    now = datetime.now(timezone.utc)
    for key, name in (("971", "Haroon Anwari"), ("972", "Juan Serna"), ("973", "Zachary Johnson"),
                      ("974", "Francesca Lopez"), ("975", "Mark Christopherson")):
        rookqc.remember(key, **dict(_kept(name, ledger.NOT_SET_UP, problems=["Not on the hub"]),
                                    posted=(now - timedelta(hours=3)).isoformat()))
    sent = []

    class Said:
        async def send(self, text="", **_):
            sent.append(text)

    checked_after = []

    async def run(bot, key, **kw):
        # "hes not responding": the answer first, the cards after.
        checked_after.append(bool(sent))
        return ""

    monkeypatch.setattr(client, "_sqc_run", run)

    async def both():
        done = await client._sqc_team_notes(SimpleNamespace(), Said(), franklin)
        await asyncio.sleep(0.05)
        return done

    franklin = SimpleNamespace(content=said, embeds=[], message_snapshots=[],
                               author=SimpleNamespace(display_name="Franklin", bot=False))
    assert asyncio.run(both())
    assert checked_after and all(checked_after)
    held = rookqc.recalled()
    assert held["971"]["aged"] is True
    # "good to go": every one accepted and off the QC, Juan Serna's too.
    for key in ("972", "973", "974", "975"):
        assert held[key].get("said_no_setup"), key
        assert rookqc.among("Not on the hub", held[key]["ok_problems"]), key
    assert "Dylan Walker" in sent[-1]  # not followed: said, not guessed
    for key in ("972", "973", "974", "975"):
        rookqc.remember(key, stage=ledger.FULFILLED)
    listed = ledger.daily(rookqc.recalled(), now=now, today=now.date(), through=now.date())
    for name in ("Haroon Anwari", "Juan Serna", "Zachary Johnson", "Francesca Lopez", "Mark Christopherson"):
        assert name not in listed, name


def test_the_lists_double_entries_and_misreads():
    """The Nov 5 list: Mitchell Daich "Nova says it's done" from "I didn't
    set anything up"; Payra's GRANT MEZA "never posted" while Grant A Meza
    was; overflow and not-set-up names said twice."""
    said = ("@Ryte I didn't set anything up for Mitchell. The $367.50 Zelle from today isn't tied to any fresh "
            "order, and I don't have a pull for it either. His aged order is done.")
    assert not rookqc._done_said(said)
    assert rookqc._done_said("Done ✅ Jay's order is live now: 25 Text-Verified Veterans") 
    # Payra, posted under a middle initial.
    data = {"payments": {"1": {"invoice_number": "INV-21478", "customer": "GRANT MEZA", "amount": 400,
                               "paid_on": "2026-11-05T17:03:00+00:00", "status": "succeeded"}}}
    posted = "NAME: Grant A Meza\nAMOUNT: $400.00\nMETHOD: Zelle"
    assert ledger.payra_missing(data, posted, day=date(2026, 11, 5)) == []
    # Overflow said once, under its own section.
    records = {"1": _kept("Riley Noah", ledger.OVERFLOW, todo=["The team still delivers it from overflow"]),
               "2": _kept("William Monighetti", ledger.NOT_SET_UP, problems=["Not on the hub"])}
    listed = ledger.daily(records, now=NOW, today=TODAY, through=TODAY,
                          board={"someone else": "live"})
    assert listed.count("Riley Noah") == 1
    assert listed.count("William Monighetti") == 1


def test_is_someone_still_active_is_answered_from_the_hub(monkeypatch):
    """"is robert allaraj still active" - "Board says he's live ... that's not
    something I can see from the hub" from the chat."""
    from wilbyte import hub
    from wilbyte.bot import client, mentions

    assert mentions.parse("<@1> is robert allaraj still active").action == "hubstatus"
    assert mentions.parse("is robert allaraj fulfilled?").action == "hubcheck"
    row = hub.HubAgent(id="9", name="Robert Allaraj", status="live", lead_type="Text Verified Mortgage Protection Plus",
                       start_date=date(2026, 10, 29), delivered=22, ordered=25, progress="22/25", schedule="24/7",
                       sheet_url="https://docs.google.com/x", raw={"enabled": True, "delivered_today": 1})
    monkeypatch.setattr(hub, "agents", lambda secrets, status="all": [row])
    sent = []

    class Said:
        async def send(self, text="", **_):
            sent.append(text)

    asyncio.run(client._hub_status(Said(), SimpleNamespace(secrets=None), "robert allaraj"))
    assert "✅ Yes - Robert Allaraj" in sent[0] and "22/25 sent, 3 to go" in sent[0]
    assert "switched **on**" in sent[0] and "**1** today" in sent[0] and "schedule 24/7" in sent[0]


def test_order_details_given_in_a_thread_are_kept(monkeypatch):
    """Santiago's "Invoice number : 21537 / He bought 50 aged IUL ... @Ryte"
    in Richard De Paula's thread: "You pinged and said nothing", then "No
    Payra invoice for 'number : 21537 He bought...'"."""
    from wilbyte.bot import client, mentions

    said = ("<@1> Invoice number : 21537\nHe bought 50 aged IUL (30-90) and 50 aged vet (30-90)\n"
            "States are Florida Georgia California Illinois\n<@2>")
    assert mentions.parse(said).action == "ordergiven"
    assert mentions.parse("add to trello INV-21537 bought 50 leads").action != "ordergiven"
    assert mentions.parse("invoice of David Pereira").action == "invoice"
    now = datetime.now(timezone.utc)
    rookqc.remember("981", **dict(_kept("Richard De Paula", ledger.NOT_SET_UP, problems=["Not on the hub"]),
                                  amount="750.00", posted=(now - timedelta(hours=20)).isoformat()))
    sent = []

    class Said:
        async def send(self, text="", **_):
            sent.append(text)

    thread = SimpleNamespace(name="Richard De Paula", parent=SimpleNamespace(name="rook-nova"))
    santiago = SimpleNamespace(channel=thread, author=SimpleNamespace(display_name="Santiago Villegas", name="s"))
    asyncio.run(client._order_given(SimpleNamespace(), Said(), santiago, said))
    held = rookqc.recalled()["981"]
    assert held["aged"] is True and held["invoice"] == "INV-21537" and "Santiago Villegas" in held["aged_by"]
    assert "Kept on Richard De Paula $750.00 (INV-21537)" in sent[0] and "Nova's pull" in sent[0]
    # Nobody followed by that name: nothing said.
    asyncio.run(client._order_given(SimpleNamespace(), Said(), SimpleNamespace(
        channel=SimpleNamespace(name="Nobody Here", parent=SimpleNamespace()), author=santiago.author), said.replace("21537", "99999")))
    assert len(sent) == 1


def test_lines_off_rytes_own_list_are_read(monkeypatch):
    """Franklin's "this are good to go: 📉 Ended short (3) • Jonathan Flores
    $931.50 · paid 6 days ago — 0 of 25 delivered <link>" - written up as a
    sales script."""
    from wilbyte.bot import client

    now = datetime.now(timezone.utc)
    rookqc.remember("991", **dict(_kept("Jonathan Flores", ledger.SHORT), amount="931.50",
                                  link="https://discord.com/channels/1/2/991", posted=(now - timedelta(days=6)).isoformat()))
    rookqc.remember("992", **dict(_kept("Wes Stoecklin", ledger.SHORT), amount="504.04",
                                  link="https://discord.com/channels/1/2/992", posted=(now - timedelta(days=3)).isoformat()))
    rookqc.remember("993", **dict(_kept("Wes Stoecklin", ledger.DELIVERING), amount="1000.00",
                                  link="https://discord.com/channels/1/2/993", posted=(now - timedelta(days=3)).isoformat()))
    said = ("<@1> this are good to go:\n📉 Ended short (3)\n"
            "• Jonathan Flores $931.50 · paid 6 days ago — 0 of 25 delivered https://discord.com/channels/1/2/991\n"
            "• Wes Stoecklin $504.04 · paid 3 days ago — 3 of 10 delivered Agent Lead Lab Growth Team")
    notes = client.team_notes(said)
    assert [one[0] for one in notes] == [["Jonathan Flores"], ["Wes Stoecklin"]]
    sent = []

    class Said:
        async def send(self, text="", **_):
            sent.append(text)

    async def run(bot, key, **kw):
        return ""

    monkeypatch.setattr(client, "_sqc_run", run)
    franklin = SimpleNamespace(content=said, embeds=[], message_snapshots=[],
                               author=SimpleNamespace(display_name="Franklin", bot=False))
    assert asyncio.run(client._sqc_team_notes(SimpleNamespace(), Said(), franklin))
    held = rookqc.recalled()
    assert held["991"].get("said_no_setup") and held["992"].get("said_no_setup")
    assert not held["993"].get("said_no_setup"), "Wes's other payment, by the amount"
    assert "Accepted" in sent[0]


def test_names_with_their_note_and_no_dash(monkeypatch):
    """"@Ryte William Monighetti (AGED) / Ethan C Spalla (OVERFLOW) /
    Charles loschiavo SETUP DONE / ..." - answered "Every agent going live
    today or tomorrow is set up on what they ordered"."""
    from wilbyte.bot import client

    now = datetime.now(timezone.utc)
    for key, name in (("1001", "William Monighetti"), ("1002", "Ethan C Spalla"), ("1003", "charles loschiavo"),
                      ("1004", "Cavell M Stubbs Jr"), ("1005", "Mitchell Daich")):
        rookqc.remember(key, **dict(_kept(name, ledger.NOT_SET_UP, problems=["Not on the hub"]),
                                    posted=(now - timedelta(hours=8)).isoformat()))
    said = ("<@1>\nWilliam Monighetti (AGED)\nEthan C Spalla  (OVERFLOW)\nMitchell Daich (AGED)\n"
            "Charles loschiavo SETUP DONE\nLee Ohenry (AGED)\nCavell M Stubbs Jr SETUP DONE\n"
            "Taylor Ethans SPARK AGENT. Setup done Starts Mon, Nov 9")
    notes = client.team_notes(said, client._followed_names())
    assert [one[0][0] for one in notes] == ["William Monighetti", "Ethan C Spalla", "Mitchell Daich",
                                           "Charles loschiavo", "Cavell M Stubbs Jr"]
    sent = []

    class Said:
        async def send(self, text="", **_):
            sent.append(text)

    async def run(bot, key, **kw):
        return ""

    monkeypatch.setattr(client, "_sqc_run", run)
    franklin = SimpleNamespace(content=said, embeds=[], message_snapshots=[],
                               author=SimpleNamespace(display_name="Franklin", bot=False))
    assert asyncio.run(client._sqc_team_notes(SimpleNamespace(), Said(), franklin))
    held = rookqc.recalled()
    assert held["1001"]["aged"] is True and held["1005"]["aged"] is True
    assert held["1002"].get("said_overflow")
    assert held["1003"].get("said_no_setup") and held["1004"].get("said_no_setup")
    assert "Lee Ohenry" in sent[-1] and "Taylor Ethans" in sent[-1]


def test_a_long_paste_arrives_as_a_file_and_is_read(monkeypatch):
    """"these are good to go:" with the list as message.txt - Discord's long
    paste - answered "That reads like a clear them out"; then "clearout
    payments" looked for a client channel called payments."""
    from wilbyte.bot import client, mentions

    assert mentions.parse("clearout payments").action == "ledgerclear"
    assert mentions.parse("clearout Jay Rodriguez").action == "clearout"
    pasted = ("• Thomas Poulin $90.00 · paid 7 days ago — Rook and Nova haven't said anything "
              "https://discord.com/channels/1/2/1565724767644811447\n"
              "• owen anderson $720.00 · paid 7 days ago — The hub states don't match "
              "https://discord.com/channels/1/2/1565811739574935586\n")

    class File:
        filename, size = "message.txt", len(pasted)

        async def read(self):
            return pasted.encode()

    message = SimpleNamespace(content="these are good to go:", embeds=[], message_snapshots=[], attachments=[File()],
                              author=SimpleNamespace(display_name="Franklin", bot=False))
    text = asyncio.run(client._message_text(message))
    assert "Thomas Poulin" in text and "owen anderson" in text
    assert len(client.team_notes(text)) == 2


def test_monday_catches_up_from_friday(monkeypatch):
    """"todays monday right? ... he'll go thru friday after eod" - only
    Sunday's and Monday's posts were looked through."""
    from wilbyte.bot import client

    bot = SimpleNamespace(config=SimpleNamespace(schedule=SimpleNamespace(timezone="America/New_York")),
                          _was_alive=None)
    monday = date(2026, 11, 9)
    assert client._catch_up_days(bot, monday) == [date(2026, 11, 6), date(2026, 11, 7), date(2026, 11, 8), monday]
    assert client._catch_up_days(bot, date(2026, 11, 10)) == [date(2026, 11, 9), date(2026, 11, 10)]
    # Off since Wednesday: back to then.
    bot._was_alive = datetime(2026, 11, 4, 20, 0, tzinfo=timezone.utc)
    assert client._catch_up_days(bot, monday)[0] == date(2026, 11, 4)
    # Never more than a week.
    bot._was_alive = datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert client._catch_up_days(bot, monday)[0] == date(2026, 11, 2)


def test_ryte_writes_its_own_question_and_reads_the_answer(monkeypatch):
    """"he doesnt have to use templated message. if he thinks rook and nova did
    wrong, he has to let him know - but ryte has to make sure that he is
    actually right"."""
    from wilbyte import rytevoice
    from wilbyte.bot import client

    written = []

    def compose(config, said):
        written.append(said)
        return "The hub has Text Verified Veteran Plus for Peter Kuyper, but he paid for 31 Veteran Final Expense..."

    monkeypatch.setattr(rytevoice, "compose", compose)
    sent = []

    class Thread:
        guild = SimpleNamespace(members=[SimpleNamespace(bot=True, display_name="Rook", name="Rook", mention="<@11>")])

        async def send(self, text=None, **kw):
            sent.append(text)
            return SimpleNamespace(jump_url="https://discord.com/channels/1/77/500")

    async def thread_for(bot, channel, client_, posted):
        return Thread()

    monkeypatch.setattr(client, "_rook_nova_channel", lambda bot: Thread())
    monkeypatch.setattr(client, "_rn_thread_for", thread_for)
    result = rookqc.Checked(client="Peter Kuyper", problems=["The hub has the lead type “X”, the payment “Y”"])
    post = SimpleNamespace(jump_url="https://discord.com/channels/1/2/990", content="AMOUNT: $1,050.00", embeds=[],
                           message_snapshots=[], created_at=datetime.now(timezone.utc))
    bot = SimpleNamespace(config=SimpleNamespace())
    asyncio.run(client._sqc_ask(bot, "990", post, result))
    assert sent[0].startswith("<@11> <@1529479205165858923> The hub has Text Verified Veteran Plus")
    assert written[0]["client"] == "Peter Kuyper" and written[0]["what_ryte_sees_wrong"] == result.problems

    # Their answer read: "done" while the hub doesn't show it - one follow-up.
    rookqc.remember("990", asked="https://discord.com/channels/1/77/500", asked_at=datetime.now(timezone.utc).isoformat())

    def read(config, said, asked, replies):
        return {"verdict": "still_wrong", "reason": "Nova says done, the hub still has 0 rows for him.",
                "follow_up": "Nova, you said it's done but I still don't see a row for Peter Kuyper on the hub."}

    monkeypatch.setattr(rytevoice, "read_reply", read)
    bot = SimpleNamespace(config=SimpleNamespace(), get_channel=lambda _id: Thread())
    result = rookqc.Checked(client="Peter Kuyper", problems=["Not on the hub"],
                            talk=[("Ryte", "QC on Peter Kuyper ..."), ("Nova", "Done ✅ Peter's set up")])
    asyncio.run(client._sqc_read_answers(bot, "990", post, result, rookqc.recalled()["990"]))
    assert "you said it's done but I still don't see a row" in sent[-1]
    assert any("still wrong" in one for one in result.notes)
    # The same answers: not followed up twice.
    asyncio.run(client._sqc_read_answers(bot, "990", post, result, rookqc.recalled()["990"]))
    assert sum("you said it's done" in one for one in sent) == 1

    # "It's right as is" - OK'd.
    def ok(config, said, asked, replies):
        return {"verdict": "right_as_is", "reason": "Veteran Final Expense is Veteran Plus.", "follow_up": ""}

    monkeypatch.setattr(rytevoice, "read_reply", ok)
    result = rookqc.Checked(client="Peter Kuyper", problems=["The hub has the lead type “X”, the payment “Y”"],
                            talk=[("Ryte", "QC on Peter Kuyper ..."), ("Rook", "That's the same product.")])
    asyncio.run(client._sqc_read_answers(bot, "990", post, result, rookqc.recalled()["990"]))
    assert result.rook_ok.startswith("Rook: Veteran Final Expense is Veteran Plus")


def test_the_plain_question_when_claude_cant_be_had(monkeypatch):
    from wilbyte import rytevoice
    from wilbyte.bot import client

    monkeypatch.setattr(rytevoice, "compose", lambda config, said: "")
    sent = []

    class Thread:
        guild = SimpleNamespace(members=[])

        async def send(self, text=None, **kw):
            sent.append(text)
            return SimpleNamespace(jump_url="https://discord.com/channels/1/77/501")

    async def thread_for(bot, channel, client_, posted):
        return Thread()

    monkeypatch.setattr(client, "_rook_nova_channel", lambda bot: Thread())
    monkeypatch.setattr(client, "_rn_thread_for", thread_for)
    post = SimpleNamespace(jump_url="https://discord.com/channels/1/2/991", content="AMOUNT: $90.00", embeds=[],
                           message_snapshots=[], created_at=datetime.now(timezone.utc))
    asyncio.run(client._sqc_ask(SimpleNamespace(config=SimpleNamespace()), "991", post,
                                rookqc.Checked(client="Tom Skinner", problems=["Not on the hub"])))
    assert "QC on **Tom Skinner**" in sent[0]


def test_ryte_s_facts_are_what_it_read():
    from wilbyte import rytevoice

    result = rookqc.check(PAYRA_FOR_FACTS, [])
    said = rytevoice.facts(result, invoice="INV-1", amount="$90.00")
    assert said["payment"]["amount"] == "$90.00" and said["hub_row"] is None


PAYRA_FOR_FACTS = "New Payment | Payra\nNAME: Tom Skinner\nAMOUNT: $90.00\nPRODUCT: Text-Verified Veteran Leads\n"


def test_nova_s_unanswered_question_is_chased_by_ryte(monkeypatch):
    """Benny Biniamin: Nova asked Faith and Kharyl "what is this payment for"
    and nobody answered - "stuff like ths he should take over no?"."""
    from wilbyte import rytevoice
    from wilbyte.bot import client

    monkeypatch.setattr(rytevoice, "compose_chase", lambda config, said, question, whom: "")
    sent = []

    class Thread:
        guild = SimpleNamespace(members=[SimpleNamespace(bot=False, display_name="🌺 Faith 🌺", name="f", mention="<@41>"),
                                         SimpleNamespace(bot=False, display_name="Kharyl", name="k", mention="<@42>")])

        async def send(self, text=None, **kw):
            sent.append(text)

    async def thread_for(bot, channel, client_, posted):
        return Thread()

    monkeypatch.setattr(client, "_rook_nova_channel", lambda bot: Thread())
    monkeypatch.setattr(client, "_rn_thread_for", thread_for)
    now = datetime.now(timezone.utc)
    rookqc.remember("995", posted=(now - timedelta(hours=1)).isoformat())
    result = rookqc.Checked(client="Benny Biniamin", todo=[
        "Nova asked and nobody's answered: “@🌺 Faith 🌺 @Kharyl what is this payment for (aged or fresh, which order)?”"])
    post = SimpleNamespace(jump_url="https://discord.com/channels/1/2/995", content="AMOUNT: $1,920.00", embeds=[],
                           message_snapshots=[], created_at=now - timedelta(hours=1))
    bot = SimpleNamespace(config=None)
    asyncio.run(client._sqc_chase_question(bot, "995", post, result, rookqc.recalled()["995"]))
    assert sent and sent[0].startswith("<@41> <@42> Nova's question on Benny Biniamin's $1,920.00 is still open")
    asyncio.run(client._sqc_chase_question(bot, "995", post, result, rookqc.recalled()["995"]))
    assert len(sent) == 1, "once a question"


def test_ryte_s_question_sees_the_team_s_notes_on_the_payment():
    """Will Seitz: Kharyl's "Fresh Text Verified Vet: 14, CA" in the payment
    thread, and Ryte asked for the count, lead type and states anyway."""
    from wilbyte import rytevoice

    post = ("New Payment | Zelle\nNAME: William J Seitz\nAMOUNT: $480.00\nNotes:\n"
            "Will Seitz, Fresh Text Verified Vet: 14, CA, live today or add to his current order")
    said = rytevoice.facts(rookqc.check(post, []))
    assert "Fresh Text Verified Vet: 14, CA" in said["payment_post_and_team_notes"]


def test_the_money_at_risk_is_at_the_top():
    # "we cannot lose any money with him qc-ing" - paid and not delivering
    # right, in dollars: not set up, can't, set up wrong, paused with leads owed.
    records = {
        "1": _kept("Chase Kunkel", ledger.NOT_SET_UP, amount="850", problems=["Nothing on the hub for this order yet"]),
        "2": _kept("Gabriel Espada", ledger.LIVE, amount="2,041.02", start="2026-11-02", status="live",
                   problems=["The hub has 30 ordered, paid for 58"]),
        "3": _kept("Antonio Brancato", ledger.CANT, amount="$400", cant="Nova: couldn't set Antonio up"),
        "4": _kept("Pat Paused", ledger.PAUSED, amount="300", delivered=3, ordered=14),
        # Not money falling through: with the team on Trello, and fine.
        "5": _kept("On Trello", ledger.NOT_SET_UP, amount="999", trello="https://trello.com/c/x"),
        "6": _kept("Kyle Fine", ledger.LIVE, amount="5000", start="2026-11-02", status="live"),
    }
    said = ledger.daily(records, now=NOW, today=TODAY, through=date(2026, 11, 4))
    assert "💸 $3,591 paid and not delivering right — 4 payments" in said
    assert "❌ not set up $850 (1)" in said and "⚠️ set up wrong $2,041 (1)" in said
    assert "🃏 can't set up $400 (1)" in said and "⏸️ paused with leads owed $300 (1)" in said
    assert said.index("💸") < said.index("❌ Paid, not set up")

    # Cleared from its section by a person: not counted.
    records["1"].update(cleared_at=NOW.isoformat(), cleared_sections=["❌ Paid, not set up"],
                       cleared_problems=["Nothing on the hub for this order yet"])
    assert "💸 $2,741 paid and not delivering right — 3 payments" in ledger.daily(
        records, now=NOW, today=TODAY, through=date(2026, 11, 4))


def test_nothing_at_risk_says_nothing():
    records = {"1": _kept("Kyle Fine", ledger.LIVE, start="2026-11-02", status="live")}
    assert "💸" not in ledger.daily(records, now=NOW, today=TODAY, through=date(2026, 11, 4))
