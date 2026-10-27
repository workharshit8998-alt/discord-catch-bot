import os
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
DEMO_URL = os.getenv("DEMO_URL", "https://example.com").strip()
REPORT_CHANNEL_ID = int(os.getenv("REPORT_CHANNEL_ID", "0") or "0")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

class DemoView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Verify (Training Demo)", style=discord.ButtonStyle.primary,
                       custom_id="awareness:verify_demo", emoji="🛡️")
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            f"Open the phishing-awareness training demo: {DEMO_URL}\n\n"
            "Training only. Never enter your real Discord password, login code, or token on an unfamiliar website.",
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

@bot.tree.command(name="verify-demo", description="Post a safe phishing-awareness verification demo")
@app_commands.guild_only()
async def verify_demo(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Server Verification — Awareness Demo",
        description="This security-awareness exercise shows how fake verification prompts can look.\n\n"
                    "Use the button to open the training page. This does not sign you in to Discord.",
        color=discord.Color.blurple(),
    )
    embed.set_footer(text="Training demo • Never share passwords, login codes, or session tokens")
    await interaction.response.send_message(embed=embed, view=DemoView())

@bot.tree.command(name="phishing-help", description="Learn to spot fake Discord verification")
async def phishing_help(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Spot fake verification",
        description="• Check the full website domain.\n"
                    "• Be cautious of unexpected links and urgent messages.\n"
                    "• A server verification bot never needs your Discord password or session token.\n"
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
