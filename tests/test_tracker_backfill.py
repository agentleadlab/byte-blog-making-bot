""""can ryte backfill the chargback to add on the tracker? i havent added them in a while"."""

import asyncio
from datetime import date

from wilbyte import rebuttal
from wilbyte.bot import client, jobs

NOTICE = ("Dispute Date: {dd}\nDispute Dollar Amount: {amount}\nAcquirer's Reference Number: {arn}\n"
          "Card Number: ending in 9394\nTransaction Date: {td}\nCustomer Name: {name}\nCustomer Email: x@example.com")

HEAD = ["Name of Disputer", "Date of Transaction", "Amount", "Closer", "Status"]
TABS = {
    "Oct 2026": [HEAD, ["Gavin Mathieu", "9/1/2026", "$507.15", "Tre", "Win"]],
    "Nov 2026": [HEAD],
}


def _notices():
    return [
        (NOTICE.format(dd="10/2/2026", amount="$507.15", arn="1", td="9/1/2026", name="Gavin Mathieu"), date(2026, 10, 2)),
        (NOTICE.format(dd="10/9/2026", amount="$724.50", arn="2", td="9/20/2026", name="Gavin Mathieu"), date(2026, 10, 9)),
        (NOTICE.format(dd="11/3/2026", amount="$129.37", arn="3", td="10/1/2026", name="Juliana Hernandez"), date(2026, 11, 3)),
        # The same alert twice.
        (NOTICE.format(dd="11/3/2026", amount="$129.37", arn="3", td="10/1/2026", name="Juliana Hernandez"), date(2026, 11, 3)),
        (NOTICE.format(dd="8/3/2026", amount="$99.00", arn="4", td="7/1/2026", name="Old Customer"), date(2026, 8, 3)),
        ("somebody chatting in the channel", date(2026, 11, 4)),
    ]


def test_only_the_ones_not_on_any_tab():
    got = rebuttal.notices_untracked(_notices(), TABS)
    assert [(one.customer_name, one.amount) for one, _ in got] == [
        ("Old Customer", "$99.00"), ("Gavin Mathieu", "$724.50"), ("Juliana Hernandez", "$129.37")]


class _Responder:
    requester_id = 1

    def __init__(self):
        self.sent = []

    async def send(self, text=None, view=None, **kw):
        self.sent.append(text or "")
        if view is not None:
            view.confirmed = True


def test_backfill_shows_then_writes_in_each_month(monkeypatch):
    class Message:
        def __init__(self, text, day):
            self.text, self.day = text, day
            from datetime import datetime, timezone
            self.created_at = datetime(day.year, day.month, day.day, 15, tzinfo=timezone.utc)

    class Channel:
        def history(self, **kw):
            async def gen():
                for text, day in _notices():
                    yield Message(text, day)
            return gen()

    class Bot:
        def get_channel(self, _id):
            return Channel()

    class View:
        def __init__(self, **kw):
            self.confirmed = False

        async def wait(self):
            return None

    class Secrets:
        discord_dispute_channel_id = "123"

    class Schedule:
        timezone = "America/Chicago"

    class Discord:
        approval_timeout_seconds = 60

    class Config:
        secrets, schedule, discord = Secrets(), Schedule(), Discord()

    monkeypatch.setattr(client, "_all_text", lambda message: message.text)
    monkeypatch.setattr(client.views, "ConfirmView", View)
    monkeypatch.setattr(jobs, "tracker_everything", lambda config: (TABS, []))
    written = []
    monkeypatch.setattr(jobs, "track_chargeback", lambda config, tab, row: (written.append((tab, row)) or f"{tab}, row 9", []))
    responder = _Responder()
    asyncio.run(client._backfill_tracker(Bot(), responder, Config()))
    said = "\n".join(responder.sent)
    assert "2 dispute(s) aren't on the tracker yet" in said
    assert "Old Customer" in said and "no tab for Aug 2026" in said
    assert [(tab, row[0], row[2], row[4]) for tab, row in written] == [
        ("Oct 2026", "Gavin Mathieu", "$724.50", "Pending"),
        ("Nov 2026", "Juliana Hernandez", "$129.37", "Pending")]
    assert "Added 2 row(s)" in responder.sent[-1]
