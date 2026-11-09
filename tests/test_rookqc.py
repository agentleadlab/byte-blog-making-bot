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
    # More states on the hub than ordered is said, not flagged.
    assert result.problems == [], result.problems
    assert any("VT too" in one for one in result.notes)
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
    assert len(cards.sent) == 2 and cards.sent[1].content == "@Therese @Nicole <@1>" and cards.sent[0].deleted
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


def test_nova_couldnt_set_them_up_by_name_and_paid_in_parts():
    """Charles Sellers: "I couldn't set Charles up, so I've handed it to the
    team" - no 🃏, no Make the card - and INV-21037 paid by Zelle in two parts."""
    zelle = "New Payment | Zelle 💰\nNAME: Jonathan Carter\nAMOUNT: $160.00\nPRODUCT: — (match to their order)"
    rook = ROOK.replace("Leads Ordered: 40", "Leads Ordered: 60") + ("\nSpecific Notes: Camp Takeover Financial. Paid INV-21037 ($2,160) by Zelle in two parts: "
                   "$2,000 (Nov 2) + $160 (today).")
    nova = ("@Rook I couldn't set Jonathan up, so I've handed it to the team. He's a returning client.\n"
            "Handed to the team: More than one recent payment matches Jonathan Carter — which invoice?")
    result = rookqc.check(zelle, [("Rook", rook), ("Nova", nova)],
                          hub=_hub(status="done", ordered=55, delivered=55))
    assert result.cant.startswith("Nova"), (result.cant, result.problems)
    assert any(one.startswith("Paid in parts") and "$2,000 (Nov 2) + $160 (today)" in one for one in result.notes)
    assert "can't set it up" in rookqc.said(result)


def test_cant_set_it_up_is_hammered_once(monkeypatch):
    """"this should auto hammer then right? so we can set it up trello way" -
    Charles Sellers: hammered once, not again, nor for his other payment."""
    from wilbyte.bot import client

    hammered = []

    async def gavelled(bot, payload):
        hammered.append(payload.message_id)
        # Rook's setup carried onto the card - Charles Sellers' $160 Zelle
        # post came up "Nothing to make a card from".
        notes = "\n".join(payload.setup_notes)
        assert "Email: jcarter@example.com" in notes and "Number of Requested Leads: 40" in notes, notes
        assert "States Leads In: TX, FL, GA" in notes and "Lead Type: Text-Verified Final Expense" in notes

    nova = "@Rook I couldn't set Jonathan up, so I've handed it to the team."
    result = {"one": None}

    async def checking(bot, key):
        post = SimpleNamespace(jump_url=f"https://discord.com/channels/1/2/{key}", content=PAYRA, embeds=[],
                               message_snapshots=[], channel=SimpleNamespace(id=2), guild=None)
        return post, rookqc.check(PAYRA, [("Rook", ROOK), ("Nova", nova)], hub=None)

    class Responder:
        async def send(self, text=None, **kw):
            result["one"] = text

    monkeypatch.setattr(client, "_sqc_check", checking)
    monkeypatch.setattr(client, "_order_gavelled", gavelled)
    monkeypatch.setattr(client, "_order_responder", lambda bot: Responder())
    monkeypatch.setattr(client, "_card_ping", lambda bot: "")
    now = datetime.now(timezone.utc).isoformat()
    # Off: "okay fine turn it off" - nothing hammered, the button's there.
    rookqc.remember("899", posted=now, name="Somebody Else")
    asyncio.run(client._sqc_run(SimpleNamespace(), "899", due=True))
    assert hammered == [] and "Hammered" not in result["one"]
    monkeypatch.setattr(client, "SETUP_QC_AUTO_HAMMER", True)
    rookqc.remember("900", posted=now, name="Jonathan Carter")
    rookqc.remember("901", posted=now, name="Jonathan Carter")
    asyncio.run(client._sqc_run(SimpleNamespace(), "900", due=True))
    assert hammered == [900] and "Hammered for its Trello card automatically" in result["one"]
    asyncio.run(client._sqc_run(SimpleNamespace(), "900", force=True))
    asyncio.run(client._sqc_run(SimpleNamespace(), "901", due=True))
    assert hammered == [900], "once, and not for the other part of the same order"
    assert "other payment was hammered" in result["one"]


def test_a_cant_flagged_before_the_hammer_is_checked_again_on_starting(monkeypatch):
    """"why no hammer here?" - Charles Sellers was flagged can't-set-up before
    the hammer came in, and nothing new was said to bring his QC back."""
    from wilbyte.bot import client

    later = []
    monkeypatch.setattr(client, "SETUP_QC_AUTO_HAMMER", True)
    monkeypatch.setattr(client, "_sqc_later", lambda bot, key, wait, due=False: later.append((key, due)))
    now = datetime.now(timezone.utc)
    rookqc.remember("900", posted=(now - timedelta(hours=3)).isoformat(), due_done=True, cant="Nova: couldn't set")
    rookqc.remember("901", posted=(now - timedelta(hours=3)).isoformat(), due_done=True, cant="Nova: couldn't set",
                    hammered=True)
    rookqc.remember("902", posted=(now - timedelta(hours=3)).isoformat(), due_done=True)
    client._sqc_rearm(SimpleNamespace())
    assert later == [("900", False)], later


def test_a_couldnt_from_before_isnt_hammered_once_its_set_up():
    """Antonio Brancato: hammered for a card a day on - a "couldn't" from
    before, while he was on the hub for this order, or Nova set him up after."""
    nova = "@Rook I couldn't set Jonathan up, so I've handed it to the team."
    paid = datetime(2026, 11, 2, 21, 14, tzinfo=timezone.utc)
    there = _hub(status="unsigned", updated_at=paid + timedelta(hours=1))
    on_hub = rookqc.check(PAYRA, [("Rook", ROOK), ("Nova", nova)], hub=there, paid_at=paid)
    assert not on_hub.cant and any("couldn't set it up - the hub has them unsigned" in one for one in on_hub.notes)
    later = rookqc.check(PAYRA, [("Rook", ROOK), ("Nova", nova), ("Nova", "Done ✅ Jonathan's start is set for "
                                                                          "tomorrow")], hub=None)
    assert not later.cant and any("Nova said it's set up after" in one for one in later.notes)
    still = rookqc.check(PAYRA, [("Rook", ROOK), ("Nova", nova)], hub=None)
    assert still.cant


