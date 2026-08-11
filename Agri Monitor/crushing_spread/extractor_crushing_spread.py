#!/usr/bin/env python3
"""
extractor_crushing_spread.py — Agri Monitor · Soy Crushing Spread
==================================================================
Calcula o crushing spread de soja para biodiesel no RS e no MT.

Fórmula (por tonelada de soja processada):
    Spread = (P_farelo × 0.77) + (P_biodiesel × 0.19) - (P_soja × 1000/60)

    P_soja      → R$/sc 60kg  — API CONAB precos ao produtor (RS e MT)
    P_farelo    → USD/kg FOB convertido para R$/ton via PTAX
                  CSV bulk MDIC/SECEX, NCM 23040090, filtrado por porto (URF)
    P_biodiesel → R$/m³       — ANP produtores B100 (Sul e Centro-Oeste)

Fontes (mesma abordagem dos outros extractors do projeto):
    CONAB preco soja  → API REST portaldeinformacoes.conab.gov.br (semanal)
    SECEX farelo      → CSV bulk balanca.economia.gov.br por ano (mensal)
    BCB PTAX          → API REST olinda.bcb.gov.br (diário)
    ANP biodiesel     → XLS download gov.br (semanal, ~12 dias defasagem)

Schedules (GitHub Actions — um único workflow):
    Quinta + Sexta  → CONAB soja + ANP biodiesel + PTAX + Spread
    Dias 8–15/mês   → SECEX farelo + PTAX + Spread
    Cada seção verifica freshness antes de buscar (retry automático).

Uso:
    python extractor_crushing_spread.py             # rodada normal
    python extractor_crushing_spread.py --force-all # ignora freshness
"""

import io
import logging
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

DB_DIR        = Path(__file__).parent
DB_PATH       = DB_DIR / "crushing_spread.db"
HISTORY_START = date(2013, 1, 1)
TODAY         = date.today()
NOW_STR       = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
FORCE_ALL     = False

# Freshness — dias sem atualização antes de tentar nova busca
SOJA_STALE_DAYS   = 5    # semanal
BIO_STALE_DAYS    = 10   # ANP publica ~12 dias após fechamento da semana
FARELO_STALE_DAYS = 25   # mensal

# Fatores de conversão do esmagamento
FATOR_FARELO    = 0.77
FATOR_BIODIESEL = 0.19
CONV_SC60_TON   = 1000 / 60  # BRL/sc60kg → BRL/ton
# Densidade do biodiesel (t/m³). O preço ANP é BRL/m³ (volume) e o fator de
# rendimento 0.19 é mássico (t de bio por t de soja); dividir pela densidade
# converte a massa em volume: 0.19 t → 0.19/0.89 m³.
DENSIDADE_BIO   = 0.89

# ── CONAB — API preços ao produtor ───────────────────────────────────────────
# Mesmo endpoint usado no extractor_imea.py
CONAB_API = "https://portaldeinformacoes.conab.gov.br/index.php/api"

# Produtos e UFs de interesse (nomenclatura exata da API CONAB)
CONAB_SOJA_CONFIG = [
    {"uf": "RS", "produto": "SOJA EM GRÃOS   (60 kg)", "nivel": "PRODUTOR"},
    {"uf": "MT", "produto": "SOJA EM GRÃOS   (60 kg)", "nivel": "PRODUTOR"},
]

# ── CONAB — TXT precos semanais por produto/UF ───────────────────────────────
CONAB_PRECO_URL = (
    "https://portaldeinformacoes.conab.gov.br/downloads/arquivos/PrecosSemanalUF.txt"
)
CONAB_UFS     = {"RS", "MT"}
CONAB_PRODUTO = "SOJA"
CONAB_NIVEL   = "RECEBIDO"  # nivel = "PREÇO RECEBIDO P/ PR" (preço recebido pelo produtor)

# ── SECEX — CSV bulk por ano (mesmo padrão do extractor_secex.py) ────────────
SECEX_BASE_URL = "https://balanca.economia.gov.br/balanca/bd/comexstat-bd/ncm/EXP_{ano}.csv"
FARELO_NCM     = 23040090   # Farelo e resíduos da extração de óleo de soja (int)

# Filtro por UF de embarque (SG_UF_NCM) — mais robusto que filtrar por URF
# Santos → SP | Rio Grande → RS
UF_PORTO_CONFIG = {
    "Santos":     "SP",
    "Rio Grande": "RS",
}

# ── BCB PTAX ─────────────────────────────────────────────────────────────────
BCB_PTAX_URL = (
    "https://olinda.bcb.gov.br/olinda/servico/PTAX/versao/v1/odata/"
    "CotacaoDolarPeriodo(dataInicial=@di,dataFinalCotacao=@df)"
    "?@di='{di}'&@df='{df}'&$top=1000&$orderby=dataHoraCotacao%20asc"
    "&$format=json&$select=cotacaoVenda,dataHoraCotacao"
)

# ── ANP — XLS preços biodiesel B100 produtor ─────────────────────────────────
# URL confirmada na página da ANP (arquivo .xls)
ANP_BIODIESEL_URL = (
    "https://www.gov.br/anp/pt-br/assuntos/precos-e-defesa-da-concorrencia/precos/"
    "ppidp/precos-medios-ponderados-semanais-2013.xls"
)
ANP_REGIOES = {"SUL", "CENTRO-OESTE"}

# Mapeamento regional para o cálculo do spread
REGIAO_MAP = {
    "RS": {"uf": "RS", "porto": "Rio Grande", "bio": "SUL"},
    "MT": {"uf": "MT", "porto": "Santos",     "bio": "CENTRO-OESTE"},
}


# ─────────────────────────────────────────────────────────────────────────────
# Freshness check
# ─────────────────────────────────────────────────────────────────────────────

def is_stale(conn, table, stale_days, date_col="data_referencia", where=""):
    if FORCE_ALL:
        return True
    clause = f"WHERE {where}" if where else ""
    r = conn.execute(f"SELECT MAX({date_col}) FROM {table} {clause}").fetchone()
    last = r[0] if r and r[0] else None
    if not last:
        log.info(f"  [{table}] vazio — buscando historico completo")
        return True
    last_dt = datetime.strptime(str(last)[:10], "%Y-%m-%d").date()
    age = (TODAY - last_dt).days
    log.info(
        f"  [{table}] ultimo: {str(last)[:10]} ({age}d, limiar={stale_days}d) "
        f"→ {'DESATUALIZADO' if age > stale_days else 'OK'}"
    )
    return age > stale_days


# ─────────────────────────────────────────────────────────────────────────────
# DB helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(conn):
    conn.executescript("""
    -- Preço soja ao produtor — API CONAB (semanal)
    CREATE TABLE IF NOT EXISTS soja_conab (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        data_referencia TEXT NOT NULL,
        uf              TEXT NOT NULL,
        produto_conab   TEXT NOT NULL,
        nivel           TEXT NOT NULL,
        preco_brl_kg    REAL,
        preco_brl_sc60  REAL,
        fonte           TEXT DEFAULT 'CONAB/API',
        updated_at      TEXT,
        UNIQUE(data_referencia, uf, produto_conab, nivel)
    );
    CREATE INDEX IF NOT EXISTS idx_soja_data ON soja_conab(data_referencia);
    CREATE INDEX IF NOT EXISTS idx_soja_uf   ON soja_conab(uf);

    -- Cambio PTAX — BCB (diario)
    CREATE TABLE IF NOT EXISTS fx_ptax (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        data_referencia TEXT NOT NULL UNIQUE,
        ptax_venda      REAL NOT NULL,
        fonte           TEXT DEFAULT 'BCB/PTAX',
        updated_at      TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_ptax ON fx_ptax(data_referencia);

    -- Farelo de soja FOB — CSV SECEX/MDIC (mensal, por porto)
    CREATE TABLE IF NOT EXISTS farelo_secex (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        ano          INTEGER NOT NULL,
        mes          INTEGER NOT NULL,
        porto        TEXT NOT NULL,
        co_urf       TEXT NOT NULL,
        kg_liquido   REAL,
        vl_fob_usd   REAL,
        preco_usd_kg REAL,
        fonte        TEXT DEFAULT 'SECEX/MDIC',
        updated_at   TEXT,
        UNIQUE(ano, mes, porto)
    );
    CREATE INDEX IF NOT EXISTS idx_farelo ON farelo_secex(ano, mes);

    -- Biodiesel B100 produtor — ANP (semanal)
    CREATE TABLE IF NOT EXISTS biodiesel_anp (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        data_inicial TEXT NOT NULL,
        data_final   TEXT NOT NULL,
        regiao       TEXT NOT NULL,
        preco_brl_m3 REAL,
        fonte        TEXT DEFAULT 'ANP/Produtores',
        updated_at   TEXT,
        UNIQUE(data_inicial, regiao)
    );
    CREATE INDEX IF NOT EXISTS idx_bio ON biodiesel_anp(data_inicial);

    -- Spread calculado
    CREATE TABLE IF NOT EXISTS crushing_spread (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        data_referencia    TEXT NOT NULL,
        regiao             TEXT NOT NULL,
        preco_soja_sc60    REAL,
        preco_soja_ton     REAL,
        preco_farelo_usdkg REAL,
        preco_farelo_ton   REAL,
        ptax               REAL,
        preco_bio_m3       REAL,
        receita_farelo     REAL,
        receita_biodiesel  REAL,
        custo_soja         REAL,
        spread_brl_ton     REAL,
        updated_at         TEXT,
        UNIQUE(data_referencia, regiao)
    );
    CREATE INDEX IF NOT EXISTS idx_spread ON crushing_spread(data_referencia);
    """)
    conn.commit()


