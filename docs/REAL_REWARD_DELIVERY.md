# Trigger Rewards real delivery — integration notes

This branch adds the database foundation for the shared website + Discord delivery flow.

## Intended flow
1. Website creates a pending claim containing the authenticated user's linked Discord ID.
2. Admin approves a specific pending claim.
3. A server-side transaction reserves one available inventory item for that claim and creates exactly one delivery job.
4. Bot sends a DM to that claim's Discord user only.
5. On successful Discord API response, update delivery + claim to sent/delivered. If DM is closed or Discord rejects it, record failed and leave it retryable.
6. Website reads the claimant's own claim status/history; it never receives other users' inventory secrets.

## Required environment (bot/server only)
- SUPABASE_URL
- SUPABASE_SERVICE_ROLE_KEY
- DISCORD_BOT_TOKEN

Never put the service-role key or bot token in browser code, public repo files, or client-side environment variables.

## Important compatibility note
The current bot has a local SQLite `claims` table and reads reward configuration from Supabase tables `trigger_reward_categories` and `trigger_rewards_config`. Do not silently migrate or delete those tables. The new `trg_reward_*` tables are a separate delivery layer; existing ticket and approval commands must be adapted to create/read these records before enabling live delivery.

## Before production
- Add an atomic Postgres RPC to reserve inventory and enqueue a delivery job in one transaction.
- Verify Discord account linking through OAuth2; never trust a user-submitted Discord ID.
- Enforce admin/staff authorization server-side.
- Add per-user eligibility, cooldown, region, and duplicate-claim rules.
- Use a worker/queue with idempotency; do not blindly retry a DM after an ambiguous network timeout.
- Test with dummy codes and a private test guild first.
