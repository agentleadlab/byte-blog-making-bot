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
    task.update(done_by="Nicole", done_at="2026-10-14T00:05+00:00")
    # Eastern, not UTC: "done by the hub 10/15 3:48pm" at 11:48am.
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
    assert card.ticked == ["Michael Greff - set a 10/day cap · Nicole (asked by Faith 10/13) — done by Nicole 10/13 4:05pm"]
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
    asked.reactions = [SimpleNamespace(emoji=SimpleNamespace(name="👍"), users=lambda: _gen([ELISA_P]))]
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
    asked.reactions = [SimpleNamespace(emoji=SimpleNamespace(name="👍"), users=lambda: _gen([NICOLE_P]))]

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

def test_on_the_card_under_concerns_and_ticked_only_by_its_own_id(monkeypatch):
    from tests.test_setupcheck import Board

    board = Board()
    board._list("concerns")["checkItems"].append({"id": "g1", "name": "Call Payra", "state": "incomplete"})
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)
    config = SimpleNamespace(secrets=SimpleNamespace(trello_board_id="b", trello_franklin=""))
    task = {"title": "Michael Greff - set a 10/day cap", "assignees": [{"id": "1", "name": "Nicole"}],
            "asker": "Faith", "made": "2026-10-13T19:34+00:00"}
    task["item_id"] = jobs.team_task_add(config, task)
    task.update(done_by="Nicole", done_at="2026-10-13T20:05+00:00")
    jobs.team_task_done(config, task)
    assert board.names("general") == {}
    assert board.names("concerns") == {
        "Call Payra": "incomplete",
        "Michael Greff - set a 10/day cap · Nicole (asked by Faith 10/13) — done by Nicole 10/13 4:05pm": "complete",
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


# ------------------------------------------------------------- paused, and not

def test_the_setup_check_is_paused_and_team_requests_are_off():
    """"pause but dont delete" - then team requests on the card, then "he
    doesnt need to do this" (10/22): off too."""
    assert client.CHECKLIST_PAUSED is True and client.TEAM_TASKS_ON is False
    # The Trello wrong-leads check keeps its own ten minutes.
    assert client.SETUP_CHECK_SECONDS == 600 and client.CHECKLIST_EVERY_SECONDS == 30 * 60
    assert teamtasks.TASKS_LIST == "Concerns Needs to Be Addressed"


def test_the_gates():
    import inspect

    assert "TEAM_TASKS_ON and" in inspect.getsource(client.WilByteBot.on_message)
    assert "if not TEAM_TASKS_ON:" in inspect.getsource(client.WilByteBot.on_raw_reaction_add)
    started = inspect.getsource(client.WilByteBot.on_ready)
    assert "not CHECKLIST_PAUSED" in started and "team_tasks_loop" in started


# ------------------------------------------------------------- reminders

def _open(i, hours_ago, *reminded):
    return {"id": str(i), "title": f"Client {i} - set a cap", "assignees": [{"id": "1", "name": "Nicole"}],
            "asker": "Faith", "made": (NOW - timedelta(hours=hours_ago)).isoformat(), "link": f"https://d/{i}",
            "reminded": [(NOW - timedelta(hours=h)).isoformat() for h in reminded]}


def test_a_reminder_is_due_after_four_hours_then_once_more_a_day_on():
    assert not client._team_reminder_due(_open(1, 3), NOW)
    assert client._team_reminder_due(_open(1, 5), NOW)
    assert not client._team_reminder_due(_open(1, 30, 10), NOW)
    assert client._team_reminder_due(_open(1, 30, 21), NOW)
    assert not client._team_reminder_due(_open(1, 60, 40, 21), NOW), "pinged twice already"


def test_franklin_is_pinged_in_ryte_the_goat_and_nothing_goes_to_growth(monkeypatch):
    sent = []

    class Goat:
        async def send(self, text=None, **kw):
            sent.append(text)

    # A board on Central time: 19:34 UTC is 2:34pm there, in working hours.
    teamtasks.save({"1": _open(1, 5), "2": _open(2, 1), "3": {**_open(3, 9), "done_at": NOW.isoformat()}})
    monkeypatch.setattr(client, "_hub_responder", lambda bot: Goat())
    monkeypatch.setattr(client, "_unmarked_ping", lambda config: "<@11>")
    bot = SimpleNamespace(config=SimpleNamespace(schedule=SimpleNamespace(timezone="America/Chicago")))
    due = asyncio.run(client._team_reminders(bot, now=NOW))
    assert [one["id"] for one in due] == ["1"]
    [text] = sent
    assert text.startswith("<@11> 🕓 **Still open with the team**") and "Client 1 - set a cap" in text
    assert "Client 2" not in text and "Client 3" not in text
    assert len(teamtasks.load()["1"]["reminded"]) == 1

    asyncio.run(client._team_reminders(bot, now=NOW + timedelta(minutes=30)))
    assert len(sent) == 1, "pinged again half an hour later"
    late = NOW.replace(hour=4)  # 11pm Central
    teamtasks.save({"1": _open(1, 30)})
    assert asyncio.run(client._team_reminders(bot, now=late)) == [] and len(sent) == 1, "pinged at 11pm"


# ------------------------------------------------------------- asked of the room

def test_an_at_here_request_is_tracked_and_only_a_check_from_someone_else_closes_it(card):
    santiago = person(55, "Santiago Villegas")
    asked = message(70, santiago, "@here Jack Mahn would like to be on a schedule from Monday - Saturday 9am to 10pm est",
                    channel=ROOM)
    asked.mention_everyone = True
    asyncio.run(client._team_message(BOT, asked))
    assert card.added == ["Michael Greff - set a 10/day cap · the team (asked by Santiago Villegas 10/13)"]
    assert card.reads[0]["asked"] == []

    assert not asyncio.run(client._team_task_closes(BOT, "70", NICOLE, "Nicole", emoji=SimpleNamespace(name="💙")))
    assert not asyncio.run(client._team_task_closes(BOT, "70", 55, "Santiago", emoji=SimpleNamespace(name="✅")))
    assert asyncio.run(client._team_task_closes(BOT, "70", NICOLE, "Nicole", emoji=SimpleNamespace(name="✅")))
    assert "done by Nicole" in card.ticked[0]


def test_someone_tagged_still_closes_it_with_any_emoji(card):
    asyncio.run(client._team_message(BOT, faith_asks()))
    assert asyncio.run(client._team_task_closes(BOT, "1", NICOLE, "Nicole", emoji=SimpleNamespace(name="💙")))


def test_plain_chat_with_nobody_tagged_isnt_read(card):
    asyncio.run(client._team_message(BOT, message(71, FAITH_P, "Talked to her. She is fine now", channel=ROOM)))
    assert card.reads == [] and card.added == []


def test_the_newest_ryte_checklist_card_is_the_one(monkeypatch):
    from tests.test_setupcheck import Board

    board = Board()
    board.board_cards = lambda board_id, archived=False: [
        {"id": "5f00000000000000000000aa", "name": "Ryte Checklist"},
        {"id": "6a0000000000000000000bbb", "name": "Ryte Checklist"}, {"id": "x", "name": "New Agent"}]
    seen = []
    board.card_checklists = lambda card_id: seen.append(card_id) or board.lists
    card, _lists = jobs._ryte_card(board, SimpleNamespace(secrets=SimpleNamespace(trello_board_id="b")))
    assert card["id"] == "6a0000000000000000000bbb" and seen == ["6a0000000000000000000bbb"]


# ------------------------------------------------------------- on the hub

def test_open_requests_are_checked_on_the_hub_and_ticked_when_its_done_there(card, monkeypatch):
    """"can you check hub to read and see if this was setup on hub"."""
    from wilbyte import hub

    teamtasks.save({
        "1": {"id": "1", "title": "Abdullah Hatem - set lead schedule to 7am-7pm PST", "item_id": "i1",
              "assignees": [{"id": "22", "name": "Nicole"}], "asker": "Faith", "made": NOW.isoformat()},
        "2": {"id": "2", "title": "Marcelino Jabr - set delivery hours 8AM-8PM Central, Mon-Fri", "item_id": "i2",
              "assignees": [{"id": "22", "name": "Nicole"}], "asker": "Kharyl", "made": NOW.isoformat()},
        "3": {"id": "3", "title": "Team huddle", "assignees": [], "asker": "Tre", "made": NOW.isoformat()},
    })
    agents = [hub.HubAgent(id="a", name="Abdullah Hatem", status="live", raw={"schedule": "07:00-19:00 PT"}),
              hub.HubAgent(id="b", name="Marcelino Jabr", status="scheduled", raw={"schedule": "24/7"})]
    monkeypatch.setattr(hub, "agents", lambda secrets, status="fulfilled": agents)
    asked = []

    def judging(config, *, task, records, today):
        asked.append((task, records))
        if "Abdullah" in task:
            return {"verdict": "done", "why": "schedule is 07:00-19:00 PT"}
        return {"verdict": "not_done", "why": "schedule is still 24/7"}

    monkeypatch.setattr(jobs, "hub_task_verdict", judging)
    monkeypatch.setattr(client, "_today", lambda config: NOW.date())
    bot = SimpleNamespace(config=SimpleNamespace(secrets=SimpleNamespace(hub_api_token="t")))
    checked = asyncio.run(client._team_tasks_on_hub(bot))
    assert [(task["id"], verdict["verdict"]) for task, verdict in checked] == [("1", "done"), ("2", "not_done")]
    assert "`schedule`: 07:00-19:00 PT" in asked[0][1][0], "the hub record wasn't what was judged"
    assert len(card.ticked) == 1 and card.ticked[0].endswith("done by the hub " + card.ticked[0].split("done by the hub ")[1])
    held = teamtasks.load()
    assert held["1"]["done_by"] == "the hub" and not held["2"].get("done_at")
    assert held["2"]["hub_why"] == "schedule is still 24/7"

    # Nothing changed on the hub: not asked again. Changed: asked.
    asyncio.run(client._team_tasks_on_hub(bot))
    assert len(asked) == 2
    agents[1].raw["schedule"] = "Mon-Fri 08:00-20:00 CT"
    asyncio.run(client._team_tasks_on_hub(bot))
    assert len(asked) == 3


def test_no_hub_key_no_check(card):
    bot = SimpleNamespace(config=SimpleNamespace(secrets=SimpleNamespace(hub_api_token="")))
    assert asyncio.run(client._team_tasks_on_hub(bot)) == []


@pytest.mark.parametrize("said", ["<@1> hub tasks", "<@1> check the hub for the tasks", "<@1> check tasks on the hub"])
def test_hub_tasks_command(said):
    assert mentions.parse(said).action == "hubtasks"


# ------------------------------------------------------------- the card says

def test_ticked_or_deleted_on_the_card_is_done(card, monkeypatch):
    """"You are not tracking these correctly, most of these are done"."""
    teamtasks.save({
        "1": {"id": "1", "title": "Marcelino Jabr - set delivery hours", "item_id": "i1", "made": NOW.isoformat(),
              "assignees": [{"id": "22", "name": "Nicole"}]},
        "2": {"id": "2", "title": "Ian Adams - set up new payment", "item_id": "i2", "made": NOW.isoformat(),
              "assignees": []},
        "3": {"id": "3", "title": "Izick Ruiz - set up 25 veteran leads", "item_id": "i3", "made": NOW.isoformat(),
              "assignees": []},
    })
    monkeypatch.setattr(jobs, "ryte_card_items", lambda config: {"i1": "complete", "i2": "incomplete"})
    closed = asyncio.run(client._team_tasks_from_card(BOT))
    assert {one["id"] for one in closed} == {"1", "3"}
    held = teamtasks.load()
    assert held["1"]["done_by"] == "ticked on the card" and held["3"]["done_by"] == "taken off the card"
    assert not held["2"].get("done_at")
    assert card.ticked == [], "the card was written to for what the card already said"


def test_the_list_and_the_hub_check_read_the_card_first(card, monkeypatch):
    teamtasks.save({"1": {"id": "1", "title": "Marcelino Jabr - set delivery hours", "item_id": "i1",
                          "made": NOW.isoformat(), "assignees": []}})
    monkeypatch.setattr(jobs, "ryte_card_items", lambda config: {"i1": "complete"})
    sent = []

    class Goat:
        async def send(self, text=None, **kw):
            sent.append(text)

    asyncio.run(client._team_list(Goat(), BOT))
    assert sent == ["Nothing asked of the team is open. ✅"]


def test_a_thread_request_is_named_from_the_post_it_hangs_off():
    starter = SimpleNamespace(content="", embeds=[SimpleNamespace(title="New Payment | Payra",
                              description="NAME: Killian Cooper\nPRODUCT: VET", fields=[], url=None,
                              footer=None, author=None)])

    class Parent:
        name = "new-payment"

        async def fetch_message(self, i):
            return starter

    thread = SimpleNamespace(id=5, name="@here", parent=Parent())
    where = asyncio.run(client._where_with_context(thread))
    assert "NAME: Killian Cooper" in where and "@here" in where


def test_the_6pm_card_comment_is_stopped():
    """"lets stop this" - the 🌙 list on the Ryte Checklist card."""
    assert client.TEAM_EVENING_NOTE is False


def test_the_nudges_are_stopped():
    """"it should also stop doing this now" - Still open with the team."""
    assert client.TEAM_NUDGES is False


def test_a_full_concerns_list_goes_on_to_the_next(monkeypatch):
    """CHECKLIST_TOO_MANY_CHECK_ITEMS: every team request since Concerns hit
    Trello's 200 was lost. The next one goes on "Concerns (2)"."""
    from tests.test_setupcheck import Board
    from wilbyte import trello

    board = Board(concerns=[f"old {i}" for i in range(jobs.TASKS_PER_LIST)])
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)
    config = SimpleNamespace(secrets=SimpleNamespace(trello_board_id="b", trello_franklin=""))
    task = {"title": "Ian Nye - cap 5", "assignees": [{"id": "1", "name": "Nicole"}], "asker": "Faith",
            "made": "2026-10-22T14:00+00:00"}
    first = jobs.team_task_add(config, task)
    second = jobs.team_task_add(config, dict(task, title="Drake Boren - states"))
    named = "Concerns Needs to Be Addressed (2)"
    assert [one["id"] for one in board._list(named)["checkItems"]] == [first, second]

    # Trello says full before the count does: on to the next all the same.
    board = Board(concerns=["one"])
    adding = board.add_check_item

    def refusing(list_id, name, checked=False):
        if list_id == "concerns":
            raise trello.TrelloError('POST /checklists/x/checkItems -> HTTP 403: {"error":"CHECKLIST_TOO_MANY_CHECK_ITEMS"}')
        return adding(list_id, name, checked)

    board.add_check_item = refusing
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)
    item = jobs.team_task_add(config, task)
    assert [one["id"] for one in board._list(named)["checkItems"]] == [item]


