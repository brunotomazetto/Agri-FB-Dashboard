#!/usr/bin/env python3
"""
extractor_imea.py — Agri Monitor
======================================
Execução mensal via GitHub Actions.

Fluxo:
  1. Autentica no portal IMEA
  2. Extrai custos agrícolas (SOJA, MILHO, ALGODÃO – MT) via API IMEA
  3. Extrai produtividade CONAB via API CONAB (safra/levantamento mensal)
  4. Extrai preços ao produtor CONAB via API CONAB
  5. Calcula P&L e margens (tudo em R$/ha)
  6. Atualiza dash_data.json e imea_margin_dashboard.html

═══════════════════════════════════════════════════════════════
FONTES POR TIPO DE DADO
═══════════════════════════════════════════════════════════════

  CUSTOS    → Portal IMEA (API autenticada)
              grupo CUSTO, indicador_id IS NOT NULL → portal mensal
              indicador_id IS NULL, safra_tipo='mensal' → projeção interna
              indicador_id IS NULL, safra_tipo='anual'  → histórico interno

  PREÇO     → API CONAB portaldeinformacoes.conab.gov.br
              Soja:    "SOJA EM GRÃOS (60 kg)"           ao produtor MT
              Milho:   "MILHO EM GRÃOS (60 kg)"          ao produtor MT
              Algodão: "ALGODÃO EM PLUMA TIPO BÁSICO..." ao produtor MT
              Unidade armazenada: R$/kg → ×bag_kg = R$/bag

  PRODUTIV. → API CONAB portaldeinformacoes.conab.gov.br (tabela conab_safra)
              Soja:    produto='SOJA'            uf='MT'            → sc/ha (t/ha ÷ 60)
              Milho:   produto='MILHO' safra='2ª SAFRA' uf='MT'    → sc/ha (t/ha ÷ 60)
              Algodão: produto='ALGODAO EM PLUMA' uf='MT'          → @/ha lint (t/ha ÷ 15)
              Fonte mensal: levantamentos 1→12 por safra; lev=99 = Série Histórica (final)
              Script busca lev mais recente disponível para safra corrente.

═══════════════════════════════════════════════════════════════
REGRAS DE NEGÓCIO
═══════════════════════════════════════════════════════════════

UNIDADES (tudo em R$/ha no dashboard):
  Receita = prod(bag/ha) × preço(R$/bag) = R$/ha
  Custos IMEA já em R$/ha
  Toggle bag/ha: val ÷ spot(R$/bag) → bag/ha ou @/ha conforme cmdty

SAFRA LABEL (mensal):
  SOJA 2022 interno : sem shift (header correto)
  SOJA portal       : shift -1  (portal guarda 1 ano à frente)
  MILHO/ALGODÃO     : sem shift
  mensal interno    : sem shift

SEEDS = Sementes + Semente de Cobertura (todas as culturas)

ANNUAL SNAPS (hardcoded — melhor data de custo e preço/yield por safra):
  histórico interno: custo publicado 1 ano após fechamento
    SOJA y1/y2    → preço/yield em Set/y2
    MILHO y1/y2   → preço/yield em Dez/y1
    ALGODÃO y1/y2 → preço/yield em Dez/y1
"""

import os, json, sqlite3, re, logging, time
from datetime import datetime, date
from pathlib import Path

import requests
import pandas as pd

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Caminhos ──────────────────────────────────────────────────────────────────
DB_PATH   = Path(__file__).parent / "imea.db"
DASH_PATH = Path(__file__).parent / "imea_margin_dashboard.html"
JSON_PATH = Path(__file__).parent / "dash_data.json"

# ── Credenciais IMEA ──────────────────────────────────────────────────────────
IMEA_API  = "https://api1.imea.com.br"
IMEA_USER = os.environ["IMEA_USER"]
IMEA_PASS = os.environ["IMEA_PASS"]

# ── CONAB — arquivos bulk TXT (mesmo método do extractor_conab.py) ─────────────
CONAB_BASE       = "https://portaldeinformacoes.conab.gov.br/downloads/arquivos"
CONAB_GRAOS_URL  = f"{CONAB_BASE}/LevantamentoGraos.txt"   # produtividade
CONAB_PRECO_URL  = f"{CONAB_BASE}/PrecosMensalUF.txt"       # preços ao produtor por UF

# ── IDs portal IMEA ───────────────────────────────────────────────────────────
GRUPO_CUSTO = "1121328740175912960"

# ── Config por cultura ────────────────────────────────────────────────────────
CULTURAS = {
    "SOJA": {
        "cadeia_id":       4,
        "conab_preco":     "SOJA EM GRÃOS   (60 kg)",
        "conab_nivel":     "PRODUTOR",
        "conab_produto":   "SOJA",          # para tabela conab_safra
        "conab_safra":     None,            # qualquer (UNICA)
        "bag_kg":          60,
        "portal_shift":    -1,              # DEPRECATED — não usado mais (ver safra_label_monthly)
    },
    "MILHO": {
        "cadeia_id":       3,
        "conab_preco":     "MILHO EM GRÃOS   (60 kg)",
        "conab_nivel":     "PRODUTOR",
        "conab_produto":   "MILHO",
        "conab_safra":     "2ª SAFRA",       # safrinha MT
        "bag_kg":          60,
        "portal_shift":    0,                # DEPRECATED — não usado mais
    },
    "ALGODAO": {
        "cadeia_id":       1,
        "conab_preco":     "ALGODÃO EM PLUMA TIPO BÁSICO - SLM 41-4 BRANCO  (15 kg)",
        "conab_nivel":     "PRODUTOR",
        "conab_produto":   "ALGODAO EM PLUMA",
        "conab_safra":     None,
        "bag_kg":          15,              # 1 arroba = 15 kg
        "portal_shift":    0,                # DEPRECATED — não usado mais
    },
}

CURRENT_YEAR = date.today().year

# ── Annual snaps (custo_date, label, tipo) ────────────────────────────────────
# tipo='anual' → histórico interno: preço/yield buscado em data real fechamento safra
# tipo=None    → portal/mensal interno
ANNUAL_SNAPS = {
    "SOJA": [
        ("2020-09", "2019/20",  "anual"),
        ("2021-09", "2020/21",  "anual"),
        ("2022-09", "2021/22",  "anual"),
        ("2023-09", "2022/23",  None),
        ("2024-09", "2023/24",  None),
        ("2025-09", "2024/25",  None),
        ("2025-09", "2025/26",  None),
        ("2026-02", "2026/27e", None),
    ],
    "MILHO": [
        ("2021-12", "2020/21",  "anual"),
        ("2022-12", "2021/22",  "anual"),
        ("2023-12", "2022/23",  "anual"),
        ("2023-12", "2023/24",  None),
        ("2024-12", "2024/25",  None),
        ("2025-12", "2025/26",  None),
        ("2026-02", "2026/27e", None),
    ],
    "ALGODAO": [
        ("2022-12", "2022/23",  "anual"),
        ("2023-12", "2023/24",  "anual"),
        ("2024-12", "2024/25",  "anual"),
        ("2025-12", "2025/26",  None),
        ("2026-02", "2026/27e", None),
    ],
}

# ── Custo helpers ─────────────────────────────────────────────────────────────
OTHER_C = ["Funrural","Fethab I","Fethab II","ITR","Outros Impostos e Taxas"]
OTHER_D = ["Financiamentos","Seguro da Produção","Seguro Máq. Equip. Utilit."]
OTHER_E = ["Classificação e Beneficiamento","Armazenagem","Transporte da Produção"]
OTHER_F = ["Assistência Técnica","Combustível Utilitários","Despesas Gerais"]


