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

    def __init__(self, comments, descs=None):
        self.comments = comments
        self.descs = descs or {}
        self.written = []
        self.closed = False

    def card_detail(self, card_id):
        return {"id": card_id, "desc": self.descs.get(card_id, "")}

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
        read=lambda *a, **k: {("c1", ""): {"summary": "Blue collar agents go live", "kind": "ads"}},
    )

    (one,) = tasks
    assert (one.kind, one.checklist) == ("ads", "Nicole")


def test_nicole_stays_on_general_when_the_work_is_admin(config, monkeypatch):
    board = TaggedBoard({"g": [
        {"id": "c1", "text": "@nic0l3 Add YT channel", "author": "Frank"},
    ]})

    tasks, _problems = planning(
        board, monkeypatch, config,
        read=lambda *a, **k: {("c1", ""): {"summary": "Add YT channel", "kind": "general"}},
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


# ------------------------------------ named without being tagged, and initials


ARNOLD = """Jenn = FRIDAY
- OTP VET removal and consolidation and swapping out content with creatives
Kath = FRIDAY
- MTG creatives and setting up individual ad sets for weekend launch
"""


def test_a_name_at_the_start_of_a_line_hands_the_work_over():
    """Arnold writes the week as "Jenn = FRIDAY" with the work under it and
    tags nobody. Those were jobs handed over that nothing wrote down."""
    found = tagged.named_without_tagging(ARNOLD, ["Jenn", "Kath", "Nicole"])

    assert set(found) == {"Jenn", "Kath"}
    assert "OTP VET removal" in found["Jenn"]
    assert "MTG creatives" in found["Kath"]


def test_the_lines_under_a_name_are_theirs_until_the_next_name():
    found = tagged.named_without_tagging(ARNOLD, ["Jenn", "Kath"])

    assert "MTG creatives" not in found["Jenn"]


def test_a_name_in_passing_is_not_a_job():
    """"ask Nicole about the budget" names her and hands her nothing."""
    assert tagged.named_without_tagging("ask Nicole about the budget", ["Nicole"]) == {}


def test_a_name_with_nothing_after_it_is_not_a_job():
    assert tagged.named_without_tagging("Jenn =", ["Jenn"]) == {}


def test_somebody_who_keeps_no_checklist_is_not_matched():
    assert tagged.named_without_tagging("Bob = FRIDAY\ndo it", ["Jenn"]) == {}


@pytest.mark.parametrize(
    "who, expected",
    [("Kharyl Maye Cañizares", "KC"), ("Kathleen Rabaya", "Kath"),
     ("Jennifer Hashisaki", "Jenn"), ("Nicole Sarmiento", "Nicole")],
)
def test_a_checklist_named_by_initials_is_still_theirs(who, expected):
    """Kharyl keeps one called "KC", which is not the start of her first name
    and is still hers."""
    board = ["Frank", "Kath", "Jenn", "Nicole", "Therese", "Faith", "KC"]

    assert tagged.checklist_for(who, board) == expected


def test_a_one_word_name_has_no_initials_to_match_on():
    """"K" is not enough to say whose list it is."""
    assert tagged.initials(["Kharyl"]) == set()


def test_only_the_short_forms_somebody_would_actually_write():
    """Every letter, and the first and last. Not the first two, which would
    give Kharyl a claim on a checklist called "KM"."""
    assert tagged.initials("Kharyl Maye Cañizares".split()) == {"kmc", "kc"}
    assert tagged.checklist_for("Kharyl Maye Cañizares", ["KM", "Frank"]) == ""


def test_the_tool_is_read_by_the_name_it_was_asked_for():
    """Borrowing the blog's reader meant every batch raised "Model did not
    call emit_blog_package", so every summary fell back to raw first words."""
    from wilbyte.bot import jobs

    class Block:
        type = "tool_use"
        name = "lines"
        input = {"lines": [{"comment_id": "c1", "summary": "ok", "kind": "ads"}]}

    class Answered:
        stop_reason = "tool_use"
        content = [Block()]

    assert jobs._tool_input(Answered(), "lines")["lines"][0]["summary"] == "ok"


def test_a_named_person_is_routed_like_a_tagged_one(config, monkeypatch):
    """Arnold writes "Jenn = FRIDAY" on the General card. Jenn does ads work,
    so it belongs on Ads — being named rather than tagged does not put it on
    whatever card it was written on, and does not make it everybody's."""
    board = TaggedBoard({"g": [
        {"id": "c1", "text": ARNOLD, "author": "Arnold Tarpley"},
    ]})

    tasks, _problems = planning(board, monkeypatch, config)

    assert {(one.kind, one.checklist) for one in tasks} == {
        ("ads", "Jenn"), ("ads", "Kath"),
    }
    assert not any(one.everyone for one in tasks)


def test_one_comment_can_hand_work_to_two_people(config, monkeypatch):
    """Jenn's line already being on the board is no reason to leave Kath
    without hers."""
    board = TaggedBoard({"g": [
        {"id": "c1", "text": ARNOLD, "author": "Arnold Tarpley"},
    ]})
    held = board.card_checklists
    board.card_checklists = lambda card_id: (
        [{"id": "a-Jenn", "name": "Jenn", "checkItems": [
            {"name": "x\nhttps://trello.com/c/IU4PM7wJ#comment-c1"}]},
         {"id": "a-Kath", "name": "Kath", "checkItems": []},
         {"id": "a-Nicole", "name": "Nicole", "checkItems": []}]
        if card_id == "a" else held(card_id)
    )

    tasks, _problems = planning(board, monkeypatch, config)

    assert [one.checklist for one in tasks] == ["Kath"]


def test_somebody_tagged_as_well_as_named_is_left_to_the_tag(config, monkeypatch):
    """The tag carries the whole comment; the name carries one slice."""
    board = TaggedBoard({"g": [
        {"id": "c1", "text": "@jenniferhashisaki2 all of it\n\nJenn = just this bit",
         "author": "Arnold Tarpley"},
    ]})

    tasks, _problems = planning(board, monkeypatch, config)

    # One line, not two. The tag already carries the whole comment, so the
    # name lower down is the same job said again.
    assert len([task for task in tasks if task.checklist == "Jenn"]) == 1


def test_the_preview_says_where_each_line_came_from():
    """"where did ryte get this" is a fair question to want answered before
    pressing the button rather than after."""
    task = tagged.Task(
        note=note("@nic0l3 Bump # of leads", comment_id="abc", short="MHCAKIT1"),
        kind="ads", checklist="Nicole", card_id="a", card_title="📊 Ads",
        summary="Bump # of leads to Connor Swartz's current setup",
    )

    said = tagged.describe(task)

    assert "https://trello.com/c/MHCAKIT1#comment-abc" in said
    assert "said here" in said


def test_a_line_with_no_card_to_link_to_still_reads():
    task = tagged.Task(
        note=tagged.Note(comment_id="abc", text="x"), kind="ads",
        checklist="Nicole", card_id="a", card_title="📊 Ads", summary="do it",
    )

    assert tagged.describe(task).endswith("do it")


# ----------------------------------------------------- an order already running


ONGOING = (
    "CONNOR SWARTZ has an ongoing order that still need to get fulfilled.\n\n"
    "@nic0l3 kindly bump # of leads to his current setup. thank you!"
)


def test_an_ongoing_order_is_left_alone(config, monkeypatch):
    """Connor's own card is already on Nicole's checklist — "if it says
    ongoing order specifically, dont add"."""
    board = TaggedBoard({"o": [
        {"id": "c1", "text": ONGOING, "author": "Therese Guba"},
    ]})

    tasks, problems = planning(board, monkeypatch, config)

    assert tasks == []
    # Said, not swallowed. A skip nobody can see is what has cost the most
    # time on this board.
    assert any("ongoing order" in one for one in problems)


@pytest.mark.parametrize(
    "said", ["ongoing order", "ongoing orders", "on-going order", "On Going Order"]
)
def test_the_words_themselves_however_they_are_written(said):
    assert tagged.an_ongoing_order(f"CONNOR has an {said} @nic0l3") is True


@pytest.mark.parametrize(
    "said",
    [
        "WILL SEITZ (vet)- can i completely pause my drip for tomorrow? @nic0l3",
        "KIMANI CHAMBLISS (VET)- 8AM-8PM EST LEAD SCHED @nic0l3",
        "the order is ongoing in a sense @nic0l3",
    ],
)
def test_a_real_job_about_an_agent_already_on_the_board_still_counts(said):
    """Most of what gets written on these cards is about an agent already on
    it, and asking to pause a drip is work somebody has to do."""
    assert tagged.an_ongoing_order(said) is False


# ------------------------------------------------------ jobs written in the card

# Off the real General card. Trello writes the nesting as indentation; the
# glyphs are what it renders back.
DESCRIPTION = """@nic0l3 @faithhannahcalla @thereseguba @kathleenmarie15 @franklinmaymaldonado

- @tretarpley
  - Aged distro udpate
  - What's login for active campaign
    - SMS is good now?
      - Use Ai to look up agency server/silo group to warmup
- @franklinmaymaldonado
  - Continue working on Ai SEO
- @nic0l3
  - More reminder for you, especially as within next month im going to be getting back on content heavy
    - What can you do/learn to make the YouTube/IG crank
- If we're going to turn off, turn off. If not duplicate bc this is only a retargeting ad rn
  - @jenniferhashisaki2
"""


def told(text):
    return tagged.description_tasks(text)[0]


def test_a_tag_with_lines_under_it_owns_every_one_of_them():
    """"on description just add the whole thing individually" — the sub-point
    is a job too, and folding it into the line above would lose it."""
    theirs = [one.text for one in told(DESCRIPTION) if one.username == "tretarpley"]
    assert theirs == [
        "Aged distro udpate",
        "What's login for active campaign",
        "SMS is good now?",
        "Use Ai to look up agency server/silo group to warmup",
    ]


def test_each_block_belongs_to_the_tag_that_opened_it():
    theirs = {one.username for one in told(DESCRIPTION)}
    assert theirs == {
        "tretarpley", "franklinmaymaldonado", "nic0l3", "jenniferhashisaki2",
    }
    frank = [one.text for one in told(DESCRIPTION)
             if one.username == "franklinmaymaldonado"]
    assert frank == ["Continue working on Ai SEO"]


def test_the_row_of_tags_at_the_top_is_who_it_is_addressed_to():
    """Five people named with nothing under them is the address on the card,
    not one job for all five."""
    for one in told(DESCRIPTION):
        assert "faithhannahcalla" != one.username
    assert not any(
        one.text.startswith("@") for one in told(DESCRIPTION)
    )


def test_a_tag_underneath_a_line_claims_the_line():
    """"@jenniferhashisaki2" sits under "If we're going to turn off" — the
    line above it is hers."""
    hers = [one.text for one in told(DESCRIPTION)
            if one.username == "jenniferhashisaki2"]
    assert hers == [
        "If we're going to turn off, turn off. If not duplicate bc this is "
        "only a retargeting ad rn"
    ]


def test_the_words_go_on_as_they_were_written():
    """Not summarised. The line is already the job, and its own words are the
    only thing that says later that it has been filed."""
    long = next(one for one in told(DESCRIPTION) if one.username == "nic0l3")
    assert long.text.startswith("More reminder for you, especially as within")
    assert long.text.endswith("getting back on content heavy")


def test_the_glyphs_carry_the_nesting_when_the_indentation_is_lost():
    """Somebody copying the rendered card back in loses the spaces."""
    theirs = told("• @nic0l3\n◦ Add YT channel\n◦ Check the budget")
    assert [one.username for one in theirs] == ["nic0l3", "nic0l3"]
    assert [one.text for one in theirs] == ["Add YT channel", "Check the budget"]


def test_a_description_that_tags_nobody_is_not_a_pile_of_tasks():
    """Most descriptions are notes. Nothing to file and nothing to complain
    about."""
    assert tagged.description_tasks(
        "Lead order for the day\n- 500 VET\n- 200 FEX"
    ) == ([], [])


def test_a_line_nobody_is_named_for_is_said_out_loud():
    theirs, nobody = tagged.description_tasks(
        "- @nic0l3\n  - Add YT channel\n- Somebody has to chase the invoice"
    )
    assert [one.text for one in theirs] == ["Add YT channel"]
    assert nobody == ["Somebody has to chase the invoice"]


def test_a_line_outside_a_block_belongs_to_whoever_it_names():
    theirs, nobody = tagged.description_tasks(
        "- @nic0l3\n  - Add YT channel\n- @thereseguba pause the trucker distro"
    )
    assert nobody == []
    assert ("thereseguba", "pause the trucker distro") in [
        (one.username, one.text) for one in theirs
    ]


# ------------------------------------------- the same line, the second afternoon


def test_a_line_already_on_the_list_is_matched_by_its_own_words():
    """A description line has no comment id to match on."""
    held = checklist("Aged distro udpate\nhttps://trello.com/c/IU4PM7wJ")
    assert tagged.already_said("Aged distro udpate", [held]) is True
    assert tagged.already_said("aged distro udpate.", [held]) is True
    assert tagged.already_said("Aged distro update", [held]) is False


def test_the_link_under_a_description_line_points_at_the_card():
    one = tagged.Note(
        comment_id="", text="Continue working on Ai SEO", card_short="IU4PM7wJ",
        described=True,
    )
    assert one.link() == "https://trello.com/c/IU4PM7wJ"


# -------------------------------------------------------------- on a whole board


def test_description_tasks_land_on_the_card_the_work_belongs_to(config, monkeypatch):
    """Therese's line is written on General and belongs on Ops, the same as a
    tag in a comment."""
    board = TaggedBoard({}, descs={"g": (
        "- @thereseguba\n"
        "  - Pause the trucker distro\n"
        "  - Send Monday's leads\n"
    )})

    tasks, problems = planning(board, monkeypatch, config)

    assert problems == []
    assert [(one.kind, one.checklist, one.summary) for one in tasks] == [
        ("ops", "Therese", "Pause the trucker distro"),
        ("ops", "Therese", "Send Monday's leads"),
    ]
    assert all(one.note.described for one in tasks)


def test_a_description_line_says_where_it_came_from(config, monkeypatch):
    board = TaggedBoard({}, descs={"g": "- @nic0l3\n  - Add YT channel\n"})

    tasks, _problems = planning(board, monkeypatch, config)

    said = tagged.describe(tasks[0])
    assert "in the description" in said
    assert "https://trello.com/c/IU4PM7wJ" in said


def test_a_description_line_already_filed_is_not_filed_again(config, monkeypatch):
    from wilbyte.bot import jobs

    board = TaggedBoard({}, descs={"g": "- @thereseguba\n  - Pause the trucker distro\n"})
    held = board.card_checklists

    def with_the_line(card_id):
        found = held(card_id)
        for one in found:
            if card_id == "o" and one["name"] == "Therese":
                one["checkItems"] = [
                    {"name": "Pause the trucker distro\nhttps://trello.com/c/IU4PM7wJ"}
                ]
        return found

    board.card_checklists = with_the_line
    monkeypatch.setattr(jobs, "_ask_about_tags", lambda *a, **k: {})

    tasks, problems = planning(board, monkeypatch, config)

    assert tasks == []
    assert problems == []


def test_somebody_tagged_in_the_description_with_no_checklist_is_named(config, monkeypatch):
    """Tre is on the board and keeps no checklist on any of today's cards."""
    board = TaggedBoard({}, descs={"g": "- @tretarpley\n  - Aged distro udpate\n"})

    tasks, problems = planning(board, monkeypatch, config)

    assert tasks == []
    assert any("tretarpley" in one and "Aged distro udpate" in one for one in problems)


# ------------------------------------------------------------------------ Elisa

ELISA = """- @elisadeko2
  - Aged distro udpate
  - What's login for active campaign
    - SMS is good now?
      - Use Ai to look up agency server/silo group to warmup
"""


def test_elisas_block_lands_on_general_once_she_keeps_a_checklist(config, monkeypatch):
    """"this is for Elisa / her checklist will be added on the general card
    going forward" — nothing to configure, the board says so."""
    board = TaggedBoard({}, descs={"g": ELISA})
    board.HOLDS = dict(TaggedBoard.HOLDS, g=[*TaggedBoard.HOLDS["g"], "Elisa"])
    monkeypatch.setattr(
        board, "board_members",
        lambda _b: [*BOARD, {"username": "elisadeko2", "fullName": "Elisa Deko"}],
    )

    tasks, problems = planning(board, monkeypatch, config)

    assert problems == []
    assert [(one.kind, one.checklist, one.summary) for one in tasks] == [
        ("general", "Elisa", "Aged distro udpate"),
        ("general", "Elisa", "What's login for active campaign"),
        ("general", "Elisa", "SMS is good now?"),
        ("general", "Elisa", "Use Ai to look up agency server/silo group to warmup"),
    ]


def test_until_then_her_four_lines_are_named_not_dropped(config, monkeypatch):
    """A tag in a description was typed to hand work over. Whether the reason
    is no checklist or not on the board, the work is still sitting there."""
    board = TaggedBoard({}, descs={"g": ELISA})

    tasks, problems = planning(board, monkeypatch, config)

    assert tasks == []
    # One line, not four. Four copies of the same sentence is four lines
    # nobody reads to the end of.
    (said,) = problems
    assert "@elisadeko2" in said
    assert "4 line(s)" in said
    assert "Aged distro udpate" in said


# --------------------------------------------- markdown a description came with


@pytest.mark.parametrize(
    "written, expected",
    [
        # Both real, off the General card. Trello's editor wrote them.
        (
            '[If we\'re going to turn off, turn off. If not duplicate bc this is '
            'only a retargeting ad rn]( "")',
            "If we're going to turn off, turn off. If not duplicate bc this is "
            "only a retargeting ad rn",
        ),
        ("_Use Ai to look up agency server/silo group to warmup_",
         "Use Ai to look up agency server/silo group to warmup"),
        ("**Aged distro udpate**", "Aged distro udpate"),
        ("`What's login for active campaign`", "What's login for active campaign"),
        ("[the sheet](https://docs.google.com/x)", "the sheet"),
        ("## SMS is good now?", "SMS is good now?"),
        # Left alone: not emphasis, just how somebody writes.
        ("OTP VET + removal 5*4 and lead_type stays", "OTP VET + removal 5*4 and lead_type stays"),
    ],
)
def test_the_line_goes_on_as_it_looks_on_the_card(written, expected):
    """Brackets and underscores nobody typed have no business on a checklist."""
    assert tagged.plain(written) == expected


def test_a_rule_across_the_page_is_not_a_job():
    """"⚠ …with nobody tagged for them, so I left them:" and then nothing —
    that was a line of markdown warning about itself."""
    theirs, nobody = tagged.description_tasks(
        "- @nic0l3\n  - Add YT channel\n\n***\n___\n"
    )
    assert [one.text for one in theirs] == ["Add YT channel"]
    assert nobody == []


def test_a_link_in_a_block_keeps_its_words():
    theirs, _nobody = tagged.description_tasks(
        "- @nic0l3\n  - **Check** the [budget](https://x.test) _today_\n"
    )
    assert [one.text for one in theirs] == ["Check the budget today"]


# --------------------------------------------------------------- lead schedules

# Both real, off the Ads card. Kath posts these all day and tags nobody half
# the time.
ANTHONY = "ANTHONY SINGH (VET)- monday- saturday 9 am- 9 pm"
KIMANI = "KIMANI CHAMBLISS (VET)- 8AM-8PM EST LEAD SCHED @nic0l3"


@pytest.mark.parametrize("said", [ANTHONY, KIMANI])
def test_a_schedule_is_read_as_one(said):
    assert tagged.a_lead_schedule(said) is True


@pytest.mark.parametrize(
    "said",
    [
        # An agent line, and a real job, but not a schedule.
        "WILL SEITZ (vet)- can i completely pause my drip for tomorrow? @nic0l3",
        # Hours, but nobody's schedule.
        "@franklinmaymaldonado standup moved to 9am-10am tomorrow",
        "Add in gcalendar lunch + walk",
        "New batch of creatives for VETS 2.0",
    ],
)
def test_what_is_not_a_schedule_is_left_to_the_tags(said):
    """Tight on purpose — without the agent line, "meeting 9am-10am" on the
    General card becomes a lead schedule for Nicole."""
    assert tagged.a_lead_schedule(said) is False


def test_an_untagged_schedule_still_lands_on_nicole(config, monkeypatch):
    """"if its schedule like this add to nicole on ads even if not tagged"."""
    board = TaggedBoard({"a": [
        {"id": "c1", "text": ANTHONY, "author": "Kharyl Maye Cañizares"},
    ]})

    tasks, problems = planning(board, monkeypatch, config)

    assert problems == []
    (one,) = tasks
    assert (one.kind, one.checklist) == ("ads", "Nicole")


def test_the_hours_go_on_whole(config, monkeypatch):
    """Trimmed to a line's worth, "9 am- 9 pm" loses the pm — and the hours
    are the whole content of a schedule."""
    board = TaggedBoard({"a": [
        {"id": "c1", "text": ANTHONY, "author": "Kharyl Maye Cañizares"},
    ]})

    tasks, _problems = planning(board, monkeypatch, config)

    assert tasks[0].summary == ANTHONY


def test_a_schedule_is_ads_work_whatever_the_reading_said(config, monkeypatch):
    """"sending leads" is ops in the abstract. The drip windows are set on the
    Ads card."""
    board = TaggedBoard({"g": [
        {"id": "c1", "text": ANTHONY, "author": "Kharyl Maye Cañizares"},
    ]})

    tasks, _problems = planning(
        board, monkeypatch, config,
        read=lambda *a, **k: {("c1", ""): {"summary": "Anthony's hours", "kind": "general"}},
    )

    assert (tasks[0].kind, tasks[0].checklist) == ("ads", "Nicole")


def test_a_schedule_already_filed_is_not_filed_again(config, monkeypatch):
    board = TaggedBoard({"a": [
        {"id": "c1", "text": ANTHONY, "author": "Kharyl Maye Cañizares"},
    ]})
    held = board.card_checklists

    def with_the_line(card_id):
        found = held(card_id)
        for one in found:
            if card_id == "a" and one["name"] == "Nicole":
                one["checkItems"] = [
                    {"name": f"{ANTHONY}\nhttps://trello.com/c/MHCAKIT1#comment-c1"}
                ]
        return found

    board.card_checklists = with_the_line
    tasks, problems = planning(board, monkeypatch, config)

    assert (tasks, problems) == ([], [])


def test_a_schedule_somebody_did_tag_goes_where_the_tag_says(config, monkeypatch):
    """Only the untagged ones are claimed. A schedule handed to somebody by
    name is theirs, and the tag says so."""
    board = TaggedBoard({"a": [
        {"id": "c1", "text": f"@thereseguba {ANTHONY}", "author": "Kath"},
    ]})

    tasks, _problems = planning(board, monkeypatch, config)

    assert [(one.kind, one.checklist) for one in tasks] == [("ops", "Therese")]


# ------------------------------------------- one comment, two people, two jobs

SHARED = (
    "@kharylmaye KC tell them about the Everlife aged lead discount; "
    "@faithhannahcalla Faith text Wolfpack agents"
)


def _both(_config, notes, _people):
    """Claude, told to write one line per tagged person, doing so."""
    said = notes[0].comment_id
    return {
        (said, "kharylmaye"): {
            "comment_id": said, "person": "kharylmaye",
            "summary": "Tell them about the Everlife aged lead discount",
            "kind": "general",
        },
        (said, "faithhannahcalla"): {
            "comment_id": said, "person": "faithhannahcalla",
            "summary": "Text Wolfpack agents", "kind": "general",
        },
        (said, ""): {
            "comment_id": said, "person": "kharylmaye",
            "summary": "Tell them about the Everlife aged lead discount",
            "kind": "general",
        },
    }


SHARERS = [
    {"username": "kharylmaye", "fullName": "Kharyl Maye Cañizares"},
    {"username": "faithhannahcalla", "fullName": "Faith Hannah Calla"},
]


def _sharing():
    board = TaggedBoard({"g": [{"id": "c1", "text": SHARED, "author": "Frank"}]})
    board.HOLDS = dict(TaggedBoard.HOLDS, g=[*TaggedBoard.HOLDS["g"], "KC"])
    return board


def test_two_people_in_one_comment_get_their_own_half(config, monkeypatch):
    """Both got the whole comment, and on the second run the summary had
    shortened to Faith's half — which told KC to text the Wolfpack agents."""
    board = _sharing()
    monkeypatch.setattr(
        board, "board_members",
        lambda _b: [*BOARD, *SHARERS],
    )

    tasks, _problems = planning(board, monkeypatch, config, read=_both)

    assert {one.checklist: one.summary for one in tasks} == {
        "KC": "Tell them about the Everlife aged lead discount",
        "Faith": "Text Wolfpack agents",
    }


def test_one_of_the_two_already_filed_does_not_cost_the_other_hers(config, monkeypatch):
    """Faith's line being there is no reason to leave KC without hers."""
    board = _sharing()
    monkeypatch.setattr(
        board, "board_members",
        lambda _b: [*BOARD, *SHARERS],
    )
    held = board.card_checklists

    def with_faiths(card_id):
        found = held(card_id)
        for one in found:
            if card_id == "g" and one["name"] == "Faith":
                one["checkItems"] = [
                    {"name": "Text Wolfpack agents\n"
                             "https://trello.com/c/IU4PM7wJ#comment-c1"}
                ]
        return found

    board.card_checklists = with_faiths

    tasks, _problems = planning(board, monkeypatch, config, read=_both)

    assert [one.checklist for one in tasks] == ["KC"]


def test_one_job_for_both_still_goes_to_both(config, monkeypatch):
    """"@card"-style work handed to two people is not two different jobs."""
    board = _sharing()
    monkeypatch.setattr(
        board, "board_members",
        lambda _b: [*BOARD, *SHARERS],
    )
    same = {
        ("c1", ""): {"summary": "Chase the Everlife discount", "kind": "general"},
    }

    tasks, _problems = planning(board, monkeypatch, config, read=lambda *a, **k: same)

    assert [one.summary for one in tasks] == [
        "Chase the Everlife discount", "Chase the Everlife discount",
    ]


# ------------------------------------------------------ a comment stays on its day

TWO_DAYS = {
    "general": {"id": "g", "name": "💎 General 09/09/26", "url": "https://trello.com/c/IU4PM7wJ/x"},
    "ops": {"id": "o", "name": "💻 Ops 09/09/26", "url": "https://trello.com/c/45pN1ggL/x"},
    "ads": {"id": "a", "name": "📊 Ads 09/09/26", "url": "https://trello.com/c/MHCAKIT1/x"},
    "general2": {"id": "g2", "name": "💎 General 09/10/26", "url": "https://trello.com/c/AAAA1111/x"},
    "ops2": {"id": "o2", "name": "💻 Ops 09/10/26", "url": "https://trello.com/c/BBBB2222/x"},
    "ads2": {"id": "a2", "name": "📊 Ads 09/10/26", "url": "https://trello.com/c/CCCC3333/x"},
}


class Tomorrow(TaggedBoard):
    """Today's three cards and tomorrow's, both open at once."""

    CARDS = TWO_DAYS
    HOLDS = {
        "g": ["Therese", "Faith", "Nicole", "Frank", "Kath"],
        "o": ["Therese", "Nicole"],
        "a": ["Jenn", "Kath", "Nicole"],
        "g2": ["Therese", "Faith", "Nicole", "Frank", "Kath"],
        "o2": ["Therese", "Nicole"],
        "a2": ["Jenn", "Kath", "Nicole"],
    }


def test_a_comment_on_tomorrows_card_lands_on_tomorrows_checklist(config, monkeypatch):
    """"if today still theres a card for 9/12 and theres a comment there, it
    will be added to 9/12"."""
    board = Tomorrow({"g2": [
        {"id": "c9", "text": "@thereseguba pause the trucker distro", "author": "Frank"},
    ]})

    tasks, problems = planning(board, monkeypatch, config)

    assert problems == []
    (one,) = tasks
    assert one.card_title == "💻 Ops 09/10/26"


def test_todays_comment_does_not_wander_onto_tomorrow(config, monkeypatch):
    board = Tomorrow({"g": [
        {"id": "c1", "text": "@thereseguba pause the trucker distro", "author": "Frank"},
    ]})

    tasks, _problems = planning(board, monkeypatch, config)

    assert [one.card_title for one in tasks] == ["💻 Ops 09/09/26"]


def test_both_days_come_back_together_today_first(config, monkeypatch):
    board = Tomorrow({
        "g": [{"id": "c1", "text": "@nic0l3 Add YT channel", "author": "Frank"}],
        "g2": [{"id": "c9", "text": "@nic0l3 Check the budget", "author": "Frank"}],
    })

    tasks, _problems = planning(board, monkeypatch, config)

    assert [one.card_title for one in tasks] == [
        "📊 Ads 09/09/26", "📊 Ads 09/10/26",
    ]


def test_yesterdays_card_is_finished_and_left_alone(config, monkeypatch):
    """A card in Done is done. Reading its comments would file work onto a
    checklist nobody is going to look at again."""
    from datetime import date

    from wilbyte.bot import jobs

    board = Tomorrow({"g": [
        {"id": "c1", "text": "@nic0l3 Add YT channel", "author": "Frank"},
    ]})
    monkeypatch.setattr(jobs, "open_trello", lambda cfg: board)
    monkeypatch.setattr(jobs, "board_day", lambda cfg: date(2026, 9, 10))
    monkeypatch.setattr(jobs, "_ask_about_tags", lambda *a, **k: {})

    tasks, _problems = jobs.tags_to_file(config)

    assert tasks == []


def test_a_week_written_by_name_goes_to_each_persons_own_card(config, monkeypatch):
    """Arnold hands Jenn "OTP VET removal" and Kath "MTG creatives" in one
    comment that tags nobody, and one kind read off the whole of it put both
    of them on General as a judgement call — when their work is ads work and
    their own card was never in doubt."""
    board = TaggedBoard({"g": [{
        "id": "c1", "author": "Arnold",
        "text": (
            "Jenn = FRIDAY\n"
            "- OTP VET + removal and consolidation\n"
            "Kath = FRIDAY\n"
            "- MTG creatives and setting up individual ad sets\n"
        ),
    }]})

    tasks, _problems = planning(
        board, monkeypatch, config,
        read=lambda *a, **k: {("c1", ""): {"summary": "the week", "kind": "general"}},
    )

    assert {one.checklist: (one.kind, one.judged) for one in tasks} == {
        "Jenn": ("ads", False),
        "Kath": ("ads", False),
    }
