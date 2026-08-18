"""A single reply surface for both slash commands and @mentions.

Slash commands reply through `interaction.followup`, mentions reply into the
channel. The run loop shouldn't care which, so it takes a Responder.
"""

from __future__ import annotations

import discord


class Responder:
    """Where to send output, and who is allowed to press the buttons."""

    requester_id: int | None
    channel_id: int | None

    async def send(self, content=None, *, embed=None, file=None, view=None):
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

    async def send(self, content=None, *, embed=None, file=None, view=None):
        return await self.interaction.followup.send(
            **self._kwargs(content, embed, file, view)
        )


class MessageResponder(Responder):
    def __init__(self, message: discord.Message):
        self.message = message
        self.requester_id = message.author.id
        self.channel_id = message.channel.id
        self._replied = False

    async def send(self, content=None, *, embed=None, file=None, view=None):
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

    async def send(self, content=None, *, embed=None, file=None, view=None):
        return await self.channel.send(**self._kwargs(content, embed, file, view))
