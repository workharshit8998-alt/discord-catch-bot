import os, re, sqlite3, secrets, json, asyncio, urllib.request, urllib.parse, urllib.error, base64, io
from datetime import datetime, timezone
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from invite import install_invite_module
from extract import extract_reward_from_message_link, ExtractError

load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
SITE_URL = os.getenv("DEMO_URL", "https://trigger-rewards.vercel.app").strip()
DB_FILE = os.getenv("TICKET_DB_PATH", "trigger_rewards.sqlite3")
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
SYNC_GUILD_ID = int(os.getenv("TRG_GUILD_ID", "1499228791220863110"))
VOUCH_MESSAGE_CHANNEL_ID = int(os.getenv("VOUCH_MESSAGE_CHANNEL_ID", "1499228791891824824"))  # vouch embed channel
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
invite_tracker = install_invite_module(bot)
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
    remote = []
    try:
        remote = sb_request("trigger_reward_categories",params={"select":"category_key,category_label,category_emoji,sort_order","guild_id":"eq."+str(guild_id),"active":"eq.true","order":"sort_order.asc,category_label.asc"})
    except Exception as ex:
        print("Remote category lookup unavailable; using local stock categories:", repr(ex))
    merged = {x["category_key"]: dict(x) for x in remote}
    c = database()
    local = c.execute("SELECT category_key,label,emoji FROM trg_stock_categories WHERE guild=? AND active=1 ORDER BY label", (guild_id,)).fetchall()
    c.close()
    for x in local:
        merged.setdefault(x["category_key"], {"category_key":x["category_key"],"category_label":x["label"],"category_emoji":x["emoji"],"sort_order":999})
    return list(merged.values())


def get_rewards(guild_id, category=None):
    remote = []
    params={"select":"category,category_label,category_emoji,reward_name,invite_requirement,sort_order","guild_id":"eq."+str(guild_id),"active":"eq.true","order":"sort_order.asc,reward_name.asc"}
    if category: params["category"]="eq."+category
    try:
        remote = sb_request("trigger_rewards_config",params=params)
    except Exception as ex:
        print("Remote reward lookup unavailable; using local stock rewards:", repr(ex))
    merged = {(x["category"],x["reward_name"]):dict(x) for x in remote}
    c=database()
    q="SELECT category_key,reward_name FROM trg_stock_items WHERE guild=?"
    args=[guild_id]
    if category:
        q += " AND category_key=?"; args.append(category)
    q += " GROUP BY category_key,reward_name ORDER BY reward_name"
    local_rows=c.execute(q,args).fetchall()
    c.close()
    for x in local_rows:
        key=(x["category_key"],x["reward_name"])
        if key not in merged:
            cat=next((z for z in get_categories(guild_id) if z["category_key"]==x["category_key"]),{})
            merged[key]={"category":x["category_key"],"category_label":cat.get("category_label",x["category_key"]),"category_emoji":cat.get("category_emoji","🎁"),"reward_name":x["reward_name"],"invite_requirement":0,"sort_order":999}
    return list(merged.values())




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


async def upsert_ticket_status(channel, title, description, color=0x5865f2, fields=None):
    """Keep one clean, editable status embed in each reward ticket."""
    embed = discord.Embed(title=title, description=description, color=color)
    for name, value, inline in (fields or []):
        embed.add_field(name=name, value=value, inline=inline)
    embed.set_footer(text="Trigger Rewards • Ticket Status")
    try:
        async for old in channel.history(limit=30):
            if old.author.id != bot.user.id:
                continue
            legacy_text = (old.content or "").lower()
            if any(marker in legacy_text for marker in (
                "invite check:", "your claim is **pending**",
                "invite requirement verified, say", "your claim is pending because"
            )):
                try:
                    await old.delete()
                except Exception:
                    pass
                continue
            if old.embeds:
                old_embed = old.embeds[0]
                if old_embed.footer and old_embed.footer.text == "Trigger Rewards • Ticket Status":
                    await old.edit(embed=embed)
                    return old
    except Exception as ex:
        print("Could not update previous ticket status embed:", repr(ex))
    return await channel.send(embed=embed)


async def deliver_real_reward(guild, claim_id, actor_label="auto"):
    """Deliver a shared active reward entry. Stock is never consumed or decremented."""
    c=database()
    claim=c.execute("SELECT * FROM trg_stock_claims WHERE id=? AND guild=?",(int(claim_id),guild.id)).fetchone()
    if not claim:
        c.close(); raise RuntimeError("Claim not found.")
    if claim["status"]=="delivered":
        c.close(); raise RuntimeError("This claim has already been delivered.")
    items=c.execute("SELECT * FROM trg_stock_items WHERE guild=? AND category_key=? AND reward_name=? AND active=1 ORDER BY id",
                    (guild.id,claim["category_key"],claim["reward_name"])).fetchall()
    if not items:
        c.close(); raise RuntimeError("No active stock is available. The claim remains pending.")
    item=secrets.choice(items)
    fields=json.loads(item["fields_json"] or "{}")
    changed=c.execute("UPDATE trg_stock_claims SET status='sending',stock_item_id=? WHERE id=? AND status IN ('pending','approved')",
                      (item["id"],int(claim_id))).rowcount
    c.commit(); c.close()
    if not changed:
        raise RuntimeError("This claim is already being processed or is no longer pending.")
    try:
        user=await bot.fetch_user(int(claim["user_id"]))
        attachment_records = fields.pop("__trg_attachments", []) if isinstance(fields, dict) else []

        # Match imported stock fields to the fixed Trigger Rewards delivery layout.
        normalized = {str(k).strip().casefold(): str(v).strip() for k, v in fields.items()
                      if v is not None and str(v).strip() and k != "Imported Embed JSON"}
        def pick(*names):
            for name in names:
                value = normalized.get(name.casefold())
                if value:
                    return value
            return ""

        reward_name = discord.utils.escape_markdown(str(claim["reward_name"]))
        reward_type = discord.utils.escape_markdown(pick("Type") or "Not provided")
        capes = discord.utils.escape_markdown(pick("Capes") or "Not provided")
        account_id = pick("Username", "user name", "ID", "id", "account id")
        email = pick("Email", "email address")
        password = pick("Password", "pass")
        safe_id = account_id.replace("\n", " ").replace("\r", " ")[:500]
        safe_email = email.replace("\n", " ").replace("\r", " ")[:500]
        safe_password = password.replace("\n", " ").replace("\r", " ")[:500]

        discord_files = []
        token_filename = pick("Token File")
        for record in attachment_records[:10]:
            try:
                raw = base64.b64decode(record.get("data", ""), validate=True)
                if raw:
                    filename = record.get("filename") or "reward-file"
                    discord_files.append(discord.File(io.BytesIO(raw), filename=filename))
                    if not token_filename and str(filename).lower().endswith(".txt"):
                        token_filename = str(filename)
            except Exception as file_ex:
                print("Skipping unreadable imported stock attachment:", repr(file_ex))

        # Attach the supplied Minecraft mod from the bot's own folder.
        # Upload "Token Login.jar" beside main.py on the hosting panel.
        mod_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Token Login.jar")
        if not os.path.isfile(mod_path):
            raise FileNotFoundError(
                'Token Login.jar is missing. Upload it into the same folder as main.py.'
            )
        discord_files.append(discord.File(mod_path, filename="Token Login.jar"))



        token_filename = token_filename or "Not provided"
        token_line = (
            f"**Token file:** `{discord.utils.escape_markdown(token_filename)}` (attached)"
            if token_filename != "Not provided"
            else "**Token file:** Not provided"
        )

        embed = discord.Embed(
            title="🎁 Reward Successfully Delivered",
            description=f"Your **{reward_name}** reward is ready. Keep all account details private.",
            color=0xf2b84b
        )
        embed.add_field(
            name="📋 Reward Information",
            value=f"**Type:** {reward_type}\n**Capes:** {capes}",
            inline=False
        )
        embed.add_field(
            name="🔐 Login Method 1 · ID & Password",
            value=(
                "**Working chance:** 50%\n"
                f"**ID:** ||{safe_id}||\n"
                f"**Email:** ||{safe_email}||\n"
                f"**Password:** ||{safe_password}||"
            ),
            inline=False
        )
        embed.add_field(
            name="🔑 Login Method 2 · Token Login",
            value=f"**Working chance:** 100%\n{token_line}",
            inline=False
        )
        tutorial_url = "https://dembed.cc/v/v_XBThQTxy0xUm"
        embed.add_field(
            name="🧩 Mod & 🎥 Tutorial",
            value=(
                "🧩 **Mod:** `Token Login.jar` (attached)\n"
                f"🎥 **Tutorial:** [Watch tutorial]({tutorial_url})"
            ),
            inline=False
        )
        embed.add_field(
            name="🔒 Privacy",
            value="Keep your login details and token file private. Never share them with anyone.",
            inline=False
        )
        embed.set_footer(text="TRIGGER REWARDS  •  Secure Private Delivery")
        await user.send(embed=embed, files=discord_files)
    except Exception:
        c=database()
        c.execute("UPDATE trg_stock_claims SET status='pending' WHERE id=? AND status='sending'",(int(claim_id),))
        c.commit(); c.close()
        raise
    # Charge the invite cost exactly once, only after the private DM succeeds.
    # The baseline is the cumulative number of invites already spent.
    c=database()
    try:
        c.execute("BEGIN IMMEDIATE")
        charged=c.execute(
            "UPDATE trg_stock_claims SET status='delivered',delivered_at=?,invite_charged=1 "
            "WHERE id=? AND status='sending' AND invite_charged=0",
            (datetime.now(timezone.utc).isoformat(),int(claim_id))
        ).rowcount
        if charged:
            cost=max(0,int(claim["invite_cost"] or 0))
            if cost:
                c.execute(
                    "INSERT INTO tracked_invite_baselines(guild,user,baseline,updated) VALUES(?,?,?,?) "
                    "ON CONFLICT(guild,user) DO UPDATE SET baseline=tracked_invite_baselines.baseline+excluded.baseline,updated=excluded.updated",
                    (guild.id,int(claim["user_id"]),cost,datetime.now(timezone.utc).isoformat())
                )
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()
    ticket=guild.get_channel(int(claim["ticket_channel_id"]))
    if isinstance(ticket,discord.TextChannel):
        try:
            rendered=render_ticket_message(guild.id,"delivery_confirmation_message",DEFAULT_DELIVERY_CONFIRMATION_MESSAGE,
                reward=discord.utils.escape_markdown(claim["reward_name"]),user=f"<@{claim['user_id']}>")
            await upsert_ticket_status(
                ticket, "🎁 Reward Delivery Update", rendered,
                color=0x2ecc71,
                fields=[("Status", "Delivered privately by DM", False),
                        ("Next step", "If you received your reward, reply **Legit** in this ticket.", False)]
            )
        except Exception as ex: print("Could not post delivery confirmation:",repr(ex))
    return "delivered"

