"""
scrape_worker.py — GitHub Actions scrape worker
================================================
Roda dentro do GitHub Actions quando disparado pelo /api/scrape-trigger.

Recebe os itens via variável de ambiente SCRAPE_ITEMS (JSON),
executa o scraping completo com todos os tiers,
e salva o resultado no Postgres via /api/scrape-result (POST).

Variáveis de ambiente necessárias (GitHub Actions secrets):
  SCRAPE_ITEMS     — JSON com lista de itens a scraper
  SCRAPE_JOB_ID    — ID único do job para correlação
  BACKEND_URL      — URL do backend Render (ex: https://ibba-agri-fb-feed-news.onrender.com)
  BACKEND_SECRET   — Secret compartilhado para autenticar o callback
"""

import json
import os
import sys
import re
import time
import urllib.parse
import urllib.request
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

# ── Install dependencies ──────────────────────────────────────────────────────
def _pip(pkg):
    subprocess.check_call([sys.executable, "-m", "pip", "install", pkg, "-q"])

try:
    import requests
except ImportError:
    _pip("requests"); import requests

try:
    from bs4 import BeautifulSoup
except ImportError:
    _pip("beautifulsoup4"); from bs4 import BeautifulSoup

PARSER = "html.parser"  # html.parser is built-in, no install needed

try:
    import feedparser
except ImportError:
    _pip("feedparser"); import feedparser

try:
    from curl_cffi import requests as cffi_requests
    HAS_CFFI = True
except ImportError:
    _pip("curl-cffi"); 
    try:
        from curl_cffi import requests as cffi_requests
        HAS_CFFI = True
    except:
        HAS_CFFI = False

# ── Config ────────────────────────────────────────────────────────────────────

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "max-age=0",
}

TIMEOUT = 20
MAX_WORKERS = 4
MAX_WALL_TIME = 120

RSS_FEEDS = {
    # Only Canal Rural benefits from RSS — Cloudflare blocks Tier 1 for it
    # Other WordPress sites truncate RSS summaries, so we prefer Tier 1 for them
    "canalrural.com.br": "https://www.canalrural.com.br/feed/",
}

SOURCE_SELECTORS = {
    "feedfood.com.br":          ".elementor-widget-theme-post-content .elementor-widget-container, .entry-content, .post-content",
    "canalrural.com.br":        ".article-body, .content-materia, .materia-texto, main",
    "globorural.globo.com":     "[class*='article-body'], [class*='article__body'], .mb-article__body, .mc-body, .glb-text, [data-type='text'], .content-text, article",
    "g1.globo.com":             "[class*='article-body'], [class*='article__body'], .mb-article__body, .glb-text, [data-type='text'], .content-text, article",
    "globo.com":                "[class*='article-body'], [class*='article__body'], .mb-article__body, .content-text, article",
    "agfeed.com.br":            ".post-content, .entry-content, .td-post-content, .main-content",
    "bloomberglinea.com.br":    "[class*='article-body-wrapper-bl'], [class*='body-paragraph'], .left-article-section, article",
    "bloomberglinea.com":       "[class*='article-body-wrapper-bl'], [class*='body-paragraph'], .left-article-section, article",
    "theagribiz.com":           "body",
    "noticiasagricolas.com.br": ".materia, .content.sem-video, .noticia-texto, .article-content",
    "agrolink.com.br":          ".section-description, .conteudo-noticia, .texto-noticia, .section-content",
    "neofeed.com.br":           ".first-content, .content-short, .article-master",
    "beefmagazine.com":         "[class*='ArticleBase-Body'], [class*='ArticleBody'], .article-body, .field-name-body, .entry-content",
    "moneytimes.com.br":        ".single, .mt-article__body, .article-body",
    "braziljournal.com":        ".post-content, .entry-content, .boxarticle-infos-text, article",
    "beefpoint.com.br":         ".td-post-content, .entry-content, .post-content, article",
    "_default":                 ".entry-content, .post-content, .article-body, .article__body, .story-body, .content-body, #article-body, article, main",
}

NOISE = re.compile(
    r"newsletter|publicidade|cookie|compartilh|leia mais|assine aqui|"
    r"clique aqui|publicado em|leia também|tags:|palavras.chave|"
    r"pular para o conteúdo|barra de ferramentas|sobre o wordpress|"
    r"indique a um amigo|preencha o formulário",
    re.IGNORECASE,
)

