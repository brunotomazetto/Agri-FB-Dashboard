"""
CVM Buybacks v2 — monitor de recompras e negociações de insiders.

Fontes:
  1. IPE Individual ("Posição Individual - Cia, Controladas e Coligadas")
     — PDF por empresa por mês, contém Saldo Inicial, Saldo Final,
       movimentações com data exata, e separação correta de Tesouraria
       vs. Controlada vs. Coligada.
     — Índice: ipe_cia_aberta_{year}.csv (download direto dos dados abertos CVM)
     — Documentos: links na coluna Link_Download do índice

  2. Recompras (programas formais de recompra)
     — https://dados.cvm.gov.br/dados/CIA_ABERTA/EVENTOS/RECOMPRA_ACOES/DADOS/

  Modo histórico (--bootstrap):
     Varre anos de 2022 até hoje, baixa todos os IPEs individuais.
     Pode demorar ~5-10 min para 13 tickers × 4 anos × 12 meses.

  Modo incremental (default):
     Baixa apenas os IPEs do mês atual e do mês anterior (para reapresentações).

Execução:
  python cvm_buybacks.py              # incremental
  python cvm_buybacks.py --bootstrap  # histórico completo desde 2022
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import re
import sqlite3
import time
import zipfile
from collections import defaultdict
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Iterator

import pandas as pd
import requests

# ============================================================================
# CONFIGURAÇÃO
# ============================================================================

TICKERS: dict[str, str] = {
    "MBRF3": "03853896000140",
    "BEEF3": "67620377000114",
    "JBSS3": "02916265000160",
    "ABEV3": "07526557000100",
    "MDIA3": "07206816000115",
    "CAML3": "64904295000103",
    "SLCE3": "89096457000155",
    "TTEN3": "94813102000170",
    "SMTO3": "51466860000156",
    "JALL3": "02635522000195",
    "SOJA3": "10807374000177",
    "VITT3": "45365558000109",
    "AGRO3": "07628528000159",
}

SCRIPT_DIR = Path(__file__).resolve().parent
DB_PATH    = SCRIPT_DIR / "cvm_buybacks.db"

# URL base do índice IPE por ano
IPE_IDX_URL = (
    "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/IPE/DADOS/"
    "ipe_cia_aberta_{year}.zip"
)

# URL de recompras (programas formais)
RECOMPRA_URL = (
    "https://dados.cvm.gov.br/dados/CIA_ABERTA/EVENTOS/RECOMPRA_ACOES/DADOS/"
    "cia_aberta_recompra_acoes.zip"
)

HTTP_HEADERS = {"User-Agent": "cvm-buybacks-monitor/3.0 (github-actions)"}
BOOTSTRAP_START_YEAR = 2022

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("cvm")

# ============================================================================
# SCHEMA
# ============================================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    cnpj_digits  TEXT PRIMARY KEY,
    ticker       TEXT NOT NULL,
    nome         TEXT,
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_companies_ticker ON companies(ticker);

-- Posições e movimentações do IPE Individual
-- qualificacao: 'treasury' | 'subsidiary' | 'affiliated' | 'other'
-- tipo_movimentacao: 'Saldo Inicial' | 'Saldo Final' | 'Compra à vista' | etc.
CREATE TABLE IF NOT EXISTS ipe_entries (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    cnpj_digits         TEXT    NOT NULL,
    data_referencia     TEXT    NOT NULL,   -- YYYY-MM-01
    versao              INTEGER NOT NULL DEFAULT 1,
    qualificacao        TEXT    NOT NULL,   -- treasury / subsidiary / affiliated / other
    nome_entidade       TEXT,               -- ex: "AMBEV S.A.", "Ambev Luxembourg"
    tipo_ativo          TEXT,               -- "Ações", "Derivativos", "Outros"
    caracteristica      TEXT,               -- "ON", "PN", "SWAP REFERENCIADO..."
    tipo_movimentacao   TEXT    NOT NULL,   -- Saldo Inicial | Saldo Final | Compra à vista | ...
    intermediario       TEXT,
    dia                 INTEGER,            -- dia do mês (1-31), NULL para saldos
    data_movimentacao   TEXT,               -- YYYY-MM-DD reconstituída de dia + data_referencia
    quantidade          REAL,
    preco_unitario      REAL,
    volume              REAL,
    natural_key         TEXT    UNIQUE      -- SHA-1 para deduplicação
);
CREATE INDEX IF NOT EXISTS idx_ipe_cnpj_ref ON ipe_entries(cnpj_digits, data_referencia);
CREATE INDEX IF NOT EXISTS idx_ipe_qual     ON ipe_entries(qualificacao);
CREATE INDEX IF NOT EXISTS idx_ipe_movm     ON ipe_entries(tipo_movimentacao);

-- Programas formais de recompra (inalterado da v1)
CREATE TABLE IF NOT EXISTS buyback_programs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    cnpj_digits         TEXT    NOT NULL,
    id_programa         TEXT    NOT NULL,
    data_deliberacao    TEXT,
    data_final_prazo    TEXT,
    situacao            TEXT,
    qtd_autorizada      REAL,
    qtd_acoes_em_circ   REAL,
    destinacao          TEXT,
    natural_key         TEXT    UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_bp_cnpj ON buyback_programs(cnpj_digits);

CREATE TABLE IF NOT EXISTS buyback_quantities (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    cnpj_digits         TEXT    NOT NULL,
    id_programa         TEXT    NOT NULL,
    data_referencia     TEXT,
    descricao_valor_mob TEXT,
    qtd_adquirida       REAL,
    qtd_alienada        REAL,
    qtd_cancelada        REAL,
    natural_key         TEXT    UNIQUE
);

-- ============================================================
-- Formulário Consolidado — Posição de Grupos de Pessoas Ligadas
-- Fonte: IPE Tipo = "Posição Consolidada"
-- Um PDF por empresa por mês, com uma PÁGINA por grupo:
--   Controlador | Conselho Administração | Diretoria | Conselho Fiscal | Órgãos Técnicos
-- Mesmo layout de tabela do Formulário Individual:
--   Saldo Inicial / Movimentações / Saldo Final por grupo
-- ============================================================
CREATE TABLE IF NOT EXISTS consolidated_positions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    cnpj_digits         TEXT    NOT NULL,
    data_referencia     TEXT    NOT NULL,   -- YYYY-MM-01
    versao              INTEGER NOT NULL DEFAULT 1,
    -- Grupo (detectado pelo checkbox '( X )' na página)
    grupo               TEXT    NOT NULL,  -- Controlador | CA | Diretoria | CF | Orgaos
    -- Campos idênticos ao ipe_entries
    tipo_ativo          TEXT,              -- Ações | Derivativos
    caracteristica      TEXT,              -- ON | PN
    tipo_movimentacao   TEXT    NOT NULL,  -- Saldo Inicial | Saldo Final | Compra à vista | ...
    intermediario       TEXT,
    dia                 INTEGER,
    data_movimentacao   TEXT,              -- YYYY-MM-DD
    quantidade          REAL,
    preco_unitario      REAL,
    volume              REAL,
    natural_key         TEXT    UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_cp_cnpj_ref ON consolidated_positions(cnpj_digits, data_referencia);
CREATE INDEX IF NOT EXISTS idx_cp_grupo    ON consolidated_positions(grupo);
CREATE INDEX IF NOT EXISTS idx_cp_movm     ON consolidated_positions(tipo_movimentacao);

-- Views úteis
CREATE VIEW IF NOT EXISTS v_treasury_trades AS
SELECT
    c.ticker,
    e.data_referencia    AS mes_competencia,
    e.data_movimentacao,
    e.dia,
    e.tipo_ativo,
    e.caracteristica,
    e.tipo_movimentacao,
    e.intermediario,
    e.quantidade,
    e.preco_unitario,
    e.volume
FROM ipe_entries e
JOIN companies c ON c.cnpj_digits = e.cnpj_digits
WHERE e.qualificacao = 'treasury'
  AND e.tipo_movimentacao NOT IN ('Saldo Inicial', 'Saldo Final')
  AND e.tipo_ativo = 'Ações';

CREATE VIEW IF NOT EXISTS v_treasury_balances AS
SELECT
    c.ticker,
    e.data_referencia,
    e.tipo_movimentacao,    -- 'Saldo Inicial' ou 'Saldo Final'
    e.tipo_ativo,
    e.caracteristica,
    e.quantidade
FROM ipe_entries e
JOIN companies c ON c.cnpj_digits = e.cnpj_digits
WHERE e.qualificacao = 'treasury'
  AND e.tipo_movimentacao IN ('Saldo Inicial', 'Saldo Final');
"""

# ============================================================================
# BANCO
# ============================================================================

@contextmanager
def db_conn() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
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
        # executescript executa o SQL completo incluindo VIEWs com subconsultas
        # mais robusto que split(";") que quebra em statements multi-linha
        conn.executescript(SCHEMA)
        # upsert companies
        for ticker, cnpj in TICKERS.items():
            conn.execute(
                "INSERT INTO companies(cnpj_digits, ticker) VALUES(?,?) "
                "ON CONFLICT(cnpj_digits) DO UPDATE SET ticker=excluded.ticker",
                (cnpj, ticker),
            )


# ============================================================================
# HTTP
# ============================================================================

def fetch(url: str, retries: int = 3, timeout: int = 60) -> bytes:
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=HTTP_HEADERS, timeout=timeout)
            r.raise_for_status()
            return r.content
        except requests.RequestException as e:
            if attempt < retries - 1:
                wait = 2 ** attempt
                log.warning("  retrying in %ds (%s)", wait, e)
                time.sleep(wait)
            else:
                raise


# ============================================================================
# IPE ÍNDICE — descobrir links dos documentos individuais
# ============================================================================

