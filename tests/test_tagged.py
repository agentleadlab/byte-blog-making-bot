"""Tagging somebody in a comment, turned into a line on their checklist.

Every case here is off the real General 09/09/26, Ops 09/09/26 and Ads
09/09/26 cards.
"""

from __future__ import annotations

import pytest

from wilbyte import tagged

# The real comments, verbatim.
GCAL = "@franklinmaymaldonado Add in gcalendar lunch + walk"
BC_LP = (
    "@franklinmaymaldonado make BC BENEFITS LP for trucker IUL struture and copy "
    "send to me at EOD i'll thorw in nova to make the page or tbh you can write "
    "the code and just send it to me and i can make the page and link with HUB "
    "unless you can do that"
)
CREATIVES = (
    "New batch of creatives for VETS 2.0 & VET Plus "
    "https://drive.google.com/drive/folders/1CWTbpE Can't find link "
    "@jenniferhashisaki2 @tretarpley"
)
FOR_THE_CARD = "@card add states for Jordan Kissinger"
YT = "@nic0l3 Add YT channel"

BOARD = [
    {"username": "franklinmaymaldonado", "fullName": "Franklin May Maldonado"},
    {"username": "kathleenmarie15", "fullName": "Kathleen Rabaya"},
    {"username": "jenniferhashisaki2", "fullName": "Jennifer Hashisaki"},
    {"username": "nic0l3", "fullName": "Nicole Sarmiento"},
    {"username": "thereseguba", "fullName": "Therese Guba"},
    {"username": "tretarpley", "fullName": "Tre Tarpley"},
]


def note(text, *, comment_id="c1", short="IU4PM7wJ", title="💎 General 09/09/26"):
    return tagged.Note(
        comment_id=comment_id, text=text, card_short=short, card_title=title,
    )


# ---------------------------------------------------------------- who is tagged


def test_the_tagged_usernames_come_out_in_order():
    assert tagged.mentioned(CREATIVES) == ["jenniferhashisaki2", "tretarpley"]


def test_the_same_person_twice_is_one_task():
    assert tagged.mentioned("@nic0l3 and again @nic0l3") == ["nic0l3"]


def test_at_card_is_not_a_person():
    """It names everybody on the card, which is a different question."""
    assert tagged.mentioned(FOR_THE_CARD) == []
    assert tagged.everyones_job(FOR_THE_CARD) is True


def test_a_comment_tagging_a_person_is_not_everybodys():
    assert tagged.everyones_job(GCAL) is False


def test_a_comment_with_no_tag_at_all_is_nobodys():
    assert tagged.mentioned("50-50 split LF and SF") == []
    assert tagged.everyones_job("50-50 split LF and SF") is False


# ------------------------------------------------- which checklist is whose


@pytest.mark.parametrize(
    "full_name, expected",
    [
        ("Franklin May Maldonado", "Frank"),
        ("Kathleen Rabaya", "Kath"),
        ("Jennifer Hashisaki", "Jenn"),
        ("Tre Tarpley", "Tre"),
        ("Nicole Sarmiento", "Nicole"),
        ("Therese Guba", "Therese"),
        ("Faith Hannah Calla", "Faith"),
    ],
)
def test_a_checklist_is_found_by_first_name(full_name, expected):
    """The board says Frank and Kath; the members are Franklin May Maldonado
    and Kathleen Rabaya. Nothing to configure, and it keeps working when
    somebody new joins."""
    names = ["Frank", "Kath", "Jenn", "Tre", "Nicole", "Therese", "Faith"]
    assert tagged.checklist_for(full_name, names) == expected


def test_the_longer_name_wins_when_a_board_has_both():
    """The setup cards say Kathleen and the Ads card says Kath."""
    assert tagged.checklist_for("Kathleen Rabaya", ["Kath", "Kathleen"]) == "Kathleen"


def test_somebody_with_no_checklist_gets_nothing():
    assert tagged.checklist_for("Tre Tarpley", ["Frank", "Nicole"]) == ""


# ------------------------------------------------------- which card it lands on


