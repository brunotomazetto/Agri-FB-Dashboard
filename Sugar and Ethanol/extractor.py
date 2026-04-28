#!/usr/bin/env python3
"""
extractor.py — IBBA Agri Monitor · Unified Daily Extractor
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
def is_month_5th()-> bool: return FORCE_ALL or TODAY.day == 5


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

    df = yf.Ticker("SB=F").history(start=start, end=TODAY.strftime("%Y-%m-%d"),
                                   auto_adjust=False)
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

    return {
        "vendas":  ingest_vendas(conn),
        "producao": ingest_producao(conn),
    }


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
_TMPL_BEFORE_B64 = "H4sIABH08GkC/+08W2/byHrv/hWzMnYjZU3dfIkjX3B0s6OFbamSXZz0bSSOJMYUyZKUEq+wxTko0PY8LFAcbNHH4qAtFn3IQ7EF9n31T/IL+hP6fTMkNbzIoh2vk2wDJ7Y4nPm++e6XIXX4RaNdv3zZaZKxO9GPNw7xD9GpMTrKMCODA4yq8GfCXEoGY2o7zD3KXF2eKPuZgj9u0Ak7ysw09toybTdDBqbhMgPmvdZUd3ykspk2YAq/2NIMzdWorjgDqrOjUr7Iwbiaq7Pj3nRE7S3SdMfUMHXyFTmZMp10qK25N4cFMWfjUNeMa0LGNhseZcauazmVQmGgGq+c/EA3p+pQpzbLD8xJgb6ibwq61ncKOqNDnbmFUv55fse/yk80Iz9wnAyxmX6UcdwbnTljxly+I355vPF062ml0mdD02b4iQ5dZs/75hvF0b7VjFGlb9oqsxUYOZhQe6QZleKBRVUV7xUPvtuo2KbpzjcIURTTBrayyubJye5usXigKH2dDq4rm6Uq/sD1yKY3ikptGNve3vYHJppa2dzf3z/gQPiQro3GLgDawR8ExDdR2Wzu4g8MvB5rLmAaDof+KsaMymb5WbW5h5htBjDrxe3n5Rpc0Umfr9571iyXxYIhSLDypMdGJiNXrSdbL5g+Y642oFtVG6S35VDDURxma0OERlVt6lT2rTcHG99toAZt9U31Zo5AlCGdaPpNZUbtrACbO+gD2SPbnBqqN7wkKncwMHXTlseRITng5EbhKXn3wx/gHznRdBAD6VPbH3la2MgP+agCo/MYBs4QwOxLy3XNSaVkvSGOqWsqEZPE3RxywBdhqQxzykWgTNUcS6c3FdCcNwcUNmsoAHLi8AGFGerBiFqV0h5M5SOvbbjEXxyc6YDWm0bFAR5e3xy4pgXa8a2iGSp7U0Ft+C7YP+7bmofQcYCqZrMBBwIsmk4Mjm8H0C2X6rTPdMF2UE9WKeHG+eVrxlXmGWCKMRgULHfgsjeu4oKGOqDqk8rUspg9oA47AENB0I5FB8iQ/G4Io8OsObdrZObBWGDZwV3FROBzV/DOYfqwMgAnwWwE57BRQLJmgIUzhVN+q6p4ar9KjJ6wPe3EfftSBaFy7pUFLYBc6bvG3L8NUwkK3kdgmAaLQNvxGSv4XI7weQ/4DGIfTG0HeG2ZGqczhT1wCVjgwAx3laBwhlAmquskX9p1JBryphFXfuF4fNPipiAtqYzNGSfSzcLq3DyNAdbAvGXT4+Ye2MyeZzNkt8gZLC3sCRUmGFVYyHodcUcZq/Z8pa15CvNqCnY0vFG8OFNB5WRKn7mvwc35friMplskwgbCFomy3/dlL9Dy4CKbTinBdFIbyTN0hSD/RAl6mqSzoVvZjuiuLyqPl2JSZKuqNtNUHoVi2ol+pbzCHHy27HO2FMNyqUNkd8mA2qojCwUDvgtb19RAJnghXB0qvQAaOFTPoEalOU5TQGawxmWK8FhOpTTk1j4qr75PvDkCNe5ojTdPaf/inrgI+Ct0teQxWKCMacL2GifKw3guwoqyDNKZ9qOqlawaEXbGzAdSIToJScjiI0kiSmSwzSxG3SyduqYCLlzfghxoQt9kS7vg9LaA9bmcEG5xKVxUKm8rAh03xrQRalte+BgBaie0U8OauoFn4sIuLh17CaNZytgB5pykEwnuPEE3zKmLMU3Yqhcvi8UvZWfuYeSLSb7sRGioDM3B1JnLsyLeHeaDN4fUDhLrwVyWXVkKfft+PpMqSsguBhgQ58o6M0kXAENbF/FINvpNVtyhxWLMFKAqII5LXQfSNQMKhawGzFQZL1PcXMRKYLLCJ4cVF1W0mGx38qo5Tq6UQmyUUgSfJXvWmpQlYOJKr5XSrMpCJcF8RV3Fiw15x1+Htu8RKIcT6XbMLp/nd+8S/mAjSWlisiFHUId94/Ng2Wa/34/OnVF9GnLN+wm75Mmjl4mW8qWQF+OUx3RoIIIfwLFN3VmqEcSeJC3yJ65NU6KOVI6SkYxkSeh0eGsumtZlJSSovgeTctS7J6ihALwyxUlKUvkariDoBxOpzlPQ8xlbncHe4v4iyW0IrpziChS5+VpQUe8KFQuYYaCP/CospgeJLOW7RZZbRBMKOhFJL/1GaR+3fXsgSiI9TUCSTO2cWrIpTaj1uLkdR7jWcMO1fDmt6XLgrq3fq3YPFj9e9e7JEzH/anq8/Sh6nEpvl3SmUdpNmD4POhnFxBiP7mSom68rY01VoeSUk5XhPv54gBSVDbf4B1QgH+z2fjqwEgwSACFz4oEhAk4kLSMIiQSgiAeLeKqmsxEoeLrYtZNQBES0P1VZA6gFWhxTNcDgc4IblOyIwrRsh/MpVANqB0CywAwbwWx5nc2tzZPnzVKpueX1NnNit87Y1oxrkSF5+8A2XMQP3N5UkPcYMVGPsTYbpuLqbmiJgjR5LTSel3uMKYcp39zd3T2INMj6ujm4DnvZM9HZlj2t1+zmZFFYZq9qy36hTbB7Tw1Xip96X58nt6ek6VKVEBpFzlMVdDByR+qvFqXhVd6COzfOTJ+NnvUrbAbXjnAM4TaTObUHLNRe4iMKpAAs6mKlfFNCxRUrpP3LlAqPBiqaCxMHgPd3E6ZqNAtltBdSn+0B2Nz81jbHwXdyItqzdE20XypeNoqZOvmagJYIFQ/RgrN/tfAJ0knRSPA7NaJ+ijsub5NIxjwekH38wgfcvxIK8HBI81DXnsvrLjDQ73tlHqZHvq8ux93STqKvXoLh8TxllrEfSzL2E3OMMHByz5zD68asBJauEHyYDk1kE6uTEphP9uP1TAId4RrKl2DEZ5ekdsiOn+9IGsvjo2T4IoOMWfnzorByyRpvsXbZHkLaL7dv15xLgcs4LHjnkocF72AWW+/HGxuHXygKefcv/5z4j5y0zi6bXVKrdsm7P/xAOszWTBXcyxl1XHJlqbBdh5iGfrMahKIAMlWbkYFOHecoszxtyxyDy0i4xRWT30y8zVUtcyz2cliACfGpDhtliKbyD4rFZ3oAYV5/CtwypKkodKAiA/8H4JqvcdAV8LNPoA59suWONSeXOa7q+mFBLL8dWgZvJUHbvQmA7b58T1jbS1jb7wurtIRVel9YN64aAHt52XhPaHuTANjeeRhWIPzlh7i6OAx06bYJ6dSt9xWdWAdNoqPmT7nmR3WPWxisW93a3Es+ARCqipAVARk2nTkGg3tMAqsXnUcibjhFfAnkeX/W+KRau/EyvbtBN5cJYHpZ01dN/2N0unSaGHNPoSO/jHjohFy8LJXIzCEvblRgsxM8g9L826k2ozrkmaRja5BTZq96jUFB7+fCxMoIlqdmZFSKoV8ebCXIUjqCAtfIEdbNCYY1xzRWOMngjClES1bIjfzyczJNeZL1al6c0rNsCCfkiAgI7/7pz7HZeFzEVALJP93yclL6Bux5hYYFjzzYDKKhNmNBZYPZMWj6gBoz6nCtGoy5pRwWxNjxCpDSIWfp1kPOZb7un6MGA8vwsUJg5YyPLpyVFZdLxeLl1WoRJphbmW8iKhJPt2rds8Lkl59knq5CwOVdb3aa1UKzVz37K5Rih071xVtDoyTb6+R4qPcQJQNcJype7sZExdxxsqxCvvyBGCVrdMTy1nKnVW+Sk6k7tSG5Ae68pGPTJCeaQQ3kda92dJJ7SL44uNWUnJE1fJX3HNtRj+Wd/OPzaSFH2Kl2W5cv/dJRVI38CKN/I/6m8JIRredPEgQGc4v79DW4QE4pWqTBggf2VvnHZcYcBx5kyQnOMXL+sioTDBUBRPT7RfTCQ8a+TR1ND+eIVyfZJ7Vutdc6C3KU//23f/wR/v9Majb9Vouni6JEiaAUgwIXoPcvAdUYPa2H68Q2Jz1+K4vI8vxUKyd7F9PiD8nwG0cZHmOJWMHPOxnhQVfMWrmsWu82IR0Y2KDr1Xpu/fyz6mm72sPsmI5MUOts9SzFqvNqpwprQOUWf4ElnVRL/qZ9wTFN6LemwVGdr19Xq75oAaoaHaODq1XXr6g3q11YUWfUxs3Vm+uXNFq9SzCmNjlpNprd6lnmuKE5LkgXPAcD26M6yTZO1sNp9jotDqdXvbhsg5k41uItB9OjBvzONnvrgZy2W8imU1Nb/AWYdNpev+QcKL54UQWM5xTC+3jx74DrvJpmIWz2tNvu9fha2OKpbToOrr6802rSAKKvzsJAVKB7Cqw7T0H1eQuUg5wC91tA/LmGCnIKnNeABeen69d3uNA7QuadaroFrZpYQxdv+6BdnVq6ZRfeKoPj6qZY1OxeVM9rV/U2L30NOulPB8DkTgrd7LSqVy1YptHp4i0saa1f0m2BPJrkm+pFs9UFlF0NZMHIN9Rgmg1ou9+kg3EKpAIcEO1Fu3vZFIBOgW4ABrK9MG3wStnuxV2hcUUJw+J60k2hJ932RaN9gS6haxrq4n943tNtp1kI8j7n60CrJrgqheTQkKukXr2EYItiRzumpE5d8P0GZlz1NDDaEKyvzkASPbRMTNhMnqytX9rsnrY6wPkegwBtYQKTQmUu23XYdusCDOnShLzE1Qywoss4kw4LIlpFcpJbMjYMcl7cC+dw0RMjbiH4nAWGMRFOSfaCInaq59YXNdFcefHzMtWwphOLWDx5hgyvQZ0xwyrmWfFL0odC5ZofEeAtKItXYJKfDQpRJUaS6gVpQqQoSJjhF+Zn2GRzE/LjpDVIuGh8CQi4VMGqO1xw3w6E60AcDAzHoYQv3pfW+tTGMyLygtqz+xI9mAY9jfuTjEAegd6OzWYa6uj7EAwl+Oz9KeZQ1pGcumaPn4eUi8KmI8WP2FJS9ZPY0pJObRIS/XBbP9EEw8cfKyQVPtaAXAIqh3GUr+FMfnn8IHiK1xNcJqfxpnFOrZeQVdb5QDaHdMv+8xb9utfGEdfd930Dqz7otr8y+o51EN34LdXaskyjhjGlukJnI6lUc83RSGdVfqs6G2VFreZrr3zcA7XFbEQE2yKVW4zCeOtBOhV+/vx50ESSHu2WXmIJTvNkpjgWNQI5vGbsWvEO6MKdjjUtV2HCCCuFDXvYMjEjyhyvOl2RDrzI8tmQkL0lCzryNEfm+BZnJT9xARM5Ne/+9B+7u196pIkhCNmh63d/+s/95ZRUrtt7pMJDEhnlD1pwhUeAkQzhYdsxjeZJq966LPSuup2zq96H7cv0ppal3xQabAIJNk/BGmyoDTSX8JMQuG9butQgXGESqx/uwehnqlPxXg82jntUFw23yS8/eakX/sG2BykXS88+0saQCjqS1BwCdkn9oYdoDSGmVe0hju031yFa/PFjbhHh7j5Qj2jx/UM0iRZ/vFeXaPH3/8/bRCj4O/aJFt/fs1Hk4Xq8TtHi+8+tIrlVtPjhE+sVgXl+Es2ie+QM8utIPJkJ4qLXo+hTdcTCiWvKlhQCWdePiudE92hLdXixPGP80JznUJjlXLAR9Ub9PEs6gWdeUgaZ0fuU4OXEElzlqe9vrP5Gca6owYG/UM9+vCW4v/V4GS52/olU4kiGqMZjlTjQIYrxT6ES98XxWIU4t8cPUYwHj8ukeXGiVNurV/e3NquNxn5zb2vzpFrbqe0sX6C4c2kf1JNSLf93xdCl5xjT1fZ3K0V/+e/tYvHarzzrqKx+QfQPfxZ1+MPW+u1LyGjrL+tnzQ9b5neYwVybiiIcAlob3xSq3wx09j6VfRbA58kLTc2Txb+SYv5ZMYfHL/FhoNo7k4E0hz9R1jQg9bghbPn8XOgU5mOr/tGhJJb/yMgHrv85rlUNAIHvcwfgcwfgcwfgcwfgcwfgcwfgt/O4CA9864rzZeYSS2ruUaX7+RET6YgDdxjRDCk/IvgeA377hzOdcBofvjJHun97pTmX5oraHNn7URfnwebj1bm390+kPOeErKjPkZJPp0APRPJoFTq3y0+mRI9+t8GdS/Mz83W4LN+OnLm/AIS/RlWe6ITvX5+HZLJ8r5/LwXu/r0KkF0cwdCS9/JH0bhTOvWq0OwX+ao0iXq1ZvleD59i/F7GoXiOdy+rvcUi8QasSlWr6zWHfxo1IX11c4UfgvEBb/JetMaicHXfxk60NKOYwZ2yGycGEYcIPaU3HZosfTQerbL4lqLMhBlKYWZCKbHMyneRk5PiUSYA93OEWJ/3LkLfc0F+DkkCcAqwNZkOhrpr8ogNhd/GTzkzCSE0zByZknrDltzMGufXXhB/4L37ElAQmx7YpbYqHho91VxvRto8zsDUL6md7EPpm6fwrR2W6NrPzkI4UDGtS4IoKw7/bye/ki7Dccb2x6UTl3yv9yuExgwM8Xgn5ft9ZnQj7mH+dhHh5sgFpp//5aeH/AIMs1i/kWwAA"
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
        log.info(f"IBBA Extractor | DASHBOARD-ONLY MODE | {NOW_STR}")
    else:
        log.info(f"IBBA Extractor | {TODAY} ({TODAY.strftime('%A')}) | {NOW_STR}")
        log.info(f"  Weekday: {is_weekday()} | Thursday: {is_thursday()} | 5th: {is_month_5th()} | Force: {FORCE_ALL}")
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
