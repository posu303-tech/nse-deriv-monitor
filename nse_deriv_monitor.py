#!/usr/bin/env python3
"""
NSE Derivatives Monitor - on-demand monitoring workflow
=======================================================

Three layers (institutional framing):
  L1 POSITIONING    : participant-wise OI (FII/DII/Client/Pro), FII/DII cash flows
  L2 VOLATILITY     : ATM IV, IV rank vs history, India VIX rank, term structure, skew
  L3 MICROSTRUCTURE : option chain PCR, OI walls (max-OI strikes), max pain, basis

Data sources (public, no API keys):
  - archives.nseindia.com  participant-wise OI CSVs (fao_participant_oi_DDMMYYYY.csv)
  - archives.nseindia.com  F&O UDiFF bhavcopy (option chain EOD: OI, settlement)
  - www.nseindia.com/api   option-chain-v3 (live chain when not throttled)
  - www.nseindia.com/api   fiidiiTradeReact (FII/DII cash flows)
  - Yahoo Finance          India VIX history (IV-rank baseline), NIFTY daily closes

Notes on robustness:
  - NSE's live APIs throttle aggressively (HTTP 200 with empty payload). The tool
    detects that, backs off, and falls back to the bhavcopy (archives domain),
    which carries the full EOD option chain: OI, settlement prices, volumes.
  - IV is computed locally with Black-76 from settlement prices (rate default
    6.5%), or taken from the live chain when available.
  - History accumulates in SQLite; IV rank becomes meaningful after ~2-4 weeks.

Usage:
  python nse_deriv_monitor.py                       # NIFTY + BANKNIFTY, full run
  python nse_deriv_monitor.py --symbols NIFTY       # single index
  python nse_deriv_monitor.py --no-fetch            # render from local DB only
  python nse_deriv_monitor.py --data-dir <path>     # custom data directory
  python nse_deriv_monitor.py --days 30             # participant OI calendar span
  python nse_deriv_monitor.py --backfill 365        # one-off: seed a year of OI history
"""

import argparse
import csv
import gzip
import io
import json
import math
import os
import sqlite3
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

SCHEMA_VERSION = 2
RISK_FREE = 0.065

PARTICIPANTS = ["Client", "DII", "FII", "Pro"]
OI_COLS = [
    "idx_fut_long", "idx_fut_short", "stk_fut_long", "stk_fut_short",
    "idx_opt_call_long", "idx_opt_put_long", "idx_opt_call_short", "idx_opt_put_short",
    "stk_opt_call_long", "stk_opt_put_long", "stk_opt_call_short", "stk_opt_put_short",
    "total_long", "total_short",
]
YF_MAP = {"NIFTY": "^NSEI", "BANKNIFTY": "^NSEBANK"}


# ================================================================ http utils

def _decompress(raw, encoding):
    if encoding == "gzip":
        return gzip.decompress(raw)
    return raw


class CookieJar:
    def __init__(self):
        self.cookies = {}

    def update_from(self, headers):
        try:
            for h in headers.get_all("Set-Cookie") or []:
                pair = h.split(";", 1)[0]
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    self.cookies[k.strip()] = v.strip()
        except Exception:
            pass

    def add_to_request(self, req):
        if self.cookies:
            req.add_header("Cookie", "; ".join(f"{k}={v}" for k, v in self.cookies.items()))


def http_get(url, referer="https://www.nseindia.com/", retries=3, timeout=30,
             cookie_jar=None):
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
            "Referer": referer,
            "Connection": "keep-alive",
        })
        if cookie_jar:
            cookie_jar.add_to_request(req)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = _decompress(r.read(), r.headers.get("Content-Encoding"))
                if cookie_jar is not None:
                    cookie_jar.update_from(r.headers)
                return raw
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (403, 429, 502, 503):
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"GET failed after {retries} retries: {url} ({last})")


# ================================================================ math utils

def _npdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def _npcdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def black76(F, K, T, r, sigma, is_call):
    """European option price on a forward (Black-76)."""
    if T <= 0 or sigma <= 0:
        return max(F - K, 0.0) if is_call else max(K - F, 0.0)
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    df = math.exp(-r * T)
    if is_call:
        return df * (F * _npcdf(d1) - K * _npcdf(d2))
    return df * (K * _npcdf(-d2) - F * _npcdf(-d1))


def implied_vol(price, F, K, T, r=RISK_FREE, is_call=True):
    """Black-76 bisection solver -> annualised vol (fraction) or None."""
    if not price or price <= 0 or T <= 0 or F <= 0 or K <= 0:
        return None
    intrinsic = max(F - K, 0.0) if is_call else max(K - F, 0.0)
    if price < intrinsic * 0.999:
        return None
    lo, hi = 0.005, 5.0
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        val = black76(F, K, T, r, mid, is_call)
        if abs(val - price) < 1e-4:
            return mid
        if val > price:
            hi = mid
        else:
            lo = mid
    return None


def realized_vol(closes, window=21):
    if len(closes) < window + 1:
        return None
    rets = []
    for a, b in zip(closes[-window - 1:-1], closes[-window:]):
        if a and b > 0:
            rets.append(b / a - 1.0)
    if len(rets) < window - 2:
        return None
    return statistics.stdev(rets) * (252 ** 0.5) * 100.0


def rank_pct(series, value):
    """Percentile rank of `value` within `series` (both 0-100)."""
    vals = [v for v in series if v is not None]
    if not vals or value is None:
        return None, None
    below = sum(1 for v in vals if v < value)
    le = sum(1 for v in vals if v <= value)
    return below / len(vals) * 100.0, le / len(vals) * 100.0


def to_float(v):
    try:
        if v is None or v == "":
            return None
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def parse_dmy(s):
    return datetime.strptime(s, "%d-%b-%Y").date()


def parse_ymd(s):
    return datetime.strptime(s, "%Y-%m-%d").date()