def get_pending_real_claims(guild_id):
    c=database()
    rows=[dict(x) for x in c.execute(
        "SELECT id,user_id AS discord_user_id,username AS discord_username,reward_name,status,created_at AS requested_at "
        "FROM trg_stock_claims WHERE guild=? AND status IN ('pending','approved') ORDER BY created_at LIMIT 25",
        (guild_id,)).fetchall()]
    c.close()
    return rows




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
        if not is_guild_owner(i):
            await i.response.send_message("🔒 Only the server owner can manage reward stock.", ephemeral=True)
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
    CREATE TABLE IF NOT EXISTS settings(guild INTEGER PRIMARY KEY, staff_role INTEGER DEFAULT 0, ticket_category INTEGER DEFAULT 0, vouch_channel INTEGER DEFAULT 0, invite_default INTEGER DEFAULT 3, invite_verified_message TEXT, delivery_confirmation_message TEXT);
    CREATE TABLE IF NOT EXISTS rewards(id INTEGER PRIMARY KEY, guild INTEGER, category TEXT, name TEXT, invites INTEGER, active INTEGER DEFAULT 1, UNIQUE(guild,category,name));
    CREATE TABLE IF NOT EXISTS tickets(channel INTEGER PRIMARY KEY, guild INTEGER, user INTEGER, category TEXT, reward TEXT, status TEXT DEFAULT 'open', created TEXT);
    CREATE TABLE IF NOT EXISTS ticket_counters(guild INTEGER PRIMARY KEY, last_number INTEGER NOT NULL DEFAULT 0);
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
    if "demo_vouch_template" not in settings_columns:
        c.execute("ALTER TABLE settings ADD COLUMN demo_vouch_template TEXT")
        c.commit()
    if "invite_verified_message" not in settings_columns:
        c.execute("ALTER TABLE settings ADD COLUMN invite_verified_message TEXT")
        c.commit()
    if "delivery_confirmation_message" not in settings_columns:
        c.execute("ALTER TABLE settings ADD COLUMN delivery_confirmation_message TEXT")
        c.commit()
    c.execute("""CREATE TABLE IF NOT EXISTS vouches(
        id INTEGER PRIMARY KEY AUTOINCREMENT, guild INTEGER NOT NULL, user INTEGER NOT NULL,
        reward TEXT NOT NULL, ticket_channel INTEGER NOT NULL, confirmed_at TEXT NOT NULL,
        UNIQUE(guild, ticket_channel)
    )""")
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
    # Separate counter for the new built-in tracker so old Falcon baselines are preserved.
    c.execute("""CREATE TABLE IF NOT EXISTS tracked_invite_baselines(
        guild INTEGER NOT NULL, user INTEGER NOT NULL, baseline INTEGER NOT NULL DEFAULT 0,
        updated TEXT NOT NULL, PRIMARY KEY(guild,user)
    )""")
    c.executescript("""
    CREATE TABLE IF NOT EXISTS trg_stock_categories(
        id INTEGER PRIMARY KEY AUTOINCREMENT, guild INTEGER NOT NULL,
        category_key TEXT NOT NULL, label TEXT NOT NULL, emoji TEXT NOT NULL DEFAULT '🎁',
        active INTEGER NOT NULL DEFAULT 1, UNIQUE(guild, category_key)
    );
    CREATE TABLE IF NOT EXISTS trg_stock_items(
        id INTEGER PRIMARY KEY AUTOINCREMENT, guild INTEGER NOT NULL,
        category_key TEXT NOT NULL, reward_name TEXT NOT NULL,
        fields_json TEXT NOT NULL DEFAULT '{}', active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_trg_stock_active ON trg_stock_items(guild,category_key,active);
    CREATE TABLE IF NOT EXISTS trg_stock_claims(
        id INTEGER PRIMARY KEY AUTOINCREMENT, guild INTEGER NOT NULL, user_id INTEGER NOT NULL,
        username TEXT NOT NULL, category_key TEXT NOT NULL, reward_name TEXT NOT NULL,
        ticket_channel_id INTEGER NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'pending',
        stock_item_id INTEGER, created_at TEXT NOT NULL, delivered_at TEXT, reviewed_by INTEGER,
        rejection_reason TEXT, invite_cost INTEGER NOT NULL DEFAULT 0,
        invite_charged INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_trg_claim_pending ON trg_stock_claims(guild,status,created_at);
    """)
    claim_columns={row[1] for row in c.execute("PRAGMA table_info(trg_stock_claims)").fetchall()}
    if "invite_cost" not in claim_columns:
        c.execute("ALTER TABLE trg_stock_claims ADD COLUMN invite_cost INTEGER NOT NULL DEFAULT 0")
    if "invite_charged" not in claim_columns:
        c.execute("ALTER TABLE trg_stock_claims ADD COLUMN invite_charged INTEGER NOT NULL DEFAULT 0")
    c.commit()
    return c

DEFAULT_VOUCH_TEMPLATE = "Legit got {reward} from @TriggerPlayz"
DEFAULT_DEMO_VOUCH_TEMPLATE = "**Member:** {user}\n**Reward:** {reward}\n**Vouch:** Legit\n\n*Trigger Rewards*"
DEFAULT_INVITE_VERIFIED_MESSAGE = ("✅ Invite requirement verified. Your reward is now pending delivery. "
    "Once you receive the actual reward code by DM, say **Legit** to add your name to our vouch database. "
    "This ticket will be deleted automatically after your confirmation.")
DEFAULT_DELIVERY_CONFIRMATION_MESSAGE = ("✅ The actual **{reward}** code was sent privately to {user}. "
    "If you received it, reply **Legit** to add your name to our vouch database. "
    "This ticket will be deleted automatically after your confirmation.")

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


def get_demo_vouch_template(guild_id):
    c = database()
    row = c.execute("SELECT demo_vouch_template FROM settings WHERE guild=?", (guild_id,)).fetchone()
    c.close()
    return row["demo_vouch_template"] if row and row["demo_vouch_template"] else DEFAULT_DEMO_VOUCH_TEMPLATE


def save_demo_vouch_template(guild_id, template):
    c = database()
    c.execute(
        "INSERT INTO settings(guild, demo_vouch_template) VALUES(?, ?) "
        "ON CONFLICT(guild) DO UPDATE SET demo_vouch_template=excluded.demo_vouch_template",
        (guild_id, template)
    )
    c.commit()
    c.close()


def get_ticket_message(guild_id, column, default):
    if column not in {"invite_verified_message", "delivery_confirmation_message"}:
        raise ValueError("Unsupported message setting.")
    c = database()
    row = c.execute(f"SELECT {column} FROM settings WHERE guild=?", (guild_id,)).fetchone()
    c.close()
    return row[column] if row and row[column] else default


def save_ticket_messages(guild_id, invite_message, delivery_message):
    c = database()
    c.execute(
        "INSERT INTO settings(guild, invite_verified_message, delivery_confirmation_message) VALUES(?,?,?) "
        "ON CONFLICT(guild) DO UPDATE SET invite_verified_message=excluded.invite_verified_message, "
        "delivery_confirmation_message=excluded.delivery_confirmation_message",
        (guild_id, invite_message, delivery_message)
    )
    c.commit()
    c.close()


def render_ticket_message(guild_id, column, default, **values):
    template = get_ticket_message(guild_id, column, default)
    try:
        return template.format(**values)
    except (KeyError, ValueError, IndexError):
        return default.format(**values)

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

async def get_vouch_channel(guild):
    """Resolve this server's saved vouch channel; the env ID is only a legacy fallback."""
    settings = cfg(guild.id)
    channel_id = int(settings.get("vouch_channel") or VOUCH_MESSAGE_CHANNEL_ID or 0)
    if not channel_id:
        return None
    channel = guild.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        try:
            channel = await guild.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None
    if not isinstance(channel, discord.TextChannel) or channel.guild.id != guild.id:
        return None
    return channel

