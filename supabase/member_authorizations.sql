create table if not exists public.member_authorizations (discord_user_id text primary key, token_bundle_enc text not null, expires_at timestamptz not null, created_at timestamptz not null default now(), updated_at timestamptz not null default now());
alter table public.member_authorizations enable row level security;
revoke all on table public.member_authorizations from anon, authenticated;
grant all on table public.member_authorizations to service_role;