def _parse_num(s: str | None) -> float | None:
    if not s:
        return None
    s = str(s).strip().replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def load_ipe_index(year: int) -> pd.DataFrame:
    """Baixa o ZIP do índice IPE e retorna o CSV como DataFrame."""
    url = IPE_IDX_URL.format(year=year)
    log.info("Baixando índice IPE %d …", year)
    data = fetch(url)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        # arquivo principal é ipe_cia_aberta_YYYY.csv
        names = [n for n in z.namelist() if n.endswith(".csv")]
        csv_name = next(n for n in names if "ipe_cia_aberta" in n and "_con_" not in n)
        with z.open(csv_name) as f:
            df = pd.read_csv(f, sep=";", encoding="latin-1", dtype=str)
    return df


def filter_individual_links(
    df: pd.DataFrame,
    cnpj_set: set[str],
) -> pd.DataFrame:
    """Filtra Posição Individual (tesouraria) para os CNPJs monitorados."""
    df = df.copy()
    df["cnpj_clean"] = df["CNPJ_Companhia"].str.replace(r"\D", "", regex=True)
    mask = (
        df["cnpj_clean"].isin(cnpj_set)
        & df["Tipo"].str.contains("Individual", na=False)
    )
    result = df[mask].copy()
    # Manter apenas versão mais alta por (cnpj, data_referencia)
    result["Versao"] = result["Versao"].astype(int)
    result = (
        result.sort_values("Versao", ascending=False)
        .groupby(["cnpj_clean", "Data_Referencia"], as_index=False)
        .first()
    )
    return result


def filter_consolidated_links(
    df: pd.DataFrame,
    cnpj_set: set[str],
) -> pd.DataFrame:
    """
    Filtra documentos 'Posição Consolidada' do índice IPE.

    Confirmado no portal CVM (imagem VITTIA fev/2026):
      Categoria = "Valores Mobiliários Negociados e Detidos"
      Tipo      = "Posição Consolidada"

    Cada PDF tem uma página por grupo de pessoas ligadas:
      Controlador | Conselho Administração | Diretoria | Conselho Fiscal | Órgãos Técnicos
    """
    df = df.copy()
    df["cnpj_clean"] = df["CNPJ_Companhia"].str.replace(r"\D", "", regex=True)

    tipo_col = df.get("Tipo", pd.Series(dtype=str)).fillna("")

    mask = df["cnpj_clean"].isin(cnpj_set) & tipo_col.str.contains("Consolidada", case=False, na=False)
    result = df[mask].copy()

    if result.empty:
        avail = df[df["cnpj_clean"].isin(cnpj_set)]["Tipo"].dropna().unique().tolist()
        log.warning("  [CONSOLIDADO] Nenhum doc. Tipos disponíveis: %s", avail)
        return result

    result["Versao"] = pd.to_numeric(result["Versao"], errors="coerce").fillna(1).astype(int)
    result = (
        result.sort_values("Versao", ascending=False)
        .groupby(["cnpj_clean", "Data_Referencia"], as_index=False)
        .first()
    )
    return result


# ============================================================================
# PARSER DO PDF IPE INDIVIDUAL
# ============================================================================

