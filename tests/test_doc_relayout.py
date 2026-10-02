"""One old tab of the posting doc, put into the new layout - and only when
nothing in it is lost.

"okay i want him to fix old ones" - "no i want him to correct specific only".
"""

from types import SimpleNamespace

import pytest

from wilbyte import docs, segments
from wilbyte.bot import mentions

RYTES_OWN = """Crystal Clark has been licensed a little over four years.

"I'm your agent. You just don't know it yet."

**2 segments** plus the full interview.

LONG-FORM / FULL INTERVIEW (00:02:35–00:53:26) — 50:51
Crystal Clark: Four Years In (YT Title)

Agent Success Full Interviews (Website section) https://youtu.be/P6nIKDY2tdY

(YT Description) Crystal Clark got licensed in the middle of the pandemic.

• Starting part-time as a broker
• Why her first month is still her lowest

👍 Like • 💬 Comment • 🔔 Subscribe

#lifeinsurance #agentleadlab

(Website Description) Crystal Clark joins Tre for a full-length conversation.

SEGMENT (00:03:53–00:11:02) — 7:09
Licensed During the Pandemic (YT Title)

Agent's Expectations (Website section)

(YT Description) Crystal Clark got licensed a little over four years ago.

#first30days #agentleadlab

(Website Description) Crystal Clark describes how she came into life insurance.
"""

BY_HAND = """2) Segment 2 (00:05:17–00:10:27)
His First 30 Days on Leads (YT Title)
Agent's Expectations (Website section) https://youtu.be/eZvDMI3iuGo
(YT Description)
Most agents don't struggle because leads "don't work."
They struggle because they don't know what to fix first.

He shares:
• What he closed from his first batch

#insuranceagents #agentleadlab
(Website Description)
In this clip, Ashley breaks down his first 30 days.
_______________________________________________
"""


def test_ryte_s_own_tab_reads_back():
    payload, found = segments.read_back(RYTES_OWN)

    assert payload["summary"].startswith("Crystal Clark has been licensed")
    assert payload["pull_quote"] == "I'm your agent. You just don't know it yet."
    assert [(one.long_form, one.range) for one in found] == [
        (True, "00:02:35–00:53:26"), (False, "00:03:53–00:11:02"),
    ]
    assert found[0].youtube == "https://youtu.be/P6nIKDY2tdY"
    assert found[0].website_section == "Agent Success Full Interviews"
    assert "\n\n• Starting part-time" in found[0].yt_description


def test_a_hand_pasted_tab_reads_back_with_its_youtube_link():
    _payload, found = segments.read_back(BY_HAND)

    assert found[0].yt_title == "His First 30 Days on Leads"
    assert found[0].youtube == "https://youtu.be/eZvDMI3iuGo"
    assert found[0].yt_description.startswith("Most agents don't struggle")
    assert found[0].website_description == "In this clip, Ashley breaks down his first 30 days."


def test_the_youtube_link_gets_a_field_of_its_own():
    texts = [one.text for one in segments.relayout("Ashley", BY_HAND)]
    at = texts.index("YOUTUBE LINK")
    assert texts[at + 1] == "https://youtu.be/eZvDMI3iuGo"


def test_a_word_that_would_be_lost_stops_the_whole_tab():
    """Somebody's note between the title and the description has nowhere to
    go - so the tab is left exactly as it is."""
    noted = BY_HAND.replace(
        "Agent's Expectations (Website section) https://youtu.be/eZvDMI3iuGo",
        "Agent's Expectations (Website section) https://youtu.be/eZvDMI3iuGo\nposted Tuesday",
    )
    with pytest.raises(segments.SegmentError):
        segments.relayout("Ashley", noted)


def test_the_check_catches_anything_the_layout_dropped(monkeypatch):
    real = segments.as_doc

    def forgetful(payload, found, short, *, name):
        return [one for one in real(payload, found, short, name=name)
                if "eZvDMI3iuGo" not in one.text]

    monkeypatch.setattr(segments, "as_doc", forgetful)
    with pytest.raises(segments.SegmentError, match="wouldn't survive"):
        segments.relayout("Ashley", BY_HAND)


def test_a_tab_with_no_segments_in_it_is_not_touched():
    with pytest.raises(segments.SegmentError):
        segments.read_back("Just some notes about Crystal.")


