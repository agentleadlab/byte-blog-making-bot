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


# ------------------------------------------------------------ group texts

# "Jay Rodriguez, Arnold Tarpley" - three members, Arnold in it twice. Faith
# texts from Arnold's line, and a group reply goes to Arnold's other number
# as well as to the agent.
ARNOLD = "+18005550100"
ARNOLD_TOO = "+18005550101"
JAY_NUMBER = "+19705550123"


def group(id_, at, text, *, inbound=True, conversation="C-jay"):
    return smsreplies.from_record({
        "id": id_, "creationTime": f"2026-09-24T{at}:00.000Z", "subject": text,
        "direction": "Inbound" if inbound else "Outbound",
        "from": {"phoneNumber": JAY_NUMBER, "name": "Jay Rodriguez"} if inbound
                else {"phoneNumber": ARNOLD, "name": "Arnold Tarpley"},
        # The team's other number first, the agent second.
        "to": [{"phoneNumber": ARNOLD_TOO}, {"phoneNumber": ARNOLD}] if inbound
              else [{"phoneNumber": ARNOLD_TOO, "name": "Arnold Tarpley"},
                    {"phoneNumber": JAY_NUMBER, "name": "Jay Rodriguez"}],
        "conversationId": conversation,
    })


JAY_THREAD = [
    group(1, "11:09", "could you provide me the states I added on my file"),
    group(2, "11:43", "Hi, Jay! Here's your list of states: AL,GA,HI,IA", inbound=False),
    group(3, "11:55", "Can you remove Hawaii and Alaska for me and add Oklahoma"),
    group(4, "11:57", "And could you resend an invoice with the updated states"),
]


def test_the_thread_is_read_from_the_record():
    assert JAY_THREAD[0].conversation == "C-jay"
    got = smsreplies.from_record({
        "id": 9, "direction": "Inbound", "subject": "hi",
        "from": {"phoneNumber": JAY_NUMBER}, "conversation": {"id": "C-9"},
    })
    assert got.conversation == "C-9"


def test_a_reply_to_a_group_is_still_a_reply_to_the_agent():
    """Filed by the first number it went to, it was filed under Arnold's
    other line and learned from as nobody's."""
    done = smsreplies.exchanges(JAY_THREAD[:2])

    assert len(done) == 1
    assert done[0].agent == smsreplies.digits(JAY_NUMBER)
    assert done[0].name == "Jay Rodriguez"
    assert done[0].answered.startswith("Hi, Jay!")


def test_an_agent_faith_answered_in_a_group_is_not_still_waiting():
    """Otherwise Franklin is pinged about texts Faith already answered."""
    assert smsreplies.waiting(JAY_THREAD[:2], since="2026-09-01") == []


def test_what_is_still_waiting_in_a_group_is_the_agents_last_word():
    ((agent, name, tail),) = smsreplies.waiting(JAY_THREAD, since="2026-09-01")

    assert agent == smsreplies.digits(JAY_NUMBER) and name == "Jay Rodriguez"
    assert [one.id for one in tail] == ["3", "4"]


def test_the_conversation_shown_is_that_thread():
    other = group(9, "11:58", "a different thread", conversation="C-other")

    got = smsreplies.thread(JAY_THREAD + [other], "C-jay")

    assert [one.id for one in got] == ["1", "2", "3", "4"]


def test_a_text_with_no_thread_falls_back_to_the_number():
    """Kept working for anything RingCentral hands back without one."""
    lone = Text(id="1", at="2026-09-24", inbound=True, agent="555", name="", said="hi")

    assert lone.key == "555"


# --------------------------------------------- the team texting into the line

# Tre hands an agent over from his cell, (412), into the line Faith answers
# from, (878). RingCentral labels both "Arnold Tarpley (me)".
TRE_CELL = "+14125550177"
ADRIAN = "+13125550188"


def into(id_, at, text, *, who, number, conversation="C-adrian", inbound=True):
    return smsreplies.from_record({
        "id": id_, "creationTime": f"2026-09-24T{at}:00.000Z", "subject": text,
        "direction": "Inbound" if inbound else "Outbound",
        "from": {"phoneNumber": number, "name": who} if inbound
                else {"phoneNumber": ARNOLD, "name": "Arnold Tarpley"},
        "to": [{"phoneNumber": ARNOLD}] if inbound
              else [{"phoneNumber": ADRIAN, "name": "Adrian Pacheco"}],
        "conversationId": conversation,
    })


