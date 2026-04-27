#!/usr/bin/env python3
"""
extractor_beef.py — U.S. Beef Packer Margin Tracker
All data fetched from USDA AMS PDFs — no API keys required.

PDF sources (always current week):
  CT150  (5-Area live steer/heifer): https://www.ams.usda.gov/mnreports/ams_2477.pdf
  Cutout (Choice / Select boxed beef): https://www.ams.usda.gov/mnreports/ams_2461.pdf
  Kansas weekly price:                 https://www.ams.usda.gov/mnreports/ams_2484.pdf
  Nebraska weekly price:               https://www.ams.usda.gov/mnreports/ams_2485.pdf

Usage:
  python extractor_beef.py                  # default: update latest week from PDFs
  python extractor_beef.py --history FILE   # one-time load from V4 Excel
  python extractor_beef.py --full           # force full quarterly recompute
"""

import os, re, io, sqlite3, argparse, datetime, logging
import requests
import pdfplumber
import pandas as pd

logging.basicConfig(level=logging.INFO, format="  %(message)s")
log = logging.getLogger(__name__)

# ── Paths & URLs ──────────────────────────────────────────────────────────────
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "beef.db")

PDF_URLS = {
    "ct150":    "https://www.ams.usda.gov/mnreports/ams_2477.pdf",
    "cutout":   "https://www.ams.usda.gov/mnreports/ams_2461.pdf",
    "kansas":   "https://www.ams.usda.gov/mnreports/ams_2484.pdf",
    "nebraska": "https://www.ams.usda.gov/mnreports/ams_2485.pdf",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/pdf,*/*",
}


# ══ DATABASE ══════════════════════════════════════════════════════════════════

def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Migrate old fat schema → slim schema if needed."""
    weekly_cols = {r[1] for r in conn.execute("PRAGMA table_info(beef_weekly)")}
    quarterly_cols = {r[1] for r in conn.execute("PRAGMA table_info(beef_quarterly)")}

    need_weekly = bool(weekly_cols - {"week_ending", "choice", "select_", "ct150_steer", "ct150_all", "ks_avg", "ne_avg"})
    need_quarterly = bool(quarterly_cols - {"quarter", "quarter_start", "choice", "select_", "ct150_steer", "ct150_all", "ks_avg", "ne_avg", "mbrf_gm", "jbs_gm"})

    if need_weekly:
        log.info("Migrating beef_weekly to slim schema …")
        conn.executescript("""
        CREATE TABLE beef_weekly_slim (
            week_ending  TEXT PRIMARY KEY,
            choice       REAL,
            select_      REAL,
            ct150_steer  REAL,
            ct150_all    REAL,
            ks_avg       REAL,
            ne_avg       REAL
        );
        INSERT INTO beef_weekly_slim (week_ending, choice, select_, ct150_steer, ct150_all, ks_avg, ne_avg)
        SELECT SUBSTR(week_ending,1,10), choice, select_, ct150_steer, ct150_all, ks_avg, ne_avg
        FROM beef_weekly;
        DROP TABLE beef_weekly;
        ALTER TABLE beef_weekly_slim RENAME TO beef_weekly;
        """)
        conn.commit()
        log.info("✓ beef_weekly migrated")

    if need_quarterly:
        log.info("Migrating beef_quarterly to slim schema …")
        conn.executescript("""
        CREATE TABLE beef_quarterly_slim (
            quarter       TEXT PRIMARY KEY,
            quarter_start TEXT,
            choice        REAL,
            select_       REAL,
            ct150_steer   REAL,
            ct150_all     REAL,
            ks_avg        REAL,
            ne_avg        REAL,
            mbrf_gm       REAL,
            jbs_gm        REAL
        );
        INSERT INTO beef_quarterly_slim (quarter, quarter_start, choice, select_, ct150_steer, ct150_all, ks_avg, ne_avg, mbrf_gm, jbs_gm)
        SELECT quarter, quarter_start, choice, select_, ct150_steer, ct150_all, ks_avg, ne_avg, mbrf_gm, jbs_gm
        FROM beef_quarterly;
        DROP TABLE beef_quarterly;
        ALTER TABLE beef_quarterly_slim RENAME TO beef_quarterly;
        """)
        conn.commit()
        log.info("✓ beef_quarterly migrated")


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS beef_weekly (
        week_ending  TEXT PRIMARY KEY,
        choice       REAL,
        select_      REAL,
        ct150_steer  REAL,
        ct150_all    REAL,
        ks_avg       REAL,
        ne_avg       REAL
    );
    CREATE TABLE IF NOT EXISTS beef_quarterly (
        quarter       TEXT PRIMARY KEY,
        quarter_start TEXT,
        choice        REAL,
        select_       REAL,
        ct150_steer   REAL,
        ct150_all     REAL,
        ks_avg        REAL,
        ne_avg        REAL,
        mbrf_gm       REAL,
        jbs_gm        REAL
    );
    """)
    conn.commit()
    _migrate_schema(conn)


def upsert_weekly(conn: sqlite3.Connection, rows: list[dict]) -> int:
    sql = """
    INSERT INTO beef_weekly (week_ending, choice, select_, ct150_steer, ct150_all, ks_avg, ne_avg)
    VALUES (:week_ending, :choice, :select_, :ct150_steer, :ct150_all, :ks_avg, :ne_avg)
    ON CONFLICT(week_ending) DO UPDATE SET
        choice      = COALESCE(excluded.choice,      choice),
        select_     = COALESCE(excluded.select_,     select_),
        ct150_steer = COALESCE(excluded.ct150_steer, ct150_steer),
        ct150_all   = COALESCE(excluded.ct150_all,   ct150_all),
        ks_avg      = COALESCE(excluded.ks_avg,      ks_avg),
        ne_avg      = COALESCE(excluded.ne_avg,      ne_avg)
    """
    conn.executemany(sql, rows)
    conn.commit()
    return len(rows)


# ══ PDF HELPERS ═══════════════════════════════════════════════════════════════

def fetch_pdf_text(key: str) -> str:
    """Download a PDF and return all pages concatenated as plain text."""
    url = PDF_URLS[key]
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    with pdfplumber.open(io.BytesIO(r.content)) as pdf:
        return "\n".join(p.extract_text() or "" for p in pdf.pages)


def _parse_date(text: str) -> str:
    """
    Extract the week-ending date from a USDA PDF header.
    Returns ISO date string 'YYYY-MM-DD', or last Saturday as fallback.
    """
    patterns = [
        r"[Ww]eek\s+[Ee]nding\s+\w+,?\s+(\d{1,2}/\d{1,2}/\d{4})",
        r"[Ww]eek\s+[Ee]nding\s+(\w+ \d{1,2},? \d{4})",
        r"\b(\d{1,2}/\d{1,2}/\d{4})\b",
        r"\b([A-Za-z]+ \d{1,2},?\s+\d{4})\b",
    ]
    for pat in patterns:
        m = re.search(pat, text[:800])
        if m:
            raw = m.group(1).strip().rstrip(",")
            for fmt in ("%m/%d/%Y", "%B %d %Y", "%B %d, %Y", "%b %d %Y", "%b %d, %Y"):
                try:
                    return datetime.datetime.strptime(raw, fmt).date().isoformat()
                except ValueError:
                    continue
    today = datetime.date.today()
    sat = today - datetime.timedelta(days=(today.weekday() + 2) % 7)
    log.warning("Could not parse date from PDF header; using %s", sat)
    return sat.isoformat()


def _num(s: str) -> float | None:
    """Parse a number string like '$245.82' or '22,550' → float."""
    try:
        return float(re.sub(r"[$,]", "", s.strip()))
    except (ValueError, AttributeError):
        return None


# ══ CT150 — ams_2477.pdf (LM_CT150) ══════════════════════════════════════════
# WEEKLY WEIGHTED AVERAGES section:
#   Live FOB Steer    22,550   1,571   244.96
#   Live FOB Heifer   14,926   1,413   245.02
# ct150_steer = steer price; ct150_all = (steer + heifer) / 2

def fetch_ct150() -> dict:
    text = fetch_pdf_text("ct150")
    week = _parse_date(text)
    result = {"week_ending": week, "ct150_steer": None, "ct150_all": None}

    m_steer = re.search(
        r"WEEKLY\s+WEIGHTED\s+AVERAGES.*?Live\s+FOB\s+Steer\s+[\d,]+\s+[\d,]+\s+([\d.]+)",
        text, re.IGNORECASE | re.DOTALL
    )
    m_heifer = re.search(
        r"WEEKLY\s+WEIGHTED\s+AVERAGES.*?Live\s+FOB\s+Heifer\s+[\d,]+\s+[\d,]+\s+([\d.]+)",
        text, re.IGNORECASE | re.DOTALL
    )
    steer  = _num(m_steer.group(1))  if m_steer  else None
    heifer = _num(m_heifer.group(1)) if m_heifer else None

    result["ct150_steer"] = steer
    if steer and heifer:
        result["ct150_all"] = round((steer + heifer) / 2, 4)
    elif steer:
        result["ct150_all"] = steer

    if result["ct150_steer"] is not None:
        log.info("CT150: steer=$%.2f, all=$%.2f  (week %s)",
                 result["ct150_steer"], result["ct150_all"], week)
    else:
        log.warning("CT150: could not parse price from PDF (ams_2477)")

    return result


# ══ CUTOUT — ams_2461.pdf (LM_XB459) ═════════════════════════════════════════
# Weekly Cutout Value Summary → Weekly Average row:
#   Weekly Average                              392.28    390.09
# choice = Choice 600-900; select_ = Select 600-900

def fetch_cutout() -> dict:
    text = fetch_pdf_text("cutout")
    week = _parse_date(text)
    result = {"week_ending": week, "choice": None, "select_": None}

    # Cutout values are always 3-digit (300–500 range)
    m = re.search(
        r"Weekly\s+Average\s+[\d.\s-]*(\d{3}\.\d{2})\s+(\d{3}\.\d{2})",
        text, re.IGNORECASE
    )
    if m:
        result["choice"]  = _num(m.group(1))
        result["select_"] = _num(m.group(2))

    if result["choice"] is None:
        m = re.search(
            r"Weekly\s+Average[^\n]*(\d{3}\.\d{2})[^\n]*(\d{3}\.\d{2})",
            text, re.IGNORECASE
        )
        if m:
            result["choice"]  = _num(m.group(1))
            result["select_"] = _num(m.group(2))

    if result["choice"] is None:
        block = re.search(r"Weekly\s+Average(.{0,200})", text, re.IGNORECASE | re.DOTALL)
        if block:
            nums = re.findall(r"\b(\d{3}\.\d{2})\b", block.group(1))
            if len(nums) >= 2:
                result["choice"]  = float(nums[0])
                result["select_"] = float(nums[1])

    if result["choice"] is not None:
        log.info("Cutout: Choice=%.2f, Select=%.2f  (week %s)",
                 result["choice"], result["select_"] or 0, week)
    else:
        log.warning("Cutout: could not parse Choice/Select from PDF (LM_XB459)")

    return result


# ══ KANSAS / NEBRASKA — ams_2484.pdf / ams_2485.pdf ══════════════════════════
# WEEKLY ACCUMULATED section:
#   Live Steer   X,XXX   X,XXX.XX   $XXX.XX
#   Live Heifer  X,XXX   X,XXX.XX   $XXX.XX
# ks_avg / ne_avg = (steer + heifer) / 2

def _parse_weekly_accumulated(text: str, label: str) -> tuple[float | None, str]:
    week = _parse_date(text)
    m_s = re.search(
        r"WEEKLY\s+ACCUMULATED.*?Live\s+Steer\s+[\d,]+\s+[\d,.]+\s+\$?([\d.]+)",
        text, re.IGNORECASE | re.DOTALL
    )
    m_h = re.search(
        r"WEEKLY\s+ACCUMULATED.*?Live\s+Heifer\s+[\d,]+\s+[\d,.]+\s+\$?([\d.]+)",
        text, re.IGNORECASE | re.DOTALL
    )
    steer  = _num(m_s.group(1)) if m_s else None
    heifer = _num(m_h.group(1)) if m_h else None

    if steer and heifer:
        return round((steer + heifer) / 2, 4), week
    if steer:
        log.info("%s: heifer not found, using steer only", label)
        return steer, week
    if heifer:
        log.info("%s: steer not found, using heifer only", label)
        return heifer, week

    m = re.search(r"WEEKLY\s+ACCUMULATED.*?\$\s*([\d.]+)", text, re.IGNORECASE | re.DOTALL)
    if m:
        log.info("%s: fallback to first price after WEEKLY ACCUMULATED", label)
        return _num(m.group(1)), week

    log.warning("%s: WEEKLY ACCUMULATED block not parsed", label)
    return None, week


def fetch_kansas() -> dict:
    price, week = _parse_weekly_accumulated(fetch_pdf_text("kansas"), "KS")
    if price:
        log.info("KS: avg=$%.2f  (week %s)", price, week)
    return {"week_ending": week, "ks_avg": price}


def fetch_nebraska() -> dict:
    price, week = _parse_weekly_accumulated(fetch_pdf_text("nebraska"), "NE")
    if price:
        log.info("NE: avg=$%.2f  (week %s)", price, week)
    return {"week_ending": week, "ne_avg": price}


# ══ MERGE WEEKLY DATA ═════════════════════════════════════════════════════════

def build_weekly_rows(ct150: dict, cutout: dict, ks: dict, ne: dict) -> list[dict]:
    """
    Merge data from the four PDFs into one weekly row.
    Cutout date is used as the canonical week_ending (published Fridays).
    Any PDF whose date diverges >7 days from canonical is treated as stale → NULL.
    """
    week = cutout.get("week_ending") or max(
        (d for d in [ct150.get("week_ending"), ks.get("week_ending"), ne.get("week_ending")] if d),
        default=datetime.date.today().isoformat()
    )

    def _fresh(src_week: str | None, label: str) -> bool:
        if not src_week:
            return False
        try:
            delta = abs((datetime.date.fromisoformat(src_week[:10]) -
                         datetime.date.fromisoformat(week[:10])).days)
            if delta <= 7:
                return True
            log.warning("%s PDF date %s diverges from canonical %s by >7 days — skipping",
                        label, src_week[:10], week[:10])
            return False
        except Exception:
            return False

    return [{
        "week_ending": week,
        "choice":      cutout.get("choice"),
        "select_":     cutout.get("select_"),
        "ct150_steer": ct150.get("ct150_steer") if _fresh(ct150.get("week_ending"), "CT150") else None,
        "ct150_all":   ct150.get("ct150_all")   if _fresh(ct150.get("week_ending"), "CT150") else None,
        "ks_avg":      ks.get("ks_avg")         if _fresh(ks.get("week_ending"),    "KS")    else None,
        "ne_avg":      ne.get("ne_avg")         if _fresh(ne.get("week_ending"),    "NE")    else None,
    }]


# ══ QUARTERLY RECOMPUTE ═══════════════════════════════════════════════════════

def quarter_label(d: datetime.date) -> str:
    return f"{((d.month - 1) // 3) + 1}Q{str(d.year)[-2:]}"

def quarter_start(d: datetime.date) -> datetime.date:
    m = ((d.month - 1) // 3) * 3 + 1
    return datetime.date(d.year, m, 1)


def recompute_quarterly(conn: sqlite3.Connection, full: bool = False) -> None:
    df = pd.read_sql("SELECT * FROM beef_weekly ORDER BY week_ending", conn)
    if df.empty:
        log.warning("beef_weekly is empty; skipping quarterly recompute")
        return

    df["week_ending"]   = pd.to_datetime(df["week_ending"], format="mixed").dt.date
    df["quarter"]       = df["week_ending"].apply(quarter_label)
    df["quarter_start"] = df["week_ending"].apply(lambda d: quarter_start(d).isoformat())

    agg = df.groupby(["quarter", "quarter_start"]).agg(
        choice      =("choice",      "mean"),
        select_     =("select_",     "mean"),
        ct150_steer =("ct150_steer", "mean"),
        ct150_all   =("ct150_all",   "mean"),
        ks_avg      =("ks_avg",      "mean"),
        ne_avg      =("ne_avg",      "mean"),
    ).reset_index()

    # Preserve manually-entered mbrf_gm / jbs_gm
    existing = pd.read_sql("SELECT quarter, mbrf_gm, jbs_gm FROM beef_quarterly", conn)
    agg = agg.merge(existing, on="quarter", how="left")

    sql = """
    INSERT INTO beef_quarterly
        (quarter, quarter_start, choice, select_, ct150_steer, ct150_all, ks_avg, ne_avg, mbrf_gm, jbs_gm)
    VALUES
        (:quarter, :quarter_start, :choice, :select_, :ct150_steer, :ct150_all, :ks_avg, :ne_avg, :mbrf_gm, :jbs_gm)
    ON CONFLICT(quarter) DO UPDATE SET
        quarter_start = excluded.quarter_start,
        choice        = COALESCE(excluded.choice,      choice),
        select_       = COALESCE(excluded.select_,     select_),
        ct150_steer   = COALESCE(excluded.ct150_steer, ct150_steer),
        ct150_all     = COALESCE(excluded.ct150_all,   ct150_all),
        ks_avg        = COALESCE(excluded.ks_avg,      ks_avg),
        ne_avg        = COALESCE(excluded.ne_avg,      ne_avg),
        mbrf_gm       = COALESCE(mbrf_gm, excluded.mbrf_gm),
        jbs_gm        = COALESCE(jbs_gm,  excluded.jbs_gm)
    """
    conn.executemany(sql, agg.where(pd.notnull(agg), None).to_dict("records"))
    conn.commit()
    log.info("✓ beef_quarterly: %d quarters recomputed", len(agg))


# ══ HISTORICAL LOAD ═══════════════════════════════════════════════════════════

def load_history(conn: sqlite3.Connection, xlsx_path: str) -> None:
    """One-time load from V4 Excel workbook."""
    log.info("Loading history from %s …", xlsx_path)
    xl = pd.ExcelFile(xlsx_path)
    sheet = next((s for s in xl.sheet_names if any(k in s.lower() for k in ["week","raw","data"])),
                 xl.sheet_names[0])
    df = xl.parse(sheet)
    df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]

    rename = {
        "week_end": "week_ending", "week": "week_ending", "date": "week_ending",
        "choice_cutout": "choice", "choice_$/cwt": "choice",
        "select_cutout": "select_", "select_$/cwt": "select_",
        "ct150": "ct150_steer", "ct150_steer_avg": "ct150_steer",
        "ks": "ks_avg", "ks_price": "ks_avg",
        "ne": "ne_avg", "ne_price": "ne_avg",
    }
    df.rename(columns={k: v for k, v in rename.items() if k in df.columns}, inplace=True)

    if "week_ending" not in df.columns:
        raise ValueError(f"week_ending column not found. Available: {list(df.columns)}")

    df["week_ending"] = pd.to_datetime(df["week_ending"], format="mixed").dt.date.astype(str)

    for col in ["choice", "select_", "ct150_steer", "ct150_all", "ks_avg", "ne_avg"]:
        if col not in df.columns:
            df[col] = None

    rows = df[["week_ending", "choice", "select_", "ct150_steer", "ct150_all", "ks_avg", "ne_avg"]]\
             .where(pd.notnull(df), None).to_dict("records")
    n = upsert_weekly(conn, rows)
    log.info("✓ beef_weekly: %d rows loaded from Excel", n)


# ══ WEEKLY UPDATE ═════════════════════════════════════════════════════════════

def update_weekly(conn: sqlite3.Connection) -> None:
    log.info("=== Fetching weekly data from USDA PDFs ===")

    ct150_data  = {"week_ending": None, "ct150_steer": None, "ct150_all": None}
    cutout_data = {"week_ending": None, "choice": None, "select_": None}
    ks_data     = {"week_ending": None, "ks_avg": None}
    ne_data     = {"week_ending": None, "ne_avg": None}

    for label, fetch_fn, store in [
        ("CT150",   fetch_ct150,    lambda d: ct150_data.update(d)),
        ("Cutout",  fetch_cutout,   lambda d: cutout_data.update(d)),
        ("Kansas",  fetch_kansas,   lambda d: ks_data.update(d)),
        ("Nebraska",fetch_nebraska, lambda d: ne_data.update(d)),
    ]:
        try:
            store(fetch_fn())
        except Exception as e:
            log.error("%s fetch failed: %s", label, e)

    rows = build_weekly_rows(ct150_data, cutout_data, ks_data, ne_data)
    upsert_weekly(conn, rows)
    log.info("✓ beef_weekly: upserted week_ending=%s", rows[0]["week_ending"])


# ══ MAIN ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="U.S. Beef Packer Margin Extractor")
    parser.add_argument("--history", metavar="XLSX", help="One-time load from V4 Excel workbook")
    parser.add_argument("--full", action="store_true", help="Force full quarterly recompute")
    parser.add_argument("--db", default=DB_PATH, help="Path to beef.db")
    args = parser.parse_args()

    if args.db != DB_PATH:
        DB_PATH = args.db

    log.info("Database: %s", DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    if args.history:
        load_history(conn, args.history)
    else:
        update_weekly(conn)

    log.info("=== Recomputing quarterly averages ===")
    recompute_quarterly(conn, full=args.full)

    conn.close()
    log.info("Done.")
