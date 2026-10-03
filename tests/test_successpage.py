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
    items, skipped = sp.from_tab("Karyn Giles", LAID_OUT)

    assert [(one["what"], one["section"], one["id"]) for one in items] == [
        ("Full interview", "Agent Success Full Interviews", "-aIcdjzwFsY"),
        ("Segment 2", "Aged Leads", "4JSaDVPFbTc"),
    ]
    assert items[0]["title"] == "13 Years, $181K in Six Months Off Aged Leads"
    assert items[0]["who"] == "Karyn Giles"
    # Segment 1 sits under the full interviews' section - not posted there.
    assert any("Segment 1" in one and "full interviews only" in one for one in skipped)
    assert any("Segment 3" in one and "no YouTube link" in one for one in skipped)


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
    items, skipped = sp.from_tab("Ashley Aronson", old)
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
    assert saved == [] and "Already up" in said


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
