"""
Monitor de Preços F&B — Dashboard v3 (inglês)
3 grupos: Cervejas | Carnes, Processados e Preparados | Mercearias Secas
Clusters pré-definidos + criação dinâmica na sessão
"""
import sqlite3, json
from pathlib import Path
from datetime import date

_ROOT    = Path(__file__).resolve().parent
DB_PATH  = _ROOT / "precos.db"
OUT_PATH = _ROOT / "supermarket_prices.html"

def carregar_dados():
    if not DB_PATH.exists():
        return [], [], [], None, []
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row

    ultima = con.execute("SELECT MAX(data_coleta) FROM precos").fetchone()[0]
    if not ultima:
        con.close(); return [], [], [], None, []

    todos = [dict(r) for r in con.execute("""
        SELECT p.* FROM precos p
        INNER JOIN (
            SELECT supermercado, categoria, nome_produto, embalagem, cidade,
                   CASE WHEN MIN(preco_atual) IS NOT NULL THEN MIN(preco_atual) ELSE NULL END AS melhor_preco,
                   MAX(id) AS ultimo_id
            FROM precos WHERE data_coleta=?
            GROUP BY supermercado, categoria, nome_produto, embalagem, cidade
        ) m ON p.supermercado=m.supermercado AND p.categoria=m.categoria
           AND p.nome_produto=m.nome_produto AND p.embalagem=m.embalagem
           AND p.cidade=m.cidade AND p.data_coleta=?
           AND ((m.melhor_preco IS NOT NULL AND p.preco_atual=m.melhor_preco)
                OR (m.melhor_preco IS NULL AND p.id=m.ultimo_id))
        GROUP BY p.supermercado, p.categoria, p.nome_produto, p.embalagem, p.cidade
        ORDER BY p.categoria, p.supermercado, p.nome_produto
    """, (ultima, ultima)).fetchall()]

    erros = [dict(r) for r in con.execute("""
        SELECT data_coleta, supermercado, categoria, marca, nome_produto, embalagem,
               url, url_recuperada, erro, rota_css
        FROM precos
        WHERE erro IS NOT NULL
          AND id IN (SELECT MAX(id) FROM precos WHERE erro IS NOT NULL
                     GROUP BY data_coleta, supermercado, nome_produto, embalagem, cidade)
        ORDER BY data_coleta DESC, supermercado, categoria, nome_produto
    """).fetchall()]

    # Produtos removidos permanentemente por problemas de scraping
    EXCLUIR = [
        ("Pão de Açúcar", "Original Lata"),
        ("Pão de Açúcar", "Linguiça Toscana 700g Sadia"),
        ("Pão de Açúcar", "Recheado Chocolate 100g Piraque"),
    ]
    excluir_clause = " AND NOT (" + " OR ".join(
        f"(supermercado='{sm}' AND nome_produto='{nm}')" for sm, nm in EXCLUIR
    ) + ")"

    historico = [dict(r) for r in con.execute(f"""
        SELECT data_coleta, supermercado, categoria, marca, nome_produto, embalagem,
               MIN(preco_atual) as preco_atual,
               MAX(CASE WHEN erro='copiado_dia_anterior' THEN 1 ELSE 0 END) as copiado
        FROM precos
        WHERE preco_atual IS NOT NULL AND disponivel=1
        {excluir_clause}
        GROUP BY data_coleta, supermercado, categoria, marca, nome_produto, embalagem
        ORDER BY data_coleta, supermercado, categoria, nome_produto, embalagem
    """).fetchall()]

    alertas = calcular_alertas(con, ultima)
    con.close()
    return todos, erros, historico, ultima, alertas


def calcular_alertas(con, ultima_data):
    alertas = []
    hoje = date.today()
    if ultima_data:
        dias = (hoje - date.fromisoformat(ultima_data)).days
        if dias >= 2:
            alertas.append({"nivel":"critico","titulo":f"Collection stopped for {dias} days",
                "detalhe":f"Last collection: {ultima_data}.","acao":"Go to GitHub → Actions → Run workflow."})
        elif dias == 1:
            alertas.append({"nivel":"aviso","titulo":"Yesterday's collection not found",
                "detalhe":"Possible transient failure.","acao":"Check Actions for errors."})
    rows = con.execute("""
        SELECT supermercado, categoria, COUNT(*) total,
               SUM(CASE WHEN erro IS NOT NULL THEN 1 ELSE 0 END) erros
        FROM precos WHERE data_coleta >= date('now','-7 days')
        GROUP BY supermercado, categoria
    """).fetchall()
    for r in rows:
        if r["total"] == 0: continue
        taxa = r["erros"] / r["total"]
        if taxa >= 0.8:
            alertas.append({"nivel":"critico",
                "titulo":f"{r['supermercado']} / {r['categoria']} — {int(taxa*100)}% errors",
                "detalhe":"All fallback routes failed. Likely layout change.",
                "acao":f"Review URLs for {r['supermercado']} in scraper.py."})
        elif taxa >= 0.4:
            alertas.append({"nivel":"aviso",
                "titulo":f"{r['supermercado']} / {r['categoria']} — unstable ({int(taxa*100)}%)",
                "detalhe":"Possible temporary block.",
                "acao":"Monitor for 2-3 days."})
    return alertas

import json, random
from pathlib import Path
random.seed(42)

GRUPOS = {
    "Cervejas":                         ["Cervejas"],
    "Carnes, Processados e Preparados": ["Carnes"],
    "Mercearias Secas":          ["Biscoitos","Massas","Mercearia"],
}
GRUPOS_EN = {
    "Cervejas": "Beers",
    "Carnes, Processados e Preparados": "Meat & Prepared",
    "Mercearias Secas": "Dry Goods",
}
CLUSTERS_DEF = {
    "Cervejas": [
        {"id":"heineken",   "nome":"Heineken",   "skus":["Heineken Lata_350ml","Heineken Lata_269ml","Heineken 0.0_350ml","Amstel Lata_350ml","Amstel Lata_269ml"]},
        {"id":"ambev",      "nome":"Ambev",      "skus":["Skol Lata_350ml","Skol Lata_269ml","Brahma Duplo Malte_350ml","Brahma Duplo Malte_269ml","Antarctica Lata_350ml","Budweiser Lata_350ml","Budweiser Lata_269ml","Stella Artois Long Neck_330ml","Corona Extra Lata_350ml","Corona Extra Long Neck_330ml","Spaten Puro Malte Lata_350ml","Spaten Puro Malte Lata_269ml","Original Lata_350ml","Original Lata_269ml"]},
        {"id":"petropolis", "nome":"Petrópolis", "skus":["Itaipava Lata_350ml"]},
        {"id":"premium",    "nome":"Premium",    "skus":["Heineken Lata_350ml","Heineken Lata_269ml","Heineken 0.0_350ml","Stella Artois Long Neck_330ml","Corona Extra Long Neck_330ml","Corona Extra Lata_350ml","Spaten Puro Malte Lata_350ml","Spaten Puro Malte Lata_269ml","Budweiser Lata_350ml","Budweiser Lata_269ml"]},
        {"id":"mainstream", "nome":"Mainstream", "skus":["Skol Lata_350ml","Skol Lata_269ml","Brahma Duplo Malte_350ml","Brahma Duplo Malte_269ml","Amstel Lata_350ml","Amstel Lata_269ml","Itaipava Lata_350ml","Original Lata_350ml","Original Lata_269ml"]},
    ],
    "Carnes, Processados e Preparados": [
        {"id":"beef",       "nome":"Beef",       "skus":["Picanha 1kg Bassi_1kg","Picanha 1kg Friboi_1kg","Picanha 1kg Estância 92_1kg","Fraldinha 1kg Bassi_1kg","Carne Moida 1kg Swift_1kg"]},
        {"id":"chicken",    "nome":"Chicken",    "skus":["Peito de Frango 1kg Sadia_1kg","Peito de Frango 1kg Swift_1kg","Peito de Frango 1kg Seara_1kg","Coxa de Frango 1kg Sadia_1kg","Coxa de Frango 1kg Swift_1kg","Coxa de Frango 1kg Seara_1kg","Asa de Frango 1kg Swift_1kg","Asa de Frango 1kg Sadia_1kg"]},
        {"id":"processed",  "nome":"Processed",  "skus":["Salsicha Hot Dog 500g Sadia_500g","Salsicha Hot Dog 500g Perdigão_500g","Salsicha Hot Dog 500g Seara_500g","Linguiça Toscana 700g Sadia_700g","Linguiça Toscana 700g Perdigão_700g","Linguiça Toscana 700g Swift_700g"]},
        {"id":"prepared",   "nome":"Prepared",   "skus":["Lasanha Bolonhesa 600g Sadia_600g","Lasanha Bolonhesa 600g Perdigão_600g","Lasanha Bolonhesa 600g Seara_600g","Nuggets de Frango 300g Sadia_300g"]},
    ],
    "Mercearias Secas": [
        {"id":"cookies",    "nome":"Cookies",    "skus":["Água e Sal 300g Marilan_300g","Água e Sal 300g Mabel_300g","Água e Sal 350g Vitarella_350g","Água e Sal 170g Adria_170g","Água e Sal 184g Piraque_184g","Cream Cracker 300g Marilan_300g","Cream Cracker 300g Mabel_300g","Cream Cracker 350g Vitarella_350g","Cream Cracker 184g Piraque_184g","Cream Cracker 140g Marilan_140g","Cream Cracker 165g Bauducco_165g","Cream Cracker 170g Adria_170g","Oreo 90g Mondelez_90g","Passatempo 150g Nestlé_150g","Recheado Chocolate 140g Bauducco_140g","Recheado Chocolate 100g Piraque_100g"]},
        {"id":"pasta",      "nome":"Pasta",      "skus":["Macarrão Espaguete 500g Barilla_500g","Macarrão Espaguete 500g Adria_500g","Macarrão Espaguete 500g Camil_500g","Macarrão Espaguete 500g Dona Benta_500g","Miojo Carne 85g Nissin_85g"]},
        {"id":"rice",       "nome":"Rice",       "skus":["Arroz Branco 5kg Tio João_5kg","Arroz Branco 5kg Camil_5kg"]},
        {"id":"beans",      "nome":"Beans",      "skus":["Feijão Carioca 1kg Camil_1kg","Feijão Carioca 1kg Kicaldo_1kg"]},
        {"id":"sugar",      "nome":"Sugar",      "skus":["Açúcar Refinado 1kg União_1kg","Açúcar Refinado 1kg Caravelas_1kg","Açúcar Refinado 1kg Da Barra_1kg","Açúcar Refinado 1kg Guarani_1kg"]},
        {"id":"flour",      "nome":"Flour",      "skus":["Farinha de Trigo 1kg Dona Benta_1kg","Farinha de Trigo 1kg Venturelli_1kg","Farinha de Trigo 1kg Sol_1kg"]},
        {"id":"coffee",     "nome":"Coffee",     "skus":["Café Torrado e Moído 500g Pilão_500g","Café Torrado e Moído 500g 3 Corações_500g","Café Torrado e Moído 500g Melitta_500g","Café Torrado e Moído 500g Café Brasileiro_500g","Café Torrado e Moído 500g União_500g"]},
    ],
}