def test_the_question_says_what_was_paid_for_and_what_to_do():
    """Marco Ayala's was asked as Ryte's own QC line - nothing of what he'd
    paid for, nor what Rook and Nova should do."""
    result = rookqc.check(PAYRA, [("Rook", ROOK), ("Nova", NOVA)], hub=_hub(status="done", ordered=30, delivered=31))
    asked = rookqc.question(result, link="https://discord.com/x", invoice="INV-21110")
    assert "paid $1,280.00 · INV-21110 · 40 Fresh FEX · TX, FL, GA" in asked, asked
    assert asked.endswith("Is this order set up on the hub? If not, please set it up - or say so if you can't.")
    fine = rookqc.check(PAYRA, [("Rook", ROOK), ("Nova", NOVA)], hub=_hub(sheet_url=""))
    assert rookqc.question(fine).endswith("fix what's wrong on the hub, and confirm?")


def test_rook_taking_it_to_the_team_is_heard():
    """Carson Rentz: "Got it — forwarded to Therese + Nicole in Carson Rentz's
    #rook-qc thread to check the 100 vs 21"."""
    rook_said = "Got it — forwarded to Therese + Nicole in Jonathan Carter's #rook-qc thread to check the 100 vs 21."
    result = rookqc.check(PAYRA, [("Rook", ROOK), ("Nova", NOVA), ("Rook", rook_said)], hub=_hub())
    assert result.with_team and any("took it to the team" in one for one in result.notes)
    assert not rookqc.check(PAYRA, [("Rook", ROOK), ("Nova", NOVA)], hub=_hub()).with_team


def test_leads_split_over_tabs_are_counted_once_each():
    from wilbyte import delivery

    head = ["Name", "Phone", "State"]
    one = [head, ["A B", "(555) 555-0101", "TX"], ["C D", "555-555-0102", "TX"]]
    two = [head, ["E F", "5555550103", "FL"]]
    summary = [head, ["A B", "5555550101", "TX"], ["C D", "5555550102", "TX"], ["E F", "5555550103", "FL"]]
    assert len(delivery.lead_keys(one) | delivery.lead_keys(two) | delivery.lead_keys(summary)) == 3


def test_an_aged_pull_in_rook_nova_isnt_asked_about_as_a_setup(monkeypatch):
    """Kane Curtis: a $175 Zelle, pulled as "Aged FEX 30-90: 50 pulled" in his
    #rook-nova thread - and asked about as a setup."""
    from wilbyte.bot import client

    zelle = "New Payment | Zelle 💰\nNAME: Jonathan Carter\nAMOUNT: $175.00\nPRODUCT: — (match to their order)"
    talk = [("Rook", "@Nova aged lead order for Jonathan Carter (jc@example.com) - Pull **50 Aged FEX 30-90**, "
                     "states KS OH, and share the sheet with his email."),
            ("Nova", "> 💰 Aged FEX 30-90: **50** pulled (Kansas 6, Ohio 44)")]
    post = SimpleNamespace(id=900, jump_url="https://discord.com/channels/1/2/900", content=zelle, embeds=[],
                           message_snapshots=[], created_at=datetime.now(timezone.utc))
    channel = SimpleNamespace(fetch_message=None)

    async def fetch(_id):
        return post

    channel.fetch_message = fetch

    async def talking(bot, key, held, posted):
        return list(talk)

    async def no_thread(bot, key, held):
        return None

    async def hub(bot):
        return []

    rookqc.remember("900", channel_id="2", posted=post.created_at.isoformat(), name="Jonathan Carter")
    monkeypatch.setattr(client, "_sqc_talk", talking)
    monkeypatch.setattr(client, "_order_thread", no_thread)
    monkeypatch.setattr(client, "_sqc_hub", hub)
    bot = SimpleNamespace(get_channel=lambda _id: channel)
    _post, result = asyncio.run(client._sqc_check(bot, "900"))
    assert result.aged_pull and not result.talk_about_setup()
    assert asyncio.run(client._sqc_run(bot, "900", due=True)) == ""
    assert rookqc.recalled()["900"].get("aged")


def test_rooks_states_differ_but_the_hub_has_the_payments():
    """justin techer: IN added after Rook's setup - "Rook's setup states don't
    match the payment - missing IN" with the hub's all right."""
    rook = ROOK.replace("States: TX, FL, GA", "States: TX, FL")
    result = rookqc.check(PAYRA, [("Rook", rook), ("Nova", NOVA)], hub=_hub())
    assert not any(one.startswith("Rook's setup states") for one in result.problems), result.problems
    assert any("the hub has the right ones" in one for one in result.notes)
    wrong_hub = rookqc.check(PAYRA, [("Rook", rook), ("Nova", NOVA)], hub=_hub(states=("TX", "FL")))
    assert any(one.startswith("Rook's setup states") for one in wrong_hub.problems)


def test_somebody_paying_for_them_that_rooks_setup_names():
    """Preston Tackett: two of three Zelle parts sent by his friend Ryan M
    Thompson - "Rook's setup has the name Preston Tackett, the payment Ryan
    M Thompson"."""
    zelle = "New Payment | Zelle 💰\nNAME: Ryan M Thompson\nAMOUNT: $500.00\nPRODUCT: — (match to their order)"
    rook = ROOK + ("\nSpecific Notes: Paid by Zelle in three parts: one from Jonathan Carter and two from Ryan "
                   "Thompson / Ryan M Thompson (his friend sent them for him).")
    result = rookqc.check(zelle, [("Rook", rook), ("Nova", NOVA)], hub=_hub())
    assert not any("has the name" in one for one in result.problems), result.problems
    assert any("Paid by Ryan M Thompson for Jonathan Carter" in one for one in result.notes)
    # Another client's setup, not saying they paid for him: not held up
    # against this payment at all - no guessing.
    stranger = rookqc.check(zelle, [("Rook", ROOK), ("Nova", NOVA)], hub=_hub())
    assert not any("has the name" in one for one in stranger.problems)
    assert any('No "New setup" message from Rook' in one for one in stranger.notes)


def test_trucker_real_times_is_trucker_iul():
    """Riley Noah: "yes same product"."""
    assert rookqc.family("Text Verified Trucker Real Times") == rookqc.family("Text-Verified Trucker IUL leads (fresh)")
    assert rookqc.family("Text Verified Trucker Real Time") == rookqc.family("Text Verified Trucker IUL")
    assert rookqc.family("Text Verified Trucker FEX") != rookqc.family("Text Verified Trucker IUL")


