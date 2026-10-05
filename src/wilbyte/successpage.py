"""The Success Stories page's video list, kept up to date by RYTE.

https://agentleadlab.com/success_stories - "Agents, In Their Own Words". One
row per website section, three videos each, newest first, and a "See all"
link to that section's own page. The sections are the ones RYTE files every
segment under, so a segment with its YouTube link is a card on this page
waiting to be put there.

The list lives beside the testimonials one, as its own WordPress data page,
and a snippet pasted once into the page draws it with the page's own cards.
RYTE keeps every video ever added, newest first; the page shows the first
three of each.

Nothing here sends anybody anything. It writes one page on one site, when
Franklin has said "post segments" and pressed the button.
"""

from __future__ import annotations

import json
import re

from .segments import SECTIONS

PAGE_URL = "https://agentleadlab.com/success_stories"
DATA_SLUG = "ll-training-data"
DATA_TITLE = "Success stories data (RYTE)"
MARK = "LLTRAINING-"
WHAT = "success-story videos"

#: Shown per section on the Success Stories page.
SHOWN = 3

_YOUTUBE = re.compile(r"(?:v=|youtu\.be/|shorts/|embed/)([A-Za-z0-9_-]{11})")


class SuccessError(RuntimeError):
    pass


def video_id(link: str) -> str:
    found = _YOUTUBE.search(str(link or ""))
    return found.group(1) if found else ""


def parse_live(html: str) -> dict:
    """The sections and videos the page holds now - `var LL_VIDEOS = [...]` in
    its own code - as RYTE's list starts from them the first time."""
    # GHL puts the page's code in twice: as it is, and escaped inside its own
    # page data. The plain one first; the escaped one only if that's all.
    raw = str(html or "")
    rows, problem = None, "I couldn't find the video list (LL_VIDEOS) on the Success Stories page."
    for text in (raw, raw.replace("\\u002F", "/").replace('\\"', '"')):
        for found in re.finditer(r"var LL_VIDEOS\s*=\s*(\[.*?\n\s*\]);", text, re.DOTALL):
            try:
                rows = json.loads(found.group(1))
                break
            except ValueError as exc:
                problem = f"The page's video list didn't read: {exc}"
        if rows is not None:
            break
    if rows is None:
        raise SuccessError(problem)
    sections = [
        {"c": str(row.get("c") or ""), "more": str(row.get("more") or ""),
         "v": [{"id": str(v.get("id") or ""), "t": str(v.get("t") or ""), "w": str(v.get("w") or "")}
               for v in row.get("v") or [] if v.get("id")]}
        for row in rows if row.get("c")
    ]
    if not sections:
        raise SuccessError("The Success Stories page's video list was empty.")
    return {"sections": sections}


def _section(data: dict, name: str) -> dict | None:
    wanted = re.sub(r"[^a-z]", "", (name or "").casefold())
    return next(
        (one for one in data.get("sections") or []
         if re.sub(r"[^a-z]", "", one.get("c", "").casefold()) == wanted),
        None,
    )


def _bare(title: str) -> str:
    return " ".join(re.sub(r"[^\w$%]+", " ", str(title or "").casefold()).split())


#: How alike two titles have to be to count as the same video posted twice.
SIMILAR = 0.9
#: Words a re-upload tends to gain that don't make it a different video.
_DRESSING = re.compile(r"\b(?:full interview|full|interview|official|video|clip|short|shorts|hd)\b")


def similar_titles(one: str, other: str) -> bool:
    """Whether two titles are the same video posted twice: the same once
    capitals, punctuation and a "(Full Interview)" are aside, or nearly the
    same. A different number is a different video - Part 1 isn't Part 2."""
    from difflib import SequenceMatcher

    a, b = _bare(one), _bare(other)
    if not a or not b:
        return False
    if re.findall(r"\d+", a) != re.findall(r"\d+", b):
        return False
    plain_a, plain_b = " ".join(_DRESSING.sub(" ", a).split()), " ".join(_DRESSING.sub(" ", b).split())
    if a == b or (plain_a and plain_a == plain_b):
        return True
    return SequenceMatcher(None, a, b).ratio() >= SIMILAR


