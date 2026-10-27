# Discord Verification Awareness Bot

A Discord bot and Discord-inspired browser training page for learning to spot fake verification prompts. The page is a fictional training interface, not Discord sign-in, and never requests or collects real credentials, session cookies, or tokens.

## Commands
- `/verify-demo` — posts the training button.
- `/phishing-help` — shows prevention and recovery tips.
- `/report` — lets users report suspicious verification messages to a configured moderator channel.

## Setup
1. Create a bot in the Discord Developer Portal: https://discord.com/developers/applications
2. Invite it with the `bot` and `applications.commands` scopes. Give it Send Messages and Embed Links permissions.
3. Install dependencies: `pip install -r requirements.txt`
4. Set private host environment variables:
   - `DISCORD_BOT_TOKEN` — bot token; keep it in your hosting provider's private environment settings, never in GitHub or chat.
   - `DEMO_URL` — public URL for `demo/index.html`.
   - `REPORT_CHANNEL_ID` — optional moderator channel ID; use `0` to disable.
5. Start with `python main.py`.
6. Host `demo/index.html` on a static website host and set `DEMO_URL` to that public URL.

Only run training exercises in servers where you have permission. If the bot token is exposed, reset it in the Discord Developer Portal immediately.
