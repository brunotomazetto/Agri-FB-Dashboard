"""
scraper.py — Clipping scrape endpoint
======================================
Adicione este arquivo ao seu backend existente e registre a rota /api/scrape.

Recebe:  POST /api/scrape
         { "items": [ { "id": 1, "url": "https://...", "source": "Canal Rural" }, ... ] }

Retorna: { "results": [ { "id": 1, "url": "...", "body": "...", "tier": 1, "ok": true }, ... ] }

Dependências (já no requirements.txt):
  beautifulsoup4, lxml, requests, feedparser

Para integrar com Flask:
  from scraper import scrape_batch
  # registrado em app.py via @app.route("/api/scrape")
"""

# ── Imports ───────────────────────────────────────────────────────────────────
import asyncio
import re
import subprocess
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import requests
from bs4 import BeautifulSoup

# ── Configuration ─────────────────────────────────────────────────────────────

TIMEOUT_TIER1 = 12   # seconds
TIMEOUT_TIER2 = 14
TIMEOUT_TIER3 = 16
TIMEOUT_TIER4 = 30
TIMEOUT_WAYBACK = 15
MAX_WALL_TIME = 90   # hard ceiling for the whole batch
MAX_PLAYWRIGHT_CONCURRENT = 3

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "max-age=0",
}

# ── RSS sources (skip scraping entirely) ──────────────────────────────────────

RSS_FEEDS = {
    "canalrural.com.br":    "https://www.canalrural.com.br/feed/",
    "globorural.globo.com": None,  # Feedburner desatualizado — usa Wayback como fallback
}

# ── CSS selectors per source (same as frontend SOURCE_SELECTORS) ──────────────

SOURCE_SELECTORS = {
    "feedfood.com.br":           ".elementor-widget-theme-post-content .elementor-widget-container, .entry-content, .post-content",
    "canalrural.com.br":         ".article-body, .content-materia, .materia-texto, main",
    "globorural.globo.com":      ".article-body, .mb-article__body, .content-text, article",
    "valor.globo.com":           ".article-body, .mb-article__body, .content-text, article",
    "valoreconomico.com":        ".article-body, .mb-article__body, .content-text, article",
    "agfeed.com.br":             ".post-content, .entry-content, .td-post-content, .main-content",
    "bloomberglinea.com.br":     "[class*='article-body-wrapper-bl'], [class*='body-paragraph'], .left-article-section, article",
    "bloomberglinea.com":        "[class*='article-body-wrapper-bl'], [class*='body-paragraph'], .left-article-section, article",
    "theagribiz.com":            ".feed-body, .post-hat-content, .entry-content, .post-content, .container-post",
    "noticiasagricolas.com.br":  ".materia, .content.sem-video, .noticia-texto, .article-content",
    "agrolink.com.br":           ".section-description, .conteudo-noticia, .texto-noticia, .section-content",
    "neofeed.com.br":            ".first-content, .content-short, .article-master",
    "beefmagazine.com":          "[class*='ArticleBase-Body'], [class*='ArticleBody'], .article-body, .field-name-body, .entry-content",
    "moneytimes.com.br":         ".single, .mt-article__body, .article-body",
    "braziljournal.com":         ".post-content, .entry-content, .boxarticle-infos-text, article",
    "beefpoint.com.br":          ".td-post-content, .entry-content, .post-content, article .content, .single-content",
    "_default":                  ".entry-content, .post-content, .article-body, .article__body, .story-body, .content-body, #article-body, article, main",
}

NOISE_PATTERN = re.compile(
    r"newsletter|publicidade|cookie|compartilh|"
    r"leia mais|assine aqui|clique aqui|publicado em|leia também|tags:|palavras.chave|"
    r"indique a um amigo|preencha o formulário|remeter a página|"
    r"pular para o conteúdo|barra de ferramentas|sobre o wordpress",
    re.IGNORECASE,
)

# Nav-like fragments — must match the whole string
NAV_PATTERN = re.compile(
    r"^(negócios|economia|agtech|finanças|esg|vídeos|sobre nós|anuncie|"
    r"agrolinkfito|culturas|aviação|fertilizantes|carbono|biológicos|"
    r"home|quem somos|revista|aquicultura|avicultura|bovinocultura|eventos|"
    r"selecione o país|login|idioma|español|português)\s*$",
    re.IGNORECASE,
)


