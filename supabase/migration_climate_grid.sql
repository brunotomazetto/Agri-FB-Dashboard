-- ═══════════════════════════════════════════════════════════════════
-- Migration: climate grid (Brazil-wide) + user-owned farm clusters
-- ═══════════════════════════════════════════════════════════════════
-- Context:
--   Até aqui o Climate Monitor só existia para 66 pontos fixos (SLC,
--   BrasilAgro, capitais) servidos por um history.json estático de 12 MB.
--   Para o cliente poder apontar QUALQUER coordenada do Brasil, a série
--   histórica precisa estar pré-carregada — e ela cabe, porque a grade do
--   NASA POWER é 0,5° lat × 0,625° lon: o Brasil inteiro são ~2.450
--   células, não infinitos pontos.
--
--   Duas coisas independentes entram aqui:
--
--     1. climate_cell — o DADO. Público (todo autenticado lê), escrito só
--        pelo seeder (service role) ou por admin. ~2.450 células × 17 anos.
--
--     2. farm_group / farm — a CONFIGURAÇÃO de cada usuário: quais pontos
--        formam um cluster e como pesá-los. Privado por dono, via RLS.
--        O cliente nunca cria dado climático; ele só aponta para células
--        que já existem. Por isso adicionar uma fazenda é instantâneo.
--
--   Os grupos padrão (SLC, BrasilAgro, capitais, usinas) continuam no
--   locations.json estático — eles são iguais para todo mundo e não têm
--   por que virar linha de banco. Esta migration é só para o que o
--   usuário cria.
--
-- Depende de: migration_rls_lockdown.sql (is_current_user_admin()).
-- Idempotente. Rodar no Supabase SQL Editor.
-- ═══════════════════════════════════════════════════════════════════

-- ── 1. Identificador de célula ─────────────────────────────────────
-- ÚNICA definição da fórmula no sistema. O front e o seeder replicam,
-- mas quem grava farm.cell_id é o trigger abaixo, então divergência de
-- arredondamento não chega ao banco.
--
-- Usa floor(x/d + 0.5) e NÃO round(): o round() do Python arredonda
-- meio-para-o-par e o do Postgres meio-para-cima, então uma fazenda
-- exatamente na fronteira da célula (ex.: lat −14,75) cairia em células
-- diferentes conforme quem calculou. floor(x/d + 0.5) é idêntico nas três
-- linguagens. Formato do texto: 4 casas, "-14.0000,-52.5000".
--
-- NOTA: build_regions.py ainda usa round() no cell_of() dele. Não há
-- conflito — aquele caminho consome o history.json e não toca nesta
-- tabela —, mas os dois não devem ser unificados sem um rebuild.
create or replace function public.climate_cell_id(p_lat double precision,
                                                  p_lon double precision)
returns text
language sql
immutable
strict
as $$
  select trim(to_char(floor(p_lat / 0.5   + 0.5) * 0.5,   'FM9990.0000')) || ',' ||
         trim(to_char(floor(p_lon / 0.625 + 0.5) * 0.625, 'FM9990.0000'));
$$;

-- ── 2. climate_cell — a série histórica ────────────────────────────
-- Uma linha por (célula, modelo, ano). Ler uma fazenda = ler ~17 linhas.
--
-- t/p são base64 de int16[366] escalado ×10 (temperatura em 0,1 °C e
-- chuva em 0,1 mm; -32768 = sem dado). Escolha do formato: 732 bytes por
-- ano viram ~976 chars em base64, contra ~2,5 KB do mesmo array em JSON.
-- Uma célula inteira sai em ~33 KB — contra os 12 MB que o dashboard
-- baixa hoje no load.
--
-- Índice do dia: 0 = 1/jan ... 59 = 29/fev ... 365 = 31/dez (sempre 366
-- posições, ano não bissexto deixa a 59 nula). Mesma convenção do
-- history.json, para o front tratar as duas fontes igual.
create table if not exists public.climate_cell (
  cell_id    text     not null,
  model      text     not null default 'nasa',
  year       smallint not null,
  t          text,
  p          text,
  updated_at timestamptz not null default now(),
  primary key (cell_id, model, year)
);

comment on table public.climate_cell is
  'Série diária NASA POWER por célula da grade (0,5° × 0,625°), Brasil inteiro. t/p = base64 de int16[366] ×10.';

alter table public.climate_cell enable row level security;

drop policy if exists "select_authenticated" on public.climate_cell;
drop policy if exists "modify_admin"         on public.climate_cell;

-- Dado público entre usuários autenticados: é medição de satélite, não há
-- nada a segmentar por cliente.
create policy "select_authenticated" on public.climate_cell
  for select using (auth.uid() is not null);
-- O seeder roda com service role (que ignora RLS); a política de escrita
-- existe para o caso de correção manual pelo admin.
create policy "modify_admin" on public.climate_cell
  for all using (public.is_current_user_admin())
  with check (public.is_current_user_admin());

-- ── 3. farm_group — o cluster do usuário ───────────────────────────
create table if not exists public.farm_group (
  id           uuid primary key default gen_random_uuid(),
  owner_email  text not null default lower(auth.jwt()->>'email'),
  name         text not null check (length(btrim(name)) between 1 and 60),
  scope        text not null default 'agri' check (scope in ('agri','fnb','sugar')),
  weight_label text check (weight_label is null or length(weight_label) <= 40),
  created_at   timestamptz not null default now()
);

