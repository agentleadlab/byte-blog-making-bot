"""One interview's copy into one Google Doc, as a tab of its own."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from wilbyte import docs


@pytest.mark.parametrize(
    "pasted",
    [
        "https://docs.google.com/document/d/1AbCdEfGhIjKlMnOpQrS/edit?tab=t.7",
        "https://docs.google.com/document/d/1AbCdEfGhIjKlMnOpQrS/edit#heading=x",
        "1AbCdEfGhIjKlMnOpQrS",
    ],
)
def test_the_document_id_comes_out_of_whatever_was_pasted(pasted):
    assert docs.doc_id_in(pasted) == "1AbCdEfGhIjKlMnOpQrS"


@pytest.mark.parametrize("nonsense", ["", "   ", "not a link", "https://example.com"])
def test_something_that_is_not_a_document_is_not_one(nonsense):
    assert docs.doc_id_in(nonsense) == ""


def test_no_document_set_says_which_setting():
    with pytest.raises(docs.DocsError) as raised:
        docs.DocsClient(docs.Credentials("i", "s", "r"), document="")

    assert "SEGMENTS_DOC_ID" in str(raised.value)


def test_the_tabs_of_a_document_are_flattened():
    """A tab can hold tabs. A nested one is still somewhere copy could go."""
    found = docs._tabs_in({
        "tabProperties": {"tabId": "t.0", "title": "Leonardo Lopez"},
        "childTabs": [
            {"tabProperties": {"tabId": "t.1", "title": "Take two"}},
        ],
    })

    assert [(one.tab_id, one.title) for one in found] == [
        ("t.0", "Leonardo Lopez"), ("t.1", "Take two"),
    ]


def test_a_tab_with_no_id_is_not_somewhere_to_write():
    assert docs._tabs_in({"tabProperties": {"title": "broken"}}) == []


# ------------------------------------------------ writing it, with Google stubbed


class Paper:
    """A stubbed Docs client. Remembers what it was told to do."""

    def __init__(self, tabs=(), fails=None):
        self._tabs = list(tabs)
        self.made, self.written = [], []
        self.fails = fails

    def tabs(self):
        if self.fails == "read":
            raise docs.DocsError("Google Docs refused that")
        return list(self._tabs)

    def add_tab(self, title):
        self.made.append(title)
        return docs.Tab(tab_id="t.new", title=title)

    def write(self, tab, text):
        self.written.append((tab.tab_id, text))

    def link_to(self, tab):
        return f"https://docs.google.com/document/d/D/edit?tab={tab.tab_id}"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _writing(monkeypatch, paper, *, doc_id="D"):
    from wilbyte.bot import jobs

    monkeypatch.setattr(docs, "open_docs", lambda secrets: paper)
    config = SimpleNamespace(secrets=SimpleNamespace(segments_doc_id=doc_id))
    return jobs.copy_into_doc(config, title="Emmanuel Nazco", text="the whole copy")


def test_a_new_agent_gets_a_tab_of_their_own(monkeypatch):
    paper = Paper()
    link, problems = _writing(monkeypatch, paper)

    assert paper.made == ["Emmanuel Nazco"]
    assert paper.written == [("t.new", "the whole copy\n")]
    assert link.endswith("tab=t.new")
    assert problems == []


def test_a_tab_that_already_exists_is_not_made_twice(monkeypatch):
    """Two runs over the same interview should not leave two tabs for
    somebody to pick between."""
    paper = Paper(tabs=[docs.Tab(tab_id="t.3", title="Emmanuel Nazco")])
    link, problems = _writing(monkeypatch, paper)

    assert paper.made == []
    assert paper.written[0][0] == "t.3"
    assert link.endswith("tab=t.3")
    assert any("already a tab" in one for one in problems)


def test_the_name_is_matched_however_it_was_capitalised(monkeypatch):
    paper = Paper(tabs=[docs.Tab(tab_id="t.3", title="  emmanuel nazco ")])
    _writing(monkeypatch, paper)

    assert paper.made == []


def test_somebody_else_s_tab_is_left_alone(monkeypatch):
    paper = Paper(tabs=[docs.Tab(tab_id="t.1", title="Leonardo Lopez")])
    _writing(monkeypatch, paper)

    assert paper.made == ["Emmanuel Nazco"]
    assert paper.written[0][0] == "t.new"


def test_no_doc_configured_is_silent_rather_than_a_problem(monkeypatch):
    """The segment command worked before there was a doc to write into."""
    paper = Paper()
    link, problems = _writing(monkeypatch, paper, doc_id="")

    assert (link, problems) == ("", [])
    assert paper.made == []


def test_a_refusal_is_reported_rather_than_raised(monkeypatch):
    link, problems = _writing(monkeypatch, Paper(fails="read"))

    assert link == ""
    assert "Couldn't write it into the doc" in problems[0]


def test_the_scope_is_named_when_google_refuses():
    """A 403 from Docs says nothing about which scope is missing, and the
    answer is always the same one."""
    import httpx

    client = docs.DocsClient(docs.Credentials("i", "s", "r"), document="D")
    client._token, client._token_until = "tok", 9e18

    def refuse(*args, **kwargs):
        return httpx.Response(403, text="forbidden", request=httpx.Request("GET", "https://x"))

    client._client.request = refuse
    with pytest.raises(docs.DocsError) as raised:
        client.tabs()

    assert "auth/documents" in str(raised.value)
    client.close()


# --------------------------------- checking it without filing an interview

# A scope that was not ticked and a doc that was never shared look identical
# until an interview is filed, which is the worst moment to find out.


def _checking(monkeypatch, *, doc_id="D", paper=None, blows_up=None):
    from wilbyte.bot import jobs

    def opening(secrets):
        if blows_up is not None:
            raise blows_up
        return paper or Paper()

    monkeypatch.setattr(docs, "open_docs", opening)
    return jobs._check_docs(
        SimpleNamespace(secrets=SimpleNamespace(segments_doc_id=doc_id))
    )


def test_a_reachable_doc_says_how_many_tabs_it_has(monkeypatch):
    paper = Paper(tabs=[
        docs.Tab(tab_id="t.1", title="Leonardo Lopez"),
        docs.Tab(tab_id="t.2", title="Emmanuel Nazco"),
    ])
    (ok, said), = _checking(monkeypatch, paper=paper)

    assert ok is True
    assert "2 tab(s)" in said
    assert "Emmanuel Nazco" in said


def test_a_missing_scope_is_named_in_the_check(monkeypatch):
    (ok, said), = _checking(
        monkeypatch,
        blows_up=docs.DocsError(
            "Google Docs refused that. The refresh token in .env was minted "
            "without https://www.googleapis.com/auth/documents"
        ),
    )

    assert ok is False
    assert "auth/documents" in said


def test_a_doc_nobody_shared_is_told_apart_from_a_missing_scope(monkeypatch):
    (ok, said), = _checking(
        monkeypatch,
        blows_up=docs.DocsError(
            "Google has no document with that id, or the account the token "
            "belongs to cannot see it."
        ),
    )

    assert ok is False
    assert "cannot see it" in said


def test_not_configured_is_neither_pass_nor_fail(monkeypatch):
    (ok, said), = _checking(monkeypatch, doc_id="")

    assert ok is None
    assert "not configured" in said


def test_the_check_never_writes(monkeypatch):
    paper = Paper()
    _checking(monkeypatch, paper=paper)

    assert (paper.made, paper.written) == ([], [])


def test_the_tab_list_is_actually_asked_for():
    """`@RYTE check` said "0 tab(s)" of a document with twenty-five. Left
    false, Google returns the first tab's text and an empty tabs list — so
    the guard against making somebody a second tab had nothing to compare
    against and would have made one every run."""
    import httpx

    asked = {}

    client = docs.DocsClient(docs.Credentials("i", "s", "r"), document="D")
    client._token, client._token_until = "tok", 9e18

    def watching(method, url, **kwargs):
        asked.update(kwargs.get("params") or {})
        return httpx.Response(
            200,
            json={"tabs": [{"tabProperties": {"tabId": "t.1", "title": "Karyn Giles"}}]},
            request=httpx.Request(method, url),
        )

    client._client.request = watching
    found = client.tabs()

    assert asked["includeTabsContent"] == "true"
    assert [one.title for one in found] == ["Karyn Giles"]
    client.close()


def test_the_whole_document_is_not_downloaded_to_read_its_tab_names():
    """Twenty-five interviews of text to find out what the tabs are called."""
    import httpx

    asked = {}
    client = docs.DocsClient(docs.Credentials("i", "s", "r"), document="D")
    client._token, client._token_until = "tok", 9e18
    client._client.request = lambda method, url, **kw: (
        asked.update(kw.get("params") or {}),
        httpx.Response(200, json={"tabs": []}, request=httpx.Request(method, url)),
    )[1]

    client.tabs()

    assert "tabProperties" in asked["fields"]
    assert "body" not in asked["fields"]
    client.close()
