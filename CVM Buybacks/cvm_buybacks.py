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
  python cvm_buybacks_v2.py              # incremental
  python cvm_buybacks_v2.py --bootstrap  # histórico completo desde 2022
"""
from __future__ import annotations

import argparse
import hashlib
import io
import logging
import re
import sqlite3
import time
import zipfile
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
        for stmt in SCHEMA.split(";"):
            stmt = stmt.strip()
            if stmt:
                try:
                    conn.execute(stmt)
                except sqlite3.OperationalError as e:
                    if "already exists" not in str(e):
                        raise
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
    try:
        import pdfplumber
    except ImportError:
        raise ImportError("pdfplumber not installed — run: pip install pdfplumber")

    def make_key(*parts) -> str:
        raw = "|".join(str(p) for p in parts)
        return hashlib.sha1(raw.encode()).hexdigest()

    def to_date(dia: int | None, ref: str) -> str | None:
        if not dia:
            return None
        y, m = int(ref[:4]), int(ref[5:7])
        return f"{y:04d}-{m:02d}-{dia:02d}"

    def norm_qty(s: str) -> float | None:
        s = s.strip().replace(".", "").replace(",", ".")
        try:
            return float(s)
        except ValueError:
            return None

    def norm_op(words: list[str]) -> str:
        """Junta fragmentos de operação e normaliza."""
        raw = " ".join(words)
        # Normalizar formas quebradas comuns
        raw = re.sub(r"ENTREGA\s+DE\s+AÇÕES\s+RESTRITAS", "Entrega de ações restritas", raw, flags=re.I)
        raw = re.sub(r"ENTREGA\s+DE\s+AÇÕES\s+BÔNUS", "Entrega de ações bônus", raw, flags=re.I)
        raw = re.sub(r"ENTREGA\s+DE\s+AÇÕES\s+BONUS", "Entrega de ações bônus", raw, flags=re.I)
        raw = re.sub(r"Compra\s+à\s+vista", "Compra à vista", raw, flags=re.I)
        raw = re.sub(r"Venda\s+à\s+vista", "Venda à vista", raw, flags=re.I)
        raw = re.sub(r"Compra\s+à\s+termo", "Compra à termo", raw, flags=re.I)
        raw = re.sub(r"Venda\s+à\s+termo", "Venda à termo", raw, flags=re.I)
        raw = re.sub(r"DERIVATIVO\s+COM\s+LIQUIDAÇÃ[O]\s+FINANCEIR[A]",
                     "Derivativo com liquidação financeira", raw, flags=re.I)
        return " ".join(raw.split())

    # Colunas X aproximadas (centro ± tolerância)
    COL_ATIVO   = (30,  90)    # tipo_ativo
    COL_CARACT  = (100, 200)   # característica — pode ir até antes do intermediário
    COL_INTERM  = (190, 255)   # intermediário
    COL_OP      = (245, 315)   # operação
    COL_DIA     = (305, 345)   # dia
    COL_QTY     = (345, 430)   # quantidade
    COL_PRECO   = (425, 490)   # preço
    COL_VOL     = (485, 560)   # volume

    def in_col(x: float, col: tuple) -> bool:
        return col[0] <= x <= col[1]

    rows: list[dict] = []

    # Estado atual de entidade (qualificação, nome)
    current_qual = None
    current_nome = None
    current_section = None  # 'saldo_inicial' | 'movimentacoes' | 'saldo_final'

    # Buffer para montar operação multi-linha
    pending: dict | None = None

    def flush_pending():
        nonlocal pending
        if pending and pending.get("tipo_ativo"):
            dia = pending.get("dia")
            qty = norm_qty(pending.get("qty_str", "")) if pending.get("qty_str") else None
            preco = norm_qty(pending.get("preco_str", "")) if pending.get("preco_str") else None
            vol = norm_qty(pending.get("vol_str", "")) if pending.get("vol_str") else None
            op = norm_op(pending.get("op_words", []))
            caract = " ".join(pending.get("caract_words", [])).strip()
            if not caract:
                caract = pending.get("caract_str", "")
            interm = " ".join(pending.get("interm_words", [])).strip() or None
            nk = make_key(cnpj_digits, data_ref, versao, current_qual,
                          current_nome, pending["tipo_ativo"], caract, op, dia, qty)
            rows.append({
                "cnpj_digits": cnpj_digits,
                "data_referencia": data_ref,
                "versao": versao,
                "qualificacao": current_qual or "other",
                "nome_entidade": current_nome,
                "tipo_ativo": pending["tipo_ativo"],
                "caracteristica": caract,
                "tipo_movimentacao": op if op else "Operação",
                "intermediario": interm,
                "dia": dia,
                "data_movimentacao": to_date(dia, data_ref),
                "quantidade": qty,
                "preco_unitario": preco,
                "volume": vol,
                "natural_key": nk,
            })
        pending = None

    def add_balance(tipo_mov: str, tipo_ativo: str, caract: str, qty_str: str):
        qty = norm_qty(qty_str)
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

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            words = page.extract_words(x_tolerance=3, y_tolerance=3)

            # Agrupar palavras por linha (y0 arredondada)
            from collections import defaultdict
            lines_map: dict[int, list] = defaultdict(list)
            for w in words:
                y_key = round(w["top"] / 4) * 4  # bucket de 4px
                lines_map[y_key].append(w)

            for y_key in sorted(lines_map.keys()):
                line_words = sorted(lines_map[y_key], key=lambda w: w["x0"])
                texts = [(w["x0"], w["text"]) for w in line_words]

                # Junta todos os textos da linha
                full_line = " ".join(t for _, t in texts)

                # --- Detectar cabeçalho de entidade ---
                if "Qualificação:" in full_line or "Qualificação:Companhia" in full_line:
                    qual_raw = re.sub(r".*Qualificação:\s*", "", full_line).strip()
                    if "Tesouraria" in qual_raw:
                        current_qual = "treasury"
                    elif "Controlada" in qual_raw:
                        current_qual = "subsidiary"
                    elif "Coligada" in qual_raw:
                        current_qual = "affiliated"
                    else:
                        current_qual = "other"
                    current_section = None
                    flush_pending()
                    continue

                if "Nome:" in full_line:
                    m = re.search(r"Nome:\s*(.+?)(?:\s{3,}|CPF|$)", full_line)
                    if m:
                        current_nome = m.group(1).strip()
                    continue

                # --- Detectar seções ---
                if "Saldo Inicial" in full_line and "Movimentações" not in full_line:
                    flush_pending()
                    current_section = "saldo_inicial"
                    continue
                if "Movimentações" in full_line:
                    flush_pending()
                    current_section = "movimentacoes"
                    continue
                if "Saldo Final" in full_line and "Valor Mobiliário" not in full_line:
                    flush_pending()
                    current_section = "saldo_final"
                    continue

                # Pular linhas de cabeçalho de tabela
                if any(kw in full_line for kw in [
                    "Valor Mobiliário", "Características dos Títulos",
                    "Mobiliário/Derivativo", "Intermediário", "Operação",
                    "FORMULÁRIO", "Negociação de Valores", "Em 0", "ocorreram",
                    "não foram", "Denominação da Companhia",
                ]):
                    continue

                if not current_section or not current_qual:
                    continue

                # --- Processar Saldo Inicial / Final ---
                if current_section in ("saldo_inicial", "saldo_final"):
                    tipo_mov = "Saldo Inicial" if current_section == "saldo_inicial" else "Saldo Final"

                    # Extrair tipo_ativo (x≈42), caract (x≈122-305), qty (x≈462+)
                    tipo_ativo_w = [t for x, t in texts if in_col(x, COL_ATIVO)]
                    caract_w = [t for x, t in texts if in_col(x, (100, 460))]
                    qty_w = [t for x, t in texts if in_col(x, (460, 560))]

                    if tipo_ativo_w and qty_w:
                        tipo_ativo = tipo_ativo_w[0]
                        caract = " ".join(caract_w).strip()
                        qty_str = qty_w[-1]  # último valor
                        if re.match(r"[\d.,]+$", qty_str.replace(".", "").replace(",", "")):
                            add_balance(tipo_mov, tipo_ativo, caract, qty_str)
                    elif not tipo_ativo_w and caract_w:
                        # Continuação de caract multi-linha (ex: "LIQUIDACAO FINANCEIRA")
                        # Ignorar — já foi capturado na linha anterior via full caract
                        pass

                # --- Processar Movimentações ---
                elif current_section == "movimentacoes":
                    # Detectar início de nova operação: linha com tipo_ativo em COL_ATIVO
                    ativo_w = [t for x, t in texts if in_col(x, COL_ATIVO)]
                    caract_on_line = [t for x, t in texts if in_col(x, COL_CARACT)]
                    interm_on_line = [t for x, t in texts if in_col(x, COL_INTERM)]
                    op_on_line     = [t for x, t in texts if in_col(x, COL_OP)]
                    dia_on_line    = [t for x, t in texts if in_col(x, COL_DIA)]
                    qty_on_line    = [t for x, t in texts if in_col(x, COL_QTY)]
                    preco_on_line  = [t for x, t in texts if in_col(x, COL_PRECO)]
                    vol_on_line    = [t for x, t in texts if in_col(x, COL_VOL)]

                    if ativo_w:
                        # Nova operação — flush anterior
                        flush_pending()
                        pending = {
                            "tipo_ativo": ativo_w[0],
                            "caract_words": caract_on_line,
                            "interm_words": interm_on_line,
                            "op_words": op_on_line,
                            "dia": int(dia_on_line[0]) if dia_on_line and dia_on_line[0].isdigit() else None,
                            "qty_str": qty_on_line[0] if qty_on_line else None,
                            "preco_str": preco_on_line[0] if preco_on_line else None,
                            "vol_str": vol_on_line[0] if vol_on_line else None,
                        }
                    elif pending:
                        # Linha de continuação — acumular op e interm multi-linha
                        if op_on_line:
                            pending["op_words"].extend(op_on_line)
                        if interm_on_line:
                            pending["interm_words"].extend(interm_on_line)
                        if caract_on_line and not pending.get("caract_words"):
                            pending["caract_words"] = caract_on_line
                        # Preencher campos numéricos se ainda vazios
                        if not pending["dia"] and dia_on_line and dia_on_line[0].isdigit():
                            pending["dia"] = int(dia_on_line[0])
                        if not pending["qty_str"] and qty_on_line:
                            pending["qty_str"] = qty_on_line[0]
                        if not pending["preco_str"] and preco_on_line:
                            pending["preco_str"] = preco_on_line[0]
                        if not pending["vol_str"] and vol_on_line:
                            pending["vol_str"] = vol_on_line[0]

    flush_pending()
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

            def flush_op(buf, sec):
                if not buf:
                    return None
                ativo_w, caract_w, interm_w, op_w, dia_w, qty_w, preco_w, vol_w = (
                    [], [], [], [], [], [], [], [])
                for line in buf:
                    for w in sorted(line, key=lambda x: x["x0"]):
                        x = w["x0"]; t = w["text"]
                        if in_c(x, C_ATIVO):   ativo_w.append(t)
                        if in_c(x, C_CARACT):  caract_w.append(t)
                        if in_c(x, C_INTERM):  interm_w.append(t)
                        if in_c(x, C_OP):      op_w.append(t)
                        if in_c(x, C_DIA) and re.match(r"^\d{1,2}$", t):
                            dia_w.append(t)
                        if in_c(x, C_QTY):     qty_w.append(t)
                        if in_c(x, C_PRECO):   preco_w.append(t)
                        if in_c(x, C_VOL):     vol_w.append(t)

                tipo_ativo    = " ".join(ativo_w).strip() or None
                caracteristica = " ".join(caract_w).strip() or None
                intermediario  = " ".join(interm_w).strip() or None
                operacao       = " ".join(op_w).strip() or None
                dia            = int(dia_w[0]) if dia_w else None
                quantidade     = norm_num(" ".join(qty_w))
                preco          = norm_num(" ".join(preco_w))
                volume         = norm_num(" ".join(vol_w))

                # Saldos: quantidade em coluna mais à direita
                if sec in ("saldo_inicial", "saldo_final") and not quantidade:
                    sw = []
                    for line in buf:
                        for w in sorted(line, key=lambda x: x["x0"]):
                            if in_c(w["x0"], C_SALDO):
                                sw.append(w["text"])
                    quantidade = norm_num(" ".join(sw))

                tipo_mov = (
                    "Saldo Inicial" if sec == "saldo_inicial"
                    else "Saldo Final" if sec == "saldo_final"
                    else operacao
                )
                if not tipo_mov:
                    return None

                nk = make_key(cnpj_digits, data_ref, versao, grupo_label, tipo_mov, dia, quantidade, preco)
                return {
                    "cnpj_digits":       cnpj_digits,
                    "data_referencia":   data_ref,
                    "versao":            versao,
                    "grupo":             grupo_label,
                    "tipo_ativo":        tipo_ativo or "Ações",
                    "caracteristica":    caracteristica,
                    "tipo_movimentacao": tipo_mov,
                    "intermediario":     intermediario,
                    "dia":               dia,
                    "data_movimentacao": to_date(dia, data_ref),
                    "quantidade":        quantidade,
                    "preco_unitario":    preco,
                    "volume":            volume,
                    "natural_key":       nk,
                }

            for y in sorted_ys:
                line = sorted(lines_map[y], key=lambda w: w["x0"])
                text = " ".join(w["text"] for w in line).lower()

                if "saldo inicial" in text:
                    if op_buffer:
                        rec = flush_op(op_buffer, section)
                        if rec: rows.append(rec)
                    op_buffer = []; section = "saldo_inicial"; continue
                elif "movimenta" in text and ("mês" in text or "mes" in text):
                    if op_buffer:
                        rec = flush_op(op_buffer, section)
                        if rec: rows.append(rec)
                    op_buffer = []; section = "movimentacoes"; continue
                elif "saldo final" in text:
                    if op_buffer:
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
    Retorna True apenas se todos os 5 grupos com Saldo Final já estão no banco.
    Isso garante que PDFs com grupos perdidos pelo bug do checkbox (ex: Diretoria)
    sejam re-parseados e completados corretamente.
    """
    r = conn.execute(
        "SELECT COUNT(DISTINCT grupo) FROM consolidated_positions "
        "WHERE cnpj_digits=? AND data_referencia=? AND versao=? "
        "AND tipo_movimentacao='Saldo Final'",
        (cnpj_digits, data_ref, versao),
    ).fetchone()
    return r[0] >= 5


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
# MAIN
# ============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="CVM Buybacks v2 — IPE Individual")
    parser.add_argument("--bootstrap", action="store_true",
                        help=f"Puxa histórico completo desde {BOOTSTRAP_START_YEAR}")
    args = parser.parse_args()

    init_db()
    cnpj_to_ticker = {v: k for k, v in TICKERS.items()}

    with db_conn() as conn:
        # Determinar anos a processar
        current_year = date.today().year
        if args.bootstrap:
            years = list(range(BOOTSTRAP_START_YEAR, current_year + 1))
        else:
            # Incremental: apenas ano atual (e anterior em dezembro)
            years = [current_year]
            if date.today().month == 1:
                years.insert(0, current_year - 1)

        log.info("Iniciando ingestão IPE Individual para anos: %s", years)
        total_ipe = 0
        for year in years:
            n = ingest_ipe_year(year, cnpj_to_ticker, conn)
            total_ipe += n
            log.info("Ano %d: %d linhas inseridas", year, n)

        log.info("IPE total: %d linhas", total_ipe)

        # Formulário Consolidado — posição por grupo (Controlador, CA, Diretoria, CF, Órgãos)
        log.info("Iniciando ingestão Formulário Consolidado para anos: %s", years)
        total_consolidated = 0
        for year in years:
            n = ingest_consolidated_year(year, cnpj_to_ticker, conn)
            total_consolidated += n
            log.info("Consolidado ano %d: %d linhas inseridas", year, n)

        log.info("Consolidado total: %d linhas", total_consolidated)

        # Recompras
        n_recompra = ingest_recompras(conn)
        log.info("Recompras: %d registros inseridos", n_recompra)

    log.info("Concluído. DB: %s", DB_PATH)


if __name__ == "__main__":
    main()
