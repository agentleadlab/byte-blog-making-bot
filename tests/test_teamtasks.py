"""What one teammate asks another to do, on the Ryte Checklist card - ticked
when the one asked reacts or says it's done."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from wilbyte import teamtasks
from wilbyte.bot import client, jobs, mentions

NOW = datetime(2026, 10, 13, 19, 34, tzinfo=timezone.utc)
FAITH, NICOLE, ELISA, RYTE = 11, 22, 33, 99


@pytest.mark.parametrize("said, done", [
    ("done", True), ("Done ✅", True), ("all set", True), ("capped him at 10", True), ("updated the cap", True),
    ("✅", True), ("will do", False), ("not done yet", False), ("I'll set it up later", False),
    ("on it", False), ("got it", False), ("what cap?", False), ("10/day max", False),
])
def test_what_counts_as_done(said, done):
    assert teamtasks.said_done(said) is done


def test_only_the_one_asked_closes_it_or_anybody_but_the_asker_when_nobody_was():
    task = {"asker_id": str(FAITH), "assignees": [{"id": str(NICOLE), "name": "Nicole"}]}
    assert teamtasks.closes_it(task, NICOLE) and not teamtasks.closes_it(task, ELISA)
    assert not teamtasks.closes_it(task, FAITH)
    nobody = {"asker_id": str(FAITH), "assignees": []}
    assert teamtasks.closes_it(nobody, ELISA) and not teamtasks.closes_it(nobody, FAITH)


def test_the_item_says_the_job_who_and_who_asked():
    task = {"title": "Michael Greff - set a 10/day cap", "assignees": [{"id": "1", "name": "Nicole"}],
            "asker": "Faith", "made": "2026-10-13T19:34+00:00"}
    assert teamtasks.item_name(task) == "Michael Greff - set a 10/day cap · Nicole (asked by Faith 10/13)"
    task.update(done_by="Nicole", done_at="2026-10-13T20:05+00:00")
    assert teamtasks.done_name(task).endswith("— done by Nicole 10/13 8:05pm")


def test_overdue_and_tidy():
    held = {
        "new": {"made": (NOW - timedelta(hours=1)).isoformat()},
        "old": {"made": (NOW - timedelta(hours=5)).isoformat()},
        "done": {"made": (NOW - timedelta(days=40)).isoformat(), "done_at": (NOW - timedelta(days=35)).isoformat()},
        "lately": {"made": (NOW - timedelta(days=3)).isoformat(), "done_at": (NOW - timedelta(days=2)).isoformat()},
    }
    assert [one["made"] for one in teamtasks.overdue(held, NOW)] == [held["old"]["made"]]
    assert set(teamtasks.tidy(held, NOW)) == {"new", "old", "lately"}


# ------------------------------------------------------------- in Discord

class Person(SimpleNamespace):
    pass


def person(i, name, bot=False):
    return Person(id=i, display_name=name, name=name.lower(), bot=bot)


class Channel(SimpleNamespace):
    pass


THREAD = Channel(id=500, name="Lead Schedule", parent=SimpleNamespace(name="internal-team"))
ROOM = Channel(id=600, name="internal-team", parent=None)


def message(i, author, text, *, channel=THREAD, mentions=(), reply_to=None, snapshots=(), when=NOW):
    return SimpleNamespace(
        id=i, author=author, content=text, channel=channel, mentions=list(mentions), embeds=[],
        attachments=[], message_snapshots=list(snapshots), created_at=when,
        reference=SimpleNamespace(message_id=reply_to) if reply_to else None,
        jump_url=f"https://discord.com/channels/1/{channel.id}/{i}", guild=SimpleNamespace(id=1),
    )


@pytest.fixture
def card(monkeypatch):
    """The card's General Tasks, and what Claude said each message was."""
    held = SimpleNamespace(added=[], ticked=[], reads=[], task=True)

    def adding(config, task):
        held.added.append(teamtasks.item_name(task))
        return f"item{len(held.added)}"

    def reading(config, **kw):
        held.reads.append(kw)
        return {"task": held.task or kw.get("forced", False), "title": "Michael Greff - set a 10/day cap"}

    monkeypatch.setattr(jobs, "team_task_add", adding)
    monkeypatch.setattr(jobs, "team_task_done", lambda config, task: held.ticked.append(teamtasks.done_name(task)))
    monkeypatch.setattr(jobs, "read_team_request", reading)
    return held