# ════════════════════════════════════════════════════════════════════════════════
# BANCO DE DADOS
# ════════════════════════════════════════════════════════════════════════════════
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS historico (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        cultura         TEXT,
        cadeia_id       INTEGER,
        indicador_id    TEXT,
        indicador_nome  TEXT,
        safra           TEXT,
        safra_id        TEXT,
        safra_tipo      TEXT,
        data_referencia TEXT,
        ano             INTEGER,
        mes             INTEGER,
        valor           REAL,
        unidade         TEXT,
        estado          TEXT,
        grupo           TEXT,
        updated_at      TEXT
    );
    CREATE TABLE IF NOT EXISTS preco_conab (
        id                    INTEGER PRIMARY KEY AUTOINCREMENT,
        cultura               TEXT,
        produto_conab         TEXT,
        nivel_comercializacao TEXT,
        data_referencia       TEXT,
        valor_kg              REAL,
        updated_at            TEXT,
        UNIQUE(cultura, produto_conab, nivel_comercializacao, data_referencia)
    );
    CREATE TABLE IF NOT EXISTS conab_safra (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        produto            TEXT,
        cultura            TEXT,
        uf                 TEXT,
        ano_agricola       TEXT,
        safra              TEXT,
        id_levantamento    INTEGER,
        dsc_levantamento   TEXT,
        produtividade_t_ha REAL,
        prod_bag_ha        REAL,
        bag_kg             INTEGER,
        updated_at         TEXT,
        UNIQUE(produto, uf, ano_agricola, safra, id_levantamento)
    );
    CREATE INDEX IF NOT EXISTS idx_hist_cultura_grupo
        ON historico(cultura, grupo, data_referencia);
    CREATE INDEX IF NOT EXISTS idx_hist_ind
        ON historico(cultura, indicador_id, data_referencia);
    CREATE INDEX IF NOT EXISTS idx_preco_cultura
        ON preco_conab(cultura, data_referencia);
    CREATE INDEX IF NOT EXISTS idx_conab_safra_lookup
        ON conab_safra(cultura, uf, ano_agricola, id_levantamento);
    """)
    conn.commit()


# ════════════════════════════════════════════════════════════════════════════════
# FETCH — PORTAL IMEA (custos)
# ════════════════════════════════════════════════════════════════════════════════
def imea_token():
    """
    Autentica no portal IMEA Digital.
    Campos obrigatórios descobertos via DevTools:
      username, password, grant_type, client_id=2
    Sem client_id → 400 Bad Request.
    """
    r = requests.post(
        f"{IMEA_API}/token",
        data={
            "username":   IMEA_USER,
            "password":   IMEA_PASS,
            "grant_type": "password",
            "client_id":  "2",
        },
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def imea_get(token, path, **params):
    r = requests.get(
        f"{IMEA_API}{path}",
        headers={"Authorization": f"Bearer {token}"},
        params=params, timeout=60,
    )
    r.raise_for_status()
    return r.json()


def fetch_imea_custo(conn, token, cultura, cadeia_id, now_str):
    """
    Extrai custos mensais do IMEA via Excel S3.

    Novo fluxo (API /grupo/.../indicadores foi descontinuada):
      1. GET api1.imea.com.br/api/arquivo?cadeia=X&tipo=... → lista Excel disponíveis
      2. Seleciona o arquivo "Mensal Transgênica/GMO" (mais completo)
         Fallback: qualquer arquivo "Mensal"
      3. Download do Excel do S3 (URL pré-assinada, sem auth adicional)
      4. Lê aba MT (ex: Soja_GMO_MT, Milho_GMO_MT, Algodao_GMO_MT)
      5. Parseia linhas de custo → insere em historico

    Estrutura do Excel (aba *_MT):
      L6:  Safra   | 2026/27 | 2026/27 | ...
      L7:  Ano     | 2026    | 2026    | ...
      L8:  Mês     | Janeiro | Fevereiro | Março* | ...
      L9+: indicador_nome | val_jan | val_fev | ...
    """
    import io as _io

    TIPO_ID = "696277432068079616"  # Custo de Produção

    log.info(f"  [{cultura}] Buscando CUSTO via Excel IMEA")

    # ── 1. Listar arquivos disponíveis ────────────────────────────────────────
    try:
        r = requests.get(
            f"{IMEA_API}/api/arquivo",
            params={"cadeia": cadeia_id, "tipo": TIPO_ID,
                    "page": 1, "pageSize": 50, "nome": "", "sort": 1},
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        r.raise_for_status()
        arquivos = r.json().get("Result", [])
    except Exception as e:
        log.warning(f"  [{cultura}] Listagem de arquivos falhou: {e}")
        return 0

    if not arquivos:
        log.warning(f"  [{cultura}] Nenhum arquivo encontrado")
        return 0

    # ── 2. Selecionar arquivo "Mensal Transgênica/GMO" ────────────────────────
    # Preferência por cultura:
    #   Soja/Milho: "Mensal Transgênica/GMO" > qualquer "Mensal"
    #   Algodão:    "Mensal (R$/ha)" >> rejeita "¢/lb" (unidade errada)
    def score(a):
        n = a.get("Nome", "").lower()
        # Rejeitar qualquer arquivo em ¢/lb (unidade errada para nosso banco)
        if "¢/lb" in n or ("¢" in n and "lb" in n):
            return 99
        # Soja/Milho: priorizar GMO/Transgênica
        if "mensal" in n and ("gmo" in n or "transgên" in n or "transgen" in n):
            return 0
        # Algodão R$/ha ou qualquer outro Mensal
        if "mensal" in n and "r$/ha" in n:
            return 0
        if "mensal" in n:
            return 1
        return 2

    arquivos_sorted = sorted(arquivos, key=score)
    arquivo = arquivos_sorted[0]
    if score(arquivo) == 99:
        log.warning(f"  [{cultura}] Nenhum arquivo adequado encontrado (todos em ¢/lb?)")
        return 0
    log.info(f"  [{cultura}] Usando: {arquivo['Nome']}")

    # ── 3. Download do Excel ──────────────────────────────────────────────────
    try:
        r = requests.get(arquivo["Path"], timeout=60)
        r.raise_for_status()
        xls_bytes = r.content
    except Exception as e:
        log.warning(f"  [{cultura}] Download falhou: {e}")
        return 0

    # ── 4. Identificar aba MT ─────────────────────────────────────────────────
    try:
        xls = pd.ExcelFile(_io.BytesIO(xls_bytes))
    except Exception as e:
        log.warning(f"  [{cultura}] Erro ao abrir Excel: {e}")
        return 0

    # Aba MT: ex "Soja_GMO_MT", "Milho_GMO_MT", "Algodao_GMO_MT"
    aba_mt = next(
        (s for s in xls.sheet_names
         if s.endswith("_MT") and "mensal" not in s.lower()),
        None
    )
    if not aba_mt:
        # Fallback: primeira aba que contenha "MT"
        aba_mt = next((s for s in xls.sheet_names if "MT" in s.upper()), None)
    if not aba_mt:
        log.warning(f"  [{cultura}] Aba MT não encontrada. Abas: {xls.sheet_names}")
        return 0

    log.info(f"  [{cultura}] Aba: {aba_mt}")
    df = pd.read_excel(_io.BytesIO(xls_bytes), sheet_name=aba_mt, header=None)

    # ── 5. Parsear cabeçalho (safra, ano, mês) ────────────────────────────────
    MESES_PT = {
        "janeiro":1,"fevereiro":2,"março":3,"abril":4,"maio":5,"junho":6,
        "julho":7,"agosto":8,"setembro":9,"outubro":10,"novembro":11,"dezembro":12,
    }

    # Encontrar linhas de header (Safra, Ano, Mês)
    safra_row = ano_row = mes_row = None
    for i, row in df.iterrows():
        first = str(row.iloc[0]).strip().lower()
        if first == "safra":   safra_row = i
        elif first == "ano":   ano_row   = i
        elif first == "mês" or first == "mes": mes_row = i
        if safra_row and ano_row and mes_row:
            break

    if mes_row is None:
        log.warning(f"  [{cultura}] Linha de mês não encontrada")
        return 0

    # Mapear coluna → (ano, mes, safra, data_ref)
    # Limita a 500 colunas para evitar processar os 16384 do Excel de algodão
    cols_meta = {}
    max_col = min(len(df.columns), 500)
    for col in range(1, max_col):
        try:
            mes_str = str(df.iloc[mes_row, col]).strip().lower().replace("*","").replace(" ","")
            mes_num = MESES_PT.get(mes_str)
            if not mes_num:
                continue
            ano = int(float(str(df.iloc[ano_row, col]).strip()))
            if ano < 2000 or ano > 2050:  # sanity check
                continue
            safra = str(df.iloc[safra_row, col]).strip() if safra_row is not None else ""
            data_ref = f"{ano:04d}-{mes_num:02d}-15"
            cols_meta[col] = {"ano": ano, "mes": mes_num,
                              "safra": safra, "data_ref": data_ref}
        except Exception:
            continue

    if not cols_meta:
        log.warning(f"  [{cultura}] Nenhuma coluna de mês encontrada")
        return 0


    # ── 6. Parsear e inserir — lê tudo da planilha, sem inventar nada ───────
    # A planilha já tem todos os agregados (A. CUSTEIO, COE, COT, CT, etc.)
    # Lemos cada linha e mapeamos o nome para o equivalente da API histórica.
    # Zero premissas inventadas — tudo vem da IMEA.

    # Linhas de metadados que não são custos
    SKIP_EXACT = {
        "Unidade: R$/ha.", "Fonte: Imea.", "Unidade: R$/ha",
    }
    SKIP_PREFIXES = (
        "produtividade", "dólar", "dollar", "nota:", "setembro", "**",
        "*estimativa", "*a produtividade",
    )

    # Mapeamento nome planilha → nome banco (compatível com API histórica)
    # Seções agregadas da planilha → nomes que o dashboard consome
    NOME_MAP = {
        "A. CUSTEIO (1+2...+6)":                                  "Custeio",
        "B. MANUTENÇÃO":                                           "Manutenção",
        "C. IMPOSTOS E TAXAS":                                     "Impostos e Taxas",
        "D. FINANCEIRAS":                                          "Financeiras",
        "E. PÓS-PRODUÇÃO":                                        "Pós-Produção",
        "F. OUTROS CUSTOS":                                        "Outros Custos",
        "G. ARRENDAMENTO":                                         "Arrendamento",
        "H. DEPRECIAÇÕES":                                        "Depreciações",
        "I. MÃO-DE-OBRA FAMILIAR":                                "Mão-de-obra Familiar",
        "J. CUSTO DE OPORTUNIDADE":                               "Custo de Oportunidade",
        "6. MÃO DE OBRA":                                          "Mão de Obra",
        "1. SEMENTES":                                             "Sementes",
        "2. FERTILIZANTES E CORRETIVOS":                           "Fertilizantes e Corretivos",
        "3. DEFENSIVOS":                                           "Defensivos",
        "4. OPERAÇÕES MECANIZADAS (óleo diesel e lubrificantes)":  "OPERAÇÕES MECANIZADAS",
        "4. OPERAÇÕES MECANIZADAS":                                "OPERAÇÕES MECANIZADAS",
        "5. SERVIÇOS TERCEIRIZADOS":                               "Serviços Terceirizados",
        # Totais — mapeados para os nomes exatos do banco histórico
        "COE (A + B + ... + F + G)":  "Custo Operacional Efetivo",
        "COT (COE + H + I)":          "Custo Operacional Total",
        "CT (COT + J)":               "Custo Total",
    }

    inserted = 0

    for i, row in df.iterrows():
        if i <= mes_row:
            continue

        ind_nome_raw = str(row.iloc[0]).strip()
        if not ind_nome_raw or ind_nome_raw.lower() == "nan":
            continue
        if ind_nome_raw in SKIP_EXACT:
            continue
        if any(ind_nome_raw.lower().startswith(p) for p in SKIP_PREFIXES):
            continue

        ind_nome = NOME_MAP.get(ind_nome_raw, ind_nome_raw)

        for col, meta in cols_meta.items():
            try:
                val = float(str(row.iloc[col]).strip().replace(",", "."))
            except (ValueError, IndexError):
                continue

            data_ref = meta["data_ref"]
            exists = conn.execute(
                """SELECT 1 FROM historico
                   WHERE cultura=? AND indicador_nome=?
                   AND data_referencia=? AND grupo='CUSTO'
                   AND indicador_id IS NOT NULL""",
                (cultura, ind_nome, data_ref)
            ).fetchone()
            if exists:
                continue

            # A safra gravada no banco é sempre a calculada por calendário
            # (calc_safra_label), não o rótulo cru da coluna "Safra" do
            # Excel. Isso evita duas fontes de verdade divergentes para o
            # mesmo mês (ex.: a IMEA já republicando abr-jun/2026 como
            # "2026/27e" — projeção antecipada da próxima safra — enquanto
            # o calendário real ainda considera esses meses parte da safra
            # 2025/26, em andamento). Com uma única regra determinística
            # decidindo a safra na hora de gravar, nunca existem duas
            # linhas para o mesmo (cultura, indicador, mês) sob rótulos de
            # safra diferentes — a informação nunca duplica.
            safra_correta = calc_safra_label(cultura, data_ref[:7])

            conn.execute(
                """INSERT INTO historico
                   (cultura, cadeia_id, indicador_id, indicador_nome,
                    safra, safra_id, safra_tipo,
                    data_referencia, ano, mes, valor,
                    unidade, estado, grupo, updated_at)
                   VALUES (?,?,?,?,?,NULL,'mensal',?,?,?,?,
                           'R$/ha','MT','CUSTO',?)""",
                (cultura, cadeia_id,
                 f"xlsx_{cultura.lower()}_{ind_nome[:40]}",
                 ind_nome,
                 safra_correta, data_ref,
                 meta["ano"], meta["mes"], round(val, 4),
                 now_str),
            )
            inserted += 1

    conn.commit()
    log.info(f"  [{cultura}] CUSTO: {inserted} novas linhas inseridas")
    return inserted


# ════════════════════════════════════════════════════════════════════════════════
# HELPERS CONAB TXT — mesmo padrão do extractor_conab.py
# ════════════════════════════════════════════════════════════════════════════════
def _baixa_conab_txt(url):
    """Baixa e parseia arquivo .txt bulk da CONAB (latin1, sep=';')."""
    import io as _io
    r = requests.get(url, timeout=180, verify=False,
        headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    df = pd.read_csv(_io.StringIO(r.content.decode("latin1")), sep=";", dtype=str)
    df.columns = [c.strip() for c in df.columns]
    for c in df.columns:
        df[c] = df[c].str.strip()
    return df


def _parse_float_br(val):
    """Converte número BR '1.868,7' ou decimal '263.7' para float."""
    try:
        s = str(val).strip()
        if "," in s and "." in s:
            s = s.replace(".", "").replace(",", ".")
        elif "," in s:
            s = s.replace(",", ".")
        return float(s)
    except (ValueError, AttributeError):
        return None


def _normaliza_lev(val):
    """1-12 levantamentos mensais + 99 Série Histórica. Outros descartados."""
    try:
        v = int(str(val).strip())
        if 1 <= v <= 12 or v == 99:
            return v
        return None
    except (ValueError, TypeError):
        return None


# ════════════════════════════════════════════════════════════════════════════════
# FETCH — CONAB PREÇOS via PrecosMensalUF.txt
# ════════════════════════════════════════════════════════════════════════════════
# ════════════════════════════════════════════════════════════════════════════════
# FETCH — CONAB PREÇOS via Pentaho CDA (portaldeinformacoes.conab.gov.br)
# ════════════════════════════════════════════════════════════════════════════════
# ════════════════════════════════════════════════════════════════════════════════
# FETCH — CONAB PREÇOS via PrecosSemanalUF.txt
# ════════════════════════════════════════════════════════════════════════════════
def fetch_conab_preco(conn, now_str):
    """
    Baixa PrecosSemanalUF.txt e atualiza preco_conab para as 3 culturas.
    Mesmo método do extractor_crushing_spread.py que já funciona no GitHub Actions.

    Arquivo: portaldeinformacoes.conab.gov.br/downloads/arquivos/PrecosSemanalUF.txt
    Produto filtro: 'SOJA', 'MILHO', 'ALGODAO EM PLUMA' (match exato após strip)
    Nível filtro: contém 'RECEBIDO' (= preço recebido pelo produtor)
    UF: MT
    Frequência: semanal — agrega por mês (média mensal) para preco_conab

    Armazena valor_kg = preco_kg na tabela preco_conab (R$/kg).
    Conversão no dashboard: valor_kg × bag_kg = R$/bag
    """
    CONAB_SEMANAL_URL = (
        "https://portaldeinformacoes.conab.gov.br/downloads/arquivos/PrecosSemanalUF.txt"
    )

    # Mapeamento produto no arquivo → (cultura, bag_kg, prod_label, nivel_label, min_kg, max_kg)
    # prod_label e nivel_label devem ser IDÊNTICOS à série histórica no banco.
    # min_kg/max_kg: sanity check para rejeitar valores claramente errados.
    # O PrecosSemanalUF.txt mistura níveis — alguns registros de MILHO chegam
    # com R$1.63/kg (atacado) em vez de R$0.80/kg (produtor). O filtro descarta.
    PROD_MAP = {
        "SOJA": (
            "SOJA", 60,
            "SOJA EM GRÃOS   (60 kg)", "PRODUTOR",
            1.00, 4.00,   # R$/kg razoável: R$60-240/sc
        ),
        "MILHO": (
            "MILHO", 60,
            "MILHO EM GRÃOS   (60 kg)", "PRODUTOR",
            0.40, 1.20,   # R$/kg razoável: R$24-72/sc (exclui atacado ~R$1.63/kg)
        ),
        "ALGODAO EM PLUMA": (
            "ALGODAO", 15,
            "ALGODÃO EM PLUMA TIPO BÁSICO - SLM 41-4 BRANCO  (15 kg)", "PRODUTOR",
            5.00, 15.00,  # R$/kg razoável: R$75-225/@
        ),
    }

    log.info("  [CONAB] Buscando preços via PrecosSemanalUF.txt")
    try:
        content = _baixa_conab_txt(CONAB_SEMANAL_URL)
        # _baixa_conab_txt retorna DataFrame — mas aqui o arquivo pode ter
        # separador diferente, então fazemos raw download
    except Exception:
        pass

    # Download raw para detectar separador (igual ao crushing spread)
    import io as _io
    try:
        r = requests.get(CONAB_SEMANAL_URL, timeout=180, verify=False,
            headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        content_bytes = r.content
    except Exception as e:
        log.warning(f"  [CONAB] Preço falhou: {e}")
        return 0

    # Detectar encoding e separador
    for enc in ("latin-1", "utf-8-sig", "utf-8"):
        try:
            text = content_bytes.decode(enc)
            break
        except UnicodeDecodeError:
            continue

    first_line = text.splitlines()[0] if text.splitlines() else ""
    sep = "\t" if "\t" in first_line else ";"
    df = pd.read_csv(_io.StringIO(text), sep=sep, on_bad_lines="skip", dtype=str)
    df.columns = [c.strip().upper() for c in df.columns]
    log.info(f"  [CONAB] Preço — {len(df)} linhas | colunas: {list(df.columns)}")

    # Detectar colunas flexivelmente
    uf_col    = next((c for c in df.columns if c in ("UF", "SIGLA_UF")), None)
    prod_col  = next((c for c in df.columns if "PRODUTO" in c), None)
    nivel_col = next((c for c in df.columns if "NIVEL" in c or "COMERCI" in c), None)
    date_col  = next((c for c in df.columns if "DATA" in c), None)
    preco_col = next((c for c in df.columns if "VALOR" in c or "PRECO" in c), None)

    if not all([uf_col, prod_col, date_col, preco_col]):
        log.warning(f"  [CONAB] Colunas não encontradas: {list(df.columns)}")
        return 0

    # Filtrar MT + produtos de interesse + nível RECEBIDO
    df = df[df[uf_col].str.strip().str.upper() == "MT"].copy()
    df = df[df[prod_col].str.strip().str.upper().isin(PROD_MAP.keys())].copy()
    if nivel_col:
        df = df[df[nivel_col].str.strip().str.upper().str.contains("RECEBIDO", na=False)].copy()

    log.info(f"  [CONAB] Preço MT filtrado: {len(df)} linhas")

    # Agregar semanal → mensal (média por mês/produto)
    # DATA_INICIAL_FINAL_SEMANA: "DD-MM-YYYY - DD-MM-YYYY" — pega a data inicial
    records = {}  # (cultura, ym) → [preco_kg]
    n_fail = 0
    for _, row in df.iterrows():
        prod_key = str(row[prod_col]).strip().upper()
        if prod_key not in PROD_MAP:
            continue

        # Parsear data
        raw_field = str(row.get(date_col, "")).strip()
        raw_date  = raw_field.split(" - ")[0].strip().replace("-", "/")
        dr = None
        for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d"):
            try:
                dr = datetime.strptime(raw_date, fmt)
                break
            except Exception:
                continue
        if not dr:
            n_fail += 1
            continue

        preco_kg = _parse_float_br(row.get(preco_col))
        if not preco_kg or preco_kg <= 0:
            continue

        cultura, bag_kg, prod_label, nivel, min_kg, max_kg = PROD_MAP[prod_key]

        # Sanity check: rejeita valores fora do range esperado ao produtor
        if not (min_kg <= preco_kg <= max_kg):
            continue

        ym = dr.strftime("%Y-%m")
        key = (cultura, ym)
        records.setdefault(key, []).append(preco_kg)

    if n_fail:
        log.warning(f"  [CONAB] {n_fail} linhas com data inválida ignoradas")

    # Inserir (ou atualizar) média mensal em preco_conab.
    # INSERT OR REPLACE garante que cada (cultura, produto, nivel, data) tenha
    # exatamente 1 linha — rodadas repetidas atualizam o valor em vez de duplicar.
    inserted = updated = 0
    for (cultura, ym), precos in records.items():
        _, bag_kg, prod_label, nivel, _, _ = PROD_MAP[
            next(k for k, v in PROD_MAP.items() if v[0] == cultura)
        ]
        avg_kg   = round(sum(precos) / len(precos), 6)
        data_ref = f"{ym}-15"
        existing = conn.execute(
            "SELECT id, valor_kg FROM preco_conab "
            "WHERE cultura=? AND produto_conab=? AND nivel_comercializacao=? "
            "AND strftime('%Y-%m',data_referencia)=?",
            (cultura, prod_label, nivel, ym),
        ).fetchone()
        if existing:
            if abs(existing[1] - avg_kg) > 1e-6:
                conn.execute(
                    "UPDATE preco_conab SET valor_kg=?, updated_at=? WHERE id=?",
                    (avg_kg, now_str, existing[0]),
                )
                updated += 1
        else:
            try:
                conn.execute(
                    """INSERT INTO preco_conab
                       (cultura,produto_conab,nivel_comercializacao,
                        data_referencia,valor_kg,updated_at)
                       VALUES(?,?,?,?,?,?)""",
                    (cultura, prod_label, nivel, data_ref, avg_kg, now_str),
                )
                inserted += 1
            except Exception:
                pass

    conn.commit()
    log.info(f"  [CONAB] Preço: {inserted} novos | {updated} atualizados | "
             f"{len(records)} meses processados")
    return inserted + updated


# ════════════════════════════════════════════════════════════════════════════════
# FETCH — CONAB PRODUTIVIDADE via LevantamentoGraos.txt
# ════════════════════════════════════════════════════════════════════════════════
def fetch_conab_safra(conn, now_str):
    """
    Baixa LevantamentoGraos.txt e atualiza conab_safra para as 3 culturas MT.
    Mesmo método do extractor_conab.py — verify=False, latin1, sep=';'.

    Produtos filtrados (nomes exatos no arquivo):
      SOJA, MILHO (2ª SAFRA), ALGODAO EM PLUMA

    Conversão: t/ha × 1000 ÷ bag_kg = bag/ha
      SOJA/MILHO: ÷60 → sc/ha | ALGODÃO: ÷15 → @/ha lint
    """
    log.info("  [CONAB] Buscando produtividade via LevantamentoGraos.txt")
    try:
        df = _baixa_conab_txt(CONAB_GRAOS_URL)
        log.info(f"  [CONAB] Safra — {len(df)} linhas | colunas: {list(df.columns)}")
    except Exception as e:
        log.warning(f"  [CONAB] Produtividade falhou: {e}")
        return 0

    # Mapeamento produto no arquivo → (cultura, bag_kg, safra_filter)
    PROD_MAP = {
        "SOJA":             ("SOJA",    60, None),
        "MILHO":            ("MILHO",   60, "2ª SAFRA"),
        "ALGODAO EM PLUMA": ("ALGODAO", 15, None),
    }

    df_filt = df[
        (df["produto"].isin(PROD_MAP.keys())) &
        (df["uf"] == "MT")
    ].copy()

    # Coluna de produtividade tem nome diferente no arquivo de grãos
    col_prod = next(
        (c for c in df.columns if "produtividade" in c.lower() or "rendimento" in c.lower()),
        None
    )
    if not col_prod:
        log.warning(f"  [CONAB] Coluna produtividade não encontrada: {list(df.columns)}")
        return 0

    inserted = 0
    for _, row in df_filt.iterrows():
        prod_key = row["produto"]
        if prod_key not in PROD_MAP:
            continue
        cultura, bag_kg, safra_filter = PROD_MAP[prod_key]

        safra_tipo = row.get("safra", "")
        if safra_filter and safra_tipo != safra_filter:
            continue

        id_lev = _normaliza_lev(row.get("id_levantamento", ""))
        if id_lev is None:
            continue

        prod_t = _parse_float_br(row.get(col_prod, ""))
        if not prod_t or prod_t <= 0:
            continue

        prod_bag = round(prod_t * 1000 / bag_kg, 4)
        ano_ag   = row.get("ano_agricola", "")

        try:
            conn.execute(
                """INSERT OR REPLACE INTO conab_safra
                   (produto,cultura,uf,ano_agricola,safra,id_levantamento,dsc_levantamento,
                    produtividade_t_ha,prod_bag_ha,bag_kg,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (prod_key, cultura, "MT", ano_ag, safra_tipo, id_lev,
                 row.get("dsc_levantamento", ""),
                 prod_t, prod_bag, bag_kg, now_str),
            )
            if conn.execute("SELECT changes()").fetchone()[0]:
                inserted += 1
        except Exception:
            pass

    conn.commit()
    log.info(f"  [CONAB] Produtividade: {inserted} novos/atualizados registros")
    return inserted


