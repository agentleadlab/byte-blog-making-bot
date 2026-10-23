"""RYTE's memory, backed up every night - "yes make it back up every night
to icloud".

The same three things `scripts/pack-state.sh` packs - state/, corpus/ and the
saved bodies of posts still to publish - in the same layout, so `@RYTE
restore state` takes a nightly backup as it takes a hand-made one. Into
iCloud Drive's "Ryte Backups" folder on the Mac, so it leaves the machine by
itself; the last `KEEP` nights kept, older ones deleted.
"""

from __future__ import annotations

import os
import tarfile
from datetime import date
from pathlib import Path

from .config import REPO_ROOT

#: How many nightly backups are kept.
KEEP = 14
#: iCloud Drive on a Mac.
ICLOUD = Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
FOLDER = "Ryte Backups"
NAME = "ryte-state-{day}.tar.gz"
_SKIP = (".tmp", ".DS_Store")


def where() -> Path | None:
    """The backup folder: RYTE_BACKUP_DIR when set, else iCloud Drive's
    "Ryte Backups" - None when neither is there (Railway, a Mac without
    iCloud Drive)."""
    override = (os.getenv("RYTE_BACKUP_DIR") or "").strip()
    if override:
        return Path(override).expanduser()
    return ICLOUD / FOLDER if ICLOUD.is_dir() else None


def name_for(day: date) -> str:
    return NAME.format(day=day.isoformat())


def done_for(folder: Path | None, day: date) -> bool:
    return bool(folder) and (folder / name_for(day)).is_file()


def pack(folder: Path, day: date, *, state_dir: Path | None = None, corpus_dir: Path | None = None,
         out_dir: Path | None = None) -> Path:
    """Tonight's backup into `folder`, the oldest past `KEEP` deleted. The
    backup's path. Written beside itself first, so a half-written one never
    sits there looking whole."""
    from .corpus import corpus_dir as _corpus
    from .state import _state_dir

    state_dir = state_dir or _state_dir()
    corpus_dir = corpus_dir or _corpus()
    out_dir = out_dir or REPO_ROOT / "out"
    folder.mkdir(parents=True, exist_ok=True)
    final = folder / name_for(day)
    partial = folder / (final.name + ".part")

    def add(tar, root: Path, as_name: str) -> None:
        if not root.is_dir():
            return
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.name.endswith(_SKIP) or path.name == "update-blocked":
                continue
            try:
                tar.add(str(path), arcname=f"{as_name}/{path.relative_to(root).as_posix()}")
            except OSError:
                # A file being rewritten this second: tomorrow's has it.
                continue

    with tarfile.open(partial, "w:gz") as tar:
        add(tar, state_dir, "state")
        add(tar, corpus_dir, "corpus")
        if out_dir.is_dir():
            for one in sorted(out_dir.glob("*/ghl-payload.json")):
                tar.add(str(one), arcname=f"out/{one.parent.name}/{one.name}")
    partial.replace(final)
    prune(folder)
    return final


def prune(folder: Path, keep: int = KEEP) -> list[Path]:
    """The backups past the newest `keep`, deleted. Only Ryte's own."""
    mine = sorted(folder.glob(NAME.format(day="*")))
    gone = mine[:-keep] if keep and len(mine) > keep else []
    for one in gone:
        try:
            one.unlink()
        except OSError:
            pass
    return gone
