"""
update_commodities.py

Single script for the commodity price database: Aluminum (LME), Wheat and
Corn (CBOT), and Barley (NCDEX).

Two modes:

1) Daily update (default, used by GitHub Actions):
       python update_commodities.py
   Fetches the latest value for all four series and upserts one row per
   commodity into the DB.

2) One-time historical backfill (only needed if the .db is ever lost,
   corrupted, or rebuilt from scratch):
       python update_commodities.py --backfill path/to/Cost_Model.xlsx
   Loads historical series from the "BBG - Yearly" sheet up to the last
   date where values actually change (later rows are carry-forward
   template placeholders and are skipped). Weekend rows are also skipped
   for all series, to stay consistent with the daily job, which never
   writes them either (none of the sources trade on weekends).

Sources & why:
-------------
Aluminum spot   -> Westmetall (westmetall.com). Publishes daily LME
                    Aluminium Cash-Settlement, sourced from the LME.
                    (The LME's own site blocks automated requests with a
                    403, so it can't be used directly.)

Aluminum future -> TradingView's public forward-curve data API
                    (scanner.tradingview.com/futures/scan). Requests the
                    full LME Aluminium High Grade ("LME:AH") forward curve
                    and picks the contract expiring ~12 months ahead of
                    the spot date, using standard futures month codes
                    (F,G,H,J,K,M,N,Q,U,V,X,Z for Jan..Dec). E.g. any day in
                    August 2026 -> contract AHQ2027. Matches Bloomberg's
                    LA13 Comdty (generic 13th-month forward) used in the
                    historical backfill.

Wheat / Corn    -> Yahoo Finance, via yfinance (tickers ZW=F and ZC=F,
                    CBOT wheat/corn futures, USD cents/bushel). Matches
                    Bloomberg's W 1 Comdty / C 1 Comdty exactly on
                    cross-checked dates.

USD/BRL         -> Banco Central do Brasil, official PTAX rate
                    (olinda.bcb.gov.br/olinda/servico/PTAX). Free, official,
                    government API, no auth needed.

INR/BRL         -> Yahoo Finance, ticker INRBRL=X. BCB's PTAX API only
                    covers 10 currencies (not INR), so this is the direct
                    cross rate instead of a synthetic USD/INR + USD/BRL
                    computation.

Barley          -> TradingView's public single-symbol quote API
                    (scanner.tradingview.com/symbol?symbol=...), symbol
                    NCDEX:BARLEYJPR1! (NCDEX Barley futures, INR/quintal).
                    Matches Bloomberg's FU1 Comdty exactly on cross-checked
                    dates. (investing.com has the same data but blocks
                    automated/datacenter requests with a 403 -- confirmed
                    by direct testing -- so it can't be used from GitHub
                    Actions.)
"""

import argparse
import json
import re
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from dateutil.relativedelta import relativedelta

DB_PATH = Path(__file__).parent / "commodities_prices.db"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

MONTH_CODES = {
    1: "F", 2: "G", 3: "H", 4: "J", 5: "K", 6: "M",
    7: "N", 8: "Q", 9: "U", 10: "V", 11: "X", 12: "Z",
}

