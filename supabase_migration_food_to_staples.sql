-- ═══════════════════════════════════════════════════════════════════
-- Migration: normalize legacy sector/subsector IDs in Supabase
-- ═══════════════════════════════════════════════════════════════════
-- Two changes are merged into this script:
--
--   1. Subsector rename: 'food' → 'staples'
--      The frontend sub-tab under Food & Beverage was renamed from
--      "Food" (id='food') to "Staples" (id='staples'). Any rows
--      inserted via the admin UI while the new id was 'food' need
--      to be migrated.
--
--   2. Sector consolidation: 'food' / 'bev' → 'fnb'
--      The seed used to split the Food & Beverage tab into two DB
--      sectors ('food', 'bev'). The frontend now uses a single
--      sector id 'fnb'. The previous code papered over this with
--      a normalize() function; this migration removes the need
--      for it by aligning the data with the new ids.
--
-- Both blocks are idempotent: no-op if no rows match.
-- Run this in the Supabase SQL Editor.
-- ═══════════════════════════════════════════════════════════════════

-- 1. Subsector: food → staples
update public.dashboards  set subsector = 'staples' where subsector = 'food';
update public.access_logs set subsector = 'staples' where subsector = 'food';

-- 2. Sector: food / bev → fnb
update public.dashboards  set sector = 'fnb' where sector in ('food','bev');
update public.access_logs set sector = 'fnb' where sector in ('food','bev');

-- Verify (expect zero rows with sector in ('food','bev') or subsector='food'):
select 'dashboards'  as table_name, sector, subsector, count(*) as row_count
  from public.dashboards  group by sector, subsector
union all
select 'access_logs' as table_name, sector, subsector, count(*) as row_count
  from public.access_logs group by sector, subsector
order by table_name, sector, subsector;