def test_will_is_william_on_the_hub():
    """William Stanger paid; the hub, and Nova's #launching, have Will Stanger."""
    rows = [_hub(name="Will Stanger"), _hub(name="Will Smith"), _hub(name="Bill Stanley")]
    assert [one.name for one in rookqc.hub_named(rows, "William Stanger")] == ["Will Stanger"]
    assert [one.name for one in rookqc.hub_named(rows, "Will Stanger")] == ["Will Stanger"]
    assert rookqc.hub_named(rows, "Wilma Stanger") == []


def test_fresh_and_aged_on_one_invoice():
    """William McClure: 50 Fresh Vets and 100 Aged IUL 180+ read as 150 "Fresh
    Vets + Aged IUL 180+" against his Vets setup."""
    post = PAYRA.replace("> Fresh FEX: 40 @ $32.00 — states TX, FL, GA",
                         "> Fresh FEX: 40 @ $32.00 — states TX, FL, GA\n> Aged IUL 180+: 100 @ $3.00 — states TX")
    paid = rookqc.payment(post)
    assert paid.count == 40 and paid.lead_type == "Fresh FEX" and paid.states == ["TX", "FL", "GA"]
    assert paid.aged == "100 Aged IUL 180+"
    result = rookqc.check(post, [("Rook", ROOK), ("Nova", NOVA)], hub=_hub(), sheet_rows=1)
    assert result.problems == [], result.problems
    assert any("Also on the invoice: 100 Aged IUL 180+" in one for one in result.notes)


def test_iuls_plural_is_iul():
    """Connor Swartz: "Fresh Trucker IULs" against the hub's Trucker IUL."""
    assert rookqc.family("Fresh Trucker IULs") == rookqc.family("Text Verified Trucker IUL")


def test_the_same_amount_twice_minutes_apart(monkeypatch):
    """Anthony Speciale, Calvin Child, Ayman Kafafi: two posts each, same
    amount, minutes apart - one said nothing's been said about it."""
    from wilbyte.bot import client

    now = datetime.now(timezone.utc)
    rookqc.remember("900", posted=(now - timedelta(minutes=9)).isoformat(), name="Calvin Child", client="Calvin Child",
                    amount="1,050.00", link="https://discord.com/channels/1/2/900")
    rookqc.remember("901", posted=now.isoformat(), name="Calvin Child")
    post = SimpleNamespace(content="AMOUNT: $1,050.00", embeds=[], message_snapshots=[], created_at=now)
    result = SimpleNamespace(client="Calvin Child", notes=[],
                             problems=["Rook and Nova haven't said anything about this payment in #rook-nova"])
    client._sqc_twin("901", post, result, rookqc.recalled()["901"])
    assert len(result.problems) == 1 and "Same $1,050.00 from them 9 min after" in result.problems[0]
    assert "posted twice, or paid twice" in result.problems[0] and result.twin
    parts = SimpleNamespace(client="Calvin Child", notes=["Paid in parts - “two parts”"], problems=[])
    client._sqc_twin("901", post, parts, rookqc.recalled()["901"])
    assert parts.problems == []


def test_all_but_and_the_states_written_out_are_the_same():
    """Duehat Kanan: Rook's "all but NY" against the 50 others written out.
    Ciana Araya: the hub's 47 against "all but NY, UT, ND, SD"."""
    from wilbyte.ordercards import US_STATES

    every = [code for code in US_STATES if code != "NY"]
    post = PAYRA.replace("— states TX, FL, GA", "— states " + ", ".join(every))
    rook = ROOK.replace("States: TX, FL, GA", "States: all but NY")
    result = rookqc.check(post, [("Rook", rook), ("Nova", NOVA)], hub=_hub(states=tuple(c for c in every if c != "DC")))
    assert not any("states" in one.casefold() for one in result.problems), result.problems
    assert any("DC left off" in one for one in result.notes)
    four = rookqc.check(PAYRA.replace("— states TX, FL, GA", "— states all but NY, UT, ND, SD"),
                        [("Rook", ROOK.replace("States: TX, FL, GA", "States: all but NY, UT, ND, SD")),
                         ("Nova", NOVA)],
                        hub=_hub(states=tuple(c for c in US_STATES if c not in ("NY", "UT", "ND", "SD"))))
    assert not any("states" in one.casefold() for one in four.problems), four.problems


def test_a_row_with_every_lead_delivered_is_an_older_order():
    """Brandon Nguyen, Benny Biniamin: "paused" at 31 of 30 and 26 of 25, held
    up as their new payments set up."""
    from datetime import date

    paid = datetime(2026, 11, 2, 18, 0, tzinfo=timezone.utc)
    filled = _hub(status="paused", ordered=40, delivered=41, start_date=date(2026, 10, 20),
                  updated_at=paid - timedelta(days=3))
    result = rookqc.check(PAYRA, [("Rook", ROOK), ("Nova", NOVA)], hub=filled, paid_at=paid)
    assert any("Nothing on the hub for this order yet" in one for one in result.problems), result.problems
    # Jay Trandahl: his 25 renewed onto his Oct 29 row, delivered since - this order, done.
    renewed = _hub(status="paused", ordered=55, delivered=57, start_date=date(2026, 10, 20),
                   updated_at=paid + timedelta(days=1))
    jay = rookqc.check(PAYRA, [("Rook", ROOK), ("Nova", NOVA)], hub=renewed, paid_at=paid)
    assert not any("Nothing on the hub" in one for one in jay.problems), jay.problems
    today = _hub(status="paused", ordered=40, delivered=41, start_date=date(2026, 11, 2))
    fast = rookqc.check(PAYRA, [("Rook", ROOK), ("Nova", NOVA)], hub=today, paid_at=paid)
    assert not any("Nothing on the hub" in one for one in fast.problems), "started the day it was paid: this one"


def test_names_with_apostrophes_initials_and_jr():
    rows = [_hub(name="D'shane Bannister"), _hub(name="Cavell Stubbs"), _hub(name="William Seitz")]
    assert [one.name for one in rookqc.hub_named(rows, "D’shane Bannister")] == ["D'shane Bannister"]
    assert [one.name for one in rookqc.hub_named(rows, "Cavell M Stubbs Jr")] == ["Cavell Stubbs"]
    assert [one.name for one in rookqc.hub_named(rows, "William J Seitz")] == ["William Seitz"]


def test_nobody_said_anything_but_the_hub_has_it_since_the_payment():
    """Thomas Poulin: "Rook and Nova haven't said anything" listed as set up
    wrong, while the hub had him for this order."""
    paid = datetime(2026, 11, 2, 18, 0, tzinfo=timezone.utc)
    row = _hub(status="live", delivered=3, updated_at=paid + timedelta(hours=2))
    result = rookqc.check(PAYRA, [], hub=row, paid_at=paid)
    assert not any("haven't said anything" in one for one in result.problems), result.problems
    assert any("checked on the hub alone" in one for one in result.notes)
    stale = rookqc.check(PAYRA, [], hub=_hub(updated_at=paid - timedelta(days=2)), paid_at=paid)
    assert any("haven't said anything" in one for one in stale.problems)
    answered_not = rookqc.check(PAYRA, [("Rook", ROOK)], hub=row, paid_at=paid)
    assert not any("Nova hasn't answered" in one for one in answered_not.problems)


