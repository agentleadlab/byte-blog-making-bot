"""Appending finished segments to a Google Doc."""

import httpx
import pytest

from wilbyte import gdocs
from wilbyte.gdocs import DocsError, as_document, doc_id, end_of, explain
from wilbyte.segments import parse_segments

DOC = "1A0kadrdI1iOzrGcjWt3G54s7a77_3JfzzP1zgmyy1Dw"


def _segments():
    keep, _ = parse_segments({"segments": [{
        "kind": "segment",
        "start": "00:00:36",
        "end": "00:08:16",
        "yt_title": "Licensed in a Week",
        "website_section": "Agent Success Full Interviews",
        "hook": "Maddy met two agents at her apartment pool.",
        "bullets": ["Licensed a week later", "First call ever closed", "Week one $10,000"],
        "hashtags": ["newagent"],
        "website_description": "How a pool conversation became a licence.",
    }]})
    return keep


def test_the_id_comes_out_of_a_pasted_edit_link():
    assert doc_id(f"https://docs.google.com/document/d/{DOC}/edit?usp=sharing") == DOC


def test_a_bare_id_is_accepted_as_itself():
    assert doc_id(DOC) == DOC


def test_something_that_is_not_a_doc_link_is_refused():
    with pytest.raises(DocsError, match="doesn't look like a Google Doc"):
        doc_id("https://agentleadlab.com/")


def test_the_insert_point_is_before_the_document_s_final_newline():
    """Docs refuses a write at endIndex itself, so one before it is the end."""
    assert end_of({"body": {"content": [{"endIndex": 1}, {"endIndex": 421}]}}) == 420


def test_an_empty_document_still_has_somewhere_to_write():
    assert end_of({}) == 1
    assert end_of({"body": {"content": []}}) == 1


def test_the_appended_text_carries_the_heading_and_every_segment():
    text = as_document(
        _segments(), heading="Maddy Grundig", link="https://youtu.be/abc", summary="A summary."
    )
    assert text.startswith("Maddy Grundig\n\nhttps://youtu.be/abc\n\nA summary.")
    assert "SEGMENT (00:00:36–00:08:16) — 7:40" in text
    assert "(Website Description)" in text


def test_it_ends_with_a_gap_so_the_next_run_starts_clean():
    assert as_document(_segments()).endswith("\n\n")


def test_a_run_with_no_heading_has_no_blank_line_where_one_would_be():
    assert not as_document(_segments()).startswith("\n")


def test_a_missing_docs_scope_is_named_as_such_not_as_a_sharing_problem():
    """Otherwise somebody shares the doc again and it stays broken."""
    response = httpx.Response(
        403,
        text='{"error": {"message": "Request had insufficient authentication scopes."}}',
        request=httpx.Request("GET", "https://docs.googleapis.com/"),
    )
    said = explain(response, DOC)
    assert "doesn't cover Docs" in said
    assert gdocs.SCOPE in said


def test_a_plain_403_is_read_as_the_account_lacking_access():
    response = httpx.Response(
        403, text="The caller does not have permission",
        request=httpx.Request("GET", "https://docs.googleapis.com/"),
    )
    assert "edit access" in explain(response, DOC)


def test_a_404_names_the_id_that_was_looked_for():
    response = httpx.Response(
        404, text="not found", request=httpx.Request("GET", "https://docs.googleapis.com/")
    )
    assert DOC in explain(response, DOC)


def test_nothing_is_written_when_no_doc_is_set(monkeypatch):
    """Writing into a shared document waits to be asked for."""
    monkeypatch.delenv(gdocs.DOC_ID_VAR, raising=False)
    with pytest.raises(DocsError, match="No Google Doc set"):
        gdocs.append(_segments())
