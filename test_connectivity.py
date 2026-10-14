#!/usr/bin/env python3
"""
Connectivity smoke test for hosted (GitHub Actions) runs.

Checks whether each data source this project depends on is reachable from the
current network location (local machine, CI runner, VPS). Fast: no backoff
loops, short timeouts.

Exit code 0 if critical sources OK (archives + FII/DII), else 1.
"""
import io
import json
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
IST = timezone(timedelta(hours=5, minutes=30))


def get(url, referer="https://www.nseindia.com/", timeout=20):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "gzip, deflate",
        "Referer": referer, "Connection": "keep-alive"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        enc = r.headers.get("Content-Encoding")
        if enc == "gzip":
            import gzip
            raw = gzip.decompress(raw)
        return r.status, raw


def probe(name, fn, critical):
    t0 = time.time()
    try:
        detail = fn()
        ms = int((time.time() - t0) * 1000)
        print(f"  OK   {name:<38} [{ms} ms] {detail}")
        return True
    except Exception as e:  # noqa: BLE001
        ms = int((time.time() - t0) * 1000)
        tag = "FAIL*" if critical else "warn "
        print(f"  {tag} {name:<38} [{ms} ms] {type(e).__name__}: {str(e)[:110]}")
        return not critical


def main():
    today = datetime.now(IST).date()
    print(f"Connectivity test | {datetime.now(IST).isoformat(timespec='seconds')}")

    ok = True

    # 1. Participant OI CSV (walk back to first weekday)
    def participant():
        for i in range(1, 6):
            d = today - timedelta(days=i)
            if d.weekday() >= 5:
                continue
            url = ("https://archives.nseindia.com/content/nsccl/"
                   f"fao_participant_oi_{d.strftime('%d%m%Y')}.csv")
            try:
                st, raw = get(url)
                if b"Client" in raw and b"FII" in raw:
                    return f"{d} file, {len(raw)} bytes"
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    continue
                raise
        raise RuntimeError("no participant file found in last 5 days")

    ok &= probe("participant OI CSV (archives)", participant, critical=True)

    # 2. Bhavcopy (walk back)
    def bhavcopy():
        for i in range(0, 6):
            d = today - timedelta(days=i)
            if d.weekday() >= 5:
                continue
            url = ("https://archives.nseindia.com/content/fo/"
                   f"BhavCopy_NSE_FO_0_0_0_{d.strftime('%Y%m%d')}_F_0000.csv.zip")
            try:
                st, raw = get(url, timeout=40)
                zf = zipfile.ZipFile(io.BytesIO(raw))
                with zf.open(zf.namelist()[0]) as fh:
                    head = fh.read(400).decode("utf-8", "replace")
                if "TckrSymb" in head:
                    return f"{d} file, {len(raw)} bytes"
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    continue
                raise
        raise RuntimeError("no bhavcopy found in last 6 days")

    ok &= probe("F&O bhavcopy zip (archives)", bhavcopy, critical=True)

    # 3. FII/DII cash API
    def fiidii():
        st, raw = get("https://www.nseindia.com/api/fiidiiTradeReact",
                      referer="https://www.nseindia.com/reports/fii-dii")
        j = json.loads(raw)
        return f"{len(j)} rows, latest {j[0].get('date')}"

    ok &= probe("FII/DII cash API (www.nseindia)", fiidii, critical=True)

    # 4. Option chain contract-info
    def contract_info():
        st, raw = get("https://www.nseindia.com/api/option-chain-contract-info?symbol=NIFTY",
                      referer="https://www.nseindia.com/option-chain")
        j = json.loads(raw)
        return f"{len(j.get('expiryDates', []))} expiries"

    ok &= probe("option-chain-contract-info (www)", contract_info, critical=False)

    # 5. Live option chain (expected to throttle; informational)
    def live_chain():
        st, raw = get("https://www.nseindia.com/api/option-chain-v3?type=Indices&symbol=NIFTY",
                      referer="https://www.nseindia.com/option-chain")
        j = json.loads(raw)
        rec = j.get("records", {})
        if not rec.get("data"):
            raise RuntimeError("empty payload (throttled - bhavcopy fallback will be used)")
        return f"{len(rec.get('data', []))} strikes live"

    probe("live option-chain-v3 (best effort)", live_chain, critical=False)

    # 6. Yahoo Finance
    def yahoo():
        st, raw = get("https://query1.finance.yahoo.com/v8/finance/chart/%5EINDIAVIX"
                      "?range=5d&interval=1d", referer="https://finance.yahoo.com/")
        j = json.loads(raw)
        res = (j.get("chart", {}).get("result") or [None])[0]
        return f"{len(res['timestamp'])} bars" if res else "no result"

    ok &= probe("Yahoo Finance (India VIX)", yahoo, critical=False)

    print("\nRESULT:", "critical sources reachable" if ok else "CRITICAL SOURCES UNREACHABLE")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
