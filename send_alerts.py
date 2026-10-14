#!/usr/bin/env python3
"""
Alert engine for the NSE Derivatives Monitor.

Reads the latest monitor report (JSON), evaluates rules from alerts_config.json
(next to this script), applies per-rule cooldowns (data/alert_state.json), and
delivers fired alerts through whichever channels are configured:

  1. Telegram  - if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set
  2. GitHub issue - if GH_TOKEN (or GITHUB_TOKEN) and GITHUB_REPOSITORY are set
     (GitHub then emails the repository owner - no SMTP setup needed)
  3. output/alerts.log - always (audit trail, committed by the runner)

Usage:
  python send_alerts.py                # evaluate rules; deliver if triggered
  python send_alerts.py --dry-run      # print what would be sent; no delivery, no state change
  python send_alerts.py --test         # send a test alert through the configured channels
  python send_alerts.py --report PATH  # use a specific report JSON
"""

import argparse
import glob
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
DASHBOARD_URL = "https://posu303-tech.github.io/nse-deriv-monitor/"

DEFAULT_CONFIG = {
    "default_cooldown_days": 5,
    "rules": [
        {
            "name": "fii_futures_shift",
            "enabled": True,
            "threshold_contracts": 50000,
            "cooldown_days": 7,
            "description": "FII index-futures net position moved more than the threshold week-over-week"
        },
        {
            "name": "vix_rank_extreme",
            "enabled": True,
            "high": 85,
            "low": 15,
            "cooldown_days": 7,
            "description": "India VIX 1-year percentile rank in an extreme zone"
        },
        {
            "name": "iv_rv_gap",
            "enabled": True,
            "threshold_vol_pts": 5.0,
            "cooldown_days": 5,
            "description": "NIFTY ATM IV vs 21-day realized vol gap beyond threshold"
        },
        {
            "name": "fii_option_pcr_extreme",
            "enabled": True,
            "high": 1.5,
            "low": 0.6,
            "cooldown_days": 5,
            "description": "FII index-options put/call ratio in an extreme zone"
        },
    ],
}


def log(msg):
    print(msg, flush=True)


def load_config(path):
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(DEFAULT_CONFIG, fh, indent=2)
        log(f"  created default config: {path}")
        return DEFAULT_CONFIG
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as e:  # noqa: BLE001
        log(f"  WARN: config unreadable ({e}); using defaults")
        return DEFAULT_CONFIG


def load_state(path):
    try:
        with open(path, encoding="utf-8") as fh:
            s = json.load(fh)
            s.setdefault("rules", {})
            return s
    except Exception:
        return {"rules": {}}


def save_state(path, state):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)


def find_latest_report(out_dir, explicit=None):
    if explicit:
        return explicit if os.path.exists(explicit) else None
    files = sorted(glob.glob(os.path.join(out_dir, "monitor_*.json")),
                   key=os.path.getmtime)
    return files[-1] if files else None


# ------------------------------------------------------------------ rules

