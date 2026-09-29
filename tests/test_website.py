"""The website's video testimonials: the list RYTE keeps on WordPress."""

import asyncio
from types import SimpleNamespace as NS

import httpx
import pytest

from wilbyte import website

LIVE = """<style>.ll-video-featured { color: red }</style>
<div class="ll-video-featured ll-reveal"> <button type="button" class="ll-video-frame" data-vid="U1O8FXthqzQ"
 aria-label="Play Emanuel Nazco's testimonial"> <img src="https://img.youtube.com/vi/U1O8FXthqzQ/hqdefault.jpg"> </button>
 <div class="ll-video-meta"><div class="ll-speaker-name">Emanuel Nazco</div><p class="ll-speaker-quote">"You can&#39;t complain about the leads."</p></div> </div>
<script> var LL_PAGE_SIZE = 8; var LL_VIDEOS = [
 { id: 'nxsWmQrPq-Q', name: 'Jonathan Shinn', quote: '"The speed between thought and action — that\\'s what separates people."' },
 { id: '-aIcdjzwFsY', name: 'Karyn Giles', quote: '"I did $181,000 my first six months."' }
 /* ---- ADD NEW INTERVIEWS BELOW THIS LINE ----
 , { id: 'PASTE_ID_HERE', name: 'Name Here', quote: '"Your quote here."' } */
]; </script>"""


def test_the_live_list_is_read_off_the_page():
    got = website.parse_live(LIVE)
    assert got["featured"] == {"id": "U1O8FXthqzQ", "name": "Emanuel Nazco",
                               "quote": '"You can\'t complain about the leads."'}
    assert got["videos"] == [
        {"id": "nxsWmQrPq-Q", "name": "Jonathan Shinn",
         "quote": '"The speed between thought and action — that\'s what separates people."'},
        {"id": "-aIcdjzwFsY", "name": "Karyn Giles", "quote": '"I did $181,000 my first six months."'},
    ], "the example line in the comment is not an interview"
    assert got["page_size"] == 8
    with pytest.raises(website.WebsiteError):
        website.parse_live("<html>no videos here</html>")


def test_what_wordpress_does_to_text_leaves_the_list_alone():
    data = {"featured": {"id": "a", "name": "María O'Neil", "quote": '"From $0 to $15K — in 2x3 months..."'},
            "videos": [], "page_size": 8}
    stored = website.page_content(data)
    assert website.MARK in stored and "'" not in stored.split(website.MARK)[1]
    wrapped = "<p>This page…</p>\n<p>" + stored.split("<p>")[-1] + "\n"
    assert website.decode(wrapped) == data
    assert website.decode("nothing") is None and website.decode(website.MARK + "abc") is None


def test_the_new_interview_is_featured_and_the_last_moves_to_the_top():
    held = website.parse_live(LIVE)
    new = {"id": "NEW", "name": " Emanuel  Nazco Jr ", "quote": '"x"'}

    got, moved = website.add(held, new)

    assert got["featured"]["id"] == "NEW" and moved == "Emanuel Nazco"
    assert [one["id"] for one in got["videos"]] == ["U1O8FXthqzQ", "nxsWmQrPq-Q", "-aIcdjzwFsY"]
    again, moved = website.add(got, {"id": "nxsWmQrPq-Q", "name": "Jonathan Shinn", "quote": '"y"'})
    assert [one["id"] for one in again["videos"]].count("nxsWmQrPq-Q") == 0, "moved, not twice"
    assert again["featured"]["id"] == "nxsWmQrPq-Q"
    same, moved = website.add(again, {"id": "nxsWmQrPq-Q", "name": "Jonathan Shinn", "quote": '"z"'})
    assert moved == "" and len(same["videos"]) == len(again["videos"])


def test_quotes_are_written_the_way_the_page_writes_them():
    assert website.quote_marks("I sold 5 policies my first week") == '"I sold 5 policies my first week"'
    assert website.quote_marks('“Curly” in, straight out') == '"Curly” in, straight out"'.replace('”', '”')
    assert website.quote_marks("") == ""


