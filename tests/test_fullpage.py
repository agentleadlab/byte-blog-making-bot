"""The Agent Success Full Interviews page: "ryte just have to duplicate the
current section and swap out that yt link"."""

import json
import os
from pathlib import Path
from types import SimpleNamespace as NS

import httpx
import pytest

from wilbyte import fullpage, website


def test_a_new_one_goes_first_and_one_already_there_moves_up():
    data = fullpage.add(None, {"id": "AAAAAAAAAAA", "title": "A", "name": "Ann", "about": "about  a"})
    data = fullpage.add(data, {"id": "BBBBBBBBBBB", "title": "B", "name": "Bo", "about": "b"})
    assert [one["id"] for one in data["rows"]] == ["BBBBBBBBBBB", "AAAAAAAAAAA"]
    assert data["rows"][1]["about"] == "about a"
    again = fullpage.add(data, {"id": "AAAAAAAAAAA", "title": "A", "name": "Ann", "about": "a"})
    assert [one["id"] for one in again["rows"]] == ["AAAAAAAAAAA", "BBBBBBBBBBB"], "moved, not twice"


def test_taking_one_off_hides_it_even_when_built_by_hand_and_adding_brings_it_back():
    data = fullpage.add(None, {"id": "AAAAAAAAAAA", "title": "A", "name": "Ann", "about": "a"})
    data = fullpage.remove(data, "AAAAAAAAAAA")
    data = fullpage.remove(data, "HANDBUILT00")
    assert data["rows"] == [] and data["gone"] == ["AAAAAAAAAAA", "HANDBUILT00"]
    assert fullpage.remove(data, "HANDBUILT00")["gone"] == data["gone"], "said once"
    back = fullpage.add(data, {"id": "HANDBUILT00", "title": "H", "name": "Hal", "about": "h"})
    assert back["gone"] == ["AAAAAAAAAAA"] and back["rows"][0]["id"] == "HANDBUILT00"


@pytest.mark.parametrize("said, parts, about", [
    ("testimonial https://youtu.be/9Ul9Fv3Jxzs quote: Every lead closed about: Raymond walks through it.",
     ("https://youtu.be/9Ul9Fv3Jxzs", "", "Every lead closed"), "Raymond walks through it."),
    ("testimonial https://youtu.be/9Ul9Fv3Jxzs about: He explains. name: Raymond Ortiz quote: x",
     ("https://youtu.be/9Ul9Fv3Jxzs", "Raymond Ortiz", "x"), "He explains."),
    ("testimonial https://youtu.be/9Ul9Fv3Jxzs name: Cole quote: Connect the dots",
     ("https://youtu.be/9Ul9Fv3Jxzs", "Cole", "Connect the dots"), ""),
])
def test_the_paragraph_can_be_given_in_any_order(said, parts, about):
    from wilbyte.bot import mentions

    got = mentions.parse("<@1> " + said)
    assert got.action == "testimonial"
    assert mentions.testimonial_parts(got.brief) == parts
    assert mentions.testimonial_about(got.brief) == about


CONFIG = NS(secrets=NS(wordpress_url="https://leadlabcrm.com/", wordpress_user="franklin",
                       wordpress_app_password="abcd efgh ijkl mnop"))


def test_the_paragraph_is_written_from_the_captions_when_not_given(monkeypatch):
    from wilbyte import youtube
    from wilbyte.bot import jobs

    monkeypatch.setattr(youtube, "fetch_video", lambda vid: NS(title="Burning $10K a Month"))
    monkeypatch.setattr(jobs, "waiting_on_captions", lambda link: ("the transcript", ""))
    monkeypatch.setattr(jobs, "_pick_testimonial", lambda *a: pytest.fail("name and quote were given"))
    monkeypatch.setattr(jobs, "_write_about", lambda config, title, name, text: f"{name} explains  it.")
    got = jobs.testimonial_draft(CONFIG, "https://youtu.be/9Ul9Fv3Jxzs", name="Raymond", quote="x")
    assert got["about"] == "Raymond explains it."

    monkeypatch.setattr(jobs, "waiting_on_captions", lambda link: pytest.fail("nothing to read for"))
    got = jobs.testimonial_draft(CONFIG, "https://youtu.be/9Ul9Fv3Jxzs", name="Raymond", quote="x",
                                 about="Given  as is.")
    assert got["about"] == "Given as is."


def test_no_captions_means_no_paragraph_not_a_failure(monkeypatch):
    from wilbyte import youtube
    from wilbyte.bot import jobs

    monkeypatch.setattr(youtube, "fetch_video", lambda vid: NS(title="t"))
    monkeypatch.setattr(jobs, "waiting_on_captions", lambda link: ("", "no captions yet"))
    got = jobs.testimonial_draft(CONFIG, "https://youtu.be/9Ul9Fv3Jxzs", name="Raymond", quote="x")
    assert got["about"] == "" and got["name"] == "Raymond"