# Short image caption patterns — "Foto: X", "Divulgação", "Crédito:", etc.
CAPTION = re.compile(
    r"^(foto[:\s]|imagem[:\s]|divulgação|crédito[:\s]|reprodução|legenda[:\s]|"
    r"ilustração|arte[:\s]|reuters|getty|afp|ap photo)",
    re.IGNORECASE,
)

NAV = re.compile(
    r"^(negócios|economia|agtech|finanças|esg|vídeos|sobre nós|anuncie|"
    r"home|quem somos|revista|aquicultura|avicultura|bovinocultura|"
    r"selecione o país|login|idioma|español|português)\s*$",
    re.IGNORECASE,
)

def resolve_url(url: str) -> str:
    """Resolve Google News redirect URLs to the real source URL."""
    if "news.google.com" not in url:
        return url
    print(f"  resolving google news: {url[:60]}")

    # Google News uses a JS redirect. The real URL must be fetched via their
    # internal batchexecute API. Extract the article ID and request the redirect.
    try:
        m = re.search(r'/articles/([A-Za-z0-9_\-]+)', url)
        if not m:
            return url
        article_id = m.group(1)

        # Fetch the article page to get the data-n-au attribute or signature
        r = requests.get(url, headers=BROWSER_HEADERS, timeout=10)
        html = r.text

        # Look for the redirect URL in the page (data-n-au or c-wiz)
        au = re.search(r'data-n-au="(https?://[^"]+)"', html)
        if au:
            real = au.group(1)
            if "google" not in real:
                print(f"  resolved via data-n-au: {real[:70]}")
                return real

        # Try to find any external URL in the page
        ext = re.findall(r'"(https?://(?![^"]*google)[^"]+)"', html)
        for u in ext:
            if any(d in u for d in ["globo.com", "canalrural", "agfeed", "noticiasagricolas",
                                     "agrolink", "moneytimes", "braziljournal", "beefpoint",
                                     "neofeed", "bloomberglinea", "theagribiz", "feedfood"]):
                print(f"  resolved via page scan: {u[:70]}")
                return u
    except Exception as e:
        print(f"  resolve error: {e}")

    print(f"  could not resolve")
    return url


def extract_globo(html_str, url):
    """Extract article body from Globo/G1 pages.
    Globo CMS uses <p class="content-text__container"> for article paragraphs."""
    import html as html_mod
    soup = BeautifulSoup(html_str, PARSER)

    # Globo's article paragraphs have class "content-text__container"
    paras = []
    for p in soup.find_all("p", class_=re.compile(r"content-text")):
        t = html_mod.unescape(p.get_text(" ", strip=True))
        t = re.sub(r"\s+", " ", t).strip()
        if len(t) > 40 and not NOISE.search(t) and not CAPTION.match(t):
            paras.append(t)

    if paras:
        return "\n\n".join(paras)

    # Fallback: try mc-article-body / mc-column containers
    for sel in [".mc-article-body", ".mc-column", "[itemprop='articleBody']", ".wall-body"]:
        container = soup.select_one(sel)
        if container:
            for p in container.find_all("p"):
                t = html_mod.unescape(p.get_text(" ", strip=True))
                t = re.sub(r"\s+", " ", t).strip()
                if len(t) > 40 and not NOISE.search(t) and not CAPTION.match(t):
                    paras.append(t)
            if paras:
                return "\n\n".join(paras)

    # Diagnostic: log what classes exist
    classes = set()
    for el in soup.find_all(class_=True):
        for c in (el.get("class") or []):
            if any(k in c.lower() for k in ["content", "article", "body", "text", "materia", "corpo"]):
                classes.add(c)
    print(f"  globo classes found: {sorted(classes)[:15]}")

    return ""


def hostname(url):
    try:
        return urllib.parse.urlparse(url).hostname.replace("www.", "")
    except:
        return ""

def get_sel(url):
    host = hostname(url)
    for k, v in SOURCE_SELECTORS.items():
        if k != "_default" and k in host:
            return v
    return SOURCE_SELECTORS["_default"]