def last_date(conn, table, col="data_referencia", where=""):
    clause = f"WHERE {where}" if where else ""
    r = conn.execute(f"SELECT MAX({col}) FROM {table} {clause}").fetchone()
    return r[0] if r and r[0] else None


def safe_float(val):
    try:
        f = float(str(val).replace(",", ".").strip())
        return None if str(f) in ("nan", "inf", "-inf") else f
    except Exception:
        return None


def parse_date_br(raw):
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(str(raw).strip(), fmt).strftime("%Y-%m-%d")
        except Exception:
            continue
    return None


def _last_value(value_map, sorted_keys, target):
    """Forward-fill: retorna o último valor disponível em ou antes de target."""
    for k in reversed(sorted_keys):
        if k <= target:
            return value_map.get(k)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# HTTP helper
# ─────────────────────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9",
}


def download(url, label, fatal=True, extra_headers=None):
    hdrs = {**HEADERS, **(extra_headers or {})}
    for attempt in range(1, 4):
        try:
            log.info(f"[{label}] Download (tentativa {attempt}): {url}")
            r = requests.get(url, headers=hdrs, timeout=120, verify=False)
            r.raise_for_status()
            log.info(f"[{label}] {len(r.content):,} bytes")
            return r.content
        except requests.RequestException as e:
            log.warning(f"[{label}] Tentativa {attempt} falhou: {e}")
            if attempt < 3:
                time.sleep(10 * attempt)
    msg = f"[{label}] Todas as tentativas falharam."
    if fatal:
        raise RuntimeError(msg)
    log.error(msg)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# SEÇÃO 1 — Soja CONAB (API REST — mesmo padrão do extractor_imea.py)
# ─────────────────────────────────────────────────────────────────────────────

def run_soja(conn):
    """
    Precos de soja ao produtor — CONAB PrecosSemanalUF.txt (update semanal)
    Fonte: portaldeinformacoes.conab.gov.br/downloads/arquivos/PrecosSemanalUF.txt
    Produto: 'SOJA' (match exato apos strip — exclui farelo, semente, oleo)
    Nivel: 'PREÇO RECEBIDO P/ PR' (preco recebido pelo produtor, campo DSC_NIVEL_COMERCIALIZACAO)
    UFs: RS e MT | Preco em R$/kg → converte para R$/sc60 (×60)

    Historico anterior a 2025 vem do portal Siagro (precos mensais):
    portaldeinformacoes.conab.gov.br → Mercado → Preços Agropecuários
    Produto: 'SOJA EM GRÃOS   (60 kg)' | Nivel: PRODUTOR | UF: MT, RS
    e foi carregado manualmente no DB via XLSX exportado do portal.
    """
    log.info("=" * 60)
    log.info("Soja — CONAB PrecosSemanalUF.txt (RS e MT)")
    log.info("=" * 60)

    if not is_stale(conn, "soja_conab", SOJA_STALE_DAYS):
        log.info("[Soja] Dado fresco — pulando.")
        return {"skipped": True}

    ld      = last_date(conn, "soja_conab")
    content = download(CONAB_PRECO_URL, "conab-soja", fatal=True)

    for enc in ("latin-1", "utf-8-sig", "utf-8"):
        try:
            text = content.decode(enc)
            break
        except UnicodeDecodeError:
            continue

    first_line = text.splitlines()[0] if text.splitlines() else ""
    sep = "\t" if "\t" in first_line else ";"
    df = pd.read_csv(io.StringIO(text), sep=sep, on_bad_lines="skip", dtype=str)
    df.columns = [c.strip().upper() for c in df.columns]
    log.info(f"[Soja] Colunas: {list(df.columns)} | Linhas: {len(df)}")

    uf_col    = next((c for c in df.columns if c in ("UF", "SIGLA_UF")), None)
    prod_col  = next((c for c in df.columns if "PRODUTO" in c), None)
    nivel_col = next((c for c in df.columns if "NIVEL" in c or "COMERCI" in c), None)
    date_col  = next((c for c in df.columns if "DATA" in c), None)
    preco_col = next((c for c in df.columns if "VALOR" in c or "PRECO" in c), None)

    if not all([uf_col, prod_col, date_col, preco_col]):
        raise RuntimeError(f"[Soja] Colunas nao encontradas: {list(df.columns)}")

    # Produto exato = 'SOJA' (apos strip)
    df = df[df[prod_col].str.strip().str.upper() == "SOJA"].copy()
    log.info(f"[Soja] Linhas produto='SOJA': {len(df)}")

    # UFs RS e MT
    df = df[df[uf_col].str.strip().str.upper().isin({"RS", "MT"})].copy()
    log.info(f"[Soja] Apos filtro UF: {len(df)} linhas")

    # Nivel: 'PREÇO RECEBIDO P/ PR' — contem 'RECEBIDO'
    if nivel_col:
        niveis = df[nivel_col].str.strip().str.upper().unique()
        log.info(f"[Soja] Niveis disponiveis: {list(niveis)}")
        df = df[df[nivel_col].str.strip().str.upper().str.contains("RECEBIDO", na=False)].copy()
        log.info(f"[Soja] Apos filtro nivel RECEBIDO: {len(df)} linhas")

    if df.empty:
        log.warning("[Soja] Nenhuma linha apos filtros.")
        return {"inserido": 0}

    # Log das primeiras linhas para diagnostico
    amostra = df.head(3)
    for _, row in amostra.iterrows():
        log.info(
            f"[Soja] Amostra: uf={row.get(uf_col,'?')} | "
            f"data_raw='{row.get(date_col,'?')}' | "
            f"preco={row.get(preco_col,'?')}"
        )
    log.info(f"[Soja] ld (ultimo no banco) = {ld}")

    inserted = 0
    n_date_fail = 0
    n_ld_skip   = 0
    for _, row in df.iterrows():
        # DATA_INICIAL_FINAL_SEMANA: "DD-MM-YYYY - DD-MM-YYYY" — pega a inicial
        raw_field = str(row.get(date_col, "")).strip()
        raw_date  = raw_field.split(" - ")[0].strip()
        # Converte DD-MM-YYYY para parse_date_br (que aceita DD/MM/YYYY)
        raw_date  = raw_date.replace("-", "/")
        dr = parse_date_br(raw_date)
        if not dr:
            n_date_fail += 1
            if n_date_fail <= 2:
                log.warning(f"[Soja] Parse falhou: raw_field='{raw_field}' → raw_date='{raw_date}'")
            continue
        if ld and dr <= ld:
            n_ld_skip += 1
            continue
        uf       = str(row.get(uf_col, "")).strip().upper()
        nivel    = str(row.get(nivel_col, "")).strip() if nivel_col else "PREÇO RECEBIDO P/ PR"
        preco_kg = safe_float(row.get(preco_col))
        if not preco_kg or preco_kg <= 0:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO soja_conab "
            "(data_referencia, uf, produto_conab, nivel, "
            " preco_brl_kg, preco_brl_sc60, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (dr, uf, "SOJA", nivel, preco_kg, round(preco_kg * 60, 4), NOW_STR),
        )
        if conn.execute("SELECT changes()").fetchone()[0]:
            inserted += 1

    conn.commit()
    log.info(f"[Soja] {inserted} linhas inseridas | "
             f"date_fail={n_date_fail} | ld_skip={n_ld_skip}.")
    return {"inserido": inserted}


def run_ptax(conn):
    log.info("=" * 60)
    log.info("Cambio PTAX — BCB (incremental)")
    log.info("=" * 60)

    ld    = last_date(conn, "fx_ptax")
    start = (
        (datetime.strptime(ld, "%Y-%m-%d") + timedelta(days=1)).date()
        if ld else HISTORY_START
    )
    if start > TODAY:
        log.info("[PTAX] Ja atualizado.")
        return {"inserido": 0}

    inserted = 0
    current  = start
    while current <= TODAY:
        end_chunk = min(date(current.year, 12, 31), TODAY)
        url = BCB_PTAX_URL.format(
            di=current.strftime("%m-%d-%Y"),
            df=end_chunk.strftime("%m-%d-%Y"),
        )
        try:
            r    = requests.get(url, timeout=30)
            r.raise_for_status()
            data = r.json().get("value", [])
        except Exception as e:
            log.error(f"[PTAX] Falha em {current.year}: {e}")
            break

        for item in data:
            raw_dt = str(item.get("dataHoraCotacao", ""))[:10]
            ptax   = item.get("cotacaoVenda")
            if not raw_dt or ptax is None:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO fx_ptax "
                "(data_referencia, ptax_venda, updated_at) VALUES (?,?,?)",
                (raw_dt, float(ptax), NOW_STR),
            )
            if conn.execute("SELECT changes()").fetchone()[0]:
                inserted += 1

        log.info(f"[PTAX] {current.year}: {len(data)} registros")
        current = date(current.year + 1, 1, 1)
        time.sleep(0.3)

    conn.commit()
    log.info(f"[PTAX] {inserted} linhas inseridas.")
    return {"inserido": inserted}


