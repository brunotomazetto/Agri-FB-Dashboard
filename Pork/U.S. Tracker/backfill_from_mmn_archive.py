#!/usr/bin/env python3
"""
backfill_from_mmn_archive.py — recover historical carcass / hog prices
=====================================================================

The live extractor only ever sees *today's* PDF: USDA overwrites
www.ams.usda.gov/mnreports/*.pdf on every publication, so a week the
scheduled job missed (or committed and then threw away — see the
verify-step outage that silently blocked every commit from 2026-07-01
to 2026-09-04) was assumed lost forever.

It isn't. MyMarketNews keeps every past release:

    https://mymarketnews.ams.usda.gov/viewReport/<slug>

and its tree is driven by a plain JSON endpoint —

    /get_previous_release/<slug>?type=month&month=<M>&year=<YYYY>
    -> {"data": [{"report_date": "08/28/2026",
                  "document_url": "/filerepo/.../ams_2497_01660.pdf"}, ...]}

so the whole archive can be walked programmatically. This script does
that for the two series this tracker scrapes live:

    2497  LM_PK601  National Daily Pork FOB Omaha  -> carcass  (USD/cwt)
    2675  LM_HG217  Daily Direct Afternoon Hog     -> hog_price (USD/cwt)

PDFs are parsed with the *same* functions the live path uses
(parse_carcass_from_text / _parse_hog_price_from_text), so a backfilled
week is byte-for-byte what the daily job would have stored.

WEEKLY AGGREGATION
  Rows are keyed by the Monday of their ISO week, matching both the
  hard-coded seed's cadence and sync_corn_sbm_from_chicken(), so a
  backfilled week also picks up corn/SBM from chicken.db for free.

  carcass   = the "Five Day Average" from the LAST report of that week.
              That line *is* the week's average, so taking one report is
              correct — averaging five overlapping rolling averages
              would only smear it.
  hog_price = mean of that week's daily national weighted averages.

Nothing is overwritten: upsert_weekly only fills columns that are NULL,
so seed rows and live captures always win over a backfilled value.

USAGE
  python backfill_from_mmn_archive.py --from 2026-04 --to 2026-09
  python backfill_from_mmn_archive.py --from 2026-04 --to 2026-09 --dry-run
"""

import argparse
import io
import re
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta

import requests

import extractor_pork_us as ex

