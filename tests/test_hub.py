"""The Distro Hub's fulfilled orders, against the agents' own sheets."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from wilbyte import delivery, hub

NOW = datetime(2026, 10, 8, 20, 0, tzinfo=timezone.utc)
TOKEN = "rok_fake_for_tests_only"


def record(**over):
    one = {
        "id": "a1", "name": "Tavin Example", "status": "fulfilled", "fulfilled": True,
        "lead_type": "VET", "lead_type_label": "Text Verified Veteran Plus",
        "start_date": "2026-10-05", "delivered": 26, "ordered": 26, "progress": "26/26",
        "daily_cap": 6, "sheet_url": "https://docs.google.com/spreadsheets/d/SHEET123456789/edit",
        "discord_channel": "", "fulfilled_at": "2026-10-08T15:00:00Z", "updated_at": "2026-10-08T15:00:00Z",
    }
    one.update(over)
    return one


def test_an_agent_is_read_as_the_api_gives_it():
    agent = hub.read(record())
    assert agent.name == "Tavin Example" and agent.fulfilled is True
    assert agent.start_date == date(2026, 10, 5)
    assert (agent.delivered, agent.ordered) == (26, 26)
    assert agent.lead_type == "Text Verified Veteran Plus"
    assert agent.fulfilled_at == datetime(2026, 10, 8, 15, 0, tzinfo=timezone.utc)


def test_the_counts_come_off_progress_when_the_numbers_are_missing():
    agent = hub.read(record(delivered=None, ordered=None, progress="34/40"))
    assert (agent.delivered, agent.ordered) == (34, 40)


@pytest.mark.parametrize("body", [
    [record()], {"agents": [record()]}, {"data": [record()]},
])
def test_the_list_is_found_wherever_the_reply_puts_it(body):
    assert [one.id for one in map(hub.read, hub._records(body))] == ["a1"]


def _answering(monkeypatch, status=200, body=None):
    asked = {}

    def get(url, params=None, headers=None, timeout=None):
        asked.update(url=url, params=params, headers=headers)
        return httpx.Response(status, json=body if body is not None else [record()],
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(hub.httpx, "get", get)
    return asked


def test_the_token_goes_in_the_header_and_never_the_url(monkeypatch):
    asked = _answering(monkeypatch)
    hub.agents(SimpleNamespace(hub_api_token=TOKEN, hub_api_url=""))
    assert asked["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in asked["url"] and TOKEN not in str(asked["params"])
    assert asked["url"] == hub.DEFAULT_URL and asked["params"] == {"status": "fulfilled"}


def test_a_refused_key_says_what_to_check_and_not_the_key(monkeypatch):
    _answering(monkeypatch, status=403, body={"error": "forbidden"})
    with pytest.raises(hub.HubError) as raised:
        hub.agents(SimpleNamespace(hub_api_token=TOKEN, hub_api_url=""))
    assert "HUB_API_TOKEN" in str(raised.value)
    assert TOKEN not in str(raised.value)


def test_no_token_is_said_before_anything_is_asked(monkeypatch):
    def never(*a, **kw):
        raise AssertionError("asked the hub without a key")

    monkeypatch.setattr(hub.httpx, "get", never)
    with pytest.raises(hub.HubError, match="HUB_API_TOKEN"):
        hub.agents(SimpleNamespace(hub_api_token="", hub_api_url=""))


def _sheet(*dates):
    rows = [["Name", "Email", "Phone Number", "Date Added"]]
    rows += [[f"Lead {n}", f"l{n}@example.com", "555-010-0000", day] for n, day in enumerate(dates)]
    return delivery.count_rows(rows)


def test_a_full_sheet_is_not_short():
    one = hub.Checked(agent=hub.read(record()), counted=_sheet(*["10/06/2026"] * 26))
    assert one.on_sheet == 26 and one.short_by() == 0
    assert hub.describe(one).startswith("✅")


def test_a_short_sheet_says_by_how_many():
    one = hub.Checked(agent=hub.read(record()), counted=_sheet(*["10/06/2026"] * 23))
    assert one.short_by() == 3
    line = hub.describe(one)
    assert line.startswith("⚠") and "hub says 26/26" in line and "3 the hub sent aren't on the sheet" in line


def test_the_whole_sheet_is_set_against_the_hubs_running_count():
    """Hunter Kiser: "hub says 229/40, the sheet has 231". The hub's first
    number is everything it has ever sent him; the sheet holds it all."""
    one = hub.Checked(agent=hub.read(record(delivered=229, ordered=40, progress="229/40")),
                      counted=_sheet(*["10/06/2026"] * 231))
    assert one.short_by() == 0 and hub.describe(one).startswith("✅")


def test_more_on_the_sheet_than_the_hub_sent_is_fine():
    """Leads from before the hub, on the same sheet."""
    one = hub.Checked(agent=hub.read(record(delivered=27, ordered=27, progress="27/27")),
                      counted=_sheet(*["10/06/2026"] * 191))
    assert one.short_by() == 0


def test_an_unread_sheet_is_never_called_short():
    one = hub.Checked(agent=hub.read(record()), problems=["Couldn't read the lead sheet: 403"])
    assert one.short_by() == 0 and hub.describe(one).startswith("•")


def test_only_fresh_unchecked_fulfilled_orders_with_a_sheet_are_due():
    found = [
        hub.read(record(id="new")),
        hub.read(record(id="old", fulfilled_at="2026-09-01T00:00:00Z", updated_at="2026-09-01T00:00:00Z")),
        hub.read(record(id="nosheet", sheet_url="")),
        hub.read(record(id="active", status="active", fulfilled=False)),
        hub.read(record(id="seen")),
    ]
    said = {hub.key(found[-1]): 26}
    assert [one.id for one in hub.due(found, now=NOW, said=said)] == ["new"]


def test_a_later_save_doesnt_make_the_same_order_new():
    """fulfilled_at is the row's last save. The order is the id, the start
    and the size."""
    first = hub.read(record(fulfilled_at="2026-10-06T00:00:00Z"))
    saved_again = hub.read(record(fulfilled_at="2026-10-08T19:00:00Z"))
    assert hub.key(first) == hub.key(saved_again)
    assert hub.key(first) != hub.key(hub.read(record(start_date="2026-11-01")))


def test_one_agent_by_name():
    found = [hub.read(record(id="1", name="Tavin Dougher")), hub.read(record(id="2", name="Shelby Guest"))]
    assert [one.id for one in hub.named(found, "tavin dougher")] == ["1"]
    assert hub.named(found, "Tavin Guest") == []


def test_the_loop_says_only_the_short_ones_and_remembers_what_it_read(monkeypatch):
    import asyncio

    from wilbyte.bot import client, jobs

    found = [hub.read(record(id="full", name="Full Agent")), hub.read(record(id="short", name="Short Agent")),
             hub.read(record(id="unread", name="Unread Agent"))]
    monkeypatch.setattr(hub, "fulfilled_tab", lambda secrets: found)

    def checking(config, agents):
        out = []
        for agent in agents:
            if agent.id == "unread":
                out.append(hub.Checked(agent=agent, problems=["403"]))
            else:
                out.append(hub.Checked(agent=agent, counted=_sheet(*["10/06/2026"] * (26 if agent.id == "full" else 20))))
        return out

    monkeypatch.setattr(jobs, "hub_check", checking)
    monkeypatch.setattr(client, "_unmarked_ping", lambda config: "")
    monkeypatch.setattr(client, "datetime", SimpleNamespace(now=lambda tz=None: NOW))
    sent = []

    class Responder:
        async def send(self, text=None, **kw):
            sent.append(text)

    monkeypatch.setattr(client, "_board_responder", lambda bot: Responder())
    ticks = iter([False, True])
    bot = SimpleNamespace(is_closed=lambda: next(ticks), config=SimpleNamespace(secrets=None))

    async def no_wait(_s):
        return None

    monkeypatch.setattr(client.asyncio, "sleep", no_wait)
    asyncio.run(client.hub_check_loop(bot))

    [message] = sent
    assert "Short Agent" in message and "6 the hub sent aren't on the sheet" in message
    assert "Full Agent" not in message and "Unread Agent" not in message
    held = hub.checked()
    assert hub.key(found[0]) in held and hub.key(found[1]) in held
    assert hub.key(found[2]) not in held, "an unread sheet was marked checked and won't be tried again"


@pytest.mark.parametrize("said, who", [
    ("<@1> hub check", ""), ("<@1> hub check Tavin Dougher", "Tavin Dougher"), ("<@1> hub", ""),
])
def test_hub_check_is_a_command(said, who):
    from wilbyte.bot import mentions

    asked = mentions.parse(said)
    assert asked.action == "hubcheck" and (asked.brief or "") == who


def test_a_hub_link_is_not_the_command():
    from wilbyte.bot import mentions

    assert mentions.parse("<@1> https://hub.agentleadlab.com/distro/agents").action != "hubcheck"


def test_a_wrong_address_says_where_the_api_is(monkeypatch):
    """"Distro Hub - The hub answered 404" - /api/agents, when the hub's API
    is under /distro."""
    _answering(monkeypatch, status=404, body={})
    with pytest.raises(hub.HubError) as raised:
        hub.agents(SimpleNamespace(hub_api_token=TOKEN, hub_api_url="https://hub.agentleadlab.com/api/agents"))
    assert "/distro/api/agents" in str(raised.value) and "HUB_API_URL" in str(raised.value)


def test_the_done_tab_is_not_the_fulfilled_tab():
    """"joevanny is not even here" - he's in Done, closed out; the check is
    for the hub's Fulfilled tab. The API flags both as fulfilled."""
    assert hub.read(record(status="done", fulfilled=True)).fulfilled is False
    assert hub.read(record(status="fulfilled", fulfilled=None)).fulfilled is True
    # The tab's page is /distro/agents?status=ended.
    assert hub.read(record(status="ended", fulfilled=True)).fulfilled is True
    # Joevanny Astorga, paused, reached his count once - not on the tab.
    assert hub.read(record(status="paused", fulfilled=True)).fulfilled is False
    assert hub.read(record(status="live", fulfilled=True)).fulfilled is False
    assert hub.read(record(status="live", fulfilled=False)).fulfilled is False