CLUSTER_COLORS = ["#0a0a0f","#0e9f6e","#e02424","#c27803","#7e3af2","#0694a2","#ff8a4c","#84cc16","#ec4899","#14b8a6"]


def aba_cervejas():
    return """
    <div class="page" id="page-grupo-Cervejas">

      <!-- BLOCK 1: BREWERY PRICE INDEX -->
      <div class="section">
        <div class="section-head">
          <span class="section-title">Brewery Price Index — BRL/hl</span>
          <span style="font-size:11px;color:var(--muted)">Weighted average across supermarkets</span>
        </div>
        <p style="font-size:11px;color:var(--muted);margin-bottom:.85rem">
          Ambev: Brahma 30%, Skol 25%, Antarctica/Original/Bud 10% each, Corona/Spaten/Stella 5% each ·
          Heineken: Heineken 80%, Amstel 20% · Petrópolis: Itaipava 100%
        </p>
        <div style="height:300px;margin-bottom:.5rem"><canvas id="chart-brewery-index"></canvas></div>
        <div id="tabela-brewery-index" style="margin-top:.5rem"></div>
      </div>

      <!-- BLOCK 2: PRICE PER HECTOLITER BY BRAND -->
      <div class="section">
        <div class="section-head">
          <span class="section-title">Price per Hectoliter by Brand</span>
          <span style="font-size:11px;color:var(--muted)">Average across supermarkets · BRL/hl</span>
        </div>
        <div style="display:flex;gap:1.25rem;flex-wrap:wrap;align-items:flex-start;margin-bottom:.85rem">
          <div>
            <div class="ctrl-label">Brand</div>
            <div id="chk-brand-hl" style="max-height:200px;overflow-y:auto;border:1px solid var(--border);border-radius:6px;padding:6px 8px;min-width:200px;background:var(--card)"></div>
            <button onclick="toggleBrandsHL(true)" class="btn-mini accent">all</button>
            <button onclick="toggleBrandsHL(false)" class="btn-mini muted">none</button>
          </div>
          <div>
            <div class="ctrl-label">Period</div>
            <select id="sel-periodo-hl" onchange="renderPrecoHL()" class="sel-ctrl" style="width:180px;margin-bottom:6px">
              <option value="tudo">Full history</option>
              <option value="7d">Last 7 days</option>
              <option value="30d">Last 30 days</option>
              <option value="3m">Last 3 months</option>
              <option value="ano">This year</option>
            </select>
          </div>
        </div>
        <div style="height:320px;margin-bottom:1rem"><canvas id="chart-preco-hl"></canvas></div>
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:.5rem">
          <span class="section-title" style="font-size:12px">Price per hectoliter — most recent day in period</span>
        </div>
        <div id="tabela-preco-hl"></div>
      </div>

      <!-- BLOCK 3: PRICE HISTORY -->
      <div class="section">
        <div class="section-head">
          <span class="section-title">Beers — Price History</span>
          <span style="font-size:11px;color:var(--muted)">São Paulo — SP</span>
        </div>
        <div style="display:flex;gap:1.25rem;flex-wrap:wrap;align-items:flex-start;margin-bottom:.85rem">
          <div>
            <div class="ctrl-label">Product</div>
            <div id="chk-prod-Cervejas" style="max-height:200px;overflow-y:auto;border:1px solid var(--border);border-radius:6px;padding:6px 8px;min-width:240px;background:var(--card)"></div>
            <button onclick="toggleTodos('chk-prod-Cervejas',true,'Cervejas')" class="btn-mini accent">all</button>
            <button onclick="toggleTodos('chk-prod-Cervejas',false,'Cervejas')" class="btn-mini muted">none</button>
          </div>
          <div>
            <div class="ctrl-label">Supermarket</div>
            <div id="chk-sm-Cervejas" style="border:1px solid var(--border);border-radius:6px;padding:6px 8px;min-width:170px;background:var(--card)"></div>
            <button onclick="toggleTodos('chk-sm-Cervejas',true,'Cervejas')" class="btn-mini accent">all</button>
            <button onclick="toggleTodos('chk-sm-Cervejas',false,'Cervejas')" class="btn-mini muted">none</button>
          </div>
          <div>
            <div class="ctrl-label">Period</div>
            <select id="sel-periodo-Cervejas" onchange="onPeriodoChange('Cervejas')" class="sel-ctrl" style="width:180px;margin-bottom:6px">
              <option value="tudo">Full history</option>
              <option value="7d">Last 7 days</option>
              <option value="30d">Last 30 days</option>
              <option value="3m">Last 3 months</option>
              <option value="ano">This year</option>
              <option value="custom">Custom range...</option>
            </select>
            <div id="range-Cervejas" style="display:none;flex-direction:column;gap:4px">
              <div style="display:flex;align-items:center;gap:6px;font-size:11px;color:var(--muted)">
                <span>From</span><input type="date" id="dt-de-Cervejas" onchange="renderGrupo('Cervejas')" class="sel-ctrl">
              </div>
              <div style="display:flex;align-items:center;gap:6px;font-size:11px;color:var(--muted)">
                <span>To</span><input type="date" id="dt-ate-Cervejas" onchange="renderGrupo('Cervejas')" class="sel-ctrl">
              </div>
            </div>
          </div>
        </div>
        <div style="height:320px;margin-bottom:1rem"><canvas id="chart-Cervejas"></canvas></div>
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:.5rem">
          <span class="section-title" style="font-size:12px">Price comparison — most recent day in period</span>
          <button class="btn btn-green btn-excel" style="font-size:11px;padding:5px 10px" onclick="exportarComparacao('Cervejas')">⬇ Excel</button>
        </div>
        <div id="tabela-comp-Cervejas"></div>
      </div>

    </div>"""

def aba_grupo(grupo_nome, cats):
    gid = grupo_nome.replace(" ","_").replace(",","").replace("/","")
    clusters_j = json.dumps(CLUSTERS_DEF.get(grupo_nome, []), ensure_ascii=False)
    cats_j = json.dumps(cats, ensure_ascii=False).replace('"', "&quot;")
    return f"""
    <div class="page" id="page-grupo-{gid}">

      <!-- BLOCK 1: CLUSTERS -->
      <div class="section">
        <div class="section-head">
          <span class="section-title">Clusters</span>
          <button class="btn btn-black" onclick="abrirModalNovoCluster('{gid}',{cats_j})">+ New cluster</button>
        </div>
        <p style="font-size:11px;color:var(--muted);margin-bottom:.85rem">
          Average price per cluster · click a SKU tag to remove/include it · toggle clusters on the chart below
        </p>
        <div id="cluster-cards-{gid}" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:.75rem;margin-bottom:1rem"></div>
        <div class="section-title" style="font-size:12px;margin-bottom:.5rem">Cluster price history</div>
        <div style="height:260px"><canvas id="chart-cluster-{gid}"></canvas></div>
      </div>

      <!-- BLOCK 2: FILTERS + CHART -->
      <div class="section">
        <div class="section-head">
          <span class="section-title">{GRUPOS_EN.get(grupo_nome, grupo_nome)} — Price History</span>
          <span style="font-size:11px;color:var(--muted)">São Paulo — SP</span>
        </div>
        <div style="display:flex;gap:1.25rem;flex-wrap:wrap;align-items:flex-start;margin-bottom:.85rem">
          <div>
            <div class="ctrl-label">Product</div>
            <div id="chk-prod-{gid}" style="max-height:200px;overflow-y:auto;border:1px solid var(--border);border-radius:6px;padding:6px 8px;min-width:240px;background:var(--card)"></div>
            <button onclick="toggleTodos('chk-prod-{gid}',true,'{gid}')" class="btn-mini accent">all</button>
            <button onclick="toggleTodos('chk-prod-{gid}',false,'{gid}')" class="btn-mini muted">none</button>
          </div>
          <div>
            <div class="ctrl-label">Supermarket</div>
            <div id="chk-sm-{gid}" style="border:1px solid var(--border);border-radius:6px;padding:6px 8px;min-width:170px;background:var(--card)"></div>
            <button onclick="toggleTodos('chk-sm-{gid}',true,'{gid}')" class="btn-mini accent">all</button>
            <button onclick="toggleTodos('chk-sm-{gid}',false,'{gid}')" class="btn-mini muted">none</button>
          </div>
          <div>
            <div class="ctrl-label">Period</div>
            <select id="sel-periodo-{gid}" onchange="onPeriodoChange('{gid}')" class="sel-ctrl" style="width:180px;margin-bottom:6px">
              <option value="tudo">Full history</option>
              <option value="7d">Last 7 days</option>
              <option value="30d">Last 30 days</option>
              <option value="3m">Last 3 months</option>
              <option value="ano">This year</option>
              <option value="custom">Custom range...</option>
            </select>
            <div id="range-{gid}" style="display:none;flex-direction:column;gap:4px">
              <div style="display:flex;align-items:center;gap:6px;font-size:11px;color:var(--muted)">
                <span>From</span><input type="date" id="dt-de-{gid}" onchange="renderGrupo('{gid}')" class="sel-ctrl">
              </div>
              <div style="display:flex;align-items:center;gap:6px;font-size:11px;color:var(--muted)">
                <span>To</span><input type="date" id="dt-ate-{gid}" onchange="renderGrupo('{gid}')" class="sel-ctrl">
              </div>
            </div>
          </div>
        </div>
        <div style="height:320px;margin-bottom:1rem"><canvas id="chart-{gid}"></canvas></div>
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:.5rem">
          <span class="section-title" style="font-size:12px">Price comparison — most recent day in period</span>
          <button class="btn btn-green btn-excel" style="font-size:11px;padding:5px 10px" onclick="exportarComparacao('{gid}')">⬇ Excel</button>
        </div>
        <div id="tabela-comp-{gid}"></div>
      </div>

    </div>"""