# ════════════════════════════════════════════════════════════════════════════════
# SAFRA LABEL
# ════════════════════════════════════════════════════════════════════════════════
def norm_y(y1):
    suf = "e" if y1 >= CURRENT_YEAR else ""
    return f"{y1}/{str(y1+1)[2:]}{suf}"


def parse_shift(raw, shift=0):
    if not raw: return None
    s = str(raw).replace("e","").replace("E","").strip()
    parts = s.split("/")
    try:
        y1 = int(parts[0]) if len(parts[0])==4 else 2000+int(parts[0])
        return norm_y(y1 + shift)
    except: return None


def calc_safra_label(cultura, ym):
    """
    Regra ÚNICA e determinística de safra a partir de (cultura, 'YYYY-MM').
    Usada tanto pela geração do dataset (safra_label_monthly) quanto pela
    migração de limpeza do banco (migrate_safra_labels) — garante que as
    duas nunca fiquem dessincronizadas de novo.

      SOJA            : início em outubro → Out/ano1 .. Set/ano1+1
      MILHO / ALGODAO : início em janeiro → Jan/ano1 .. Dez/ano1
    """
    ano, mes = int(ym[:4]), int(ym[5:7])
    if cultura == "SOJA":
        y1 = ano if mes >= 10 else ano - 1
    else:  # MILHO, ALGODAO
        y1 = ano
    return norm_y(y1)