def _workbook(monkeypatch, tabs: dict):
    """A lead sheet with these tabs: {title: number of leads}."""
    from wilbyte import gsheets

    class Sheets:
        def __init__(self, creds):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def tabs(self, sheet):
            return [{"title": one} for one in tabs]

        def rows(self, sheet, span):
            title = span.split("!")[0].strip("'")
            return [["Name", "Email", "Phone Number"]] + [[f"L{n}", "x@example.com", "555"] for n in range(tabs[title])]

        def rows_of(self, sheet, spans):
            self.reads = getattr(self, "reads", 0) + 1
            return [self.rows(sheet, one) for one in spans]

    monkeypatch.setattr(gsheets, "SheetsClient", Sheets)
    monkeypatch.setattr(gsheets, "credentials", lambda secrets: None)


LINK = "https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcd/edit"


def test_the_leads_are_found_on_whichever_tab_they_are(monkeypatch):
    """Joevanny Astorga: "the sheet has 0" with 22 on a tab that isn't the first."""
    from wilbyte.bot import jobs

    _workbook(monkeypatch, {"Instructions": 0, "Leads": 22})
    counted, trouble = jobs._count_lead_tab(SimpleNamespace(secrets=None), LINK, "Text Verified Trucker IUL")
    assert trouble == "" and counted.rows == 22 and counted.tab == "Leads"