def test_payra_refusing_the_token_is_said_once_and_its_return_too(monkeypatch):
    """From 7:40 on 10/22 Payra refused Ryte's token and it was only in the log."""
    import asyncio as _asyncio

    from wilbyte import payraapi

    said = []
    runs = iter([payraapi.PayraError("Payra didn't accept the token - check PAYRA_API_TOKEN in .env."),
                 payraapi.PayraError("Payra didn't accept the token - check PAYRA_API_TOKEN in .env."),
                 {"payments": 1}])

    def syncing(config):
        got = next(runs)
        if isinstance(got, Exception):
            raise got
        return got

    class Responder:
        async def send(self, text):
            said.append(text)

    closed = iter([False, False, False, True])
    bot = SimpleNamespace(config=SimpleNamespace(secrets=SimpleNamespace(discord_notify_user_id="42")),
                          is_closed=lambda: next(closed))
    monkeypatch.setattr(jobs, "payra_sync", syncing)
    monkeypatch.setattr(client, "PAYRA_SYNC_SECONDS", 0)
    monkeypatch.setattr(client, "_order_responder", lambda bot: Responder())
    _asyncio.run(client.payra_loop(bot))
    assert len(said) == 2
    assert said[0].startswith("<@42> ") and "PAYRA_API_TOKEN" in said[0]
    assert "reading again" in said[1]
