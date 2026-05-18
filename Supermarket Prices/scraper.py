"""
Monitor de Preços — Scraper v3 (anti-bot máximo + retry inteligente)

Melhorias vs versão anterior:
- Stealth completo: fingerprint JS, canvas noise, WebGL spoof, plugins fake
- Headers HTTP realistas por supermercado (Referer, Sec-Fetch-*, Accept)
- CEP injetado via cookie E via localStorage antes de cada produto
- wait_for_selector nos elementos de preço (não só timeout fixo)
- Retry automático com backoff: até 3 tentativas por produto
- Rota 0 aprimorada: canonical → busca → URL alternativa no banco
- Seletores CSS atualizados e expandidos para cada supermercado
- Scroll automático para forçar lazy-load de preços
- Salva URL recuperada no banco para reutilizar na próxima coleta
- Commit incremental a cada cidade (não perde dados se o job cair)
"""

import sqlite3, json, re, time, random, csv, hashlib, urllib.request, urllib.error
from datetime import date, datetime
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

_ROOT    = Path(__file__).resolve().parent
DB_PATH  = _ROOT / "precos.db"
LOG_PATH = _ROOT / "coleta.log"

# ─── URLs de busca (Rota 0) ───────────────────────────────────────────────────
BUSCA_URL = {
    "Pão de Açúcar":     "https://www.paodeacucar.com/busca?q={q}",
    "Extra":             "https://www.extramercado.com.br/busca?q={q}",
    "Atacadão":          "https://www.atacadao.com.br/busca/{q}",
}

LINK_SELETOR = {
    "Pão de Açúcar":     'a[href*="/produto/"]',
    "Extra":             'a[href*="/produto/"]',
    "Atacadão":          'a[href*="/p"]',
}

# ─── Headers realistas por supermercado ──────────────────────────────────────
HEADERS_SM = {
    "Pão de Açúcar": {
        "Referer": "https://www.google.com.br/",
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Dest": "document",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9",
    },
    "Extra": {
        "Referer": "https://www.google.com.br/",
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Dest": "document",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9",
        "Origin": "https://www.extramercado.com.br",
    },
    "Atacadão": {
        "Referer": "https://www.google.com.br/",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9",
    },
}

# ─── Script stealth (mascara fingerprints do Playwright) ─────────────────────
STEALTH_JS = """
// Remove webdriver flag
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});

// Plugins realistas
Object.defineProperty(navigator, 'plugins', {get: () => [
  {name:'Chrome PDF Plugin',filename:'internal-pdf-viewer',description:'Portable Document Format'},
  {name:'Chrome PDF Viewer',filename:'mhjfbmdgcfjbbpaeojofohoefgiehjai',description:''},
  {name:'Native Client',filename:'internal-nacl-plugin',description:''},
]});

// Languages
Object.defineProperty(navigator, 'languages', {get: () => ['pt-BR','pt','en-US','en']});

// Platform
Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});

// Hardware concurrency
Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});

// Device memory
Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});

// Canvas fingerprint noise
const origGetContext = HTMLCanvasElement.prototype.getContext;
HTMLCanvasElement.prototype.getContext = function(type, ...args) {
  const ctx = origGetContext.call(this, type, ...args);
  if(type === '2d' && ctx) {
    const origFillText = ctx.fillText.bind(ctx);
    ctx.fillText = function(...a) {
      ctx.shadowBlur = Math.random() * 0.1;
      return origFillText(...a);
    };
  }
  return ctx;
};

// WebGL vendor spoof
const origGetParam = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(param) {
  if(param === 37445) return 'Google Inc. (NVIDIA)';
  if(param === 37446) return 'ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)';
  return origGetParam.call(this, param);
};

// Permissions API
if(navigator.permissions) {
  const origQuery = navigator.permissions.query.bind(navigator.permissions);
  navigator.permissions.query = (params) => {
    if(params.name === 'notifications') return Promise.resolve({state: 'default'});
    return origQuery(params);
  };
}

// Chrome runtime object
window.chrome = {runtime: {}, loadTimes: () => {}, csi: () => {}};
"""

# ─── Injeção de CEP por supermercado ─────────────────────────────────────────
def injetar_cep(page, supermercado, cep):
    """Injeta CEP via cookie e localStorage antes de acessar o produto."""
    try:
        dominio = {
            "Pão de Açúcar":     ".paodeacucar.com",
            "Extra":             ".extra.com.br",
            "Atacadão":          ".atacadao.com.br",
        }.get(supermercado)
        if not dominio: return

        # Cookie
        page.context.add_cookies([{
            "name": "userPostalCode", "value": cep,
            "domain": dominio, "path": "/",
        }])
        # Também tenta via localStorage
        page.evaluate(f"""() => {{
            try {{
                localStorage.setItem('userPostalCode', '{cep}');
                localStorage.setItem('selectedCEP', '{cep}');
                localStorage.setItem('zipCode', '{cep}');
            }} catch(e) {{}}
        }}""")
    except Exception:
        pass

# ─── Cidades ──────────────────────────────────────────────────────────────────
# Para ativar mais cidades, descomente as linhas abaixo
CIDADES = [
    {"cidade":"São Paulo",    "uf":"SP","regiao":"Sudeste", "cep":"01310100"},
    # {"cidade":"Recife",       "uf":"PE","regiao":"Nordeste","cep":"50010010"},
    # {"cidade":"Porto Alegre", "uf":"RS","regiao":"Sul",     "cep":"90010150"},
]

# Mapeamento categoria → grupo do dashboard
CAT_GRUPO = {
    "Cervejas":  "Cervejas",
    "Carnes":    "Carnes, Processados e Preparados",
    "Biscoitos": "Mercearias Secas",
    "Massas":    "Mercearias Secas",
    "Mercearia": "Mercearias Secas",
}

