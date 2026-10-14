#!/usr/bin/env python3
"""
Weekly digest for the NSE Derivatives Monitor (Fridays, IST).

Contents:
  - 5-session changes in participant positioning (net index futures + option PCR)
  - 5-day cumulative FII/DII cash flows
  - Volatility week: India VIX change / week range / rank, per-symbol IV changes
  - Rollover context: near/next month futures OI and settle, roll %, roll spread
    (carry), plus 5-session changes - the standard month-end rollover analytics

Outputs:
  output/weekly_YYYYMMDD.html / .json   (dated copies)
  output/weekly.html                    (latest, published to the dashboard)

Delivery (per alerts_config.json -> "weekly_digest"):
  - Telegram (if TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID set)
  - GitHub issue labeled 'digest' (if GH_TOKEN + GITHUB_REPOSITORY set)
  Both default ON; disable via config or --no-deliver.

Usage:
  python weekly_digest.py               # generate only on Fridays (IST)
  python weekly_digest.py --force       # generate regardless of weekday
  python weekly_digest.py --no-deliver  # generate files only
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nse_deriv_monitor as mon  # noqa: E402  (helpers: IST, parse_ymd, fmt, ...)

try:
    from send_alerts import send_telegram, send_github_issue  # noqa: E402
except Exception:  # noqa: BLE001
    send_telegram = None
    send_github_issue = None

IST = mon.IST
N_SESSIONS = 5


# ---------------------------------------------------------------- data blocks

def participant_block(conn, n=N_SESSIONS):
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM participant_oi ORDER BY date")]
    if not dates:
        return None
    cur_d = dates[-1]
    ref_d = dates[-n - 1] if len(dates) > n else (dates[0] if len(dates) > 1 else None)

    def snap(d):
        out = {}
        if not d:
            return out
        for p, il, is_, cl, pl, cs, ps in conn.execute(
                "SELECT participant, idx_fut_long, idx_fut_short, idx_opt_call_long, "
                "idx_opt_put_long, idx_opt_call_short, idx_opt_put_short "
                "FROM participant_oi WHERE date=?", (d,)):
            ce = (cl or 0) + (cs or 0)
            pe = (pl or 0) + (ps or 0)
            out[p] = {"net": (il or 0) - (is_ or 0),
                      "pcr": round(pe / ce, 2) if ce else None}
        return out

    cur, ref = snap(cur_d), snap(ref_d)
    rows = {}
    for p in mon.PARTICIPANTS:
        c, r = cur.get(p, {}), ref.get(p, {})
        rows[p] = {
            "net": c.get("net"),
            "net_chg": (c.get("net") - r["net"])
            if (c.get("net") is not None and r.get("net") is not None) else None,
            "pcr": c.get("pcr"),
            "pcr_chg": round(c.get("pcr") - r.get("pcr"), 2)
            if (c.get("pcr") is not None and r.get("pcr") is not None) else None,
        }
    return {"cur_date": cur_d, "ref_date": ref_d, "rows": rows}


def cash_block(conn, n=N_SESSIONS):
    rows = list(conn.execute("SELECT date, category, net FROM cash_flow ORDER BY date"))
    dates = sorted({r[0] for r in rows})[-n:]
    tot = {}
    for d, cat, net in rows:
        if d in dates and net is not None:
            tot[cat] = tot.get(cat, 0.0) + net
    return {"dates": dates, "totals": tot}


def vix_block(data_dir, n=N_SESSIONS):
    try:
        with open(os.path.join(data_dir, "india_vix_cache.json"), encoding="utf-8") as fh:
            series = json.load(fh).get("series", [])
    except Exception:
        return None
    vals = [h["close"] for h in series]
    if len(vals) < 2:
        return None
    cur = vals[-1]
    ref = vals[-n - 1] if len(vals) > n else vals[0]
    week = vals[-n:]
    rank, _ = mon.rank_pct(vals[-252:], cur)
    return {"last": cur, "chg": cur - ref, "week_min": min(week),
            "week_max": max(week), "rank": rank}


def symbols_block(conn, symbols, n=N_SESSIONS):
    out = {}
    for sym in symbols:
        rows = conn.execute(
            "SELECT date, spot, atm_iv, pcr FROM iv_snapshot WHERE symbol=? ORDER BY date",
            (sym,)).fetchall()
        if not rows:
            continue
        c = rows[-1]
        e = {"date": c[0], "spot": c[1], "atm_iv": c[2], "pcr": c[3]}
        ref = rows[-n - 1] if len(rows) > n else (rows[0] if len(rows) > 1 else None)
        if ref:
            e["ref_date"] = ref[0]
            e["spot_chg_pct"] = (c[1] / ref[1] - 1) * 100 if c[1] and ref[1] else None
            e["iv_chg"] = (c[2] - ref[2]) if c[2] and ref[2] else None
            e["pcr_chg"] = (c[3] - ref[3]) if c[3] and ref[3] else None
        out[sym] = e
    return out


def rollover_block(conn, symbols, n=N_SESSIONS):
    """Near/next month futures: roll %, roll spread (carry), 5-session changes."""
    out = {}
    for sym in symbols:
        rows = list(conn.execute(
            "SELECT date, expiry, settle, oi FROM fut_snapshot WHERE symbol=? ORDER BY date",
            (sym,)))
        if not rows:
            out[sym] = {"missing": True}
            continue
        dates = sorted({r[0] for r in rows})
        cur_d = dates[-1]
        per_date = {}
        for d, e, s, oi in rows:
            per_date.setdefault(d, {})[e] = {"settle": s, "oi": oi}
        cur = per_date[cur_d]
        exps = [e for e in sorted(cur) if e > cur_d]
        if len(exps) < 2:
            out[sym] = {"missing": True}
            continue
        near_e, next_e = exps[0], exps[1]
        near, nxt = cur[near_e], cur[next_e]
        e = {"date": cur_d, "near_expiry": near_e, "next_expiry": next_e,
             "days_to_expiry": (mon.parse_ymd(near_e) - mon.parse_ymd(cur_d)).days,
             "near_oi": near["oi"], "next_oi": nxt["oi"],
             "near_settle": near["settle"], "next_settle": nxt["settle"]}
        if near["oi"] and nxt["oi"] and (near["oi"] + nxt["oi"]) > 0:
            e["roll_pct"] = nxt["oi"] / (near["oi"] + nxt["oi"]) * 100
        if near["settle"] and nxt["settle"]:
            e["spread"] = nxt["settle"] - near["settle"]
            e["spread_pct"] = e["spread"] / near["settle"] * 100
        if len(dates) > n:
            ref = per_date.get(dates[-n - 1], {})
            if near_e in ref and next_e in ref:
                if near["oi"] is not None and ref[near_e]["oi"] is not None:
                    e["near_oi_chg"] = near["oi"] - ref[near_e]["oi"]
                    e["next_oi_chg"] = nxt["oi"] - ref[next_e]["oi"]
                    tot0 = ref[near_e]["oi"] + ref[next_e]["oi"]
                    if tot0:
                        e["roll_pct_prev"] = ref[next_e]["oi"] / tot0 * 100
                if ref[near_e]["settle"] and ref[next_e]["settle"]:
                    e["spread_prev"] = ref[next_e]["settle"] - ref[near_e]["settle"]
        # thin next-month contracts produce unreliable settlement spreads
        if near["oi"] and nxt["oi"] is not None:
            e["thin_next"] = nxt["oi"] < 5000 or nxt["oi"] < 0.02 * near["oi"]
        e["note"] = rollover_note(e)
        out[sym] = e
    return out


def rollover_note(e):
    bits = []
    rp, rp0 = e.get("roll_pct"), e.get("roll_pct_prev")
    if rp is not None and rp0 is not None:
        d = rp - rp0
        if d > 2:
            bits.append("roll-in accelerating")
        elif d < -2:
            bits.append("roll-in slowing")
    ni, nn = e.get("near_oi_chg"), e.get("next_oi_chg")
    if ni is not None and nn is not None:
        if ni < 0 and nn > 0:
            bits.append("positions carried to next month")
        elif ni < 0 and nn <= 0 and abs(ni) > 3 * abs(nn):
            bits.append("net unwinding")
    sp, sp0 = e.get("spread"), e.get("spread_prev")
    if sp is not None and sp0 is not None and not e.get("thin_next"):
        if sp - sp0 > 10:
            bits.append("carry premium widened")
        elif sp0 - sp > 10:
            bits.append("carry premium narrowed")
    if e.get("thin_next"):
        bits.append("next-month OI thin (spread unreliable)")
    return "; ".join(bits) if bits else "steady"


# ---------------------------------------------------------------- rendering

def build_payload(data_dir, session_symbols):
    db = os.path.join(data_dir, "nse_monitor.db")
    conn = sqlite3.connect(db)
    try:
        pb = participant_block(conn)
        cb = cash_block(conn)
        vb = vix_block(data_dir)
        sb = symbols_block(conn, session_symbols)
        rb = rollover_block(conn, session_symbols)
    finally:
        conn.close()

    now = datetime.now(IST)
    payload = {
        "date": now.date().isoformat(),
        "generated_at": now.isoformat(timespec="seconds"),
        "week_span": {"from": pb["ref_date"] if pb else None, "to": pb["cur_date"] if pb else None},
        "participant": pb, "cash": cb, "vix": vb, "symbols": sb, "rollover": rb,
    }
    payload["headline"] = headline(pb, vb, rb)
    return payload


def headline(pb, vb, roll):
    bits = []
    if pb:
        fii = (pb.get("rows") or {}).get("FII") or {}
        c = fii.get("net_chg")
        if c is not None:
            if abs(c) < 2000:
                bits.append("FII futures flat")
            else:
                bits.append("FII covering" if c > 0 else "FII adding shorts")
    if vb:
        bits.append(f"VIX {vb['last']:.2f} ({vb['chg']:+.2f})")
    for sym in ("NIFTY", "BANKNIFTY"):
        r = roll.get(sym) or {}
        if r.get("roll_pct") is not None:
            tag = f"{sym} roll {r['roll_pct']:.0f}%"
            if r.get("roll_pct_prev") is not None:
                tag += f" (prev {r['roll_pct_prev']:.0f}%)"
            bits.append(tag)
    return "; ".join(bits) if bits else "not enough history yet"


def render_text(p):
    L = []
    L.append(f"NSE WEEKLY DIGEST - {p['date']}")
    L.append(f"Headline: {p['headline']}")
    pb = p.get("participant")
    if pb:
        L.append("")
        L.append(f"POSITIONING (5 sessions: {pb['ref_date']} -> {pb['cur_date']})")
        for name in mon.PARTICIPANTS:
            r = pb["rows"].get(name, {})
            if r.get("net") is None:
                continue
            line = f"  {name:<7} net {mon.fmt(r['net'], 0):>10}"
            if r.get("net_chg") is not None:
                line += f" ({mon.signed(r['net_chg'], 0)})"
            if r.get("pcr") is not None:
                line += f"   PCR {r['pcr']:.2f}"
                if r.get("pcr_chg") is not None:
                    line += f" ({r['pcr_chg']:+.2f})"
            L.append(line)
    cb = p.get("cash")
    if cb and cb.get("dates"):
        fii = cb["totals"].get("FII/FPI")
        dii = cb["totals"].get("DII")
        L.append("")
        L.append(f"CASH (cumulative, {len(cb['dates'])} sessions): "
                 f"FII {mon.signed(fii)} cr | DII {mon.signed(dii)} cr")
    vb = p.get("vix")
    if vb:
        L.append("")
        L.append(f"VOLATILITY: India VIX {vb['last']:.2f} ({vb['chg']:+.2f}), "
                 f"week range {vb['week_min']:.2f}-{vb['week_max']:.2f}, "
                 f"1y rank {mon.fmt(vb.get('rank'), 0)}")
    for sym, e in (p.get("symbols") or {}).items():
        if e.get("atm_iv") is None:
            continue
        seg = f"  {sym:<11} IV {e['atm_iv']:.2f}%"
        if e.get("iv_chg") is not None:
            seg += f" ({e['iv_chg']:+.2f})"
        if e.get("pcr") is not None:
            seg += f" | PCR {e['pcr']:.2f}"
            if e.get("pcr_chg") is not None:
                seg += f" ({e['pcr_chg']:+.2f})"
        if e.get("spot_chg_pct") is not None:
            seg += f" | spot {e['spot_chg_pct']:+.1f}%"
        L.append(seg)
    rb = p.get("rollover")
    if rb:
        L.append("")
        L.append("ROLLOVER (futures, near -> next month)")
        for sym, e in rb.items():
            if e.get("missing") or e.get("roll_pct") is None:
                continue
            seg = (f"  {sym:<11} {e['near_expiry']} in {e['days_to_expiry']}d | "
                   f"roll {e['roll_pct']:.0f}%")
            if e.get("roll_pct_prev") is not None:
                seg += f" (prev {e['roll_pct_prev']:.0f}%)"
            if e.get("spread") is not None:
                seg += f" | spread {e['spread']:+.1f}pts ({e['spread_pct']:+.2f}%)"
            seg += f" | {e['note']}"
            L.append(seg)
    L.append("")
    L.append("Dashboard: https://posu303-tech.github.io/nse-deriv-monitor/")
    return "\n".join(L)


def render_markdown(p):
    L = [f"### NSE Weekly Digest - {p['date']}", "",
         f"**{p['headline']}**", ""]
    pb = p.get("participant")
    if pb:
        L.append(f"**Positioning** (5 sessions: {pb['ref_date']} -> {pb['cur_date']})")
        L.append("")
        L.append("| Participant | Net fut | 5s change | PCR | PCR chg |")
        L.append("|---|---|---|---|---|")
        for name in mon.PARTICIPANTS:
            r = pb["rows"].get(name, {})
            L.append(f"| {name} | {mon.fmt(r.get('net'), 0)} | "
                     f"{mon.signed(r.get('net_chg'), 0)} | {mon.fmt(r.get('pcr'), 2)} | "
                     f"{mon.signed(r.get('pcr_chg'), 2)} |")
        L.append("")
    cb = p.get("cash")
    if cb and cb.get("dates"):
        L.append(f"**Cash** ({cb['dates'][0]} .. {cb['dates'][-1]}): "
                 f"FII {mon.signed(cb['totals'].get('FII/FPI'))} cr | "
                 f"DII {mon.signed(cb['totals'].get('DII'))} cr")
        L.append("")
    vb = p.get("vix")
    if vb:
        L.append(f"**Volatility**: India VIX {vb['last']:.2f} ({vb['chg']:+.2f}), "
                 f"week range {vb['week_min']:.2f}-{vb['week_max']:.2f}, "
                 f"1y rank {mon.fmt(vb.get('rank'), 0)}")
        L.append("")
    rb = p.get("rollover")
    if rb:
        L.append("**Rollover**")
        L.append("")
        L.append("| Symbol | Near expiry (d) | Roll % | prev | Spread (pts) | 5s near OI Δ | 5s next OI Δ | Note |")
        L.append("|---|---|---|---|---|---|---|---|")
        for sym, e in rb.items():
            if e.get("missing") or e.get("roll_pct") is None:
                continue
            L.append(f"| {sym} | {e['near_expiry']} ({e['days_to_expiry']}d) | "
                     f"{e['roll_pct']:.0f}% | "
                     f"{mon.fmt(e.get('roll_pct_prev'), 0)}% | "
                     f"{mon.signed(e.get('spread'), 1)} | "
                     f"{mon.signed(e.get('near_oi_chg'), 0)} | "
                     f"{mon.signed(e.get('next_oi_chg'), 0)} | {e['note']} |")
        L.append("")
    L.append("[Dashboard](https://posu303-tech.github.io/nse-deriv-monitor/) | "
             "[Trends](https://posu303-tech.github.io/nse-deriv-monitor/trends.html)")
    return "\n".join(L)


def render_html(p):
    def esc(x):
        return str(x).replace("&", "&amp;").replace("<", "&lt;")

    parts = ["""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NSE Weekly Digest</title><style>
