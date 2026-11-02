"""Nova's aged pulls checked against the payments - "Ryte doesnt need to
create aged leads card anymore ... but i want ryte qc-ing it instead"."""

from wilbyte import agedqc

POST = """New Payment | Payra ✅
NAME: Kory Parent
EMAIL: kory@example.com
PRODUCT: Text-Verified Aged IUL Leads
INVOICE: INV-21087 · card •••• 9204 · 11/02/2026 10:01 AM
Notes:
> Name: Kory Parent
> Email: kory@example.com
> Aged IUL 30-90: 25 @ $8.00 — states AZ, LA, TX, VA, WA"""

DONE = """@Rook Kory Parent's aged order is done (INV-21087) — let them know their sheet is shared with their email:
> ✅ Aged IUL 30-90: **25** pulled (Arizona 5, Louisiana 5, Texas 5, Virginia 5, Washington 5) · sheet shared with kory@example.com
> https://docs.google.com/spreadsheets/d/1P5AgOAWTSHxfGd/edit"""


def test_a_pull_that_matches_the_payment():
    result = agedqc.check(POST, DONE, sheet_rows=25, sheet_states={"AZ": 5, "LA": 5, "TX": 5, "VA": 5, "WA": 5})
    assert result.problems == [] and result.client == "Kory Parent" and result.invoice == "INV-21087"
    assert "matches the payment" in agedqc.said(result)


def test_a_short_pull_a_stray_state_and_the_wrong_email_are_said():
    done = (DONE.replace("**25**", "**20**").replace("Texas 5", "Florida 5")
            .replace("shared with kory@example.com", "shared with someone@example.com"))
    result = agedqc.check(POST, done, sheet_rows=19)
    said = " ".join(result.problems)
    assert "paid for 25, Nova pulled 20" in said and "pulled in FL" in said
    assert "someone@example.com" in said and "The sheet has 19 leads" in said


def test_two_lines_paired_by_what_they_are():
    post = POST.replace("> Aged IUL 30-90: 25 @ $8.00 — states AZ, LA, TX, VA, WA",
                        "> Aged Vets 180+: 100 @ $1.00 — states AL, TX\n> Aged IUL 30-90: 25 @ $8.00 — states AZ")
    done = ("Kory Parent's aged order is done (INV-21087)\n✅ Aged IUL 30-90 days: 25 pulled (Arizona 25)\n"
            "✅ Aged Vets 180+: 100 pulled (Alabama 50, Texas 50) · sheet shared with kory@example.com\n"
            "https://docs.google.com/spreadsheets/d/abc/edit")
    assert agedqc.check(post, done).problems == []
    assert any("pulled none of it" in one for one in agedqc.check(post, done.replace("Aged Vets 180+: 100", "Aged FEX 180+: 100")).problems)


def test_a_sheet_that_cant_be_opened_is_said_not_held():
    result = agedqc.check(POST, DONE, sheet_problem="couldn't open it (403)")
    assert result.problems == [] and "wasn't checked" in agedqc.said(result)


def test_the_sheets_own_states_and_count():
    rows = [["Name", "Phone", "State"], ["A", "1", "AZ"], ["B", "2", "Texas"], [], ["C", "3", "FL"]]
    assert agedqc.sheet_states(rows) == (3, {"AZ": 1, "TX": 1, "FL": 1})
    result = agedqc.check(POST, DONE, sheet_rows=25, sheet_states={"AZ": 20, "FL": 5})
    assert any("FL, which they didn't order" in one for one in result.problems)


def test_the_qc_is_kept_for_the_audit():
    result = agedqc.check(POST, DONE)
    agedqc.remember("900", result)
    assert agedqc.recalled()["900"]["problems"] == []


def test_asking_for_it():
    from wilbyte.bot import mentions

    assert mentions.parse("<@1> aged qc").action == "agedqc"
    assert mentions.parse("<@1> aged qc 3 days").action == "agedqc"


def test_the_state_column_is_found_by_what_it_holds():
    """Kory Parent's sheet: owned by agentleadlab, and "The sheet itself
    wasn't checked" - a link banner above the leads, and no heading called
    State to find the column by."""
    rows = [["https://agentleadlab.com/replace"], ["First", "Last", "Phone", "Location", "Priority"],
            ["Ann", "Lee", "555", "Arizona", "high"], ["Bo", "Ray", "556", "LA", "high"],
            ["https://agentleadlab.com/replace"], ["Cy", "Fox", "557", "Texas", "low"]]
    assert agedqc.sheet_states(rows) == (3, {"AZ": 1, "LA": 1, "TX": 1})
    assert agedqc.sheet_states([["Name"], ["Ann"]]) == (0, {})


def test_the_share_said_other_ways():
    for said in ("sheet sent to kory@example.com", "sheet shared to **kory@example.com**",
                 "access for kory@example.com"):
        result = agedqc.check(POST, DONE.replace("sheet shared with kory@example.com", said))
        assert not any("who the sheet was shared with" in one for one in result.problems), said