@pytest.mark.parametrize("said, who", [
    ("fix doc Crystal Clark", "Crystal Clark"),
    ("fix the segments doc for Ashley", "Ashley"),
    ("redo old segments", ""),
])
def test_asking_for_it(said, who):
    got = mentions.parse(said, max_batch=5)
    assert (got.action, got.brief) == ("doclayout", who)


class Doc:
    def __init__(self, tabs):
        self.tabs_held = tabs
        self.replaced = []

    def contents(self):
        return [(docs.Tab(tab_id=f"t.{n}", title=title), text, "")
                for n, (title, text) in enumerate(self.tabs_held)]

    def replace_rich(self, tab, paras):
        self.replaced.append(tab.title)

    def link_to(self, tab):
        return f"https://docs.google.com/document/d/D/edit?tab={tab.tab_id}"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _plan(monkeypatch, tabs, who):
    from wilbyte.bot import jobs

    paper = Doc(tabs)
    monkeypatch.setattr(docs, "open_docs", lambda secrets: paper)
    config = SimpleNamespace(secrets=SimpleNamespace(segments_doc_id="D"))
    return jobs, config, paper, jobs.doc_relayout_plan(config, who)


def test_only_the_named_tab(monkeypatch):
    """"no i want him to correct specific only"."""
    _jobs, _c, _p, (ready, left) = _plan(
        monkeypatch, [("Crystal Clark", RYTES_OWN), ("Ashley", BY_HAND)], "crystal clark",
    )
    assert [one[0].title for one in ready] == ["Crystal Clark"] and left == []


def test_no_name_redoes_nothing(monkeypatch):
    _jobs, _c, _p, (ready, left) = _plan(monkeypatch, [("Crystal Clark", RYTES_OWN)], "")
    assert ready == [] and "Which one" in left[0]


def test_a_name_two_tabs_share_asks_which(monkeypatch):
    _jobs, _c, _p, (ready, left) = _plan(
        monkeypatch, [("William Hayes", RYTES_OWN), ("William Ortiz", RYTES_OWN)], "william",
    )
    assert ready == [] and "More than one" in left[0]


def test_the_exact_name_beats_a_longer_one(monkeypatch):
    _jobs, _c, _p, (ready, _left) = _plan(
        monkeypatch, [("Crystal Clark 2", RYTES_OWN), ("Crystal Clark", RYTES_OWN)], "Crystal Clark",
    )
    assert [one[0].title for one in ready] == ["Crystal Clark"]


def test_a_tab_already_laid_out_is_left(monkeypatch):
    laid = "Crystal Clark — interview segments\nFull interview + 1 segment\nYOUTUBE TITLE\nx"
    _jobs, _c, _p, (ready, left) = _plan(monkeypatch, [("Crystal Clark", laid)], "Crystal Clark")
    assert ready == [] and "already" in left[0]


def test_a_tab_changed_since_the_button_is_left(monkeypatch):
    jobs, config, paper, (ready, _left) = _plan(
        monkeypatch, [("Crystal Clark", RYTES_OWN)], "Crystal Clark",
    )
    paper.tabs_held = [("Crystal Clark", RYTES_OWN + "\nedited")]
    done, problems = jobs.doc_relayout(config, ready)

    assert done == [] and paper.replaced == [] and "changed" in problems[0]


def test_the_named_tab_is_redone(monkeypatch):
    jobs, config, paper, (ready, _left) = _plan(
        monkeypatch, [("Crystal Clark", RYTES_OWN)], "Crystal Clark",
    )
    done, problems = jobs.doc_relayout(config, ready)

    assert paper.replaced == ["Crystal Clark"] and problems == []


def test_replacing_clears_the_tab_and_writes_in_one_request():
    sent = []

    class Client(docs.DocsClient):
        def end_of(self, tab):
            return 40

        def _call(self, method, path="", **kw):
            sent.append(kw["json"]["requests"])
            return {}

    client = Client(SimpleNamespace(client_id="", client_secret="", refresh_token=""), document="D")
    client.replace_rich(docs.Tab(tab_id="t.1", title="x"), [docs.Para("hello")])

    assert len(sent) == 1
    assert sent[0][0] == {"deleteContentRange": {"range": {"startIndex": 1, "endIndex": 39, "tabId": "t.1"}}}
    assert sent[0][1]["insertText"]["location"]["index"] == 1


