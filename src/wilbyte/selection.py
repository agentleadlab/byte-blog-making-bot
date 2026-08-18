"""Wil's editorial judgment calls, encoded.

Two decisions he makes by hand on every post:

1. **Blog title** - pick a headline option that is clearly *not* the article's
   own H1. In the walkthrough: "I always opt to not choose whatever the headline
   one of the copy that he spit out... I use the headline that is the opposite or
   not similar to the headline 1."

2. **Cover image text** - a 3-5 word highlighted kicker on top, and a different,
   longer headline underneath. He combines options 1+2 or 2+3, never repeating
   the same line twice.
"""

from __future__ import annotations

import re

from .config import Config
from .models import CopyPackage, CoverPlan, Headline

# Words that carry no distinguishing signal when comparing two headlines.
_STOPWORDS = frozenset(
    """a an and are as at be but by for from how if in into is it its of on or that
    the to up via was what when where which who why will with you your""".split()
)

# How similar a headline may be to the article H1 before it counts as "the same".
SIMILARITY_THRESHOLD = 0.6

# How short a complete headline may be, relative to a trimmed one, and still be
# preferred for the cover.
WHOLE_HEADLINE_RATIO = 0.6

# How far over the character limit a *complete* headline may run before it is
# worth cutting. The renderer scales text to its box, so a little over just
# reads slightly smaller - where a cut can change what the line says.
HEADLINE_OVERFLOW_RATIO = 1.25


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9$']+", text.lower())
    return {w for w in words if w not in _STOPWORDS}


def similarity(a: str, b: str) -> float:
    """Jaccard overlap of significant words. 1.0 = same headline, 0.0 = unrelated."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def choose_title(copy: CopyPackage, *, threshold: float = SIMILARITY_THRESHOLD) -> tuple[Headline, str]:
    """Pick the blog listing title.

    Wil's rule is "use the headline that is the opposite or not similar to
    headline 1 clearly" - so take the option that overlaps the article H1 the
    *least*, not merely the first one that clears a bar. Ties go to the earlier
    option. Returns the headline and a note explaining the choice.
    """
    scored = [(h, similarity(h.text, copy.article_h1)) for h in copy.headline_options]
    best_index, (chosen, score) = min(enumerate(scored), key=lambda pair: pair[1][1])
    index = best_index + 1

    if score < threshold:
        return chosen, f"option {index} (least similar to H1: {score:.2f})"

    return chosen, (
        f"option {index} chosen as least-similar fallback (similarity {score:.2f}); "
        "every headline option overlapped the article H1 - worth a manual look"
    )


def _strip_trailing_punct(text: str) -> str:
    return text.strip().rstrip(":—–-,. ")


def _strip_label_punct(text: str) -> str:
    """As above, plus sentence enders. A big headline may ask a question; the
    small label above it should not, and a trailing `?` there reads as debris."""
    return _strip_trailing_punct(text).rstrip("?! ").rstrip(":—–-,. ")


def _normalize(text: str) -> str:
    """Lowercase, punctuation-free form used for prefix comparisons."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9$ ]+", "", text.lower())).strip()


# A kicker is a label, not the first half of a sentence. Any slice that opens
# with one of these is the start of a question whose answer got cut off -
# "Why Your Life Insurance Intro" - and on the image it reads as an
# interrupted thought sitting above an unrelated headline.
_CLAUSE_OPENERS = frozenset(
    """why how what when where who which whether if is are was were do does did
    can could should would will has have had""".split()
)


def reads_as_a_label(text: str) -> bool:
    """True if this could sit on a cover as a phrase in its own right.

    Three ways it fails, all of them seen on real covers: it opens a question
    it never answers ("Why Your Life Insurance Intro"), it carries a clause it
    never finishes ("The Intro Script That Decides", "New Agents Stall After
    Thirty"), or it trails off on a joining word.

    Coordinators are allowed through - "Team, Focus and Routine" is a list, not
    an unfinished thought.
    """
    words = _strip_label_punct(text).split()
    if len(words) < 2:
        return False
    if _bare(words[0]) in _CLAUSE_OPENERS:
        return False
    if any(_bare(w) in _FRAGMENT_MARKERS for w in words):
        return False
    # A sentence break inside the phrase means it spans two thoughts:
    # "LEADS? ANSWER ONE QUESTION FIRST" is the end of one and the whole of
    # the next.
    if re.search(r"[?!.](?=\s)", _strip_label_punct(text)):
        return False
    return _bare(words[-1]) not in _STOPWORDS