def staff(i):
    if not i.guild or not isinstance(i.user, discord.Member): return False
    s=cfg(i.guild.id)
    return i.user.guild_permissions.administrator or (s["staff_role"] and any(r.id==s["staff_role"] for r in i.user.roles))


def is_guild_owner(interaction):
    return bool(interaction.guild and interaction.guild.owner_id == interaction.user.id)


class StockCategoryModal(discord.ui.Modal, title="Add Stock Category"):
    key = discord.ui.TextInput(label="Category key", placeholder="minecraft", max_length=32)
    label = discord.ui.TextInput(label="Category name", placeholder="Minecraft", max_length=80)
    emoji = discord.ui.TextInput(label="Emoji", default="🎁", max_length=16, required=False)

    async def on_submit(self, i):
        if not is_guild_owner(i):
            return await i.response.send_message("Only the server owner can manage stock.", ephemeral=True)
        key = re.sub(r"[^a-z0-9_-]", "", str(self.key).lower().strip())
        label = str(self.label).strip()
        if not key or not label:
            return await i.response.send_message("Enter a valid category key and name.", ephemeral=True)
        c = database()
        c.execute("INSERT INTO trg_stock_categories(guild,category_key,label,emoji,active) VALUES(?,?,?,?,1) "
                  "ON CONFLICT(guild,category_key) DO UPDATE SET label=excluded.label,emoji=excluded.emoji,active=1",
                  (i.guild.id,key,label,str(self.emoji).strip() or "🎁"))
        c.commit(); c.close()
        await i.response.send_message(f"✅ Stock category **{label}** saved.", ephemeral=True)


class StockItemModal(discord.ui.Modal, title="Add Shared Reward Stock"):
    reward_value = discord.ui.TextInput(
        label="Reward",
        style=discord.TextStyle.paragraph,
        placeholder="Write the reward details here...",
        max_length=3500
    )

    def __init__(self, category_key, reward_name):
        super().__init__()
        self.category_key = category_key
        self.reward_name = reward_name

    async def on_submit(self, i):
        if not is_guild_owner(i):
            return await i.response.send_message("Only the server owner can manage stock.", ephemeral=True)
        value = str(self.reward_value).strip()
        if not value:
            return await i.response.send_message("Write the reward details first.", ephemeral=True)
        now = datetime.now(timezone.utc).isoformat()
        values = {"Reward": value}
        c = database()
        c.execute("INSERT INTO trg_stock_items(guild,category_key,reward_name,fields_json,active,created_at,updated_at) VALUES(?,?,?,?,1,?,?)",
                  (i.guild.id, self.category_key, self.reward_name, json.dumps(values,ensure_ascii=False), now, now))
        c.commit(); c.close()
        await i.response.send_message(f"✅ Added shared stock for **{self.reward_name}**. This entry remains active and can be delivered repeatedly until paused or removed.",ephemeral=True)
        # Process every pending claim in creation order; reusable stock is not depleted.
        c=database()
        pending=[x["id"] for x in c.execute("SELECT id FROM trg_stock_claims WHERE guild=? AND status='pending' ORDER BY created_at",(i.guild.id,)).fetchall()]
        c.close()
        delivered=0
        for claim_id in pending:
            try:
                await deliver_real_reward(i.guild,claim_id,"auto")
                delivered+=1
            except Exception as ex:
                print("Pending stock claim remains queued:",claim_id,repr(ex))
        if pending:
            try: await i.followup.send(f"Processed pending claims: **{delivered}/{len(pending)}** delivered. Others remain pending.",ephemeral=True)
            except Exception: pass


class StockRewardSelect(discord.ui.Select):
    def __init__(self, category_key, rewards):
        self.category_key = category_key
        options = [discord.SelectOption(label=x["reward_name"][:100], value=x["reward_name"])
                   for x in rewards[:25]]
        super().__init__(placeholder="Choose the reward", min_values=1, max_values=1, options=options)

    async def callback(self, i):
        if not is_guild_owner(i):
            return await i.response.send_message("Only the server owner can manage stock.", ephemeral=True)
        await i.response.send_modal(StockItemModal(self.category_key, self.values[0]))


class StockRewardSelectView(discord.ui.View):
    def __init__(self, category_key, rewards):
        super().__init__(timeout=180)
        self.add_item(StockRewardSelect(category_key, rewards))


class StockCategorySelect(discord.ui.Select):
    def __init__(self, categories):
        options = [discord.SelectOption(label=x["category_label"][:100], value=x["category_key"],
                    emoji=x.get("category_emoji") or "🎁") for x in categories[:25]]
        super().__init__(placeholder="Choose a category", min_values=1, max_values=1, options=options)

    async def callback(self, i):
        if not is_guild_owner(i):
            return await i.response.send_message("Only the server owner can manage stock.", ephemeral=True)
        key = self.values[0]
        try:
            rewards = get_rewards(i.guild.id, key)
        except Exception as ex:
            return await i.response.send_message("Could not load rewards: " + str(ex)[:150], ephemeral=True)
        if not rewards:
            return await i.response.send_message("No rewards are configured in this category yet. Add the reward name in your existing reward settings first.", ephemeral=True)
        await i.response.send_message("Now choose which reward this stock belongs to:",
                                      view=StockRewardSelectView(key, rewards), ephemeral=True)


class StockCategorySelectView(discord.ui.View):
    def __init__(self, categories):
        super().__init__(timeout=180)
        self.add_item(StockCategorySelect(categories))


class EditStockFieldsModal(discord.ui.Modal, title="Edit Reward Fields"):
    item_id = discord.ui.TextInput(label="Stock entry ID", max_length=12)
    fields = discord.ui.TextInput(label="Complete reward fields (JSON object)",style=discord.TextStyle.paragraph,max_length=3500)

    async def on_submit(self,i):
        if not is_guild_owner(i):
            return await i.response.send_message("Only the server owner can manage stock.",ephemeral=True)
        try:
            item_id=int(str(self.item_id))
            values=json.loads(str(self.fields))
            if not isinstance(values,dict) or not values: raise ValueError("Enter a non-empty JSON object.")
            values={str(k)[:60]:str(v)[:1500] for k,v in values.items() if str(k).strip()}
        except Exception as ex:
            return await i.response.send_message("Invalid entry: "+str(ex)[:150],ephemeral=True)
        c=database()
        changed=c.execute("UPDATE trg_stock_items SET fields_json=?,updated_at=? WHERE id=? AND guild=?",
            (json.dumps(values,ensure_ascii=False),datetime.now(timezone.utc).isoformat(),item_id,i.guild.id)).rowcount
        c.commit(); c.close()
        await i.response.send_message("✅ Reward fields updated." if changed else "Stock entry not found.",ephemeral=True)


class StockItemSelect(discord.ui.Select):
    def __init__(self, rows):
        self.rows=rows
        options=[discord.SelectOption(label=f"{r['reward_name']}"[:100],
            description=f"{r['category_key']} · {'Active' if r['active'] else 'Paused'}"[:100],
            value=str(r["id"])) for r in rows[:25]]
        super().__init__(placeholder="Select a reward to activate/pause",min_values=1,max_values=1,options=options)
    async def callback(self,i):
        if not is_guild_owner(i):
            return await i.response.send_message("Only the server owner can manage stock.",ephemeral=True)
        item_id=int(self.values[0])
        self.view.selected_stock_id=item_id
        c=database()
        row=c.execute("SELECT * FROM trg_stock_items WHERE id=? AND guild=?",(item_id,i.guild.id)).fetchone()
        if not row:
            c.close(); return await i.response.send_message("Stock entry not found.",ephemeral=True)
        new=0 if row["active"] else 1
        c.execute("UPDATE trg_stock_items SET active=?,updated_at=? WHERE id=?",(new,datetime.now(timezone.utc).isoformat(),item_id))
        c.commit(); c.close()
        await i.response.send_message(f"**{row['reward_name']}** is now {'Active' if new else 'Paused'}.",ephemeral=True)


class PendingStockClaimSelect(discord.ui.Select):
    def __init__(self, rows):
        self.rows=rows
        options=[discord.SelectOption(label=f"{r['username']} · {r['reward_name']}"[:100],value=str(r["id"]),
            description=f"Claim #{r['id']} · {r['status']}") for r in rows[:25]]
        super().__init__(placeholder="Select a pending claim",min_values=1,max_values=1,options=options)
    async def callback(self,i):
        if not is_guild_owner(i):
            return await i.response.send_message("Only the server owner can manage claims here.",ephemeral=True)
        self.view.selected_claim=int(self.values[0])
        await i.response.send_message(f"Selected claim **#{self.values[0]}**.",ephemeral=True)