def test_the_filed_as_line_at_the_bottom_moves_to_the_top():
    """"we put the trello card for the interview? always put it on top"."""
    filed = BY_HAND + (
        "Filed as Evan Scott Interview in Marketing Department — "
        "https://trello.com/c/zU6Es5Kc/16231-evan-scott-interview\n"
    )
    texts = [one.text for one in segments.relayout("Evan Scott", filed)]

    assert texts[1:3] == ["TRELLO CARD", "https://trello.com/c/zU6Es5Kc/16231-evan-scott-interview"]
    assert not any(one.startswith("Filed as") for one in texts)
    _payload, found = segments.read_back(filed)
    assert "trello" not in found[-1].website_description


def test_a_new_tab_has_its_card_on_top():
    keep, _ = segments.parse_segments({"segments": [{
        "kind": "segment", "start": "00:00:00", "end": "00:08:00", "yt_title": "T",
        "website_section": segments.SECTIONS[0], "hook": "h", "bullets": ["a", "b", "c"],
        "website_description": "w",
    }]})
    texts = [one.text for one in segments.as_doc({}, keep, [], name="Evan Scott",
                                                  card="https://trello.com/c/abc")]
    assert texts[:3] == ["Evan Scott — interview segments", "TRELLO CARD", "https://trello.com/c/abc"]


def test_new_interviews_are_laid_out_with_the_card_that_was_just_made(config, monkeypatch):
    import asyncio

    from wilbyte.bot import client

    seen = {}

    def copy_into_doc(cfg, *, title, text, paragraphs=None):
        seen["paras"] = paragraphs
        return "", []

    monkeypatch.setattr(client.jobs, "file_interview",
                        lambda cfg, **kw: ("https://trello.com/c/new", "cx", []))
    monkeypatch.setattr(client.jobs, "copy_into_doc", copy_into_doc)
    monkeypatch.setattr(client.jobs, "hand_off_interview", lambda cfg, **kw: ([], []))

    class Said:
        requester_id = None

        async def send(self, *a, **k):
            pass

    asyncio.run(client._file_interview(
        Said(), config, [], topic="Evan Scott", link="", passcode="", copy="x",
        lay_out=lambda card: [docs.Para(card)],
    ))
    assert [one.text for one in seen["paras"]] == ["https://trello.com/c/new"]


P = docs.PICTURE

RUN_TOGETHER = (
    "SEGMENT (00:11:31–00:16:10) — 4:39 Emanuel's Full Veteran Call Process (YT Title) "
    "Veteran Training (Website section) (YT Description) Emanuel dials the same lead three "
    "times a day. • Dialing the same lead three times • Why acting like an agent for life turns "
    "veterans into a referral source Say you'll be their agent for life, then behave like one. "
    "If you're an agency owner looking to make a million dollars a month, apply here: "
    "https://agentleadlab.com/strategysession VISIT US https://agentleadlab.com/\n"
    f"Follow Us on Instagram https://www.instagram.com/agentleadlab_/ {P} Like • {P} Comment • "
    f"{P} Subscribe #veteranleads #agentleadlab (Website Description) Emanuel Nazco walks his "
    "veteran call from the first dial to the close.\n"
)


def test_a_tab_whose_line_breaks_were_lost_reads_back():
    """Emanuel Nazco's: every segment one long paragraph."""
    _payload, found = segments.read_back(RUN_TOGETHER)

    assert found[0].yt_title == "Emanuel's Full Veteran Call Process"
    assert found[0].website_section == "Veteran Training"
    assert found[0].yt_description.split("\n") == [
        "Emanuel dials the same lead three times a day.",
        "",
        "• Dialing the same lead three times",
        "• Why acting like an agent for life turns veterans into a referral source",
        "",
        "Say you'll be their agent for life, then behave like one.",
        "",
        "If you're an agency owner looking to make a million dollars a month, apply here: "
        "https://agentleadlab.com/strategysession",
        "VISIT US https://agentleadlab.com/",
        "Follow Us on Instagram https://www.instagram.com/agentleadlab_/",
        "👍 Like • 💬 Comment • 🔔 Subscribe",
        "",
        "#veteranleads #agentleadlab",
    ]
    assert found[0].website_description.startswith("Emanuel Nazco walks")
    assert segments.relayout("Emanuel Nazco", RUN_TOGETHER)


