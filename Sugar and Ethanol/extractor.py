#!/usr/bin/env python3
"""
extractor.py — Agri Monitor · Unified Daily Extractor
===========================================================
Runs daily via GitHub Actions. Each section has its own schedule logic:

  S&E (Sugar NY11, Ethanol UDOP, FX PTAX) → every weekday
  Fuel Parity (ANP weekly prices)           → Thursdays only
  Supply/Demand (ANP monthly volumes)       → 5th of each month only

If it's not the right day for a section, it skips silently (no error).
If it IS the right day and the fetch fails, it raises so GitHub marks the run red.

Sources:
  NY11   → Yahoo Finance (SB=F)
  Etanol → UDOP (udop.com.br) via undetected-chromedriver + Xvfb
  FX     → BCB PTAX API (olinda.bcb.gov.br)
  Fuel   → ANP Série Histórica de Preços (semanal, xlsx)
  Vendas → ANP dados abertos (vendas-etanol-hidratado-m3-{Y}.csv, vendas-gasolina-c-m3-{Y}.csv)
  Produção → ANP dados abertos (producao-etanol-hidratado-m3.csv)
"""

import io
import logging
import sqlite3
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

# ── Chrome / Selenium (only imported when needed) ──────────────────────────
try:
    import undetected_chromedriver as uc
    from selenium.webdriver.common.by import By
    HAS_CHROME = True
except ImportError:
    HAS_CHROME = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

DB_PATH       = Path(__file__).parent / "commodities.db"
HISTORY_START = "2010-01-01"
TODAY         = date.today()
NOW_STR       = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

FORCE_ALL = False  # overridden in main() if --force-all passed

# ─────────────────────────────────────────────────────────────────────────────
# Schedule helpers — silent skip if not the right day
# ─────────────────────────────────────────────────────────────────────────────

def is_weekday()  -> bool: return TODAY.weekday() < 5           # Mon–Fri
def is_thursday() -> bool: return FORCE_ALL or TODAY.weekday() == 3

# ── Supply/Demand windows ───────────────────────────────────────────────────
# ANP "vendas" (sales) is published on the last business day of month M+1 for
# month M's data — confirmed empirically (Mar->30/04, Apr->29/05, May->30/06,
# Jun->31/07, all landing on the last weekday on/before the calendar month-end).
# Window = last days of the month + first few days of the next month as a
# safety net in case publication slips.
def is_vendas_window() -> bool:
    return FORCE_ALL or TODAY.day >= 28 or TODAY.day <= 3

# ANP "produção" (biofuel production) has no confirmed fixed publish day —
# observed fills ranged from the 16th to the 24th of the month. Check weekly
# (Fridays) across that broader window until a tighter pattern is confirmed.
def is_producao_window() -> bool:
    return FORCE_ALL or (12 <= TODAY.day <= 28 and TODAY.weekday() == 4)