def pct_change(cur, prev):
    if cur is None or prev in (None, 0):
        return None
    return (cur - prev) / abs(prev) * 100.0


# ================================================================ database

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS participant_oi (
    date TEXT NOT NULL,
    participant TEXT NOT NULL,
    idx_fut_long INTEGER, idx_fut_short INTEGER,
    stk_fut_long INTEGER, stk_fut_short INTEGER,
    idx_opt_call_long INTEGER, idx_opt_put_long INTEGER,
    idx_opt_call_short INTEGER, idx_opt_put_short INTEGER,
    stk_opt_call_long INTEGER, stk_opt_put_long INTEGER,
    stk_opt_call_short INTEGER, stk_opt_put_short INTEGER,
    total_long INTEGER, total_short INTEGER,
    fetched_at TEXT,
    PRIMARY KEY (date, participant)
);

CREATE TABLE IF NOT EXISTS cash_flow (
    date TEXT NOT NULL,
    category TEXT NOT NULL,
    buy REAL, sell REAL, net REAL,
    fetched_at TEXT,
    PRIMARY KEY (date, category)
);

CREATE TABLE IF NOT EXISTS iv_snapshot (
    date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    spot REAL, atm_strike REAL, expiry TEXT,
    atm_iv REAL, atm_ce_iv REAL, atm_pe_iv REAL,
    skew_5pct REAL, term_structure TEXT,
    pcr REAL, max_ce_oi_strike REAL, max_pe_oi_strike REAL,
    max_pain REAL, total_ce_oi REAL, total_pe_oi REAL,
    atm_fut_basis REAL, source TEXT,
    fetched_at TEXT,
    PRIMARY KEY (date, symbol)
);
"""


def open_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(DB_SCHEMA)
    with conn:
        cur = conn.execute("SELECT value FROM meta WHERE key='schema_version'")
        row = cur.fetchone()
        ver = int(row[0]) if row else 0
        if ver < SCHEMA_VERSION:
            # iv_snapshot columns changed during development: rebuild it.
            conn.execute("DROP TABLE IF EXISTS iv_snapshot")
            conn.executescript(DB_SCHEMA)
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)",
                         (str(SCHEMA_VERSION),))
    return conn


# ================================================================ participant OI

def parse_participant_csv(text):
    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader if any(c.strip() for c in r)]
    if len(rows) < 3:
        return {}
    out = {}
    for row in rows[2:]:
        if not row or not row[0].strip():
            continue
        name = row[0].strip()
        if name not in PARTICIPANTS:
            continue
        vals = []
        for c in row[1:1 + len(OI_COLS)]:
            v = to_float(c)
            vals.append(int(v) if v is not None else None)
        while len(vals) < len(OI_COLS):
            vals.append(None)
        out[name] = vals
    return out


def fetch_participant_history(conn, days=21, max_files=12, verbose=True):
    fetched = 0
    today = datetime.now(IST).date()
    stored = {r[0] for r in conn.execute("SELECT DISTINCT date FROM participant_oi")}
    for i in range(days + 1):
        if fetched >= max_files:
            break
        day = today - timedelta(days=i)
        if day.weekday() >= 5 or day == today:
            continue  # archive publishes after market close / next morning
        ds_iso = day.strftime("%Y-%m-%d")
        if ds_iso in stored:
            continue
        ds = day.strftime("%d%m%Y")
        url = f"https://archives.nseindia.com/content/nsccl/fao_participant_oi_{ds}.csv"
        try:
            raw = http_get(url, retries=2)
            rows = parse_participant_csv(raw.decode("utf-8", "replace"))
            if not rows:
                continue
            now = datetime.now(IST).isoformat(timespec="seconds")
            with conn:
                for p, cols in rows.items():
                    conn.execute(
                        "INSERT OR REPLACE INTO participant_oi VALUES "
                        "(:date,:participant,:idx_fut_long,:idx_fut_short,:stk_fut_long,"
                        ":stk_fut_short,:idx_opt_call_long,:idx_opt_put_long,"
                        ":idx_opt_call_short,:idx_opt_put_short,:stk_opt_call_long,"
                        ":stk_opt_put_long,:stk_opt_call_short,:stk_opt_put_short,"
                        ":total_long,:total_short,:fetched_at)",
                        {"date": ds_iso, "participant": p, "fetched_at": now,
                         **dict(zip(OI_COLS, cols))})
            fetched += 1
            if verbose:
                print(f"    participant OI {ds_iso}: OK")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue  # holiday not covered above / file absent
            if verbose:
                print(f"    participant OI {ds_iso}: HTTP {e.code}")
        except Exception as e:  # noqa: BLE001
            if verbose:
                print(f"    participant OI {ds_iso}: {type(e).__name__}")
        time.sleep(0.2)
    return fetched


# ================================================================ cash flows

def fetch_cash_flows(conn, verbose=True):
    try:
        raw = http_get("https://www.nseindia.com/api/fiidiiTradeReact",
                       referer="https://www.nseindia.com/reports/fii-dii", retries=2)
        data = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        if verbose:
            print(f"    cash flow: {type(e).__name__}: {e}")
        return 0
    n = 0
    now = datetime.now(IST).isoformat(timespec="seconds")
    with conn:
        for row in data:
            date = (row.get("date") or "").strip()
            cat = (row.get("category") or "").strip()
            if not date or not cat:
                continue
            try:
                dt = datetime.strptime(date, "%d-%b-%Y").date().isoformat()
            except ValueError:
                continue
            conn.execute("INSERT OR REPLACE INTO cash_flow VALUES (?,?,?,?,?,?)",
                         (dt, cat, to_float(row.get("buyValue")), to_float(row.get("sellValue")),
                          to_float(row.get("netValue")), now))
            n += 1
    if verbose:
        print(f"    cash flow: {n} rows")
    return n


# ================================================================ bhavcopy

def fetch_bhavcopy(days_back=10, verbose=True):
    """Download most recent F&O UDiFF bhavcopy. Returns (rows, trade_date) or (None, None)."""
    today = datetime.now(IST).date()
    for i in range(days_back + 1):
        day = today - timedelta(days=i)
        if day.weekday() >= 5:
            continue
        ds = day.strftime("%Y%m%d")
        url = (f"https://archives.nseindia.com/content/fo/"
               f"BhavCopy_NSE_FO_0_0_0_{ds}_F_0000.csv.zip")
        try:
            raw = http_get(url, retries=1, timeout=60)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue
            if verbose:
                print(f"    bhavcopy {ds}: HTTP {e.code}")
            continue
        except Exception as e:  # noqa: BLE001
            if verbose:
                print(f"    bhavcopy {ds}: {type(e).__name__}")
            continue
        try:
            zf = zipfile.ZipFile(io.BytesIO(raw))
            with zf.open(zf.namelist()[0]) as fh:
                rows = list(csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8")))
            if verbose:
                print(f"    bhavcopy {ds}: {len(rows)} rows")
            return rows, day
        except Exception as e:  # noqa: BLE001
            if verbose:
                print(f"    bhavcopy {ds}: parse error {type(e).__name__}")
            continue
    return None, None


def bhavcopy_option_data(rows, trade_dt, symbol):
    """Extract chain structure for one index from bhavcopy rows.

    Returns dict:
      futures: {expiry_iso: settle}
      options: {expiry_iso: {strike: {'CE': {...}, 'PE': {...}}}}
      underlying: float (close)
    """
    futures, options, underlying = {}, {}, None
    for r in rows:
        if r.get("TckrSymb") != symbol:
            continue
        ftp = r.get("FinInstrmTp")
        exp = r.get("XpryDt")
        if not exp:
            continue
        try:
            exp_d = parse_ymd(exp)
        except ValueError:
            continue
        if ftp == "IDF":
            futures[exp] = to_float(r.get("SttlmPric"))
            if underlying is None:
                underlying = to_float(r.get("UndrlygPric"))
        elif ftp == "IDO" and exp_d > trade_dt:
            strike = to_float(r.get("StrkPric"))
            opt = r.get("OptnTp")
            if strike is None or opt not in ("CE", "PE"):
                continue
            options.setdefault(exp, {}).setdefault(strike, {})[opt] = {
                "settle": to_float(r.get("SttlmPric")),
                "close": to_float(r.get("ClsPric")),
                "oi": to_float(r.get("OpnIntrst")) or 0.0,
                "oi_chg": to_float(r.get("ChngInOpnIntrst")) or 0.0,
                "vol": to_float(r.get("TtlTradgVol")) or 0.0,
            }
    return {"futures": futures, "options": options, "underlying": underlying}


def metrics_from_strikes(strikes, spot, forward, T):
    """ATM IV, skew, PCR, OI walls, max pain from a {strike: {CE,PE}} dict."""
    ks = sorted(strikes)
    if not ks or forward <= 0:
        return None
    atm_strike = min(ks, key=lambda k: abs(k - forward))

    def iv_at(strike, side):
        d = (strikes.get(strike) or {}).get(side)
        if not d or not d.get("settle"):
            return None
        iv = implied_vol(d["settle"], forward, strike, T, RISK_FREE, side == "CE")
        return round(iv * 100, 2) if iv else None

    ce_iv, pe_iv = iv_at(atm_strike, "CE"), iv_at(atm_strike, "PE")
    vals = [v for v in (ce_iv, pe_iv) if v]
    atm_iv = statistics.fmean(vals) if vals else None

    down = min(ks, key=lambda k: abs(k - forward * 0.95))
    up = min(ks, key=lambda k: abs(k - forward * 1.05))
    put_iv, call_iv = iv_at(down, "PE"), iv_at(up, "CE")
    if put_iv is None or call_iv is None:
        # fallback: nearest quoted OTM strike on each side
        def nearest_iv(target, side):
            for k in sorted(ks, key=lambda x: abs(x - target)):
                v = iv_at(k, side)
                if v is not None:
                    return v
            return None
        put_iv = put_iv if put_iv is not None else nearest_iv(forward * 0.95, "PE")
        call_iv = call_iv if call_iv is not None else nearest_iv(forward * 1.05, "CE")
    skew = round(put_iv - call_iv, 2) if (put_iv is not None and call_iv is not None) else None

    ce_oi = sum((strikes[k].get("CE") or {}).get("oi", 0.0) for k in ks)
    pe_oi = sum((strikes[k].get("PE") or {}).get("oi", 0.0) for k in ks)
    pcr = round(pe_oi / ce_oi, 3) if ce_oi else None
    max_ce = max(ks, key=lambda k: (strikes[k].get("CE") or {}).get("oi", 0.0))
    max_pe = max(ks, key=lambda k: (strikes[k].get("PE") or {}).get("oi", 0.0))

    def pain(k0):
        total = 0.0
        for k in ks:
            ce = (strikes[k].get("CE") or {}).get("oi", 0.0)
            pe = (strikes[k].get("PE") or {}).get("oi", 0.0)
            if k < k0:
                total += ce * (k0 - k)
            elif k > k0:
                total += pe * (k - k0)
        return total

    max_pain = min(ks, key=pain) if len(ks) < 500 else None
    return {
        "atm_strike": float(atm_strike), "atm_iv": round(atm_iv, 2) if atm_iv else None,
        "ce_iv": ce_iv, "pe_iv": pe_iv, "skew_5pct": skew, "pcr": pcr,
        "total_ce_oi": ce_oi, "total_pe_oi": pe_oi,
        "max_ce_oi_strike": float(max_ce), "max_pe_oi_strike": float(max_pe),
        "max_pain": float(max_pain) if max_pain else None,
    }


def snapshot_from_bhavcopy(conn, rows, trade_dt, symbol, verbose=True):
    data = bhavcopy_option_data(rows, trade_dt, symbol)
    options, futures, underlying = data["options"], data["futures"], data["underlying"]
    if not options or not underlying:
        if verbose:
            print(f"    {symbol}: bhavcopy has no live option chain")
        return None

    expiries = sorted(options)[:3]
    now = datetime.now(IST).isoformat(timespec="seconds")
    date_iso = trade_dt.isoformat()
    term, atoms = {}, []
    fut_ref_exp = min(futures, key=lambda e: abs(parse_ymd(e) - trade_dt)) if futures else None
    fut_spot_basis = None
    if fut_ref_exp and futures.get(fut_ref_exp):
        fut_spot_basis = {"close": round(futures[fut_ref_exp] - underlying, 2),
                          "expiry": fut_ref_exp}
    for exp in expiries:
        T = max((parse_ymd(exp) - trade_dt).days, 1) / 365.0
        fut = futures.get(exp)
        forward = fut if fut else underlying * math.exp(RISK_FREE * T)
        m = metrics_from_strikes(options[exp], underlying, forward, T)
        if not m:
            continue
        m["expiry"] = exp
        m["forward"] = forward
        m["basis"] = fut_spot_basis if (fut_spot_basis and futures.get(exp)) else None
        if m["atm_iv"]:
            term[exp] = m["atm_iv"]
        atoms.append(m)

    if not atoms:
        return None
    n0 = atoms[0]
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO iv_snapshot (date, symbol, spot, atm_strike, expiry, "
            "atm_iv, atm_ce_iv, atm_pe_iv, skew_5pct, term_structure, pcr, max_ce_oi_strike, "
            "max_pe_oi_strike, max_pain, total_ce_oi, total_pe_oi, atm_fut_basis, source, "
            "fetched_at) VALUES "
            "(:date,:symbol,:spot,:atm_strike,:expiry,:atm_iv,:ce_iv,:pe_iv,:skew,:term,"
            ":pcr,:max_ce,:max_pe,:max_pain,:tot_ce,:tot_pe,:basis,:source,:fetched_at)",
            {"date": date_iso, "symbol": symbol, "spot": underlying,
             "atm_strike": n0["atm_strike"], "expiry": n0["expiry"], "atm_iv": n0["atm_iv"],
             "ce_iv": n0["ce_iv"], "pe_iv": n0["pe_iv"], "skew": n0["skew_5pct"],
             "term": json.dumps(term), "pcr": n0["pcr"],
             "max_ce": n0["max_ce_oi_strike"], "max_pe": n0["max_pe_oi_strike"],
             "max_pain": n0["max_pain"], "tot_ce": n0["total_ce_oi"],
             "tot_pe": n0["total_pe_oi"],
             "basis": json.dumps(n0["basis"]) if n0.get("basis") else None,
             "source": "bhavcopy", "fetched_at": now})
    if verbose:
        iv_s = f"{n0['atm_iv']:.2f}" if n0["atm_iv"] else "n/a"
        print(f"    {symbol} (bhavcopy {date_iso}): spot={underlying:.1f}, "
              f"ATM IV={iv_s}, PCR={n0['pcr']}")
    return {"atoms": atoms, "spot": underlying, "source": "bhavcopy", "date": date_iso}


# ================================================================ live chain (best effort)

def chain_strikes(rec):
    out = {}
    for row in rec.get("data", []):
        sp = row.get("strikePrice")
        if sp is None:
            continue
        entry = out.setdefault(sp, {})
        for side in ("CE", "PE"):
            if row.get(side):
                entry[side] = row[side]
    return out, rec.get("underlyingValue") or None


def fetch_live_chain_snapshot(conn, symbol, attempts=2, verbose=True):
    """Best-effort live option chain (often throttled). Returns dict or None."""
    params = {"type": "Indices", "symbol": symbol}
    url = "https://www.nseindia.com/api/option-chain-v3?" + urllib.parse.urlencode(params)
    rec = None
    for i in range(attempts):
        try:
            raw = http_get(url, referer="https://www.nseindia.com/option-chain", retries=2)
            j = json.loads(raw)
            rec = j.get("records", {})
            if rec.get("data"):
                break
            rec = None
        except Exception:
            rec = None
        time.sleep(5 * (i + 1))
    if not rec:
        if verbose:
            print(f"    {symbol}: live chain unavailable (throttled); using bhavcopy")
        return None

    expiries = rec.get("expiryDates", [])
    now = datetime.now(IST)
    today = now.date()
    valid = [e for e in expiries if parse_dmy(e) >= today][:3]
    atoms = []
    for exp in valid:
        u = ("https://www.nseindia.com/api/option-chain-v3?"
             + urllib.parse.urlencode({"type": "Indices", "symbol": symbol, "expiry": exp}))
        try:
            r2 = json.loads(http_get(u, referer="https://www.nseindia.com/option-chain"))
            strikes_raw, spot = chain_strikes(r2.get("records", {}))
            if not strikes_raw or not spot:
                continue
            strikes = {}
            for k, sides in strikes_raw.items():
                strikes[k] = {}
                for side in ("CE", "PE"):
                    d = sides.get(side)
                    if d:
                        iv = to_float(d.get("impliedVolatility")) or 0
                        strikes[k][side] = {
                            "oi": to_float(d.get("openInterest")) or 0.0,
                            "oi_chg": to_float(d.get("changeinOpenInterest")) or 0.0,
                            "vol": to_float(d.get("totalTradedVolume")) or 0.0,
                            "iv": iv if iv > 0 else None,
                        }
            T = max((parse_dmy(exp) - today).days, 1) / 365.0
            forward = spot * math.exp(RISK_FREE * T)
            m = metrics_from_strikes(strikes, spot, forward, T)
            if not m:
                continue
            # prefer exchange-published IVs where available
            atm = m["atm_strike"]
            ivs = [strikes.get(atm, {}).get(s, {}).get("iv") for s in ("CE", "PE")]
            ivs = [v for v in ivs if v]
            if ivs:
                m["atm_iv"] = round(statistics.fmean(ivs), 2)
            m["expiry"], m["forward"] = exp, forward
            m["basis"] = None
            atoms.append(m)
            time.sleep(1.2)
        except Exception:
            continue
    if not atoms:
        return None

    # futures basis via contract-info (works even when the chain is throttled)
    basis = None
    try:
        ci = json.loads(http_get(
            "https://www.nseindia.com/api/option-chain-contract-info?symbol="
            + urllib.parse.quote(symbol),
            referer="https://www.nseindia.com/option-chain", retries=1))
        futs = ci.get("futuresData") or ci.get("futures") or []
        spot_v = rec.get("underlyingValue")
        if futs and spot_v:
            f0 = futs[0]
            price = to_float(f0.get("lastPrice") or f0.get("price") or f0.get("closePrice"))
            exp = f0.get("expiryDate") or f0.get("expiry") or ""
            if price:
                basis = {"close": round(price - spot_v, 2), "expiry": str(exp)}
    except Exception:
        pass

    n0 = atoms[0]
    date_iso = today.isoformat()
    term = {a["expiry"]: a["atm_iv"] for a in atoms if a["atm_iv"]}
    fetched_at = now.isoformat(timespec="seconds")
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO iv_snapshot (date, symbol, spot, atm_strike, expiry, "
            "atm_iv, atm_ce_iv, atm_pe_iv, skew_5pct, term_structure, pcr, max_ce_oi_strike, "
            "max_pe_oi_strike, max_pain, total_ce_oi, total_pe_oi, atm_fut_basis, source, "
            "fetched_at) VALUES "
            "(:date,:symbol,:spot,:atm_strike,:expiry,:atm_iv,:ce_iv,:pe_iv,:skew,:term,"
            ":pcr,:max_ce,:max_pe,:max_pain,:tot_ce,:tot_pe,:basis,:source,:fetched_at)",
            {"date": date_iso, "symbol": symbol, "spot": rec.get("underlyingValue"),
             "atm_strike": n0["atm_strike"], "expiry": n0["expiry"], "atm_iv": n0["atm_iv"],
             "ce_iv": n0["ce_iv"], "pe_iv": n0["pe_iv"], "skew": n0["skew_5pct"],
             "term": json.dumps(term), "pcr": n0["pcr"],
             "max_ce": n0["max_ce_oi_strike"], "max_pe": n0["max_pe_oi_strike"],
             "max_pain": n0["max_pain"], "tot_ce": n0["total_ce_oi"],
             "tot_pe": n0["total_pe_oi"],
             "basis": json.dumps(basis) if basis else None,
             "source": "live", "fetched_at": fetched_at})
    if verbose:
        print(f"    {symbol} (live): spot={rec.get('underlyingValue')}, ATM IV={n0['atm_iv']}")
    return {"atoms": atoms, "spot": rec.get("underlyingValue"), "source": "live", "date": date_iso}


# ================================================================ vol context

def cached_series(path, yahoo_symbol, days, max_age_h=20):
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                c = json.load(fh)
            if (time.time() - c.get("ts", 0)) / 3600 <= max_age_h:
                return c["series"]
    except Exception:
        pass
    try:
        end = int(time.time())
        start = end - days * 86400
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/"
               f"{urllib.parse.quote(yahoo_symbol)}?period1={start}&period2={end}&interval=1d")
        raw = http_get(url, referer="https://finance.yahoo.com/", retries=2, timeout=25)
        j = json.loads(raw)
        res = (j.get("chart", {}).get("result") or [None])[0]
        if not res:
            return None
        ts = res.get("timestamp") or []
        closes = (res.get("indicators", {}).get("quote") or [{}])[0].get("close") or []
        series = [{"date": datetime.fromtimestamp(t, IST).date().isoformat(), "close": float(c)}
                  for t, c in zip(ts, closes) if c is not None]
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"ts": time.time(), "series": series}, fh)
        return series
    except Exception:
        return None


def vol_context(data_dir, verbose=True):
    ctx = {}
    vix_series = cached_series(os.path.join(data_dir, "india_vix_cache.json"), "^INDIAVIX", 800)
    if vix_series:
        vals = [h["close"] for h in vix_series]
        win = vals[-252:]
        rank, pc = rank_pct(win, vals[-1])
        ctx["vix"] = {"last": vals[-1], "rank_1y": rank, "pct_1y": pc,
                      "min_1y": min(win), "max_1y": max(win), "n": len(win)}
    nifty_series = cached_series(os.path.join(data_dir, "nifty_px_cache.json"), "^NSEI", 400)
    if nifty_series:
        closes = [h["close"] for h in nifty_series]
        ctx["nifty_realvol"] = {"rv10": realized_vol(closes, 10), "rv21": realized_vol(closes, 21)}
    return ctx


# ================================================================ report data

def build_summary(conn, symbols, verbose=True):
    s = {"generated_at": datetime.now(IST).isoformat(timespec="seconds"), "symbols": {}}

    dates = [r[0] for r in conn.execute("SELECT DISTINCT date FROM participant_oi ORDER BY date")]
    if dates:
        def snap(d):
            out = {}
            for p, il, is_, cl, pl, cs, ps in conn.execute(
                    "SELECT participant, idx_fut_long, idx_fut_short, idx_opt_call_long, "
                    "idx_opt_put_long, idx_opt_call_short, idx_opt_put_short "
                    "FROM participant_oi WHERE date=?", (d,)):
                out[p] = {"fut_long": il, "fut_short": is_, "call_long": cl, "put_long": pl,
                          "call_short": cs, "put_short": ps}
            return out
        latest_d = dates[-1]
        ref_d = dates[-6] if len(dates) > 5 else (dates[0] if len(dates) > 1 else None)
        latest, ref = snap(latest_d), (snap(ref_d) if ref_d else {})
        s["participant"] = {"latest_date": latest_d, "ref_date": ref_d, "rows": {}}
        for p in PARTICIPANTS:
            cur, old = latest.get(p, {}), ref.get(p, {})
            fl, fs = cur.get("fut_long") or 0, cur.get("fut_short") or 0
            net = fl - fs
            prev_net = ((old.get("fut_long") or 0) - (old.get("fut_short") or 0)) if ref_d else None
            lr = (fl / (fl + fs) * 100) if (fl + fs) else None
            ce_oi = (cur.get("call_long") or 0) + (cur.get("call_short") or 0)
            pe_oi = (cur.get("put_long") or 0) + (cur.get("put_short") or 0)
            s["participant"]["rows"][p] = {
                "idx_fut_long": fl, "idx_fut_short": fs, "idx_fut_net": net,
                "idx_fut_net_prev": prev_net, "long_ratio": lr,
                "idx_opt_pcr": round(pe_oi / ce_oi, 3) if ce_oi else None,
            }

    cfd = {}
    for date, cat, buy, sell, net in conn.execute(
            "SELECT date, category, buy, sell, net FROM cash_flow ORDER BY date DESC"):
        cfd.setdefault(date, {})[cat] = {"buy": buy, "sell": sell, "net": net}
    s["cash"] = {d: cfd[d] for d in sorted(cfd, reverse=True)[:3]}

    for sym in symbols:
        rows = list(conn.execute(
            "SELECT date, spot, atm_strike, expiry, atm_iv, atm_ce_iv, atm_pe_iv, skew_5pct, "
            "term_structure, pcr, max_ce_oi_strike, max_pe_oi_strike, max_pain, "
            "total_ce_oi, total_pe_oi, atm_fut_basis, source "
            "FROM iv_snapshot WHERE symbol=? ORDER BY date", (sym,)))
        sym_s = {}
        if rows:
            cols = ["date", "spot", "atm_strike", "expiry", "atm_iv", "ce_iv", "pe_iv",
                    "skew_5pct",
                    "term_structure", "pcr", "max_ce_oi_strike", "max_pe_oi_strike", "max_pain",
                    "total_ce_oi", "total_pe_oi", "basis", "source"]
            latest = dict(zip(cols, rows[-1]))
            try:
                latest["term_structure"] = json.loads(latest["term_structure"] or "{}")
            except Exception:
                latest["term_structure"] = {}
            if isinstance(latest.get("basis"), str):
                try:
                    latest["basis"] = json.loads(latest["basis"])
                except Exception:
                    latest["basis"] = None
            sym_s.update(latest)
            iv_series = [r[4] for r in rows if r[4] is not None]
            if len(iv_series) >= 8:
                rank, pc = rank_pct(iv_series, iv_series[-1])
                sym_s["iv_rank_local"] = {"n": len(iv_series), "rank": rank, "pct": pc}
            if len(rows) > 1 and rows[-2][4] and rows[-1][4]:
                sym_s["atm_iv_prev"] = rows[-2][4]
                sym_s["atm_iv_prev_date"] = rows[-2][0]
        s["symbols"][sym] = sym_s
    return s


# ================================================================ rendering

def fmt(v, digits=2, na="-"):
    if v is None:
        return na
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return na
        return f"{v:,.{digits}f}"
    return str(v)


def signed(v, digits=1, suffix=""):
    if v is None:
        return "n/a"
    return f"{'+' if v >= 0 else ''}{v:,.{digits}f}{suffix}"


def basis_display(basis):
    """-> (value_str, label) from a {close, expiry} dict or raw number."""
    if basis is None:
        return None, ""
    if isinstance(basis, dict):
        val, exp = basis.get("close"), basis.get("expiry")
        label = ""
        if exp:
            try:
                label = parse_ymd(exp).strftime("%d-%b")
            except ValueError:
                try:
                    label = parse_dmy(exp).strftime("%d-%b")
                except ValueError:
                    label = str(exp)
        return signed(val, 1), f" (fut {label})"
    return signed(basis, 1), ""


def term_display(term):
    out = {}
    for k, v in term.items():
        try:
            out[parse_ymd(k).strftime("%d-%b")] = v
        except ValueError:
            try:
                out[parse_dmy(k).strftime("%d-%b")] = v
            except ValueError:
                out[k] = v
    return out


def render_console(summary, symbols):
    out = []
    now = datetime.now(IST)
    out.append("=" * 74)
    out.append(f"  NSE DERIVATIVES MONITOR  |  {now.strftime('%d-%b-%Y %H:%M IST')}")
    out.append("=" * 74)

    p = summary.get("participant")
    if p:
        out.append("")
        out.append(f"L1 POSITIONING - participant OI ({p['latest_date']}"
                   + (f" vs {p['ref_date']})" if p.get("ref_date") else ")"))
        out.append(f"  {'Participant':<8} {'Fut L':>9} {'Fut S':>9} {'Net':>10} {'d vs ref':>10} "
                   f"{'L/S':>7} {'Opt PCR':>8}")
        for name in PARTICIPANTS:
            r = p["rows"].get(name, {})
            delta = None
            if r.get("idx_fut_net_prev") is not None:
                delta = (r.get("idx_fut_net") or 0) - r["idx_fut_net_prev"]
            lr = r.get("long_ratio")
            lr_s = f"{lr:.1f}%" if lr is not None else "n/a"
            out.append(f"  {name:<8} {fmt(r.get('idx_fut_long'), 0):>9} "
                       f"{fmt(r.get('idx_fut_short'), 0):>9} {fmt(r.get('idx_fut_net'), 0):>10} "
                       f"{signed(delta, 0, 'ct'):>10} {lr_s:>7} "
                       f"{fmt(r.get('idx_opt_pcr'), 2):>8}")
        out.append("    (FII net short futures ~ hedging flows; PCR > 1 = put-heavy positioning)")

    if summary.get("cash"):
        out.append("")
        out.append("  FII / DII CASH MARKET (Rs crore)")
        for d, cats in summary["cash"].items():
            fii = (cats.get("FII/FPI") or {}).get("net")
            dii = (cats.get("DII") or {}).get("net")
            out.append(f"    {d}:  FII {signed(fii)}   DII {signed(dii)}")

    for sym in symbols:
        s = summary["symbols"].get(sym, {})
        out.append("")
        out.append(f"L2 VOL / L3 MICROSTRUCTURE - {sym}")
        if not s.get("date"):
            out.append("    (no snapshot yet - run without --no-fetch)")
            continue
        src = "live" if s.get("source") == "live" else "bhavcopy (EOD)"
        bval, blabel = basis_display(s.get("basis"))
        basis_s = f"  basis {bval}{blabel}" if bval else ""
        out.append(f"    snapshot {s['date']}  [{src}]  spot {fmt(s.get('spot'))}{basis_s}")
        iv_line = f"    ATM IV {fmt(s.get('atm_iv'))}%"
        if s.get("atm_iv_prev"):
            iv_line += (f"  (prev {fmt(s.get('atm_iv_prev'))}% on {s.get('atm_iv_prev_date')})")
        out.append(iv_line)
        out.append(f"    Skew (5% OTM put-call) {fmt(s.get('skew_5pct'))}   "
                   f"PCR {fmt(s.get('pcr'))}   "
                   f"walls: call {fmt(s.get('max_ce_oi_strike'), 0)} / "
                   f"put {fmt(s.get('max_pe_oi_strike'), 0)}   "
                   f"max pain {fmt(s.get('max_pain'), 0)}")
        term = term_display(s.get("term_structure") or {})
        if term:
            out.append("    Term structure ATM IV: " + "   ".join(
                f"{k}: {fmt(v)}" for k, v in term.items()))
        if s.get("iv_rank_local"):
            r = s["iv_rank_local"]
            out.append(f"    IV rank (local history, {r['n']} sessions): "
                       f"{fmt(r['rank'], 0)}th percentile")

    vix = summary.get("vix")
    rv = summary.get("nifty_realvol")
    if vix or rv:
        out.append("")
        out.append("  VOL CONTEXT")
        if vix:
            out.append(f"    India VIX {fmt(vix.get('last'))}  1y rank {fmt(vix.get('rank_1y'), 0)}  "
                       f"1y range {fmt(vix.get('min_1y'))} - {fmt(vix.get('max_1y'))}")
        if rv:
            out.append(f"    NIFTY realized vol: 10d {fmt(rv.get('rv10'))}% / "
                       f"21d {fmt(rv.get('rv21'))}%  (compare with ATM IV)")
    out.append("")
    out.append("-" * 74)
    out.append("  Framework: L1 positioning (weekly) -> L2 volatility (IV rank, skew)")
    out.append("  -> L3 microstructure (walls, PCR, max pain). Data: NSE public sources.")
    out.append("-" * 74)
    return "\n".join(out)


def render_html(summary, symbols):
    def esc(x):
        return str(x).replace("&", "&amp;").replace("<", "&lt;")

    def card(t, v, sub=""):
        return (f'<div class="card"><div class="t">{esc(t)}</div>'
                f'<div class="v">{esc(v)}</div><div class="s">{esc(sub)}</div></div>')

    parts = ["""<!doctype html><html><head><meta charset="utf-8">
