"""The setups going live: the threads, the hub and Payra, on the Ryte Checklist card."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from wilbyte import hub, setupcheck
from wilbyte.bot import client, jobs, mentions

TODAY = date(2026, 10, 13)


def agent(**over):
    one = dict(id="a1", name="Gavin Littledike", status="scheduled", lead_type="FEX",
               start_date=date(2026, 10, 14), ordered=25, daily_cap=5,
               sheet_url="https://docs.google.com/spreadsheets/d/SHEET123/edit",
               states=("AZ", "IL", "TX"), schedule="Mon-Fri 9am-5pm CST",
               raw={"states": "AZ,IL,TX"})
    one.update(over)
    return hub.HubAgent(**one)


def asked(**over):
    got = dict(lead_type="Final Expense", leads=25, daily_cap=5, states=["AZ", "IL", "TX"],
               hours="9-5 CST weekdays", start_date="2026-10-14", same_as_before=False,
               hours_match="yes", live=False)
    got.update(over)
    return setupcheck.asked_from(got, thread="Gavin Littledike - FEX 25", link="https://discord.com/x")


def wrong_of(result):
    return {one.what: one.said for one in result.wrong}


# ------------------------------------------------------------- the item

def test_the_item_is_the_cards_own_pattern():
    assert setupcheck.item_title("Gavin Littledike", "FEX", date(2026, 10, 14)) == \
        "Gavin Littledike (FEX) - 10/14 - Wednesday"
    assert setupcheck.item_title("Gavin Littledike", "", None) == "Gavin Littledike - no live date"


def test_a_subtype_the_thread_names_has_to_be_on_the_hub():
    assert setupcheck.same_product("Trucker IUL", "TRUCKER_IUL") is True
    assert setupcheck.same_product("IUL", "TRUCKER_IUL") is True
    assert setupcheck.same_product("Trucker IUL", "IUL") is False
    assert setupcheck.same_product("Final Expense", "MTG_PLUS") is False
    assert setupcheck.same_product("", "FEX") is None


# ------------------------------------------------------------- the thread

def test_the_readers_answer_is_tidied():
    got = setupcheck.asked_from({"lead_type": " Trucker  IUL ", "leads": "15", "daily_cap": 0,
                                 "states": "az, tx and IL", "start_date": "2026-10-14",
                                 "hours_match": "unclear"})
    assert got.lead_type == "Trucker IUL" and got.leads == 15
    assert got.daily_cap is None, "0 is 'none asked', not a cap of nothing"
    assert got.states == ("AZ", "TX", "IL")
    assert got.start == date(2026, 10, 14) and got.hours_match == ""


def _thread(name):
    return SimpleNamespace(name=name)


def test_a_thread_is_theirs_by_the_name_at_the_front():
    threads = [_thread("Lauren Huddleston · Text-Verified Veteran Leads [FRESH - $45/Lead]"),
               _thread("Edward Conway - Trucker IUL 15 leads"), _thread("NAME: Madison McGill"),
               _thread("Marissa M")]
    assert setupcheck.threads_for("Lauren Huddleston", threads) == [threads[0]]
    assert setupcheck.threads_for("Edward Conway", threads) == [threads[1]]
    assert setupcheck.threads_for("Madison McGill", threads) == [threads[2]]
    assert setupcheck.threads_for("Marissa Martinez", threads) == [threads[3]]
    assert setupcheck.threads_for("Gavin Littledike", threads) == []


def test_a_first_name_and_initial_that_fits_two_threads_is_neither():
    threads = [_thread("Marissa M"), _thread("Marissa M")]
    assert setupcheck.threads_for("Marissa Martinez", threads) == []


# ------------------------------------------------------------- the checks

def test_everything_matching_is_ticked():
    result = setupcheck.check(agent(), asked(), paid=True, today=TODAY)
    assert result.all_good, wrong_of(result)


def test_the_cap_the_states_and_the_count_are_held_to_the_thread():
    result = setupcheck.check(agent(daily_cap=0, states=("AZ", "IL", "OH"), ordered=20),
                              asked(), paid=True, today=TODAY)
    wrong = wrong_of(result)
    assert "no cap" in wrong["Daily cap"]
    assert "missing on the hub: TX" in wrong["States"] and "OH" in wrong["States"]
    assert wrong["Lead count"] == "thread asked for 25, the hub has 20"


def test_the_wrong_product_and_hours_and_date_are_said():
    result = setupcheck.check(agent(lead_type="MTG_PLUS", start_date=date(2026, 10, 15)),
                              asked(hours_match="no", hours_why="asked 9-12 CST, the hub has 24/7"),
                              paid=True, today=TODAY)
    wrong = wrong_of(result)
    assert set(wrong) == {"Lead type", "Start date", "Hours"}
    assert wrong["Hours"] == "asked 9-12 CST, the hub has 24/7"


def test_not_paid_and_no_thread_are_concerns_but_unread_payra_isnt():
    result = setupcheck.check(agent(), None, paid=False, today=TODAY)
    assert set(wrong_of(result)) == {"Paid", "Thread"}
    result = setupcheck.check(agent(), asked(), paid=None, today=TODAY)
    assert result.all_good


def test_what_the_thread_doesnt_say_is_never_wrong():
    result = setupcheck.check(agent(), asked(lead_type="", leads=0, daily_cap=0, states=[],
                                             start_date="", hours_match="unclear"),
                              paid=True, today=TODAY)
    assert result.all_good, wrong_of(result)


def test_no_states_on_the_hub_is_only_wrong_when_the_hub_has_a_states_field():
    assert "States set" in wrong_of(setupcheck.check(agent(states=()), asked(states=[]), paid=True, today=TODAY))
    assert setupcheck.check(agent(states=(), raw={}), asked(states=[]), paid=True, today=TODAY).all_good


def test_whos_coming_up():
    found = [agent(id="1", start_date=TODAY + timedelta(days=1)),
             agent(id="2", start_date=TODAY + timedelta(days=30)),
             agent(id="3", status="live"), agent(id="4", status="pending", start_date=None),
             agent(id="5", start_date=TODAY - timedelta(days=5))]
    assert [one.id for one in setupcheck.coming_up(found, TODAY)] == ["1", "4"]


def test_tomorrows_not_ready():
    tomorrow = TODAY + timedelta(days=1)
    good = setupcheck.check(agent(id="g", start_date=tomorrow), asked(start_date=tomorrow.isoformat()),
                            paid=True, today=TODAY)
    bad = setupcheck.check(agent(id="b", start_date=tomorrow), None, paid=True, today=TODAY)
    later = setupcheck.check(agent(id="l", start_date=TODAY + timedelta(days=3)), None, paid=True, today=TODAY)
    assert setupcheck.not_ready([good, bad, later], tomorrow) == [bad]


# ------------------------------------------------------------- the card

class Board:
    """The Ryte Checklist card, in memory."""

    def __init__(self, items=(), concerns=()):
        self.lists = [
            {"id": "setup", "name": "Agent Setup",
             "checkItems": [{"id": f"s{i}", "name": n, "state": "incomplete"} for i, n in enumerate(items)]},
            {"id": "concerns", "name": "Concerns Needs to Be Addressed",
             "checkItems": [{"id": f"c{i}", "name": n, "state": "incomplete"} for i, n in enumerate(concerns)]},
            {"id": "general", "name": "General Tasks", "checkItems": []},
        ]
        self.comments, self.made = [], 0

    def board_cards(self, board_id, archived=False):
        return [{"id": "other", "name": "New Agent"}, {"id": "ryte", "name": "Ryte  Checklist"}]

    def card_checklists(self, card_id):
        assert card_id == "ryte"
        return self.lists

    def create_checklist(self, card_id, name):
        made = {"id": name, "name": name, "checkItems": []}
        self.lists.append(made)
        return made

    def _list(self, list_id):
        return next(one for one in self.lists if one["id"] == list_id)

    def _item(self, item_id):
        return next(item for one in self.lists for item in one["checkItems"] if item["id"] == item_id)

    def add_check_item(self, list_id, name, checked=False):
        self.made += 1
        item = {"id": f"n{self.made}", "name": name, "state": "incomplete"}
        self._list(list_id)["checkItems"].append(item)
        return dict(item)

    def set_check_item(self, card_id, item_id, *, done=None, name=None):
        item = self._item(item_id)
        if done is not None:
            item["state"] = "complete" if done else "incomplete"
        if name is not None:
            item["name"] = name

    def add_comment(self, card_id, text):
        self.comments.append(text)

    def board_members(self, board_id):
        return [{"username": "nova12", "fullName": "Nova"}, {"username": "franklin88", "fullName": "Franklin Reyes"}]

    def close(self):
        pass

    def names(self, list_id):
        return {one["name"]: one["state"] for one in self._list(list_id)["checkItems"]}


CONFIG = SimpleNamespace(secrets=SimpleNamespace(trello_board_id="b", trello_franklin=""))


def _sync(monkeypatch, board, results, said=None):
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)
    return jobs.sync_ryte_card(CONFIG, results, said_before=said or {})


def test_a_good_setup_is_added_and_ticked_with_nothing_said(monkeypatch):
    board = Board()
    fresh, said, problems = _sync(monkeypatch, board, [setupcheck.check(agent(), asked(), paid=True, today=TODAY)])
    assert problems == [] and fresh == [] and board.comments == []
    assert board.names("setup") == {"Gavin Littledike (FEX) - 10/14 - Wednesday": "complete"}


def test_a_wrong_setup_is_raised_once_tagging_franklin_and_ticked_off_when_put_right(monkeypatch):
    board = Board()
    bad = setupcheck.check(agent(daily_cap=0), asked(), paid=True, today=TODAY)
    fresh, said, _ = _sync(monkeypatch, board, [bad])
    title = "Gavin Littledike (FEX) - 10/14 - Wednesday"
    assert board.names("setup") == {title: "incomplete"}
    [line] = board.names("concerns")
    assert line.startswith(title + ": Daily cap")
    [comment] = board.comments
    assert comment.startswith("@franklin88 ⚠ **" + title) and "Thread: https://discord.com/x" in comment
    assert [one[0] for one in fresh] == [title]

    # The next pass: still wrong, nothing said again, nothing doubled.
    fresh, said, _ = _sync(monkeypatch, board, [bad], said)
    assert fresh == [] and len(board.comments) == 1 and len(board.names("concerns")) == 1

    # Put right on the hub: ticked in both places, left on the card to see.
    fresh, said, _ = _sync(monkeypatch, board, [setupcheck.check(agent(), asked(), paid=True, today=TODAY)], said)
    assert board.names("setup") == {title: "complete"}
    assert board.names("concerns") == {line: "complete"}
    assert len(board.comments) == 1


def test_a_moved_live_date_renames_the_item_rather_than_doubling_it(monkeypatch):
    board = Board()
    _fresh, said, _ = _sync(monkeypatch, board, [setupcheck.check(agent(), asked(), paid=True, today=TODAY)])
    moved = agent(start_date=date(2026, 10, 16))
    _sync(monkeypatch, board, [setupcheck.check(moved, asked(start_date="2026-10-16"), paid=True, today=TODAY)], said)
    assert board.names("setup") == {"Gavin Littledike (FEX) - 10/16 - Friday": "complete"}


def test_two_orders_for_one_client_are_two_items(monkeypatch):
    board = Board()
    _sync(monkeypatch, board, [setupcheck.check(agent(), asked(), paid=True, today=TODAY),
                               setupcheck.check(agent(id="a2", lead_type="MTG_PLUS"),
                                                asked(lead_type="Mortgage"), paid=True, today=TODAY)])
    assert set(board.names("setup")) == {"Gavin Littledike (FEX) - 10/14 - Wednesday",
                                         "Gavin Littledike (MTG_PLUS) - 10/14 - Wednesday"}


def test_the_general_tasks_are_never_touched(monkeypatch):
    board = Board()
    board._list("general")["checkItems"].append({"id": "g1", "name": "Call Payra", "state": "incomplete"})
    _sync(monkeypatch, board, [setupcheck.check(agent(daily_cap=0), None, paid=False, today=TODAY)])
    assert board.names("general") == {"Call Payra": "incomplete"}


def test_no_card_is_a_problem_said_not_a_card_made(monkeypatch):
    board = Board()
    board.board_cards = lambda board_id, archived=False: [{"id": "other", "name": "New Agent"}]
    fresh, said, problems = _sync(monkeypatch, board, [setupcheck.check(agent(), None, paid=True, today=TODAY)],
                                  {"kept": 1})
    assert fresh == [] and said == {"kept": 1}
    assert "Ryte Checklist" in problems[0]


# ------------------------------------------------------------- payments

def test_paid_by_the_hubs_email_or_whole_name(monkeypatch):
    from wilbyte import payraapi

    now = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
    data = {"invoices": {"i1": {"name": "G. Littledike", "email": "gavin@example.com"}}}
    paid_on = {"i1": now - timedelta(days=3)}
    monkeypatch.setattr(payraapi, "load", lambda *a, **k: data)
    monkeypatch.setattr(payraapi, "payments_on", lambda d, inv: [
        {"paid_on": paid_on["i1"].isoformat(), "ok": True}])
    monkeypatch.setattr(payraapi, "_went_through", lambda payment: payment["ok"])

    assert jobs.paid_for(agent(raw={"email": "Gavin@example.com"}), now=now) is True
    assert jobs.paid_for(agent(raw={}), now=now) is False, "a different first name isn't them"
    data["invoices"]["i1"]["name"] = "Gavin Littledike"
    assert jobs.paid_for(agent(raw={}), now=now) is True
    paid_on["i1"] = now - timedelta(days=60)
    assert jobs.paid_for(agent(raw={}), now=now) is False, "last order's payment isn't this one's"
    monkeypatch.setattr(payraapi, "load", lambda *a, **k: {})
    assert jobs.paid_for(agent(), now=now) is None


# ------------------------------------------------------------- the pass

class Message:
    def __init__(self, i, text, when):
        self.id, self.content = i, text
        self.author = SimpleNamespace(display_name="Nova", name="nova")
        self.created_at = when
        self.attachments = [SimpleNamespace(url="https://cdn.discordapp.com/a.png", content_type="image/png")] if i == 2 else []
        self.embeds = []


class Thread:
    def __init__(self, name, messages):
        self.name, self.jump_url, self._messages = name, "https://discord.com/t/1", messages
        self.id = hash(name)

    def history(self, limit=80):
        async def gen():
            for one in reversed(self._messages):
                yield one
        return gen()


class Guild:
    name = "𝐀𝐠𝐞𝐧𝐭 𝐋𝐞𝐚𝐝 𝐋𝐚𝐛 Growth Team"
    text_channels = []

    def __init__(self, threads):
        self.threads = threads

    async def active_threads(self):
        return self.threads


def _pass(monkeypatch, *, agents, threads, reads):
    when = datetime(2026, 10, 12, 15, tzinfo=timezone.utc)
    monkeypatch.setattr(hub, "agents", lambda secrets, status="fulfilled": [a for a in agents if a.status == status])
    monkeypatch.setattr(client, "_today", lambda config: TODAY)
    monkeypatch.setattr(jobs, "paid_for", lambda one: True)
    calls = []

    def reading(config, one, said, *, today):
        calls.append((one.name, [m["text"] for m in said], [i for m in said for i in m["images"]]))
        return reads[one.name]

    monkeypatch.setattr(jobs, "read_setup_thread", reading)
    board = Board()
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)
    guild = Guild([Thread(name, [Message(1, f"{name} bought", when), Message(2, "set up", when + timedelta(hours=1))])
                   for name in threads])
    bot = SimpleNamespace(config=SimpleNamespace(secrets=SimpleNamespace(trello_board_id="b", trello_franklin="",
                                                                         discord_setup_guild_id="")),
                          guilds=[guild], get_guild=lambda i: None)
    return bot, board, calls


def test_the_pass_reads_each_thread_once_until_it_changes(monkeypatch):
    got = dict(lead_type="Final Expense", leads=25, daily_cap=0, states=[], hours="", start_date="",
               same_as_before=False, hours_match="unclear", live=False)
    bot, board, calls = _pass(monkeypatch, agents=[agent(), agent(id="z", name="Zed Nobody")],
                              threads=["Gavin Littledike - FEX 25"], reads={"Gavin Littledike": got})
    results, fresh, problems = asyncio.run(client._setup_pass(bot))
    assert problems == []
    assert [(one.agent.name, one.all_good) for one in results] == [("Gavin Littledike", True), ("Zed Nobody", False)]
    assert calls == [("Gavin Littledike", ["Gavin Littledike - FEX 25 bought", "set up"],
                      ["https://cdn.discordapp.com/a.png"])]
    assert [one[0] for one in fresh] == ["Zed Nobody (FEX) - 10/14 - Wednesday"]

    asyncio.run(client._setup_pass(bot))
    assert len(calls) == 1, "an unchanged thread was read again"
    assert len(board.comments) == 1, "the same concern was said twice"


def test_without_the_growth_server_nothing_is_called_missing(monkeypatch):
    bot, board, calls = _pass(monkeypatch, agents=[agent()], threads=[], reads={})
    bot.guilds = []
    results, fresh, problems = asyncio.run(client._setup_pass(bot))
    assert results == [] and fresh == [] and board.comments == []
    assert "Growth Team server" in problems[0]


def test_the_pass_never_writes_to_the_hub(monkeypatch):
    """"make sure ryte is not deleting anyone on hub or changing states and caps"."""
    import httpx

    def refuse(*a, **k):
        raise AssertionError("RYTE wrote to the hub")

    for verb in ("post", "put", "patch", "delete"):
        monkeypatch.setattr(httpx, verb, refuse)
        monkeypatch.setattr(httpx.Client, verb, refuse)
    bot, board, calls = _pass(monkeypatch, agents=[agent()], threads=["Gavin Littledike"],
                              reads={"Gavin Littledike": {"lead_type": "FEX"}})
    results, _fresh, problems = asyncio.run(client._setup_pass(bot))
    assert len(results) == 1 and problems == []


# ------------------------------------------------------------- the command

@pytest.mark.parametrize("said", ["<@1> checklist", "<@1> ryte checklist", "<@1> update the checklist",
                                  "<@1> check list"])
def test_checklist_is_a_command(said):
    assert mentions.parse(said).action == "rytechecklist"


def test_closed_threads_are_found_public_private_and_in_forums_but_not_old_ones():
    now = datetime.now(timezone.utc)

    def closed(name, days):
        return SimpleNamespace(id=hash(name), name=name, archive_timestamp=now - timedelta(days=days))

    class Listing:
        def __init__(self, public=(), private=()):
            self.public, self.private = public, private

        def archived_threads(self, *, private=False, limit=50):
            async def gen():
                for one in (self.private if private else self.public):
                    yield one
            return gen()

    class Forum:
        def archived_threads(self, *, limit=50):
            async def gen():
                yield closed("Edward Conway - Trucker IUL", 2)
            return gen()

    class Server:
        text_channels = [Listing(public=[closed("Gavin Littledike", 3), closed("Old One", 45)],
                                 private=[closed("Madison McGill", 1)])]
        forums = [Forum()]

        async def active_threads(self):
            return [SimpleNamespace(id=1, name="Lauren Huddleston")]

    names = {one.name for one in asyncio.run(client._setup_threads(Server()))}
    assert names == {"Lauren Huddleston", "Gavin Littledike", "Madison McGill", "Edward Conway - Trucker IUL"}
