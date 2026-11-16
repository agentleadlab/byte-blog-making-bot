""""@Ryte text verified aged veteran leads" - the answer to Ryte's "Which one?"
for a payment link, read as a new ask for SMS copy."""

import asyncio
import time
from types import SimpleNamespace

from wilbyte.bot import client

NAMES = ["Text-Verified Aged Veteran Leads", "Text-Verified Veteran Leads"]


def test_the_answer_picks_the_package():
    assert client._paylink_pick("<@1> text verified aged veteran leads", NAMES) == NAMES[0]
    assert client._paylink_pick("<@1> text-verified veteran leads", NAMES) == NAMES[1]
    assert client._paylink_pick("<@1> 2", NAMES) == NAMES[1]
    assert client._paylink_pick("<@1> the aged one", NAMES) == NAMES[0]
    assert client._paylink_pick("<@1> veteran", NAMES) == NAMES[1]
    assert client._paylink_pick("<@1> write me an email", NAMES) == ""


def test_a_waiting_question_is_answered_with_the_link(monkeypatch):
    made = []

    async def link(responder, config, said, *, chosen="", message=None, amount=None):
        made.append((said, chosen))

    monkeypatch.setattr(client, "_payment_link", link)
    message = SimpleNamespace(channel=SimpleNamespace(id=5), author=SimpleNamespace(id=9),
                              content="<@1> text verified aged veteran leads")
    client._PAYLINK_WAITING[(5, 9)] = {"said": "$350 for 50 Aged Vets", "kind": "which", "names": NAMES,
                                       "when": time.monotonic()}
    assert asyncio.run(client._paylink_answer(None, None, message)) is True
    assert made == [("$350 for 50 Aged Vets", NAMES[0])]
    assert (5, 9) not in client._PAYLINK_WAITING
    # Nothing waiting: not taken.
    assert asyncio.run(client._paylink_answer(None, None, message)) is False



def test_how_much_is_answered_by_the_next_tag(monkeypatch):
    # Tre: "i need a stripe link for 50 text verified veteran leads for
    # 36/lead with 20% OFF" -> "How much?" -> "1800 for 50 text verified
    # veteran leads" was written up as SMS copy, and "$1800" got a chat reply.
    made = []

    async def link(responder, config, said, *, chosen="", message=None, amount=None):
        made.append((said, amount))

    monkeypatch.setattr(client, "_payment_link", link)
    for answer, want in (("<@1> $1800", 1800.0), ("<@1> 1800 for 50 text verified veteran leads", 1800.0),
                         ("<@1> 1800", 1800.0)):
        client._PAYLINK_WAITING[(5, 9)] = {"said": "stripe link for 50 vets", "kind": "amount", "amounts": [],
                                           "when": time.monotonic()}
        message = SimpleNamespace(channel=SimpleNamespace(id=5), author=SimpleNamespace(id=9), content=answer)
        assert asyncio.run(client._paylink_answer(None, None, message)) is True
        assert made[-1] == ("stripe link for 50 vets", want)


def test_the_discount_question_takes_the_option_number(monkeypatch):
    made = []

    async def link(responder, config, said, *, chosen="", message=None, amount=None):
        made.append(amount)

    monkeypatch.setattr(client, "_payment_link", link)
    client._PAYLINK_WAITING[(5, 9)] = {"said": "x", "kind": "amount", "amounts": [1800.0, 1440.0],
                                       "when": time.monotonic()}
    message = SimpleNamespace(channel=SimpleNamespace(id=5), author=SimpleNamespace(id=9), content="<@1> 1")
    assert asyncio.run(client._paylink_answer(None, None, message)) is True
    assert made == [1800.0]


def test_the_amounts_read_without_a_dollar_sign():
    from wilbyte import products

    tre = "i need a stripe link for 50 text verified veteran leads for 36/lead with 20% OFF discount"
    assert [one for one, _why in products.amount_choices(tre)] == [1800.0, 1440.0]
    assert products.amount_asked("i need a stripe link for 1800 for 50 text verified veteran leads") == 1800.0
    assert products.how_many("i need a stripe link for 1800 for 50 text verified veteran leads") == 50
    assert products.amount_choices("15 leads at $43 each, with a 10% discount. Total: $580.50") == []
