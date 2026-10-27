# Discord Phishing Awareness Bot

A working Discord bot and browser-based training page for demonstrating phishing warning signs. It does not imitate Discord authentication, collect real credentials, or extract account/session tokens.

## Commands
- `/verify-demo` — posts an awareness embed and training button.
- `/phishing-help` — shows prevention and recovery tips.
- `/report` — sends a suspicious-message report to a configured moderator channel.

## Setup
1. Create a bot in the Discord Developer Portal: https://discord.com/developers/applications
2. Invite it with the `bot` and `applications.commands` scopes. Give it Send Messages and Embed Links permissions.
3. Install dependencies: `pip install -r requirements.txt`
4. Set private host environment variables:
   - `DISCORD_BOT_TOKEN` — bot token (never commit this to GitHub)
   - `DEMO_URL` — public URL for `demo/index.html`
   - `REPORT_CHANNEL_ID` — optional moderator channel ID; use `0` to disable
5. Start with `python bot.py`.
6. Host `demo/index.html` on a static website host and set `DEMO_URL` to that URL.

Only run training exercises in servers where you have permission. If a token is exposed, reset it in the Discord Developer Portal immediately.
