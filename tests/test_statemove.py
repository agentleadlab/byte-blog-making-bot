"""Moving RYTE's memory from the Mac to the new host."""

from __future__ import annotations

import io
import tarfile

import pytest

from wilbyte import statemove


def _tar(files: dict) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as box:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            box.addfile(info, io.BytesIO(data))
    return out.getvalue()


def test_what_the_mac_packs_is_what_the_new_host_restores(tmp_path):
    mac_state, mac_corpus = tmp_path / "mac" / "state", tmp_path / "mac" / "corpus"
    (mac_state / "sub").mkdir(parents=True)
    mac_corpus.mkdir(parents=True)
    (mac_state / "ledger.json").write_text('{"posted": ["abc"]}')
    (mac_state / "sub" / "playbook.md").write_text("Faith says hi")
    (mac_state / "half.tmp").write_text("mid-write")
    (mac_corpus / "copy.json").write_text("[]")

    found = statemove.read(statemove.pack(mac_state, mac_corpus))
    assert set(found.files) == {"state/ledger.json", "state/sub/playbook.md", "corpus/copy.json"}

    new_state, new_corpus = tmp_path / "data" / "state", tmp_path / "data" / "corpus"
    new_state.mkdir(parents=True)
    (new_state / "ledger.json").write_text('{"posted": []}')
    kept = statemove.restore(found, state_dir=new_state, corpus_dir=new_corpus,
                             backup_dir=tmp_path / "data" / "backups")
    assert (new_state / "ledger.json").read_text() == '{"posted": ["abc"]}'
    assert (new_state / "sub" / "playbook.md").read_text() == "Faith says hi"
    assert (new_corpus / "copy.json").read_text() == "[]"
    # What was there before is kept, not lost.
    assert statemove.read(kept.read_bytes()).files["state/ledger.json"] == b'{"posted": []}'


@pytest.mark.parametrize("name", [
    "/etc/passwd", "../outside.json", "state/../../outside.json", ".env", "src/wilbyte/hub.py", "state",
])
def test_nothing_but_state_and_corpus_files_comes_out(name):
    found = statemove.read(_tar({name: b"x", "state/ok.json": b"{}"}))
    assert list(found.files) == ["state/ok.json"]
    assert name in found.refused


def test_a_file_that_isnt_an_archive_is_said():
    with pytest.raises(ValueError, match="isn't a .tar.gz or .zip"):
        statemove.read(b"just some text")


@pytest.mark.parametrize("said", ["<@1> restore state", "<@1> restore your memory", "<@1> Restore state."])
def test_restore_state_is_a_command(said):
    from wilbyte.bot import mentions

    assert mentions.parse(said).action == "restorestate"


def test_only_franklin_can_replace_the_memory():
    import asyncio
    from types import SimpleNamespace

    from wilbyte.bot import client

    sent = []

    class Responder:
        requester_id = 2

        async def send(self, text=None, **kw):
            sent.append(text)

    config = SimpleNamespace(secrets=SimpleNamespace(discord_notify_user_id="1"))
    message = SimpleNamespace(author=SimpleNamespace(id=2), attachments=[])
    asyncio.run(client._restore_state(None, Responder(), config, message))
    assert sent == ["Only Franklin can replace my memory."]


def test_posts_still_to_publish_come_across_and_nothing_else_from_out(tmp_path):
    """A post scheduled on the Mac publishes from Railway: its saved body is
    carried, and nothing else of out/. The Mac's "update blocked" isn't."""
    data = _tar({"state/ledger.json": b"{}", "state/update-blocked": b"x",
                 "out/my-post/ghl-payload.json": b'{"a": 1}', "out/my-post/post.html": b"<p>",
                 "out/copy/x.txt": b"y", "out/deep/er/ghl-payload.json": b"{}"})
    found = statemove.read(data)
    assert set(found.files) == {"state/ledger.json", "state/update-blocked", "out/my-post/ghl-payload.json"}
    assert found.count("out") == 1
    state, corpus, out = tmp_path / "state", tmp_path / "corpus", tmp_path / "out"
    statemove.restore(found, state_dir=state, corpus_dir=corpus, backup_dir=tmp_path / "b", out_dir=out)
    assert (out / "my-post" / "ghl-payload.json").read_text() == '{"a": 1}'
    assert not (state / "update-blocked").exists()
    # And with nowhere for out/ to go, it's left out rather than written anywhere.
    statemove.restore(found, state_dir=state, corpus_dir=corpus, backup_dir=tmp_path / "b")
