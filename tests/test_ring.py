"""Replying the way Faith does: reading, drafting, and the ping to Franklin."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest

from wilbyte import ringcentral, ringtexts, smsreplies
from wilbyte.bot import jobs

NOW = datetime(2026, 9, 24, 15, 0, tzinfo=timezone.utc)
SHELBY = "+18015550142"
FAITH = "+18015550199"


def record(id_, minutes_ago, text, *, inbound=True, number=SHELBY, name=""):
    at = (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    them = {"phoneNumber": number, "name": name}
    us = {"phoneNumber": FAITH}
    return {
        "id": id_, "creationTime": at, "subject": text,
        "direction": "Inbound" if inbound else "Outbound",
        "from": them if inbound else us, "to": [us] if inbound else [them],
    }


class Reading:
    """`open_ring` as far as a catch-up goes."""

    def __init__(self, records=(), *, error=None):
        self.records, self.error, self.asked = list(records), error, []

    def texts(self, *, since, until=""):
        self.asked.append(since)
        if self.error:
            raise self.error
        return list(self.records)

    def owner(self):
        return "Arnold Tarpley"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _ringing(monkeypatch, records=(), *, error=None):
    box = Reading(records, error=error)
    monkeypatch.setattr(ringcentral, "open_ring", lambda secrets: box)
    return box


HISTORY = [
    record(1, 3000, "can you pause my leads for the weekend", name="Shelby Guest"),
    record(2, 2998, "Hi Shelby! Yes of course 😊 I'll pause them Sat and Sun. "
                    "Want them back Monday morning?", inbound=False, name="Shelby Guest"),
    record(3, 2000, "when do my leads start"),
    record(4, 1998, "Hi! Let me check with the team and get back to you shortly 😊",
           inbound=False),
]


# ----------------------------------------------------------------- reading


def test_the_first_read_goes_back_months(monkeypatch):
    box = _ringing(monkeypatch, HISTORY)

    jobs.ring_catch_up(NS(secrets=None), now=NOW)

    first = datetime.fromisoformat(box.asked[0].replace("Z", "+00:00"))
    assert (NOW - first).days == jobs.RING_FIRST_DAYS


def test_later_reads_only_ask_for_whats_new(monkeypatch):
    """A minute's watch should cost the last minute, not four months."""
    box = _ringing(monkeypatch, HISTORY)
    jobs.ring_catch_up(NS(secrets=None), now=NOW)
    jobs.ring_catch_up(NS(secrets=None), now=NOW)

    second = datetime.fromisoformat(box.asked[1].replace("Z", "+00:00"))
    newest = NOW - timedelta(minutes=1998)
    assert second == newest - timedelta(minutes=10), "no overlap, or from the start"


def test_reading_the_same_texts_twice_keeps_them_once(monkeypatch):
    _ringing(monkeypatch, HISTORY)
    jobs.ring_catch_up(NS(secrets=None), now=NOW)
    data, _ = jobs.ring_catch_up(NS(secrets=None), now=NOW)

    assert len(data["texts"]) == 4


def test_a_failed_read_says_why_and_keeps_what_was_there(monkeypatch):
    _ringing(monkeypatch, HISTORY)
    jobs.ring_catch_up(NS(secrets=None), now=NOW)
    _ringing(monkeypatch, error=ringcentral.RingError("RingCentral refused that."))

    data, problems = jobs.ring_catch_up(NS(secrets=None), now=NOW)

    assert problems == ["RingCentral refused that."]
    assert len(data["texts"]) == 4


# ----------------------------------------------------------------- waiting


def test_an_unanswered_text_is_waiting(monkeypatch):
    _ringing(monkeypatch, HISTORY + [record(5, 5, "are my leads paused?")])

    found, _data, _ = jobs.ring_waiting(NS(secrets=None), now=NOW)

    assert [[one.said for one in tail] for _a, _n, tail in found] == [
        ["are my leads paused?"]
    ]


def test_an_agent_still_typing_is_left_a_moment(monkeypatch):
    """They send "hey", then the question - and a draft answering "hey" is a
    ping about nothing."""
    _ringing(monkeypatch, HISTORY + [record(5, 0.5, "hey")])

    found, _data, _ = jobs.ring_waiting(NS(secrets=None), now=NOW)

    assert found == []


def test_a_text_already_pinged_about_is_not_pinged_again(monkeypatch):
    """A restart must not ping Franklin about the same message twice."""
    _ringing(monkeypatch, HISTORY + [record(5, 5, "are my leads paused?")])
    jobs.ring_pinged("5")

    found, _data, _ = jobs.ring_waiting(NS(secrets=None), now=NOW)

    assert found == []