def evaluate(report, cfg):
    """-> list of {'rule', 'severity', 'message'}"""
    fired = []
    rules = {r["name"]: r for r in cfg.get("rules", [])}
    part = (report.get("participant") or {}).get("rows", {})
    fii = part.get("FII", {})

    r = rules.get("fii_futures_shift", {})
    if r.get("enabled", True):
        net, prev = fii.get("idx_fut_net"), fii.get("idx_fut_net_prev")
        if net is not None and prev is not None:
            delta = net - prev
            if abs(delta) >= r.get("threshold_contracts", 50000):
                direction = "reduced shorts / added longs" if delta > 0 else "added shorts / cut longs"
                fired.append({
                    "rule": "fii_futures_shift", "severity": "high",
                    "message": (f"FII index futures net moved {delta:+,.0f} contracts w/w "
                                f"(now {net:,.0f}; {direction})"),
                })

    r = rules.get("vix_rank_extreme", {})
    if r.get("enabled", True):
        vix = report.get("vix") or {}
        rank, last = vix.get("rank_1y"), vix.get("last")
        if rank is not None and last is not None:
            if rank >= r.get("high", 85):
                fired.append({
                    "rule": "vix_rank_extreme", "severity": "high",
                    "message": (f"India VIX {last:.2f} at {rank:.0f}th percentile (1y) - "
                                f"elevated fear / high-vol regime"),
                })
            elif rank <= r.get("low", 15):
                fired.append({
                    "rule": "vix_rank_extreme", "severity": "medium",
                    "message": (f"India VIX {last:.2f} at {rank:.0f}th percentile (1y) - "
                                f"complacency regime; cheap hedges"),
                })

    r = rules.get("iv_rv_gap", {})
    if r.get("enabled", True):
        iv = ((report.get("symbols") or {}).get("NIFTY") or {}).get("atm_iv")
        rv = (report.get("nifty_realvol") or {}).get("rv21")
        if iv is not None and rv is not None:
            gap = iv - rv
            if abs(gap) >= r.get("threshold_vol_pts", 5.0):
                side = "rich (premium-selling setup)" if gap > 0 else "cheap vs realized (long-vol setup)"
                fired.append({
                    "rule": "iv_rv_gap", "severity": "medium",
                    "message": (f"NIFTY ATM IV {iv:.2f}% vs realized vol (21d) {rv:.2f}% - "
                                f"{gap:+.2f} vol pts, options {side}"),
                })

    r = rules.get("fii_option_pcr_extreme", {})
    if r.get("enabled", True):
        pcr = fii.get("idx_opt_pcr")
        if pcr is not None:
            if pcr >= r.get("high", 1.5) or pcr <= r.get("low", 0.6):
                bias = "put-heavy (defensive/fear)" if pcr >= r.get("high", 1.5) else "call-heavy (aggressive)"
                fired.append({
                    "rule": "fii_option_pcr_extreme", "severity": "medium",
                    "message": f"FII index options PCR {pcr:.2f} - {bias}",
                })
    return fired


def apply_cooldowns(fired, cfg, state):
    rules = {r["name"]: r for r in cfg.get("rules", [])}
    default_cd = cfg.get("default_cooldown_days", 5)
    now = datetime.now(IST)
    out = []
    for f in fired:
        cd = rules.get(f["rule"], {}).get("cooldown_days", default_cd)
        last = state["rules"].get(f["rule"])
        if last:
            try:
                if (now - datetime.fromisoformat(last)) < timedelta(days=cd):
                    log(f"  suppressed (cooldown {cd}d): {f['rule']}")
                    continue
            except ValueError:
                pass
        out.append(f)
        state["rules"][f["rule"]] = now.isoformat(timespec="seconds")
    return out


# ------------------------------------------------------------------ delivery

def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return False
    try:
        data = urllib.parse.urlencode({
            "chat_id": chat, "text": text, "disable_web_page_preview": "true",
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data,
            headers={"User-Agent": "nse-deriv-monitor"})
        with urllib.request.urlopen(req, timeout=20) as r:
            ok = r.status == 200
        log(f"  telegram: {'sent' if ok else 'unexpected status'}")
        return ok
    except Exception as e:  # noqa: BLE001
        log(f"  telegram delivery failed: {type(e).__name__}: {e}")
        return False


def _github_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "nse-deriv-monitor",
    }


def send_github_issue(title, body):
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        return False
    api = f"https://api.github.com/repos/{repo}"
    hdrs = _github_headers(token)
    try:
        # ensure the 'alert' label exists (ignore 422 = already exists)
        try:
            req = urllib.request.Request(
                api + "/labels",
                data=json.dumps({"name": "alert", "color": "d73a4a",
                                 "description": "NSE monitor alert"}).encode(),
                headers=hdrs, method="POST")
            urllib.request.urlopen(req, timeout=15)
        except Exception:
            pass
        req = urllib.request.Request(
            api + "/issues",
            data=json.dumps({"title": title, "body": body, "labels": ["alert"]}).encode(),
            headers=hdrs, method="POST")
        with urllib.request.urlopen(req, timeout=20) as r:
            ok = r.status in (200, 201)
            loc = r.headers.get("Location", "")
        log(f"  github issue: {'created' if ok else 'unexpected status'} {loc}")
        return ok
    except Exception as e:  # noqa: BLE001
        log(f"  github issue delivery failed: {type(e).__name__}: {e}")
        return False


