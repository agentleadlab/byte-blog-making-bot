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


def flat(content=None, embed=None) -> str:
    """A message as text - what it says, card and all - for asserting on."""
    parts = [str(content or "")]
    if embed is not None:
        parts += [embed.author.name or "", embed.title or "", embed.description or ""]
        parts += [f"{one.name}: {one.value}" for one in embed.fields]
        parts += [embed.footer.text or ""]
    return "\n".join(one for one in parts if one)


class Heard:
    def __init__(self):
        self.said = []
        self.cards = []

    async def send(self, content=None, **kw):
        self.cards.append(kw.get("embed"))
        self.said.append(flat(content, kw.get("embed")))


def _once(monkeypatch, *, drafted=None, problems=(), found=True, ready=True,
          card=None):
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
    def pinged(mid, *, pending=None):
        marked.append(mid)
        pinged.pending = pending

    monkeypatch.setattr(jobs, "ring_pinged", pinged)
    monkeypatch.setattr(
        jobs, "ring_context",
        lambda cfg, data, texts, **kw: {"card": card, "history": [], "lessons": [],
                                        "playbook": ""},
    )

    async def no_playbook(*a, **kw):
        return None

    monkeypatch.setattr(client, "_write_the_playbook", no_playbook)
    monkeypatch.setattr(client, "_study", no_playbook)

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

    assert "✏️ Fill in: date" in said[0]


def test_a_draft_cannot_break_out_of_its_box(monkeypatch):
    """Three backticks in a draft would close the block and spill the rest."""
    said, _ = _once(monkeypatch, drafted={
        "reply": "use ``` this", "why": "", "blanks": [],
    })

    body = said[0].split("**Reply like Faith**\n", 1)[1]
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
        lambda bot, setting="ringcentral_channel_id": (
            (None, "I'm not allowed to post in #ryte-responder.")
            if setting == "ringcentral_channel_id" else (None, "")
        ),
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


# ---------------------------------------------- the agent's card, by number


class Board:
    """Trello, as far as finding an agent's card goes."""

    def __init__(self, cards, comments=None, *, fails=False):
        self.cards, self.comments, self.fails = cards, comments or {}, fails

    def board_cards(self, board_id, *, archived=False):
        if self.fails:
            raise RuntimeError("Trello is down")
        return list(self.cards)

    def board_lists(self, board_id):
        return [{"id": "L-done", "name": "Done"}, {"id": "L-que", "name": "In Que"}]

    def card_comments(self, card_id):
        return list(self.comments.get(card_id, []))

    def close(self):
        pass


ADRIAN_CARD = {
    "id": "c-adrian", "name": "New Agent - Adrian Pacheco", "idList": "L-que",
    "shortUrl": "https://trello.com/c/adrian", "dateLastActivity": "2026-09-24T12:00",
    "desc": "Phone: +1 (312) 555-0188\nLead Type: OTP Trucker IUL\nLive Friday September 26",
}


def _board(monkeypatch, cards, comments=None, *, fails=False):
    board = Board(cards, comments, fails=fails)
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)
    return NS(secrets=NS(trello_board_id="b"))


def test_the_agent_is_found_by_the_number_texting(monkeypatch):
    config = _board(monkeypatch, [
        {"id": "x", "name": "💎 General 09/24/26", "desc": "call 312-555-0188"},
        ADRIAN_CARD,
    ], {"c-adrian": [
        "✅ OTP TRUCKER IUL ON DISTRO HUB setup is complete for ADRIAN PACHECO",
        "Sheet link: https://docs.google.com/spreadsheets/d/abc/edit",
    ]})

    card = jobs.agent_on_the_board(config, "+13125550188")

    assert card["agent"] == "Adrian Pacheco"
    assert card["list"] == "In Que"
    assert "OTP Trucker IUL" in card["desc"]
    assert card["setup"].startswith("✅ OTP TRUCKER IUL")
    assert card["sheet"].startswith("https://docs.google.com/spreadsheets/d/abc")


def test_by_name_when_the_card_has_no_number_on_it(monkeypatch):
    config = _board(monkeypatch, [{**ADRIAN_CARD, "desc": "Lead Type: OTP Trucker IUL"}])

    card = jobs.agent_on_the_board(config, "+19995550000", "Adrian Pacheco")

    assert card and card["agent"] == "Adrian Pacheco"


def test_a_client_who_ordered_again_is_read_from_the_newest_card(monkeypatch):
    """A repeat client keeps the old card as well, and the old order is not
    what they are texting about."""
    old = {**ADRIAN_CARD, "id": "old", "idList": "L-done",
           "dateLastActivity": "2026-05-01", "desc": "Phone: 312-555-0188\n10 OTP VETS"}
    config = _board(monkeypatch, [old, ADRIAN_CARD])

    card = jobs.agent_on_the_board(config, "3125550188")

    assert "Trucker" in card["desc"]


def test_an_archived_card_says_so(monkeypatch):
    config = _board(monkeypatch, [{**ADRIAN_CARD, "closed": True}])

    assert jobs.agent_on_the_board(config, "3125550188")["list"] == "archived"


