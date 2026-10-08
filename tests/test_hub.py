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
    assert line.startswith("⚠") and "hub says 26/26" in line and "**3 short**" in line


def test_leads_from_an_earlier_order_on_the_same_sheet_dont_count():
    """A reorder reuses the sheet. Only rows dated since this order started
    are this order's."""
    one = hub.Checked(agent=hub.read(record()),
                      counted=_sheet(*(["09/01/2026"] * 26 + ["10/06/2026"] * 20)))
    assert one.on_sheet == 20 and one.short_by() == 6


def test_an_undated_sheet_is_counted_whole_and_says_so():
    rows = [["Name", "Email", "Phone Number"]] + [[f"L{n}", "x@example.com", "555"] for n in range(26)]
    one = hub.Checked(agent=hub.read(record()), counted=delivery.count_rows(rows))
    assert one.on_sheet == 26
    assert "doesn't date its leads" in hub.describe(one)


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
    monkeypatch.setattr(hub, "agents", lambda secrets: found)

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
    assert "Short Agent" in message and "6 short" in message
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