def therese():
    """She keeps one on General as well as on Ops. Her work is Ops."""
    return tagged.Person(
        "thereseguba", "Therese Guba",
        {"ops": "Therese", "general": "Therese"}, home="ops",
    )


def nicole():
    return tagged.Person(
        "nic0l3", "Nicole Sarmiento",
        {"ads": "Nicole", "general": "Nicole"}, home="ads",
    )


def test_the_work_decides_not_the_card_it_was_said_on():
    """"if Therese is tag on a comment on general card, you add them on the OPS
    card not on general card check list" — even when the reading disagrees."""
    kind, judged = tagged.where(therese(), note(GCAL), judged="general")
    assert kind == "ops"
    assert judged is False


def test_an_ops_persons_work_is_never_a_judgement_call():
    for said in ("ops", "ads", "general", ""):
        assert tagged.where(therese(), note(GCAL), judged=said)[0] == "ops"


def test_an_ads_person_defaults_to_their_own_card():
    """Nicole is on Ads and on General, so ads work goes to Ads."""
    kind, judged = tagged.where(nicole(), note(YT), judged="ads")
    assert (kind, judged) == ("ads", False)


def test_admin_is_the_one_thing_that_pulls_her_onto_general():
    """"if its admin/executive comments it will be on general"."""
    kind, judged = tagged.where(nicole(), note(YT), judged="general")
    assert (kind, judged) == ("general", True)


def test_an_unreadable_comment_leaves_an_ads_person_on_ads():
    """When nothing said which kind it was — no key, a bad night at Anthropic —
    her own card is where her work usually is."""
    kind, judged = tagged.where(nicole(), note(YT), judged="")
    assert (kind, judged) == ("ads", False)


def test_somebody_who_is_neither_lands_on_general():
    """Frank and Faith keep checklists but do no ads or ops work."""
    frank = tagged.Person(
        "franklinmaymaldonado", "Franklin May Maldonado", {"general": "Frank"},
    )
    assert tagged.where(frank, note(GCAL), judged="ads")[0] == "general"


@pytest.mark.parametrize(
    "names, expected",
    [(["Therese"], "ops"), (["Nicole"], "ads"), (["Kath"], "ads"),
     (["Jenn"], "ads"), (["Frank"], ""), (["Faith"], "")],
)
def test_who_does_what_is_read_off_the_checklist_names(names, expected):
    assert tagged.home_for(names) == expected


# ------------------------------------------------------------ not filing twice


def checklist(*items):
    return {"name": "Frank", "checkItems": [{"name": one} for one in items]}


def test_a_comment_already_on_a_checklist_is_left_alone():
    """By the comment's id, which is exact — the wording is not."""
    held = checklist(
        "Add in gcalendar lunch + walk\n"
        "https://trello.com/c/IU4PM7wJ#comment-c1"
    )
    assert tagged.already_on(note(GCAL), held) is True


def test_a_line_somebody_worded_differently_still_counts():
    """Franklin typed his own line for the same comment. The link is what
    matches, so RYTE does not add a second one beside it."""
    held = checklist("https://trello.com/c/IU4PM7wJ#comment-c1 -- lunch walk thing")
    assert tagged.already_on(note(GCAL), held) is True


def test_a_different_comment_is_not_that_comment():
    held = checklist("something else\nhttps://trello.com/c/IU4PM7wJ#comment-c2")
    assert tagged.already_on(note(GCAL), held) is False


def test_filed_anywhere_on_todays_cards_counts_for_a_person():
    """The line for a comment on General lands on Ops, so looking only at
    General would file it again every afternoon."""
    on_ops = checklist("x\nhttps://trello.com/c/IU4PM7wJ#comment-c1")
    assert tagged.already_filed(note(GCAL), [checklist("nothing"), on_ops]) is True


# --------------------------------------------------------------- the line itself


def test_the_line_is_the_summary_then_the_comment_link():
    task = tagged.Task(
        note=note(GCAL), kind="general", checklist="Frank",
        card_id="x", card_title="💎 General 09/09/26",
        summary="Add in gcalendar lunch + walk",
    )
    assert task.item() == (
        "Add in gcalendar lunch + walk\n"
        "https://trello.com/c/IU4PM7wJ#comment-c1"
    )


