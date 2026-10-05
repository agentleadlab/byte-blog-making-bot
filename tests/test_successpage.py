"""The Success Stories page's videos, kept up to date by RYTE.

"i want to tell ryte to post it" - and the full interview goes up too.
"""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from wilbyte import successpage as sp

ROWS = [
    {"c": "Agent Success Full Interviews", "more": "https://agentleadlab.com/training/agent-success-full-interviews",
     "v": [{"id": "AAAAAAAAAAA", "t": "Old full one", "w": "James Geway"}]},
    {"c": "Aged Leads", "more": "https://agentleadlab.com/training/aged-leads",
     "v": [{"id": "BBBBBBBBBBB", "t": "Old aged one", "w": "Marc Peters"}]},
    {"c": "Veteran Training", "more": "https://agentleadlab.com/training/veteran-protection", "v": []},
    {"c": "Agent's Expectations", "more": "https://agentleadlab.com/training/agents-expectations", "v": []},
]


def _page(rows=ROWS) -> str:
    listed = json.dumps(rows, indent=1)
    escaped = listed.replace('"', '\\"').replace("/", "\\u002F")
    return (f"<script>\n  var LL_VIDEOS = {listed};\n</script>"
            f'<script>window.__NUXT__="  var LL_VIDEOS = {escaped};\\n"</script>')


def test_the_live_page_is_read():
    data = sp.parse_live(_page())
    assert [row["c"] for row in data["sections"]][:2] == ["Agent Success Full Interviews", "Aged Leads"]
    assert data["sections"][1]["v"][0] == {"id": "BBBBBBBBBBB", "t": "Old aged one", "w": "Marc Peters"}


def test_a_page_without_the_list_says_so():
    with pytest.raises(sp.SuccessError):
        sp.parse_live("<html>nothing</html>")


def _item(section, vid, title):
    return {"section": section, "id": vid, "title": title, "who": "Karyn Giles", "what": "x"}


def test_new_videos_go_first_in_their_section_in_the_order_given():
    data = sp.parse_live(_page())
    updated, went, already = sp.add(data, [
        _item("Aged Leads", "CCCCCCCCCCC", "First new"),
        _item("Aged Leads", "DDDDDDDDDDD", "Second new"),
    ])
    aged = next(row for row in updated["sections"] if row["c"] == "Aged Leads")
    assert [v["t"] for v in aged["v"]] == ["First new", "Second new", "Old aged one"]
    assert len(went) == 2 and already == []
    # What it was given is left as it was.
    assert len(next(r for r in data["sections"] if r["c"] == "Aged Leads")["v"]) == 1


def test_one_already_up_is_not_added_twice():
    data = sp.parse_live(_page())
    _updated, went, already = sp.add(data, [_item("Aged Leads", "BBBBBBBBBBB", "Again")])
    assert went == [] and already[0]["id"] == "BBBBBBBBBBB"


def test_an_unknown_section_is_refused():
    with pytest.raises(sp.SuccessError):
        sp.add(sp.parse_live(_page()), [_item("Not A Section", "CCCCCCCCCCC", "x")])


def test_taking_one_down():
    updated, gone = sp.remove(sp.parse_live(_page()), "BBBBBBBBBBB")
    assert gone[0]["section"] == "Aged Leads"
    assert next(r for r in updated["sections"] if r["c"] == "Aged Leads")["v"] == []


LAID_OUT = """Karyn Giles — interview segments
TRELLO CARD
https://trello.com/c/huTGzi4b/16225-karyn-giles-interview
Full interview + 3 segments
At a glance
Full interview   00:03:43–00:55:02 · 51:19 · Agent Success Full Interviews
13 Years, $181K
Full interview  ·  00:03:43–00:55:02  ·  51:19
YOUTUBE TITLE
13 Years, $181K in Six Months Off Aged Leads
WEBSITE SECTION
Agent Success Full Interviews
YOUTUBE LINK
https://youtu.be/-aIcdjzwFsY
YOUTUBE DESCRIPTION
Karyn overdrafted for gas.
WEBSITE DESCRIPTION
Karyn has sold insurance for 13 years.
Segment 1  ·  00:03:43–00:10:39  ·  6:56
YOUTUBE TITLE
Five Bartending Jobs to $181,000  https://youtu.be/OBFITZL2uSc
WEBSITE SECTION
Agent Success Full Interviews
YOUTUBE DESCRIPTION
Karyn was bartending.
WEBSITE DESCRIPTION
How she got in.
Segment 2  ·  00:10:39–00:19:21  ·  8:42
YOUTUBE TITLE
How to Make an Aged Lead Feel New
WEBSITE SECTION
Aged Leads
YOUTUBE LINK
https://youtu.be/4JSaDVPFbTc
YOUTUBE DESCRIPTION
The opener.
WEBSITE DESCRIPTION
The verbatim opener.
Segment 3  ·  00:19:21–00:28:53  ·  9:32
YOUTUBE TITLE
The Full Veteran Telesales Script
WEBSITE SECTION
Veteran Training
YOUTUBE DESCRIPTION
The script.
WEBSITE DESCRIPTION
The whole call.
"""