def safra_label_monthly(conn, cultura, ym):
    """
    Determina o label de safra para um mês mensal — pelo CALENDÁRIO, não
    mais pela fonte.

    Antes, essa função lia o campo 'safra' gravado no portal/histórico IMEA
    e aplicava um shift fixo (-1 para soja) pra compensar o portal "guardar
    1 ano à frente". Isso quebrava sempre que a própria fonte publicava um
    mês com o rótulo errado (ex.: Out–Dez/2022 SOJA vieram do portal já
    como "2023/24", quando ainda eram 2022/23) — o shift fixo não corrige
    um erro pontual da fonte, só desloca todo mundo igual, então o erro
    passava direto.

    Agora o rótulo é 100% determinístico a partir do calendário de safra
    (calc_safra_label — a mesma regra que safra_inicio() já usa, então os
    dois ficam sempre consistentes entre si). Não depende mais do campo
    'safra' da fonte nem de shift por cultura.
    """
    return calc_safra_label(cultura, ym)


def safra_inicio(conn, cultura, ym, safra_lbl=None):
    lbl = safra_lbl or safra_label_monthly(conn, cultura, ym)
    if lbl:
        try:
            y1 = int(lbl.replace("e","").split("/")[0])
            return f"{y1}-10-01" if cultura == "SOJA" else f"{y1}-01-01"
        except: pass
    ano = int(ym[:4])
    return f"{ano-1}-10-01" if cultura == "SOJA" else f"{ano}-01-01"