body{background:#0f1420;color:#e8edf7;font-family:Segoe UI,Arial,sans-serif;margin:28px;max-width:1080px}
h1{font-size:22px;margin-bottom:2px} h2{font-size:15px;color:#9fb3d1;border-bottom:1px solid #26314a;
padding-bottom:6px;margin-top:26px}
a{color:#6ea8fe} .muted{color:#7d8fa9;font-size:12px}
.hl{background:#182035;border-left:3px solid #6ea8fe;border-radius:8px;padding:10px 14px;
margin:14px 0;font-size:14px;font-weight:600}
table{border-collapse:collapse;width:100%;margin:8px 0}
th,td{padding:7px 10px;border-bottom:1px solid #26314a;text-align:right;font-size:13px}
th{color:#9fb3d1} td:first-child,th:first-child{text-align:left}
.note{color:#9fb3d1;text-align:left}
</style></head><body>"""]
    parts.append(f"<h1>NSE Weekly Digest</h1><div class='muted'>Week to "
                 f"{esc(p['date'])} &middot; generated {esc(p['generated_at'])} &middot; "
                 f"<a href='index.html'>daily report</a> &middot; "
                 f"<a href='trends.html'>trends</a></div>")
    parts.append(f"<div class='hl'>{esc(p['headline'])}</div>")

    pb = p.get("participant")
    if pb:
        parts.append(f"<h2>Positioning (5 sessions: {esc(pb['ref_date'])} &rarr; "
                     f"{esc(pb['cur_date'])})</h2>")
        parts.append("<table><tr><th>Participant</th><th>Net fut</th><th>5s change</th>"
                     "<th>PCR</th><th>PCR chg</th></tr>")
        for name in mon.PARTICIPANTS:
            r = pb["rows"].get(name, {})
            parts.append(f"<tr><td>{name}</td><td>{mon.fmt(r.get('net'),0)}</td>"
                         f"<td>{mon.signed(r.get('net_chg'),0)}</td>"
                         f"<td>{mon.fmt(r.get('pcr'),2)}</td>"
                         f"<td>{mon.signed(r.get('pcr_chg'),2)}</td></tr>")
        parts.append("</table>")

    cb = p.get("cash")
    if cb and cb.get("dates"):
        parts.append(f"<h2>Cash market (cumulative, {len(cb['dates'])} sessions)</h2>")
        parts.append(f"<p>FII/FPI net <b>{mon.signed(cb['totals'].get('FII/FPI'))} cr</b> "
                     f"&middot; DII net <b>{mon.signed(cb['totals'].get('DII'))} cr</b></p>")

    vb = p.get("vix")
    if vb:
        parts.append("<h2>Volatility</h2>")
        parts.append(f"<p>India VIX <b>{vb['last']:.2f}</b> ({vb['chg']:+.2f} w/w) "
                     f"&middot; week range {vb['week_min']:.2f}-{vb['week_max']:.2f} "
                     f"&middot; 1y rank {mon.fmt(vb.get('rank'),0)}</p>")
    sb = p.get("symbols") or {}
    any_sym = any(e.get("atm_iv") is not None for e in sb.values())
    if any_sym:
        parts.append("<table><tr><th>Symbol</th><th>ATM IV</th><th>5s chg</th>"
                     "<th>PCR</th><th>5s chg</th><th>Spot 5s</th></tr>")
        for sym, e in sb.items():
            if e.get("atm_iv") is None:
                continue
            parts.append(f"<tr><td>{esc(sym)}</td><td>{e['atm_iv']:.2f}%</td>"
                         f"<td>{mon.signed(e.get('iv_chg'),2)}</td>"
                         f"<td>{mon.fmt(e.get('pcr'),2)}</td>"
                         f"<td>{mon.signed(e.get('pcr_chg'),2)}</td>"
                         f"<td>{mon.signed(e.get('spot_chg_pct'),1)}%</td></tr>")
        parts.append("</table>")

    rb = p.get("rollover")
    if rb:
        parts.append("<h2>Rollover context (futures, near &rarr; next month)</h2>")
        parts.append("<table><tr><th>Symbol</th><th>Near expiry</th><th>Roll %</th><th>prev</th>"
                     "<th>Spread (pts)</th><th>5s near OI &Delta;</th><th>5s next OI &Delta;</th>"
                     "<th>Note</th></tr>")
        for sym, e in rb.items():
            if e.get("missing") or e.get("roll_pct") is None:
                continue
            parts.append(
                f"<tr><td>{esc(sym)}</td><td>{esc(e['near_expiry'])} ({e['days_to_expiry']}d)</td>"
                f"<td>{e['roll_pct']:.0f}%</td>"
                f"<td>{mon.fmt(e.get('roll_pct_prev'),0)}%</td>"
                f"<td>{mon.signed(e.get('spread'),1)}</td>"
                f"<td>{mon.signed(e.get('near_oi_chg'),0)}</td>"
                f"<td>{mon.signed(e.get('next_oi_chg'),0)}</td>"
                f"<td class='note'>{esc(e['note'])}</td></tr>")
        parts.append("</table>")
        parts.append("<p class='muted'>Roll % = next-month OI / (near + next OI). Spread = next minus "
                     "near settle (positive = carry premium). Notes are rule-based readings.</p>")

    parts.append("<p class='muted'>Sources: NSE participant-wise OI archive, NSE F&amp;O bhavcopy. "
                 "Educational tool, not investment advice.</p></body></html>")
    return "".join(parts)


# ---------------------------------------------------------------- main

def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    script_dir = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="NSE weekly digest (Fridays)")
    ap.add_argument("--data-dir", default=os.path.join(script_dir, "data"))
    ap.add_argument("--out-dir", default=os.path.join(script_dir, "output"))
    ap.add_argument("--symbols", nargs="+", default=mon.DEFAULT_SYMBOLS)
    ap.add_argument("--force", action="store_true",
                    help="generate regardless of weekday (for testing)")
    ap.add_argument("--no-deliver", action="store_true",
                    help="write files only; skip Telegram / GitHub issue delivery")
    args = ap.parse_args()

    now = datetime.now(IST)
    if not args.force and now.weekday() != 4:  # Monday=0 ... Friday=4
        print(f"weekly digest: not Friday in IST ({now.strftime('%A')}) - skipping "
              f"(use --force to generate)")
        return 0

    if not os.path.exists(os.path.join(args.data_dir, "nse_monitor.db")):
        print("weekly digest: no database - run the monitor first")
        return 1

    payload = build_payload(args.data_dir, args.symbols)
    text = render_text(payload)
    markdown = render_markdown(payload)
    html = render_html(payload)
    payload["text"] = text

    os.makedirs(args.out_dir, exist_ok=True)
    stamp = now.strftime("%Y%m%d")
    dated_html = os.path.join(args.out_dir, f"weekly_{stamp}.html")
    dated_json = os.path.join(args.out_dir, f"weekly_{stamp}.json")
    latest_html = os.path.join(args.out_dir, "weekly.html")
    for path, content in ((dated_html, html), (latest_html, html)):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
    with open(dated_json, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)

    print(text)
    print(f"\nSaved: {dated_html}")
    print(f"Saved: {dated_json}")

    # ------------------------------------------------------------ delivery
    cfg = {}
    cfg_path = os.path.join(script_dir, "alerts_config.json")
    try:
        with open(cfg_path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except Exception:  # noqa: BLE001
        pass
    wd = cfg.get("weekly_digest", {})
    deliver = not args.no_deliver
    delivered = []
    if deliver and wd.get("deliver_telegram", True) and send_telegram:
        if send_telegram(text):
            delivered.append("telegram")
    if deliver and wd.get("deliver_github_issue", True) and send_github_issue:
        title = f"NSE weekly digest: {payload['date']}"
        if send_github_issue(title, markdown, label="digest", color="0e8a16"):
            delivered.append("github-issue")
    if deliver and not delivered:
        print("digest delivery: no channel configured (Telegram secrets / GH_TOKEN) "
              "- files only")

    log_path = os.path.join(args.out_dir, "alerts.log")
    try:
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(f"{payload['generated_at']} weekly-digest fired "
                     f"delivered={delivered or ['log-only']}\n")
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
