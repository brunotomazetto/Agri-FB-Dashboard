-- ═══════════════════════════════════════════════════════════════════
-- Itaú BBA — Agribusiness, Food & Beverage Portal
-- FULL SETUP SCRIPT — run this in Supabase SQL Editor
-- ═══════════════════════════════════════════════════════════════════

-- ── 1. TABLES ──────────────────────────────────────────────────────

create table if not exists public.portal_users (
  id uuid primary key default gen_random_uuid(),
  email text unique not null,
  display_name text not null,
  is_admin boolean default false,
  active boolean default true,
  created_at timestamptz default now()
);

create table if not exists public.dashboards (
  id serial primary key,
  sector text not null,
  subsector text not null,
  title text not null,
  description text,
  source text,
  note text default '',
  footer text default '',
  url text default '#',
  tags text[] default '{}',
  display_order int default 1,
  visible_to_all boolean default true,
  coming_soon boolean default false
);

create table if not exists public.team_settings (
  id int primary key default 1,
  banner_title text default 'Agribusiness, Food & Beverage Coverage',
  banner_sub text default 'Itaú BBA Equity Research',
  promo_image_url text default '',
  promo_link_url text default '',
  analysts jsonb default '[]'
);

-- ── 2. ROW LEVEL SECURITY ──────────────────────────────────────────

alter table public.portal_users  enable row level security;
alter table public.dashboards     enable row level security;
alter table public.team_settings  enable row level security;

-- Drop existing policies if re-running
drop policy if exists "read_all"       on public.portal_users;
drop policy if exists "read_all"       on public.dashboards;
drop policy if exists "read_all"       on public.team_settings;
drop policy if exists "write_anon"     on public.portal_users;
drop policy if exists "write_anon"     on public.dashboards;
drop policy if exists "write_anon"     on public.team_settings;

-- Allow anon key to read everything (portal loads data before login check)
create policy "read_all"   on public.portal_users  for select using (true);
create policy "read_all"   on public.dashboards     for select using (true);
create policy "read_all"   on public.team_settings  for select using (true);

-- Allow anon key to write (admin actions go through the anon key in this setup)
create policy "write_anon" on public.portal_users  for all using (true) with check (true);
create policy "write_anon" on public.dashboards     for all using (true) with check (true);
create policy "write_anon" on public.team_settings  for all using (true) with check (true);

-- ── 3. TEAM SETTINGS (initial row) ────────────────────────────────

insert into public.team_settings (id, banner_title, banner_sub, promo_image_url, promo_link_url, analysts)
values (
  1,
  'Agribusiness, Food & Beverage Coverage',
  'Itaú BBA Equity Research',
  '', '',
  '[
    {"name":"Gustavo Troyano","role":"Head of Sector — Agribusiness, Food & Beverage","email":"gustavo.troyano@itaubba.com"},
    {"name":"Bruno Tomazetto","role":"Equity Research Analyst","email":"bruno.tomazetto@itaubba.com"},
    {"name":"Ryu Matsuyama","role":"Equity Research Analyst","email":"ryu.matsuyama@itaubba.com"}
  ]'::jsonb
)
on conflict (id) do update set
  banner_title = excluded.banner_title,
  banner_sub   = excluded.banner_sub,
  analysts     = excluded.analysts;

-- ── 4. PORTAL USERS (initial) ─────────────────────────────────────
-- Note: passwords are managed by Supabase Auth.
-- Create matching users in Authentication → Users first.

insert into public.portal_users (email, display_name, is_admin, active) values
  ('admin@itaubba.com',           'Admin',          true,  true),
  ('bruno.tomazetto@itaubba.com', 'Bruno Tomazetto', false, true),
  ('ryu.matsuyama@itaubba.com',   'Ryu Matsuyama',   false, true)
on conflict (email) do nothing;

-- ── 5. DASHBOARDS (14 placeholder entries) ────────────────────────

truncate public.dashboards restart identity;

insert into public.dashboards
  (sector, subsector, title, description, source, note, footer, url, tags, display_order, visible_to_all, coming_soon)
values

-- AGRIBUSINESS / GRAINS
('agri','grains',
  'Grains Price Monitor',
  'Lorem ipsum dolor sit amet, consectetur adipiscing elit. Pellentesque euismod erat a arcu ultrices, vel tincidunt libero viverra. Track prices across corn, soy and wheat.',
  'CBOT / ESALQ', '', 'AUTO-UPDATES ON LOAD',
  '#dash-grains-prices', '{"CBOT","ESALQ","Weekly"}', 1, true, false),

('agri','grains',
  'Grains Trade Flow Dashboard',
  'Sed ut perspiciatis unde omnis iste natus error sit voluptatem accusantium doloremque laudantium. Compare origination volumes and export flows by destination.',
  'USDA / ANEC',
  '* This dashboard may take longer to load due to data source latency.',
  'INTERACTIVE FILTERS',
  '#dash-grains-trade', '{"USDA","ANEC","Monthly"}', 2, true, false),

-- AGRIBUSINESS / SUGAR & ETHANOL
('agri','sugar',
  'Sugar & Ethanol Price Tracker',
  'Lorem ipsum dolor sit amet, consectetur adipiscing elit. Ut enim ad minima veniam, quis nostrum exercitationem. Weekly prices for sugar and anhydrous ethanol.',
  'CEPEA / UNICA', '', 'AUTO-UPDATES ON LOAD',
  '#dash-sugar-prices', '{"CEPEA","UNICA","Weekly"}', 1, true, false),

