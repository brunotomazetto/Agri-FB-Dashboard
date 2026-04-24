"""
CVM Buybacks — monitor de negociações de insiders e programas de recompra.

Fontes públicas (Portal de Dados Abertos CVM):
    1. VLMO (Valores Mobiliários Negociados e Detidos) — art. 11 da Resolução CVM 44
       (norma que substituiu a Instrução CVM 358 em 2021).
       Atualizado semanalmente. Cobre compras/vendas e saldos por:
       Companhia, Controlador, Conselho de Administração, Diretoria,
       Conselho Fiscal e Pessoas Ligadas.

    2. Programa de Recompra de Ações — dataset lançado pela CVM em nov/2025.
       Atualizado diariamente. Lista todos os programas aprovados, em
       andamento e encerrados, com quantidade aprovada vs adquirida.

Execução típica (GitHub Actions, semanalmente):
    python cvm_buybacks.py              # incremental: ano atual + anterior
    python cvm_buybacks.py --bootstrap  # primeira carga: últimos 5 anos

O SQLite (cvm_buybacks.db) é commitado no próprio repositório.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import logging
import re
import sqlite3
import zipfile
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Iterator

import pandas as pd
import requests

# ============================================================================
# CONFIGURAÇÃO — edite aqui os tickers monitorados
# ============================================================================

# Dict de ticker -> CNPJ (somente dígitos). Usar CNPJ é mais robusto que
# tentar resolver pelo nome, já que grupos econômicos compartilham nomes.
# Adicione/remova tickers à vontade.
TICKERS: dict[str, str] = {
    "MBRF3": "03853896000140",  # MBRF Global Foods (ex-Marfrig, incorporou BRF em set/2025)
    "BEEF3": "67620377000114",  # Minerva
    "JBSS3": "02916265000160",  # JBS
    "ABEV3": "07526557000100",  # Ambev
    "MDIA3": "07206816000115",  # M. Dias Branco
    "CAML3": "64904295000103",  # Camil Alimentos
    "SLCE3": "89096457000155",  # SLC Agrícola
    "TTEN3": "94813102000170",  # 3tentos Agroindustrial
    "SMTO3": "51466860000156",  # São Martinho
    "JALL3": "02635522000195",  # Jalles Machado
    "SOJA3": "10807374000177",  # Boa Safra Sementes
    "VITT3": "45365558000109",  # Vittia Fertilizantes e Biológicos
    "AGRO3": "07628528000159",  # BrasilAgro
}

# ============================================================================
# CONSTANTES INTERNAS
# ============================================================================

SCRIPT_DIR = Path(__file__).resolve().parent
DB_PATH = SCRIPT_DIR / "cvm_buybacks.db"

VLMO_URL = (
    "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/VLMO/DADOS/"
    "vlmo_cia_aberta_{year}.zip"
)
RECOMPRA_URL = (
    "https://dados.cvm.gov.br/dados/CIA_ABERTA/EVENTOS/RECOMPRA_ACOES/DADOS/"
    "cia_aberta_recompra_acoes.zip"
)

# User-Agent cordial (algumas APIs públicas rejeitam clientes anônimos)
HTTP_HEADERS = {"User-Agent": "cvm-buybacks-monitor/1.0"}


# ============================================================================
# SCHEMA DO BANCO
# ============================================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    cnpj_digits  TEXT PRIMARY KEY,
    ticker       TEXT NOT NULL,
    nome         TEXT,
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_companies_ticker ON companies(ticker);

CREATE TABLE IF NOT EXISTS vlmo_movements (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    cnpj_digits              TEXT    NOT NULL,
    nome_companhia           TEXT,
    data_referencia          TEXT    NOT NULL,     -- YYYY-MM-01
    data_entrega             TEXT,
    versao                   INTEGER,
    tipo_cargo               TEXT,                 -- Controlador / Conselho / Diretor / Companhia / etc.
    tipo_movimentacao        TEXT,                 -- Compra / Venda / Bonificação / etc.
    intermediario            TEXT,
    especie_vm               TEXT,                 -- ON / PN / UNT
    caracteristica_vm        TEXT,
    mercado                  TEXT,
    quantidade               REAL,
    preco                    REAL,
    volume                   REAL,
    natural_key              TEXT    NOT NULL UNIQUE,
    ingested_at              TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_mov_cnpj_data ON vlmo_movements(cnpj_digits, data_referencia);

CREATE TABLE IF NOT EXISTS vlmo_positions (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    cnpj_digits              TEXT    NOT NULL,
    nome_companhia           TEXT,
    data_referencia          TEXT    NOT NULL,
    tipo_cargo               TEXT,
    especie_vm               TEXT,
    caracteristica_vm        TEXT,
    saldo_inicial            REAL,
    saldo_final              REAL,
    qtd_compra               REAL,
    qtd_venda                REAL,
    natural_key              TEXT    NOT NULL UNIQUE,
    ingested_at              TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_pos_cnpj_data ON vlmo_positions(cnpj_digits, data_referencia);

CREATE TABLE IF NOT EXISTS buyback_programs (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    cnpj_digits              TEXT    NOT NULL,
    nome_companhia           TEXT,
    numero_programa          TEXT,
    data_aprovacao           TEXT,
    data_inicio              TEXT,
    data_fim                 TEXT,
    situacao                 TEXT,
    especie_acao             TEXT,
    qtd_aprovada             REAL,
    qtd_adquirida            REAL,
    percentual_free_float    REAL,
    natural_key              TEXT    NOT NULL UNIQUE,
    ingested_at              TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_buyback_cnpj ON buyback_programs(cnpj_digits);

CREATE TABLE IF NOT EXISTS ingestion_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at       TEXT NOT NULL DEFAULT (datetime('now')),
    source       TEXT NOT NULL,
    rows_seen    INTEGER,
    rows_new     INTEGER,
    status       TEXT,
    message      TEXT
);
"""