def test_otp_truckers_is_trucker_iul_and_a_business_payer_is_a_note():
    assert rookqc.family("OTP Truckers") == rookqc.family("Text Verified Trucker IUL")
    zelle = PAYRA.replace("NAME: Jonathan Carter", "NAME: The Eissa Group Llc")
    result = rookqc.check(zelle, [("Rook", ROOK), ("Nova", NOVA)], hub=_hub())
    assert not any("has the name" in one for one in result.problems), result.problems


def test_their_setup_over_another_clients_the_talk_picked_up():
    """The $500 Ryan M Thompson sent for Preston Tackett came out as Preston
    Bridges'."""
    zelle = "New Payment | Zelle 💰\nNAME: Ryan M Thompson\nAMOUNT: $500.00\nPRODUCT: — (match to their order)"
    tackett = ROOK.replace("Jonathan Carter", "Preston Tackett") + "\nSpecific Notes: two parts from Ryan M Thompson."
    bridges = ROOK.replace("Jonathan Carter", "Preston Bridges").replace("jcarter@", "pb@")
    result = rookqc.check(zelle, [("Rook", tackett), ("Rook", bridges), ("Nova", NOVA)], hub=_hub())
    assert result.client == "Preston Tackett"


def test_strict_names():
    assert rookqc.same_person_strict("Will Stanger", "William Stanger")
    assert not rookqc.same_person_strict("Duehat Kanan", "Mohammad Kanan")
    assert not rookqc.same_person_strict("Brandon Nguyen", "Mai Nguyen")


def test_a_problem_whose_numbers_moved_is_still_the_same_one():
    """Karli Kelley's overflow re-tagged Franklin as new each day: "paid 3
    days ago" became "paid 4 days ago"."""
    result = rookqc.Checked(problems=["Overflow order paid 4 days ago, and nothing says it's delivered yet"])
    kept = rookqc.flagged_before(result, {"flagged": {
        "Overflow order paid 3 days ago, and nothing says it's delivered yet": "2026-11-02T15:00:00+00:00"}})
    assert result.still and not result.fixed
    assert list(kept.values()) == ["2026-11-02T15:00:00+00:00"]


def test_the_name_as_written_and_as_the_same_persons_are_both_looked_for():
    """Tom Skinner: his ended row found as "Tom Skinner", the live one written
    "Thomas Skinner" never looked for."""
    rows = [_hub(name="Tom Skinner", status="ended"), _hub(name="Thomas Skinner", status="live")]
    assert [one.name for one in rookqc.hub_named(rows, "Tom Skinner")] == ["Tom Skinner", "Thomas Skinner"]


def test_another_open_row_that_started_before_the_payment_is_another_order():
    """William McClure: his live Trucker row held up against his new Vets."""
    from datetime import date

    paid = datetime(2026, 10, 31, 23, 27, tzinfo=timezone.utc)
    trucker = _hub(lead_type="Text Verified Trucker IUL", status="live", ordered=20, delivered=7,
                   start_date=date(2026, 10, 20))
    result = rookqc.check(PAYRA, [("Rook", ROOK), ("Nova", NOVA)], hub=trucker, paid_at=paid)
    assert any("Nothing on the hub for this order yet" in one and "Trucker IUL" in one for one in result.problems)
    assert not any("lead type" in one for one in result.problems), result.problems


def test_a_quote_starts_at_the_words_not_the_tags():
    """William McClure's: "60184287276> @🌺 Faith 🌺 @Nicole I couldn't ..."."""
    said = ("<@1360184287276> @🌺 Faith 🌺 @Nicole Lots of words before this so the quote has to cut somewhere. "
            "I couldn't set up either Veteran order, so I've handed both to the team.")
    quoted = rookqc._quote(said, rookqc.CANT)
    assert quoted.startswith("I couldn't set up either Veteran order"), quoted


def test_rooks_launch_day_is_read_from_the_day_they_paid():
    """William McClure: Rook's "Monday", written the Saturday he paid (Oct 31),
    came out on his card as Monday Nov 9 - the Monday after the card was made."""
    paid = datetime(2026, 10, 31, 23, 27, tzinfo=timezone.utc)  # Sat 7:27 PM Eastern
    assert rookqc._launch_on("Monday", paid).startswith("Monday Nov 2 (Rook's setup said “Monday” on Sat Oct 31")
    assert rookqc._launch_on("today at 8 AM", paid).startswith("Saturday Oct 31")
    assert rookqc._launch_on("tomorrow", paid).startswith("Sunday Nov 1")
    assert rookqc._launch_on("Monday 11/9", paid) == "Monday 11/9"
    assert rookqc._launch_on("as soon as he signs", paid) == "as soon as he signs"
    rook = ROOK.replace("Launch Date: as soon as he signs", "Launch Date: Monday")
    result = rookqc.check(PAYRA, [("Rook", rook), ("Nova", NOVA)], hub=_hub())
    assert "Launch Date: Monday Nov 2" in rookqc.card_notes(result, paid_at=paid)


def test_rook_saying_its_right_as_it_is_oks_what_was_asked(monkeypatch):
    """Chris Amaya: "Expected — INV-21202 (paid by Chris Amaya) is Alain
    Ishimwe's order ... the hub uses his list ... No fix needed."."""
    from wilbyte.bot import client

    asked = ("@Rook @Nova QC on Jonathan Carter - paid $1,280.00\\n"
             "• The hub states don't match the payment - missing GA")
    answer = "Expected — INV-21202 is another agent's order, so the hub uses his list. No fix needed."
    talk = [("Rook", ROOK), ("Nova", NOVA), ("Ryte", asked), ("Rook", answer)]
    hub = _hub(states=("TX", "FL"))
    assert rookqc.check(PAYRA, talk, hub=hub).rook_ok.startswith("Expected")
    assert not rookqc.check(PAYRA, talk[:3] + [("Rook", "Forwarded to Nicole in his #rook-qc thread.")],
                            hub=hub).rook_ok
    post = SimpleNamespace(jump_url="https://discord.com/channels/1/2/900", content=PAYRA, embeds=[],
                           message_snapshots=[], created_at=datetime.now(timezone.utc) - timedelta(hours=2),
                           channel=SimpleNamespace(id=2), guild=None)

    async def checking(bot, key):
        return post, rookqc.check(PAYRA, talk, hub=hub)

    sent = []

    class Responder:
        async def send(self, text=None, **kw):
            sent.append(text)

    monkeypatch.setattr(client, "_sqc_check", checking)
    monkeypatch.setattr(client, "_order_responder", lambda bot: Responder())
    monkeypatch.setattr(client, "_card_ping", lambda bot: "")
    rookqc.remember("900", posted=post.created_at.isoformat(), due_done=True,
                    asked_problems=["The hub states don't match the payment - missing GA"])
    asyncio.run(client._sqc_run(SimpleNamespace(), "900", force=True))
    held = rookqc.recalled()["900"]
    assert held["ok_problems"] == ["The hub states don't match the payment - missing GA"]
    assert held["ok_by"].startswith("Rook – “Expected")
    assert "OK'd by Rook" in sent[-1] and "⚠" not in sent[-1].split("\\n")[0]


