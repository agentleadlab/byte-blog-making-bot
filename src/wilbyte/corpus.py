"""RYTE's memory: every piece of copy Agent Lead Lab has written.

Feed it via Discord attachments, pasted text, or a `corpus/` folder in the repo.
Each piece is stored with a format label so retrieval can say "show me how we
write SMS" rather than mixing a text message in with a blog post.

Storage is a JSONL ledger - append-only, human-readable, greppable, and it
diffs cleanly. Copy is small; there is no need for a database here.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import REPO_ROOT
from .formats import guess_label

# Pieces shorter than this are almost always headers or stray lines.
MIN_PIECE_CHARS = 20
# A single piece longer than this is truncated when shown to the model.
MAX_EXAMPLE_CHARS = 2400

_SPLIT_RE = re.compile(r"^\s*(?:-{3,}|={3,}|\*{3,})\s*$", re.MULTILINE)
_WORD_RE = re.compile(r"[a-z0-9$']+")

# Words too common in this niche to help rank one piece above another.
_STOPWORDS = frozenset(
    """a an and are as at be been but by can did do does for from get got had has
    have how i if in into is it its just like me more my no not of on or our out
    so than that the their them then there these they this to up us was we were
    what when where which who why will with you your""".split()
)


def corpus_dir() -> Path:
    """Where the corpus lives - the persistent volume in a container."""
    override = os.getenv("WILBYTE_CORPUS_DIR")
    if override:
        return Path(override)
    state = os.getenv("WILBYTE_STATE_DIR")
    if state:
        return Path(state) / "corpus"
    return REPO_ROOT / "corpus"


@dataclass
class Piece:
    """One piece of past copy."""

    id: str
    label: str  # sms | email | ad | blog | ...
    text: str
    title: str = ""
    source: str = ""  # filename or "pasted"
    tags: list[str] = field(default_factory=list)
    added_at: str = ""
    added_by: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def preview(self) -> str:
        flat = " ".join(self.text.split())
        return flat if len(flat) <= 100 else flat[:99] + "…"

    def for_prompt(self) -> str:
        text = self.text.strip()
        if len(text) > MAX_EXAMPLE_CHARS:
            text = text[:MAX_EXAMPLE_CHARS].rstrip() + "\n[…trimmed]"
        header = f"[{self.label}]"
        if self.title:
            header += f" {self.title}"
        return f"{header}\n{text}"


class CorpusError(RuntimeError):
    """Raised when copy can't be ingested."""


# --------------------------------------------------------------------- storage


class Corpus:
    def __init__(self, directory: Path | None = None):
        self.dir = directory or corpus_dir()
        self.path = self.dir / "pieces.jsonl"
        self._pieces: list[Piece] | None = None

    # -- loading ---------------------------------------------------------

    @property
    def pieces(self) -> list[Piece]:
        if self._pieces is None:
            self._pieces = self._load()
        return self._pieces

    def _load(self) -> list[Piece]:
        pieces: list[Piece] = []
        for path in self._jsonl_files():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                    pieces.append(Piece(**raw))
                except (ValueError, TypeError):
                    # One malformed line must not hide the rest of the library.
                    continue
        return pieces

    def _jsonl_files(self) -> list[Path]:
        """The volume's ledger, plus any `corpus/*.jsonl` committed to the repo."""
        paths = []
        if self.path.exists():
            paths.append(self.path)
        repo_corpus = REPO_ROOT / "corpus"
        if repo_corpus.exists() and repo_corpus.resolve() != self.dir.resolve():
            paths.extend(sorted(repo_corpus.glob("*.jsonl")))
        return paths

    def reload(self) -> None:
        self._pieces = None

    # -- writing ---------------------------------------------------------

    def add(self, pieces: list[Piece]) -> list[Piece]:
        """Append pieces, skipping ones already stored. Returns what was new."""
        known = {p.id for p in self.pieces}
        fresh = []
        for piece in pieces:
            if piece.id in known:
                continue
            known.add(piece.id)
            fresh.append(piece)

        if not fresh:
            return []

        self.dir.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            for piece in fresh:
                fh.write(json.dumps(piece.to_dict(), ensure_ascii=False) + "\n")

        if self._pieces is not None:
            self._pieces.extend(fresh)
        return fresh

    # -- reading ---------------------------------------------------------

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for piece in self.pieces:
            counts[piece.label] = counts.get(piece.label, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))

    def total_words(self) -> int:
        return sum(len(p.text.split()) for p in self.pieces)

    def search(
        self,
        brief: str,
        *,
        label: str | None = None,
        limit: int = 8,
        char_budget: int = 14_000,
    ) -> list[Piece]:
        """Pieces most relevant to `brief`, preferring the requested format.

        Scoring is plain word overlap. With a library this size that beats
        embeddings on predictability, costs nothing, and is explainable when a
        result looks odd.
        """
        pool = [p for p in self.pieces if not label or p.label == label]
        if not pool:
            # Nothing in that format yet - fall back to the whole library so the
            # voice still carries, rather than writing from nothing.
            pool = list(self.pieces)
        if not pool:
            return []

        query = _tokens(brief)
        scored = sorted(
            pool, key=lambda p: (-_score(p, query), -len(p.text)),
        )

        chosen: list[Piece] = []
        used = 0
        for piece in scored:
            if len(chosen) >= limit:
                break
            cost = min(len(piece.text), MAX_EXAMPLE_CHARS)
            if chosen and used + cost > char_budget:
                continue
            chosen.append(piece)
            used += cost
        return chosen


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall(text.lower()) if w not in _STOPWORDS and len(w) > 2}