# ─── Produtos por categoria ───────────────────────────────────────────────────
PRODUTOS = {
    "Cervejas": [
        {"marca":"Antarctica",   "nome":"Antarctica Lata",            "embalagem":"350ml"},
        {"marca":"Heineken",      "nome":"Heineken Lata",              "embalagem":"350ml"},
        {"marca":"Heineken",      "nome":"Heineken Lata",              "embalagem":"269ml"},
        {"marca":"Heineken",      "nome":"Heineken 0.0",               "embalagem":"350ml"},
        {"marca":"Skol",          "nome":"Skol Lata",                  "embalagem":"350ml"},
        {"marca":"Skol",          "nome":"Skol Lata",                  "embalagem":"269ml"},
        {"marca":"Brahma",        "nome":"Brahma Duplo Malte",         "embalagem":"350ml"},
        {"marca":"Brahma",        "nome":"Brahma Duplo Malte",         "embalagem":"269ml"},
        {"marca":"Stella Artois", "nome":"Stella Artois Long Neck",    "embalagem":"330ml"},
        {"marca":"Corona",        "nome":"Corona Extra Long Neck",     "embalagem":"330ml"},
        {"marca":"Corona",        "nome":"Corona Extra Lata",          "embalagem":"350ml"},
        {"marca":"Budweiser",     "nome":"Budweiser Lata",             "embalagem":"350ml"},
        {"marca":"Budweiser",     "nome":"Budweiser Lata",             "embalagem":"269ml"},
        {"marca":"Amstel",        "nome":"Amstel Lata",                "embalagem":"350ml"},
        {"marca":"Amstel",        "nome":"Amstel Lata",                "embalagem":"269ml"},
        {"marca":"Spaten",        "nome":"Spaten Puro Malte Lata",     "embalagem":"350ml"},
        {"marca":"Spaten",        "nome":"Spaten Puro Malte Lata",     "embalagem":"269ml"},
        {"marca":"Original",      "nome":"Original Lata",              "embalagem":"350ml"},
        {"marca":"Original",      "nome":"Original Lata",              "embalagem":"269ml"},
        {"marca":"Itaipava",      "nome":"Itaipava Lata",              "embalagem":"350ml"},
    ],
    "Carnes": [
        {"marca":"Sadia",       "nome":"Salsicha Hot Dog 500g Sadia",      "embalagem":"500g"},
        {"marca":"Perdigão",    "nome":"Salsicha Hot Dog 500g Perdigão",   "embalagem":"500g"},
        {"marca":"Seara",       "nome":"Salsicha Hot Dog 500g Seara",      "embalagem":"500g"},
        {"marca":"Sadia",       "nome":"Linguiça Toscana 700g Sadia",      "embalagem":"700g"},
        {"marca":"Perdigão",    "nome":"Linguiça Toscana 700g Perdigão",   "embalagem":"700g"},
        {"marca":"Swift",       "nome":"Linguiça Toscana 700g Swift",      "embalagem":"700g"},
        {"marca":"Sadia",       "nome":"Nuggets de Frango 300g Sadia",     "embalagem":"300g"},
        {"marca":"Sadia",       "nome":"Lasanha Bolonhesa 600g Sadia",     "embalagem":"600g"},
        {"marca":"Perdigão",    "nome":"Lasanha Bolonhesa 600g Perdigão",  "embalagem":"600g"},
        {"marca":"Seara",       "nome":"Lasanha Bolonhesa 600g Seara",     "embalagem":"600g"},
        {"marca":"Sadia",       "nome":"Peito de Frango 1kg Sadia",        "embalagem":"1kg"},
        {"marca":"Swift",       "nome":"Peito de Frango 1kg Swift",        "embalagem":"1kg"},
        {"marca":"Seara",       "nome":"Peito de Frango 1kg Seara",        "embalagem":"1kg"},
        {"marca":"Sadia",       "nome":"Coxa de Frango 1kg Sadia",         "embalagem":"1kg"},
        {"marca":"Swift",       "nome":"Coxa de Frango 1kg Swift",         "embalagem":"1kg"},
        {"marca":"Seara",       "nome":"Coxa de Frango 1kg Seara",         "embalagem":"1kg"},
        {"marca":"Swift",       "nome":"Asa de Frango 1kg Swift",          "embalagem":"1kg"},
        {"marca":"Sadia",       "nome":"Asa de Frango 1kg Sadia",          "embalagem":"1kg"},
        {"marca":"Bassi",       "nome":"Fraldinha 1kg Bassi",              "embalagem":"1kg"},
        {"marca":"Bassi",       "nome":"Picanha 1kg Bassi",                "embalagem":"1kg"},
        {"marca":"Friboi",      "nome":"Picanha 1kg Friboi",               "embalagem":"1kg"},
        {"marca":"Estância 92", "nome":"Picanha 1kg Estância 92",          "embalagem":"1kg"},
        {"marca":"Swift",       "nome":"Carne Moida 1kg Swift",            "embalagem":"1kg"},
    ],
    "Biscoitos": [
        {"marca":"Marilan",  "nome":"Água e Sal 300g Marilan",           "embalagem":"300g"},
        {"marca":"Mabel",    "nome":"Água e Sal 300g Mabel",             "embalagem":"300g"},
        {"marca":"Vitarella","nome":"Água e Sal 350g Vitarella",         "embalagem":"350g"},
        {"marca":"Adria",    "nome":"Água e Sal 170g Adria",             "embalagem":"170g"},
        {"marca":"Piraquê",  "nome":"Água e Sal 184g Piraque",           "embalagem":"184g"},
        {"marca":"Marilan",  "nome":"Cream Cracker 300g Marilan",        "embalagem":"300g"},
        {"marca":"Mabel",    "nome":"Cream Cracker 300g Mabel",          "embalagem":"300g"},
        {"marca":"Vitarella","nome":"Cream Cracker 350g Vitarella",      "embalagem":"350g"},
        {"marca":"Piraquê",  "nome":"Cream Cracker 184g Piraque",        "embalagem":"184g"},
        {"marca":"Marilan",  "nome":"Cream Cracker 140g Marilan",        "embalagem":"140g"},
        {"marca":"Bauducco", "nome":"Cream Cracker 165g Bauducco",       "embalagem":"165g"},
        {"marca":"Adria",    "nome":"Cream Cracker 170g Adria",          "embalagem":"170g"},
        {"marca":"Mondelez", "nome":"Oreo 90g Mondelez",                 "embalagem":"90g"},
        {"marca":"Nestlé",   "nome":"Passatempo 150g Nestlé",            "embalagem":"150g"},
        {"marca":"Bauducco", "nome":"Recheado Chocolate 140g Bauducco",  "embalagem":"140g"},
        {"marca":"Piraquê",  "nome":"Recheado Chocolate 100g Piraque",   "embalagem":"100g"},
    ],
    "Massas": [
        {"marca":"Barilla",    "nome":"Macarrão Espaguete 500g Barilla",    "embalagem":"500g"},
        {"marca":"Adria",      "nome":"Macarrão Espaguete 500g Adria",      "embalagem":"500g"},
        {"marca":"Camil",      "nome":"Macarrão Espaguete 500g Camil",      "embalagem":"500g"},
        {"marca":"Dona Benta", "nome":"Macarrão Espaguete 500g Dona Benta", "embalagem":"500g"},
        {"marca":"Nissin",     "nome":"Miojo Carne 85g Nissin",             "embalagem":"85g"},
    ],
    "Mercearia": [
        {"marca":"Tio João",        "nome":"Arroz Branco 5kg Tio João",                    "embalagem":"5kg"},
        {"marca":"Camil",           "nome":"Arroz Branco 5kg Camil",                       "embalagem":"5kg"},
        {"marca":"Camil",           "nome":"Feijão Carioca 1kg Camil",                     "embalagem":"1kg"},
        {"marca":"Kicaldo",         "nome":"Feijão Carioca 1kg Kicaldo",                   "embalagem":"1kg"},
        {"marca":"União",           "nome":"Açúcar Refinado 1kg União",                    "embalagem":"1kg"},
        {"marca":"Caravelas",       "nome":"Açúcar Refinado 1kg Caravelas",                "embalagem":"1kg"},
        {"marca":"Da Barra",        "nome":"Açúcar Refinado 1kg Da Barra",                 "embalagem":"1kg"},
        {"marca":"Guarani",         "nome":"Açúcar Refinado 1kg Guarani",                  "embalagem":"1kg"},
        {"marca":"Dona Benta",      "nome":"Farinha de Trigo 1kg Dona Benta",              "embalagem":"1kg"},
        {"marca":"Venturelli",      "nome":"Farinha de Trigo 1kg Venturelli",              "embalagem":"1kg"},
        {"marca":"Sol",             "nome":"Farinha de Trigo 1kg Sol",                     "embalagem":"1kg"},
        {"marca":"Pilão",           "nome":"Café Torrado e Moído 500g Pilão",              "embalagem":"500g"},
        {"marca":"3 Corações",      "nome":"Café Torrado e Moído 500g 3 Corações",         "embalagem":"500g"},
        {"marca":"Melitta",         "nome":"Café Torrado e Moído 500g Melitta",            "embalagem":"500g"},
        {"marca":"Café Brasileiro", "nome":"Café Torrado e Moído 500g Café Brasileiro",    "embalagem":"500g"},
        {"marca":"União",           "nome":"Café Torrado e Moído 500g União",              "embalagem":"500g"},
    ],
}