BOT = SimpleNamespace(config=SimpleNamespace(), user=person(RYTE, "RYTE", bot=True))
FAITH_P, NICOLE_P, ELISA_P = person(FAITH, "Faith"), person(NICOLE, "Nicole"), person(ELISA, "Elisa")
FORWARD = SimpleNamespace(content="Can we slow down the leads a bit ... 10/day max", embeds=[], attachments=[
    SimpleNamespace(url="https://cdn.discordapp.com/shot.png", content_type="image/png")])


def faith_asks(i=1, channel=THREAD):
    return message(i, FAITH_P, f"<@{NICOLE}> greff", channel=channel, mentions=[NICOLE_P], snapshots=[FORWARD])


def test_faiths_request_goes_on_the_card_with_the_forward_read(card):
    asyncio.run(client._team_message(BOT, faith_asks()))
    assert card.added == ["Michael Greff - set a 10/day cap · Nicole (asked by Faith 10/13)"]
    [read] = card.reads
    assert "(forwarded) Can we slow down" in read["text"] and read["images"] == ["https://cdn.discordapp.com/shot.png"]
    assert read["asked"] == ["Nicole"] and "Lead Schedule" in read["where"]
    asyncio.run(client._team_message(BOT, faith_asks()))
    assert len(card.added) == 1, "the same message went on the card twice"


def test_chat_that_isnt_a_job_stays_off_the_card(card):
    card.task = False
    asyncio.run(client._team_message(BOT, message(2, ELISA_P, f"<@{NICOLE}> thanks!!", mentions=[NICOLE_P])))
    assert card.added == []


def test_a_reply_ping_or_a_bot_or_ryte_isnt_somebody_asked(card):
    # Discord puts the replied-to person in `mentions` with no tag typed.
    asyncio.run(client._team_message(BOT, message(3, FAITH_P, "please cap him", mentions=[NICOLE_P])))
    asyncio.run(client._team_message(BOT, message(4, FAITH_P, f"<@{RYTE}> cap him", mentions=[BOT.user])))
    assert card.added == [] and card.reads == []


def test_nicoles_done_in_the_thread_ticks_it_and_nobody_elses_does(card):
    asyncio.run(client._team_message(BOT, faith_asks()))
    asyncio.run(client._team_message(BOT, message(5, ELISA_P, "done")))
    asyncio.run(client._team_message(BOT, message(6, NICOLE_P, "will do")))
    assert card.ticked == []
    asyncio.run(client._team_message(BOT, message(7, NICOLE_P, "done, capped at 10", when=NOW + timedelta(minutes=31))))
    assert card.ticked == ["Michael Greff - set a 10/day cap · Nicole (asked by Faith 10/13) — done by Nicole 10/13 8:05pm"]
    asyncio.run(client._team_message(BOT, message(8, NICOLE_P, "done")))
    assert len(card.ticked) == 1


def test_in_a_busy_channel_only_a_reply_to_the_request_closes_it(card):
    asyncio.run(client._team_message(BOT, faith_asks(channel=ROOM)))
    asyncio.run(client._team_message(BOT, message(9, NICOLE_P, "done", channel=ROOM)))
    assert card.ticked == [], "Nicole finishing something else ticked Faith's request"
    asyncio.run(client._team_message(BOT, message(10, NICOLE_P, "done", channel=ROOM, reply_to=1)))
    assert len(card.ticked) == 1


