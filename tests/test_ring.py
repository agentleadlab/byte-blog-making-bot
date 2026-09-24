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

    def __init__(self, records=(), *, error=None, numbers_fail=False):
        self.records, self.error, self.asked = list(records), error, []
        self.numbers_fail = numbers_fail

    def texts(self, *, since, until=""):
        self.asked.append(since)
        if self.error:
            raise self.error
        return list(self.records)

    def owner(self):
        return "Arnold Tarpley"

    def own_numbers(self):
        if self.numbers_fail:
            raise ringcentral.RingError("refused")
        return ["+18785550100", "+14125550177"]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _ringing(monkeypatch, records=(), *, error=None, numbers_fail=False):
    box = Reading(records, error=error, numbers_fail=numbers_fail)
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



def test_the_lines_own_numbers_are_remembered(monkeypatch):
    """Both "Arnold Tarpley (me)" in the app."""
    _ringing(monkeypatch, HISTORY)

    data, _ = jobs.ring_catch_up(NS(secrets=None), now=NOW)

    assert data["numbers"] == ["+18785550100", "+14125550177"]


def test_not_being_able_to_list_them_does_not_stop_the_read(monkeypatch):
    """Worth having, not worth losing the texts over."""
    _ringing(monkeypatch, HISTORY, numbers_fail=True)

    data, problems = jobs.ring_catch_up(NS(secrets=None), now=NOW)

    assert problems == [] and len(data["texts"]) == 4


def test_a_text_from_the_lines_other_number_is_the_team_even_unnamed(monkeypatch):
    """RingCentral named the (412) number here, but a text that comes back
    without a name must not become an agent waiting on a reply."""
    data = {"owner": "Arnold Tarpley", "numbers": ["+14125550177"], "texts": [
        {"id": "1", "at": "1", "inbound": True, "agent": "4125550177",
         "name": "", "said": "With Wolfpack so take care of him"},
    ]}

    (text,) = jobs.ring_texts(NS(secrets=None), data)

    assert text.team is True



def test_faiths_number_can_be_said_outright(monkeypatch):
    """RINGCENTRAL_FAITH_NUMBER wins over working it out."""
    data = {"texts": [
        {"id": str(at), "at": str(at), "inbound": False, "agent": "5",
         "name": "", "said": "hi", "sender": "4125550177"} for at in range(5)
    ] + [
        {"id": "9", "at": "9", "inbound": False, "agent": "5",
         "name": "", "said": "hi", "sender": "8785550100"},
    ]}

    worked_out = jobs.ring_texts(NS(secrets=None), data)
    said_so = jobs.ring_texts(
        NS(secrets=NS(ringcentral_team="", ringcentral_faith_number="(878) 555-0100")),
        data,
    )

    assert [one.team for one in worked_out][-1] is True
    assert [one.team for one in said_so][:5] == [True] * 5
    assert said_so[-1].team is False



def test_texts_read_before_senders_were_kept_are_read_again_once(monkeypatch):
    """Without the sender, Tre's texts and Faith's cannot be told apart, and
    the months already read would go on teaching Tre's writing as hers."""
    old = {"texts": [
        {"id": "1", "at": "2026-09-20T10:00:00.000Z", "inbound": False,
         "agent": "5", "name": "", "said": "hi"},
    ], "pinged": []}
    ringtexts.save(old)
    box = _ringing(monkeypatch, HISTORY)

    jobs.ring_catch_up(NS(secrets=None), now=NOW)
    jobs.ring_catch_up(NS(secrets=None), now=NOW)

    first = datetime.fromisoformat(box.asked[0].replace("Z", "+00:00"))
    second = datetime.fromisoformat(box.asked[1].replace("Z", "+00:00"))
    assert (NOW - first).days == jobs.RING_FIRST_DAYS, "not read again from the start"
    assert (NOW - second).days < 5, "read from the start every time"


# ------------------------------------------ "@Ryte respond" + a screenshot