# ─── Links verificados ────────────────────────────────────────────────────────
LINKS = {
    "Pão de Açúcar": {
        "Cervejas": {
            "Amstel Lata_269ml":             "https://www.paodeacucar.com/produto/339944/cerveja-lager-puro-malte-amstel-lata-269ml",
            "Amstel Lata_350ml":             "https://www.paodeacucar.com/produto/1606864/cerveja-lager-puro-malte-amstel-lata-350ml",
            "Brahma Duplo Malte_350ml":      "https://www.paodeacucar.com/produto/462219/cerveja-pilsner-duplo-malte-brahma-lata-350ml",
            "Budweiser Lata_269ml":          "https://www.paodeacucar.com/produto/323696/cerveja-pilsen-budweiser-lata-269ml",
            "Corona Extra Lata_350ml":       "https://www.paodeacucar.com/produto/1461878/cerveja-corona-extra-lata-350ml",
            "Corona Extra Long Neck_330ml":  "https://www.paodeacucar.com/produto/456783/cerveja-pilsen-corona-garrafa-330ml",
            "Heineken 0.0_350ml":            "https://www.paodeacucar.com/produto/1606861/cerveja-heineken-zero-lata-sleek-350ml",
            "Heineken Lata_269ml":           "https://www.paodeacucar.com/produto/1376370/cerveja-lager-puro-malte-heineken-lata-269ml",
            "Heineken Lata_350ml":           "https://www.paodeacucar.com/produto/1606865/cerveja-heineken-lata-sleek-350ml",
            "Itaipava Lata_350ml":           "https://www.paodeacucar.com/produto/112967/cerveja-pilsen-itaipava-lata-350ml",
            "Original Lata_269ml":           "https://www.paodeacucar.com/produto/479389/cerveja-pilsen-antarctica-original-lata-269ml",
                        "Skol Lata_269ml":               "https://www.paodeacucar.com/produto/71229/cerveja-skol-pilsen-lata-269ml",
            "Spaten Puro Malte Lata_269ml":  "https://www.paodeacucar.com/produto/1461013/cerveja-munich-helles-puro-malte-spaten-lata-269ml",
            "Spaten Puro Malte Lata_350ml":  "https://www.paodeacucar.com/produto/583963/cerveja-munich-helles-puro-malte-spaten-lata-350ml",
            "Stella Artois Long Neck_330ml": "https://www.paodeacucar.com/produto/452630/cerveja-lager-premium-puro-malte-stella-artois-garrafa-330ml",
        },
        "Carnes": {
            "Salsicha Hot Dog 500g Sadia_500g":     "https://www.paodeacucar.com/produto/114859/salsicha-hot-dog-sadia-500g-10-unidades",
            "Salsicha Hot Dog 500g Perdigão_500g":  "https://www.paodeacucar.com/produto/113887/salsicha-hot-dog-perdigao-500g-12-unidades",
            "Salsicha Hot Dog 500g Seara_500g":     "https://www.paodeacucar.com/produto/21730/salsicha-hot-dog-seara-500g",
                        "Linguiça Toscana 700g Perdigão_700g":  "https://www.paodeacucar.com/produto/1638980/linguica-toscana-perdigao-na-brasa-700g",
            "Linguiça Toscana 700g Swift_700g":     "https://www.paodeacucar.com/produto/434959/linguica-toscana-swift-700g",
            "Nuggets de Frango 300g Sadia_300g":    "https://www.paodeacucar.com/produto/142969/empanado-de-frango-peito-crocante-sadia-nuggets-pacote-300g",
            "Lasanha Bolonhesa 600g Sadia_600g":    "https://www.paodeacucar.com/produto/344410/lasanha-bolonhesa-sadia-pacote-600g",
            "Lasanha Bolonhesa 600g Perdigão_600g": "https://www.paodeacucar.com/produto/391814/lasanha-bolonhesa-perdigao-nosso-menu-pacote-600g",
            "Lasanha Bolonhesa 600g Seara_600g":    "https://www.paodeacucar.com/produto/113511/lasanha-bolonhesa-seara-600g",
            "Peito de Frango 1kg Sadia_1kg":        "https://www.paodeacucar.com/produto/65885/file-de-peito-de-frango-congelado-sem-pele-sem-osso-sadia-1kg",
            "Peito de Frango 1kg Swift_1kg":        "https://www.paodeacucar.com/produto/445611/file-de-peito-de-frango-swift-do-campo-1kg",
            "Peito de Frango 1kg Seara_1kg":        "https://www.paodeacucar.com/produto/217037/coxa-de-frango-congelada-seara-1kg",
            "Coxa de Frango 1kg Swift_1kg":         "https://www.paodeacucar.com/produto/445594/coxa-de-frango-swift-1kg",
            "Asa de Frango 1kg Swift_1kg":          "https://www.paodeacucar.com/produto/452777/asa-de-frango-swift-bandeja-1kg",
            "Fraldinha 1kg Bassi_1kg":              "https://www.paodeacucar.com/produto/114843/fraldinha-extra-limpa-bovina-bassi-1,2kg",
            "Picanha 1kg Bassi_1kg":                "https://www.paodeacucar.com/produto/115631/picanha-bovina-extra-limpa-pedaco-bassi-a vacuo-1,4kg",
            "Picanha 1kg Friboi_1kg":               "https://www.paodeacucar.com/produto/164337/picanha-resfriada-maturatta-friboi-1,7kg",
            "Picanha 1kg Estância 92_1kg":          "https://www.paodeacucar.com/produto/1574780/picanha-estancia-92-resfriado-1,3kg",
            "Carne Moida 1kg Swift_1kg":            "https://www.paodeacucar.com/produto/1616628/carne-moida-swift-1kg",
        },
        "Biscoitos": {
            "Água e Sal 300g Marilan_300g":          "https://www.paodeacucar.com/produto/1639482/biscoito-agua-e-sal-marilan-300g",
            "Água e Sal 300g Mabel_300g":            "https://www.paodeacucar.com/produto/1616940/biscoito-agua-e-sal-mabel-pacote-300g",
            "Água e Sal 350g Vitarella_350g":        "https://www.paodeacucar.com/produto/1286774/biscoito-agua-e-sal-tradicional-vitarella-pacote-350g",
            "Cream Cracker 300g Marilan_300g":       "https://www.paodeacucar.com/produto/1639385/biscoito-cream-cracker-marilan-300g",
            "Cream Cracker 300g Mabel_300g":         "https://www.paodeacucar.com/produto/1616942/biscoito-cream-cracker-mabel-300g",
            "Cream Cracker 184g Piraque_184g":       "https://www.paodeacucar.com/produto/1606433/biscoito-cream-cracker-piraque-pacote-184g",
            "Cream Cracker 140g Marilan_140g":       "https://www.paodeacucar.com/produto/1448576/biscoito-cream-cracker-marilan-pacote-170g",
            "Cream Cracker 165g Bauducco_165g":      "https://www.paodeacucar.com/produto/1629268/biscoito-cream-cracker-tradicional-bauducco-pacote-165g",
            "Cream Cracker 170g Adria_170g":         "https://www.paodeacucar.com/produto/1602912/biscoito-cream-cracker-folhado-manteiga-adria-folhata-pacote-170g",
            "Oreo 90g Mondelez_90g":                 "https://www.paodeacucar.com/produto/301575/biscoito-original-oreo-pacote-90g",
            "Passatempo 150g Nestlé_150g":           "https://www.paodeacucar.com/produto/177670/biscoito-recheio-chocolate-passatempo-pacote-130g",
            "Recheado Chocolate 140g Bauducco_140g": "https://www.paodeacucar.com/produto/310906/biscoito-wafer-recheio-chocolate-bauducco-pacote-140g",
                    },
        "Massas": {
            "Macarrão Espaguete 500g Barilla_500g":  "https://www.paodeacucar.com/produto/279928/macarrao-com-ovos-espaguete-8-barilla-pacote-500g",
            "Macarrão Espaguete 500g Adria_500g":    "https://www.paodeacucar.com/produto/111375/macarrao-adria-com-ovos-espaguete---8-500g",
            "Miojo Carne 85g Nissin_85g":            "https://www.paodeacucar.com/produto/169902/macarrao-instantaneo-de-carne-nissin-miojo-lamen-pacote-85g",
        },
        "Mercearia": {
            "Arroz Branco 5kg Tio João_5kg":                    "https://www.paodeacucar.com/produto/138068/arroz-agulhinha-tipo-1-tio-joao-pacote-5kg",
            "Arroz Branco 5kg Camil_5kg":                       "https://www.paodeacucar.com/produto/41329/arroz-agulhinha-tipo-1-camil-pacote-5kg",
            "Feijão Carioca 1kg Camil_1kg":                     "https://www.paodeacucar.com/produto/9461/feijao-carioca-tipo-1-camil-pacote-1kg",
            "Feijão Carioca 1kg Kicaldo_1kg":                   "https://www.paodeacucar.com/produto/109209/feijao-carioca-tipo-1-kicaldo-pacote-1kg",
            "Açúcar Refinado 1kg União_1kg":                    "https://www.paodeacucar.com/produto/74215/acucar-refinado-uniao-pacote-1kg",
            "Açúcar Refinado 1kg Caravelas_1kg":                "https://www.paodeacucar.com/produto/61474/acucar-refinado-caravelas-pacote-1kg",
            "Açúcar Refinado 1kg Da Barra_1kg":                 "https://www.paodeacucar.com/produto/10669/acucar-refinado-da-barra-pacote-1kg",
            "Farinha de Trigo 1kg Dona Benta_1kg":              "https://www.paodeacucar.com/produto/43619/farinha-de-trigo-tradicional-dona-benta-pacote-1kg",
            "Café Torrado e Moído 500g Pilão_500g":             "https://www.paodeacucar.com/produto/152052/cafe-torrado-e-moido-tradicional-pilao-pacote-500g",
            "Café Torrado e Moído 500g Café Brasileiro_500g":   "https://www.paodeacucar.com/produto/62071/cafe-torrado-e-moido-tradicional-cafe-brasileiro-pacote-500g",
            "Café Torrado e Moído 500g União_500g":             "https://www.paodeacucar.com/produto/1376191/cafe-torrado-e-moido-tradicional-uniao-pacote-500g",
        },
    },
    "Extra": {
        "Cervejas": {
            "Amstel Lata_269ml":             "https://www.extramercado.com.br/produto/369620/cerveja-lager-puro-malte-amstel-lata-269ml",
            "Amstel Lata_350ml":             "https://www.extramercado.com.br/produto/1641973/cerveja-lager-puro-malte-amstel-lata-350ml",
            "Antarctica Lata_350ml":         "https://www.extramercado.com.br/produto/589/cerveja-pilsen-antarctica-lata-350ml",
            "Brahma Duplo Malte_269ml":      "https://www.extramercado.com.br/produto/869637/cerveja-pilsner-duplo-malte-brahma-lata-269ml",
            "Brahma Duplo Malte_350ml":      "https://www.extramercado.com.br/produto/485275/cerveja-pilsner-duplo-malte-brahma-lata-350ml",
            "Budweiser Lata_269ml":          "https://www.extramercado.com.br/produto/347007/cerveja-pilsen-budweiser-lata-269ml",
            "Budweiser Lata_350ml":          "https://www.extramercado.com.br/produto/190773/cerveja-lager-budweiser-lata-350ml",
            "Corona Extra Lata_350ml":       "https://www.extramercado.com.br/produto/1500760/cerveja-corona-extra-lata-350ml",
            "Corona Extra Long Neck_330ml":  "https://www.extramercado.com.br/produto/452503/cerveja-pilsen-corona-garrafa-330ml",
            "Heineken 0.0_350ml":            "https://www.extramercado.com.br/produto/1641970/cerveja-heineken-zero-lata-sleek-350ml",
            "Heineken Lata_269ml":           "https://www.extramercado.com.br/produto/1440048/cerveja-lager-puro-malte-heineken-lata-269ml",
            "Heineken Lata_350ml":           "https://www.extramercado.com.br/produto/1641976/cerveja-heineken-lata-sleek-350ml",
            "Itaipava Lata_350ml":           "https://www.extramercado.com.br/produto/112967/cerveja-pilsen-itaipava-lata-350ml",
            "Original Lata_269ml":           "https://www.extramercado.com.br/produto/519820/cerveja-pilsen-antarctica-original-lata-269ml",
            "Original Lata_350ml":           "https://www.extramercado.com.br/produto/434881/cerveja-pilsen-antarctica-original-lata-350ml",
            "Skol Lata_269ml":               "https://www.extramercado.com.br/produto/71229/cerveja-skol-pilsen-lata-269ml",
            "Skol Lata_350ml":               "https://www.extramercado.com.br/produto/91224/cerveja-pilsen-skol-lata-350ml",
            "Spaten Puro Malte Lata_269ml":  "https://www.extramercado.com.br/produto/1500377/cerveja-munich-helles-puro-malte-spaten-lata-269ml",
            "Spaten Puro Malte Lata_350ml":  "https://www.extramercado.com.br/produto/628110/cerveja-munich-helles-puro-malte-spaten-lata-350ml",
            "Stella Artois Lata_269ml":      "https://www.extramercado.com.br/produto/71886/cerveja-stella-artois-puro-malte-269ml-lata",
        },
        "Carnes": {
            "Salsicha Hot Dog 500g Sadia_500g":     "https://www.extramercado.com.br/produto/114859/salsicha-hot-dog-sadia-500g-10-unidades",
            "Salsicha Hot Dog 500g Perdigão_500g":  "https://www.extramercado.com.br/produto/113887/salsicha-hot-dog-perdigao-500g-12-unidades",
            "Salsicha Hot Dog 500g Seara_500g":     "https://www.extramercado.com.br/produto/21730/salsicha-hot-dog-seara-500g",
            "Linguiça Toscana 700g Sadia_700g":     "https://www.extramercado.com.br/produto/1649350/linguica-toscana-sadia-700g",
            "Linguiça Toscana 700g Perdigão_700g":  "https://www.extramercado.com.br/produto/1667071/linguica-toscana-perdigao-na-brasa-700g",
            "Linguiça Toscana 700g Swift_700g":     "https://www.extramercado.com.br/produto/422282/linguica-toscana-swift-700g",
            "Nuggets de Frango 300g Sadia_300g":    "https://www.extramercado.com.br/produto/142969/empanado-de-frango-peito-crocante-sadia-nuggets-pacote-300g",
            "Lasanha Bolonhesa 600g Sadia_600g":    "https://www.extramercado.com.br/produto/374206/lasanha-bolonhesa-sadia-pacote-600g",
            "Lasanha Bolonhesa 600g Perdigão_600g": "https://www.extramercado.com.br/produto/392502/lasanha-bolonhesa-perdigao-nosso-menu-pacote-600g",
            "Lasanha Bolonhesa 600g Seara_600g":    "https://www.extramercado.com.br/produto/113511/lasanha-bolonhesa-seara-600g",
            "Peito de Frango 1kg Sadia_1kg":        "https://www.extramercado.com.br/produto/65885/file-de-peito-de-frango-congelado-sem-pele-sem-osso-sadia-1kg",
            "Peito de Frango 1kg Swift_1kg":        "https://www.extramercado.com.br/produto/422487/file-de-peito-de-frango-swift-do-campo-1kg",
            "Coxa de Frango 1kg Sadia_1kg":         "https://www.extramercado.com.br/produto/66810/coxa-de-frango-congelada-sadia-1kg",
            "Coxa de Frango 1kg Swift_1kg":         "https://www.extramercado.com.br/produto/422468/coxa-de-frango-swift-1kg",
            "Coxa de Frango 1kg Seara_1kg":         "https://www.extramercado.com.br/produto/217037/coxa-de-frango-congelada-seara-1kg",
            "Asa de Frango 1kg Swift_1kg":          "https://www.extramercado.com.br/produto/463439/asa-de-frango-swift-bandeja-1kg",
            "Fraldinha 1kg Bassi_1kg":              "https://www.extramercado.com.br/produto/114843/fraldinha-extra-limpa-bovina-bassi-1,2kg",
            "Picanha 1kg Bassi_1kg":                "https://www.extramercado.com.br/produto/115631/picanha-bovina-extra-limpa-pedaco-bassi-a%C2%A0vacuo-1,4kg",
            "Picanha 1kg Estância 92_1kg":          "https://www.extramercado.com.br/produto/1613858/picanha-estancia-92-resfriado-1,3kg",
            "Carne Moida 1kg Swift_1kg":            "https://www.extramercado.com.br/produto/1651983/carne-moida-swift-1kg",
        },
        "Biscoitos": {
            "Água e Sal 300g Marilan_300g":          "https://www.extramercado.com.br/produto/1667484/biscoito-agua-e-sal-marilan-300g",
            "Água e Sal 350g Vitarella_350g":        "https://www.extramercado.com.br/produto/1376440/biscoito-agua-e-sal-tradicional-vitarella-pacote-350g",
            "Água e Sal 170g Adria_170g":            "https://www.extramercado.com.br/produto/1642405/biscoito-agua-e-sal-adria-pacote-170g",
            "Água e Sal 184g Piraque_184g":          "https://www.extramercado.com.br/produto/1641546/biscoito-agua-e-sal-piraque-pacote-184g",
            "Cream Cracker 300g Marilan_300g":       "https://www.extramercado.com.br/produto/1667380/biscoito-cream-cracker-marilan-300g",
            "Cream Cracker 350g Vitarella_350g":     "https://www.extramercado.com.br/produto/1376439/biscoito-cream-cracker-amanteigado-tradicional-vitarella-pacote-350g",
            "Cream Cracker 184g Piraque_184g":       "https://www.extramercado.com.br/produto/1641542/biscoito-cream-cracker-piraque-pacote-184g",
            "Cream Cracker 140g Marilan_140g":       "https://www.extramercado.com.br/produto/1667384/biscoito-cream-cracker-marilan-pacote-140g",
            "Cream Cracker 165g Bauducco_165g":      "https://www.extramercado.com.br/produto/1660610/biscoito-cream-cracker-tradicional-bauducco-pacote-165g",
            "Cream Cracker 170g Adria_170g":         "https://www.extramercado.com.br/produto/1638049/biscoito-cream-cracker-folhado-manteiga-adria-folhata-pacote-170g",
            "Oreo 90g Mondelez_90g":                 "https://www.extramercado.com.br/produto/323114/biscoito-original-oreo-pacote-90g",
            "Passatempo 150g Nestlé_150g":           "https://www.extramercado.com.br/produto/177670/biscoito-recheio-chocolate-passatempo-pacote-130g",
            "Recheado Chocolate 140g Bauducco_140g": "https://www.extramercado.com.br/produto/335660/biscoito-wafer-recheio-chocolate-bauducco-pacote-140g",
                    },
        "Massas": {
            "Macarrão Espaguete 500g Barilla_500g":   "https://www.extramercado.com.br/produto/305593/macarrao-com-ovos-espaguete-8-barilla-pacote-500g",
            "Macarrão Espaguete 500g Adria_500g":     "https://www.extramercado.com.br/produto/111375/macarrao-adria-com-ovos-espaguete---8-500g",
            "Macarrão Espaguete 500g Dona Benta_500g":"https://www.extramercado.com.br/produto/5789/macarrao-de-semola-com-ovos-linguine-dona-benta-pacote-500g",
            "Miojo Carne 85g Nissin_85g":             "https://www.extramercado.com.br/produto/169902/macarrao-instantaneo-de-carne-nissin-miojo-lamen-pacote-85g",
        },
        "Mercearia": {
            "Arroz Branco 5kg Tio João_5kg":                    "https://www.extramercado.com.br/produto/138068/arroz-agulhinha-tipo-1-tio-joao-pacote-5kg",
            "Arroz Branco 5kg Camil_5kg":                       "https://www.extramercado.com.br/produto/41329/arroz-agulhinha-tipo-1-camil-pacote-5kg",
            "Feijão Carioca 1kg Camil_1kg":                     "https://www.extramercado.com.br/produto/9461/feijao-carioca-tipo-1-camil-pacote-1kg",
            "Feijão Carioca 1kg Kicaldo_1kg":                   "https://www.extramercado.com.br/produto/109209/feijao-carioca-tipo-1-kicaldo-pacote-1kg",
            "Açúcar Refinado 1kg União_1kg":                    "https://www.extramercado.com.br/produto/74215/acucar-refinado-uniao-pacote-1kg",
            "Açúcar Refinado 1kg Caravelas_1kg":                "https://www.extramercado.com.br/produto/61474/acucar-refinado-caravelas-pacote-1kg",
            "Açúcar Refinado 1kg Guarani_1kg":                  "https://www.extramercado.com.br/produto/359075/acucar-refinado-guarani-pacote-1kg",
            "Farinha de Trigo 1kg Sol_1kg":                     "https://www.extramercado.com.br/produto/359075/farinha-de-trigo-sol-1kg",
            "Café Torrado e Moído 500g Pilão_500g":             "https://www.extramercado.com.br/produto/152052/cafe-torrado-e-moido-tradicional-pilao-pacote-500g",
            "Café Torrado e Moído 500g Melitta_500g":           "https://www.extramercado.com.br/produto/345621/cafe-torrado-e-moido-tradicional-melitta-pacote-500g",
            "Café Torrado e Moído 500g Café Brasileiro_500g":   "https://www.extramercado.com.br/produto/62071/cafe-torrado-e-moido-tradicional-cafe-brasileiro-pacote-500g",
            "Café Torrado e Moído 500g União_500g":             "https://www.extramercado.com.br/produto/1442268/cafe-torrado-e-moido-tradicional-uniao-pacote-500g",
        },
    },
    "Atacadão": {
        "Cervejas": {
            "Amstel Lata_269ml":             "https://www.atacadao.com.br/cerveja-amstel-54353-11244/p",
            "Amstel Lata_350ml":             "https://www.atacadao.com.br/cerveja-amstel-sleek-86708-11276/p",
            "Antarctica Lata_350ml":         "https://www.atacadao.com.br/cerveja-antarctica-9218-11292/p",
            "Brahma Duplo Malte_269ml":      "https://www.atacadao.com.br/cerveja-brahma-duplo-malte-lata-com-269ml-74794-11647/p",
            "Brahma Duplo Malte_350ml":      "https://www.atacadao.com.br/cerveja-brahma-duplo-malte-lata-com-350ml-67653-11651/p",
            "Budweiser Lata_269ml":          "https://www.atacadao.com.br/cerveja-budweiser-51187-11765/p",
            "Budweiser Lata_350ml":          "https://www.atacadao.com.br/cerveja-budweiser-sleek-lata-com-350ml-80258-11811/p",
            "Corona Extra Long Neck_330ml":  "https://www.atacadao.com.br/cerveja-corona-long-neck-com-330ml-66884-12000/p",
            "Heineken 0.0_350ml":            "https://www.atacadao.com.br/cerveja-heineken-zero-sleek-86709-12501/p",
            "Heineken Lata_269ml":           "https://www.atacadao.com.br/cerveja-heineken-lata-com-269ml-76983-12460/p",
            "Heineken Lata_350ml":           "https://www.atacadao.com.br/cerveja-heineken-sleek-86733-12486/p",
            "Itaipava Lata_350ml":           "https://www.atacadao.com.br/cerveja-itaipava-9850-12669/p",
            "Original Lata_269ml":           "https://www.atacadao.com.br/cerveja-original-lata-com-269ml-71793-12854/p",
            "Original Lata_350ml":           "https://www.atacadao.com.br/cerveja-original-lata-com-350ml-65159-12864/p",
            "Skol Lata_269ml":               "https://www.atacadao.com.br/cerveja-skol-redondinha-6183-13325/p",
            "Skol Lata_350ml":               "https://www.atacadao.com.br/cerveja-skol-pilsen-18650-13267/p",
            "Spaten Puro Malte Lata_269ml":  "https://www.atacadao.com.br/cerveja-puro-malte-spaten-lata-com-269ml-83458-13164/p",
            "Spaten Puro Malte Lata_350ml":  "https://www.atacadao.com.br/cerveja-spaten-puro-malte-lata-com-350ml-74632-13351/p",
            "Stella Artois Lata_269ml":      "https://www.atacadao.com.br/cerveja-stella-artois-lata-com-269ml-58207-13384/p",
            "Stella Artois Long Neck_330ml": "https://www.atacadao.com.br/cerveja-stella-artois-68018-13362/p",
        },
        "Carnes": {
            "Salsicha Hot Dog 500g Sadia_500g":     "https://www.atacadao.com.br/salsicha-hot-dog-sadia-resfriada-49270-17530/p",
            "Salsicha Hot Dog 500g Perdigão_500g":  "https://www.atacadao.com.br/salsicha-hot-dog-perdigao-resfriada-5970-17504/p",
            "Salsicha Hot Dog 500g Seara_500g":     "https://www.atacadao.com.br/salsicha-hot-dog-seara-resfriada-62542-17547/p",
            "Linguiça Toscana 700g Sadia_700g":     "https://www.atacadao.com.br/linguica-toscana-sadia-congelada-86374-36784/p",
            "Linguiça Toscana 700g Perdigão_700g":  "https://www.atacadao.com.br/linguica-toscana-perdigao-nabrasa-9975-59299/p",
            "Nuggets de Frango 300g Sadia_300g":    "https://www.atacadao.com.br/nuggets-de-frango-sadia-crocante-19582-15758/p",
            "Lasanha Bolonhesa 600g Sadia_600g":    "https://www.atacadao.com.br/lasanha-sadia-congelada-bolonhesa-54196-29612/p",
            "Lasanha Bolonhesa 600g Perdigão_600g": "https://www.atacadao.com.br/lasanha-perdigao-congelada-bolonhesa-58251-29563/p",
            "Lasanha Bolonhesa 600g Seara_600g":    "https://www.atacadao.com.br/lasanha-seara-congelada-bolonhesa-32549-29633/p",
            "Peito de Frango 1kg Sadia_1kg":        "https://www.atacadao.com.br/file-de-peito-de-frango-sadia-congelado-bifes-3499-56449/p",
            "Peito de Frango 1kg Seara_1kg":        "https://www.atacadao.com.br/file-de-peito-de-frango-seara-congelado-37849-12021/p",
            "Coxa de Frango 1kg Sadia_1kg":         "https://www.atacadao.com.br/coxas-de-frango-sadia-congelado-assa-facil-47388-26263/p",
            "Asa de Frango 1kg Sadia_1kg":          "https://www.atacadao.com.br/asa-de-frango-sadia-congelada-15062-15376/p",
        },
        "Biscoitos": {
            "Água e Sal 300g Marilan_300g":          "https://www.atacadao.com.br/biscoito-marilan-agua-e-sal-12305-61201/p",
            "Água e Sal 300g Mabel_300g":            "https://www.atacadao.com.br/biscoito-mabel-agua-e-sal-90091-40582/p",
            "Água e Sal 350g Vitarella_350g":        "https://www.atacadao.com.br/biscoito-vitarella-agua-e-sal-pacote-com-350g-75654-31551/p",
            "Água e Sal 170g Adria_170g":            "https://www.atacadao.com.br/biscoito-adria-agua-e-sal-86312-24062/p",
            "Cream Cracker 300g Mabel_300g":         "https://www.atacadao.com.br/biscoito-mabel-cream-cracker-90090-40581/p",
            "Cream Cracker 350g Vitarella_350g":     "https://www.atacadao.com.br/biscoito-cream-cracker-vitarella-tradicional-75658-25458/p",
            "Cream Cracker 140g Marilan_140g":       "https://www.atacadao.com.br/biscoito-marilan-cream-cracker-12307-61182/p",
            "Cream Cracker 170g Adria_170g":         "https://www.atacadao.com.br/biscoito-adria-cream-cracker-86314-24086/p",
            "Oreo 90g Mondelez_90g":                 "https://www.atacadao.com.br/biscoito-recheado-oreo-original-49265-28699/p",
            "Passatempo 150g Nestlé_150g":           "https://www.atacadao.com.br/biscoito-recheado-passatempo-nestle-chocolate-pacote-com-130g-55979-28839/p",
            "Recheado Chocolate 140g Bauducco_140g": "https://www.atacadao.com.br/biscoito-wafer-bauducco-chocolate-39393-31872/p",
            "Recheado Chocolate 100g Piraque_100g":  "https://www.atacadao.com.br/biscoito-wafer-piraque-chocolate-pacote-com-100g-76847-32707/p",
        },
        "Massas": {
            "Macarrão Espaguete 500g Barilla_500g":   "https://www.atacadao.com.br/macarrao-com-ovos-barilla-espaguete-8-74384-1093/p",
            "Macarrão Espaguete 500g Adria_500g":     "https://www.atacadao.com.br/macarrao-com-ovos-adria-espaguete-34028-1061/p",
            "Macarrão Espaguete 500g Camil_500g":     "https://www.atacadao.com.br/macarrao-com-ovos-camil-espaguete-94005-44267/p",
            "Macarrão Espaguete 500g Dona Benta_500g":"https://www.atacadao.com.br/macarrao-com-ovos-dona-benta-espaguete-pacote-com-500g-1998-1187/p",
            "Miojo Carne 85g Nissin_85g":             "https://www.atacadao.com.br/macarrao-instantaneo-nissin-lamen-carne-5612-3297/p",
        },
        "Mercearia": {
            "Arroz Branco 5kg Tio João_5kg":                    "https://www.atacadao.com.br/arroz-tio-joao-agulhinha---tipo-1-5148-15022/p",
            "Arroz Branco 5kg Camil_5kg":                       "https://www.atacadao.com.br/arroz-camil-agulhinha---tipo-1-pacote-com-5kg-12658-13743/p",
            "Feijão Carioca 1kg Camil_1kg":                     "https://www.atacadao.com.br/feijao-carioca-camil-tipo-1-7382-9742/p",
            "Feijão Carioca 1kg Kicaldo_1kg":                   "https://www.atacadao.com.br/feijao-carioca-kicaldo-tipo-1-pacote-com-1kg-11874-9925/p",
            "Açúcar Refinado 1kg União_1kg":                    "https://www.atacadao.com.br/acucar-uniao-refinado-21176-2371/p",
            "Açúcar Refinado 1kg Caravelas_1kg":                "https://www.atacadao.com.br/acucar-caravelas-refinado-25668-1517/p",
            "Açúcar Refinado 1kg Da Barra_1kg":                 "https://www.atacadao.com.br/acucar-da-barra-refinado-pacote-com-1kg-15604-1814/p",
            "Farinha de Trigo 1kg Dona Benta_1kg":              "https://www.atacadao.com.br/farinha-de-trigo-dona-benta-tipo-1-pacote-com-1kg-23162-8563/p",
            "Farinha de Trigo 1kg Venturelli_1kg":              "https://www.atacadao.com.br/farinha-de-trigo-venturelli-tipo-1-pacote-com-1kg-66335-9230/p",
            "Café Torrado e Moído 500g Pilão_500g":             "https://www.atacadao.com.br/cafe-pilao-almofada-4959-3014/p",
            "Café Torrado e Moído 500g 3 Corações_500g":        "https://www.atacadao.com.br/cafe-3-coracoes-tradicional-23371-915/p",
            "Café Torrado e Moído 500g Melitta_500g":           "https://www.atacadao.com.br/cafe-melitta-tradicional-vacuo-caixeta-com-500g-18816-2617/p",
            "Café Torrado e Moído 500g União_500g":             "https://www.atacadao.com.br/cafe-uniao-tradicional-76123-3853/p",
        },
    },
}