def _decode_html_entities(text: str) -> str:
    """Decode common HTML entities that BeautifulSoup may leave encoded."""
    import html
    return html.unescape(text)


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_hostname(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).hostname.replace("www.", "")
    except Exception:
        return ""


def get_selectors(url: str) -> str:
    host = get_hostname(url)
    for pattern, sel in SOURCE_SELECTORS.items():
        if pattern == "_default":
            continue
        if pattern in host:
            return sel
    return SOURCE_SELECTORS["_default"]


def extract_text(html: str, url: str) -> str:
    """Parse HTML and extract article body using source-specific CSS selectors."""
    if not html or len(html) < 300:
        return ""

    soup = BeautifulSoup(html, "lxml")

    # Remove noise elements
    for tag in soup.find_all(["script", "style", "nav", "header", "footer",
                               "aside", "figure", "figcaption", "noscript"]):
        tag.decompose()
    for tag in soup.find_all(class_=re.compile(
            r"ad-|banner|related|share|social|comment|newsletter|publicidade|paywall|"
            r"sidebar|menu|nav|breadcrumb|tag|categoria|author|date|meta")):
        tag.decompose()

    selectors = [s.strip() for s in get_selectors(url).split(",") if s.strip()]

    # Try CSS selectors first
    container = None
    for sel in selectors:
        try:
            container = soup.select_one(sel)
            if container:
                break
        except Exception:
            continue

    scope = container or soup.body or soup
    paras = []
    for p in scope.find_all("p"):
        text = p.get_text(separator=" ", strip=True)
        text = _decode_html_entities(text)
        text = re.sub(r"\s+", " ", text).strip()
        # Skip short, noisy, or nav-like paragraphs
        if len(text) < 60:
            continue
        if NOISE_PATTERN.search(text):
            continue
        if NAV_PATTERN.match(text):
            continue
        # Skip if it looks like a JS comment block
        if text.startswith("//") or text.startswith("/*"):
            continue
        paras.append(text)

    return "\n\n".join(paras)


# ── Tier 1: requests ──────────────────────────────────────────────────────────

def tier1_fetch(url: str) -> Optional[str]:
    """Simple requests with browser headers."""
    try:
        resp = requests.get(url, headers=BROWSER_HEADERS, timeout=TIMEOUT_TIER1,
                            allow_redirects=True)
        if resp.status_code == 200:
            return resp.text
    except Exception:
        pass
    return None


# ── Tier 2: curl with full headers ───────────────────────────────────────────

def tier2_curl(url: str) -> Optional[str]:
    """curl with browser-like headers. Bypasses basic anti-bot."""
    cmd = [
        "curl", "-sL", "--max-time", str(TIMEOUT_TIER2),
        "-A", BROWSER_HEADERS["User-Agent"],
        "-H", f"Accept: {BROWSER_HEADERS['Accept']}",
        "-H", f"Accept-Language: {BROWSER_HEADERS['Accept-Language']}",
        "-H", "Sec-Fetch-Dest: document",
        "-H", "Sec-Fetch-Mode: navigate",
        "-H", "Sec-Fetch-Site: none",
        "-H", "Sec-Fetch-User: ?1",
        "-H", "Upgrade-Insecure-Requests: 1",
        "--compressed",
        url,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_TIER2 + 2)
        if result.returncode == 0 and len(result.stdout) > 300:
            return result.stdout
    except Exception:
        pass
    return None


# ── Tier 3: curl-cffi (TLS fingerprinting — resolve Cloudflare) ──────────────
# Pure Python implementation of Chrome TLS fingerprinting.
# Resolves Cloudflare-protected sites like Canal Rural and Globo Rural.
# Install: pip install curl-cffi

def tier3_cffi(url: str) -> Optional[str]:
    """Chrome TLS fingerprinting via curl-cffi. Resolves Cloudflare."""
    try:
        from curl_cffi import requests as cffi_requests
        resp = cffi_requests.get(
            url,
            impersonate="chrome124",
            timeout=TIMEOUT_TIER3,
        )
        if resp.status_code == 200 and len(resp.text) > 300:
            return resp.text
    except ImportError:
        pass  # curl-cffi not installed
    except Exception:
        pass
    return None

# ── Tier 4: Playwright headless ───────────────────────────────────────────────
# Funciona quando rodando via Dockerfile (Chromium já instalado na imagem).

