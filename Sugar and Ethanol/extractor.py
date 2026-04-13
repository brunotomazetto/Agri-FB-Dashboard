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
_TMPL_BEFORE_B64 = "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"
_TMPL_AFTER_B64  = "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"

def generate_dashboard(conn: sqlite3.Connection) -> None:
    """Regenerate se_dashboard.html with latest data from DB."""
    import base64, gzip
    from collections import OrderedDict

    log.info("[Dashboard] Regenerating se_dashboard.html...")

    # ── Extract all data ──────────────────────────────────────────────────────
    ATR_VHP=1.05; ATR_HYD=1.68; FRETE=85.0; ELEVACAO=10.5; CONV_L_TON=1.04; CONV_TON_LB=22.0

    se_rows = conn.execute("""
        SELECT e.data_referencia, s.preco_usdclb, e.preco_brl_m3, f.ptax_venda
        FROM etanol_cepea e
        JOIN sugar_ny11  s ON s.data_referencia = e.data_referencia
        JOIN fx_usdbrl   f ON f.data_referencia = e.data_referencia
        ORDER BY e.data_referencia
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