def test_a_new_text_after_a_ping_is_waiting_again(monkeypatch):
    _ringing(monkeypatch, HISTORY + [record(5, 10, "are my leads paused?"),
                                     record(6, 5, "hello??")])
    jobs.ring_pinged("5")

    found, _data, _ = jobs.ring_waiting(NS(secrets=None), now=NOW)

    assert [one.said for one in found[0][2]] == ["are my leads paused?", "hello??"]


# ---------------------------------------------------------------- drafting


class Claude:
    """Stands in for Anthropic, and remembers what it was asked."""

    asked: list = []

    def __init__(self, api_key=None):
        self.messages = self

    def create(self, **kw):
        type(self).asked.append(kw)
        return NS(stop_reason="tool_use", content=[NS(
            type="tool_use", name="reply", input={
                "reply": "Hi Shelby! Yes they're paused until [date] 😊",
                "why": "She confirms and offers the date back",
                "blanks": ["date"],
            },
        )])


def _drafting(monkeypatch):
    import anthropic

    Claude.asked = []
    monkeypatch.setattr(anthropic, "Anthropic", Claude)
    return NS(secrets=NS(anthropic_api_key="k", require=lambda *a: None),
              copy=NS(model="claude-test"))


def _done():
    texts = [one for one in (smsreplies.from_record(r) for r in HISTORY) if one]
    return smsreplies.exchanges(texts)


def test_a_draft_is_built_from_her_own_replies(monkeypatch):
    config = _drafting(monkeypatch)

    got = jobs.draft_like_faith(config, asked="are my leads paused?", done=_done())

    prompt = Claude.asked[0]["messages"][0]["content"]
    assert "I'll pause them Sat and Sun" in prompt, "her replies weren't shown"
    assert "are my leads paused?" in prompt
    assert got["reply"].startswith("Hi Shelby!")
    assert got["blanks"] == ["date"]


def test_it_is_told_to_invent_nothing(monkeypatch):
    """A reply telling an agent a launch date nobody agreed to reads exactly
    as confident as a right one."""
    config = _drafting(monkeypatch)
    jobs.draft_like_faith(config, asked="when do I go live", done=_done())

    system = Claude.asked[0]["system"]
    assert "Never state a fact" in system
    assert "bracketed blank" in system
    assert "Nothing you write is sent" in system


def test_with_none_of_her_replies_it_will_not_draft_in_its_own_voice(monkeypatch):
    config = _drafting(monkeypatch)

    with pytest.raises(ValueError) as said:
        jobs.draft_like_faith(config, asked="are my leads paused?", done=[])

    assert "not hers" in str(said.value)
    assert Claude.asked == []


# -------------------------------------------------------------- the ping


class Heard:
    def __init__(self):
        self.said = []

    async def send(self, content=None, **kw):
        self.said.append(str(content or ""))


def _once(monkeypatch, *, drafted=None, problems=(), found=True, ready=True):
    from wilbyte.bot import client

    # Already said it's connected, unless the test is about saying so.
    monkeypatch.setattr(client, "_RING_READY", [True] if ready else [])
    heard = Heard()
    texts = [one for one in (smsreplies.from_record(r) for r in HISTORY) if one]
    tail = [smsreplies.from_record(record(5, 5, "are my leads paused?"))]
    data = {"texts": [one.as_dict() for one in texts + tail], "pinged": []}
    marked = []
    monkeypatch.setattr(
        jobs, "ring_waiting",
        lambda cfg: ([("8015550142", "Shelby Guest", tail)] if found else [],
                     data, list(problems)),
    )
    monkeypatch.setattr(jobs, "ring_pinged", lambda mid: marked.append(mid))

    def drafting(cfg, **kw):
        if isinstance(drafted, Exception):
            raise drafted
        return drafted or {"reply": "Hi Shelby! Yes 😊", "why": "like her",
                           "blanks": [], "examples": 2}

    monkeypatch.setattr(jobs, "draft_like_faith", drafting)
    monkeypatch.setattr(client, "_ring_responder", lambda bot: heard)
    bot = NS(config=NS(secrets=NS(discord_notify_user_id="42"),
                       schedule=NS(timezone="America/Chicago")))
    asyncio.run(client._ring_once(bot))
    return heard.said, marked


def test_franklin_is_pinged_with_the_text_and_her_reply(monkeypatch):
    said, marked = _once(monkeypatch)
    (note,) = said

    assert note.startswith("<@42>")
    assert "Shelby Guest" in note and "(801) 555-0142" in note
    assert "> are my leads paused?" in note
    assert "```\nHi Shelby! Yes 😊\n```" in note
    assert marked == ["5"]