def get_price_ym(safra_lbl, cultura, cost_ym):
    """Para anuais históricos internos: data real de fechamento da safra."""
    if not safra_lbl: return cost_ym
    try:
        y1 = int(safra_lbl.replace("e","").split("/")[0])
        return f"{y1+1}-09" if cultura == "SOJA" else f"{y1}-12"
    except: return cost_ym


# ════════════════════════════════════════════════════════════════════════════════
# PRODUTIVIDADE (conab_safra)
# ════════════════════════════════════════════════════════════════════════════════
def get_prod(conn, cultura, ym, safra_lbl=None):
    """
    Retorna produtividade em bag/ha da tabela conab_safra.
      SOJA/MILHO : sc/ha  (bag_kg=60)
      ALGODÃO    : @/ha lint (bag_kg=15)

    Lógica: determina safra pelo label, busca levantamento mais recente.
    Prefere lev=99 (Série Histórica final) quando disponível.
    Fallback: safra mais recente disponível (para projeções futuras).
    """
    lbl = safra_lbl or safra_label_monthly(conn, cultura, ym)
    if not lbl: return None
    ano_ag = lbl.replace("e","").strip()
    r = conn.execute(
        "SELECT prod_bag_ha FROM conab_safra "
        "WHERE cultura=? AND uf='MT' AND ano_agricola=? "
        "ORDER BY id_levantamento DESC LIMIT 1",
        (cultura, ano_ag)).fetchone()
    if r: return round(r[0], 1)
    # Fallback: última safra disponível
    r = conn.execute(
        "SELECT prod_bag_ha FROM conab_safra WHERE cultura=? AND uf='MT' "
        "ORDER BY ano_agricola DESC, id_levantamento DESC LIMIT 1", (cultura,)).fetchone()
    return round(r[0], 1) if r else None


# ════════════════════════════════════════════════════════════════════════════════
# PREÇO (preco_conab)
# ════════════════════════════════════════════════════════════════════════════════
def get_price_spot(conn, cultura, ym):
    """R$/bag: soja/milho ×60kg, algodão ×15kg (@)."""
    cfg = CULTURAS[cultura]
    r = conn.execute(
        "SELECT valor_kg FROM preco_conab WHERE cultura=? AND produto_conab=? "
        "AND nivel_comercializacao=? AND strftime('%Y-%m',data_referencia)=?",
        (cultura, cfg["conab_preco"], cfg["conab_nivel"], ym)).fetchone()
    return round(r[0] * cfg["bag_kg"], 2) if r else None


def get_price_avg(conn, cultura, ym, inicio):
    """Preço médio safra (crop avg) em R$/bag."""
    cfg = CULTURAS[cultura]
    r = conn.execute(
        "SELECT AVG(valor_kg) FROM preco_conab WHERE cultura=? AND produto_conab=? "
        "AND nivel_comercializacao=? AND strftime('%Y-%m',data_referencia)<=? "
        "AND data_referencia>=?",
        (cultura, cfg["conab_preco"], cfg["conab_nivel"], ym, inicio)).fetchone()
    return round(r[0] * cfg["bag_kg"], 2) if r and r[0] else None