def test_the_question_shows_what_each_side_says():
    """Justin Techer's "has IN not ordered" was right, and nobody could tell
    without opening the invoice and the hub."""
    rook = ROOK.replace("States: TX, FL, GA", "States: TX, FL, GA, IN")
    result = rookqc.check(PAYRA, [("Rook", rook), ("Nova", NOVA)], hub=_hub(id=2070, states=("TX", "FL", "GA", "IN")))
    assert any("has IN not ordered" in one for one in result.problems + result.notes), "Rook's extra state is said"
    asked = rookqc.question(result, invoice="INV-21110")
    assert "The payment (INV-21110): 40 Fresh FEX" in asked, asked
    assert "Rook's setup: 40 Text-Verified Final Expense (fresh)" in asked
    assert "The hub: Jonathan Carter (row 2070)" in asked


def test_a_line_about_someone_else_in_novas_message_isnt_theirs():
    """David Jacob's card carried "Ryan's pause is the team's ..." and "Ryan
    Gsell was paused by the team"."""
    nova = (NOVA + "\nHanded to the team: Ryan Gsell was paused by the team (order filled) — their start is a "
            "person's call.\nHanded to the team: Ryan's pause is the team's, so only they can start him, and the "
            "second 15 leads still need adding.")
    result = rookqc.check(PAYRA, [("Rook", ROOK), ("Nova", nova)], hub=_hub())
    assert not any("Ryan" in one for one in result.todo + result.problems), (result.todo, result.problems)
    assert any("contract is signed" in one for one in result.todo)


def test_a_different_phone_is_a_note():
    """Corbin Reed: taken to Therese and Nicole for Rook's setup's phone."""
    rook = ROOK.replace("Phone: +15555550101", "Phone: +15555550199")
    result = rookqc.check(PAYRA, [("Rook", rook), ("Nova", NOVA)], hub=_hub())
    assert not any("phone" in one for one in result.problems)
    assert any("has the phone +15555550199" in one for one in result.notes)


def test_the_same_states_in_another_order_are_the_same_problem():
    """Connor Swartz: asked again at 2:08 - the hub listed his extra states in
    another order."""
    one = "The hub states don't match the payment - has MT, PA, SD, WI, WV not ordered"
    other = "The hub states don't match the payment - has SD, MT, WI, WV, PA not ordered"
    assert rookqc.among(other, [one])
    assert not rookqc.among("The hub states don't match the payment - has SD not ordered", [one])
    said = "Already with Nicole in Jonathan Carter's #rook-qc thread (states mismatch)."
    result = rookqc.check(PAYRA, [("Rook", ROOK), ("Nova", NOVA), ("Rook", said)], hub=_hub())
    assert result.with_team


def test_blue_collar_and_vet_widows():
    """Jake Shortino's Blue Collar, Alexander Salinas' "Fresh Vet Widows"."""
    assert rookqc.family("Text-Verified Blue Collar Leads") == rookqc.family("Text Verified Blue Collar IUL")
    assert rookqc.family("Fresh Vet Widows") == rookqc.family("Text Verified Widow Plus")
    assert rookqc.family("Text Verified Veteran Plus") != rookqc.family("Text Verified Widow Plus")


def test_their_own_setup_over_one_that_names_them_as_payer():
    """Colton Ramon's $1,407.60 came out as Keaton Artherton's, whose setup
    named Colton too."""
    post = PAYRA.replace("Jonathan Carter", "Colton Ramon").replace("jcarter@", "colton@")
    keaton = ROOK.replace("Jonathan Carter", "Keaton Artherton").replace("jcarter@", "keaton@").replace(
        "Leads Ordered: 40", "Leads Ordered: 20") + "\nSpecific Notes: same agency as Colton Ramon."
    colton = ROOK.replace("Jonathan Carter", "Colton Ramon").replace("jcarter@", "colton@")
    result = rookqc.check(post, [("Rook", colton), ("Rook", keaton), ("Nova", NOVA)], hub=_hub())
    assert result.client == "Colton Ramon"



def test_of_their_setups_the_one_for_what_this_payment_bought():
    """William McClure's old $1,140 Trucker payment was held to his Vets setup."""
    post = PAYRA.replace("Fresh FEX: 40", "Fresh Truckers: 40")
    vets = ROOK.replace("Text-Verified Final Expense (fresh)", "Text-Verified Veteran leads (fresh)")
    trucker = ROOK.replace("Text-Verified Final Expense (fresh)", "Text-Verified Trucker IUL leads (fresh)")
    result = rookqc.check(post, [("Rook", trucker), ("Rook", vets), ("Nova", NOVA)],
                          hub=_hub(lead_type="Text Verified Trucker IUL"))
    assert not any("lead type" in one for one in result.problems), result.problems


def test_a_setup_that_only_names_the_payer_isnt_theirs():
    """Colton Ramon's $1,407.60 for his own 40 came out as Keaton Artherton's 20
    - Keaton's setup named Colton, which isn't Colton paying for him."""
    post = PAYRA.replace("Jonathan Carter", "Colton Ramon").replace("jcarter@", "colton@")
    keaton = ROOK.replace("Jonathan Carter", "Keaton Artherton").replace("jcarter@", "keaton@").replace(
        "Leads Ordered: 40", "Leads Ordered: 20").replace("+15555550101", "+15555550177") + (
        "\nSpecific Notes: Everlife agent, same agency as Colton Ramon.")
    result = rookqc.check(post, [("Rook", keaton), ("Nova", NOVA)], hub=_hub(name="Colton Ramon"))
    assert result.client == "Colton Ramon" and not any("Keaton" in one for one in result.problems), result.problems
    paying = keaton.replace("same agency as Colton Ramon", "paid by Colton Ramon for him")
    assert rookqc.check(post, [("Rook", paying), ("Nova", NOVA)], hub=_hub()).client == "Keaton Artherton"


