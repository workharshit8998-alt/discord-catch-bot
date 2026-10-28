import os, re, sqlite3, secrets
from datetime import datetime, timezone
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
SITE_URL = os.getenv("DEMO_URL", "https://trigger-rewards.vercel.app").strip()
DB_FILE = os.getenv("TICKET_DB_PATH", "trigger_rewards.sqlite3")
intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)
CATS = {"youtube": ("▶️", "YouTube"), "minecraft": ("🟩", "Minecraft"), "streaming": ("📺", "OTT & Streaming")}

def database():
    c = sqlite3.connect(DB_FILE)
    c.row_factory = sqlite3.Row
    c.executescript("""
    CREATE TABLE IF NOT EXISTS settings(guild INTEGER PRIMARY KEY, staff_role INTEGER DEFAULT 0, ticket_category INTEGER DEFAULT 0, vouch_channel INTEGER DEFAULT 0, invite_default INTEGER DEFAULT 3);
    CREATE TABLE IF NOT EXISTS rewards(id INTEGER PRIMARY KEY, guild INTEGER, category TEXT, name TEXT, invites INTEGER, active INTEGER DEFAULT 1, UNIQUE(guild,category,name));
    CREATE TABLE IF NOT EXISTS tickets(channel INTEGER PRIMARY KEY, guild INTEGER, user INTEGER, category TEXT, reward TEXT, status TEXT DEFAULT 'open', created TEXT);
    CREATE TABLE IF NOT EXISTS claims(id INTEGER PRIMARY KEY, guild INTEGER, user INTEGER, reward TEXT, code TEXT UNIQUE, status TEXT DEFAULT 'issued', created TEXT, redeemed TEXT, UNIQUE(guild,user,reward));
    """)
    c.commit()
    return c

def cfg(guild):
    c=database()
    c.execute("INSERT OR IGNORE INTO settings(guild,invite_default) VALUES(?,3)",(guild,))
    c.commit()
    row=dict(c.execute("SELECT * FROM settings WHERE guild=?",(guild,)).fetchone())
    c.close()
    return row

def staff(i):
    if not i.guild or not isinstance(i.user, discord.Member): return False
    s=cfg(i.guild.id)
    return i.user.guild_permissions.administrator or (s["staff_role"] and any(r.id==s["staff_role"] for r in i.user.roles))

class Panel(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label="Open Reward Ticket",style=discord.ButtonStyle.success,emoji="🎟️",custom_id="trg:open")
    async def open(self,i,b):
        if not i.guild: return await i.response.send_message("Use this in a server.",ephemeral=True)
        c=database()
        old=c.execute("SELECT channel FROM tickets WHERE guild=? AND user=? AND status='open'",(i.guild.id,i.user.id)).fetchone()
        if old and i.guild.get_channel(old["channel"]):
            ch=i.guild.get_channel(old["channel"]); c.close()
            return await i.response.send_message(f"You already have an open ticket: {ch.mention}",ephemeral=True)
        s=cfg(i.guild.id); cat=i.guild.get_channel(s["ticket_category"]) if s["ticket_category"] else None
        overwrites={i.guild.default_role:discord.PermissionOverwrite(view_channel=False),i.user:discord.PermissionOverwrite(view_channel=True,send_messages=True,read_message_history=True)}
        if i.guild.me: overwrites[i.guild.me]=discord.PermissionOverwrite(view_channel=True,send_messages=True,manage_channels=True,read_message_history=True)
        role=i.guild.get_role(s["staff_role"]) if s["staff_role"] else None
        if role: overwrites[role]=discord.PermissionOverwrite(view_channel=True,send_messages=True,read_message_history=True)
        name=re.sub(r"[^a-z0-9-]","",i.user.name.lower().replace(" ","-"))[:18] or "member"
        try: ch=await i.guild.create_text_channel(f"reward-{name}",category=cat,overwrites=overwrites,topic=f"Reward ticket user={i.user.id}")
        except discord.Forbidden:
            c.close(); return await i.response.send_message("I need Manage Channels, Send Messages and Embed Links permissions.",ephemeral=True)
        c.execute("INSERT INTO tickets(channel,guild,user,created) VALUES(?,?,?,?)",(ch.id,i.guild.id,i.user.id,datetime.now(timezone.utc).isoformat()))
        c.commit(); c.close()
        e=discord.Embed(title="🎟️ Trigger Rewards Support",description=f"Welcome {i.user.mention}! Choose a category below.\n\nNever share your Discord password, session token or 2FA codes.",color=0xf2b84b)
        e.set_footer(text="Trigger Rewards • Private support")
        await ch.send(content=i.user.mention,embed=e,view=Categories())
        await i.response.send_message(f"Your private ticket is ready: {ch.mention}",ephemeral=True)

