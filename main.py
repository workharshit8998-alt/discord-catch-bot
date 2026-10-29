import os, re, sqlite3, secrets, json, asyncio, urllib.request, urllib.parse, urllib.error, textwrap
from io import BytesIO
from datetime import datetime, timezone
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont

load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
SITE_URL = os.getenv("DEMO_URL", "https://trigger-rewards.vercel.app").strip()
DB_FILE = os.getenv("TICKET_DB_PATH", "trigger_rewards.sqlite3")
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
SYNC_GUILD_ID = int(os.getenv("TRG_GUILD_ID", "1499228791220863110"))
VOUCH_EVIDENCE_CHANNEL_ID = 1499228792089215108  # image/transcript channel
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
                title="🎉 Your reward is approved!",
                description=(
                    f"**Reward:** {t['reward']}\n"
                    f"**Your code:** ||{code}||\n\n"
                    f"[Claim here]({SITE_URL})\n\n"
                    "Keep this code private. It is unique to you and this reward."
                ),
                color=0x57f287
            )
            # Post the same reward embed in the ticket and in the member's DM.
            await message.channel.send(embed=dm)
            try:
                await member.send(embed=dm)
            except discord.Forbidden:
                await message.channel.send(f"{member.mention} I couldn't DM you, so your reward embed is available above in this ticket.")
            await message.channel.send(
                f"**Did you receive your reward?** Reply with **Legit** to submit your vouch.\n"
                f"Example: Legit got {t['reward']} from @TriggerPlayz"
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
        except Exception:
            pass


async def capture_legit_evidence(message):
    """When the ticket owner says Legit after a code was issued, create a transcript image and post it to the evidence channel."""
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
    claim = c.execute(
        "SELECT * FROM claims WHERE guild=? AND user=? AND reward=? AND status='issued' ORDER BY id DESC LIMIT 1",
        (message.guild.id, message.author.id, ticket["reward"])
    ).fetchone()
    if not claim:
        c.close()
        return
    # Mark this ticket as processed before awaiting network operations to avoid duplicate evidence posts.
    c.execute("UPDATE tickets SET status='legit_received' WHERE channel=?", (message.channel.id,))
    c.commit()
    c.close()
    try:
        # Evidence image should contain ONLY the reward-issued message and the member's Legit reply.
        # Never screenshot the whole ticket conversation.
        history = [m async for m in message.channel.history(limit=60, before=message, oldest_first=False)]
        reward_message = None
        for candidate in history:
            if not candidate.author.bot:
                continue
            parts = [candidate.content or ""]
            for emb in candidate.embeds:
                parts.extend([emb.title or "", emb.description or ""])
                parts.extend(f"{field.name} {field.value}" for field in emb.fields)
            combined = "\n".join(parts).casefold()
            has_code = "your code" in combined or "reward code" in combined
            has_reward = ticket["reward"].casefold() in combined
            if has_code and has_reward:
                reward_message = candidate
                break
        evidence_messages = ([reward_message] if reward_message else []) + [message]
        messages = []
        for item in evidence_messages:
            stamp = item.created_at.strftime("%I:%M %p").lstrip("0")
            content = item.content or ""
            for emb in item.embeds[:2]:
                if emb.title:
                    content += ("\n" if content else "") + emb.title
                if emb.description:
                    content += ("\n" if content else "") + emb.description
                for field in emb.fields[:5]:
                    content += ("\n" if content else "") + f"{field.name}: {field.value}"
            if item.attachments:
                content += ("\n" if content else "") + "[attachment: " + ", ".join(a.filename for a in item.attachments[:3]) + "]"
            content = content.strip() or "[embed / attachment]"
            # Render Markdown as readable chat text instead of raw **bold** and link syntax.
            content = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1 (\2)", content)
            content = re.sub(r"<@!?(\d+)>", "@member", content)
            content = re.sub(r"<#(\d+)>", "#channel", content)
            content = content.replace("**", "").replace("__", "").replace("~~", "").replace(chr(96), "")
            content = re.sub(r"(?m)^\s*>\s?", "", content)
            # Never expose a reward code in the public evidence image.
            content = re.sub(r"(?i)(your code\s*:\s*)\|\|.*?\|\|", r"\1[SPOILER HIDDEN]", content)
            content = re.sub(r"(?i)(your code\s*:\s*).*", r"\1[SPOILER HIDDEN]", content)
            # The image is a transcript rendering, not a literal Discord screenshot.
            safe = content.encode("latin-1", "replace").decode("latin-1")
            body = []
            for paragraph in safe.splitlines() or [""]:
                body.extend(textwrap.wrap(paragraph, width=72) or [""])
            avatar_bytes = None
            try:
                avatar_bytes = await item.author.display_avatar.replace(size=64).read()
            except Exception:
                pass
            messages.append((item.author.display_name.encode("latin-1", "replace").decode("latin-1"),
                             stamp, body, item.author.bot, item.author.display_name[:1].upper() or "?", avatar_bytes))
        width, padding = 1000, 34
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 17)
            font_bold = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 17)
            font_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14)
        except OSError:
            font = ImageFont.load_default()
            font_bold = font
            font_small = font
        row_heights = [max(82, 54 + len(body) * 27) for _, _, body, _, _, _ in messages]
        height = max(440, 132 + sum(row_heights) + 80)
        image = Image.new("RGB", (width, min(height, 6000)), (49, 51, 56))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, width, 66), fill=(35, 36, 40))
        draw.text((padding, 22), "#  " + message.channel.name, fill=(245, 245, 247), font=font_bold)
        draw.line((padding, 67, width-padding, 67), fill=(25, 26, 29), width=2)
        draw.text((padding, 86), message.created_at.strftime("%B %d, %Y"), fill=(160, 163, 170), font=font_small)
        y = 122
        palette = [(88, 101, 242), (235, 69, 158), (87, 242, 135), (242, 184, 75)]
        for idx, (author, stamp, body, is_bot, initial, avatar_bytes) in enumerate(messages):
            row_h = row_heights[idx]
            if y + row_h > image.height - 20:
                break
            avatar_color = palette[idx % len(palette)]
            avatar_box = (padding, y + 4, padding + 42, y + 46)
            if avatar_bytes:
                try:
                    avatar = Image.open(BytesIO(avatar_bytes)).convert("RGBA").resize((42, 42))
                    mask = Image.new("L", (42, 42), 0)
                    ImageDraw.Draw(mask).ellipse((0, 0, 41, 41), fill=255)
                    image.paste(avatar, (padding, y + 4), mask)
                except Exception:
                    draw.ellipse(avatar_box, fill=avatar_color)
                    draw.text((padding + 16, y + 16), initial, fill=(255, 255, 255), font=font_bold)
            else:
                draw.ellipse(avatar_box, fill=avatar_color)
                draw.text((padding + 16, y + 16), initial, fill=(255, 255, 255), font=font_bold)
            name_color = (88, 101, 242) if is_bot else (240, 240, 242)
            draw.text((padding + 58, y + 4), author[:42], fill=name_color, font=font_bold if is_bot else font)
            name_width = draw.textbbox((padding + 58, y + 4), author[:42], font=font)[2]
            draw.text((min(name_width + 12, width - 150), y + 6), stamp, fill=(163, 166, 173), font=font_small)
            text_y = y + 30
            for line in body:
                if text_y > image.height - 25:
                    break
                draw.text((padding + 58, text_y), line[:100], fill=(220, 221, 224), font=font)
                text_y += 24
            y += row_h
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        buffer.seek(0)
        target = message.guild.get_channel(VOUCH_EVIDENCE_CHANNEL_ID)
        if not isinstance(target, discord.TextChannel):
            target = await message.guild.fetch_channel(VOUCH_EVIDENCE_CHANNEL_ID)
        embed = discord.Embed(
            title="✅ Reward Vouch Received",
            description=f"**Member:** {message.author.mention}\n**Reward:** {ticket['reward']}\n**Ticket:** #{message.channel.name}\n**Vouch:** Legit\n\n**Vouch message:** {get_vouch_template(message.guild.id).format(reward=ticket['reward'], user=message.author.mention, ticket=message.channel.name)}",
            color=0xf2b84b
        )
        # Send the transcript image to the original evidence channel.
        await target.send(file=discord.File(buffer, filename=f"reward-vouch-{message.author.id}.png"))
        # Send the vouch embed separately to the newly specified vouch channel.
        vouch_target = message.guild.get_channel(VOUCH_MESSAGE_CHANNEL_ID)
        if not isinstance(vouch_target, discord.TextChannel):
            vouch_target = await message.guild.fetch_channel(VOUCH_MESSAGE_CHANNEL_ID)
        await vouch_target.send(embed=embed)
        await message.channel.send("✅ Thanks! Your conversation image and vouch have been posted to their configured channels. This ticket will close in 10 seconds.")
        await asyncio.sleep(10)
        c = database()
        c.execute("UPDATE tickets SET status='closed' WHERE channel=?", (message.channel.id,))
        c.commit()
        c.close()
        try:
            await message.channel.delete(reason="Reward vouch evidence captured")
        except discord.Forbidden:
            pass
    except Exception as ex:
        print("Could not capture reward vouch evidence:", repr(ex))
        c = database()
        c.execute("UPDATE tickets SET status='open' WHERE channel=?", (message.channel.id,))
        c.commit()
        c.close()
        try:
            await message.channel.send("⚠️ I couldn't send the vouch evidence. Please check the bot's channel permissions.")
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