ADRIAN_THREAD = [
    into(1, "11:43", "With Wolfpack so take care of him",
         who="Arnold Tarpley", number=TRE_CELL),
    into(2, "11:44", "Hi faith this is Adrian Pacheco paid for OTP Trucker IUL leads",
         who="Arnold Tarpley", number=TRE_CELL),
    into(3, "12:01", "👍", who="Adrian Pacheco", number=ADRIAN),
]


def test_the_owner_texting_in_is_the_team_not_an_agent():
    smsreplies.mark_team(ADRIAN_THREAD, ["Arnold Tarpley"])

    assert [one.team for one in ADRIAN_THREAD] == [True, True, False]


def test_arnold_handing_an_agent_over_is_never_what_is_waiting():
    """Read as an agent, Franklin is pinged to reply like Faith to Arnold."""
    texts = smsreplies.mark_team(list(ADRIAN_THREAD), ["Arnold Tarpley"])

    ((agent, name, tail),) = smsreplies.waiting(texts, since="2026-09-01")

    assert name == "Adrian Pacheco"
    assert [one.said for one in tail] == ["👍"]


def test_nobody_but_the_team_texting_leaves_nobody_waiting():
    texts = smsreplies.mark_team(list(ADRIAN_THREAD[:2]), ["Arnold Tarpley"])

    assert smsreplies.waiting(texts, since="2026-09-01") == []


def test_arnold_chiming_in_does_not_answer_the_agent_either():
    """The agent asked, Arnold said something, Faith has not replied: the
    agent is still waiting."""
    texts = smsreplies.mark_team([
        into(1, "11:00", "when do my leads start", who="Adrian Pacheco", number=ADRIAN),
        into(2, "11:05", "Faith can you check this", who="Arnold Tarpley",
             number=TRE_CELL),
    ], ["Arnold Tarpley"])

    ((_agent, _name, tail),) = smsreplies.waiting(texts, since="2026-09-01")

    assert [one.said for one in tail] == ["when do my leads start"]


def test_the_team_is_never_learned_from_as_a_question():
    texts = smsreplies.mark_team(list(ADRIAN_THREAD) + [
        into(4, "12:05", "Welcome Adrian! So glad to have you", who="",
             number=ADRIAN, inbound=False),
    ], ["Arnold Tarpley"])

    (done,) = smsreplies.exchanges(texts)

    assert done.asked == "👍"
    assert "Wolfpack" not in done.asked


def test_what_faith_sent_is_never_the_team():
    """Outbound is Faith, whoever the line is named for."""
    texts = smsreplies.mark_team([
        into(1, "12:05", "Welcome!", who="", number=ADRIAN, inbound=False),
    ], ["Arnold Tarpley"])

    assert texts[0].team is False


def test_faiths_group_reply_is_hers_even_when_it_went_to_arnolds_other_number():
    """A group reply can go first to Arnold's other line, which RingCentral
    also names "Arnold Tarpley". Marked as the team, her answer would be
    passed over and Jay left looking as if he were still waiting."""
    texts = smsreplies.mark_team(list(JAY_THREAD[:2]), ["Arnold Tarpley"])

    assert texts[1].team is False
    assert smsreplies.waiting(texts, since="2026-09-01") == []
    assert len(smsreplies.exchanges(texts)) == 1



# ------------------------------------------ only Faith's number is Faith

FAITH_LINE = "+18785550100"


def sent(id_, at, text, *, sender, conversation="C-x", to=ADRIAN):
    return smsreplies.from_record({
        "id": id_, "creationTime": f"2026-09-24T{at}:00.000Z", "subject": text,
        "direction": "Outbound",
        "from": {"phoneNumber": sender, "name": "Arnold Tarpley"},
        "to": [{"phoneNumber": to, "name": "Adrian Pacheco"}],
        "conversationId": conversation,
    })


def asked(id_, at, text, *, conversation="C-x"):
    return into(id_, at, text, who="Adrian Pacheco", number=ADRIAN,
                conversation=conversation)


def test_what_was_sent_says_which_number_sent_it():
    assert sent(1, "10:00", "hi", sender=FAITH_LINE).sender == "8785550100"
    assert asked(2, "10:01", "hi").sender == ""