def test_an_emoji_from_nicole_ticks_it_and_from_anybody_else_doesnt(card):
    asyncio.run(client._team_message(BOT, faith_asks()))
    assert not asyncio.run(client._team_task_closes(BOT, "1", ELISA, "Elisa"))
    assert asyncio.run(client._team_task_closes(BOT, "1", NICOLE, "Nicole"))
    assert len(card.ticked) == 1 and "done by Nicole" in card.ticked[0]


def test_what_was_missed_while_down_is_caught_up(card, monkeypatch):
    asyncio.run(client._team_message(BOT, faith_asks()))
    asked = faith_asks()
    asked.reactions = [SimpleNamespace(users=lambda: _gen([ELISA_P]))]
    later = [message(11, ELISA_P, "done"), message(12, NICOLE_P, "all set ✅")]

    class Where:
        id = THREAD.id
        parent = THREAD.parent

        async def fetch_message(self, i):
            return asked

        def history(self, **kw):
            return _gen(later)

    bot = SimpleNamespace(config=SimpleNamespace(), get_channel=lambda i: Where())
    asyncio.run(client._team_tasks_catch_up(bot))
    assert len(card.ticked) == 1 and "done by Nicole" in card.ticked[0]


def test_a_reaction_from_nicole_is_caught_up_too(card):
    asyncio.run(client._team_message(BOT, faith_asks()))
    asked = faith_asks()
    asked.reactions = [SimpleNamespace(users=lambda: _gen([NICOLE_P]))]

    class Where:
        async def fetch_message(self, i):
            return asked

        def history(self, **kw):
            return _gen([])

    asyncio.run(client._team_tasks_catch_up(SimpleNamespace(config=SimpleNamespace(), get_channel=lambda i: Where())))
    assert len(card.ticked) == 1


def _gen(items):
    async def gen():
        for one in items:
            yield one
    return gen()


# ------------------------------------------------------------- the card

def test_on_the_card_under_general_tasks_and_ticked_only_by_its_own_id(monkeypatch):
    from tests.test_setupcheck import Board

    board = Board()
    board._list("general")["checkItems"].append({"id": "g1", "name": "Call Payra", "state": "incomplete"})
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)
    config = SimpleNamespace(secrets=SimpleNamespace(trello_board_id="b", trello_franklin=""))
    task = {"title": "Michael Greff - set a 10/day cap", "assignees": [{"id": "1", "name": "Nicole"}],
            "asker": "Faith", "made": "2026-10-13T19:34+00:00"}
    task["item_id"] = jobs.team_task_add(config, task)
    task.update(done_by="Nicole", done_at="2026-10-13T20:05+00:00")
    jobs.team_task_done(config, task)
    assert board.names("general") == {
        "Call Payra": "incomplete",
        "Michael Greff - set a 10/day cap · Nicole (asked by Faith 10/13) — done by Nicole 10/13 8:05pm": "complete",
    }


# ------------------------------------------------------------- commands

@pytest.mark.parametrize("said, action", [
    ("<@1> track https://discord.com/channels/1/2/3", "teamtrack"),
    ("<@1> track this <https://discord.com/channels/1/2/3>", "teamtrack"),
    ("<@1> team tasks", "teamtasks"), ("<@1> open tasks", "teamtasks"),
])
def test_commands(said, action):
    assert mentions.parse(said).action == action


def test_track_puts_a_missed_one_on_the_card(card):
    card.task = False  # Franklin said track it: it's a task whatever Claude makes of it
    sent = []

    class Responder:
        async def send(self, text=None, **kw):
            sent.append(text)

    asked = faith_asks(42)
    where = SimpleNamespace(fetch_message=lambda i: _done(asked))
    bot = SimpleNamespace(config=SimpleNamespace(), user=BOT.user, get_channel=lambda i: where)
    asyncio.run(client._team_track(bot, Responder(), "track https://discord.com/channels/1/500/42", None))
    assert len(card.added) == 1 and "📌" in sent[0] and "Nicole reacts" in sent[0]


async def _done(value):
    return value