def test_a_short_comment_is_already_its_own_summary():
    assert tagged.brief_already(GCAL) is True
    assert tagged.trim(GCAL) == "Add in gcalendar lunch + walk"


def test_a_long_one_is_not():
    assert tagged.brief_already(BC_LP) is False


def test_the_fallback_summary_invents_nothing():
    """When the comment can't be read, the first line of it goes on rather than
    nothing at all — a line on the wrong list is recoverable, a task nobody
    wrote down is not."""
    written = tagged.trim(BC_LP)
    assert written.startswith("make BC BENEFITS LP")
    assert len(written.split()) <= tagged.MOST_WORDS


def test_the_tags_are_stripped_out_of_the_summary():
    assert "@" not in tagged.trim(CREATIVES)


def test_a_comment_that_is_only_a_tag_summarises_to_nothing():
    assert tagged.trim("@nic0l3") == ""


# ------------------------------------------------- the whole thing, on a board


class TaggedBoard:
    """The three cards, their checklists and their comments."""

    CARDS = {
        "general": {"id": "g", "name": "💎 General 09/09/26", "url": "https://trello.com/c/IU4PM7wJ/x"},
        "ops": {"id": "o", "name": "💻 Ops 09/09/26", "url": "https://trello.com/c/45pN1ggL/x"},
        "ads": {"id": "a", "name": "📊 Ads 09/09/26", "url": "https://trello.com/c/MHCAKIT1/x"},
    }
    HOLDS = {
        "g": ["Therese", "Faith", "Nicole", "Frank", "Kath"],
        "o": ["Therese", "Nicole"],
        "a": ["Jenn", "Kath", "Nicole"],
    }

    def __init__(self, comments):
        self.comments = comments
        self.written = []
        self.closed = False

    def board_lists(self, _board_id):
        return [{"id": "l1", "name": "Quality Check"}]

    def list_cards(self, _list_id):
        return list(self.CARDS.values())

    def card_checklists(self, card_id):
        return [
            {"id": f"{card_id}-{name}", "name": name, "checkItems": []}
            for name in self.HOLDS.get(card_id, [])
        ]

    def card_notes(self, card_id):
        return self.comments.get(card_id, [])

    def board_members(self, _board_id):
        return list(BOARD)

    def add_check_item(self, checklist_id, name, **kwargs):
        self.written.append((checklist_id, name))
        return {"id": "i1"}

    def close(self):
        self.closed = True


def planning(board, monkeypatch, config, *, read=None):
    from datetime import date

    from wilbyte.bot import jobs

    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)
    monkeypatch.setattr(jobs, "board_day", lambda cfg: date(2026, 9, 9))
    monkeypatch.setattr(jobs, "_ask_about_tags", read or (lambda *a, **k: {}))
    return jobs.tags_to_file(config)


def test_therese_tagged_on_general_lands_on_ops(config, monkeypatch):
    """"if Therese is tag on a comment on general card, you add them on the OPS
    card not on general card check list"."""
    board = TaggedBoard({"g": [
        {"id": "c1", "text": "@thereseguba pause the trucker distro", "author": "Frank"},
    ]})

    tasks, problems = planning(board, monkeypatch, config)

    assert problems == []
    (one,) = tasks
    assert (one.kind, one.checklist) == ("ops", "Therese")
    assert one.card_title == "💻 Ops 09/09/26"
    assert one.judged is False


def test_nicole_goes_to_ads_when_the_work_is_ads(config, monkeypatch):
    board = TaggedBoard({"g": [
        {"id": "c1", "text": "@nic0l3 Blue collar agents go live", "author": "Frank"},
    ]})

    tasks, _problems = planning(
        board, monkeypatch, config,
        read=lambda *a, **k: {"c1": {"summary": "Blue collar agents go live", "kind": "ads"}},
    )

    (one,) = tasks
    assert (one.kind, one.checklist) == ("ads", "Nicole")