def _norm_qty(s: str) -> float | None:
    """'144.870.526' → 144870526.0"""
    if not s:
        return None
    s = s.strip().replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def parse_ipe_pdf(pdf_bytes: bytes, cnpj_digits: str, data_ref: str, versao: int) -> list[dict]:
    """
    Parse do PDF IPE Individual usando posições X das palavras.

    Layout (posições X aproximadas, consistentes nos PDFs CVM):
      x≈42-90    tipo_ativo  (Ações | Derivativos | Outros)
      x≈100-200  característica (ON, PN, ADR, SWAP REFERENCIADO...)
      x≈198-255  intermediário (Safra, Santander, Direto c/ a Cia...)
      x≈251-315  operação (Compra à vista, Entrega de ações...) — pode quebrar linha
      x≈305-345  dia (1-31)
      x≈345-430  quantidade
      x≈425-495  preço
      x≈485-560  volume

    Estratégia para movimentações:
      - Uma linha com DIA (número 1-31 na coluna x≈310) = âncora de uma operação.
      - Linhas sem DIA mas com conteúdo na coluna OP = continuação do fragmento
        de operação da linha anterior.
      - Intermediário fica na linha imediatamente acima ou mesma linha que o dia.
    """
    try:
        import pdfplumber
    except ImportError:
        raise ImportError("pdfplumber not installed — run: pip install pdfplumber")

    from collections import defaultdict

    def make_key(*parts) -> str:
        return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()

    def to_date(dia, ref):
        if not dia:
            return None
        y, m = int(ref[:4]), int(ref[5:7])
        return f"{y:04d}-{m:02d}-{int(dia):02d}"

    def norm_num(s):
        if not s:
            return None
        s = str(s).strip().replace(".", "").replace(",", ".")
        try:
            return float(s)
        except ValueError:
            return None

    # Colunas X (lo, hi)
    C_ATIVO  = (30,  90)
    C_CARACT = (100, 200)
    C_INTERM = (190, 255)
    C_OP     = (245, 322)
    C_DIA    = (305, 348)
    C_QTY    = (345, 435)
    C_PRECO  = (425, 495)
    C_VOL    = (485, 562)
    C_SALDO  = (455, 562)   # coluna quantidade nos saldos (x≈462-546)

    def in_c(x, c): return c[0] <= x <= c[1]

    rows: list[dict] = []
    current_qual = None
    current_nome = None

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        all_words = []
        for page in pdf.pages:
            ws = page.extract_words(x_tolerance=3, y_tolerance=3)
            # Adicionar offset de página para separar páginas
            page_offset = page.bbox[3] * pdf.pages.index(page)
            for w in ws:
                all_words.append({**w, "top": w["top"] + page_offset})

    # Agrupar em linhas físicas por y (bucket de 3px)
    lines_map: dict[int, list] = defaultdict(list)
    for w in all_words:
        y_key = round(w["top"] / 3) * 3
        lines_map[y_key].append(w)

    sorted_ys = sorted(lines_map.keys())

    # ---- Estados de parse ----
    section = None   # None | 'saldo_inicial' | 'movimentacoes' | 'saldo_final'
    # Buffer de linhas físicas para montar uma operação lógica
    op_buffer: list[list] = []  # lista de linhas físicas pertencentes à op atual

    def extract_op_fields(buf: list[list]) -> dict | None:
        """Extrai campos de uma operação a partir de buffer de linhas físicas."""
        if not buf:
            return None

        # Coletar todas as palavras por coluna
        ativo_words  = []
        caract_words = []
        interm_words = []
        op_words     = []
        dia_words    = []
        qty_words    = []
        preco_words  = []
        vol_words    = []

        for line in buf:
            for w in sorted(line, key=lambda x: x["x0"]):
                x = w["x0"]
                t = w["text"]
                if in_c(x, C_ATIVO):   ativo_words.append(t)
                elif in_c(x, C_CARACT):caract_words.append(t)
                if in_c(x, C_INTERM):  interm_words.append(t)
                if in_c(x, C_OP):      op_words.append(t)
                if in_c(x, C_DIA):
                    if re.match(r"^\d{1,2}$", t): dia_words.append(t)
                if in_c(x, C_QTY):     qty_words.append(t)
                if in_c(x, C_PRECO):   preco_words.append(t)
                if in_c(x, C_VOL):     vol_words.append(t)

        if not ativo_words or not dia_words:
            return None

        tipo_ativo = ativo_words[0]
        caract = " ".join(caract_words).strip()
        # Remover fragmentos de operação que foram captados em caract
        caract = re.sub(r"\b(Direto|c/|a|Cia|Corretora|Santander|Safra|BTG|XP|Itaú)\b.*", "", caract).strip()

        # Intermediário: palavras de C_INTERM que não são parte da operação
        op_keywords = {"ENTREGA", "DE", "AÇÕES", "RESTRITAS", "BÔNUS", "BONUS",
                       "Compra", "Venda", "à", "vista", "termo", "DERIVATIVO",
                       "COM", "LIQUIDAÇÃO", "FINANCEIRA", "LIQUIDAÇÃ", "FINANCEIR"}
        interm_clean = [t for t in interm_words if t not in op_keywords]
        interm = " ".join(interm_clean).strip() or None

        # Operação: normalizar
        op_raw = " ".join(op_words)
        op_raw = re.sub(r"ENTREGA\s+DE\s+AÇÕES\s+RESTRITAS", "Entrega de ações restritas", op_raw, flags=re.I)
        op_raw = re.sub(r"ENTREGA\s+DE\s+AÇÕES\s+B[ÔO]NUS", "Entrega de ações bônus", op_raw, flags=re.I)
        op_raw = re.sub(r"Compra\s+à\s+vista", "Compra à vista", op_raw, flags=re.I)
        op_raw = re.sub(r"Venda\s+à\s+vista", "Venda à vista", op_raw, flags=re.I)
        op_raw = re.sub(r"Compra\s+à\s+termo", "Compra à termo", op_raw, flags=re.I)
        op_raw = re.sub(r"Venda\s+à\s+termo", "Venda à termo", op_raw, flags=re.I)
        op_raw = re.sub(r"DERIVATIVO\s+COM\s+LIQUIDAÇÃ[O]\s+FINANCEIR[A]",
                        "Derivativo com liquidação financeira", op_raw, flags=re.I)
        op_raw = " ".join(op_raw.split())

        dia = int(dia_words[0])

        # Quantidade: pegar apenas números com ponto como separador de milhar
        qty_str = next((t for t in qty_words if re.match(r"[\d.]+$", t.replace(",", ""))), None)
        preco_str = next((t for t in preco_words if re.match(r"[\d,]+$", t.replace(".", ""))), None)
        vol_str = next((t for t in vol_words if re.match(r"[\d.,]+$", t)), None)

        return {
            "tipo_ativo": tipo_ativo,
            "caracteristica": caract,
            "intermediario": interm,
            "tipo_movimentacao": op_raw if op_raw else "Operação",
            "dia": dia,
            "quantidade": norm_num(qty_str),
            "preco_unitario": norm_num(preco_str),
            "volume": norm_num(vol_str),
        }

    # Operações canônicas para limpeza final
    CANONICAL_OPS = [
        "Entrega de ações restritas", "Entrega de ações bônus",
        "Compra à vista", "Venda à vista", "Compra à termo", "Venda à termo",
        "Compra", "Venda",
        "Derivativo com liquidação financeira",
        "Outras Entradas", "Outras Saídas",
        "Ações de plano de remuneração", "Units de plano de remuneração",
        "Desligamento/saída", "Posse", "Subscrição",
        "Doação (donatário)", "Doação (doador)",
        "Grupamento", "Desdobramento/bonificação",
        "Devolução de empréstimo (locador)", "Contratação de empréstimo (locador)",
        "Devolução de empréstimo", "Contratação de empréstimo",
        "Exercício de opção de compra", "Exercício de opção",
        "Baixa para plano de remuneração", "Baixa para beneficiários",
        "Lançamento de ações",
    ]

    def clean_op_final(raw: str) -> str:
        """Tenta identificar a operação canônica no texto acumulado."""
        raw_norm = re.sub(r"\s+", " ", raw).strip()
        # Remover números soltos no início (dias que ficaram na string)
        raw_norm = re.sub(r"^\d{1,2}\s+", "", raw_norm).strip()

        # Normalizar fragmentos de operação quebrada
        # Entrega de ações restritas (ordem normal e invertida)
        raw_norm = re.sub(r"AÇÕES\s+RESTRITAS\s+ENTREGA",  "Entrega de ações restritas", raw_norm, flags=re.I)
        raw_norm = re.sub(r"ENTREGA\s+DE\s+AÇÕES\s+RESTRITAS", "Entrega de ações restritas", raw_norm, flags=re.I)
        raw_norm = re.sub(r"DE\s+AÇÕES\s+RESTRITAS.*", "Entrega de ações restritas", raw_norm, flags=re.I)
        raw_norm = re.sub(r"AÇÕES\s+RESTRITAS\b", "Entrega de ações restritas", raw_norm, flags=re.I)
        raw_norm = re.sub(r"ENTREGA\s+DE\s+AÇÕES\s+B[ÔO]NUS", "Entrega de ações bônus", raw_norm, flags=re.I)
        raw_norm = re.sub(r"DE\s+AÇÕES\s+B[ÔO]NUS.*", "Entrega de ações bônus", raw_norm, flags=re.I)
        raw_norm = re.sub(r"DE\s+AÇÕES\s+BONUS.*",     "Entrega de ações bônus", raw_norm, flags=re.I)

        # Options / stock options → "Exercício de opção de compra"
        raw_norm = re.sub(r"\bOPTIONS\s+STOCK\b", "Exercício de opção de compra", raw_norm, flags=re.I)
        raw_norm = re.sub(r"\bSTOCK\s+OPTIONS?\b", "Exercício de opção de compra", raw_norm, flags=re.I)
        raw_norm = re.sub(r"\bOPTIONS?\b",         "Exercício de opção de compra", raw_norm, flags=re.I)

        # Baixa para beneficiários / plano de remuneração
        raw_norm = re.sub(r"DESTINADA\s+AOS?\s+BENEFICI[AÁ]RIOS.*", "Baixa para beneficiários", raw_norm, flags=re.I)

        # Derivativo com liquidação financeira (fragmentos)
        raw_norm = re.sub(r"O\s+FINANCEIR\s*A\b", "Derivativo com liquidação financeira", raw_norm, flags=re.I)
        raw_norm = re.sub(r"LIQUIDAÇÃ\s*O\s+FINANCEIR\s*A", "Derivativo com liquidação financeira", raw_norm, flags=re.I)
        raw_norm = re.sub(r"DERIVATIVO\s+COM\s+LIQUIDAÇÃ[O]\s+FINANCEIR[A]",
                          "Derivativo com liquidação financeira", raw_norm, flags=re.I)

        # Lançamento de ações (MBRF3 derivativos)
        raw_norm = re.sub(r"(LANÇAMENTO|ENTO)\s+DE\s+\d*\s*AÇÕES", "Lançamento de ações", raw_norm, flags=re.I)

        # Expandir fragmentos comuns de operação quebrada em 2 linhas
        raw_norm = re.sub(r"\bCompra\s+à\b(?!\s+vista|\s+termo)", "Compra à vista", raw_norm)
        raw_norm = re.sub(r"\bVenda\s+à\b(?!\s+vista|\s+termo)",  "Venda à vista",  raw_norm)
        raw_norm = re.sub(r"^\s*vista\b", "Compra à vista", raw_norm)

        # Desdobramento com typo
        raw_norm = re.sub(r"BONFICAÇÃ\s*O\b", "Desdobramento/bonificação", raw_norm, flags=re.I)

        # Ações ref a ILP → plano de remuneração
        raw_norm = re.sub(r"AÇÕES\s+REF\s+A\s+ILP", "Ações de plano de remuneração", raw_norm, flags=re.I)

        # Procurar match canônico (case-insensitive)
        for op in sorted(CANONICAL_OPS, key=len, reverse=True):
            if op.lower() in raw_norm.lower():
                return op

        return raw_norm

    def flush_op_buffer():
        if not op_buffer:
            return
        fields = extract_op_fields(op_buffer)
        if fields:
            fields["tipo_movimentacao"] = clean_op_final(fields["tipo_movimentacao"])
            nk = make_key(cnpj_digits, data_ref, versao, current_qual, current_nome,
                          fields["tipo_ativo"], fields["caracteristica"],
                          fields["tipo_movimentacao"], fields["dia"], fields["quantidade"])
            rows.append({
                "cnpj_digits": cnpj_digits,
                "data_referencia": data_ref,
                "versao": versao,
                "qualificacao": current_qual or "other",
                "nome_entidade": current_nome,
                **fields,
                "data_movimentacao": to_date(fields["dia"], data_ref),
                "natural_key": nk,
            })
        op_buffer.clear()

    def add_balance(tipo_mov, tipo_ativo, caract, qty_str):
        qty = norm_num(qty_str)
        nk = make_key(cnpj_digits, data_ref, versao, current_qual,
                      current_nome, tipo_ativo, caract, tipo_mov)
        rows.append({
            "cnpj_digits": cnpj_digits,
            "data_referencia": data_ref,
            "versao": versao,
            "qualificacao": current_qual or "other",
            "nome_entidade": current_nome,
            "tipo_ativo": tipo_ativo,
            "caracteristica": caract,
            "tipo_movimentacao": tipo_mov,
            "intermediario": None,
            "dia": None,
            "data_movimentacao": None,
            "quantidade": qty,
            "preco_unitario": None,
            "volume": None,
            "natural_key": nk,
        })

    for y_key in sorted_ys:
        line = sorted(lines_map[y_key], key=lambda w: w["x0"])
        texts_x = [(w["x0"], w["text"]) for w in line]
        full = " ".join(t for _, t in texts_x)

        # --- Cabeçalhos de entidade ---
        if "Qualificação:" in full:
            flush_op_buffer()
            section = None
            q = re.sub(r".*Qualificação:\s*", "", full).strip()
            if "Tesouraria" in q:   current_qual = "treasury"
            elif "Controlada" in q: current_qual = "subsidiary"
            elif "Coligada" in q:   current_qual = "affiliated"
            else:                   current_qual = "other"
            continue

        if "Nome:" in full and "CPF" not in full[:50]:
            m = re.search(r"Nome:\s*(.+?)(?:\s{3,}|CPF|$)", full)
            if m: current_nome = m.group(1).strip()
            continue
        if "Nome:" in full:
            m = re.search(r"Nome:\s*(.+?)(?:\s{3,}|CPF|$)", full)
            if m: current_nome = m.group(1).strip()
            continue

        # --- Seções ---
        if "Saldo Inicial" in full and "Movimentações" not in full and "Valor Mobiliário" not in full:
            flush_op_buffer()
            section = "saldo_inicial"
            continue
        if "Movimentações" in full:
            flush_op_buffer()
            section = "movimentacoes"
            continue
        if "Saldo Final" in full and "Valor Mobiliário" not in full:
            flush_op_buffer()
            section = "saldo_final"
            continue

        # Pular linhas de cabeçalho de tabela e metadados
        if any(k in full for k in [
            "Valor Mobiliário", "Características dos Títulos", "Mobiliário/Derivativo",
            "Intermediário", "Operação", "FORMULÁRIO", "Negociação de Valores",
            "ocorreram", "não foram", "Denominação da Companhia", "CPF/CNPJ",
        ]):
            continue
        if re.match(r"^Em \d{2}/\d{4}$", full.strip()):
            continue
        if not full.strip():
            continue

        if not section or not current_qual:
            continue

        # --- Saldo Inicial / Final ---
        if section in ("saldo_inicial", "saldo_final"):
            tipo_mov = "Saldo Inicial" if section == "saldo_inicial" else "Saldo Final"
            ativo_w = [t for x, t in texts_x if in_c(x, C_ATIVO)]
            if not ativo_w:
                continue
            tipo_ativo = ativo_w[0]
            # Característica: palavras entre x≈100 e x≈460
            caract_w = [t for x, t in texts_x if 100 <= x <= 460 and t != tipo_ativo]
            caract = " ".join(caract_w).strip()
            # Quantidade: última palavra no lado direito
            qty_w = [t for x, t in texts_x if x > 455]
            if qty_w:
                qty_str = qty_w[-1]
                add_balance(tipo_mov, tipo_ativo, caract, qty_str)
            continue

        # --- Movimentações ---
        if section == "movimentacoes":
            # Verificar se esta linha tem DIA (âncora de nova operação)
            dia_w = [t for x, t in texts_x if in_c(x, C_DIA) and re.match(r"^\d{1,2}$", t)]
            ativo_w = [t for x, t in texts_x if in_c(x, C_ATIVO) and t in ("Ações", "Derivativos", "Outros")]

            if ativo_w and dia_w:
                # Nova operação completa em uma linha (ou início de operação)
                flush_op_buffer()
                op_buffer.append(line)
            elif ativo_w and not dia_w:
                # Linha de tipo_ativo sem dia — início de operação multi-linha
                flush_op_buffer()
                op_buffer.append(line)
            elif not ativo_w and dia_w and op_buffer:
                # Linha com dia mas sem tipo_ativo — pertence à op anterior
                op_buffer.append(line)
            elif not ativo_w and not dia_w and op_buffer:
                # Linha de continuação (operação quebrada)
                op_buffer.append(line)
            # else: linha irrelevante

    flush_op_buffer()
    return rows