class Pages:
    """WordPress with a page per slug."""

    def __init__(self):
        self.pages, self.home = {}, ""

    def handler(self, request):
        path = request.url.path
        if path == "/wp-json/wp/v2/pages" and request.method == "GET":
            slug = request.url.params.get("slug")
            return httpx.Response(200, json=[self.pages[slug]] if slug in self.pages else [])
        if path.startswith("/wp-json/wp/v2/pages") and request.method == "POST":
            body = json.loads(request.content)
            self.pages[body["slug"]] = {"id": len(self.pages) + 1, "content": {"raw": body["content"]}}
            return httpx.Response(201, json=self.pages[body["slug"]])
        return httpx.Response(404)


@pytest.fixture
def wp(monkeypatch):
    made = Pages()
    made.pages[website.DATA_SLUG] = {"id": 1, "content": {"raw": website.page_content(
        {"featured": {"id": "U1O8FXthqzQ", "name": "Emanuel Nazco", "quote": '"b"'}, "videos": [], "page_size": 8})}}
    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(made.handler), **kw))
    from wilbyte.bot import jobs

    monkeypatch.setattr(jobs, "full_interviews_plan", lambda config: ({}, {}, [], []))
    return made


def _full(wp):
    return website.decoded(wp.pages[fullpage.DATA_SLUG]["content"]["raw"], mark=fullpage.MARK)


def test_add_puts_it_first_on_the_full_interviews_page_too(wp):
    from wilbyte.bot import jobs

    said = jobs.testimonial_publish(CONFIG, {"id": "9Ul9Fv3Jxzs", "title": "Burning $10K a Month",
                                             "name": "Raymond", "quote": '"x"', "about": "Raymond explains it."})
    assert "✅ First on the **Full Interviews** page too." in said
    assert _full(wp)["rows"] == [{"id": "9Ul9Fv3Jxzs", "title": "Burning $10K a Month",
                                  "name": "Raymond", "about": "Raymond explains it."}]
    assert website.decode(wp.pages[website.DATA_SLUG]["content"]["raw"])["featured"]["id"] == "9Ul9Fv3Jxzs"

    said = jobs.testimonial_publish(CONFIG, {"id": "NOPARAGRAPH", "title": "t", "name": "Cole",
                                             "quote": '"y"', "about": ""})
    assert "Not on the Full Interviews page: there's no paragraph about it" in said
    assert [one["id"] for one in _full(wp)["rows"]] == ["9Ul9Fv3Jxzs"]


def test_taking_one_down_takes_it_off_the_full_interviews_page_too(wp, monkeypatch):
    from wilbyte.bot import jobs

    jobs.testimonial_publish(CONFIG, {"id": "9Ul9Fv3Jxzs", "title": "t", "name": "Raymond",
                                      "quote": '"x"', "about": "a"})
    monkeypatch.setattr(jobs, "_success_list", lambda site: ({"sections": []}, False))
    said = jobs.testimonial_take_down(CONFIG, "9Ul9Fv3Jxzs")
    assert "Off the Full Interviews page too" in said
    assert _full(wp) == {"rows": [], "gone": ["9Ul9Fv3Jxzs"]}


def test_the_card_shows_the_paragraph_before_add(monkeypatch):
    import asyncio

    from wilbyte.bot import client, jobs, views

    asked = {}
    monkeypatch.setattr(jobs, "testimonial_draft", lambda config, link, **kw: asked.update(kw) or {
        "id": "9Ul9Fv3Jxzs", "title": "t", "name": "Raymond", "quote": '"x"', "about": kw["about"] or ""})

    class View:
        confirmed = False

        def __init__(self, **kw):
            pass

        async def wait(self):
            pass

    monkeypatch.setattr(views, "ConfirmView", View)
    sent = []

    class Heard:
        requester_id = 1

        async def send(self, content=None, **kw):
            sent.append(kw.get("embed"))

    config = NS(discord=NS(approval_timeout_seconds=60))
    asyncio.run(client._website_testimonial(Heard(), config, "https://youtu.be/9Ul9Fv3Jxzs quote: x about: He explains."))
    assert asked["about"] == "He explains."
    assert sent[-1].fields[0].name == "Full Interviews page" and sent[-1].fields[0].value == "He explains."
    asyncio.run(client._website_testimonial(Heard(), config, "https://youtu.be/9Ul9Fv3Jxzs name: Raymond quote: x"))
    assert sent[-1].fields[0].value.startswith("⚠ No paragraph")


# ------------------------------------------------ the snippet, in a browser

LOADER = Path(__file__).resolve().parents[1] / "website" / "full-interviews-loader.html"


