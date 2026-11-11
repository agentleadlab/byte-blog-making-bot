""""ryte needs to know that excel is down" - every card got "Google said no to
that sheet (403)", Therese, Nicole and Franklin tagged on each."""

import asyncio
from types import SimpleNamespace

from wilbyte import sheetsdown
from wilbyte.bot import client, jobs


def test_three_sheets_refused_is_an_outage_said_once_and_its_end_too():
    sheetsdown.failed("a", now=0)
    sheetsdown.failed("a", now=10)
    sheetsdown.failed("b", now=20)
    assert not sheetsdown.down(), "the same sheet twice is that sheet"
    sheetsdown.failed("c", now=30)
    assert sheetsdown.down()
    assert [state for state, _ in sheetsdown.to_say()] == ["down"]
    sheetsdown.failed("d", now=40)
    assert sheetsdown.to_say() == [], "said once"
    sheetsdown.opened()
    assert not sheetsdown.down() and [state for state, _ in sheetsdown.to_say()] == ["up"]


def test_spread_out_failures_arent_an_outage():
    for at, one in ((0, "a"), (30 * 60, "b"), (60 * 60, "c")):
        sheetsdown.failed(one, now=at)
    assert not sheetsdown.down()


def test_while_down_a_payments_sheet_isnt_its_problem(monkeypatch):
    said = []

    class Channel:
        async def send(self, text):
            said.append(text)

    async def cards(_bot, _kind):
        return Channel()

    monkeypatch.setattr(client, "_card_channel", cards)
    monkeypatch.setattr(jobs, "lead_sheet_rows", lambda config, url: (None, "couldn't open it (403)"))
    for one in ("a", "b", "c"):
        sheetsdown.failed(one)
    bot = SimpleNamespace(config=None)
    rows, problem = asyncio.run(client._sqc_sheet(bot, "https://docs.google.com/spreadsheets/d/zzz"))
    assert (rows, problem) == (None, "")
    assert len(said) == 1 and "Google Sheets is refusing me on every sheet" in said[0]


def test_the_off_button(tmp_path, monkeypatch):
    from wilbyte import prefs
    from wilbyte.bot.mentions import parse

    monkeypatch.setattr(prefs, "PREFS_PATH", tmp_path / "prefs.json")
    for said, how in (("<@1> sheets off", "off"), ("<@1> stop flagging the sheets", "off"),
                      ("<@1> turn off sheet checks", "off"), ("<@1> sheets on", "on"),
                      ("<@1> sheets back on", "on")):
        request = parse(said)
        assert (request.action, request.brief) == ("sheetsswitch", how), said
    sheetsdown.switch(True)
    assert sheetsdown.switched_off()
    monkeypatch.setattr(jobs, "lead_sheet_rows", lambda config, url: (_ for _ in ()).throw(AssertionError("read")))
    assert asyncio.run(client._sqc_sheet(SimpleNamespace(config=None), "https://x")) == (None, "")
    sheetsdown.switch(False)
    assert not sheetsdown.switched_off()