def test_a_laid_out_tab_gives_its_segments_with_links():
    items, skipped, waiting = sp.from_tab("Karyn Giles", LAID_OUT)

    assert [one["what"] for one in waiting] == ["Segment 3"]
    assert [(one["what"], one["section"], one["id"]) for one in items] == [
        ("Full interview", "Agent Success Full Interviews", "-aIcdjzwFsY"),
        ("Segment 2", "Aged Leads", "4JSaDVPFbTc"),
    ]
    assert items[0]["title"] == "13 Years, $181K in Six Months Off Aged Leads"
    assert items[0]["who"] == "Karyn Giles"
    # Segment 1 sits under the full interviews' section - not posted there.
    assert any("Segment 1" in one and "full interviews only" in one for one in skipped)


def test_an_old_tab_is_read_too():
    old = (
        "LONG-FORM / FULL INTERVIEW (00:00:00–00:40:00) — 40:00\n"
        "Ashley Full Story (YT Title)\n"
        "Agent Success Full Interviews (Website section) https://youtu.be/EEEEEEEEEEE\n"
        "(YT Description)\nHe explains.\n#agentleadlab\n"
        "(Website Description)\nAshley shares.\n"
        "SEGMENT (00:05:17–00:10:27)\n"
        "His First 30 Days (YT Title)\n"
        "Agent's Expectations (Website section) https://youtu.be/eZvDMI3iuGo\n"
        "(YT Description)\nMost agents.\n#agentleadlab\n"
        "(Website Description)\nIn this clip.\n"
    )
    items, skipped, _waiting = sp.from_tab("Ashley Aronson", old)
    assert [(one["section"], one["id"]) for one in items] == [
        ("Agent Success Full Interviews", "EEEEEEEEEEE"),
        ("Agent's Expectations", "eZvDMI3iuGo"),
    ]
    assert skipped == []


def test_posting_starts_from_the_live_page_and_saves_beside_the_testimonials(monkeypatch):
    from wilbyte.bot import jobs

    saved = []

    class Site:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def load(self, *, slug, mark):
            assert (slug, mark) == ("ll-training-data", "LLTRAINING-")
            return None

        def live_page(self, url=""):
            assert url == sp.PAGE_URL
            return _page()

        def save(self, data, **kw):
            saved.append((data, kw))

    monkeypatch.setattr(jobs, "_wordpress", lambda config: Site())
    said = jobs.success_post(SimpleNamespace(), [_item("Aged Leads", "CCCCCCCCCCC", "New one")])

    data, kw = saved[0]
    assert kw["slug"] == "ll-training-data" and kw["mark"] == "LLTRAINING-"
    assert next(r for r in data["sections"] if r["c"] == "Aged Leads")["v"][0]["id"] == "CCCCCCCCCCC"
    assert "New one" in said and "started from the videos already on the page" in said


def test_nothing_new_writes_nothing(monkeypatch):
    from wilbyte.bot import jobs

    saved = []

    class Site:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def load(self, **kw):
            return sp.parse_live(_page())

        def save(self, data, **kw):
            saved.append(data)

    monkeypatch.setattr(jobs, "_wordpress", lambda config: Site())
    said = jobs.success_post(SimpleNamespace(), [_item("Aged Leads", "BBBBBBBBBBB", "Old aged one")])
    assert saved == [] and "Not added: Old aged one — already on the page under Aged Leads" in said


def test_asking_for_it():
    from wilbyte.bot import mentions

    assert (mentions.parse("post segments Karyn Giles", max_batch=5).action,
            mentions.parse("post segments Karyn Giles", max_batch=5).brief) == ("postsegments", "Karyn Giles")
    got = mentions.parse("success remove https://youtu.be/4JSaDVPFbTc", max_batch=5)
    assert (got.action, got.brief) == ("successremove", "https://youtu.be/4JSaDVPFbTc")


