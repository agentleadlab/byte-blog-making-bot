"""Live on the hub, owed leads, and none in three days - Bryce Evans'
recurring Spark order went from Oct 31 to Nov 9 and nothing said so."""

from datetime import date, datetime, timedelta, timezone

from wilbyte import ledger, stalls
from wilbyte.hub import HubAgent

NOW = datetime(2026, 11, 9, 22, 0, tzinfo=timezone.utc)


def _row(**over):
    return HubAgent(**{"id": "2301", "name": "Bryce Evans", "status": "live", "lead_type": "Text Verified Veteran Plus",
                       "delivered": 12, "ordered": 30, "start_date": date(2026, 10, 20), **over})


def test_a_live_row_that_stops_is_listed_after_three_days(tmp_path):
    path = tmp_path / "s.json"
    assert stalls.watch([_row()], now=NOW - timedelta(days=4), path=path) == []
    assert stalls.watch([_row()], now=NOW - timedelta(days=2), path=path) == []
    found = stalls.watch([_row()], now=NOW, today=NOW.date(), path=path)
    assert [agent.name for agent, _since in found] == ["Bryce Evans"]
    line = stalls.lines(found, now=NOW)[0]
    assert "no new leads in 4 days" in line and "12 of 30 delivered" in line and "row 2301" in line


def test_a_lead_going_out_starts_the_clock_again(tmp_path):
    path = tmp_path / "s.json"
    stalls.watch([_row()], now=NOW - timedelta(days=4), path=path)
    assert stalls.watch([_row(delivered=13)], now=NOW - timedelta(days=1), path=path) == []
    assert stalls.watch([_row(delivered=13)], now=NOW, path=path) == []


def test_the_hubs_own_last_lead_time_is_used_when_it_has_one(tmp_path):
    row = _row(raw={"last_lead_at": "2026-10-31T15:00:00Z"})
    found = stalls.watch([row], now=NOW, today=NOW.date(), path=tmp_path / "s.json")
    assert found and stalls.lines(found, now=NOW)[0].count("no new leads in 9 days") == 1


def test_not_live_done_or_not_started_isnt_stalled(tmp_path):
    rows = [_row(id="1", status="paused", raw={"last_lead_at": "2026-10-01T00:00:00Z"}),
            _row(id="2", delivered=30, raw={"last_lead_at": "2026-10-01T00:00:00Z"}),
            _row(id="3", start_date=date(2026, 11, 12), raw={"last_lead_at": "2026-10-01T00:00:00Z"})]
    assert stalls.watch(rows, now=NOW, today=NOW.date(), path=tmp_path / "s.json") == []


def test_the_list_says_it():
    said = ledger.daily({}, now=NOW, today=NOW.date(), through=NOW.date() + timedelta(days=1),
                        stalled=["• **Bryce Evans** — live, no new leads in 9 days (row 2301)"])
    assert "🛑 Live, no leads in 3+ days** (1)" in said and "Bryce Evans" in said


def test_a_spark_post_ryte_cant_read_is_on_the_list(monkeypatch):
    # Bryce Evans paid, nobody gavelled it - a #spark post that isn't a new
    # member or a state change is kept, not passed over.
    from types import SimpleNamespace

    from wilbyte.bot import client

    renewal = SimpleNamespace(id=77, channel=SimpleNamespace(name="⚡│ꜱᴘᴀʀᴋ"), created_at=NOW,
                              jump_url="https://discord.com/channels/1/2/77", content="Membership renewed on Spark: "
                              "Bryce Evans, Veteran pool, paid through Nov 16", embeds=[], attachments=[])
    member = SimpleNamespace(id=78, channel=SimpleNamespace(name="spark"), created_at=NOW,
                             jump_url="https://discord.com/channels/1/2/78",
                             content="New lead pool member on Spark", embeds=[], attachments=[])
    assert client._spark_unread(renewal)
    assert not client._spark_unread(member)
    assert not client._spark_unread(SimpleNamespace(id=1, channel=SimpleNamespace(name="general"),
                                                    content="x", embeds=[], attachments=[]))
    client._spark_keep_unread(renewal)
    lines = client._spark_unread_lines()
    assert len(lines) == 1 and "Bryce Evans" in lines[0] and "77" in lines[0]
    said = ledger.daily({}, now=NOW, today=NOW.date(), through=NOW.date(), spark=lines)
    assert "⚡ #spark posts nobody's acted on** (1)" in said
