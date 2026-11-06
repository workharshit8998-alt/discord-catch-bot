import os, re, sqlite3, secrets, json, asyncio, urllib.request, urllib.parse, urllib.error
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
VOUCH_MESSAGE_CHANNEL_ID = 1499228791891824824  # vouch embed channel
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
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




def sb_rpc(function_name, payload):
    """Call a SECURITY DEFINER RPC using the bot-only Supabase service role."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError("Supabase service-role configuration is missing.")
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/rpc/{function_name}",
        data=body, method="POST",
        headers={"apikey":SUPABASE_SERVICE_ROLE_KEY,
                 "Authorization":f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                 "Content-Type":"application/json",
                 "Accept":"application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raise RuntimeError("Supabase delivery RPC HTTP "+str(e.code)+": "+e.read().decode("utf-8","replace")[:220]) from e


async def deliver_real_reward(guild, claim_id, actor_label="admin"):
    """Atomically reserve stock, DM it, then record the result. Never expose stock in a channel."""
    reserved = sb_rpc("trg_reserve_reward_delivery", {
        "p_claim_id": str(claim_id), "p_guild_id": str(guild.id),
        "p_actor_discord_id": str(actor_label)
    })
    if isinstance(reserved, list):
        reserved = reserved[0] if reserved else None
    if not isinstance(reserved, dict) or not reserved.get("inventory_id"):
        raise RuntimeError("No matching stock is available, or this claim is no longer pending.")
    inventory_id = reserved["inventory_id"]
    discord_id = int(reserved["discord_user_id"])
    secret = str(reserved["secret_payload"])
    reward_name = str(reserved["reward_name"])
    try:
        user = await bot.fetch_user(discord_id)
        embed = discord.Embed(
            title="🎁 Your Trigger Rewards prize is here!",
            description=f"**Reward:** {reward_name}\n\n**Your reward code:**\n||{secret}||\n\nKeep this code private. Do not share passwords, account tokens, or 2FA codes.",
            color=0xf2b84b
        )
        embed.set_footer(text="Trigger Rewards • Private delivery")
        dm = await user.send(embed=embed)
    except Exception as ex:
        # The DM did not succeed, so releasing the reserved item is safe.
        try:
            sb_rpc("trg_finish_reward_delivery", {
                "p_claim_id": str(claim_id), "p_inventory_id": str(inventory_id),
                "p_message_id": None, "p_success": False,
                "p_error": type(ex).__name__[:80]
            })
        except Exception as record_error:
            print("Could not record failed reward delivery:", repr(record_error))
        if isinstance(ex, discord.Forbidden):
            raise RuntimeError(f"Could not DM <@{discord_id}> (DMs may be closed). Stock was returned to inventory.") from ex
        raise RuntimeError(f"DM failed for <@{discord_id}>: {type(ex).__name__}.") from ex

    # Discord accepted the DM. If finalization fails, leave stock locked in sending
    # and do NOT release/retry it automatically: that avoids delivering the same code twice.
    try:
        sb_rpc("trg_finish_reward_delivery", {
            "p_claim_id": str(claim_id), "p_inventory_id": str(inventory_id),
            "p_message_id": str(dm.id), "p_success": True, "p_error": None
        })
    except Exception as ex:
        print("DM succeeded but delivery finalization failed; inventory remains locked:", repr(ex))
        raise RuntimeError("Discord accepted the DM, but Supabase could not confirm it. Do not retry this claim until an admin checks delivery state.") from ex
    ticket_channel_id = reserved.get("source_ticket_channel_id")
    if ticket_channel_id:
        ticket_channel = guild.get_channel(int(ticket_channel_id))
        if isinstance(ticket_channel, discord.TextChannel):
            try:
                await ticket_channel.send(
                    f"✅ The actual **{reward_name}** code was sent privately to <@{discord_id}>. "
                    "If you received it, reply **Legit** here to submit your vouch."
                )
            except Exception as ticket_error:
                print("Could not post delivery confirmation in ticket:", repr(ticket_error))
    return f"✅ Delivered **{reward_name}** to <@{discord_id}>."


def get_pending_real_claims(guild_id):
    return sb_request("trg_reward_claims", params={
        "select":"id,discord_user_id,discord_username,reward_name,status,requested_at",
        "guild_id":"eq."+str(guild_id),
        "status":"in.(pending,delivery_failed)",
        "order":"requested_at.asc",
        "limit":"25"
    })



def get_real_delivery_history(guild_id):
    return sb_request("trg_reward_deliveries", params={
        "select":"id,status,discord_user_id,discord_message_id,initiated_by_discord_id,attempts,created_at,sent_at,claim:trg_reward_claims(reward_name,discord_username)",
        "claim.guild_id":"eq."+str(guild_id),
        "order":"created_at.desc",
        "limit":"15"
    })


class AddRewardStockModal(discord.ui.Modal, title="Add Real Reward Stock"):
    category = discord.ui.TextInput(label="Category key", placeholder="minecraft", max_length=32)
    reward = discord.ui.TextInput(label="Exact reward name", placeholder="Minecraft Rank", max_length=120)
    region = discord.ui.TextInput(label="Region (AS / AU)", default="AS", max_length=8)
    secret = discord.ui.TextInput(label="Actual reward code", style=discord.TextStyle.paragraph, max_length=500)
    async def on_submit(self, i: discord.Interaction):
        if not i.guild or not i.user.guild_permissions.administrator:
            return await i.response.send_message("Administrator only.", ephemeral=True)
        category_key = re.sub(r"[^a-z0-9_-]", "", str(self.category).lower().strip())
        reward_name = str(self.reward).strip()
        region_value = str(self.region).strip().upper()
        secret_value = str(self.secret).strip()
        if not category_key or not reward_name or not secret_value or region_value not in {"AS", "AU"}:
            return await i.response.send_message("Enter a category, exact reward name, AS/AU region, and a real reward code.", ephemeral=True)
        try:
            matches = get_rewards(i.guild.id, category_key)
            if not any(x["reward_name"] == reward_name for x in matches):
                return await i.response.send_message("That exact reward is not configured in this category. Add it first, then add stock.", ephemeral=True)
            sb_request("trg_reward_inventory", "POST", payload={
                "guild_id":str(i.guild.id),
                "reward_key":category_key+":"+reward_name,
                "reward_name":reward_name,
                "region":region_value,
                "secret_payload":secret_value,
                "status":"available"
            })
            await i.response.send_message("✅ One real reward code was added to secure inventory. It is hidden from the website and will only be sent by DM to an eligible claimant.", ephemeral=True)
        except Exception as ex:
            await i.response.send_message("Could not add stock: "+str(ex)[:180], ephemeral=True)


class PendingClaimsSelect(discord.ui.Select):
    def __init__(self, claims):
        self.claims = claims
        options = []
        for row in claims:
            username = (row.get("discord_username") or "Discord member")[:45]
            label = f"{username} · {row.get('reward_name','Reward')}"[:100]
            status = row.get("status", "pending")
            options.append(discord.SelectOption(
                label=label,
                value=str(row["id"]),
                description=("Retry failed delivery" if status == "delivery_failed" else "Pending reward delivery")[:100]
            ))
        super().__init__(placeholder="Select pending claimants (multiple allowed)",
                         min_values=1, max_values=len(options), options=options)
    async def callback(self, i: discord.Interaction):
        self.view.selected_claim_ids = list(self.values)
        await i.response.send_message(f"Selected **{len(self.values)}** pending claim(s). Press Deliver Selected to send their matching rewards by DM.", ephemeral=True)


class DeliveryQueueView(discord.ui.View):
    def __init__(self, claims):
        super().__init__(timeout=300)
        self.selected_claim_ids = []
        self.claims = claims
        if claims:
            self.add_item(PendingClaimsSelect(claims))
    async def interaction_check(self, i: discord.Interaction):
        if not i.guild or not i.user.guild_permissions.administrator:
            await i.response.send_message("🔒 Server administrators only.", ephemeral=True)
            return False
        return True
    @discord.ui.button(label="Deliver Selected", style=discord.ButtonStyle.success, emoji="📨", row=1)
    async def deliver_selected(self, i: discord.Interaction, button: discord.ui.Button):
        if not self.selected_claim_ids:
            return await i.response.send_message("Select at least one pending claimant first.", ephemeral=True)
        await i.response.defer(ephemeral=True, thinking=True)
        results = []
        for claim_id in self.selected_claim_ids[:25]:
            try:
                results.append(await deliver_real_reward(i.guild, claim_id, str(i.user.id)))
            except Exception as ex:
                results.append("⚠️ "+str(ex)[:180])
        summary = "\n".join(results)
        await i.followup.send(summary[:1900] or "No deliveries were processed.", ephemeral=True)



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
    # Migrate older databases without losing existing settings.
    settings_columns = {row["name"] for row in c.execute("PRAGMA table_info(settings)").fetchall()}
    if "auto_delivery" not in settings_columns:
        c.execute("ALTER TABLE settings ADD COLUMN auto_delivery INTEGER NOT NULL DEFAULT 0")
        c.commit()
    if "vouch_template" not in settings_columns:
        c.execute("ALTER TABLE settings ADD COLUMN vouch_template TEXT NOT NULL DEFAULT 'Legit got {reward} from @TriggerPlayz'")
        c.commit()
    # Migrate older databases: users may claim the same reward again after its cooldown.
    schema = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='claims'").fetchone()
    if schema and "UNIQUE(guild,user,reward)" in (schema["sql"] or "").replace(" ", ""):
        c.executescript("""
        ALTER TABLE claims RENAME TO claims_old;
        CREATE TABLE claims(id INTEGER PRIMARY KEY, guild INTEGER, user INTEGER, reward TEXT, code TEXT UNIQUE, status TEXT DEFAULT 'issued', created TEXT, redeemed TEXT);
        INSERT INTO claims(id,guild,user,reward,code,status,created,redeemed)
          SELECT id,guild,user,reward,code,status,created,redeemed FROM claims_old;
        DROP TABLE claims_old;
        """)
        c.commit()
    c.execute("""CREATE TABLE IF NOT EXISTS reward_cooldowns(
        guild INTEGER NOT NULL, user INTEGER NOT NULL, reward TEXT NOT NULL,
        last_claim TEXT NOT NULL, PRIMARY KEY(guild,user,reward)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS invite_baselines(
        guild INTEGER NOT NULL, user INTEGER NOT NULL, baseline INTEGER NOT NULL DEFAULT 0,
        updated TEXT NOT NULL, PRIMARY KEY(guild,user)
    )""")
    c.commit()
    return c

DEFAULT_VOUCH_TEMPLATE = "Legit got {reward} from @TriggerPlayz"

def get_vouch_template(guild_id):
    c = database()
    row = c.execute("SELECT vouch_template FROM settings WHERE guild=?", (guild_id,)).fetchone()
    c.close()
    return (row["vouch_template"] if row and row["vouch_template"] else DEFAULT_VOUCH_TEMPLATE)

def save_vouch_template(guild_id, template):
    c = database()
    c.execute(
        "INSERT INTO settings(guild, vouch_template) VALUES(?, ?) "
        "ON CONFLICT(guild) DO UPDATE SET vouch_template=excluded.vouch_template",
        (guild_id, template)
    )
    c.commit()
    c.close()

def cooldown_seconds(c, guild_id, user_id, reward_name):
    row = c.execute(
        "SELECT last_claim FROM reward_cooldowns WHERE guild=? AND user=? AND reward=?",
        (guild_id, user_id, reward_name)
    ).fetchone()
    # Honour recent claims created before this cooldown feature was added.
    last_value = row["last_claim"] if row else None
    if not last_value:
        old_claim = c.execute(
            "SELECT created FROM claims WHERE guild=? AND user=? AND reward=? ORDER BY id DESC LIMIT 1",
            (guild_id, user_id, reward_name)
        ).fetchone()
        last_value = old_claim["created"] if old_claim else None
    if not last_value:
        return 0
    try:
        last = datetime.fromisoformat(last_value)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return max(0, 120 - int((datetime.now(timezone.utc) - last).total_seconds()))
    except (TypeError, ValueError):
        return 0

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
        remaining = cooldown_seconds(c, i.guild.id, i.user.id, name)
        if remaining:
            c.close()
            return await i.response.send_message(
                f"⏳ You can claim **{name}** again in **{remaining // 60}:{remaining % 60:02d}**. "
                "Each reward has its own 2-minute cooldown.",
                ephemeral=True
            )
        c.execute("UPDATE tickets SET reward=? WHERE channel=?",(name,i.channel_id)); c.commit(); c.close()
        e=discord.Embed(title="📋 Invite Requirement",description=f"**Reward:** {name}\n**Required invites:** {r['invite_requirement']}\n\nRun your invite tracker bot's -i command, then wait for staff to verify the result in this ticket. Staff will use /approve-claim after checking the tracker output.",color=0x5865f2)
        e.set_footer(text="Verification is staff-checked for now; no invite count is guessed.")
        await i.response.send_message(embed=e,view=CloseView())


async def auto_verify_falcon_report(message):
    """Verify Falcon -i output in a reward ticket, then remove the ticket."""
    if not message.guild or not isinstance(message.channel, discord.TextChannel):
        return
    if not message.author.bot or not message.embeds:
        return
    embed = message.embeds[0]
    if (embed.title or "").strip().lower() != "invite log":
        return
    match = re.search(r"has\s+(\d+)\s+invites", embed.description or "", re.I)
    requested = re.search(r"Requested by\s+(.+?)(?:\s*\||$)", embed.footer.text or "", re.I)
    if not match or not requested:
        return
    invites = int(match.group(1))
    requester_name = requested.group(1).strip().lstrip("@").lower()
    c = database()
    t = c.execute("SELECT * FROM tickets WHERE channel=? AND guild=? AND status='open'",
                  (message.channel.id, message.guild.id)).fetchone()
    c.close()
    if not t or not t["reward"] or not t["category"]:
        return
    member = message.guild.get_member(t["user"])
    if not member:
        return
    # Only trust a report that identifies the actual ticket owner.
    accepted_names = {member.name.lower(), member.display_name.lower()}
    if requester_name not in accepted_names:
        return
    # Compare Falcon's cumulative total with the last successful reward baseline.
    c = database()
    remaining = cooldown_seconds(c, message.guild.id, member.id, t["reward"])
    baseline_row = c.execute(
        "SELECT baseline FROM invite_baselines WHERE guild=? AND user=?",
        (message.guild.id, member.id)
    ).fetchone()
    baseline = int(baseline_row["baseline"]) if baseline_row else 0
    new_invites = max(0, invites - baseline)
    c.close()
    try:
        rewards = get_rewards(message.guild.id, t["category"])
        reward = next((r for r in rewards if r["reward_name"] == t["reward"]), None)
        if not reward:
            await message.channel.send("⚠️ I couldn't find this reward's current invite requirement. Staff must check this ticket.")
            return
        required = int(reward.get("invite_requirement") or 0)
        if remaining:
            await message.channel.send(
                f"⏳ This reward is on cooldown for this member. Try again in "
                f"**{remaining // 60}:{remaining % 60:02d}**."
            )
            return
        if new_invites < required:
            await message.channel.send(
                f"❌ **Invite verification failed.** Falcon's total is **{invites}**; "
                f"your last reward baseline is **{baseline}**, so you have **{new_invites} new invites**. "
                f"**{required} new invites** are required. This ticket will be deleted in 15 seconds."
            )
        else:
            c = database()
            code = None
            for _ in range(30):
                candidate = "".join(secrets.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789") for _ in range(12))
                try:
                    now_iso = datetime.now(timezone.utc).isoformat()
                    c.execute("INSERT INTO claims(guild,user,reward,code,created) VALUES(?,?,?,?,?)",
                              (message.guild.id, member.id, t["reward"], candidate, now_iso))
                    c.execute(
                        "INSERT INTO reward_cooldowns(guild,user,reward,last_claim) VALUES(?,?,?,?) "
                        "ON CONFLICT(guild,user,reward) DO UPDATE SET last_claim=excluded.last_claim",
                        (message.guild.id, member.id, t["reward"], now_iso)
                    )
                    c.commit()
                    code = candidate
                    break
                except sqlite3.IntegrityError:
                    continue
            c.close()
            if not code:
                await message.channel.send("⚠️ Couldn't generate a unique reward code. Staff must check this ticket.")
                return
            try:
                sb_request("trigger_reward_valid_codes", "POST", payload={
                    "guild_id": str(message.guild.id), "category": t["category"],
                    "reward_name": t["reward"], "code_value": code, "active": True
                })
            except Exception:
                c = database()
                c.execute("DELETE FROM claims WHERE guild=? AND user=? AND reward=? AND code=?",
                          (message.guild.id, member.id, t["reward"], code))
                c.commit()
                c.close()
                await message.channel.send("⚠️ Code registration failed, so no code was issued. Staff must check this ticket.")
                return
            # Create the shared, server-only pending claim. This is the only source shown in the DM delivery queue.
            try:
                already_created = sb_request("trg_reward_claims", params={
                    "select":"id,status",
                    "source_ticket_channel_id":"eq."+str(message.channel.id),
                    "limit":"1"
                })
                if already_created:
                    raise RuntimeError("This ticket already has a real-reward claim; duplicate invite-log event ignored.")
                pending = sb_request("trg_reward_claims", "POST", payload={
                    "guild_id":str(message.guild.id),
                    "discord_user_id":str(member.id),
                    "discord_username":member.display_name[:100],
                    "reward_key":t["category"]+":"+t["reward"],
                    "reward_name":t["reward"],
                    "source_ticket_channel_id":str(message.channel.id),
                    "status":"pending"
                })
                if not pending:
                    raise RuntimeError("Supabase did not return the pending claim.")
            except Exception as ex:
                # Do not leave a website verification code active if the real-delivery claim failed to persist.
                try:
                    sb_request("trigger_reward_valid_codes", "PATCH", params={
                        "guild_id":"eq."+str(message.guild.id), "code_value":"eq."+code
                    }, payload={"active":False})
                except Exception:
                    pass
                c = database()
                c.execute("DELETE FROM claims WHERE guild=? AND user=? AND reward=? AND code=?", (message.guild.id, member.id, t["reward"], code))
                c.commit(); c.close()
                await message.channel.send("⚠️ Your invite check passed, but the shared delivery claim could not be saved. No reward code was issued. Staff must check Supabase.")
                return
            claim_id = pending[0]["id"] if isinstance(pending, list) else pending.get("id")
            # Consume only the invite requirement for this reward, not the entire cumulative total.
            # This preserves any surplus invites for the member's next claim.
            c = database()
            next_baseline = baseline + required
            c.execute(
                "INSERT INTO invite_baselines(guild,user,baseline,updated) VALUES(?,?,?,?) "
                "ON CONFLICT(guild,user) DO UPDATE SET baseline=excluded.baseline, updated=excluded.updated",
                (message.guild.id, member.id, next_baseline, datetime.now(timezone.utc).isoformat())
            )
            c.commit()
            c.close()
            dm = discord.Embed(
                title="🎟️ Your website verification code is ready!",
                description=(
                    f"**Reward:** {t['reward']}\n"
                    f"**Website verification code (not the actual prize):** ||{code}||\n\n"
                    f"[Claim here]({SITE_URL})\n\n"
                    "Keep this verification code private. Your actual reward code is delivered separately by the bot after staff delivery."
                ),
                color=0x57f287
            )
            # The 12-character website code remains the legacy verification/timer code.
            # Actual reward stock is sent separately only through the secure delivery workflow.
            await message.channel.send(embed=dm)
            try:
                await member.send(embed=dm)
            except discord.Forbidden:
                await message.channel.send(f"{member.mention} I couldn't DM the website verification code. Please enable DMs and ask staff for help.")
            # Optional automatic real-reward delivery. Disabled by default; admins enable it explicitly.
            try:
                if cfg(message.guild.id).get("auto_delivery") and claim_id:
                    result = await deliver_real_reward(message.guild, claim_id, "auto")
                    await message.channel.send(result)
            except Exception as delivery_error:
                await message.channel.send("⚠️ Automatic real-reward delivery did not complete: "+str(delivery_error)[:180]+" Use /admin-panel → Pending DM Deliveries to retry.")
            await message.channel.send(
                "✅ Invite requirement verified. Your real reward is now pending delivery. "
                "The bot will ask for your vouch only after the actual reward code has been sent by DM."
            )
            # Keep successful tickets open so the member can say "Legit"; evidence is captured then.
            return
        await asyncio.sleep(15)
        c = database()
        c.execute("UPDATE tickets SET status='closed' WHERE channel=?", (message.channel.id,))
        c.commit()
        c.close()
        try:
            await message.channel.delete(reason="Automatic invite verification completed")
        except discord.Forbidden:
            await message.channel.send("⚠️ Verification finished, but I can't delete this ticket. Give the bot Manage Channels permission.")
    except Exception as ex:
        print("Automatic invite verification failed:", repr(ex))
        try:
            await message.channel.send("⚠️ Automatic verification hit an error. Staff must check this ticket; it will not be deleted.")
        except Exception:            pass


async def capture_legit_evidence(message):
    """Post a text-only vouch after the ticket owner confirms a delivered reward."""
    if not message.guild or not isinstance(message.channel, discord.TextChannel):
        return
    if message.author.bot or message.content.strip().casefold() not in {"legit", "legit!", "legit."}:
        return

    c = database()
    ticket = c.execute(
        "SELECT * FROM tickets WHERE channel=? AND guild=? AND status='open'",
        (message.channel.id, message.guild.id)
    ).fetchone()
    if not ticket or int(ticket["user"]) != message.author.id or not ticket["reward"]:
        c.close()
        return
    local_claim = c.execute(
        "SELECT id FROM claims WHERE guild=? AND user=? AND reward=? AND status='issued' ORDER BY id DESC LIMIT 1",
        (message.guild.id, message.author.id, ticket["reward"])
    ).fetchone()
    c.close()
    if not local_claim:
        return

    # A public vouch is accepted only after Supabase confirms this user's real delivery.
    try:
        delivered_claim = sb_request("trg_reward_claims", params={
            "select":"id,reward_name,discord_user_id",
            "source_ticket_channel_id":"eq."+str(message.channel.id),
            "discord_user_id":"eq."+str(message.author.id),
            "status":"eq.delivered",
            "limit":"1"
        })
        if not delivered_claim:
            await message.channel.send(
                "⏳ Your reward delivery hasn't been confirmed yet. Please wait for your private DM, then reply **Legit**."
            )
            return
    except Exception:
        await message.channel.send(
            "⚠️ I couldn't verify the delivery, so your vouch wasn't posted. Please ask staff to check."
        )
        return

    # Mark this ticket before network sends to prevent duplicate vouch posts.
    c = database()
    c.execute("UPDATE tickets SET status='legit_received' WHERE channel=?", (message.channel.id,))
    c.commit()
    c.close()

    try:
        vouch_target = message.guild.get_channel(VOUCH_MESSAGE_CHANNEL_ID)
        if not isinstance(vouch_target, discord.TextChannel):
            vouch_target = await message.guild.fetch_channel(VOUCH_MESSAGE_CHANNEL_ID)

        template = get_vouch_template(message.guild.id)
        rendered = template.format(
            reward=discord.utils.escape_markdown(ticket["reward"]),
            user=message.author.mention,
            ticket=discord.utils.escape_markdown(message.channel.name)
        )
        embed = discord.Embed(
            title="✅ Reward Vouch Received",
            description=(
                f"**Member:** {message.author.mention}\n"
                f"**Reward:** {discord.utils.escape_markdown(ticket['reward'])}\n"
                f"**Ticket:** #{discord.utils.escape_markdown(message.channel.name)}\n"
                f"**Vouch:** Legit\\n\\n"
                f"**Vouch message:** {rendered[:1800]}\\n\\n"
                "Must Verify here to get reward https://discord.com/channels/1499228791220863110/1499322272861913169"
            ),
            color=0xf2b84b
        )
        embed.set_footer(text="Trigger Rewards • Verified delivery")
        await vouch_target.send(embed=embed)

        await message.channel.send(
            f"✅ Thanks! Your vouch has been posted in {vouch_target.mention}. This ticket will close in 10 seconds."
        )
        await asyncio.sleep(10)
        c = database()
        c.execute("UPDATE tickets SET status='closed' WHERE channel=?", (message.channel.id,))
        c.commit()
        c.close()
        try:
            await message.channel.delete(reason="Verified reward vouch posted")
        except discord.Forbidden:
            pass
    except Exception as ex:
        print("Could not post verified reward vouch:", repr(ex))
        c = database()
        c.execute("UPDATE tickets SET status='open' WHERE channel=?", (message.channel.id,))
        c.commit()
        c.close()
        try:
            await message.channel.send(
                "⚠️ I couldn't post your vouch. Please check the bot's permissions in the vouch channel."
            )
        except Exception:
            pass


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
async def on_message(message):
    await auto_verify_falcon_report(message)
    await capture_legit_evidence(message)
    await bot.process_commands(message)

@bot.command(name="sync")
@commands.guild_only()
@commands.has_guild_permissions(administrator=True)
async def sync_commands(ctx):
    guild = discord.Object(id=ctx.guild.id)
    try:
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        await ctx.reply(f"✅ Synced **{len(synced)}** slash command(s) to **{ctx.guild.name}**.", mention_author=False)
    except Exception as ex:
        await ctx.reply(f"❌ Command sync failed: {type(ex).__name__}: {str(ex)[:180]}", mention_author=False)

@sync_commands.error
async def sync_commands_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.reply("🔒 Only server administrators can use !sync.", mention_author=False)
    elif isinstance(error, commands.NoPrivateMessage):
        return
    else:
        await ctx.reply("❌ I couldn't run !sync. Check the bot logs for details.", mention_author=False)


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


class CategoryEditModal(discord.ui.Modal, title="Edit Reward Category"):
    key = discord.ui.TextInput(label="Existing category key", placeholder="youtube", max_length=32)
    label = discord.ui.TextInput(label="New display name", placeholder="YouTube Reward", max_length=80)
    emoji = discord.ui.TextInput(label="New emoji", placeholder="▶️", required=False, max_length=16)
    async def on_submit(self, i: discord.Interaction):
        if not i.guild or not i.user.guild_permissions.administrator:
            return await i.response.send_message("Administrator only.", ephemeral=True)
        key = re.sub(r"[^a-z0-9_-]", "", str(self.key).lower().strip())
        label = str(self.label).strip()
        try:
            cats = get_categories(i.guild.id)
            cat = next((x for x in cats if x["category_key"] == key), None)
            if not cat: return await i.response.send_message("Category key not found.", ephemeral=True, view=AdminPanel())
            emoji = str(self.emoji).strip() or cat.get("category_emoji") or "🎁"
            sb_request("trigger_reward_categories", "PATCH", params={"guild_id":f"eq.{i.guild.id}","category_key":f"eq.{key}"}, payload={"category_label":label,"category_emoji":emoji,"active":True})
            sb_request("trigger_rewards_config", "PATCH", params={"guild_id":f"eq.{i.guild.id}","category":f"eq.{key}"}, payload={"category_label":label,"category_emoji":emoji})
            await i.response.send_message(f"✅ Updated **{label}**. Website category and its reward labels are synced.", ephemeral=True, view=AdminPanel())
        except Exception as ex:
            await i.response.send_message("Could not edit category: " + str(ex)[:180], ephemeral=True)

class RewardEditModal(discord.ui.Modal, title="Edit Reward & Requirements"):
    category = discord.ui.TextInput(label="Category key", placeholder="youtube", max_length=32)
    current_name = discord.ui.TextInput(label="Current exact reward name", placeholder="10K YouTube Subscribers Account", max_length=80)
    new_name = discord.ui.TextInput(label="New reward name", placeholder="10K Subscribers", max_length=80)
    invites = discord.ui.TextInput(label="Required invites (0-1000)", placeholder="3", max_length=4)
    details = discord.ui.TextInput(label="Reward text / code / notes", placeholder="Paste reward text here (optional)", required=False, style=discord.TextStyle.paragraph, max_length=1000)
    async def on_submit(self, i: discord.Interaction):
        if not i.guild or not i.user.guild_permissions.administrator:
            return await i.response.send_message("Administrator only.", ephemeral=True)
        key = re.sub(r"[^a-z0-9_-]", "", str(self.category).lower().strip())
        old = str(self.current_name).strip()
        name = str(self.new_name).strip()
        try:
            count = int(str(self.invites).strip())
            if count < 0 or count > 1000: raise ValueError()
        except ValueError:
            return await i.response.send_message("Invite requirement must be a whole number from 0 to 1000.", ephemeral=True)
        try:
            cat = next((x for x in get_categories(i.guild.id) if x["category_key"] == key), None)
            if not cat: return await i.response.send_message("Category key not found.", ephemeral=True, view=AdminPanel())
            found = next((x for x in get_rewards(i.guild.id, key) if x["reward_name"] == old), None)
            if not found: return await i.response.send_message("Existing reward not found. Use Add Reward to create it.", ephemeral=True, view=AdminPanel())
            sb_request("trigger_rewards_config", "PATCH", params={"guild_id":f"eq.{i.guild.id}","category":f"eq.{key}","reward_name":f"eq.{old}"}, payload={"reward_name":name,"invite_requirement":count,"reward_text":str(self.details).strip(),"active":True})
            await i.response.send_message(f"✅ Updated **{name}** · {count} required invites. Reward text saved.", ephemeral=True, view=AdminPanel())
        except Exception as ex:
            await i.response.send_message("Could not edit reward: " + str(ex)[:180], ephemeral=True)


class ValidCodeModal(discord.ui.Modal, title="Add Valid Reward Code"):
    category = discord.ui.TextInput(label="Category key", placeholder="youtube", max_length=32)
    reward = discord.ui.TextInput(label="Exact reward name", placeholder="10K YouTube Subscribers Account", max_length=120)
    code = discord.ui.TextInput(label="Valid code to accept on website", placeholder="Enter the exact code", max_length=200)
    async def on_submit(self, i: discord.Interaction):
        if not i.guild or not i.user.guild_permissions.administrator:
            return await i.response.send_message("Administrator only.", ephemeral=True)
        key = re.sub(r"[^a-z0-9_-]", "", str(self.category).lower().strip())
        reward_name = str(self.reward).strip()
        code_value = str(self.code).strip()
        if not key or not reward_name or not code_value:
            return await i.response.send_message("Fill in all fields.", ephemeral=True)
        try:
            category = next((x for x in get_categories(i.guild.id) if x["category_key"] == key), None)
            reward = next((x for x in get_rewards(i.guild.id, key) if x["reward_name"] == reward_name), None)
            if not category or not reward:
                return await i.response.send_message("Category or exact reward name was not found. Check View Setup first.", ephemeral=True, view=AdminPanel())
            sb_request("trigger_reward_valid_codes", "POST", payload={"guild_id":str(i.guild.id),"category":key,"reward_name":reward_name,"code_value":code_value,"active":True})
            await i.response.send_message("✅ Valid code saved for **"+reward_name+"**. Other codes will show as incorrect.", ephemeral=True, view=AdminPanel())
        except Exception as ex:
            await i.response.send_message("Could not save code: "+str(ex)[:180], ephemeral=True)

class VouchTemplateModal(discord.ui.Modal):
    def __init__(self, current_template):
        super().__init__(title="Edit Vouch Message")
        self.template = discord.ui.TextInput(
            label="Vouch message template",
            style=discord.TextStyle.paragraph,
            default=current_template[:500],
            placeholder="Example: Legit got {reward} from {user}",
            max_length=500
        )
        self.add_item(self.template)

    async def on_submit(self, i: discord.Interaction):
        if not i.guild or not i.user.guild_permissions.administrator:
            return await i.response.send_message("Administrator only.", ephemeral=True)
        template = str(self.template).strip()
        allowed = {"reward", "user", "ticket"}
        placeholders = re.findall(r"\{([^{}]+)\}", template)
        if any(name not in allowed for name in placeholders):
            return await i.response.send_message(
                "Unsupported placeholder. Use only {reward}, {user}, and {ticket}.",
                ephemeral=True
            )
        if not template:
            return await i.response.send_message("Vouch message cannot be empty.", ephemeral=True)
        try:
            template.format(reward="REWARD", user="@user", ticket="ticket")
        except (KeyError, ValueError, IndexError):
            return await i.response.send_message(
                "The template has invalid braces or placeholders. Use only {reward}, {user}, and {ticket}.",
                ephemeral=True
            )
        try:
            save_vouch_template(i.guild.id, template)
            await i.response.send_message(
                "✅ Vouch message saved. Available placeholders: {reward}, {user}, {ticket}.",
                ephemeral=True,
                view=AdminPanel()
            )
        except Exception as ex:
            await i.response.send_message("Could not save vouch message: " + str(ex)[:180], ephemeral=True)


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
    @discord.ui.button(label="Add Valid Code", style=discord.ButtonStyle.success, emoji="🔐", row=1)
    async def add_valid_code(self, i: discord.Interaction, b: discord.ui.Button):
        await i.response.send_modal(ValidCodeModal())
    @discord.ui.button(label="Edit Category", style=discord.ButtonStyle.primary, emoji="✏️", row=1)
    async def edit_category(self, i: discord.Interaction, b: discord.ui.Button):
        await i.response.send_modal(CategoryEditModal())
    @discord.ui.button(label="Edit Reward", style=discord.ButtonStyle.success, emoji="🛠️", row=1)
    async def edit_reward(self, i: discord.Interaction, b: discord.ui.Button):
        await i.response.send_modal(RewardEditModal())
    @discord.ui.button(label="View Setup", style=discord.ButtonStyle.secondary, emoji="📊", row=0)
    async def view_setup(self, i: discord.Interaction, b: discord.ui.Button):
        try:
            cats = get_categories(i.guild.id)
            rewards = get_rewards(i.guild.id)
            e = discord.Embed(title="📊 Trigger Rewards Configuration", color=0xf2b84b)
            e.add_field(name=f"Categories ({len(cats)})", value="\n".join(f"{x.get('category_emoji','🎁')} **{x['category_label']}** · {x['category_key']}" for x in cats[:15]) or "No categories yet.", inline=False)
            e.add_field(name=f"Active rewards ({len(rewards)})", value="\n".join(f"• **{x['reward_name']}** — {x['invite_requirement']} invites ({x['category']})" for x in rewards[:20]) or "No rewards yet.", inline=False)
            e.set_footer(text="Use buttons to add more. /category-remove and /reward-remove can hide entries.")
            await i.response.send_message(embed=e, ephemeral=True, view=AdminPanel())
        except Exception as ex:
            await i.response.send_message("Could not read configuration: " + str(ex)[:180], ephemeral=True)
    @discord.ui.button(label="Edit Vouch Message", style=discord.ButtonStyle.primary, emoji="📝", row=2)
    async def edit_vouch_message(self, i: discord.Interaction, b: discord.ui.Button):
        try:
            await i.response.send_modal(VouchTemplateModal(get_vouch_template(i.guild.id)))
        except Exception as ex:
            await i.response.send_message("Could not open vouch editor: " + str(ex)[:180], ephemeral=True)


    @discord.ui.button(label="Add Real Reward Stock", style=discord.ButtonStyle.success, emoji="📦", row=3)
    async def add_real_stock(self, i: discord.Interaction, b: discord.ui.Button):
        await i.response.send_modal(AddRewardStockModal())

    @discord.ui.button(label="Pending DM Deliveries", style=discord.ButtonStyle.success, emoji="📨", row=3)
    async def pending_deliveries(self, i: discord.Interaction, b: discord.ui.Button):
        try:
            claims = get_pending_real_claims(i.guild.id)
            if not claims:
                return await i.response.send_message("There are no pending or failed real-reward deliveries right now.", ephemeral=True)
            embed = discord.Embed(
                title="📨 Pending Reward Deliveries",
                description="Only verified claimants awaiting delivery are listed. Select one or more people, then deliver each matching reward. No server-wide DMs.",
                color=0xf2b84b
            )
            embed.set_footer(text=f"{len(claims)} claim(s) shown • maximum 25 per page")
            await i.response.send_message(embed=embed, view=DeliveryQueueView(claims), ephemeral=True)
        except Exception as ex:
            await i.response.send_message("Could not load pending deliveries: "+str(ex)[:180], ephemeral=True)


    @discord.ui.button(label="Delivery History", style=discord.ButtonStyle.secondary, emoji="📜", row=4)
    async def delivery_history(self, i: discord.Interaction, b: discord.ui.Button):
        try:
            rows = get_real_delivery_history(i.guild.id)
            if not rows:
                return await i.response.send_message("No real-reward delivery attempts have been recorded yet.", ephemeral=True)
            lines = []
            for row in rows[:15]:
                claim = row.get("claim") or {}
                reward = claim.get("reward_name", "Reward")
                username = claim.get("discord_username") or row.get("discord_user_id")
                sent = row.get("sent_at") or row.get("created_at", "")
                lines.append(f"**{reward}** → {username} · {row.get('status','unknown')} · attempts: {row.get('attempts',0)} · {str(sent)[:19]}")
            embed = discord.Embed(title="📜 Real Reward Delivery History", description="\n".join(lines)[:3900], color=0xf2b84b)
            embed.set_footer(text="Latest 15 attempts • No reward codes are shown here")
            await i.response.send_message(embed=embed, ephemeral=True)
        except Exception as ex:
            await i.response.send_message("Could not load delivery history: "+str(ex)[:180], ephemeral=True)

    @discord.ui.button(label="Ticket Setup Help", style=discord.ButtonStyle.secondary, emoji="🎟️", row=2)
    async def ticket_help(self, i: discord.Interaction, b: discord.ui.Button):
        e = discord.Embed(title="🎟️ Ticket Setup", description="1. Run /ticket-setup and select the public channel.\n2. Optionally choose a Discord category for new ticket channels.\n3. Run /staff-role to set who can view tickets.\n4. Add categories and rewards here.\n5. Test by opening a ticket as a regular member.", color=0xf2b84b)
        await i.response.send_message(embed=e, ephemeral=True, view=AdminPanel())

@bot.tree.command(name="admin-panel", description="Open the embedded Trigger Rewards admin control panel")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def admin_panel(i: discord.Interaction):
    if not i.guild or not i.user.guild_permissions.administrator:
        return await i.response.send_message("🔒 Server administrators only.", ephemeral=True)
    e = discord.Embed(title="⚙️ TRIGGER REWARDS • ADMIN CONTROL", description="Manage reward categories, invite requirements and ticket setup directly inside Discord. No admin website required.\n\nCategories — add or edit the category label and emoji.\nRewards — add or edit inner rewards, invite requirements, and reward text.\nValid codes — use Add Valid Code to choose the category, exact reward, and accepted code.\nVouch message — edit the vouch post template here; placeholders: {reward}, {user}, {ticket}.\nView Setup — inspect active categories and rewards.", color=0xf2b84b)
    e.set_footer(text="Admin-only controls • Changes sync to the public rewards website")
    await i.response.send_message(embed=e, view=AdminPanel(), ephemeral=True)


@bot.tree.command(name="vouch-add", description="Post clearly labelled demo vouches for layout testing")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def vouch_add(i: discord.Interaction, number: app_commands.Range[int, 1, 10]):
    """Generate demo-only sample vouches; these are never genuine customer testimonials."""
    if not i.guild or not i.user.guild_permissions.administrator:
        return await i.response.send_message("Administrator only.", ephemeral=True)
    target = i.guild.get_channel(VOUCH_MESSAGE_CHANNEL_ID)
    if not isinstance(target, discord.TextChannel):
        try:
            target = await i.guild.fetch_channel(VOUCH_MESSAGE_CHANNEL_ID)
        except Exception:
            target = None
    if not isinstance(target, discord.TextChannel):
        return await i.response.send_message(
            "The configured vouch channel could not be found. Check VOUCH_MESSAGE_CHANNEL_ID.",
            ephemeral=True
        )

    first_names = ["Nova", "Pixel", "Shadow", "Lunar", "Frost", "Blaze", "Echo", "Astra", "Raven", "Cobalt"]
    second_names = ["Fox", "Wolf", "Byte", "Craft", "Spark", "Knight", "Cloud", "Ninja", "Drift", "Quest"]
    providers = ["Minecraft Rank", "YouTube Premium", "Streaming Reward"]
    posted = 0
    failures = []
    for index in range(number):
        username = f"{secrets.choice(first_names)}{secrets.choice(second_names)}{secrets.randbelow(9000) + 1000}"
        provider = secrets.choice(providers)
        safe_username = discord.utils.escape_mentions(discord.utils.escape_markdown(username))
        safe_provider = discord.utils.escape_mentions(discord.utils.escape_markdown(provider))
        try:
            template = get_vouch_template(i.guild.id)
            rendered = template.format(reward=safe_provider, user=safe_username, ticket="DEMO")
            embed = discord.Embed(
                title="🧪 DEMO VOUCH • NOT VERIFIED",
                description=(
                    f"**Member (sample):** {safe_username}\n"
                    f"**Reward (sample):** {safe_provider}\n"
                    f"**Ticket:** Demo #{index + 1}\n"
                    f"**Vouch:** {rendered[:1800]}\n\n"
                    "**Status:** GENERATED DEMO — not a real customer or verified delivery."
                ),
                color=0x9b59b6
            )
            embed.set_footer(text="Trigger Rewards • Demo only, not verified")
            await target.send(embed=embed)
            posted += 1
            await asyncio.sleep(0.5)
        except Exception as ex:
            failures.append(f"{index + 1}: {type(ex).__name__}")
    summary = f"Posted **{posted}/{number}** clearly labelled demo vouches in {target.mention}."
    if failures:
        summary += "\nFailed: " + ", ".join(failures[:10])
    await i.response.send_message(summary, ephemeral=True)


@bot.tree.command(name="fake-vouch", description="Post one clearly labelled demo vouch")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def fake_vouch(
    i: discord.Interaction,
    username: app_commands.Range[str, 1, 80],
    user_provider: app_commands.Range[str, 1, 80]
):
    """Post a DEMO-only sample; it never creates or verifies a real reward claim."""
    if not i.guild or not i.user.guild_permissions.administrator:
        return await i.response.send_message("Administrator only.", ephemeral=True)
    target = i.guild.get_channel(VOUCH_MESSAGE_CHANNEL_ID)
    if not isinstance(target, discord.TextChannel):
        try:
            target = await i.guild.fetch_channel(VOUCH_MESSAGE_CHANNEL_ID)
        except Exception:
            target = None
    if not isinstance(target, discord.TextChannel):
        return await i.response.send_message(
            "The configured vouch channel could not be found. Check VOUCH_MESSAGE_CHANNEL_ID.",
            ephemeral=True
        )

    safe_username = discord.utils.escape_mentions(discord.utils.escape_markdown(username.strip()))
    safe_provider = discord.utils.escape_mentions(discord.utils.escape_markdown(user_provider.strip()))
    try:
        template = get_vouch_template(i.guild.id)
        rendered = template.format(reward=safe_provider, user=safe_username, ticket="DEMO")
    except (KeyError, ValueError, IndexError):
        return await i.response.send_message(
            "Your saved vouch template could not be rendered. Check /admin-panel → Edit Vouch Message.",
            ephemeral=True
        )
    embed = discord.Embed(
        title="🧪 DEMO VOUCH • NOT VERIFIED",
        description=(
            f"**Member (demo text):** {safe_username}\n"
            f"**Reward (demo):** {safe_provider}\n"
            f"**Vouch:** {rendered[:1800]}\n\n"
            "**Status:** DEMO ONLY — not a real customer review or verified delivery."
        ),
        color=0x9b59b6
    )
    embed.set_footer(text="Trigger Rewards • Admin-created demonstration")
    try:
        await target.send(embed=embed)
        await i.response.send_message(
            f"✅ Demo vouch posted in {target.mention}. It is labelled DEMO and does not count as a verified vouch.",
            ephemeral=True
        )
    except discord.Forbidden:
        await i.response.send_message(
            "I cannot post in the vouch channel. Give the bot View Channel, Send Messages and Embed Links permissions.",
            ephemeral=True
        )
    except Exception as ex:
        await i.response.send_message("Could not post demo vouch: " + str(ex)[:160], ephemeral=True)


@bot.tree.command(name="reward-auto-delivery", description="Enable or disable automatic real reward DMs after invite verification")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def reward_auto_delivery(i: discord.Interaction, enabled: bool):
    if not i.guild or not i.user.guild_permissions.administrator:
        return await i.response.send_message("Administrator only.", ephemeral=True)
    c = database()
    c.execute("INSERT INTO settings(guild,auto_delivery) VALUES(?,?) ON CONFLICT(guild) DO UPDATE SET auto_delivery=excluded.auto_delivery",
              (i.guild.id, 1 if enabled else 0))
    c.commit(); c.close()
    state = "enabled" if enabled else "disabled"
    await i.response.send_message(f"Automatic real-reward DM delivery is now **{state}**. It only runs for verified ticket claims with matching available stock.", ephemeral=True)


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

@bot.tree.command(name="redeem-code",description="Staff permanently invalidate a code after redemption")
@app_commands.guild_only()
async def redeem(i:discord.Interaction,code:str):
    if not i.guild or not staff(i): return await i.response.send_message("Staff only.",ephemeral=True)
    if not re.fullmatch(r"[A-Z0-9]{12}",code.upper()): return await i.response.send_message("Code must be exactly 12 capital letters/numbers.",ephemeral=True)
    code = code.upper()
    c=database(); row=c.execute("SELECT * FROM claims WHERE guild=? AND code=?",(i.guild.id,code)).fetchone()
    if not row: c.close(); return await i.response.send_message("Code not found.",ephemeral=True)
    if row["status"]=="redeemed": c.close(); return await i.response.send_message("This code was already redeemed and is permanently invalid.",ephemeral=True)
    # Invalidate the matching website code as well, so it cannot be submitted again.
    try:
        tickets = sb_request("trigger_reward_valid_codes", "GET", params={
            "select":"id,category,reward_name,code_value,active",
            "guild_id":"eq."+str(i.guild.id),
            "code_value":"eq."+code,
            "active":"eq.true"
        })
        for item in tickets:
            sb_request("trigger_reward_valid_codes", "PATCH", params={"id":"eq."+str(item["id"])}, payload={"active":False})
    except Exception as ex:
        c.close()
        return await i.response.send_message("Website code could not be invalidated, so the local claim was left unchanged. Try again. Details: " + str(ex)[:120],ephemeral=True)
    c.execute("UPDATE claims SET status='redeemed',redeemed=? WHERE id=?",(datetime.now(timezone.utc).isoformat(),row["id"])); c.commit(); c.close()
    await i.response.send_message(f"Code redeemed and invalidated on the website. Reward: **{row['reward']}**.",ephemeral=True)

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