def extract(html_str, url):
    import html as html_mod
    if not html_str or len(html_str) < 300:
        return ""
    soup = BeautifulSoup(html_str, PARSER)
    for t in soup.find_all(["script","style","nav","header","footer","aside","noscript","figure","figcaption"]):
        t.decompose()
    for t in soup.find_all(class_=re.compile(r"ad-|banner|related|share|social|comment|newsletter|publicidade|paywall|sidebar|menu|breadcrumb|author")):
        t.decompose()

    sels = [s.strip() for s in get_sel(url).split(",") if s.strip()]
    container = None
    for s in sels:
        try:
            el = soup.select_one(s)
            if el:
                container = el
                break
        except:
            pass

    scope = container or soup.body or soup
    paras = []
    for p in scope.find_all("p"):
        t = p.get_text(" ", strip=True)
        # Double-decode HTML entities (e.g. &amp;#8230; → … )
        t = html_mod.unescape(html_mod.unescape(t))
        t = re.sub(r"\s+", " ", t).strip()
        if len(t) < 60 or NOISE.search(t) or NAV.match(t) or CAPTION.match(t) or t.startswith("//") or t.startswith("/*"):
            continue
        # Skip RSS trailer lines like "O post X apareceu primeiro em Y"
        if re.search(r"apareceu primeiro em|posted first on|the post .+ appeared first", t, re.I):
            continue
        paras.append(t)
    return "\n\n".join(paras)

# ── Tiers ─────────────────────────────────────────────────────────────────────

def tier1(url):
    try:
        r = requests.get(url, headers=BROWSER_HEADERS, timeout=TIMEOUT, allow_redirects=True)
        if r.status_code == 200 and len(r.content) > 300:
            return r.text
    except:
        pass
    return None

def tier2(url):
    cmd = ["curl", "-sL", "--max-time", str(TIMEOUT),
           "-A", BROWSER_HEADERS["User-Agent"],
           "-H", f"Accept: {BROWSER_HEADERS['Accept']}",
           "-H", f"Accept-Language: {BROWSER_HEADERS['Accept-Language']}",
           "-H", "Sec-Fetch-Dest: document",
           "-H", "Sec-Fetch-Mode: navigate",
           "-H", "Sec-Fetch-Site: none",
           "-H", "Sec-Fetch-User: ?1",
           "-H", "Upgrade-Insecure-Requests: 1",
           "--compressed", url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT+2)
        if r.returncode == 0 and len(r.stdout) > 300:
            return r.stdout
    except:
        pass
    return None

def tier3(url):
    if not HAS_CFFI:
        return None
    try:
        r = cffi_requests.get(url, impersonate="chrome124", timeout=TIMEOUT)
        if r.status_code == 200 and len(r.content) > 300:
            try:
                return r.content.decode("utf-8")
            except:
                return r.content.decode("latin-1")
    except:
        pass
    return None

def wayback(url):
    try:
        api = f"https://archive.org/wayback/available?url={urllib.parse.quote(url)}"
        r = requests.get(api, timeout=15)
        snap = r.json().get("archived_snapshots", {}).get("closest", {}).get("url")
        if snap:
            r2 = requests.get(snap, headers=BROWSER_HEADERS, timeout=15)
            if r2.status_code == 200:
                return r2.text
    except:
        pass
    return None

def rss_fetch(url, feed_url):
    try:
        r = requests.get(feed_url, headers=BROWSER_HEADERS, timeout=12)
        content = r.content
        # Prepend XML encoding declaration to force UTF-8 — prevents double-encoding
        if not content.startswith(b'<?xml'):
            content = b'<?xml version="1.0" encoding="UTF-8"?>\n' + content
        feed = feedparser.parse(content)

        if not feed.entries:
            feed = feedparser.parse(feed_url)

        if not feed or not feed.entries:
            return None

        target_path = urllib.parse.urlparse(url).path.rstrip("/")
        target_slug = target_path.split("/")[-1] if target_path else ""

        best = None
        for entry in feed.entries:
            ep = urllib.parse.urlparse(entry.get("link","")).path.rstrip("/")
            if ep == target_path or (target_slug and ep.endswith(target_slug)):
                best = entry
                break

        if not best:
            return None

        import html as html_mod
        def parse_html(raw):
            soup = BeautifulSoup(raw, PARSER)
            paras = []
            for p in soup.find_all("p"):
                t = p.get_text(" ", strip=True)
                t = html_mod.unescape(html_mod.unescape(t))  # double-decode
                t = re.sub(r"\s+", " ", t).strip()
                if len(t) > 60 and not NOISE.search(t):
                    if not re.search(r"apareceu primeiro em|posted first on", t, re.I):
                        paras.append(t)
            return "\n\n".join(paras) if paras else None

        if hasattr(best, "content") and best.content:
            result = parse_html(best.content[0].get("value", ""))
            if result:
                return result
        if hasattr(best, "summary") and best.summary:
            return parse_html(best.summary)
    except:
        pass
    return None

# ── Main scrape ───────────────────────────────────────────────────────────────

