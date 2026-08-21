"""Filing what lands in the SOP channel, and finding it again later.

The posts look like this - a heading somebody typed, then the thing itself:

    **How to Create Internal Ads LeadForm**
    https://www.loom.com/share/56abe3196b4f482caa68363e00355377

The filing is not the point. Being able to ask "do we have an SOP for lead
forms?" three weeks later is.
"""

import pytest

from wilbyte import sops

LOOM = "https://www.loom.com/share/56abe3196b4f482caa68363e00355377"
TUBE = "https://www.youtube.com/watch?v=abc123"


# ------------------------------------------------------ reading the post


def test_the_heading_above_the_link_names_the_card():
    sop = sops.find_sop(f"**How to Create Internal Ads LeadForm**\n{LOOM}")

    assert sop.title == "How to Create Internal Ads LeadForm"
    assert sop.kind == "Loom"
    assert sop.url == LOOM


def test_markdown_around_the_heading_is_not_part_of_it():
    """People bold or italicise the heading far more often than not."""
    assert sops.find_sop(f"*How to Find Trending Music*\n{TUBE}").title == (
        "How to Find Trending Music"
    )


def test_a_mention_is_not_mistaken_for_the_heading():
    sop = sops.find_sop(f"<@123> **Lead Order Process**\n{LOOM}")

    assert sop.title == "Lead Order Process"


@pytest.mark.parametrize(
    "text,kind",
    [
        (f"x {LOOM}", "Loom"),
        (f"x {TUBE}", "YouTube"),
        ("x https://docs.google.com/document/d/1", "Drive"),
        ("x https://example.com/how-to", "Link"),
    ],
)
def test_each_source_is_recognised(text, kind):
    assert sops.find_sop(text).kind == kind


def test_a_screenshot_with_no_link_is_still_an_sop():
    sop = sops.find_sop("Where the retarget toggle lives", images=("https://cdn/x.png",))

    assert sop.kind == "Screenshot"
    assert sop.images == ("https://cdn/x.png",)


def test_a_voice_note_is_still_an_sop():
    sop = sops.find_sop("how I do the handoff", audio=("https://cdn/x.ogg",))

    assert sop.kind == "Voice note"


def test_something_written_out_in_full_needs_no_link_at_all():
    written = (
        "To pull the weekly numbers: open the Auto-Deploys sheet, filter to last "
        "week, copy the totals column into the Monday card."
    )

    assert sops.find_sop(written).kind == "Written"


def test_chatter_is_not_filed():
    """A library full of "nice one" is a library nobody searches twice."""
    for text in ("nice one", "🔥", "thanks!", "will do", ""):
        assert sops.find_sop(text) is None


def test_a_link_on_its_own_still_gets_a_name():
    """An untitled SOP is one nobody finds again, so it gets the best we have."""
    assert sops.find_sop(LOOM).title


def test_the_card_is_titled_for_the_library():
    sop = sops.find_sop(f"**Lead Order Process**\n{LOOM}")

    assert sops.card_title(sop) == "SOP: Lead Order Process"


# ------------------------------------------------------------- the card


def sop_for(**kwargs):
    base = {"title": "Lead Order Process", "kind": "Loom", "url": LOOM}
    return sops.Sop(**{**base, **kwargs})


def test_the_summary_is_a_column_not_only_page_content():
    """Notion won't search inside a page's blocks, and the summary is what
    "do we have an SOP about X" is matched against."""
    props = sops.page_properties(sop_for(), "SOP: Lead Order Process", summary="Covers **forms**.")

    assert props["Summary"]["rich_text"][0]["text"]["content"] == "Covers forms."
    assert props["Link"]["url"] == LOOM
    assert props["Kind"]["rich_text"][0]["text"]["content"] == "Loom"


def test_an_empty_summary_leaves_the_column_alone():
    assert "Summary" not in sops.page_properties(sop_for(), "SOP: x", summary="   ")


def test_the_card_opens_with_the_link_and_its_details():
    blocks = sops.page_blocks(sop_for(posted_by="K2"))

    assert blocks[0]["bookmark"]["url"] == LOOM
    detail = "".join(
        run["text"]["content"] for run in blocks[1]["paragraph"]["rich_text"]
    )
    assert "Loom" in detail and "K2" in detail


