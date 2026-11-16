import os
import discord
from discord.ext import commands

def install_member_pull_commands(bot: commands.Bot):
    @bot.command(name="setup_pull_panel")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def setup_pull_panel(ctx: commands.Context):
        url = os.getenv("PULL_VERIFY_URL", "https://trg-member-verification-ika3l4afm.vercel.app").rstrip("/")
        if not url:
            return await ctx.reply("Set PULL_VERIFY_URL to the deployed verification website first.")
        embed = discord.Embed(title="Verify & Join Our Community",description="Click below to verify with Discord. Review the permissions Discord displays, then choose whether to join the server.",color=discord.Color.blurple())
        embed.add_field(name="How it works",value="1. Authorize this application.\n2. Review the requested access.\n3. Click Join server yourself.",inline=False)
        embed.set_footer(text="Joining is optional. You can revoke authorization in Discord settings.")
        view=discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label="Verify with Discord",style=discord.ButtonStyle.link,url=url+"/api/oauth/start"))
        await ctx.send(embed=embed,view=view)
    return setup_pull_panel