def scrape_one(item):
    url = item.get("url", "")
    item_id = item.get("id")

    # Resolve Google News redirect URLs to the real source URL
    url = resolve_url(url)
    host = hostname(url)
    print(f"  scrape_one: id={item_id} host={host} url={url[:80]}")

    # If still Google News after resolve, skip — can't extract
    if "news.google.com" in host:
        print(f"  ✗ unresolved google news url")
        return {"id": item_id, "url": url, "body": "", "tier": None, "ok": False, "error": "unresolved google news url"}

    # Canal Rural — Cloudflare protected. Try cffi (tier3) first, then RSS
    if "canalrural.com.br" in host:
        html = tier3(url)
        if html:
            body = extract(html, url)
            if body:
                print(f"  ✓ tier3-cffi {url[:60]}")
                return {"id": item_id, "url": url, "body": body, "tier": 3, "ok": True}
        for domain, feed_url in RSS_FEEDS.items():
            if domain in host and feed_url:
                body = rss_fetch(url, feed_url)
                if body:
                    print(f"  ✓ rss {url[:60]}")
                    return {"id": item_id, "url": url, "body": body, "tier": "rss", "ok": True}
                break
        print(f"  canal rural: cffi and rss both failed")

    # G1/Globo — try to extract from __NEXT_DATA__ JSON first
    if "globo.com" in host or "g1.globo.com" in host:
        html = tier1(url) or tier3(url)
        if html:
            body = extract_globo(html, url)
            if body:
                print(f"  ✓ globo-json {url[:70]}")
                return {"id": item_id, "url": url, "body": body, "tier": 1, "ok": True}

    # Tier cascade
    for tier_fn, tier_name in [(tier1, 1), (tier2, 2), (tier3, 3)]:
        html = tier_fn(url)
        if html:
            body = extract(html, url)
            print(f"  tier{tier_name} html={len(html)}b body={len(body)}chars")
            if body:
                print(f"  ✓ tier{tier_name} {url[:70]}")
                return {"id": item_id, "url": url, "body": body, "tier": tier_name, "ok": True}
        else:
            print(f"  tier{tier_name} no html")

    # Wayback fallback
    html = wayback(url)
    if html:
        body = extract(html, url)
        if body:
            print(f"  ✓ wayback {url[:70]}")
            return {"id": item_id, "url": url, "body": body, "tier": "wayback", "ok": True}

    print(f"  ✗ failed  {url[:70]}")
    return {"id": item_id, "url": url, "body": "", "tier": None, "ok": False, "error": "all tiers failed"}

def scrape_batch(items):
    results = []
    deadline = time.time() + MAX_WALL_TIME
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(scrape_one, item): item for item in items}
        for future in as_completed(futures, timeout=MAX_WALL_TIME):
            if time.time() > deadline:
                break
            try:
                results.append(future.result(timeout=90))
            except Exception as e:
                item = futures[future]
                print(f"  ✗ exception: {e} url={item.get('url','')[:60]}")
                results.append({"id": item.get("id"), "url": item.get("url",""),
                                 "body": "", "tier": None, "ok": False, "error": str(e)})
    return results

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    job_id      = os.environ.get("SCRAPE_JOB_ID", "")
    items_json  = os.environ.get("SCRAPE_ITEMS", "[]")
    backend_url = os.environ.get("BACKEND_URL", "")
    secret      = os.environ.get("BACKEND_SECRET", "")

    if not job_id or not backend_url:
        print("ERROR: SCRAPE_JOB_ID and BACKEND_URL are required")
        sys.exit(1)

    print(f"[scrape_worker] VERSION=2024-FINAL job={job_id}")
    items = json.loads(items_json)
    print(f"[scrape_worker] {len(items)} items to scrape")
    for item in items:
        print(f"[scrape_worker]   → id={item.get('id')} url={item.get('url','')[:80]}")

    results = scrape_batch(items)
    ok_count = sum(1 for r in results if r.get("ok"))
    print(f"[scrape_worker] done: {ok_count}/{len(results)} ok")

    # Post results back to Render backend
    payload = json.dumps({
        "job_id": job_id,
        "secret": secret,
        "results": results,
    }).encode("utf-8")

    callback_url = backend_url.rstrip("/") + "/api/scrape-result"
    req = urllib.request.Request(
        callback_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            print(f"[scrape_worker] callback: {resp.status}")
    except Exception as e:
        print(f"[scrape_worker] callback failed: {e}")
        sys.exit(1)

    print("[scrape_worker] complete")