def gerar_html(todos, erros, historico, ultima_data, alertas):
    ok     = [r for r in todos if r.get("preco_atual")]
    err    = [r for r in todos if not r.get("preco_atual")]
    promos = [r for r in ok if r.get("em_promocao")]
    badge  = len(alertas)
    todos_j    = json.dumps(todos,     ensure_ascii=False)
    erros_j    = json.dumps(erros,     ensure_ascii=False)
    hist_j     = json.dumps(historico, ensure_ascii=False)
    alertas_j  = json.dumps(alertas,   ensure_ascii=False)
    grupos_j   = json.dumps({g:cats for g,cats in GRUPOS.items()}, ensure_ascii=False)
    clusters_j = json.dumps(CLUSTERS_DEF, ensure_ascii=False)
    colors_j   = json.dumps(CLUSTER_COLORS, ensure_ascii=False)
    abas_list  = []
    for g,cats in GRUPOS.items():
        if g == "Cervejas":
            abas_list.append(aba_cervejas())
        else:
            abas_list.append(aba_grupo(g,cats))
    abas = "".join(abas_list)
    grupo_tabs = ""
    for g in GRUPOS:
        gid = g.replace(" ","_").replace(",","").replace("/","")
        label = GRUPOS_EN.get(g, g)
        grupo_tabs += f'<button class="tab-btn" onclick="showTab(\'grupo-{gid}\',this)">{label}</button>\n    '

    _js_extra = ""

    return _js_extra + f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>F&B Price Monitor</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/xlsx@0.18.5/dist/xlsx.full.min.js"></script>
<style>
:root{{--bg:#f4f6f9;--card:#fff;--border:#e2e8f0;--text:#1a202c;--muted:#718096;
  --accent:#0a0a0f;--green:#0e9f6e;--red:#e02424;--yellow:#c27803;--blue:#0694a2;
  --radius:10px;--font:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:var(--font);background:var(--bg);color:var(--text);font-size:14px}}
border-radius:16px;padding:2.5rem 2rem;width:340px;text-align:center;box-shadow:0 20px 60px rgba(0,0,0,.25)}}