# ============================================================================
# INSERÇÃO
# ============================================================================

INSERT_IPE = """
INSERT OR IGNORE INTO ipe_entries
  (cnpj_digits, data_referencia, versao, qualificacao, nome_entidade,
   tipo_ativo, caracteristica, tipo_movimentacao, intermediario,
   dia, data_movimentacao, quantidade, preco_unitario, volume, natural_key)
VALUES
  (:cnpj_digits, :data_referencia, :versao, :qualificacao, :nome_entidade,
   :tipo_ativo, :caracteristica, :tipo_movimentacao, :intermediario,
   :dia, :data_movimentacao, :quantidade, :preco_unitario, :volume, :natural_key)
"""


def upsert_ipe_rows(conn: sqlite3.Connection, rows: list[dict]) -> int:
    inserted = 0
    for row in rows:
        cur = conn.execute(INSERT_IPE, row)
        inserted += cur.rowcount
    return inserted


# ============================================================================
# INGESTÃO IPE
# ============================================================================


# ============================================================================
# FORMULÁRIO CONSOLIDADO — POSIÇÃO DE GRUPOS (Controlador, CA, Diretoria, etc.)
# ============================================================================

# Mapeamento X do '(' do checkbox por grupo — confirmado com pdfplumber VITTIA fev/2026
# A detecção usa o X do próprio parêntese '(' que precede o 'X',
# não o X do label de texto (que pode estar fragmentado em múltiplas linhas).
#
# Layout confirmado (x0 do '('):
#   Controlador:          x ≈ 122  → range [115, 145]
#   Conselho Administração: x ≈ 199  → range [190, 215]
#   Diretoria:            x ≈ 305  → range [295, 320]  ← label dividido "Direto"+"ria"
#   Conselho Fiscal:      x ≈ 335  → range [325, 355]
#   Órgãos Técnicos:      x ≈ 411  → range [403, 425]
GRUPO_X_RANGES = [
    (115, 145, "Controlador"),
    (190, 215, "CA"),        # Conselho Administração
    (295, 320, "Diretoria"),
    (325, 355, "CF"),        # Conselho Fiscal
    (403, 425, "Orgaos"),   # Órgãos Técnicos ou Consultivos
]
GRUPO_LABELS = {
    "Controlador": "Controlador",
    "CA":          "Conselho Administração",
    "Diretoria":   "Diretoria",
    "CF":          "Conselho Fiscal",
    "Orgaos":      "Órgãos Técnicos",
}