def test_a_draft_that_failed_is_tried_again_rather_than_forgotten(monkeypatch):
    said, marked = _once(monkeypatch, drafted=RuntimeError("overloaded"))

    assert said == [] and marked == []


def test_what_to_fill_in_is_said(monkeypatch):
    said, _ = _once(monkeypatch, drafted={
        "reply": "They're paused until [date]", "why": "", "blanks": ["date"],
    })

    assert "fill in date" in said[0]


def test_a_draft_cannot_break_out_of_its_box(monkeypatch):
    """Three backticks in a draft would close the block and spill the rest."""
    said, _ = _once(monkeypatch, drafted={
        "reply": "use ``` this", "why": "", "blanks": [],
    })

    body = said[0].split("**Reply like Faith:**\n", 1)[1]
    assert body.count("```") == 2


def test_a_setup_problem_is_said_once_not_every_minute(monkeypatch):
    from wilbyte.bot import client

    client._RING_SAID.clear()
    first, _ = _once(monkeypatch, problems=["RingCentral refused that."], found=False)
    second, _ = _once(monkeypatch, problems=["RingCentral refused that."], found=False)

    assert first == ["⚠ RingCentral: RingCentral refused that."]
    assert second == []


def test_nothing_is_read_until_ringcentral_is_set_up(monkeypatch):
    from wilbyte.bot import client

    asked = []
    monkeypatch.setattr(client, "_ring_once", lambda bot: asked.append(1))
    monkeypatch.setattr(client, "RING_CHECK_SECONDS", 0)

    class Bot:
        config = NS(secrets=NS(ringcentral_client_id="", ringcentral_client_secret="",
                               ringcentral_jwt="", ringcentral_extension=""))
        ticks = 0

        def is_closed(self):
            type(self).ticks += 1
            return type(self).ticks > 2

    asyncio.run(client.ring_loop(Bot()))

    assert asked == []


# ------------------------------------------------- which channel, by name


def _channel(name, *, cid=1, send=True, view=True):
    import discord

    guild = NS(me=object())
    one = NS(id=cid, name=name, type=discord.ChannelType.text, guild=guild)
    one.permissions_for = lambda me: NS(view_channel=view, send_messages=send)
    return one


def _bot(wanted, channels):
    return NS(
        config=NS(secrets=NS(ringcentral_channel_id=wanted)),
        get_all_channels=lambda: list(channels),
        get_channel=lambda cid: next((one for one in channels if one.id == cid), None),
    )


def test_the_channel_can_be_named_the_way_it_reads():
    """"#ryte-responder" is what Franklin sees. An id means Developer Mode."""
    from wilbyte.bot import client

    theirs = _channel("ryte-responder", cid=7)
    got, trouble = client._ring_channel(_bot("#ryte-responder", [
        _channel("general", cid=1), theirs,
    ]))

    assert got is theirs and trouble == ""


def test_or_by_its_id():
    from wilbyte.bot import client

    theirs = _channel("ryte-responder", cid=7)
    got, trouble = client._ring_channel(_bot("7", [theirs]))

    assert got is theirs and trouble == ""


def test_a_private_channel_it_cannot_post_in_is_said_not_swallowed():
    """Every ping would fail, and from where Franklin sits that is the same
    as no agent having texted."""
    from wilbyte.bot import client

    got, trouble = client._ring_channel(_bot("ryte-responder", [
        _channel("ryte-responder", send=False),
    ]))

    assert got is None
    assert "not allowed to post in #ryte-responder" in trouble
    assert "Send Messages" in trouble


def test_a_channel_it_cannot_see_at_all_says_to_give_it_access():
    from wilbyte.bot import client

    got, trouble = client._ring_channel(_bot("ryte-responder", [_channel("general")]))

    assert got is None
    assert "give my role access" in trouble


def test_two_channels_with_the_name_are_not_guessed_between():
    from wilbyte.bot import client

    got, trouble = client._ring_channel(_bot("ryte-responder", [
        _channel("ryte-responder", cid=1), _channel("ryte-responder", cid=2),
    ]))

    assert got is None and "2 channels" in trouble


def test_nothing_set_is_the_board_channel_and_no_complaint():
    from wilbyte.bot import client

    assert client._ring_channel(_bot("", [])) == (None, "")


def test_the_channel_problem_is_said_once_where_the_pings_went(monkeypatch):
    from wilbyte.bot import client

    client._RING_SAID.clear()
    heard = Heard()
    monkeypatch.setattr(jobs, "ring_waiting", lambda cfg: ([], {"texts": []}, []))
    monkeypatch.setattr(client, "_ring_responder", lambda bot: heard)
    monkeypatch.setattr(
        client, "_ring_channel",
        lambda bot: (None, "I'm not allowed to post in #ryte-responder."),
    )
    for _ in range(2):
        asyncio.run(client._ring_once(NS(config=None)))

    assert heard.said == ["⚠ RingCentral: I'm not allowed to post in #ryte-responder."]