SELETORES = {
    "Pão de Açúcar": [
        # GPA / VTEX IO
        ".sales .value",
        "span.sales",
        ".price__sales",
        "[class*='sales'] [class*='value']",
        # VTEX genérico
        "span[class*='sellingPrice']",
        "[class*='ProductPrice'] [class*='selling']",
        ".product-price",
    ],
    "Extra": [
        # extramercado.com.br — mesmo grupo GPA/VTEX
        ".sales .value",
        "span.sales",
        ".price__sales",
        "[class*='sales'] [class*='value']",
        "span[class*='sellingPrice']",
        "[class*='ProductPrice'] [class*='selling']",
        ".product-price",
        "[class*='price-selling']",
        "[class*='priceContainer'] span",
    ],
    "Atacadão": [
        # VTEX legado
        "span[class*='sellingPrice']",
        "h3.valornormal",
        ".valornormal",
        # VTEX IO
        ".price-best-price",
        "span[class*='selling']",
        "[class*='Price']:not([class*='list'])",
        # Atacadão específico
        "[class*='bestPrice']",
        "[class*='priceValue']",
    ],
    "Mateus": [
        "[class*='price'] [class*='value']",
        "span[class*='Price']",
        "[class*='selling']",
        ".product-price",
        "[data-price]",
        "[class*='preco']",
    ],
}