def parse_consolidated_pdf(
    pdf_bytes: bytes,
    cnpj_digits: str,
    data_ref: str,
    versao: int,
) -> list[dict]:
    """
    Parse do PDF 'Formulário Consolidado' (Posição Consolidada).

    Layout confirmado pelos PDFs da VITTIA fev/2026:
    - Cada PÁGINA = 1 grupo de pessoas ligadas
    - Linha y≈150-220: checkbox '( X )' indica qual grupo
    - Tabela de movimentações: mesmas colunas X do Formulário Individual
    - Grupos: Controlador | CA | Diretoria | CF | Órgãos Técnicos
    """
    try:
        import pdfplumber
    except ImportError:
        raise ImportError("pdfplumber not installed")

    from collections import defaultdict

    def make_key(*parts) -> str:
        return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()

    def to_date(dia, ref: str):
        if not dia:
            return None
        y, m = int(ref[:4]), int(ref[5:7])
        return f"{y:04d}-{m:02d}-{int(dia):02d}"

    def norm_num(s):
        if not s:
            return None
        s = str(s).strip().replace(".", "").replace(",", ".")
        try:
            return float(s)
        except ValueError:
            return None

    # Colunas X — idênticas ao parser Individual
    C_ATIVO  = (30,  90)
    C_CARACT = (100, 200)
    C_INTERM = (190, 255)
    C_OP     = (245, 322)
    C_DIA    = (305, 348)
    C_QTY    = (345, 435)
    C_PRECO  = (425, 495)
    C_VOL    = (485, 562)
    C_SALDO  = (455, 562)

    def in_c(x, c): return c[0] <= x <= c[1]

    rows: list[dict] = []

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            ws = page.extract_words(x_tolerance=3, y_tolerance=3)
            if not ws:
                continue

            # ── Detectar grupo pelo checkbox '( X )' em y≈140-220 ──────────
            # Usamos o X do '(' que precede o 'X', não o X do label de texto,
            # porque o label pode estar fragmentado em múltiplas linhas (ex: "Direto"+"ria")
            # IMPORTANTE: ordenar por (top, x0) e verificar mesma linha para evitar
            # que palavras de linhas diferentes quebrem a detecção do padrão '( X )'
            grupo_code = None
            checkbox_words = sorted(
                [w for w in ws if 140 <= w["top"] <= 220],
                key=lambda w: (round(w["top"] / 3) * 3, w["x0"])
            )
            for i, w in enumerate(checkbox_words):
                if w["text"] in ("X", "x") and 0 < i < len(checkbox_words) - 1:
                    prev = checkbox_words[i - 1]
                    nxt  = checkbox_words[i + 1]
                    # Verificar que estão na mesma linha (y próximo)
                    if (prev["text"] == "(" and nxt["text"] == ")"
                            and abs(prev["top"] - w["top"]) < 5
                            and abs(nxt["top"]  - w["top"]) < 5):
                        paren_x = prev["x0"]
                        for x_lo, x_hi, code in GRUPO_X_RANGES:
                            if x_lo <= paren_x <= x_hi:
                                grupo_code = code
                                break
                        break

            if not grupo_code:
                log.debug("  parse_consolidated: página sem grupo, skip")
                continue

            grupo_label = GRUPO_LABELS.get(grupo_code, grupo_code)

            # ── Parse da tabela (idêntico ao Individual) ─────────────────────
            lines_map: dict[int, list] = defaultdict(list)
            for w in ws:
                y_key = round(w["top"] / 3) * 3
                lines_map[y_key].append(w)
            sorted_ys = sorted(lines_map.keys())

            section = None
            op_buffer: list[list] = []

            def make_row(tipo_mov, tipo_ativo, caract, intermediario, dia,
                         quantidade, preco, volume):
                """Cria um registro e gera a natural_key."""
                nk = make_key(cnpj_digits, data_ref, versao, grupo_label,
                              tipo_mov, tipo_ativo, dia, quantidade, preco)
                return {
                    "cnpj_digits":       cnpj_digits,
                    "data_referencia":   data_ref,
                    "versao":            versao,
                    "grupo":             grupo_label,
                    "tipo_ativo":        tipo_ativo or "Ações",
                    "caracteristica":    caract,
                    "tipo_movimentacao": tipo_mov,
                    "intermediario":     intermediario,
                    "dia":               dia,
                    "data_movimentacao": to_date(dia, data_ref),
                    "quantidade":        quantidade,
                    "preco_unitario":    preco,
                    "volume":            volume,
                    "natural_key":       nk,
                }

            def flush_saldo(buf, sec):
                """
                Para Saldo Inicial/Final: emite UM registro por linha de ativo.
                Cada linha independente tem tipo_ativo + quantidade na coluna direita.
                Evita o bug de múltiplos ativos (Ações + Opções + ADRs) numa página.
                """
                if not buf:
                    return []
                tipo_mov = "Saldo Inicial" if sec == "saldo_inicial" else "Saldo Final"
                recs = []
                for line in buf:
                    sorted_line = sorted(line, key=lambda w: w["x0"])
                    ativo_w  = [w["text"] for w in sorted_line if in_c(w["x0"], C_ATIVO)]
                    caract_w = [w["text"] for w in sorted_line
                                if in_c(w["x0"], C_CARACT) and w["text"] not in ativo_w]
                    # Quantidade: último número na parte direita da linha (x > 455)
                    saldo_w  = [w["text"] for w in sorted_line if w["x0"] > 455]
                    if not ativo_w:
                        continue
                    # Tentar extrair quantidade: último token numérico
                    qty = None
                    for tok in reversed(saldo_w):
                        v = norm_num(tok)
                        if v is not None and v >= 0:
                            qty = v; break
                    if qty is None:
                        # Fallback: qualquer número na linha inteira
                        all_nums = [norm_num(w["text"]) for w in sorted_line
                                    if norm_num(w["text"]) is not None]
                        if all_nums:
                            qty = max(all_nums)  # maior valor = quantidade de ações
                    tipo_ativo = ativo_w[0]
                    caract     = " ".join(caract_w).strip() or None
                    recs.append(make_row(tipo_mov, tipo_ativo, caract,
                                        None, None, qty, None, None))
                return recs

            def _norm_consolidated_op(raw: str) -> str:
                """Normaliza fragmentos de operação quebrada no Formulário Consolidado."""
                r = re.sub(r"\s+", " ", raw).strip()
                # Remover números soltos 1-31 que são dias vazados da coluna C_DIA
                # ex: "Venda à vista 12" → "Venda à vista"
                # ex: "EMPRÉSTIM 19 O" → "EMPRÉSTIM O"
                r = re.sub(r"(?<![\d])\b([1-9]|[12]\d|3[01])\b(?![\d])", " ", r)
                r = re.sub(r"\s+", " ", r).strip()
                # Fragmentos de "Compra à vista" / "Venda à vista"
                r = re.sub(r"^vista\b", "Compra à vista", r)
                r = re.sub(r"\bCompra\s+à$", "Compra à vista", r)
                r = re.sub(r"\bVenda\s+à$",  "Venda à vista",  r)
                r = re.sub(r"\bvista\s+Compra\s+à\b", "Compra à vista", r, flags=re.I)
                r = re.sub(r"\bvista\s+Venda\s+à\b",  "Venda à vista",  r, flags=re.I)
                r = re.sub(r"\btermo\s+Compra\s+à\b", "Compra à termo", r, flags=re.I)
                r = re.sub(r"\btermo\s+Venda\s+à\b",  "Venda à termo",  r, flags=re.I)
                r = re.sub(r"\bCompra\s+à\s+vista\b", "Compra à vista", r, flags=re.I)
                r = re.sub(r"\bVenda\s+à\s+vista\b",  "Venda à vista",  r, flags=re.I)
                r = re.sub(r"\bCompra\s+à\s+termo\b", "Compra à termo", r, flags=re.I)
                r = re.sub(r"\bVenda\s+à\s+termo\b",  "Venda à termo",  r, flags=re.I)
                # Entrega de ações restritas / bônus
                r = re.sub(r"ACOES\s+RESTRITAS\s+ENTREGA", "Entrega de ações restritas", r, flags=re.I)
                r = re.sub(r"ACOES\s+RESTRITAS\b",         "Entrega de ações restritas", r, flags=re.I)
                r = re.sub(r"AÇÕES\s+RESTRITAS\s+ENTREGA", "Entrega de ações restritas", r, flags=re.I)
                r = re.sub(r"AÇÕES\s+RESTRITAS\b",         "Entrega de ações restritas", r, flags=re.I)
                r = re.sub(r"ENTREGA\s+DE\s+AÇÕES\s+RESTRITAS", "Entrega de ações restritas", r, flags=re.I)
                r = re.sub(r"^ENTREGA$", "Entrega de ações restritas", r, flags=re.I)
                # Exercício de opção
                r = re.sub(r"\d{2}\s+DE\s+OPCOES\s+EXERCICIO", "Exercício de opção de compra", r, flags=re.I)
                r = re.sub(r"\d{2}\s+DE\s+OPCOES\b",            "Exercício de opção de compra", r, flags=re.I)
                r = re.sub(r"\d{2}\s+Opções\s+Exercício\s+de",  "Exercício de opção de compra", r, flags=re.I)
                r = re.sub(r"DE\s+OPÇÕES\s+ENTREGA",              "Exercício de opção de compra", r, flags=re.I)
                r = re.sub(r"DE\s+OPÇÕES\s+EXERCÍCIO",            "Exercício de opção de compra", r, flags=re.I)
                r = re.sub(r"DE\s+OPCOES\b",                      "Exercício de opção de compra", r, flags=re.I)
                r = re.sub(r"Direto\s+AÇÕES\s+RESTRITAS\s+EXERCÍCIO", "Exercício de opção de compra", r, flags=re.I)
                r = re.sub(r"Direto\s+DE\s+OPÇÕES\s+EXERCÍCIO",       "Exercício de opção de compra", r, flags=re.I)
                r = re.sub(r"Direto\s+DE\s+OPÇÕES\s+ENTREGA",         "Exercício de opção de compra", r, flags=re.I)
                r = re.sub(r"Direto\s+DE\s+OPÇÕES\b",                 "Exercício de opção de compra", r, flags=re.I)
                r = re.sub(r"STOCK\s+OPTION",  "Exercício de opção de compra", r, flags=re.I)
                r = re.sub(r"PLANO\s+DE\b",   "Ações de plano de remuneração", r, flags=re.I)
                # Desdobramento/bonificação
                r = re.sub(r"o/bonificação\s+Desdobrament", "Desdobramento/bonificação", r, flags=re.I)
                r = re.sub(r"\d{2}\s+o/bonificação.*",     "Desdobramento/bonificação", r, flags=re.I)
                r = re.sub(r"Desdobrament$",                 "Desdobramento/bonificação", r, flags=re.I)
                # Devolução/Contratação de empréstimo
                r = re.sub(r"empréstimo\s+\(locador\)\s+Devolução\s+de", "Devolução de empréstimo (locador)", r, flags=re.I)
                r = re.sub(r"empréstimo\s+\(locador\)\s+Contratação\s+de", "Contratação de empréstimo (locador)", r, flags=re.I)
                # Ações decorrentes de exercício
                r = re.sub(r"Ações\s+decorrentes\s+de.*exercício.*opção.*compra", "Exercício de opção de compra", r, flags=re.I)
                r = re.sub(r"AÇÕES\s+DECORREN\s+TES\s+DE", "Exercício de opção de compra", r, flags=re.I)
                # Desligamento
                r = re.sub(r"Desligamento/$", "Desligamento/saída", r)
                # Posse com dia anexado
                r = re.sub(r"Posse\s+\d{1,2}$", "Posse", r)
                # Duplicação de "Entrega de"
                r = re.sub(r"(Entrega de ){2,}ações restritas", "Entrega de ações restritas", r, flags=re.I)
                # Operações coladas: manter primeira
                for _pat, _canon in [
                    (r"^Venda\s+à\s+vista\b.*", "Venda à vista"),
                    (r"^Compra\s+à\s+vista\b.*", "Compra à vista"),
                    (r"^Entrega\s+de\s+ações\s+restritas\b.*", "Entrega de ações restritas"),
                    (r"^Exercício\s+de\s+opção\b.*", "Exercício de opção de compra"),
                ]:
                    if re.match(_pat, r, re.I):
                        r = _canon; break
                # Outras normalizações
                r = re.sub(r"^DO\s+OPCOES\s+ENTREGA", "Exercício de opção de compra", r, flags=re.I)
                r = re.sub(r"^CALL\s+STOCK$", "Exercício de opção de compra", r, flags=re.I)
                r = re.sub(r"^STOCKOPTI\s+ON$", "Exercício de opção de compra", r, flags=re.I)
                r = re.sub(r"^STOCK$", "Exercício de opção de compra", r, flags=re.I)
                r = re.sub(r"^ACOES\s+DIFERIDAS$", "Ações de plano de remuneração", r, flags=re.I)
                r = re.sub(r"^DIRETA\s+OUTORGA$", "Outorga de ações", r, flags=re.I)
                r = re.sub(r"^RENUNCIA\b", "RENÚNCIA", r)
                r = re.sub(r"RENÚNCIA\s+ENTREGA", "RENÚNCIA", r, flags=re.I)
                r = re.sub(r"^ELEICAO\b", "Eleição", r)
                r = re.sub(r"^Ações\s+de\b\s*$", "Ações de plano de remuneração", r, flags=re.I)
                r = re.sub(r"^o/bonificação$", "Desdobramento/bonificação", r, flags=re.I)
                r = re.sub(r"^saída$", "Desligamento/saída", r)
                r = re.sub(r"^ENTRE\s+\w+\s+FUNDOS\s+CONTROLA.*", "Transferência entre fundos controladores", r, flags=re.I)
                r = re.sub(r"^ENTIDADES\s+DO\s+CONTROLA.*", "Transferência entre entidades controlador", r, flags=re.I)
                # Limpar DIA no início da string (artefato do parser)
                r = re.sub(r"^\d{1,2}\s+", "", r).strip()
                return r.strip()

            def flush_op(buf, sec):
                """Para movimentações: acumula multi-linha e emite 1 registro."""
                if not buf or sec != "movimentacoes":
                    return None
                ativo_w=[]; caract_w=[]; interm_w=[]; op_w=[]
                dia_w=[]; qty_w=[]; preco_w=[]; vol_w=[]
                for line in buf:
                    for w in sorted(line, key=lambda x: x["x0"]):
                        x = w["x0"]; t = w["text"]
                        if in_c(x, C_ATIVO):   ativo_w.append(t)
                        if in_c(x, C_CARACT):  caract_w.append(t)
                        if in_c(x, C_INTERM):  interm_w.append(t)
                        # Coletar dia ANTES de op_w para poder excluir o token do campo op
                        if in_c(x, C_DIA) and re.match(r"^\d{1,2}$", t):
                            dia_w.append(t)
                        # Excluir da coluna OP: tokens numéricos 1-31 que já foram capturados
                        # como dia (overlap entre C_OP e C_DIA em x≈305-322)
                        if in_c(x, C_OP) and not (re.match(r"^\d{1,2}$", t) and in_c(x, C_DIA)):
                            op_w.append(t)
                        if in_c(x, C_QTY):     qty_w.append(t)
                        if in_c(x, C_PRECO):   preco_w.append(t)
                        if in_c(x, C_VOL):     vol_w.append(t)

                tipo_ativo    = ativo_w[0] if ativo_w else "Ações"
                caracteristica = " ".join(caract_w).strip() or None
                intermediario  = " ".join(interm_w).strip() or None
                operacao       = _norm_consolidated_op(" ".join(op_w))
                dia            = int(dia_w[0]) if dia_w else None
                quantidade     = norm_num(" ".join(qty_w))
                preco          = norm_num(" ".join(preco_w))
                volume         = norm_num(" ".join(vol_w))
                if not operacao:
                    return None
                # Descartar fragmentos sem dados úteis
                if dia is None and quantidade is None:
                    return None
                # Normalizar operações que ficaram como fragmento de linha 2
                # ex: "empréstimo (locador)" sozinho → completar com contexto
                if re.match(r"^empréstimo", operacao, re.I):
                    if "locador" in operacao:
                        operacao = "Devolução de empréstimo (locador)"
                    else:
                        operacao = "Devolução de empréstimo"
                return make_row(operacao, tipo_ativo, caracteristica,
                                intermediario, dia, quantidade, preco, volume)

            for y in sorted_ys:
                line = sorted(lines_map[y], key=lambda w: w["x0"])
                text = " ".join(w["text"] for w in line).lower()

                if "saldo inicial" in text:
                    # Flush anterior: saldo → flush_saldo, movs → flush_op
                    if op_buffer:
                        if section in ("saldo_inicial", "saldo_final"):
                            rows.extend(flush_saldo(op_buffer, section))
                        else:
                            rec = flush_op(op_buffer, section)
                            if rec: rows.append(rec)
                    op_buffer = []; section = "saldo_inicial"; continue
                elif "movimenta" in text and ("mês" in text or "mes" in text):
                    if op_buffer:
                        if section in ("saldo_inicial", "saldo_final"):
                            rows.extend(flush_saldo(op_buffer, section))
                        else:
                            rec = flush_op(op_buffer, section)
                            if rec: rows.append(rec)
                    op_buffer = []; section = "movimentacoes"; continue
                elif "saldo final" in text:
                    if op_buffer:
                        if section in ("saldo_inicial", "saldo_final"):
                            rows.extend(flush_saldo(op_buffer, section))
                        else:
                            rec = flush_op(op_buffer, section)
                            if rec: rows.append(rec)
                    op_buffer = []; section = "saldo_final"; continue
                elif any(hdr in text for hdr in [
                    "valor mobiliário", "intermediário", "operação",
                    "características", "derivativo", "quantidade",
                ]):
                    continue  # header, skip
                if section is None:
                    continue
                dia_words = [
                    w for w in line
                    if in_c(w["x0"], C_DIA) and re.match(r"^\d{1,2}$", w["text"])
                ]
                if dia_words and op_buffer:
                    rec = flush_op(op_buffer, section)
                    if rec: rows.append(rec)
                    op_buffer = [line]
                else:
                    op_buffer.append(line)

            if op_buffer:
                if section in ("saldo_inicial", "saldo_final"):
                    rows.extend(flush_saldo(op_buffer, section))
                else:
                    rec = flush_op(op_buffer, section)
                    if rec: rows.append(rec)

    return rows


