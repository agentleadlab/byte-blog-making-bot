"""A single reply surface for both slash commands and @mentions.

Slash commands reply through `interaction.followup`, mentions reply into the
channel. The run loop shouldn't care which, so it takes a Responder.

Everything that goes out is split to Discord's length limit first. A message
one character over is refused outright, and the exception is not one the
pipeline catches - so RYTE went silent mid-answer, on the one message that had
grown past the limit as commands were added to it.
"""

from __future__ import annotations

import discord

# Discord refuses a message over 2000 characters. Split a little under, so a
# trailing marker or a stray newline can't tip a piece over the edge.
MAX_MESSAGE = 1900


def split_message(content: str, *, limit: int = MAX_MESSAGE) -> list[str]:
    """One message split into pieces Discord will accept.

    Broken on blank lines first, then single lines, so a list of SOPs or a help
    page comes apart between its entries rather than mid-word. A single line
    longer than the limit - a very long URL, say - is cut as a last resort,
    because refusing to send it at all is worse.
    """
    text = content or ""
    if len(text) <= limit:
        return [text]

    pieces: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                pieces.append(current)
                current = ""
            pieces.append(line[:limit])
            line = line[limit:]
        if not current:
            current = line
        elif len(current) + 1 + len(line) <= limit:
            current = f"{current}\n{line}"
        else:
            pieces.append(current)
            current = line
    if current:
        pieces.append(current)
    return pieces


class Responder:
    """Where to send output, and who is allowed to press the buttons."""

    requester_id: int | None
    channel_id: int | None

    async def send(self, content=None, *, embed=None, file=None, view=None):
        """Send one reply, in as many pieces as Discord's limit requires.

        The embed, file and view ride on the last piece so the buttons end up
        under the whole thing rather than halfway through it.
        """
        pieces = split_message(content) if content is not None else [None]
        last = None
        for index, piece in enumerate(pieces):
            final = index == len(pieces) - 1
            last = await self._send_one(
                piece,
                embed=embed if final else None,
                file=file if final else None,
                view=view if final else None,
            )
        return last

    async def _send_one(self, content=None, *, embed=None, file=None, view=None):
        raise NotImplementedError

    @staticmethod
    def _kwargs(content, embed, file, view) -> dict:
        # discord.py treats an explicit None differently from an omitted kwarg
        # in some versions, so only pass what's actually set.
        payload = {}
        if content is not None:
            payload["content"] = content
        if embed is not None:
            payload["embed"] = embed
        if file is not None:
            payload["file"] = file
        if view is not None:
            payload["view"] = view
        return payload


class InteractionResponder(Responder):
    def __init__(self, interaction: discord.Interaction):
        self.interaction = interaction
        self.requester_id = interaction.user.id
        self.channel_id = interaction.channel_id

    async def _send_one(self, content=None, *, embed=None, file=None, view=None):
        return await self.interaction.followup.send(
            **self._kwargs(content, embed, file, view)
        )


class MessageResponder(Responder):
    def __init__(self, message: discord.Message):
        self.message = message
        self.requester_id = message.author.id
        self.channel_id = message.channel.id
        self._replied = False

    async def _send_one(self, content=None, *, embed=None, file=None, view=None):
        payload = self._kwargs(content, embed, file, view)
        # Reply to the mention once so the thread is easy to follow, then keep
        # the rest as plain channel messages to avoid a wall of reply chips.
        if not self._replied:
            self._replied = True
            return await self.message.reply(**payload, mention_author=False)
        return await self.message.channel.send(**payload)


class ChannelResponder(Responder):
    """Talks to a channel directly, with no message to reply to.

    The watcher has no request to answer - a video was announced somewhere else
    and the work happens here - so it needs somewhere to put its output and
    someone to own the approval buttons.
    """

    def __init__(self, channel, *, requester_id: int | None = None):
        self.channel = channel
        # None on purpose: anyone in this channel may approve a post that
        # started from an announcement rather than from a request.
        self.requester_id = requester_id
        self.channel_id = getattr(channel, "id", None)

    async def _send_one(self, content=None, *, embed=None, file=None, view=None):
        return await self.channel.send(**self._kwargs(content, embed, file, view))