def test_nobody_on_the_board_is_nothing_rather_than_a_guess(monkeypatch):
    config = _board(monkeypatch, [ADRIAN_CARD])

    assert jobs.agent_on_the_board(config, "+19995550000", "Nobody Known") is None


def test_a_board_that_wont_answer_costs_the_context_not_the_draft(monkeypatch):
    config = _board(monkeypatch, [], fails=True)
    config.secrets.ringcentral_team = ""

    known = jobs.ring_context(config, {"playbook": {"text": "Be brief."}}, [],
                              agent="3125550188", name="Adrian")

    assert known["card"] is None
    assert known["playbook"] == "Be brief."


# ------------------------------------------------ all of it in the draft


def test_the_draft_is_given_the_card_the_history_the_corrections_and_the_playbook(
    monkeypatch,
):
    config = _drafting(monkeypatch)
    card = {"agent": "Adrian Pacheco", "list": "In Que", "touched": "2026-09-24",
            "desc": "Lead Type: OTP Trucker IUL\nLive Friday September 26",
            "setup": "✅ setup is complete", "sheet": "https://sheet"}
    history = [smsreplies.Text(id="h", at="1", inbound=True, agent="1", name="",
                               said="I bought 25 trucker leads in June")]
    lessons = [{"asked": "pause", "suggested": "Paused!", "sent": "All set Adrian! 🙂"}]

    jobs.draft_like_faith(
        config, asked="when do my leads start", done=_done(), card=card,
        history=history, lessons=lessons, playbook="She signs off with 🙂.",
    )

    prompt = Claude.asked[0]["messages"][0]["content"]
    system = Claude.asked[0]["system"]
    assert "FAITH'S PLAYBOOK" in prompt and "She signs off with 🙂." in prompt
    assert "Live Friday September 26" in prompt and '"In Que" list' in prompt
    assert "I bought 25 trucker leads in June" in prompt
    assert "Faith sent: All set Adrian! 🙂" in prompt
    assert "agent's card from the team's board" in system
    assert "older order" in system
    assert "do what she did" in system


def test_without_any_of_it_the_draft_is_as_before(monkeypatch):
    config = _drafting(monkeypatch)
    jobs.draft_like_faith(config, asked="are my leads paused?", done=_done())

    prompt = Claude.asked[0]["messages"][0]["content"]
    assert prompt.startswith("Here is how Faith has answered agents before")


def test_how_she_already_talks_to_this_agent_is_shown_first(monkeypatch):
    config = _drafting(monkeypatch)
    jobs.draft_like_faith(config, asked="are my leads paused?", done=_done(),
                          agent="+14358173162")

    prompt = Claude.asked[0]["messages"][0]["content"]
    first = prompt.split("EXAMPLE 1\n", 1)[1].split("EXAMPLE 2", 1)[0]
    assert "Faith:" in first


# ------------------------------------------------ learning what she sent


def test_a_suggestion_is_remembered_with_what_it_said(monkeypatch):
    jobs.ring_pinged("5", pending={"key": "C-1", "at": "2026-09-24T10:00",
                                   "asked": "pause", "draft": "Paused!", "agent": "1"})

    data = ringtexts.load()
    assert data["pinged"] == ["5"]
    assert data["pending"][0]["draft"] == "Paused!"