def upsert_consolidated_positions(conn: sqlite3.Connection, rows: list[dict]) -> int:
    total = 0
    for r in rows:
        conn.execute(
            "INSERT OR IGNORE INTO consolidated_positions"
            "(cnpj_digits,data_referencia,versao,grupo,tipo_ativo,caracteristica,"
            "tipo_movimentacao,intermediario,dia,data_movimentacao,quantidade,"
            "preco_unitario,volume,natural_key)"
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (r["cnpj_digits"], r["data_referencia"], r["versao"],
             r["grupo"], r.get("tipo_ativo"), r.get("caracteristica"),
             r["tipo_movimentacao"], r.get("intermediario"),
             r.get("dia"), r.get("data_movimentacao"),
             r.get("quantidade"), r.get("preco_unitario"), r.get("volume"),
             r["natural_key"]),
        )
        total += conn.execute("SELECT changes()").fetchone()[0]
    return total


def already_ingested_consolidated(
    conn: sqlite3.Connection, cnpj_digits: str, data_ref: str, versao: int
) -> bool:
    """
    Lógica de deduplicação robusta para o Formulário Consolidado.

    Retorna True (pula o PDF) apenas se:
      1. Já existe pelo menos 1 Saldo Final no banco para este (cnpj, mes, versao), E
      2. O número de grupos com SF é >= ao número de grupos do mês anterior da mesma empresa.

    Isso garante que:
      - Meses novos (sem nenhuma linha): sempre parseados → OK
      - Meses já no banco com todos os grupos: pulados → eficiente
      - Meses no banco com grupos faltando (bug antigo): re-parseados → auto-corrige
      - Empresas com poucos grupos (ex: SMTO3 só tem Controlador): pulados corretamente
    """
    # Quantos grupos com SF já temos para este mês?
    current = conn.execute(
        "SELECT COUNT(DISTINCT grupo) FROM consolidated_positions "
        "WHERE cnpj_digits=? AND data_referencia=? AND versao=? "
        "AND tipo_movimentacao='Saldo Final'",
        (cnpj_digits, data_ref, versao),
    ).fetchone()[0]

    if current == 0:
        return False  # nenhuma linha — precisa parsear

    # Quantos grupos tem o mês mais recente já no banco (excluindo este mês)?
    prev = conn.execute(
        "SELECT COUNT(DISTINCT grupo) FROM consolidated_positions "
        "WHERE cnpj_digits=? AND data_referencia < ? "
        "AND tipo_movimentacao='Saldo Final' "
        "AND data_referencia = ("
        "  SELECT MAX(data_referencia) FROM consolidated_positions "
        "  WHERE cnpj_digits=? AND data_referencia < ? AND tipo_movimentacao='Saldo Final'"
        ")",
        (cnpj_digits, data_ref, cnpj_digits, data_ref),
    ).fetchone()[0]

    if prev == 0:
        # Primeiro mês no banco — assume completo se tem pelo menos 1 grupo
        return current >= 1

    # Completo se tem tantos grupos quanto o mês anterior
    return current >= prev


def ingest_consolidated_year(
    year: int, cnpj_to_ticker: dict[str, str], conn: sqlite3.Connection
) -> int:
    """
    Baixa e parseia documentos 'Posição Consolidada' do índice IPE.
    Uma página por grupo; mesmo layout de tabela do Individual.
    """
    try:
        df_idx = load_ipe_index(year)
    except Exception as e:
        log.warning("Índice IPE %d indisponível para Consolidado: %s", year, e)
        return 0

    cnpj_set = set(cnpj_to_ticker.keys())
    links_df = filter_consolidated_links(df_idx, cnpj_set)
    if links_df.empty:
        return 0

    log.info("  Consolidado: %d documentos em %d", len(links_df), year)
    total = 0
    for _, row in links_df.iterrows():
        cnpj   = row["cnpj_clean"]
        ticker = cnpj_to_ticker.get(cnpj, cnpj)
        ref    = row["Data_Referencia"]
        versao = int(row.get("Versao", 1))
        url    = row["Link_Download"]

        if already_ingested_consolidated(conn, cnpj, ref, versao):
            log.debug("  skip Consolidado %s %s v%d", ticker, ref, versao)
            continue

        log.info("  Consolidado baixando %s %s v%d …", ticker, ref, versao)
        try:
            pdf_bytes = fetch(url, timeout=30)
            rows_parsed = parse_consolidated_pdf(pdf_bytes, cnpj, ref, versao)
            n = upsert_consolidated_positions(conn, rows_parsed)
            total += n
            conn.commit()
            log.info("    → %d linhas inseridas (%d parsed)", n, len(rows_parsed))
            time.sleep(0.3)
        except Exception as e:
            log.error("  ERRO Consolidado %s %s: %s", ticker, ref, e)
            continue

    return total


def already_ingested(conn: sqlite3.Connection, cnpj_digits: str, data_ref: str, versao: int) -> bool:
    """Verifica se já temos dados para este (cnpj, mes, versao)."""
    r = conn.execute(
        "SELECT COUNT(*) FROM ipe_entries "
        "WHERE cnpj_digits=? AND data_referencia=? AND versao=?",
        (cnpj_digits, data_ref, versao),
    ).fetchone()
    return r[0] > 0


def ingest_ipe_year(year: int, cnpj_to_ticker: dict[str, str], conn: sqlite3.Connection) -> int:
    """Baixa o índice IPE do ano, filtra tickers, baixa e parseia cada PDF."""
    try:
        df_idx = load_ipe_index(year)
    except Exception as e:
        log.warning("Índice IPE %d indisponível: %s", year, e)
        return 0

    cnpj_set = set(cnpj_to_ticker.keys())
    links_df = filter_individual_links(df_idx, cnpj_set)
    log.info("  %d docs individuais encontrados para %d", len(links_df), year)

    total = 0
    for _, row in links_df.iterrows():
        cnpj   = row["cnpj_clean"]
        ticker = cnpj_to_ticker.get(cnpj, cnpj)
        ref    = row["Data_Referencia"]  # YYYY-MM-01
        versao = int(row.get("Versao", 1))
        url    = row["Link_Download"]

        if already_ingested(conn, cnpj, ref, versao):
            log.debug("  skip %s %s v%d (já no banco)", ticker, ref, versao)
            continue

        log.info("  baixando %s %s v%d …", ticker, ref, versao)
        try:
            pdf_bytes = fetch(url, timeout=30)
            rows = parse_ipe_pdf(pdf_bytes, cnpj, ref, versao)
            n = upsert_ipe_rows(conn, rows)
            total += n
            conn.commit()
            log.info("    → %d linhas inseridas (%d parsed)", n, len(rows))
            time.sleep(0.3)  # não sobrecarregar o servidor CVM
        except Exception as e:
            log.error("  ERRO %s %s: %s", ticker, ref, e)
            continue

    return total


