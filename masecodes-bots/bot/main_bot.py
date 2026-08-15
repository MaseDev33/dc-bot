import asyncio
import os
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from .database import Database
from .embeds import success_embed, error_embed, info_embed

logger = logging.getLogger("masecodes.main")

PROTECTED_ROLE_ID = 1537861422107852830
OWNER_ID = 1480898643123896480


async def check_protected_target(interaction: discord.Interaction, member: discord.Member | None, action: str) -> bool:
    if member is None:
        return True
    if not any(role.id == PROTECTED_ROLE_ID for role in member.roles):
        return True
    if interaction.user.id == OWNER_ID:
        return True
    await interaction.response.send_message(
        embed=error_embed(
            "Protected user",
            f"<@{member.id}> has the protected role and cannot be {action} unless <@{OWNER_ID}> performs the action.",
        )
    )
    return False


class MainBot(commands.Bot):
    def __init__(self, db: Database, guild_id: Optional[int], **kwargs):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix="m!", intents=intents, **kwargs)
        self.db = db
        self.guild_id = guild_id
        self.session: aiohttp.ClientSession | None = None
        self.added_tasks = False

    async def setup_hook(self):
        # debug: report local command tree size before syncing
        try:
            local_cmds = len(self.tree.get_commands())
        except Exception:
            local_cmds = None
        logger.info("Local command tree size before sync: %s", local_cmds)

        synced = await self.tree.sync()
        logger.info("Synced %d global app commands", len(synced))
        # log bot identity and application id for troubleshooting
        try:
            logger.info("Main bot user: %s (id=%s) application_id=%s", getattr(self.user, 'name', None), getattr(self.user, 'id', None), self.application_id)
        except Exception:
            logger.exception("Could not log bot identity")

        # debug: check guild object and bot member in guild
        try:
            if self.guild_id:
                guild_obj = self.get_guild(int(self.guild_id))
                if guild_obj:
                    logger.info("Found guild locally: %s (id=%s)", guild_obj.name, guild_obj.id)
                    me = guild_obj.get_member(self.user.id)
                    logger.info("Bot member in guild: %s", getattr(me, 'display_name', None))
                else:
                    logger.warning("Guild id %s not found in cache; bot may not be in that guild", self.guild_id)
        except Exception:
            logger.exception("Failed to inspect guild membership")

        if not self.added_tasks:
            self.session = aiohttp.ClientSession()
            self.rss_task.start()
            # start GitHub polling task (interval configurable via GITHUB_POLL_INTERVAL)
            try:
                self.github_task.start()
            except Exception:
                logger.exception("Failed to start github task")
            self.tempban_task.start()
            self.added_tasks = True

    async def close(self):
        if self.session:
            await self.session.close()
        await super().close()

    # --- Background tasks ---
    @tasks.loop(seconds=300)
    async def rss_task(self):
        url = os.getenv("BLOG_RSS_URL")
        if not url:
            return
        try:
            async with self.session.get(url, timeout=30) as r:
                text = await r.text()
            # naive xml parse
            import xml.etree.ElementTree as ET

            root = ET.fromstring(text)
            # support both rss and atom
            items = root.findall(".//item") or root.findall(".//entry")
            for item in items[:5]:
                guid = item.findtext("guid") or item.findtext("id") or item.findtext("link")
                title = item.findtext("title") or "(no title)"
                link = item.findtext("link") or item.findtext("id") or ""
                pub = item.findtext("pubDate") or item.findtext("updated")
                summary = item.findtext("description") or item.findtext("summary") or ""

                exist = await self.db.fetchone("SELECT 1 FROM blog_posts WHERE guid = ?", (guid,))
                if not exist:
                    ts = int(datetime.now(timezone.utc).timestamp())
                    await self.db.execute(
                        "INSERT INTO blog_posts (guid, title, url, published, summary) VALUES (?, ?, ?, ?, ?)",
                        (guid, title, link, ts, summary[:1000]),
                    )
                    # post to blog channel
                    channel_id = os.getenv("BLOG_CHANNEL_ID")
                    ping_target = os.getenv("BLOG_PING_CHANNEL_ID")
                    blog_ch = None
                    if channel_id:
                        blog_ch = self.get_channel(int(channel_id))
                        if blog_ch:
                            e = info_embed(title, summary[:500])
                            e.add_field(name="Link", value=link, inline=False)
                            await blog_ch.send(embed=e)
                    # DM subscribers
                    rows = await self.db.fetchall("SELECT user_id FROM blog_subscribers")
                    for (user_id,) in rows:
                        try:
                            user = await self.fetch_user(user_id)
                            dm = info_embed("New blog post", f"{title}\n{link}")
                            await user.send(embed=dm)
                        except Exception:
                            logger.exception("Failed to send blog DM to %s", user_id)
                    # Handle ping target: can be a channel ID or a role ID (role mention in blog channel)
                    if ping_target:
                        try:
                            # first try as a channel
                            ping_obj = self.get_channel(int(ping_target))
                            if ping_obj:
                                await ping_obj.send(f"New blog post: {title} {link}")
                            else:
                                # treat as role id and mention in the blog channel
                                guild_id = os.getenv("GUILD_ID")
                                if guild_id and blog_ch:
                                    guild = self.get_guild(int(guild_id))
                                    if guild:
                                        role = guild.get_role(int(ping_target))
                                        if role:
                                            await blog_ch.send(f"{role.mention} New blog post: {title} {link}")
                        except Exception:
                            logger.exception("Failed to send blog ping for target %s", ping_target)
        except Exception:
            logger.exception("RSS task failed")

    @tasks.loop(seconds=60)
    async def tempban_task(self):
        rows = await self.db.fetchall("SELECT user_id, expires_at FROM temporary_bans")
        now = int(datetime.now(timezone.utc).timestamp())
        for user_id, expires_at in rows:
            if expires_at and expires_at <= now:
                try:
                    guild_id = os.getenv("GUILD_ID")
                    if guild_id:
                        guild = self.get_guild(int(guild_id))
                        if guild:
                            bans = await guild.bans()
                            # only unban if currently banned
                            for b in bans:
                                if b.user.id == int(user_id):
                                    await guild.unban(b.user, reason="Temporary ban expired")
                                    await self.db.execute("DELETE FROM temporary_bans WHERE user_id = ?", (user_id,))
                                    # log
                                    mod_ch = os.getenv("MOD_LOG_CHANNEL_ID")
                                    if mod_ch:
                                        ch = self.get_channel(int(mod_ch))
                                        if ch:
                                            e = success_embed("Temporary ban expired", f"Unbanned <@{user_id}>")
                                            await ch.send(embed=e)
                except Exception:
                    logger.exception("Failed to process tempban for %s", user_id)

    @tasks.loop(seconds=120)
    async def github_task(self):
        # Poll GitHub for configured repositories or username activity.
        repos = os.getenv("GITHUB_REPOSITORIES", "").strip()
        username = os.getenv("GITHUB_USERNAME", "").strip()
        channel_id = os.getenv("GITHUB_CHANNEL_ID")
        if not channel_id:
            return
        headers = {"Accept": "application/vnd.github.v3+json"}
        token = os.getenv("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"token {token}"

        async def process_events(url):
            try:
                async with self.session.get(url, headers=headers, timeout=30) as r:
                    if r.status != 200:
                        logger.warning("GitHub returned %s for %s", r.status, url)
                        return
                    data = await r.json()
            except Exception:
                logger.exception("Failed to fetch GitHub events from %s", url)
                return

            # events come newest first; process oldest->newest
            for ev in reversed(data):
                ev_id = ev.get("id")
                ev_type = ev.get("type")
                repo_name = ev.get("repo", {}).get("name")
                # dedupe
                exists = await self.db.fetchone("SELECT 1 FROM github_events WHERE event_id = ?", (ev_id,))
                if exists:
                    continue
                # store raw payload (stringified)
                try:
                    import json

                    payload_text = json.dumps(ev.get("payload", {}))
                except Exception:
                    payload_text = ""

                await self.db.execute(
                    "INSERT INTO github_events (repo, event_id, type, payload, timestamp) VALUES (?, ?, ?, ?, ?)",
                    (repo_name, ev_id, ev_type, payload_text, int(datetime.now(timezone.utc).timestamp())),
                )

                # Build embed per event type
                ch = self.get_channel(int(channel_id))
                if not ch:
                    continue

                try:
                    if ev_type == "PushEvent":
                        p = ev.get("payload", {})
                        commits = p.get("commits") or []
                        count = len(commits)
                        actor = ev.get("actor", {}).get("login", "unknown")
                        ref = p.get("ref", "")
                        compare_url = p.get("compare") or ""
                        title = f"🐙 GitHub Push — {repo_name}"

                        if count:
                            desc_lines = [f"{count} commit(s) pushed by {actor}"]
                            for c in commits[:5]:
                                sha = c.get("sha", "")[:7]
                                msg = c.get("message", "").splitlines()[0]
                                url = c.get("url") or c.get("html_url") or ""
                                if url.startswith("api."):
                                    url = url.replace("api.", "").replace("repos/", "")
                                if url:
                                    desc_lines.append(f"• [`{sha}`]({url}) {msg}")
                                else:
                                    desc_lines.append(f"• `{sha}` {msg}")
                            if ref:
                                desc_lines.append(f"Ref: {ref}")
                            embed = info_embed(title, "\n".join(desc_lines))
                            first_url = commits[0].get("url") or commits[0].get("html_url") or ""
                            if first_url.startswith("api."):
                                first_url = first_url.replace("api.", "").replace("repos/", "")
                            if first_url:
                                embed.add_field(name="View", value=f"[Commit Link]({first_url})")
                            if compare_url:
                                embed.add_field(name="Compare", value=f"[GitHub compare]({compare_url})", inline=False)
                        else:
                            desc_lines = [f"A push event was received from {actor}."]
                            if ref:
                                desc_lines.append(f"Ref: {ref}")
                            desc_lines.append("The upstream payload did not include any commit entries.")
                            embed = info_embed(title, "\n".join(desc_lines))
                            if compare_url:
                                embed.add_field(name="Compare", value=f"[GitHub compare]({compare_url})", inline=False)
                        await ch.send(embed=embed)

                    elif ev_type == "PullRequestEvent":
                        p = ev.get("payload", {})
                        action = p.get("action")
                        pr = p.get("pull_request", {})
                        title = f"🔀 Pull Request {action} — {repo_name}"
                        desc = f"#{pr.get('number')} {pr.get('title')}\nAuthor: {pr.get('user', {}).get('login')}"
                        embed = info_embed(title, desc)
                        embed.add_field(name="Link", value=pr.get("html_url", ""), inline=False)
                        await ch.send(embed=embed)

                    elif ev_type == "ReleaseEvent":
                        p = ev.get("payload", {})
                        release = p.get("release", {})
                        title = f"🏷️ Release — {repo_name}"
                        desc = f"{release.get('name') or release.get('tag_name')}\nAuthor: {release.get('author', {}).get('login') }"
                        embed = info_embed(title, desc)
                        embed.add_field(name="Link", value=release.get("html_url", ""), inline=False)
                        await ch.send(embed=embed)

                    else:
                        # Generic event posting for other event types
                        title = f"🔔 {ev_type} — {repo_name}"
                        embed = info_embed(title, str(ev.get("repo", {})))
                        await ch.send(embed=embed)
                except Exception:
                    logger.exception("Failed to post GitHub event %s", ev_id)

        # process repos
        targets = []
        if repos:
            for r in [x.strip() for x in repos.split(",") if x.strip()]:
                # API: /repos/{owner}/{repo}/events
                targets.append(f"https://api.github.com/repos/{r}/events")
        if username:
            targets.append(f"https://api.github.com/users/{username}/events")

        for url in targets:
            await process_events(url)

    # --- Commands ---
    # Note: app commands are registered in `create_main_bot` using `bot.tree.command`.


def create_main_bot(db: Database, guild_id: Optional[int]) -> MainBot:
    bot = MainBot(db, guild_id, application_id=os.getenv("MAIN_APPLICATION_ID"))
    # remove default help to allow a custom m!help command
    try:
        bot.remove_command("help")
    except Exception:
        pass

    @bot.tree.command(name="subscribe-to-blog")
    @app_commands.allowed_contexts(
        guilds=True,
        dms=True,
        private_channels=True,
    )
    async def subscribe(interaction: discord.Interaction):
        user_id = interaction.user.id
        exist = await db.fetchone("SELECT 1 FROM blog_subscribers WHERE user_id = ?", (user_id,))
        if exist:
            e = info_embed("Already subscribed", "You are already subscribed to blog notifications.")
            await interaction.response.send_message(embed=e)
            return
        ts = int(datetime.now(timezone.utc).timestamp())
        await db.execute("INSERT INTO blog_subscribers (user_id, subscribed_at) VALUES (?, ?)", (user_id, ts))
        e = success_embed("Subscribed", "You will receive DMs for new blog posts.")
        await interaction.response.send_message(embed=e)

    @bot.tree.command(name="ping")
    @app_commands.allowed_contexts(
        guilds=True,
        dms=True,
        private_channels=True,
    )
    async def ping_cmd(interaction: discord.Interaction):
        e = info_embed("Pong", f"Latency: {round(bot.latency*1000)}ms")
        await interaction.response.send_message(embed=e)

    @bot.tree.command(name="refresh-commands")
    @app_commands.allowed_contexts(
        guilds=True,
        dms=False,
        private_channels=False,
    )
    @app_commands.describe()
    async def refresh_commands(interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(embed=error_embed("Guild only", "This refresh command can only be used in a server."))
            return
        if not interaction.user.guild_permissions.manage_guild and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(embed=error_embed("Permission denied", "You need Manage Server or Administrator to refresh slash commands."))
            return
        guild = discord.Object(id=interaction.guild.id)
        try:
            await bot.tree.clear_commands(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            await interaction.response.send_message(embed=success_embed("Commands refreshed", f"Synced {len(synced)} slash commands for this guild."))
        except Exception:
            logger.exception("Failed to refresh slash commands")
            await interaction.response.send_message(embed=error_embed("Refresh failed", "The slash command tree could not be refreshed."))

    @bot.tree.command(name="warn")
    @app_commands.allowed_contexts(
        guilds=True,
        dms=False,
        private_channels=False,
    )
    @app_commands.describe(user="User to warn", reason="Reason for the warning")
    async def warn(interaction: discord.Interaction, user: discord.Member, reason: str):
        if interaction.guild is None:
            await interaction.response.send_message(embed=error_embed("Guild only", "This command can only be used in a server."))
            return
        if not interaction.user.guild_permissions.moderate_members:
            await interaction.response.send_message(embed=error_embed("Permission denied", "You lack Moderate Members."))
            return
        # Attempt to DM the user before recording the warning
        dm_sent = False
        try:
            dm = info_embed("You have been warned", f"Server: {interaction.guild.name}\nModerator: {interaction.user.mention}\nReason: {reason}")
            await user.send(embed=dm)
            dm_sent = True
        except Exception:
            logger.exception("Failed to DM warned user")

        ts = int(datetime.now(timezone.utc).timestamp())
        await db.execute("INSERT INTO warnings (user_id, moderator_id, reason, timestamp) VALUES (?, ?, ?, ?)", (user.id, interaction.user.id, reason, ts))
        e = info_embed("🛡️ User Warned", f"User: {user.mention}\nModerator: {interaction.user.mention}\nReason: {reason}")
        if not dm_sent:
            e.add_field(name="Notice", value="Could not DM the user before warning.")
        await interaction.response.send_message(embed=e)
        # mod log
        mod_ch = os.getenv("MOD_LOG_CHANNEL_ID")
        if mod_ch:
            ch = bot.get_channel(int(mod_ch))
            if ch:
                await ch.send(embed=e)

    @bot.tree.command(name="warnings")
    @app_commands.allowed_contexts(
        guilds=True,
        dms=False,
        private_channels=False,
    )
    @app_commands.describe(user="User to view warnings for")
    async def warnings_cmd(interaction: discord.Interaction, user: discord.Member):
        if interaction.guild is None:
            await interaction.response.send_message(embed=error_embed("Guild only", "This command can only be used in a server."))
            return
        if not interaction.user.guild_permissions.moderate_members:
            await interaction.response.send_message(embed=error_embed("Permission denied", "You lack Moderate Members."))
            return
        rows = await db.fetchall("SELECT id, moderator_id, reason, timestamp FROM warnings WHERE user_id = ? ORDER BY timestamp DESC", (user.id,))
        if not rows:
            await interaction.response.send_message(embed=info_embed("Warnings", "No warnings found."))
            return
        desc = "\n\n".join([f"ID: {r[0]} — Moderator: <@{r[1]}> — {r[2]}" for r in rows[:10]])
        e = info_embed(f"Warnings for {user}", desc)
        await interaction.response.send_message(embed=e)

    @bot.tree.command(name="ban")
    @app_commands.allowed_contexts(
        guilds=True,
        dms=False,
        private_channels=False,
    )
    @app_commands.describe(user="User to ban", reason="Reason for ban")
    async def ban_cmd(interaction: discord.Interaction, user: discord.User, reason: str):
        if interaction.guild is None:
            await interaction.response.send_message(embed=error_embed("Guild only", "This command can only be used in a server."))
            return
        if not interaction.user.guild_permissions.ban_members:
            await interaction.response.send_message(embed=error_embed("Permission denied", "You lack Ban Members."))
            return
        guild = interaction.guild
        target_member = guild.get_member(user.id)
        if not await check_protected_target(interaction, target_member, "banned"):
            return
        # DM the user before banning
        dm_sent = False
        try:
            dm = info_embed("You have been banned", f"Server: {interaction.guild.name}\nModerator: {interaction.user.mention}\nReason: {reason}")
            await user.send(embed=dm)
            dm_sent = True
        except Exception:
            logger.exception("Failed to DM user before ban")

        try:
            await guild.ban(user, reason=reason)
            ts = int(datetime.now(timezone.utc).timestamp())
            await db.execute("INSERT INTO moderation_actions (action, user_id, moderator_id, reason, timestamp) VALUES (?, ?, ?, ?, ?)", ("ban", user.id, interaction.user.id, reason, ts))
            e = info_embed("User Banned", f"User: {getattr(user, 'mention', str(user))}\nModerator: {interaction.user.mention}\nReason: {reason}")
            if not dm_sent:
                e.add_field(name="Notice", value="Could not DM the user before banning.")
            await interaction.response.send_message(embed=e)
            mod_ch = os.getenv("MOD_LOG_CHANNEL_ID")
            if mod_ch:
                ch = bot.get_channel(int(mod_ch))
                if ch:
                    await ch.send(embed=e)
        except Exception:
            logger.exception("Ban failed")
            await interaction.response.send_message(embed=error_embed("Ban failed", "Could not ban the user."))

    @bot.tree.command(name="tempban")
    @app_commands.allowed_contexts(
        guilds=True,
        dms=False,
        private_channels=False,
    )
    @app_commands.describe(user="User to tempban", duration="Duration like 3h, 5d", reason="Reason")
    async def tempban_cmd(interaction: discord.Interaction, user: discord.User, duration: str, reason: str):
        if interaction.guild is None:
            await interaction.response.send_message(embed=error_embed("Guild only", "This command can only be used in a server."))
            return
        if not interaction.user.guild_permissions.ban_members:
            await interaction.response.send_message(embed=error_embed("Permission denied", "You lack Ban Members."))
            return
        target_member = interaction.guild.get_member(user.id)
        if not await check_protected_target(interaction, target_member, "tempbanned"):
            return
        # parse duration
        mul = {"s":1, "m":60, "h":3600, "d":86400, "w":604800}
        try:
            unit = duration[-1]
            val = int(duration[:-1])
            secs = val * mul.get(unit, 0)
            if secs <= 0:
                raise ValueError()
        except Exception:
            await interaction.response.send_message(embed=error_embed("Invalid duration", "Use formats like 30s, 5m, 3h, 5d, 1w."))
            return
        expires = int((datetime.now(timezone.utc) + timedelta(seconds=secs)).timestamp())
        # Attempt to DM before tempbanning
        dm_sent = False
        try:
            dm = info_embed("You have been temporarily banned", f"Server: {interaction.guild.name}\nDuration: {duration}\nModerator: {interaction.user.mention}\nReason: {reason}")
            await user.send(embed=dm)
            dm_sent = True
        except Exception:
            logger.exception("Failed to DM user before tempban")

        try:
            await interaction.guild.ban(user, reason=reason)
            ts = int(datetime.now(timezone.utc).timestamp())
            await db.execute("INSERT OR REPLACE INTO temporary_bans (user_id, moderator_id, reason, expires_at, created_at) VALUES (?, ?, ?, ?, ?)", (user.id, interaction.user.id, reason, expires, ts))
            e = info_embed("Temporary ban", f"User: {getattr(user, 'mention', str(user))}\nDuration: {duration}\nReason: {reason}")
            if not dm_sent:
                e.add_field(name="Notice", value="Could not DM the user before tempban.")
            await interaction.response.send_message(embed=e)
            mod_ch = os.getenv("MOD_LOG_CHANNEL_ID")
            if mod_ch:
                ch = bot.get_channel(int(mod_ch))
                if ch:
                    await ch.send(embed=e)
        except Exception:
            logger.exception("Tempban failed")
            await interaction.response.send_message(embed=error_embed("Tempban failed", "Could not tempban the user."))

    @bot.tree.command(name="unban")
    @app_commands.allowed_contexts(
        guilds=True,
        dms=False,
        private_channels=False,
    )
    @app_commands.describe(user_id="Discord user ID to unban")
    async def unban_cmd(interaction: discord.Interaction, user_id: str):
        if interaction.guild is None:
            await interaction.response.send_message(embed=error_embed("Guild only", "This command can only be used in a server."))
            return
        if not interaction.user.guild_permissions.ban_members:
            await interaction.response.send_message(embed=error_embed("Permission denied", "You lack Ban Members."))
            return
        try:
            uid = int(user_id)
            await interaction.guild.unban(discord.Object(id=uid))
            await db.execute("INSERT INTO moderation_actions (action, user_id, moderator_id, reason, timestamp) VALUES (?, ?, ?, ?, ?)", ("unban", uid, interaction.user.id, "manual unban", int(datetime.now(timezone.utc).timestamp())))
            await interaction.response.send_message(embed=success_embed("Unbanned", f"Unbanned <@{uid}>") )
        except Exception:
            logger.exception("Unban failed")
            await interaction.response.send_message(embed=error_embed("Unban failed", "Could not unban the ID provided."))

    @bot.tree.command(name="kick")
    @app_commands.allowed_contexts(
        guilds=True,
        dms=False,
        private_channels=False,
    )
    @app_commands.describe(user="User to kick", reason="Reason")
    async def kick_cmd(interaction: discord.Interaction, user: discord.Member, reason: str):
        if interaction.guild is None:
            await interaction.response.send_message(embed=error_embed("Guild only", "This command can only be used in a server."))
            return
        if not interaction.user.guild_permissions.kick_members:
            await interaction.response.send_message(embed=error_embed("Permission denied", "You lack Kick Members."))
            return
        if not await check_protected_target(interaction, user, "kicked"):
            return
        # DM before kicking
        dm_sent = False
        try:
            dm = info_embed("You have been kicked", f"Server: {interaction.guild.name}\nModerator: {interaction.user.mention}\nReason: {reason}")
            await user.send(embed=dm)
            dm_sent = True
        except Exception:
            logger.exception("Failed to DM user before kick")

        try:
            await user.kick(reason=reason)
            await db.execute("INSERT INTO moderation_actions (action, user_id, moderator_id, reason, timestamp) VALUES (?, ?, ?, ?, ?)", ("kick", user.id, interaction.user.id, reason, int(datetime.now(timezone.utc).timestamp())))
            e = info_embed("User kicked", f"{user.mention} — {reason}")
            if not dm_sent:
                e.add_field(name="Notice", value="Could not DM the user before kick.")
            await interaction.response.send_message(embed=e)
        except Exception:
            logger.exception("Kick failed")
            await interaction.response.send_message(embed=error_embed("Kick failed", "Could not kick user."))

    @bot.tree.command(name="clear")
    @app_commands.allowed_contexts(
        guilds=True,
        dms=False,
        private_channels=False,
    )
    @app_commands.describe(amount="Number of messages to delete")
    async def clear_cmd(interaction: discord.Interaction, amount: int):
        if interaction.guild is None:
            await interaction.response.send_message(embed=error_embed("Guild only", "This command can only be used in a server."))
            return
        if not interaction.user.guild_permissions.manage_messages:
            await interaction.response.send_message(embed=error_embed("Permission denied", "You lack Manage Messages."))
            return
        if amount < 1 or amount > 1000:
            await interaction.response.send_message(embed=error_embed("Invalid amount", "Enter a number between 1 and 1000."))
            return
        deleted = await interaction.channel.purge(limit=amount)
        await db.execute("INSERT INTO moderation_actions (action, user_id, moderator_id, reason, timestamp) VALUES (?, ?, ?, ?, ?)", ("clear", None, interaction.user.id, f"cleared {len(deleted)} messages", int(datetime.now(timezone.utc).timestamp())))
        await interaction.response.send_message(embed=success_embed("Messages deleted", f"Deleted {len(deleted)} messages."))

    @bot.tree.command(name="lock")
    @app_commands.allowed_contexts(
        guilds=True,
        dms=False,
        private_channels=False,
    )
    async def lock_cmd(interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(embed=error_embed("Guild only", "This command can only be used in a server."))
            return
        if not interaction.user.guild_permissions.manage_channels:
            await interaction.response.send_message(embed=error_embed("Permission denied", "You lack Manage Channels."))
            return
        everyone = interaction.guild.default_role
        await interaction.channel.set_permissions(everyone, send_messages=False)
        await interaction.response.send_message(embed=info_embed("Channel locked", "This channel is now read-only for regular members."))

    @bot.tree.command(name="unlock")
    @app_commands.allowed_contexts(
        guilds=True,
        dms=False,
        private_channels=False,
    )
    async def unlock_cmd(interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(embed=error_embed("Guild only", "This command can only be used in a server."))
            return
        if not interaction.user.guild_permissions.manage_channels:
            await interaction.response.send_message(embed=error_embed("Permission denied", "You lack Manage Channels."))
            return
        everyone = interaction.guild.default_role
        await interaction.channel.set_permissions(everyone, send_messages=None)
        await interaction.response.send_message(embed=info_embed("Channel unlocked", "This channel has been unlocked."))

    @bot.tree.command(name="unsubscribe-from-blog")
    @app_commands.allowed_contexts(
        guilds=True,
        dms=True,
        private_channels=True,
    )
    async def unsubscribe(interaction: discord.Interaction):
        user_id = interaction.user.id
        await db.execute("DELETE FROM blog_subscribers WHERE user_id = ?", (user_id,))
        e = success_embed("Unsubscribed", "You will no longer receive blog DMs.")
        await interaction.response.send_message(embed=e)

    @bot.tree.command(name="blog-subscription-status")
    @app_commands.allowed_contexts(
        guilds=True,
        dms=True,
        private_channels=True,
    )
    async def sub_status(interaction: discord.Interaction):
        user_id = interaction.user.id
        exist = await db.fetchone("SELECT 1 FROM blog_subscribers WHERE user_id = ?", (user_id,))
        if exist:
            e = info_embed("Subscription status", "You are subscribed to blog DMs.")
        else:
            e = info_embed("Subscription status", "You are NOT subscribed to blog DMs.")
        await interaction.response.send_message(embed=e)


    return bot