# ─────────────────────────────────────────────────────────────────────────────
# SEÇÃO 3 — Farelo SECEX via CSV bulk (mesmo padrão do extractor_secex.py)
# ─────────────────────────────────────────────────────────────────────────────

def run_farelo(conn):
    log.info("=" * 60)
    log.info("Farelo de Soja FOB — CSV SECEX/MDIC (Santos e Rio Grande)")
    log.info("=" * 60)

    # Freshness check via data sintética YYYY-MM-01
    r = conn.execute(
        "SELECT MAX(printf('%04d-%02d-01', ano, mes)) FROM farelo_secex"
    ).fetchone()
    last_ym = r[0] if r and r[0] else None

    if not FORCE_ALL and last_ym:
        last_dt = datetime.strptime(last_ym, "%Y-%m-%d").date()
        age     = (TODAY - last_dt).days
        log.info(
            f"  [farelo_secex] ultimo: {last_ym[:7]} ({age}d, "
            f"limiar={FARELO_STALE_DAYS}d) → "
            f"{'DESATUALIZADO' if age > FARELO_STALE_DAYS else 'OK'}"
        )
        if age <= FARELO_STALE_DAYS:
            log.info("[Farelo] Dado fresco — pulando.")
            return {"skipped": True}

    # Descobre a partir de qual ano buscar (mesmo padrão do extractor_secex.py)
    r2 = conn.execute(
        "SELECT MAX(ano) FROM farelo_secex"
    ).fetchone()
    ultimo_ano = int(r2[0]) if r2 and r2[0] else None

    if ultimo_ano:
        anos = list(range(ultimo_ano, TODAY.year + 1))
        log.info(f"[Farelo] Atualização incremental desde {ultimo_ano}")
    else:
        anos = list(range(HISTORY_START.year, TODAY.year + 1))
        log.info(f"[Farelo] Carga histórica desde {HISTORY_START.year}")

    total_inserted = 0

    for ano in anos:
        url = SECEX_BASE_URL.format(ano=ano)
        log.info(f"[Farelo] Baixando CSV {ano}...")

        try:
            r_http = requests.get(url, stream=True, verify=False, timeout=120)
            if r_http.status_code != 200:
                log.warning(f"[Farelo] {ano} nao disponivel (status {r_http.status_code})")
                continue

            df = pd.read_csv(
                io.StringIO(r_http.content.decode("latin1")),
                sep=";",
                dtype={"CO_NCM": int, "CO_URF": str},
            )

            # Filtra NCM de farelo de soja (CO_NCM lido como int)
            df_farelo = df[df["CO_NCM"] == FARELO_NCM].copy()
            if df_farelo.empty:
                log.info(f"[Farelo] {ano}: NCM {FARELO_NCM} nao encontrado")
                continue

            log.info(f"[Farelo] {ano}: {len(df_farelo)} linhas de farelo encontradas")
            # Mostra UFs de embarque disponiveis para diagnostico
            if "SG_UF_NCM" in df_farelo.columns:
                ufs = df_farelo["SG_UF_NCM"].dropna().unique()
                log.info(f"[Farelo] {ano}: UFs de embarque: {sorted(ufs)}")

            # Filtra por UF de embarque (SG_UF_NCM) — mais robusto que filtrar por URF
            # SP = Santos | RS = Rio Grande
            for porto, uf_emb in UF_PORTO_CONFIG.items():
                col_uf = next((c for c in df_farelo.columns
                               if "UF_NCM" in c or "SG_UF" in c), None)
                if col_uf is None:
                    log.warning(f"[Farelo] {ano}: coluna UF nao encontrada. "
                                f"Colunas: {list(df_farelo.columns[:8])}")
                    break
                df_porto = df_farelo[df_farelo[col_uf].str.strip().str.upper() == uf_emb].copy()
                if df_porto.empty:
                    log.info(f"[Farelo] {ano}/{porto} (UF={uf_emb}): sem dados")
                    continue

                df_agg = (
                    df_porto.groupby(["CO_ANO", "CO_MES"])[["VL_FOB", "KG_LIQUIDO"]]
                    .sum()
                    .reset_index()
                )

                for _, row in df_agg.iterrows():
                    a   = int(row["CO_ANO"])
                    m   = int(row["CO_MES"])
                    kg  = safe_float(row["KG_LIQUIDO"])
                    fob = safe_float(row["VL_FOB"])
                    if not kg or kg == 0 or not fob:
                        continue

                    conn.execute(
                        "INSERT OR REPLACE INTO farelo_secex "
                        "(ano, mes, porto, co_urf, kg_liquido, vl_fob_usd, "
                        " preco_usd_kg, updated_at) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (a, m, porto, uf_emb, kg, fob,
                         round(fob / kg, 6), NOW_STR),
                    )
                    if conn.execute("SELECT changes()").fetchone()[0]:
                        total_inserted += 1

            conn.commit()
            log.info(f"[Farelo] {ano}: processado")

        except Exception as e:
            log.error(f"[Farelo] Erro ao processar {ano}: {e}")

    log.info(f"[Farelo] {total_inserted} registros inseridos/atualizados.")
    return {"inserido": total_inserted}


# ─────────────────────────────────────────────────────────────────────────────
# SEÇÃO 4 — Biodiesel ANP
# ─────────────────────────────────────────────────────────────────────────────

def run_biodiesel(conn):
    log.info("=" * 60)
    log.info("Biodiesel B100 — ANP Produtores (Sul e Centro-Oeste)")
    log.info("=" * 60)

    if not is_stale(conn, "biodiesel_anp", BIO_STALE_DAYS, date_col="data_inicial"):
        log.info("[Biodiesel] Dado fresco — pulando.")
        return {"skipped": True}

    ld      = last_date(conn, "biodiesel_anp", col="data_inicial")
    content = download(ANP_BIODIESEL_URL, "biodiesel-anp", fatal=True)
    df      = _parse_anp_biodiesel(content)

    if df.empty or "regiao" not in df.columns:
        log.warning("[Biodiesel] Parser nao retornou dados com coluna 'regiao'.")
        return {"inserido": 0}

    df = df[df["regiao"].isin(ANP_REGIOES)]
    if ld:
        df = df[df["data_inicial"] > ld]
    if df.empty:
        log.info("[Biodiesel] Nenhum dado novo.")
        return {"inserido": 0}

    inserted = 0
    for _, row in df.iterrows():
        conn.execute(
            "INSERT OR IGNORE INTO biodiesel_anp "
            "(data_inicial, data_final, regiao, preco_brl_m3, updated_at) "
            "VALUES (?,?,?,?,?)",
            (row["data_inicial"], row["data_final"],
             row["regiao"], row["preco"], NOW_STR),
        )
        if conn.execute("SELECT changes()").fetchone()[0]:
            inserted += 1

    conn.commit()
    log.info(f"[Biodiesel] {inserted} linhas inseridas.")
    return {"inserido": inserted}


