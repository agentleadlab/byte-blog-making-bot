"""The nightly backup - "yes make it back up every night to icloud"."""

from __future__ import annotations

import tarfile
from datetime import date, timedelta

from wilbyte import backup, statemove
from wilbyte.bot import mentions


def _memory(tmp_path):
    state, corpus, out = tmp_path / "state", tmp_path / "corpus", tmp_path / "out"
    (state / "sub").mkdir(parents=True)
    (state / "ordercards.json").write_text("{}")
    (state / "sub" / "x.json").write_text("[]")
    (state / "half.tmp").write_text("no")
    (state / "update-blocked").write_text("no")
    corpus.mkdir()
    (corpus / "post.md").write_text("copy")
    (out / "vid1").mkdir(parents=True)
    (out / "vid1" / "ghl-payload.json").write_text("{}")
    (out / "vid1" / "draft.md").write_text("not memory")
    return state, corpus, out


def test_a_nights_backup_is_restorable_and_only_memory(tmp_path):
    state, corpus, out = _memory(tmp_path)
    folder = tmp_path / "iCloud" / "Ryte Backups"
    made = backup.pack(folder, date(2026, 10, 23), state_dir=state, corpus_dir=corpus, out_dir=out)
    assert made.name == "ryte-state-2026-10-23.tar.gz" and backup.done_for(folder, date(2026, 10, 23))
    with tarfile.open(made) as tar:
        names = set(tar.getnames())
    assert names == {"state/ordercards.json", "state/sub/x.json", "corpus/post.md", "out/vid1/ghl-payload.json"}
    # What `@Ryte restore state` reads, it reads whole.
    found = statemove.read(made.read_bytes())
    assert set(found.files) == names and not list(folder.glob("*.part"))


def test_only_the_last_fourteen_are_kept(tmp_path):
    state, corpus, out = _memory(tmp_path)
    folder = tmp_path / "b"
    folder.mkdir()
    (folder / "something-else.txt").write_text("mine")
    start = date(2026, 10, 1)
    for n in range(16):
        backup.pack(folder, start + timedelta(days=n), state_dir=state, corpus_dir=corpus, out_dir=out)
    kept = sorted(one.name for one in folder.glob("ryte-state-*.tar.gz"))
    assert len(kept) == 14 and kept[0] == "ryte-state-2026-10-03.tar.gz"
    assert (folder / "something-else.txt").exists()


def test_where_it_goes(tmp_path, monkeypatch):
    monkeypatch.setenv("RYTE_BACKUP_DIR", str(tmp_path / "x"))
    assert backup.where() == tmp_path / "x"
    monkeypatch.delenv("RYTE_BACKUP_DIR")
    monkeypatch.setattr(backup, "ICLOUD", tmp_path / "nope")
    assert backup.where() is None
    monkeypatch.setattr(backup, "ICLOUD", tmp_path)
    assert backup.where() == tmp_path / "Ryte Backups"


def test_backup_now_is_asked_for():
    for typed in ("<@1> backup now", "<@1> back up", "<@1> backup"):
        assert mentions.parse(typed).action == "backupnow"
