"""Faith's texts with the agents, as the exchanges they were."""

from __future__ import annotations

from wilbyte import smsreplies
from wilbyte.smsreplies import Text

SHELBY = "8015550142"
JAY = "8015550100"


def said(at, text, *, inbound=True, agent=SHELBY, name="", id_=None):
    return Text(id=id_ or f"{agent}-{at}", at=f"2026-09-{at}", inbound=inbound,
                agent=agent, name=name, said=text)


# ------------------------------------------------------- reading a record


def test_an_agents_text_is_theirs():
    got = smsreplies.from_record({
        "id": 1, "direction": "Inbound", "creationTime": "2026-09-23T14:00:00.000Z",
        "from": {"phoneNumber": "+18015550142", "name": "Shelby Guest"},
        "to": [{"phoneNumber": "+18015550199"}], "subject": "can you pause my leads",
    })

    assert got.inbound and got.agent == SHELBY and got.name == "Shelby Guest"
    assert got.said == "can you pause my leads"


def test_faiths_reply_belongs_to_the_agent_it_went_to():
    """The agent is whoever is not Faith - the recipient of what she sent."""
    got = smsreplies.from_record({
        "id": 2, "direction": "Outbound", "creationTime": "2026-09-23T14:02:00.000Z",
        "from": {"phoneNumber": "+18015550199"},
        "to": [{"phoneNumber": "+18015550142", "name": "Shelby Guest"}],
        "subject": "Hi Shelby! Yes of course",
    })

    assert not got.inbound and got.agent == SHELBY


def test_a_picture_with_no_words_is_still_a_message():
    """An agent sending a screenshot is still waiting on a reply."""
    got = smsreplies.from_record({
        "id": 3, "direction": "Inbound", "from": {"phoneNumber": "+18015550142"},
        "subject": "", "attachments": [{"type": "MmsAttachment"}],
    })

    assert got.said == "[sent a picture]"


def test_a_text_with_nothing_in_it_is_nothing():
    assert smsreplies.from_record({
        "id": 4, "direction": "Inbound", "from": {"phoneNumber": "+1435"},
        "subject": "  ", "attachments": [{"type": "Text"}],
    }) is None


def test_one_number_written_three_ways_is_one_agent():
    assert smsreplies.digits("+1 (801) 555-0142") == SHELBY
    assert smsreplies.digits("18015550142") == SHELBY
    assert smsreplies.digits("801.555.0142") == SHELBY


# ------------------------------------------------------------- exchanges


def test_what_they_asked_and_what_she_answered_is_one_exchange():
    done = smsreplies.exchanges([
        said(10, "can you pause my leads"),
        said(11, "Hi Shelby! Yes of course", inbound=False),
    ])

    assert len(done) == 1
    assert done[0].asked == "can you pause my leads"
    assert done[0].answered == "Hi Shelby! Yes of course"


def test_several_texts_in_a_row_are_one_turn():
    """Agents send "hey", then the question, then "?"."""
    done = smsreplies.exchanges([
        said(10, "hey"), said(11, "when do my leads start"), said(12, "?"),
        said(13, "Hi! Checking now", inbound=False),
        said(14, "They start Monday", inbound=False),
    ])

    assert done[0].asked == "hey\nwhen do my leads start\n?"
    assert done[0].answered == "Hi! Checking now\nThey start Monday"


def test_faith_starting_a_conversation_is_not_an_answer():
    done = smsreplies.exchanges([
        said(10, "Reminder: your leads go live today!", inbound=False),
        said(11, "thanks!"),
        said(12, "You're welcome :)", inbound=False),
    ])

    assert [one.asked for one in done] == ["thanks!"]


def test_one_conversation_is_many_exchanges():
    done = smsreplies.exchanges([
        said(10, "pause please"), said(11, "Done!", inbound=False),
        said(12, "unpause please"), said(13, "All set!", inbound=False),
    ])

    assert [one.asked for one in done] == ["pause please", "unpause please"]


def test_two_agents_are_never_mixed():
    done = smsreplies.exchanges([
        said(10, "pause please", agent=SHELBY),
        said(11, "what's my sheet link", agent=JAY),
        said(12, "Done!", inbound=False, agent=SHELBY),
    ])

    assert [(one.agent, one.asked) for one in done] == [(SHELBY, "pause please")]


# --------------------------------------------------------------- waiting


def test_an_agent_with_the_last_word_is_waiting():
    found = smsreplies.waiting([
        said(10, "pause please"), said(11, "Done!", inbound=False),
        said(12, "actually unpause"), said(13, "sorry"),
    ], since="2026-09-01")

    ((agent, _name, tail),) = found
    assert agent == SHELBY
    assert [one.said for one in tail] == ["actually unpause", "sorry"]


def test_an_agent_faith_already_answered_is_not():
    assert smsreplies.waiting([
        said(10, "pause please"), said(11, "Done!", inbound=False),
    ], since="2026-09-01") == []


def test_nobody_from_months_ago_comes_back():
    """The first run after setting this up would otherwise ping about every
    agent who ever had the last word."""
    assert smsreplies.waiting([said(10, "pause please")], since="2026-09-20") == []


# ---------------------------------------------------------- the examples


def test_the_most_alike_come_first():
    """How she handled "pause my leads" last month is how she would now."""
    done = smsreplies.exchanges([
        said(10, "when is my launch date", agent="1"),
        said(11, "Your launch is set for Monday!", inbound=False, agent="1"),
        said(12, "please pause my leads for the weekend", agent="2"),
        said(13, "Paused! Back on Monday", inbound=False, agent="2"),
        said(14, "what states am I getting", agent="3"),
        said(15, "All the ones on your order", inbound=False, agent="3"),
    ])

    picked = smsreplies.closest(done, "can you pause my leads", most=1, recent=0)

    assert picked[0].answered == "Paused! Back on Monday"


def test_the_closer_match_wins_even_when_it_is_newer():
    """Sorted by how alike, not by when: the older, looser match must not
    push out the one that is actually about the same thing."""
    done = smsreplies.exchanges([
        said(10, "can you check my leads", agent="1"),
        said(11, "Checking now!", inbound=False, agent="1"),
        said(20, "please pause my leads for the weekend", agent="2"),
        said(21, "Paused! Back on Monday", inbound=False, agent="2"),
    ])

    picked = smsreplies.closest(
        done, "pause my leads this weekend please", most=1, recent=0,
    )

    assert picked[0].answered == "Paused! Back on Monday"


def test_her_newest_come_too_whatever_they_were_about():
    """The way she writes drifts, and her newest texts are her."""
    done = smsreplies.exchanges([
        said(10, "please pause my leads", agent="1"),
        said(11, "Paused!", inbound=False, agent="1"),
        said(20, "what states", agent="2"),
        said(21, "All of them!", inbound=False, agent="2"),
    ])

    picked = smsreplies.closest(done, "pause my leads", most=1, recent=1)

    assert [one.answered for one in picked] == ["Paused!", "All of them!"]


def test_an_exchange_is_never_shown_twice():
    done = smsreplies.exchanges([
        said(10, "please pause my leads", agent="1"),
        said(11, "Paused!", inbound=False, agent="1"),
    ])

    picked = smsreplies.closest(done, "pause my leads", most=5, recent=5)

    assert len(picked) == 1


def test_the_thread_is_that_agent_only_and_the_newest():
    texts = [said(10 + at, f"{at}", agent=SHELBY) for at in range(15)]
    texts.append(said(9, "someone else", agent=JAY))

    got = smsreplies.thread(texts, SHELBY, most=3)

    assert [one.said for one in got] == ["12", "13", "14"]