def test_what_faith_sent_next_becomes_a_lesson_on_the_next_read(monkeypatch):
    """Every ping is graded by what she actually sent a minute later."""
    asked_at = (NOW - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    ringtexts.save({"texts": [], "pinged": [], "pending": [{
        "key": "C-shelby", "at": asked_at, "asked": "are my leads paused?",
        "draft": "Hi! Yes 😊", "agent": "4358173162",
    }]})
    answer = record(9, 20, "Yes they are Shelby! Back on Monday 🙂", inbound=False)
    answer["conversationId"] = "C-shelby"
    _ringing(monkeypatch, [answer])

    data, _ = jobs.ring_catch_up(NS(secrets=None), now=NOW)

    assert data["pending"] == []
    assert data["lessons"][-1]["sent"] == "Yes they are Shelby! Back on Monday 🙂"
    assert data["lessons"][-1]["suggested"] == "Hi! Yes 😊"


# ------------------------------------------------------------ the playbook


def test_a_playbook_is_due_when_never_written_or_a_week_old():
    fresh = jobs._ring_iso(NOW - timedelta(days=1))
    stale = jobs._ring_iso(NOW - timedelta(days=8))

    assert jobs.ring_playbook_due({}, now=NOW)
    assert not jobs.ring_playbook_due({"playbook": {"text": "x", "made": fresh}}, now=NOW)
    assert jobs.ring_playbook_due({"playbook": {"text": "x", "made": stale}}, now=NOW)


def test_the_playbook_is_studied_from_her_replies(monkeypatch):
    config = _drafting(monkeypatch)
    done = [smsreplies.Exchange(agent="1", name="", asked=f"q{at}", answered=f"a{at}",
                                at=str(at)) for at in range(30)]

    def writes(self, **kw):
        Claude.asked.append(kw)
        return NS(content=[NS(type="text", text="## How she writes\nShort.")])

    monkeypatch.setattr(Claude, "create", writes)
    got = jobs.faith_playbook(config, done)

    assert got.startswith("## How she writes")
    assert "Agent: q0\nFaith: a0" in Claude.asked[0]["messages"][0]["content"]
    assert "never a policy" in Claude.asked[0]["system"]


def test_too_few_replies_to_study_are_not_studied(monkeypatch):
    config = _drafting(monkeypatch)

    with pytest.raises(ValueError):
        jobs.faith_playbook(config, _done())

    assert Claude.asked == []


def test_a_written_playbook_is_posted_for_franklin_to_correct(monkeypatch):
    from wilbyte.bot import client

    monkeypatch.setattr(client, "_PLAYBOOK_FAILED", [])
    monkeypatch.setattr(jobs, "faith_playbook", lambda cfg, done, learned=(), links=(): "## How she writes")
    kept = []
    monkeypatch.setattr(jobs, "ring_keep_playbook", lambda text, n: kept.append((text, n)))
    sent = []

    class Here:
        async def send(self, content=None, **kw):
            sent.append((content, kw.get("file")))

    data = {}
    asyncio.run(client._write_the_playbook(NS(config=None), Here(), data, [1, 2, 3]))

    assert kept == [("## How she writes", 3)]
    assert "Faith's playbook" in sent[0][0] and "tell me" in sent[0][0]
    assert sent[0][1].filename == "faith-playbook.md"
    assert data["playbook"]["text"] == "## How she writes"


def test_a_playbook_that_failed_is_not_tried_again_every_minute(monkeypatch):
    """A study of her whole history that keeps failing is a bill, not a retry."""
    from wilbyte.bot import client

    monkeypatch.setattr(client, "_PLAYBOOK_FAILED", [])
    tries = []

    def fails(cfg, done, learned=(), links=()):
        tries.append(1)
        raise RuntimeError("overloaded")

    monkeypatch.setattr(jobs, "faith_playbook", fails)

    class Here:
        async def send(self, content=None, **kw):
            raise AssertionError("posted a failed playbook")

    for _ in range(3):
        asyncio.run(client._write_the_playbook(NS(config=None), Here(), {}, [1]))

    assert tries == [1]


def test_a_fresh_playbook_is_not_written_again(monkeypatch):
    from wilbyte.bot import client

    monkeypatch.setattr(client, "_PLAYBOOK_FAILED", [])
    monkeypatch.setattr(jobs, "faith_playbook",
                        lambda cfg, done, learned=(), links=(): (_ for _ in ()).throw(AssertionError("rewrote")))
    fresh = {"playbook": {"text": "x", "made": jobs._ring_iso(datetime.now(timezone.utc))}}

    asyncio.run(client._write_the_playbook(NS(config=None), Heard(), fresh, [1]))


# ------------------------------------------------ the ping uses all of it


def test_the_ping_is_drafted_with_what_is_known_and_graded_later(monkeypatch):
    said, marked = _once(monkeypatch, card={
        "url": "https://trello.com/c/adrian", "list": "In Que",
    })

    assert marked == ["5"]
    assert "Their card: [In Que](https://trello.com/c/adrian)" in said[0]
    pending = jobs.ring_pinged.pending
    assert pending["draft"] == "Hi Shelby! Yes 😊"
    assert pending["asked"] == "are my leads paused?"


# ----------------------------------------- why a suggestion was not sent


def _says(monkeypatch, name, answer):
    """Claude answering with one tool call, remembering what it was asked."""
    config = _drafting(monkeypatch)

    def create(self, **kw):
        Claude.asked.append(kw)
        if isinstance(answer, Exception):
            raise answer
        got = answer(kw) if callable(answer) else answer
        return NS(stop_reason="tool_use", content=[NS(type="tool_use", name=name, input=got)])

    monkeypatch.setattr(Claude, "create", create)
    return config


def _txt(at, said, *, inbound=True, key="C-1", team=False):
    one = smsreplies.Text(id=at, at=at, inbound=inbound, agent="3125550188",
                          name="", said=said, conversation=key)
    one.team = team
    return one


LESSON = {
    "asked": "when do my leads start?", "suggested": "Hi! They start [launch date] 😊",
    "sent": "Hi Adrian! Friday 🙂", "at": "2026-09-24T10:05:00.000Z",
    "asked_at": "2026-09-24T10:00:00.000Z", "agent": "3125550188", "key": "C-1",
    "same": False, "kept": 0.2, "name": "Adrian Pacheco",
}


def test_a_changed_suggestion_is_explained_from_everything_around_it(monkeypatch):
    config = _says(monkeypatch, "lesson", {
        "why": "She knew the launch day off the card.",
        "rule": "Give the launch day when the card has it.", "kind": "facts",
    })

    got = jobs.explain_the_change(
        config, {**LESSON, "note": "the card says Friday, use it"},
        before=[_txt("1", "hey"), _txt("2", "when do my leads start?")],
        after=[_txt("3", "perfect thanks!")],
        card={"agent": "Adrian Pacheco", "desc": "Live Friday September 26"},
    )

    prompt = Claude.asked[0]["messages"][0]["content"]
    assert got == {"why": "She knew the launch day off the card.",
                   "rule": "Give the launch day when the card has it.", "kind": "facts"}
    for shown in ("Live Friday September 26", "Agent: hey", "Hi! They start [launch date]",
                  "Hi Adrian! Friday", "Agent: perfect thanks!",
                  "FRANKLIN SAID ABOUT THE SUGGESTION:\nthe card says Friday"):
        assert shown in prompt, shown
    assert "do not second-guess it" in Claude.asked[0]["system"]


def test_nothing_texted_back_is_said_as_such(monkeypatch):
    config = _says(monkeypatch, "lesson", {"why": "Called.", "rule": "", "kind": "action"})

    jobs.explain_the_change(config, {**LESSON, "sent": "", "note": "I called him"})

    assert "nothing was texted back" in Claude.asked[0]["messages"][0]["content"]


def test_an_empty_explanation_is_not_a_lesson(monkeypatch):
    config = _says(monkeypatch, "lesson", {"why": " ", "rule": "x", "kind": "other"})

    with pytest.raises(ValueError):
        jobs.explain_the_change(config, LESSON)


# ------------------------------------------------ why she answered that way


def _swaps(count):
    return [smsreplies.Exchange(agent="1", name="", asked=f"q{at}", answered=f"a{at}",
                                at=f"2026-09-2{at}", key="C") for at in range(count)]


def test_each_of_her_replies_is_explained_with_the_conversation_before_it(monkeypatch):
    config = _says(monkeypatch, "reasons", {"reasons": [
        {"n": 2, "why": "She was holding the date back."},
        {"n": 1, "why": "Invoices go to Tre."},
        {"n": 9, "why": "no such exchange"},
        {"n": "x", "why": "not a number"},
    ]})
    one, two, three = _swaps(3)

    got = jobs.explain_her_replies(config, [
        (one, [_txt("1", "is my invoice ready?")]), (two, []), (three, []),
    ])

    prompt = Claude.asked[0]["messages"][0]["content"]
    assert "EXCHANGE 1\nAgent: is my invoice ready?\nFAITH ANSWERED: a0" in prompt
    assert "EXCHANGE 2\nAgent: q1\nFAITH ANSWERED: a1" in prompt
    # the one skipped is remembered as studied, so it isn't paid for every minute
    assert got == {one.id: "Invoices go to Tre.", two.id: "She was holding the date back.",
                   three.id: ""}


def test_a_batch_with_nothing_explained_is_a_failure_not_a_blank(monkeypatch):
    config = _says(monkeypatch, "reasons", {"reasons": []})

    with pytest.raises(ValueError):
        jobs.explain_her_replies(config, [(one, []) for one in _swaps(2)])


# ------------------------------------------------------------ studying


def _studying(monkeypatch, data, *, lesson=None, reasons=None):
    ringtexts.save(data)
    monkeypatch.setattr(jobs, "agent_on_the_board", lambda cfg, n, name="": None)
    asked = {"lessons": [], "reasons": []}

    def change(cfg, one, **kw):
        asked["lessons"].append((one, kw))
        if isinstance(lesson, Exception):
            raise lesson
        return lesson or {"why": "She knew.", "rule": "Say it.", "kind": "facts"}

    def replies(cfg, batch):
        asked["reasons"].append(batch)
        if isinstance(reasons, Exception):
            raise reasons
        return {one.id: f"why {one.answered}" for one, _before in batch}

    monkeypatch.setattr(jobs, "explain_the_change", change)
    monkeypatch.setattr(jobs, "explain_her_replies", replies)
    return asked


def _history(count, *, day="2026-09-20"):
    texts = []
    for at in range(count):
        texts.append(_txt(f"{day}T1{at}:00:00.000Z", f"question {at}", key=f"C-{at}"))
        texts.append(_txt(f"{day}T1{at}:05:00.000Z", f"answer {at}", inbound=False, key=f"C-{at}"))
    return [one.as_dict() for one in texts]


def test_corrections_are_explained_and_her_replies_studied(monkeypatch):
    data = {"texts": _history(3), "lessons": [
        dict(LESSON), {**LESSON, "asked_at": "x", "same": True},
    ]}
    asked = _studying(monkeypatch, data)

    got = jobs.ring_study(NS(secrets=None), now=NOW)

    kept = ringtexts.load()
    assert len(asked["lessons"]) == 1, "a suggestion sent as written was explained"
    assert kept["lessons"][0]["why"] == "She knew." and kept["lessons"][0]["rule"] == "Say it."
    assert [one["why"] for one in got["explained"]] == ["She knew."]
    assert set(kept["reasons"].values()) == {"why answer 0", "why answer 1", "why answer 2"}
    # newest first
    assert [one.answered for one, _ in asked["reasons"][0]] == ["answer 2", "answer 1", "answer 0"]
    assert got["studied"] == 3 and got["problems"] == []


def test_an_answer_still_being_written_is_not_studied_yet(monkeypatch):
    fresh = (NOW - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    data = {"texts": [_txt("2026-09-24T00:00:00.000Z", "q").as_dict(),
                      _txt(fresh, "a", inbound=False).as_dict()]}
    asked = _studying(monkeypatch, data)

    got = jobs.ring_study(NS(secrets=None), now=NOW)

    assert asked["reasons"] == [] and got["studied"] == 0


def test_her_whole_history_is_read_a_batch_at_a_time(monkeypatch):
    monkeypatch.setattr(jobs, "REASON_BATCH", 2)
    asked = _studying(monkeypatch, {"texts": _history(5)})

    jobs.ring_study(NS(secrets=None), now=NOW)
    jobs.ring_study(NS(secrets=None), now=NOW)

    assert [[one.answered for one, _ in batch] for batch in asked["reasons"]] == [
        ["answer 4", "answer 3"], ["answer 2", "answer 1"],
    ]


def test_a_lesson_that_keeps_failing_is_given_up_on(monkeypatch):
    asked = _studying(monkeypatch, {"texts": [], "lessons": [dict(LESSON)]},
                      lesson=RuntimeError("overloaded"))

    for _ in range(5):
        got = jobs.ring_study(NS(secrets=None), now=NOW)

    assert len(asked["lessons"]) == 3
    assert ringtexts.load()["lessons"][0]["tries"] == 3
    assert got["problems"] == []


def test_a_failed_study_says_so(monkeypatch):
    _studying(monkeypatch, {"texts": _history(1)}, reasons=RuntimeError("overloaded"))

    got = jobs.ring_study(NS(secrets=None), now=NOW)

    assert got["problems"] and "overloaded" in got["problems"][0]
    assert not ringtexts.load().get("reasons")


def test_reasons_for_texts_no_longer_kept_are_let_go(monkeypatch):
    _studying(monkeypatch, {"texts": _history(1), "reasons": {"gone|1": "old"}})

    jobs.ring_study(NS(secrets=None), now=NOW)

    assert "gone|1" not in ringtexts.load()["reasons"]


# ------------------------------------------- Franklin saying why, in Discord


def test_a_reply_to_a_lesson_replaces_the_guess(monkeypatch):
    ringtexts.save({"lessons": [{**LESSON, "why": "guess", "rule": "guess",
                                 "lesson_post": "900"}]})

    assert jobs.ring_told(900, "  she always calls   on refunds ") == "lesson"

    lesson = ringtexts.load()["lessons"][0]
    assert lesson["note"] == "she always calls on refunds"
    assert "why" not in lesson and "rule" not in lesson
    assert smsreplies.needs_explaining([lesson]) == [lesson]


def test_a_reply_to_a_ping_waits_for_her_answer(monkeypatch):
    ringtexts.save({"pending": [{"id": "5", "posted": "901", "key": "C-1", "at": "1"}]})

    assert jobs.ring_told("901", "never say paused") == "ping"
    assert jobs.ring_told("901", "call instead") == "ping"

    assert ringtexts.load()["pending"][0]["note"] == "never say paused / call instead"


def test_a_reply_to_anything_else_is_not_a_lesson():
    ringtexts.save({"pending": [{"id": "5", "posted": "901"}]})

    assert jobs.ring_told("902", "hello") is None
    assert jobs.ring_told("901", "   ") is None


def test_which_post_was_which_is_remembered():
    ringtexts.save({"pending": [{"id": "5"}, {"id": "6"}], "lessons": [dict(LESSON)]})

    jobs.ring_posted("6", 901)
    jobs.ring_posted("", 902, lesson=jobs._lesson_id(LESSON))

    data = ringtexts.load()
    assert [one.get("posted") for one in data["pending"]] == [None, "901"]
    assert data["lessons"][0]["lesson_post"] == "902"
    assert jobs.ring_posts(data) == {901, 902}


def test_the_ping_and_the_note_follow_the_suggestion_into_its_lesson(monkeypatch):
    asked_at = (NOW - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    ringtexts.save({"texts": [], "pinged": [], "pending": [{
        "id": "5", "key": "C-shelby", "at": asked_at, "asked": "paused?",
        "draft": "Hi! Yes 😊", "agent": "4358173162", "posted": "901",
        "note": "tell her Monday", "name": "Shelby Guest",
    }]})
    answer = record(9, 20, "Yes they are Shelby! Back on Monday 🙂", inbound=False)
    answer["conversationId"] = "C-shelby"
    _ringing(monkeypatch, [answer])

    data, _ = jobs.ring_catch_up(NS(secrets=None), now=NOW)

    lesson = data["lessons"][-1]
    assert (lesson["posted"], lesson["note"], lesson["name"]) == ("901", "tell her Monday", "Shelby Guest")


# ---------------------------------------------- what the draft is given


def test_the_draft_answers_for_her_reasons_and_what_was_learned(monkeypatch):
    config = _drafting(monkeypatch)
    done = _done()
    done[-1].why = "She holds the date back until the team confirms."

    jobs.draft_like_faith(
        config, asked="are my leads paused?", done=done,
        learned=["Give the launch day when the card has it."],
        lessons=[{**LESSON, "note": "use the card", "why": "She knew it."}],
    )

    prompt = Claude.asked[0]["messages"][0]["content"]
    assert "(Why she answered that way: She holds the date back" in prompt
    assert "WHAT YOU HAVE LEARNED FROM HER CORRECTIONS" in prompt
    assert "- Give the launch day when the card has it." in prompt
    assert "Franklin said: use the card\nWhy: She knew it." in prompt
    assert "outranks everything else" in Claude.asked[0]["system"]


def test_the_playbook_is_written_with_her_reasons_and_what_was_learned(monkeypatch):
    config = _drafting(monkeypatch)
    done = _swaps(9) * 3
    done[0].why = "Invoices go to Tre."

    def writes(self, **kw):
        Claude.asked.append(kw)
        return NS(content=[NS(type="text", text="## Playbook")])

    monkeypatch.setattr(Claude, "create", writes)
    jobs.faith_playbook(config, done, ["Never promise a refund."])

    prompt = Claude.asked[0]["messages"][0]["content"]
    assert "(Why: Invoices go to Tre.)" in prompt
    assert "- Never promise a refund." in prompt


def test_the_playbook_is_rewritten_once_enough_has_been_learned():
    made = jobs._ring_iso(NOW - timedelta(days=1))
    newer = jobs._ring_iso(NOW - timedelta(hours=1))
    lessons = [{"rule": f"r{at}", "at": newer} for at in range(jobs.PLAYBOOK_NEW_RULES - 1)]
    held = {"playbook": {"text": "x", "made": made}}

    assert not jobs.ring_playbook_due({**held, "lessons": lessons}, now=NOW)
    assert not jobs.ring_playbook_due(
        {**held, "lessons": lessons + [{"rule": "old", "at": made[:-5]}]}, now=NOW)
    assert jobs.ring_playbook_due(
        {**held, "lessons": lessons + [{"rule": "new", "at": newer}]}, now=NOW)


def test_what_is_known_includes_what_was_learned(monkeypatch):
    config = _board(monkeypatch, [])
    config.secrets.ringcentral_team = ""

    known = jobs.ring_context(config, {"lessons": [{"rule": "Be brief."}]}, [], agent="")

    assert known["learned"] == ["Be brief."]


# ------------------------------------------------------- in the channel


class Posted:
    def __init__(self):
        self.said, self.next = [], 700

    async def send(self, content=None, **kw):
        self.said.append(flat(content, kw.get("embed")))
        self.next += 1
        return NS(id=self.next)


def test_each_lesson_is_told_to_franklin(monkeypatch):
    from wilbyte.bot import client

    monkeypatch.setattr(client, "_STUDY_FAILED", [])
    monkeypatch.setattr(client, "_RING_POSTS", set())
    lesson = {**LESSON, "why": "She knew it off the card.", "rule": "Use the card."}
    ringtexts.save({"lessons": [lesson, {"same": True, "sent": "a"}]})
    monkeypatch.setattr(jobs, "ring_study", lambda cfg: {
        "explained": [lesson], "studied": 20, "problems": []})
    here = Posted()

    asyncio.run(client._study(NS(config=None), here))

    (said,) = here.said
    assert said.startswith("📝 Learned something\nFrom Adrian Pacheco")
    assert "> Hi! They start [launch date] 😊" in said and "> Hi Adrian! Friday 🙂" in said
    assert "Why: She knew it off the card." in said
    assert "Next time: Use the card." in said
    assert "Used as written or nearly: 1 of my last 2" in said
    assert "Reply to this" in said
    assert client._RING_POSTS == {701}
    assert ringtexts.load()["lessons"][0]["lesson_post"] == "701"


def test_studying_that_failed_waits_before_trying_again(monkeypatch):
    from wilbyte.bot import client

    monkeypatch.setattr(client, "_STUDY_FAILED", [])
    tries = []

    def study(cfg):
        tries.append(1)
        return {"explained": [], "studied": 0, "problems": ["Couldn't study: overloaded"]}

    monkeypatch.setattr(jobs, "ring_study", study)
    for _ in range(3):
        asyncio.run(client._study(NS(config=None), Posted()))

    assert tries == [1]


def test_a_lesson_franklin_told_shows_his_words(monkeypatch):
    from wilbyte.bot import client

    said = flat(embed=client._ring_lesson({**LESSON, "sent": "", "note": "I called him",
                                           "why": "It was handled by phone.", "rule": ""}))

    assert "What was sent: *Nothing was texted back.*" in said
    assert "You told me: I called him" in said
    assert "Next time" not in said


def _reply(to, text, *, bot=False):
    replied = []

    async def reply(content, **kw):
        replied.append(content)

    message = NS(author=NS(bot=bot), reference=NS(message_id=to), content=text,
                 reply=reply)
    return message, replied


def test_a_reply_to_a_lesson_is_taken_as_the_reason(monkeypatch):
    from wilbyte.bot import client

    monkeypatch.setattr(client, "_RING_POSTS", {900})
    ringtexts.save({"lessons": [{**LESSON, "lesson_post": "900", "why": "guess"}]})
    message, replied = _reply(900, "she always calls on refunds")

    assert client._is_ring_reply(message)
    asyncio.run(client._ring_told(message))

    assert ringtexts.load()["lessons"][0]["note"] == "she always calls on refunds"
    assert "Got it" in replied[0]


def test_replies_to_anything_else_are_left_alone(monkeypatch):
    from wilbyte.bot import client

    monkeypatch.setattr(client, "_RING_POSTS", {900})

    assert not client._is_ring_reply(_reply(901, "hi")[0])
    assert not client._is_ring_reply(_reply(900, "hi", bot=True)[0])
    assert not client._is_ring_reply(NS(author=NS(bot=False), reference=None))


def test_the_ping_remembers_which_message_it_was(monkeypatch):
    from wilbyte.bot import client

    monkeypatch.setattr(client, "_RING_POSTS", set())
    posted = []
    monkeypatch.setattr(jobs, "ring_posted",
                        lambda ring, discord, lesson="": posted.append((ring, discord)))

    asyncio.run(client._remember_post(NS(id=777), "5"))
    asyncio.run(client._remember_post(None, "6"))

    assert posted == [("5", 777)] and client._RING_POSTS == {777}


def test_only_a_few_corrections_are_worked_out_a_minute(monkeypatch):
    """Each is a Claude call; a backlog is worked through, not paid for at once."""
    lessons = [{**LESSON, "asked_at": f"2026-09-2{at}"} for at in range(5)]
    asked = _studying(monkeypatch, {"texts": [], "lessons": lessons})

    jobs.ring_study(NS(secrets=None), now=NOW)

    assert len(asked["lessons"]) == jobs.STUDY_LESSONS



# ------------------------------------------------ the team's second screen


class Posting:
    """A channel, keeping what was posted in it."""

    def __init__(self, channel_id, *, fails=False):
        self.channel_id, self.requester_id, self.fails = channel_id, None, fails
        self.got, self.next = [], channel_id * 100

    async def send(self, content=None, **kw):
        if self.fails:
            raise RuntimeError("Missing Access")
        file = kw.get("file")
        self.got.append((content, kw.get("embed"), file.fp.read() if file else None))
        self.next += 1
        return NS(id=self.next)


def test_the_team_sees_everything_with_nobody_tagged():
    """"its juts like a second screen ... but Ryte wont press me or anyone
    unlike on my server"."""
    from wilbyte.bot import client, embeds

    mine, team = Posting(1), Posting(2)
    both = client.AlsoThere(mine, team)
    card = embeds.ring_ping(who="Shelby", said="paused?", drafted={"reply": "Yes!"})

    sent = asyncio.run(both.send("<@42>", embed=card))

    assert mine.got == [("<@42>", card, None)]
    assert team.got == [(None, card, None)]
    assert sent.id == 101 and both.last_echo.id == 201


def test_words_stay_and_only_the_tags_go():
    from wilbyte.bot import client

    assert client._untagged("<@42> <@!7> <@&9> @everyone @here heads up") == "heads up"
    assert client._untagged("<@42>") is None
    assert client._untagged(None) is None


def test_a_file_goes_to_both():
    import io

    import discord

    from wilbyte.bot import client

    mine, team = Posting(1), Posting(2)
    asyncio.run(client.AlsoThere(mine, team).send(
        "📘 playbook", file=discord.File(io.BytesIO(b"## How she writes"), filename="p.md"),
    ))

    assert mine.got[0][2] == b"## How she writes" and team.got[0][2] == b"## How she writes"


def test_the_team_copy_failing_never_costs_franklin_his_ping():
    from wilbyte.bot import client

    mine = Posting(1)
    both = client.AlsoThere(mine, Posting(2, fails=True))

    sent = asyncio.run(both.send("<@42> hi"))

    assert mine.got == [("<@42> hi", None, None)] and sent.id == 101
    assert both.last_echo is None


def test_a_reply_on_the_team_screen_teaches_the_same(monkeypatch):
    """Faith replying there with the real reason is the best teacher there is."""
    from wilbyte.bot import client

    monkeypatch.setattr(client, "_RING_POSTS", set())
    ringtexts.save({"pending": [{"id": "5"}], "lessons": [dict(LESSON)]})

    asyncio.run(client._remember_post(NS(id=101), "5", also=NS(id=201)))
    asyncio.run(client._remember_post(NS(id=102), "", lesson=jobs._lesson_id(LESSON), also=NS(id=202)))

    assert client._RING_POSTS == {101, 201, 102, 202}
    assert jobs.ring_posts(ringtexts.load()) == {101, 201, 102, 202}
    assert jobs.ring_told("201", "we call these ones") == "ping"
    assert jobs.ring_told("202", "she knew the date") == "lesson"
    data = ringtexts.load()
    assert data["pending"][0]["note"] == "we call these ones"
    assert data["lessons"][0]["note"] == "she knew the date"


def _screens(monkeypatch, mine, shared):
    from wilbyte.bot import client

    channels = {"ringcentral_channel_id": mine, "ringcentral_shared_channel_id": shared}
    monkeypatch.setattr(client, "_ring_channel",
                        lambda bot, setting="ringcentral_channel_id": (channels[setting], ""))
    monkeypatch.setattr(client, "_board_responder", lambda bot: None)
    return client._ring_responder(NS())


def test_with_a_second_screen_set_both_are_posted_to(monkeypatch):
    from wilbyte.bot import client

    got = _screens(monkeypatch, NS(id=1), NS(id=2))

    assert isinstance(got, client.AlsoThere)
    assert got.main.channel_id == 1 and got.shared.channel_id == 2


def test_without_one_it_is_franklins_channel_as_before(monkeypatch):
    from wilbyte.bot import client

    assert not isinstance(_screens(monkeypatch, NS(id=1), None), client.AlsoThere)
    # the same channel named twice is one channel, not two copies of everything
    assert not isinstance(_screens(monkeypatch, NS(id=1), NS(id=1)), client.AlsoThere)


def test_ryte_answers_on_the_team_screen_too():
    from wilbyte.bot import client

    config = NS(secrets=NS(discord_channel_ids=["9"], discord_sop_channel_ids=[],
                           ringcentral_channel_id="1", ringcentral_shared_channel_id="2",
                           discord_role_ids=[]))

    assert client.is_allowed(channel_id=2, user=NS(), config=config)[0]
    assert client.is_allowed(channel_id=1, user=NS(), config=config)[0]
    assert not client.is_allowed(channel_id=3, user=NS(), config=config)[0]


# ------------------------------------------------ a year of her texts, once


class Reaching(Reading):
    """RingCentral answering each read with the texts in its window."""

    def __init__(self, records, **kw):
        super().__init__(records, **kw)
        self.windows = []

    def texts(self, *, since, until=""):
        self.windows.append((since, until))
        if self.error:
            raise self.error
        return [one for one in self.records
                if one["creationTime"] >= since and (not until or one["creationTime"] < until)]


def _stored(minutes_ago):
    one = smsreplies.from_record(record(f"s{minutes_ago}", minutes_ago, "stored"))
    return one.as_dict() | {"sender": ""}


def test_the_months_before_the_first_read_are_read_once(monkeypatch):
    """"learning from 1680 of Faith's replies (6000 texts)" - the first read
    went back 120 days and the cap dropped the rest. Both are raised, and what
    is older is still on RingCentral."""
    days = 24 * 60
    old = record("old", 200 * days, "can I get a refund", inbound=True)
    box = Reaching([old, record("new", 5, "hi")])
    monkeypatch.setattr(ringcentral, "open_ring", lambda secrets: box)
    ringtexts.save({"texts": [_stored(100 * days), _stored(60)], "pinged": []})

    data, problems = jobs.ring_catch_up(NS(secrets=None), now=NOW)
    again, _ = jobs.ring_catch_up(NS(secrets=None), now=NOW)

    assert problems == []
    since, until = box.windows[1]
    assert (NOW - jobs._ring_when(since)).days == jobs.RING_FIRST_DAYS
    assert until == _stored(100 * days)["at"]
    assert "old" in {one["id"] for one in data["texts"]}
    assert data["texts"][0]["id"] == "old", "kept oldest first"
    assert data["reach"] == jobs.RING_FIRST_DAYS
    assert len(box.windows) == 3, "reached back again on the next read"


def test_reaching_back_failing_costs_that_read_nothing(monkeypatch):
    box = Reaching([record("new", 5, "hi")])
    monkeypatch.setattr(ringcentral, "open_ring", lambda secrets: box)
    ringtexts.save({"texts": [_stored(100 * 24 * 60)], "pinged": []})
    reads = []

    def texts(*, since, until=""):
        reads.append(until)
        if until:
            raise ringcentral.RingError("rate limited")
        return [record("new", 5, "hi")]

    box.texts = texts
    data, problems = jobs.ring_catch_up(NS(secrets=None), now=NOW)

    assert problems == []
    assert "new" in {one["id"] for one in data["texts"]}
    assert "reach" not in data, "gave up on the older months after one failure"


def test_the_very_first_read_already_went_all_the_way_back(monkeypatch):
    box = Reaching(HISTORY)
    monkeypatch.setattr(ringcentral, "open_ring", lambda secrets: box)

    data, _ = jobs.ring_catch_up(NS(secrets=None), now=NOW)

    assert len(box.windows) == 1 and data["reach"] == jobs.RING_FIRST_DAYS


def test_a_couple_of_years_of_texts_are_kept():
    assert ringtexts.KEEP_TEXTS >= 40000