MMN_BASE = "https://mymarketnews.ams.usda.gov"
SLUG_CARCASS = 2497
SLUG_HOG     = 2675
TIMEOUT = 60
RETRY   = 3

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Accept": "application/json,text/html,*/*",
}


def _get(url: str, **kw):
    for attempt in range(RETRY):
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, **kw)
            r.raise_for_status()
            return r
        except Exception as e:
            if attempt == RETRY - 1:
                print(f"    ✗ {url[:90]}: {e}")
                return None
            time.sleep(2 ** attempt)
    return None


def list_month(slug: int, year: int, month: int) -> list[dict]:
    """Archived releases for one month: [{'date': datetime, 'url': str}, ...]."""
    r = _get(f"{MMN_BASE}/get_previous_release/{slug}",
             params={"type": "month", "month": month, "year": year})
    if not r:
        return []
    try:
        data = r.json().get("data", []) or []
    except Exception as e:
        print(f"    ✗ JSON decode {slug} {year}-{month:02d}: {e}")
        return []
    out = []
    for d in data:
        try:
            dt = datetime.strptime(d["report_date"], "%m/%d/%Y")
        except (KeyError, ValueError, TypeError):
            continue
        url = d.get("document_url") or ""
        if url:
            out.append({"date": dt, "url": url if url.startswith("http") else MMN_BASE + url})
    return sorted(out, key=lambda x: x["date"])


def pdf_text(url: str) -> str:
    import pdfplumber
    r = _get(url)
    if not r:
        return ""
    try:
        with pdfplumber.open(io.BytesIO(r.content)) as pdf:
            return "\n".join(p.extract_text() or "" for p in pdf.pages)
    except Exception as e:
        print(f"    ✗ parse {url[-28:]}: {e}")
        return ""


def monday_of(dt: datetime) -> str:
    return (dt - timedelta(days=dt.weekday())).strftime("%Y-%m-%d")


def months_between(start: str, end: str):
    y, m = (int(x) for x in start.split("-"))
    ey, em = (int(x) for x in end.split("-"))
    while (y, m) <= (ey, em):
        yield y, m
        m += 1
        if m > 12:
            m, y = 1, y + 1


def main():
    ap = argparse.ArgumentParser(description="Backfill pork_us.db from the MMN archive")
    ap.add_argument("--from", dest="start", required=True, metavar="YYYY-MM")
    ap.add_argument("--to",   dest="end",   required=True, metavar="YYYY-MM")
    ap.add_argument("--dry-run", action="store_true",
                    help="parse and report, but do not write to pork_us.db")
    args = ap.parse_args()

    print("=" * 62)
    print(f"MMN archive backfill — {args.start} → {args.end}")
    print("=" * 62)

    # ── Walk the archive ─────────────────────────────────────────────────────
    carcass_by_week = defaultdict(list)   # monday -> [(date, five_day_avg)]
    hog_by_week     = defaultdict(list)   # monday -> [daily wtd avg]

    for year, month in months_between(args.start, args.end):
        for slug, label in ((SLUG_CARCASS, "carcass"), (SLUG_HOG, "hog")):
            docs = list_month(slug, year, month)
            print(f"\n[{year}-{month:02d}] {label}: {len(docs)} arquivos no arquivo MMN")
            for d in docs:
                text = pdf_text(d["url"])
                if not text:
                    continue
                if slug == SLUG_CARCASS:
                    rows = ex.parse_carcass_from_text(text)
                    if rows:
                        carcass_by_week[monday_of(d["date"])].append(
                            (d["date"], rows[0]["value"]))
                else:
                    price = ex._parse_hog_price_from_text(text)
                    if price:
                        hog_by_week[monday_of(d["date"])].append(price)

    # ── Collapse to one row per ISO week ─────────────────────────────────────
    weeks = sorted(set(carcass_by_week) | set(hog_by_week))
    rows = []
    for wk in weeks:
        row = {"report_date": wk}
        if carcass_by_week.get(wk):
            # last report of the week — its "Five Day Average" IS the week
            last = max(carcass_by_week[wk], key=lambda x: x[0])
            row["carcass"] = round(last[1], 4)
        if hog_by_week.get(wk):
            vals = hog_by_week[wk]
            row["hog_price"] = round(sum(vals) / len(vals), 4)
        c, h = row.get("carcass"), row.get("hog_price")
        if c and h:
            row["spread_non_integrated"] = round((c / h) / 45.36, 6)
        rows.append(row)

    print("\n" + "=" * 62)
    print(f"{'semana (seg)':<14}{'carcass':>10}{'hog':>10}   (n carcass / n hog)")
    for r in rows:
        nc = len(carcass_by_week.get(r["report_date"], []))
        nh = len(hog_by_week.get(r["report_date"], []))
        c = f"{r['carcass']:.2f}" if r.get("carcass") else "—"
        h = f"{r['hog_price']:.2f}" if r.get("hog_price") else "—"
        print(f"{r['report_date']:<14}{c:>10}{h:>10}   ({nc} / {nh})")
    print(f"\n{len(rows)} semanas reconstruídas do arquivo MMN")

    if args.dry_run:
        print("\n--dry-run: nada gravado.")
        return

    # ── Write ────────────────────────────────────────────────────────────────
    conn = sqlite3.connect(ex.DB_PATH)
    ex.init_db(conn)
    before = conn.execute("SELECT COUNT(*) FROM weekly").fetchone()[0]
    ex.upsert_weekly(conn, rows, label="MMN archive — ")

    # Feed cost + spreads, same pipeline the live run uses
    ex.sync_corn_sbm_from_chicken(conn)
    ex.fill_fc_spot(conn)
    ex.compute_feed_grain_6m(conn)
    print("\n[2] Materialising quarterly table …")
    ex.materialise_quarterly(conn)

    after = conn.execute("SELECT COUNT(*) FROM weekly").fetchone()[0]
    conn.close()
    print(f"\n✓ weekly: {before} → {after} linhas")


if __name__ == "__main__":
    main()
