"""Rook and Nova's setups checked against each payment - "i want ryte qc-ing
all of rook channel for me ... the sheet, and the actual hub"."""

import asyncio
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


def test_a_setup_called_off_isnt_the_one_checked():
    """Marcus Uhrich: Luna's "New fresh setup" for 50, then Rook's "ignore
    Luna's setup above ... already renewed with 55 from INV-21186"."""
    luna = ROOK.replace("Leads Ordered: 40", "Leads Ordered: 50")
    rook_off = ("@Nova ⚠️ ignore Luna's setup above for Jonathan Carter (50 leads). He was already renewed with 40 "
                "from INV-21186 in his thread. Do NOT add or renew again.")
    result = rookqc.check(PAYRA, [("Luna", luna), ("Rook", rook_off), ("Rook", ROOK), ("Nova", NOVA)], hub=_hub())
    assert not any("50 leads" in one for one in result.problems), result.problems
    assert any("Luna's setup (50 leads) was called off by Rook" in one for one in result.notes), result.notes
    alone = rookqc.check(PAYRA, [("Luna", luna), ("Rook", rook_off), ("Nova", NOVA)], hub=_hub())
    assert not any("50 leads" in one for one in alone.problems), alone.problems


def test_a_renewal_added_twice_on_the_hub():
    """The 50 Rook said not to add, added on top of the 40 paid for."""
    talk = [("Rook", ROOK), ("Nova", NOVA)]
    twice = rookqc.check(PAYRA, talk, hub=_hub(ordered=150, delivered=60))
    assert any("90 still to deliver" in one and "added twice" in one for one in twice.problems), twice.problems
    left = rookqc.check(PAYRA, talk, hub=_hub(ordered=103, delivered=60))
    assert not any("still to deliver" in one for one in left.problems)
    assert any("left over from before" in one for one in left.notes)
    assert not any("still to deliver" in one for one in rookqc.check(PAYRA, talk, hub=_hub()).problems)


def test_two_open_hub_rows_for_one_payment():
    """Aaron Crane: "Aaron Crane" and "Aaron_Crane_2", 30 each, both
    unsigned, for one payment of 30 - said ✅. And Nova's warning, handed to
    the team: "the hub would add a second 30 leads ... (he'd be owed 60)"."""
    nova = (NOVA + "\nHanded to the team: I have no way to move a payment from one order to another. If I run the "
            "renewal again with INV-21170, the hub would add a second 30 leads to his order (he'd be owed 60) "
            "instead of re-linking the payment.")
    one = _hub(name="Jonathan Carter", status="unsigned", ordered=40, delivered=0)
    two = _hub(name="Jonathan_Carter_2", status="unsigned", ordered=40, delivered=0)
    result = rookqc.check(PAYRA, [("Rook", ROOK), ("Nova", nova)], hub=one, hub_rows=[one, two])
    said = " ".join(result.problems)
    assert "2 open hub rows" in said and "80 to deliver for the 40 paid for" in said, result.problems
    assert "Nova warns: I have no way to move a payment" in said
    assert "✅" not in rookqc.said(result)
    # An older order still delivering beside the new one isn't that.
    older = _hub(name="Jonathan Carter", status="live", ordered=50, delivered=45)
    fine = rookqc.check(PAYRA, [("Rook", ROOK), ("Nova", NOVA)], hub=two, hub_rows=[older, two])
    assert not any("open hub rows" in one or "added twice" in one for one in fine.problems), fine.problems


def test_something_still_for_the_team_isnt_a_plain_tick():
    result = rookqc.check(PAYRA, [("Rook", ROOK), ("Nova", NOVA)], hub=_hub(), sheet_rows=1)
    assert result.problems == [] and result.todo
    assert "still something for the team" in rookqc.said(result)


def test_a_flag_still_there_and_one_fixed_are_said():
    """Kyle Moffitt: flagged for hub states he didn't order, QC'd again -
    "did ryte check if this was fixed, cuz we flagged this already"."""
    held = {"problems": ["The hub has no sheet for them", "Old problem gone now"], "at": "2026-11-02T21:17:00+00:00",
            "asked": "https://discord.com/channels/1/2/3", "asked_at": "2026-11-02T21:20:00+00:00"}
    result = rookqc.check(PAYRA, [("Rook", ROOK), ("Nova", NOVA)], hub=_hub(sheet_url=""))
    kept = rookqc.flagged_before(result, held)
    said = rookqc.said(result)
    assert "The hub has no sheet for them — **still not fixed** (flagged Nov 2, 4:17 PM)" in said, said
    assert "✅ Fixed since it was flagged: Old problem gone now" in said
    assert "Asked Rook & Nova Nov 2, 4:20 PM" in said
    assert kept == {"The hub has no sheet for them": "2026-11-02T21:17:00+00:00"}
    again = rookqc.check(PAYRA, [("Rook", ROOK), ("Nova", NOVA)], hub=_hub(sheet_url=""))
    rookqc.flagged_before(again, {"flagged": kept})
    assert again.still == kept and again.fixed == []