# ------------------------------------------------ the snippet, in a browser

LOADER = Path(__file__).resolve().parents[1] / "website" / "success-stories-loader.html"

PAGE_SCRIPT = """
<div id="alp-stack"></div>
<script>
  var LL_VIDEOS = %s;
  var stack = document.getElementById('alp-stack');
  /* GHL runs the page's own code late - after the snippet has its list. */
  setTimeout(function(){ stack.innerHTML = LL_VIDEOS.map(function(c){
    return '<section class="alp-cat"><h2 class="alp-catname">' + c.c + '</h2><div class="alp-vgrid">' +
      c.v.map(function(v){ return '<article class="alp-vcard"><button class="alp-frame" data-vid="' + v.id +
        '"></button><h3 class="alp-vtitle">' + v.t + '</h3></article>'; }).join('') + '</div></section>';
  }).join(''); }, 300);
  stack.addEventListener('click', function(e){
    var f = e.target.closest('.alp-frame'); if (!f) return;
    var d = document.createElement('div'); d.className = 'alp-frame';
    d.innerHTML = '<iframe data-playing="' + f.getAttribute('data-vid') + '"></iframe>';
    f.parentNode.replaceChild(d, f);
  });
</script>
"""


def _browser():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("no playwright")
    return sync_playwright


def _show(data, *, status=200):
    sync_playwright = _browser()
    page_html = PAGE_SCRIPT % json.dumps(ROWS) + LOADER.read_text(encoding="utf-8")
    body = json.dumps([{"content": {"rendered": "<p>LLTRAINING-" + json.dumps(data).encode().hex() + "</p>"}}])
    with sync_playwright() as p:
        kw = {"executable_path": "/opt/pw-browsers/chromium"} if os.path.exists("/opt/pw-browsers/chromium") else {}
        try:
            browser = p.chromium.launch(**kw)
        except Exception:
            pytest.skip("no browser here")
        page = browser.new_page()
        page.route("https://agentleadlab.test/", lambda r: r.fulfill(body=page_html, content_type="text/html"))
        page.route("https://leadlabcrm.com/**", lambda r: r.fulfill(
            status=status, body=body if status == 200 else "", content_type="application/json",
            headers={"access-control-allow-origin": "*"},
        ))
        page.goto("https://agentleadlab.test/")
        page.wait_for_timeout(900)
        titles = page.eval_on_selector_all(".alp-vtitle", "els => els.map(e => e.textContent)")
        page.click(".alp-frame")
        playing = page.eval_on_selector("iframe", "e => e.getAttribute('data-playing')") if page.query_selector("iframe") else None
        browser.close()
    return titles, playing


def test_the_page_shows_ryte_s_list_three_to_a_section_and_still_plays():
    data, _went, _already = sp.add(sp.parse_live(_page()), [
        _item("Agent Success Full Interviews", f"N{n}NNNNNNNNN", f"New {n}") for n in range(4)
    ])
    titles, playing = _show(data)

    assert titles[:3] == ["New 0", "New 1", "New 2"]
    assert "New 3" not in titles and "Old full one" not in titles
    assert playing == "N0NNNNNNNNN"


def test_if_the_list_cant_be_read_the_page_is_left_as_it_was():
    titles, _playing = _show({}, status=500)
    assert titles == ["Old full one", "Old aged one"]



# ------------------------------------------------ found on the channel by title

UPLOADS = [
    {"id": "VET0000000A", "title": "The Full Veteran Telesales Script!"},
    {"id": "TWIN000000A", "title": "Spending the Money"},
    {"id": "TWIN000000B", "title": "Spending the money"},
    {"id": "SHORT00000A", "title": "The Full Veteran Telesales"},
]


def _waiting(title, what="Segment 3"):
    return {"section": "Veteran Training", "id": "", "title": title, "who": "Karyn Giles", "what": what}


def test_a_segment_is_found_by_its_exact_title():
    found, missing = sp.match_uploads([_waiting("The Full Veteran Telesales Script")], UPLOADS)
    assert [(one["id"], one["found"]) for one in found] == [("VET0000000A", True)] and missing == []


def test_a_shortened_title_is_not_a_match():
    """"i dont want him uploading incorrect videos"."""
    found, missing = sp.match_uploads([_waiting("The Full Veteran Telesales Script Part 2")], UPLOADS)
    assert found == [] and "no video on the channel with that exact title" in missing[0]