# ------------------------------------------- saying it's connected, once


def test_a_working_connection_says_so_once(monkeypatch):
    """With no agent waiting, a working watcher and a broken one are otherwise
    equally silent."""
    from wilbyte.bot import client

    said, _ = _once(monkeypatch, found=False, ready=False)

    assert len(said) == 1
    assert said[0].startswith("📱 Connected to RingCentral")
    assert "2 of Faith's replies" in said[0]
    assert "can't text anybody" in said[0]
    assert client._RING_READY, "would say it again next minute"


def test_it_is_not_said_again_the_next_minute(monkeypatch):
    said, _ = _once(monkeypatch, found=False, ready=True)

    assert said == []


def test_it_is_not_said_when_something_is_wrong(monkeypatch):
    """"Connected" beside a refusal would be two answers to one question."""
    said, _ = _once(monkeypatch, found=False, ready=False,
                    problems=["RingCentral refused that."])

    assert not any("Connected" in one for one in said)


def test_connected_to_somebody_with_no_replies_says_check_the_extension():
    """The wrong extension connects perfectly well."""
    from wilbyte.bot import client

    said = client._ring_ready(40, 0)

    assert "none of them are Faith answering" in said
    assert "RINGCENTRAL_EXTENSION" in said


def test_half_set_up_names_what_is_missing():
    """Some set is somebody who meant to and got a name wrong."""
    from wilbyte.bot import client

    missing = client._ring_half_set(NS(
        ringcentral_client_id="a", ringcentral_client_secret="b",
        ringcentral_jwt="", ringcentral_extension="103",
    ))

    assert missing == ["RINGCENTRAL_JWT"]


def test_not_using_it_at_all_is_nobodys_business():
    from wilbyte.bot import client

    blank = NS(ringcentral_client_id="", ringcentral_client_secret="",
               ringcentral_jwt="", ringcentral_extension="")

    assert client._ring_half_set(blank) == []


def test_half_set_up_is_said_once_by_the_loop(monkeypatch):
    from wilbyte.bot import client

    client._RING_SAID.clear()
    heard = Heard()
    monkeypatch.setattr(client, "_ring_responder", lambda bot: heard)
    monkeypatch.setattr(client, "RING_CHECK_SECONDS", 0)

    class Bot:
        config = NS(secrets=NS(ringcentral_client_id="a", ringcentral_client_secret="b",
                               ringcentral_jwt="", ringcentral_extension="103"))
        ticks = 0

        def is_closed(self):
            type(self).ticks += 1
            return type(self).ticks > 3

    asyncio.run(client.ring_loop(Bot()))

    assert heard.said == ["⚠ RingCentral is half set up — missing RINGCENTRAL_JWT in .env."]


# ------------------------------------------------ the team, in the draft


def test_the_line_owner_is_remembered_from_the_read(monkeypatch):
    _ringing(monkeypatch, HISTORY)

    data, _ = jobs.ring_catch_up(NS(secrets=None), now=NOW)

    assert data["owner"] == "Arnold Tarpley"


def test_more_colleagues_can_be_named(monkeypatch):
    """Anybody else who texts into the line from their own number."""
    data = {"owner": "Arnold Tarpley", "texts": [
        {"id": "1", "at": "2026-09-24", "inbound": True, "agent": "1",
         "name": "Tre Tarpley", "said": "faith take care of this one"},
        {"id": "2", "at": "2026-09-24", "inbound": True, "agent": "2",
         "name": "Adrian Pacheco", "said": "hi"},
    ]}

    texts = jobs.ring_texts(NS(secrets=NS(ringcentral_team="Tre Tarpley, Andrea")), data)

    assert [one.team for one in texts] == [True, False]


def test_the_draft_is_told_which_lines_are_the_team(monkeypatch):
    config = _drafting(monkeypatch)
    handed = smsreplies.mark_team([
        smsreplies.Text(id="1", at="1", inbound=True, agent="9", name="Arnold Tarpley",
                        said="With Wolfpack so take care of him"),
        smsreplies.Text(id="2", at="2", inbound=True, agent="8", name="Adrian Pacheco",
                        said="👍"),
    ], ["Arnold Tarpley"])

    jobs.draft_like_faith(config, asked="👍", done=_done(), thread=handed)

    prompt = Claude.asked[0]["messages"][0]["content"]
    assert "Team: With Wolfpack so take care of him" in prompt
    assert "Agent: 👍" in prompt
    assert "Lines marked Team are colleagues" in prompt