# ─── Banco de dados ───────────────────────────────────────────────────────────
def init_db():
    DB_PATH.parent.mkdir(exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS precos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        data_coleta TEXT NOT NULL, horario_coleta TEXT NOT NULL,
        supermercado TEXT NOT NULL, categoria TEXT NOT NULL,
        grupo TEXT NOT NULL DEFAULT '',
        marca TEXT NOT NULL, nome_produto TEXT NOT NULL, embalagem TEXT NOT NULL,
        cidade TEXT NOT NULL, uf TEXT NOT NULL, regiao TEXT NOT NULL,
        preco_atual REAL, preco_original REAL,
        em_promocao INTEGER DEFAULT 0, disponivel INTEGER DEFAULT 1,
        url TEXT, url_recuperada TEXT, rota_css INTEGER, tentativas INTEGER DEFAULT 1, erro TEXT
    )""")
    cols = [r[1] for r in con.execute("PRAGMA table_info(precos)").fetchall()]
    for col, typ in [("categoria","TEXT"),("grupo","TEXT"),("rota_css","INTEGER"),
                     ("url_recuperada","TEXT"),("tentativas","INTEGER")]:
        if col not in cols:
            con.execute(f"ALTER TABLE precos ADD COLUMN {col} {typ}")
    for idx in [
        "CREATE INDEX IF NOT EXISTS idx_data    ON precos(data_coleta)",
        "CREATE INDEX IF NOT EXISTS idx_produto ON precos(marca,nome_produto,embalagem)",
        "CREATE INDEX IF NOT EXISTS idx_cat     ON precos(categoria)",
        "CREATE INDEX IF NOT EXISTS idx_url     ON precos(url)",
    ]:
        con.execute(idx)
    con.commit()
    return con

def inserir(con, r):
    """Insere resultado de coleta, evitando duplicatas no mesmo dia.

    Regras:
    - Se já existe linha com preco_atual real hoje → só sobrescreve se o novo
      preço vier de uma rota melhor (rota menor = mais confiável) E a variação
      for plausível (≤ 30% de diferença).
    - Se só existe linha de erro/indisponível hoje → substitui pela nova.
    - Nunca insere rota 12 quando já existe rota ≤ 11 com preço válido.
    - Validação de sanidade: novo preço não pode diferir >50% do último preço
      limpo dos últimos 14 dias. Se diferir, descarta e mantém o existente.
    """
    sm   = r["supermercado"]
    nome = r["nome_produto"]
    emb  = r["embalagem"]
    data = r["data_coleta"]
    novo_preco = r.get("preco_atual")
    nova_rota  = r.get("rota_css")

    # ── Validação de sanidade contra histórico ────────────────────────────────
    if novo_preco:
        ultimo = con.execute("""
            SELECT preco_atual FROM precos
            WHERE supermercado=? AND nome_produto=? AND embalagem=?
              AND preco_atual IS NOT NULL AND erro IS NULL
              AND rota_css != 99
              AND data_coleta >= date(?, '-14 days') AND data_coleta < ?
            ORDER BY data_coleta DESC LIMIT 1
        """, (sm, nome, emb, data, data)).fetchone()
        if ultimo and ultimo[0]:
            variacao = abs(novo_preco - ultimo[0]) / ultimo[0]
            if variacao > 0.50:
                # Variação >50% vs histórico recente — provável captura errada
                r = dict(r)
                r["erro"] = f"preco_suspeito_{novo_preco:.2f}_vs_{ultimo[0]:.2f}"
                r["preco_atual"] = None
                r["rota_css"]    = None
                novo_preco = None
                nova_rota  = None

    # ── Verificar o que já existe hoje para esse SKU ──────────────────────────
    existente = con.execute("""
        SELECT rowid, preco_atual, rota_css, erro FROM precos
        WHERE supermercado=? AND nome_produto=? AND embalagem=? AND data_coleta=?
        ORDER BY
            CASE WHEN preco_atual IS NOT NULL AND erro IS NULL THEN 0 ELSE 1 END,
            CASE WHEN rota_css IS NOT NULL THEN rota_css ELSE 999 END
        LIMIT 1
    """, (sm, nome, emb, data)).fetchone()

    if existente:
        ex_rowid, ex_preco, ex_rota, ex_erro = existente

        if ex_preco is not None and ex_erro is None:
            # Já existe preço limpo hoje
            if not novo_preco:
                # Novo resultado é erro — não polui o que já está bom
                return
            # Só substitui se nova rota for mais confiável E preço compatível
            ex_rota_n  = ex_rota  if ex_rota  is not None else 999
            novo_rota_n = nova_rota if nova_rota is not None else 999
            if novo_rota_n < ex_rota_n:
                # Rota melhor — atualiza no lugar
                con.execute("""
                    UPDATE precos SET horario_coleta=?, preco_atual=?, preco_original=?,
                        em_promocao=?, rota_css=?, tentativas=?, erro=NULL,
                        url_recuperada=?
                    WHERE rowid=?
                """, (r["horario_coleta"], novo_preco, r.get("preco_original"),
                      int(r.get("em_promocao", False)), nova_rota,
                      r.get("tentativas", 1), r.get("url_recuperada"), ex_rowid))
            # Se rota igual ou pior, descarta silenciosamente
            return
        else:
            # Existente é erro/indisponível — substitui pelo novo (UPDATE no lugar)
            con.execute("""
                UPDATE precos SET horario_coleta=?, preco_atual=?, preco_original=?,
                    em_promocao=?, disponivel=?, url=?, url_recuperada=?,
                    rota_css=?, tentativas=?, erro=?
                WHERE rowid=?
            """, (r["horario_coleta"], novo_preco, r.get("preco_original"),
                  int(r.get("em_promocao", False)), int(r.get("disponivel", True)),
                  r.get("url"), r.get("url_recuperada"), nova_rota,
                  r.get("tentativas", 1), r.get("erro"), ex_rowid))
            return

    # ── Nenhum registro hoje → INSERT normal ─────────────────────────────────
    con.execute("""INSERT INTO precos
        (data_coleta,horario_coleta,supermercado,categoria,grupo,marca,nome_produto,
         embalagem,cidade,uf,regiao,preco_atual,preco_original,
         em_promocao,disponivel,url,url_recuperada,rota_css,tentativas,erro)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        r["data_coleta"],r["horario_coleta"],r["supermercado"],r["categoria"],r.get("grupo",""),
        r["marca"],r["nome_produto"],r["embalagem"],r["cidade"],r["uf"],r["regiao"],
        r.get("preco_atual"),r.get("preco_original"),
        int(r.get("em_promocao",False)),int(r.get("disponivel",True)),
        r.get("url"),r.get("url_recuperada"),r.get("rota_css"),
        r.get("tentativas",1),r.get("erro"),
    ))