def _score(piece: Piece, query: set[str]) -> float:
    if not query:
        return 0.0
    piece_tokens = _tokens(f"{piece.title} {' '.join(piece.tags)} {piece.text}")
    if not piece_tokens:
        return 0.0
    overlap = len(query & piece_tokens)
    if not overlap:
        return 0.0
    # Normalise by query size so a long piece doesn't win on volume alone.
    return overlap / len(query)


# ------------------------------------------------------------------- ingestion


def make_id(text: str) -> str:
    """Content hash, so re-uploading the same file doesn't duplicate anything."""
    normalized = " ".join(text.split()).lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def build_piece(
    text: str,
    *,
    label: str | None = None,
    title: str = "",
    source: str = "",
    tags: list[str] | None = None,
    added_by: str = "",
) -> Piece | None:
    """Make a Piece, or None if the text is too thin to be worth storing."""
    text = text.strip()
    if len(text) < MIN_PIECE_CHARS:
        return None
    return Piece(
        id=make_id(text),
        label=label or guess_label(text, filename=source),
        text=text,
        title=title.strip()[:120],
        source=source,
        tags=tags or [],
        added_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        added_by=added_by,
    )


def parse_upload(
    data: bytes | str,
    *,
    filename: str,
    label: str | None = None,
    added_by: str = "",
) -> list[Piece]:
    """Turn an uploaded file into pieces.

    Understands .csv, .json/.jsonl, and plain text or markdown - the last of
    which is split on a `---` line so one file can hold many pieces.
    """
    text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data
    suffix = Path(filename).suffix.lower()

    if suffix == ".csv":
        return _parse_csv(text, filename=filename, label=label, added_by=added_by)
    if suffix in (".json", ".jsonl"):
        return _parse_json(text, filename=filename, label=label, added_by=added_by)
    return _parse_text(text, filename=filename, label=label, added_by=added_by)


def _parse_text(text: str, *, filename: str, label: str | None, added_by: str) -> list[Piece]:
    chunks = _SPLIT_RE.split(text)
    if len(chunks) == 1:
        # No explicit separators - try blank-line-separated blocks, but only if
        # that yields several substantial pieces rather than shredding an article.
        blocks = [b for b in re.split(r"\n{3,}", text) if len(b.strip()) >= MIN_PIECE_CHARS]
        if len(blocks) > 2 and all(len(b) < 1200 for b in blocks):
            chunks = blocks

    pieces = []
    for chunk in chunks:
        piece = build_piece(
            chunk, label=label, source=filename, added_by=added_by,
            title=_first_line(chunk),
        )
        if piece:
            pieces.append(piece)
    return pieces


def _parse_csv(text: str, *, filename: str, label: str | None, added_by: str) -> list[Piece]:
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise CorpusError(f"{filename}: could not read a header row.")

    lookup = {name.lower().strip(): name for name in reader.fieldnames}
    body_key = _first_present(lookup, ("body", "copy", "text", "message", "content", "post"))
    if not body_key:
        raise CorpusError(
            f"{filename}: needs a column named body, copy, text, message, or content. "
            f"Found: {', '.join(reader.fieldnames)}"
        )
    label_key = _first_present(lookup, ("format", "type", "channel", "kind"))
    title_key = _first_present(lookup, ("title", "name", "subject", "campaign"))
    tags_key = _first_present(lookup, ("tags", "tag", "topic", "topics"))

    pieces = []
    for row in reader:
        body = (row.get(body_key) or "").strip()
        if not body:
            continue
        row_label = label
        if not row_label and label_key:
            from .formats import find_label

            row_label = find_label(row.get(label_key))
        piece = build_piece(
            body,
            label=row_label,
            title=(row.get(title_key) or "").strip() if title_key else "",
            source=filename,
            tags=_split_tags(row.get(tags_key)) if tags_key else [],
            added_by=added_by,
        )
        if piece:
            pieces.append(piece)
    return pieces


def _parse_json(text: str, *, filename: str, label: str | None, added_by: str) -> list[Piece]:
    records: list[dict] = []
    stripped = text.strip()
    if stripped.startswith("["):
        try:
            loaded = json.loads(stripped)
        except ValueError as exc:
            raise CorpusError(f"{filename}: invalid JSON ({exc}).") from exc
        records = [r for r in loaded if isinstance(r, dict)]
    else:
        for line in stripped.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                records.append(record)

    if not records:
        raise CorpusError(f"{filename}: no JSON objects found.")

    from .formats import find_label

    pieces = []
    for record in records:
        lowered = {k.lower(): v for k, v in record.items()}
        body = next(
            (str(lowered[k]) for k in ("body", "copy", "text", "message", "content") if lowered.get(k)),
            "",
        )
        if not body:
            continue
        raw_label = label or find_label(
            next((str(lowered[k]) for k in ("format", "type", "channel") if lowered.get(k)), "")
        )
        tags = lowered.get("tags") or []
        if isinstance(tags, str):
            tags = _split_tags(tags)
        piece = build_piece(
            body,
            label=raw_label,
            title=str(lowered.get("title") or lowered.get("subject") or ""),
            source=filename,
            tags=[str(t) for t in tags][:10],
            added_by=added_by,
        )
        if piece:
            pieces.append(piece)
    return pieces


def _first_present(lookup: dict[str, str], candidates: tuple[str, ...]) -> str | None:
    for candidate in candidates:
        if candidate in lookup:
            return lookup[candidate]
    return None


def _split_tags(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [t.strip() for t in re.split(r"[,;|]", raw) if t.strip()][:10]


def _first_line(text: str) -> str:
    for line in text.strip().splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:120]
    return ""
