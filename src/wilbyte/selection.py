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


def _normalize(text: str) -> str:
    """Lowercase, punctuation-free form used for prefix comparisons."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9$ ]+", "", text.lower())).strip()


def kicker_candidates(copy: CopyPackage, config: Config) -> list[str]:
    """Short fragments usable as the highlighted line on the cover image.

    Preference order:
      1. The segment before a colon/dash in a headline ("Aged, Fresh, Premium:").
      2. The leading N words of a headline.
    """
    lo, hi = config.cover.kicker_min_words, config.cover.kicker_max_words
    candidates: list[str] = []

    for headline in copy.headline_options:
        text = headline.text
        head = re.split(r"[:—–]| - ", text, maxsplit=1)[0]
        head = _strip_trailing_punct(head)
        if lo <= len(head.split()) <= hi:
            candidates.append(head)

    for headline in copy.headline_options:
        words = _strip_trailing_punct(headline.text).split()
        for size in range(hi, lo - 1, -1):
            if len(words) >= size:
                candidates.append(" ".join(words[:size]))
                break

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

    if len(chosen) <= limit:
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
    candidates = kicker_candidates(copy, config)
    title_tokens = _tokens(title.text)
    h1_normalized = _normalize(copy.article_h1)

    kicker = ""
    for candidate in candidates:
        normalized = _normalize(candidate)
        # Reject a kicker that just restates the opening of the article H1 - the
        # cover would then say the same thing the page already says.
        if h1_normalized.startswith(normalized):
            continue
        # Reject a kicker that is contained in the big headline below it.
        if _normalize(title.text).startswith(normalized):
            continue
        if _tokens(candidate) <= title_tokens:
            continue
        kicker = candidate
        break

    note = "kicker from a different headline option"
    if not kicker:
        kicker = candidates[0] if candidates else _fallback_kicker(copy, config)
        note = "kicker overlaps the cover headline - worth a manual look"

    return CoverPlan(
        kicker=kicker.upper(),
        headline=cover_headline(copy, title, config).upper(),
        source_note=note,
    )


def _fallback_kicker(copy: CopyPackage, config: Config) -> str:
    """Last resort: the opening words of the article H1."""
    words = _strip_trailing_punct(copy.article_h1).split()
    return " ".join(words[: config.cover.kicker_max_words]) or "AGENT LEAD LAB"