class Shot:
    def __init__(self, name="thread.png", data=b"png", size=100, fails=False):
        self.filename, self.data, self.size, self.fails = name, data, size, fails

    async def read(self):
        if self.fails:
            raise RuntimeError("Discord said no")
        return self.data


ADRIAN_SHOT = [
    {"side": "team", "name": "Arnold Tarpley", "text": "With Wolfpack so take care of him"},
    {"side": "team", "name": "Arnold Tarpley",
     "text": "Hi faith this is Adrian Pacheco paid for OTP Trucker IUL leads"},
    {"side": "agent", "name": "Adrian Pacheco", "text": "👍"},
]


def _responding(monkeypatch, *, said="", shots=(), read=ADRIAN_SHOT, history=True,
                read_fails=False):
    from wilbyte.bot import client

    heard, asked = Heard(), {}
    if history:
        _ringing(monkeypatch, HISTORY)
        jobs.ring_catch_up(NS(secrets=None), now=NOW)

    def reading(cfg, got, *, line_name=""):
        asked["shots"], asked["line_name"] = got, line_name
        if read_fails:
            raise RuntimeError("overloaded")
        return list(read)

    def drafting(cfg, **kw):
        asked["draft"] = kw
        return {"reply": "Welcome Adrian! 😊", "why": "like her welcomes", "blanks": []}

    monkeypatch.setattr(jobs, "read_texts_off", reading)
    monkeypatch.setattr(jobs, "draft_like_faith", drafting)
    config = NS(secrets=NS(ringcentral_client_id="", ringcentral_client_secret="",
                           ringcentral_jwt="", ringcentral_extension=""))
    asyncio.run(client._respond_like_faith(
        heard, config, NS(attachments=list(shots)), said,
    ))
    return heard.said, asked


def test_a_screenshot_is_read_and_answered_as_faith(monkeypatch):
    said, asked = _responding(monkeypatch, shots=[Shot()])

    assert asked["shots"] == [("thread.png", b"png")]
    assert asked["draft"]["asked"] == "👍"
    assert asked["draft"]["name"] == "Adrian Pacheco"
    assert "Reply like Faith to Adrian Pacheco" in said[0]
    assert "```\nWelcome Adrian! 😊\n```" in said[0]


def test_tres_handover_in_the_screenshot_is_context_not_the_question(monkeypatch):
    """The draft sees it, labelled, so it knows Adrian is new and what he
    bought - but it is not what Adrian asked."""
    _said, asked = _responding(monkeypatch, shots=[Shot()])

    thread = asked["draft"]["thread"]
    assert [one.team for one in thread] == [True, True, False]
    assert "Wolfpack" not in asked["draft"]["asked"]


def test_the_lines_own_name_is_what_the_reader_is_told_is_the_team(monkeypatch):
    _said, asked = _responding(monkeypatch, shots=[Shot()])

    assert asked["line_name"] == "Arnold Tarpley"


def test_a_screenshot_faith_already_answered_says_so(monkeypatch):
    said, asked = _responding(monkeypatch, shots=[Shot()], read=ADRIAN_SHOT + [
        {"side": "faith", "name": "", "text": "Welcome Adrian!"},
    ])

    assert "already answered" in said[0]
    assert "draft" not in asked


def test_the_agents_words_can_be_pasted_instead(monkeypatch):
    said, asked = _responding(monkeypatch, said="can you pause my leads")

    assert asked["draft"]["asked"] == "can you pause my leads"
    assert "shots" not in asked


def test_nothing_sent_says_how(monkeypatch):
    said, _ = _responding(monkeypatch, history=False)

    assert "screenshot" in said[0] and "@Ryte respond" in said[0]


def test_a_screenshot_too_big_to_read_is_said(monkeypatch):
    said, _ = _responding(
        monkeypatch, shots=[Shot(size=jobs.RING_SHOT_BYTES + 1)], history=False,
    )

    assert "over 5MB" in said[0]


