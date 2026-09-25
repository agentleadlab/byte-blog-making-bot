"""Finding out what Payra's API is, from its own docs, and what the token
can read - without changing anything.

"will it work? how do i know what that api is for?" The docs live at
app.payra.com/docs/3.1, which RYTE on the office Mac can reach and this
code's author could not. So RYTE reads them - every page the introduction
links to - and hands them back as one file, and tries the token on each
read-only address the docs name.

GET only. Nothing is created, charged, refunded or changed, and nothing an
agent paid is shown: only which addresses answered and the names of the
fields in what came back.

The pure half is here - reading pages, finding the addresses, describing a
reply - and the fetching is `jobs.payra_probe`.
"""

from __future__ import annotations

import html as htmlmod
import re
from urllib.parse import urljoin, urlsplit

DOCS_START = "https://app.payra.com/docs/3.1/introduction"

#: Pages read, at most. The docs are a sidebar of sections, not a site.
MOST_PAGES = 60

#: Addresses tried, at most.
MOST_TRIES = 25


def page_text(page: str) -> str:
    """A docs page as readable text: headings, paragraphs and code kept,
    scripts and styles gone."""
    page = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", str(page or ""))
    page = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h[1-6]|tr|pre|code|table)>", "\n", page)
    page = re.sub(r"(?i)<h([1-6])[^>]*>", lambda m: "\n" + "#" * int(m.group(1)) + " ", page)
    text = htmlmod.unescape(re.sub(r"<[^>]+>", " ", page))
    lines = [" ".join(line.split()) for line in text.splitlines()]
    kept, blank = [], False
    for line in lines:
        if line:
            kept.append(line)
            blank = False
        elif not blank:
            kept.append("")
            blank = True
    return "\n".join(kept).strip()


def doc_links(page: str, base: str) -> list[str]:
    """The other docs pages a page links to - same site, same version."""
    root = urlsplit(base)
    prefix = "/".join(root.path.split("/")[:3])  # "/docs/3.1"
    found = []
    for href in re.findall(r"""href\s*=\s*["']([^"'#]+)""", str(page or ""), re.IGNORECASE):
        url = urljoin(base, href.strip())
        parts = urlsplit(url)
        if parts.netloc == root.netloc and parts.path.startswith(prefix) and url not in found:
            found.append(url.split("?")[0])
    return found


_CALL = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\s+(https?://[^\s\"'<>`]+|/[^\s\"'<>`]+)")
_ABSOLUTE = re.compile(r"https?://[\w.-]+(?:/[^\s\"'<>`]*)?", re.IGNORECASE)


def calls_in(text: str) -> list[tuple[str, str]]:
    """Every "GET /something" the docs name, in order, each once."""
    found = []
    for method, path in _CALL.findall(str(text or "")):
        pair = (method.upper(), path.rstrip(".,;:)"))
        if pair not in found:
            found.append(pair)
    return found


def api_bases(text: str) -> list[str]:
    """The sites the API lives on - "https://api.payra.com" - from the full
    addresses the docs write out, with /api or /v1 in them. A relative
    "/v1/invoices" is joined onto these."""
    found = []
    for one in _ABSOLUTE.findall(str(text or "")):
        parts = urlsplit(one.rstrip("/.,;:)"))
        if "/docs/" in parts.path or not re.search(r"/(?:api|v\d)\b", parts.path + "/", re.IGNORECASE):
            continue
        root = f"{parts.scheme}://{parts.netloc}"
        if root not in found:
            found.append(root)
    return found


def worth_trying(calls, bases) -> list[str]:
    """The read-only addresses to try: GETs with nothing to fill in."""
    urls = []
    for method, path in calls:
        if method != "GET" or re.search(r"[{}:<>]|\$\w", path.split("://", 1)[-1]):
            continue
        if path.startswith("http"):
            url = path
        elif bases:
            url = bases[0].rstrip("/") + "/" + path.lstrip("/")
        else:
            continue
        if url not in urls:
            urls.append(url)
    return urls[:MOST_TRIES]


def shape_of(body, depth: int = 0) -> str:
    """What a reply looks like, without anything in it: field names, and the
    field names of the first item of a list. Never a value - these are
    agents' payments."""
    if depth > 3:
        return "…"
    if isinstance(body, dict):
        inner = []
        for key, value in list(body.items())[:30]:
            if isinstance(value, (dict, list)):
                inner.append(f"{key}: {shape_of(value, depth + 1)}")
            else:
                inner.append(str(key))
        return "{" + ", ".join(inner) + "}"
    if isinstance(body, list):
        return f"[{len(body)} × {shape_of(body[0], depth + 1)}]" if body else "[]"
    return type(body).__name__