# ============================================================================
# RECOMPRAS (programas formais — inalterado da v1)
# ============================================================================

def _clean(v) -> str | None:
    if pd.isna(v):
        return None
    s = str(v).strip()
    return s if s else None


def _num(v) -> float | None:
    s = _clean(v)
    if s is None:
        return None
    s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def ingest_recompras(conn: sqlite3.Connection) -> int:
    log.info("Baixando programas de recompra …")
    try:
        data = fetch(RECOMPRA_URL)
    except Exception as e:
        log.warning("Recompras indisponíveis: %s", e)
        return 0

    cnpj_set = set(TICKERS.values())
    total = 0

    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()

        # --- programas ---
        # Colunas reais (2025+):
        #   ID_Programa, CNPJ_Companhia, Nome_Companhia, Data_Deliberacao,
        #   Data_Final_Prazo, Situacao, Tipo_Operacao, Motivo, Finalidade_Compra,
        #   Quantidade_Acoes_Ordinarias, Quantidade_Acoes_Preferenciais
        prog_f = next((n for n in names if "recompra_acoes.csv" in n
                       and "intermediarios" not in n and "quantidades" not in n), None)
        if prog_f:
            with z.open(prog_f) as f:
                df = pd.read_csv(f, sep=";", encoding="latin-1", dtype=str)
            df["cnpj_clean"] = df["CNPJ_Companhia"].str.replace(r"\D", "", regex=True)
            df = df[df["cnpj_clean"].isin(cnpj_set)]
            for _, r in df.iterrows():
                # qtd_autorizada = soma de ON + PN (quando disponíveis)
                qtd_on  = _num(r.get("Quantidade_Acoes_Ordinarias"))
                qtd_pn  = _num(r.get("Quantidade_Acoes_Preferenciais"))
                qtd_aut = (qtd_on or 0) + (qtd_pn or 0) or None
                nk = hashlib.sha1(
                    f"{r['cnpj_clean']}|{r.get('ID_Programa','')}".encode()
                ).hexdigest()
                conn.execute(
                    "INSERT OR IGNORE INTO buyback_programs"
                    "(cnpj_digits,id_programa,data_deliberacao,data_final_prazo,"
                    "situacao,qtd_autorizada,qtd_acoes_em_circ,destinacao,natural_key)"
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (r["cnpj_clean"], _clean(r.get("ID_Programa")),
                     _clean(r.get("Data_Deliberacao")), _clean(r.get("Data_Final_Prazo")),
                     _clean(r.get("Situacao")), qtd_aut,
                     None,   # Quantidade_Acoes_Emitidas_Circulacao não existe mais
                     _clean(r.get("Finalidade_Compra")), nk),
                )
                total += conn.execute("SELECT changes()").fetchone()[0]

        # --- quantidades ---
        # Colunas reais (2025+):
        #   ID_Programa, Tipo_Acao, Classe_Acao, Quantidade_Circulacao, Quantidade_Operacao
        # Não tem CNPJ_Companhia — join via ID_Programa com buyback_programs
        qtd_f = next((n for n in names if "quantidades" in n), None)
        if qtd_f:
            with z.open(qtd_f) as f:
                df_qtd = pd.read_csv(f, sep=";", encoding="latin-1", dtype=str)
            # Buscar IDs dos nossos programas no banco
            our_ids = {str(r[0]) for r in conn.execute(
                "SELECT id_programa FROM buyback_programs WHERE cnpj_digits IN (%s)"
                % ",".join("?" * len(cnpj_set)), list(cnpj_set)
            ).fetchall()}
            df_qtd = df_qtd[df_qtd["ID_Programa"].isin(our_ids)]
            for _, r in df_qtd.iterrows():
                nk = hashlib.sha1(
                    f"{r.get('ID_Programa','')}|{r.get('Tipo_Acao','')}|"
                    f"{r.get('Classe_Acao','')}".encode()
                ).hexdigest()
                conn.execute(
                    "INSERT OR IGNORE INTO buyback_quantities"
                    "(cnpj_digits,id_programa,data_referencia,descricao_valor_mob,"
                    "qtd_adquirida,qtd_alienada,qtd_cancelada,natural_key)"
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (None,  # sem CNPJ na tabela de quantidades — recuperar via join
                     _clean(r.get("ID_Programa")),
                     None,  # sem data_referencia no novo formato
                     _clean(r.get("Tipo_Acao")),
                     _num(r.get("Quantidade_Operacao")),
                     None, None, nk),
                )
                total += conn.execute("SELECT changes()").fetchone()[0]

    conn.commit()
    return total


# ============================================================================
# DASHBOARD — extrai dados do banco e injeta no HTML
# ============================================================================

DASHBOARD_HTML         = SCRIPT_DIR / "cvm_buybacks.html"
DASHBOARD_INSIDER_HTML = SCRIPT_DIR / "cvm_insider.html"

GRUPO_TO_ROLE: dict[str, str] = {
    "Controlador":            "ctrl",
    "Diretoria":              "mgmt",
    "Conselho Administração": "board",
    "Conselho Fiscal":        "board",
    "Órgãos Técnicos":        "board",
}


def _classify_op(op: str | None) -> str | None:
    if not op:
        return None
    o = op.lower()
    # Eventos corporativos sem direção de mercado → ignorar no dashboard
    if any(x in o for x in ["renúncia", "renuncia", "reeleição", "eleicao", "eleição",
                              "não reeleição", "reorganizaç", "transferê", "transfere",
                              "intercompany", "listagem", "acordo de acionista",
                              "signatári", "inclusão de acionista"]):
        return None
    if any(x in o for x in ["venda", "desligamento", "saída", "saida", "doador",
                              "liquidaç", "contratação de empréstimo",
                              "contratacao de emprestimo"]):
        return "sell"
    if any(x in o for x in ["compra", "subscri", "exercí", "exerci", "restritas",
                              "bonificac", "bonificaç", "desdobr", "plano", "posse",
                              "ilp", "incorpora", "devolução de empréstimo",
                              "devolucao de emprestimo", "vista", "termo",
                              "option", "opções", "opção", "donatário", "donatario",
                              "de opções", "vesting"]):
        return "buy"
    return None


def _replace_block(html: str, name: str, new_val: str) -> str:
    m = re.search(rf"(const {re.escape(name)}\s+=\s+)", html)
    if not m:
        log.warning("Constante '%s' não encontrada no HTML — pulando", name)
        return html
    start = m.end()
    depth = 0
    i = start
    while i < len(html):
        c = html[i]
        if c in "{[":   depth += 1
        elif c in "}]":
            depth -= 1
            if depth == 0:
                break
        i += 1
    return html[:start] + new_val + html[i + 1:]


