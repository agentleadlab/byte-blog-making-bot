""""i want ryte to be able to do what i did" - the chargeback-rebuttal steps."""

import asyncio

from wilbyte import rebuttal, sheetcolors
from wilbyte.bot import client

GREEN, PURPLE, YELLOW, RED, WHITE = (0.71, 0.88, 0.8), (0.85, 0.82, 0.91), (1.0, 0.95, 0.6), (0.96, 0.8, 0.8), (1, 1, 1)


def _sheet():
    """Rows 1-2 title, 3-7 the legend, 8 headers, then leads coloured in A."""
    rows = [[("Bryce Evans - Text Verified Veteran Plus Leads", WHITE)], [("", None)]]
    for colour, words in ((GREEN, "Green = Sold"), (PURPLE, "Purple = disconnected/wrong number"),
                          ((0.8, 0.85, 0.95), "Blue = call blocker"), (YELLOW, "Yellow = booked appointment"),
                          (RED, "Red = not interested")):
        rows.append([(words, colour)])
    rows.append([("First Name", WHITE), ("Last Name", WHITE), ("Phone", WHITE)])
    fills = [GREEN, WHITE, YELLOW, GREEN, RED, (0.70, 0.87, 0.79), WHITE, PURPLE]
    for at, fill in enumerate(fills):
        rows.append([(f"Lead{at}", fill), ("Doe", WHITE), (f"555-010{at}", WHITE)])
    return rows


def test_sold_rows_counted_off_the_legend():
    got = sheetcolors.count(_sheet())
    assert got["legend"] and got["leads"] == 8
    assert got["counts"]["sold"] == 3 and got["counts"]["appointment"] == 1
    assert got["rows"]["sold"] == [9, 12, 14]
    line = sheetcolors.said(got)
    assert "3 Sold (rows 9, 12, 14)" in line and "1 appointment booked" in line and "out of 8 leads" in line


def test_no_legend_falls_back_to_green_and_yellow():
    rows = [[("Name", WHITE), ("Phone", WHITE)]] + [[(f"L{at}", fill), ("555-0100", WHITE)]
                                                     for at, fill in enumerate([GREEN, YELLOW, WHITE])]
    got = sheetcolors.count(rows)
    assert got["counts"] == {"sold": 1, "appointment": 1} and not got["legend"]


def test_nothing_marked_says_nothing():
    rows = [[("Name", WHITE)], [("L1", WHITE), ("555-0100", WHITE)]]
    assert sheetcolors.said(sheetcolors.count(rows)) == ""


def test_the_two_elevate_codes():
    assert rebuttal.code_in("Reason Code: 4553") == "4553"
    assert rebuttal.code_in("code 8002") == "8002"
    assert rebuttal.code_in("Quality of Goods and Services") == "4553"
    # An amount ending in the digits of a code is not the code.
    assert rebuttal.code_in("Dispute Amount: $ 1,245.53") != "4553"


NOTICE = ("MID: 510200014664\nDBA Name: AGENT LEAD LAB\nDispute Date: 11/2/2026\nDispute Type: Debited\n"
          "Dispute Dollar Amount: {amount}\nAcquirer's Reference Number: {arn}\nCard Number: ending in 9394\n"
          "Transaction Date: {date}\nCustomer Name: Gavin Mathieu\nCustomer Email: gavin@example.com")


def test_other_disputes_from_the_same_cardholder():
    this = rebuttal.read_facts(NOTICE.format(amount="$507.15", arn="111", date="9/1/2026"))
    notices = [
        NOTICE.format(amount="$507.15", arn="111", date="9/1/2026"),          # this one
        NOTICE.format(amount="$724.50", arn="222", date="9/20/2026") + "\nReason Code: 8002",
        "⚖️ **Chargeback**\n• **Customer** — Gavin Mathieu\n• **Amount** — $724.50\n• **Transaction** — 9/20/2026",
        NOTICE.format(amount="$99.00", arn="333", date="9/3/2026").replace("Gavin Mathieu", "Someone Else")
              .replace("gavin@", "else@"),
    ]
    others = rebuttal.other_disputes(this, notices)
    assert [one.arn for one in others] == ["222"]
    said = rebuttal.pattern_said(others)
    assert "$724.50" in said and "8002" in said and "September 20, 2026" in said


def test_checked_against_the_banks_email():
    alert = rebuttal.read_facts("Customer Name: Gavin Mathieu\nDispute Amount: $507.15\nCard: 9394\n"
                                "Transaction Date: 9/1/2026")
    bank = NOTICE.format(amount="$507.15", arn="111", date="9/1/2026")
    assert rebuttal.against_bank(alert, bank) == []
    assert alert.mid == "510200014664" and alert.arn == "111" and alert.dispute_type == "Debited"

    wrong = rebuttal.read_facts("Customer Name: Gavin Mathieu\nDispute Amount: $724.50\nCard: 9394\n"
                                "Transaction Date: 9/1/2026")
    said = rebuttal.against_bank(wrong, bank)
    assert len(said) == 1 and said[0].startswith("amount")


class _Channel:
    def __init__(self):
        self.sent = []

    async def send(self, text=None, **kw):
        self.sent.append(text or "")


def test_an_alert_builds_its_rebuttal_once(monkeypatch):
    monkeypatch.setattr(client, "CHARGEBACK_AUTO", True)
    channel = _Channel()
    monkeypatch.setattr(client, "_chargeback_responder", lambda bot: channel)
    built = []

    async def fake_rebuttal(responder, config, message, said, *, bot=None):
        built.append(said)

    monkeypatch.setattr(client, "_rebuttal", fake_rebuttal)

    class Message:
        content = "@here"
        embeds = []
        jump_url = "https://discord.com/channels/1/2/3"
        attachments = []

    text = NOTICE.format(amount="$507.15", arn="111", date="9/1/2026")
    monkeypatch.setattr(client, "_all_text", lambda message: text)

    class Bot:
        config = None

    asyncio.run(client.handle_dispute(Bot(), Message()))
    asyncio.run(client.handle_dispute(Bot(), Message()))
    assert len(built) == 1
    assert any("No reason code" in one for one in channel.sent)
    assert any("Building the rebuttal now" in one for one in channel.sent)
