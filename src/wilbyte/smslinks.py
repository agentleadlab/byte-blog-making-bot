"""The links in the texts: what each one is, and which ones Faith sends.

"Here's your sheet: https://docs.google.com/spreadsheets/d/…" is not a
string of letters to copy - it is the agent's lead sheet, and RYTE can open
it. An agent pasting a Stripe link is asking about a payment; Faith sending
the same form link to forty agents is a habit a draft can follow. So each
link is named for what it is, and Faith's own are counted: the ones she
sends everybody are hers to reuse, the ones she sent one agent are theirs.

No network in here. Opening a link is `jobs.read_link`, with RYTE's access.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

# Texts carry links bare ("docs.google.com/…"), with a scheme, or wrapped in
# punctuation a sentence put there - "(here: https://…)." - which is not
# part of the link.
_LINK = re.compile(
    r"(?:https?://|www\.)[^\s<>\"']+"
    r"|\b(?:[a-z0-9-]+\.)+(?:com|net|org|io|co|me|ly|us|app|link)/[^\s<>\"']*",
    re.IGNORECASE,
)
_TRAILING = ".,;:!?)]}'\""


def links_in(text: str) -> list[str]:
    """Every link in some text, in order, each once, with a scheme."""
    found, seen = [], set()
    for one in _LINK.findall(str(text or "")):
        one = one.rstrip(_TRAILING)
        if not one.lower().startswith(("http://", "https://")):
            one = "https://" + one
        if one not in seen and "." in urlsplit(one).netloc:
            seen.add(one)
            found.append(one)
    return found


#: (kind, what it is, how to recognise it) - first match wins.
KINDS = (
    ("sheet", "a Google Sheet", r"docs\.google\.com/spreadsheets/"),
    ("doc", "a Google Doc", r"docs\.google\.com/(?:document|presentation|forms)/"),
    ("drive", "a Google Drive file", r"drive\.google\.com/"),
    ("trello", "a Trello card", r"trello\.com/c/"),
    ("loom", "a Loom video", r"loom\.com/(?:share|embed|v)/"),
    ("stripe_pay", "a Stripe payment link", r"(?:buy|checkout)\.stripe\.com/"),
    ("stripe_invoice", "a Stripe invoice", r"(?:invoice|pay)\.stripe\.com/"),
    ("pandadoc", "a PandaDoc document", r"pandadoc\.com/"),
    ("zoom", "a Zoom meeting", r"zoom\.us/"),
    ("calendar", "a booking link", r"calendly\.com/|/widget/booking/|/booking/"),
    ("form", "a form", r"/widget/form/|/widget/survey/|forms\.gle/|jotform|typeform"),
)


def kind_of(link: str) -> tuple[str, str]:
    """(kind, what it is in words) - ("sheet", "a Google Sheet")."""
    said = str(link or "").lower()
    for kind, words, pattern in KINDS:
        if re.search(pattern, said):
            return kind, words
    return "web", f"a page on {urlsplit(said).netloc.removeprefix('www.') or 'the web'}"


def same_link(link: str) -> str:
    """A link as compared with another: host and path, no tracking tail.

    Faith's form link sent with "?utm_source=sms" and without is one link.
    A sheet's tab is kept - "#gid=" is not in the path - because the tab is
    part of which sheet it is only in a way that does not matter here.
    """
    parts = urlsplit(str(link or ""))
    return (parts.netloc.lower().removeprefix("www.") + parts.path.rstrip("/")).strip()


def catalog(texts: list, *, most: int = 25) -> list[dict]:
    """Every link Faith has sent, most sent first.

    [{"link", "kind", "what", "times", "agents", "last", "said"}], where
    `agents` is how many different agents got it and `said` is her newest
    text with it in, for what it is for. Sent to several agents, it is one
    of her standing links; sent to one, it belongs to that agent.
    """
    held: dict[str, dict] = {}
    for one in sorted(texts, key=lambda text: text.at):
        if one.inbound or one.team:
            continue
        for link in links_in(one.said):
            key = same_link(link)
            if not key:
                continue
            kind, what = kind_of(link)
            entry = held.setdefault(key, {
                "link": link, "kind": kind, "what": what, "times": 0,
                "agents": set(), "last": "", "said": "",
            })
            entry["times"] += 1
            entry["agents"].add(one.agent)
            entry["last"], entry["said"], entry["link"] = one.at, one.said, link
    found = sorted(
        held.values(), key=lambda one: (len(one["agents"]), one["times"], one["last"]),
        reverse=True,
    )[:most]
    return [{**one, "agents": len(one["agents"])} for one in found]


def standing(texts: list, *, most: int = 15) -> list[dict]:
    """Her links for everybody - sent to at least two different agents."""
    return [one for one in catalog(texts, most=most * 3) if one["agents"] >= 2][:most]


def search(texts: list, words: str, *, agent: str = "", most: int = 12) -> list:
    """Past texts with these words in, newest first - any conversation.

    For the context clue in somebody else's thread: three agents saying
    their leads stopped this morning, or the last time this agent asked.
    """
    wanted = [one for one in re.findall(r"[a-z0-9']+", str(words or "").casefold()) if len(one) > 2]
    if not wanted:
        return []
    found = []
    for one in sorted(texts, key=lambda text: text.at, reverse=True):
        if agent and one.agent != agent:
            continue
        said = one.said.casefold()
        if all(word in said for word in wanted):
            found.append(one)
            if len(found) >= most:
                break
    return found


def tally(done: list) -> list[dict]:
    """The links in her answers, counted. [{"link", "what", "times", "last"}].

    Counted here rather than by Claude: "which link does she send" is a
    question with an exact answer, and a count is not something to guess.
    """
    held: dict[str, dict] = {}
    for one in sorted(done, key=lambda swap: swap.at):
        for link in links_in(one.answered):
            entry = held.setdefault(same_link(link), {
                "link": link, "what": kind_of(link)[1], "times": 0, "last": "",
            })
            entry["times"] += 1
            entry["last"], entry["link"] = one.at, link
    return sorted(held.values(), key=lambda one: (one["times"], one["last"]), reverse=True)