def test_nicole_stays_on_general_when_the_work_is_admin(config, monkeypatch):
    board = TaggedBoard({"g": [
        {"id": "c1", "text": "@nic0l3 Add YT channel", "author": "Frank"},
    ]})

    tasks, _problems = planning(
        board, monkeypatch, config,
        read=lambda *a, **k: {"c1": {"summary": "Add YT channel", "kind": "general"}},
    )

    (one,) = tasks
    assert (one.kind, one.checklist) == ("general", "Nicole")


def test_at_card_goes_to_everybody_on_that_card(config, monkeypatch):
    """"if its card, just add them to all checklist of the card, this example
    add it to Jenn, Kath, and Nicole"."""
    board = TaggedBoard({"a": [
        {"id": "c1", "text": FOR_THE_CARD, "author": "Faith Hannah Calla"},
    ]})

    tasks, _problems = planning(board, monkeypatch, config)

    assert {one.checklist for one in tasks} == {"Jenn", "Kath", "Nicole"}
    assert {one.kind for one in tasks} == {"ads"}
    assert all(one.everyone for one in tasks)


def test_two_people_tagged_in_one_comment_get_one_line_each(config, monkeypatch):
    board = TaggedBoard({"g": [
        {"id": "c1", "text": "@nic0l3 @thereseguba test", "author": "Frank"},
    ]})

    tasks, _problems = planning(board, monkeypatch, config)

    assert {(one.kind, one.checklist) for one in tasks} == {
        ("ads", "Nicole"), ("ops", "Therese"),
    }


def test_somebody_with_no_checklist_is_named_not_guessed_at(config, monkeypatch):
    """Tre is tagged on cards all day and keeps no checklist on any of them."""
    board = TaggedBoard({"g": [
        {"id": "c1", "text": "@tretarpley have a look at this", "author": "Kath"},
    ]})

    tasks, problems = planning(board, monkeypatch, config)

    assert tasks == []
    assert any("@tretarpley" in one for one in problems)


def test_a_comment_already_filed_is_not_filed_again(config, monkeypatch):
    from wilbyte.bot import jobs

    board = TaggedBoard({"g": [
        {"id": "c1", "text": "@thereseguba pause the trucker distro", "author": "Frank"},
    ]})
    held = board.card_checklists
    board.card_checklists = lambda card_id: (
        [{"id": "o-Therese", "name": "Therese", "checkItems": [
            {"name": "pause it\nhttps://trello.com/c/IU4PM7wJ#comment-c1"}]}]
        if card_id == "o" else held(card_id)
    )

    tasks, _problems = planning(board, monkeypatch, config)

    assert tasks == []


def test_a_comment_nobody_tagged_is_not_a_task(config, monkeypatch):
    board = TaggedBoard({"g": [
        {"id": "c1", "text": "50-50 split LF and SF", "author": "Frank"},
    ]})

    tasks, problems = planning(board, monkeypatch, config)

    assert (tasks, problems) == ([], [])


def test_the_line_written_carries_the_link_back(config, monkeypatch):
    from wilbyte.bot import jobs

    board = TaggedBoard({"g": [
        {"id": "c1", "text": GCAL, "author": "Franklin May Maldonado"},
    ]})
    tasks, _problems = planning(board, monkeypatch, config)

    landed, problems = jobs.file_tags(config, tasks)

    assert problems == []
    (where, line) = board.written[0]
    assert where == "g-Frank"
    assert line == (
        "Add in gcalendar lunch + walk\nhttps://trello.com/c/IU4PM7wJ#comment-c1"
    )
    assert landed == ["💎 General 09/09/26 · Frank — Add in gcalendar lunch + walk"]


# --------------------------------------------------- watching, rather than asked


def test_the_watcher_costs_one_request_while_nothing_happens(config, monkeypatch):
    """One call to ask whether any of the three cards was touched at all. The
    comments and the checklists are only read once one has."""
    from datetime import date

    from wilbyte.bot import jobs

    asked = []

    class Quiet:
        def board_cards(self, board_id):
            asked.append(board_id)
            return [
                {"id": "g", "name": "💎 General 09/09/26", "dateLastActivity": "T1"},
                {"id": "o", "name": "💻 Ops 09/09/26", "dateLastActivity": "T2"},
                {"id": "a", "name": "📊 Ads 09/09/26", "dateLastActivity": "T3"},
            ]

        def close(self):
            pass

    monkeypatch.setattr(jobs, "open_trello", lambda cfg: Quiet())
    monkeypatch.setattr(jobs, "board_day", lambda cfg: date(2026, 9, 9))

    first = jobs.tags_stamp(config)
    again = jobs.tags_stamp(config)

    assert first == again
    assert len(asked) == 2  # one apiece, and nothing else was read


