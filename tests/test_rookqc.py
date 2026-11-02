"""Rook and Nova's setups checked against each payment - "i want ryte qc-ing
all of rook channel for me ... the sheet, and the actual hub"."""

from types import SimpleNamespace

from wilbyte import rookqc

PAYRA = """New Payment | Payra ✅
NAME: Jonathan Carter
EMAIL: jcarter@example.com
PHONE: +15555550101
AMOUNT: $1,280.00
PRODUCT: Text-Verified Final Expense Leads
INVOICE: INV-21110 · card •••• 1111 · 11/02/2026 11:20 AM
Notes:
> Name: Jonathan Carter
> Email: jcarter@example.com
> Phone Number: +15555550101
> Fresh FEX: 40 @ $32.00 — states TX, FL, GA"""

ROOK = """@Nova New fresh setup:
Name: Jonathan Carter
Email: jcarter@example.com
Phone: +15555550101
States: TX, FL, GA
Lead Type Ordered: Text-Verified Final Expense (fresh)
Number of Leads Ordered: 40
Launch Date: as soon as he signs"""

NOVA = ("@Rook I set Jonathan's hours (Eastern)... His 40 FEX leads are on hold until the team gives the go-ahead.\n"
        "Handed to the team: his contract is signed, but only the team can release his hold to start him.")


def _hub(**over):
    return SimpleNamespace(**{"name": "Jonathan Carter", "status": "paused", "ordered": 40, "delivered": 0,
                              "daily_cap": 5, "schedule": "Mon-Fri", "lead_type": "Text-Verified Final Expense",
                              "states": ("TX", "FL", "GA"), "sheet_url": "https://docs.google.com/x", **over})


def test_a_setup_that_matches_everywhere():
    result = rookqc.check(PAYRA, [("Rook", ROOK), ("Nova", NOVA)], hub=_hub(), sheet_rows=1)
    assert result.problems == [], result.problems
    assert result.todo == ["his contract is signed, but only the team can release his hold to start him."]
    said = rookqc.said(result)
    assert "✅" in said and "Handed to the team" in said and "The sheet opens" in said


def test_every_field_rook_or_the_hub_got_wrong_is_said():
    rook = ROOK.replace("States: TX, FL, GA", "States: TX, FL, NY").replace("Leads Ordered: 40", "Leads Ordered: 25")
    rook = rook.replace("jcarter@example.com", "jc@example.com").replace("Final Expense", "Veteran")
    result = rookqc.check(PAYRA, [("Rook", rook), ("Nova", NOVA)],
                          hub=_hub(ordered=20, states=("TX",), lead_type="Text-Verified Veteran", sheet_url=""))
    said = " ".join(result.problems)
    for wanted in ("has 25 leads, the payment 40", "jc@example.com", "has NY not ordered", "missing GA",
                   "lead type", "The hub has 20 ordered", "The hub has no sheet"):
        assert wanted in said, wanted


def test_nobody_has_picked_it_up():
    result = rookqc.check(PAYRA, [])
    assert not result.picked_up and "haven't said anything" in result.problems[0]


def test_rook_or_nova_saying_they_cant_set_it_up():
    nova = "@Rook I can't set this up - the hub won't take a Spanish trucker campaign."
    result = rookqc.check(PAYRA, [("Rook", ROOK), ("Nova", nova)], hub=None)
    assert result.cant.startswith("Nova") and any("can't set it up" in one for one in result.problems)
    assert not rookqc.CANT.search(NOVA), "a hold handed to the team isn't a setup they can't do"


def test_a_zelle_payment_is_checked_against_rooks_setup():
    """Kurtis Kunz paid by Zelle as "Kunz Marketing, Llc": the post says
    nothing of states or count - the hub's held to Rook's setup."""
    zelle = "New Payment | Zelle 💰\nNAME: Kunz Marketing, Llc\nAMOUNT: $800.00\nPRODUCT: — (match to their order)"
    rook = ROOK.replace("Jonathan Carter", "Kurtis Kunz")
    result = rookqc.check(zelle, [("Rook", rook), ("Nova", NOVA)], hub=_hub(name="Kurtis Kunz", states=("TX", "FL")))
    assert result.client == "Kurtis Kunz"
    assert any("missing GA" in one for one in result.problems)