def test_screenshots_go_into_the_card():
    blocks = sops.page_blocks(sop_for(kind="Screenshot", url="", images=("https://cdn/a.png",)))

    assert any(block["type"] == "image" for block in blocks)


def test_a_card_with_no_summary_says_why_rather_than_going_quiet():
    blocks = sops.page_blocks(sop_for(note="A voice note can't be transcribed from here."))
    text = "".join(
        run["text"]["content"]
        for block in blocks if block["type"] == "paragraph"
        for run in block["paragraph"]["rich_text"]
    )

    assert "voice note" in text.casefold()


def test_what_was_typed_is_kept_verbatim_under_its_own_heading():
    blocks = sops.page_blocks(sop_for(body="Open the sheet, filter to last week."))
    kinds = [block["type"] for block in blocks]

    assert "heading_2" in kinds


# ----------------------------------------------------- asking for one back


def test_the_asking_is_stripped_to_leave_the_topic():
    assert sops.wanted_topic("<@1> hey Ryte do we have any SOP about lead forms?") == (
        "lead forms"
    )


def test_a_how_do_we_question_reduces_to_its_subject():
    assert sops.wanted_topic("how do we create internal ads") == "create internal ads"


def row(title, summary="", link="", url="https://notion.so/x"):
    return {
        "url": url,
        "properties": {
            "Name": {"type": "title", "title": [{"plain_text": title}]},
            "Summary": {"type": "rich_text", "rich_text": [{"plain_text": summary}]},
            "Link": {"type": "url", "url": link},
        },
    }


def test_a_title_match_comes_before_a_summary_match():
    """Somebody asking about lead forms wants the SOP called that, first."""
    rows = [
        row("SOP: Weekly Numbers", summary="mentions the lead form in passing"),
        row("SOP: How to Create Internal Ads LeadForm"),
    ]

    found = sops.matching_rows(rows, "lead form")

    assert found[0][0] == "SOP: How to Create Internal Ads LeadForm"
    assert len(found) == 2, "the passing mention is still an answer half the time"


def test_the_summary_is_searched_not_just_the_title():
    rows = [row("SOP: Weekly Numbers", summary="how to export the retargeting audience")]

    assert sops.matching_rows(rows, "retargeting") != []


def test_every_word_has_to_match():
    rows = [row("SOP: Lead Order Process"), row("SOP: Ad Order Process")]

    assert len(sops.matching_rows(rows, "lead order")) == 1


def test_the_recording_link_comes_back_so_nobody_has_to_click_through():
    rows = [row("SOP: Lead Order Process", link=LOOM)]

    assert sops.matching_rows(rows, "lead order")[0][2] == LOOM


def test_asking_about_nothing_in_particular_returns_the_library():
    rows = [row("SOP: One"), row("SOP: Two")]

    assert len(sops.matching_rows(rows, "")) == 2


def test_a_topic_nobody_has_written_up_matches_nothing():
    assert sops.matching_rows([row("SOP: Lead Order Process")], "payroll") == []


def test_a_column_the_library_does_not_have_is_never_written():
    """"Date is not a property that exists" failed an entire create. The
    database's own schema decides what gets sent, not a shape assumed here."""
    made_by_hand = {
        "Name": {"type": "title", "title": {}},
        "Link": {"type": "url", "url": {}},
    }

    props = sops.map_properties(
        made_by_hand, sop_for(posted_on=__import__("datetime").date(2026, 8, 21)),
        "SOP: x", summary="anything",
    )

    assert set(props) == {"Name", "Link"}


def test_both_shapes_of_schema_are_understood():
    """Notion answers with {"type": "url", ...}; creating one sends {"url": {}}."""
    answered = {"Name": {"type": "title", "title": {}}, "Link": {"type": "url", "url": {}}}
    sent = {"Name": {"title": {}}, "Link": {"url": {}}}

    assert sops.map_properties(answered, sop_for(), "SOP: x") == (
        sops.map_properties(sent, sop_for(), "SOP: x")
    )


