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


class ApprovalView(discord.ui.View):
    """Approve / save as draft / skip / stop, restricted to the requester.

    Nothing reaches GHL until one of these is clicked, so a bad headline or a
    wrong slug is caught here rather than in the blog.
    """

    def __init__(self, *, requester_id: int, timeout: float):
        super().__init__(timeout=timeout)
        self.requester_id = requester_id
        self.decision: Decision = Decision.TIMEOUT
        self.decided_by: str | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
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
