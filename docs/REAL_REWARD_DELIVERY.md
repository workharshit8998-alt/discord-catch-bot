# Trigger Rewards real delivery — Discord admin workflow

This branch adds Discord-side stock management and private delivery controls while preserving the existing ticket flow.

## How it works

1. A member opens the existing reward ticket and chooses a configured category and reward.
2. The existing invite-tracker verification checks the ticket owner's invite total.
3. After a successful check, the bot creates one shared pending delivery claim in Supabase. The source ticket ID prevents duplicate claims from replayed invite-log messages.
4. An administrator opens `/admin-panel` and uses **Add Real Reward Stock** to add an actual code for an already-configured reward. The code is stored only in the server-only inventory table.
5. The administrator opens **Pending DM Deliveries**, selects one or more verified pending claimants, and presses **Deliver Selected**.
6. The bot atomically reserves one matching stock item per claim, privately DMs only the selected claimant, and records the Discord message ID and final status.
7. If Discord rejects the DM, the bot records a failure and releases the stock for retry. If Discord accepts the DM but Supabase finalization fails, the stock stays locked in a sending state to avoid accidental duplicate delivery; an administrator must inspect it before retrying.

## Discord commands and controls

- `/admin-panel` — existing admin panel, with **Add Real Reward Stock** and **Pending DM Deliveries** controls.
- `/reward-auto-delivery enabled:true|false` — optional automatic delivery after a verified ticket claim. It is disabled by default.
- Existing ticket commands, category/reward setup, invite verification, vouch evidence, and close-ticket flow remain in place.

## Required configuration

No new environment variables are required. The bot needs the existing private hosting variables:

- `DISCORD_BOT_TOKEN`
- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- `TRG_GUILD_ID`
- `DEMO_URL` (optional; defaults to the current website URL)

Never put the service-role key or bot token in browser code, public repository files, or client-side environment variables.

## Database setup

Apply `supabase/migrations/202611060001_trigger_reward_delivery.sql` to the existing Supabase project before starting this bot version. It creates the inventory, pending claims, delivery history, and service-role-only RPCs used to reserve stock and finalize delivery.

The bot's pending queue currently receives claims created by the existing Discord ticket + invite-verification flow. The public website's current code/timer flow is not an authenticated Discord-linked claim system and does not yet create these pending claims. Do not claim website-originated claims are integrated until OAuth account linking and a secure server-side claim endpoint are added.

## Security and operational notes

- Only server administrators can use the delivery controls.
- The queue contains only pending or known-failed claims; it never DMs all server members.
- Stock secrets are never shown in the public website or Discord channels.
- The bot verifies that the selected reward exists in the configured category before accepting stock.
- Inventory reservation is transactional and uses row locking to prevent two admins from allocating the same item.
- A successful DM is recorded with its Discord message ID.
- A failed DM is retryable. An ambiguous post-send database failure remains locked rather than risking a duplicate code.
- Add only legitimate reward codes that you own or are authorized to distribute. Never store account passwords, session tokens, or 2FA codes as reward inventory.

## Rollout and testing

1. Review and merge the bot and website schema pull requests only after checking the diff.
2. Apply the Supabase migration.
3. Confirm the bot hosting environment has the service-role key and bot token set privately.
4. Restart/redeploy the bot from this feature branch.
5. Add a dummy code to a test reward, create a test claim using a test account, and deliver it to a test Discord account with DMs enabled.
6. Test closed DMs, no matching stock, multi-select delivery, automatic delivery disabled/enabled, and retry after a known DM failure.
7. Do not enable automatic delivery in production until the manual test passes.