def test_rook_nova_is_read_once_for_many_payments(monkeypatch):
    """"why so long??" - "setup qc" read #rook-nova whole for each payment."""
    import asyncio
    from wilbyte.bot import client

    reads = {"history": 0, "archived": 0, "inside": 0}
    now = datetime.now(timezone.utc)

    def msg(i, text):
        return SimpleNamespace(id=i, created_at=now - timedelta(minutes=30 - i), content=text, embeds=[],
                               message_snapshots=[], reference=None,
                               author=SimpleNamespace(display_name="Rook", name="Rook", bot=True))

    class Thread:
        id, name, created_at = 77, "Jonathan Carter", now - timedelta(minutes=20)

        def history(self, **kw):
            reads["inside"] += 1

            async def gen():
                yield msg(9, NOVA)
            return gen()

    class Channel:
        threads = [Thread()]

        def history(self, **kw):
            reads["history"] += 1

            async def gen():
                yield msg(1, ROOK)
            return gen()

        def archived_threads(self, **kw):
            reads["archived"] += 1

            async def gen():
                if False:
                    yield None
            return gen()

    monkeypatch.setattr(client, "_rook_nova_channel", lambda bot: Channel())
    held = {"names": ["Jonathan Carter"]}
    for _ in range(5):
        talk = asyncio.run(client._sqc_talk(SimpleNamespace(), "900", held, now - timedelta(hours=1)))
        assert [who for who, _said in talk] == ["Rook", "Rook"], talk
    assert reads == {"history": 1, "archived": 1, "inside": 1}, reads


class _CardChannel:
    """#setup-cards: what's sent, edited and deleted."""

    def __init__(self, name="setup-cards"):
        self.id, self.name, self.mention, self.sent, self.guild = 600, name, f"#{name}", [], None

    async def send(self, content=None, embed=None, view=None, **kw):
        test = self

        class Sent:
            id = 700 + len(test.sent)
            components = []
            deleted = False
            edits = []

            async def edit(self, **kw):
                self.edits.append(kw)

            async def delete(self):
                self.deleted = True

        one = Sent()
        one.content, one.embed, one.view = content, embed, view
        one.edits = []
        self.sent.append(one)
        return one

    async def fetch_message(self, message_id):
        return next(one for one in self.sent if one.id == message_id)


def test_each_payment_has_one_card_kept_up_to_date(monkeypatch):
    """"we can do discord card sure but i want specific channels only where
    thats the only thing that will be there"."""
    from wilbyte.bot import client

    cards = _CardChannel()
    told = []

    class Responder:
        async def send(self, text=None, **kw):
            told.append(text)

    post = SimpleNamespace(jump_url="https://discord.com/channels/1/2/900", content=PAYRA, embeds=[],
                           message_snapshots=[])
    state = {"result": rookqc.check(PAYRA, [("Rook", ROOK)], hub=_hub())}

    async def checking(bot, key):
        return post, state["result"]

    async def card_channel(bot, kind):
        return cards

    monkeypatch.setattr(client, "_sqc_check", checking)
    monkeypatch.setattr(client, "_card_channel", card_channel)
    monkeypatch.setattr(client, "_order_responder", lambda bot: Responder())
    monkeypatch.setattr(client, "_card_ping", lambda bot: "<@1> ")
    bot = SimpleNamespace()
    # Paid, Rook's setup in, Nova not yet: the card's up, waiting, nobody tagged.
    assert asyncio.run(client._sqc_run(bot, "900")) == "waiting"
    assert len(cards.sent) == 1 and cards.sent[0].content is None and "Waiting on Rook" in cards.sent[0].embed.description
    # Fifteen minutes on, Nova still quiet: a problem - sent again at the bottom, Franklin tagged.
    assert asyncio.run(client._sqc_run(bot, "900", due=True)) == "problem"
    assert len(cards.sent) == 2 and cards.sent[1].content == "<@1>" and cards.sent[0].deleted
    assert "Nova hasn't answered" in cards.sent[1].embed.description
    # Nova answers: the same card edited, nobody tagged, nothing new in the channel.
    state["result"] = rookqc.check(PAYRA, [("Rook", ROOK), ("Nova", NOVA)], hub=_hub(), sheet_rows=1)
    assert asyncio.run(client._sqc_run(bot, "900")) == "todo"
    assert len(cards.sent) == 2 and cards.sent[1].edits
    assert "Fixed since it was flagged: Nova hasn't answered" in cards.sent[1].edits[-1]["embed"].description
    assert told == [], "nothing said anywhere but the card's channel"


