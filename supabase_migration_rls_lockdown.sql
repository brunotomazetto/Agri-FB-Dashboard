-- ═══════════════════════════════════════════════════════════════════
-- Migration: lock down RLS policies
-- ═══════════════════════════════════════════════════════════════════
-- Context:
--   The original setup used "using (true)" RLS policies, allowing the
--   anon key to read AND write every table. With the deployed dashboard
--   exposing the anon key (as designed), this means anyone with the URL
--   could list emails, create admin accounts, edit dashboards, etc.
--
--   This migration:
--     1. Adds a helper function is_current_user_admin() that checks
--        whether the current JWT belongs to an active admin.
--     2. Replaces the open policies with: authenticated users can read,
--        only admins can write.
--     3. Allows authenticated users to update their own row in
--        portal_users (display_name / email), with a trigger blocking
--        privilege escalation (is_admin / active).
--     4. Tightens access_logs: any authenticated user can insert events,
--        only admins can read them.
--
-- Safe to run on existing data: only policies / functions change, no
-- table contents are touched.
-- Idempotent: drops legacy policies and recreates the new ones.
-- Run this in the Supabase SQL Editor.
-- ═══════════════════════════════════════════════════════════════════

-- ── 1. Helper function ─────────────────────────────────────────────
create or replace function public.is_current_user_admin()
returns boolean
language sql
stable
security definer
set search_path = public
as $$
  select coalesce(
    (select is_admin from public.portal_users
       where lower(email) = lower(auth.jwt()->>'email')
         and active = true
       limit 1),
    false
  );
$$;

-- ── 2. Make sure RLS is enabled on all 4 tables ────────────────────
alter table public.portal_users  enable row level security;
alter table public.dashboards    enable row level security;
alter table public.team_settings enable row level security;
alter table public.access_logs   enable row level security;

-- ── 3. Drop legacy / current policies ──────────────────────────────
drop policy if exists "read_all"             on public.portal_users;
drop policy if exists "write_anon"           on public.portal_users;
drop policy if exists "select_authenticated" on public.portal_users;
drop policy if exists "modify_admin"         on public.portal_users;
drop policy if exists "update_self"          on public.portal_users;

drop policy if exists "read_all"             on public.dashboards;
drop policy if exists "write_anon"           on public.dashboards;
drop policy if exists "select_authenticated" on public.dashboards;
drop policy if exists "modify_admin"         on public.dashboards;

drop policy if exists "read_all"             on public.team_settings;
drop policy if exists "write_anon"           on public.team_settings;
drop policy if exists "select_authenticated" on public.team_settings;
drop policy if exists "modify_admin"         on public.team_settings;

drop policy if exists "read_all"             on public.access_logs;
drop policy if exists "write_anon"           on public.access_logs;
drop policy if exists "insert_authenticated" on public.access_logs;
drop policy if exists "select_admin"         on public.access_logs;

-- ── 4. New strict policies ─────────────────────────────────────────

-- portal_users
create policy "select_authenticated" on public.portal_users
  for select using (auth.uid() is not null);
create policy "modify_admin" on public.portal_users
  for all using (public.is_current_user_admin())
  with check (public.is_current_user_admin());
create policy "update_self" on public.portal_users
  for update
  using  (lower(email) = lower(auth.jwt()->>'email'))
  with check (lower(email) = lower(auth.jwt()->>'email'));

-- dashboards
create policy "select_authenticated" on public.dashboards
  for select using (auth.uid() is not null);
create policy "modify_admin" on public.dashboards
  for all using (public.is_current_user_admin())
  with check (public.is_current_user_admin());

-- team_settings
create policy "select_authenticated" on public.team_settings
  for select using (auth.uid() is not null);
create policy "modify_admin" on public.team_settings
  for all using (public.is_current_user_admin())
  with check (public.is_current_user_admin());

-- access_logs: any authenticated user inserts events; only admins read
create policy "insert_authenticated" on public.access_logs
  for insert with check (auth.uid() is not null);
create policy "select_admin" on public.access_logs
  for select using (public.is_current_user_admin());
-- (no update / delete policies → those operations are blocked)

-- ── 5. Trigger: prevent self-promotion ─────────────────────────────
create or replace function public.portal_users_protect_admin_fields()
returns trigger
language plpgsql
as $$
begin
  if (new.is_admin is distinct from old.is_admin
      or new.active is distinct from old.active)
     and not public.is_current_user_admin() then
    raise exception 'permission denied: only admins can change is_admin or active';
  end if;
  return new;
end;
$$;
drop trigger if exists portal_users_protect_admin on public.portal_users;
create trigger portal_users_protect_admin
  before update on public.portal_users
  for each row execute function public.portal_users_protect_admin_fields();

-- ── 6. Verify ──────────────────────────────────────────────────────
-- List all policies in the 4 tables (you should see only the new ones):
select schemaname, tablename, policyname, cmd
  from pg_policies
  where schemaname = 'public'
    and tablename in ('portal_users','dashboards','team_settings','access_logs')
  order by tablename, policyname;
