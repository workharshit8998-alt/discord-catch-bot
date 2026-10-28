# Discord Verification Awareness Bot

A Discord bot and Discord-inspired browser training page for learning to spot fake verification prompts. The page is a fictional training interface, not Discord sign-in, and never requests or collects real credentials, session cookies, or tokens.

## Commands
- `/verify-demo` — posts the Trigger Rewards demo link.
- `/demo-stats` — administrator-only view of up to the 10 latest demo events saved in Supabase.
- `/phishing-help` — shows prevention and recovery tips.
- `/report` — lets users report suspicious verification messages to a configured moderator channel.

## Setup
1. Create a bot in the Discord Developer Portal: https://discord.com/developers/applications
2. Invite it with the `bot` and `applications.commands` scopes. Give it Send Messages and Embed Links permissions.
3. Install dependencies: `pip install -r requirements.txt`
4. In Botkeep, set private environment variables:
   - `DISCORD_BOT_TOKEN` — bot token; keep it private.
   - `DEMO_URL` — optional; defaults to `https://trigger-rewards.vercel.app`.
   - `SUPABASE_URL` — optional; defaults to `https://rhhvpyeowumfludgjcnw.supabase.co`.
   - `SUPABASE_SERVICE_ROLE_KEY` — private key from the dedicated Trigger Rewards Supabase project; never commit it to GitHub.
   - `REPORT_CHANNEL_ID` — optional moderator channel ID; use `0` to disable.
5. Set the startup file to `main.py`, save variables, and restart the bot.

## Dedicated Supabase project
Trigger Rewards uses a separate project, not the Alt Generator database:
- Dashboard: https://supabase.com/dashboard/project/rhhvpyeowumfludgjcnw
- API URL: `https://rhhvpyeowumfludgjcnw.supabase.co`
- Table: `public.trigger_demo_events`

The service-role key bypasses RLS, so keep it private and configure it only in Botkeep's secret environment settings.

Only run training exercises in servers where you have permission. If the bot token is exposed, reset it in the Discord Developer Portal immediately.