# ─────────────────────────────────────────────────────────────────────────────
# DB helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS sugar_ny11 (
        id INTEGER PRIMARY KEY AUTOINCREMENT, data_referencia TEXT NOT NULL,
        ano INTEGER, mes INTEGER, preco_usdclb REAL NOT NULL,
        open_usdclb REAL, high_usdclb REAL, low_usdclb REAL, volume REAL,
        fonte TEXT DEFAULT 'Yahoo/SB=F', updated_at TEXT, UNIQUE(data_referencia));
    CREATE INDEX IF NOT EXISTS idx_sugar ON sugar_ny11(data_referencia);

    CREATE TABLE IF NOT EXISTS etanol_cepea (
        id INTEGER PRIMARY KEY AUTOINCREMENT, data_referencia TEXT NOT NULL,
        ano INTEGER, mes INTEGER, preco_brl_m3 REAL NOT NULL,
        fonte TEXT DEFAULT 'UDOP/CEPEA-Paulinia', updated_at TEXT,
        UNIQUE(data_referencia));
    CREATE INDEX IF NOT EXISTS idx_etanol ON etanol_cepea(data_referencia);

    CREATE TABLE IF NOT EXISTS fx_usdbrl (
        id INTEGER PRIMARY KEY AUTOINCREMENT, data_referencia TEXT NOT NULL,
        ano INTEGER, mes INTEGER, ptax_venda REAL NOT NULL,
        fonte TEXT DEFAULT 'BCB/PTAX', updated_at TEXT,
        UNIQUE(data_referencia));
    CREATE INDEX IF NOT EXISTS idx_fx ON fx_usdbrl(data_referencia);

    CREATE TABLE IF NOT EXISTS anp_estados (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        data_inicial TEXT NOT NULL, data_final TEXT NOT NULL,
        regiao TEXT, estado TEXT NOT NULL, produto TEXT NOT NULL,
        preco_medio_revenda REAL, updated_at TEXT,
        UNIQUE(data_inicial, estado, produto));
    CREATE INDEX IF NOT EXISTS idx_anp_est ON anp_estados(data_inicial, estado, produto);

    CREATE TABLE IF NOT EXISTS anp_brasil (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        data_inicial TEXT NOT NULL, data_final TEXT NOT NULL,
        produto TEXT NOT NULL, preco_medio_revenda REAL, updated_at TEXT,
        UNIQUE(data_inicial, produto));
    CREATE INDEX IF NOT EXISTS idx_anp_br ON anp_brasil(data_inicial, produto);

    CREATE TABLE IF NOT EXISTS anp_vendas_uf (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ano INTEGER NOT NULL, mes INTEGER NOT NULL, estado TEXT NOT NULL,
        eth_hid_m3 REAL, gas_c_m3 REAL, updated_at TEXT,
        UNIQUE(ano, mes, estado));
    CREATE INDEX IF NOT EXISTS idx_vendas ON anp_vendas_uf(ano, mes, estado);

    CREATE TABLE IF NOT EXISTS anp_producao_uf (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ano INTEGER NOT NULL, mes INTEGER NOT NULL, estado TEXT NOT NULL,
        eth_hid_m3 REAL, eth_ani_m3 REAL, updated_at TEXT,
        UNIQUE(ano, mes, estado));
    CREATE INDEX IF NOT EXISTS idx_prod ON anp_producao_uf(ano, mes, estado);
    """)
    conn.commit()


def last_date(conn, table, col="data_referencia"):
    r = conn.execute(f"SELECT MAX({col}) FROM {table}").fetchone()
    return r[0] if r and r[0] else None


def last_year_month(conn, table):
    r = conn.execute(
        f"SELECT MAX(ano), MAX(mes) FROM {table} "
        f"WHERE ano=(SELECT MAX(ano) FROM {table})"
    ).fetchone()
    return (int(r[0]), int(r[1])) if r and r[0] else None


def safe_float(val):
    try:
        f = float(val)
        return None if str(f) == "nan" else f
    except:
        return None


def parse_date(raw):
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(raw).strip(), fmt).strftime("%Y-%m-%d")
        except:
            continue
    return None


# ─────────────────────────────────────────────────────────────────────────────
# HTTP helpers
# ─────────────────────────────────────────────────────────────────────────────

ANP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/csv,application/vnd.ms-excel,*/*",
    "Referer": "https://www.gov.br/anp/pt-br/",
}

def download(url: str, label: str, fatal: bool = True) -> bytes | None:
    for attempt in range(1, 4):
        try:
            log.info(f"[{label}] Downloading (attempt {attempt}): {url}")
            r = requests.get(url, headers=ANP_HEADERS, timeout=60)
            r.raise_for_status()
            log.info(f"[{label}] {len(r.content):,} bytes")
            return r.content
        except requests.RequestException as e:
            log.warning(f"[{label}] Attempt {attempt} failed: {e}")
            if attempt < 3:
                time.sleep(10 * attempt)
    msg = f"[{label}] All download attempts failed."
    if fatal:
        raise RuntimeError(msg)   # marks GitHub run red
    log.error(msg)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# ══ SECTION 1: S&E  (runs every weekday) ════════════════════════════════════
# ─────────────────────────────────────────────────────────────────────────────

def run_se(conn: sqlite3.Connection) -> dict:
    if not is_weekday():
        log.info("[S&E] Not a weekday — skipping.")
        return {"skipped": True}

    log.info("=" * 60)
    log.info("S&E — Sugar NY11 · Ethanol UDOP · FX PTAX")
    log.info("=" * 60)

    results = {}
    results["ny11"] = fetch_sugar_ny11(conn)
    results["fx"]   = fetch_fx_usdbrl(conn)
    results["eth"]  = fetch_etanol_cepea(conn)   # Chrome — last, heaviest
    return results


# ── NY11 ──────────────────────────────────────────────────────────────────────

def fetch_sugar_ny11(conn) -> int:
    try:
        import yfinance as yf
    except ImportError:
        raise RuntimeError("[NY11] yfinance not installed")

    log.info("[NY11] Fetching Yahoo Finance (SB=F)...")
    ld = last_date(conn, "sugar_ny11")
    start = (datetime.strptime(ld, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d") \
            if ld else HISTORY_START
    if start > TODAY.strftime("%Y-%m-%d"):
        log.info("[NY11] Already up to date.")
        return 0

    # yfinance's `end` is exclusive of the date itself, so end=TODAY always
    # skips today's own close — even after market close, even hours after
    # the run started. Using TODAY+1 lets today's close (if already settled
    # by run time) be captured the same evening instead of the next run.
    end = (TODAY + timedelta(days=1)).strftime("%Y-%m-%d")
    df = yf.Ticker("SB=F").history(start=start, end=end, auto_adjust=False)
    if df is None or df.empty:
        log.info("[NY11] No new data.")
        return 0

    df.index = pd.to_datetime(df.index).tz_localize(None)
    inserted = 0
    for ts, row in df.iterrows():
        dr = ts.strftime("%Y-%m-%d")
        cl = safe_float(row.get("Close"))
        if not cl:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO sugar_ny11 "
            "(data_referencia,ano,mes,preco_usdclb,open_usdclb,high_usdclb,low_usdclb,volume,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (dr, int(dr[:4]), int(dr[5:7]), cl,
             safe_float(row.get("Open")), safe_float(row.get("High")),
             safe_float(row.get("Low")),  safe_float(row.get("Volume")), NOW_STR))
        if conn.execute("SELECT changes()").fetchone()[0]:
            inserted += 1
    conn.commit()
    log.info(f"[NY11] {inserted} rows inserted.")
    return inserted


# ── FX PTAX ───────────────────────────────────────────────────────────────────

BCB_URL = (
    "https://olinda.bcb.gov.br/olinda/servico/PTAX/versao/v1/odata/"
    "CotacaoDolarPeriodo(dataInicial=@dataInicial,dataFinalCotacao=@dataFinalCotacao)"
    "?@dataInicial='{di}'&@dataFinalCotacao='{df}'"
    "&$top=1000&$skip={skip}&$orderby=dataHoraCotacao%20asc"
    "&$format=json&$select=cotacaoVenda,dataHoraCotacao"
)

def fetch_fx_usdbrl(conn) -> int:
    log.info("[FX] Fetching BCB PTAX...")
    ld = last_date(conn, "fx_usdbrl")
    start = (datetime.strptime(ld, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d") \
            if ld else HISTORY_START
    if start > TODAY.strftime("%Y-%m-%d"):
        log.info("[FX] Already up to date.")
        return 0

    di = datetime.strptime(start, "%Y-%m-%d").strftime("%m-%d-%Y")
    df = TODAY.strftime("%m-%d-%Y")
    inserted = 0
    skip = 0
    while True:
        url = BCB_URL.format(di=di, df=df, skip=skip)
        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            data = r.json().get("value", [])
        except Exception as e:
            raise RuntimeError(f"[FX] BCB API failed at skip={skip}: {e}")

        if not data:
            break
        for item in data:
            raw_dt = item.get("dataHoraCotacao", "")[:10]
            ptax   = item.get("cotacaoVenda")
            if not raw_dt or ptax is None:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO fx_usdbrl "
                "(data_referencia,ano,mes,ptax_venda,updated_at) VALUES(?,?,?,?,?)",
                (raw_dt, int(raw_dt[:4]), int(raw_dt[5:7]), float(ptax), NOW_STR))
            if conn.execute("SELECT changes()").fetchone()[0]:
                inserted += 1
        log.info(f"[FX] skip={skip}: {len(data)} records")
        if len(data) < 1000:
            break
        skip += 1000
        time.sleep(0.3)

    conn.commit()
    log.info(f"[FX] {inserted} rows inserted.")
    return inserted


# ── Ethanol UDOP ──────────────────────────────────────────────────────────────

UDOP_URL = "https://www.udop.com.br/indicadores-etanol"

def make_driver():
    if not HAS_CHROME:
        raise RuntimeError("[ETANOL] undetected-chromedriver not installed")
    chrome = subprocess.run(["which", "google-chrome"], capture_output=True, text=True).stdout.strip()
    ver    = subprocess.run([chrome, "--version"], capture_output=True, text=True).stdout.strip()
    major  = int(ver.split()[-1].split(".")[0])
    log.info(f"[ETANOL] Chrome {ver} (major={major})")
    opts = uc.ChromeOptions()
    opts.binary_location = chrome
    for arg in ["--no-sandbox","--disable-dev-shm-usage","--disable-gpu",
                "--window-size=1280,900","--lang=pt-BR"]:
        opts.add_argument(arg)
    return uc.Chrome(options=opts, version_main=major)

def fetch_etanol_cepea(conn) -> int:
    ld = last_date(conn, "etanol_cepea")
    log.info(f"[ETANOL] Last in DB: {ld or 'none'}")
    driver, rows = None, []
    try:
        driver = make_driver()
        log.info(f"[ETANOL] Navigating to {UDOP_URL}")
        driver.get(UDOP_URL)
        time.sleep(8)
        try:
            driver.find_element(By.XPATH,
                "//button[contains(text(),'Diário') or contains(text(),'Di')]").click()
            time.sleep(2)
        except: pass
        try:
            driver.find_element(By.XPATH,
                "//button[contains(text(),'São Paulo')]").click()
            time.sleep(2)
        except: pass

        table = driver.find_element(By.CSS_SELECTOR, "table")
        for linha in table.find_elements(By.TAG_NAME, "tr"):
            cels = [c.text.strip() for c in linha.find_elements(By.TAG_NAME, "td")]
            if len(cels) < 2:
                continue
            dr = parse_date(cels[0])
            if not dr:
                continue
            try:
                val = float(cels[1].replace(".", "").replace(",", "."))
                if val > 0:
                    rows.append({"data_ref": dr, "preco_m3": val})
            except: continue

        log.info(f"[ETANOL] {len(rows)} rows read | "
                 f"{rows[-1]['data_ref'] if rows else '—'} → {rows[0]['data_ref'] if rows else '—'}")
    except Exception as e:
        raise RuntimeError(f"[ETANOL] Scraping failed: {e}")
    finally:
        if driver:
            try: driver.quit()
            except: pass

    if not rows:
        raise RuntimeError("[ETANOL] No data obtained from UDOP")

    if ld:
        rows = [r for r in rows if r["data_ref"] > ld]
    if not rows:
        log.info("[ETANOL] Nothing new.")
        return 0

    inserted = 0
    for r in rows:
        conn.execute(
            "INSERT OR IGNORE INTO etanol_cepea "
            "(data_referencia,ano,mes,preco_brl_m3,updated_at) VALUES(?,?,?,?,?)",
            (r["data_ref"], int(r["data_ref"][:4]), int(r["data_ref"][5:7]),
             r["preco_m3"], NOW_STR))
        if conn.execute("SELECT changes()").fetchone()[0]:
            inserted += 1
    conn.commit()
    log.info(f"[ETANOL] {inserted} rows inserted.")
    return inserted


# ─────────────────────────────────────────────────────────────────────────────
# ══ SECTION 2: Fuel Parity  (runs Thursdays only) ═══════════════════════════
# ─────────────────────────────────────────────────────────────────────────────

ANP_BASE     = "https://www.gov.br/anp/pt-br/centrais-de-conteudo/dados-abertos/arquivos"
FUEL_EST_URL = "https://www.gov.br/anp/pt-br/assuntos/precos-e-defesa-da-concorrencia/precos/precos-revenda-e-de-distribuicao-combustiveis/shlp/semanal/semanal-estados-desde-2013.xlsx"
FUEL_BR_URL  = "https://www.gov.br/anp/pt-br/assuntos/precos-e-defesa-da-concorrencia/precos/precos-revenda-e-de-distribuicao-combustiveis/shlp/semanal/semanal-brasil-desde-2013.xlsx"
PRODUTOS     = {"ETANOL HIDRATADO", "GASOLINA COMUM"}

def run_fuel(conn: sqlite3.Connection) -> dict:
    if not is_weekday():
        log.info("[Fuel] Not a weekday — skipping.")
        return {"skipped": True}

    log.info("=" * 60)
    log.info("Fuel Parity — ANP weekly prices (Etanol + Gasolina)")
    log.info("=" * 60)

    return {
        "estados": ingest_fuel_estados(conn),
        "brasil":  ingest_fuel_brasil(conn),
    }


def parse_anp_fuel_excel(content: bytes, label: str) -> pd.DataFrame | None:
    try:
        raw = pd.read_excel(io.BytesIO(content), sheet_name=0, header=None)
        header_row = next(
            (i for i, row in raw.iterrows() if "DATA INICIAL" in str(row.values)),
            None
        )
        if header_row is None:
            raise ValueError("'DATA INICIAL' header not found")
        df = pd.read_excel(io.BytesIO(content), sheet_name=0, header=header_row)
        df = df.dropna(subset=["DATA INICIAL"])
        df = df[df["PRODUTO"].isin(PRODUTOS)]
        df["DATA INICIAL"] = pd.to_datetime(df["DATA INICIAL"]).dt.strftime("%Y-%m-%d")
        df["DATA FINAL"]   = pd.to_datetime(df["DATA FINAL"]).dt.strftime("%Y-%m-%d")
        df["PREÇO MÉDIO REVENDA"] = pd.to_numeric(df["PREÇO MÉDIO REVENDA"], errors="coerce")
        log.info(f"[{label}] Parsed {len(df)} rows | "
                 f"{df['DATA INICIAL'].min()} → {df['DATA INICIAL'].max()}")
        return df
    except Exception as e:
        raise RuntimeError(f"[{label}] Excel parse failed: {e}")


def ingest_fuel_estados(conn) -> int:
    ld = last_date(conn, "anp_estados", "data_inicial")
    content = download(FUEL_EST_URL, "fuel-estados", fatal=True)
    df = parse_anp_fuel_excel(content, "fuel-estados")
    if ld:
        df = df[df["DATA INICIAL"] > ld]
    if df.empty:
        log.info("[fuel-estados] Nothing new.")
        return 0
    inserted = 0
    for _, r in df.iterrows():
        conn.execute(
            "INSERT OR IGNORE INTO anp_estados "
            "(data_inicial,data_final,regiao,estado,produto,preco_medio_revenda,updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (r["DATA INICIAL"], r["DATA FINAL"],
             r.get("REGIÃO") or r.get("REGIAO"),
             r["ESTADO"], r["PRODUTO"],
             float(r["PREÇO MÉDIO REVENDA"]) if pd.notna(r["PREÇO MÉDIO REVENDA"]) else None,
             NOW_STR))
        if conn.execute("SELECT changes()").fetchone()[0]:
            inserted += 1
    conn.commit()
    log.info(f"[fuel-estados] {inserted} rows inserted.")
    return inserted


def ingest_fuel_brasil(conn) -> int:
    ld = last_date(conn, "anp_brasil", "data_inicial")
    content = download(FUEL_BR_URL, "fuel-brasil", fatal=True)
    df = parse_anp_fuel_excel(content, "fuel-brasil")
    if ld:
        df = df[df["DATA INICIAL"] > ld]
    if df.empty:
        log.info("[fuel-brasil] Nothing new.")
        return 0
    inserted = 0
    for _, r in df.iterrows():
        conn.execute(
            "INSERT OR IGNORE INTO anp_brasil "
            "(data_inicial,data_final,produto,preco_medio_revenda,updated_at) "
            "VALUES(?,?,?,?,?)",
            (r["DATA INICIAL"], r["DATA FINAL"], r["PRODUTO"],
             float(r["PREÇO MÉDIO REVENDA"]) if pd.notna(r["PREÇO MÉDIO REVENDA"]) else None,
             NOW_STR))
        if conn.execute("SELECT changes()").fetchone()[0]:
            inserted += 1
    conn.commit()
    log.info(f"[fuel-brasil] {inserted} rows inserted.")
    return inserted


# ─────────────────────────────────────────────────────────────────────────────
# ══ SECTION 3: Supply/Demand  (runs on 5th of each month) ═══════════════════
# ─────────────────────────────────────────────────────────────────────────────

VENDAS_CSV_URL = "https://www.gov.br/anp/pt-br/centrais-de-conteudo/dados-abertos/arquivos/vdpb/vendas-derivados-petroleo-e-etanol/vendas-combustiveis-m3-1990-2025.csv"
PRODUCAO_URL   = "https://www.gov.br/anp/pt-br/assuntos/producao-e-fornecimento-de-biocombustiveis/etanol/arquivos-etanol/pb-da-etanol.zip"

MES_PT = {
    "JAN":1,"FEV":2,"MAR":3,"ABR":4,"MAI":5,"JUN":6,
    "JUL":7,"AGO":8,"SET":9,"OUT":10,"NOV":11,"DEZ":12,
}
ESTADO_NORM = {
    "Acre":"ACRE","Alagoas":"ALAGOAS","Amapá":"AMAPÁ","Amazonas":"AMAZONAS",
    "Bahia":"BAHIA","Ceará":"CEARÁ","Distrito Federal":"DISTRITO FEDERAL",
    "Espírito Santo":"ESPÍRITO SANTO","Goiás":"GOIÁS","Maranhão":"MARANHÃO",
    "Mato Grosso":"MATO GROSSO","Mato Grosso do Sul":"MATO GROSSO DO SUL",
    "Minas Gerais":"MINAS GERAIS","Pará":"PARÁ","Paraíba":"PARAÍBA",
    "Paraná":"PARANÁ","Pernambuco":"PERNAMBUCO","Piauí":"PIAUÍ",
    "Rio de Janeiro":"RIO DE JANEIRO","Rio Grande do Norte":"RIO GRANDE DO NORTE",
    "Rio Grande do Sul":"RIO GRANDE DO SUL","Rondônia":"RONDÔNIA",
    "Roraima":"RORAIMA","Santa Catarina":"SANTA CATARINA",
    "São Paulo":"SÃO PAULO","Sergipe":"SERGIPE","Tocantins":"TOCANTINS",
}

def run_supply_demand(conn: sqlite3.Connection) -> dict:
    if not is_weekday():
        log.info("[Supply/Demand] Not a weekday — skipping.")
        return {"skipped": True}

    log.info("=" * 60)
    log.info("Supply/Demand — ANP monthly volumes (Vendas + Produção)")
    log.info("=" * 60)

    results = {}

    if is_vendas_window():
        results["vendas"] = ingest_vendas(conn)
    else:
        log.info("[vendas] Outside publication window (day 28-31 or 1-3) — skipping.")
        results["vendas"] = {"skipped": True}

    if is_producao_window():
        results["producao"] = ingest_producao(conn)
    else:
        log.info("[producao] Outside publication window (Fridays, day 12-28) — skipping.")
        results["producao"] = {"skipped": True}

    return results


def parse_vendas_year(content: bytes, year: int, label: str) -> pd.DataFrame | None:
    for enc in ("latin-1", "utf-8-sig", "utf-8"):
        try:
            text = content.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    df = pd.read_csv(io.StringIO(text), sep=";", on_bad_lines="skip")
    df.columns = [c.strip().upper() for c in df.columns]
    uf_col = next(
        (c for c in df.columns if any(k in c for k in ("FEDERAÇÃO","FEDERACAO","ESTADO"," UF"))),
        None
    )
    if not uf_col:
        raise RuntimeError(f"[{label}] UF column not found. Cols: {list(df.columns)}")
    df = df[df[uf_col].notna()]
    df = df[~df[uf_col].str.upper().str.contains(r"TOTAL|BRASIL|REGIÃO|REGIAO|GRANDE",
                                                    na=False, regex=True)]
    mes_cols = {col: MES_PT[col[:3].upper()] for col in df.columns if col[:3].upper() in MES_PT}
    if not mes_cols:
        raise RuntimeError(f"[{label}] No month columns found")
    rows = []
    for _, row in df.iterrows():
        uf = str(row[uf_col]).strip().upper()
        for col, mes_num in mes_cols.items():
            val = row.get(col)
            if pd.isna(val):
                continue
            try:
                v = float(str(val).replace(".", "").replace(",", "."))
                rows.append({"ano": year, "mes": mes_num, "estado": uf, "volume": v})
            except: continue
    return pd.DataFrame(rows) if rows else None


def ingest_vendas(conn) -> int:
    """
    Downloads the consolidated ANP vendas CSV (all years, all products)
    and inserts only rows newer than last in DB.
    Format: ANO;MÊS;GRANDE REGIÃO;UNIDADE DA FEDERAÇÃO;PRODUTO;VENDAS
    """
    last = last_year_month(conn, "anp_vendas_uf")
    last_ano = last[0] if last else 2013
    last_mes = last[1] if last else 0

    content = download(VENDAS_CSV_URL, "vendas", fatal=True)

    for enc in ("utf-8-sig", "latin-1", "utf-8"):
        try:
            text = content.decode(enc); break
        except UnicodeDecodeError: continue

    df = pd.read_csv(io.StringIO(text), sep=";", on_bad_lines="skip")
    df.columns = [c.strip() for c in df.columns]

    # Find columns
    ano_col    = next((c for c in df.columns if c.upper() in ("ANO","AÑO")), None)
    mes_col    = next((c for c in df.columns if "MÊS" in c.upper() or "MES" in c.upper()), None)
    uf_col     = next((c for c in df.columns if "FEDERAÇÃO" in c.upper() or "FEDERACAO" in c.upper()), None)
    prod_col   = next((c for c in df.columns if "PRODUTO" in c.upper()), None)
    vendas_col = next((c for c in df.columns if "VENDAS" in c.upper()), None)

    if not all([ano_col, mes_col, uf_col, prod_col, vendas_col]):
        raise RuntimeError(f"[vendas] Missing columns. Got: {list(df.columns)}")

    # Filter to only our products
    df = df[df[prod_col].isin(["ETANOL HIDRATADO", "GASOLINA C"])].copy()

    # Map month names to numbers
    df["mes_num"] = df[mes_col].str[:3].str.upper().map(MES_PT)
    df = df[df["mes_num"].notna()].copy()
    df["mes_num"] = df["mes_num"].astype(int)
    df["ano_num"] = pd.to_numeric(df[ano_col], errors="coerce").astype("Int64")
    df = df[df["ano_num"].notna()].copy()

    # Convert vendas values
    df["volume"] = pd.to_numeric(
        df[vendas_col].astype(str).str.replace(".", "").str.replace(",", "."),
        errors="coerce"
    )
    df["estado"] = df[uf_col].str.strip().str.upper()

    # Pivot eth + gas into same row
    piv = df.pivot_table(
        index=["ano_num", "mes_num", "estado"],
        columns=prod_col,
        values="volume",
        aggfunc="sum"
    ).reset_index()
    piv.columns.name = None
    piv = piv.rename(columns={
        "ano_num": "ano", "mes_num": "mes",
        "ETANOL HIDRATADO": "eth_hid_m3",
        "GASOLINA C": "gas_c_m3"
    })
    if "eth_hid_m3" not in piv.columns: piv["eth_hid_m3"] = None
    if "gas_c_m3"   not in piv.columns: piv["gas_c_m3"]   = None

    # Only new rows
    piv = piv[
        (piv["ano"] > last_ano) |
        ((piv["ano"] == last_ano) & (piv["mes"] > last_mes))
    ]

    if piv.empty:
        log.info("[vendas] Nothing new.")
        return 0

    log.info(f"[vendas] {len(piv)} new rows to insert | "
             f"up to {int(piv['ano'].max())}-{int(piv['mes'].max()):02d}")

    inserted = 0
    for _, r in piv.iterrows():
        conn.execute(
            "INSERT OR IGNORE INTO anp_vendas_uf "
            "(ano,mes,estado,eth_hid_m3,gas_c_m3,updated_at) VALUES(?,?,?,?,?,?)",
            (int(r.ano), int(r.mes), r.estado,
             float(r.eth_hid_m3) if pd.notna(r.get("eth_hid_m3")) else None,
             float(r.gas_c_m3)   if pd.notna(r.get("gas_c_m3"))   else None,
             NOW_STR))
        if conn.execute("SELECT changes()").fetchone()[0]:
            inserted += 1
    conn.commit()
    log.info(f"[vendas] {inserted} rows inserted.")
    return inserted


def ingest_producao(conn) -> int:
    import zipfile
    last = last_year_month(conn, "anp_producao_uf")
    last_ano = last[0] if last else 2016
    last_mes = last[1] if last else 0

    content = download(PRODUCAO_URL, "producao", fatal=True)

    # Extract Etanol_Produção.csv from zip
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            csv_name = next((n for n in zf.namelist()
                             if "rodu" in n.lower() and n.endswith(".csv")), None)
            if not csv_name:
                raise RuntimeError(f"[producao] Etanol_Produção.csv not found in zip. Files: {zf.namelist()}")
            log.info(f"[producao] Extracting: {csv_name}")
            raw = zf.read(csv_name)
    except zipfile.BadZipFile as e:
        raise RuntimeError(f"[producao] Bad zip file: {e}")

    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            text = raw.decode(enc); break
        except UnicodeDecodeError: continue

    df = pd.read_csv(io.StringIO(text), sep=",")
    df.columns = [c.strip() for c in df.columns]
    date_col = next((c for c in df.columns if "MÊS" in c.upper() or "MES" in c.upper()), None)
    hid_col  = next((c for c in df.columns if "HIDRATADO" in c.upper()), None)
    ani_col  = next((c for c in df.columns if "ANIDRO"   in c.upper()), None)
    est_col  = next((c for c in df.columns if "ESTADO"   in c.upper()), None)
    if not all([date_col, hid_col, est_col]):
        raise RuntimeError(f"[producao] Missing columns. Got: {list(df.columns)}")

    df["mes_ano"]    = pd.to_datetime(df[date_col], format="%m/%Y")
    df["ano"]        = df["mes_ano"].dt.year.astype(int)
    df["mes"]        = df["mes_ano"].dt.month.astype(int)
    df["estado"]     = df[est_col].str.strip().map(ESTADO_NORM).fillna(
                          df[est_col].str.strip().str.upper())
    df["eth_hid_m3"] = pd.to_numeric(df[hid_col], errors="coerce")
    df["eth_ani_m3"] = pd.to_numeric(df[ani_col], errors="coerce") if ani_col else None

    df = df[(df["ano"] > last_ano) | ((df["ano"] == last_ano) & (df["mes"] > last_mes))]
    if df.empty:
        log.info("[producao] Nothing new.")
        return 0

    inserted = 0
    for _, r in df.iterrows():
        conn.execute(
            "INSERT OR IGNORE INTO anp_producao_uf "
            "(ano,mes,estado,eth_hid_m3,eth_ani_m3,updated_at) VALUES(?,?,?,?,?,?)",
            (int(r.ano), int(r.mes), r.estado,
             float(r.eth_hid_m3) if pd.notna(r.eth_hid_m3) else None,
             float(r.eth_ani_m3) if pd.notna(r.eth_ani_m3) else None,
             NOW_STR))
        if conn.execute("SELECT changes()").fetchone()[0]:
            inserted += 1
    conn.commit()
    log.info(f"[producao] {inserted} rows inserted.")
    return inserted



# ─────────────────────────────────────────────────────────────────────────────
# ══ DASHBOARD GENERATION (runs after every scraper section) ═════════════════
# ─────────────────────────────────────────────────────────────────────────────

# HTML template stored as compressed base64 (before/after the data block)
_TMPL_BEFORE_B64 = "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"
_TMPL_AFTER_B64  = "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"

def generate_dashboard(conn: sqlite3.Connection) -> None:
    """Regenerate se_dashboard.html with latest data from DB."""
    import base64, gzip
    from collections import OrderedDict

    log.info("[Dashboard] Regenerating se_dashboard.html...")

    # ── Extract all data ──────────────────────────────────────────────────────
    ATR_VHP=1.05; ATR_HYD=1.68; FRETE=85.0; ELEVACAO=10.5; CONV_L_TON=1.04; CONV_TON_LB=22.0

    # Use sugar_ny11 as the spine (most complete), join ethanol/FX with tolerance
    # For missing ethanol: use last available price on or before that date
    # For missing FX: use last available rate on or before that date
    se_rows = conn.execute("""
        SELECT
            s.data_referencia,
            s.preco_usdclb,
            (SELECT e.preco_brl_m3 FROM etanol_cepea e
             WHERE e.data_referencia <= s.data_referencia
             ORDER BY e.data_referencia DESC LIMIT 1) AS preco_brl_m3,
            (SELECT f.ptax_venda FROM fx_usdbrl f
             WHERE f.data_referencia <= s.data_referencia
             ORDER BY f.data_referencia DESC LIMIT 1) AS ptax_venda
        FROM sugar_ny11 s
        ORDER BY s.data_referencia
    """).fetchall()
    se_data = []
    for dr, sugar, eth_m3, fx in se_rows:
        if not all([sugar, eth_m3, fx]): continue
        equiv = (((eth_m3*ATR_VHP/ATR_HYD)+FRETE+(ELEVACAO*fx))/CONV_L_TON/CONV_TON_LB)/fx
        se_data.append({"d":dr,"sugar":round(sugar,4),"eth":round(eth_m3,2),
                         "fx":round(fx,4),"equiv":round(equiv,2),"diff":round(equiv-sugar,2)})

    uf_series = {}
    for date, uf, parity in conn.execute("""
        SELECT e.data_inicial, e.estado, ROUND(e.preco_medio_revenda/g.preco_medio_revenda,4)
        FROM anp_estados e
        JOIN anp_estados g ON g.data_inicial=e.data_inicial AND g.estado=e.estado AND g.produto='GASOLINA COMUM'
        WHERE e.produto='ETANOL HIDRATADO' AND e.preco_medio_revenda IS NOT NULL AND g.preco_medio_revenda IS NOT NULL
        ORDER BY e.data_inicial
    """).fetchall():
        if uf not in uf_series: uf_series[uf] = []
        uf_series[uf].append({"d":date,"p":parity})

    br_series = [{"d":r[0],"p":r[1]} for r in conn.execute("""
        SELECT e.data_inicial, ROUND(e.preco_medio_revenda/g.preco_medio_revenda,4)
        FROM anp_brasil e
        JOIN anp_brasil g ON g.data_inicial=e.data_inicial AND g.produto='GASOLINA COMUM'
        WHERE e.produto='ETANOL HIDRATADO' AND e.preco_medio_revenda IS NOT NULL AND g.preco_medio_revenda IS NOT NULL
        ORDER BY e.data_inicial
    """).fetchall()]

    map_data = {}
    for date, uf, parity in conn.execute("""
        SELECT e.data_inicial, e.estado, ROUND(e.preco_medio_revenda/g.preco_medio_revenda,4)
        FROM anp_estados e
        JOIN anp_estados g ON g.data_inicial=e.data_inicial AND g.estado=e.estado AND g.produto='GASOLINA COMUM'
        WHERE e.produto='ETANOL HIDRATADO' AND e.preco_medio_revenda IS NOT NULL AND g.preco_medio_revenda IS NOT NULL
    """).fetchall():
        if date not in map_data: map_data[date] = {}
        map_data[date][uf] = parity

    map_dates = sorted(map_data.keys())
    month_map = OrderedDict()
    for dt in map_dates:
        month_map[dt[:7]] = dt
    MONTH_DATES  = list(month_map.values())
    MONTH_LABELS = list(month_map.keys())

    deficit_rows = conn.execute("""
        SELECT v.ano, v.mes, v.estado,
               ROUND(v.eth_hid_m3) AS vendas_m3,
               ROUND(COALESCE(p.eth_hid_m3,0)) AS prod_m3,
               ROUND(COALESCE(p.eth_hid_m3,0) - v.eth_hid_m3) AS saldo_m3
        FROM anp_vendas_uf v
        LEFT JOIN anp_producao_uf p ON p.ano=v.ano AND p.mes=v.mes AND p.estado=v.estado
        WHERE v.ano >= 2017 AND v.eth_hid_m3 IS NOT NULL
        ORDER BY v.ano, v.mes, v.estado
    """).fetchall()

    otto_rows = conn.execute("""
        SELECT ano, mes, estado,
               ROUND(eth_hid_m3*0.70/(eth_hid_m3*0.70+gas_c_m3),4)
        FROM anp_vendas_uf
        WHERE eth_hid_m3 IS NOT NULL AND gas_c_m3 IS NOT NULL
          AND (eth_hid_m3*0.70+gas_c_m3) > 0
        ORDER BY ano, mes, estado
    """).fetchall()

    deficit_series = {}; deficit_map = {}
    for ano, mes, estado, vendas, prod, saldo in deficit_rows:
        d = f"{ano}-{mes:02d}"
        if estado not in deficit_series: deficit_series[estado] = []
        deficit_series[estado].append({"d":d,"vendas":vendas,"prod":prod,"saldo":saldo})
        if d not in deficit_map: deficit_map[d] = {}
        deficit_map[d][estado] = {"s":saldo,"v":vendas,"p":prod}

    otto_series = {}; otto_map = {}
    for ano, mes, estado, pene in otto_rows:
        d = f"{ano}-{mes:02d}"
        if estado not in otto_series: otto_series[estado] = []
        otto_series[estado].append({"d":d,"p":float(pene)})
        if d not in otto_map: otto_map[d] = {}
        otto_map[d][estado] = float(pene)

    def_months  = sorted(deficit_map.keys())
    otto_months = sorted(otto_map.keys())

    def build_by_year(months):
        by_year = {}
        for m in months:
            y, mo = m[:4], m[5:7]
            if y not in by_year: by_year[y] = []
            by_year[y].append(mo)
        return by_year

    def_by_year = build_by_year(def_months)
    ott_by_year = build_by_year(otto_months)
    def_years   = sorted(def_by_year.keys(), reverse=True)
    ott_years   = sorted(ott_by_year.keys(), reverse=True)

    by_month2 = {}
    for uf, arr in deficit_series.items():
        for r in arr:
            d = r["d"]
            if d not in by_month2: by_month2[d] = {"vendas":0,"prod":0}
            by_month2[d]["vendas"] += (r.get("vendas") or 0)
            by_month2[d]["prod"]   += (r.get("prod") or 0)
    br_def = [{"d":d,"vendas":round(v["vendas"]),"prod":round(v["prod"]),"saldo":round(v["prod"]-v["vendas"])}
               for d,v in sorted(by_month2.items())]

    by_month3 = {}
    for uf, arr in deficit_series.items():
        for r in arr:
            d = r["d"]
            otto_val = otto_map.get(d, {}).get(uf)
            if otto_val is None or not r.get("vendas"): continue
            eth_eq = r["vendas"] * 0.70
            gas    = eth_eq * (1 - otto_val) / otto_val
            if d not in by_month3: by_month3[d] = {"eth_eq":0,"gas":0}
            by_month3[d]["eth_eq"] += eth_eq
            by_month3[d]["gas"]    += gas
    br_otto = [{"d":d,"p":round(v["eth_eq"]/(v["eth_eq"]+v["gas"]),4)}
                for d,v in sorted(by_month3.items()) if (v["eth_eq"]+v["gas"])>0]

    UF_CODE_SD = {
        'ACRE':'AC','ALAGOAS':'AL','AMAPÁ':'AP','AMAZONAS':'AM','BAHIA':'BA',
        'CEARÁ':'CE','DISTRITO FEDERAL':'DF','ESPÍRITO SANTO':'ES','GOIÁS':'GO',
        'MARANHÃO':'MA','MATO GROSSO':'MT','MATO GROSSO DO SUL':'MS','MINAS GERAIS':'MG',
        'PARÁ':'PA','PARAÍBA':'PB','PARANÁ':'PR','PERNAMBUCO':'PE','PIAUÍ':'PI',
        'RIO DE JANEIRO':'RJ','RIO GRANDE DO NORTE':'RN','RIO GRANDE DO SUL':'RS',
        'RONDÔNIA':'RO','RORAIMA':'RR','SANTA CATARINA':'SC','SÃO PAULO':'SP',
        'SERGIPE':'SE','TOCANTINS':'TO'
    }


    CODE_UF_PARITY = {"AC": "ACRE", "AL": "ALAGOAS", "AP": "AMAPA", "AM": "AMAZONAS", "BA": "BAHIA", "CE": "CEARA", "DF": "DISTRITO FEDERAL", "ES": "ESPIRITO SANTO", "GO": "GOIAS", "MA": "MARANHAO", "MT": "MATO GROSSO", "MS": "MATO GROSSO DO SUL", "MG": "MINAS GERAIS", "PA": "PARA", "PB": "PARAIBA", "PR": "PARANA", "PE": "PERNAMBUCO", "PI": "PIAUI", "RJ": "RIO DE JANEIRO", "RN": "RIO GRANDE DO NORTE", "RS": "RIO GRANDE DO SUL", "RO": "RONDONIA", "RR": "RORAIMA", "SC": "SANTA CATARINA", "SP": "SAO PAULO", "SE": "SERGIPE", "TO": "TOCANTINS"}
    CODE_NAME_MAP  = {"AC": "Acre", "AL": "Alagoas", "AP": "Amap\u00e1", "AM": "Amazonas", "BA": "Bahia", "CE": "Cear\u00e1", "DF": "Distrito Federal", "ES": "Esp\u00edrito Santo", "GO": "Goi\u00e1s", "MA": "Maranh\u00e3o", "MT": "Mato Grosso", "MS": "Mato Grosso do Sul", "MG": "Minas Gerais", "PA": "Par\u00e1", "PB": "Para\u00edba", "PR": "Paran\u00e1", "PE": "Pernambuco", "PI": "Piau\u00ed", "RJ": "Rio de Janeiro", "RN": "Rio Grande do Norte", "RS": "Rio Grande do Sul", "RO": "Rond\u00f4nia", "RR": "Roraima", "SC": "Santa Catarina", "SP": "S\u00e3o Paulo", "SE": "Sergipe", "TO": "Tocantins"}
    CODE_UF_SD_MAP   = {"AC": "ACRE", "AL": "ALAGOAS", "AP": "AMAP\u00c1", "AM": "AMAZONAS", "BA": "BAHIA", "CE": "CEAR\u00c1", "DF": "DISTRITO FEDERAL", "ES": "ESP\u00cdRITO SANTO", "GO": "GOI\u00c1S", "MA": "MARANH\u00c3O", "MT": "MATO GROSSO", "MS": "MATO GROSSO DO SUL", "MG": "MINAS GERAIS", "PA": "PAR\u00c1", "PB": "PARA\u00cdBA", "PR": "PARAN\u00c1", "PE": "PERNAMBUCO", "PI": "PIAU\u00cd", "RJ": "RIO DE JANEIRO", "RN": "RIO GRANDE DO NORTE", "RS": "RIO GRANDE DO SUL", "RO": "ROND\u00d4NIA", "RR": "RORAIMA", "SC": "SANTA CATARINA", "SP": "S\u00c3O PAULO", "SE": "SERGIPE", "TO": "TOCANTINS"}
    CODE_NAME_SD_MAP = {"AC": "Acre", "AL": "Alagoas", "AP": "Amap\u00e1", "AM": "Amazonas", "BA": "Bahia", "CE": "Cear\u00e1", "DF": "Distrito Federal", "ES": "Esp\u00edrito Santo", "GO": "Goi\u00e1s", "MA": "Maranh\u00e3o", "MT": "Mato Grosso", "MS": "Mato Grosso do Sul", "MG": "Minas Gerais", "PA": "Par\u00e1", "PB": "Para\u00edba", "PR": "Paran\u00e1", "PE": "Pernambuco", "PI": "Piau\u00ed", "RJ": "Rio de Janeiro", "RN": "Rio Grande do Norte", "RS": "Rio Grande do Sul", "RO": "Rond\u00f4nia", "RR": "Roraima", "SC": "Santa Catarina", "SP": "S\u00e3o Paulo", "SE": "Sergipe", "TO": "Tocantins"}
    UF_COORDS_SD_MAP = {"AC": [-9.02, -70.81], "AL": [-9.57, -36.78], "AM": [-3.47, -65.1], "AP": [1.41, -51.77], "BA": [-12.96, -41.7], "CE": [-5.5, -39.32], "DF": [-15.78, -47.93], "ES": [-19.19, -40.34], "GO": [-15.83, -49.84], "MA": [-5.42, -45.44], "MG": [-18.1, -44.38], "MS": [-20.77, -54.79], "MT": [-12.64, -55.42], "PA": [-3.41, -52.29], "PB": [-7.24, -36.78], "PE": [-8.38, -37.86], "PI": [-6.6, -42.28], "PR": [-24.89, -51.55], "RJ": [-22.25, -42.66], "RN": [-5.81, -36.59], "RO": [-10.83, -63.34], "RR": [1.99, -61.33], "RS": [-30.03, -53.2], "SC": [-27.45, -50.94], "SE": [-10.57, -37.45], "SP": [-22.25, -48.59], "TO": [-10.25, -48.25]}
    import json as _json
    J = lambda x: _json.dumps(x, separators=(',',':'))

    data_block = f"""
