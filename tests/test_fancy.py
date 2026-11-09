"""Channel names in the team's style - "✉️│ᴀᴅᴍɪɴ-ᴀɢᴇɴᴛʟᴇᴀᴅʟᴀʙ-ᴄᴏᴍ"."""

import asyncio
from types import SimpleNamespace

from wilbyte import clearout, fancy
from wilbyte.bot import client
from wilbyte.bot.mentions import parse


def test_the_style():
    assert fancy.styled("🎀|email-update") == "🎀|ᴇᴍᴀɪʟ-ᴜᴘᴅᴀᴛᴇ"
    assert fancy.styled("🏧|budgets_review_channel") == "🏧|ʙᴜᴅɢᴇᴛꜱ_ʀᴇᴠɪᴇᴡ_ᴄʜᴀɴɴᴇʟ"
    assert fancy.styled("🎀email-update") == "🎀│ᴇᴍᴀɪʟ-ᴜᴘᴅᴀᴛᴇ"
    assert fancy.is_styled("✉️|ᴀᴅᴍɪɴ-ᴀɢᴇɴᴛʟᴇᴀᴅʟᴀʙ-ᴄᴏᴍ")
    assert not fancy.is_styled("🎀|email-update")
    assert fancy.plain("✉️|ᴀᴅᴍɪɴ-ᴀɢᴇɴᴛʟᴇᴀᴅʟᴀʙ-ᴄᴏᴍ") == "✉️|admin-agentleadlab-com"


def test_ryte_still_finds_its_channels_once_styled():
    for name in ("rook-qc", "rook-nova", "launching", "new-payment", "agent-setup"):
        assert client._plain_letters(fancy.styled("📋│" + name)) == client._plain_letters(name)
    assert clearout.tidy(fancy.styled("jay-rodriguez-iul")) == "jayrodrigueziul"
    assert clearout.words_of(fancy.styled("🔥│dylan_rankin-vet")) == ["dylan", "rankin"]


def test_asked_for():
    assert parse("<@1> can ryte change discord channel names?").action == "stylechannels"
    assert parse("<@1> rename the channels in Gabi & Ryte").action == "stylechannels"
    assert parse("<@1> clearout Jay Rodriguez").action == "clearout"


class _Channel:
    def __init__(self, name, category=None):
        self.name, self.category, self.position, self.renamed = name, category, 0, None

    def permissions_for(self, _me):
        return SimpleNamespace(manage_channels=True)

    async def edit(self, *, name, reason):
        self.renamed = name


def _run(guild, asked="rename channels", confirmed=True, monkeypatch=None):
    sent = []

    class Responder:
        requester_id = 1

        async def send(self, text="", **_kw):
            sent.append(text)

    class View:
        def __init__(self, **_kw):
            self.confirmed = confirmed

        async def wait(self):
            return None

    monkeypatch.setattr(client.views, "ConfirmView", View)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(client.asyncio, "sleep", lambda _s: real_sleep(0))
    config = SimpleNamespace(secrets=SimpleNamespace(discord_clients_guild_id="999"),
                             discord=SimpleNamespace(approval_timeout_seconds=1))
    message = SimpleNamespace(guild=guild, channel_mentions=[], author=SimpleNamespace(display_name="Franklin"))
    asyncio.run(client._style_channels(Responder(), config, message, asked))
    return sent


def test_the_server_s_channels_are_shown_then_renamed(monkeypatch):
    gabi = SimpleNamespace(name="Mindful Vitality")
    channels = [_Channel("🎀|email-update"), _Channel("✉️|ᴀᴅᴍɪɴ-ᴄᴏᴍ"), _Channel("🌿|notes", gabi)]
    guild = SimpleNamespace(id=1, name="The Goat", channels=channels, categories=[], me=object())
    sent = _run(guild, monkeypatch=monkeypatch)
    assert "#🎀|email-update → #🎀|ᴇᴍᴀɪʟ-ᴜᴘᴅᴀᴛᴇ" in "\n".join(sent)
    assert channels[0].renamed == "🎀|ᴇᴍᴀɪʟ-ᴜᴘᴅᴀᴛᴇ"
    assert channels[1].renamed is None, "already styled"
    assert channels[2].renamed is None, "Gabi's - do not touch it"
    assert "Renamed **1** of 1" in sent[-1]


def test_never_the_clients_server(monkeypatch):
    channels = [_Channel("jay-rodriguez-iul")]
    guild = SimpleNamespace(id=999, name="Clients", channels=channels, categories=[], me=object())
    sent = _run(guild, monkeypatch=monkeypatch)
    assert channels[0].renamed is None and "clients server" in sent[-1]


def test_a_chargeback_goes_to_dispute_management_not_the_board(monkeypatch):
    # "wrong channel now dont we" - three chargebacks in #trello-manager.
    trello = SimpleNamespace(id=1, name="👩‍💼│ᴛʀᴇʟʟᴏ-ᴍᴀɴᴀɢᴇʀ")
    disputes = SimpleNamespace(id=2, name="📋│ᴅɪꜱᴘᴜᴛᴇ-ᴍᴀɴᴀɢᴇᴍᴇɴᴛ")
    goat = SimpleNamespace(id=10, name="The Goat & The Best", text_channels=[trello, disputes])
    config = SimpleNamespace(secrets=SimpleNamespace(discord_chargeback_channel_id=None,
                                                     discord_dispute_channel_id="99",
                                                     discord_talk_guild_id=""))
    bot = SimpleNamespace(config=config, guilds=[goat], get_channel=lambda _id: None)
    monkeypatch.setattr(client, "_board_responder", lambda _bot: "board")
    assert client._chargeback_responder(bot).channel is disputes


def test_ayoko_nga_once_ever_then_the_job(tmp_path, monkeypatch):
    said = []

    class Message:
        channel = SimpleNamespace(name="👩‍💼│ᴛʀᴇʟʟᴏ-ᴍᴀɴᴀɢᴇʀ")
        author = SimpleNamespace(bot=False, display_name="Kharyl")

        async def reply(self, text, **_kw):
            said.append(text)

    real_sleep = asyncio.sleep
    monkeypatch.setattr(client.asyncio, "sleep", lambda _s: real_sleep(0))
    monkeypatch.setattr(client, "_ayoko_nga_path", lambda: tmp_path / "ayoko.json")
    asyncio.run(client._ayoko_nga(Message()))
    asyncio.run(client._ayoko_nga(Message()))
    assert said == ["ayoko nga"]
    other = Message()
    other.channel = SimpleNamespace(name="general")
    (tmp_path / "ayoko.json").unlink()
    asyncio.run(client._ayoko_nga(other))
    assert said == ["ayoko nga"], "only in #trello-manager"