# ════════════════════════════════════════════════════════════════════════════════
# QUERIES DE CUSTO
# ════════════════════════════════════════════════════════════════════════════════
def qm(conn, c, ind, ym, safra_lbl=None):
    """Mensal: portal > mensal interno > qualquer.

    Se safra_lbl for informado, prioriza a linha cujo campo 'safra' bate
    com o rótulo esperado pelo calendário (calc_safra_label) para aquele
    mês. Isso evita ambiguidade quando, num mesmo mês, a fonte publica ao
    mesmo tempo o dado real da safra corrente E uma projeção antecipada da
    próxima safra (ex.: abr/2026 com 'safra=2025/26' — o dado real — e
    'safra=2026/27e' — projeção precoce — coexistindo na tabela). Sem esse
    filtro, a query pegaria a que viesse primeiro no banco, o que é
    indeterminístico e pode silenciosamente trocar o dado real pelo da
    projeção futura. Se nada bater com o rótulo esperado, cai no
    comportamento antigo (sem filtro de safra) — preserva compatibilidade
    com todo o histórico onde não há ambiguidade."""
    bases = [
        "SELECT valor FROM historico WHERE cultura=? AND indicador_nome=? AND strftime('%Y-%m',data_referencia)=? AND grupo='CUSTO' AND indicador_id IS NOT NULL",
        "SELECT valor FROM historico WHERE cultura=? AND indicador_nome=? AND strftime('%Y-%m',data_referencia)=? AND grupo='CUSTO' AND safra_tipo='mensal'",
        "SELECT valor FROM historico WHERE cultura=? AND indicador_nome=? AND strftime('%Y-%m',data_referencia)=? AND grupo='CUSTO'",
    ]
    if safra_lbl:
        for base in bases:
            r = conn.execute(base + " AND safra=? LIMIT 1", (c, ind, ym, safra_lbl)).fetchone()
            if r and r[0]: return r[0]
    for base in bases:
        r = conn.execute(base + " LIMIT 1", (c, ind, ym)).fetchone()
        if r and r[0]: return r[0]
    return None


def qa(conn, c, ind, ym):
    """Anual (safra_tipo='anual')."""
    r = conn.execute(
        "SELECT valor FROM historico WHERE cultura=? AND indicador_nome=? "
        "AND strftime('%Y-%m',data_referencia)=? AND grupo='CUSTO' "
        "AND safra_tipo='anual' LIMIT 1", (c, ind, ym)).fetchone()
    return r[0] if r and r[0] else None


def get_seeds(conn, c, ym, anual=False, safra_lbl=None):
    """Seeds = Sementes + Semente de Cobertura."""
    q = qa if anual else (lambda *a: qm(*a, safra_lbl))
    for n in ["Sementes","Semente de Soja","Semente de milho","Semente de Milho","Semente de Algodão"]:
        v = q(conn, c, n, ym)
        if v is not None:
            return round(v + (q(conn, c, "Semente de Cobertura", ym) or 0), 2)
    return None


def get_ferts(conn, c, ym, anual=False, safra_lbl=None):
    q = qa if anual else (lambda *a: qm(*a, safra_lbl))
    v = q(conn, c, "Fertilizantes e Corretivos", ym)
    if v: return v
    return sum(q(conn, c, n, ym) or 0 for n in
               ["Macronutriente","Micronutriente","Corretivo de Solo"]) or None


def get_pests(conn, c, ym, anual=False, safra_lbl=None):
    q = qa if anual else (lambda *a: qm(*a, safra_lbl))
    v = q(conn, c, "Defensivos", ym)
    if v: return v
    return sum(q(conn, c, n, ym) or 0 for n in
               ["Fungicida","Herbicida","Inseticida","Adjuvante/Outros"]) or None


def get_other(conn, c, ym, anual=False, safra_lbl=None):
    """
    Other Costs = F. OUTROS CUSTOS da planilha IMEA
    (Assistência Técnica + Combustível Utilitários + Despesas Gerais)

    Manutenção, Impostos, Financeiras, Pós-Produção e Operações Mecanizadas
    NÃO entram aqui — estão embutidos no COE e nunca foram parte do
    "Other" do dashboard na série histórica da API.
    """
    q = qa if anual else (lambda *a: qm(*a, safra_lbl))
    v = q(conn, c, "Outros Custos", ym)
    if v:
        return v
    # Fallback: soma dos componentes de F se o agregado não existir
    return sum(q(conn, c, n, ym) or 0 for n in OTHER_F) or None


# ════════════════════════════════════════════════════════════════════════════════
# BUILD P&L RECORD (tudo em R$/ha)
# ════════════════════════════════════════════════════════════════════════════════
def build_rec(conn, cultura, ym, s_label, anual=False):
    """
    Constrói registro P&L completo.
    anual=True → custos de cost_ym (histórico interno), preço/yield de price_ym (fechamento real).
    """
    price_ym = get_price_ym(s_label, cultura, ym) if anual else ym
    inicio   = safra_inicio(conn, cultura, price_ym, s_label)
    prod     = get_prod(conn, cultura, price_ym, s_label)
    spot     = get_price_spot(conn, cultura, price_ym)
    std      = get_price_avg(conn, cultura, price_ym, inicio) or spot

    rb_spot = round(spot * prod, 2) if spot and prod else None
    rb_std  = round(std  * prod, 2) if std  and prod else None

    qf = qa if anual else (lambda *a: qm(*a, s_label))
    coe_v = qf(conn, cultura, "Custo Operacional Efetivo", ym)
    if not coe_v:
        cot = qf(conn, cultura, "Custo Operacional Total", ym)
        d2  = qf(conn, cultura, "Depreciações", ym)
        m2  = qf(conn, cultura, "Mão de Obra", ym)
        p2  = (qf(conn, cultura, "Pró-Labore", ym) or
               qf(conn, cultura, "Mão-de-obra Familiar", ym))
        coe_v = (cot - d2 - m2 - p2) if (cot and d2 and m2 and p2) else cot

    arr   = qf(conn, cultura, "Arrendamento", ym)
    dep   = qf(conn, cultura, "Depreciações", ym)
    mo    = qf(conn, cultura, "Mão de Obra", ym)
    pl    = (qf(conn, cultura, "Pró-Labore", ym) or
             qf(conn, cultura, "Mão-de-obra Familiar", ym))
    sem   = get_seeds(conn, cultura, ym, anual, s_label)
    fer   = get_ferts(conn, cultura, ym, anual, s_label)
    pes   = get_pests(conn, cultura, ym, anual, s_label)
    othr  = get_other(conn, cultura, ym, anual, s_label)
    labor = round((mo or 0) + (pl or 0), 2) if (mo or pl) else None
    coe_s = (coe_v - arr) if coe_v and arr else coe_v

    named   = (sem or 0) + (fer or 0) + (pes or 0) + (labor or 0) + (othr or 0)
    gp_ex   = (rb_std - named) if rb_std and named else None
    gp_inc  = (gp_ex  - arr)   if gp_ex is not None and arr else gp_ex
    gm_ex_p = gp_ex  / rb_std  if gp_ex  is not None and rb_std else None
    gm_in_p = gp_inc / rb_std  if gp_inc is not None and rb_std else None

    if anual:
        n = conn.execute(
            "SELECT COUNT(DISTINCT indicador_nome) FROM historico "
            "WHERE cultura=? AND grupo='CUSTO' AND strftime('%Y-%m',data_referencia)=? "
            "AND safra_tipo='anual'", (cultura, ym)).fetchone()[0]
    else:
        n = conn.execute(
            "SELECT COUNT(DISTINCT indicador_nome) FROM historico "
            "WHERE cultura=? AND grupo='CUSTO' AND strftime('%Y-%m',data_referencia)=? "
            "AND (safra_tipo='mensal' OR indicador_id IS NOT NULL)", (cultura, ym)).fetchone()[0]

    def r(v):  return round(v, 2) if v is not None else None
    def r4(v): return round(v, 4) if v is not None else None
    return {
        "d": ym, "safra": s_label,
        "spot": spot, "std": std,   # R$/bag — usado pelo toggle bag/ha
        "prod": prod,               # bag/ha (sc/ha soja/milho, @/ha algodão)
        "rb_spot": rb_spot, "rb_std": rb_std,  # R$/ha
        "sem": r(sem), "fer": r(fer), "pes": r(pes), "labor": labor,
        "other": r(othr), "coe_s": r(coe_s), "arr": r(arr), "dep": r(dep),
        "gp_ex": r(gp_ex), "gp_inc": r(gp_inc),
        "gm_ex_pct": r4(gm_ex_p), "gm_inc_pct": r4(gm_in_p),
        "ok": n >= 30,
    }


