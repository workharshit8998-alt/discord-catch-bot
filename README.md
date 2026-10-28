# Trigger Rewards Discord Ticket Bot

Discord ticket and reward-claim bot for Trigger Rewards. Reward categories and reward names are stored in the dedicated Supabase project and shared with the website.

## Botkeep environment variables

Set these as private environment variables. Never commit secrets or share them in chat.

- `DISCORD_BOT_TOKEN` — Discord bot token.
- `SUPABASE_URL` — the Trigger Rewards Supabase project URL.
- `SUPABASE_SERVICE_ROLE_KEY` — the private service-role key for the same Supabase project. **Botkeep only; never put this key in Vercel or browser code.**
- `TRG_GUILD_ID` — your Discord server ID (`1499228791220863110`) so new slash commands sync immediately to your server.
- `DEMO_URL` — optional; defaults to https://trigger-rewards.vercel.app.
- `TICKET_DB_PATH` — optional SQLite path. Confirm Botkeep preserves this file across restarts if you use local ticket/claim history.

Deploy this repository with startup file `main.py`, install `requirements.txt`, and restart the service after syncing the latest GitHub commit.

## Website environment variables

In the Vercel project, configure:

- `SUPABASE_URL` — the same Trigger Rewards Supabase project URL.
- `SUPABASE_ANON_KEY` — a public/anon/publishable key for read-only access protected by Supabase RLS.
- `TRG_GUILD_ID` — your Discord server ID.

Never add `SUPABASE_SERVICE_ROLE_KEY` to the website. Redeploy after changing environment variables.

## Configure categories and rewards in Discord

Run as a server administrator:

1. `/category-add key:youtube label:"YouTube" emoji:▶️`
2. `/reward-add category_key:youtube name:"Exact reward name" invites:10`
3. Repeat `/reward-add` for every reward you want in that category.
4. Use `/category-add` for each additional category.
5. Use `/reward-remove` to hide a reward and `/category-remove` to hide a category and its rewards.
6. Run `/ticket-setup` to post the ticket panel.

The category key is a short stable slug using letters, numbers, hyphens or underscores. It is not the displayed category label. The website reads active categories and rewards from the shared Supabase database.

## Ticket and invite verification

Members open a private ticket, choose a configured category and reward, then show staff their Falcon invite output. Staff must manually verify the tracker result before running `/approve-claim member:@User` inside the ticket. Falcon output is not automatically parsed; invite counts are never guessed or automatically subtracted.

Use `/staff-role` to configure ticket staff, `/set-vouch-channel` for vouches, and `/redeem-code` to mark a code redeemed.

## Important limitations

- The website displays configured categories and rewards, but its code/timer pages are not yet a real code-validation or reward-fulfillment system.
- Bot claims and issued codes are stored in local SQLite, not Supabase. Verify that Botkeep storage persists before relying on claim history.
- Invite verification is staff-reviewed; Falcon data is not fetched or validated by the bot automatically.
- Test the complete flow with dummy rewards before offering anything real.

Never ask members for Discord passwords, session tokens, or 2FA codes.