class Categories(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.select(placeholder="Choose a reward category",custom_id="trg:category",options=[
        discord.SelectOption(label="YouTube",value="youtube",emoji="▶️"),
        discord.SelectOption(label="Minecraft",value="minecraft",emoji="🟩"),
        discord.SelectOption(label="OTT & Streaming",value="streaming",emoji="📺")])
    async def choose(self,i,s):
        c=database(); t=c.execute("SELECT * FROM tickets WHERE channel=? AND status='open'",(i.channel_id,)).fetchone()
        if not t or t["user"]!=i.user.id:
            c.close(); return await i.response.send_message("Only the ticket owner can choose a category.",ephemeral=True)
        cat=s.values[0]; c.execute("UPDATE tickets SET category=? WHERE channel=?",(cat,i.channel_id))
        rows=c.execute("SELECT name,invites FROM rewards WHERE guild=? AND category=? AND active=1 ORDER BY name",(i.guild.id,cat)).fetchall()
        c.commit(); c.close()
        if not rows: return await i.response.send_message("No rewards configured here yet. Ask an administrator to use /reward-add.",ephemeral=True)
        view=RewardChoices(cat,[dict(r) for r in rows])
        await i.response.send_message(f"Selected **{CATS[cat][1]}**. Now choose your reward.",view=view,ephemeral=True)

class RewardChoices(discord.ui.View):
    def __init__(self,cat,rows):
        super().__init__(timeout=300); self.cat=cat
        sel=discord.ui.Select(placeholder="Choose a reward",options=[discord.SelectOption(label=r["name"][:100],value=r["name"],description=f"Required invites: {r['invites']}") for r in rows[:25]])
        sel.callback=self.pick; self.add_item(sel)
    async def pick(self,i):
        name=i.data["values"][0]; c=database()
        t=c.execute("SELECT * FROM tickets WHERE channel=? AND status='open'",(i.channel_id,)).fetchone()
        r=c.execute("SELECT invites FROM rewards WHERE guild=? AND category=? AND name=? AND active=1",(i.guild.id,self.cat,name)).fetchone()
        if not t or t["user"]!=i.user.id or not r:
            c.close(); return await i.response.send_message("Ticket/reward is no longer valid.",ephemeral=True)
        prior=c.execute("SELECT status FROM claims WHERE guild=? AND user=? AND reward=?",(i.guild.id,i.user.id,name)).fetchone()
        if prior:
            c.close(); return await i.response.send_message("You already claimed this reward. Duplicate claims are blocked.",ephemeral=True)
        c.execute("UPDATE tickets SET reward=? WHERE channel=?",(name,i.channel_id)); c.commit(); c.close()
        e=discord.Embed(title="📋 Invite Requirement",description=f"**Reward:** {name}\n**Required invites:** {r['invites']}\n\nRun your invite tracker bot's -i command, then wait for staff to verify the result in this ticket. Staff will use /approve-claim after checking the tracker output.",color=0x5865f2)
        e.set_footer(text="Verification is staff-checked for now; no invite count is guessed.")
        await i.response.send_message(embed=e,view=CloseView())

class CloseView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label="Close Ticket",style=discord.ButtonStyle.danger,emoji="🔒",custom_id="trg:close")
    async def close(self,i,b):
        c=database(); t=c.execute("SELECT * FROM tickets WHERE channel=? AND status='open'",(i.channel_id,)).fetchone(); c.close()
        if not t: return await i.response.send_message("Ticket is already closed.",ephemeral=True)
        if i.user.id!=t["user"] and not staff(i): return await i.response.send_message("Only the owner or staff can close this ticket.",ephemeral=True)
        if isinstance(i.channel,discord.TextChannel):
            m=i.guild.get_member(t["user"])
            if m: await i.channel.set_permissions(m,view_channel=False,send_messages=False)
        c=database(); c.execute("UPDATE tickets SET status='closed' WHERE channel=?",(i.channel_id,)); c.commit(); c.close()
        await i.response.send_message("🔒 Ticket closed.")

@bot.event
async def on_ready():
    bot.add_view(Panel()); bot.add_view(Categories()); bot.add_view(CloseView())
    try:
        synced=await bot.tree.sync()
        print(f"Ready as {bot.user}; synced {len(synced)} commands")
    except Exception as e: print(f"Command sync error: {e}")

