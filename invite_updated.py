"""
Trigger Rewards - independent invite tracker module
---------------------------------------------------
Compatible with discord.py 2.x. This module does NOT edit main.py, issue rewards,
reset real invite data, or delete tickets. It tracks invite usage and produces
a clearly-labelled verification report for staff/member review.

Integration:
    from invite import install_invite_module
    invite_tracker = install_invite_module(bot)

Required Discord bot settings:
    - Server Members Intent enabled in the Developer Portal and in main.py
    - Manage Server permission (to fetch invite usage)
    - View Channels / Send Messages / Embed Links for report delivery

The module uses its own SQLite database file: trigger_invites.sqlite3
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("trigger_rewards.invites")

DEFAULT_DB = Path("trigger_invites.sqlite3")
DEFAULT_FAKE_ACCOUNT_DAYS = 7


@dataclass
class InviteSnapshot:
    code: str
    uses: int
    inviter_id: Optional[int]
    max_uses: int
    temporary: bool


class InviteTracker:
    def __init__(
        self,
        bot: commands.Bot,
        db_path: Path = DEFAULT_DB,
        fake_account_days: int = DEFAULT_FAKE_ACCOUNT_DAYS,
    ):
        self.bot = bot
        self.db_path = Path(db_path)
        self.fake_account_days = max(0, int(fake_account_days))
        self._guild_invites: dict[int, dict[str, InviteSnapshot]] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._ready_once = False
        self._init_db()

    def _connect(self):
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        return con

    def _init_db(self):
        with self._connect() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS invite_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    member_id INTEGER NOT NULL,
                    inviter_id INTEGER,
                    invite_code TEXT,
                    event TEXT NOT NULL,
                    account_created TEXT,
                    is_suspected_fake INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_invite_events_member
                    ON invite_events(guild_id, member_id);
                CREATE INDEX IF NOT EXISTS idx_invite_events_inviter
                    ON invite_events(guild_id, inviter_id);
                CREATE TABLE IF NOT EXISTS known_members (
                    guild_id INTEGER NOT NULL,
                    member_id INTEGER NOT NULL,
                    first_seen_join TEXT NOT NULL,
                    last_event TEXT NOT NULL,
                    inviter_id INTEGER,
                    is_suspected_fake INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(guild_id, member_id)
                );
                CREATE TABLE IF NOT EXISTS manual_invite_adjustments (
                    guild_id INTEGER NOT NULL,
                    member_id INTEGER NOT NULL,
                    amount INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(guild_id, member_id)
                );
                CREATE TABLE IF NOT EXISTS reward_invite_spends (
                    claim_id INTEGER PRIMARY KEY,
                    guild_id INTEGER NOT NULL,
                    member_id INTEGER NOT NULL,
                    amount INTEGER NOT NULL CHECK(amount >= 0),
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_reward_invite_spends_member
                    ON reward_invite_spends(guild_id, member_id);
                """
            )
            columns = {row[1] for row in con.execute("PRAGMA table_info(known_members)").fetchall()}
            if "inviter_id" not in columns:
                con.execute("ALTER TABLE known_members ADD COLUMN inviter_id INTEGER")
            if "is_suspected_fake" not in columns:
                con.execute("ALTER TABLE known_members ADD COLUMN is_suspected_fake INTEGER NOT NULL DEFAULT 0")

    def _lock_for(self, guild_id: int) -> asyncio.Lock:
        return self._locks.setdefault(guild_id, asyncio.Lock())

    async def _fetch_snapshot(self, guild: discord.Guild):
        invites = await guild.invites()
        result = {}
        for invite in invites:
            result[invite.code] = InviteSnapshot(
                code=invite.code,
                uses=int(invite.uses or 0),
                inviter_id=invite.inviter.id if invite.inviter else None,
                max_uses=int(invite.max_uses or 0),
                temporary=bool(invite.temporary),
            )
        return result

    async def warm_cache(self):
        """Cache current invite use totals. Existing historical joins are not reconstructed."""
        for guild in self.bot.guilds:
            if not guild.me or not guild.me.guild_permissions.manage_guild:
                log.warning("Invite tracker needs Manage Server in guild %s", guild.id)
                continue
            try:
                self._guild_invites[guild.id] = await self._fetch_snapshot(guild)
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.warning("Could not cache invites for guild %s: %s", guild.id, exc)

    async def on_ready(self):
        if not self._ready_once:
            self._ready_once = True
            await self.warm_cache()

    async def on_invite_create(self, invite: discord.Invite):
        if invite.guild is None:
            return
        try:
            self._guild_invites[invite.guild.id] = await self._fetch_snapshot(invite.guild)
        except (discord.Forbidden, discord.HTTPException):
            pass

    async def on_invite_delete(self, invite: discord.Invite):
        if invite.guild is None:
            return
        try:
            self._guild_invites[invite.guild.id] = await self._fetch_snapshot(invite.guild)
        except (discord.Forbidden, discord.HTTPException):
            pass

    async def on_member_join(self, member: discord.Member):
        guild = member.guild
        async with self._lock_for(guild.id):
            before = self._guild_invites.get(guild.id, {})
            inviter_id = None
            used_code = None
            after = None
            try:
                after = await self._fetch_snapshot(guild)
                increased = [
                    snap for code, snap in after.items()
                    if code in before and snap.uses > before[code].uses
                ]
                # Only attribute when exactly one invite's usage increased.
                if len(increased) == 1:
                    used_code = increased[0].code
                    inviter_id = increased[0].inviter_id
                elif len(increased) > 1:
                    log.info("Ambiguous invite attribution in guild %s for member %s", guild.id, member.id)
                self._guild_invites[guild.id] = after
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.warning("Could not resolve invite for member %s: %s", member.id, exc)

            now = datetime.now(timezone.utc)
            age_days = max(0, (now - member.created_at).days)
            suspected_fake = int(self.fake_account_days > 0 and age_days < self.fake_account_days)
            now_iso = now.isoformat()
            with self._connect() as con:
                previous = con.execute(
                    "SELECT 1 FROM known_members WHERE guild_id=? AND member_id=?",
                    (guild.id, member.id),
                ).fetchone()
                event = "rejoin" if previous else "join"
                con.execute(
                    """INSERT INTO invite_events
                    (guild_id, member_id, inviter_id, invite_code, event,
                     account_created, is_suspected_fake, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (guild.id, member.id, inviter_id, used_code, event,
                     member.created_at.isoformat(), suspected_fake, now_iso),
                )
                con.execute(
                    """INSERT INTO known_members
                    (guild_id, member_id, first_seen_join, last_event, inviter_id, is_suspected_fake)
                    VALUES (?, ?, ?, 'join', ?, ?)
                    ON CONFLICT(guild_id, member_id)
                    DO UPDATE SET last_event='join',
                                  inviter_id=COALESCE(excluded.inviter_id, known_members.inviter_id),
                                  is_suspected_fake=excluded.is_suspected_fake""",
                    (guild.id, member.id, now_iso, inviter_id, suspected_fake),
                )
            if inviter_id:
                # A custom event lets main.py automatically re-check that inviter's open reward ticket.
                self.bot.dispatch("invite_attributed", member, inviter_id, event)

    async def on_member_remove(self, member: discord.Member):
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._connect() as con:
            known = con.execute(
                "SELECT inviter_id FROM known_members WHERE guild_id=? AND member_id=?",
                (member.guild.id, member.id),
            ).fetchone()
            inviter_id = known["inviter_id"] if known else None
            con.execute(
                """INSERT INTO invite_events
                (guild_id, member_id, inviter_id, invite_code, event,
                 account_created, is_suspected_fake, created_at)
                VALUES (?, ?, ?, NULL, 'leave', ?, 0, ?)""",
                (member.guild.id, member.id, inviter_id, member.created_at.isoformat(), now_iso),
            )
            con.execute(
                "UPDATE known_members SET last_event='leave' WHERE guild_id=? AND member_id=?",
                (member.guild.id, member.id),
            )

    def stats(self, guild_id: int, member_id: int) -> dict:
        """Return deduplicated invite counts; fake flags are age-based warnings only."""
        with self._connect() as con:
            total = con.execute(
                "SELECT COUNT(DISTINCT member_id) FROM invite_events WHERE guild_id=? AND inviter_id=? AND event IN ('join','rejoin')",
                (guild_id, member_id),
            ).fetchone()[0]
            active = con.execute(
                "SELECT COUNT(*) FROM known_members WHERE guild_id=? AND inviter_id=? AND last_event='join'",
                (guild_id, member_id),
            ).fetchone()[0]
            left = con.execute(
                "SELECT COUNT(*) FROM invite_events WHERE guild_id=? AND inviter_id=? AND event='leave'",
                (guild_id, member_id),
            ).fetchone()[0]
            suspected_fake = con.execute(
                "SELECT COUNT(*) FROM known_members WHERE guild_id=? AND inviter_id=? AND last_event='join' AND is_suspected_fake=1",
                (guild_id, member_id),
            ).fetchone()[0]
            rejoined = con.execute(
                "SELECT COUNT(DISTINCT member_id) FROM invite_events WHERE guild_id=? AND inviter_id=? AND event='rejoin' AND created_at >= ?",
                (guild_id, member_id, (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()),
            ).fetchone()[0]
            adjustment_row = con.execute(
                "SELECT amount FROM manual_invite_adjustments WHERE guild_id=? AND member_id=?",
                (guild_id, member_id),
            ).fetchone()
            adjustment = int(adjustment_row[0]) if adjustment_row else 0
            spent_row = con.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM reward_invite_spends WHERE guild_id=? AND member_id=?",
                (guild_id, member_id),
            ).fetchone()
            spent = int(spent_row[0] or 0)
        eligible = max(0, int(active) - int(suspected_fake))
        remaining = max(0, eligible + adjustment - spent)
        return {
            "total": remaining,
            "joins": int(total),
            "active": int(active),
            "left": int(left),
            "suspected_fake": int(suspected_fake),
            "rejoined": int(rejoined),
            "manual_adjustment": adjustment,
            "spent_on_rewards": spent,
            "eligible_estimate": remaining,
        }

    def record_reward_spend(self, guild_id: int, member_id: int, amount: int, claim_id: int) -> bool:
        """Deduct invite credit once per successfully delivered reward claim."""
        amount = max(0, int(amount))
        if amount == 0:
            return False
        with self._connect() as con:
            cur = con.execute(
                """INSERT OR IGNORE INTO reward_invite_spends
                   (claim_id, guild_id, member_id, amount, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (int(claim_id), int(guild_id), int(member_id), amount,
                 datetime.now(timezone.utc).isoformat()),
            )
            return cur.rowcount == 1

    def add_invites(self, guild_id: int, member_id: int, amount: int) -> int:
        if amount < 1 or amount > 100000:
            raise ValueError("Amount must be between 1 and 100000.")
        with self._connect() as con:
            con.execute(
                """INSERT INTO manual_invite_adjustments(guild_id, member_id, amount, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(guild_id, member_id) DO UPDATE SET
                     amount=manual_invite_adjustments.amount + excluded.amount,
                     updated_at=excluded.updated_at""",
                (guild_id, member_id, amount, datetime.now(timezone.utc).isoformat()),
            )
            row = con.execute(
                "SELECT amount FROM manual_invite_adjustments WHERE guild_id=? AND member_id=?",
                (guild_id, member_id),
            ).fetchone()
            return int(row[0])

    def leaderboard(self, guild_id: int, limit: int = 10):
        with self._connect() as con:
            rows = con.execute(
                """SELECT inviter_id, COUNT(*) AS total
                   FROM known_members
                   WHERE guild_id=? AND inviter_id IS NOT NULL AND last_event='join'
                     AND is_suspected_fake=0
                   GROUP BY inviter_id ORDER BY total DESC LIMIT ?""",
                (guild_id, max(1, min(int(limit), 25))),
            ).fetchall()
        return [(int(row['inviter_id']), int(row['total'])) for row in rows]

    def clear_inviter(self, guild_id: int, inviter_id: int) -> int:
        """Reset one inviter's attributed history without touching other members' records."""
        with self._connect() as con:
            count = con.execute(
                "SELECT COUNT(DISTINCT member_id) FROM invite_events WHERE guild_id=? AND inviter_id=?",
                (guild_id, inviter_id),
            ).fetchone()[0]
            con.execute("DELETE FROM invite_events WHERE guild_id=? AND inviter_id=?", (guild_id, inviter_id))
            con.execute("UPDATE known_members SET inviter_id=NULL WHERE guild_id=? AND inviter_id=?", (guild_id, inviter_id))
            con.execute("DELETE FROM manual_invite_adjustments WHERE guild_id=? AND member_id=?", (guild_id, inviter_id))
        return int(count)

    async def make_report(self, guild: discord.Guild, member: discord.Member):
        data = self.stats(guild.id, member.id)
        embed = discord.Embed(
            title="Invite log",
            description=(
                f"» **{member.display_name} has {data['total']} invites**\n\n"
                f"**Joins :** {data['joins']}\n"
                f"**Left :** {data['left']}\n"
                f"**Fake :** {data['suspected_fake']}\n"
                f"**Rejoins :** {data['rejoined']} (**7d**)"
            ),
            color=0x36C5BD,
            timestamp=datetime.now(timezone.utc),
        )
        avatar = member.display_avatar
        if avatar:
            embed.set_thumbnail(url=avatar.url)
        embed.set_footer(text=f"Requested by {member.display_name} | Trigger Rewards")
        return embed, data


class InviteCheckView(discord.ui.View):
    def __init__(self, tracker: InviteTracker, owner_id: Optional[int] = None):
        super().__init__(timeout=None)
        self.tracker = tracker
        self.owner_id = owner_id

    @discord.ui.button(
        label="Check My Invites",
        style=discord.ButtonStyle.primary,
        emoji="📨",
        custom_id="trg_invites:check",
    )
    async def check_my_invites(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Use this button inside a server.", ephemeral=True)
        if self.owner_id and interaction.user.id != self.owner_id:
            return await interaction.response.send_message("This panel belongs to another member.", ephemeral=True)
        embed, _ = await self.tracker.make_report(interaction.guild, interaction.user)
        await interaction.response.send_message(embed=embed, ephemeral=True)


def install_invite_module(bot: commands.Bot, db_path: Path = DEFAULT_DB) -> InviteTracker:
    """Register event listeners and /check-invites command; call once from main.py."""
    tracker = InviteTracker(bot, db_path=db_path)
    bot.add_listener(tracker.on_ready, "on_ready")
    bot.add_listener(tracker.on_invite_create, "on_invite_create")
    bot.add_listener(tracker.on_invite_delete, "on_invite_delete")
    bot.add_listener(tracker.on_member_join, "on_member_join")
    bot.add_listener(tracker.on_member_remove, "on_member_remove")

    @bot.command(name="i")
    @commands.guild_only()
    async def invite_self(ctx: commands.Context):
        data = tracker.stats(ctx.guild.id, ctx.author.id)
        embed = discord.Embed(title="📨 Your Invite Statistics", color=0xF2B84B)
        embed.add_field(name="Total tracked", value=str(data["total"]), inline=True)
        embed.add_field(name="Currently in server", value=str(data["active"]), inline=True)
        embed.add_field(name="Left", value=str(data["left"]), inline=True)
        embed.add_field(name="Rejoined", value=str(data["rejoined"]), inline=True)
        embed.add_field(name="New-account warnings", value=str(data["suspected_fake"]), inline=True)
        embed.add_field(name="Estimated eligible", value=str(data["eligible_estimate"]), inline=True)
        embed.set_footer(text="Only joins tracked after setup are counted; new-account warnings are not proof of abuse.")
        await ctx.reply(embed=embed, mention_author=False)

    @bot.command(name="inviteinfo")
    @commands.guild_only()
    async def invite_info(ctx: commands.Context, member: discord.Member):
        data = tracker.stats(ctx.guild.id, member.id)
        embed = discord.Embed(title=f"📨 Invite Statistics • {member.display_name}", color=0xF2B84B)
        embed.add_field(name="Total tracked", value=str(data["total"]), inline=True)
        embed.add_field(name="Active", value=str(data["active"]), inline=True)
        embed.add_field(name="Left", value=str(data["left"]), inline=True)
        embed.add_field(name="Rejoined", value=str(data["rejoined"]), inline=True)
        embed.add_field(name="New-account warnings", value=str(data["suspected_fake"]), inline=True)
        embed.add_field(name="Estimated eligible", value=str(data["eligible_estimate"]), inline=True)
        await ctx.reply(embed=embed, mention_author=False)

    @invite_info.error
    async def invite_info_error(ctx, error):
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.reply("Usage: `!inviteinfo @member`", mention_author=False)
        elif isinstance(error, commands.MemberNotFound):
            await ctx.reply("I couldn't find that server member.", mention_author=False)

    @bot.command(name="topinvites")
    @commands.guild_only()
    async def top_invites(ctx: commands.Context):
        rows = tracker.leaderboard(ctx.guild.id, 10)
        if not rows:
            return await ctx.reply("No eligible tracked invites yet.", mention_author=False)
        lines = []
        for position, (user_id, count) in enumerate(rows, 1):
            lines.append(f"**{position}.** <@{user_id}> — **{count}** active eligible")
        embed = discord.Embed(title="🏆 Invite Leaderboard", description="\n".join(lines), color=0xF2B84B)
        embed.set_footer(text="Based on currently present invitees; only tracked joins count.")
        await ctx.reply(embed=embed, mention_author=False)

    @bot.command(name="clearinvites")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def clear_invites(ctx: commands.Context, member: discord.Member):
        count = tracker.clear_inviter(ctx.guild.id, member.id)
        await ctx.reply(f"🧹 Reset tracked invite history for {member.mention}. **{count}** attributed member record(s) cleared.", mention_author=False)

    @clear_invites.error
    async def clear_invites_error(ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.reply("🔒 You need Manage Server permission to clear invite stats.", mention_author=False)
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.reply("Usage: `!clearinvites @member`", mention_author=False)

    @bot.tree.command(name="setup_invite_pannel", description="Post a public panel where members can check their invites")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def setup_invite_pannel(interaction: discord.Interaction):
        embed = discord.Embed(
            title="📨 Invite Tracker",
            description=(
                "Click the button below to check your invite log.\n\n"
                "Your report shows **joins, left members, fake-account warnings, re-joins (7d), and total invites**."
            ),
            color=0x36C5BD,
        )
        embed.set_footer(text="Trigger Rewards • Invite tracking starts after setup")
        await interaction.response.send_message(
            "✅ Invite panel posted.", ephemeral=True
        )
        await interaction.channel.send(embed=embed, view=InviteCheckView(tracker))

    add_group = app_commands.Group(name="add", description="Admin invite adjustments")

    @add_group.command(name="invites", description="Add invite credit to a member")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(member="Member receiving invite credit", amount="Number of invites to add")
    async def add_invites(interaction: discord.Interaction, member: discord.Member, amount: app_commands.Range[int, 1, 100000]):
        new_total = tracker.add_invites(interaction.guild_id, member.id, amount)
        await interaction.response.send_message(
            f"✅ Added **{amount}** invite credit to {member.mention}. Their displayed invite total is now **{new_total}**.",
            ephemeral=True,
        )

    bot.tree.add_command(add_group)

    @bot.tree.command(name="check-invites", description="Open the Trigger Rewards invite statistics panel")
    @app_commands.guild_only()
    async def check_invites(interaction: discord.Interaction):
        embed = discord.Embed(
            title="📨 TRIGGER REWARDS • INVITE TRACKER",
            description=(
                "Click the button below to view your tracked invite statistics.\n\n"
                "Statistics include joins, members who left, rejoins and suspected new accounts."
            ),
            color=0xF2B84B,
        )
        embed.set_footer(text="Tracking begins after the module is installed and initialized.")
        await interaction.response.send_message(
            embed=embed,
            view=InviteCheckView(tracker, owner_id=interaction.user.id),
            ephemeral=True,
        )

    # Register the persistent view so the panel buttons keep working after restarts.
    bot.add_view(InviteCheckView(tracker))
    return tracker
