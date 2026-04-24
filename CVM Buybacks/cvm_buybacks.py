"""
CVM Buybacks — monitor de negociações de insiders e programas de recompra.

Fontes públicas (Portal de Dados Abertos CVM):

    1. VLMO (Valores Mobiliários Negociados e Detidos)
       https://dados.cvm.gov.br/dataset/cia_aberta-doc-vlmo
       Base legal: art. 11 da Resolução CVM 44 (substituiu a ICVM 358 em 2021).
       Atualizado semanalmente pela CVM.

       Dentro de cada ZIP anual vêm 2 CSVs:
         - vlmo_cia_aberta_AAAA.csv      -> índice de entregas (protocolos,
                                            versões, links para o PDF original)
         - vlmo_cia_aberta_con_AAAA.csv  -> CONTEÚDO: saldos e movimentações
                                            dos insiders. É o arquivo principal.

       Cada linha do arquivo "con" é uma de três coisas, identificadas pela
       coluna Tipo_Movimentacao:
         - "Saldo Inicial"  -> posição no 1º dia do mês (sem Data_Movimentacao)
         - "Saldo Final"    -> posição no último dia do mês
         - qualquer outro   -> operação real (Compra à vista, Venda à vista,
                               Posse, Desligamento/saída, Doação, Subscrição,
                               Plano de remuneração, etc. — ~30 tipos)

    2. Recompras (dataset lançado em nov/2025, atualizado diariamente)
       https://dados.cvm.gov.br/dataset/cia_aberta-eventos-recompra_acoes

       Dentro do ZIP vêm 3 CSVs complementares:
         - programa            -> cabeçalho: aprovação, prazo, qtd aprovada
         - quantidades         -> execução por classe de ação
         - intermediarios      -> corretoras contratadas

Ingestão idempotente via `natural_key` (hash SHA-1 dos campos que definem
uma linha univocamente). A CVM reapresenta arquivos passados semanalmente
com correções; rodar 52 semanas seguidas não duplica nada.
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

HTTP_HEADERS = {"User-Agent": "cvm-buybacks-monitor/2.0"}


# ============================================================================
# SCHEMA DO BANCO
# ============================================================================

SCHEMA = """
-- Cadastro dos tickers monitorados (populado a partir do dict TICKERS)
CREATE TABLE IF NOT EXISTS companies (
    cnpj_digits  TEXT PRIMARY KEY,
    ticker       TEXT NOT NULL,
    nome         TEXT,
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_companies_ticker ON companies(ticker);


-- ---------------------------------------------------------------------------
-- VLMO: tabela UNIFICADA que espelha o CSV vlmo_cia_aberta_con_AAAA.csv
--
-- Cada linha pode ser:
--   - Saldo Inicial / Saldo Final  (sem data_movimentacao)
--   - Uma operação real (Compra, Venda, Posse, Doação, etc.)
--
-- A view v_insider_trades filtra só operações reais; v_monthly_balances
-- filtra só saldos finais.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS vlmo_entries (
    id                              INTEGER PRIMARY KEY AUTOINCREMENT,

    -- identificação da companhia reportante
    cnpj_digits                     TEXT    NOT NULL,
    nome_companhia                  TEXT,

    -- período e versão do informe
    data_referencia                 TEXT    NOT NULL,    -- mês de competência, YYYY-MM-01
    versao                          INTEGER,

    -- QUEM: categoria e identidade do titular
    tipo_empresa                    TEXT,                -- Companhia / Controladora / Controlada
    empresa                         TEXT,                -- nome/razão social do titular
    tipo_cargo                      TEXT,                -- Controlador ou Vinculado / Conselho de Administração /
                                                         -- Diretor / Conselho Fiscal / Órgão Estatutário

    -- O QUE aconteceu
    tipo_movimentacao               TEXT,                -- "Saldo Inicial" / "Saldo Final" / "Compra à vista" /
                                                         -- "Venda à vista" / "Posse" / "Desligamento/saída" /
                                                         -- "Doação (donatário)" / "Subscrição" / etc.
    descricao_movimentacao          TEXT,                -- texto livre (ex.: "Divórcio", "Herança")
    tipo_operacao                   TEXT,                -- Crédito / Débito (entrada/saída)

    -- ATIVO negociado
    tipo_ativo                      TEXT,                -- Ações / Debêntures / Opções / Units / BDR / etc.
    caracteristica_vm               TEXT,                -- ON / PN / PNA / UNT / emissão, etc.

    -- contraparte (corretora)
    intermediario                   TEXT,

    -- QUANDO e QUANTO
    data_movimentacao               TEXT,                -- NULL para saldos
    quantidade                      REAL,                -- nº de papéis (positivo; sinal está em tipo_operacao)
    preco_unitario                  REAL,                -- R$ por papel
    volume                          REAL,                -- R$ total (geralmente qtd * preço)

    -- ---------------------------------------------------------------------------
    -- Classificação semântica (derivada de tipo_empresa + tipo_cargo)
    --
    --   'treasury'      -> Companhia reportando operações de tesouraria
    --                      (Tipo_Empresa='Companhia' + Tipo_Cargo='Controlador ou Vinculado')
    --                      É o buyback real: a empresa comprando/vendendo suas próprias ações.
    --
    --   'insider'       -> Administrador individual negociando ações da companhia
    --                      (Tipo_Empresa='Companhia' + Tipo_Cargo ≠ Controlador)
    --                      Ex: Diretor, Conselho de Administração, Conselho Fiscal.
    --
    --   'subsidiary'    -> Controlada reportando posição do grupo
    --                      (Tipo_Empresa='Controlada')
    -- ---------------------------------------------------------------------------
    qualificacao                    TEXT,

    -- deduplicação
    natural_key                     TEXT    NOT NULL UNIQUE,
    ingested_at                     TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_vlmo_cnpj_mes    ON vlmo_entries(cnpj_digits, data_referencia);
CREATE INDEX IF NOT EXISTS idx_vlmo_mov_real    ON vlmo_entries(cnpj_digits, tipo_movimentacao);
CREATE INDEX IF NOT EXISTS idx_vlmo_cargo       ON vlmo_entries(tipo_cargo);
CREATE INDEX IF NOT EXISTS idx_vlmo_qualif      ON vlmo_entries(qualificacao);


-- ---------------------------------------------------------------------------
-- VLMO: metadados das entregas (arquivo vlmo_cia_aberta_AAAA.csv — sem _con_)
-- Útil para rastrear reapresentações e linkar o PDF original na CVM.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS vlmo_filings (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    cnpj_digits            TEXT    NOT NULL,
    nome_companhia         TEXT,
    data_referencia        TEXT    NOT NULL,    -- mês de competência
    data_entrega           TEXT,                -- quando foi entregue à CVM
    versao                 INTEGER,
    codigo_cvm             TEXT,
    categoria              TEXT,
    tipo                   TEXT,
    tipo_apresentacao      TEXT,                -- AP / RE - Reapresentação Espontânea / etc.
    motivo_reapresentacao  TEXT,
    protocolo_entrega      TEXT,
    link_download          TEXT,
    natural_key            TEXT    NOT NULL UNIQUE,
    ingested_at            TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_filings_cnpj ON vlmo_filings(cnpj_digits, data_referencia);


-- ---------------------------------------------------------------------------
-- RECOMPRA: programa principal
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS buyback_programs (
    id                               INTEGER PRIMARY KEY AUTOINCREMENT,
    cnpj_digits                      TEXT    NOT NULL,
    nome_companhia                   TEXT,
    id_programa                      TEXT    NOT NULL,   -- identificador único do programa na CVM
    quantidade_acoes_ordinarias      REAL,
    quantidade_acoes_preferenciais   REAL,
    finalidade_compra                TEXT,
    motivo                           TEXT,
    data_deliberacao                 TEXT,
    data_final_prazo                 TEXT,
    situacao                         TEXT,                -- Em Andamento / Encerrado / Cancelado
    tipo_operacao                    TEXT,
    natural_key                      TEXT    NOT NULL UNIQUE,
    ingested_at                      TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_buyback_cnpj ON buyback_programs(cnpj_digits);
CREATE INDEX IF NOT EXISTS idx_buyback_prog ON buyback_programs(id_programa);


-- ---------------------------------------------------------------------------
-- RECOMPRA: quantidades executadas por classe de ação
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS buyback_quantities (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    id_programa              TEXT    NOT NULL,
    classe_acao              TEXT,
    tipo_acao                TEXT,
    quantidade_circulacao    REAL,
    quantidade_operacao      REAL,                         -- qtd efetivamente operada
    natural_key              TEXT    NOT NULL UNIQUE,
    ingested_at              TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_buyback_qty_prog ON buyback_quantities(id_programa);


-- ---------------------------------------------------------------------------
-- RECOMPRA: intermediários contratados
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS buyback_intermediaries (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    id_programa           TEXT    NOT NULL,
    intermediario         TEXT,
    cnpj_intermediario    TEXT,
    natural_key           TEXT    NOT NULL UNIQUE,
    ingested_at           TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_buyback_int_prog ON buyback_intermediaries(id_programa);


-- ---------------------------------------------------------------------------
-- Auditoria de execuções
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ingestion_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at       TEXT NOT NULL DEFAULT (datetime('now')),
    source       TEXT NOT NULL,
    rows_seen    INTEGER,
    rows_new     INTEGER,
    status       TEXT,
    message      TEXT
);


-- ---------------------------------------------------------------------------
-- VIEWS: consultas prontas
-- ---------------------------------------------------------------------------

-- Apenas OPERAÇÕES REAIS de INSIDERS (Diretores, Conselho, etc.)
-- Exclui saldos e exclui tesouraria (qualificacao = 'treasury')
DROP VIEW IF EXISTS v_insider_trades;
CREATE VIEW v_insider_trades AS
SELECT
    c.ticker,
    e.nome_companhia,
    e.data_referencia                                 AS mes_competencia,
    e.data_movimentacao,
    e.tipo_empresa,
    e.tipo_cargo,
    e.empresa,
    e.tipo_movimentacao,
    e.descricao_movimentacao,
    e.tipo_operacao,
    e.tipo_ativo,
    e.caracteristica_vm,
    e.intermediario,
    e.quantidade,
    e.preco_unitario,
    e.volume,
    e.qualificacao,
    CASE WHEN e.tipo_operacao = 'Débito'
         THEN -COALESCE(e.quantidade, 0)
         ELSE  COALESCE(e.quantidade, 0)
    END                                               AS quantidade_assinada
FROM vlmo_entries e
JOIN companies c ON c.cnpj_digits = e.cnpj_digits
WHERE e.tipo_movimentacao NOT IN ('Saldo Inicial', 'Saldo Final')
  AND e.qualificacao = 'insider';


-- Operações reais de TESOURARIA (buyback — a empresa comprando/vendendo suas próprias ações)
DROP VIEW IF EXISTS v_treasury_trades;
CREATE VIEW v_treasury_trades AS
SELECT
    c.ticker,
    e.nome_companhia,
    e.data_referencia                                 AS mes_competencia,
    e.data_movimentacao,
    e.tipo_movimentacao,
    e.tipo_ativo,
    e.caracteristica_vm,
    e.intermediario,
    e.quantidade,
    e.preco_unitario,
    e.volume,
    CASE WHEN e.tipo_operacao = 'Débito'
         THEN -COALESCE(e.quantidade, 0)
         ELSE  COALESCE(e.quantidade, 0)
    END                                               AS quantidade_assinada
FROM vlmo_entries e
JOIN companies c ON c.cnpj_digits = e.cnpj_digits
WHERE e.tipo_movimentacao NOT IN ('Saldo Inicial', 'Saldo Final')
  AND e.qualificacao = 'treasury'
  AND e.tipo_ativo   = 'Ações';


-- Saldos FINAIS por (mês, cargo, ativo) — útil pra ver como a posição evoluiu
DROP VIEW IF EXISTS v_monthly_balances;
CREATE VIEW v_monthly_balances AS
SELECT
    c.ticker,
    e.nome_companhia,
    e.data_referencia           AS mes_competencia,
    e.tipo_empresa,
    e.tipo_cargo,
    e.tipo_ativo,
    e.caracteristica_vm,
    SUM(e.quantidade)           AS saldo_final_consolidado
FROM vlmo_entries e
JOIN companies c ON c.cnpj_digits = e.cnpj_digits
WHERE e.tipo_movimentacao = 'Saldo Final'
GROUP BY c.ticker, e.nome_companhia, e.data_referencia,
         e.tipo_empresa, e.tipo_cargo, e.tipo_ativo, e.caracteristica_vm;


-- Compras e vendas agregadas por mês / cargo (só à vista e à termo, em ações)
DROP VIEW IF EXISTS v_net_trading_monthly;
CREATE VIEW v_net_trading_monthly AS
SELECT
    c.ticker,
    e.nome_companhia,
    e.data_referencia           AS mes_competencia,
    e.tipo_cargo,
    e.caracteristica_vm,
    SUM(CASE WHEN e.tipo_movimentacao LIKE 'Compra%'
             THEN e.quantidade ELSE 0 END)     AS qtd_compra,
    SUM(CASE WHEN e.tipo_movimentacao LIKE 'Venda%'
             THEN e.quantidade ELSE 0 END)     AS qtd_venda,
    SUM(CASE WHEN e.tipo_movimentacao LIKE 'Compra%' THEN e.quantidade
             WHEN e.tipo_movimentacao LIKE 'Venda%'  THEN -e.quantidade
             ELSE 0 END)                       AS qtd_liquida,
    SUM(CASE WHEN e.tipo_movimentacao LIKE 'Compra%' OR e.tipo_movimentacao LIKE 'Venda%'
             THEN COALESCE(e.volume, e.quantidade * e.preco_unitario) END) AS volume_total_brl,
    COUNT(*)                                   AS n_operacoes
FROM vlmo_entries e
JOIN companies c ON c.cnpj_digits = e.cnpj_digits
WHERE e.tipo_ativo = 'Ações'
  AND (e.tipo_movimentacao LIKE 'Compra%' OR e.tipo_movimentacao LIKE 'Venda%')
GROUP BY c.ticker, e.nome_companhia, e.data_referencia, e.tipo_cargo, e.caracteristica_vm;


-- Programas de recompra, com % executado por classe
DROP VIEW IF EXISTS v_buyback_status;
CREATE VIEW v_buyback_status AS
SELECT
    c.ticker,
    p.nome_companhia,
    p.id_programa,
    p.data_deliberacao,
    p.data_final_prazo,
    p.situacao,
    p.finalidade_compra,
    p.tipo_operacao,
    p.quantidade_acoes_ordinarias,
    p.quantidade_acoes_preferenciais,
    q.classe_acao,
    q.tipo_acao,
    q.quantidade_circulacao,
    q.quantidade_operacao,
    CASE WHEN COALESCE(p.quantidade_acoes_ordinarias, 0)
            + COALESCE(p.quantidade_acoes_preferenciais, 0) > 0
         THEN ROUND(100.0 * COALESCE(q.quantidade_operacao, 0) /
              (COALESCE(p.quantidade_acoes_ordinarias, 0) +
               COALESCE(p.quantidade_acoes_preferenciais, 0)), 2)
    END                                                   AS pct_executado
FROM buyback_programs p
JOIN companies c ON c.cnpj_digits = p.cnpj_digits
LEFT JOIN buyback_quantities q ON q.id_programa = p.id_programa;
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
        # Migration: adiciona coluna qualificacao se ainda não existir
        # (bancos criados antes desta versão não têm a coluna)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(vlmo_entries)").fetchall()}
        if "qualificacao" not in cols:
            log.info("Migration: adicionando coluna qualificacao a vlmo_entries")
            conn.execute("ALTER TABLE vlmo_entries ADD COLUMN qualificacao TEXT")
            conn.execute("""
                UPDATE vlmo_entries SET qualificacao = CASE
                    WHEN tipo_empresa = 'Controlada'               THEN 'subsidiary'
                    WHEN tipo_cargo   = 'Controlador ou Vinculado' THEN 'treasury'
                    ELSE                                                'insider'
                END
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_vlmo_qualif ON vlmo_entries(qualificacao)"
            )
            log.info("Migration qualificacao: concluida")


def sync_companies() -> None:
    with db_conn() as conn:
        for ticker, cnpj in TICKERS.items():
            conn.execute(
                """
                INSERT INTO companies (cnpj_digits, ticker, updated_at)
                VALUES (?, ?, datetime('now'))
                ON CONFLICT(cnpj_digits) DO UPDATE SET
                    ticker = excluded.ticker,
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
        if "," in s:
            return float(s.replace(".", "").replace(",", "."))
        return float(s)
    except (ValueError, TypeError):
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

def _nk_vlmo_entry(row: pd.Series) -> str:
    """Chave natural para uma linha do arquivo _con_."""
    parts = [
        only_digits(row.get("CNPJ_Companhia", "")),
        str(row.get("Data_Referencia", "")),
        str(row.get("Versao", "")),
        str(row.get("Tipo_Empresa", "")),
        str(row.get("Empresa", "")),
        str(row.get("Tipo_Cargo", "")),
        str(row.get("Tipo_Movimentacao", "")),
        str(row.get("Descricao_Movimentacao", "")),
        str(row.get("Tipo_Ativo", "")),
        str(row.get("Caracteristica_Valor_Mobiliario", "")),
        str(row.get("Intermediario", "")),
        str(row.get("Data_Movimentacao", "")),
        str(row.get("Quantidade", "")),
        str(row.get("Preco_Unitario", "")),
        str(row.get("Volume", "")),
    ]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def _nk_vlmo_filing(row: pd.Series) -> str:
    return hashlib.sha1(
        str(row.get("Protocolo_Entrega", "")).encode("utf-8")
    ).hexdigest()


def _ingest_vlmo_content(df: pd.DataFrame, cnpjs: set[str]) -> int:
    """Ingere o CSV _con_ (conteúdo). Retorna linhas novas."""
    df = df.copy()
    df["__cnpj_d"] = df["CNPJ_Companhia"].map(only_digits)
    df = df[df["__cnpj_d"].isin(cnpjs)]
    if df.empty:
        return 0

    df["Data_Referencia"]    = df["Data_Referencia"].map(parse_date)
    df["Data_Movimentacao"]  = df["Data_Movimentacao"].map(parse_date)

    rows_new = 0
    with db_conn() as conn:
        for _, row in df.iterrows():
            nk = _nk_vlmo_entry(row)

            # Classificação semântica derivada dos campos CVM:
            #   treasury  -> Companhia reportando tesouraria (Controlador ou Vinculado)
            #   insider   -> Administrador individual (Diretor, Conselho, etc.)
            #   subsidiary-> Controlada reportando posição do grupo
            tipo_empresa = str(row.get("Tipo_Empresa") or "")
            tipo_cargo   = str(row.get("Tipo_Cargo")   or "")
            if tipo_empresa == "Controlada":
                qualificacao = "subsidiary"
            elif tipo_cargo == "Controlador ou Vinculado":
                qualificacao = "treasury"
            else:
                qualificacao = "insider"

            cur = conn.execute(
                """
                INSERT OR IGNORE INTO vlmo_entries (
                    cnpj_digits, nome_companhia, data_referencia, versao,
                    tipo_empresa, empresa, tipo_cargo,
                    tipo_movimentacao, descricao_movimentacao, tipo_operacao,
                    tipo_ativo, caracteristica_vm, intermediario,
                    data_movimentacao, quantidade, preco_unitario, volume,
                    qualificacao, natural_key
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    row["__cnpj_d"],
                    row.get("Nome_Companhia"),
                    row.get("Data_Referencia"),
                    int(row["Versao"]) if pd.notna(row.get("Versao")) else None,
                    row.get("Tipo_Empresa"),
                    row.get("Empresa"),
                    row.get("Tipo_Cargo"),
                    row.get("Tipo_Movimentacao"),
                    row.get("Descricao_Movimentacao"),
                    row.get("Tipo_Operacao"),
                    row.get("Tipo_Ativo"),
                    row.get("Caracteristica_Valor_Mobiliario"),
                    row.get("Intermediario"),
                    row.get("Data_Movimentacao"),
                    safe_float(row.get("Quantidade")),
                    safe_float(row.get("Preco_Unitario")),
                    safe_float(row.get("Volume")),
                    qualificacao,
                    nk,
                ),
            )
            rows_new += cur.rowcount
    return rows_new


def _ingest_vlmo_index(df: pd.DataFrame, cnpjs: set[str]) -> int:
    """Ingere o CSV índice (sem _con_). Retorna linhas novas."""
    df = df.copy()
    df["__cnpj_d"] = df["CNPJ_Companhia"].map(only_digits)
    df = df[df["__cnpj_d"].isin(cnpjs)]
    if df.empty:
        return 0

    df["Data_Referencia"] = df["Data_Referencia"].map(parse_date)
    df["Data_Entrega"]    = df["Data_Entrega"].map(parse_date)

    rows_new = 0
    with db_conn() as conn:
        for _, row in df.iterrows():
            nk = _nk_vlmo_filing(row)
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO vlmo_filings (
                    cnpj_digits, nome_companhia, data_referencia, data_entrega,
                    versao, codigo_cvm, categoria, tipo, tipo_apresentacao,
                    motivo_reapresentacao, protocolo_entrega, link_download,
                    natural_key
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    row["__cnpj_d"],
                    row.get("Nome_Companhia"),
                    row.get("Data_Referencia"),
                    row.get("Data_Entrega"),
                    int(row["Versao"]) if pd.notna(row.get("Versao")) else None,
                    row.get("Codigo_CVM"),
                    row.get("Categoria"),
                    row.get("Tipo"),
                    row.get("Tipo_Apresentacao"),
                    row.get("Motivo_Reapresentacao"),
                    row.get("Protocolo_Entrega"),
                    row.get("Link_Download"),
                    nk,
                ),
            )
            rows_new += cur.rowcount
    return rows_new


def ingest_vlmo_year(year: int, cnpjs: set[str]) -> dict:
    url = VLMO_URL.format(year=year)
    log.info("GET %s", url)
    try:
        r = requests.get(url, headers=HTTP_HEADERS, timeout=120)
        if r.status_code == 404:
            log.info("Ano %d ainda não publicado. Pulando.", year)
            log_run(f"vlmo_{year}", 0, 0, "skipped", "404")
            return {"entries_new": 0, "filings_new": 0}
        r.raise_for_status()
    except requests.RequestException as e:
        log.error("Falha baixando VLMO %d: %s", year, e)
        log_run(f"vlmo_{year}", 0, 0, "error", str(e))
        return {"entries_new": 0, "filings_new": 0, "error": str(e)}

    zf = zipfile.ZipFile(io.BytesIO(r.content))

    entries_new = 0
    filings_new = 0

    for name in zf.namelist():
        if not name.endswith(".csv"):
            continue
        log.info("  Lendo %s", name)
        with zf.open(name) as f:
            df = pd.read_csv(f, sep=";", encoding="latin-1", dtype=str)

        if "_con_" in name:
            entries_new += _ingest_vlmo_content(df, cnpjs)
        else:
            filings_new += _ingest_vlmo_index(df, cnpjs)

    result = {"entries_new": entries_new, "filings_new": filings_new}
    log_run(f"vlmo_{year}", entries_new + filings_new, entries_new + filings_new,
            "ok", str(result))
    log.info("VLMO %d: %s", year, result)
    return result


# ============================================================================
# INGESTÃO — RECOMPRAS (3 arquivos)
# ============================================================================

def _nk(*parts) -> str:
    return hashlib.sha1("|".join(str(p or "") for p in parts).encode("utf-8")).hexdigest()


def _ingest_buyback_programs(df: pd.DataFrame, cnpjs: set[str]) -> int:
    df = df.copy()
    df["__cnpj_d"] = df["CNPJ_Companhia"].map(only_digits)
    df = df[df["__cnpj_d"].isin(cnpjs)]
    if df.empty:
        return 0

    rows_new = 0
    with db_conn() as conn:
        for _, row in df.iterrows():
            id_programa = str(row.get("ID_Programa", "") or "")
            nk = _nk(row["__cnpj_d"], id_programa, row.get("Data_Deliberacao"))
            cur = conn.execute(
                """
                INSERT INTO buyback_programs (
                    cnpj_digits, nome_companhia, id_programa,
                    quantidade_acoes_ordinarias, quantidade_acoes_preferenciais,
                    finalidade_compra, motivo, data_deliberacao, data_final_prazo,
                    situacao, tipo_operacao, natural_key
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(natural_key) DO UPDATE SET
                    situacao         = excluded.situacao,
                    data_final_prazo = COALESCE(excluded.data_final_prazo, buyback_programs.data_final_prazo),
                    motivo           = COALESCE(excluded.motivo, buyback_programs.motivo),
                    ingested_at      = datetime('now')
                """,
                (
                    row["__cnpj_d"],
                    row.get("Nome_Companhia"),
                    id_programa,
                    safe_float(row.get("Quantidade_Acoes_Ordinarias")),
                    safe_float(row.get("Quantidade_Acoes_Preferenciais")),
                    row.get("Finalidade_Compra"),
                    row.get("Motivo"),
                    parse_date(row.get("Data_Deliberacao")),
                    parse_date(row.get("Data_Final_Prazo")),
                    row.get("Situacao"),
                    row.get("Tipo_Operacao"),
                    nk,
                ),
            )
            if cur.rowcount > 0:
                rows_new += 1
    return rows_new


def _ingest_buyback_quantities(df: pd.DataFrame, known_programs: set[str]) -> int:
    """Ingere quantidades só para programas que já monitoramos."""
    df = df.copy()
    df["__id"] = df["ID_Programa"].astype(str).str.strip()
    df = df[df["__id"].isin(known_programs)]
    if df.empty:
        return 0

    rows_new = 0
    with db_conn() as conn:
        for _, row in df.iterrows():
            nk = _nk(row["__id"], row.get("Classe_Acao"), row.get("Tipo_Acao"))
            cur = conn.execute(
                """
                INSERT INTO buyback_quantities (
                    id_programa, classe_acao, tipo_acao,
                    quantidade_circulacao, quantidade_operacao, natural_key
                ) VALUES (?,?,?,?,?,?)
                ON CONFLICT(natural_key) DO UPDATE SET
                    quantidade_circulacao = excluded.quantidade_circulacao,
                    quantidade_operacao   = excluded.quantidade_operacao,
                    ingested_at           = datetime('now')
                """,
                (
                    row["__id"],
                    row.get("Classe_Acao"),
                    row.get("Tipo_Acao"),
                    safe_float(row.get("Quantidade_Circulacao")),
                    safe_float(row.get("Quantidade_Operacao")),
                    nk,
                ),
            )
            if cur.rowcount > 0:
                rows_new += 1
    return rows_new


def _ingest_buyback_intermediaries(df: pd.DataFrame, known_programs: set[str]) -> int:
    df = df.copy()
    df["__id"] = df["ID_Programa"].astype(str).str.strip()
    df = df[df["__id"].isin(known_programs)]
    if df.empty:
        return 0

    rows_new = 0
    with db_conn() as conn:
        for _, row in df.iterrows():
            nk = _nk(row["__id"], row.get("CNPJ_Intermediario"), row.get("Intermediario"))
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO buyback_intermediaries (
                    id_programa, intermediario, cnpj_intermediario, natural_key
                ) VALUES (?,?,?,?)
                """,
                (
                    row["__id"],
                    row.get("Intermediario"),
                    row.get("CNPJ_Intermediario"),
                    nk,
                ),
            )
            rows_new += cur.rowcount
    return rows_new


def ingest_recompras(cnpjs: set[str]) -> dict:
    log.info("GET %s", RECOMPRA_URL)
    try:
        r = requests.get(RECOMPRA_URL, headers=HTTP_HEADERS, timeout=120)
        r.raise_for_status()
    except requests.RequestException as e:
        log.error("Falha baixando recompras: %s", e)
        log_run("recompra", 0, 0, "error", str(e))
        return {"error": str(e)}

    zf = zipfile.ZipFile(io.BytesIO(r.content))

    # Lê todos os CSVs primeiro (precisamos conhecer os programas antes das qtds/intermediários)
    csvs: dict[str, pd.DataFrame] = {}
    for name in zf.namelist():
        if name.endswith(".csv"):
            with zf.open(name) as f:
                csvs[name] = pd.read_csv(f, sep=";", encoding="latin-1", dtype=str)

    programs_new = 0
    quantities_new = 0
    intermediaries_new = 0

    # 1) Programas principais (têm CNPJ_Companhia)
    for name, df in csvs.items():
        if "CNPJ_Companhia" in df.columns and "ID_Programa" in df.columns:
            log.info("  Ingerindo programas: %s", name)
            programs_new += _ingest_buyback_programs(df, cnpjs)

    # Agora temos os id_programa das nossas companhias no DB — filtramos os outros arquivos por eles
    with db_conn() as conn:
        rows = conn.execute("SELECT DISTINCT id_programa FROM buyback_programs").fetchall()
        known_programs = {r["id_programa"] for r in rows if r["id_programa"]}
    log.info("  %d programas conhecidos no DB", len(known_programs))

    # 2) Quantidades (tem ID_Programa + Classe_Acao + Quantidade_*)
    for name, df in csvs.items():
        if "CNPJ_Companhia" in df.columns:
            continue  # é o arquivo principal
        if "Quantidade_Operacao" in df.columns or "Quantidade_Circulacao" in df.columns:
            log.info("  Ingerindo quantidades: %s", name)
            quantities_new += _ingest_buyback_quantities(df, known_programs)

    # 3) Intermediários (tem ID_Programa + Intermediario + CNPJ_Intermediario)
    for name, df in csvs.items():
        if "CNPJ_Companhia" in df.columns:
            continue
        if "CNPJ_Intermediario" in df.columns or "Intermediario" in df.columns:
            log.info("  Ingerindo intermediários: %s", name)
            intermediaries_new += _ingest_buyback_intermediaries(df, known_programs)

    result = {
        "programs_new": programs_new,
        "quantities_new": quantities_new,
        "intermediaries_new": intermediaries_new,
    }
    total = sum(result.values())
    log_run("recompra", total, total, "ok", str(result))
    log.info("Recompras: %s", result)
    return result


# ============================================================================
# MAIN
# ============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="CVM Buybacks monitor")
    parser.add_argument("--bootstrap", action="store_true",
                        help="Primeira carga: puxa 5 anos.")
    parser.add_argument("--years", type=int, default=2,
                        help="Anos para trás (default: 2 — ano atual e anterior).")
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