def kicker_candidates(copy: CopyPackage, config: Config) -> list[str]:
    """Short fragments usable as the highlighted line on the cover image.

    Preference order:
      1. The kicker the copywriter wrote for this post.
      2. The segment before a colon/dash in a headline ("Aged, Fresh, Premium:").
      3. The leading N words of a headline, if that leaves a phrase that stands up.

    Slicing words off a headline was the only source for a long time, and it
    produces text that is grammatical nowhere: the cover said "WHY YOUR LIFE
    INSURANCE INTRO" above a headline about aged versus fresh leads. Asking for
    a purpose-written label removes the guesswork; the slices remain as a
    fallback, now filtered so a fragment can't reach the image.
    """
    lo, hi = config.cover.kicker_min_words, config.cover.kicker_max_words
    candidates: list[str] = []

    written = _strip_label_punct(copy.cover_kicker)
    # Trust it on wording, not on length - an eight-word "kicker" would run off
    # the highlight box whatever it says.
    if written and len(written.split()) <= hi + 1:
        candidates.append(written)

    for headline in copy.headline_options:
        text = headline.text
        head = re.split(r"[:—–]| - ", text, maxsplit=1)[0]
        head = _strip_label_punct(head)
        if lo <= len(head.split()) <= hi:
            candidates.append(head)

    # Deliberately *not* the leading N words of a headline. That was the only
    # other source for a long time and it cannot work: the first five words of
    # a sentence are the first five words of a sentence. It produced "WHY YOUR
    # LIFE INSURANCE INTRO" and "THE INTRO SCRIPT THAT DECIDES" - each one a
    # thought that stops before it arrives. Take the subject instead.
    for headline in copy.headline_options:
        label = label_from(headline.text, config)
        if reads_as_a_label(label):
            candidates.append(label)

    # De-duplicate case-insensitively while preserving order.
    seen: set[str] = set()
    unique = []
    for candidate in candidates:
        key = candidate.lower()
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def cover_headline(copy: CopyPackage, title: Headline, config: Config) -> str:
    """The big line on the cover - short enough to still read as a poster.

    The blog title can run long; the cover cannot. Preference order:
      1. The chosen title, if it's already short.
      2. The shortest headline option that fits.
      3. The chosen title cut at a natural break (colon, dash, or a clause).
    """
    limit = config.cover.headline_max_chars
    chosen = _strip_trailing_punct(title.text)

    # The template shrinks text to fit its box, so the limit is about how a
    # poster reads, not about what physically fits. Cutting a title one
    # character over the line turned "The Wrong Question New Life Insurance
    # Agents Ask" into "...Insurance Agents" - a fragment that means something
    # else. A whole headline slightly over always beats that.
    if len(chosen) <= limit * HEADLINE_OVERFLOW_RATIO:
        return chosen

    trimmed = _shorten(title.text, limit)

    # A complete headline reads better than a truncated one even when it's a
    # little shorter - "Why Your Sales Stalled After You Paused" beats "Here Is
    # Exactly Why Your Sales Stalled After". But a drastically shorter option
    # wastes the canvas, so it has to be within reach of the trimmed length.
    fitting = [
        _strip_trailing_punct(h.text) for h in copy.headline_options
        if len(_strip_trailing_punct(h.text)) <= limit
    ]
    if fitting:
        best = max(fitting, key=len)
        if len(best) >= len(trimmed) * WHOLE_HEADLINE_RATIO:
            return best

    return trimmed


def _shorten(text: str, limit: int) -> str:
    """Cut at the last natural break that fits the character limit."""
    text = _strip_trailing_punct(text)

    # A parenthetical or trailing clause is the first thing to go.
    for separator in ("(", " — ", " – ", ": ", " - "):
        head = _strip_trailing_punct(text.split(separator, 1)[0])
        if head and len(head) <= limit:
            return head

    words = text.split()
    while words and len(" ".join(words)) > limit:
        words.pop()
    # Don't end on a word that leaves the line dangling.
    while len(words) > 3 and words[-1].lower() in _STOPWORDS:
        words.pop()
    return _strip_trailing_punct(" ".join(words))


