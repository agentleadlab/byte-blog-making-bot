"""Rook and Nova's setups checked against each payment - "i want ryte qc-ing
all of rook channel for me ... the sheet, and the actual hub"."""

from datetime import datetime, timedelta, timezone
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


def test_live_on_the_hub_puts_what_went_before_aside():
    """Matthew Stewart, live with 3 delivered: Nova's earlier "I can't find a
    PAID payment" still under Handed to the team, with an Ask button.
    Cheyenne Williams, live on the states she paid for: "Rook's setup has no
    states"."""
    nova = "Handed to the team: the hold after signing is the team's, so only a person can release it."
    result = rookqc.check(PAYRA, [("Rook", ROOK), ("Nova", nova)], hub=_hub(status="live", delivered=3), sheet_rows=3)
    assert result.todo == [] and result.problems == []
    assert any("Handed to the team earlier" in one for one in result.notes)
    bare = ROOK.replace("States: TX, FL, GA", "States: same as before")
    result = rookqc.check(PAYRA, [("Rook", bare), ("Nova", NOVA)], hub=_hub(status="live", delivered=15), sheet_rows=15)
    assert result.problems == [], result.problems
    # Not going yet: the hand-offs are still the team's work.
    result = rookqc.check(PAYRA, [("Rook", ROOK), ("Nova", NOVA)], hub=_hub(status="paused"))
    assert result.todo


def test_several_hub_rows_the_one_this_payment_fits():
    """Jay Rodriguez: Jay_Rodriguez and Jay_Rodriguez_2 on the hub - the
    newest was checked, and read "has IA not ordered"."""
    from datetime import datetime, timezone

    old = _hub(name="Jonathan_Carter", states=("TX", "FL", "IA"), updated_at=datetime(2026, 11, 2, tzinfo=timezone.utc))
    right = _hub(name="Jonathan_Carter_2", states=("TX", "FL", "GA"), updated_at=datetime(2026, 10, 1, tzinfo=timezone.utc))
    result = rookqc.check(PAYRA, [("Rook", ROOK), ("Nova", NOVA)], hub=old, hub_rows=[old, right])
    assert not any("IA" in one for one in result.problems), result.problems
    assert any("2 hub rows" in one and "Jonathan_Carter_2" in one for one in result.notes)


def test_an_overflow_order_is_checked_by_what_was_delivered():
    """Karli Kelley's "Overflow Trucker - $25/lead", 15, delivered by the team:
    held to her older trucker row on the hub. Max Murphy's: "Not on the hub"."""
    post = PAYRA.replace("> Fresh FEX: 40 @ $32.00 — states TX, FL, GA",
                         "> Lead Type: Overflow Trucker - $25/lead\n> States Leads In: FL, PA, CA\n"
                         "> Number of Requested Leads: 15")
    luna = "✅ This order has been fulfilled — Delivered 15 of 15"
    result = rookqc.check(post, [("Luna", luna)], hub=_hub(states=("NC", "MD"), ordered=20))
    assert result.problems == [], result.problems
    assert any("Delivered 15 of 15, as paid for" in one for one in result.ok)
    short = rookqc.check(post, [("Luna", "This order has been fulfilled — Delivered 6 of 6")])
    assert any("Delivered 6 of 6 - the payment was for 15" in one for one in short.problems)
    fresh = rookqc.check(post, [], paid_at=datetime.now(timezone.utc) - timedelta(hours=2))
    assert fresh.problems == [] and any("still delivers it" in one for one in fresh.todo), fresh.problems
    assert fresh.overflow and not rookqc.check(PAYRA, []).overflow
    late = rookqc.check(post, [], paid_at=datetime.now(timezone.utc) - timedelta(days=4))
    assert any("paid 4 days ago, and nothing says it's delivered" in one for one in late.problems)


def test_rooks_setup_for_two_payments():
    """Ryan Gsell: 30 trucker leads paid in two payments of 15 - "Rook's setup
    has 30 leads, the payment 15"."""
    post = PAYRA.replace("Fresh FEX: 40 @", "Fresh FEX: 15 @")
    rook = ROOK.replace("Leads Ordered: 40", "Leads Ordered: 30")
    talk = [("Rook", "Jonathan Carter: BOTH payments are in #new-payment - INV-21095 and INV-21096"),
            ("Rook", rook), ("Nova", NOVA)]
    result = rookqc.check(post, talk, hub=_hub(ordered=34))
    assert not any("30 leads, the payment 15" in one for one in result.problems), result.problems
    assert any("covers more than this payment" in one for one in result.notes)