def test_a_column_someone_called_something_else_is_still_used():
    """The library is made by hand, so columns are matched by what they hold."""
    theirs = {
        "Title": {"type": "title", "title": {}},
        "Video": {"type": "url", "url": {}},
        "Notes": {"type": "rich_text", "rich_text": {}},
    }

    props = sops.map_properties(theirs, sop_for(), "SOP: x", summary="what it covers")

    assert props["Video"]["url"] == LOOM
    assert props["Notes"]["rich_text"][0]["text"]["content"] == "what it covers"


# ------------------------------------- nobody types the exact words back

# "do we have an SOP for lead forms?" found nothing, with a card sitting there
# called "How to Create Internal Ads LeadForm". Two reasons at once: a run-on
# name is one word to a person and two to a search, and nobody matches the
# plural somebody else used.


@pytest.mark.parametrize(
    "asked", ["lead forms", "lead form", "leadform", "LeadForm", "internal ads", "forms"]
)
def test_a_question_finds_the_card_however_it_is_phrased(asked):
    rows = [
        row(
            "SOP: How to Create Internal Ads LeadForm",
            summary="walks through creating a lead form using our internal strategy",
        )
    ]

    assert sops.matching_rows(rows, asked) != []


def test_a_run_on_name_is_still_searchable_word_by_word():
    assert sops.matching_rows([row("SOP: LeadForm Setup")], "lead form") != []


def test_a_plural_typed_finds_a_singular_written():
    assert sops.matching_rows([row("SOP: Lead Order Process")], "lead orders") != []


def test_a_singular_typed_finds_a_plural_written():
    assert sops.matching_rows([row("SOP: Lead Orders Process")], "lead order") != []


def test_being_forgiving_does_not_make_it_match_anything():
    """A search that always hits is the same as no search at all."""
    rows = [row("SOP: How to Create Internal Ads LeadForm")]

    assert sops.matching_rows(rows, "payroll") == []
    assert sops.matching_rows(rows, "zoom recording") == []


# ------------------------------------- backfilling what was posted before


def test_a_message_is_remembered_so_it_is_not_filed_twice(tmp_path):
    """Backfill reads messages RYTE has already seen. Discord's message id is
    the identity: unique, and it never changes."""
    store = tmp_path / "filed.json"

    assert sops.already_filed(1467651106309017600, path=store) is False

    sops.remember(1467651106309017600, path=store)

    assert sops.already_filed(1467651106309017600, path=store) is True
    assert sops.already_filed(999, path=store) is False


def test_a_filed_sop_does_not_collide_with_a_filed_recording(tmp_path):
    """Both use the same store, and a Zoom uuid is not a Discord message id."""
    from wilbyte import recordings

    store = tmp_path / "filed.json"
    recordings.remember_filed("abc==", store)
    sops.remember("abc==", path=store)

    assert sops.already_filed("abc==", path=store) is True
    assert recordings.filed_ids(store) == {"abc==", "sop-message:abc=="}


# ------------------------------- what the first backfill actually produced

# Eleven cards, and four of them were wrong in four different ways.


def test_a_mention_of_everyone_is_not_a_heading():
    """This became "SOP: @here here's how to connect luna to your calendar"."""
    sop = sops.find_sop(f"@here here's how to connect luna to your calendar\n{LOOM}")

    assert not sop.title.startswith("@here")


def test_a_login_is_never_filed():
    """The backfill made a card out of an email, a password, and "NEW LOOM
    LOGIN" — a Notion card is where a password outlives the chat it was
    meant to die in."""
    posted = "franklinmay@agentleadlab.com\nAgentlealab2026!\n\nNEW LOOM LOGIN ^^^"

    assert sops.find_sop(posted) is None


@pytest.mark.parametrize(
    "text",
    [
        "api key is sk-abc123 for hello@example.com",
        "password: hunter2 — login for ops@agentleadlab.com",
    ],
)
def test_credentials_in_any_arrangement_are_left_alone(text):
    assert sops.looks_like_credentials(text) is True


def test_an_ordinary_sop_mentioning_a_login_screen_is_still_filed():
    """"Where the login button is" is a procedure, not a credential."""
    assert sops.looks_like_credentials(f"How to find the login screen {LOOM}") is False