# ════════════════════════════════════════════════════════════════════════════════
# DATASET BUILDER
# ════════════════════════════════════════════════════════════════════════════════
# ════════════════════════════════════════════════════════════════════════════════
# CORREÇÕES PONTUAIS — meses com anomalia confirmada na fonte
# ════════════════════════════════════════════════════════════════════════════════
# Meses em que os itens de custo publicados pela IMEA vieram com um dado
# claramente fora do padrão (pico/vale isolado entre o mês anterior e o
# seguinte), sem arquivo original disponível para conferência manual.
# Enquanto isso não é confirmado na fonte, interpolamos linearmente (média
# simples) entre o mês anterior e o seguinte, só para os campos de CUSTO —
# preço, produtividade e receita do mês NÃO são tocados.
#
# Formato: (cultura, "YYYY-MM"): ("YYYY-MM anterior", "YYYY-MM seguinte")
COST_INTERPOLATE = {
    ("SOJA", "2022-11"): ("2022-10", "2022-12"),
}

_COST_FIELDS = ["sem", "fer", "pes", "labor", "other", "coe_s", "arr", "dep"]


def apply_cost_interpolation(monthly):
    """Substitui os campos de custo (e recalcula gp_ex/gp_inc/margens) dos
    meses listados em COST_INTERPOLATE pela média simples dos meses vizinhos.
    Preço, produtividade e receita bruta do mês permanecem os originais."""
    by_ym = {r["d"]: r for r in monthly}
    for (cultura, ym), (prev_ym, next_ym) in COST_INTERPOLATE.items():
        rec, prev, nxt = by_ym.get(ym), by_ym.get(prev_ym), by_ym.get(next_ym)
        if not (rec and prev and nxt):
            continue
        for f in _COST_FIELDS:
            if prev.get(f) is not None and nxt.get(f) is not None:
                rec[f] = round((prev[f] + nxt[f]) / 2, 2)
        named = sum(rec.get(f) or 0 for f in ["sem", "fer", "pes", "labor", "other"])
        rb_std = rec.get("rb_std")
        gp_ex  = (rb_std - named) if rb_std and named else None
        gp_inc = (gp_ex - rec["arr"]) if gp_ex is not None and rec.get("arr") else gp_ex
        rec["gp_ex"]      = round(gp_ex, 2) if gp_ex is not None else None
        rec["gp_inc"]     = round(gp_inc, 2) if gp_inc is not None else None
        rec["gm_ex_pct"]  = round(gp_ex / rb_std, 4) if gp_ex is not None and rb_std else None
        rec["gm_inc_pct"] = round(gp_inc / rb_std, 4) if gp_inc is not None and rb_std else None
        log.info(f"  [{cultura}] {ym}: custos interpolados de {prev_ym}/{next_ym} "
                 f"(sem confirmação da fonte original)")
    return monthly


def build_dataset(conn):
    output = {}
    for cultura in ["SOJA", "MILHO", "ALGODAO"]:
        start = "2022-01-01" if cultura == "SOJA" else "2023-01-01"
        m_yms = [r[0] for r in conn.execute("""
            SELECT DISTINCT strftime('%Y-%m',data_referencia) FROM historico
            WHERE grupo='CUSTO' AND cultura=?
              AND data_referencia BETWEEN ? AND date('now','+60 days')
              AND (safra_tipo='mensal' OR indicador_id IS NOT NULL)
            ORDER BY 1""", (cultura, start)).fetchall()]
        monthly = [
            build_rec(conn, cultura, ym, safra_label_monthly(conn, cultura, ym) or ym)
            for ym in m_yms
        ]

        # Preenche gaps de mês na série mensal usando o dado 'anual'
        # (histórico interno) quando ele é a ÚNICA fonte disponível pra
        # aquele mês — ex.: ALGODÃO dez/2024 nunca foi publicado pela fonte
        # via portal/xlsx mensal, só existe via convenção de fechamento
        # anual. Sem isso, o gráfico mensal teria um buraco (nov/2024 →
        # jan/2025 pulando dez/2024) mesmo havendo dado real pra aquele mês.
        existing_yms = set(m_yms)
        anual_only_yms = [r[0] for r in conn.execute("""
            SELECT DISTINCT strftime('%Y-%m',data_referencia) FROM historico
            WHERE grupo='CUSTO' AND cultura=? AND safra_tipo='anual'
              AND data_referencia BETWEEN ? AND date('now','+60 days')
            ORDER BY 1""", (cultura, start)).fetchall()]
        gap_yms = [ym for ym in anual_only_yms if ym not in existing_yms]
        for ym in gap_yms:
            lbl = calc_safra_label(cultura, ym)
            rec = build_rec(conn, cultura, ym, lbl, anual=True)
            if rec.get("ok"):
                monthly.append(rec)
                log.info(f"  [{cultura}] {ym}: gap preenchido com dado anual "
                         f"(fonte não publicou via mensal/portal)")
        monthly.sort(key=lambda r: r["d"])

        monthly = apply_cost_interpolation(monthly)

        # A entrada de ANNUAL_SNAPS referente a safra em andamento tem o mês
        # hardcoded e fica desatualizada a cada nova publicacao mensal da
        # IMEA (ex.: ficou travada em "2026-02" mesmo ja tendo dado mensal
        # ate "2026-06"). Em vez de mes fixo, para a safra que esta de fato
        # em andamento -- identificada como a safra da publicacao mensal
        # mais recente -- usamos sempre o mes mais recente disponivel na
        # serie mensal com aquele mesmo rotulo de safra. Assim a visao
        # "Annual" acompanha automaticamente o ultimo mes publicado, sem
        # precisar editar este dicionario a cada novo boletim. Rotulos que
        # ainda nao tem nenhum dado mensal (previsao precoce da proxima
        # safra) permanecem com o mes hardcoded original.
        #
        # Comparação normalizada (ignora sufixo "e"): calc_safra_label()
        # decide o sufixo "e" com base em date.today() no momento em que o
        # script roda (norm_y: "e" apenas se y1 >= CURRENT_YEAR). Isso
        # significa que uma safra hoje rotulada "2026/27e" passa a ser
        # calculada como "2026/27" (sem "e") assim que o calendário virar
        # pra 2027, mesmo que o ANNUAL_SNAPS continue com o literal "e"
        # hardcoded. Sem normalizar, essa comparação pararia de casar
        # silenciosamente na virada do ano e o bug original (Annual travado
        # num mês antigo) voltaria a acontecer.
        def _sem_e(lbl):
            return lbl[:-1] if lbl.endswith("e") else lbl

        snaps = list(ANNUAL_SNAPS[cultura])
        if snaps and monthly:
            latest_label = max(monthly, key=lambda r: r["d"])["safra"]
            idxs = [i for i, (_, lbl, _) in enumerate(snaps)
                    if _sem_e(lbl) == _sem_e(latest_label)]
            if idxs:
                i = idxs[-1]
                ym0, lbl0, tipo0 = snaps[i]
                candidatos = [r["d"] for r in monthly if r["safra"] == latest_label]
                if candidatos:
                    snaps[i] = (max(candidatos), lbl0, tipo0)

        annual, seen = [], set()
        for ym, lbl, tipo in snaps:
            if lbl in seen: continue
            seen.add(lbl)
            annual.append(build_rec(conn, cultura, ym, lbl, anual=(tipo == "anual")))
        annual.sort(key=lambda x: x["safra"])
        output[cultura] = {"monthly": monthly, "annual": annual}
    return output


# ════════════════════════════════════════════════════════════════════════════════
# DASHBOARD UPDATER
# ════════════════════════════════════════════════════════════════════════════════
def update_dashboard(data):
    if not DASH_PATH.exists():
        log.warning(f"Dashboard não encontrado: {DASH_PATH}")
        return
    html = DASH_PATH.read_text(encoding="utf-8")
    new_html = re.sub(
        r"const RAW=\{.*?\};",
        f"const RAW={json.dumps(data)};",
        html, flags=re.DOTALL,
    )
    DASH_PATH.write_text(new_html, encoding="utf-8")
    log.info(f"Dashboard atualizado ({len(new_html):,} chars)")


