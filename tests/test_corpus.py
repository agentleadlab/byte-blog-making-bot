"""Corpus ingestion, storage and retrieval."""

import json

import pytest

from wilbyte.corpus import Corpus, CorpusError, build_piece, make_id, parse_upload
from wilbyte.formats import find, find_label, guess_label

SMS_TEXT = "aged leads are $2.50 right now. want 100 of them before friday? reply YES"
EMAIL_TEXT = "Subject: Your leads are getting stale\n\nHey, quick one.\n\nThe leads you bought..."


@pytest.fixture
def store(tmp_path):
    return Corpus(tmp_path / "corpus")


# ------------------------------------------------------------------- ingestion


def test_plain_text_becomes_one_piece(store):
    pieces = parse_upload(SMS_TEXT, filename="blast.txt", label="sms")

    assert len(pieces) == 1
    assert pieces[0].label == "sms"
    assert pieces[0].text == SMS_TEXT


def test_dashes_split_a_file_into_many_pieces():
    text = f"{SMS_TEXT}\n---\nsecond message about live transfers going out today\n---\nthird one here about otp leads"

    pieces = parse_upload(text, filename="sms.txt")

    assert len(pieces) == 3
    assert all(p.label == "sms" for p in pieces)  # label inferred from filename


def test_thin_fragments_are_dropped():
    pieces = parse_upload("ok\n---\n" + SMS_TEXT, filename="x.txt")

    assert len(pieces) == 1


def test_csv_with_a_format_column(store):
    csv_text = (
        "format,title,body\n"
        f"sms,Friday blast,{SMS_TEXT}\n"
        'email,Stale leads,"Subject: hey there, this is the body of an email we sent"\n'
    )

    pieces = parse_upload(csv_text, filename="copy.csv")

    assert [p.label for p in pieces] == ["sms", "email"]
    assert pieces[0].title == "Friday blast"


def test_csv_accepts_alternative_column_names():
    pieces = parse_upload(f"type,copy\ntext,{SMS_TEXT}\n", filename="c.csv")

    assert pieces[0].label == "sms"  # "text" is an alias for sms


def test_csv_without_a_body_column_says_so():
    with pytest.raises(CorpusError, match="body, copy, text"):
        parse_upload("name,notes\nfoo,bar\n", filename="bad.csv")


def test_jsonl_upload():
    lines = "\n".join([
        json.dumps({"format": "sms", "body": SMS_TEXT}),
        json.dumps({"format": "email", "body": EMAIL_TEXT}),
    ])

    pieces = parse_upload(lines, filename="copy.jsonl")

    assert [p.label for p in pieces] == ["sms", "email"]


def test_json_array_upload():
    data = json.dumps([
        {"channel": "email", "subject": "Hi", "body": EMAIL_TEXT},
        {"channel": "sms", "body": SMS_TEXT},
    ])

    pieces = parse_upload(data, filename="copy.json")

    assert [p.label for p in pieces] == ["email", "sms"]


def test_explicit_label_overrides_everything():
    pieces = parse_upload(f"format,body\nemail,{SMS_TEXT}\n", filename="x.csv", label="ad")

    assert pieces[0].label == "ad"


# --------------------------------------------------------------------- storage


def test_pieces_survive_a_reload(store):
    store.add(parse_upload(SMS_TEXT, filename="a.txt", label="sms"))

    reopened = Corpus(store.dir)

    assert len(reopened.pieces) == 1
    assert reopened.pieces[0].text == SMS_TEXT


def test_the_same_copy_is_not_stored_twice(store):
    first = store.add(parse_upload(SMS_TEXT, filename="a.txt", label="sms"))
    # Same text, different file, different whitespace.
    second = store.add(parse_upload(f"  {SMS_TEXT}  ", filename="b.txt", label="sms"))

    assert len(first) == 1
    assert second == []
    assert len(store.pieces) == 1


def test_id_ignores_whitespace_and_case():
    assert make_id("Hello  World") == make_id("hello world")


def test_counts_by_label(store):
    store.add(parse_upload(SMS_TEXT, filename="a.txt", label="sms"))
    store.add(parse_upload(EMAIL_TEXT, filename="b.txt", label="email"))

    assert store.counts() == {"email": 1, "sms": 1}


def test_a_corrupt_line_does_not_hide_the_rest(store):
    store.add(parse_upload(SMS_TEXT, filename="a.txt", label="sms"))
    with store.path.open("a", encoding="utf-8") as fh:
        fh.write("{not json\n")

    assert len(Corpus(store.dir).pieces) == 1


# ------------------------------------------------------------------- retrieval


def test_search_prefers_the_requested_format(store):
    store.add([build_piece(SMS_TEXT, label="sms", source="a")])
    store.add([build_piece(EMAIL_TEXT, label="email", source="b")])

    results = store.search("aged leads pricing", label="sms")

    assert all(p.label == "sms" for p in results)


def test_search_ranks_on_overlap_with_the_brief(store):
    store.add([
        build_piece("live transfers are calling your phone in 30 seconds flat today", label="sms", source="a"),
        build_piece("aged leads at $2.50 each, buy 100 before friday and work them", label="sms", source="b"),
    ])

    top = store.search("aged leads friday pricing", label="sms")[0]

    assert "aged leads" in top.text


def test_search_falls_back_to_other_formats_when_none_match(store):
    """Better to carry the voice from a blog post than to write from nothing."""
    store.add([build_piece(EMAIL_TEXT, label="email", source="b")])

    results = store.search("anything", label="sms")

    assert len(results) == 1
    assert results[0].label == "email"


def test_search_on_an_empty_library_returns_nothing(store):
    assert store.search("anything", label="sms") == []


def test_search_respects_the_character_budget(store):
    store.add([build_piece("x " * 2000 + f"aged leads {i}", label="sms", source=str(i))
               for i in range(10)])

    results = store.search("aged leads", label="sms", char_budget=5000)

    assert 0 < len(results) < 10


# ----------------------------------------------------------------- format guess


@pytest.mark.parametrize(
    "word,expected",
    [("sms", "sms"), ("texts", "sms"), ("fb", "ad"), ("vsl", "script"), ("emails", "email")],
)
def test_format_aliases_resolve(word, expected):
    assert find(word).key == expected


def test_blog_is_a_corpus_only_label():
    assert find("blog") is None
    assert find_label("blog") == "blog"


def test_guess_label_uses_the_filename_first():
    assert guess_label("anything at all here", filename="sms-blasts.csv") == "sms"


def test_guess_label_spots_an_email_by_its_subject_line():
    assert guess_label(EMAIL_TEXT) == "email"


def test_guess_label_calls_a_short_single_line_an_sms():
    assert guess_label(SMS_TEXT) == "sms"


def test_guess_label_gives_up_gracefully():
    assert guess_label("a\n\nb\n\nc " * 5) == "unsorted"