# --- Excel backfill layout (sheet "BBG - Yearly") ---
SHEET_NAME = "BBG - Yearly"
DATE_COL = 4          # column D
USDBRL_COL = 5         # column E  (USDBRL Curncy)
ALU_FUTURE_COL = 9    # column I  (LA13 Comdty)
ALU_SPOT_COL = 10     # column J  (LOAHDY LME Comdty)
BARLEY_COL = 12        # column L  (FU1 Comdty, raw INR)
WHEAT_COL = 13         # column M  (W 1 Comdty)
CORN_COL = 15          # column O  (C 1 Comdty, Chicago)
INRBRL_COL = 16        # column P  (INRBRL Curncy)
FIRST_DATA_ROW = 4


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS aluminum_lme (
            date                 TEXT PRIMARY KEY,
            spot                 REAL,
            spot_source          TEXT,
            future                REAL,
            future_contract       TEXT,
            future_target_month   TEXT,
            future_source         TEXT,
            updated_at              TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS wheat_cbot (
            date       TEXT PRIMARY KEY,
            close      REAL,
            open       REAL,
            high       REAL,
            low        REAL,
            volume     REAL,
            source     TEXT,
            updated_at   TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS corn_cbot (
            date       TEXT PRIMARY KEY,
            close      REAL,
            open       REAL,
            high       REAL,
            low        REAL,
            volume     REAL,
            source     TEXT,
            updated_at   TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS barley_ncdex (
            date       TEXT PRIMARY KEY,
            close      REAL,
            currency   TEXT DEFAULT 'INR',
            source     TEXT,
            updated_at   TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS fx_usdbrl (
            date       TEXT PRIMARY KEY,
            rate       REAL,
            source     TEXT,
            updated_at   TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS fx_inrbrl (
            date       TEXT PRIMARY KEY,
            rate       REAL,
            source     TEXT,
            updated_at   TEXT DEFAULT (datetime('now'))
        );
        """
    )
    conn.commit()


def is_weekday(d: date) -> bool:
    return d.weekday() < 5  # Mon-Fri


# ----------------------------------------------------------------------
# Daily fetchers
# ----------------------------------------------------------------------

def get_westmetall_spot() -> tuple[str, float]:
    """Returns (date_str YYYY-MM-DD, cash_settlement_price) for the most
    recent trading day published on westmetall.com."""
    year = datetime.now(timezone.utc).year
    url = f"https://www.westmetall.com/en/markdaten.php?action=table&field=LME_Al_cash&year={year}"
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")
    data_rows = []
    for tr in soup.find_all("tr"):
        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(cells) >= 3 and re.match(r"^\d{2}\.\s*\w+\s*\d{4}$", cells[0]):
            data_rows.append(cells)

    if not data_rows:
        raise RuntimeError("Westmetall: could not parse any data rows from the page")

    most_recent = data_rows[0]  # table lists most recent date first
    parsed_date = datetime.strptime(most_recent[0], "%d. %B %Y").date()
    cash_price = float(most_recent[1].replace(",", ""))
    return parsed_date.strftime("%Y-%m-%d"), cash_price


def get_tradingview_aluminum_future(spot_date: date) -> tuple[str, float]:
    """Returns (contract_symbol, close_price) for the LME Aluminium High
    Grade contract expiring ~12 months after spot_date."""
    target = spot_date + relativedelta(months=12)
    code = MONTH_CODES[target.month]
    symbol = f"AH{code}{target.year}"
    full_symbol = f"LME:{symbol}"

    url = "https://scanner.tradingview.com/futures/scan?label-product=futures-forward-curve"
    payload = {
        "columns": ["pricescale", "minmov", "minmove2", "fractional", "expiration", "close", "name", "currency"],
        "filter": [{"left": "close", "operation": "nempty"}, {"left": "expiration", "operation": "nempty"}],
        "ignore_unknown_fields": False,
        "sort": {"sortBy": "expiration", "sortOrder": "asc"},
        "markets": ["futures"],
        "index_filters": [{"name": "root", "values": ["LME:AH"]}],
    }
    headers = {
        "content-type": "text/plain;charset=UTF-8",
        "accept": "application/json",
        "user-agent": USER_AGENT,
        "referer": "https://www.tradingview.com/",
    }
    r = requests.post(url, data=json.dumps(payload), headers=headers, timeout=30)
    r.raise_for_status()
    data = r.json()

    for row in data.get("data", []):
        if row["s"] == full_symbol:
            close = row["d"][5]  # index of 'close' per the columns list above
            return symbol, float(close)

    raise RuntimeError(f"TradingView: contract {full_symbol} not found in forward curve response")


def get_yfinance_ohlc(ticker: str) -> tuple[str, dict]:
    """Returns (date_str, {close, open, high, low, volume}) for the most
    recent trading day of the given Yahoo Finance ticker."""
    import yfinance as yf  # imported here so the module is only required for this path

    hist = yf.Ticker(ticker).history(period="5d")
    if hist.empty:
        raise RuntimeError(f"yfinance: no data returned for {ticker}")

    last = hist.iloc[-1]
    last_date = hist.index[-1].strftime("%Y-%m-%d")
    return last_date, {
        "close": float(last["Close"]),
        "open": float(last["Open"]),
        "high": float(last["High"]),
        "low": float(last["Low"]),
        "volume": float(last["Volume"]),
    }


def get_bcb_usdbrl(max_days_back: int = 7) -> tuple[str, float]:
    """Returns (date_str, ptax_venda_rate) for the most recent business
    day's official USD/BRL PTAX rate from the Central Bank of Brazil.
    Tries today, then walks backward day by day (weekends/holidays have
    no quote) up to max_days_back."""
    d = datetime.now(timezone.utc).date()
    for _ in range(max_days_back):
        date_param = d.strftime("%m-%d-%Y")
        url = (
            "https://olinda.bcb.gov.br/olinda/servico/PTAX/versao/v1/odata/"
            f"CotacaoDolarDia(dataCotacao='{date_param}')?$format=json"
        )
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        data = r.json()
        values = data.get("value", [])
        if values:
            return d.strftime("%Y-%m-%d"), float(values[0]["cotacaoVenda"])
        d -= relativedelta(days=1)
    raise RuntimeError(f"BCB PTAX: no USD/BRL quote found in the last {max_days_back} days")


def get_yfinance_inrbrl() -> tuple[str, float]:
    """Returns (date_str, close) for the most recent trading day of the
    INR/BRL cross rate."""
    import yfinance as yf

    hist = yf.Ticker("INRBRL=X").history(period="5d")
    if hist.empty:
        raise RuntimeError("yfinance: no data returned for INRBRL=X")
    last = hist.iloc[-1]
    last_date = hist.index[-1].strftime("%Y-%m-%d")
    return last_date, float(last["Close"])


def get_tradingview_barley() -> tuple[str, float]:
    """Returns (date_str today UTC, close_price) for NCDEX Barley futures.
    NCDEX:BARLEYJPR1! is the front-month rolling contract; TradingView's
    single-symbol quote endpoint doesn't expose the trade date directly,
    so we timestamp it with today's UTC date (the endpoint always
    reflects the latest available session)."""
    url = "https://scanner.tradingview.com/symbol"
    params = {
        "symbol": "NCDEX:BARLEYJPR1!",
        "fields": "close,open,high,low,currency,volume",
        "no_404": "true",
    }
    headers = {
        "accept": "application/json",
        "user-agent": USER_AGENT,
        "referer": "https://www.tradingview.com/",
    }
    r = requests.get(url, params=params, headers=headers, timeout=30)
    r.raise_for_status()
    data = r.json()
    if not data or "close" not in data:
        raise RuntimeError(f"TradingView: no barley data returned ({data!r})")

    today = datetime.now(timezone.utc).date().strftime("%Y-%m-%d")
    return today, float(data["close"])


# ----------------------------------------------------------------------
# Upserts
# ----------------------------------------------------------------------

def upsert_aluminum(conn, trade_date, spot, spot_source, future, future_contract, future_source):
    target_month = None
    if future_contract or future is not None:
        d = datetime.strptime(trade_date, "%Y-%m-%d").date()
        target_month = (d + relativedelta(months=12)).strftime("%Y-%m")
    conn.execute(
        """
        INSERT INTO aluminum_lme
            (date, spot, spot_source, future, future_contract, future_target_month, future_source)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET
            spot = COALESCE(excluded.spot, aluminum_lme.spot),
            spot_source = COALESCE(excluded.spot_source, aluminum_lme.spot_source),
            future = COALESCE(excluded.future, aluminum_lme.future),
            future_contract = COALESCE(excluded.future_contract, aluminum_lme.future_contract),
            future_target_month = COALESCE(excluded.future_target_month, aluminum_lme.future_target_month),
            future_source = COALESCE(excluded.future_source, aluminum_lme.future_source)
        """,
        (trade_date, spot, spot_source, future, future_contract, target_month, future_source),
    )


def upsert_ohlc(conn, table, trade_date, ohlc, source):
    conn.execute(
        f"""
        INSERT INTO {table} (date, close, open, high, low, volume, source)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET
            close=excluded.close, open=excluded.open, high=excluded.high,
            low=excluded.low, volume=excluded.volume, source=excluded.source
        """,
        (trade_date, ohlc["close"], ohlc["open"], ohlc["high"], ohlc["low"], ohlc["volume"], source),
    )


def upsert_fx(conn, table, trade_date, rate, source):
    conn.execute(
        f"""
        INSERT INTO {table} (date, rate, source) VALUES (?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET rate=excluded.rate, source=excluded.source
        """,
        (trade_date, rate, source),
    )


def upsert_barley(conn, trade_date, close, source):
    conn.execute(
        """
        INSERT INTO barley_ncdex (date, close, currency, source)
        VALUES (?, ?, 'INR', ?)
        ON CONFLICT(date) DO UPDATE SET
            close=excluded.close, currency=excluded.currency, source=excluded.source
        """,
        (trade_date, close, source),
    )


# ----------------------------------------------------------------------
# Daily update
# ----------------------------------------------------------------------

def run_daily_update() -> int:
    conn = sqlite3.connect(DB_PATH)
    create_schema(conn)
    had_error = False

    # --- Aluminum ---
    spot_date, spot_price, future_contract, future_price = None, None, None, None
    try:
        spot_date, spot_price = get_westmetall_spot()
        print(f"[Aluminum] Spot OK: {spot_date} -> {spot_price} (westmetall)")
    except Exception as e:
        had_error = True
        print(f"[Aluminum] Spot FAILED: {e}", file=sys.stderr)

    reference_date = (
        datetime.strptime(spot_date, "%Y-%m-%d").date() if spot_date else date.today()
    )
    try:
        future_contract, future_price = get_tradingview_aluminum_future(reference_date)
        print(f"[Aluminum] Future OK: {future_contract} -> {future_price} (tradingview)")
    except Exception as e:
        had_error = True
        print(f"[Aluminum] Future FAILED: {e}", file=sys.stderr)

    if spot_date or future_contract:
        row_date = spot_date or reference_date.strftime("%Y-%m-%d")
        upsert_aluminum(
            conn, row_date,
            spot_price, "westmetall" if spot_price is not None else None,
            future_price, future_contract, "tradingview" if future_price is not None else None,
        )
        conn.commit()
        print(f"[Aluminum] Saved row for {row_date}")

    # --- Wheat ---
    try:
        d, ohlc = get_yfinance_ohlc("ZW=F")
        upsert_ohlc(conn, "wheat_cbot", d, ohlc, "yahoo/ZW=F")
        conn.commit()
        print(f"[Wheat] OK: {d} -> {ohlc['close']} (yahoo/ZW=F)")
    except Exception as e:
        had_error = True
        print(f"[Wheat] FAILED: {e}", file=sys.stderr)

    # --- Corn ---
    try:
        d, ohlc = get_yfinance_ohlc("ZC=F")
        upsert_ohlc(conn, "corn_cbot", d, ohlc, "yahoo/ZC=F")
        conn.commit()
        print(f"[Corn] OK: {d} -> {ohlc['close']} (yahoo/ZC=F)")
    except Exception as e:
        had_error = True
        print(f"[Corn] FAILED: {e}", file=sys.stderr)

    # --- Barley ---
    try:
        d, close = get_tradingview_barley()
        upsert_barley(conn, d, close, "tradingview/NCDEX:BARLEYJPR1!")
        conn.commit()
        print(f"[Barley] OK: {d} -> {close} (tradingview/NCDEX:BARLEYJPR1!)")
    except Exception as e:
        had_error = True
        print(f"[Barley] FAILED: {e}", file=sys.stderr)

    # --- USD/BRL ---
    try:
        d, rate = get_bcb_usdbrl()
        upsert_fx(conn, "fx_usdbrl", d, rate, "bcb/ptax")
        conn.commit()
        print(f"[USDBRL] OK: {d} -> {rate} (bcb/ptax)")
    except Exception as e:
        had_error = True
        print(f"[USDBRL] FAILED: {e}", file=sys.stderr)

    # --- INR/BRL ---
    try:
        d, rate = get_yfinance_inrbrl()
        upsert_fx(conn, "fx_inrbrl", d, rate, "yahoo/INRBRL=X")
        conn.commit()
        print(f"[INRBRL] OK: {d} -> {rate} (yahoo/INRBRL=X)")
    except Exception as e:
        had_error = True
        print(f"[INRBRL] FAILED: {e}", file=sys.stderr)

    conn.close()
    return 1 if had_error else 0


# ----------------------------------------------------------------------
# One-time historical backfill
# ----------------------------------------------------------------------

def find_last_real_data_row(ws, first_row: int, cols: list[int]) -> int:
    """Find the last row where EVERY given column still shows real,
    changing data -- i.e. the most conservative cutoff across all series.
    Different series can freeze on different days (e.g. one market closes
    for the day before another), so we take the minimum (earliest) cutoff
    among all columns to avoid including a partially-stale row where some
    series already have fresh data but others are still a carry-forward
    duplicate of the previous day."""
    last_row = ws.max_row
    for r in range(last_row, first_row, -1):
        d = ws.cell(row=r, column=DATE_COL).value
        if d is not None:
            last_row = r
            break

    cutoffs = []
    for c in cols:
        prev = None
        col_cutoff = last_row
        for r in range(last_row, first_row, -1):
            v = ws.cell(row=r, column=c).value
            if prev is not None and v != prev:
                col_cutoff = r + 1
                break
            prev = v
        cutoffs.append(col_cutoff)
    return min(cutoffs)


def run_backfill(xlsx_path: str, end_date: date | None = None) -> int:
    import openpyxl  # only needed for this mode

    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    ws = wb[SHEET_NAME]

    cols = [ALU_FUTURE_COL, ALU_SPOT_COL, BARLEY_COL, WHEAT_COL, CORN_COL, USDBRL_COL, INRBRL_COL]
    last_real_row = find_last_real_data_row(ws, FIRST_DATA_ROW, cols)
    cutoff_msg = ""
    if end_date is not None:
        # Walk back further if a hard end_date cap is given.
        while (ws.cell(row=last_real_row, column=DATE_COL).value.date() > end_date
               and last_real_row > FIRST_DATA_ROW):
            last_real_row -= 1
        cutoff_msg = f" (capped at requested end_date={end_date})"
    print(f"Loading rows {FIRST_DATA_ROW}..{last_real_row}{cutoff_msg} "
          f"({ws.cell(row=FIRST_DATA_ROW, column=DATE_COL).value.date()} "
          f"to {ws.cell(row=last_real_row, column=DATE_COL).value.date()})")

    conn = sqlite3.connect(DB_PATH)
    create_schema(conn)

    counts = {"aluminum": 0, "wheat": 0, "corn": 0, "barley": 0, "usdbrl": 0, "inrbrl": 0}
    skipped_weekends = 0

    for r in range(FIRST_DATA_ROW, last_real_row + 1):
        d = ws.cell(row=r, column=DATE_COL).value
        if d is None:
            continue
        d = d.date() if hasattr(d, "date") else d
        if not is_weekday(d):
            skipped_weekends += 1
            continue
        date_str = d.strftime("%Y-%m-%d")

        alu_future = ws.cell(row=r, column=ALU_FUTURE_COL).value
        alu_spot = ws.cell(row=r, column=ALU_SPOT_COL).value
        if isinstance(alu_future, (int, float)) and isinstance(alu_spot, (int, float)):
            target_month = (d + relativedelta(months=12)).strftime("%Y-%m")
            conn.execute(
                """INSERT INTO aluminum_lme
                    (date, spot, spot_source, future, future_contract, future_target_month, future_source)
                   VALUES (?, ?, 'bloomberg_backfill', ?, NULL, ?, 'bloomberg_backfill')
                   ON CONFLICT(date) DO UPDATE SET
                       spot=excluded.spot, spot_source=excluded.spot_source,
                       future=excluded.future, future_target_month=excluded.future_target_month,
                       future_source=excluded.future_source""",
                (date_str, float(alu_spot), float(alu_future), target_month),
            )
            counts["aluminum"] += 1

        wheat = ws.cell(row=r, column=WHEAT_COL).value
        if isinstance(wheat, (int, float)):
            conn.execute(
                """INSERT INTO wheat_cbot (date, close, source) VALUES (?, ?, 'bloomberg_backfill')
                   ON CONFLICT(date) DO UPDATE SET close=excluded.close, source=excluded.source""",
                (date_str, float(wheat)),
            )
            counts["wheat"] += 1

        corn = ws.cell(row=r, column=CORN_COL).value
        if isinstance(corn, (int, float)):
            conn.execute(
                """INSERT INTO corn_cbot (date, close, source) VALUES (?, ?, 'bloomberg_backfill')
                   ON CONFLICT(date) DO UPDATE SET close=excluded.close, source=excluded.source""",
                (date_str, float(corn)),
            )
            counts["corn"] += 1

        barley = ws.cell(row=r, column=BARLEY_COL).value
        if isinstance(barley, (int, float)):
            conn.execute(
                """INSERT INTO barley_ncdex (date, close, currency, source)
                   VALUES (?, ?, 'INR', 'bloomberg_backfill')
                   ON CONFLICT(date) DO UPDATE SET close=excluded.close, source=excluded.source""",
                (date_str, float(barley)),
            )
            counts["barley"] += 1

        usdbrl = ws.cell(row=r, column=USDBRL_COL).value
        if isinstance(usdbrl, (int, float)):
            conn.execute(
                """INSERT INTO fx_usdbrl (date, rate, source) VALUES (?, ?, 'bloomberg_backfill')
                   ON CONFLICT(date) DO UPDATE SET rate=excluded.rate, source=excluded.source""",
                (date_str, float(usdbrl)),
            )
            counts["usdbrl"] += 1

        inrbrl = ws.cell(row=r, column=INRBRL_COL).value
        if isinstance(inrbrl, (int, float)):
            conn.execute(
                """INSERT INTO fx_inrbrl (date, rate, source) VALUES (?, ?, 'bloomberg_backfill')
                   ON CONFLICT(date) DO UPDATE SET rate=excluded.rate, source=excluded.source""",
                (date_str, float(inrbrl)),
            )
            counts["inrbrl"] += 1

    conn.commit()
    print(f"Backfilled (skipped {skipped_weekends} weekend rows): {counts}")
    conn.close()
    return 0


# ----------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--backfill", metavar="XLSX_PATH",
        help="One-time: load historical data from the given Excel file instead of running the daily update.",
    )
    parser.add_argument(
        "--end-date", metavar="YYYY-MM-DD", default=None,
        help="Optional hard cap on the last date to backfill (only used with --backfill).",
    )
    args = parser.parse_args()

    if args.backfill:
        end_date = datetime.strptime(args.end_date, "%Y-%m-%d").date() if args.end_date else None
        return run_backfill(args.backfill, end_date=end_date)
    return run_daily_update()


if __name__ == "__main__":
    sys.exit(main())