class ImportMessageLinkModal(discord.ui.Modal, title="Import Forwarded Reward Embed"):
    message_link = discord.ui.TextInput(
        label="Message link",
        placeholder="https://discord.com/channels/server/channel/message",
        max_length=200,
    )

    async def on_submit(self, i: discord.Interaction):
        if not is_guild_owner(i):
            return await i.response.send_message("Only the server owner can import reward stock.", ephemeral=True)
        await i.response.defer(ephemeral=True, thinking=True)
        try:
            allowed_channel = int(os.getenv("REWARD_IMPORT_CHANNEL_ID", "0") or 0) or None
            extracted = await extract_reward_from_message_link(
                bot, i.guild, str(self.message_link), allowed_channel_id=allowed_channel
            )
        except ExtractError as ex:
            return await i.followup.send(f"❌ Import failed: {ex}", ephemeral=True)
        except Exception as ex:
            print("Reward embed extraction failed:", repr(ex))
            return await i.followup.send("❌ I couldn't extract that message. Check bot permissions and try again.", ephemeral=True)

        preview = discord.Embed(
            title="🔎 Imported Reward Preview",
            description=(
                f"**Forwarded by:** {discord.utils.escape_markdown(extracted.author)}\n"
                f"**Text / embed fields:** {len(extracted.fields)}\n"
                f"**Attachments downloaded:** {len(extracted.attachments)}\n"
                f"**Source message:** `{extracted.source_message_id}`"
            ),
            color=0xf2b84b,
        )
        for key, value in list(extracted.fields.items())[:8]:
            preview.add_field(name=str(key)[:256], value=str(value)[:900] or "—", inline=False)
        if extracted.attachments:
            preview.add_field(
                name="📎 Files",
                value="\n".join(f"• {discord.utils.escape_markdown(a.filename)} ({a.size:,} bytes)" for a in extracted.attachments[:10])[:1024],
                inline=False,
            )
        if extracted.warnings:
            preview.add_field(name="⚠️ Import warnings", value="\n".join(extracted.warnings)[:1024], inline=False)
        preview.set_footer(text="Review the extracted data, then choose the stock category and reward.")
        categories = get_categories(i.guild.id)
        if not categories:
            return await i.followup.send(embed=preview, content="No stock categories exist yet. Add a category first, then retry the import.", ephemeral=True)
        await i.followup.send(embed=preview, content="Choose where this imported reward should be stored:",
                              view=ExtractedImportCategoryView(extracted, categories), ephemeral=True)


class ExtractedImportCategorySelect(discord.ui.Select):
    def __init__(self, extracted, categories):
        self.extracted = extracted
        options = [discord.SelectOption(label=x["category_label"][:100], value=x["category_key"],
                    emoji=x.get("category_emoji") or "🎁") for x in categories[:25]]
        super().__init__(placeholder="Choose stock category", min_values=1, max_values=1, options=options)

    async def callback(self, i: discord.Interaction):
        if not is_guild_owner(i):
            return await i.response.send_message("Only the server owner can import stock.", ephemeral=True)
        key = self.values[0]
        try:
            rewards = get_rewards(i.guild.id, key)
        except Exception as ex:
            return await i.response.send_message("Could not load rewards: " + str(ex)[:150], ephemeral=True)
        if not rewards:
            return await i.response.send_message("No rewards are configured in this category. Add the reward first, then import again.", ephemeral=True)
        await i.response.send_message(
            "Now choose the exact reward this imported embed belongs to:",
            view=ExtractedImportRewardView(self.extracted, key, rewards), ephemeral=True
        )


class ExtractedImportCategoryView(discord.ui.View):
    def __init__(self, extracted, categories):
        super().__init__(timeout=300)
        self.add_item(ExtractedImportCategorySelect(extracted, categories))


class ExtractedImportRewardSelect(discord.ui.Select):
    def __init__(self, extracted, category_key, rewards):
        self.extracted = extracted
        self.category_key = category_key
        options = [discord.SelectOption(label=x["reward_name"][:100], value=x["reward_name"])
                   for x in rewards[:25]]
        super().__init__(placeholder="Choose reward", min_values=1, max_values=1, options=options)

    async def callback(self, i: discord.Interaction):
        if not is_guild_owner(i):
            return await i.response.send_message("Only the server owner can import stock.", ephemeral=True)
        self.view.reward_name = self.values[0]
        await i.response.edit_message(
            content=f"Selected **{self.values[0]}**. Press **Save Imported Stock** to add this entry.",
            view=self.view,
        )


class ExtractedImportRewardView(discord.ui.View):
    def __init__(self, extracted, category_key, rewards):
        super().__init__(timeout=300)
        self.extracted = extracted
        self.category_key = category_key
        self.reward_name = None
        self.add_item(ExtractedImportRewardSelect(extracted, category_key, rewards))

    @discord.ui.button(label="Save Imported Stock", style=discord.ButtonStyle.success, emoji="📥", row=1)
    async def save_import(self, i: discord.Interaction, button: discord.ui.Button):
        if not is_guild_owner(i):
            return await i.response.send_message("Only the server owner can import stock.", ephemeral=True)
        if not self.reward_name:
            return await i.response.send_message("Choose a reward from the dropdown first.", ephemeral=True)
        extracted = self.extracted
        values = extracted.stock_fields()
        # Store attachment bytes in the existing fields_json column. No schema or DB rename.
        if extracted.attachments:
            values["__trg_attachments"] = [
                {"filename": a.filename, "content_type": a.content_type,
                 "size": a.size, "data": base64.b64encode(a.data).decode("ascii")}
                for a in extracted.attachments[:10]
            ]
        now = datetime.now(timezone.utc).isoformat()
        try:
            payload = json.dumps(values, ensure_ascii=False)
            if len(payload.encode("utf-8")) > 32 * 1024 * 1024:
                return await i.response.send_message("This import is too large to store safely. Reduce the number or size of attachments.", ephemeral=True)
            c = database()
            c.execute(
                "INSERT INTO trg_stock_items(guild,category_key,reward_name,fields_json,active,created_at,updated_at) VALUES(?,?,?,?,1,?,?)",
                (i.guild.id, self.category_key, self.reward_name, payload, now, now),
            )
            c.commit()
            c.close()
        except Exception as ex:
            print("Could not save imported reward stock:", repr(ex))
            return await i.response.send_message("❌ Could not save this imported reward. No stock was added.", ephemeral=True)

        await i.response.edit_message(
            content=(f"✅ Imported reward added to **{self.category_key} / {self.reward_name}**. "
                     f"Saved {len(extracted.fields)} extracted fields and {len(extracted.attachments)} files."),
            embed=None, view=None,
        )
        # Match the existing Add Reward Stock behavior: retry pending claims after stock is saved.
        c = database()
        pending = [x["id"] for x in c.execute(
            "SELECT id FROM trg_stock_claims WHERE guild=? AND category_key=? AND reward_name=? AND status='pending' ORDER BY created_at",
            (i.guild.id, self.category_key, self.reward_name),
        ).fetchall()]
        c.close()
        delivered = 0
        for claim_id in pending:
            try:
                await deliver_real_reward(i.guild, claim_id, "auto")
                delivered += 1
            except Exception as ex:
                print("Imported stock pending claim remains queued:", claim_id, repr(ex))
        if pending:
            try:
                await i.followup.send(f"Pending claims processed: **{delivered}/{len(pending)}** delivered. Any others remain pending.", ephemeral=True)
            except Exception:
                pass


class StockManageView(discord.ui.View):
    def __init__(self,guild_id):
        super().__init__(timeout=300)
        self.guild_id=guild_id
        self.selected_stock_id=None
        c=database()
        rows=c.execute("SELECT id,category_key,reward_name,active FROM trg_stock_items WHERE guild=? ORDER BY id DESC",(guild_id,)).fetchall()
        c.close()
        if rows: self.add_item(StockItemSelect(rows))
    async def interaction_check(self,i):
        if not is_guild_owner(i):
            await i.response.send_message("🔒 Only the server owner can manage reward stock.",ephemeral=True)
            return False
        return True
    @discord.ui.button(label="Add Embedded",style=discord.ButtonStyle.success,emoji="📥",row=1)
    async def add_embedded(self,i,b):
        await i.response.send_modal(ImportMessageLinkModal())
    @discord.ui.button(label="Add Category",style=discord.ButtonStyle.primary,emoji="🗂️",row=1)
    async def add_category(self,i,b):
        await i.response.send_modal(StockCategoryModal())
    @discord.ui.button(label="Add Reward Stock",style=discord.ButtonStyle.success,emoji="📦",row=1)
    async def add_stock(self,i,b):
        categories = get_categories(i.guild.id)
        if not categories:
            return await i.response.send_message("No categories are configured yet. Add a category first.", ephemeral=True)
        await i.response.send_message("Choose the category for this stock:", view=StockCategorySelectView(categories), ephemeral=True)
    @discord.ui.button(label="Pending Claims",style=discord.ButtonStyle.secondary,emoji="⏳",row=1)
    async def pending(self,i,b):
        c=database()
        rows=[dict(x) for x in c.execute("SELECT * FROM trg_stock_claims WHERE guild=? AND status IN ('pending','approved') ORDER BY created_at LIMIT 25",(i.guild.id,)).fetchall()]
        c.close()
        if not rows: return await i.response.send_message("No pending claims.",ephemeral=True)
        view=PendingStockClaimsView(rows)
        await i.response.send_message("Select a pending claim, then choose Deliver or Reject.",view=view,ephemeral=True)
    @discord.ui.button(label="Edit Fields",style=discord.ButtonStyle.primary,emoji="✏️",row=2)
    async def edit_fields(self,i,b):
        if not self.selected_stock_id:
            return await i.response.send_message("Select a stock entry from the dropdown first.",ephemeral=True)
        c=database()
        row=c.execute("SELECT fields_json FROM trg_stock_items WHERE id=? AND guild=?",(self.selected_stock_id,i.guild.id)).fetchone()
        c.close()
        if not row: return await i.response.send_message("Stock entry not found.",ephemeral=True)
        modal=EditStockFieldsModal()
        modal.item_id.default=str(self.selected_stock_id)
        modal.fields.default=row["fields_json"][:3500]
        await i.response.send_modal(modal)
    @discord.ui.button(label="Remove Stock Entry",style=discord.ButtonStyle.danger,emoji="🗑️",row=2)
    async def remove_stock(self,i,b):
        if not self.selected_stock_id:
            return await i.response.send_message("Select a stock entry from the dropdown first.",ephemeral=True)
        c=database()
        changed=c.execute("DELETE FROM trg_stock_items WHERE id=? AND guild=?",(self.selected_stock_id,i.guild.id)).rowcount
        c.commit(); c.close()
        self.selected_stock_id=None
        await i.response.send_message("Stock entry removed." if changed else "Stock entry not found.",ephemeral=True)
    @discord.ui.button(label="Refresh Dashboard",style=discord.ButtonStyle.secondary,emoji="🔄",row=2)
    async def refresh(self,i,b):
        await i.response.edit_message(embed=stock_dashboard_embed(i.guild.id),view=StockManageView(i.guild.id))