.erro-login{{color:var(--red);font-size:12px;margin-top:8px;display:none}}
#app{{display:block;min-height:100vh}}
header{{background:#0a0a0f;color:#fff;padding:.85rem 1.5rem;display:flex;justify-content:space-between;align-items:center}}
.header-left{{display:flex;align-items:center;gap:12px}}
.hdiv{{width:1px;height:24px;background:rgba(255,255,255,.25)}}
header h1{{font-size:15px;font-weight:600}}
header .meta{{font-size:11px;opacity:.75;text-align:right;line-height:1.6}}
.tab-bar{{background:var(--card);border-bottom:1.5px solid var(--border);padding:0 1.5rem;display:flex;overflow-x:auto}}
.tab-btn{{padding:11px 16px;background:none;border:none;border-bottom:2.5px solid transparent;font-size:13px;font-weight:500;cursor:pointer;color:var(--muted);white-space:nowrap;transition:.15s;position:relative}}
.tab-btn.active{{color:var(--accent);border-bottom-color:var(--accent)}}
.tab-btn:hover:not(.active){{color:var(--text)}}
.nbadge{{position:absolute;top:8px;right:4px;background:var(--red);color:#fff;font-size:9px;font-weight:700;border-radius:8px;padding:1px 5px}}
.main{{padding:1.25rem 1.5rem;max-width:1600px;margin:0 auto}}
.page{{display:none}}.page.active{{display:block}}
.section{{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:1.1rem 1.25rem;margin-bottom:1.1rem}}
.section-head{{display:flex;justify-content:space-between;align-items:center;margin-bottom:.9rem;flex-wrap:wrap;gap:.5rem}}
.section-title{{font-size:14px;font-weight:600;color:var(--accent)}}
.kpi-row{{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:.75rem;margin-bottom:1.1rem}}
.kpi{{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:.9rem 1rem}}
.kpi-label{{font-size:11px;color:var(--muted);margin-bottom:3px}}
.kpi-val{{font-size:24px;font-weight:700}}
.kpi-sub{{font-size:11px;color:var(--muted)}}
.kpi.warn .kpi-val{{color:var(--yellow)}}.kpi.green .kpi-val{{color:var(--green)}}
.filters{{display:flex;gap:.5rem;flex-wrap:wrap;align-items:center;margin-bottom:.75rem}}
.filters label{{font-size:12px;color:var(--muted)}}
.filters select,.filters input{{font-size:12px;padding:5px 8px;border:1px solid var(--border);border-radius:6px;background:var(--card)}}
.table-wrap{{overflow-x:auto}}
table{{width:100%;border-collapse:collapse;font-size:12px}}
thead th{{text-align:left;padding:6px 8px;background:#f8fafc;font-weight:600;font-size:11px;color:var(--muted);border-bottom:1.5px solid var(--border)}}
td{{padding:6px 8px;border-bottom:1px solid var(--border)}}
tr:hover td{{background:#fafafa}}
.badge{{font-size:10px;padding:2px 7px;border-radius:8px;font-weight:600;white-space:nowrap}}
.b-ok{{background:#d1fae5;color:#065f46}}.b-err{{background:#fee2e2;color:#991b1b}}
.b-promo{{background:#fef3c7;color:#92400e}}
.b-pa{{background:#dcfce7;color:#166534}}.b-ex{{background:#fff7ed;color:#9a3412}}
.b-at{{background:#fefce8;color:#713f12}}
.btn{{padding:7px 14px;border:none;border-radius:7px;font-size:12px;font-weight:600;cursor:pointer}}
.btn-green{{background:var(--green);color:#fff}}.btn-black{{background:#0a0a0f;color:#fff}}
.alertas-ok{{background:#f0fdf4;border:1px solid #a7f3d0;border-radius:8px;padding:.75rem 1rem;font-size:12px;color:#065f46}}
.alerta{{display:flex;gap:.75rem;padding:.75rem;border-radius:8px;margin-bottom:.5rem}}
.alerta-critico{{background:#fef2f2;border:1px solid #fca5a5}}
.alerta-aviso{{background:#fffbeb;border:1px solid #fcd34d}}
.al-icon{{font-size:16px;flex-shrink:0}}
.al-titulo{{font-weight:600;font-size:12px;margin-bottom:2px}}
.al-detalhe{{font-size:11px;color:var(--muted)}}.al-acao{{font-size:11px;color:var(--accent);margin-top:2px}}
.ctrl-label{{font-size:11px;font-weight:600;color:var(--muted);margin-bottom:5px;text-transform:uppercase;letter-spacing:.5px}}
.sel-ctrl{{font-size:11px;border:1px solid var(--border);border-radius:6px;padding:5px 8px;display:block}}
.btn-mini{{font-size:10px;background:none;border:none;cursor:pointer;padding:3px 0;margin-right:8px}}
.btn-mini.accent{{color:var(--accent)}}.btn-mini.muted{{color:var(--muted)}}
.cluster-card{{background:#f8fafc;border:1px solid var(--border);border-radius:8px;padding:.85rem 1rem}}
.cluster-title{{font-size:12px;font-weight:600;margin-bottom:.5rem;display:flex;justify-content:space-between;align-items:center;gap:.5rem}}
.cluster-toggle{{width:36px;height:20px;border-radius:10px;border:none;cursor:pointer;position:relative;transition:.2s;flex-shrink:0}}
.cluster-toggle.on{{background:var(--accent)}}.cluster-toggle.off{{background:#cbd5e0}}
.cluster-toggle::after{{content:"";position:absolute;top:3px;width:14px;height:14px;border-radius:50%;background:#fff;transition:.2s}}
.cluster-toggle.on::after{{right:3px}}.cluster-toggle.off::after{{left:3px}}
.cluster-sm-row{{display:flex;align-items:center;gap:8px;margin-bottom:3px;font-size:12px}}
.cluster-avg{{font-weight:700;min-width:70px}}
.cluster-skus{{font-size:10px;color:var(--muted);margin-top:6px;border-top:1px solid var(--border);padding-top:5px;max-height:70px;overflow-y:auto}}
.sku-tag{{display:inline-block;background:#e2e8f0;border-radius:4px;padding:2px 7px;margin:2px;font-size:10px;cursor:pointer;transition:.15s}}
.sku-tag:hover{{background:#cbd5e0}}
.sku-tag.removed{{background:#fee2e2;color:#991b1b;text-decoration:line-through}}
/* Modal */
.modal-overlay{{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.45);z-index:999;align-items:center;justify-content:center}}
.modal-overlay.open{{display:flex}}
.modal-box{{background:var(--card);border-radius:12px;padding:1.5rem;width:540px;max-height:82vh;overflow-y:auto;box-shadow:0 20px 60px rgba(0,0,0,.2)}}
.modal-head{{display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem}}
.modal-close{{background:none;border:none;cursor:pointer;font-size:20px;color:var(--muted);line-height:1}}
</style>
</head>
<body>

<div id="app">
  

  <div class="tab-bar">
    {grupo_tabs}
  </div>

  <div class="main">

    <!-- OVERVIEW -->
    <div class="page active" id="page-inicio">
      <div class="kpi-row" id="kpi-row"></div>
      <div id="alertas-banner"></div>
      <div class="section">
        <div class="section-head">
          <span class="section-title">Data — collection of {ultima_data}</span>
          <button class="btn btn-green btn-excel" onclick="exportarExcel()">⬇ Excel</button>
        </div>
        <div class="filters">
          <label>Supermarket:</label>
          <select id="f-sm" onchange="filtrarTabela()">
            <option value="">All</option><option>Pão de Açúcar</option><option>Extra</option><option>Atacadão</option>
          </select>
          <label>Group:</label>
          <select id="f-grupo" onchange="filtrarTabela()">
            <option value="">All</option>
            <option value="Cervejas">Beers</option>
            <option value="Meat & Prepared">Meat &amp; Prepared</option>
            <option value="Dry Goods">Dry Goods</option>
          </select>
          <label>Category:</label>
          <select id="f-cat" onchange="filtrarTabela()">
            <option value="">All</option>
            <option>Cervejas</option><option>Carnes</option><option>Biscoitos</option><option>Massas</option><option>Mercearia</option>
          </select>
          <label>Brand:</label>
          <select id="f-marca" onchange="filtrarTabela()"><option value="">All</option></select>
          <label>Status:</label>
          <select id="f-status" onchange="filtrarTabela()">
            <option value="">All</option><option value="ok">✅ With price</option><option value="erro">❌ Error</option>
          </select>
          <input type="text" id="f-busca" placeholder="Search..." oninput="filtrarTabela()" style="width:150px">
        </div>
        <div class="table-wrap"><table>
          <thead><tr><th>Status</th><th>Supermarket</th><th>Group</th><th>Category</th><th>Brand</th><th>Product</th><th>Size</th><th>Date</th><th>Price</th><th>Orig.</th><th>Disc.</th><th>Error</th></tr></thead>
          <tbody id="tabela-body"></tbody>
        </table></div>
        <div id="tabela-count" style="font-size:11px;color:var(--muted);margin-top:6px;text-align:right"></div>
      </div>
    </div>

    {abas}

    <!-- ERRORS -->
    <div class="page" id="page-erros">
      <div class="section">
        <div class="section-head">
          <span class="section-title">Errors by collection date</span>
          <button class="btn btn-green btn-excel" onclick="exportarErrosExcel()">⬇ Excel</button>
        </div>
        <div class="filters">
          <label>Date:</label><select id="fe-dia" onchange="filtrarErros()"><option value="">All</option></select>
          <label>Supermarket:</label>
          <select id="fe-sm" onchange="filtrarErros()">
            <option value="">All</option><option>Pão de Açúcar</option><option>Extra</option><option>Atacadão</option>
          </select>
          <label>Category:</label>
          <select id="fe-cat" onchange="filtrarErros()">
            <option value="">All</option><option>Cervejas</option><option>Carnes</option><option>Biscoitos</option><option>Massas</option><option>Mercearia</option>
          </select>
          <label>Error type:</label><select id="fe-tipo" onchange="filtrarErros()"><option value="">All</option></select>
        </div>
        <div class="table-wrap"><table>
          <thead><tr><th>Date</th><th>Supermarket</th><th>Category</th><th>Product</th><th>Size</th><th>Error</th><th>URL</th></tr></thead>
          <tbody id="erros-body"></tbody>
        </table></div>
        <div id="erros-count" style="font-size:11px;color:var(--muted);margin-top:6px;text-align:right"></div>
      </div>
    </div>

    <!-- ALERTS -->
    <div class="page" id="page-alertas">
      <div class="section">
        <div class="section-title" style="margin-bottom:.9rem">System alerts</div>
        <div id="alertas-lista"></div>
      </div>
    </div>

  </div><!-- /main -->
</div><!-- /app -->

<!-- NEW CLUSTER MODAL -->
<div class="modal-overlay" id="modal-novo-cluster">
  <div class="modal-box">
    <div class="modal-head">
      <span style="font-size:15px;font-weight:600">New Cluster</span>
      <button class="modal-close" onclick="fecharModal()">✕</button>
    </div>
    <div style="margin-bottom:.75rem">
      <label style="font-size:12px;font-weight:600;display:block;margin-bottom:4px">Cluster name</label>
      <input type="text" id="modal-cluster-nome" placeholder="e.g. My selection"
        style="width:100%;padding:8px 12px;border:1px solid var(--border);border-radius:7px;font-size:13px">
    </div>
    <div style="margin-bottom:.75rem">
      <label style="font-size:12px;font-weight:600;display:block;margin-bottom:4px">Select SKUs</label>
      <input type="text" id="modal-busca" placeholder="Filter products..." oninput="filtrarModalSkus()"
        style="width:100%;padding:8px 12px;border:1px solid var(--border);border-radius:7px;font-size:13px;margin-bottom:6px">
      <div id="modal-skus-lista" style="max-height:280px;overflow-y:auto;border:1px solid var(--border);border-radius:7px;padding:6px"></div>
    </div>
    <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:1rem">
      <button onclick="fecharModal()" class="btn" style="background:var(--border);color:var(--text)">Cancel</button>
      <button onclick="salvarNovoCluster()" class="btn btn-black">Create</button>
    </div>
  </div>
</div>

<script>
const USUARIOS = {{"ryumatsuyama":"admin123","brunotomazetto":"admin123","gustavotroyano":"admin123"}};
const SENHA_GLOBAL = "ibbafb123";
const TODOS    = {todos_j};
const ERROS_H  = {erros_j};
const HIST     = {hist_j};
const ALERTAS  = {alertas_j};
const GRUPOS   = {grupos_j};
const CLUSTERS_DEF_ORIG = {clusters_j};
const CLUSTER_COLORS = {colors_j};
const SM_BADGE = {{"Pão de Açúcar":"b-pa","Extra":"b-ex","Atacadão":"b-at"}};

function fmt(v){{return v!=null?"BRL "+v.toFixed(2).replace(".",","):"—"}}
function fmtPct(a,b){{return(a&&b&&b>a)?"-"+Math.round((b-a)/b*100)+"%":"—"}}

// ── Auth ────────────────────────────────────────────────────────────────────
window.onload=()=>{{init();}};
function showTab(id,btn){{
  document.querySelectorAll(".page").forEach(p=>p.classList.remove("active"));
  document.querySelectorAll(".tab-btn").forEach(b=>b.classList.remove("active"));
  document.getElementById("page-"+id)?.classList.add("active"); btn.classList.add("active");
}}
function init(){{
  // Admin check via parent STATE
  try {{
    var isAdmin = window.parent && window.parent.STATE && 
                  window.parent.STATE.currentUser && 
                  window.parent.STATE.currentUser.is_admin;
    if (!isAdmin) {{
      document.querySelectorAll('.btn-excel').forEach(function(b){{ b.style.display='none'; }});
    }}
  }} catch(e) {{ /* standalone mode — hide excel buttons */ 
    document.querySelectorAll('.btn-excel').forEach(function(b){{ b.style.display='none'; }});
  }}
  // Mostra Beers por default
  const firstBtn = document.querySelector('.tab-bar .tab-btn');
  if(firstBtn) firstBtn.click();
  renderBreweryIndex();
  renderPrecoHL();
  renderKPIs(); renderAlertasBanner(); renderAlertasLista();
  populaFiltros(); filtrarTabela();
  populaFiltrosErros(); filtrarErros();
  initGrupos();
}}

// ── KPIs ─────────────────────────────────────────────────────────────────────
function renderKPIs(){{
  const ok=TODOS.filter(r=>r.preco_atual),err=TODOS.filter(r=>!r.preco_atual);
  const nc=ALERTAS.filter(a=>a.nivel==="critico").length;
  document.getElementById("kpi-row").innerHTML=`
    <div class="kpi"><div class="kpi-label">Total collected</div><div class="kpi-val">${{TODOS.length}}</div></div>
    <div class="kpi green"><div class="kpi-label">With price</div><div class="kpi-val">${{ok.length}}</div></div>
    <div class="kpi ${{err.length?"warn":""}}"><div class="kpi-label">Errors</div><div class="kpi-val">${{err.length}}</div><div class="kpi-sub">${{TODOS.length?(err.length/TODOS.length*100).toFixed(0):0}}%</div></div>
    <div class="kpi"><div class="kpi-label">On promotion</div><div class="kpi-val" style="color:var(--yellow)">${{TODOS.filter(r=>r.em_promocao).length}}</div></div>
    <div class="kpi"><div class="kpi-label">Supermarkets</div><div class="kpi-val">${{new Set(ok.map(r=>r.supermercado)).size}}</div></div>
    <div class="kpi ${{nc?"warn":""}}"><div class="kpi-label">Alerts</div><div class="kpi-val">${{ALERTAS.length}}</div><div class="kpi-sub">${{nc}} critical</div></div>`;
}}
function renderAlertasBanner(){{
  const crit=ALERTAS.filter(a=>a.nivel==="critico");
  document.getElementById("alertas-banner").innerHTML=crit.length===0
    ?`<div class="alertas-ok" style="margin-bottom:1rem">✅ No critical issues detected.</div>`
    :crit.map(a=>`<div class="alerta alerta-critico" style="margin-bottom:.5rem"><div class="al-icon">🔴</div><div><div class="al-titulo">${{a.titulo}}</div><div class="al-detalhe">${{a.detalhe}}</div><div class="al-acao">${{a.acao}}</div></div></div>`).join("");
}}
function renderAlertasLista(){{
  const el=document.getElementById("alertas-lista");
  if(!ALERTAS.length){{el.innerHTML=`<div class="alertas-ok">✅ No active alerts.</div>`;return;}}
  el.innerHTML=ALERTAS.map(a=>`<div class="alerta alerta-${{a.nivel}}"><div class="al-icon">${{a.nivel==="critico"?"🔴":"🟡"}}</div><div><div class="al-titulo">[${{a.nivel.toUpperCase()}}] ${{a.titulo}}</div><div class="al-detalhe">${{a.detalhe}}</div><div class="al-acao">${{a.acao}}</div></div></div>`).join("");
}}

// ── Overview table ────────────────────────────────────────────────────────────
function populaFiltros(){{
  const m=document.getElementById("f-marca");
  if(m)[...new Set(TODOS.map(r=>r.marca))].sort().forEach(x=>m.innerHTML+=`<option>${{x}}</option>`);
}}
let tabelaData=[];
function filtrarTabela(){{
  const sm=(document.getElementById("f-sm")||{{}}).value||"";
  const grp=(document.getElementById("f-grupo")||{{}}).value||"";
  const cat=(document.getElementById("f-cat")||{{}}).value||"";
  const marca=(document.getElementById("f-marca")||{{}}).value||"";
  const st=(document.getElementById("f-status")||{{}}).value||"";
  const busca=((document.getElementById("f-busca")||{{}}).value||"").toLowerCase();
  tabelaData=TODOS.filter(r=>(!sm||r.supermercado===sm)&&(!grp||r.grupo===grp)&&(!cat||r.categoria===cat)&&(!marca||r.marca===marca)&&(!st||(st==="ok"?!!r.preco_atual:!r.preco_atual))&&(!busca||(r.nome_produto+r.marca).toLowerCase().includes(busca)));
  const body=document.getElementById("tabela-body"); if(!body)return;
  body.innerHTML=tabelaData.slice(0,2000).map(r=>{{
    const st=r.preco_atual?`<span class="badge b-ok">✅</span>`:`<span class="badge b-err">❌</span>`;
    const promo=r.em_promocao?`<span class="badge b-promo">promo</span>`:"";
    return `<tr><td>${{st}}</td><td><span class="badge ${{SM_BADGE[r.supermercado]||""}}">${{r.supermercado}}</span></td>
      <td style="font-size:11px;color:var(--muted)">${{r.grupo||""}}</td><td style="color:var(--muted)">${{r.categoria}}</td>
      <td style="font-weight:500">${{r.marca}}</td><td>${{r.nome_produto}}</td><td>${{r.embalagem}}</td>
      <td style="font-size:11px;color:var(--muted)">${{r.data_coleta||""}}</td>
      <td style="font-weight:700;color:${{r.em_promocao?"var(--green)":"inherit"}}">${{fmt(r.preco_atual)}} ${{promo}}</td>
      <td style="color:var(--muted);text-decoration:line-through">${{fmt(r.preco_original)}}</td>
      <td>${{fmtPct(r.preco_atual,r.preco_original)!=="—"?`<span class="badge b-promo">${{fmtPct(r.preco_atual,r.preco_original)}}</span>`:"—"}}</td>
      <td style="font-size:11px;color:var(--red);max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${{r.erro||""}}</td></tr>`;
  }}).join("");
  document.getElementById("tabela-count").textContent=`Showing ${{Math.min(tabelaData.length,2000)}} of ${{tabelaData.length}} records`;
}}

// ── Errors ────────────────────────────────────────────────────────────────────
function populaFiltrosErros(){{
  const dias=[...new Set(ERROS_H.map(r=>r.data_coleta))].sort().reverse();
  const tipos=[...new Set(ERROS_H.map(r=>r.erro).filter(Boolean))].sort();
  const d=document.getElementById("fe-dia"),t=document.getElementById("fe-tipo");
  if(d)dias.forEach(x=>d.innerHTML+=`<option>${{x}}</option>`);
  if(t)tipos.forEach(x=>t.innerHTML+=`<option>${{x}}</option>`);
}}
let errosData=[];
function filtrarErros(){{
  const dia=(document.getElementById("fe-dia")||{{}}).value||"";
  const sm=(document.getElementById("fe-sm")||{{}}).value||"";
  const cat=(document.getElementById("fe-cat")||{{}}).value||"";
  const tipo=(document.getElementById("fe-tipo")||{{}}).value||"";
  errosData=ERROS_H.filter(r=>(!dia||r.data_coleta===dia)&&(!sm||r.supermercado===sm)&&(!cat||r.categoria===cat)&&(!tipo||r.erro===tipo));
  const body=document.getElementById("erros-body"); if(!body)return;
  body.innerHTML=errosData.map(r=>`<tr>
    <td style="color:var(--muted)">${{r.data_coleta}}</td>
    <td><span class="badge ${{SM_BADGE[r.supermercado]||""}}">${{r.supermercado}}</span></td>
    <td style="color:var(--muted)">${{r.categoria}}</td><td>${{r.nome_produto}}</td><td>${{r.embalagem}}</td>
    <td style="font-size:11px;color:var(--red)">${{r.erro||""}}</td>
    <td>${{r.url?`<a href="${{r.url}}" target="_blank" style="color:var(--accent);font-size:10px">↗</a>`:"—"}}</td>
  </tr>`).join("");
  document.getElementById("erros-count").textContent=`${{errosData.length}} errors`;
}}

// ── Excel ─────────────────────────────────────────────────────────────────────
function exportarExcel(){{
  if(!tabelaData.length)return;
  const wb=XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(wb,XLSX.utils.json_to_sheet(tabelaData),"Prices");
  XLSX.writeFile(wb,`fnb_prices_{ultima_data}.xlsx`);
}}
function exportarErrosExcel(){{
  const wb=XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(wb,XLSX.utils.json_to_sheet(errosData.length?errosData:ERROS_H),"Errors");
  XLSX.writeFile(wb,`fnb_errors_{ultima_data}.xlsx`);
}}
function exportarComparacao(gid){{
  const el=document.getElementById("tabela-comp-"+gid);
  if(!el)return;
  const rows=[];
  el.querySelectorAll("tbody tr").forEach(tr=>{{
    const cells=[...tr.querySelectorAll("td")].map(td=>td.textContent.trim());
    if(cells.length) rows.push(cells);
  }});
  if(!rows.length)return;
  const headers=[...el.querySelectorAll("thead th")].map(th=>th.textContent.trim());
  const data=[headers,...rows];
  const ws=XLSX.utils.aoa_to_sheet(data);
  const wb=XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(wb,ws,"Comparison");
  XLSX.writeFile(wb,`comparison_${{gid}}_{ultima_data}.xlsx`);
}}
function exportarGrupoExcel(gid,nome){{
  const dados=grupoTabelaData[gid]||[]; if(!dados.length)return;
  const wb=XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(wb,XLSX.utils.json_to_sheet(dados),nome.slice(0,31));
  XLSX.writeFile(wb,`fnb_${{gid}}_{ultima_data}.xlsx`);
}}

// ══════════════════════════════════════════════════════════════════════════════
// GROUP PAGES
// ══════════════════════════════════════════════════════════════════════════════
const grupoCharts={{}};
const clusterCharts={{}};
const grupoTabelaData={{}};
// Per-group cluster state: gid -> [ {{id, nome, skus: [...], removedSkus: Set, visible: bool}} ]
const grupoClusterState={{}};

function gid2grupo(gid){{
  return Object.keys(GRUPOS).find(g=>g.replace(/ /g,"_").replace(/,/g,"").replace(/[/]/g,"")==gid)||"";
}}

function initGrupos(){{
  Object.entries(GRUPOS).forEach(([grupo,cats])=>{{
    const gid=grupo.replace(/ /g,"_").replace(/,/g,"").replace(/[/]/g,"");
    // Init cluster state from defaults
    const defClusters=CLUSTERS_DEF_ORIG[grupo]||[]; grupoClusterState[gid]=defClusters.map(c=>{{const o=Object.assign({{}},c); o.removedSkus=new Set(); o.visible=true; return o;}});
    popularChkProdGrupo(gid,cats);
    popularChkSMGrupo(gid);
    renderGrupo(gid);
  }});
}}

function popularChkProdGrupo(gid,cats){{
  const el=document.getElementById("chk-prod-"+gid); if(!el)return;
  const prods=[...new Map(HIST.filter(r=>cats.includes(r.categoria)).map(r=>[r.nome_produto+"_"+r.embalagem,r.nome_produto+" "+r.embalagem])).entries()].sort((a,b)=>a[1].localeCompare(b[1]));
  el.innerHTML=prods.map(([k,v],i)=>`<label style="display:flex;align-items:center;gap:6px;padding:3px 0;font-size:11px;cursor:pointer">
    <input type="checkbox" value="${{k}}" ${{new Set(["Heineken Lata_350ml","Amstel Lata_350ml","Brahma Duplo Malte_350ml","Corona Extra Lata_350ml","Itaipava Lata_350ml","Skol Lata_350ml"]).has(k)?"checked":""}} onchange="renderGrupo('${{gid}}')" style="accent-color:var(--accent)"> ${{v}}</label>`).join("");
}}

function popularChkSMGrupo(gid){{
  const el=document.getElementById("chk-sm-"+gid); if(!el)return;
  const sms=[...new Set(HIST.map(r=>r.supermercado))].sort();
  el.innerHTML=`<label style="display:flex;align-items:center;gap:6px;padding:3px 0 5px;margin-bottom:3px;border-bottom:1px solid var(--border);font-size:11px;cursor:pointer">
    <input type="checkbox" value="__avg__" checked onchange="renderGrupo('${{gid}}')" style="accent-color:var(--blue)">
    <span style="color:var(--blue);font-weight:600">⌀ Average</span></label>`+
  sms.map(sm=>`<label style="display:flex;align-items:center;gap:6px;padding:3px 0;font-size:11px;cursor:pointer">
    <input type="checkbox" value="${{sm}}" onchange="renderGrupo('${{gid}}')" style="accent-color:var(--accent)"> ${{sm}}</label>`).join("");
}}

function toggleTodos(cid,marcar,gid){{
  document.querySelectorAll(`#${{cid}} input[type=checkbox]`).forEach(cb=>cb.checked=marcar);
  renderGrupo(gid);
}}
function getChecked(cid){{return[...document.querySelectorAll(`#${{cid}} input[type=checkbox]:checked`)].map(cb=>cb.value);}}

function onPeriodoChange(gid){{
  document.getElementById("range-"+gid).style.display=document.getElementById("sel-periodo-"+gid).value==="custom"?"flex":"none";
  renderGrupo(gid);
}}
function getPeriodoDatas(gid,all){{
  const s=[...all].sort(); if(!s.length)return s;
  const p=document.getElementById("sel-periodo-"+gid)?.value||"tudo";
  if(p==="7d")return s.slice(-7); if(p==="30d")return s.slice(-30);
  if(p==="3m")return s.slice(-90);
  if(p==="ano")return s.filter(d=>d.startsWith(s[s.length-1].slice(0,4)));
  if(p==="custom"){{
    const de=document.getElementById("dt-de-"+gid)?.value||"";
    const ate=document.getElementById("dt-ate-"+gid)?.value||"";
    return s.filter(d=>(!de||d>=de)&&(!ate||d<=ate));
  }}
  return s;
}}

function renderGrupo(gid){{
  const cats=GRUPOS[gid2grupo(gid)]||[];
  const smsSel=getChecked("chk-sm-"+gid);
  const smsFisicos=smsSel.filter(s=>s!=="__avg__");
  const showAvg=smsSel.includes("__avg__");
  const prodsSel=getChecked("chk-prod-"+gid);

  // Se Average marcado sem SM físico selecionado, usa todos os SMs disponíveis para calcular média
  const smsParaDados = smsFisicos.length > 0 ? smsFisicos
    : [...new Set(HIST.filter(r=>cats.includes(r.categoria)).map(r=>r.supermercado))];

  let dadosBase=HIST.filter(r=>cats.includes(r.categoria)&&smsParaDados.includes(r.supermercado));
  if(prodsSel.length) dadosBase=dadosBase.filter(r=>prodsSel.includes(r.nome_produto+"_"+r.embalagem));
  const datas=getPeriodoDatas(gid,[...new Set(dadosBase.map(r=>r.data_coleta))]);
  const dados=dadosBase.filter(r=>datas.includes(r.data_coleta));

  // Build series per SM+product — só se houver SMs físicos selecionados
  const mostrarLinhasIndividuais = smsFisicos.length > 0;
  const series=new Map();
  if(mostrarLinhasIndividuais) dados.forEach(r=>{{
    const lbl=`${{r.supermercado}} — ${{r.nome_produto}} ${{r.embalagem}}`;
    if(!series.has(lbl))series.set(lbl,{{}});
    const bd=series.get(lbl);
    if(!bd[r.data_coleta])bd[r.data_coleta]=[];
    bd[r.data_coleta].push(r.preco_atual);
  }});

  // Average series: avg across ALL available supermarkets
  if(showAvg){{
    const dadosAvgBase=HIST.filter(r=>cats.includes(r.categoria));
    const dadosAvgFiltered=prodsSel.length?dadosAvgBase.filter(r=>prodsSel.includes(r.nome_produto+"_"+r.embalagem)):dadosAvgBase;
    const dadosAvg=dadosAvgFiltered.filter(r=>datas.includes(r.data_coleta));
    const byProd=new Map();
    dadosAvg.forEach(r=>{{
      const k=r.nome_produto+"_"+r.embalagem;
      if(!byProd.has(k))byProd.set(k,{{}});
      const bd=byProd.get(k);
      if(!bd[r.data_coleta])bd[r.data_coleta]=[];
      bd[r.data_coleta].push(r.preco_atual);
    }});
    // Atribui cor por SKU antes de inserir na série
    let skuColorIdx=0;
    const skuColors=new Map();
    byProd.forEach((byDate,k)=>{{
      if(!skuColors.has(k)) skuColors.set(k, CLUSTER_COLORS[skuColorIdx++%CLUSTER_COLORS.length]);
    }});
    byProd.forEach((byDate,k)=>{{
      const [nome,...rest]=k.split("_"); const emb=rest.join("_");
      const lbl=`⌀ ${{nome}} ${{emb}}`;
      series.set(lbl,Object.fromEntries(Object.entries(byDate).map(([d,v])=>[d,[v.reduce((a,b)=>a+b,0)/v.length]])));
    }});
  }}

  // Mapeia posição de cada SKU no array de séries para determinar cor
  const seriesKeys=[...series.keys()];
  // Pré-calcula cores: Average recebe cor baseada na posição do SKU entre as médias
  const avgKeys=seriesKeys.filter(k=>k.startsWith("⌀"));
  const indKeys=seriesKeys.filter(k=>!k.startsWith("⌀"));

  const datasets=[...series.entries()].slice(0,20).map(([lbl,byDate],i)=>{{
    const isAvg=lbl.startsWith("⌀");
    // Cor: Average usa índice dentro das médias; individual usa índice dentro das individuais
    const colorIdx=isAvg
      ? avgKeys.indexOf(lbl) % CLUSTER_COLORS.length
      : indKeys.indexOf(lbl) % CLUSTER_COLORS.length;
    const color=CLUSTER_COLORS[colorIdx];
    return {{label:lbl,
      data:datas.map(d=>{{const v=byDate[d];return v?.length?+(v.reduce((a,b)=>a+b,0)/v.length).toFixed(2):null;}}),
      borderColor:color,backgroundColor:color+"22",borderDash:isAvg?[6,3]:[],
      borderWidth:isAvg?2.5:1.5,tension:.3,spanGaps:true,pointRadius:3,pointHoverRadius:5}};
  }});

  if(grupoCharts[gid]){{grupoCharts[gid].destroy();grupoCharts[gid]=null;}}
  const ctx=document.getElementById("chart-"+gid);
  if(ctx) grupoCharts[gid]=new Chart(ctx,{{
    type:"line",data:{{labels:datas,datasets}},
    options:{{responsive:true,maintainAspectRatio:false,interaction:{{mode:"index",intersect:false}},
      plugins:{{legend:{{position:"top",labels:{{boxWidth:12,font:{{size:10}}}}}},
        tooltip:{{callbacks:{{label:c=>`${{c.dataset.label}}: ${{c.parsed.y!=null?"BRL "+c.parsed.y.toFixed(2).replace(".",","):"—"}}`}}}}}},
      scales:{{x:{{ticks:{{font:{{size:11}},maxRotation:45}}}},
        y:{{ticks:{{font:{{size:11}},callback:v=>"BRL"+v.toFixed(2).replace(".",",")}},beginAtZero:false}}}}}}
  }});

  const ultimoDia=datas[datas.length-1]||"";
  renderTabelaComparacao(gid,dados.filter(r=>r.data_coleta===ultimoDia),ultimoDia,showAvg,smsFisicos);
  renderTabelaGrupoFiltrada(gid);
  renderClusterCards(gid);
  renderClusterChart(gid);
}}

function renderTabelaComparacao(gid,dadosDia,ultimoDia,showAvg,smsFisicos){{
  const el=document.getElementById("tabela-comp-"+gid); if(!el)return;
  if(!dadosDia.length){{el.innerHTML=`<p style="font-size:12px;color:var(--muted)">No data for selected period.</p>`;return;}}
  const map=new Map();
  dadosDia.forEach(r=>{{
    const k=r.supermercado+"||"+r.nome_produto+"||"+r.embalagem;
    if(!map.has(k))map.set(k,{{sm:r.supermercado,nome:r.nome_produto,emb:r.embalagem,precos:[]}});
    map.get(k).precos.push(r.preco_atual);
  }});
  const linhas=[...map.values()].map(g=>{{const m=+(g.precos.reduce((a,b)=>a+b,0)/g.precos.length).toFixed(2);return{{...g,media:m}};}}).sort((a,b)=>a.nome.localeCompare(b.nome)||a.media-b.media);
  const prodAvg=new Map();
  if(showAvg){{
    const bp=new Map();
    dadosDia.forEach(r=>{{const k=r.nome_produto+"||"+r.embalagem;if(!bp.has(k))bp.set(k,[]);bp.get(k).push(r.preco_atual);}});
    bp.forEach((v,k)=>prodAvg.set(k,+(v.reduce((a,b)=>a+b,0)/v.length).toFixed(2)));
  }}
  const minP=Math.min(...linhas.map(r=>r.media));
  el.innerHTML=`<p style="font-size:11px;color:var(--muted);margin-bottom:.5rem">Prices collected on <strong>${{ultimoDia||"—"}}</strong></p>
    <div class="table-wrap"><table>
      <thead><tr><th>Supermarket</th><th>Product</th><th>Size</th><th>Avg Price</th>${{showAvg?`<th style="color:var(--blue)">⌀ Average</th>`:""}}
      </tr></thead><tbody>
      ${{linhas.map(r=>{{const avg=prodAvg.get(r.nome+"||"+r.emb);
        return `<tr style="${{r.media===minP?"background:#f0fdf4":""}}">
          <td><span class="badge ${{SM_BADGE[r.sm]||""}}">${{r.sm}}</span></td>
          <td style="font-weight:${{r.media===minP?600:400}}">${{r.nome}}</td><td>${{r.emb}}</td>
          <td style="font-weight:600;color:${{r.media===minP?"var(--green)":"inherit"}}">${{fmt(r.media)}} ${{r.media===minP?"🏆":""}}</td>
          ${{showAvg?`<td style="color:var(--blue);font-weight:500">${{avg?fmt(avg):"—"}}</td>`:""}}</tr>`;
      }}).join("")}}
      </tbody></table></div>`;
}}

// ── Cluster cards ─────────────────────────────────────────────────────────────
function renderClusterCards(gid){{
  const el=document.getElementById("cluster-cards-"+gid); if(!el)return;
  const clusters=grupoClusterState[gid]||[];
  const grupo=gid2grupo(gid);
  const cats=GRUPOS[grupo]||[];
  const ultima=TODOS.filter(r=>cats.includes(r.categoria)).map(r=>r.data_coleta).sort().pop()||"";
  const dadosDia=TODOS.filter(r=>cats.includes(r.categoria)&&r.preco_atual&&r.data_coleta===ultima);

  el.innerHTML=clusters.map((c,ci)=>{{
    const activeSKUs=c.skus.filter(s=>!c.removedSkus.has(s));
    const color=CLUSTER_COLORS[ci%CLUSTER_COLORS.length];
    const porSM=new Map();
    dadosDia.filter(r=>activeSKUs.includes(r.nome_produto+"_"+r.embalagem))
      .forEach(r=>{{if(!porSM.has(r.supermercado))porSM.set(r.supermercado,[]);porSM.get(r.supermercado).push(r.preco_atual);}});
    const allP=dadosDia.filter(r=>activeSKUs.includes(r.nome_produto+"_"+r.embalagem)).map(r=>r.preco_atual);
    const overallAvg=allP.length?+(allP.reduce((a,b)=>a+b,0)/allP.length).toFixed(2):null;
    const smRows=[...porSM.entries()].sort((a,b)=>a[1].reduce((x,y)=>x+y,0)/a[1].length-b[1].reduce((x,y)=>x+y,0)/b[1].length)
      .map(([sm,p])=>{{const avg=+(p.reduce((a,b)=>a+b,0)/p.length).toFixed(2);
        return `<div class="cluster-sm-row"><span class="badge ${{SM_BADGE[sm]||""}}" style="min-width:95px">${{sm}}</span><span class="cluster-avg">${{fmt(avg)}}</span><span style="font-size:10px;color:var(--muted)">${{p.length}} SKU(s)</span></div>`;
      }}).join("");
    const skuTags=c.skus.map(s=>{{
      const removed=c.removedSkus.has(s);
      const label=s.replace(/_[^_]*$/,"");
      return `<span class="sku-tag ${{removed?"removed":""}}" onclick="toggleClusterSku('${{gid}}',${{ci}},'${{s}}')" title="${{removed?"Re-add":"Remove"}}">${{label}}</span>`;
    }}).join("");
    const isCustom=!CLUSTERS_DEF_ORIG[gid2grupo(gid)]?.find(x=>x.id===c.id);
    return `<div class="cluster-card" style="border-left:3px solid ${{color}}">
      <div class="cluster-title">
        <div style="display:flex;align-items:center;gap:8px">
          <span style="width:10px;height:10px;border-radius:50%;background:${{color}};flex-shrink:0"></span>
          <span>${{c.nome}}</span>
          ${{overallAvg?`<span style="color:var(--muted);font-weight:400;font-size:11px">avg ${{fmt(overallAvg)}}</span>`:""}}
        </div>
        <div style="display:flex;align-items:center;gap:6px">
          ${{isCustom?`<button onclick="removeCustomCluster('${{gid}}',${{ci}})" style="background:none;border:none;cursor:pointer;font-size:11px;color:var(--muted)">✕</button>`:""}}
          <button class="cluster-toggle ${{c.visible?"on":"off"}}" onclick="toggleClusterVisible('${{gid}}',${{ci}})" title="Toggle on chart"></button>
        </div>
      </div>
      ${{smRows||`<p style="font-size:11px;color:var(--muted)">No data.</p>`}}
      <div class="cluster-skus">${{skuTags}}</div>
    </div>`;
  }}).join("");
}}

function toggleClusterSku(gid,ci,sku){{
  const c=grupoClusterState[gid][ci];
  if(c.removedSkus.has(sku))c.removedSkus.delete(sku); else c.removedSkus.add(sku);
  renderClusterCards(gid); renderClusterChart(gid);
}}
function toggleClusterVisible(gid,ci){{
  grupoClusterState[gid][ci].visible=!grupoClusterState[gid][ci].visible;
  renderClusterCards(gid); renderClusterChart(gid);
}}
function removeCustomCluster(gid,ci){{
  grupoClusterState[gid].splice(ci,1);
  renderClusterCards(gid); renderClusterChart(gid);
}}

// ── Cluster historical chart ──────────────────────────────────────────────────
function renderClusterChart(gid){{
  const grupo=gid2grupo(gid);
  const cats=GRUPOS[grupo]||[];
  const clusters=(grupoClusterState[gid]||[]).filter(c=>c.visible);
  const datas=[...new Set(HIST.filter(r=>cats.includes(r.categoria)).map(r=>r.data_coleta))].sort();

  const datasets=clusters.map((c,ci)=>{{
    const activeSKUs=c.skus.filter(s=>!c.removedSkus.has(s));
    const color=CLUSTER_COLORS[ci%CLUSTER_COLORS.length];
    const data=datas.map(d=>{{
      const vals=HIST.filter(r=>cats.includes(r.categoria)&&activeSKUs.includes(r.nome_produto+"_"+r.embalagem)&&r.data_coleta===d).map(r=>r.preco_atual);
      return vals.length?+(vals.reduce((a,b)=>a+b,0)/vals.length).toFixed(2):null;
    }});
    return {{label:c.nome,data,borderColor:color,backgroundColor:color+"22",tension:.3,spanGaps:true,pointRadius:3,pointHoverRadius:5,borderWidth:2}};
  }});

  if(clusterCharts[gid]){{clusterCharts[gid].destroy();clusterCharts[gid]=null;}}
  const ctx=document.getElementById("chart-cluster-"+gid); if(!ctx)return;
  if(!datasets.length){{
    const c2=ctx.getContext("2d"); c2.clearRect(0,0,ctx.width,ctx.height);
    c2.font="13px sans-serif"; c2.fillStyle="#999"; c2.textAlign="center";
    c2.fillText("No clusters visible",ctx.width/2,ctx.height/2); return;
  }}
  clusterCharts[gid]=new Chart(ctx,{{
    type:"line",data:{{labels:datas,datasets}},
    options:{{responsive:true,maintainAspectRatio:false,interaction:{{mode:"index",intersect:false}},
      plugins:{{legend:{{position:"top",labels:{{boxWidth:12,font:{{size:11}}}}}},
        tooltip:{{callbacks:{{label:c=>`${{c.dataset.label}}: ${{c.parsed.y!=null?"BRL "+c.parsed.y.toFixed(2).replace(".",","):"—"}}`}}}}}},
      scales:{{x:{{ticks:{{font:{{size:11}},maxRotation:45}}}},
        y:{{ticks:{{font:{{size:11}},callback:v=>"BRL"+v.toFixed(2).replace(".",",")}},beginAtZero:false}}}}}}
  }});
}}

// ── Group data table ──────────────────────────────────────────────────────────
function renderTabelaGrupoFiltrada(gid){{
  const grupo=gid2grupo(gid); if(!grupo)return;
  const cats=GRUPOS[grupo];
  const sm=(document.getElementById("tbl-sm-"+gid)||{{}}).value||"";
  const busca=((document.getElementById("tbl-busca-"+gid)||{{}}).value||"").toLowerCase();
  const dados=TODOS.filter(r=>cats.includes(r.categoria)&&(!sm||r.supermercado===sm)&&(!busca||(r.nome_produto+r.marca).toLowerCase().includes(busca)));
  grupoTabelaData[gid]=dados;
  const body=document.getElementById("tbl-body-"+gid); if(!body)return;
  body.innerHTML=dados.slice(0,500).map(r=>{{
    const promo=r.em_promocao?`<span class="badge b-promo">promo</span>`:"";
    return `<tr>
      <td><span class="badge ${{SM_BADGE[r.supermercado]||""}}">${{r.supermercado}}</span></td>
      <td style="color:var(--muted)">${{r.categoria}}</td>
      <td style="font-weight:500">${{r.marca}}</td><td>${{r.nome_produto}}</td><td>${{r.embalagem}}</td>
      <td style="font-size:11px;color:var(--muted)">${{r.data_coleta}}</td>
      <td style="font-weight:600;color:${{r.em_promocao?"var(--green)":"inherit"}}">${{fmt(r.preco_atual)}} ${{promo}}</td>
      <td style="color:var(--muted);text-decoration:line-through">${{fmt(r.preco_original)}}</td>
      <td>${{fmtPct(r.preco_atual,r.preco_original)!=="—"?`<span class="badge b-promo">${{fmtPct(r.preco_atual,r.preco_original)}}</span>`:"—"}}</td>
      <td>${{r.preco_atual?`<span class="badge b-ok">✅</span>`:`<span class="badge b-err">❌</span>`}}</td></tr>`;
  }}).join("");
  const cnt=document.getElementById("tbl-count-"+gid);
  if(cnt)cnt.textContent=`Showing ${{Math.min(dados.length,500)}} of ${{dados.length}} records`;
}}

// ── New cluster modal ─────────────────────────────────────────────────────────
let modalCurrentGid="";
let modalCurrentCats=[];

function abrirModalNovoCluster(gid,cats){{
  modalCurrentGid=gid; modalCurrentCats=cats;
  document.getElementById("modal-cluster-nome").value="";
  document.getElementById("modal-busca").value="";
  preencherModalSkus("");
  document.getElementById("modal-novo-cluster").classList.add("open");
}}
function fecharModal(){{
  document.getElementById("modal-novo-cluster").classList.remove("open");
}}
function filtrarModalSkus(){{
  preencherModalSkus(document.getElementById("modal-busca").value||"");
}}
function preencherModalSkus(busca){{
  const el=document.getElementById("modal-skus-lista"); if(!el)return;
  const b=busca.toLowerCase();
  const skus=[...new Map(HIST.filter(r=>modalCurrentCats.includes(r.categoria)).map(r=>[r.nome_produto+"_"+r.embalagem,r.nome_produto+" "+r.embalagem])).entries()]
    .sort((a,b2)=>a[1].localeCompare(b2[1]))
    .filter(([k,v])=>!b||v.toLowerCase().includes(b));
  el.innerHTML=skus.map(([k,v])=>`
    <label style="display:flex;align-items:center;gap:6px;padding:4px 2px;font-size:12px;cursor:pointer;border-bottom:1px solid #f5f5f5">
      <input type="checkbox" class="modal-sku-cb" value="${{k}}" style="accent-color:var(--accent)">
      <span>${{v}}</span>
    </label>`).join("");
}}
// ── BREWERY INDEX & PRICE PER HL ─────────────────────────────────────────────

const ML_MAP = {{
  "Amstel Lata":              {{"269ml":269,"350ml":350}},
  "Antarctica Lata":          {{"350ml":350}},
  "Brahma Duplo Malte":       {{"269ml":269,"350ml":350}},
  "Budweiser Lata":           {{"269ml":269,"350ml":350}},
  "Corona Extra Lata":        {{"350ml":350}},
  "Corona Extra Long Neck":   {{"330ml":330}},
  "Heineken 0.0":             {{"350ml":350}},
  "Heineken Lata":            {{"269ml":269,"350ml":350}},
  "Itaipava Lata":            {{"350ml":350}},
  "Original Lata":            {{"269ml":269,"350ml":350}},
  "Skol Lata":                {{"269ml":269,"350ml":350}},
  "Spaten Puro Malte Lata":   {{"269ml":269,"350ml":350}},
  "Stella Artois Long Neck":  {{"330ml":330}},
}};

const BRAND_MAP = {{
  "Amstel Lata":"Amstel","Antarctica Lata":"Antarctica",
  "Brahma Duplo Malte":"Brahma","Budweiser Lata":"Budweiser",
  "Corona Extra Lata":"Corona","Corona Extra Long Neck":"Corona",
  "Heineken 0.0":"Heineken","Heineken Lata":"Heineken",
  "Itaipava Lata":"Itaipava","Original Lata":"Original",
  "Skol Lata":"Skol","Spaten Puro Malte Lata":"Spaten",
  "Stella Artois Long Neck":"Stella",
}};

const BREWERY_WEIGHTS = {{
  "Ambev": {{
    "Brahma":0.30,"Skol":0.25,"Antarctica":0.10,
    "Original":0.10,"Budweiser":0.10,"Corona":0.05,
    "Spaten":0.05,"Stella":0.05
  }},
  "Heineken": {{"Heineken":0.80,"Amstel":0.20}},
  "Petrópolis": {{"Itaipava":1.0}},
}};

const BREWERY_COLORS = {{"Ambev":"#E02424","Heineken":"#0E9F6E","Petrópolis":"#C27803"}};
const BRAND_COLORS = ["#0a0a0f","#0e9f6e","#e02424","#c27803","#7e3af2",
                      "#0694a2","#ff8a4c","#84cc16","#ec4899","#14b8a6","#f59e0b"];

function precoHL(nome, emb, preco) {{
  const ml = (ML_MAP[nome]||{{}})[emb];
  if (!ml || !preco) return null;
  return (preco / ml) * 100000;
}}

function mediaHL(brand, datas_arr) {{
  // Média do preço/hl de um brand numa lista de datas, média dos SMs
  const rows = HIST.filter(r=>r.categoria==="Cervejas" && BRAND_MAP[r.nome_produto]===brand);
  if (!rows.length) return null;
  const byData = {{}};
  datas_arr.forEach(d=>{{
    const r_dia = rows.filter(r=>r.data_coleta===d);
    if (!r_dia.length) return;
    const hls = r_dia.map(r=>precoHL(r.nome_produto,r.embalagem,r.preco_atual)).filter(v=>v);
    if (hls.length) byData[d] = hls.reduce((a,b)=>a+b,0)/hls.length;
  }});
  return byData;
}}

function renderBreweryIndex() {{
  const cervDatas = [...new Set(HIST.filter(r=>r.categoria==="Cervejas").map(r=>r.data_coleta))].sort();
  const datasets = Object.entries(BREWERY_WEIGHTS).map(([brewery, weights])=>({{
    label: brewery,
    borderColor: BREWERY_COLORS[brewery],
    backgroundColor: BREWERY_COLORS[brewery]+"22",
    tension:0.3, pointRadius:3, fill:false,
    data: cervDatas.map(d=>{{
      let total=0, wSum=0;
      Object.entries(weights).forEach(([brand,w])=>{{
        const hl = (mediaHL(brand,[d])||{{}})[d];
        if (hl) {{ total+=hl*w; wSum+=w; }}
      }});
      return wSum>0 ? +(total/wSum).toFixed(2) : null;
    }})
  }}));

  const ctx = document.getElementById("chart-brewery-index");
  if (!ctx) return;
  if (ctx._chart) ctx._chart.destroy();
  ctx._chart = new Chart(ctx, {{
    type:"line",
    data:{{labels:cervDatas, datasets}},
    options:{{
      responsive:true, maintainAspectRatio:false,
      plugins:{{legend:{{position:"top"}}}},
      scales:{{y:{{ticks:{{callback:v=>"BRL"+v.toFixed(0)}},title:{{display:true,text:"BRL/hl"}}}}}}
    }}
  }});

  // Tabela última data
  const ultima = cervDatas[cervDatas.length-1]||"";
  const tbl = document.getElementById("tabela-brewery-index");
  if (!tbl) return;
  tbl.innerHTML = `<table style="width:100%;border-collapse:collapse;font-size:12px">
    <thead><tr style="background:var(--bg2)">
      <th style="padding:6px 10px;text-align:left">Brewery</th>
      <th style="padding:6px 10px;text-align:right">BRL/hl (latest)</th>
    </tr></thead><tbody>`+
    Object.entries(BREWERY_WEIGHTS).map(([brewery,weights])=>{{
      let total=0,wSum=0;
      Object.entries(weights).forEach(([brand,w])=>{{
        const hl=(mediaHL(brand,[ultima])||{{}})[ultima];
        if(hl){{total+=hl*w;wSum+=w;}}
      }});
      const val = wSum>0?`BRL ${{(total/wSum).toFixed(2)}}`:"—";
      return `<tr style="border-bottom:1px solid var(--border)">
        <td style="padding:6px 10px;font-weight:600;color:${{BREWERY_COLORS[brewery]}}">${{brewery}}</td>
        <td style="padding:6px 10px;text-align:right">${{val}}</td></tr>`;
    }}).join("")+`</tbody></table>`;
}}

function initBrandHL() {{
  const el = document.getElementById("chk-brand-hl");
  if (!el || el.innerHTML) return;
  const brands = [...new Set(Object.values(BRAND_MAP))].sort();
  const defaultBrands = new Set(["Skol","Brahma","Corona","Heineken","Itaipava"]);
  el.innerHTML = brands.map((b,i)=>`<label style="display:flex;align-items:center;gap:6px;padding:3px 0;font-size:11px;cursor:pointer">
    <input type="checkbox" class="brand-hl-cb" value="${{b}}" ${{defaultBrands.has(b)?"checked":""}} onchange="renderPrecoHL()" style="accent-color:var(--accent)"> ${{b}}</label>`).join("");
}}
function toggleBrandsHL(v) {{
  document.querySelectorAll(".brand-hl-cb").forEach(cb=>cb.checked=v);
  renderPrecoHL();
}}
function getHLPeriodDatas(allDatas) {{
  const s=[...allDatas].sort();
  const p=document.getElementById("sel-periodo-hl")?.value||"tudo";
  if(p==="7d") return s.slice(-7);
  if(p==="30d") return s.slice(-30);
  if(p==="3m") return s.slice(-90);
  if(p==="ano") return s.filter(d=>d.startsWith(s[s.length-1]?.slice(0,4)||""));
  return s;
}}
function renderPrecoHL() {{
  initBrandHL();
  const allDatas = [...new Set(HIST.filter(r=>r.categoria==="Cervejas").map(r=>r.data_coleta))].sort();
  const datas = getHLPeriodDatas(allDatas);
  const selBrands = [...document.querySelectorAll(".brand-hl-cb:checked")].map(cb=>cb.value);
  if (!selBrands.length) return;

  const datasets = selBrands.map((b,i)=>{{
    const color = BRAND_COLORS[i % BRAND_COLORS.length];
    return {{
      label: b,
      borderColor: color,
      backgroundColor: color+"22",
      tension:0.3, pointRadius:3, fill:false,
      data: datas.map(d=>{{
        const hl=(mediaHL(b,[d])||{{}})[d];
        return hl?+hl.toFixed(2):null;
      }})
    }};
  }});

  const ctx = document.getElementById("chart-preco-hl");
  if (!ctx) return;
  if (ctx._chart) ctx._chart.destroy();
  ctx._chart = new Chart(ctx, {{
    type:"line",
    data:{{labels:datas, datasets}},
    options:{{
      responsive:true, maintainAspectRatio:false,
      plugins:{{legend:{{position:"top"}}}},
      scales:{{y:{{ticks:{{callback:v=>"BRL"+v.toFixed(0)}},title:{{display:true,text:"BRL/hl"}}}}}}
    }}
  }});

  // Tabela última data
  const ultima = datas[datas.length-1]||"";
  const tbl = document.getElementById("tabela-preco-hl");
  if (!tbl) return;
  tbl.innerHTML = `<table style="width:100%;border-collapse:collapse;font-size:12px">
    <thead><tr style="background:var(--bg2)">
      <th style="padding:6px 10px;text-align:left">Brand</th>
      <th style="padding:6px 10px;text-align:right">BRL/hl (latest)</th>
    </tr></thead><tbody>`+
    selBrands.map(b=>{{
      const hl=(mediaHL(b,[ultima])||{{}})[ultima];
      return `<tr style="border-bottom:1px solid var(--border)">
        <td style="padding:6px 10px;font-weight:600">${{b}}</td>
        <td style="padding:6px 10px;text-align:right">${{hl?`BRL ${{hl.toFixed(2)}}`:"—"}}</td></tr>`;
    }}).join("")+`</tbody></table>`;
}}
function salvarNovoCluster(){{
  const nome=(document.getElementById("modal-cluster-nome")?.value||"").trim();
  if(!nome){{alert("Enter a cluster name.");return;}}
  const skus=[...document.querySelectorAll(".modal-sku-cb:checked")].map(cb=>cb.value);
  if(!skus.length){{alert("Select at least one SKU.");return;}}
  if(!grupoClusterState[modalCurrentGid])grupoClusterState[modalCurrentGid]=[];
  grupoClusterState[modalCurrentGid].push({{id:"custom_"+Date.now(),nome,skus,removedSkus:new Set(),visible:true}});
  fecharModal();
  renderClusterCards(modalCurrentGid);
  renderClusterChart(modalCurrentGid);
}}

</script>
</body>
</html>"""

def main():
    OUT_PATH.parent.mkdir(exist_ok=True)
    todos, erros, historico, ultima_data, alertas = carregar_dados()
    if not ultima_data:
        ultima_data = str(date.today())
    html = gerar_html(todos, erros, historico, ultima_data, alertas)
    OUT_PATH.write_text(html, encoding="utf-8")
    n_err = sum(1 for r in todos if not r.get("preco_atual"))
    nc = sum(1 for a in alertas if a["nivel"] == "critico")
    print(f"Dashboard generated: {len(todos)} records, {n_err} errors, {nc} critical alerts")

if __name__ == "__main__":
    main()