def test_two_videos_with_the_title_is_a_question_not_a_guess():
    found, missing = sp.match_uploads([_waiting("Spending the Money", "Segment 4")], UPLOADS)
    assert found == [] and "2 videos on the channel have that title" in missing[0]


def test_titles_match_past_capitals_and_punctuation_only():
    assert sp.same_title("Stop Trying to Marry the Client", "stop trying to marry the client!")
    assert not sp.same_title("Stop Trying to Marry the Client", "Stop Trying to Marry the Client 2")
    assert not sp.same_title("$5,600 Order", "5600 Order")


def test_the_plan_looks_on_the_interviews_own_channel(monkeypatch):
    from wilbyte import docs, youtube_api
    from wilbyte.bot import jobs

    asked = {}

    class Doc:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def contents(self):
            return [(docs.Tab("t.1", "Karyn Giles"), LAID_OUT, "")]

    monkeypatch.setattr(docs, "open_docs", lambda secrets: Doc())
    monkeypatch.setattr(youtube_api, "get_video", lambda vid: {"snippet": {"channelId": "UCabcdefghij"}})

    def uploads(playlist, *, limit=None):
        asked["playlist"] = playlist
        return [{"contentDetails": {"videoId": "VET0000000A"},
                 "snippet": {"title": "The Full Veteran Telesales Script"}}]

    monkeypatch.setattr(youtube_api, "list_playlist_items", uploads)
    config = SimpleNamespace(secrets=SimpleNamespace(segments_doc_id="D"))
    _tab, items, skipped = jobs.success_plan(config, "Karyn Giles")

    assert asked["playlist"] == "UUabcdefghij"
    assert [one["what"] for one in items] == ["Full interview", "Segment 2", "Segment 3"]
    assert items[2]["id"] == "VET0000000A" and items[2]["found"] is True


def test_both_channels_are_read_from_links_or_handles():
    """"yt has two accounts"."""
    assert sp.channels_in(
        "https://www.youtube.com/@agentleadlab, https://youtube.com/channel/UCabcdefghijklmnopqrstuv"
    ) == ["@agentleadlab", "UCabcdefghijklmnopqrstuv"]
    assert sp.channels_in("@one @two @one") == ["@one", "@two"]


def test_the_main_channel_decides_first():
    main = [{"id": "MAIN0000000", "title": "The Full Veteran Telesales Script"}]
    second = [{"id": "SECOND00000", "title": "The Full Veteran Telesales Script"},
              {"id": "ONLYTWO0000", "title": "Stop Trying to Marry the Client"}]
    found, missing = sp.match_channels(
        [_waiting("The Full Veteran Telesales Script"), _waiting("Stop Trying to Marry the Client", "Segment 7"),
         _waiting("Nowhere At All", "Segment 8")],
        [main, second],
    )
    assert [one["id"] for one in found] == ["MAIN0000000", "ONLYTWO0000"]
    assert "no video on either channel" in missing[0]


def test_two_with_the_title_on_the_main_channel_is_still_a_question():
    main = [{"id": "TWIN000000A", "title": "Spending the Money"}, {"id": "TWIN000000B", "title": "Spending the money"}]
    found, missing = sp.match_channels([_waiting("Spending the Money", "Segment 4")],
                                       [main, [{"id": "OTHER000000", "title": "Spending the Money"}]])
    assert found == [] and "2 videos on the channel" in missing[0]


def test_with_no_link_in_the_tab_the_set_channels_are_searched(monkeypatch):
    from wilbyte import docs, youtube_api
    from wilbyte.bot import jobs

    unlinked = LAID_OUT.replace("YOUTUBE LINK\nhttps://youtu.be/-aIcdjzwFsY\n", "").replace(
        "YOUTUBE LINK\nhttps://youtu.be/4JSaDVPFbTc\n", "").replace("  https://youtu.be/OBFITZL2uSc", "")

    class Doc:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def contents(self):
            return [(docs.Tab("t.1", "Evan Scott"), unlinked, "")]

    read = []
    monkeypatch.setattr(docs, "open_docs", lambda secrets: Doc())
    monkeypatch.setattr(youtube_api, "_get", lambda path, params, **kw: {"items": [{"id": "UC" + params["forHandle"][1:].ljust(22, "x")}]})

    def uploads(playlist, *, limit=None):
        read.append(playlist)
        return [{"contentDetails": {"videoId": "FULL0000000"},
                 "snippet": {"title": "13 Years, $181K in Six Months Off Aged Leads"}}] if playlist.startswith("UUsecond") else []

    monkeypatch.setattr(youtube_api, "list_playlist_items", uploads)
    config = SimpleNamespace(secrets=SimpleNamespace(segments_doc_id="D", youtube_channel_id="@main, @second"))
    _tab, items, _skipped = jobs.success_plan(config, "Evan Scott")

    assert [one[:6] for one in read] == ["UUmain", "UUseco"]
    assert [(one["what"], one["id"]) for one in items] == [("Full interview", "FULL0000000")]