class PendingStockClaimsView(discord.ui.View):
    def __init__(self,rows):
        super().__init__(timeout=300)
        self.selected_claim=None
        self.add_item(PendingStockClaimSelect(rows))
    async def interaction_check(self,i):
        if not is_guild_owner(i):
            await i.response.send_message("Only the server owner can manage claims.",ephemeral=True); return False
        return True
    @discord.ui.button(label="Deliver Selected",style=discord.ButtonStyle.success,emoji="📨")
    async def deliver(self,i,b):
        if not self.selected_claim: return await i.response.send_message("Select a claim first.",ephemeral=True)
        await i.response.defer(ephemeral=True,thinking=True)
        try: result=await deliver_real_reward(i.guild,self.selected_claim,str(i.user.id))
        except Exception as ex: result="⚠️ "+str(ex)[:250]
        await i.followup.send(result,ephemeral=True)
    @discord.ui.button(label="Approve Selected",style=discord.ButtonStyle.primary,emoji="✅")
    async def approve(self,i,b):
        if not self.selected_claim: return await i.response.send_message("Select a claim first.",ephemeral=True)
        c=database()
        changed=c.execute("UPDATE trg_stock_claims SET status='approved',reviewed_by=? WHERE id=? AND guild=? AND status='pending'",
                          (i.user.id,self.selected_claim,i.guild.id)).rowcount
        c.commit(); c.close()
        await i.response.send_message("Claim approved. Use Deliver Selected when stock is active." if changed else "Claim was not pending.",ephemeral=True)
    @discord.ui.button(label="Reject Selected",style=discord.ButtonStyle.danger,emoji="✖️")
    async def reject(self,i,b):
        if not self.selected_claim: return await i.response.send_message("Select a claim first.",ephemeral=True)
        c=database()
        row=c.execute("SELECT * FROM trg_stock_claims WHERE id=? AND guild=? AND status='pending'",(self.selected_claim,i.guild.id)).fetchone()
        if not row:
            c.close(); return await i.response.send_message("Claim not found or no longer pending.",ephemeral=True)
        c.execute("UPDATE trg_stock_claims SET status='rejected',reviewed_by=? WHERE id=?",(i.user.id,self.selected_claim))
        c.commit(); c.close()
        ticket=i.guild.get_channel(int(row["ticket_channel_id"]))
        if isinstance(ticket,discord.TextChannel):
            await ticket.send("❌ Your reward claim was rejected by the server owner. This ticket will be deleted in 30 seconds.")
            await asyncio.sleep(30)
            try: await ticket.delete(reason="Reward claim rejected by server owner")
            except Exception: pass
        await i.response.send_message("Claim rejected.",ephemeral=True)


def stock_dashboard_embed(guild_id):
    c=database()
    active=c.execute("SELECT COUNT(*) n FROM trg_stock_items WHERE guild=? AND active=1",(guild_id,)).fetchone()["n"]
    paused=c.execute("SELECT COUNT(*) n FROM trg_stock_items WHERE guild=? AND active=0",(guild_id,)).fetchone()["n"]
    pending=c.execute("SELECT COUNT(*) n FROM trg_stock_claims WHERE guild=? AND status IN ('pending','approved')",(guild_id,)).fetchone()["n"]
    delivered=c.execute("SELECT COUNT(*) n FROM trg_stock_claims WHERE guild=? AND status='delivered'",(guild_id,)).fetchone()["n"]
    items=c.execute("SELECT category_key,reward_name,active FROM trg_stock_items WHERE guild=? ORDER BY id DESC LIMIT 12",(guild_id,)).fetchall()
    c.close()
    e=discord.Embed(title="📦 Trigger Rewards • Stock Manager",description="Manage shared reward entries. Active stock is reusable and is never automatically consumed.",color=0xf2b84b)
    e.add_field(name="Active rewards",value=str(active),inline=True)
    e.add_field(name="Paused rewards",value=str(paused),inline=True)
    e.add_field(name="Pending claims",value=str(pending),inline=True)
    e.add_field(name="Successful deliveries",value=str(delivered),inline=True)
    e.add_field(name="Recent stock entries",value="\n".join(f"• {x['category_key']} / **{x['reward_name']}** — {'Active' if x['active'] else 'Paused'}" for x in items) or "No stock entries yet.",inline=False)
    e.set_footer(text="Only the server owner can use these controls.")
    return e


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
        # Allocate an increasing numeric ticket name, e.g. ticket-323.
        # Reserve the number before the Discord API call to avoid duplicates on concurrent opens.
        c.execute("BEGIN IMMEDIATE")
        c.execute(
            "INSERT INTO ticket_counters(guild, last_number) VALUES(?, 0) "
            "ON CONFLICT(guild) DO NOTHING",
            (i.guild.id,)
        )
        c.execute(
            "UPDATE ticket_counters SET last_number = last_number + 1 WHERE guild=?",
            (i.guild.id,)
        )
        ticket_number = c.execute(
            "SELECT last_number FROM ticket_counters WHERE guild=?",
            (i.guild.id,)
        ).fetchone()["last_number"]
        c.commit()
        try:
            ch = await i.guild.create_text_channel(
                f"ticket-{ticket_number}", category=cat, overwrites=overwrites,
                topic=f"Reward ticket number={ticket_number} user={i.user.id}"
            )
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