@bot.tree.command(name="ticket-setup",description="Post the Trigger Rewards ticket panel")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def ticket_setup(i:discord.Interaction,channel:discord.TextChannel,category:discord.CategoryChannel=None):
    if not i.guild or not i.user.guild_permissions.administrator: return await i.response.send_message("Administrator only.",ephemeral=True)
    c=database(); c.execute("INSERT INTO settings(guild,ticket_category) VALUES(?,?) ON CONFLICT(guild) DO UPDATE SET ticket_category=excluded.ticket_category",(i.guild.id,category.id if category else 0)); c.commit(); c.close()
    e=discord.Embed(title="🎁 TRIGGER REWARDS",description="Claim rewards through a private ticket.\n\n1. Open a ticket.\n2. Choose YouTube, Minecraft, or OTT & Streaming.\n3. Select a reward and complete its invite requirement.\n4. Staff verify the tracker result and issue a unique code.\n\nEach user can claim each reward once. Codes are single-use and become invalid after redemption. Never share passwords or tokens.",color=0xf2b84b)
    e.add_field(name="Reward categories",value="▶️ YouTube  •  🟩 Minecraft  •  📺 OTT & Streaming",inline=False); e.set_footer(text="Trigger Rewards • Official reward support")
    await channel.send(embed=e,view=Panel())
    await i.response.send_message(f"Panel posted in {channel.mention}.",ephemeral=True)

@bot.tree.command(name="staff-role",description="Set the role that can view reward tickets")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def staff_role(i:discord.Interaction,role:discord.Role):
    if not i.guild or not i.user.guild_permissions.administrator: return await i.response.send_message("Administrator only.",ephemeral=True)
    c=database(); c.execute("INSERT INTO settings(guild,staff_role) VALUES(?,?) ON CONFLICT(guild) DO UPDATE SET staff_role=excluded.staff_role",(i.guild.id,role.id)); c.commit(); c.close()
    await i.response.send_message(f"Ticket staff role set to {role.mention}.",ephemeral=True)

@bot.tree.command(name="invite-requirement",description="Set the default invite requirement")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def invite_req(i:discord.Interaction,count:app_commands.Range[int,0,1000]):
    if not i.guild or not i.user.guild_permissions.administrator: return await i.response.send_message("Administrator only.",ephemeral=True)
    c=database(); c.execute("INSERT INTO settings(guild,invite_default) VALUES(?,?) ON CONFLICT(guild) DO UPDATE SET invite_default=excluded.invite_default",(i.guild.id,count)); c.commit(); c.close()
    await i.response.send_message(f"Default invite requirement set to **{count}**.",ephemeral=True)

@bot.tree.command(name="reward-add",description="Add a reward to the category menu")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.choices(category=[app_commands.Choice(name="YouTube",value="youtube"),app_commands.Choice(name="Minecraft",value="minecraft"),app_commands.Choice(name="OTT & Streaming",value="streaming")])
async def reward_add(i:discord.Interaction,category:app_commands.Choice[str],name:app_commands.Range[str,2,80],invites:app_commands.Range[int,0,1000]=None):
    if not i.guild or not i.user.guild_permissions.administrator: return await i.response.send_message("Administrator only.",ephemeral=True)
    required=invites if invites is not None else cfg(i.guild.id)["invite_default"]
    c=database()
    try: c.execute("INSERT INTO rewards(guild,category,name,invites) VALUES(?,?,?,?)",(i.guild.id,category.value,name.strip(),required)); c.commit()
    except sqlite3.IntegrityError:
        c.close(); return await i.response.send_message("That reward already exists in this category.",ephemeral=True)
    c.close(); await i.response.send_message(f"Added **{name}** under **{CATS[category.value][1]}**, requiring **{required}** invites.",ephemeral=True)

@bot.tree.command(name="reward-remove",description="Disable a reward option")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.choices(category=[app_commands.Choice(name="YouTube",value="youtube"),app_commands.Choice(name="Minecraft",value="minecraft"),app_commands.Choice(name="OTT & Streaming",value="streaming")])
async def reward_remove(i:discord.Interaction,category:app_commands.Choice[str],name:str):
    if not i.guild or not i.user.guild_permissions.administrator: return await i.response.send_message("Administrator only.",ephemeral=True)
    c=database(); cur=c.execute("UPDATE rewards SET active=0 WHERE guild=? AND category=? AND name=?",(i.guild.id,category.value,name.strip())); c.commit(); c.close()
    await i.response.send_message("Reward disabled." if cur.rowcount else "Reward not found.",ephemeral=True)

