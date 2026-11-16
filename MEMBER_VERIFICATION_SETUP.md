# Discord OAuth member verification setup

## What was added
- `website/`: Next.js Discord OAuth2 verification site.
- `member_pull_panel.py`: adds `!setup_pull_panel` to the existing discord.py bot.
- `supabase/member_authorizations.sql`: schema for encrypted OAuth token storage.

This flow is consent-based. Members authorize the app and personally click **Join server**. It does not bulk-add or silently join people.

## Required Vercel environment variables
Already configured automatically:
- `SUPABASE_URL`
- `NEXT_PUBLIC_SERVER_NAME`
- `TOKEN_ENCRYPTION_KEY` (generated secret)
- `SESSION_SECRET` (generated secret)

Still required from the owner:
- `SUPABASE_SERVICE_ROLE_KEY`: use the service_role secret already used by the existing bot host, or obtain it from Supabase Project Settings > API Keys. Never expose it in browser code.
- `DISCORD_CLIENT_ID`: the Client ID of the same Discord application that owns the bot.
- `DISCORD_CLIENT_SECRET`: that application's OAuth2 Client Secret.
- `DISCORD_BOT_TOKEN`: bot token for that same application.
- `DEST_GUILD_ID`: exact destination server ID.

Set these in Vercel Project Settings > Environment Variables for Production, Preview, and Development, then redeploy.

## Discord Developer Portal
Add this exact OAuth2 redirect URI:
`https://trg-member-verification-harshitkittu8998-6086s-projects.vercel.app/api/oauth/callback`

The website requests `identify guilds.join`. The bot must be in the destination server and have the permission Discord requires for adding members with OAuth2.

## Existing bot
The existing `main.py` imports `member_pull_panel.py` and registers `!setup_pull_panel`. Pull latest GitHub files to the bot host and restart it. Then run `!setup_pull_panel` in the channel where the panel should appear.

## Privacy
Tokens are encrypted at rest. The website includes **Disconnect & delete saved authorization**, which deletes the user's saved token and clears the browser session. Users may also revoke the app in Discord's Authorized Apps settings.