def test_faiths_number_is_the_one_nearly_everything_goes_out_from():
    texts = [sent(at, f"10:{at:02d}", "hi", sender=FAITH_LINE) for at in range(9)]
    texts.append(sent(99, "11:00", "take care of him", sender=TRE_CELL))

    assert smsreplies.faiths_number(texts) == "8785550100"


def test_nothing_sent_yet_is_no_number_rather_than_a_guess():
    assert smsreplies.faiths_number([asked(1, "10:00", "hi")]) == ""


def test_tre_sending_from_the_line_is_never_learned_as_faith():
    """Both numbers can send from the line. Only one of them is her."""
    texts = smsreplies.mark_team([
        asked(1, "10:00", "when do my leads start", conversation="C-1"),
        sent(2, "10:01", "yo bro checking", sender=TRE_CELL, conversation="C-1"),
        asked(3, "11:00", "can you pause them", conversation="C-2"),
        sent(4, "11:01", "Hi Adrian! Paused 😊", sender=FAITH_LINE, conversation="C-2"),
    ], ["Arnold Tarpley"], faith=FAITH_LINE)

    answers = [one.answered for one in smsreplies.exchanges(texts)]

    assert answers == ["Hi Adrian! Paused 😊"]
    assert texts[1].team is True and texts[3].team is False


def test_tre_chiming_in_does_not_answer_the_agent():
    """Adrian asked, Tre said something, Faith has not replied - Adrian is
    still waiting, and that is the ping."""
    texts = smsreplies.mark_team([
        asked(1, "10:00", "when do my leads start"),
        sent(2, "10:01", "Faith will get back to you", sender=TRE_CELL),
    ], ["Arnold Tarpley"], faith=FAITH_LINE)

    ((_agent, _name, tail),) = smsreplies.waiting(texts, since="2026-09-01")

    assert [one.said for one in tail] == ["when do my leads start"]


def test_with_no_number_for_faith_nothing_sent_is_taken_away_from_her():
    """Not knowing which is hers is no reason to decide none of them are."""
    texts = smsreplies.mark_team(
        [sent(1, "10:00", "hi", sender=TRE_CELL)], ["Arnold Tarpley"], faith="",
    )

    assert texts[0].team is False


# ------------------------------------------------------- context clues


def test_a_cards_phone_number_is_found_however_it_was_written():
    assert smsreplies.phones_in("Phone: +16146033618") == {"6146033618"}
    assert smsreplies.phones_in("Phone Number: 435-817-3162") == {"4358173162"}
    assert smsreplies.phones_in("call (786) 609-0765 after 5") == {"7866090765"}


def test_numbers_that_arent_phones_are_not():
    assert smsreplies.phones_in("25 x MTG Standard @ $28.00 = $700.00") == set()
    # An ARN's last ten digits are not somebody's phone.
    assert smsreplies.phones_in("ARN 24556406248809639521734") == set()


def test_how_she_already_talks_to_this_agent_comes_first():
    """The tone she takes with somebody she has texted for months is not the
    one she takes with somebody new."""
    done = smsreplies.exchanges([
        said(10, "please pause my leads", agent="1"),
        said(11, "Paused!", inbound=False, agent="1"),
        said(12, "yo can u send invoice", agent="2"),
        said(13, "Sent bro 🙌", inbound=False, agent="2"),
    ])

    picked = smsreplies.closest(done, "pause my leads", most=1, recent=0, agent="2")

    assert [one.answered for one in picked] == ["Sent bro 🙌", "Paused!"]


def test_without_an_agent_it_is_as_before():
    done = smsreplies.exchanges([
        said(10, "please pause my leads", agent="1"),
        said(11, "Paused!", inbound=False, agent="1"),
    ])

    assert [one.answered for one in smsreplies.closest(done, "pause", recent=0)] == [
        "Paused!"
    ]


def test_the_agents_other_conversations_are_their_history():
    texts = [
        said(1, "I bought 25 vets", agent="1", id_="a"),
        said(2, "Great!", inbound=False, agent="1", id_="b"),
        Text(id="c", at="2026-09-03", inbound=True, agent="1", name="",
             said="now", conversation="C-now"),
        said(4, "someone else", agent="2", id_="d"),
    ]

    got = smsreplies.history(texts, "1", besides="C-now")

    assert [one.id for one in got] == ["a", "b"]


