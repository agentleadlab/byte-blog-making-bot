"""What a post does for search, checked before it goes out.

"the analytics and seo is not improving". The brief always asked for a
primary keyword, a keyword map and internal links - and all three went into
a notes file nobody opened. The title that went to GHL was picked for being
*least* like the keyword-bearing H1, the image alt text was the slug with
hyphens in it, every post carried the same tags, and the internal links the
brief asked for could not exist: the model was never told what posts there
were to link to.

So the keyword is a field now, carried into the title, the slug, the H1, the
opening, a subheading, the description and the alt text - and checked here,
where a miss becomes a line on the review card instead of a post that ranks
for nothing. Links to the site's own posts are kept only when the post is
real: a link to a slug the model imagined is a 404 on the page, which search
engines count against it.

No network in here.
"""

from __future__ import annotations

import html as htmlmod
import re
from dataclasses import dataclass, field


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9$]+", htmlmod.unescape(str(text or "")).casefold())


def _root(word: str) -> str:
    """"leads" and "lead", "vets" and "vet", are one word to a search engine."""
    if len(word) > 3 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


# Words a keyword can drop without being a different search.
_FILLER = frozenset("a an the of for to in on and or with your my our how why what".split())


def has_keyword(text: str, keyword: str) -> bool:
    """Whether `text` carries the keyword: every word of it, singular or
    plural, in any order. "Veteran Life Insurance Leads: Why They Convert"
    carries "veteran life insurance lead"."""
    wanted = {_root(one) for one in _words(keyword) if one not in _FILLER}
    if not wanted:
        return False
    present = {_root(one) for one in _words(text)}
    return wanted <= present


def plain(html: str) -> str:
    """The words of some HTML, tags gone."""
    return " ".join(htmlmod.unescape(re.sub(r"<[^>]+>", " ", str(html or ""))).split())


def first_paragraph(html: str) -> str:
    found = re.search(r"<p\b[^>]*>(.*?)</p>", str(html or ""), re.IGNORECASE | re.DOTALL)
    return plain(found.group(1)) if found else ""


def opening(html: str, words: int = 100) -> str:
    """The first hundred words of the body, after the H1 - where search
    engines and readers both decide what the page is about."""
    body = re.sub(r"<h1\b.*?</h1>", " ", str(html or ""), flags=re.IGNORECASE | re.DOTALL)
    return " ".join(plain(body).split()[:words])


def subheadings(html: str) -> list[str]:
    return [plain(one) for one in re.findall(
        r"<h2\b[^>]*>(.*?)</h2>", str(html or ""), re.IGNORECASE | re.DOTALL
    )]


_LINK = re.compile(r"<a\b[^>]*\bhref\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
                   re.IGNORECASE | re.DOTALL)


def _same(url: str) -> str:
    """A URL as compared with another: no scheme, no www, no trailing slash,
    no query or fragment."""
    url = re.sub(r"^https?://", "", str(url or "").strip(), flags=re.IGNORECASE)
    url = re.sub(r"^www\.", "", url, flags=re.IGNORECASE)
    return re.split(r"[?#]", url, maxsplit=1)[0].rstrip("/").casefold()


def _a_post(href: str, base: str) -> bool:
    """Whether a link points at one of this blog's posts. `base` is where
    they live - "https://agentleadlab.com/post/"."""
    return _same(href).startswith(_same(base) + "/")


def site_links(html: str, base: str) -> list[str]:
    """Every link in the body to a post on this blog."""
    return [href for href, _text in _LINK.findall(str(html or "")) if _a_post(href, base)]


def keep_known_links(html: str, known, base: str) -> tuple[str, list[str]]:
    """(the body with links to posts that don't exist unwrapped, what went).

    The words stay; only the link comes off. Links anywhere else - the
    schedule-a-call page, the CRM - are left alone.
    """
    real = {_same(one) for one in known or ()}
    gone: list[str] = []

    def check(found):
        href, text = found.group(1), found.group(2)
        if _a_post(href, base) and _same(href) not in real:
            gone.append(href)
            return text
        return found.group(0)

    return _LINK.sub(check, str(html or "")), gone


def tags(base, primary: str, secondary=()) -> list[str]:
    """The post's own keywords, then the brand's, each once."""
    seen, out = set(), []
    for one in [primary, *(secondary or ()), *(base or ())]:
        tidy = " ".join(str(one or "").split()).casefold()
        if tidy and tidy not in seen:
            seen.add(tidy)
            out.append(tidy)
    return out


@dataclass
class Report:
    """Where the keyword landed, and what is missing."""

    keyword: str
    placed: dict = field(default_factory=dict)
    internal: int = 0
    removed: list = field(default_factory=list)

    @property
    def missing(self) -> list[str]:
        return [where for where, ok in self.placed.items() if not ok]

    def line(self) -> str:
        """For the review card: "veteran life insurance leads — title ✓ ..."."""
        if not self.keyword:
            return "No target keyword was chosen."
        marks = " · ".join(f"{where} {'✓' if ok else '✗'}" for where, ok in self.placed.items())
        return (
            f"**{self.keyword}** — {marks} · {self.internal} internal "
            f"link{'' if self.internal == 1 else 's'}"
        )

    def warnings(self) -> list[str]:
        said = []
        if not self.keyword:
            said.append("SEO: no target keyword - the post isn't aimed at any search.")
        elif self.missing:
            said.append(f"SEO: \"{self.keyword}\" isn't in the {', '.join(self.missing)}.")
        if self.keyword and not self.internal:
            said.append("SEO: no links to other Agent Lead Lab posts.")
        if self.removed:
            said.append(
                f"SEO: took out {len(self.removed)} link(s) to posts that don't exist."
            )
        return said


def check(*, keyword: str, title: str, slug: str, h1: str, html: str,
          description: str, alt: str, base: str, removed=()) -> Report:
    """Where the keyword is and isn't, in the places that count."""
    places = {
        "title": title, "URL": slug.replace("-", " "), "H1": h1,
        "opening": opening(html), "a subheading": " ".join(subheadings(html)),
        "description": description, "image alt": alt,
    }
    return Report(
        keyword=" ".join(str(keyword or "").split()),
        placed={where: has_keyword(text, keyword) for where, text in places.items()}
        if keyword else {},
        internal=len(site_links(html, base)),
        removed=list(removed),
    )