def _parse_anp_biodiesel(content):
    """
    Layout confirmado nos logs (linha 8 = regioes, linha 9+ = dados):
      ['(A partir de 2013)', 'Norte', 'Nordeste', 'Centro-Oeste', 'Sul', 'Sudeste']
      linha 9: [produto, data_ini, data_fim, norte, nordeste, centro-oeste, sul, sudeste, brasil]
      produto ex: "Biodiesel (B100) (R$/m3)"
      datas como objetos datetime pandas
      idx_sul=6, idx_co=5 (confirmados nos logs)
    """
    try:
        xl = pd.ExcelFile(io.BytesIO(content), engine="xlrd")
        log.info(f"[Biodiesel] Abas: {xl.sheet_names}")
    except Exception as e:
        raise RuntimeError(f"[Biodiesel] Nao foi possivel abrir .xls: {e}")

    all_rows = []

    for sheet in xl.sheet_names:
        try:
            # Le sem cabecalho — retorna DataFrame com indice 0..N
            raw = xl.parse(sheet, header=None)
        except Exception:
            continue

        # Reset index para garantir acesso por posicao numerica com .iloc
        raw = raw.reset_index(drop=True)

        # Procura linha de regioes (Norte + Nordeste + Sul juntos)
        regiao_pos = None  # posicao numerica no DataFrame resetado
        for pos in range(len(raw)):
            vals = [str(v).strip() for v in raw.iloc[pos].tolist() if pd.notna(v) and str(v).strip()]
            vals_up = [v.upper() for v in vals]
            if "NORTE" in vals_up and "NORDESTE" in vals_up and "SUL" in vals_up:
                regiao_pos = pos
                log.info(f"[Biodiesel] Linha de regioes na posicao {pos}: {vals}")
                break

        if regiao_pos is None:
            log.warning(f"[Biodiesel] Aba '{sheet}': linha de regioes nao encontrada")
            continue

        # Mapa de indices de coluna a partir da linha de regioes
        regiao_vals = raw.iloc[regiao_pos].tolist()
        log.info(f"[Biodiesel] Valores das colunas: {regiao_vals}")

        idx_sul = None
        idx_co  = None
        for j, v in enumerate(regiao_vals):
            v_str = str(v).strip().upper() if pd.notna(v) else ""
            if "SUL" in v_str and "SUDE" not in v_str:
                idx_sul = j
            if "CENTRO" in v_str:
                idx_co = j
        log.info(f"[Biodiesel] idx_sul={idx_sul}, idx_co={idx_co}")

        if idx_sul is None and idx_co is None:
            log.warning("[Biodiesel] SUL e CENTRO-OESTE nao mapeados")
            continue

        def parse_any_date(rv):
            if rv is None:
                return None
            if hasattr(rv, 'strftime'):
                return rv.strftime("%Y-%m-%d")
            s = str(rv).strip()
            if not s or s.lower() in ("nat", "nan", ""):
                return None
            # "2013-01-06 00:00:00" ou "2013-01-06"
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y"):
                try:
                    return datetime.strptime(s[:19], fmt).strftime("%Y-%m-%d")
                except Exception:
                    continue
            return None

        # Itera linhas de dados abaixo da linha de regioes (usa posicao numerica)
        n_parsed = 0
        n_bio    = 0
        for pos in range(regiao_pos + 1, len(raw)):
            row_vals = raw.iloc[pos].tolist()

            # Coluna 0 = produto
            prod_raw = str(row_vals[0]).strip() if pd.notna(row_vals[0]) else ""
            if not prod_raw:
                continue
            prod_up = prod_raw.upper()

            # Conta quantas linhas de biodiesel passou
            if "BIODIESEL" in prod_up or "B100" in prod_up:
                n_bio += 1
                if n_bio <= 3:
                    log.info(f"[Biodiesel] Linha {pos}: produto='{prod_raw}', "
                             f"col1={row_vals[1] if len(row_vals)>1 else '?'}, "
                             f"col2={row_vals[2] if len(row_vals)>2 else '?'}, "
                             f"col{idx_sul}={row_vals[idx_sul] if idx_sul and idx_sul<len(row_vals) else '?'}, "
                             f"col{idx_co}={row_vals[idx_co] if idx_co and idx_co<len(row_vals) else '?'}")
            else:
                continue

            di  = parse_any_date(row_vals[1] if len(row_vals) > 1 else None)
            df_ = parse_any_date(row_vals[2] if len(row_vals) > 2 else None)

            if not di or not df_:
                continue

            for idx, regiao_nome in [(idx_sul, "SUL"), (idx_co, "CENTRO-OESTE")]:
                if idx is None or idx >= len(row_vals):
                    continue
                val = row_vals[idx]
                if val is None or (isinstance(val, float) and pd.isna(val)):
                    continue
                val_str = str(val).strip()
                if val_str in ("***", "", "nan", "NaN"):
                    continue
                preco = safe_float(val_str)
                if preco and preco > 0:
                    # Planilha ANP usa R$/litro (~6-8 R$/l para biodiesel)
                    # Precisamos de R$/m³ (~6000-8000 R$/m³) → multiplica por 1000
                    if preco < 50:        # R$/litro
                        preco_m3 = round(preco * 1000, 2)
                    elif preco < 500:     # R$/100l (improvavel, mas cobre)
                        preco_m3 = round(preco * 10, 2)
                    else:                 # ja em R$/m³
                        preco_m3 = round(preco, 2)
                    all_rows.append({
                        "data_inicial": di,
                        "data_final":   df_,
                        "regiao":       regiao_nome,
                        "preco":        preco_m3,
                    })
                    n_parsed += 1

        log.info(f"[Biodiesel] Aba '{sheet}': {n_bio} linhas B100 encontradas, "
                 f"{n_parsed} registros extraidos")

    result = pd.DataFrame(all_rows)
    if not result.empty:
        result = result.drop_duplicates(subset=["data_inicial", "regiao"])
        result = result.sort_values("data_inicial")
        log.info(f"[Biodiesel] {len(result)} registros | "
                 f"{result['data_inicial'].min()} → {result['data_inicial'].max()}")
        log.info(f"[Biodiesel] Regioes: {result['regiao'].value_counts().to_dict()}")
    else:
        log.warning("[Biodiesel] Nenhum registro parseado.")
    return result


def run_spread(conn):
    log.info("=" * 60)
    log.info("Crushing Spread — Calculando RS e MT")
    log.info("=" * 60)

    # Media por data/UF para consolidar diferentes pontos de coleta
    soja_df = pd.read_sql(
        "SELECT data_referencia, uf, AVG(preco_brl_sc60) AS preco "
        "FROM soja_conab WHERE preco_brl_sc60 > 0 "
        "GROUP BY data_referencia, uf ORDER BY data_referencia",
        conn,
    )
    farelo_df = pd.read_sql(
        "SELECT ano, mes, porto, preco_usd_kg "
        "FROM farelo_secex WHERE preco_usd_kg > 0 ORDER BY ano, mes",
        conn,
    )
    ptax_df = pd.read_sql(
        "SELECT data_referencia, ptax_venda FROM fx_ptax ORDER BY data_referencia",
        conn,
    )
    bio_df = pd.read_sql(
        "SELECT data_inicial, data_final, regiao, preco_brl_m3 "
        "FROM biodiesel_anp WHERE preco_brl_m3 > 0 ORDER BY data_inicial",
        conn,
    )

    missing = [n for n, df in [("soja", soja_df), ("farelo", farelo_df),
                                ("ptax", ptax_df), ("bio", bio_df)] if df.empty]
    if missing:
        log.warning(f"[Spread] Dados insuficientes — faltam: {missing}")
        return {"calculado": 0}

    ld_spread = last_date(conn, "crushing_spread")
    start = (
        datetime.strptime(ld_spread, "%Y-%m-%d").date() + timedelta(days=1)
        if ld_spread else HISTORY_START
    )

    ptax_map   = dict(zip(ptax_df["data_referencia"],
                          ptax_df["ptax_venda"].astype(float)))
    ptax_dates = sorted(ptax_map.keys())

    inserted = 0
    d = start

    while d <= TODAY:
        d_str = d.strftime("%Y-%m-%d")
        ano   = d.year
        mes   = d.month

        ptax = _last_value(ptax_map, ptax_dates, d_str)
        if ptax is None:
            d += timedelta(days=7)
            continue

        for regiao, cfg in REGIAO_MAP.items():

            # Soja — forward-fill por UF
            uf_df      = soja_df[soja_df["uf"] == cfg["uf"]]
            soja_map   = dict(zip(uf_df["data_referencia"],
                                  uf_df["preco"].astype(float)))
            soja_dates = sorted(soja_map.keys())
            p_soja_sc60 = _last_value(soja_map, soja_dates, d_str)
            if p_soja_sc60 is None:
                continue

            # Farelo — mes corrente, fallback mes anterior
            porto_df   = farelo_df[farelo_df["porto"] == cfg["porto"]]
            farelo_row = porto_df[
                (porto_df["ano"] == ano) & (porto_df["mes"] == mes)
            ]
            if farelo_row.empty:
                prev_mes = mes - 1 if mes > 1 else 12
                prev_ano = ano if mes > 1 else ano - 1
                farelo_row = porto_df[
                    (porto_df["ano"] == prev_ano) & (porto_df["mes"] == prev_mes)
                ]
            if farelo_row.empty:
                continue
            p_farelo_usdkg = float(farelo_row.iloc[-1]["preco_usd_kg"])

            # Biodiesel — semana ANP, fallback 21 dias
            bio_reg  = bio_df[bio_df["regiao"] == cfg["bio"]]
            bio_rows = bio_reg[
                (bio_reg["data_inicial"] <= d_str) &
                (bio_reg["data_final"]   >= d_str)
            ]
            if bio_rows.empty:
                cutoff   = (d - timedelta(days=21)).strftime("%Y-%m-%d")
                bio_rows = bio_reg[bio_reg["data_final"] >= cutoff]
                if bio_rows.empty:
                    continue
            p_bio_m3 = float(bio_rows.iloc[-1]["preco_brl_m3"])

            # Calculo do spread
            p_soja_ton    = p_soja_sc60 * CONV_SC60_TON
            p_farelo_ton  = p_farelo_usdkg * 1000 * ptax  # USD/kg → R$/ton

            receita_farelo    = p_farelo_ton * FATOR_FARELO
            receita_biodiesel = p_bio_m3     * FATOR_BIODIESEL / DENSIDADE_BIO
            custo_soja        = p_soja_ton
            spread            = receita_farelo + receita_biodiesel - custo_soja

            conn.execute(
                "INSERT OR REPLACE INTO crushing_spread "
                "(data_referencia, regiao, preco_soja_sc60, preco_soja_ton, "
                " preco_farelo_usdkg, preco_farelo_ton, ptax, preco_bio_m3, "
                " receita_farelo, receita_biodiesel, custo_soja, spread_brl_ton, "
                " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (d_str, regiao,
                 round(p_soja_sc60,    4), round(p_soja_ton,    4),
                 round(p_farelo_usdkg, 6), round(p_farelo_ton,  4),
                 round(ptax,           4), round(p_bio_m3,      4),
                 round(receita_farelo, 4), round(receita_biodiesel, 4),
                 round(custo_soja,     4), round(spread,         4),
                 NOW_STR),
            )
            if conn.execute("SELECT changes()").fetchone()[0]:
                inserted += 1

        d += timedelta(days=7)

    conn.commit()
    log.info(f"[Spread] {inserted} registros calculados/atualizados.")
    return {"calculado": inserted}


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard HTML
# ─────────────────────────────────────────────────────────────────────────────