def test_scheduled_for_this_order_puts_what_was_handed_over_behind_it():
    """Alyssa Booska and Ian Gibson: scheduled as paid for, and tagged to the
    team for what Nova handed over before."""
    paid = datetime(2026, 11, 3, 17, 50, tzinfo=timezone.utc)
    nova = NOVA + "\nHanded to the team: They have more than one row on that lead type - a person picks which one."
    row = _hub(status="scheduled", updated_at=paid + timedelta(hours=1))
    result = rookqc.check(PAYRA, [("Rook", ROOK), ("Nova", nova)], hub=row, paid_at=paid)
    assert result.todo == [] and any("Handed to the team earlier" in one for one in result.notes)
    held = rookqc.check(PAYRA, [("Rook", ROOK), ("Nova", nova)], hub=_hub(status="unsigned",
                                                                         updated_at=paid + timedelta(hours=1)),
                        paid_at=paid)
    assert held.todo, "waiting on a signature or a hold: still the team's"


def test_a_payment_that_doesnt_say_what_for_isnt_guessed_a_new_order():
    """Brandon Nguyen: a $400 Zelle with no product, the day after his Vets
    order started - read as a new order nobody set up; it paid for that one."""
    from datetime import date

    zelle = "New Payment | Zelle 💰\nNAME: Jonathan Carter\nAMOUNT: $400.00\nPRODUCT: — (match to their order)"
    paid = datetime(2026, 11, 2, 13, 58, tzinfo=timezone.utc)
    row = _hub(status="ended", ordered=30, delivered=31, start_date=date(2026, 10, 27),
               lead_type="Text Verified Veteran Plus", updated_at=paid - timedelta(hours=5))
    result = rookqc.check(zelle, [("Rook", "Already with Faith (unmatched Zelle) for Jonathan Carter.")], hub=row,
                          hub_rows=[row], paid_at=paid)
    said = " ".join(result.problems)
    assert "Not sure which order this pays for" in said and "started Oct 27" in said, result.problems
    assert "Nothing on the hub for this order yet" not in said
    # Delivered to since he paid: that order was this payment's - done.
    after = _hub(status="ended", ordered=30, delivered=31, start_date=date(2026, 10, 27),
                 lead_type="Text Verified Veteran Plus", updated_at=paid + timedelta(days=1))
    paid_for = rookqc.check(zelle, [("Rook", "Already with Faith for Jonathan Carter.")], hub=after,
                            hub_rows=[after], paid_at=paid)
    assert not any("Not sure" in one or "Nothing on the hub" in one for one in paid_for.problems)
    # An invoice that says what it bought is still held to it.
    known = rookqc.check(PAYRA, [("Rook", ROOK), ("Nova", NOVA)], hub=row, hub_rows=[row], paid_at=paid)
    assert any("Nothing on the hub for this order yet" in one for one in known.problems)


def test_nova_on_someone_else_isnt_nova_saying_theirs_is_done():
    """Jay Trandahl's card quoted Nova on Jay Rodriguez: "Nova says it's done"."""
    post = PAYRA.replace("Jonathan Carter", "Jay Trandahl")
    rook = ROOK.replace("Jonathan Carter", "Jay Trandahl")
    other = ("**Jay Rodriguez** — what was said above in #rook-nova, kept here so it's one card: Therese asked to set "
             "Jay's new order up on Jay_Rodriguez. Done ✅ set up. Also mentions Jay Trandahl in passing.")
    result = rookqc.check(post, [("Rook", rook), ("Nova", other)], hub=None, hub_rows=[])
    assert not any("Nova says it's done" in one for one in result.problems), result.problems
    mine = rookqc.check(post, [("Rook", rook), ("Nova", "Got it ✅ I set up Jay Trandahl's order: 40 FEX")],
                        hub=None, hub_rows=[])
    assert any("Nova says it's done" in one for one in mine.problems)


def test_business_payer_first_word_is_nobody():
    from wilbyte import rookqc

    assert not rookqc._first_named("✅ the order for Kurtis Kunz is set up", "The Eissa Group Llc")
    assert rookqc._first_named("I set up Jonathan's order", "Jonathan Carter")


def test_rooks_aged_setup_is_not_a_setup():
    from wilbyte import rookqc

    aged = rookqc.Checked(setup_text="New aged setup:\nName: Christopher Gallion\nLead Type: Aged MTG 180+\n"
                                     "Number of Leads: 100")
    assert not aged.talk_about_setup()
    fresh = rookqc.Checked(setup_text="New fresh setup:\nName: Christopher Gallion\nLead Type: Vets\n"
                                      "Number of Leads: 25")
    assert fresh.talk_about_setup()


def test_okd_states_taken_out_of_a_bigger_problem():
    from wilbyte import rookqc

    okd = ["The hub's states don't match the payment - has CT not ordered"]
    left, done = rookqc.ok_left("The hub's states don't match the payment - has CT not ordered - missing GA", okd)
    assert left == "The hub's states don't match the payment - missing GA"
    assert done == "The hub's states don't match the payment - has CT not ordered"
    assert rookqc.ok_left("The hub's states don't match the payment - has CT not ordered", okd) == (
        None, "The hub's states don't match the payment - has CT not ordered")
    assert rookqc.ok_left("Not on the hub", okd) == ("Not on the hub", "")


def test_nova_saying_it_isnt_done_isnt_done():
    from wilbyte import rookqc

    assert rookqc._done_said("@Rook Got it ✅ I set up Kurtis's 25 Text-Verified Veteran leads in his 20 states.")
    assert rookqc._done_said("Done ✅ Jay's order is live now")
    assert rookqc._done_said("> 💰 Aged MTG 180+: 100 pulled (Connecticut 8)")
    assert not rookqc._done_said("@Rook Scout has never had a Widow order, so there's no last count, states or price "
                                 "to copy. Neither of his two orders is live. Once you send it I'll get him set up.")
    assert not rookqc._done_said("Is he set up already?")


def test_a_business_payer_isnt_any_message_with_the_and_llc():
    from wilbyte import rookqc

    eissa = rookqc.Order(name="The Eissa Group Llc")
    assert not rookqc.about("The $800 Zelle from Kunz Marketing, Llc was for Kurtis", eissa)
    assert rookqc.about("The Eissa Group Llc paid for Mohammad", eissa)