# ════════════════════════════════════════════════════════════════════════════════
# MIGRATION — idempotent, run once per startup
# ════════════════════════════════════════════════════════════════════════════════
def migrate_indicador_ids(conn, now_str):
    """
    Limpeza única e idempotente de historico.indicador_id.

    Recalcula o indicador_id de toda linha xlsx_* usando o truncamento
    atual (40 chars) em vez do antigo (20 chars), que fazia indicadores
    diferentes colidirem no mesmo id (ex.: 'Custo de Oportunidade' e
    'Custo de Oportunidade da Terra' truncavam para o mesmo prefixo de 20
    caracteres). Não afeta indicador_id de outras origens (numérico, do
    pipeline de portal descontinuado). Seguro para rodar a cada
    inicialização: só grava quando o valor muda.
    """
    rows = conn.execute(
        "SELECT id, cultura, indicador_nome, indicador_id FROM historico "
        "WHERE indicador_id LIKE 'xlsx_%'"
    ).fetchall()
    updated = 0
    for row in rows:
        correct = f"xlsx_{row['cultura'].lower()}_{row['indicador_nome'][:40]}"
        if row["indicador_id"] != correct:
            conn.execute(
                "UPDATE historico SET indicador_id=?, updated_at=? WHERE id=?",
                (correct, now_str, row["id"]),
            )
            updated += 1
    if updated:
        log.info(f"[migrate] historico.indicador_id: {updated} linhas corrigidas (truncamento 20→40 chars)")
    conn.commit()


def migrate_safra_labels(conn, now_str):
    """
    Limpeza única e idempotente da coluna historico.safra.

    Reescreve o rótulo de safra de TODAS as linhas de grupo='CUSTO' que
    alimentam a série mensal do dashboard (safra_tipo='mensal' OU
    indicador_id IS NOT NULL) usando calc_safra_label() — a mesma regra
    determinística que safra_label_monthly() já usa para montar o dataset.

    Isso corrige o rótulo errado que a própria fonte (portal IMEA) publicou
    para alguns meses (ex.: Out–Dez/2022 SOJA gravados como "2023/24"),
    deixando o banco consistente com o que o dashboard exibe — sem isso, o
    campo 'safra' do banco ficava como "lixo histórico" divergente do que
    é realmente usado.

    NÃO toca nas linhas safra_tipo='anual' (histórico interno pré-tracking
    mensal): essas usam uma convenção própria de "custo publicado 1 ano após
    o fechamento" (ver ANNUAL_SNAPS) que não segue o calendário mês-a-mês.

    Seguro para rodar a cada inicialização: só grava quando o valor muda.
    """
    rows = conn.execute(
        "SELECT id, cultura, data_referencia, safra FROM historico "
        "WHERE grupo='CUSTO' AND (safra_tipo='mensal' OR indicador_id IS NOT NULL)"
    ).fetchall()

    updated = 0
    for row in rows:
        ym = row["data_referencia"][:7]
        correct = calc_safra_label(row["cultura"], ym)
        if row["safra"] != correct:
            conn.execute(
                "UPDATE historico SET safra=?, updated_at=? WHERE id=?",
                (correct, now_str, row["id"]),
            )
            updated += 1

    if updated:
        log.info(f"[migrate] historico.safra: {updated} linhas corrigidas para o rótulo determinístico")
    conn.commit()


def migrate_preco_conab(conn):
    """
    Limpeza única e idempotente da tabela preco_conab:

    1. Remove combinações produto_conab/nivel_comercializacao que o dashboard
       nunca consome (atacado, varejo, sementes etc.) — evita que o LIMIT 1
       em get_price_spot pague uma linha errada.
    2. Consolida duplicatas antigas (por mês/cultura) em uma única linha com
       o valor médio, caso existam remanescentes de versões anteriores do script
       que usavam INSERT OR IGNORE sem constraint eficaz.

    Seguro para rodar a cada inicialização: não altera dados válidos.
    """
    KEEP = [
        ("SOJA",    "SOJA EM GRÃOS   (60 kg)",                                       "PRODUTOR"),
        ("MILHO",   "MILHO EM GRÃOS   (60 kg)",                                      "PRODUTOR"),
        ("ALGODAO", "ALGODÃO EM PLUMA TIPO BÁSICO - SLM 41-4 BRANCO  (15 kg)",       "PRODUTOR"),
    ]
    placeholders = " OR ".join(
        ["(cultura=? AND produto_conab=? AND nivel_comercializacao=?)"] * len(KEEP)
    )
    params = [v for t in KEEP for v in t]
    deleted = conn.execute(
        f"DELETE FROM preco_conab WHERE NOT ({placeholders})", params
    ).rowcount
    if deleted:
        log.info(f"[migrate] preco_conab: {deleted} linhas com produto/nível fora do escopo removidas")

    # Consolidar duplicatas remanescentes (mesmo cultura+produto+nivel+mês)
    dups = conn.execute("""
        SELECT cultura, produto_conab, nivel_comercializacao,
               strftime('%Y-%m', data_referencia) AS ym,
               COUNT(*) AS n, AVG(valor_kg) AS avg_kg, MAX(updated_at) AS upd
        FROM preco_conab
        GROUP BY cultura, produto_conab, nivel_comercializacao, ym
        HAVING n > 1
    """).fetchall()
    if dups:
        for row in dups:
            conn.execute(
                "DELETE FROM preco_conab WHERE cultura=? AND produto_conab=? "
                "AND nivel_comercializacao=? AND strftime('%Y-%m',data_referencia)=?",
                (row[0], row[1], row[2], row[3]),
            )
            conn.execute(
                "INSERT INTO preco_conab (cultura,produto_conab,nivel_comercializacao,"
                "data_referencia,valor_kg,updated_at) VALUES (?,?,?,?,?,?)",
                (row[0], row[1], row[2], row[3] + "-15", round(row[5], 6), row[6]),
            )
        log.info(f"[migrate] preco_conab: {len(dups)} meses com duplicatas consolidados")

    conn.commit()


# ════════════════════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════════════════════
def main():
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log.info("=" * 60)
    log.info("IMEA Extractor — iniciando")
    log.info("=" * 60)

    conn = get_conn()
    ensure_schema(conn)
    migrate_preco_conab(conn)  # idempotent cleanup: remove stale product/nivel combos
    migrate_indicador_ids(conn, now_str)  # idempotent cleanup: corrige truncamento de indicador_id
    migrate_safra_labels(conn, now_str)  # idempotent cleanup: corrige historico.safra

    # ── 1. Autenticar IMEA ────────────────────────────────────────────────────
    log.info("Autenticando no portal IMEA...")
    token = None
    try:
        token = imea_token()
        log.info("Token IMEA obtido")
    except Exception as e:
        log.error(f"Autenticação IMEA falhou: {e}")

    # ── 2. Custos IMEA ────────────────────────────────────────────────────────
    if token:
        for cultura, cfg in CULTURAS.items():
            log.info(f"--- CUSTO {cultura} ---")
            fetch_imea_custo(conn, token, cultura, cfg["cadeia_id"], now_str)

    # ── 3. Preços CONAB (arquivo bulk PrecosMensalUF.txt) ────────────────────
    log.info("--- Preços CONAB ---")
    fetch_conab_preco(conn, now_str)

    # ── 4. Produtividade CONAB (arquivo bulk LevantamentoGraos.txt) ──────────
    log.info("--- Produtividade CONAB ---")
    fetch_conab_safra(conn, now_str)

    # ── 5. Build dataset + dashboard ─────────────────────────────────────────
    log.info("Construindo dataset P&L...")
    data = build_dataset(conn)
    JSON_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info(f"JSON salvo: {JSON_PATH}")
    update_dashboard(data)

    conn.close()

    # ── Summary ───────────────────────────────────────────────────────────────
    log.info("=" * 60)
    for c in ["SOJA", "MILHO", "ALGODAO"]:
        m    = len(data[c]["monthly"])
        a    = len(data[c]["annual"])
        last = data[c]["monthly"][-1]["d"] if data[c]["monthly"] else "—"
        gm   = data[c]["monthly"][-1].get("gm_ex_pct")
        gm_s = f"{gm*100:.1f}%" if gm is not None else "—"
        ok   = sum(1 for r in data[c]["monthly"] if r["ok"])
        log.info(f"  {c:8}: {m:3} meses ({ok} ok) | {a} safras anuais | "
                 f"último={last} | GM={gm_s}")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