# ============================================================================
# UTILIDADES
# ============================================================================

log = logging.getLogger("cvm_buybacks")


@contextmanager
def db_conn() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with db_conn() as conn:
        conn.executescript(SCHEMA)


def sync_companies() -> None:
    """Popula/atualiza a tabela companies a partir do dict TICKERS."""
    with db_conn() as conn:
        for ticker, cnpj in TICKERS.items():
            conn.execute(
                """
                INSERT INTO companies (cnpj_digits, ticker, updated_at)
                VALUES (?, ?, datetime('now'))
                ON CONFLICT(cnpj_digits) DO UPDATE SET
                    ticker     = excluded.ticker,
                    updated_at = datetime('now')
                """,
                (cnpj, ticker),
            )


def watched_cnpjs() -> set[str]:
    return set(TICKERS.values())


def only_digits(s) -> str:
    if pd.isna(s) or s is None:
        return ""
    return re.sub(r"\D", "", str(s))


def parse_date(s) -> str | None:
    """Aceita vários formatos da CVM e devolve ISO 'YYYY-MM-DD'."""
    if pd.isna(s) or s == "" or s is None:
        return None
    s = str(s).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    if re.fullmatch(r"\d{4}-\d{2}", s):
        return f"{s}-01"
    return None


def safe_float(v) -> float | None:
    if pd.isna(v) or v == "" or v is None:
        return None
    try:
        s = str(v)
        # CSV CVM usa ',' decimal e às vezes '.' como milhar
        if "," in s:
            return float(s.replace(".", "").replace(",", "."))
        return float(s)
    except (ValueError, TypeError):
        return None


def pick(row: pd.Series, *candidates: str):
    """Primeiro nome de coluna que existir (tolerante a renomeações da CVM)."""
    for c in candidates:
        if c in row.index and not pd.isna(row[c]):
            return row[c]
    return None


def log_run(source: str, seen: int, new: int, status: str, msg: str = "") -> None:
    with db_conn() as conn:
        conn.execute(
            "INSERT INTO ingestion_log (source, rows_seen, rows_new, status, message) VALUES (?,?,?,?,?)",
            (source, seen, new, status, msg),
        )


# ============================================================================
# INGESTÃO — VLMO
# ============================================================================