# ------------------------------------------------ "not to post similar/duplicates"


def test_the_same_video_in_another_section_is_not_added():
    _data, went, already = sp.add(sp.parse_live(_page()), [_item("Veteran Training", "BBBBBBBBBBB", "New title")])
    assert went == [] and "already on the page under Aged Leads" in already[0]["why"]


def test_a_re_upload_with_the_same_title_is_not_added():
    _data, went, already = sp.add(sp.parse_live(_page()), [_item("Aged Leads", "ZZZZZZZZZZZ", "OLD AGED ONE!")])
    assert went == [] and "looks the same as “Old aged one”" in already[0]["why"]


def test_a_near_identical_title_is_not_added():
    _data, went, already = sp.add(sp.parse_live(_page()), [
        _item("Agent Success Full Interviews", "ZZZZZZZZZZZ", "Old full one (Full Interview)"),
    ])
    assert went == [] and already


def test_the_same_one_twice_in_one_post_goes_up_once():
    _data, went, already = sp.add(sp.parse_live(_page()), [
        {**_item("Aged Leads", "CCCCCCCCCCC", "Brand new"), "what": "Segment 2"},
        {**_item("Aged Leads", "CCCCCCCCCCC", "Brand new"), "what": "Segment 3"},
    ])
    assert [one["what"] for one in went] == ["Segment 2"]
    assert "the same as Segment 2 in this post" in already[0]["why"]


@pytest.mark.parametrize("one, other, same", [
    ("Stop Trying to Marry the Client", "stop trying to marry the client!", True),
    ("Karyn Giles: 13 Years", "Karyn Giles: 13 Years — Full Interview", True),
    ("How She Closes Veterans, Part 1", "How She Closes Veterans, Part 2", False),
    ("Why Aged Leads Work", "Why Fresh Leads Work", False),
    ("The Full Veteran Telesales Script", "The Full IUL Telesales Script", False),
    ("Why She Quizzes Veterans on Their Own Benefits Before She Ever Quotes",
     "Why She Quizzes Veterans On Their Own Benefits Before She Quotes", True),
])
def test_what_counts_as_the_same_title(one, other, same):
    assert sp.similar_titles(one, other) is same


def test_duplicates_are_said_before_the_button(monkeypatch):
    from wilbyte import docs, youtube_api
    from wilbyte.bot import jobs

    class Doc:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def contents(self):
            return [(docs.Tab("t.1", "Karyn Giles"), LAID_OUT, "")]

    class Site:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def load(self, **kw):
            data = sp.parse_live(_page())
            data["sections"][1]["v"].append({"id": "4JSaDVPFbTc", "t": "How to Make an Aged Lead Feel New", "w": "Karyn"})
            return data

    monkeypatch.setattr(docs, "open_docs", lambda secrets: Doc())
    monkeypatch.setattr(jobs, "_wordpress", lambda config: Site())
    monkeypatch.setattr(youtube_api, "get_video", lambda vid: {"snippet": {}})
    config = SimpleNamespace(secrets=SimpleNamespace(segments_doc_id="D"))
    _tab, items, skipped = jobs.success_plan(config, "Karyn Giles")

    assert [one["what"] for one in items] == ["Full interview"]
    assert any(one.startswith("Segment 2 — already on the page under Aged Leads") for one in skipped)



@pytest.mark.parametrize("said, section", [
    ("Agent’s Expectation", "Agent's Expectations"),
    ("agents expectations", "Agent's Expectations"),
    ("Veteran Trainings", "Veteran Training"),
    ("IUL training", "IUL Training"),
    ("Veteran", ""),
    ("", ""),
])
def test_a_section_is_known_past_its_spelling(said, section):
    """Karyn Giles's Segment 1: "“Agent’s Expectation” isn't one of the website sections"."""
    assert sp.section_named(said) == section
