"""The video testimonials on the website, kept up to date by RYTE.

"we have testimonials thats keeps getting updated ... once they are
uploaded on YT we want them added on our website ... i want it for ryte to be
able to add those new ones to the code we use".

The page's own code holds the list - a featured interview and `LL_VIDEOS`
under it - and changing it meant editing a twenty-thousand-line Elementor
widget by hand. So the list lives on the WordPress site as data RYTE writes,
and a small script added once to the page reads it and draws the section with
the page's own cards. What is already in the page stays as the fallback: if
the list can't be read, nothing on the site changes.

The data is a WordPress page, `ll-videos-data`, because that is what RYTE can
write with an Application Password and what any page - leadlabcrm.com's own or
a GHL landing page - can read back through the REST API. It is stored as hex:
WordPress runs a page's text through filters that turn quotes curly and "2x3"
into "2×3", and hex is the one spelling none of them touch.

Nothing here sends anybody anything; it writes one page on one site, when
Franklin has pressed the button.
"""

from __future__ import annotations

import json
import re

DATA_SLUG = "ll-videos-data"
DATA_TITLE = "Video testimonials data (RYTE)"
MARK = "LLVIDEOS-"


class WebsiteError(RuntimeError):
    pass


def encode(data: dict, *, mark: str = MARK) -> str:
    return mark + json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8").hex()


def decoded(text: str, *, mark: str) -> dict | None:
    """What a data page holds, whatever its shape."""
    found = re.search(re.escape(mark) + r"([0-9a-f]+)", str(text or ""))
    if not found or len(found.group(1)) % 2:
        return None
    try:
        data = json.loads(bytes.fromhex(found.group(1)).decode("utf-8"))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def decode(text: str) -> dict | None:
    data = decoded(text, mark=MARK)
    return data if data and isinstance(data.get("videos"), list) else None


def page_content(data: dict, *, mark: str = MARK, what: str = "video testimonials") -> str:
    return (
        f"<p>This page holds the list of {what} shown on the site. RYTE keeps "
        "it up to date - please don't edit it.</p>\n"
        f"<p>{encode(data, mark=mark)}</p>"
    )


# ------------------------------------------------ what the live page holds

_ENTRY = re.compile(
    r"\{\s*id:\s*'((?:[^'\\]|\\.)*)'\s*,\s*name:\s*'((?:[^'\\]|\\.)*)'\s*,\s*"
    r"quote:\s*'((?:[^'\\]|\\.)*)'\s*\}"
)


def _unjs(said: str) -> str:
    return re.sub(r"\\(.)", r"\1", said)


def parse_live(html: str) -> dict:
    """The featured interview and LL_VIDEOS, off the page as it is now - the
    list RYTE starts from the first time. Entries inside comments (the "copy
    a line above" example) are not interviews."""
    import html as htmlmod

    text = str(html or "")
    start = text.find("var LL_VIDEOS")
    if start < 0:
        raise WebsiteError("I couldn't find the video list (LL_VIDEOS) on the page.")
    end = text.find("];", start)
    block = re.sub(r"/\*.*?\*/", "", text[start:end if end > 0 else None], flags=re.DOTALL)
    videos = [
        {"id": _unjs(one), "name": _unjs(two), "quote": _unjs(three)}
        for one, two, three in _ENTRY.findall(block)
        if one != "PASTE_ID_HERE"
    ]
    size = re.search(r"LL_PAGE_SIZE\s*=\s*(\d+)", text)
    featured = None
    box = re.search(r'class="ll-video-featured.*?data-vid="([^"]+)".*?'
                    r'class="ll-speaker-name">(.*?)</div>.*?class="ll-speaker-quote">(.*?)</p>',
                    text, re.DOTALL)
    if box:
        featured = {
            "id": box.group(1),
            "name": htmlmod.unescape(re.sub(r"<[^>]+>", "", box.group(2))).strip(),
            "quote": htmlmod.unescape(re.sub(r"<[^>]+>", "", box.group(3))).strip(),
        }
    if not videos and not featured:
        raise WebsiteError("The page's video list was empty.")
    return {"featured": featured, "videos": videos, "page_size": int(size.group(1)) if size else 8}


def add(data: dict, video: dict) -> tuple[dict, str]:
    """The new interview as the featured one, whoever was featured moved to
    the top of the grid. (the new list, who moved down or "").

    An interview already on the page is moved, not added twice.
    """
    video = {"id": str(video["id"]), "name": str(video["name"]).strip(), "quote": str(video["quote"]).strip()}
    before = data.get("featured")
    videos = [one for one in data.get("videos") or [] if one.get("id") != video["id"]]
    moved = ""
    if before and before.get("id") != video["id"]:
        videos.insert(0, before)
        moved = str(before.get("name") or "")
    return {**data, "featured": video, "videos": videos}, moved