const SE_DATA      = {J(se_data)};
const UF_SERIES    = {J(uf_series)};
const BR_SERIES    = {J(br_series)};
const MAP_DATA     = {J(map_data)};
const MONTH_DATES  = {J(MONTH_DATES)};
const MONTH_LABELS = {J(MONTH_LABELS)};
const DEF_SERIES   = {J(deficit_series)};
const OTTO_SERIES  = {J(otto_series)};
const DEF_MAP      = {J(deficit_map)};
const OTTO_MAP     = {J(otto_map)};
const DEF_MONTHS   = {J(def_months)};
const OTTO_MONTHS  = {J(otto_months)};
const DEF_BY_YEAR  = {J(def_by_year)};
const OTT_BY_YEAR  = {J(ott_by_year)};
const DEF_YEARS    = {J(def_years)};
const OTT_YEARS    = {J(ott_years)};
const BR_DEF_SERIES  = {J(br_def)};
const BR_OTTO_SERIES = {J(br_otto)};
const UF_CODE_SD   = {J(UF_CODE_SD)};
const UF_COORDS_SD = {J(UF_COORDS_SD_MAP)};
const CODE_UF_SD   = {J(CODE_UF_SD_MAP)};
const CODE_NAME_SD = {J(CODE_NAME_SD_MAP)};
const CODE_UF      = {J(CODE_UF_PARITY)};
const CODE_NAME    = {J(CODE_NAME_MAP)};
const MONTH_NAMES  = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];"""

    # Decompress templates and assemble
    tmpl_before = gzip.decompress(base64.b64decode(_TMPL_BEFORE_B64)).decode("utf-8")
    tmpl_after  = gzip.decompress(base64.b64decode(_TMPL_AFTER_B64)).decode("utf-8")
    html = tmpl_before + data_block + tmpl_after

    out_path = DB_PATH.parent / "se_dashboard.html"
    out_path.write_text(html, encoding="utf-8")
    log.info(f"[Dashboard] Written: {out_path} ({len(html):,} chars)")

# ─────────────────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────────────────

def summary(conn):
    log.info("=" * 60)
    log.info("DB SUMMARY")
    pairs = [
        ("sugar_ny11",      "data_referencia"),
        ("etanol_cepea",    "data_referencia"),
        ("fx_usdbrl",       "data_referencia"),
        ("anp_estados",     "data_inicial"),
        ("anp_brasil",      "data_inicial"),
    ]
    for tbl, col in pairs:
        r = conn.execute(f"SELECT COUNT(*), MIN({col}), MAX({col}) FROM {tbl}").fetchone()
        log.info(f"  {tbl:22}: {r[0]:7,} | {r[1] or '—'} → {r[2] or '—'}")
    for tbl in ["anp_vendas_uf","anp_producao_uf"]:
        r = conn.execute(f"SELECT COUNT(*), MIN(ano), MAX(ano) FROM {tbl}").fetchone()
        lm = conn.execute(
            f"SELECT MAX(ano), MAX(mes) FROM {tbl} WHERE ano=(SELECT MAX(ano) FROM {tbl})"
        ).fetchone()
        log.info(f"  {tbl:22}: {r[0]:7,} | {r[1]}→{r[2]} | latest: {lm[0]}-{lm[1]:02d}")
    log.info("=" * 60)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    # Support --dashboard-only and --force-all flags
    dashboard_only = "--dashboard-only" in sys.argv
    global FORCE_ALL
    FORCE_ALL      = "--force-all" in sys.argv

    log.info("=" * 60)
    if dashboard_only:
        log.info(f"Agri Extractor | DASHBOARD-ONLY MODE | {NOW_STR}")
    else:
        log.info(f"Agri Extractor | {TODAY} ({TODAY.strftime('%A')}) | {NOW_STR}")
        log.info(f"  Weekday: {is_weekday()} | Thursday: {is_thursday()} | "
                 f"Vendas window: {is_vendas_window()} | Producao window: {is_producao_window()} | "
                 f"Force: {FORCE_ALL}")
    log.info("=" * 60)

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = get_conn()
    ensure_schema(conn)

    errors = []

    if not dashboard_only:
        # S&E — daily
        try:
            run_se(conn)
        except Exception as e:
            log.error(f"[S&E] FAILED: {e}")
            errors.append(f"S&E: {e}")

        # Fuel — Thursdays
        try:
            run_fuel(conn)
        except Exception as e:
            log.error(f"[Fuel] FAILED: {e}")
            errors.append(f"Fuel: {e}")

        # Supply/Demand — 5th of month
        try:
            run_supply_demand(conn)
        except Exception as e:
            log.error(f"[Supply/Demand] FAILED: {e}")
            errors.append(f"Supply/Demand: {e}")

    # Regenerate dashboard with latest data
    try:
        generate_dashboard(conn)
    except Exception as e:
        log.error(f'[Dashboard] Generation failed: {e}')
        errors.append(f'Dashboard: {e}')

    summary(conn)
    conn.close()

    if errors:
        log.error(f"EXTRACTOR FINISHED WITH {len(errors)} ERROR(S):")
        for e in errors:
            log.error(f"  • {e}")
        sys.exit(1)
    else:
        log.info("All sections completed successfully.")


if __name__ == "__main__":
    main()