@bot.tree.command(name="approve-claim",description="Staff approve a verified ticket and issue a unique 7-digit code")
@app_commands.guild_only()
async def approve(i:discord.Interaction,member:discord.Member):
    if not i.guild or not staff(i): return await i.response.send_message("Only configured staff or admins can approve claims.",ephemeral=True)
    c=database(); t=c.execute("SELECT * FROM tickets WHERE channel=? AND guild=? AND user=? AND status='open'",(i.channel_id,i.guild.id,member.id)).fetchone()
    if not t or not t["reward"]: c.close(); return await i.response.send_message("Run this inside the member's open ticket after they choose a reward.",ephemeral=True)
    old=c.execute("SELECT status FROM claims WHERE guild=? AND user=? AND reward=?",(i.guild.id,member.id,t["reward"])).fetchone()
    if old: c.close(); return await i.response.send_message("This member already has a claim for this reward.",ephemeral=True)
    code=None
    for _ in range(30):
        candidate=f"{secrets.randbelow(10_000_000):07d}"
        try:
            c.execute("INSERT INTO claims(guild,user,reward,code,created) VALUES(?,?,?,?,?)",(i.guild.id,member.id,t["reward"],candidate,datetime.now(timezone.utc).isoformat())); c.commit(); code=candidate; break
        except sqlite3.IntegrityError: pass
    c.close()
    if not code: return await i.response.send_message("Could not generate a unique code; try again.",ephemeral=True)
    await i.response.send_message(f"Approved claim for {member.mention}.",ephemeral=True)
    e=discord.Embed(title="🎉 Your reward is approved!",description=f"**Reward:** {t['reward']}\n**Your code:** ||{code}||\n\n[Claim here]({SITE_URL})\n\nKeep this code private. It is unique to you and this reward.",color=0x57f287)
    try: await member.send(embed=e)
    except discord.Forbidden: await i.channel.send(f"{member.mention} I couldn't DM you. Your code is ||{code}||")
    await i.channel.send(embed=discord.Embed(title="💛 Leave a vouch",description=f"If you received your reward, please leave a vouch in the configured vouch channel. Example: Legit got {t['reward']} from TriggerPlayz.",color=0xf2b84b),view=CloseView())

@bot.tree.command(name="set-vouch-channel",description="Set the vouch channel")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def set_vouch(i:discord.Interaction,channel:discord.TextChannel):
    if not i.guild or not i.user.guild_permissions.administrator: return await i.response.send_message("Administrator only.",ephemeral=True)
    c=database(); c.execute("INSERT INTO settings(guild,vouch_channel) VALUES(?,?) ON CONFLICT(guild) DO UPDATE SET vouch_channel=excluded.vouch_channel",(i.guild.id,channel.id)); c.commit(); c.close()
    await i.response.send_message(f"Vouch channel set to {channel.mention}.",ephemeral=True)

@bot.tree.command(name="redeem-code",description="Staff permanently invalidate a code after redemption")
@app_commands.guild_only()
async def redeem(i:discord.Interaction,code:str):
    if not i.guild or not staff(i): return await i.response.send_message("Staff only.",ephemeral=True)
    if not re.fullmatch(r"\d{7}",code): return await i.response.send_message("Code must be exactly 7 digits.",ephemeral=True)
    c=database(); row=c.execute("SELECT * FROM claims WHERE guild=? AND code=?",(i.guild.id,code)).fetchone()
    if not row: c.close(); return await i.response.send_message("Code not found.",ephemeral=True)
    if row["status"]=="redeemed": c.close(); return await i.response.send_message("This code was already redeemed and is permanently invalid.",ephemeral=True)
    c.execute("UPDATE claims SET status='redeemed',redeemed=? WHERE id=?",(datetime.now(timezone.utc).isoformat(),row["id"])); c.commit(); c.close()
    await i.response.send_message(f"Code redeemed and invalidated. Reward: **{row['reward']}**.",ephemeral=True)

@bot.tree.command(name="close-ticket",description="Close your current reward ticket")
@app_commands.guild_only()
async def close_cmd(i:discord.Interaction):
    if not i.guild: return await i.response.send_message("Server only.",ephemeral=True)
    c=database(); t=c.execute("SELECT * FROM tickets WHERE channel=? AND status='open'",(i.channel_id,)).fetchone(); c.close()
    if not t: return await i.response.send_message("No open ticket found.",ephemeral=True)
    if i.user.id!=t["user"] and not staff(i): return await i.response.send_message("Only the owner or staff can close this ticket.",ephemeral=True)
    member=i.guild.get_member(t["user"])
    if member and isinstance(i.channel,discord.TextChannel): await i.channel.set_permissions(member,view_channel=False,send_messages=False)
    c=database(); c.execute("UPDATE tickets SET status='closed' WHERE channel=?",(i.channel_id,)); c.commit(); c.close()
    await i.response.send_message("Ticket closed.")

if not TOKEN: raise SystemExit("Missing DISCORD_BOT_TOKEN. Set it in private hosting environment variables.")
bot.run(TOKEN)