async def tier4_playwright(url: str) -> Optional[str]:
    """Headless Chromium com stealth. Resolve JS challenges e SPAs."""
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            context = await browser.new_context(
                user_agent=BROWSER_HEADERS["User-Agent"],
                locale="pt-BR",
                extra_http_headers={"Accept-Language": "pt-BR,pt;q=0.9"},
            )
            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                window.chrome = { runtime: {} };
            """)
            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT_TIER4 * 1000)
            await page.wait_for_timeout(2000)
            html = await page.content()
            await browser.close()
            return html if len(html) > 300 else None
    except Exception:
        return None


def tier4_sync(url: str) -> Optional[str]:
    """Sync wrapper para Playwright."""
    try:
        return asyncio.run(tier4_playwright(url))
    except Exception:
        return None


# ── Wayback Machine fallback ──────────────────────────────────────────────────

def wayback_fetch(url: str) -> Optional[str]:
    """Find most recent Wayback snapshot and fetch it."""
    try:
        api = f"https://archive.org/wayback/available?url={urllib.parse.quote(url)}"
        resp = requests.get(api, timeout=TIMEOUT_WAYBACK)
        data = resp.json()
        snapshot_url = data.get("archived_snapshots", {}).get("closest", {}).get("url")
        if not snapshot_url:
            return None
        resp2 = requests.get(snapshot_url, headers=BROWSER_HEADERS, timeout=TIMEOUT_WAYBACK)
        return resp2.text if resp2.status_code == 200 else None
    except Exception:
        return None


# ── RSS fetch ─────────────────────────────────────────────────────────────────

def rss_fetch(url: str, feed_url: str) -> Optional[str]:
    """Fetch article body from RSS feed by matching URL.
    Tries exact path match first, then partial slug match."""
    try:
        import feedparser
        feed = feedparser.parse(feed_url)
        if not feed.entries:
            return None

        target_path = urllib.parse.urlparse(url).path.rstrip("/")
        # Extract slug (last path segment) for fuzzy matching
        target_slug = target_path.split("/")[-1] if target_path else ""

        best_entry = None
        for entry in feed.entries:
            entry_link = entry.get("link", "")
            entry_path = urllib.parse.urlparse(entry_link).path.rstrip("/")
            # Exact path match
            if entry_path == target_path:
                best_entry = entry
                break
            # Slug match (handles trailing slash differences, www vs non-www)
            if target_slug and entry_path.endswith(target_slug):
                best_entry = entry
                break

        if not best_entry:
            return None

        # Extract content: prefer content:encoded > summary
        def _parse_content(raw_html):
            soup = BeautifulSoup(raw_html, "html.parser")
            paras = [p.get_text(separator=" ", strip=True)
                     for p in soup.find_all("p")
                     if len(p.get_text(strip=True)) > 60]
            if paras:
                return "\n\n".join(paras)
            text = re.sub(r"<[^>]+>", " ", raw_html)
            text = re.sub(r"\s+", " ", text).strip()
            return text if len(text) > 60 else None

        # feedparser maps content:encoded to entry.content[0]
        if hasattr(best_entry, "content") and best_entry.content:
            result = _parse_content(best_entry.content[0].get("value", ""))
            if result:
                return result

        if hasattr(best_entry, "summary") and best_entry.summary:
            result = _parse_content(best_entry.summary)
            if result:
                return result

    except Exception:
        pass
    return None


# ── Main scrape function ──────────────────────────────────────────────────────

def scrape_one(item: dict) -> dict:
    """
    Scrape a single article through the tier cascade.
    Returns { id, url, body, tier, ok, error }.
    """
    url = item.get("url", "")
    item_id = item.get("id")
    host = get_hostname(url)

    # RSS shortcut (Canal Rural)
    for domain, feed_url in RSS_FEEDS.items():
        if domain in host and feed_url:
            body = rss_fetch(url, feed_url)
            if body:
                return {"id": item_id, "url": url, "body": body, "tier": "rss", "ok": True}
            break  # RSS failed, continue to tiers

    # Globo Rural and Beef Point — Cloudflare protected, skip straight to cffi
    if "globorural.globo.com" in host or "beefpoint.com.br" in host:
        html = tier3_cffi(url)
        if html:
            body = extract_text(html, url)
            if body:
                return {"id": item_id, "url": url, "body": body, "tier": 3, "ok": True}
        # Wayback as last resort
        html = wayback_fetch(url)
        if html:
            body = extract_text(html, url)
            if body:
                return {"id": item_id, "url": url, "body": body, "tier": "wayback", "ok": True}
        return {"id": item_id, "url": url, "body": "", "tier": None, "ok": False,
                "error": f"{host} blocked — cffi and wayback failed"}

    # Tier 1
    html = tier1_fetch(url)
    if html:
        body = extract_text(html, url)
        if body:
            return {"id": item_id, "url": url, "body": body, "tier": 1, "ok": True}

    # Tier 2
    html = tier2_curl(url)
    if html:
        body = extract_text(html, url)
        if body:
            return {"id": item_id, "url": url, "body": body, "tier": 2, "ok": True}

    # Tier 3 — curl-cffi Chrome TLS fingerprinting (resolves Cloudflare)
    html = tier3_cffi(url)
    if html:
        body = extract_text(html, url)
        if body:
            return {"id": item_id, "url": url, "body": body, "tier": 3, "ok": True}

    # Tier 4 — Playwright (most expensive, used last)
    html = tier4_sync(url)
    if html:
        body = extract_text(html, url)
        if body:
            return {"id": item_id, "url": url, "body": body, "tier": 4, "ok": True}

    # Wayback fallback (for all other blocked sites)
    html = wayback_fetch(url)
    if html:
        body = extract_text(html, url)
        if body:
            return {"id": item_id, "url": url, "body": body, "tier": "wayback", "ok": True}

    return {"id": item_id, "url": url, "body": "", "tier": None, "ok": False,
            "error": "all tiers failed"}


def scrape_batch(items: list) -> list:
    """
    Scrape a batch of articles in parallel (capped at 4 concurrent).
    Hard wall-time ceiling: MAX_WALL_TIME seconds.
    """
    results = []
    deadline = time.time() + MAX_WALL_TIME

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(scrape_one, item): item for item in items}
        for future in as_completed(futures, timeout=MAX_WALL_TIME):
            if time.time() > deadline:
                break
            try:
                results.append(future.result(timeout=5))
            except Exception as e:
                item = futures[future]
                results.append({"id": item.get("id"), "url": item.get("url", ""),
                                 "body": "", "tier": None, "ok": False, "error": str(e)})

    return results


# ── Standalone test ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    test_items = [
        {"id": 1, "url": "https://feedfood.com.br/exportacoes-avancam-e-saldo-externo-chega-a-us-47-bi-em-junho/", "source": "Feed & Food"},
        {"id": 2, "url": "https://www.canalrural.com.br/agricultura/projeto-soja-brasil/colheita-de-soja-e-concluida-no-brasil-aponta-relatorio-da-conab/", "source": "Canal Rural"},
        {"id": 3, "url": "https://www.noticiasagricolas.com.br/noticias/milho/422913-conab-indica-colheita-do-milho-em-6-7-e-produtividades-acima-do-esperado-no-mato-grosso.html", "source": "Notícias Agrícolas"},
        {"id": 4, "url": "https://www.agrolink.com.br/noticias/especie-e-cepa-definem-eficiencia-dos-bioinsumos_515669.html", "source": "AgroLink"},
        {"id": 5, "url": "https://neofeed.com.br/negocios/yum-brands-fatia-pizza-hut-em-venda-de-us-27-bilhoes/", "source": "NeoFeed"},
        {"id": 6, "url": "https://www.moneytimes.com.br/brasil-ve-alivio-de-custos-com-adubos-e-diesel-apos-acordo-entre-eua-e-ira-sobre-guerra-diz-ministro-pads/", "source": "Money Times"},
        {"id": 7, "url": "https://braziljournal.com/no-biometano-plano-bilionario-da-gasmig-atrai-bp-mitsui-gestoras-e-a-jf/", "source": "Brazil Journal"},
        {"id": 8, "url": "https://www.bloomberglinea.com.br/agro/com-jbs-como-socia-mantiqueira-preve-10-mi-de-galinhas-nos-eua-e-mira-top-5-global/", "source": "Bloomberg Línea"},
        {"id": 9, "url": "https://www.beefmagazine.com/market-news/consumers-really-like-beef-and-theyre-willing-to-pay-for-it", "source": "Beef Magazine"},
    ]
    print("Testing scraper...\n")
    results = scrape_batch(test_items)
    for r in sorted(results, key=lambda x: x["id"]):
        status = f"✅ tier={r['tier']}" if r["ok"] else f"❌ {r.get('error','')}"
        preview = r["body"][:120].replace("\n", " ") if r["body"] else ""
        print(f"[{r['id']:2d}] {status}")
        print(f"     {preview}\n")