def test_the_tab_named_for_the_lead_type_wins(monkeypatch):
    from wilbyte.bot import jobs

    _workbook(monkeypatch, {"Vets": 40, "Trucker": 22})
    counted, _ = jobs._count_lead_tab(SimpleNamespace(secrets=None), LINK, "Text Verified Trucker IUL")
    assert counted.tab == "Trucker" and counted.rows == 22


def test_another_orders_tab_is_never_counted_as_this_ones(monkeypatch):
    """Abraham's mortgage order is not the 19 leads on his IUL tab."""
    from wilbyte.bot import jobs

    _workbook(monkeypatch, {"IUL Plus": 19, "Mortgage": 0})
    counted, _ = jobs._count_lead_tab(SimpleNamespace(secrets=None), LINK,
                                      "Text Verified Mortgage Protection Standard")
    assert counted.rows == 0


def test_short_says_which_side_its_on():
    marked_early = hub.Checked(agent=hub.read(record(delivered=19, ordered=21, progress="19/21")),
                               counted=_sheet(*["10/06/2026"] * 19))
    assert "marked fulfilled 2 short" in hub.describe(marked_early)
    lost = hub.Checked(agent=hub.read(record(delivered=22, ordered=22, progress="22/22")),
                       counted=_sheet(*["10/06/2026"] * 20))
    assert "2 the hub sent aren't on the sheet" in hub.describe(lost)