def test_a_new_comment_changes_the_stamp(config, monkeypatch):
    from datetime import date

    from wilbyte.bot import jobs

    when = {"g": "T1"}

    class Board:
        def board_cards(self, _board_id):
            return [
                {"id": "g", "name": "💎 General 09/09/26",
                 "dateLastActivity": when["g"]},
                {"id": "o", "name": "💻 Ops 09/09/26", "dateLastActivity": "T2"},
            ]

        def close(self):
            pass

    monkeypatch.setattr(jobs, "open_trello", lambda cfg: Board())
    monkeypatch.setattr(jobs, "board_day", lambda cfg: date(2026, 9, 9))

    before = jobs.tags_stamp(config)
    when["g"] = "T9"

    assert jobs.tags_stamp(config) != before


def test_the_lead_order_card_is_not_watched(config, monkeypatch):
    """Nothing gets tagged onto it — what goes on it is what agents bought."""
    from datetime import date

    from wilbyte.bot import jobs

    class Board:
        def board_cards(self, _board_id):
            return [
                {"id": "g", "name": "💎 General 09/09/26", "dateLastActivity": "T1"},
                {"id": "lo", "name": "Lead Order 09/09/26", "dateLastActivity": "T4"},
            ]

        def close(self):
            pass

    monkeypatch.setattr(jobs, "open_trello", lambda cfg: Board())
    monkeypatch.setattr(jobs, "board_day", lambda cfg: date(2026, 9, 9))

    assert "lead_order" not in jobs.tags_stamp(config)


# ------------------------------------------------------ what a tag actually is


AGENT_TALK = (
    "✅ OTP VET ON DISTRO HUB setup is complete for CORBIN SIMPSON\n"
    "✅ @Corbin Simpson\nReady to go live Thursday, Sep 10"
)
JUST_A_LINK = "@kathleenmarie15 https://chatgpt.com/s/m_6aa1df52eb58819191b4aee60be120ee"


def test_an_agents_name_is_not_a_tag(config, monkeypatch):
    """Therese's confirmations say "@Corbin Simpson" and Faith writes "@jadon".
    Those are the agents being talked about, not somebody being given a job —
    Trello renders them as plain text for the same reason."""
    board = TaggedBoard({"o": [
        {"id": "c1", "text": AGENT_TALK, "author": "Therese Guba"},
    ]})

    tasks, problems = planning(board, monkeypatch, config)

    assert tasks == []
    assert problems == []  # and not thirteen names nobody recognises


def test_somebody_really_on_the_board_is_still_named(config, monkeypatch):
    """Tre is a member. He keeps no checklist, so his line is a job nobody
    wrote down — worth saying, unlike an agent's name."""
    board = TaggedBoard({"g": [
        {"id": "c1", "text": "@tretarpley have a look at this", "author": "Kath"},
    ]})

    _tasks, problems = planning(board, monkeypatch, config)

    assert any("@tretarpley" in one for one in problems)


def test_a_pasted_link_is_not_a_summary(config, monkeypatch):
    """One went on as "[https://chatgpt.com/s/m_6aa1df...](https://chatgpt.com/s"
    — Discord had made half of it a markdown link and cut the rest. The link
    back to the comment is already on the line."""
    assert tagged.trim(JUST_A_LINK) == ""

    board = TaggedBoard({"a": [
        {"id": "c1", "text": JUST_A_LINK, "author": "Frank"},
    ]})
    tasks, _problems = planning(board, monkeypatch, config)

    assert tasks == []


def test_the_words_around_a_link_still_count():
    said = "@nic0l3 new creatives here https://drive.google.com/drive/folders/1CW"
    assert tagged.trim(said) == "new creatives here"