def build_message(fired, test=False):
    ts = datetime.now(IST).strftime("%d-%b-%Y %H:%M IST")
    head = "NSE Monitor TEST alert" if test else "NSE Monitor alert"
    lines = [f"{head} - {ts}", ""]
    for f in fired:
        lines.append(f"* {f['message']}")
    lines += ["", f"Dashboard: {DASHBOARD_URL}"]
    return "\n".join(lines)


# ------------------------------------------------------------------ main

def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    script_dir = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="NSE monitor alert engine")
    ap.add_argument("--data-dir", default=os.path.join(script_dir, "data"))
    ap.add_argument("--out-dir", default=os.path.join(script_dir, "output"))
    ap.add_argument("--report", default=None, help="explicit report JSON path")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test", action="store_true",
                    help="send a test alert through the configured channels")
    args = ap.parse_args()

    config_path = os.path.join(script_dir, "alerts_config.json")
    state_path = os.path.join(args.data_dir, "alert_state.json")
    log_path = os.path.join(args.out_dir, "alerts.log")
    alerts_json = os.path.join(args.out_dir, "alerts.json")

    cfg = load_config(config_path)
    state = load_state(state_path)

    report_path = find_latest_report(args.out_dir, args.report)
    report = {}
    if report_path:
        try:
            with open(report_path, encoding="utf-8") as fh:
                report = json.load(fh)
        except Exception as e:  # noqa: BLE001
            log(f"WARN: could not read report {report_path}: {e}")
    if not args.test and not report:
        log("no report found; nothing to evaluate")
        _write_json(alerts_json, {"fired": [], "note": "no report"})
        return 0

    if args.test:
        fired = [{"rule": "test_alert", "severity": "info",
                  "message": "Test alert - delivery pipeline is working. "
                             "Real alerts fire on FII positioning shifts, VIX-rank "
                             "extremes, IV/realized gaps and PCR extremes."}]
    else:
        log(f"evaluating: {os.path.basename(report_path)}")
        fired = evaluate(report, cfg)
        fired = apply_cooldowns(fired, cfg, state)

    if not fired:
        log("no alerts fired")
        _write_json(alerts_json, {"fired": [], "evaluated": report_path})
        return 0

    text = build_message(fired, test=args.test)
    log("")
    log(text)
    log("")

    fired_names = [f["rule"] for f in fired]
    if args.dry_run:
        log("[dry-run] no delivery, no state change")
        _write_json(alerts_json, {"fired": fired_names, "dry_run": True})
        return 0

    delivered = []
    if send_telegram(text):
        delivered.append("telegram")
    title = ("TEST - " if args.test else "") + \
            f"NSE monitor alert: {', '.join(fired_names)}"
    if send_github_issue(title, text):
        delivered.append("github-issue")

    # always keep an audit trail
    os.makedirs(args.out_dir, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as fh:
        stamp = datetime.now(IST).isoformat(timespec="seconds")
        fh.write(f"{stamp} {'TEST ' if args.test else ''}fired={fired_names} "
                 f"delivered={delivered or ['log-only']}\n")
        for f in fired:
            fh.write(f"    {f['rule']}: {f['message']}\n")

    if not delivered:
        log("  no delivery channel configured (set TELEGRAM_* secrets or GH_TOKEN) "
            "- alert recorded in output/alerts.log only")

    if not args.test:
        save_state(state_path, state)  # cooldowns advance even if only logged
    _write_json(alerts_json, {"fired": fired_names, "delivered": delivered,
                              "at": datetime.now(IST).isoformat(timespec="seconds")})
    return 0


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2)


if __name__ == "__main__":
    sys.exit(main())
