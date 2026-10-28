# Trigger Rewards Discord Ticket Bot

A Discord ticket and reward-claim bot for Trigger Rewards. The bot uses private ticket channels, role-based staff access, configurable reward menus, staff-approved invite checks, unique seven-digit codes, and one-time redemption tracking.

## Deploy on Botkeep

1. Use this repository and set the startup file to `main.py`.
2. Install dependencies from `requirements.txt`.
3. Set these as private environment variables in Botkeep. Do not commit tokens or secret keys.
   - `DISCORD_BOT_TOKEN` — Discord bot token.
   - `DEMO_URL` — optional; defaults to https://trigger-rewards.vercel.app.
   - `TICKET_DB_PATH` — optional; defaults to `trigger_rewards.sqlite3`.
4. Invite the bot with the `bot` and `applications.commands` scopes. Give it View Channels, Send Messages, Embed Links, Read Message History, Manage Channels and Manage Roles only if needed. Enable Server Members Intent in the Developer Portal if required by your server.
5. Restart the bot and wait for slash commands to sync.

## First-time setup

Run these commands as an administrator:

1. `/staff-role role:@YourStaffRole` — staff can see tickets and approve claims.
2. `/set-vouch-channel channel:#vouches` — configure the vouch channel.
3. `/invite-requirement count:3` — choose the default invite requirement.
4. Add reward choices with `/reward-add`, selecting a category, reward name and invite count.
5. `/ticket-setup channel:#open-ticket category:TicketCategory` — post the public ticket panel.

Users click **Open Reward Ticket**, choose YouTube, Minecraft or OTT & Streaming, and then select a configured reward. The ticket is visible to its owner, staff role and the bot.

## Invite verification and codes

The bot currently asks the user to run the server's invite-tracker command and requires staff to verify the tracker result before running `/approve-claim member:@User` inside that user's ticket. Automatic invite-tracker parsing is not enabled because the tracker bot's identity and exact response format must be configured and tested first. The bot does not invent invite counts.

Each user can receive only one code per reward. Codes are seven digits and are sent by DM inside Discord spoiler syntax. Staff can run `/redeem-code code:1234567` after the reward is redeemed; the code is then permanently marked redeemed and cannot be redeemed again.

## Important limitations

- The bot's claim records currently use a local SQLite file. Confirm that your host preserves its files across restarts/redeploys; otherwise move the claims database to durable storage before using real rewards.
- The website claim page is linked, but it is **not yet connected to the bot's claim database**. A code shown by the bot is not automatically validated by the website yet.
- Invite results are staff-verified, not automatically parsed from the tracker bot.
- Test the flow in a private test channel before using real rewards.

Never request Discord passwords, session tokens or 2FA codes. Keep bot tokens private.