def test_all_but_states_are_compared_as_left_out():
    payra = PAYRA.replace("> Fresh FEX: 40 @ $32.00 — states TX, FL, GA",
                          "> Lead Type: Fresh FEX\n> Number of Leads: 40\n> States: All states except NY, CA")
    rook = ROOK.replace("States: TX, FL, GA", "States: All except NY, CA")
    assert rookqc.payment(payra).left_out == ["NY", "CA"]
    result = rookqc.check(payra, [("Rook", rook), ("Nova", NOVA)], hub=_hub(states=()))
    assert not any("Rook's setup" in one and "states" in one for one in result.problems)


def test_a_message_is_matched_to_its_payment():
    from wilbyte.bot import client

    held = {"names": ["Kurtis Kunz"], "emails": ["kurtiskunz@example.com"], "phone": "+18015550123"}
    assert client._sqc_mentions("Kurtis Kunz (25 VET, new): hours ...", "999", held)
    assert client._sqc_mentions("paid https://discord.com/channels/1/2/999", "999", held)
    assert client._sqc_mentions("email KurtisKunz@example.com set", "999", held)
    assert not client._sqc_mentions("Jonathan Carter (40 FEX)", "999", held)


def test_asking_for_it_and_the_question():
    from wilbyte.bot import mentions

    assert mentions.parse("<@1> setup qc").action == "setupqc"
    assert mentions.parse("<@1> setup qc 3 days").action == "setupqc"
    result = rookqc.check(PAYRA, [("Rook", ROOK.replace("Leads Ordered: 40", "Leads Ordered: 25")), ("Nova", NOVA)],
                          hub=_hub())
    asked = rookqc.question(result, link="https://discord.com/x")
    assert "has 25 leads, the payment 40" in asked and "who has it?" in asked


def test_the_qc_is_kept():
    rookqc.remember("900", digest="x")
    assert rookqc.recalled()["900"]["digest"] == "x"


def test_when_the_qc_is_said(monkeypatch):
    import asyncio

    from wilbyte.bot import client

    sent = []

    class Responder:
        async def send(self, text=None, **kw):
            sent.append((text, kw.get("view")))

    post = SimpleNamespace(jump_url="https://discord.com/channels/1/2/900", content=PAYRA, embeds=[],
                           message_snapshots=[])
    state = {"result": rookqc.check(PAYRA, [("Rook", ROOK)], hub=_hub())}

    async def checking(bot, key):
        return post, state["result"]

    monkeypatch.setattr(client, "_sqc_check", checking)
    monkeypatch.setattr(client, "_order_responder", lambda bot: Responder())
    monkeypatch.setattr(client, "_card_ping", lambda bot: "<@1> ")
    bot = SimpleNamespace()
    asyncio.run(client._sqc_run(bot, "900"))
    assert sent == [], "Rook's setup alone, Nova not answered and not 15 minutes yet - nothing said"
    asyncio.run(client._sqc_run(bot, "900", due=True))
    assert len(sent) == 1 and "Nova hasn't answered" in sent[0][0] and sent[0][1] is not None
    asyncio.run(client._sqc_run(bot, "900", due=True))
    assert len(sent) == 1, "the same QC isn't said twice"
    state["result"] = rookqc.check(PAYRA, [("Rook", ROOK), ("Nova", NOVA)], hub=_hub())
    asyncio.run(client._sqc_run(bot, "900"))
    assert len(sent) == 2 and "✅" in sent[1][0] and not sent[1][0].startswith("<@1>")


def test_rook_taking_states_off_the_lead_request_instead_of_the_payment():
    """David Jacob paid for 25 Fresh Vets in 12 states; Rook's setup had 25
    states "from his lead request card", and asked Nova to set up "the 25
    states above"."""
    post = PAYRA.replace("Jonathan Carter", "David Jacob").replace(
        "> Fresh FEX: 40 @ $32.00 — states TX, FL, GA",
        "> Fresh Vets: 25 @ $45.00 — states AL, AZ, AR, CA, DE, GA, IN, KY, MD, MN, PA, TX")
    rook = ("@Nova New fresh setup:\nName: David Jacob\nEmail: jcarter@example.com\nPhone: +15555550101\n"
            "States: CA, TX, PA, AL, AR, AZ, DE, GA, IN, KY, MD, MN, MO, MS, NC, NE, NJ, NV, OH, OK, OR, SC, TN, VT, WI\n"
            "Lead Type Ordered: Text-Verified Veteran leads (fresh)\nNumber of Leads Ordered: 25\nLaunch Date: today ASAP")
    result = rookqc.check(post, [("Rook", rook), ("Nova", "Handed to the team: no payment from David Jacob found.")])
    assert any("has MO, MS, NC, NE, NJ, NV, OH, OK, OR, SC, TN, VT, WI not ordered" in one for one in result.problems)
    assert not any("doesn't say everything" in one for one in result.notes)