def test_an_aged_pull_on_the_same_invoice_isnt_the_setup_done():
    from wilbyte import rookqc

    said = "Pulled 100 for William McClure — sheet shared with ww@example.com."
    assert rookqc._done_said(said)
    assert not rookqc._done_said(said, pulls=False)


def test_a_payment_made_into_a_trello_card_isnt_asked_which_order(monkeypatch):
    """Robert Hollis: "Not sure which order this pays for" under "Its Trello
    card is made" - the team made it a new order."""
    import asyncio

    from wilbyte import ordercards
    from wilbyte.bot import client

    result = rookqc.Checked(client="Robert Hollis", problems=[
        "Not sure which order this pays for - the payment doesn't say, and their Text Verified Veteran Plus order "
        "started Oct 20 (done, 10 of 10 delivered). Payment for that one, or a new order not set up yet?"])
    post = SimpleNamespace(jump_url="https://discord.com/channels/1/2/902", content="AMOUNT: $340.00", embeds=[],
                           message_snapshots=[], created_at=None)

    async def checking(bot, key):
        return post, result

    seen = []

    async def card(bot, cards, key, post_, result_, held, **kw):
        seen.append(result_)
        return "ok"

    monkeypatch.setattr(client, "_sqc_check", checking)
    monkeypatch.setattr(ordercards, "load", lambda: {"902": {"status": "made", "url": "https://trello.com/c/TesCSkss"}})
    monkeypatch.setattr(client, "_sqc_card", card)

    async def channel(bot, kind):
        return object()

    monkeypatch.setattr(client, "_card_channel", channel)
    rookqc.remember("902", posted="2026-11-04T15:00:00+00:00")
    asyncio.run(client._sqc_run(SimpleNamespace(), "902", due=True))
    assert not any(one.startswith("Not sure which order") for one in result.problems)
    assert any("new order on Trello" in one for one in result.notes)


def test_a_new_agent_card_made_by_hand_answers_which_order(monkeypatch):
    """Franklin made "New Agent - Robert Hollis" by hand for the $245; Ryte
    still asked "Not sure which order this pays for"."""
    import asyncio
    import time as _time

    from wilbyte.bot import client

    posted = datetime(2026, 11, 4, 15, 0, tzinfo=timezone.utc)
    made = format(int(posted.timestamp()) + 600, "x") + "0" * 16
    monkeypatch.setattr(client, "_BOARD_CARDS", [_time.time(), [
        {"id": made, "name": "New Agent - Robert Hollis", "shortUrl": "https://trello.com/c/hand1"},
        {"id": format(int(posted.timestamp()) - 86400 * 20, "x") + "0" * 16, "name": "New Agent - Robert Hollis",
         "shortUrl": "https://trello.com/c/old1"}]])
    result = rookqc.Checked(client="Robert Hollis", problems=[
        "Not sure which order this pays for - the payment doesn't say, and their Text Verified Veteran Plus order "
        "started Oct 20 (done, 10 of 10 delivered). Payment for that one, or a new order not set up yet?",
        "Not on the hub"])
    post = SimpleNamespace(jump_url="https://discord.com/channels/1/2/903", content="AMOUNT: $245.00", embeds=[],
                           message_snapshots=[], created_at=posted)

    async def checking(bot, key):
        return post, result

    async def card(bot, cards, key, post_, result_, held, **kw):
        return "ok"

    async def channel(bot, kind):
        return object()

    monkeypatch.setattr(client, "_sqc_check", checking)
    monkeypatch.setattr(client, "_sqc_card", card)
    monkeypatch.setattr(client, "_card_channel", channel)
    rookqc.remember("903", posted=posted.isoformat())
    asyncio.run(client._sqc_run(SimpleNamespace(config=SimpleNamespace()), "903", due=True))
    assert result.trello == "https://trello.com/c/hand1"
    assert not result.problems
    from wilbyte import paycards

    standing, _title, body = paycards.setup_card(result)
    assert standing == "cant" and "With the team on Trello - not on the hub yet" in body
    assert rookqc.recalled()["903"]["trello"] == "https://trello.com/c/hand1"


def test_the_teams_answer_in_rook_qc_is_read():
    """Dane Orshoff: Rook took it to Nicole in #rook-qc, Nicole answered "All
    good. I've shared it with him" - and Ryte kept following up."""
    qc = rookqc.IN_ROOK_QC
    talk = [("Rook", ROOK),
            ("Rook" + qc, "@Nicole Dane Orshoff's 11 aged IUL are pulled but the sheet share failed - can you share "
                          "it with dane@example.com please."),
            ("𝓝𝓲𝓬𝓸𝓵𝓮" + qc, "All good. I've shared it with him using his email: dane@example.com. @Rook")]
    result = rookqc.check(PAYRA, talk)
    assert result.with_team and result.team_ok.endswith("“All good. I've shared it with him using his email: "
                                                         "dane@example.com. @Rook”")
    assert not rookqc.team_answered([("Nicole" + qc, "Can you send me his email?")])
    assert not rookqc.team_answered([("Nicole" + qc, "All good"), ("Rook" + qc, "it's still not on the hub")])
    assert not rookqc.team_answered([("Nicole" + qc, "not yet, waiting on Faith")])
    assert rookqc.team_answered([("Therese" + qc, "Done ✅ set up on the hub")])


def test_the_teams_answer_ends_rytes_follow_ups(monkeypatch):
    import asyncio

    from wilbyte import ledger
    from wilbyte.bot import client

    result = rookqc.Checked(client="Dane Orshoff", problems=["Not on the hub"], todo=["share the sheet"],
                            team_ok="Nicole: “All good. I've shared it with him”")
    post = SimpleNamespace(jump_url="https://discord.com/channels/1/2/904", content="AMOUNT: $121.50", embeds=[],
                           message_snapshots=[], created_at=None)

    async def checking(bot, key):
        return post, result

    seen = []

    async def card(bot, cards, key, post_, result_, held, **kw):
        seen.append(list(result_.problems))
        return "ok"

    async def channel(bot, kind):
        return object()

    monkeypatch.setattr(client, "_sqc_check", checking)
    monkeypatch.setattr(client, "_sqc_card", card)
    monkeypatch.setattr(client, "_card_channel", channel)
    now = datetime.now(timezone.utc)
    rookqc.remember("904", posted=(now - timedelta(hours=5)).isoformat())
    asyncio.run(client._sqc_run(SimpleNamespace(), "904", due=True))
    held = rookqc.recalled()["904"]
    assert seen == [[]] and rookqc.among("Not on the hub", held["ok_problems"])
    listed = ledger.daily(rookqc.recalled(), now=now, today=now.date(), through=now.date(), only=ledger.MISSED)
    assert "Dane Orshoff" not in listed


