import os, re, sqlite3, secrets, json, urllib.request, urllib.parse, urllib.error
from datetime import datetime, timezone
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
SITE_URL = os.getenv("DEMO_URL", "https://trigger-rewards.vercel.app").strip()
DB_FILE = os.getenv("TICKET_DB_PATH", "trigger_rewards.sqlite3")
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
SYNC_GUILD_ID = int(os.getenv("TRG_GUILD_ID", "1499228791220863110"))
intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)
CATS = {"youtube": ("▶️", "YouTube"), "minecraft": ("🟩", "Minecraft"), "streaming": ("📺", "OTT & Streaming")}

def sb_request(table, method="GET", params=None, payload=None):
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError("Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in Botkeep environment variables.")
    query = urllib.parse.urlencode(params or {}, doseq=True)
    url = f"{SUPABASE_URL}/rest/v1/{table}" + (f"?{query}" if query else "")
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method=method, headers={"apikey":SUPABASE_SERVICE_ROLE_KEY,"Authorization":f"Bearer {SUPABASE_SERVICE_ROLE_KEY}","Content-Type":"application/json","Prefer":"return=representation,resolution=merge-duplicates"})
    try:
        with urllib.request.urlopen(req, timeout=12) as response:
            raw=response.read().decode("utf-8")
            return json.loads(raw) if raw else []
    except urllib.error.HTTPError as e:
        raise RuntimeError("Supabase HTTP "+str(e.code)+": "+e.read().decode("utf-8","replace")[:250]) from e

def get_categories(guild_id):
    return sb_request("trigger_reward_categories",params={"select":"category_key,category_label,category_emoji,sort_order","guild_id":"eq."+str(guild_id),"active":"eq.true","order":"sort_order.asc,category_label.asc"})

def get_rewards(guild_id, category=None):
    p={"select":"category,category_label,category_emoji,reward_name,invite_requirement,sort_order","guild_id":"eq."+str(guild_id),"active":"eq.true","order":"sort_order.asc,reward_name.asc"}
    if category: p["category"]="eq."+category
    return sb_request("trigger_rewards_config",params=p)


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
        try:
            cats = get_categories(i.guild.id)
            if cats:
                await ch.send(content=i.user.mention, embed=e, view=Categories(cats))
            else:
                await ch.send(content=i.user.mention + " No reward categories are configured yet. Ask an administrator to use /category-add.")
        except Exception as ex:
            print("Could not load categories:", ex)
            await ch.send(content=i.user.mention + " Rewards database is not ready. Please contact an administrator.")
        await i.response.send_message(f"Your private ticket is ready: {ch.mention}",ephemeral=True)

class Categories(discord.ui.View):
    def __init__(self, categories):
        super().__init__(timeout=None)
        options = [discord.SelectOption(label=x["category_label"][:100], value=x["category_key"], emoji=x.get("category_emoji") or "🎁") for x in categories[:25]]
        select = discord.ui.Select(placeholder="Choose a reward category", custom_id="trg:category", options=options)
        select.callback = self.choose
        self.add_item(select)

    async def choose(self, i):
        c = database()
        ticket = c.execute("SELECT * FROM tickets WHERE channel=? AND status='open'", (i.channel_id,)).fetchone()
        if not ticket or ticket["user"] != i.user.id:
            c.close()
            return await i.response.send_message("Only the ticket owner can choose a category.", ephemeral=True)
        key = self.children[0].values[0]
        try:
            category = next((x for x in get_categories(i.guild.id) if x["category_key"] == key), None)
            rows = get_rewards(i.guild.id, key)
        except Exception as ex:
            c.close()
            return await i.response.send_message("Could not load rewards: " + str(ex)[:180], ephemeral=True)
        if not category:
            c.close()
            return await i.response.send_message("That category is no longer active.", ephemeral=True)
        c.execute("UPDATE tickets SET category=? WHERE channel=?", (key, i.channel_id))
        c.commit()
        c.close()
        if not rows:
            return await i.response.send_message("No rewards configured here yet. Ask staff to use /reward-add.", ephemeral=True)
        await i.response.send_message("Selected **" + category["category_label"] + "**. Now choose your reward.", view=RewardChoices(key, rows), ephemeral=True)