def url_recuperada_do_banco(con, supermercado, nome_produto, embalagem):
    """Reutiliza URL que já foi recuperada com sucesso em coletas anteriores."""
    row = con.execute("""
        SELECT url_recuperada FROM precos
        WHERE supermercado=? AND nome_produto=? AND embalagem=?
          AND url_recuperada IS NOT NULL AND preco_atual IS NOT NULL
        ORDER BY data_coleta DESC LIMIT 1
    """, (supermercado, nome_produto, embalagem)).fetchone()
    return row[0] if row else None

# ─── Extração de preço ────────────────────────────────────────────────────────
def extrair_preco(texto):
    if not texto: return None
    nums = re.findall(r'\d+[.,]\d{2}', re.sub(r'\s+','',str(texto).replace('\xa0','')))
    return float(nums[0].replace(',','.')) if nums else None

def extrair_via_json_ld(page):
    try:
        for s in page.query_selector_all('script[type="application/ld+json"]'):
            try:
                data = json.loads(s.inner_text())
                for item in (data if isinstance(data,list) else [data]):
                    offers = item.get("offers") or item.get("Offers")
                    if offers:
                        if isinstance(offers,list): offers = offers[0]
                        p = offers.get("price") or offers.get("lowPrice")
                        if p: return float(str(p).replace(',','.'))
            except Exception: continue
    except Exception: pass
    return None

def extrair_via_meta(page):
    try:
        for sel in [
            'meta[property="product:price:amount"]',
            'meta[name="price"]',
            'meta[itemprop="price"]',
            'meta[property="og:price:amount"]',
        ]:
            el = page.query_selector(sel)
            if el:
                p = extrair_preco(el.get_attribute("content"))
                if p: return p
    except Exception: pass
    return None

def extrair_via_js(page):
    try:
        return page.evaluate(r"""() => {
            // 1. TreeWalker por texto R$ X,XX
            const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
            let n;
            while(n = w.nextNode()) {
                const t = n.textContent.trim();
                if (/R\$\s*\d+[.,]\d{2}/.test(t) && t.length < 30) {
                    const m = t.match(/\d+[.,]\d{2}/);
                    if (m) return parseFloat(m[0].replace(',','.'));
                }
            }
            // 2. Seletores data-* e atributos
            const attrSels = [
                '[data-price]','[data-selling-price]','[data-product-price]',
                '[itemprop="price"]','[class*="priceValue"]','[class*="bestPrice"]',
            ];
            for (const sel of attrSels) {
                for (const el of document.querySelectorAll(sel)) {
                    const dp = el.getAttribute('data-price')
                             || el.getAttribute('data-selling-price')
                             || el.getAttribute('data-product-price')
                             || el.getAttribute('content');
                    if (dp) {
                        const m = dp.match(/\d+[.,]\d{2}/);
                        if (m) return parseFloat(m[0].replace(',','.'));
                    }
                }
            }
            // 3. Classes de preço genéricas
            const classSels = [
                '[class*="price"]','[class*="Price"]','[class*="preco"]','[class*="Preco"]',
            ];
            for (const sel of classSels) {
                for (const el of document.querySelectorAll(sel)) {
                    const t = el.textContent.trim();
                    if (/R\$\s*\d+[.,]\d{2}/.test(t) && t.length < 30) {
                        const m = t.match(/\d+[.,]\d{2}/);
                        if (m) return parseFloat(m[0].replace(',','.'));
                    }
                }
            }
            return null;
        }""")
    except Exception: return None

