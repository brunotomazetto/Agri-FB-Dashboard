#!/usr/bin/env python3
"""
extractor_imea.py — IBBA Agri Monitor
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
              indicador_id IS NULL, safra_tipo='mensal' → IBBA projeção
              indicador_id IS NULL, safra_tipo='anual'  → IBBA histórico

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
  SOJA 2022 IBBA    : sem shift (header correto)
  SOJA portal       : shift -1  (portal guarda 1 ano à frente)
  MILHO/ALGODÃO     : sem shift
  IBBA mensal       : sem shift

SEEDS = Sementes + Semente de Cobertura (todas as culturas)

ANNUAL SNAPS (hardcoded — melhor data de custo e preço/yield por safra):
  IBBA histórico: custo publicado 1 ano após fechamento
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
IMEA_USER = os.getenv("IMEA_USER", "ryu.matsuyama@itaubba.com")
IMEA_PASS = os.getenv("IMEA_PASS", "falabrod")

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
        "portal_shift":    -1,              # portal 1 ano à frente
    },
    "MILHO": {
        "cadeia_id":       3,
        "conab_preco":     "MILHO EM GRÃOS   (60 kg)",
        "conab_nivel":     "PRODUTOR",
        "conab_produto":   "MILHO",
        "conab_safra":     "2ª SAFRA",       # safrinha MT
        "bag_kg":          60,
        "portal_shift":    0,
    },
    "ALGODAO": {
        "cadeia_id":       1,
        "conab_preco":     "ALGODÃO EM PLUMA TIPO BÁSICO - SLM 41-4 BRANCO  (15 kg)",
        "conab_nivel":     "PRODUTOR",
        "conab_produto":   "ALGODAO EM PLUMA",
        "conab_safra":     None,
        "bag_kg":          15,              # 1 arroba = 15 kg
        "portal_shift":    0,
    },
}

CURRENT_YEAR = date.today().year

# ── Annual snaps (custo_date, label, tipo) ────────────────────────────────────
# tipo='anual' → IBBA histórico: preço/yield buscado em data real fechamento safra
# tipo=None    → portal/IBBA mensal
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
    """Extrai custos mensais do portal IMEA. Insere apenas registros novos."""
    log.info(f"  [{cultura}] Buscando CUSTO no portal IMEA")
    try:
        data = imea_get(token, f"/grupo/{GRUPO_CUSTO}/cadeia/{cadeia_id}/indicadores")
    except Exception as e:
        log.warning(f"  [{cultura}] CUSTO falhou: {e}")
        return 0

    inserted = 0
    for ind in data:
        ind_id   = str(ind.get("id", ""))
        ind_nome = ind.get("nome", "")
        for pt in (ind.get("series") or []):
            safra      = pt.get("safra")
            safra_id   = str(pt.get("safraId", ""))
            safra_tipo = pt.get("safraTipo")
            data_ref   = (pt.get("dataReferencia") or "")[:10]
            valor      = pt.get("valor")
            if valor is None or not data_ref:
                continue
            if conn.execute(
                "SELECT 1 FROM historico WHERE cultura=? AND indicador_id=? AND data_referencia=?",
                (cultura, ind_id, data_ref)
            ).fetchone():
                continue
            conn.execute(
                """INSERT INTO historico
                   (cultura,cadeia_id,indicador_id,indicador_nome,safra,safra_id,safra_tipo,
                    data_referencia,ano,mes,valor,unidade,estado,grupo,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,'R$/ha','MT','CUSTO',?)""",
                (cultura, cadeia_id, ind_id, ind_nome, safra, safra_id, safra_tipo,
                 data_ref, int(data_ref[:4]), int(data_ref[5:7]), valor, now_str),
            )
            inserted += 1
    conn.commit()
    log.info(f"  [{cultura}] CUSTO: {inserted} novas linhas")
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
def fetch_conab_preco(conn, now_str):
    """
    Baixa preços recebidos pelo produtor MT via Pentaho CDA do portal CONAB.
    Sem reCAPTCHA — dois requests simples (POST doQuery → GET unwrapQuery).

    Fluxo:
      1. POST doQuery com parâmetros → retorna UUID
      2. GET unwrapQuery?uuid=... → baixa XLS com série histórica
      3. Parseia XLS (formato wide: meses nas linhas, UFs nas colunas)
      4. Extrai coluna MT e insere em preco_conab

    Produtos buscados: SOJA, MILHO (GRÃOS), ALGODÃO (PLUMA)
    """
    PENTAHO = "https://pentahoportaldeinformacoes.conab.gov.br/pentaho/plugin/cda/api"
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Origin":  "https://portaldeinformacoes.conab.gov.br",
        "Referer": "https://portaldeinformacoes.conab.gov.br/",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    # (produto_pentaho, classificacao, cultura_interna, bag_kg, produto_conab_label, nivel_label)
    PRODUTOS = [
        ("SOJA",    "EM GRÃOS",  "SOJA",    60,
         "SOJA EM GRÃOS   (60 kg)",
         "PRODUTOR"),
        ("MILHO",   "EM GRÃOS",  "MILHO",   60,
         "MILHO EM GRÃOS   (60 kg)",
         "PRODUTOR"),
        ("ALGODÃO", "EM PLUMA",  "ALGODAO", 15,
         "ALGODÃO EM PLUMA TIPO BÁSICO - SLM 41-4 BRANCO  (15 kg)",
         "PRODUTOR"),
    ]

    import io as _io
    MESES_PT = {
        "JAN":1,"FEV":2,"MAR":3,"ABR":4,"MAI":5,"JUN":6,
        "JUL":7,"AGO":8,"SET":9,"OUT":10,"NOV":11,"DEZ":12,
    }

    total_inserted = 0

    for prod_p, classif, cultura, bag_kg, prod_label, nivel in PRODUTOS:
        log.info(f"  [{cultura}] Buscando preço CONAB via Pentaho CDA")
        try:
            # ── Step 1: doQuery ──────────────────────────────────────────────
            payload = {
                "paramproduto": prod_p,
                "paramregiaoUf": "MT",
                "paramnivelComercializacao": "PREÇO RECEBIDO P/ PRODUTOR",
                "parammesAnoUF": "[Ano Mes].[Mes-Ano].Members",  # série histórica completa
                "paramclassificacao": classif,
                "paramlinhaRegiao": "[Ano Mes].[Mes-Ano].Members",
                "paramtipoVisao": "MENSAL",
                "path": "/home/SIAGRO/PrecoMedio.cda",
                "dataAccessId": "MediaPrecoUF",
                "outputIndexId": "1",
                "pageSize": "0",
                "pageStart": "0",
                "sortBy": "",
                "paramsearchBox": "",
                "outputType": "xls",
                "settingattachmentName": "PrecoMedioUF.xls",
                "wrapItUp": "true",
            }
            r1 = requests.post(
                f"{PENTAHO}/doQuery",
                data=payload, headers=HEADERS, timeout=60, verify=False,
            )
            r1.raise_for_status()
            uuid = r1.text.strip()
            if not uuid:
                log.warning(f"  [{cultura}] doQuery retornou UUID vazio")
                continue

            # ── Step 2: unwrapQuery ──────────────────────────────────────────
            r2 = requests.get(
                f"{PENTAHO}/unwrapQuery",
                params={"path": "/home/SIAGRO/PrecoMedio.cda", "uuid": uuid},
                headers=HEADERS, timeout=60, verify=False,
            )
            r2.raise_for_status()
            if len(r2.content) < 100:
                log.warning(f"  [{cultura}] Arquivo XLS vazio ({len(r2.content)} bytes)")
                continue

            # ── Step 3: parsear XLS ──────────────────────────────────────────
            # Formato: row 0 = header (["Ano Mes.Ano Mes", "CENTRO-OESTE/...", ..., "MT/Preco Medio KG"])
            # Linhas: ["ABR-2025", 1.91, ..., 1.86]
            df = pd.read_excel(_io.BytesIO(r2.content), header=None)

            # Encontrar coluna MT
            header = [str(v) for v in df.iloc[0]]
            mt_col = next(
                (i for i, h in enumerate(header) if "MT" in h.upper() and i > 0),
                None
            )
            if mt_col is None:
                log.warning(f"  [{cultura}] Coluna MT não encontrada. Header: {header}")
                continue

            inserted = 0
            for _, row in df.iloc[1:].iterrows():
                mes_ano = str(row.iloc[0]).strip()   # ex: "ABR-2025"
                valor_bag = row.iloc[mt_col]
                if pd.isna(valor_bag) or not mes_ano or mes_ano == "nan":
                    continue

                # Parsear "ABR-2025" → ano=2025, mes=4
                try:
                    parts = mes_ano.split("-")
                    mes_str = parts[0].upper()[:3]
                    ano = int(parts[1])
                    mes = MESES_PT.get(mes_str)
                    if not mes:
                        continue
                    data_ref = f"{ano:04d}-{mes:02d}-15"
                    valor_kg = round(float(valor_bag) / bag_kg, 6)
                except Exception:
                    continue

                try:
                    conn.execute(
                        """INSERT OR IGNORE INTO preco_conab
                           (cultura,produto_conab,nivel_comercializacao,
                            data_referencia,valor_kg,updated_at)
                           VALUES(?,?,?,?,?,?)""",
                        (cultura, prod_label, nivel, data_ref, valor_kg, now_str),
                    )
                    if conn.execute("SELECT changes()").fetchone()[0]:
                        inserted += 1
                except Exception:
                    pass

            conn.commit()
            log.info(f"  [{cultura}] Preço CONAB: {inserted} novos registros")
            total_inserted += inserted

        except Exception as e:
            log.warning(f"  [{cultura}] Preço CONAB falhou: {e}")

    return total_inserted


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


def safra_label_monthly(conn, cultura, ym):
    cfg   = CULTURAS[cultura]
    shift = cfg["portal_shift"]
    if cultura == "SOJA" and ym[:4] == "2022":
        r = conn.execute(
            "SELECT safra FROM historico WHERE cultura='SOJA' AND grupo='CUSTO' "
            "AND strftime('%Y-%m',data_referencia)=? AND safra_tipo='mensal' "
            "AND safra IS NOT NULL LIMIT 1", (ym,)).fetchone()
        return parse_shift(r[0], 0) if r else None
    r = conn.execute(
        "SELECT safra FROM historico WHERE cultura=? AND grupo='CUSTO' "
        "AND strftime('%Y-%m',data_referencia)=? AND safra IS NOT NULL "
        "AND indicador_id IS NOT NULL LIMIT 1", (cultura, ym)).fetchone()
    if r and r[0]: return parse_shift(r[0], shift)
    r = conn.execute(
        "SELECT safra FROM historico WHERE cultura=? AND grupo='CUSTO' "
        "AND strftime('%Y-%m',data_referencia)=? AND safra IS NOT NULL "
        "AND indicador_id IS NULL AND safra_tipo='mensal' LIMIT 1", (cultura, ym)).fetchone()
    return parse_shift(r[0], 0) if r and r[0] else None


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
    """Para IBBA anuais históricos: data real de fechamento da safra."""
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
def qm(conn, c, ind, ym):
    """Mensal: portal > IBBA mensal > qualquer."""
    for sql in [
        "SELECT valor FROM historico WHERE cultura=? AND indicador_nome=? AND strftime('%Y-%m',data_referencia)=? AND grupo='CUSTO' AND indicador_id IS NOT NULL LIMIT 1",
        "SELECT valor FROM historico WHERE cultura=? AND indicador_nome=? AND strftime('%Y-%m',data_referencia)=? AND grupo='CUSTO' AND safra_tipo='mensal' LIMIT 1",
        "SELECT valor FROM historico WHERE cultura=? AND indicador_nome=? AND strftime('%Y-%m',data_referencia)=? AND grupo='CUSTO' LIMIT 1",
    ]:
        r = conn.execute(sql, (c, ind, ym)).fetchone()
        if r and r[0]: return r[0]
    return None


def qa(conn, c, ind, ym):
    """Anual (safra_tipo='anual')."""
    r = conn.execute(
        "SELECT valor FROM historico WHERE cultura=? AND indicador_nome=? "
        "AND strftime('%Y-%m',data_referencia)=? AND grupo='CUSTO' "
        "AND safra_tipo='anual' LIMIT 1", (c, ind, ym)).fetchone()
    return r[0] if r and r[0] else None


def get_seeds(conn, c, ym, anual=False):
    """Seeds = Sementes + Semente de Cobertura."""
    q = qa if anual else qm
    for n in ["Sementes","Semente de Soja","Semente de milho","Semente de Milho","Semente de Algodão"]:
        v = q(conn, c, n, ym)
        if v is not None:
            return round(v + (q(conn, c, "Semente de Cobertura", ym) or 0), 2)
    return None


def get_ferts(conn, c, ym, anual=False):
    q = qa if anual else qm
    v = q(conn, c, "Fertilizantes e Corretivos", ym)
    if v: return v
    return sum(q(conn, c, n, ym) or 0 for n in
               ["Macronutriente","Micronutriente","Corretivo de Solo"]) or None


def get_pests(conn, c, ym, anual=False):
    q = qa if anual else qm
    v = q(conn, c, "Defensivos", ym)
    if v: return v
    return sum(q(conn, c, n, ym) or 0 for n in
               ["Fungicida","Herbicida","Inseticida","Adjuvante/Outros"]) or None


def get_other(conn, c, ym, anual=False):
    q = qa if anual else qm
    man = q(conn, c, "Manutenção", ym) or 0
    tax = (q(conn, c, "Impostos e Taxas", ym) or q(conn, c, "Impostos e Taxas ", ym) or
           sum(q(conn, c, n, ym) or 0 for n in OTHER_C)) or 0
    fin = q(conn, c, "Financeiras", ym) or sum(q(conn, c, n, ym) or 0 for n in OTHER_D) or 0
    pos = q(conn, c, "Pós-Produção", ym) or sum(q(conn, c, n, ym) or 0 for n in OTHER_E) or 0
    oth = q(conn, c, "Outros Custos", ym) or sum(q(conn, c, n, ym) or 0 for n in OTHER_F) or 0
    mec = (q(conn, c, "OPERAÇÕES MECANIZADAS", ym) or
           q(conn, c, "Operações Mecanizadas", ym)) or 0
    return (man + tax + fin + pos + oth + mec) or None


# ════════════════════════════════════════════════════════════════════════════════
# BUILD P&L RECORD (tudo em R$/ha)
# ════════════════════════════════════════════════════════════════════════════════
def build_rec(conn, cultura, ym, s_label, anual=False):
    """
    Constrói registro P&L completo.
    anual=True → custos de cost_ym (IBBA), preço/yield de price_ym (fechamento real).
    """
    price_ym = get_price_ym(s_label, cultura, ym) if anual else ym
    inicio   = safra_inicio(conn, cultura, price_ym, s_label)
    prod     = get_prod(conn, cultura, price_ym, s_label)
    spot     = get_price_spot(conn, cultura, price_ym)
    std      = get_price_avg(conn, cultura, price_ym, inicio) or spot

    rb_spot = round(spot * prod, 2) if spot and prod else None
    rb_std  = round(std  * prod, 2) if std  and prod else None

    qf = qa if anual else qm
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
    sem   = get_seeds(conn, cultura, ym, anual)
    fer   = get_ferts(conn, cultura, ym, anual)
    pes   = get_pests(conn, cultura, ym, anual)
    othr  = get_other(conn, cultura, ym, anual)
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
        annual, seen = [], set()
        for ym, lbl, tipo in ANNUAL_SNAPS[cultura]:
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
# MAIN
# ════════════════════════════════════════════════════════════════════════════════
def main():
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log.info("=" * 60)
    log.info("IMEA Extractor — iniciando")
    log.info("=" * 60)

    conn = get_conn()
    ensure_schema(conn)

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