def _natural_key_mov(row: pd.Series) -> str:
    parts = [
        only_digits(row.get("CNPJ_Companhia", "")),
        str(row.get("Data_Referencia", "")),
        str(row.get("Tipo_Cargo", "")),
        str(row.get("Tipo_Movimentacao", "")),
        str(row.get("Especie_Valor_Mobiliario", "")),
        str(row.get("Caracteristica_Valor_Mobiliario", "")),
        str(row.get("Intermediario", "")),
        str(row.get("Quantidade", "")),
        str(row.get("Preco", "")),
        str(row.get("Volume", "")),
    ]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def _natural_key_con(row: pd.Series) -> str:
    parts = [
        only_digits(row.get("CNPJ_Companhia", "")),
        str(row.get("Data_Referencia", "")),
        str(row.get("Tipo_Cargo", "")),
        str(row.get("Especie_Valor_Mobiliario", "")),
        str(row.get("Caracteristica_Valor_Mobiliario", "")),
    ]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def ingest_vlmo_year(year: int, cnpjs: set[str]) -> dict:
    url = VLMO_URL.format(year=year)
    log.info("GET %s", url)
    try:
        r = requests.get(url, headers=HTTP_HEADERS, timeout=120)
        if r.status_code == 404:
            log.info("Ano %d ainda não publicado. Pulando.", year)
            log_run(f"vlmo_{year}", 0, 0, "skipped", "404")
            return {"mov_new": 0, "con_new": 0}
        r.raise_for_status()
    except requests.RequestException as e:
        log.error("Falha baixando VLMO %d: %s", year, e)
        log_run(f"vlmo_{year}", 0, 0, "error", str(e))
        return {"mov_new": 0, "con_new": 0, "error": str(e)}

    zf = zipfile.ZipFile(io.BytesIO(r.content))
    mov_new = _ingest_vlmo_csv(zf, "_mov_", cnpjs, is_movement=True)
    con_new = _ingest_vlmo_csv(zf, "_con_", cnpjs, is_movement=False)

    result = {"mov_new": mov_new, "con_new": con_new}
    log_run(f"vlmo_{year}", mov_new + con_new, mov_new + con_new, "ok", str(result))
    log.info("VLMO %d: %s", year, result)
    return result


def _ingest_vlmo_csv(zf: zipfile.ZipFile, name_substr: str, cnpjs: set[str], is_movement: bool) -> int:
    candidates = [n for n in zf.namelist() if name_substr in n and n.endswith(".csv")]
    if not candidates:
        return 0

    name = candidates[0]
    log.info("  Lendo %s", name)
    with zf.open(name) as f:
        df = pd.read_csv(f, sep=";", encoding="latin-1", dtype=str)

    if df.empty:
        return 0

    df["__cnpj_d"] = df["CNPJ_Companhia"].map(only_digits)
    df = df[df["__cnpj_d"].isin(cnpjs)].copy()
    if df.empty:
        return 0

    df["Data_Referencia"] = df["Data_Referencia"].map(parse_date)
    if is_movement and "Data_Entrega" in df.columns:
        df["Data_Entrega"] = df["Data_Entrega"].map(parse_date)

    rows_new = 0
    with db_conn() as conn:
        for _, row in df.iterrows():
            if is_movement:
                nk = _natural_key_mov(row)
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO vlmo_movements (
                        cnpj_digits, nome_companhia, data_referencia, data_entrega,
                        versao, tipo_cargo, tipo_movimentacao, intermediario,
                        especie_vm, caracteristica_vm, mercado,
                        quantidade, preco, volume, natural_key
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        row["__cnpj_d"],
                        row.get("Nome_Companhia"),
                        row.get("Data_Referencia"),
                        row.get("Data_Entrega"),
                        row.get("Versao"),
                        row.get("Tipo_Cargo"),
                        row.get("Tipo_Movimentacao"),
                        row.get("Intermediario"),
                        row.get("Especie_Valor_Mobiliario"),
                        row.get("Caracteristica_Valor_Mobiliario"),
                        row.get("Mercado"),
                        safe_float(row.get("Quantidade")),
                        safe_float(row.get("Preco")),
                        safe_float(row.get("Volume")),
                        nk,
                    ),
                )
            else:
                nk = _natural_key_con(row)
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO vlmo_positions (
                        cnpj_digits, nome_companhia, data_referencia,
                        tipo_cargo, especie_vm, caracteristica_vm,
                        saldo_inicial, saldo_final, qtd_compra, qtd_venda, natural_key
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        row["__cnpj_d"],
                        row.get("Nome_Companhia"),
                        row.get("Data_Referencia"),
                        row.get("Tipo_Cargo"),
                        row.get("Especie_Valor_Mobiliario"),
                        row.get("Caracteristica_Valor_Mobiliario"),
                        safe_float(row.get("Saldo_Inicial")),
                        safe_float(row.get("Saldo_Final")),
                        safe_float(row.get("Quantidade_Compra")),
                        safe_float(row.get("Quantidade_Venda")),
                        nk,
                    ),
                )
            rows_new += cur.rowcount
    return rows_new


# ============================================================================
# INGESTÃO — RECOMPRAS
# ============================================================================