def test_veteran_final_expense_is_the_veteran_product():
    """Peter Kuyper: "the hub has the lead type “Text Verified Veteran Plus”,
    the payment “Veteran Final Expense”" - one product, asked as two."""
    assert rookqc.family("Veteran Final Expense") == rookqc.family("Text Verified Veteran Plus")
    assert rookqc.family("Veteran Widow FEX") == rookqc.family("Text Verified Veteran Widows")
    assert rookqc.family("Veteran Widows × 10 @ $25/lead") != rookqc.family("Text Verified Veteran Plus")
    assert rookqc.family("Text Verified Final Expense") != rookqc.family("Text Verified Veteran Plus")


def test_a_lead_type_question_carries_no_state_lists():
    hub = SimpleNamespace(name="Peter Kuyper", id="2226", lead_type="Text Verified Veteran Plus", status="scheduled",
                          ordered=31, delivered=0, states=("AK", "AL", "AR"))
    result = rookqc.Checked(client="Peter Kuyper", problems=["The hub has the lead type “X”, the payment “Y”"],
                            hub=hub, paid=rookqc.Order(name="Peter Kuyper", lead_type="Veteran Final Expense"))
    said = rookqc.evidence(result)
    assert "states" not in said and "31 ordered / 0 delivered" in said


def test_what_the_team_said_was_wrong_on_the_list():
    """Nov 7: a dozen "has X not ordered", Scout Allen's 24/23, Thomas
    Poulin's $90, Connor Swartz's 4 add-on leads, Perry Sandlin's
    support@agentleadlab.com, Evan Mori's two-product setup - "these are
    wrong"."""
    extra = rookqc.check(PAYRA, [("Rook", ROOK), ("Nova", NOVA)], hub=_hub(states=("TX", "FL", "GA", "MI", "VA")))
    assert not any("states" in one for one in extra.problems)
    short = rookqc.check(PAYRA, [("Rook", ROOK), ("Nova", NOVA)], hub=_hub(states=("TX", "FL")))
    assert any("missing GA" in one for one in short.problems), "a state left off is still said"
    one_off = rookqc.check(PAYRA, [("Rook", ROOK), ("Nova", NOVA)], hub=_hub(delivered=24), sheet_rows=23)
    assert not any("sheet" in one for one in one_off.problems)
    many_off = rookqc.check(PAYRA, [("Rook", ROOK), ("Nova", NOVA)], hub=_hub(delivered=24), sheet_rows=20)
    assert any("sheet has 20" in one for one in many_off.problems)
    small = rookqc.check(PAYRA.replace("$1,280.00", "$90.00"), [])
    assert not small.problems and any("small payment" in one for one in small.notes)
    stand_in = rookqc.check(PAYRA, [("Rook", ROOK.replace("jcarter@example.com", "support@agentleadlab.com")),
                                    ("Nova", NOVA)], hub=_hub())
    assert not any("email" in one for one in stand_in.problems)


def test_are_these_people_set_up():
    """"are these people setup?" and six names with what they bought -
    answered "every agent going live today or tomorrow is set up"."""
    from wilbyte.bot import mentions

    said = ("<@1> are these people setup?\n1\tElvis Farina\tWEEKEND20\t$1,863.00\t50 fresh vets (FL, PA)\n"
            "3 Jane Torres WEEKEND20 $890.10 25 OTP mortgage protection\n"
            "4\tCavell (CJ) Stubbs Jr\tWEEKEND20\t$400.00 (Zelle)\t20 fresh widows\n"
            "6\tEvan Mori\tWEEKEND5\t$937.71\t10 fresh vets + 10 fresh IUL Plus + 10 overflow blue collar\n"
            "7\tNobody Here\tWEEKEND5\t$100.00\t5 fresh vets")
    assert mentions.parse(said).action == "setupask"
    asked = rookqc.asked_orders(said)
    assert asked[1] == ("Jane Torres", "25 OTP mortgage protection") and asked[2][0] == "Cavell Stubbs Jr"
    rows = [_hub(name="Elvis Farina", lead_type="Text Verified Veteran Plus", ordered=50, states=("FL", "PA")),
            _hub(name="Jane Torres", lead_type="Text Verified Mortgage Protection Plus", ordered=20),
            _hub(name="Cavell Stubbs", lead_type="Text Verified Veteran Widows", ordered=20),
            _hub(name="Evan Mori", lead_type="Text Verified Veteran Plus", ordered=10),
            _hub(name="Evan Mori", lead_type="Text Verified IUL Plus", ordered=10)]
    said_back = [rookqc.setup_answer(name, bought, rows) for name, bought in asked]
    assert said_back[0].startswith("✅ **Elvis Farina**")
    assert said_back[1].startswith("⚠️ **Jane Torres**") and "20 ordered on the hub, bought 25" in said_back[1]
    assert said_back[2].startswith("✅ **Cavell Stubbs Jr**")
    assert said_back[3].startswith("✅ **Evan Mori**") and "overflow's the team's" in said_back[3]
    assert said_back[4].startswith("❌ **Nobody Here** — not on the hub")


def test_a_name_in_brackets_is_another_it_goes_by():
    """"JT (James) Wideman" - "I looked for JT (James) Wideman and found
    nobody"; "Cavell (CJ) Stubbs Jr"; Chaz for Charles."""
    assert rookqc.name_aliases("JT (James) Wideman") == ["JT (James) Wideman", "JT Wideman", "James Wideman"]
    assert "CJ Stubbs" in rookqc.name_aliases("Cavell (CJ) Stubbs Jr")
    assert "Hussein Suleiman" in rookqc.name_aliases("Shane Suleiman (also goes by Hussein Suleiman)")
    rows = [SimpleNamespace(name="James Wideman"), SimpleNamespace(name="Cavell Stubbs"),
            SimpleNamespace(name="Somebody Else")]
    assert [one.name for one in rookqc.hub_named(rows, "JT (James) Wideman")] == ["James Wideman"]
    assert [one.name for one in rookqc.hub_named(rows, "Cavell (CJ) Stubbs Jr")] == ["Cavell Stubbs"]
    assert rookqc.same_person_strict("JT (James) Wideman", "James Wideman")
    assert rookqc.same_person_strict("chaz loschiavo", "Charles loschiavo")
    assert not rookqc.same_person_strict("JT (James) Wideman", "James Wyman")