def build_dashboard(conn: sqlite3.Connection) -> None:
    """Lê dados do banco e injeta no cvm_buybacks.html."""
    if not DASHBOARD_HTML.exists():
        log.warning("Template HTML não encontrado: %s — pulando build", DASHBOARD_HTML)
        return

    tickers = [r[0] for r in conn.execute(
        "SELECT ticker FROM companies ORDER BY ticker"
    ).fetchall()]
    cnpj_of = {r[0]: r[1] for r in conn.execute(
        "SELECT ticker, cnpj_digits FROM companies"
    ).fetchall()}

    # ── BUYBACK_DAILY ─────────────────────────────────────────────────────────
    buyback_daily: dict = {}
    for r in conn.execute("""
        SELECT c.ticker, e.data_movimentacao, e.caracteristica,
               e.quantidade, e.preco_unitario, e.volume, e.intermediario
        FROM ipe_entries e JOIN companies c ON c.cnpj_digits=e.cnpj_digits
        WHERE e.tipo_ativo='Ações'
          AND e.qualificacao='treasury'
          AND e.tipo_movimentacao IN ('Compra à vista','Compra à termo','Compra')
          AND (e.preco_unitario IS NULL OR e.preco_unitario > 0)
        ORDER BY c.ticker, e.data_movimentacao
    """):
        t = r["ticker"]
        if t not in buyback_daily:
            buyback_daily[t] = []
        buyback_daily[t].append({
            "d":  r["data_movimentacao"],
            "cl": r["caracteristica"] or "ON",
            "q":  round(r["quantidade"] or 0),
            "p":  round(r["preco_unitario"], 4) if r["preco_unitario"] else None,
            "v":  round(r["volume"], 2)          if r["volume"]        else None,
            "i":  r["intermediario"],
        })

    # ── BUYBACK_MONTHLY ───────────────────────────────────────────────────────
    buyback_monthly: dict = {}
    for t, rows in buyback_daily.items():
        mon: dict = {}
        for r in rows:
            if not r["d"]:
                continue
            k = r["d"][:7] + "-01"
            if k not in mon:
                mon[k] = {"d": k, "bq": 0, "bv": 0.0}
            mon[k]["bq"] += r["q"]
            mon[k]["bv"] += r["v"] or 0.0
        buyback_monthly[t] = sorted(mon.values(), key=lambda x: x["d"])

    # ── INSIDER_SERIES ────────────────────────────────────────────────────────
    tsy_sf: dict = defaultdict(dict)
    for r in conn.execute("""
        SELECT c.ticker, e.data_referencia, SUM(e.quantidade) qty
        FROM ipe_entries e JOIN companies c ON c.cnpj_digits=e.cnpj_digits
        WHERE e.tipo_movimentacao='Saldo Final' AND e.tipo_ativo='Ações'
          AND e.qualificacao='treasury'
          AND e.quantidade IS NOT NULL
        GROUP BY c.ticker, e.data_referencia
    """):
        tsy_sf[r["ticker"]][r["data_referencia"]] = round(r["qty"] or 0)

    cons_sf: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    for r in conn.execute("""
        SELECT c.ticker, cp.data_referencia, cp.grupo, SUM(cp.quantidade) qty
        FROM consolidated_positions cp
        JOIN companies c ON c.cnpj_digits=cp.cnpj_digits
        WHERE cp.tipo_movimentacao='Saldo Final' AND cp.tipo_ativo='Ações'
          AND cp.quantidade IS NOT NULL
        GROUP BY c.ticker, cp.data_referencia, cp.grupo
    """):
        role = GRUPO_TO_ROLE.get(r["grupo"])
        if role:
            cons_sf[r["ticker"]][r["data_referencia"]][role] += r["qty"] or 0

    insider_series: dict = {}
    for t in tickers:
        months = sorted(
            set(tsy_sf.get(t, {}).keys()) | set(cons_sf.get(t, {}).keys())
        )
        if not months:
            continue
        series = []
        last: dict = {"ctrl": None, "mgmt": None, "board": None, "tsy": None}
        for m in months:
            cg  = cons_sf.get(t, {}).get(m, {})
            tsy = tsy_sf.get(t, {}).get(m)
            row = {
                "d":     m,
                "tsy":   tsy              if tsy   is not None else last["tsy"],
                "ctrl":  round(cg["ctrl"])  if "ctrl"  in cg   else last["ctrl"],
                "mgmt":  round(cg["mgmt"])  if "mgmt"  in cg   else last["mgmt"],
                "board": round(cg["board"]) if "board" in cg   else last["board"],
            }
            last = {k: row[k] for k in ["ctrl", "mgmt", "board", "tsy"]}
            series.append(row)
        insider_series[t] = series

    # ── INSIDER_AGG ───────────────────────────────────────────────────────────
    agg_raw: dict = defaultdict(lambda: defaultdict(lambda: {
        "ctrl_bq": 0, "ctrl_bv": 0.0, "ctrl_sq": 0, "ctrl_sv": 0.0,
        "mgmt_bq": 0, "mgmt_bv": 0.0, "mgmt_sq": 0, "mgmt_sv": 0.0,
        "board_bq": 0, "board_bv": 0.0, "board_sq": 0, "board_sv": 0.0,
    }))
    for r in conn.execute("""
        SELECT c.ticker, cp.data_referencia, cp.grupo, cp.tipo_movimentacao,
               SUM(cp.quantidade) qty, SUM(COALESCE(cp.volume, 0)) vol
        FROM consolidated_positions cp
        JOIN companies c ON c.cnpj_digits=cp.cnpj_digits
        WHERE cp.tipo_ativo='Ações'
          AND cp.tipo_movimentacao NOT IN ('Saldo Inicial','Saldo Final')
        GROUP BY c.ticker, cp.data_referencia, cp.grupo, cp.tipo_movimentacao
    """):
        role      = GRUPO_TO_ROLE.get(r["grupo"])
        direction = _classify_op(r["tipo_movimentacao"])
        if not role or not direction:
            continue
        qty = round(r["qty"] or 0)
        vol = round(r["vol"] or 0, 2)
        d   = agg_raw[r["ticker"]][r["data_referencia"]]
        if direction == "buy":
            d[f"{role}_bq"] += qty;  d[f"{role}_bv"] += vol
        else:
            d[f"{role}_sq"] += qty;  d[f"{role}_sv"] += vol

    insider_agg: dict = {
        t: sorted([{"d": m, **v} for m, v in months.items()], key=lambda x: x["d"])
        for t, months in agg_raw.items()
    }

    # ── PROGRAMS_FULL ─────────────────────────────────────────────────────────
    programs_full: dict = defaultdict(list)
    for r in conn.execute("""
        SELECT c.ticker, b.id_programa, b.data_deliberacao, b.data_final_prazo,
               b.situacao, b.qtd_autorizada, b.qtd_acoes_em_circ, b.destinacao
        FROM buyback_programs b JOIN companies c ON c.cnpj_digits=b.cnpj_digits
        ORDER BY c.ticker, b.data_deliberacao
    """):
        programs_full[r["ticker"]].append({
            "id_programa":      r["id_programa"],
            "data_deliberacao": r["data_deliberacao"],
            "data_final_prazo": r["data_final_prazo"],
            "situacao":         r["situacao"],
            "auth_qty":         r["qtd_autorizada"],
            "float_qty":        r["qtd_acoes_em_circ"],
            "dest":             r["destinacao"] or "—",
        })

    # ── CONSOLIDATED + REALIZED ───────────────────────────────────────────────
    consolidated: dict = {}
    realized:     dict = {}
    for t in tickers:
        cnpj   = cnpj_of[t]
        active = [p for p in programs_full.get(t, []) if p["situacao"] == "Em Andamento"]
        realized[t] = {
            p["id_programa"]: {"realized_q": 0, "realized_v": 0, "pct_done": 0.0}
            for p in programs_full.get(t, [])
        }
        if not active:
            consolidated[t] = None
            continue
        oldest     = min(p["data_deliberacao"] for p in active if p["data_deliberacao"])
        total_auth = sum(p["auth_qty"] or 0 for p in active)
        bought     = conn.execute("""
            SELECT SUM(e.quantidade) total FROM ipe_entries e
            WHERE e.cnpj_digits=? AND e.tipo_ativo='Ações'
              AND e.qualificacao='treasury'
              AND e.tipo_movimentacao IN ('Compra à vista','Compra à termo','Compra')
              AND (e.preco_unitario IS NULL OR e.preco_unitario > 0)
              AND e.data_movimentacao >= ?
        """, (cnpj, oldest)).fetchone()["total"] or 0
        pct = round(bought / total_auth * 100, 1) if total_auth else 0.0
        consolidated[t] = {
            "n_programs":    len(active),
            "total_auth":    round(total_auth),
            "total_done":    round(bought),
            "total_vol":     0,
            "pct_done":      pct,
            "float_qty":     active[0]["float_qty"],
            "oldest_delib":  oldest,
            "latest_expiry": max(p["data_final_prazo"] for p in active
                                 if p["data_final_prazo"]),
        }

    # ── COMPANY_NAMES ─────────────────────────────────────────────────────────
    company_names = {t: t for t in tickers}

    # ── Injetar nos HTMLs (buybacks + insider) ────────────────────────────────
    js = lambda obj: json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    data_map = [
        ("BUYBACK_DAILY",   buyback_daily),
        ("BUYBACK_MONTHLY", buyback_monthly),
        ("INSIDER_AGG",     insider_agg),
        ("PROGRAMS_FULL",   dict(programs_full)),
        ("REALIZED",        realized),
        ("CONSOLIDATED",    consolidated),
        ("INSIDER_SERIES",  insider_series),
        ("COMPANY_NAMES",   company_names),
    ]

    for html_path in [DASHBOARD_HTML, DASHBOARD_INSIDER_HTML]:
        if not html_path.exists():
            log.warning("Template não encontrado: %s — pulando", html_path)
            continue
        html = html_path.read_text(encoding="utf-8")
        for name, obj in data_map:
            html = _replace_block(html, name, js(obj))
        # JBSS3: dados mantidos no banco mas removido do dropdown
        html = html.replace('<option value="JBSS3">JBSS3</option>', '')
        # Remover declarações de variáveis duplicadas (artefato de geração)
        import re as _re
        for _var in ["let SEL_TICKER='ABEV3', PERIOD='all', VIEW='qty', GRAN='monthly';",
                     "let CH={};",
                     "const C  = {ctrl:'#FF5500', mgmt:'#1A1A1A', board:'#888888'};",
                     "const CA = {ctrl:'rgba(255,85,0,0.75)', mgmt:'rgba(26,26,26,0.75)', board:'rgba(136,136,136,0.75)'};"]:
            _positions = [m.start() for m in _re.finditer(_re.escape(_var), html)]
            for _pos in reversed(_positions[1:]):
                html = html[:_pos] + html[_pos+len(_var):]
        html_path.write_text(html, encoding="utf-8")
        log.info("Dashboard atualizado: %s (%d bytes)", html_path, len(html))


# ============================================================================
# MAIN
# ============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="CVM Buybacks — IPE Individual + Consolidado")
    parser.add_argument("--bootstrap", action="store_true",
                        help=f"Puxa histórico completo desde {BOOTSTRAP_START_YEAR}")
    parser.add_argument("--no-dashboard", action="store_true",
                        help="Pula a atualização do dashboard HTML")
    args = parser.parse_args()

    init_db()
    cnpj_to_ticker = {v: k for k, v in TICKERS.items()}

    with db_conn() as conn:
        current_year = date.today().year
        if args.bootstrap:
            years = list(range(BOOTSTRAP_START_YEAR, current_year + 1))
        else:
            years = [current_year]
            if date.today().month <= 2:
                years.insert(0, current_year - 1)

        log.info("IPE Individual — anos: %s", years)
        total_ipe = 0
        for year in years:
            n = ingest_ipe_year(year, cnpj_to_ticker, conn)
            total_ipe += n
            log.info("  Ano %d: %d linhas", year, n)
        log.info("IPE total: %d linhas", total_ipe)

        log.info("Formulário Consolidado — anos: %s", years)
        total_con = 0
        for year in years:
            n = ingest_consolidated_year(year, cnpj_to_ticker, conn)
            total_con += n
            log.info("  Consolidado %d: %d linhas", year, n)
        log.info("Consolidado total: %d linhas", total_con)

        n_recompra = ingest_recompras(conn)
        log.info("Recompras: %d registros", n_recompra)

        if not args.no_dashboard:
            log.info("Atualizando dashboard …")
            build_dashboard(conn)

    log.info("Concluído. DB: %s", DB_PATH)


if __name__ == "__main__":
    main()