# ---------------------------------------------------------- learning


def _pending(key="C-1", at="2026-09-24T10:00", draft="Hi! Paused 😊", asked="pause"):
    return {"key": key, "at": at, "asked": asked, "draft": draft, "agent": "1"}


def _faith(at, text, key="C-1", *, team=False):
    one = Text(id=at, at=at, inbound=False, agent="1", name="", said=text,
               conversation=key)
    one.team = team
    return one


def test_what_faith_sent_after_a_suggestion_is_the_lesson():
    lessons, waiting = smsreplies.lessons_from(
        [_pending()],
        [_faith("2026-09-24T10:05", "All set Shelby! Leads are paused 🙂")],
        now="2026-09-24T11:00", gone_after="2026-09-21",
    )

    assert waiting == []
    assert lessons == [{
        "asked": "pause", "suggested": "Hi! Paused 😊",
        "sent": "All set Shelby! Leads are paused 🙂",
        "at": "2026-09-24T10:05", "agent": "1", "same": False,
    }]


def test_the_first_thing_she_sent_is_the_answer_not_a_later_one():
    lessons, _ = smsreplies.lessons_from(
        [_pending()],
        [_faith("2026-09-24T10:09", "later"), _faith("2026-09-24T10:05", "first")],
        now="2026-09-24T11:00", gone_after="2026-09-21",
    )

    assert lessons[0]["sent"] == "first"


def test_what_was_sent_before_the_agents_text_is_not_the_answer():
    lessons, waiting = smsreplies.lessons_from(
        [_pending()], [_faith("2026-09-24T09:00", "earlier")],
        now="2026-09-24T11:00", gone_after="2026-09-21",
    )

    assert lessons == [] and len(waiting) == 1


def test_tre_answering_is_not_faiths_answer():
    lessons, waiting = smsreplies.lessons_from(
        [_pending()], [_faith("2026-09-24T10:05", "on it", team=True)],
        now="2026-09-24T11:00", gone_after="2026-09-21",
    )

    assert lessons == [] and len(waiting) == 1


def test_a_suggestion_nobody_answered_for_days_is_let_go():
    """The agent got a call, or it needed no reply. Neither says anything
    about how she writes."""
    lessons, waiting = smsreplies.lessons_from(
        [_pending(at="2026-09-19T10:00")], [],
        now="2026-09-24T11:00", gone_after="2026-09-21",
    )

    assert lessons == [] and waiting == []


def test_a_suggestion_sent_word_for_word_is_marked_as_right():
    lessons, _ = smsreplies.lessons_from(
        [_pending(draft="Hi! Paused 😊")], [_faith("2026-09-24T10:05", "hi paused")],
        now="2026-09-24T11:00", gone_after="2026-09-21",
    )

    assert lessons[0]["same"] is True


def test_only_the_ones_she_wrote_differently_are_shown_as_corrections():
    lessons = [
        {"asked": "send invoice", "suggested": "b", "sent": "B!", "same": False},
        {"asked": "pause them please", "suggested": "c", "sent": "C!", "same": False},
        {"asked": "can you pause my leads", "suggested": "a", "sent": "a", "same": True},
    ]

    picked = smsreplies.pick_lessons(lessons, "can you pause my leads", most=2)

    assert [one["sent"] for one in picked] == ["C!", "B!"]


def test_the_newest_correction_is_always_among_them():
    lessons = [
        {"asked": "pause my leads", "suggested": "a", "sent": "A!", "same": False},
        {"asked": "send invoice", "suggested": "b", "sent": "B!", "same": False},
    ]

    picked = smsreplies.pick_lessons(lessons, "pause my leads", most=2)

    assert {one["sent"] for one in picked} == {"A!", "B!"}


def test_the_playbook_is_studied_from_all_of_her_history_not_one_week():
    done = list(range(1000))

    picked = smsreplies.sample_for_playbook(done, most=10)

    assert len(picked) == 10 and picked[0] == 0 and picked[-1] >= 900


def test_a_short_history_is_studied_whole():
    assert smsreplies.sample_for_playbook([1, 2, 3], most=10) == [1, 2, 3]