def _natural_key_buyback(row: pd.Series) -> str:
    parts = [
        only_digits(pick(row, "CNPJ_Companhia", "CNPJ_CIA", "CNPJ")),
        str(pick(row, "Numero_Programa", "Programa", "ID_Programa") or ""),
        str(pick(row, "Data_Aprovacao", "Data_Deliberacao") or ""),
        str(pick(row, "Especie_Acao", "Especie", "Tipo_Acao") or ""),
    ]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def ingest_recompras(cnpjs: set[str]) -> dict:
    log.info("GET %s", RECOMPRA_URL)
    try:
        r = requests.get(RECOMPRA_URL, headers=HTTP_HEADERS, timeout=120)
        r.raise_for_status()
    except requests.RequestException as e:
        log.error("Falha baixando recompras: %s", e)
        log_run("recompra", 0, 0, "error", str(e))
        return {"rows_new": 0, "error": str(e)}

    zf = zipfile.ZipFile(io.BytesIO(r.content))
    csvs = [n for n in zf.namelist() if n.endswith(".csv")]
    rows_new = 0
    rows_seen = 0

    for csv_name in csvs:
        with zf.open(csv_name) as f:
            df = pd.read_csv(f, sep=";", encoding="latin-1", dtype=str)
        if df.empty:
            continue

        cnpj_col = next((c for c in ("CNPJ_Companhia", "CNPJ_CIA", "CNPJ") if c in df.columns), None)
        if cnpj_col is None:
            log.warning("  %s sem coluna de CNPJ — pulando", csv_name)
            continue

        df["__cnpj_d"] = df[cnpj_col].map(only_digits)
        df = df[df["__cnpj_d"].isin(cnpjs)].copy()
        rows_seen += len(df)
        if df.empty:
            continue

        with db_conn() as conn:
            for _, row in df.iterrows():
                nk = _natural_key_buyback(row)
                cur = conn.execute(
                    """
                    INSERT INTO buyback_programs (
                        cnpj_digits, nome_companhia, numero_programa,
                        data_aprovacao, data_inicio, data_fim, situacao,
                        especie_acao, qtd_aprovada, qtd_adquirida,
                        percentual_free_float, natural_key
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(natural_key) DO UPDATE SET
                        situacao      = excluded.situacao,
                        data_fim      = COALESCE(excluded.data_fim, buyback_programs.data_fim),
                        qtd_adquirida = COALESCE(excluded.qtd_adquirida, buyback_programs.qtd_adquirida),
                        ingested_at   = datetime('now')
                    """,
                    (
                        row["__cnpj_d"],
                        pick(row, "Nome_Companhia", "Denom_Social"),
                        pick(row, "Numero_Programa", "Programa", "ID_Programa"),
                        parse_date(pick(row, "Data_Aprovacao", "Data_Deliberacao")),
                        parse_date(pick(row, "Data_Inicio", "Data_Inicio_Programa")),
                        parse_date(pick(row, "Data_Fim", "Data_Encerramento", "Data_Termino")),
                        pick(row, "Situacao", "Status"),
                        pick(row, "Especie_Acao", "Especie", "Tipo_Acao"),
                        safe_float(pick(row, "Quantidade_Aprovada", "Qtd_Aprovada", "Qtd_Autorizada")),
                        safe_float(pick(row, "Quantidade_Adquirida", "Qtd_Adquirida", "Qtd_Comprada")),
                        safe_float(pick(row, "Percentual_Free_Float", "Pct_Free_Float")),
                        nk,
                    ),
                )
                if cur.rowcount > 0:
                    rows_new += 1

    log_run("recompra", rows_seen, rows_new, "ok")
    log.info("Recompras: %d novas (%d vistas)", rows_new, rows_seen)
    return {"rows_new": rows_new, "rows_seen": rows_seen}


# ============================================================================
# MAIN
# ============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="CVM Buybacks monitor")
    parser.add_argument(
        "--bootstrap", action="store_true",
        help="Primeira carga: puxa 5 anos de histórico.",
    )
    parser.add_argument(
        "--years", type=int, default=2,
        help="Anos para trás (default: 2 — ano atual e anterior).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
    )

    log.info("Inicializando DB em %s", DB_PATH)
    init_db()
    sync_companies()
    cnpjs = watched_cnpjs()
    log.info("Monitorando %d tickers: %s", len(TICKERS), ", ".join(TICKERS.keys()))

    current_year = date.today().year
    if args.bootstrap:
        years = list(range(current_year - 4, current_year + 1))
    else:
        years = list(range(current_year - args.years + 1, current_year + 1))
    log.info("Anos VLMO: %s", years)

    for y in years:
        try:
            ingest_vlmo_year(y, cnpjs)
        except Exception as e:
            log.exception("Falha no VLMO %d", y)
            log_run(f"vlmo_{y}", 0, 0, "error", str(e))

    try:
        ingest_recompras(cnpjs)
    except Exception as e:
        log.exception("Falha nas recompras")
        log_run("recompra", 0, 0, "error", str(e))

    log.info("Pronto.")


if __name__ == "__main__":
    main()