def test_left_out_isnt_wrong_and_the_payment_nova_couldnt_find_is_this_one():
    """Max Wilson: "Rook's setup has no email", "Rook's setup has no states"
    and Nova's "I can't find a PAID payment for Max Wilson" under Handed to
    the team - while the hub has VT he didn't order."""
    bare = ROOK.replace("Email: jcarter@example.com\n", "").replace("States: TX, FL, GA\n", "")
    nova = ("Handed to the team: I can't find a PAID payment for Jonathan Carter in #new-payment or #onboarding.\n"
            "Handed to the team: The hub holds him for the team's go-ahead after signing.")
    result = rookqc.check(PAYRA, [("Rook", bare), ("Nova", nova)], hub=_hub(status="unsigned",
                                                                             states=("TX", "FL", "GA", "VT")))
    assert result.problems == ["The hub states don't match the payment - has VT not ordered"], result.problems
    assert result.todo == ["The hub holds him for the team's go-ahead after signing."]
    assert any("couldn't find the payment" in one for one in result.notes)


def test_rooks_order_details_in_the_invoice_forms_words():
    """Kyle Moffitt paid for 28 Fresh Vets in HI and AK; Rook posted "order
    details from Rook (the client's texts)" - 31 Final Expense in nine
    states - and it went unread, so nothing was flagged."""
    post = PAYRA.replace("> Fresh FEX: 40 @ $32.00 — states TX, FL, GA", "> Fresh Vets: 28 @ $45.00, 20% off — states HI, AK")
    rook = ("@Nova order details from Rook (the client's texts):\nName: Jonathan Carter\nEmail: jcarter@example.com\n"
            "Phone Number: +15555550101\nLead Type: Text-Verified Final Expense [FRESH - $32/Lead]\n"
            "States Leads In: Hawaii, California, Washington, Alaska, Oregon, Washington, Utah, Texas, Georgia\n"
            "Number of Requested Leads: 31")
    setup = rookqc.rook_setup(rook)
    assert setup.count == 31 and "CA" in setup.states
    said = " ".join(rookqc.check(post, [("Rook", rook), ("Nova", "Discount code ORDER = $32.00/lead off")]).problems)
    assert "31 leads, the payment 28" in said and "lead type" in said and "not ordered" in said


def test_a_row_of_another_lead_type_isnt_this_order():
    """Preston Bridges: his new 31 Fresh Vets, not set up yet, held up against
    his old "Text Verified Mortgage Protection Standard" row, ended."""
    old = _hub(name="Preston_Bridges", lead_type="Text Verified Mortgage Protection Standard", status="ended",
               states=("UT", "AZ"), ordered=54, delivered=54)
    result = rookqc.check(PAYRA, [("Rook", ROOK), ("Nova", "I haven't set anything up for him yet.")], hub=old)
    assert any("Nothing on the hub for this order yet" in one and "Mortgage Protection" in one
               for one in result.problems), result.problems
    assert not any("lead type" in one or "not ordered" in one for one in result.problems)


def test_an_open_row_over_one_thats_over():
    """Isaiah Wehner: four hub rows, a done one checked."""
    done = _hub(name="Isaiah_Wehner", status="done", states=("TX",), ordered=30)
    open_row = _hub(name="Isaiah_Wehner_3", status="scheduled")
    assert rookqc.resolve_hub([done, open_row], rookqc.payment(PAYRA))[0] is open_row


def test_an_open_row_of_another_lead_type_is_still_held_to_the_order():
    """No row of this lead type, but an open one: maybe set up wrong - said."""
    row = _hub(lead_type="Text Verified Mortgage Protection Standard", status="scheduled")
    result = rookqc.check(PAYRA, [("Rook", ROOK), ("Nova", NOVA)], hub=row)
    assert any("lead type" in one for one in result.problems), result.problems