class Site:
    """WordPress, as far as RYTE can tell."""

    def __init__(self, page=None, home=LIVE):
        self.page, self.home, self.seen = page, home, []

    def handler(self, request):
        self.seen.append(request)
        path = request.url.path
        if path == "/wp-json/wp/v2/users/me":
            return httpx.Response(200, json={"name": "Franklin", "roles": ["administrator"]})
        if path == "/wp-json/wp/v2/pages" and request.method == "GET":
            return httpx.Response(200, json=[self.page] if self.page else [])
        if path.startswith("/wp-json/wp/v2/pages") and request.method == "POST":
            import json

            body = json.loads(request.content)
            self.page = {"id": 7, "content": {"raw": body["content"]}, "link": "https://site/ll-videos-data"}
            return httpx.Response(201, json=self.page)
        if path == "/":
            return httpx.Response(200, text=self.home)
        return httpx.Response(404)


@pytest.fixture
def site(monkeypatch):
    made = Site()
    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(made.handler), **kw))
    return made


CONFIG = NS(secrets=NS(wordpress_url="https://leadlabcrm.com/", wordpress_user="franklin",
                       wordpress_app_password="abcd efgh ijkl mnop"))


def test_the_first_add_starts_from_the_page_and_writes_one_page(site):
    from wilbyte.bot import jobs

    said = jobs.testimonial_publish(CONFIG, {"id": "NEW", "name": "Maddy Grundig", "quote": '"x"'})

    assert said.startswith("✅ **Maddy Grundig** is the featured interview on the website now, "
                           "and **Emanuel Nazco** moved to the top of the grid.")
    assert "I started from the 2 interviews already on the page" in said
    posts = [one for one in site.seen if one.method == "POST"]
    assert [str(one.url) for one in posts] == ["https://leadlabcrm.com/wp-json/wp/v2/pages"]
    assert posts[0].headers["authorization"].startswith("Basic ")
    kept = website.decode(site.page["content"]["raw"])
    assert kept["featured"]["id"] == "NEW" and kept["videos"][0]["id"] == "U1O8FXthqzQ"

    site.seen.clear()
    jobs.testimonial_publish(CONFIG, {"id": "NEWER", "name": "Cole", "quote": '"y"'})
    assert [str(one.url) for one in site.seen if one.method == "POST"] == [
        "https://leadlabcrm.com/wp-json/wp/v2/pages/7"], "the same page updated"
    assert not [one for one in site.seen if one.url.path == "/"], "the page isn't read again"
    assert website.decode(site.page["content"]["raw"])["videos"][0]["id"] == "NEW"


def test_a_refused_password_says_what_to_check(monkeypatch):
    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real(
        transport=httpx.MockTransport(lambda r: httpx.Response(401, json={})), **kw))
    from wilbyte.bot import jobs

    with pytest.raises(website.WebsiteError, match="WORDPRESS_APP_PASSWORD"):
        jobs.website_check(CONFIG)
    with pytest.raises(website.WebsiteError, match="WORDPRESS_URL"):
        website.WordPress("", "u", "p")


def test_the_check_says_who_and_what(site):
    from wilbyte.bot import jobs

    said = jobs.website_check(CONFIG)
    assert said.startswith("✅ Signed in to <https://leadlabcrm.com> as **Franklin** (administrator).")
    assert "**Emanuel Nazco** featured, 2 more in the grid" in said


def test_a_name_and_quote_given_are_used_as_given(monkeypatch):
    from wilbyte import youtube
    from wilbyte.bot import jobs

    monkeypatch.setattr(youtube, "fetch_video", lambda vid: NS(title="How Emanuel Nazco Writes $250 Policies"))
    monkeypatch.setattr(jobs, "_pick_testimonial", lambda *a: pytest.fail("Claude asked for nothing"))

    got = jobs.testimonial_draft(CONFIG, "https://youtu.be/U1O8FXthqzQ", name="Emanuel Nazco",
                                 quote="No miracle lead out there")
    assert got == {"id": "U1O8FXthqzQ", "title": "How Emanuel Nazco Writes $250 Policies",
                   "name": "Emanuel Nazco", "quote": '"No miracle lead out there"'}