def generate_dashboard(conn) -> None:
    """
    Regenera crushing_spread.html com os dados mais recentes do banco.
    Mesmo padrão dos outros dashboards do projeto (dados embutidos como JSON).
    """
    log.info("[Dashboard] Gerando crushing_spread.html...")

    BIO_START = "2022-01-01"

    # ── Carrega series ────────────────────────────────────────────────────────
    rows = conn.execute("""
        SELECT data_referencia, regiao,
               preco_soja_sc60, preco_farelo_usdkg, preco_farelo_ton, ptax,
               preco_bio_m3, receita_farelo, receita_biodiesel,
               custo_soja, spread_brl_ton
        FROM crushing_spread
        WHERE data_referencia >= ?
        ORDER BY data_referencia, regiao
    """, (BIO_START,)).fetchall()

    last_date = conn.execute(
        "SELECT MAX(data_referencia) FROM crushing_spread WHERE data_referencia >= ?",
        (BIO_START,)
    ).fetchone()[0] or ""

    series = {"MT": [], "RS": []}
    for r in rows:
        series[r["regiao"]].append({
            "d":         r["data_referencia"],
            "soja_sc":   round(r["preco_soja_sc60"],    2),
            "farelo_t":  round(r["preco_farelo_ton"],   2),
            "farelo_usdkg": round(r["preco_farelo_usdkg"], 6),
            "ptax":      round(r["ptax"],               4),
            "bio_m3":    round(r["preco_bio_m3"],       2),
            "rec_farelo":round(r["receita_farelo"],     2),
            "rec_bio":   round(r["receita_biodiesel"],  2),
            "custo":     round(r["custo_soja"],         2),
            "spread":    round(r["spread_brl_ton"],     2),
        })

    # Últimos valores por regiao para o P&L
    latest = {}
    for regiao in ["MT", "RS"]:
        r = conn.execute("""
            SELECT preco_soja_sc60, preco_farelo_usdkg, preco_farelo_ton, ptax,
                   preco_bio_m3, receita_farelo, receita_biodiesel, custo_soja, spread_brl_ton
            FROM crushing_spread
            WHERE regiao=? AND data_referencia=(SELECT MAX(data_referencia) FROM crushing_spread WHERE regiao=?)
        """, (regiao, regiao)).fetchone()
        if r:
            latest[regiao] = {
                "soja_sc":      round(r["preco_soja_sc60"],    2),
                "farelo_usdkg": round(r["preco_farelo_usdkg"], 6),
                "farelo_t":     round(r["preco_farelo_ton"],   2),
                "ptax":         round(r["ptax"],               4),
                "bio_m3":       round(r["preco_bio_m3"],       2),
                "rec_farelo":   round(r["receita_farelo"],     2),
                "rec_bio":      round(r["receita_biodiesel"],  2),
                "custo":        round(r["custo_soja"],         2),
                "spread":       round(r["spread_brl_ton"],     2),
            }

    import json as _json
    data_json = _json.dumps({"last_date": last_date, "series": series, "latest": latest})

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>Soy Crushing Spread — Biodiesel</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script>
// Rotulos do eixo X: horizontais enquanto couberem; quando nao couberem, vao
// para 90 graus (leitura de baixo para cima, topo do texto a esquerda). Nunca
// na diagonal — sozinho o Chart.js escolheria qualquer angulo entre 0 e 50.
(function(){{
  function tickFontString(scale){{
    var f = scale.options.ticks.font || {{}}, d = Chart.defaults.font;
    var size = f.size || d.size || 12, fam = f.family || d.family, w = f.weight || '';
    return (w ? w + ' ' : '') + size + 'px ' + fam;
  }}
  // Quantos rotulos vao mesmo aparecer. Este hook roda ANTES do autoSkip, entao
  // scale.ticks ainda tem a lista inteira. Com autoSkip livre o Chart.js pula
  // rotulos ate nao haver sobreposicao — nesse caso girar seria desnecessario;
  // o aperto so acontece quando maxTicksLimit forca uma quantidade fixa.
  function shownTickCount(scale){{
    var n = (scale.ticks || []).length, o = scale.options.ticks;
    if (o.autoSkip === false) return n;
    return o.maxTicksLimit ? Math.min(n, o.maxTicksLimit) : 0;
  }}
  function overflows(scale){{
    if (!scale.isHorizontal()) return false;
    var n = shownTickCount(scale);
    if (!n) return false;
    var ctx = scale.ctx, widest = 0, ticks = scale.ticks;
    ctx.save();
    ctx.font = tickFontString(scale);
    for (var i = 0; i < ticks.length; i++){{
      var l = ticks[i].label;
      var s = Array.isArray(l) ? l.join(' ') : String(l == null ? '' : l);
      widest = Math.max(widest, ctx.measureText(s).width);
    }}
    ctx.restore();
    return widest + 6 > scale.width / n;   // 6px de respiro entre rotulos
  }}
  function snapRotation(scale){{ scale.labelRotation = overflows(scale) ? 90 : 0; }}
  ['category','linear','logarithmic','time','timeseries'].forEach(function(t){{
    Chart.defaults.scales[t] = Chart.defaults.scales[t] || {{}};
    Chart.defaults.scales[t].afterCalculateLabelRotation = snapRotation;
  }});
}})();
</script>
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0;}}
:root{{
  --orange:#FF5500;--black:#1A1A1A;--gray-dark:#333;--gray-mid:#888;
  --gray-light:#F4F4F4;--border:#E5E5E5;--white:#fff;
  --p1:#FF5500;--p2:#1A1A1A;--p3:#8C8C8C;--p4:#BDBDBD;--p5:#123B63;
  --font:'Segoe UI',Helvetica,Arial,sans-serif;--radius:8px;
}}
html,body{{font-family:var(--font);background:var(--gray-light);color:var(--gray-dark);}}
.filter-bar{{background:var(--white);border-bottom:1px solid var(--border);
  padding:12px 20px;display:flex;align-items:flex-end;gap:16px;flex-wrap:wrap;}}
.filter-group{{display:flex;flex-direction:column;gap:4px;}}
.filter-label{{font-size:10px;font-weight:700;color:var(--gray-mid);text-transform:uppercase;letter-spacing:.5px;}}
.filter-sep{{width:1px;height:44px;background:var(--border);align-self:center;}}
.seg{{display:inline-flex;background:var(--gray-light);border:1px solid var(--border);border-radius:5px;padding:2px;gap:2px;}}
.seg-btn{{padding:5px 14px;border:none;border-radius:4px;font-size:12px;font-weight:600;
  cursor:pointer;font-family:var(--font);background:transparent;color:var(--gray-mid);transition:all .15s;}}
.seg-btn.on{{background:var(--orange);color:white;box-shadow:0 1px 3px rgba(0,0,0,.15);}}
.seg-btn:hover:not(.on){{color:var(--gray-dark);}}
.blend-group{{min-width:230px;}}
.blend-row{{display:flex;align-items:center;gap:10px;}}
.blend-range{{-webkit-appearance:none;appearance:none;width:160px;height:4px;border-radius:2px;
  background:linear-gradient(90deg,var(--p1) 0%,var(--p1) 50%,var(--border) 50%,var(--border) 100%);outline:none;cursor:pointer;}}
.blend-range::-webkit-slider-thumb{{-webkit-appearance:none;appearance:none;width:14px;height:14px;border-radius:50%;
  background:var(--p1);border:2px solid var(--white);box-shadow:0 1px 3px rgba(0,0,0,.3);cursor:pointer;}}
.blend-range::-moz-range-thumb{{width:14px;height:14px;border-radius:50%;background:var(--p1);
  border:2px solid var(--white);box-shadow:0 1px 3px rgba(0,0,0,.3);cursor:pointer;}}
.blend-val{{font-size:11.5px;font-weight:700;color:var(--gray-dark);white-space:nowrap;min-width:118px;}}
.blend-val .rs{{color:var(--p5);}}
.blend-val .mt{{color:var(--p1);}}
.body{{padding:16px 20px 60px;}}
.section-hdr{{display:flex;align-items:center;justify-content:space-between;margin:20px 0 10px;}}
.section-title{{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.7px;
  color:var(--gray-mid);border-left:3px solid var(--orange);padding-left:8px;}}
.view-toggle{{display:inline-flex;background:var(--gray-light);border:1px solid var(--border);border-radius:5px;padding:2px;gap:2px;}}
.vt-btn{{padding:4px 12px;border:none;border-radius:4px;font-size:11px;font-weight:600;
  cursor:pointer;font-family:var(--font);background:transparent;color:var(--gray-mid);transition:all .15s;}}