def find(data: dict, said: str) -> list[dict]:
    """The interviews a YouTube link, id or whole name means - featured one
    included. A name that two interviews share (there are two Williams)
    finds both, so the link has to settle it."""
    wanted = str(said or "").strip()
    link = re.search(r"(?:v=|youtu\.be/|shorts/|embed/)([A-Za-z0-9_-]{11})", wanted)
    key = link.group(1) if link else wanted
    everyone = ([data["featured"]] if data.get("featured") else []) + list(data.get("videos") or [])
    by_id = [one for one in everyone if one.get("id") == key]
    if by_id:
        return by_id
    name = " ".join(wanted.split()).casefold()
    return [one for one in everyone if " ".join(str(one.get("name") or "").split()).casefold() == name]


def remove(data: dict, video_id: str) -> tuple[dict, str]:
    """The list without that interview. (the new list, who is featured now).
    Taking the featured one away puts the next in line back in its place."""
    videos = [one for one in data.get("videos") or [] if one.get("id") != video_id]
    featured = data.get("featured")
    if featured and featured.get("id") == video_id:
        featured = videos.pop(0) if videos else None
    return {**data, "featured": featured, "videos": videos}, str((featured or {}).get("name") or "")


def quote_marks(quote: str) -> str:
    """The quote in straight double quotes, as every one on the page is."""
    said = " ".join(str(quote or "").split()).strip().strip('"“”').strip()
    return f'"{said}"' if said else ""


# ------------------------------------------------ WordPress


class WordPress:
    """leadlabcrm.com's REST API, as the user whose Application Password it is.
    One page is written - the data page - and nothing else."""

    def __init__(self, url: str, user: str, password: str, *, timeout: float = 30.0):
        import httpx

        if not (url and user and password):
            raise WebsiteError(
                "WORDPRESS_URL, WORDPRESS_USER and WORDPRESS_APP_PASSWORD all need to be in .env."
            )
        self.url = url.rstrip("/")
        self._http = httpx.Client(timeout=timeout, auth=(user, password.replace(" ", "")),
                                  headers={"Accept": "application/json"}, follow_redirects=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._http.close()

    def _check(self, got, doing: str):
        if got.status_code in (401, 403):
            raise WebsiteError(
                f"WordPress refused to let me {doing} (HTTP {got.status_code}) - check "
                "WORDPRESS_USER and WORDPRESS_APP_PASSWORD, and that the user is an admin."
            )
        if got.status_code >= 400:
            raise WebsiteError(f"WordPress said HTTP {got.status_code} when I tried to {doing}.")
        return got.json()

    def me(self) -> dict:
        return self._check(self._http.get(f"{self.url}/wp-json/wp/v2/users/me",
                                          params={"context": "edit"}), "sign in")

    def live_page(self, url: str = "") -> str:
        got = self._http.get(url or self.url + "/", auth=None)
        if got.status_code >= 400:
            raise WebsiteError(f"{url or 'The home page'} answered HTTP {got.status_code}.")
        return got.text

    def _data_page(self, slug: str = DATA_SLUG) -> dict | None:
        found = self._check(self._http.get(
            f"{self.url}/wp-json/wp/v2/pages",
            params={"slug": slug, "status": "publish,draft,private", "context": "edit"},
        ), "read the video list")
        return found[0] if found else None

    def load(self, *, slug: str = DATA_SLUG, mark: str = MARK) -> dict | None:
        page = self._data_page(slug)
        if not page:
            return None
        content = page.get("content") or {}
        said = content.get("raw") or content.get("rendered") or ""
        return decode(said) if mark == MARK else decoded(said, mark=mark)

    def save(self, data: dict, *, slug: str = DATA_SLUG, title: str = DATA_TITLE,
             mark: str = MARK, what: str = "video testimonials") -> str:
        page = self._data_page(slug)
        body = {"title": title, "content": page_content(data, mark=mark, what=what),
                "status": "publish", "slug": slug, "comment_status": "closed",
                "ping_status": "closed"}
        where = f"{self.url}/wp-json/wp/v2/pages" + (f"/{page['id']}" if page else "")
        saved = self._check(self._http.post(where, json=body), "save the video list")
        return str(saved.get("link") or "")