def duplicate_of(data: dict, item: dict) -> str:
    """Why this is already on the page, or "" when it isn't - "it has to make
    sure not to post similar/duplicates". The same video in any section, or
    one whose title is the same or nearly so."""
    for row in data.get("sections") or []:
        for v in row.get("v") or []:
            if v.get("id") == item["id"]:
                return f"already on the page under {row['c']}"
            if similar_titles(v.get("t"), item["title"]):
                return f"looks the same as “{v.get('t')}” already under {row['c']}"
    return ""


def add(data: dict, items: list[dict]) -> tuple[dict, list[dict], list[dict]]:
    """Each {section, id, title, who} first in its section. (the new list,
    what went up, what was already there - each with its `why`). Nothing is
    added twice: not a video already on the page, not one with the same or a
    near-identical title, and not the same one twice in one go."""
    data = json.loads(json.dumps(data))
    went, already = [], []
    for item in items:
        if _section(data, item["section"]) is None:
            raise SuccessError(f"There's no “{item['section']}” section on the Success Stories page.")
        why = duplicate_of(data, item) or next(
            (f"the same as {one['what']} in this post" for one in went
             if one["id"] == item["id"] or similar_titles(one["title"], item["title"])),
            "",
        )
        if why:
            already.append({**item, "why": why})
            continue
        went.append(item)
    # Last first, so the order they were given in is the order they show in.
    for item in reversed(went):
        _section(data, item["section"])["v"].insert(0, {"id": item["id"], "t": item["title"], "w": item["who"]})
    return data, went, already


def remove(data: dict, video: str) -> tuple[dict, list[dict]]:
    """The list without that video, wherever it is. (the new list, what went)."""
    data = json.loads(json.dumps(data))
    gone = []
    for row in data.get("sections") or []:
        keep = []
        for v in row["v"]:
            (gone if v["id"] == video else keep).append({**v, "section": row["c"]})
        row["v"] = [{k: v[k] for k in ("id", "t", "w")} for v in keep]
    return data, gone


# ------------------------------------------------ out of the posting doc

_HEAD = re.compile(
    r"^(Full interview|Segment \d+)\s+·\s+(\d{1,2}:\d{2}(?::\d{2})?)\s*[–—-]\s*(\d{1,2}:\d{2}(?::\d{2})?)",
    re.IGNORECASE,
)
_LABELS = {"YOUTUBE TITLE": "title", "WEBSITE SECTION": "section", "YOUTUBE LINK": "link"}


def from_tab(name: str, text: str) -> tuple[list[dict], list[str], list[dict]]:
    """The segments in one tab of the posting doc, ready to post.
    ([{section, id, title, who, what}], [what was skipped and why],
    [the ones with no YouTube link in the doc - to be looked for]).

    The laid-out tab - "YOUTUBE TITLE", "WEBSITE SECTION", "YOUTUBE LINK" -
    or an old one, read the way `fix doc` reads it. A segment with no
    YouTube link yet is skipped and said: there is nothing to show.
    """
    from . import segments as segmenting

    said = str(text or "").replace("\x0b", "\n").replace(segmenting._PIC, "")
    found = []
    if "\nYOUTUBE TITLE" in said:
        current, field = None, ""
        for line in said.split("\n"):
            bare = line.strip()
            head = _HEAD.match(bare)
            if head:
                current = {"what": head.group(1).capitalize() if head.group(1)[0].islower() else head.group(1),
                           "long": head.group(1).casefold() == "full interview",
                           "title": "", "section": "", "link": "", "links": []}
                found.append(current)
                field = ""
                continue
            if current is None:
                continue
            if bare in _LABELS:
                field = _LABELS[bare]
                continue
            if bare.isupper() and len(bare) > 3:
                field = ""
                continue
            current["links"] += [one for one in re.findall(r"https?://\S+", bare) if video_id(one)]
            if field and bare and not current[field]:
                current[field] = bare
                field = ""
        entries = [
            {"what": one["what"], "long": one["long"],
             "title": " ".join(re.sub(r"https?://\S+", " ", one["title"]).split()),
             "section": one["section"],
             "link": one["link"] if video_id(one["link"]) else (one["links"][0] if one["links"] else "")}
            for one in found
        ]
    else:
        try:
            _payload, recovered = segmenting.read_back(text)
        except segmenting.SegmentError as exc:
            raise SuccessError(f"I couldn't read the segments in “{name}”: {exc}") from exc
        entries, number = [], 0
        for one in recovered:
            number += 0 if one.long_form else 1
            entries.append({
                "what": "Full interview" if one.long_form else f"Segment {number}",
                "long": one.long_form, "title": one.yt_title, "section": one.website_section,
                "link": one.youtube.split()[0] if one.youtube else "",
            })

    items, skipped, waiting = [], [], []
    for one in entries:
        # The rule, applied here too: whatever an old tab says, the full
        # interview goes under Agent Success Full Interviews and a clip never.
        section = one["section"]
        if one["long"]:
            section = segmenting.FULL_INTERVIEWS
        elif section.casefold() == segmenting.FULL_INTERVIEWS.casefold():
            skipped.append(f"{one['what']} — filed under {section}, which is for full interviews only; "
                           "fix its section in the doc first")
            continue
        if not any(section.casefold() == known.casefold() for known in SECTIONS):
            skipped.append(f"{one['what']} — “{section or 'no section'}” isn't one of the website sections")
            continue
        vid = video_id(one["link"])
        entry = {"section": next(k for k in SECTIONS if k.casefold() == section.casefold()),
                 "id": vid, "title": one["title"], "who": name, "what": one["what"]}
        if not vid:
            waiting.append(entry)
            continue
        items.append(entry)
    return items, skipped, waiting


