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

    async def link(responder, config, said, *, chosen="", message=None):
        made.append((said, chosen))

    monkeypatch.setattr(client, "_payment_link", link)
    message = SimpleNamespace(channel=SimpleNamespace(id=5), author=SimpleNamespace(id=9),
                              content="<@1> text verified aged veteran leads")
    client._PAYLINK_WAITING[(5, 9)] = ("$350 for 50 Aged Vets", NAMES, time.monotonic())
    assert asyncio.run(client._paylink_answer(None, None, message)) is True
    assert made == [("$350 for 50 Aged Vets", NAMES[0])]
    assert (5, 9) not in client._PAYLINK_WAITING
    # Nothing waiting: not taken.
    assert asyncio.run(client._paylink_answer(None, None, message)) is False
