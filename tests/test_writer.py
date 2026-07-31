"""Format schemas, prompt assembly, and output validation."""

import pytest

from wilbyte.corpus import build_piece
from wilbyte.formats import AD, BY_KEY, EMAIL, FORMATS, SMS
from wilbyte.writer import (
    WriterError,
    build_system_prompt,
    build_user_message,
    parse_result,
    render_text,
)


def payload(**overrides):
    base = {
        "variants": [
            {"body": "aged leads are $2.50 today. want 100 before friday? reply YES"},
            {"body": "your leads went cold. ours are $2.50. reply YES for 100"},
        ],
        "notes": "One price-led, one problem-led.",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------- schema


def test_every_format_produces_a_valid_schema():
    for fmt in FORMATS:
        schema = fmt.output_schema()
        item = schema["properties"]["variants"]["items"]

        assert schema["required"] == ["variants"]
        assert set(item["required"]) == {f.key for f in fmt.fields}


def test_character_limits_reach_the_schema():
    described = SMS.output_schema()["properties"]["variants"]["items"]["properties"]["body"]

    assert "160 characters" in described["description"]


def test_email_has_the_three_fields_a_send_needs():
    assert [f.key for f in EMAIL.fields] == ["subject", "preview_text", "body"]


# ---------------------------------------------------------------------- prompt


def test_system_prompt_carries_the_house_voice_and_the_format_rules():
    prompt = build_system_prompt(SMS)

    assert "Agent Lead Lab" in prompt
    assert "No income claims" in prompt  # the compliance rule survives
    assert "160 characters" in prompt
    assert "SMS" in prompt


def test_user_message_includes_the_examples_and_the_brief():
    examples = [build_piece("aged leads at $2.50 each before friday", label="sms", source="a")]

    message = build_user_message("price drop on aged leads", examples, SMS)

    assert "EXAMPLE 1" in message
    assert "aged leads at $2.50" in message
    assert "price drop on aged leads" in message


def test_user_message_says_so_when_there_is_nothing_to_learn_from():
    message = build_user_message("price drop", [], SMS)

    assert "No past copy" in message
    assert "EXAMPLE" not in message


def test_long_examples_are_trimmed_before_they_reach_the_model():
    piece = build_piece("word " * 5000, label="sms", source="a")

    assert len(piece.for_prompt()) < 3000
    assert "trimmed" in piece.for_prompt()


# ----------------------------------------------------------------- parsing out


def test_variants_are_parsed():
    result = parse_result(payload(), SMS, "price drop", [])

    assert len(result.variants) == 2
    assert result.variants[0].get("body").startswith("aged leads")
    assert result.notes == "One price-led, one problem-led."


def test_an_overlong_field_is_flagged_not_silently_cut():
    """A truncated SMS is a different message - the operator should decide."""
    long_body = "x" * 200
    result = parse_result(payload(variants=[{"body": long_body}]), SMS, "b", [])

    assert result.variants[0].get("body") == long_body  # untouched
    assert result.warnings
    assert "200 chars" in result.warnings[0]
    assert "limit 160" in result.warnings[0]


def test_no_variants_is_an_error():
    with pytest.raises(WriterError, match="no variants"):
        parse_result({"variants": []}, SMS, "b", [])


def test_empty_variants_are_an_error():
    with pytest.raises(WriterError, match="no content"):
        parse_result({"variants": [{"body": "  "}]}, SMS, "b", [])


def test_missing_fields_become_empty_strings_not_crashes():
    result = parse_result(
        {"variants": [{"headline": "Only the headline"}]}, AD, "b", []
    )

    assert result.variants[0].get("headline") == "Only the headline"
    assert result.variants[0].get("primary_text") == ""


def test_result_knows_whether_it_was_grounded():
    grounded = parse_result(payload(), SMS, "b", [build_piece("x" * 50, label="sms", source="a")])
    ungrounded = parse_result(payload(), SMS, "b", [])

    assert grounded.grounded
    assert not ungrounded.grounded


# --------------------------------------------------------------------- render


def test_rendered_text_is_readable_and_complete():
    result = parse_result(payload(), SMS, "price drop on aged leads", [])

    text = render_text(result)

    assert "Variant 1" in text
    assert "Variant 2" in text
    assert "price drop on aged leads" in text
    assert "aged leads are $2.50" in text


def test_rendered_text_labels_multi_field_formats():
    result = parse_result(
        {"variants": [{"subject": "Leads went cold", "preview_text": "here's why",
                       "body": "line one\nline two"}]},
        EMAIL, "b", [],
    )

    text = render_text(result)

    assert "Subject: Leads went cold" in text
    assert "Body:" in text
    assert "line two" in text