def same_title(one: str, other: str) -> bool:
    """The same title, capitals and punctuation aside - and nothing else aside.
    "Not Playing Small" is "not playing small!"; a shortened title is not."""
    def bare(said: str) -> str:
        return " ".join(re.sub(r"[^\w$%]+", " ", str(said or "").casefold()).split())

    return bool(bare(one)) and bare(one) == bare(other)


def channels_in(said: str) -> list[str]:
    """The channels a setting names, in order: UC... ids, @handles, or links
    to either - "yt has two accounts"."""
    found = []
    for one in re.split(r"[\s,]+", str(said or "")):
        one = one.strip().rstrip("/")
        if not one:
            continue
        uc = re.search(r"(UC[A-Za-z0-9_-]{20,})", one)
        handle = re.search(r"(?:^|/)(@[A-Za-z0-9._-]+)", one)
        if uc:
            found.append(uc.group(1))
        elif handle:
            found.append(handle.group(1))
    return list(dict.fromkeys(found))


def match_channels(waiting: list[dict], by_channel: list[list[dict]]) -> tuple[list[dict], list[str]]:
    """`match_uploads` across several channels, main one first: the first
    channel with that title decides, and two videos with it there is still a
    question rather than a pick."""
    found, missing = [], []
    for one in waiting:
        answer = None
        for uploads in by_channel:
            hit, miss = match_uploads([one], uploads)
            if hit:
                answer = (hit, [])
                break
            if miss and "videos on the channel have that title" in miss[0]:
                answer = ([], miss)
                break
        if answer is None:
            answer = ([], [f"{one['what']} — no video on "
                           f"{'either channel' if len(by_channel) > 1 else 'the channel'} "
                           "with that exact title yet"])
        found += answer[0]
        missing += answer[1]
    return found, missing


def match_uploads(waiting: list[dict], uploads: list[dict]) -> tuple[list[dict], list[str]]:
    """The segments with no link in the doc, looked for on the channel by title.
    ([found, each with its id], [what wasn't, and why]).

    "i dont want him uploading incorrect videos" - so only an exact title, and
    only one video with it. Two with the same title is a question for a
    person, not a coin to toss.
    """
    found, missing = [], []
    for one in waiting:
        hits = list(dict.fromkeys(
            up["id"] for up in uploads if up.get("id") and same_title(up.get("title"), one["title"])
        ))
        if len(hits) == 1:
            found.append({**one, "id": hits[0], "found": True})
        elif hits:
            missing.append(f"{one['what']} — {len(hits)} videos on the channel have that title; "
                           "paste the right link into the doc")
        else:
            missing.append(f"{one['what']} — no video on the channel with that exact title yet")
    return found, missing