class RewardChoices(discord.ui.View):
    def __init__(self,cat,rows):
        super().__init__(timeout=300); self.cat=cat
        sel=discord.ui.Select(placeholder="Choose a reward",options=[discord.SelectOption(label=r["reward_name"][:100],value=r["reward_name"],description="Required invites: "+str(r.get("invite_requirement",0))) for r in rows[:25]])
        sel.callback=self.pick; self.add_item(sel)
    async def pick(self,i):
        name=i.data["values"][0]; c=database()
        t=c.execute("SELECT * FROM tickets WHERE channel=? AND status='open'",(i.channel_id,)).fetchone()
        try:
            r=next((x for x in get_rewards(i.guild.id,self.cat) if x["reward_name"]==name),None)
        except Exception as ex:
            c.close()
            return await i.response.send_message("Could not verify reward: "+str(ex)[:180],ephemeral=True)
        if not t or t["user"]!=i.user.id or not r:
            c.close(); return await i.response.send_message("Ticket/reward is no longer valid.",ephemeral=True)
        prior=c.execute("SELECT status FROM claims WHERE guild=? AND user=? AND reward=?",(i.guild.id,i.user.id,name)).fetchone()
        if prior:
            c.close(); return await i.response.send_message("You already claimed this reward. Duplicate claims are blocked.",ephemeral=True)
        c.execute("UPDATE tickets SET reward=? WHERE channel=?",(name,i.channel_id)); c.commit(); c.close()
        e=discord.Embed(title="📋 Invite Requirement",description=f"**Reward:** {name}\n**Required invites:** {r['invite_requirement']}\n\nRun your invite tracker bot's -i command, then wait for staff to verify the result in this ticket. Staff will use /approve-claim after checking the tracker output.",color=0x5865f2)
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
    bot.add_view(Panel()); bot.add_view(CloseView())
    try:
        guild = discord.Object(id=SYNC_GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        print(f"Ready as {bot.user}; synced {len(synced)} commands to guild {SYNC_GUILD_ID}")
    except Exception as e:
        print(f"Command sync error: {e}")


class CategoryAddModal(discord.ui.Modal, title="Add Reward Category"):
    key = discord.ui.TextInput(label="Category key", placeholder="youtube", max_length=32)
    label = discord.ui.TextInput(label="Display name", placeholder="YouTube", max_length=80)
    emoji = discord.ui.TextInput(label="Emoji", placeholder="▶️", required=False, max_length=16)
    async def on_submit(self, i: discord.Interaction):
        if not i.guild or not i.user.guild_permissions.administrator:
            return await i.response.send_message("Administrator only.", ephemeral=True)
        key = re.sub(r"[^a-z0-9_-]", "", str(self.key).lower().strip())
        if len(key) < 2:
            return await i.response.send_message("Use at least 2 letters/numbers for the category key.", ephemeral=True)
        try:
            sb_request("trigger_reward_categories", "POST", params={"on_conflict":"guild_id,category_key"}, payload={"guild_id":str(i.guild.id),"category_key":key,"category_label":str(self.label).strip(),"category_emoji":str(self.emoji).strip() or "🎁","active":True})
            await i.response.send_message(f"✅ Category **{str(self.label).strip()}** saved. Add rewards from this panel.", ephemeral=True, view=AdminPanel())
        except Exception as ex:
            await i.response.send_message("Could not save category: " + str(ex)[:180], ephemeral=True)

class RewardAddModal(discord.ui.Modal, title="Add Reward"):
    name = discord.ui.TextInput(label="Exact reward name", placeholder="Minecraft Rank", max_length=80)
    category = discord.ui.TextInput(label="Category key", placeholder="minecraft", max_length=32)
    invites = discord.ui.TextInput(label="Required invites", placeholder="10", max_length=4)
    async def on_submit(self, i: discord.Interaction):
        if not i.guild or not i.user.guild_permissions.administrator:
            return await i.response.send_message("Administrator only.", ephemeral=True)
        key = re.sub(r"[^a-z0-9_-]", "", str(self.category).lower().strip())
        try:
            count = int(str(self.invites).strip())
            if count < 0 or count > 1000: raise ValueError()
        except ValueError:
            return await i.response.send_message("Invite requirement must be a whole number from 0 to 1000.", ephemeral=True)
        try:
            category = next((x for x in get_categories(i.guild.id) if x["category_key"] == key), None)
            if not category:
                return await i.response.send_message("Category key not found. Add the category first.", ephemeral=True, view=AdminPanel())
            sb_request("trigger_rewards_config", "POST", params={"on_conflict":"guild_id,category,reward_name"}, payload={"guild_id":str(i.guild.id),"category":key,"category_label":category["category_label"],"category_emoji":category.get("category_emoji") or "🎁","reward_name":str(self.name).strip(),"invite_requirement":count,"active":True})
            await i.response.send_message(f"✅ Added **{str(self.name).strip()}** to **{category['category_label']}** — {count} invites required.", ephemeral=True, view=AdminPanel())
        except Exception as ex:
            await i.response.send_message("Could not save reward: " + str(ex)[:180], ephemeral=True)

class AdminPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
    async def interaction_check(self, i: discord.Interaction):
        if not i.guild or not i.user.guild_permissions.administrator:
            await i.response.send_message("🔒 Server administrators only.", ephemeral=True)
            return False
        return True
    @discord.ui.button(label="Add Category", style=discord.ButtonStyle.primary, emoji="🗂️", row=0)
    async def add_category(self, i: discord.Interaction, b: discord.ui.Button):
        await i.response.send_modal(CategoryAddModal())
    @discord.ui.button(label="Add Reward", style=discord.ButtonStyle.success, emoji="🎁", row=0)
    async def add_reward(self, i: discord.Interaction, b: discord.ui.Button):
        await i.response.send_modal(RewardAddModal())
    @discord.ui.button(label="View Setup", style=discord.ButtonStyle.secondary, emoji="📊", row=0)
    async def view_setup(self, i: discord.Interaction, b: discord.ui.Button):
        try:
            cats = get_categories(i.guild.id)
            rewards = get_rewards(i.guild.id)
            e = discord.Embed(title="📊 Trigger Rewards Configuration", color=0xf2b84b)
            e.add_field(name=f"Categories ({len(cats)})", value="\\n".join(f"{x.get('category_emoji','🎁')} **{x['category_label']}** · {x['category_key']}" for x in cats[:15]) or "No categories yet.", inline=False)
            e.add_field(name=f"Active rewards ({len(rewards)})", value="\\n".join(f"• **{x['reward_name']}** — {x['invite_requirement']} invites ({x['category']})" for x in rewards[:20]) or "No rewards yet.", inline=False)
            e.set_footer(text="Use buttons to add more. /category-remove and /reward-remove can hide entries.")
            await i.response.send_message(embed=e, ephemeral=True, view=AdminPanel())
        except Exception as ex:
            await i.response.send_message("Could not read configuration: " + str(ex)[:180], ephemeral=True)
    @discord.ui.button(label="Ticket Setup Help", style=discord.ButtonStyle.secondary, emoji="🎟️", row=1)
    async def ticket_help(self, i: discord.Interaction, b: discord.ui.Button):
        e = discord.Embed(title="🎟️ Ticket Setup", description="1. Run /ticket-setup and select the public channel.\\n2. Optionally choose a Discord category for new ticket channels.\\n3. Run /staff-role to set who can view tickets.\\n4. Add categories and rewards here.\\n5. Test by opening a ticket as a regular member.", color=0xf2b84b)
        await i.response.send_message(embed=e, ephemeral=True, view=AdminPanel())

@bot.tree.command(name="admin-panel", description="Open the embedded Trigger Rewards admin control panel")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def admin_panel(i: discord.Interaction):
    if not i.guild or not i.user.guild_permissions.administrator:
        return await i.response.send_message("🔒 Server administrators only.", ephemeral=True)
    e = discord.Embed(title="⚙️ TRIGGER REWARDS • ADMIN CONTROL", description="Manage reward categories, invite requirements and ticket setup directly inside Discord. No admin website required.\\n\\nCategories — create a category key, label and emoji.\\nRewards — add a reward with its exact name and invite requirement.\\nView Setup — inspect active categories and rewards.", color=0xf2b84b)
    e.set_footer(text="Admin-only controls • Changes sync to the public rewards website")
    await i.response.send_message(embed=e, view=AdminPanel(), ephemeral=True)

@bot.tree.command(name="ticket-setup",description="Post the Trigger Rewards ticket panel")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def ticket_setup(i:discord.Interaction,channel:discord.TextChannel,category:discord.CategoryChannel=None):
    if not i.guild or not i.user.guild_permissions.administrator: return await i.response.send_message("Administrator only.",ephemeral=True)
    c=database(); c.execute("INSERT INTO settings(guild,ticket_category) VALUES(?,?) ON CONFLICT(guild) DO UPDATE SET ticket_category=excluded.ticket_category",(i.guild.id,category.id if category else 0)); c.commit(); c.close()
    e=discord.Embed(title="🎁 TRIGGER REWARDS",description="Claim rewards through a private ticket.\n\n1. Open a ticket.\n2. Choose a category configured by server staff.\n3. Select a reward and complete its invite requirement.\n4. Staff verify the tracker result and issue a unique code.\n\nEach user can claim each reward once. Codes are single-use and become invalid after redemption. Never share passwords or tokens.",color=0xf2b84b)
    e.set_footer(text="Trigger Rewards • Official reward support")
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

@bot.tree.command(name="category-add",description="Create or update a website reward category")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def category_add(i:discord.Interaction,key:app_commands.Range[str,2,32],label:app_commands.Range[str,2,80],emoji:str="🎁"):
    if not i.guild or not i.user.guild_permissions.administrator:
        return await i.response.send_message("Administrator only.",ephemeral=True)
    key=re.sub(r"[^a-z0-9_-]","",key.lower().strip())
    if not key:
        return await i.response.send_message("Use letters, numbers, hyphens or underscores for the key.",ephemeral=True)
    try:
        sb_request("trigger_reward_categories","POST",params={"on_conflict":"guild_id,category_key"},payload={"guild_id":str(i.guild.id),"category_key":key,"category_label":label.strip(),"category_emoji":emoji.strip()[:16] or "🎁","active":True})
        await i.response.send_message("Saved category **"+label.strip()+"**. Use /reward-add to add its rewards.",ephemeral=True)
    except Exception as ex:
        await i.response.send_message("Could not save category: "+str(ex)[:220],ephemeral=True)

@bot.tree.command(name="category-remove",description="Hide a category and its rewards from the website")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def category_remove(i:discord.Interaction,key:app_commands.Range[str,2,32]):
    if not i.guild or not i.user.guild_permissions.administrator:
        return await i.response.send_message("Administrator only.",ephemeral=True)
    key=re.sub(r"[^a-z0-9_-]","",key.lower().strip())
    try:
        sb_request("trigger_reward_categories","PATCH",params={"guild_id":f"eq.{i.guild.id}","category_key":f"eq.{key}"},payload={"active":False})
        sb_request("trigger_rewards_config","PATCH",params={"guild_id":f"eq.{i.guild.id}","category":f"eq.{key}"},payload={"active":False})
        await i.response.send_message("Category **"+key+"** and its rewards are hidden.",ephemeral=True)
    except Exception as ex:
        await i.response.send_message("Could not remove category: "+str(ex)[:220],ephemeral=True)

@bot.tree.command(name="reward-add",description="Add a website reward with its exact name and invite requirement")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def reward_add(i:discord.Interaction,category_key:app_commands.Range[str,2,32],name:app_commands.Range[str,2,80],invites:app_commands.Range[int,0,1000]):
    if not i.guild or not i.user.guild_permissions.administrator:
        return await i.response.send_message("Administrator only.",ephemeral=True)
    key=re.sub(r"[^a-z0-9_-]","",category_key.lower().strip())
    try:
        category=next((x for x in get_categories(i.guild.id) if x["category_key"]==key),None)
        if not category:
            return await i.response.send_message("Category not found. Run /category-add first.",ephemeral=True)
        sb_request("trigger_rewards_config","POST",params={"on_conflict":"guild_id,category,reward_name"},payload={"guild_id":str(i.guild.id),"category":key,"category_label":category["category_label"],"category_emoji":category.get("category_emoji") or "🎁","reward_name":name.strip(),"invite_requirement":int(invites),"active":True})
        await i.response.send_message("Added **"+name.strip()+"** under **"+category["category_label"]+"** with **"+str(invites)+"** required invites.",ephemeral=True)
    except Exception as ex:
        await i.response.send_message("Could not save reward: "+str(ex)[:220],ephemeral=True)

@bot.tree.command(name="reward-remove",description="Hide a website reward")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def reward_remove(i:discord.Interaction,category_key:app_commands.Range[str,2,32],name:app_commands.Range[str,2,80]):
    if not i.guild or not i.user.guild_permissions.administrator:
        return await i.response.send_message("Administrator only.",ephemeral=True)
    key=re.sub(r"[^a-z0-9_-]","",category_key.lower().strip())
    try:
        rows=sb_request("trigger_rewards_config","PATCH",params={"guild_id":f"eq.{i.guild.id}","category":f"eq.{key}","reward_name":f"eq.{name.strip()}"},payload={"active":False})
        await i.response.send_message("Reward hidden." if rows else "Reward not found.",ephemeral=True)
    except Exception as ex:
        await i.response.send_message("Could not remove reward: "+str(ex)[:220],ephemeral=True)

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