('agri','sugar',
  'Crush Season Dashboard',
  'Nemo enim ipsam voluptatem quia voluptas sit aspernatur aut odit aut fugit. Track sugarcane crushing volumes and ATR yields across Brazil.',
  'UNICA / MAPA',
  '* This dashboard may take longer to load due to data source latency.',
  'QUARTERLY DATA',
  '#dash-sugar-crush', '{"UNICA","MAPA","Quarterly"}', 2, true, false),

-- F&B / BEEF
('fnb','beef',
  'Beef Price Monitor',
  'Lorem ipsum dolor sit amet, consectetur adipiscing elit. Proin ac dolor a purus ornare tincidunt. Track live cattle, carcass and wholesale beef prices across Brazil.',
  'CEPEA / B3', '', 'AUTO-UPDATES ON LOAD',
  '#dash-beef-prices', '{"CEPEA","B3","Weekly"}', 1, true, false),

('fnb','beef',
  'Beef Export Dashboard',
  'Ut labore et dolore magnam aliquam quaerat voluptatem. Compare export volumes and revenues by destination country and cut type.',
  'MDIC / ABIEC',
  '* This dashboard may take longer to load due to data source latency.',
  'INTERACTIVE FILTERS',
  '#dash-beef-exports', '{"MDIC","ABIEC","Monthly"}', 2, true, false),

-- F&B / CHICKEN
('fnb','chicken',
  'Poultry Price Monitor',
  'Lorem ipsum dolor sit amet, consectetur adipiscing elit. Duis aute irure dolor in reprehenderit in voluptate. Track live bird and whole chicken prices.',
  'CEPEA / ABPA', '', 'AUTO-UPDATES ON LOAD',
  '#dash-chicken-prices', '{"CEPEA","ABPA","Weekly"}', 1, true, false),

('fnb','chicken',
  'Poultry Export Dashboard',
  'Excepteur sint occaecat cupidatat non proident, sunt in culpa qui officia deserunt mollit anim id est laborum. Compare export performance vs. prior years.',
  'MDIC / ABPA',
  '* This dashboard may take longer to load due to data source latency.',
  'INTERACTIVE FILTERS',
  '#dash-chicken-exports', '{"MDIC","ABPA","Monthly"}', 2, true, false),

-- F&B / PORK
('fnb','pork',
  'Pork Price Monitor',
  'Lorem ipsum dolor sit amet, consectetur adipiscing elit. Suspendisse potenti. Track live hog, carcass and pork cut prices across major Brazilian markets.',
  'CEPEA / ABPA', '', 'AUTO-UPDATES ON LOAD',
  '#dash-pork-prices', '{"CEPEA","ABPA","Weekly"}', 1, true, false),

('fnb','pork',
  'Pork Trade Flow Dashboard',
  'Sed do eiusmod tempor incididunt ut labore et dolore magna aliqua. Compare origination volumes, slaughter data and export flows by destination.',
  'MDIC / ABPA',
  '* This dashboard may take longer to load due to data source latency.',
  'INTERACTIVE FILTERS',
  '#dash-pork-trade', '{"MDIC","ABPA","Monthly"}', 2, true, false),

-- F&B / STAPLES
('fnb','staples',
  'Staples Price Monitor',
  'Lorem ipsum dolor sit amet, consectetur adipiscing elit. Ut enim ad minim veniam. Track rice, beans, edible oil and flour prices across Brazilian retail.',
  'IBGE / FGV', '', 'AUTO-UPDATES ON LOAD',
  '#dash-staples-prices', '{"IBGE","FGV","Weekly"}', 1, true, false),

('fnb','staples',
  'Staples Volume Dashboard',
  'Duis aute irure dolor in reprehenderit in voluptate velit esse cillum dolore eu fugiat nulla pariatur. Track retail volumes and shelf price inflation by category.',
  'IBGE / Nielsen',
  '* This dashboard may take longer to load due to data source latency.',
  'INTERACTIVE FILTERS',
  '#dash-staples-volume', '{"IBGE","Nielsen","Monthly"}', 2, true, false),

-- F&B / BEVERAGES
('fnb','beverages',
  'Beverages Price Monitor',
  'Lorem ipsum dolor sit amet, consectetur adipiscing elit. Vivamus lacinia odio vitae vestibulum. Track beer, soft drink and spirits pricing across Brazilian retail.',
  'IBGE / Euromonitor', '', 'AUTO-UPDATES ON LOAD',
  '#dash-bev-prices', '{"IBGE","Euromonitor","Weekly"}', 1, true, false),

('fnb','beverages',
  'Beverages Volume Dashboard',
  'Curabitur pretium tincidunt lacus. Nulla gravida orci a odio. Compare volume and revenue performance across beer, CSD and RTD categories.',
  'IBGE / Nielsen',
  '* This dashboard may take longer to load due to data source latency.',
  'INTERACTIVE FILTERS',
  '#dash-bev-volume', '{"IBGE","Nielsen","Monthly"}', 2, true, false);

-- ── DONE ──────────────────────────────────────────────────────────
-- Verify:
select count(*) as total_dashboards from public.dashboards;
select count(*) as total_users       from public.portal_users;
select banner_title                  from public.team_settings where id=1;
