import os
import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from .database import Database
from .embeds import info_embed, success_embed, error_embed

logger = logging.getLogger("masecodes.appeals")


class AppealModal(discord.ui.Modal, title="Submit an Appeal"):
    reason = discord.ui.TextInput(label="Reason for appeal", style=discord.TextStyle.short, max_length=200)
    explanation = discord.ui.TextInput(label="Explanation", style=discord.TextStyle.paragraph, max_length=2000)
    additional = discord.ui.TextInput(label="Additional information (optional)", style=discord.TextStyle.paragraph, required=False)

    def __init__(self, bot, db: Database):
        super().__init__()
        self.bot = bot
        self.db = db

    async def on_submit(self, interaction: discord.Interaction):
        ts = int(datetime.now(timezone.utc).timestamp())
        user = interaction.user
        row = await self.db.execute(
            "INSERT INTO appeals (discord_user_id, username, submitted_at, reason, explanation, additional_info, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user.id, str(user), ts, self.reason.value, self.explanation.value, self.additional.value or "", "pending"),
        )
        # get last id
        cur = await self.db.fetchone("SELECT last_insert_rowid()")
        appeal_id = (await self.db.fetchone("SELECT id FROM appeals ORDER BY id DESC LIMIT 1"))[0]

        # post to moderator channel
        ch_id = os.getenv("APPEALS_CHANNEL_ID")
        if ch_id:
            ch = self.bot.get_channel(int(ch_id))
            if ch:
                e = info_embed("⚖️ New Moderation Appeal", f"Appeal ID: #{appeal_id}\nUser: {user.mention}\nStatus: pending")
                e.add_field(name="Reason", value=self.reason.value[:500], inline=False)
                e.add_field(name="Explanation", value=self.explanation.value[:500], inline=False)
                e.add_field(name="Additional", value=self.additional.value[:300] or "(none)", inline=False)
                # buttons
                view = discord.ui.View()

                async def accept_callback(interaction2: discord.Interaction):
                    if interaction2.guild is None or not (interaction2.user.guild_permissions.manage_guild or interaction2.user.guild_permissions.moderate_members or interaction2.user.guild_permissions.ban_members):
                        await interaction2.response.send_message(embed=error_embed("Unauthorized", "You are not authorized to perform this action."), ephemeral=True)
                        return
                    await self.db.execute("UPDATE appeals SET status = ?, moderator_id = ?, decision = ?, decision_at = ? WHERE id = ?", ("accepted", interaction2.user.id, "accepted", int(datetime.now(timezone.utc).timestamp()), appeal_id))
                    await interaction2.response.edit_message(embed=info_embed("Appeal accepted", f"Appeal #{appeal_id} accepted by {interaction2.user.mention}"), view=None)
                    try:
                        await user.send(embed=success_embed("Appeal accepted", "Your appeal has been accepted. A moderator will follow up."))
                    except Exception:
                        logger.exception("Failed to DM appeal accepted")

                async def deny_callback(interaction2: discord.Interaction):
                    if interaction2.guild is None or not (interaction2.user.guild_permissions.manage_guild or interaction2.user.guild_permissions.moderate_members or interaction2.user.guild_permissions.ban_members):
                        await interaction2.response.send_message(embed=error_embed("Unauthorized", "You are not authorized to perform this action."), ephemeral=True)
                        return
                    await self.db.execute("UPDATE appeals SET status = ?, moderator_id = ?, decision = ?, decision_at = ? WHERE id = ?", ("denied", interaction2.user.id, "denied", int(datetime.now(timezone.utc).timestamp()), appeal_id))
                    await interaction2.response.edit_message(embed=info_embed("Appeal denied", f"Appeal #{appeal_id} denied by {interaction2.user.mention}"), view=None)
                    try:
                        await user.send(embed=info_embed("Appeal denied", "Your appeal has been denied."))
                    except Exception:
                        logger.exception("Failed to DM appeal denied")

                accept = discord.ui.Button(label="Accept Appeal", style=discord.ButtonStyle.success)
                deny = discord.ui.Button(label="Deny Appeal", style=discord.ButtonStyle.danger)
                accept.callback = accept_callback
                deny.callback = deny_callback
                view.add_item(accept)
                view.add_item(deny)
                await ch.send(embed=e, view=view)

        await interaction.response.send_message(embed=success_embed("Appeal Submitted", f"Your appeal has been submitted as #{appeal_id}"))


class AppealsBot(commands.Bot):
    def __init__(self, db: Database, guild_id: int | None = None, **kwargs):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents, **kwargs)
        self.db = db
        self.guild_id = guild_id

    async def setup_hook(self):
        if self.guild_id:
            synced = await self.tree.sync(guild=discord.Object(id=self.guild_id))
            logger.info("Appeals bot synced %d commands to guild %s", len(synced), self.guild_id)
        else:
            synced = await self.tree.sync()
            logger.info("Appeals bot synced %d global commands", len(synced))


def create_appeals_bot(db: Database, guild_id: int | None = None) -> AppealsBot:
    bot = AppealsBot(db, guild_id, application_id=os.getenv("APPEALS_APPLICATION_ID"))

    @bot.tree.command(name="appeal")
    @app_commands.allowed_contexts(
        guilds=True,
        dms=True,
        private_channels=True,
    )
    async def appeal(interaction: discord.Interaction):
        modal = AppealModal(bot, db)
        await interaction.response.send_modal(modal)

    return bot