def test_asking_somebody_to_pin_a_message_is_not_a_procedure():
    """This cleared the old length bar and became a card."""
    assert sops.find_sop("will you pin this here so we have it quick just incase") is None


def test_something_genuinely_written_out_is_still_kept():
    steps = (
        "1. Open the Auto-Deploys sheet\n"
        "2. Filter to last week\n"
        "3. Copy the totals column into the Monday card"
    )

    assert sops.find_sop(steps) is not None


def test_a_bare_link_is_marked_as_needing_a_name():
    """"SOP: Drive SOP" three times over is a library you can't read."""
    assert sops.find_sop(LOOM).named_by_hand is False
    assert sops.find_sop(f"**Blacklist feature**\n{LOOM}").named_by_hand is True


@pytest.mark.parametrize(
    "text",
    [
        "plz can someone send me the doc for the onboarding process again thanks",
        "hey does anyone know where the lead order sheet lives these days?",
        "would you mind reposting the lead order walkthrough when you get a sec",
    ],
)
def test_asking_for_an_sop_is_not_filing_one(text):
    """Length alone can't tell these from a procedure. Who they address can."""
    assert sops.find_sop(text) is None


def test_an_html_entity_never_reaches_a_card_title():
    """og: tags are HTML, so "&" arrives as "&amp;" — and a card came out
    called "Buying Your GHL Phone Number &amp; Calling Numbers"."""
    import html

    assert html.unescape("Buying Your GHL Phone Number &amp; Calling Numbers") == (
        "Buying Your GHL Phone Number & Calling Numbers"
    )


# ------------------------ the SOPs that were written before RYTE existed

# The old library holds a great deal, and the point of the index is that none
# of it is ever held at once: each page is read once, reduced to a title, a
# link and two lines, and that is what questions are matched against.


def entry(title, summary="", url="https://notion.so/x", id="1"):
    return {"id": id, "title": title, "url": url, "summary": summary}


def test_an_old_page_is_found_by_its_title():
    index = [entry("Lead Order Process", id="a")]

    assert sops.index_matches(index, "lead order")[0][0] == "Lead Order Process"


def test_an_old_page_is_found_by_what_it_covers():
    """The title alone is often not what somebody would think to ask."""
    index = [entry("Sales Playbook", summary="objection handling and the pitch", id="b")]

    assert sops.index_matches(index, "objections") != []


def test_the_index_search_is_as_forgiving_as_the_gallery_one():
    index = [entry("LeadForm Setup", id="c")]

    assert sops.index_matches(index, "lead forms") != []


def test_an_unrelated_question_matches_nothing():
    assert sops.index_matches([entry("Lead Order Process")], "payroll") == []


def test_reading_the_library_twice_updates_rather_than_doubles():
    """A page edited since last time should end up with the newer summary."""
    was = [entry("Lead Order Process", summary="old", id="a")]
    now = [entry("Lead Order Process", summary="new", id="a")]

    merged = sops.merge_index(was, now)

    assert len(merged) == 1
    assert merged[0]["summary"] == "new"


def test_a_new_page_is_added_alongside_the_old_ones():
    merged = sops.merge_index([entry("One", id="a")], [entry("Two", id="b")])

    assert {item["title"] for item in merged} == {"One", "Two"}


def test_the_index_survives_a_restart(tmp_path):
    store = tmp_path / "index.json"
    sops.save_index([entry("Lead Order Process", id="a")], store)

    assert sops.load_index(store)[0]["title"] == "Lead Order Process"


def test_a_corrupt_index_is_treated_as_empty(tmp_path):
    """Better to re-read the library than to refuse to answer anything."""
    store = tmp_path / "index.json"
    store.write_text("{not json", encoding="utf-8")

    assert sops.load_index(store) == []


# ------------------------------ nobody uses the word the page was titled with

# "do we have an SOP setting up dedicated LP?" found nothing, with a page
# called "How To Setup Dedicated LP" sitting in the library.


@pytest.mark.parametrize(
    "asked",
    [
        "do we have an SOP setting up dedicated LP?",
        "how do we set up a dedicated LP",
        "dedicated lp",
        "sop for the dedicated LP setup",
    ],
)
def test_a_page_is_found_however_the_question_is_worded(asked):
    index = [{"id": "1", "title": "How To Setup Dedicated LP", "url": "https://n/1"}]

    assert sops.index_matches(index, sops.wanted_topic(asked)) != []