def test_an_order_written_as_the_invoices_form():
    """Mitchell Daich: "I couldn't read the payment's order lines" and his 30
    pulled flagged as "isn't on the payment"."""
    post = ("New Payment | Payra ✅\nNAME: Mitchell Daich\nEMAIL: mitch@example.com\n"
            "PRODUCT: Text-Verified Aged Veteran Leads\nINVOICE: INV-21108 · card •••• 1 · 11/02/2026\nNotes:\n"
            "> Name: Mitchell Daich\n> Lead Type: Text-Verified Aged Veteran - 30-90 days [AGED - $7/Lead]\n"
            "> Number of Requested Leads: 30\n> States Leads In: Texas, Florida")
    done = ("Mitchell Daich's aged order is done (INV-21108)\n✅ Aged Veteran 30-90 days: **30** pulled (Texas 20, "
            "Florida 10) · sheet shared with mitch@example.com\nhttps://docs.google.com/spreadsheets/d/abc/edit")
    result = agedqc.check(post, done)
    assert result.problems == [], result.problems
    assert sorted(agedqc.ordered(post)[0].states) == ["FL", "TX"]


RACHNA_ASK = ("@Nova aged lead order for Rachna Khim (rachna@example.com) — Tre approved a SWAP of her paid Nov 1 "
              "order ($155.25): she was sent 150 Aged Mortgage Protection 180+ by mistake and wanted vets. Pull "
              "**150 Aged Veteran 180+**, states TX CA AZ FL IN, camp none, and share the sheet with her email.\n"
              "repeat ok")
RACHNA_DONE = ("> 💰 Aged Vets 180+: **150** pulled (Arizona 30, California 30, Texas 30, Florida 30, Indiana 30) · "
               "sheet shared with rachna@example.com\nhttps://docs.google.com/spreadsheets/d/abc/edit")


def test_an_aged_pull_rook_asked_for_in_rook_nova():
    """Rachna Khim's swap: no payment post of its own - held to Rook's ask."""
    asked = agedqc.rook_asked(RACHNA_ASK)
    assert asked.client == "Rachna Khim" and asked.email == "rachna@example.com"
    assert [(one.package, one.count, one.states) for one in asked.lines] == [
        ("Aged Veteran 180+", 150, ["TX", "CA", "AZ", "FL", "IN"])]
    result = agedqc.check("", RACHNA_DONE, asked=asked, sheet_rows=150)
    assert result.problems == [], result.problems
    assert "matches Rook's ask" in agedqc.said(result)
    wrong = agedqc.check("", RACHNA_DONE.replace("**150** pulled", "**120** pulled")
                         .replace("Indiana 30", "Ohio 0").replace("rachna@", "rk@"), asked=asked)
    said = " ".join(wrong.problems)
    assert "Rook asked for 150, Nova pulled 120" in said and "OH" in said and "Rook's email" in said


def test_a_stock_check_isnt_a_pull():
    ask = ("@Nova aged stock check for Aiden Larsen (Wolfpack): 50 Aged IUL 90-180, states AZ IL LA MI SC AK TX — "
           "how many are available to him? (check only, do not pull)")
    assert agedqc.rook_asked(ask) is None


def test_rook_asking_for_two_aged_lines():
    asked = agedqc.rook_asked("@Nova replacement for Jo Doe (j@example.com): pull 20 Aged FEX 90-180 in states GA "
                              "FL and 10 Aged Vets 30-90 in states TX")
    assert [(one.package, one.count, one.states) for one in asked.lines] == [
        ("Aged FEX 90-180", 20, ["GA", "FL"]), ("Aged Vets 30-90", 10, ["TX"])]


def test_the_rook_nova_aged_qc_waits_for_nova_and_says_it_once(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from wilbyte.bot import client

    sent = []

    class Responder:
        async def send(self, text=None, **kw):
            sent.append(text)

    nova = SimpleNamespace(bot=True, id=7, display_name="Nova", name="Nova")
    rook = SimpleNamespace(bot=True, id=8, display_name="Rook", name="Rook")
    ask = SimpleNamespace(content=RACHNA_ASK, embeds=[], message_snapshots=[], author=rook)
    done = SimpleNamespace(content=RACHNA_DONE, embeds=[], message_snapshots=[], author=nova,
                           jump_url="https://discord.com/channels/1/2/3/4")

    class Thread:
        id = 55
        jump_url = "https://discord.com/channels/1/55"
        parent = SimpleNamespace(name="⏳｜rook-nova")

        async def _fetch(self, _id):
            return ask

        def history(self, **kw):
            async def gen():
                for one in (ask, done):
                    yield one
            return gen()

    thread = Thread()
    thread.parent.fetch_message = thread._fetch
    done.channel = thread
    bot = SimpleNamespace(user=SimpleNamespace(id=1), config=None)
    assert client._aged_rook_pull(bot, done)
    monkeypatch.setattr(client, "AGED_ROOK_WAIT", 0)
    monkeypatch.setattr(client, "_order_responder", lambda bot: Responder())
    monkeypatch.setattr(client, "_card_ping", lambda bot: "<@1> ")
    monkeypatch.setattr(client.jobs, "aged_sheet_read", lambda config, url: (150, {"TX": 150}, ""))
    asyncio.run(client._aged_rook_qc(bot, done))
    asyncio.run(client._aged_rook_qc(bot, done))
    assert len(sent) == 1 and "Rachna Khim" in sent[0] and "matches Rook's ask" in sent[0], sent
