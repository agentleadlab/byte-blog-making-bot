"""Paid but not set up - "money taken with nothing delivered is how a chargeback starts"."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from wilbyte import paidsetup
from wilbyte.bot import jobs

NOW = datetime(2026, 10, 5, 18, 0, tzinfo=timezone.utc)


def _data(*invoices):
    """RYTE's copy of Payra: (invoice id, name, email, phone, paid at, status, refund)."""
    data = {"invoices": {}, "payments": {}}
    for n, (iid, name, email, phone, paid_at, status, refund) in enumerate(invoices):
        data["invoices"][iid] = {
            "id": iid, "number": f"INV-{n}", "name": name, "email": email, "phone": phone,
            "active": True, "lines": [{"name": "25 OTP Vets", "quantity": 1, "price": 1360}],
        }
        data["payments"][f"p{n}"] = {
            "id": f"p{n}", "amount": 1360, "paid_on": paid_at.isoformat(), "status": status,
            "refund": refund, "invoice_ids": [iid],
        }
    return data


def _paid(name="Jay Rodriguez", email="jay@example.com", phone="5550100001", ago=timedelta(days=1)):
    return paidsetup.Paid(invoice_id="i1", number="INV-1", name=name, email=email, phone=phone,
                          amount="$1,360.00", paid_at=NOW - ago, what="25 OTP Vets × 1 @ $1,360.00")


def test_only_payments_that_went_through_between_two_weeks_and_four_hours_ago():
    data = _data(
        ("i1", "Jay Rodriguez", "jay@example.com", "5550100001", NOW - timedelta(days=1), "succeeded", False),
        ("i2", "Too Soon", "", "", NOW - timedelta(hours=2), "succeeded", False),
        ("i3", "Long Ago", "", "", NOW - timedelta(days=20), "succeeded", False),
        ("i4", "Declined Card", "", "", NOW - timedelta(days=1), "declined", False),
        ("i5", "Got A Refund", "", "", NOW - timedelta(days=1), "succeeded", True),
    )
    found = paidsetup.recent_payments(data, now=NOW)
    assert [one.name for one in found] == ["Jay Rodriguez"]
    assert found[0].amount == "$1,360.00" and "25 OTP Vets" in found[0].what


def _card(name, desc="", made=NOW - timedelta(days=1), tail="0" * 16, title="New Agent - "):
    return {"id": f"{int(made.timestamp()):08x}" + tail, "name": f"{title}{name}", "desc": desc,
            "shortUrl": f"https://trello.com/c/{tail[:4]}"}


def test_their_card_is_found_by_email_phone_or_whole_name_never_part_of_one():
    paid = _paid()
    assert paidsetup.their_cards(paid, [_card("J. Rodriguez", "Email: JAY@example.com")])
    assert paidsetup.their_cards(paid, [_card("Jay R", "Phone: (555) 010-0001")])
    assert paidsetup.their_cards(paid, [_card("Jay Rodriguez")])
    assert not paidsetup.their_cards(paid, [_card("Jay Rodriguez Jr")])
    assert not paidsetup.their_cards(paid, [_card("Maria Rodriguez")])


def test_a_card_from_a_much_earlier_order_isnt_this_payments():
    paid = _paid()
    old = _card("Jay Rodriguez", made=NOW - timedelta(days=90))
    assert paidsetup.own_card(paid, [old]) is None
    new = _card("Jay Rodriguez", made=NOW - timedelta(days=3), tail="1" * 16)
    assert paidsetup.own_card(paid, [old, new]) is new


class Board:
    def __init__(self, cards, comments=None):
        self.cards, self.comments = cards, comments or {}

    def board_cards(self, board_id, archived=False):
        return self.cards

    def card_comments(self, card_id):
        return self.comments.get(card_id, [])

    def close(self):
        pass


def _check(monkeypatch, data, cards, comments=None):
    from wilbyte import payraapi

    monkeypatch.setattr(payraapi, "load", lambda *a, **k: data)
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: Board(cards, comments))
    config = SimpleNamespace(secrets=SimpleNamespace(trello_board_id="b"))
    return jobs.paid_not_set_up(config, now=NOW)


def _paid_data(**people):
    return _data(*[(f"i{name[:3]}", name, email, "", NOW - timedelta(days=1), "succeeded", False)
                   for name, email in people.items()])


def test_paid_with_no_card_and_paid_with_a_card_with_no_launch_date(monkeypatch):
    data = _paid_data(**{"Jay Rodriguez": "jay@example.com", "Ana Ruiz": "ana@example.com",
                         "Bo Diaz": "bo@example.com", "Cy Lee": "cy@example.com"})
    cards = [
        _card("Ana Ruiz", "25 OTP Vets", tail="1" * 16),                                  # no launch
        _card("Bo Diaz", "25 OTP Vets\nLaunch Date: Friday, October 9", tail="2" * 16),   # fine
        _card("Cy Lee", "25 OTP Vets", tail="3" * 16),                                    # launch in a comment
    ]
    found, problems = _check(monkeypatch, data, cards, {cards[2]["id"]: ["Launch Date: Friday, October 9"]})

    assert problems == []
    assert sorted((one.paid.name, one.why) for one in found) == [
        ("Ana Ruiz", "no launch date"), ("Jay Rodriguez", "no card")]


def test_a_plan_renewing_on_an_old_card_is_not_flagged(monkeypatch):
    data = _paid_data(**{"Jay Rodriguez": "jay@example.com"})
    old = _card("Jay Rodriguez", "uprise vets $350/week", made=NOW - timedelta(days=120))
    found, _problems = _check(monkeypatch, data, [old])
    assert found == []


def test_no_payra_copy_says_so(monkeypatch):
    found, problems = _check(monkeypatch, {}, [])
    assert found == [] and "PAYRA_API_TOKEN" in problems[0]


def test_describe_and_said_once():
    one = paidsetup.Unset(paid=_paid(), why="no card")
    line = paidsetup.describe(one)
    assert "**Jay Rodriguez** — paid $1,360.00 on Oct 4" in line and "no card on the board" in line
    other = paidsetup.Unset(paid=_paid(), why="no launch date", card_url="https://trello.com/c/x")
    assert "card has no launch date" in paidsetup.describe(other)

    paidsetup.remember([paidsetup.key(one)])
    assert paidsetup.key(one) in paidsetup.said() and paidsetup.key(other) not in paidsetup.said()


def test_the_command_is_heard_and_answers(monkeypatch):
    import asyncio

    from wilbyte.bot import client, mentions

    for text in ("<@1> not set up", "<@1> paid not set up?", "<@1> payra setup"):
        assert mentions.parse(text).action == "notsetup"

    monkeypatch.setattr(jobs, "paid_not_set_up", lambda config: (
        [paidsetup.Unset(paid=_paid(), why="no card")], []))
    said = []

    class Heard:
        async def send(self, text=None, **kw):
            said.append(text)

    asyncio.run(client._not_set_up(Heard(), None))
    assert "Paid, but not set up" in said[0] and "Jay Rodriguez" in said[0]
