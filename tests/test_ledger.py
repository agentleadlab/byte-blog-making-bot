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
    assert "⏰ Should be live and isn't** (1)" in said and "James Wyman" in said
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
    monkeypatch.setattr(client, "SETUP_QC_ASK_GAP", timedelta(0))
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