def test_only_pictures_are_read(monkeypatch):
    said, asked = _responding(
        monkeypatch, shots=[Shot(name="notes.pdf"), Shot(name="shot.JPG")],
    )

    assert asked["shots"] == [("shot.JPG", b"png")]


def test_a_screenshot_that_could_not_be_read_says_so(monkeypatch):
    said, asked = _responding(monkeypatch, shots=[Shot()], read_fails=True)

    assert "Couldn't read that screenshot" in said[0]
    assert "draft" not in asked


def test_with_none_of_her_replies_it_still_will_not_draft(monkeypatch):
    said, asked = _responding(monkeypatch, shots=[Shot()], history=False)

    assert "not hers" in said[0]
    assert "shots" not in asked


def test_respond_is_read_before_anything_else_again():
    from wilbyte.bot import mentions

    asked = mentions.parse("<@1> respond when do I go live? check my leads")

    assert asked.action == "respond"
    assert asked.brief == "when do I go live? check my leads"


def test_respond_only_counts_as_the_first_word_again():
    from wilbyte.bot import mentions

    assert mentions.parse("<@1> email about how to respond to agents").action == "write"


# ----------------------------------------------- reading the screenshot


def test_the_screenshot_goes_to_claude_as_a_picture(monkeypatch):
    config = _drafting(monkeypatch)
    real = Claude.create

    def conversation(self, **kw):
        Claude.asked.append(kw)
        return NS(stop_reason="tool_use", content=[NS(
            type="tool_use", name="conversation", input={"messages": [
                {"side": "team", "name": "Arnold Tarpley", "text": "take care of him"},
                {"side": "agent", "name": "Adrian Pacheco", "text": " 👍 "},
                {"side": "nonsense", "name": "", "text": "dropped"},
                {"side": "agent", "name": "", "text": "  "},
            ]},
        )])

    monkeypatch.setattr(Claude, "create", conversation)
    got = jobs.read_texts_off(config, [("thread.png", b"\x89PNG")],
                              line_name="Arnold Tarpley")
    monkeypatch.setattr(Claude, "create", real)

    content = Claude.asked[0]["messages"][0]["content"]
    assert content[0]["type"] == "image"
    assert content[0]["source"]["media_type"] == "image/png"
    assert '"Arnold Tarpley"' in content[-1]["text"]
    assert got == [
        {"side": "team", "name": "Arnold Tarpley", "text": "take care of him"},
        {"side": "agent", "name": "Adrian Pacheco", "text": "👍"},
    ]


def test_no_picture_is_not_sent_to_be_read(monkeypatch):
    config = _drafting(monkeypatch)

    with pytest.raises(ValueError):
        jobs.read_texts_off(config, [("thread.png", b"")])

    assert Claude.asked == []


# ------------------------------------------- RYTE may talk in that channel


def _allowed(channel_id, name, ring):
    from wilbyte.bot import client

    return client.is_allowed(
        channel_id=channel_id, user=NS(roles=[]), channel_name=name,
        config=NS(secrets=NS(discord_channel_ids=["1"], discord_sop_channel_ids=[],
                             discord_role_ids=[], ringcentral_channel_id=ring)),
    )[0]


def test_the_suggestions_channel_is_one_ryte_talks_in():
    """"@Ryte respond" there went unanswered: named for RYTE to post in, and
    missing from the list of channels it answers in."""
    assert _allowed(1359897310242279596, "ryte-responder", "1359897310242279596")
    assert _allowed(555, "ryte-responder", "ryte-responder")
    assert _allowed(555, "ryte-responder", "#ryte-responder")


def test_every_other_channel_is_as_it_was():
    assert _allowed(1, "general", "ryte-responder")
    assert not _allowed(2, "general", "ryte-responder")
    assert not _allowed(2, "general", "")
    # An id is matched as an id, never against a channel's name.
    assert not _allowed(2, "555", "555")
