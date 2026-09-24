"""Approval buttons shown under each post preview."""

from __future__ import annotations

from enum import Enum

import discord


class Decision(str, Enum):
    APPROVE = "approve"
    DRAFT = "draft"
    SKIP = "skip"
    STOP = "stop"
    TIMEOUT = "timeout"


class RecordingPicker(discord.ui.View):
    """Pick which call a link refers to.

    Zoom's API returns a different share token than its website does, so a
    pasted link cannot be resolved to a meeting - not by better matching, not
    at all. Everything else tried here either guessed wrong or asked people to
    leave the conversation. Whoever posted the link knows the call on sight.
    """

    def __init__(self, choices: list[tuple[str, str, str]], *, requester_id: int | None, timeout: float):
        super().__init__(timeout=timeout)
        self.chosen: str | None = None
        self.answered = False
        self.requester_id = requester_id

        # Discord's limits: 25 options, 100 characters of label, 100 of value.
        self._select = discord.ui.Select(
            placeholder="Which call is this?",
            options=[
                discord.SelectOption(label=label[:100], description=note[:100], value=key[:100])
                for label, note, key in choices[:25]
            ],
            min_values=1,
            max_values=1,
        )
        self._select.callback = self._picked
        self.add_item(self._select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.requester_id is None or interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message(
            "Only the person who posted the link can pick the call.", ephemeral=True
        )
        return False

    async def _done(self, interaction: discord.Interaction, note: str) -> None:
        self.answered = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content=note, view=self)
        self.stop()

    async def _picked(self, interaction: discord.Interaction) -> None:
        self.chosen = self._select.values[0]
        await self._done(interaction, "Reading that call…")

    @discord.ui.button(label="None of these", style=discord.ButtonStyle.secondary, emoji="🚫")
    async def none_of_these(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.chosen = None
        await self._done(interaction, "Filing it with the link only.")

    async def on_timeout(self) -> None:
        self.chosen = None
        for child in self.children:
            child.disabled = True


class ApprovalView(discord.ui.View):
    """Approve / save as draft / skip / stop, restricted to the requester.

    Nothing reaches GHL until one of these is clicked, so a bad headline or a
    wrong slug is caught here rather than in the blog.
    """

    def __init__(self, *, requester_id: int | None, timeout: float):
        super().__init__(timeout=timeout)
        self.requester_id = requester_id
        self.decision: Decision = Decision.TIMEOUT
        self.decided_by: str | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # No requester means nobody asked for this run - it started because a
        # video was announced. Locking the buttons to whoever posted the
        # announcement would lock them to a bot, and nobody could approve.
        if self.requester_id is None:
            return True
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the person who started this run can approve it.", ephemeral=True
            )
            return False
        return True

    async def _finish(self, interaction: discord.Interaction, decision: Decision, note: str) -> None:
        self.decision = decision
        self.decided_by = interaction.user.display_name
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content=note, view=self)
        self.stop()

    @discord.ui.button(label="Schedule it", style=discord.ButtonStyle.success, emoji="✅")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._finish(interaction, Decision.APPROVE, "✅ Scheduling…")

    @discord.ui.button(label="Save as draft", style=discord.ButtonStyle.primary, emoji="📝")
    async def draft(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._finish(interaction, Decision.DRAFT, "📝 Saving as a draft…")

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.secondary, emoji="⏭")
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._finish(
            interaction, Decision.SKIP, "⏭ Skipped — nothing sent, and the slot stays open."
        )

    @discord.ui.button(label="Stop the run", style=discord.ButtonStyle.danger, emoji="🛑")
    async def stop_run(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._finish(interaction, Decision.STOP, "🛑 Stopping after this one.")

    async def on_timeout(self) -> None:
        self.decision = Decision.TIMEOUT
        for child in self.children:
            child.disabled = True


class ConfirmView(discord.ui.View):
    """One yes, one no, for a change that writes to something already live.

    Moving a scheduled post or pushing one out early edits the blog itself, so
    the plan is shown first and nothing happens until somebody looks at it and
    presses the button.
    """

    def __init__(
        self,
        *,
        requester_id: int | None,
        timeout: float,
        label: str,
        emoji: str,
        danger: bool = False,
        stoppable: bool = False,
    ):
        super().__init__(timeout=timeout)
        self.requester_id = requester_id
        self.confirmed = False
        #: Whether they asked to stop going down the list. Only offered during
        #: a run: "leave it" means leave this one and go on to the next, and
        #: with a hundred and eighty to go there has to be a way to say enough
        #: that is not walking away from the keyboard.
        self.stopped = False
        if not stoppable:
            self.remove_item(self._stop)
        #: Whether somebody actually pressed one of the two. A list that timed
        #: out is a list nobody saw, and that is worth saying; "leave it" has
        #: already been answered and is not.
        self.answered = False
        self._go.label = label
        self._go.emoji = emoji
        # Red for the ones that cannot be undone. Everything else RYTE asks
        # about can be put back, and a button that bans somebody and deletes
        # their channel should not look like the one that moves a blog post.
        if danger:
            self._go.style = discord.ButtonStyle.danger

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.requester_id is None or interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message(
            "Only the person who asked for this can confirm it.", ephemeral=True
        )
        return False

    @discord.ui.button(label="Do it", style=discord.ButtonStyle.success, emoji="✅")
    async def _go(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        await self._close(interaction, "✅ Working on it…")

    @discord.ui.button(label="Leave it", style=discord.ButtonStyle.secondary, emoji="✖")
    async def _no(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = False
        await self._close(interaction, "✖ Left alone — nothing changed.")

    @discord.ui.button(
        label="Stop the run", style=discord.ButtonStyle.secondary, emoji="🛑"
    )
    async def _stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = False
        self.stopped = True
        await self._close(interaction, "🛑 Stopped — this one is untouched too.")

    async def _close(self, interaction: discord.Interaction, note: str) -> None:
        self.answered = True
        for child in self.children:
            child.disabled = True
        # The answer counts whether or not the message could be redrawn. A
        # press Discord took too long to acknowledge makes this edit fail, and
        # stopping only after it meant whatever was waiting on the press
        # waited twelve hours for a button that had already been pressed.
        try:
            await interaction.response.edit_message(content=note, view=self)
        finally:
            self.stop()

    async def on_timeout(self) -> None:
        self.confirmed = False
        for child in self.children:
            child.disabled = True


class ChannelPicker(discord.ui.View):
    """Pick one of the quiet channels to clear out.

    A dropdown rather than twenty-five buttons: buttons would fill every row
    Discord allows and arrive as a wall of channel names with no room to say
    how long each has been quiet. An option carries that underneath the name.

    Picking one starts a clear-out and nothing more. The two questions that
    follow are the same two as when the name is typed by hand, and the one
    that cannot be undone is still the second of them.
    """

    def __init__(
        self,
        choices: list[tuple[str, str]],
        *,
        requester_id: int | None,
        timeout: float,
    ):
        super().__init__(timeout=timeout)
        self.chosen: str | None = None
        #: Whether they asked to be taken through the whole list rather than
        #: pick one of it - "so i dont have to pick anymore".
        self.run = False
        self.answered = False
        self.requester_id = requester_id

        # Discord's limits: 25 options, 100 characters of label and of
        # description.
        self._select = discord.ui.Select(
            placeholder="Clear one of these out…",
            options=[
                discord.SelectOption(label=name[:100], description=note[:100], value=name[:100])
                for name, note in choices[:25]
            ],
            min_values=1,
            max_values=1,
        )
        self._select.callback = self._picked
        self.add_item(self._select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.requester_id is None or interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message(
            "Only the person who asked for the list can clear one out.",
            ephemeral=True,
        )
        return False

    async def _picked(self, interaction: discord.Interaction) -> None:
        self.chosen = self._select.values[0]
        self.answered = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=f"🧹 **#{self.chosen}** — looking at what to keep…", view=self
        )
        self.stop()

    @discord.ui.button(
        label="Go through them one by one",
        style=discord.ButtonStyle.primary, emoji="🧹",
    )
    async def _run(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.run = True
        self.answered = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content="🧹 Going down the list — the same two questions each time.",
            view=self,
        )
        self.stop()

    async def on_timeout(self) -> None:
        self.chosen = None
        for child in self.children:
            child.disabled = True