def test_the_tab_is_asked_for_as_ended_if_fulfilled_finds_nobody(monkeypatch):
    """"Nobody is on the hub's Fulfilled tab right now" - with 52 on it."""
    asked = []

    def agents(secrets, *, status="fulfilled"):
        asked.append(status)
        return [hub.read(record(status="paused"))] if status == "fulfilled" else [hub.read(record(status="ended"))]

    monkeypatch.setattr(hub, "agents", agents)
    assert [one.status for one in hub.fulfilled_tab(None)] == ["ended"]
    assert asked == ["fulfilled", "ended"]


def test_a_count_the_hub_never_moved_isnt_called_short():
    """Daniella Martinez: "hub says 0/25, the sheet has 26"."""
    one = hub.Checked(agent=hub.read(record(delivered=0, ordered=25, progress="0/25")),
                      counted=_sheet(*["10/06/2026"] * 26))
    assert one.short_by() == 0 and one.counter_off()
    assert hub.describe(one).startswith("❔") and "hub's count that's behind" in hub.describe(one)


def test_the_hub_check_paces_itself_between_sheets(monkeypatch):
    """Two sheets unread at the end of 51: "Google is rate-limiting us (429)"."""
    from wilbyte.bot import jobs

    slept = []
    monkeypatch.setattr("time.sleep", lambda s: slept.append(s))
    monkeypatch.setattr(jobs, "_count_lead_tab", lambda config, link, kind="": (_sheet("10/06/2026"), ""))
    jobs.hub_check(None, [hub.read(record(id=str(n))) for n in range(3)])
    assert slept == [jobs.HUB_SHEET_PAUSE] * 2


IAN = [
    [""], ["When YOU MAKE A SALE HERE IS THE LINK TO DISPO THE LEAD ---- LINK ---- https://x"],
    ["Name", "Email", "Phone Number", "Age", "State", "Notes", "Lead Opt In Time Stamp"],
] + [[f"Old {n}", f"o{n}@example.com", "555-010-0000", "70", "Georgia", "called", "Oct 2, 1:12:36 AM EDT"]
     for n in range(57)] + [
    ["NEW LEAD ORDER 10/05"],
] + [[f"New {n}", f"n{n}@example.com", "555-010-0001", "65", "Texas", "called", "Oct 5, 7:40:53 PM EDT"]
     for n in range(30)]


def test_the_order_is_counted_under_its_own_new_lead_order_line():
    """Ian Miller: "NEW LEAD ORDER 10/05", and the 30 under it are the hub's 30/30."""
    counted = delivery.count_rows(IAN)
    assert counted.rows == 87 and counted.this_order == 30
    assert counted.order_mark == "NEW LEAD ORDER 10/05"
    one = hub.Checked(agent=hub.read(record(name="Ian Miller", delivered=30, ordered=30, progress="30/30",
                                            start_date="2026-10-05")), counted=counted)
    assert one.on_sheet == 30 and one.short_by() == 0
    assert "under “NEW LEAD ORDER 10/05”" in hub.describe(one)


def test_a_short_order_under_its_line_is_caught():
    rows = IAN[:-3]   # 27 of the 30
    one = hub.Checked(agent=hub.read(record(delivered=30, ordered=30, progress="30/30")),
                      counted=delivery.count_rows(rows))
    assert one.short_by() == 3


def test_the_hubs_own_time_stamp_is_a_date():
    assert delivery.as_day("Oct 5, 7:20:33 PM EDT", today=date(2026, 10, 9)) == date(2026, 10, 5)
    assert delivery.as_day("Dec 30, 9:00:00 AM EST", today=date(2026, 1, 3)) == date(2025, 12, 30)


def test_a_count_past_the_order_is_held_against_the_whole_sheet():
    """Hunter Kiser, 229/40: the hub's count is everything it has sent."""
    rows = [["Name", "Email", "Phone Number", "Date Added"]]
    rows += [[f"L{n}", "x@example.com", "555", "09/01/2026"] for n in range(191)]
    rows += [[f"L{n}", "x@example.com", "555", "10/06/2026"] for n in range(40)]
    one = hub.Checked(agent=hub.read(record(delivered=229, ordered=40, progress="229/40", start_date="2026-10-05")),
                      counted=delivery.count_rows(rows))
    assert one.on_sheet == 231 and one.short_by() == 0