-- Dois clusters com o mesmo nome para o mesmo dono viram confusão no
-- seletor; entre donos diferentes não há problema nenhum.
create unique index if not exists farm_group_owner_name_uq
  on public.farm_group (lower(owner_email), lower(btrim(name)));

create index if not exists farm_group_owner_idx
  on public.farm_group (lower(owner_email));

-- ── 4. farm — o ponto ──────────────────────────────────────────────
-- owner_email é desnormalizado de propósito: a política RLS vira uma
-- comparação direta em vez de um join com farm_group a cada linha. O
-- trigger abaixo garante que ele nunca divirja do dono do grupo.
create table if not exists public.farm (
  id          uuid primary key default gen_random_uuid(),
  group_id    uuid not null references public.farm_group(id) on delete cascade,
  owner_email text not null default lower(auth.jwt()->>'email'),
  name        text not null check (length(btrim(name)) between 1 and 60),
  lat         double precision not null check (lat between -90 and 90),
  lon         double precision not null check (lon between -180 and 180),
  weight      double precision check (weight is null or weight >= 0),
  cell_id     text not null default '',
  created_at  timestamptz not null default now()
);

create index if not exists farm_group_id_idx on public.farm (group_id);
create index if not exists farm_owner_idx    on public.farm (lower(owner_email));
create index if not exists farm_cell_idx     on public.farm (cell_id);

-- cell_id e owner_email são derivados, nunca confiáveis vindos do cliente.
-- Coluna gerada não serve aqui porque owner_email precisa de um lookup em
-- farm_group, então os dois são resolvidos no mesmo trigger.
create or replace function public.farm_fill_derived()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
declare
  v_owner text;
begin
  select owner_email into v_owner from public.farm_group where id = new.group_id;
  if v_owner is null then
    raise exception 'farm.group_id % does not exist', new.group_id;
  end if;
  new.owner_email := v_owner;
  new.cell_id     := public.climate_cell_id(new.lat, new.lon);
  return new;
end;
$$;

drop trigger if exists farm_fill_derived_trg on public.farm;
create trigger farm_fill_derived_trg
  before insert or update of group_id, lat, lon on public.farm
  for each row execute function public.farm_fill_derived();

-- ── 5. RLS: cada um vê e mexe só no que é seu ──────────────────────
alter table public.farm_group enable row level security;
alter table public.farm       enable row level security;

drop policy if exists "own_all" on public.farm_group;
drop policy if exists "own_all" on public.farm;

-- Sem política de admin de propósito: o portfólio do cliente é dele. Um
-- admin que precise auditar usa o service role, o que fica no log do
-- Supabase — melhor que uma porta aberta permanente na política.
create policy "own_all" on public.farm_group
  for all to authenticated
  using      (lower(owner_email) = lower(auth.jwt()->>'email'))
  with check (lower(owner_email) = lower(auth.jwt()->>'email'));

-- No INSERT o owner_email ainda é o default (o trigger só roda depois do
-- with check em alguns caminhos), então a política valida contra o grupo:
-- só dá para pendurar uma fazenda num grupo que já é seu.
create policy "own_all" on public.farm
  for all to authenticated
  using (exists (select 1 from public.farm_group g
                  where g.id = farm.group_id
                    and lower(g.owner_email) = lower(auth.jwt()->>'email')))
  with check (exists (select 1 from public.farm_group g
                       where g.id = farm.group_id
                         and lower(g.owner_email) = lower(auth.jwt()->>'email')));

-- ── 6. Leitura em bloco das séries de um cluster ───────────────────
-- Sem isto o front faria uma query por célula. Com 30 fazendas em 20
-- células distintas seriam 20 round-trips; aqui é um só. O DISTINCT na
-- lista de células é o que faz duas fazendas vizinhas custarem uma
-- leitura, não duas.
create or replace function public.climate_series_for_group(p_group_id uuid,
                                                           p_model text default 'nasa')
returns table (cell_id text, year smallint, t text, p text)
language sql
stable
security invoker
as $$
  select c.cell_id, c.year, c.t, c.p
    from public.climate_cell c
   where c.model = p_model
     and c.cell_id in (select distinct f.cell_id
                         from public.farm f
                        where f.group_id = p_group_id)
   order by c.cell_id, c.year;
$$;

-- security invoker + RLS em farm garante que pedir o group_id de outro
-- usuário devolve zero linhas.

-- ── 7. Verificação ─────────────────────────────────────────────────
-- Fórmula da célula (esperado: -14.0000,-52.5000 e -14.5000,-53.1250):
select public.climate_cell_id(-14.20, -52.50) as a,
       public.climate_cell_id(-14.75, -52.90) as b;

-- Cobertura carregada (esperado ~2.450 células × 17 anos após o seed):
select model, count(distinct cell_id) as cells, count(*) as rows,
       min(year) as y0, max(year) as y1
  from public.climate_cell group by model;

select schemaname, tablename, policyname, cmd
  from pg_policies
 where schemaname = 'public'
   and tablename in ('climate_cell','farm_group','farm')
 order by tablename, policyname;
