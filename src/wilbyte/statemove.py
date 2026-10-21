"""Moving RYTE's memory from one machine to another - the Mac to Railway.

"can we have ryte where it doesnt rely on my mac being open". The code moves
by itself, from GitHub. What RYTE remembers doesn't: which videos are posted,
Faith's lessons and playbook, the Payra history the API no longer hands back,
the keep list, what's been said and checked. That lives in `state/` (and the
copy library in `corpus/`) on whichever machine has been running it, and a
new one starting empty would repost, re-ask and forget.

`scripts/pack-state.sh` packs those two folders on the Mac. `@RYTE restore
state` with the file attached unpacks them where this copy keeps its own -
only into those two folders, never anywhere else, and with what was there
kept aside first.
"""

from __future__ import annotations

import io
import tarfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

#: The folders an archive may write into, by their name in it.
ROOTS = ("state", "corpus", "out")
#: Of out/, only the saved bodies of posts still to publish - a post
#: scheduled before the move publishes from Railway as it would have from the
#: Mac. Nothing else from out/ is memory.
PAYLOAD = "ghl-payload.json"

#: Bigger than any real memory - Faith's texts are the bulk, a few MB.
MOST_BYTES = 200 * 1024 * 1024


@dataclass
class Packed:
    """What an archive holds, once it's been looked at."""

    files: dict = field(default_factory=dict)   # "state/ledger.json" -> bytes
    refused: list = field(default_factory=list)

    def count(self, root: str) -> int:
        return sum(1 for name in self.files if name.startswith(root + "/"))

    @property
    def size(self) -> int:
        return sum(len(one) for one in self.files.values())


def _safe(name: str) -> str | None:
    """The member's path if it's a plain file under state/ or corpus/, else
    None. Never absolute, never `..`, never anything else in the archive."""
    path = PurePosixPath(str(name or "").replace("\\", "/"))
    parts = [part for part in path.parts if part not in ("", ".")]
    if not parts or path.is_absolute() or ".." in parts or parts[0] not in ROOTS or len(parts) < 2:
        return None
    if parts[0] == "out" and (len(parts) != 3 or parts[-1] != PAYLOAD):
        return None
    if parts[-1].endswith(".tmp") or parts[-1].startswith("._") or parts[-1] == ".DS_Store":
        return None
    return "/".join(parts)


def read(data: bytes) -> Packed:
    """Look inside a .tar.gz or .zip without writing anything."""
    found = Packed()
    total = 0
    try:
        if zipfile.is_zipfile(io.BytesIO(data)):
            with zipfile.ZipFile(io.BytesIO(data)) as box:
                for info in box.infolist():
                    if info.is_dir():
                        continue
                    name = _safe(info.filename)
                    if name is None:
                        found.refused.append(info.filename)
                        continue
                    total += info.file_size
                    if total > MOST_BYTES:
                        raise ValueError("bigger than RYTE's memory could be")
                    found.files[name] = box.read(info)
        else:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as box:
                for member in box.getmembers():
                    if member.isdir():
                        continue
                    name = _safe(member.name)
                    if name is None or not member.isfile():
                        found.refused.append(member.name)
                        continue
                    total += member.size
                    if total > MOST_BYTES:
                        raise ValueError("bigger than RYTE's memory could be")
                    found.files[name] = box.extractfile(member).read()
    except (tarfile.TarError, zipfile.BadZipFile, OSError) as exc:
        raise ValueError(f"that isn't a .tar.gz or .zip I can open ({type(exc).__name__})") from None
    return found


def pack(state_dir: Path, corpus_dir: Path, out_dir: Path | None = None) -> bytes:
    """This copy's memory as a .tar.gz, the same shape `read` takes."""
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as box:
        for root, where in (("state", state_dir), ("corpus", corpus_dir), ("out", out_dir)):
            if where is None or not where.is_dir():
                continue
            for path in sorted(where.rglob("*")):
                if path.is_file() and _safe(f"{root}/{path.relative_to(where).as_posix()}"):
                    box.add(path, arcname=f"{root}/{path.relative_to(where).as_posix()}")
    return out.getvalue()


def restore(found: Packed, *, state_dir: Path, corpus_dir: Path, backup_dir: Path,
            out_dir: Path | None = None) -> Path:
    """Write the archive's files over this copy's, after keeping what was
    there. Returns where the old memory was kept."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    kept = backup_dir / f"ryte-state-before-restore-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz"
    kept.write_bytes(pack(state_dir, corpus_dir, out_dir))
    folders = {"state": state_dir, "corpus": corpus_dir, "out": out_dir}
    for name, data in found.files.items():
        root, rest = name.split("/", 1)
        # A marker that only ever meant something on the machine it was made
        # on: "update blocked" brought across says so on every start.
        if root == "state" and rest == "update-blocked":
            continue
        if folders.get(root) is None:
            continue
        where = folders[root] / rest
        # Resolved and checked again: nothing lands outside its folder.
        base = folders[root].resolve()
        if base not in where.resolve().parents:
            continue
        where.parent.mkdir(parents=True, exist_ok=True)
        spare = where.with_name(where.name + ".restoring")
        spare.write_bytes(data)
        spare.replace(where)
    return kept