<title>NSE Derivatives Monitor</title><style>
body{background:#0f1420;color:#e8edf7;font-family:Segoe UI,Arial,sans-serif;margin:28px;max-width:1080px}
h1{font-size:22px;margin-bottom:2px} h2{font-size:15px;color:#9fb3d1;border-bottom:1px solid #26314a;
padding-bottom:6px;margin-top:28px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:12px 0}
.card{background:#161d2e;border:1px solid #26314a;border-radius:10px;padding:12px}
.card .t{font-size:12px;color:#8ea3c4}.card .v{font-size:21px;font-weight:600;margin:4px 0}
.card .s{font-size:12px;color:#7d8fa9}
table{border-collapse:collapse;width:100%;margin:8px 0}
th,td{padding:7px 10px;border-bottom:1px solid #26314a;text-align:right;font-size:13px}
th{color:#9fb3d1} td:first-child,th:first-child{text-align:left}
.pos{color:#53d769}.neg{color:#ff6b6b}.muted{color:#7d8fa9;font-size:12px}
</style></head><body>"""]
    parts.append(f"<h1>NSE Derivatives Monitor</h1><div class='muted'>Generated "
                 f"{esc(summary['generated_at'])}</div>")

    p = summary.get("participant")
    if p:
        parts.append(f"<h2>L1 &middot; Participant OI ({esc(p['latest_date'])}"
                     + (f" vs {esc(p['ref_date'])}" if p.get("ref_date") else "") + ")</h2>")
        rows = ['<table><tr><th>Participant</th><th>Idx Fut Long</th><th>Idx Fut Short</th>'
                '<th>Net</th><th>&Delta; vs ref</th><th>L/S ratio</th><th>Idx Opt PCR</th></tr>']
        for name in PARTICIPANTS:
            r = p["rows"].get(name, {})
            delta = None
            if r.get("idx_fut_net_prev") is not None:
                delta = (r.get("idx_fut_net") or 0) - r["idx_fut_net_prev"]
            cls = "pos" if (delta or 0) >= 0 else "neg"
            rows.append(
                f"<tr><td>{esc(name)}</td><td>{fmt(r.get('idx_fut_long'),0)}</td>"
                f"<td>{fmt(r.get('idx_fut_short'),0)}</td><td>{fmt(r.get('idx_fut_net'),0)}</td>"
                f"<td class='{cls}'>{signed(delta,0)}</td>"
                f"<td>{fmt(r.get('long_ratio'),1,'n/a')}</td>"
                f"<td>{fmt(r.get('idx_opt_pcr'))}</td></tr>")
        rows.append("</table>")
        parts.append("".join(rows))

    if summary.get("cash"):
        rows = ['<table><tr><th>Date</th><th>FII net (Rs cr)</th><th>DII net (Rs cr)</th></tr>']
        for d, cats in summary["cash"].items():
            rows.append(f"<tr><td>{esc(d)}</td>"
                        f"<td>{signed((cats.get('FII/FPI') or {}).get('net'),0)}</td>"
                        f"<td>{signed((cats.get('DII') or {}).get('net'),0)}</td></tr>")
        rows.append("</table>")
        parts.append("<h2>FII / DII Cash Market</h2>" + "".join(rows))

    for sym in symbols:
        s = summary["symbols"].get(sym, {})
        parts.append(f"<h2>L2 / L3 &middot; {esc(sym)}</h2>")
        if not s.get("date"):
            parts.append("<div class='muted'>No snapshot yet.</div>")
            continue
        parts.append("<div class='grid'>")
        bval, blabel = basis_display(s.get("basis"))
        spot_sub = f"source: {s.get('source','-')}"
        if bval:
            spot_sub += f" | basis {bval}{blabel}"
        parts.append(card("Spot" + (f" ({s['date']})" if s.get("date") else ""),
                          fmt(s.get("spot")), spot_sub))
        parts.append(card("ATM IV", f"{fmt(s.get('atm_iv'))}%",
                          f"prev {fmt(s.get('atm_iv_prev'))}%" if s.get("atm_iv_prev") else "nearest expiry"))
        r = s.get("iv_rank_local")
        parts.append(card("IV rank (local)", f"{fmt(r['rank'],0)}th pctile" if r else "n/a",
                          f"{r['n']} sessions" if r else "building history"))
        parts.append(card("Skew 5% OTM", fmt(s.get("skew_5pct")), "put IV - call IV"))
        parts.append(card("PCR (near expiry)", fmt(s.get("pcr")), ""))
        parts.append(card("Walls CE / PE",
                          f"{fmt(s.get('max_ce_oi_strike'),0)} / {fmt(s.get('max_pe_oi_strike'),0)}",
                          f"max pain {fmt(s.get('max_pain'),0)}"))
        parts.append("</div>")
        term = term_display(s.get("term_structure") or {})
        if term:
            parts.append("<h2>ATM IV term structure</h2><table><tr>"
                         + "".join(f"<th>{esc(k)}</th>" for k in term) + "</tr><tr>"
                         + "".join(f"<td>{fmt(v)}</td>" for v in term.values()) + "</tr></table>")

    vix, rv = summary.get("vix"), summary.get("nifty_realvol")
    if vix or rv:
        parts.append("<h2>Vol context</h2><div class='grid'>")
        if vix:
            parts.append(card("India VIX", fmt(vix.get("last")),
                              f"1y rank {fmt(vix.get('rank_1y'),0)} / range "
                              f"{fmt(vix.get('min_1y'))}-{fmt(vix.get('max_1y'))}"))
        if rv:
            parts.append(card("NIFTY realized vol",
                              f"{fmt(rv.get('rv10'))}% / {fmt(rv.get('rv21'))}%", "10d / 21d"))
        parts.append("</div>")
    parts.append("<p class='muted'>Sources: NSE participant-wise OI archive, NSE F&amp;O bhavcopy, "
                 "NSE FII/DII API, Yahoo Finance. Educational tool, not investment advice.</p>"
                 "</body></html>")
    return "".join(parts)


# ================================================================ main

def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="NSE derivatives monitoring workflow")
    ap.add_argument("--symbols", nargs="+", default=["NIFTY", "BANKNIFTY"])
    ap.add_argument("--data-dir",
                    default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
    ap.add_argument("--out-dir",
                    default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "output"))
    ap.add_argument("--days", type=int, default=21,
                    help="calendar days to probe for participant OI files")
    ap.add_argument("--backfill", type=int, default=0, metavar="N",
                    help="fetch an initial N days of participant OI history (one-off seeding)")
    ap.add_argument("--no-fetch", action="store_true",
                    help="render from local DB only (no network)")
    args = ap.parse_args()

    os.makedirs(args.data_dir, exist_ok=True)
    os.makedirs(args.out_dir, exist_ok=True)
    conn = open_db(os.path.join(args.data_dir, "nse_monitor.db"))

    if not args.no_fetch:
        print("[1/4] Participant-wise OI (NSE archives)...")
        if args.backfill > 0:
            n = fetch_participant_history(conn, days=args.backfill,
                                          max_files=max(args.backfill, 260))
        else:
            n = fetch_participant_history(conn, days=args.days)
        print(f"      -> {n} new file(s) stored")
        print("[2/4] FII/DII cash flows...")
        fetch_cash_flows(conn)
        print("[3/4] Option chain snapshots...")
        bhav_rows, bhav_dt = fetch_bhavcopy()
        for sym in args.symbols:
            got = fetch_live_chain_snapshot(conn, sym)
            if not got and bhav_rows:
                snapshot_from_bhavcopy(conn, bhav_rows, bhav_dt, sym)
        print("[4/4] Vol context (India VIX / realized vol caches)...")
    else:
        print("[no-fetch] rendering from local DB")

    summary = build_summary(conn, args.symbols)
    summary.update(vol_context(args.data_dir))

    text = render_console(summary, args.symbols)
    print()
    print(text)

    stamp = datetime.now(IST).strftime("%Y%m%d_%H%M")
    json_path = os.path.join(args.out_dir, f"monitor_{stamp}.json")
    html_path = os.path.join(args.out_dir, f"monitor_{stamp}.html")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, default=str)
    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(render_html(summary, args.symbols))
    print(f"\nSaved: {json_path}")
    print(f"Saved: {html_path}")
    conn.close()


if __name__ == "__main__":
    main()