class TicketInviteView(discord.ui.View):
    def __init__(self, owner_id):
        super().__init__(timeout=900)
        self.owner_id = int(owner_id)

    @discord.ui.button(label="Recheck Invites", style=discord.ButtonStyle.primary, emoji="🔄")
    async def recheck(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or interaction.user.id != self.owner_id:
            return await interaction.response.send_message("This invite check belongs to the ticket owner.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        result = await verify_ticket_from_tracker(interaction.channel, interaction.user, announce=True)
        if result == "verified":
            await interaction.followup.send("✅ Invite requirement verified. Check the ticket for the next steps.", ephemeral=True)
        elif result == "waiting":
            await interaction.followup.send("Your invite count is still below the requirement. The ticket remains open.", ephemeral=True)
        elif result == "already":
            await interaction.followup.send("A claim is already recorded for this ticket.", ephemeral=True)
        else:
            await interaction.followup.send("I couldn't complete the check. Staff should review the ticket and bot logs.", ephemeral=True)


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
        e=discord.Embed(title="📋 Automatic Invite Verification",description=f"**Reward:** {name}\n**Required eligible invites:** {r['invite_requirement']}\n\nTrigger Rewards checks your tracked invites automatically. If you are short, keep this ticket open; it will recheck when a new invite is attributed to you. You can also press **Recheck Invites**.",color=0x5865f2)
        e.set_footer(text="Only invites tracked after setup can be counted. New-account warnings are age-based indicators, not proof.")
        await i.response.send_message(embed=e,view=TicketInviteView(i.user.id))
        await verify_ticket_from_tracker(i.channel, i.user, announce=True)


_invite_ticket_checks = set()

async def verify_ticket_from_tracker(channel, member, announce=True):
    """Verify invite requirements and create a reusable-stock claim."""
    if not channel or not getattr(channel,"guild",None) or not isinstance(member,discord.Member): return "error"
    key=(channel.guild.id,channel.id)
    if key in _invite_ticket_checks: return "waiting"
    _invite_ticket_checks.add(key)
    try:
        c=database()
        ticket=c.execute("SELECT * FROM tickets WHERE channel=? AND guild=? AND user=? AND status='open'",
                         (channel.id,channel.guild.id,member.id)).fetchone()
        if not ticket or not ticket["reward"] or not ticket["category"]:
            c.close(); return "error"
        existing=c.execute("SELECT id,status FROM trg_stock_claims WHERE ticket_channel_id=?",(channel.id,)).fetchone()
        if existing: c.close(); return "already"
        c.close()
        rewards=get_rewards(channel.guild.id,ticket["category"])
        reward=next((x for x in rewards if x["reward_name"]==ticket["reward"]),None)
        if not reward:
            if announce: await channel.send("⚠️ I couldn't find this reward's current invite requirement. Staff should check the reward configuration.")
            return "error"
        required=max(0,int(reward.get("invite_requirement") or 0))
        stats=invite_tracker.stats(channel.guild.id,member.id)
        tracked_total=int(stats["eligible_estimate"])
        c=database()
        base=c.execute("SELECT baseline FROM tracked_invite_baselines WHERE guild=? AND user=?",(channel.guild.id,member.id)).fetchone()
        baseline=int(base["baseline"]) if base else 0
        remaining=cooldown_seconds(c,channel.guild.id,member.id,ticket["reward"])
        c.close()
        available=max(0,tracked_total-baseline)
        if remaining:
            if announce: await channel.send(f"⏳ **{ticket['reward']}** is on cooldown for this member. Try again in **{remaining//60}:{remaining%60:02d}**.")
            return "waiting"
        if available<required:
            if announce:
                await upsert_ticket_status(
                    channel, "📨 Invite Progress",
                    f"{member.mention}, your tracked invite check is still in progress.",
                    color=0xf1c40f,
                    fields=[
                        ("Eligible tracked invites", f"**{available}/{required}**", True),
                        ("Still needed", f"**{max(0, required-available)}**", True),
                        ("What happens next", "Keep this ticket open. I'll automatically recheck when a new invite is attributed. You can also use **Recheck Invites**.", False)
                    ]
                )
            return "waiting"
        now=datetime.now(timezone.utc).isoformat()
        c=database()
        pending=c.execute("SELECT COUNT(*) AS n FROM trg_stock_claims WHERE guild=? AND user_id=? AND status IN ('pending','approved')",
                          (channel.guild.id,member.id)).fetchone()["n"]
        if pending:
            c.close()
            if announce: await channel.send("You already have a pending reward claim. Please wait for it to be completed.")
            return "already"
        c.execute("INSERT INTO trg_stock_claims(guild,user_id,username,category_key,reward_name,ticket_channel_id,status,created_at,invite_cost) VALUES(?,?,?,?,?,?,?,?,?)",
                  (channel.guild.id,member.id,member.display_name[:100],ticket["category"],ticket["reward"],channel.id,"pending",now,required))
        claim_id=c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.execute("INSERT INTO reward_cooldowns(guild,user,reward,last_claim) VALUES(?,?,?,?) ON CONFLICT(guild,user,reward) DO UPDATE SET last_claim=excluded.last_claim",
                  (channel.guild.id,member.id,ticket["reward"],now))
        stock_count=c.execute("SELECT COUNT(*) AS n FROM trg_stock_items WHERE guild=? AND category_key=? AND reward_name=? AND active=1",
                              (channel.guild.id,ticket["category"],ticket["reward"])).fetchone()["n"]
        c.commit(); c.close()
        if announce:
            configured_text = render_ticket_message(
                channel.guild.id, "invite_verified_message", DEFAULT_INVITE_VERIFIED_MESSAGE,
                reward=discord.utils.escape_markdown(ticket["reward"]), user=member.mention
            )
            await upsert_ticket_status(
                channel, "✅ Invite Requirement Verified",
                configured_text,
                color=0x2ecc71,
                fields=[
                    ("Verification", "Complete the server verification in #verification if you have not already.", False),
                    ("Vouch step", "After receiving your reward, reply **Legit** here to add your name to the vouch database.", False),
                    ("Reward stock", "Checking available shared stock…", False)
                ]
            )
        if stock_count:
            try:
                await deliver_real_reward(channel.guild,claim_id,"auto")
            except discord.Forbidden:
                if announce:
                    await upsert_ticket_status(
                        channel, "⏳ Reward Claim Pending",
                        "Your claim is saved, but I couldn't send you a DM. Enable DMs from this server; staff can retry delivery.",
                        color=0xf1c40f,
                        fields=[("Claim status", "Pending DM delivery", True),
                                ("Next step", "Enable server DMs and wait for staff/bot to retry.", False)]
                    )
            except Exception as ex:
                print("Automatic stock delivery failed:",repr(ex))
                if announce:
                    await upsert_ticket_status(
                        channel, "⏳ Reward Claim Pending",
                        "Your claim is saved. Staff can retry delivery from **/stock-manage**.",
                        color=0xf1c40f,
                        fields=[("Claim status", "Pending", True),
                                ("Reward", discord.utils.escape_markdown(ticket["reward"]), True)]
                    )
        else:
            if announce:
                await upsert_ticket_status(
                    channel, "⏳ Reward Claim Pending",
                    "Your claim is saved because this reward currently has no active stock. It will be delivered automatically when matching stock is added.",
                    color=0xf1c40f,
                    fields=[("Claim status", "Pending stock", True),
                            ("Reward", discord.utils.escape_markdown(ticket["reward"]), True),
                            ("Next step", "No action needed. Keep this ticket open; the bot will retry when stock is added.", False)]
                )
        return "verified"
    except Exception as ex:
        print("Tracked invite ticket verification failed:",repr(ex))
        if announce:
            try: await channel.send("⚠️ Automatic invite verification hit an error. This ticket remains open; staff should review it.")
            except Exception: pass
        return "error"
    finally:
        _invite_ticket_checks.discard(key)


async def on_invite_attributed(invitee, inviter_id, event):
    """Re-check the inviter's open reward ticket immediately after a tracked join."""
    guild = invitee.guild
    member = guild.get_member(int(inviter_id))
    if not member:
        return
    c = database()
    rows = c.execute(
        "SELECT channel FROM tickets WHERE guild=? AND user=? AND status='open' AND reward IS NOT NULL",
        (guild.id, member.id),
    ).fetchall()
    c.close()
    for row in rows:
        channel = guild.get_channel(int(row["channel"]))
        if isinstance(channel, discord.TextChannel):
            await verify_ticket_from_tracker(channel, member, announce=True)


bot.add_listener(on_invite_attributed, "on_invite_attributed")


async def auto_verify_falcon_report(message):
    """Handle Falcon invite-log embeds without generating website verification codes."""
    if not message.guild or not isinstance(message.channel,discord.TextChannel) or not message.author.bot or not message.embeds:
        return
    embed=message.embeds[0]
    if (embed.title or "").strip().lower()!="invite log":
        return
    requested=re.search(r"Requested by\\s+(.+?)(?:\\s*\\||$)",embed.footer.text or "",re.I)
    if not requested: return
    c=database()
    ticket=c.execute("SELECT * FROM tickets WHERE channel=? AND guild=? AND status='open'",(message.channel.id,message.guild.id)).fetchone()
    c.close()
    if not ticket or not ticket["reward"]: return
    member=message.guild.get_member(int(ticket["user"]))
    if not member: return
    name=requested.group(1).strip().lstrip("@").lower()
    if name not in {member.name.lower(),member.display_name.lower()}: return
    await verify_ticket_from_tracker(message.channel,member,announce=True)


async def capture_legit_evidence(message):
    """Post the member's Legit acknowledgement and remove the ticket after 15 seconds."""
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
        "SELECT id FROM trg_stock_claims WHERE guild=? AND user_id=? AND reward_name=? AND status='delivered' ORDER BY id DESC LIMIT 1",
        (message.guild.id, message.author.id, ticket["reward"])
    ).fetchone()
    c.close()
    if not local_claim:
        return

    # Prevent duplicate vouch posts while preserving the original ticket-owner checks.
    c = database()
    c.execute("UPDATE tickets SET status='legit_received' WHERE channel=?", (message.channel.id,))
    c.commit()
    c.close()

    try:
        # Use the configured vouch channel; allow the host environment to override the default.
        vouch_target = await get_vouch_channel(message.guild)
        if vouch_target is None:
            raise RuntimeError("No valid vouch channel is configured for this server. Run /set-vouch-channel and select a text channel.")
        configured_channel_id = vouch_target.id
        bot_member = message.guild.me or await message.guild.fetch_member(bot.user.id)
        perms = vouch_target.permissions_for(bot_member)
        if not perms.view_channel or not perms.send_messages or not perms.embed_links:
            raise RuntimeError(f"Bot needs View Channel, Send Messages and Embed Links in vouch channel {configured_channel_id}.")

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
                f"**Vouch:** Legit\n\n"
                f"**Vouch message:** {rendered[:1800]}\n\n"
                "Reward follow-up: the reward will be provided shortly. "
                "Verification details: https://discord.com/channels/1499228791220863110/1499322272861913169"
            ),
            color=0xf2b84b
        )
        embed.set_footer(text="Trigger Rewards • Member acknowledgement")
        await vouch_target.send(embed=embed)

        c = database()
        c.execute(
            "INSERT OR IGNORE INTO vouches(guild,user,reward,ticket_channel,confirmed_at) VALUES(?,?,?,?,?)",
            (message.guild.id, message.author.id, ticket["reward"], message.channel.id, datetime.now(timezone.utc).isoformat())
        )
        c.commit()
        c.close()

        await message.channel.send(
            f"✅ Thanks! Your vouch has been posted in {vouch_target.mention}. "
            "Your reward will be provided shortly. This ticket will be deleted in 15 seconds."
        )
        await asyncio.sleep(15)
        c = database()
        c.execute("UPDATE tickets SET status='closed' WHERE channel=?", (message.channel.id,))
        c.commit()
        c.close()
        try:
            await message.channel.delete(reason="Member submitted Legit acknowledgement")
        except discord.Forbidden:
            print("Cannot delete ticket: bot needs Manage Channels permission.")
    except Exception as ex:
        print(f"Could not post Legit acknowledgement to channel {VOUCH_MESSAGE_CHANNEL_ID}:", repr(ex))
        c = database()
        c.execute("UPDATE tickets SET status='open' WHERE channel=?", (message.channel.id,))
        c.commit()
        c.close()
        try:
            await message.channel.send(
                f"⚠️ I couldn't post your vouch: {type(ex).__name__}: {str(ex)[:250]} "
                "Please check the vouch channel ID and the bot's View Channel, Send Messages and Embed Links permissions."
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

class DeleteAllTicketsView(discord.ui.View):
    def __init__(self, requester_id):
        super().__init__(timeout=60)
        self.requester_id = requester_id

    async def interaction_check(self, i: discord.Interaction):
        if not i.guild or not i.user.guild_permissions.administrator:
            await i.response.send_message("🔒 Server administrators only.", ephemeral=True)
            return False
        if i.user.id != self.requester_id:
            await i.response.send_message("Only the administrator who started this action can confirm it.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm delete all tickets", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def confirm_delete(self, i: discord.Interaction, button: discord.ui.Button):
        await i.response.edit_message(content="🗑️ Deleting tracked reward ticket channels…", view=None)
        c = database()
        rows = c.execute("SELECT channel FROM tickets WHERE guild=? AND status!='deleted'", (i.guild.id,)).fetchall()
        channel_ids = [int(row["channel"]) for row in rows]
        deleted = missing = failed = 0
        for channel_id in channel_ids:
            channel = i.guild.get_channel(channel_id)
            if channel is None:
                missing += 1
                c.execute("UPDATE tickets SET status='deleted' WHERE guild=? AND channel=?", (i.guild.id, channel_id))
                continue
            try:
                await channel.delete(reason=f"Bulk ticket cleanup requested by {i.user} ({i.user.id})")
                deleted += 1
                c.execute("UPDATE tickets SET status='deleted' WHERE guild=? AND channel=?", (i.guild.id, channel_id))
            except discord.NotFound:
                missing += 1
                c.execute("UPDATE tickets SET status='deleted' WHERE guild=? AND channel=?", (i.guild.id, channel_id))
            except (discord.Forbidden, discord.HTTPException):
                failed += 1
        c.commit()
        c.close()
        await i.followup.send(
            f"✅ Ticket cleanup finished. Deleted: **{deleted}** · Already missing: **{missing}** · Failed: **{failed}**. "
            "Only channels recorded as Trigger Rewards tickets were targeted; other server channels were untouched.",
            ephemeral=True
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def cancel_delete(self, i: discord.Interaction, button: discord.ui.Button):
        await i.response.edit_message(content="Cancelled. No ticket channels were deleted.", view=None)


@bot.event
async def on_message(message):
    await auto_verify_falcon_report(message)
    await capture_legit_evidence(message)
    await bot.process_commands(message)

@bot.command(name="sync")
@commands.guild_only()
@commands.has_guild_permissions(administrator=True)
async def sync_commands(ctx):
    total = 0
    results = []
    try:
        # Sync this bot's global slash commands into every server it currently belongs to.
        for server in bot.guilds:
            guild_ref = discord.Object(id=server.id)
            bot.tree.copy_global_to(guild=guild_ref)
            synced = await bot.tree.sync(guild=guild_ref)
            total += len(synced)
            results.append(f"{server.name}: {len(synced)}")
        summary = " · ".join(results) if results else "No servers found"
        await ctx.reply(
            f"✅ Synced **{total}** slash-command entries across **{len(results)}** server(s).\n{summary}",
            mention_author=False
        )
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
    print(f"Ready as {bot.user}; connected to {len(bot.guilds)} server(s).")
    # Publish the same slash commands separately to every server this bot has joined.
    for server in bot.guilds:
        try:
            guild_ref = discord.Object(id=server.id)
            bot.tree.copy_global_to(guild=guild_ref)
            synced = await bot.tree.sync(guild=guild_ref)
            print(f"Synced {len(synced)} slash commands to {server.name} ({server.id})")
        except Exception as e:
            print(f"Command sync error for {server.name} ({server.id}): {e}")


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


class DemoVouchTemplateModal(discord.ui.Modal):
    def __init__(self, current_template):
        super().__init__(title="Edit Sample Vouch")
        self.template = discord.ui.TextInput(
            label="Sample embed text (all lines editable)",
            style=discord.TextStyle.paragraph,
            default=current_template[:4000],
            placeholder="Edit the sample embed text. A small sample disclosure is added automatically in the embed footer.",
            max_length=4000,
            required=True
        )
        self.add_item(self.template)

    async def on_submit(self, i: discord.Interaction):
        if not i.guild or not i.user.guild_permissions.administrator:
            return await i.response.send_message("Administrator only.", ephemeral=True)
        template = str(self.template).strip()
        allowed = {"reward", "user", "ticket"}
        placeholders = re.findall(r"\{([^{}]+)\}", template)
        if not template:
            return await i.response.send_message("Sample message cannot be empty.", ephemeral=True)
        if any(name not in allowed for name in placeholders):
            return await i.response.send_message(
                "Unsupported placeholder. Use only {reward}, {user}, and {ticket}.",
                ephemeral=True
            )
        try:
            rendered = template.format(reward="SAMPLE REWARD", user="SampleUser", ticket="Preview")
        except (KeyError, ValueError, IndexError):
            return await i.response.send_message(
                "Invalid braces or placeholders. Use only {reward}, {user}, and {ticket}.",
                ephemeral=True
            )
        if len(rendered) > 4096:
            return await i.response.send_message("The rendered message is too long for a Discord embed description (4096 characters max).", ephemeral=True)
        try:
            save_demo_vouch_template(i.guild.id, template)
            await i.response.send_message(
                "✅ Sample-vouch template saved. The embed uses a distinct colour and a small sample disclosure in its footer.",
                ephemeral=True,
                view=AdminPanel()
            )
        except Exception as ex:
            await i.response.send_message("Could not save demo vouch message: " + str(ex)[:180], ephemeral=True)


class TicketMessagesModal(discord.ui.Modal, title="Edit Ticket Messages"):
    def __init__(self, invite_message, delivery_message):
        super().__init__(title="Edit Ticket Messages")
        self.invite_message = discord.ui.TextInput(
            label="After invite requirement verified",
            style=discord.TextStyle.paragraph,
            default=invite_message[:500],
            max_length=500
        )
        self.delivery_message = discord.ui.TextInput(
            label="After actual reward DM sent",
            style=discord.TextStyle.paragraph,
            default=delivery_message[:500],
            max_length=500
        )
        self.add_item(self.invite_message)
        self.add_item(self.delivery_message)

    async def on_submit(self, i: discord.Interaction):
        if not i.guild or not i.user.guild_permissions.administrator:
            return await i.response.send_message("Administrator only.", ephemeral=True)
        invite_text = str(self.invite_message).strip()
        delivery_text = str(self.delivery_message).strip()
        if not invite_text or not delivery_text:
            return await i.response.send_message("Both messages must contain text.", ephemeral=True)
        allowed = {"reward", "user"}
        for template in (invite_text, delivery_text):
            placeholders = re.findall(r"\{([^{}]+)\}", template)
            if any(name not in allowed for name in placeholders):
                return await i.response.send_message("Unsupported placeholder. Use only {reward} and {user}.", ephemeral=True)
            try:
                template.format(reward="Reward", user="@member")
            except (KeyError, ValueError, IndexError):
                return await i.response.send_message("Invalid message formatting. Check your braces and placeholders.", ephemeral=True)
        try:
            save_ticket_messages(i.guild.id, invite_text, delivery_text)
            await i.response.send_message(
                "✅ Ticket messages saved. New tickets will use your updated wording. Placeholders: {reward}, {user}.",
                ephemeral=True, view=AdminPanel()
            )
        except Exception as ex:
            await i.response.send_message("Could not save ticket messages: " + str(ex)[:180], ephemeral=True)


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

    @discord.ui.button(label="Edit Demo Vouch", style=discord.ButtonStyle.secondary, emoji="🧪", row=2)
    async def edit_demo_vouch_message(self, i: discord.Interaction, b: discord.ui.Button):
        try:
            await i.response.send_modal(DemoVouchTemplateModal(get_demo_vouch_template(i.guild.id)))
        except Exception as ex:
            await i.response.send_message("Could not open demo vouch editor: " + str(ex)[:180], ephemeral=True)

    @discord.ui.button(label="Edit Ticket Messages", style=discord.ButtonStyle.primary, emoji="💬", row=2)
    async def edit_ticket_messages(self, i: discord.Interaction, b: discord.ui.Button):
        try:
            await i.response.send_modal(TicketMessagesModal(
                get_ticket_message(i.guild.id, "invite_verified_message", DEFAULT_INVITE_VERIFIED_MESSAGE),
                get_ticket_message(i.guild.id, "delivery_confirmation_message", DEFAULT_DELIVERY_CONFIRMATION_MESSAGE)
            ))
        except Exception as ex:
            await i.response.send_message("Could not open ticket message editor: " + str(ex)[:180], ephemeral=True)


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

@bot.tree.command(name="stock-manage", description="Open the owner-only shared reward stock manager")
@app_commands.guild_only()
async def stock_manage(i: discord.Interaction):
    if not is_guild_owner(i):
        return await i.response.send_message("🔒 Only the server owner can manage reward stock.", ephemeral=True)
    await i.response.send_message(embed=stock_dashboard_embed(i.guild.id),view=StockManageView(i.guild.id),ephemeral=True)


@bot.tree.command(name="stock-category", description="Rename or deactivate a custom stock category")
@app_commands.guild_only()
@app_commands.choices(action=[app_commands.Choice(name="Rename",value="rename"),app_commands.Choice(name="Delete",value="delete")])
async def stock_category(i: discord.Interaction, action: app_commands.Choice[str], category_key: str,
                         new_name: str = "", emoji: str = ""):
    if not is_guild_owner(i):
        return await i.response.send_message("🔒 Only the server owner can manage stock categories.",ephemeral=True)
    key=re.sub(r"[^a-z0-9_-]","",category_key.lower().strip())
    c=database()
    row=c.execute("SELECT * FROM trg_stock_categories WHERE guild=? AND category_key=?",(i.guild.id,key)).fetchone()
    if not row:
        c.close()
        return await i.response.send_message("Only custom stock categories created in /stock-manage can be renamed or deleted.",ephemeral=True)
    if action.value=="rename":
        if not new_name or not new_name.strip():
            c.close(); return await i.response.send_message("Provide new_name when renaming.",ephemeral=True)
        c.execute("UPDATE trg_stock_categories SET label=?,emoji=? WHERE guild=? AND category_key=?",
                  (new_name.strip()[:80],(emoji or row["emoji"])[:16],i.guild.id,key))
        c.commit(); c.close()
        return await i.response.send_message(f"Category **{key}** renamed to **{new_name.strip()}**.",ephemeral=True)
    c.execute("UPDATE trg_stock_categories SET active=0 WHERE guild=? AND category_key=?",(i.guild.id,key))
    c.execute("UPDATE trg_stock_items SET active=0,updated_at=? WHERE guild=? AND category_key=?",
              (datetime.now(timezone.utc).isoformat(),i.guild.id,key))
    c.commit(); c.close()
    await i.response.send_message(f"Category **{key}** deactivated and its stock paused. Existing records are retained.",ephemeral=True)


@bot.tree.command(name="admin-panel", description="Open the embedded Trigger Rewards admin control panel")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def admin_panel(i: discord.Interaction):
    if not i.guild or not i.user.guild_permissions.administrator:
        return await i.response.send_message("🔒 Server administrators only.", ephemeral=True)
    e = discord.Embed(title="⚙️ TRIGGER REWARDS • ADMIN CONTROL", description="Manage reward categories, invite requirements and ticket setup directly inside Discord. No admin website required.\n\nCategories — add or edit the category label and emoji.\nRewards — add or edit inner rewards, invite requirements, and reward text.\nValid codes — use Add Valid Code to choose the category, exact reward, and accepted code.\nReal vouch message — edit the genuine vouch template; placeholders: {reward}, {user}, {ticket}.\nSample vouch — edit the sample embed text; it uses a distinct colour and a small sample disclosure in its footer.\nTicket messages — edit invite-verified and actual-delivery messages; placeholders: {reward}, {user}.\nView Setup — inspect active categories and rewards.", color=0xf2b84b)
    e.set_footer(text="Admin-only controls • Changes sync to the public rewards website")
    await i.response.send_message(embed=e, view=AdminPanel(), ephemeral=True)


@bot.tree.command(name="vouch-add", description="Post clearly labelled demo vouches for layout testing")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def vouch_add(i: discord.Interaction, number: app_commands.Range[int, 1, 1000]):
    """Generate demo-only sample vouches; these are never genuine customer testimonials."""
    if not i.guild or not i.user.guild_permissions.administrator:
        return await i.response.send_message("Administrator only.", ephemeral=True)
    target = await get_vouch_channel(i.guild)
    if target is None:
        return await i.response.send_message(
            "No valid vouch channel is configured for this server. Run `/set-vouch-channel` and select the channel where demo vouches should appear.",
            ephemeral=True
        )

    # Defer before the batch send loop so Discord does not expire the interaction.
    await i.response.defer(ephemeral=True, thinking=True)

    first_names = ["Nova", "Pixel", "Shadow", "Lunar", "Frost", "Blaze", "Echo", "Astra", "Raven", "Cobalt"]
    second_names = ["Fox", "Wolf", "Byte", "Craft", "Spark", "Knight", "Cloud", "Ninja", "Drift", "Quest"]
    providers = ["MCFA", "Nitro 1M", "Nitro 3M", "MCFA Permanent"]
    posted = 0
    failures = []
    for index in range(number):
        username = f"{secrets.choice(first_names)}{secrets.choice(second_names)}{secrets.randbelow(9000) + 1000}"
        provider = secrets.choice(providers)
        safe_username = discord.utils.escape_mentions(discord.utils.escape_markdown(username))
        safe_provider = discord.utils.escape_mentions(discord.utils.escape_markdown(provider))
        try:
            template = get_demo_vouch_template(i.guild.id)
            rendered = template.format(reward=safe_provider, user=safe_username, ticket="Preview")
            if len(rendered) > 4096:
                raise ValueError("Template must be 4096 characters or fewer")
            embed = discord.Embed(description=rendered, color=0xf1c40f)
            embed.set_footer(text="Trigger Rewards • Member acknowledgement")
            await target.send(embed=embed)
            posted += 1
            await asyncio.sleep(0.5)
        except Exception as ex:
            failures.append(f"{index + 1}: {type(ex).__name__}")
    summary = f"Posted **{posted}/{number}** sample preview embeds in {target.mention}."
    if failures:
        summary += "\nFailed: " + ", ".join(failures[:10])
    await i.followup.send(summary, ephemeral=True)


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
    target = await get_vouch_channel(i.guild)
    if target is None:
        return await i.response.send_message(
            "No valid vouch channel is configured for this server. Run `/set-vouch-channel` and select the channel where demo vouches should appear.",
            ephemeral=True
        )

    safe_username = discord.utils.escape_mentions(discord.utils.escape_markdown(username.strip()))
    safe_provider = discord.utils.escape_mentions(discord.utils.escape_markdown(user_provider.strip()))
    try:
        template = get_demo_vouch_template(i.guild.id)
        rendered = template.format(reward=safe_provider, user=safe_username, ticket="Preview")
    except (KeyError, ValueError, IndexError):
        return await i.response.send_message(
            "Your saved demo template could not be rendered. Check /admin-panel → Edit Demo Vouch.",
            ephemeral=True
        )
    if len(rendered) > 4096:
        return await i.response.send_message(
            "Your sample template must be 4096 characters or fewer. Edit it in /admin-panel.",
            ephemeral=True
        )
    embed = discord.Embed(description=rendered, color=0x9b59b6)
    embed.set_footer(text="Trigger Rewards • Sample preview — not a verified customer vouch")
    try:
        await target.send(embed=embed)
        await i.response.send_message(
            f"✅ Sample preview posted in {target.mention}. It does not count as a verified customer vouch.",
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

@bot.tree.command(name="set-vouch-channel", description="Set this server's proof/vouch message channel")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def set_vouch_channel(i: discord.Interaction, channel: discord.TextChannel):
    if not i.guild or not i.user.guild_permissions.administrator:
        return await i.response.send_message("🔒 Server administrators only.", ephemeral=True)

    # Ensure the bot can post proof embeds in the selected channel.
    me = i.guild.me
    if me is None:
        return await i.response.send_message("I couldn't verify my permissions in that channel. Please try again.", ephemeral=True)
    perms = channel.permissions_for(me)
    if not (perms.view_channel and perms.send_messages and perms.embed_links):
        return await i.response.send_message(
            f"I need **View Channel**, **Send Messages**, and **Embed Links** in {channel.mention} before it can be used.",
            ephemeral=True,
        )

    c = database()
    c.execute(
        "INSERT INTO settings(guild, vouch_channel) VALUES(?, ?) "
        "ON CONFLICT(guild) DO UPDATE SET vouch_channel=excluded.vouch_channel",
        (i.guild.id, channel.id),
    )
    c.commit()
    c.close()
    await i.response.send_message(
        f"✅ Proof/vouch messages for **{i.guild.name}** will now be posted in {channel.mention}.",
        ephemeral=True,
    )


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

@bot.tree.command(name="delete-all", description="Delete all Trigger Rewards ticket channels in this server")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def delete_all_tickets(i: discord.Interaction):
    if not i.guild or not i.user.guild_permissions.administrator:
        return await i.response.send_message("🔒 Server administrators only.", ephemeral=True)
    c = database()
    count = c.execute("SELECT COUNT(*) AS total FROM tickets WHERE guild=? AND status!='deleted'", (i.guild.id,)).fetchone()["total"]
    c.close()
    if not count:
        return await i.response.send_message("There are no tracked Trigger Rewards ticket channels to delete.", ephemeral=True)
    await i.response.send_message(
        f"⚠️ This will permanently delete **{count}** channel(s) recorded as Trigger Rewards tickets. It will not touch other server channels. Confirm?",
        view=DeleteAllTicketsView(i.user.id), ephemeral=True
    )


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