def test_the_pasted_emoji_pictures_are_told_from_other_pictures():
    assert segments.pictures_in(RUN_TOGETHER) == (3, 0)
    assert segments.pictures_in(RUN_TOGETHER + f"\n{P} a photo") == (3, 1)


def test_a_tab_that_wasnt_run_together_keeps_its_own_lines():
    """Only a tab that lost its line breaks gets them put back."""
    _payload, found = segments.read_back(BY_HAND)
    assert found[0].yt_description.startswith(
        "Most agents don't struggle because leads \"don't work.\"\nThey struggle"
    )
    assert "He shares:\n• What he closed" in found[0].yt_description


@pytest.mark.parametrize("bullet, expected", [
    ("• into a referral source Say you'll be their agent for life.",
     ("• into a referral source", "Say you'll be their agent for life.")),
    ("• Working with Agent Lead Lab every week", ("• Working with Agent Lead Lab every week", "")),
    ("• It works, says Tre.", ("• It works, says Tre.", "")),
    ("• Keep dialing like Tre says.", ("• Keep dialing like Tre says.", "")),
])
def test_the_closing_line_comes_off_the_last_bullet(bullet, expected):
    assert segments._closing_off(bullet) == expected


def test_pictures_are_asked_about_not_refused(monkeypatch):
    """"i want him to ask me a button push or not"."""
    _jobs, _c, _p, (ready, left) = _plan(
        monkeypatch, [("Emanuel Nazco", RUN_TOGETHER + f"\n{P}")], "Emanuel Nazco",
    )
    assert left == []
    kept = ready[0][3]
    assert "come back as real emoji" in kept
    assert "1 other picture" in kept and "removed" in kept


def test_the_doc_marks_where_pictures_are():
    class Client(docs.DocsClient):
        def _call(self, method, path="", **kw):
            return {"tabs": [{
                "tabProperties": {"tabId": "t.1", "title": "Emanuel Nazco"},
                "documentTab": {"body": {"content": [{"paragraph": {"elements": [
                    {"textRun": {"content": "Follow "}},
                    {"inlineObjectElement": {"inlineObjectId": "x"}},
                    {"textRun": {"content": " Like\n"}},
                ]}}]}},
            }]}

    client = Client(SimpleNamespace(client_id="", client_secret="", refresh_token=""), document="D")
    [(tab, text, odd)] = client.contents()
    assert text == f"Follow {P} Like\n" and odd == ""


KARYN = """Karyn Giles has been selling insurance for 13 years.

https://youtu.be/-aIcdjzwFsY

LONG-FORM / FULL INTERVIEW (00:03:43–00:55:02) — 51:19
13 Years, $181K in Six Months Off Aged Leads (YT Title)

Agent Success Full Interviews (Website section)

(YT Description) Karyn Giles overdrafted her account for $20 in gas.

#agedleads #agentleadlab

(Website Description) Karyn Giles has sold insurance for 13 years.

SEGMENT (00:03:43–00:10:39) — 6:56
Five Bartending Jobs to $181,000 in Six Months  https://youtu.be/OBFITZL2uSc (YT Title)

Agent Success Full Interviews (Website section)

(YT Description) Karyn was working two to three bartending jobs.

#agedleads #agentleadlab

(Website Description) Karyn Giles walks through how she got into life insurance.
"""


def test_a_link_pasted_before_the_yt_title_label_is_not_part_of_the_title():
    """Karyn Giles: every title came out with its YouTube link on the end."""
    _payload, found = segments.read_back(KARYN)

    assert found[1].yt_title == "Five Bartending Jobs to $181,000 in Six Months"
    assert found[1].youtube == "https://youtu.be/OBFITZL2uSc"


def test_a_youtube_link_on_its_own_up_top_is_the_full_interviews():
    payload, found = segments.read_back(KARYN)

    assert found[0].long_form and found[0].youtube == "https://youtu.be/-aIcdjzwFsY"
    assert payload["summary"].startswith("Karyn Giles has been selling")
    texts = [one.text for one in segments.relayout("Karyn Giles", KARYN)]
    assert texts[0] == "Karyn Giles — interview segments"
    assert texts.count("YOUTUBE LINK") == 2
