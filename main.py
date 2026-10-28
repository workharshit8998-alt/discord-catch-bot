import os
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
import urllib.request
import urllib.error
import json

load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
DEMO_URL = os.getenv("DEMO_URL", "https://trigger-rewards.vercel.app").strip()
REPORT_CHANNEL_ID = int(os.getenv("REPORT_CHANNEL_ID", "0") or "0")
SUPABASE_URL = os.getenv(
    "SUPABASE_URL", "https://rhhvpyeowumfludgjcnw.supabase.co"
).strip().rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

class DemoView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Open Demo Website", style=discord.ButtonStyle.primary,
                       custom_id="awareness:open_trigger_demo", emoji="🌐")
    async def open_demo(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            f"Open Trigger Rewards demo: {DEMO_URL}\n\n"
            "This is a demo only. No real rewards are issued. Never enter passwords or tokens.",
            ephemeral=True,
        )

@bot.event
async def on_ready():
    bot.add_view(DemoView())
    try:
        synced = await bot.tree.sync()
        print(f"Logged in as {bot.user}; synced {len(synced)} slash commands")
    except Exception as exc:
        print(f"Command sync error: {exc}")

@bot.tree.command(name="verify-demo", description="Post a safe awareness demo link")
@app_commands.guild_only()
async def verify_demo(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Trigger Rewards — Demo",
        description="Open the reward-themed website prototype.\n\n"
                    "This is a demo only; it does not issue rewards or verify codes.",
        color=discord.Color.gold(),
        url=DEMO_URL,
    )
    embed.set_footer(text="Demo only • Never share passwords, login codes, or tokens")
    await interaction.response.send_message(embed=embed, view=DemoView())

@bot.tree.command(name="demo-stats", description="Show recent Trigger Rewards demo activity (admins only)")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def demo_stats(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Administrator permission is required.", ephemeral=True)
        return
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        await interaction.response.send_message(
            "Database connection is not configured on the bot host yet.", ephemeral=True
        )
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    url = f"{SUPABASE_URL}/rest/v1/trigger_demo_events?select=category,created_at&order=created_at.desc&limit=10"
    request = urllib.request.Request(url, headers={
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            events = json.loads(response.read().decode("utf-8"))
        counts = {"YouTube": 0, "Minecraft": 0, "OTT & streaming": 0}
        for event in events:
            if event.get("category") in counts:
                counts[event["category"]] += 1
        description = "\n".join(f"• **{name}:** {count}" for name, count in counts.items())
        description += f"\n\nLatest saved demo events: **{len(events)}** (up to 10 shown)."
        embed = discord.Embed(title="Trigger Rewards — Demo Activity", description=description,
                              color=discord.Color.gold(), url=DEMO_URL)
        embed.set_footer(text="Demo category counts only • No IP addresses or entered labels are stored")
        await interaction.followup.send(embed=embed, ephemeral=True)
    except (urllib.error.URLError, ValueError, TimeoutError) as exc:
        print(f"Supabase stats query failed: {exc}")
        await interaction.followup.send("Could not read demo activity from the database.", ephemeral=True)

@bot.tree.command(name="phishing-help", description="Learn to spot fake Discord verification")
async def phishing_help(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Spot fake verification",
        description="• Check the full website domain.\n"
                    "• Be cautious of unexpected links and urgent messages.\n"
                    "• A verification bot never needs your Discord password or session token.\n"
                    "• Open Discord directly in its official app or website.",
        color=discord.Color.green(),
    )
    embed.add_field(name="Already entered details?",
                    value="Change your password through the official Discord app/site, enable MFA, "
                          "review authorized apps, and contact Discord Support if needed.",
                    inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="report", description="Report a suspicious verification message")
@app_commands.describe(details="Describe what looked suspicious. Never include passwords or tokens.")
async def report(interaction: discord.Interaction, details: str):
    if REPORT_CHANNEL_ID:
        channel = bot.get_channel(REPORT_CHANNEL_ID)
        if channel is not None:
            embed = discord.Embed(title="Suspicious Verification Report",
                                  description=details[:1000], color=discord.Color.orange())
            embed.add_field(name="Reporter", value=f"{interaction.user} (ID: {interaction.user.id})", inline=False)
            await channel.send(embed=embed)
            await interaction.response.send_message("Report sent to moderators.", ephemeral=True)
            return
    await interaction.response.send_message(
        "Thanks for reporting. No report channel is configured; contact a moderator directly. "
        "Never include passwords or tokens.", ephemeral=True)

if not TOKEN:
    raise SystemExit("Missing DISCORD_BOT_TOKEN. Set it in your host's private environment variables.")
bot.run(TOKEN)