def test_an_aged_order_nova_pulled_is_left_to_the_aged_qc(monkeypatch):
    """Jake Scott: "Rook and Nova haven't said anything about this payment in
    #rook-nova" - an aged order, pulled by Nova in the payment's thread."""
    import asyncio

    from wilbyte.bot import client

    sent = []

    class Responder:
        async def send(self, text=None, **kw):
            sent.append(text)

    result = rookqc.check(PAYRA, [])
    result.aged_pull = True

    async def checking(bot, key):
        return SimpleNamespace(jump_url="u", content=PAYRA, embeds=[], message_snapshots=[]), result

    monkeypatch.setattr(client, "_sqc_check", checking)
    monkeypatch.setattr(client, "_order_responder", lambda bot: Responder())
    asyncio.run(client._sqc_run(SimpleNamespace(), "901", due=True))
    assert sent == [] and rookqc.recalled()["901"]["aged"] is True


def test_a_sheet_short_of_what_the_hub_delivered():
    """Carson Rentz: 100 delivered on the hub and "0 row(s)" on the sheet,
    said with a ✅."""
    result = rookqc.check(PAYRA, [("Rook", ROOK), ("Nova", NOVA)], hub=_hub(delivered=100), sheet_rows=0)
    assert any("100 delivered, but the sheet has 0" in one for one in result.problems)
    assert not rookqc.check(PAYRA, [("Rook", ROOK), ("Nova", NOVA)], hub=_hub(delivered=100), sheet_rows=101).problems


def test_on_the_hub_and_going_wins_over_the_talk():
    """"yet on the hub" - Ibrahim Shalash live with 7 delivered after Nova's
    "I couldn't set up Ibrahim Shalash yet"; Timothy Aaron Thomas done 25 of 25
    with Rook's count written another way."""
    nova = "@Faith I couldn't set up Ibrahim Shalash yet, so he has nothing gone out."
    result = rookqc.check(PAYRA, [("Rook", ROOK), ("Nova", nova)], hub=_hub(status="live", delivered=7), sheet_rows=7)
    assert not result.cant and not any("can't set it up" in one for one in result.problems), result.problems
    rook = ROOK.replace("Number of Leads Ordered: 40\n", "").replace("Lead Type Ordered: Text-Verified Final Expense",
                                                                      "Lead Type Ordered: 40 Text-Verified Final Expense")
    assert rookqc.rook_setup(rook).count == 40
    bare = ROOK.replace("Number of Leads Ordered: 40\n", "")
    result = rookqc.check(PAYRA, [("Rook", bare), ("Nova", NOVA)], hub=_hub(status="done", ordered=40, delivered=40),
                          sheet_rows=40)
    assert result.problems == [] and any("no lead count" in one for one in result.notes)
    # Not set up, and said so: still the card's.
    result = rookqc.check(PAYRA, [("Rook", ROOK), ("Nova", "I can't set this up - no hub slot.")], hub=None)
    assert result.cant


def test_a_blank_field_isnt_the_next_lines():
    """Max Wilson: "Rook's setup has the email Phone: +15614022568" - his
    Email line was blank."""
    rook = ROOK.replace("Email: jcarter@example.com", "Email:").replace("States: TX, FL, GA", "States:\nNotes: x")
    setup = rookqc.rook_setup(rook)
    assert setup.email == "" and setup.phone == "+15555550101" and setup.states == []


def test_the_qc_commands_check_today_unless_told():
    """"i want him to qc just the today"."""
    from datetime import date

    from wilbyte.bot import client

    monday = date(2026, 11, 2)
    assert client._qc_days("setup qc", monday) == [monday]
    assert client._qc_days("aged qc 3 days", monday) == [date(2026, 10, 31), date(2026, 11, 1), monday]
    assert client._qc_days("setup qc yesterday", monday) == [date(2026, 11, 1)]