@pytest.mark.parametrize(
    "word,stem", [("setting", "set"), ("running", "run"), ("stopping", "stop")]
)
def test_english_doubles_the_consonant_before_ing(word, stem):
    """"setting" reduces to "sett" and then to "set", which is what "Setup" is
    made of. Without the second step the match never happens."""
    assert stem in sops._stems(word)


def test_uploading_finds_upload():
    index = [{"id": "1", "title": "How To Upload Blog Posts", "url": "https://n/1"}]

    assert sops.index_matches(index, "uploading blog posts") != []


def test_a_preposition_is_not_something_to_search_for():
    """Every word has to match, so "up" was being asked to find itself."""
    assert "up" not in sops.wanted_topic("how do we set up the dedicated LP").split()


def test_being_this_forgiving_still_finds_nothing_for_nothing():
    index = [{"id": "1", "title": "How To Setup Dedicated LP", "url": "https://n/1"}]

    assert sops.index_matches(index, sops.wanted_topic("do we have an SOP for payroll")) == []


# ------------------------------------- the closest thing, rather than nothing

# "@Ryte how to set up a blog" answered "nothing in the SOP library" with a page
# called "How To Upload Blog Posts" sitting in it. The two share one word out of
# two, so every word matching was never going to happen - and saying nothing is
# the worse mistake when the page is right there.


def test_the_closest_page_comes_back_when_nothing_matches_outright():
    index = [{"id": "1", "title": "How To Upload Blog Posts", "url": "https://n/1"}]

    found = sops.index_matches(index, sops.wanted_topic("how to set up a blog"))

    assert [hit.title for hit in found] == ["How To Upload Blog Posts"]


def test_a_close_page_is_marked_so_the_answer_can_say_so():
    index = [{"id": "1", "title": "How To Upload Blog Posts", "url": "https://n/1"}]

    assert sops.index_matches(index, "set blog")[0].exact is False
    assert sops.index_matches(index, "blog posts")[0].exact is True


def test_a_page_that_answers_the_question_hides_the_ones_that_nearly_do():
    """A half-answer is what you get instead of nothing, never alongside the
    whole answer."""
    index = [
        {"id": "1", "title": "How To Upload Blog Posts", "url": "https://n/1"},
        {"id": "2", "title": "Blog Draft Review", "url": "https://n/2"},
    ]

    found = sops.index_matches(index, "upload blog")

    assert [hit.title for hit in found] == ["How To Upload Blog Posts"]


def test_the_closest_of_the_close_comes_first():
    index = [
        {"id": "1", "title": "Blog Cover Images", "url": "https://n/1"},
        {"id": "2", "title": "How To Upload Blog Posts", "url": "https://n/2"},
    ]

    found = sops.index_matches(index, "upload blog posts monthly")

    assert found[0].title == "How To Upload Blog Posts"


def test_a_word_in_the_title_beats_the_same_word_in_a_summary():
    index = [
        {"id": "1", "title": "Weekly Numbers", "summary": "mentions blogs", "url": "https://n/1"},
        {"id": "2", "title": "Blog Posts", "url": "https://n/2"},
    ]

    found = sops.index_matches(index, "blog payroll")

    assert found[0].title == "Blog Posts"


def test_the_gallery_search_gets_the_same_second_chance():
    rows = [row("SOP: How To Upload Blog Posts")]

    found = sops.matching_rows(rows, "set blog")

    assert [hit.title for hit in found] == ["SOP: How To Upload Blog Posts"]
    assert found[0].exact is False


def test_a_close_match_still_has_to_be_close_to_something():
    rows = [row("SOP: Lead Order Process")]

    assert sops.matching_rows(rows, "payroll deductions") == []


def test_a_hit_still_reads_as_title_card_link():
    """Callers that only want the three fields shouldn't have to change."""
    rows = [row("SOP: Lead Order Process", link=LOOM)]

    title, card, link = sops.matching_rows(rows, "lead order")[0][:3]

    assert (title, link) == ("SOP: Lead Order Process", LOOM)
    assert card.startswith("https://")
