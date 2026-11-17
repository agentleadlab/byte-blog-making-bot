"""“caleb keys aged leads sheet” - Ryte looked for a card named “caleb keys
aged leads” and found nobody."""

import json

from wilbyte import agedqc, agents
from wilbyte.bot import client


def test_order_words_are_not_the_name():
    assert agents.who_wants_sheets("caleb keys aged leads sheet") == ["caleb keys"]
    assert agents.who_wants_sheets("aged sheet for Caleb Keys") == ["Caleb Keys"]
    assert agents.who_wants_sheets("fresh leads sheet for Jay Rodriguez") == ["Jay Rodriguez"]
    assert agents.who_wants_sheets("sheet for Faith") == ["Faith"]


def test_aged_sheets_come_off_novas_pulls(tmp_path, monkeypatch):
    path = tmp_path / "aged.json"
    path.write_text(json.dumps({
        "1": {"client": "Caleb Keys", "invoice": "INV-1", "sheets": ["https://docs.google.com/spreadsheets/d/old"],
              "at": "2026-11-01T15:00:00+00:00"},
        "2": {"client": "Caleb Keys", "invoice": "INV-2", "sheets": ["https://docs.google.com/spreadsheets/d/new"],
              "at": "2026-11-15T15:00:00+00:00"},
        "3": {"client": "Someone Else", "sheets": ["https://docs.google.com/spreadsheets/d/x"],
              "at": "2026-11-16T15:00:00+00:00"},
    }))
    monkeypatch.setattr(agedqc, "PATH", path)
    found = client._aged_sheets_for("caleb keys aged leads sheet")
    assert len(found) == 2
    assert "/d/new" in found[0] and "/d/old" in found[1]
    assert not any("/d/x" in one for one in found)
    assert client._aged_sheets_for("nobody aged leads sheet") == []