.vt-btn.on{{background:var(--white);color:var(--orange);box-shadow:0 1px 3px rgba(0,0,0,.08);}}
.chart-grid{{display:grid;gap:12px;margin-bottom:12px;}}
.g2{{grid-template-columns:1fr 1fr;}}
.chart-card{{background:var(--white);border:1px solid var(--border);border-radius:var(--radius);padding:16px 18px;}}
.chart-title{{font-size:13px;font-weight:700;color:var(--black);margin-bottom:2px;}}
.chart-sub{{font-size:11px;color:var(--gray-mid);margin-bottom:12px;}}
.chart-wrap{{position:relative;height:200px;}}
.chart-wrap-tall{{position:relative;height:240px;}}
.pl-wrap{{overflow-x:auto;}}
.pl-table{{border-collapse:collapse;font-size:11.5px;width:100%;min-width:600px;}}
.pl-table th{{padding:7px 12px;font-size:9.5px;font-weight:700;text-transform:uppercase;
  letter-spacing:.4px;color:var(--gray-mid);border-bottom:2px solid var(--border);
  white-space:nowrap;background:var(--gray-light);text-align:right;}}
.pl-table th.lbl{{text-align:left;min-width:180px;position:sticky;left:0;z-index:2;background:var(--gray-light);}}
.pl-table td{{padding:6px 12px;text-align:right;border-bottom:1px solid #f0f0f0;white-space:nowrap;}}
.pl-table td.lbl{{text-align:left;position:sticky;left:0;background:var(--white);z-index:1;font-size:12px;color:var(--gray-dark);}}
.pl-table tr.group-hdr td{{background:#fafafa;font-size:9.5px;font-weight:700;text-transform:uppercase;
  letter-spacing:.4px;color:var(--gray-mid);padding-top:10px;border-bottom:none;}}
.pl-table tr.group-hdr td.lbl{{background:#fafafa;}}
.pl-table tr.sub td.lbl{{padding-left:22px;color:var(--gray-mid);font-size:11.5px;}}
.pl-table tr.total td{{font-weight:700;border-top:1.5px solid var(--border);border-bottom:1.5px solid var(--border);}}
.pl-table tr.total.spread td{{background:#fff8f5;}}
.pl-table .pos{{color:var(--p2);font-weight:600;}}
.pl-table .neg{{color:var(--p1);font-weight:600;}}
.pl-table .na{{color:#ccc;}}
.source-note{{font-size:10px;color:#bbb;text-align:right;margin-top:14px;font-style:italic;}}
@media(max-width:900px){{.g2{{grid-template-columns:1fr;}}}}
</style>
</head>
<body>
<div class="filter-bar">
  <div class="filter-group">
    <div class="filter-label">Region</div>
    <div class="seg" id="seg-region">
      <button class="seg-btn on" onclick="setRegion('MT',this)">Mato Grosso</button>
      <button class="seg-btn"    onclick="setRegion('RS',this)">Rio Grande do Sul</button>
      <button class="seg-btn"    onclick="setRegion('BLEND',this)">Blend MT + RS</button>
    </div>
  </div>
  <div class="filter-group blend-group" id="blend-group" style="display:none;">
    <div class="filter-label">Blend weight</div>
    <div class="blend-row">
      <input type="range" class="blend-range" id="blend-range" min="0" max="100" step="5" value="50"
             oninput="setBlend(this.value)"/>
      <span class="blend-val" id="blend-val"><span class="mt">MT 50%</span> · <span class="rs">RS 50%</span></span>
    </div>
  </div>
  <div class="filter-sep"></div>
  <div class="filter-group">
    <div class="filter-label">Frequency</div>
    <div class="seg" id="seg-freq">
      <button class="seg-btn on" onclick="setFreq('M',this)">Monthly</button>
      <button class="seg-btn"    onclick="setFreq('Q',this)">Quarterly</button>
    </div>
  </div>
  <div class="filter-sep"></div>
  <div class="filter-group">
    <div class="filter-label">Period</div>
    <div class="seg" id="seg-period">
      <button class="seg-btn on" onclick="setPeriod('all',this)">All</button>
      <button class="seg-btn"    onclick="setPeriod('1y',this)">1Y</button>
      <button class="seg-btn"    onclick="setPeriod('6m',this)">6M</button>
    </div>
  </div>
  <div class="filter-sep"></div>
  <div class="filter-group">
    <div class="filter-label">Formula</div>
    <div style="font-size:11.5px;color:var(--gray-mid);line-height:1.7;padding-top:2px;">
      Spread = (Meal &times;0.77) + (Bio &times;0.19 &divide; 0.89) &minus; Soy &nbsp;|&nbsp;
      <span style="color:var(--gray-dark);font-weight:600;" id="updated-label">&mdash;</span>
    </div>
  </div>
</div>
<div class="body">
  <div class="section-hdr">
    <span class="section-title">Revenue &amp; Cost Components</span>
    <div class="view-toggle">
      <button class="vt-btn on" onclick="setCompView('chart',this)">&#9783; Chart</button>
      <button class="vt-btn"    onclick="setCompView('table',this)">&#9783; P&amp;L</button>
    </div>
  </div>
  <div class="chart-card" style="margin-bottom:12px;">
    <div id="comp-chart-view">
      <div class="chart-title">Revenue &amp; Cost Components</div>
      <div class="chart-sub">BRL/ton of soy &mdash; total revenue, meal revenue, biodiesel revenue, soy cost</div>
      <div class="chart-wrap-tall"><canvas id="chartComp"></canvas></div>
    </div>
    <div id="comp-table-view" style="display:none;">
      <div class="chart-title" style="margin-bottom:12px;">P&amp;L &mdash; Revenue &amp; Cost Components</div>
      <div class="pl-wrap"><table class="pl-table" id="pl-table"></table></div>
    </div>
  </div>
  <div class="section-hdr"><span class="section-title">Crushing Spread</span></div>
  <div class="chart-grid g2">
    <div class="chart-card">
      <div class="chart-title">Historical (BRL/ton)</div>
      <div class="chart-sub">Meal FOB SECEX &middot; Biodiesel B100 ANP &middot; CONAB farm gate</div>
      <div class="chart-wrap"><canvas id="chartSpread"></canvas></div>
    </div>
    <div class="chart-card">
      <div class="chart-title">Seasonality</div>
      <div class="chart-sub" id="seas-sub-spread">BRL/ton &middot; each line = one year &middot; bold = historical avg</div>
      <div class="chart-wrap"><canvas id="chartSpreadSeas"></canvas></div>
    </div>
  </div>
  <div class="section-hdr"><span class="section-title">Biodiesel B100 &mdash; Producer Price</span></div>
  <div class="chart-grid g2">
    <div class="chart-card">
      <div class="chart-title">Historical (BRL/m&sup3;)</div>
      <div class="chart-sub" id="bio-sub">ANP weighted average &middot; producer level</div>
      <div class="chart-wrap"><canvas id="chartBio"></canvas></div>
    </div>
    <div class="chart-card">
      <div class="chart-title">Seasonality</div>
      <div class="chart-sub" id="seas-sub-bio">BRL/m&sup3; &middot; each line = one year &middot; bold = historical avg</div>
      <div class="chart-wrap"><canvas id="chartBioSeas"></canvas></div>
    </div>
  </div>
  <div class="section-hdr"><span class="section-title">Soybean Meal FOB</span></div>
  <div class="chart-grid g2">
    <div class="chart-card">
      <div class="chart-title">Historical (BRL/ton)</div>
      <div class="chart-sub" id="meal-sub">SECEX NCM 23040090 &middot; port</div>
      <div class="chart-wrap"><canvas id="chartMeal"></canvas></div>
    </div>
    <div class="chart-card">
      <div class="chart-title">Seasonality</div>
      <div class="chart-sub" id="seas-sub-meal">BRL/ton &middot; each line = one year &middot; bold = historical avg</div>
      <div class="chart-wrap"><canvas id="chartMealSeas"></canvas></div>
    </div>
  </div>
  <div class="section-hdr"><span class="section-title">Soy Farm Gate</span></div>
  <div class="chart-grid g2">
    <div class="chart-card">
      <div class="chart-title">Historical (BRL/sc 60 kg)</div>
      <div class="chart-sub">CONAB/Siagro monthly average &middot; producer level</div>
      <div class="chart-wrap"><canvas id="chartSoja"></canvas></div>
    </div>
    <div class="chart-card">
      <div class="chart-title">Seasonality</div>
      <div class="chart-sub" id="seas-sub-soja">BRL/sc 60 kg &middot; each line = one year &middot; bold = historical avg</div>
      <div class="chart-wrap"><canvas id="chartSojaSeas"></canvas></div>
    </div>
  </div>
  <p class="source-note">Sources: CONAB/Siagro (soy prices) &middot; SECEX/MDIC NCM 23040090 (meal FOB) &middot; ANP B100 producers (biodiesel) &middot; BCB PTAX (FX) &middot; Agri Monitor</p>
</div>
<script>
const CFG = {{
  MT:    {{ port:'Santos',                bio:'Centro-Oeste'          }},
  RS:    {{ port:'Rio Grande',            bio:'Sul'                   }},
  BLEND: {{ port:'Santos + Rio Grande',   bio:'Centro-Oeste + Sul'    }},
}};
const FONT = "'Segoe UI',Helvetica,Arial,sans-serif";  // mesma stack do CSS da pagina
Chart.defaults.font.family = FONT;
Chart.defaults.color = '#888';
const PAL = {{ p1:'#FF5500', p2:'#1A1A1A', p3:'#8C8C8C', p4:'#BDBDBD', p5:'#123B63' }};
const SEAS_COLORS = ['#FF5500','#123B63','#8C8C8C','#BDBDBD','#D8D8D8','#EDEDED'];
const MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const QUARTERS = ['Q1','Q2','Q3','Q4'];
const FIELDS = ['soja_sc','farelo_t','farelo_usdkg','ptax','bio_m3','rec_farelo','rec_bio','custo','spread'];
let region='MT', period='all', freq='M', compView='chart', blendW=50, charts={{}};

function blendSeries(){{
  const w=blendW/100, byD={{}};
  for(const q of RAW.series.RS) byD[q.d]=q;
  const out=[];
  for(const p of RAW.series.MT){{
    const q=byD[p.d]; if(!q) continue;
    const o={{d:p.d}};
    for(const f of FIELDS) o[f]=(p[f]==null||q[f]==null)?null:p[f]*w+q[f]*(1-w);
    out.push(o);
  }}
  return out;
}}
function rawSeries(){{ return region==='BLEND'?blendSeries():RAW.series[region]; }}
function filterData(s){{
  if(period==='all')return s;
  const c=new Date();
  if(period==='1y')c.setFullYear(c.getFullYear()-1);
  if(period==='6m')c.setMonth(c.getMonth()-6);
  return s.filter(p=>p.d>=c.toISOString().slice(0,10));
}}
function periodKey(d){{
  return freq==='M' ? d.slice(0,7) : d.slice(0,4)+'-Q'+(Math.floor((+d.slice(5,7)-1)/3)+1);
}}
function periodLabel(k){{ return freq==='M'?k:(k.slice(6,7)+'Q'+k.slice(2,4)); }}
function aggregate(s){{
  const g={{}}, order=[];
  for(const p of s){{
    const k=periodKey(p.d);
    if(!g[k]){{ g[k]={{}}; order.push(k); }}
    for(const f of FIELDS){{ const v=p[f]; if(v==null)continue; (g[k][f]=g[k][f]||[]).push(v); }}
  }}
  return order.map(k=>{{
    const o={{k:k, label:periodLabel(k)}};
    for(const f of FIELDS){{ const a=g[k][f]; o[f]=(a&&a.length)?a.reduce((x,y)=>x+y,0)/a.length:null; }}
    return o;
  }});
}}
function buildSeas(agg,field){{
  const n=freq==='M'?12:4, byY={{}};
  for(const p of agg){{
    if(p[field]==null)continue;
    const y=+p.k.slice(0,4);
    const i=freq==='M'?(+p.k.slice(5,7)-1):(+p.k.slice(6,7)-1);
    if(!byY[y])byY[y]={{}};
    byY[y][i]=p[field];
  }}
  const years=Object.keys(byY).map(Number).sort();
  const avg=y=>Array.from({{length:n}},(_,i)=>byY[y]?.[i]??null);
  const complete=years.filter(y=>y<new Date().getFullYear());
  const hist=Array.from({{length:n}},(_,i)=>{{
    const vs=complete.map(y=>avg(y)[i]).filter(v=>v!=null);
    return vs.length?vs.reduce((a,b)=>a+b)/vs.length:null;
  }});
  return {{years,avg,hist,n}};
}}
function seasLabels(){{ return freq==='M'?MONTHS:QUARTERS; }}
function seasDatasets(seas){{
  const desc=[...seas.years].sort((a,b)=>b-a);
  const ds=seas.years.map(y=>({{
    label:String(y),data:seas.avg(y),
    borderColor:SEAS_COLORS[desc.indexOf(y)]||'#EDEDED',backgroundColor:'transparent',
    borderWidth:1.5,pointRadius:2,pointHoverRadius:4,tension:.35,
  }}));
  ds.push({{
    label:'Avg '+Math.min(...seas.years)+'–'+(new Date().getFullYear()-1),
    data:seas.hist,borderColor:PAL.p2,backgroundColor:'transparent',
    borderWidth:2.5,pointRadius:3,pointHoverRadius:5,tension:.35,
  }});
  return ds;
}}
const tickFmt=v=>Number(v).toLocaleString('pt-BR',{{minimumFractionDigits:0,maximumFractionDigits:0}});
function baseOpts(yFmt){{
  return {{
    responsive:true,maintainAspectRatio:false,
    interaction:{{mode:'index',intersect:false}},
    plugins:{{
      legend:{{labels:{{color:'#888',font:{{family:FONT,size:11}},boxWidth:10,padding:10}}}},
      tooltip:{{backgroundColor:'#fff',borderColor:'#e5e5e5',borderWidth:1,
        titleColor:'#1A1A1A',bodyColor:'#888',
        titleFont:{{family:FONT,size:11,weight:'700'}},
        bodyFont:{{family:FONT,size:11}},padding:10,
        callbacks:{{label:ctx=>ctx.parsed.y==null?'':' '+yFmt(ctx.parsed.y)}}}}
    }},
    scales:{{
      x:{{grid:{{color:'rgba(0,0,0,.04)'}},ticks:{{color:'#aaa',font:{{family:FONT,size:10}},maxTicksLimit:12,maxRotation:0}}}},
      y:{{grid:{{color:'rgba(0,0,0,.04)'}},ticks:{{color:'#aaa',font:{{family:FONT,size:10}},callback:tickFmt}}}}
    }}
  }};
}}
function seasOpts(yFmt){{
  const o=baseOpts(yFmt);
  const L=seasLabels();
  o.scales.x.ticks={{...o.scales.x.ticks,callback:(_,i)=>L[i]}};
  return o;
}}
function mk(id,labels,datasets,opts){{
  if(charts[id])charts[id].destroy();
  charts[id]=new Chart(document.getElementById(id),{{type:'line',data:{{labels,datasets}},options:opts}});
}}
function setRegion(r,btn){{
  region=r;
  document.querySelectorAll('#seg-region .seg-btn').forEach(b=>b.classList.remove('on'));btn.classList.add('on');
  document.getElementById('blend-group').style.display=(r==='BLEND')?'':'none';
  render();
}}
function setFreq(f,btn){{freq=f;document.querySelectorAll('#seg-freq .seg-btn').forEach(b=>b.classList.remove('on'));btn.classList.add('on');render();}}
function setPeriod(p,btn){{period=p;document.querySelectorAll('#seg-period .seg-btn').forEach(b=>b.classList.remove('on'));btn.classList.add('on');render();}}
function setBlend(v){{
  blendW=+v;
  const el=document.getElementById('blend-range');
  el.style.background='linear-gradient(90deg,'+PAL.p1+' 0%,'+PAL.p1+' '+blendW+'%,'+'#E5E5E5 '+blendW+'%,#E5E5E5 100%)';
  document.getElementById('blend-val').innerHTML='<span class="mt">MT '+blendW+'%</span> · <span class="rs">RS '+(100-blendW)+'%</span>';
  render();
}}
function setCompView(v,btn){{
  compView=v;
  document.querySelectorAll('.vt-btn').forEach(b=>b.classList.remove('on'));btn.classList.add('on');
  document.getElementById('comp-chart-view').style.display=v==='chart'?'':'none';
  document.getElementById('comp-table-view').style.display=v==='table'?'':'none';
  if(v==='table')renderPL();
}}
function renderPL(){{
  const agg=aggregate(filterData(rawSeries()));
  const cols=agg.slice(freq==='M'?-24:-16);
  const fmtN=v=>v==null?'<span class="na">&mdash;</span>':`<span class="${{v>=0?'pos':'neg'}}">${{Number(v).toLocaleString('pt-BR',{{minimumFractionDigits:0,maximumFractionDigits:0}})}}</span>`;
  const fmtV=v=>v==null?'<span class="na">&mdash;</span>':Number(v).toLocaleString('pt-BR',{{minimumFractionDigits:0,maximumFractionDigits:0}});
  const fmtR=v=>v==null?'<span class="na">&mdash;</span>':Number(v).toLocaleString('pt-BR',{{minimumFractionDigits:2,maximumFractionDigits:2}});
  const row=(cls,lbl,field,fmt)=>`<tr class="${{cls}}"><td class="lbl">${{lbl}}</td>`+cols.map(c=>`<td>${{fmt(c[field])}}</td>`).join('')+'</tr>';
  const calc=(cls,lbl,fn,fmt)=>`<tr class="${{cls}}"><td class="lbl">${{lbl}}</td>`+cols.map(c=>`<td>${{fmt(fn(c))}}</td>`).join('')+'</tr>';
  const thead='<thead><tr><th class="lbl">Item</th>'+cols.map(c=>`<th>${{c.label}}</th>`).join('')+'</tr></thead>';
  const tbody=`<tbody>
    <tr class="group-hdr"><td class="lbl" colspan="${{cols.length+1}}">REVENUE</td></tr>
    ${{row('sub','Meal Revenue (BRL/ton)','rec_farelo',fmtV)}}
    ${{row('sub','Biodiesel Revenue (BRL/ton)','rec_bio',fmtV)}}
    ${{calc('total','Total Revenue (BRL/ton)',c=>(c.rec_farelo==null||c.rec_bio==null)?null:c.rec_farelo+c.rec_bio,fmtV)}}
    <tr class="group-hdr"><td class="lbl" colspan="${{cols.length+1}}">COST</td></tr>
    ${{row('sub','Soy Cost (BRL/ton)','custo',fmtV)}}
    <tr class="group-hdr"><td class="lbl" colspan="${{cols.length+1}}">INPUTS</td></tr>
    ${{row('sub','Soy Farm Gate (BRL/sc60)','soja_sc',fmtV)}}
    ${{row('sub','Meal FOB (BRL/ton)','farelo_t',fmtV)}}
    ${{row('sub','Biodiesel B100 (BRL/m³)','bio_m3',fmtV)}}
    ${{row('sub','PTAX (BRL/USD)','ptax',fmtR)}}
    <tr class="group-hdr"><td class="lbl" colspan="${{cols.length+1}}">RESULT</td></tr>
    ${{row('total spread','Crushing Spread (BRL/ton)','spread',fmtN)}}
  </tbody>`;
  document.getElementById('pl-table').innerHTML=thead+tbody;
}}
function render(){{
  const raw=rawSeries();
  const agg=aggregate(filterData(raw));
  const aggAll=aggregate(raw);
  const lbl=agg.map(p=>p.label);
  const fmtK=v=>'BRL '+Number(v).toLocaleString('pt-BR',{{minimumFractionDigits:0,maximumFractionDigits:0}});
  const d=new Date(RAW.last_date+'T12:00:00');
  const per=freq==='M'?'month':'quarter';
  document.getElementById('updated-label').textContent='Updated '+d.toLocaleDateString('en-GB',{{day:'2-digit',month:'short',year:'numeric'}});
  document.getElementById('meal-sub').textContent='SECEX NCM 23040090 · '+CFG[region].port+' port';
  document.getElementById('bio-sub').textContent='ANP weighted avg · '+CFG[region].bio+' region';
  const seasSub=u=>u+' · avg by '+per+' · each line = one year · bold = historical avg';
  document.getElementById('seas-sub-spread').textContent=seasSub('BRL/ton');
  document.getElementById('seas-sub-bio').textContent=seasSub('BRL/m³');
  document.getElementById('seas-sub-meal').textContent=seasSub('BRL/ton');
  document.getElementById('seas-sub-soja').textContent=seasSub('BRL/sc 60 kg');
  mk('chartComp',lbl,[
    {{label:'Total Revenue',data:agg.map(p=>(p.rec_farelo==null||p.rec_bio==null)?null:p.rec_farelo+p.rec_bio),borderColor:PAL.p1,borderWidth:2,pointRadius:0,tension:.3,backgroundColor:'transparent'}},
    {{label:'Meal Revenue', data:agg.map(p=>p.rec_farelo),borderColor:PAL.p2,borderWidth:1.5,pointRadius:0,tension:.3,borderDash:[5,3],backgroundColor:'transparent'}},
    {{label:'Bio Revenue',  data:agg.map(p=>p.rec_bio),borderColor:PAL.p3,borderWidth:1.5,pointRadius:0,tension:.3,borderDash:[3,3],backgroundColor:'transparent'}},
    {{label:'Soy Cost',     data:agg.map(p=>p.custo),borderColor:PAL.p4,borderWidth:2,pointRadius:0,tension:.3,backgroundColor:'transparent'}},
  ],baseOpts(fmtK));
  if(compView==='table')renderPL();
  mk('chartSpread',lbl,[{{label:'Spread (BRL/ton)',data:agg.map(p=>p.spread),borderColor:PAL.p1,backgroundColor:PAL.p1+'18',borderWidth:2,pointRadius:0,pointHoverRadius:3,fill:true,tension:.35}}],baseOpts(fmtK));
  mk('chartSpreadSeas',seasLabels(),seasDatasets(buildSeas(aggAll,'spread')),seasOpts(fmtK));
  mk('chartBio',lbl,[{{label:'Biodiesel B100 (BRL/m³)',data:agg.map(p=>p.bio_m3),borderColor:PAL.p1,backgroundColor:PAL.p1+'18',borderWidth:1.5,pointRadius:0,fill:true,tension:.3}}],baseOpts(fmtK));
  mk('chartBioSeas',seasLabels(),seasDatasets(buildSeas(aggAll,'bio_m3')),seasOpts(fmtK));
  mk('chartMeal',lbl,[{{label:'Meal FOB (BRL/ton)',data:agg.map(p=>p.farelo_t),borderColor:PAL.p1,backgroundColor:'transparent',borderWidth:1.5,pointRadius:0,tension:.3}}],baseOpts(fmtK));
  mk('chartMealSeas',seasLabels(),seasDatasets(buildSeas(aggAll,'farelo_t')),seasOpts(fmtK));
  mk('chartSoja',lbl,[{{label:'Soy (BRL/sc60)',data:agg.map(p=>p.soja_sc),borderColor:PAL.p1,backgroundColor:'transparent',borderWidth:1.5,pointRadius:0,tension:.3}}],baseOpts(fmtK));
  mk('chartSojaSeas',seasLabels(),seasDatasets(buildSeas(aggAll,'soja_sc')),seasOpts(fmtK));
}}
const RAW = {data_json};
render();
</script>
</body>
</html>"""

    out_path = DB_DIR / "crushing_spread.html"
    out_path.write_text(html, encoding="utf-8")
    log.info(f"[Dashboard] Escrito: {out_path} ({len(html):,} chars)")


# ─────────────────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────────────────

def summary(conn):
    log.info("=" * 60)
    log.info("RESUMO DO BANCO")

    for uf in ["RS", "MT"]:
        r = conn.execute(
            "SELECT COUNT(*), MIN(data_referencia), MAX(data_referencia), "
            "ROUND(AVG(preco_brl_sc60),2) FROM soja_conab WHERE uf=?", (uf,)
        ).fetchone()
        log.info(
            f"  soja_conab [{uf}]: {r[0]:5,} | {r[1]} → {r[2]} | avg R${r[3]}/sc"
        )

    for porto in ["Santos", "Rio Grande"]:
        r = conn.execute(
            "SELECT COUNT(*), MIN(ano), MAX(ano), ROUND(AVG(preco_usd_kg),4) "
            "FROM farelo_secex WHERE porto=?", (porto,)
        ).fetchone()
        log.info(
            f"  farelo_secex [{porto:10}]: {r[0]:5,} meses | "
            f"{r[1]}→{r[2]} | avg US${r[3]}/kg"
        )

    r = conn.execute(
        "SELECT COUNT(*), MIN(data_referencia), MAX(data_referencia) FROM fx_ptax"
    ).fetchone()
    log.info(f"  fx_ptax: {r[0]:5,} | {r[1]} → {r[2]}")

    for reg in ["SUL", "CENTRO-OESTE"]:
        r = conn.execute(
            "SELECT COUNT(*), MIN(data_inicial), MAX(data_inicial), "
            "ROUND(AVG(preco_brl_m3),2) FROM biodiesel_anp WHERE regiao=?", (reg,)
        ).fetchone()
        log.info(
            f"  biodiesel [{reg:12}]: {r[0]:5,} semanas | "
            f"{r[1]} → {r[2]} | avg R${r[3]}/m³"
        )

    log.info("-" * 60)
    log.info("ULTIMOS SPREADS:")
    for row in conn.execute(
        "SELECT regiao, data_referencia, preco_soja_sc60, "
        "preco_farelo_ton, preco_bio_m3, spread_brl_ton "
        "FROM crushing_spread "
        "WHERE data_referencia = (SELECT MAX(data_referencia) FROM crushing_spread) "
        "ORDER BY regiao"
    ).fetchall():
        log.info(
            f"  {row['regiao']} ({row['data_referencia']}): "
            f"soja={row['preco_soja_sc60']:.2f} R$/sc | "
            f"farelo={row['preco_farelo_ton']:.2f} R$/t | "
            f"bio={row['preco_bio_m3']:.2f} R$/m³ | "
            f"SPREAD = {row['spread_brl_ton']:.2f} R$/t"
        )
    log.info("=" * 60)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    global FORCE_ALL
    FORCE_ALL = "--force-all" in sys.argv

    log.info("=" * 60)
    log.info(f"Crushing Spread Extractor | {TODAY} ({TODAY.strftime('%A')}) | {NOW_STR}")
    log.info(f"  Force-all: {FORCE_ALL}")
    log.info("=" * 60)

    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_conn()
    ensure_schema(conn)

    errors = []
    for label, fn in [
        ("Soja/CONAB",    lambda: run_soja(conn)),
        ("PTAX/BCB",      lambda: run_ptax(conn)),
        ("Farelo/SECEX",  lambda: run_farelo(conn)),
        ("Biodiesel/ANP", lambda: run_biodiesel(conn)),
        ("Spread",        lambda: run_spread(conn)),
        ("Dashboard",     lambda: generate_dashboard(conn)),
    ]:
        try:
            fn()
        except Exception as e:
            log.error(f"[{label}] FALHOU: {e}")
            errors.append(f"{label}: {e}")

    summary(conn)
    conn.close()

    if errors:
        log.error(f"FINALIZADO COM {len(errors)} ERRO(S):")
        for e in errors:
            log.error(f"  * {e}")
        sys.exit(1)
    else:
        log.info("Todas as secoes concluidas com sucesso.")


if __name__ == "__main__":
    main()