def scroll_e_aguarda(page, supermercado):
    """Scroll para forçar lazy-load + espera adaptativa por supermercado."""
    try:
        # Scroll suave até o meio da página (onde geralmente fica o preço)
        page.evaluate("window.scrollTo({top: 500, behavior: 'smooth'})")
        page.wait_for_timeout(800)
        page.evaluate("window.scrollTo({top: 0, behavior: 'smooth'})")
        page.wait_for_timeout(400)
    except Exception: pass

    # Espera por seletor de preço específico (mais confiável que timeout fixo)
    seletores_espera = {
        "Pão de Açúcar":     ".sales .value, span[class*='sellingPrice']",
        "Extra":             ".sales .value, span[class*='sellingPrice']",
        "Atacadão":          "span[class*='sellingPrice'], .valornormal, .price-best-price",
    }
    sel = seletores_espera.get(supermercado)
    if sel:
        try:
            page.wait_for_selector(sel, timeout=5000, state="visible")
        except Exception:
            pass  # se não aparecer, tenta as rotas de extração mesmo assim

def pagina_valida(page):
    try:
        titulo = page.title().lower()
        url    = page.url.lower()
        # Títulos que indicam erro
        if any(x in titulo for x in ["página não encontrada","not found","erro 404","indisponível","acesso negado","untitled"]):
            return False
        # URLs que indicam erro
        if any(x in url for x in ["404","not-found","erro","blocked","captcha"]):
            return False
        # Título vazio ou genérico indica página não carregada
        if not titulo or titulo in ["extra mercado", "pão de açúcar", "atacadão"]:
            return False
        # Se o título tem conteúdo específico de produto, é válida
        # Não checa h1 pois pode não ter carregado ainda via JS
        return True
    except Exception:
        return True

def recuperar_url(page, nome_produto, embalagem, supermercado, con=None):
    """
    Rota 0 em 3 etapas:
    1. URL recuperada anteriormente no banco (mais rápido)
    2. Tag canonical da página atual
    3. Busca no site
    """
    # Etapa 1: banco de dados
    if con:
        url_banco = url_recuperada_do_banco(con, supermercado, nome_produto, embalagem)
        if url_banco:
            return url_banco, "banco"

    # Etapa 2: canonical
    try:
        canonical = page.query_selector('link[rel="canonical"]')
        if canonical:
            href = canonical.get_attribute("href")
            if href and href != page.url and "/p" in href:
                return href, "canonical"
    except Exception: pass

    # Etapa 3: busca no site
    if supermercado not in BUSCA_URL:
        return None, None
    try:
        query = f"{nome_produto} {embalagem}".replace(" ","+")
        url_busca = BUSCA_URL[supermercado].format(q=query)
        page.goto(url_busca, wait_until="domcontentloaded", timeout=15000)
        page.wait_for_timeout(1500)
        sel = LINK_SELETOR.get(supermercado, 'a[href*="/p"]')
        # Pega o primeiro link de produto válido (ignora links de categoria)
        for link in page.query_selector_all(sel):
            href = link.get_attribute("href") or ""
            if not href: continue
            if not href.startswith("http"):
                base = "/".join(BUSCA_URL[supermercado].split("/")[:3])
                href = base + href
            # Filtra links de listagem/categoria
            if any(x in href for x in ["/busca","/categoria","/c/","/colecao"]):
                continue
            return href, "busca"
    except Exception: pass
    return None, None

def coletar_pagina(page, url, supermercado, nome_produto="", embalagem="", con=None, tentativa=1, categoria=""):
    resultado = {
        "url": url, "disponivel": False, "preco_atual": None,
        "preco_original": None, "em_promocao": False,
        "rota_css": None, "url_recuperada": None, "tentativas": tentativa, "erro": None,
    }
    try:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=18000)
        except Exception as e:
            if "ERR_HTTP2_PROTOCOL_ERROR" in str(e):
                # Espera mais e tenta de novo
                time.sleep(random.uniform(8, 12))
                page.goto(url, wait_until="domcontentloaded", timeout=25000)
            else:
                raise

        # Injeta CEP após carregar a página (para sites que leem do localStorage)
        scroll_e_aguarda(page, supermercado)

        # Extra e Pão de Açúcar precisam de tempo extra para o JS carregar o conteúdo
        if supermercado in ("Extra", "Pão de Açúcar"):
            carregou = False
            for sel in ["h1", "[class*='product']", "[class*='Product']", "[itemprop='name']", ".pdp-title"]:
                try:
                    page.wait_for_selector(sel, timeout=12000, state="attached")
                    carregou = True
                    break
                except Exception:
                    continue
            if not carregou:
                page.wait_for_timeout(5000)

        # Debug — loga título e URL para diagnóstico
        _titulo_debug = page.title()
        _url_debug = page.url

        # Rota 0 — página inválida → erro direto (sem recuperação de URL)
        if not pagina_valida(page):
            resultado["erro"] = "pagina_invalida_url_desatualizada"
            return resultado

        # Verifica indisponibilidade ANTES de qualquer extração de preço
        indisponivel = page.evaluate("""() => {
            // Seletores CSS por classe
            const sels = [
                '[class*="unavailable"]', '[class*="Unavailable"]',
                '[class*="out-of-stock"]', '[class*="outOfStock"]',
                '[class*="indisponivel"]', '[class*="esgotado"]',
                '[class*="sold-out"]', '[class*="SoldOut"]',
                '[class*="sem-estoque"]', '[class*="productUnavailable"]',
                '[class*="product-unavailable"]'
            ];
            for (const s of sels) {
                const el = document.querySelector(s);
                if (el && el.offsetParent !== null) return true;
            }
            // Busca por texto exato — cobre Extra e outros sites com classes dinâmicas
            const allSpans = document.querySelectorAll('span, button, p, div, h2, h3');
            for (const el of allSpans) {
                const txt = el.textContent.trim().toLowerCase();
                if ((txt === 'indisponível' || txt === 'indisponivel' ||
                     txt === 'produto indisponível' || txt === 'esgotado' ||
                     txt === 'sem estoque') && el.offsetParent !== null) {
                    return true;
                }
            }
            // Fallback por innerText do body
            const body = document.body?.innerText?.toLowerCase() || '';
            return body.includes('avise-me quando chegar') ||
                   body.includes('avise-me quando disponível');
        }""")
        if indisponivel:
            resultado["erro"] = "produto_indisponivel"
            return resultado

        # Rotas 1-N: seletores CSS em cascata
        for i, sel in enumerate(SELETORES.get(supermercado, []), 1):
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    p = extrair_preco(el.inner_text())
                    if p and 0.5 < p < 10000:
                        resultado["preco_atual"] = p
                        resultado["rota_css"] = i
                        break
            except Exception: continue

        # Rota JSON-LD
        if not resultado["preco_atual"]:
            p = extrair_via_json_ld(page)
            if p and 0.5 < p < 10000:
                resultado["preco_atual"] = p; resultado["rota_css"] = 10

        # Rota meta tags
        if not resultado["preco_atual"]:
            p = extrair_via_meta(page)
            if p and 0.5 < p < 10000:
                resultado["preco_atual"] = p; resultado["rota_css"] = 11

        # Rota varredura JS
        if not resultado["preco_atual"]:
            p = extrair_via_js(page)
            if p and 0.5 < p < 10000:
                resultado["preco_atual"] = p; resultado["rota_css"] = 12

        # Preço original (riscado)
        for sel in [
            "span[class*='listPrice']","span[class*='ListPrice']",
            ".price__list","s span","del span",
            "span[class*='oldPrice']","[class*='originalPrice']",
            "[class*='priceFrom']","[class*='price-from']",
        ]:
            try:
                el = page.query_selector(sel)
                if el:
                    p = extrair_preco(el.inner_text())
                    if p and p > (resultado.get("preco_atual") or 0):
                        resultado["preco_original"] = p; break
            except Exception: continue

        # Filtro de sanidade — preço absurdo indica captura de pack/fardo
        PRECO_MAX = {
            "Cervejas": 20.0, "Embutidos": 60.0, "Biscoitos": 30.0,
            "Massas": 30.0, "Mercearia": 80.0, "Carnes": 400.0,
        }
        if resultado["preco_atual"]:
            cat_max = PRECO_MAX.get(categoria, 200.0)
            if resultado["preco_atual"] > cat_max:
                resultado["erro"] = f"preco_absurdo_{resultado['preco_atual']:.2f}_max_{cat_max}"
                resultado["preco_atual"] = None
                resultado["rota_css"] = None

        if resultado["preco_atual"]:
            resultado["disponivel"] = True
            if resultado["preco_original"] and resultado["preco_original"] > resultado["preco_atual"]:
                resultado["em_promocao"] = True
        else:
            if not resultado["erro"]:
                resultado["erro"] = "preco_nao_encontrado_todas_rotas"

    except PWTimeout:
        resultado["erro"] = "timeout"
    except Exception as e:
        resultado["erro"] = str(e)[:120]
    return resultado