def plan_cover(copy: CopyPackage, title: Headline, config: Config) -> CoverPlan:
    """Choose the kicker + headline pair for the cover image.

    The kicker must come from a *different* headline than the big line, so the
    cover never says the same thing twice.
    """
    # Sense first. A kicker that reads as a phrase but shares a word with the
    # headline is a small blemish; one that stops mid-thought is the bug being
    # fixed here, so anything that doesn't stand up is out before ranking.
    written = _strip_label_punct(copy.cover_kicker)
    if written and reads_as_a_label(written) and not _restates(written, title, copy):
        return CoverPlan(
            kicker=written.upper(),
            headline=cover_headline(copy, title, config).upper(),
            source_note="kicker written for this post",
        )

    candidates = [c for c in kicker_candidates(copy, config) if reads_as_a_label(c)]
    distinct = [c for c in candidates if not _echoes(c, title, copy)]
    kicker = next(iter(distinct), "") or next(iter(candidates), "")

    if not kicker:
        kicker = _fallback_kicker(copy, config)
        note = "no headline gave a usable kicker - worth a manual look"
    elif kicker in distinct:
        note = "kicker from a different headline option"
    else:
        note = "kicker overlaps the cover headline - worth a manual look"

    return CoverPlan(
        kicker=kicker.upper(),
        headline=cover_headline(copy, title, config).upper(),
        source_note=note,
    )


def _restates(candidate: str, title: Headline, copy: CopyPackage) -> bool:
    """True if the kicker just repeats the opening of what is already on the page."""
    normalized = _normalize(candidate)
    if not normalized:
        return True
    return (
        _normalize(copy.article_h1).startswith(normalized)
        or _normalize(title.text).startswith(normalized)
    )


def _echoes(candidate: str, title: Headline, copy: CopyPackage) -> bool:
    """True if this kicker would just restate what is already on the page."""
    # The subset test is a *preference*, not a veto - it is why the written
    # kicker is decided before this runs. A sensible kicker sharing two words
    # with the headline beats a fragment that shares none.
    return _restates(candidate, title, copy) or _tokens(candidate) <= _tokens(title.text)


# Where a headline stops naming its subject and starts saying something about
# it. Cutting here keeps a phrase whole; cutting at a word count does not.
_CLAUSE_BOUNDARIES = frozenset(
    """that which who whom whose when while after before because since so and but
    or is are was were will can could should would has have had does do did""".split()
)


# The subset of those that mean a clause was started. A kicker containing one
# is a sentence with its ending removed, however complete it looks. `and`/`or`/
# `but` are excluded: they join list items rather than open a clause.
_FRAGMENT_MARKERS = frozenset()  # filled in below, once both sets exist


def _bare(word: str) -> str:
    return re.sub(r"[^a-z]", "", word.lower())


_FRAGMENT_MARKERS = _CLAUSE_BOUNDARIES - {"and", "or", "but"}


def label_from(text: str, config: Config) -> str:
    """Reduce a headline to the phrase naming its subject, or "" if it can't.

    A headline is a sentence; a kicker is a label. Drop the interrogative
    opening, then cut where the sentence starts commenting rather than naming:
    "Why Your Life Insurance Intro Script Is Costing You Deals" -> "Life
    Insurance Intro Script".

    Never cuts to a word count. That is what produced "THE INTRO SCRIPT THAT
    DECIDES" and "NEW AGENTS STALL AFTER THIRTY" - phrases chopped one word
    before the word that finished them. Something too long to cut cleanly
    returns nothing at all, and the caller uses the next candidate.
    """
    # One sentence only. A headline like "Aged or Fresh Leads? Answer One
    # Question First" is two, and any phrase spanning the break reads as the
    # tail of one stapled to the head of another.
    text = re.split(r"[?!.](?:\s|$)", text, maxsplit=1)[0]

    words = _strip_label_punct(text).split()
    while words and _bare(words[0]) in (_CLAUSE_OPENERS | _STOPWORDS):
        words.pop(0)

    for index, word in enumerate(words):
        # Never cut so early that nothing is left to name.
        if index >= 2 and _bare(word) in _CLAUSE_BOUNDARIES:
            words = words[:index]
            break

    while len(words) > 2 and _bare(words[-1]) in _STOPWORDS:
        words.pop()

    limit = config.cover.kicker_max_words
    if len(words) <= limit:
        return " ".join(words)

    # No clause break to cut at, and too long to use whole - "Sell First As A
    # New Life Insurance Agent". The subject is at the end of a sentence like
    # this, so take the tail and drop whatever article it starts on.
    tail = words[-limit:]
    while len(tail) > 2 and _bare(tail[0]) in (_CLAUSE_OPENERS | _STOPWORDS):
        tail.pop(0)
    return " ".join(tail)


def _fallback_kicker(copy: CopyPackage, config: Config) -> str:
    """Last resort, when every other candidate was rejected.

    The brand name is the only thing guaranteed to read correctly, so it is
    what a post falls back to rather than a fragment of its own H1.
    """
    for source in (copy.cover_kicker, copy.article_h1):
        if not source:
            continue
        label = label_from(source, config)
        if reads_as_a_label(label):
            return label
    return "AGENT LEAD LAB"