def test_otherwise_claude_picks_them_from_the_transcript(monkeypatch):
    from wilbyte import youtube
    from wilbyte.bot import jobs

    monkeypatch.setattr(youtube, "fetch_video", lambda vid: NS(title="t"))
    monkeypatch.setattr(jobs, "waiting_on_captions", lambda link: ("the transcript", ""))
    monkeypatch.setattr(jobs, "_pick_testimonial", lambda config, title, text: {"name": "Cole", "quote": "Connect the dots"})
    got = jobs.testimonial_draft(CONFIG, "https://www.youtube.com/watch?v=Yw8m6eovS1s")
    assert (got["name"], got["quote"]) == ("Cole", '"Connect the dots"')


@pytest.mark.parametrize("said, parts", [
    ("testimonial https://youtu.be/U1O8FXthqzQ", ("https://youtu.be/U1O8FXthqzQ", "", "")),
    # "testimonials" with an s started a blog post instead
    ("testimonials https://youtu.be/9Ul9Fv3Jxzs", ("https://youtu.be/9Ul9Fv3Jxzs", "", "")),
    ("Testimonial: https://youtu.be/9Ul9Fv3Jxzs", ("https://youtu.be/9Ul9Fv3Jxzs", "", "")),
    ("website video https://youtu.be/9Ul9Fv3Jxzs", ("https://youtu.be/9Ul9Fv3Jxzs", "", "")),
    ("add to website https://www.youtube.com/watch?v=U1O8FXthqzQ name: Emanuel Nazco quote: No miracle lead",
     ("https://www.youtube.com/watch?v=U1O8FXthqzQ", "Emanuel Nazco", "No miracle lead")),
    ("testimonial <https://youtu.be/abc> name: Cole", ("https://youtu.be/abc", "Cole", "")),
])
def test_asking_for_a_testimonial(said, parts):
    from wilbyte.bot import mentions

    got = mentions.parse("<@1> " + said)
    assert got.action == "testimonial"
    assert mentions.testimonial_parts(got.brief) == parts


def test_a_bare_youtube_link_is_still_a_blog_post():
    from wilbyte.bot import mentions

    assert mentions.parse("<@1> https://youtu.be/U1O8FXthqzQ").action != "testimonial"
    assert mentions.parse("<@1> website check").action == "websitecheck"


def test_nothing_goes_on_the_site_until_add_is_pressed(monkeypatch):
    from wilbyte.bot import client, jobs, views

    published, sent = [], []
    monkeypatch.setattr(jobs, "testimonial_draft", lambda config, link, **kw: {
        "id": "U1O8FXthqzQ", "title": "t", "name": "Emanuel Nazco", "quote": '"x"'})
    monkeypatch.setattr(jobs, "testimonial_publish", lambda config, video: published.append(video) or "✅ done")

    class View:
        confirmed = False

        def __init__(self, **kw):
            pass

        async def wait(self):
            pass

    monkeypatch.setattr(views, "ConfirmView", View)

    class Heard:
        requester_id = 1

        async def send(self, content=None, **kw):
            sent.append((content, kw.get("embed")))

    config = NS(discord=NS(approval_timeout_seconds=60))
    asyncio.run(client._website_testimonial(Heard(), config, "https://youtu.be/U1O8FXthqzQ"))
    assert published == [] and sent[-1][1].description.startswith("**Emanuel Nazco**")

    View.confirmed = True
    asyncio.run(client._website_testimonial(Heard(), config, "https://youtu.be/U1O8FXthqzQ"))
    assert published and sent[-1][0] == "✅ done"

    asyncio.run(client._website_testimonial(Heard(), config, "no link here"))
    assert sent[-1][0].startswith("Send the YouTube link")