def coletar_com_retry(page, url, supermercado, nome_produto, embalagem, con, max_tentativas=2, categoria=""):
    """Tenta coletar até max_tentativas vezes com backoff exponencial."""
    for tentativa in range(1, max_tentativas + 1):
        dados = coletar_pagina(page, url, supermercado, nome_produto, embalagem, con, tentativa, categoria)
        if dados["preco_atual"]:
            return dados
        if tentativa < max_tentativas:
            # Backoff: 3s, 8s, 20s — com jitter
            espera = (3 ** tentativa) + random.uniform(0, 2)
            print(f"  [retry {tentativa}/{max_tentativas}] {nome_produto} — aguardando {espera:.0f}s")
            time.sleep(espera)
    return dados  # retorna o último (com erro)


# ─── Loop principal ───────────────────────────────────────────────────────────
def preencher_gaps(con, hoje):
    """
    Ao final de cada coleta, preenche com o último preço disponível
    os produtos que não foram coletados hoje mas têm histórico recente.

    Melhorias vs versão anterior:
    - Remove copiados obsoletos antes de recalcular (idempotente).
    - Remove entradas de erro quando já existe preço real ou copiado no mesmo dia.
    - Nunca duplica: usa INSERT apenas se não há nenhum registro hoje ainda.
    - Busca o último preço real (rota_css != 99) para evitar copiar cópias.
    """
    inseridos = 0

    # 1. Limpeza: remove copiados de hoje para recalcular do zero (idempotência)
    con.execute("DELETE FROM precos WHERE data_coleta=? AND erro='copiado_dia_anterior'", (hoje,))

    # 2. Limpeza: remove entradas de erro quando já existe preço real hoje
    con.execute("""
        DELETE FROM precos WHERE data_coleta=?
          AND erro IN ('produto_indisponivel','preco_nao_encontrado_todas_rotas','pagina_invalida_url_desatualizada')
          AND EXISTS (
            SELECT 1 FROM precos r
            WHERE r.supermercado=precos.supermercado AND r.nome_produto=precos.nome_produto
              AND r.embalagem=precos.embalagem AND r.data_coleta=precos.data_coleta
              AND r.preco_atual IS NOT NULL AND r.erro IS NULL
          )
    """, (hoje,))
    con.commit()

    # 3. Produtos com dado real hoje (após limpeza)
    tem_hoje = set()
    for r in con.execute("""
        SELECT supermercado, nome_produto, embalagem FROM precos
        WHERE data_coleta=? AND preco_atual IS NOT NULL
          AND (erro IS NULL OR erro='input_manual')
    """, (hoje,)).fetchall():
        tem_hoje.add((r[0], r[1], r[2]))

    # 4. Candidatos: produtos com preço nos últimos 7 dias OU com erro hoje
    candidatos_rows = con.execute("""
        SELECT DISTINCT supermercado, categoria, grupo, marca, nome_produto,
               embalagem, cidade, uf, regiao, url
        FROM precos
        WHERE preco_atual IS NOT NULL
          AND data_coleta >= date(?, '-20 days') AND data_coleta < ?
        UNION
        SELECT DISTINCT supermercado, categoria, grupo, marca, nome_produto,
               embalagem, cidade, uf, regiao, url
        FROM precos
        WHERE data_coleta=?
          AND erro IN ('produto_indisponivel','preco_nao_encontrado_todas_rotas','pagina_invalida_url_desatualizada')
    """, (hoje, hoje, hoje)).fetchall()

    for p in candidatos_rows:
        sm, cat, grp, marca, nome, emb = p[0], p[1], p[2], p[3], p[4], p[5]
        cidade, uf, reg, url = p[6], p[7], p[8], p[9]

        if (sm, nome, emb) in tem_hoje:
            continue

        # Busca último preço real coletado (não cópia), para não copiar cópias
        ultimo = con.execute("""
            SELECT preco_atual, preco_original, em_promocao FROM precos
            WHERE supermercado=? AND nome_produto=? AND embalagem=?
              AND preco_atual IS NOT NULL
              AND (erro IS NULL OR erro='input_manual')
              AND rota_css != 99
            ORDER BY data_coleta DESC LIMIT 1
        """, (sm, nome, emb)).fetchone()

        if not ultimo:
            continue

        con.execute("""
            INSERT INTO precos
            (data_coleta, horario_coleta, supermercado, categoria, grupo, marca,
             nome_produto, embalagem, cidade, uf, regiao, preco_atual, preco_original,
             em_promocao, disponivel, url, erro, rota_css, tentativas)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,'copiado_dia_anterior',99,1)
        """, (hoje, "00:00:00", sm, cat, grp, marca, nome, emb,
              cidade, uf, reg, ultimo[0], ultimo[1], ultimo[2], url))
        inseridos += 1
        tem_hoje.add((sm, nome, emb))  # evita duplicar dentro do mesmo loop

    con.commit()
    print(f"  → {inseridos} preços preenchidos por cópia do dia anterior")
    return inseridos


def main(categorias_filtro=None):
    """
    categorias_filtro: lista de categorias a coletar, ex: ["Cervejas"]
                       None = coleta todas
    """
    log, total_ok, total_erro = [], 0, 0
    con = init_db()
    hoje = date.today().isoformat()
    if categorias_filtro:
        print(f"Coletando categorias: {', '.join(categorias_filtro)}")
    else:
        print("Coletando todas as categorias")

    # User-agents variados para rotacionar
    USER_AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
    ]

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-extensions",
                "--disable-infobars",
            ]
        )

        for sm_nome, cats in LINKS.items():
            print(f"\n{'='*60}\n{sm_nome}\n{'='*60}")
            headers_sm = HEADERS_SM.get(sm_nome, {})

            for cat_nome, links in cats.items():
                if categorias_filtro and cat_nome not in categorias_filtro:
                    continue
                print(f"\n  [{cat_nome}]")
                for cidade_info in CIDADES:
                    # Novo contexto por cidade + supermercado (isolamento total)
                    ua = random.choice(USER_AGENTS)
                    ctx = browser.new_context(
                        user_agent=ua,
                        viewport={"width": random.choice([1280,1366,1440,1920]),
                                  "height": random.choice([768,800,900,1080])},
                        locale="pt-BR",
                        timezone_id="America/Sao_Paulo",
                        extra_http_headers={
                            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
                            **headers_sm,
                        },
                        color_scheme="light",
                    )
                    page = ctx.new_page()
                    page.add_init_script(STEALTH_JS)
                    injetar_cep(page, sm_nome, cidade_info["cep"])

                    for produto in PRODUTOS[cat_nome]:
                        chave = f"{produto['nome']}_{produto['embalagem']}"
                        url = links.get(chave)
                        if not url: continue

                        horario = datetime.now().strftime("%H:%M:%S")
                        dados = coletar_com_retry(
                                page, url, sm_nome,
                                produto["nome"], produto["embalagem"], con,
                                categoria=cat_nome
                            )
                        reg = {
                            "data_coleta": hoje, "horario_coleta": horario,
                            "supermercado": sm_nome, "categoria": cat_nome,
                            "grupo": CAT_GRUPO.get(cat_nome, ""),
                            "marca": produto["marca"], "nome_produto": produto["nome"],
                            "embalagem": produto["embalagem"],
                            "cidade": cidade_info["cidade"], "uf": cidade_info["uf"],
                            "regiao": cidade_info["regiao"], **dados,
                        }
                        inserir(con, reg)
                        con.commit()  # commit incremental — não perde dados se cair

                        rec = " [URL recuperada]" if dados.get("url_recuperada") else ""
                        ret = f" [tentativa {dados['tentativas']}]" if dados.get("tentativas",1) > 1 else ""
                        status = f"OK(rota{dados['rota_css']}){rec}{ret}" if dados["preco_atual"] else f"ERRO:{dados['erro']}"
                        preco_s = f"R${dados['preco_atual']:.2f}" if dados["preco_atual"] else "—"
                        msg = f"{hoje}|{sm_nome}|{cat_nome}|{produto['nome']} {produto['embalagem']}|{cidade_info['cidade']}|{preco_s}|{status}"
                        log.append(msg)
                        print(f"    {msg}")
                        total_ok   += bool(dados["preco_atual"])
                        total_erro += not bool(dados["preco_atual"])

                        # Delay humanizado — mais longo nos sites com maior bloqueio
                        base_delay = 4.0 if sm_nome in ["Pão de Açúcar", "Extra"] else 1.5
                        time.sleep(random.uniform(base_delay, base_delay + 3.0))

                    ctx.close()


        browser.close()

    con.close()

    # CSV diário
    csv_path = _ROOT / f"coleta_{hoje}.csv"
    con2 = sqlite3.connect(DB_PATH); con2.row_factory = sqlite3.Row
    rows = con2.execute("SELECT * FROM precos WHERE data_coleta=?", (hoje,)).fetchall()
    if rows:
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=rows[0].keys())
            w.writeheader(); w.writerows([dict(r) for r in rows])
    con2.close()

    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"\n=== {hoje} | OK:{total_ok} ERRO:{total_erro} ===\n")
        f.write("\n".join(log[-200:]))

    print(f"\n{'='*60}")
    print(f"Finalizado: {total_ok} OK, {total_erro} erros ({total_erro/(total_ok+total_erro)*100:.1f}%)")

if __name__ == "__main__":
    import sys
    # Aceita categorias como argumentos: python scraper.py Cervejas Embutidos
    cats = sys.argv[1:] if len(sys.argv) > 1 else None
    main(cats)