def test_an_aged_card_waits_on_novas_pull_then_is_checked(monkeypatch):
    from wilbyte.bot import client

    cards = _CardChannel("aged-cards")

    async def card_channel(bot, kind):
        assert kind == "aged"
        return cards

    monkeypatch.setattr(client, "_card_channel", card_channel)
    monkeypatch.setattr(client, "_card_ping", lambda bot: "<@1> ")
    aged = ("New Payment | Payra\nNAME: Kory Parent\nEMAIL: k@example.com\nPRODUCT: Aged IUL\n"
            "INVOICE: INV-21087\nNotes:\n> Aged IUL 30-90: 25 @ $8.00 — states AZ, LA")
    message = SimpleNamespace(id=950, content=aged, embeds=[], message_snapshots=[],
                              jump_url="https://discord.com/channels/1/2/950")
    asyncio.run(client._aged_waiting_card(SimpleNamespace(), message))
    assert len(cards.sent) == 1 and "Waiting on Nova's pull" in cards.sent[0].embed.description
    assert "25 Aged IUL 30-90 - AZ, LA" in cards.sent[0].embed.description
    from wilbyte import agedqc

    done = agedqc.check(aged, "✅ Aged IUL 30-90: 25 pulled (Arizona 20, Louisiana 5) · sheet shared with "
                              "k@example.com\nhttps://docs.google.com/spreadsheets/d/x/edit")
    asyncio.run(client._aged_card(SimpleNamespace(), "950", done, [], link=message.jump_url))
    assert len(cards.sent) == 1 and "matches the payment" in cards.sent[0].edits[-1]["embed"].description


def test_the_cards_go_in_agent_setup(monkeypatch):
    """"i want it here" - The Goat's "👤│agent-setup"; aged ones too until
    they have a channel of their own. None is made."""
    from wilbyte.bot import client

    setup = SimpleNamespace(name="👤│agent-setup", id=1)
    board = SimpleNamespace(name="trello-manager", id=2, category=None)
    guild = SimpleNamespace(text_channels=[board, setup], id=9)
    board.guild = guild
    monkeypatch.setattr(client, "_order_responder", lambda bot: SimpleNamespace(channel=board))
    monkeypatch.setattr(client, "_in_setup_guild", lambda bot, one: False)
    assert asyncio.run(client._card_channel(SimpleNamespace(), "setup")) is setup
    assert asyncio.run(client._card_channel(SimpleNamespace(), "aged")) is setup
    aged = SimpleNamespace(name="🗂│aged-setup", id=3)
    guild.text_channels.append(aged)
    assert asyncio.run(client._card_channel(SimpleNamespace(), "aged")) is aged
    guild.text_channels[:] = [board]
    assert asyncio.run(client._card_channel(SimpleNamespace(), "setup")) is None


def test_a_done_row_smaller_than_the_payment_is_an_older_order():
    """Jared Flanigan: paid for 40, his only hub row done at 10 of 10 -
    "The hub has 10 ordered - less than the 40 paid for", when this order
    isn't on the hub at all yet."""
    done = _hub(status="done", ordered=10, delivered=10)
    result = rookqc.check(PAYRA, [("Rook", ROOK), ("Nova", NOVA)], hub=done)
    said = " ".join(result.problems)
    assert "Nothing on the hub for this order yet" in said and "done, 10 of 10 delivered" in said, result.problems
    assert "an older order" in said and "less than the 40 paid for" not in said
    # Done and as big as the payment: this order, delivered.
    full = rookqc.check(PAYRA, [("Rook", ROOK), ("Nova", NOVA)], hub=_hub(status="done", ordered=40, delivered=40))
    assert not any("Nothing on the hub" in one for one in full.problems), full.problems


def test_novas_question_nobody_answered():
    """James Wyman: Nova asked Rook to confirm his start time for Wednesday,
    and the card said only the Discord channel that failed."""
    nova = (NOVA + "\nCan you confirm his exact start time for Wednesday with him, for example 6 AM in his time "
            "zone? Post it here and I'll set it.")
    result = rookqc.check(PAYRA, [("Rook", ROOK), ("Nova", nova)], hub=_hub(status="scheduled"))
    assert any("Nova asked and nobody's answered" in one and "start time for Wednesday" in one
               for one in result.todo), result.todo
    answered = rookqc.check(PAYRA, [("Rook", ROOK), ("Nova", nova), ("Rook", "6 AM Central")],
                            hub=_hub(status="scheduled"))
    assert not any("Nova asked" in one for one in answered.todo)


def test_a_row_done_before_the_payment_is_an_older_order():
    """Jacob Sides: paid for 6, his hub row done at 8 of 8 before he paid -
    said ✅ as delivered."""
    paid = datetime(2026, 11, 2, 22, 30, tzinfo=timezone.utc)
    old = _hub(status="done", ordered=40, delivered=40, updated_at=paid - timedelta(days=3))
    result = rookqc.check(PAYRA, [("Rook", ROOK), ("Nova", NOVA)], hub=old, paid_at=paid)
    assert any("Nothing on the hub for this order yet" in one for one in result.problems), result.problems
    after = _hub(status="done", ordered=40, delivered=40, updated_at=paid + timedelta(hours=1))
    fine = rookqc.check(PAYRA, [("Rook", ROOK), ("Nova", NOVA)], hub=after, paid_at=paid)
    assert not any("Nothing on the hub" in one for one in fine.problems), fine.problems