def _row(rid, vid, title, name, about):
    return (f'<div id="row-{rid}" class="c-row c-wrapper row-{rid}"><div class="inner">'
            f'<div class="c-column"><div class="c-video"><figure class="video-container youtube">'
            f'<span class="w-100"><div data-bg-src="https://img.youtube.com/vi/{vid}/maxresdefault.jpg" class="w-100"></div></span></figure></div>'
            f'<div class="c-heading"><div><h1>{title}</h1></div></div>'
            f'<div class="c-paragraph"><div><p><em>with {name}</em></p></div></div></div>'
            f'<div class="c-column"><div class="c-paragraph"><div><p>{about}</p></div></div></div></div></div>'
            f'<div id="row-line{rid}" class="c-row c-wrapper"><div class="inner"><hr></div></div>')


GHL_PAGE = ('<div id="row-title" class="c-row"><h1>Full Interviews</h1></div><div id="stack"></div>'
            '<script>setTimeout(function(){ document.getElementById("stack").innerHTML = %s; }, 200);</script>')


def _show(data, *, status=200, path="/training/agent-success-full-interviews"):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("no playwright")
    rows = (_row("e", "U1O8FXthqzQ", "Emanuel's story", "Emanuel Nazco", "Emanuel walks through it.")
            + _row("j", "nxsWmQrPq-Q", "Jonny's story", "Jonathan Shinn", "Jonny explains.")
            + _row("r", "9Ul9Fv3Jxzs", "Raymond's story", "Raymond", "Raymond breaks it down."))
    page_html = GHL_PAGE % json.dumps(rows) + LOADER.read_text(encoding="utf-8")
    body = json.dumps([{"content": {"rendered": "<p>LLFULL-" + json.dumps(data).encode().hex() + "</p>"}}])
    with sync_playwright() as p:
        kw = {"executable_path": "/opt/pw-browsers/chromium"} if os.path.exists("/opt/pw-browsers/chromium") else {}
        try:
            browser = p.chromium.launch(**kw)
        except Exception:
            pytest.skip("no browser here")
        page = browser.new_page()
        asked = []
        page.route("https://agentleadlab.test/**", lambda r: r.fulfill(body=page_html, content_type="text/html"))
        page.route("https://leadlabcrm.com/**", lambda r: asked.append(1) or r.fulfill(
            status=status, body=body if status == 200 else "", content_type="application/json",
            headers={"access-control-allow-origin": "*"}))
        page.route("https://img.youtube.com/**", lambda r: r.fulfill(status=404))
        page.goto("https://agentleadlab.test" + path)
        page.wait_for_timeout(1800)
        shown = page.evaluate("""() => [...document.querySelectorAll('.c-row')]
            .filter(r => r.querySelector('figure') && r.style.display !== 'none')
            .map(r => [r.querySelector('h1').textContent, r.querySelector('em').textContent,
                       r.querySelectorAll('.c-column')[1].innerText.trim(),
                       r.querySelector('[data-bg-src]').getAttribute('data-bg-src')])""")
        lines = page.evaluate("() => [...document.querySelectorAll('hr')].filter(h => h.closest('.c-row').style.display !== 'none').length")
        playing = None
        if page.query_selector('[data-ll-full="NEW00000000"] figure'):
            page.click('[data-ll-full="NEW00000000"] figure')
            playing = page.get_attribute('[data-ll-full="NEW00000000"] iframe', "src")
        browser.close()
    return shown, lines, playing, asked


def test_the_page_gets_the_new_one_first_and_still_plays():
    data = fullpage.add(None, {"id": "RAYMONDDUPE", "title": "x", "name": "x", "about": "x"})
    data = fullpage.remove(data, "RAYMONDDUPE")
    data = fullpage.add(data, {"id": "9Ul9Fv3Jxzs", "title": "Raymond's story", "name": "Raymond", "about": "a"})
    data = fullpage.add(data, {"id": "NEW00000000", "title": "Pat's story", "name": "Pat Tester",
                               "about": "Pat explains everything."})
    data = fullpage.remove(data, "nxsWmQrPq-Q")
    shown, lines, playing, _asked = _show(data)

    assert [one[0] for one in shown] == ["Pat's story", "Raymond's story", "Emanuel's story"], \
        "new first, Raymond moved up from the bottom, Jonny taken off"
    assert shown[0][1:3] == ["with Pat Tester", "Pat explains everything."]
    assert "/vi/NEW00000000/" in shown[0][3]
    assert lines == 3, "a divider under each interview shown"
    assert playing.startswith("https://www.youtube-nocookie.com/embed/NEW00000000")


def test_if_the_list_cant_be_read_the_page_is_left_as_it_was():
    shown, _lines, _playing, _asked = _show({}, status=500)
    assert [one[0] for one in shown] == ["Emanuel's story", "Jonny's story", "Raymond's story"]


def test_the_snippet_does_nothing_on_other_pages():
    _shown, _lines, _playing, asked = _show({"rows": [], "gone": []}, path="/success_stories")
    assert asked == [], "not even a request"
