import feedparser
import time
import ssl
from datetime import datetime, timedelta

_BRT = timedelta(hours=3)  # BRT = UTC-3 (Brazil abolished DST in 2019)
from urllib.parse import quote_plus

# Workaround for SSL certificate issues on Windows only
import platform
if platform.system() == "Windows":
    try:
        ssl._create_default_https_context = ssl._create_unverified_context
    except Exception:
        pass

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) NewsReader/1.0"}


class NewsFetcher:
    GOOGLE_PT = "https://news.google.com/rss/search?q={query}&hl=pt-BR&gl=BR&ceid=BR:pt"
    GOOGLE_EN = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"

    def __init__(self, config):
        self.config = config
        self.groups = config["groups"]

    def _build_url(self, terms: list, lang: str) -> str:
        # Default: pass each term as-is so Google News interprets multi-word
        # queries as AND (or as OR if the user writes "OR" explicitly). User
        # opts into exact-phrase match by wrapping the term in double quotes in
        # config.json (e.g. '"preço do frango"').
        # Rationale: the previous behaviour auto-quoted any multi-word term,
        # turning every config query into an exact-phrase search — which almost
        # never matches real article titles. Fix: hand over control to the user.
        query = " OR ".join(terms)
        encoded = quote_plus(query)
        if lang == "en":
            return self.GOOGLE_EN.format(query=encoded)
        return self.GOOGLE_PT.format(query=encoded)

    def _parse_date(self, entry) -> str:
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            try:
                # published_parsed is always UTC; convert to BRT (UTC-3)
                return (datetime(*entry.published_parsed[:6]) - _BRT).strftime("%Y-%m-%dT%H:%M:%S")
            except Exception:
                pass
        # Render server runs UTC; convert to BRT for consistency
        return (datetime.utcnow() - _BRT).strftime("%Y-%m-%dT%H:%M:%S")

    def _parse_source(self, entry) -> str:
        if hasattr(entry, "source") and entry.source:
            return getattr(entry.source, "title", "") or ""
        return ""

    def _fetch_feed(self, url: str, group_id: int) -> list:
        items = []
        try:
            import requests as _req
            resp = _req.get(url, headers=HEADERS, timeout=12, allow_redirects=True)
            if not resp.ok:
                return []
            feed = feedparser.parse(resp.content)
            for entry in feed.entries[:40]:
                title = getattr(entry, "title", "").strip()
                url_item = getattr(entry, "link", "").strip()
                if not title or not url_item:
                    continue
                summary = ""
                if hasattr(entry, "summary") and entry.summary:
                    # Strip HTML tags from summary
                    import re
                    summary = re.sub(r"<[^>]+>", "", entry.summary)[:600]
                items.append({
                    "title": title,
                    "url": url_item,
                    "source": self._parse_source(entry),
                    "published_at": self._parse_date(entry),
                    "summary": summary,
                    "group_id": group_id,
                })
        except Exception as e:
            print(f"    [WARN] Error fetching {url[:60]}: {e}")
        return items

    def _fetch_group(self, group: dict) -> list:
        items = []
        seen = set()
        gid = group["id"]

        pt_queries = group.get("search_queries_pt", [])
        en_queries = group.get("search_queries_en", [])

        # Fallback: build from keywords/companies if no explicit queries defined
        if not pt_queries:
            all_terms = group.get("companies", []) + group.get("keywords", [])
            pt_queries = [" OR ".join(all_terms[:6])] if all_terms else []

        def add_items(raw_items):
            for item in raw_items:
                if item["url"] not in seen:
                    seen.add(item["url"])
                    items.append(item)

        # Batch queries: send up to 3 terms per request to avoid URL length issues
        for query in pt_queries:
            url = self._build_url([query], "pt")
            add_items(self._fetch_feed(url, gid))
            time.sleep(0.4)

        for query in en_queries:
            url = self._build_url([query], "en")
            add_items(self._fetch_feed(url, gid))
            time.sleep(0.4)

        return items

    def _fetch_direct_feed(self, url: str, group_id: int, source_name: str,
                           topical_filter: list = None) -> list:
        items = []
        filter_kws = [kw.lower() for kw in topical_filter] if topical_filter else []
        try:
            # Fetch with explicit timeout to avoid hanging on slow/broken feeds
            import requests as _req
            resp = _req.get(url, headers=HEADERS, timeout=12, allow_redirects=True)
            if not resp.ok:
                print(f"    [WARN] Feed returned {resp.status_code}: {url[:60]}")
                return []
            feed = feedparser.parse(resp.content)
            for entry in feed.entries[:30]:
                title = getattr(entry, "title", "").strip()
                url_item = getattr(entry, "link", "").strip()
                if not title or not url_item:
                    continue
                # Resolve Google News URLs to real source URL
                url_item = _resolve_google_news_url(url_item)
                summary = ""
                if hasattr(entry, "summary") and entry.summary:
                    import re
                    summary = re.sub(r"<[^>]+>", "", entry.summary)[:600]
                # Topical filter: discard if none of the required keywords appear
                if filter_kws:
                    text = (title + " " + summary).lower()
                    if not any(kw in text for kw in filter_kws):
                        continue
                items.append({
                    "title": title,
                    "url": url_item,
                    "source": source_name or self._parse_source(entry),
                    "published_at": self._parse_date(entry),
                    "summary": summary,
                    "group_id": group_id,
                })
        except Exception as e:
            print(f"    [WARN] Error fetching direct feed {url[:60]}: {e}")
        return items

    def fetch_all(self) -> list:
        all_items = []
        for group in self.groups:
            print(f"  > {group['name']}...")
            items = self._fetch_group(group)
            all_items.extend(items)
            print(f"    {len(items)} itens encontrados")

        direct_feeds = self.config.get("direct_feeds", [])
        if direct_feeds:
            existing_urls = {item["url"] for item in all_items}
            direct_items = []
            seen = set()
            for feed_cfg in direct_feeds:
                url = feed_cfg.get("url", "")
                if not url:
                    continue
                print(f"  > Feed direto: {feed_cfg.get('source_name', url)}...")
                raw = self._fetch_direct_feed(url, feed_cfg.get("group_id", 1), feed_cfg.get("source_name", ""), feed_cfg.get("topical_filter"))
                new = [i for i in raw if i["url"] not in existing_urls and i["url"] not in seen]
                seen.update(i["url"] for i in new)
                direct_items.extend(new)
                time.sleep(0.3)
            all_items.extend(direct_items)
            print(f"    {len(direct_items)} itens de feeds diretos")

        return all_items
