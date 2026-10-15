#!/usr/bin/env python3
"""
Monthly rollover special for the NSE Derivatives Monitor.

Activates around monthly expiry week (default: within 7 calendar days of the
nearest monthly futures expiry). Regenerated on every run inside the window,
so the page tracks the roll in real time; deliveries fire twice per cycle:
  - when the window opens  ("rollover week begins")
  - on the last session before expiry ("final roll")

Contents:
  - Current roll state per symbol: roll %, roll spread (carry), 5-session OI changes
  - Roll trajectory: roll % by session over the last ~10 sessions
  - Cycle history: final roll % and roll % at the same days-to-expiry for the last
    N completed cycles (default 4), with averages - the sell-side style comparison
  - Positioning into expiry (participant 5-session changes)
  - Volatility into expiry (India VIX rank, ATM IV term structure)

Outputs:
  output/rollover_YYYYMMDD.html / .json  and  output/rollover.html (latest)

Usage:
  python rollover_special.py                  # generate only inside the window
  python rollover_special.py --force          # generate regardless (testing)
  python rollover_special.py --deliver-test   # send a test delivery to the channels
  python rollover_special.py --no-deliver     # files only
"""

import argparse
import json
import os
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nse_deriv_monitor as mon  # noqa: E402
import weekly_digest as wd  # noqa: E402

try:
    from send_alerts import send_telegram, send_github_issue  # noqa: E402
except Exception:  # noqa: BLE001
    send_telegram = None
    send_github_issue = None

IST = mon.IST


# ---------------------------------------------------------------- data

def load_per_date(conn, sym):
    per_date = {}
    for d, e, s, oi in conn.execute(
            "SELECT date, expiry, settle, oi FROM fut_snapshot WHERE symbol=? "
            "ORDER BY date", (sym,)):
        per_date.setdefault(d, {})[e] = {"settle": s, "oi": oi}
    return per_date


def roll_stats_from(per_date, d, expiry_iso=None):
    """Roll %/spread on date d; optionally anchored to a specific near expiry."""
    day = per_date.get(d)
    if not day:
        return None
    if expiry_iso:
        if expiry_iso not in day:
            return None
        later = [e for e in sorted(day) if e > expiry_iso]
        if not later:
            return None
        near, nxt = expiry_iso, later[0]
    else:
        exps = [e for e in sorted(day) if e > d]
        if len(exps) < 2:
            return None
        near, nxt = exps[0], exps[1]
    ni, nni = day[near]["oi"], day[nxt]["oi"]
    ns, nns = day[near]["settle"], day[nxt]["settle"]
    out = {"date": d, "near_expiry": near, "next_expiry": nxt,
           "dte": (mon.parse_ymd(near) - mon.parse_ymd(d)).days,
           "near_oi": ni, "next_oi": nni,
           "near_settle": ns, "next_settle": nns}
    if ni and nni and (ni + nni) > 0:
        out["roll_pct"] = nni / (ni + nni) * 100
    if ns and nns:
        out["spread"] = nns - ns
    return out


def roll_trajectory(conn, sym, days=10):
    per_date = load_per_date(conn, sym)
    out = []
    for d in sorted(per_date)[-days:]:
        st = roll_stats_from(per_date, d)
        if st and st.get("roll_pct") is not None:
            out.append(st)
    return out


def roll_on_or_before(conn, sym, target_date, expiry_iso):
    per_date = load_per_date(conn, sym)
    for d in sorted(per_date, reverse=True):
        if d > target_date:
            continue
        st = roll_stats_from(per_date, d, expiry_iso=expiry_iso)
        if st:
            return st
    return None


def cycle_history(conn, sym, anchor, dte, n_cycles=4):
    """Last N completed cycles: final roll % and roll % at the current T-offset."""
    expiries = [e for (e,) in conn.execute(
        "SELECT DISTINCT expiry FROM fut_snapshot WHERE symbol=?", (sym,)) if e < anchor]
    if not expiries:
        return []
    out = []
    for e in sorted(expiries)[-n_cycles:][::-1]:  # most recent cycle first
        e_d = mon.parse_ymd(e)
        fin = roll_on_or_before(conn, sym, (e_d - timedelta(days=1)).isoformat(), e)
        off = roll_on_or_before(conn, sym, (e_d - timedelta(days=dte)).isoformat(), e)
        if fin or off:
            out.append({"expiry": e, "final": fin, "at_offset": off})
    return out


def cycle_averages(hist):
    finals = [h["final"]["roll_pct"] for h in hist
              if h.get("final") and h["final"].get("roll_pct") is not None]
    offs = [h["at_offset"]["roll_pct"] for h in hist
            if h.get("at_offset") and h["at_offset"].get("roll_pct") is not None]
    return {
        "final_avg": statistics.fmean(finals) if finals else None,
        "final_n": len(finals),
        "offset_avg": statistics.fmean(offs) if offs else None,
        "offset_n": len(offs),
    }


def cycle_anchor(conn, symbols):
    """Nearest monthly futures expiry (min across symbols at the latest data date)."""
    latest = conn.execute("SELECT MAX(date) FROM fut_snapshot").fetchone()[0]
    if not latest:
        return None, None
    expiries = set()
    for sym in symbols:
        for (e,) in conn.execute(
                "SELECT DISTINCT expiry FROM fut_snapshot WHERE symbol=? AND date=?",
                (sym, latest)):
            if e > latest:
                expiries.add(e)
    if not expiries:
        return None, latest
    return min(expiries), latest


def term_structure(conn, symbols):
    out = {}
    for sym in symbols:
        row = conn.execute(
            "SELECT term_structure FROM iv_snapshot WHERE symbol=? ORDER BY date DESC LIMIT 1",
            (sym,)).fetchone()
        if not row or not row[0]:
            continue
        try:
            ts = json.loads(row[0])
        except Exception:  # noqa: BLE001
            continue
        items = sorted(ts.items())[:2]
        if items:
            out[sym] = items
    return out


def build_payload(data_dir, symbols, n_cycles=4):
    db = os.path.join(data_dir, "nse_monitor.db")
    conn = sqlite3.connect(db)
    try:
        anchor, last_date = cycle_anchor(conn, symbols)
        if not anchor:
            return None
        today = datetime.now(IST).date()
        dte = (mon.parse_ymd(anchor) - today).days

        current = wd.rollover_block(conn, symbols)
        trajectories = {sym: roll_trajectory(conn, sym, days=10) for sym in symbols}

        history = {}
        for sym in symbols:
            hist = cycle_history(conn, sym, anchor, dte, n_cycles=n_cycles)
            if hist:
                history[sym] = {"cycles": hist, "avg": cycle_averages(hist)}

        payload = {
            "date": today.isoformat(),
            "generated_at": datetime.now(IST).isoformat(timespec="seconds"),
            "anchor_expiry": anchor,
            "days_to_expiry": dte,
            "last_data_date": last_date,
            "current": current,
            "trajectories": trajectories,
            "cycle_history": history,
            "positioning": wd.participant_block(conn),
            "vix": wd.vix_block(data_dir),
            "term_structure": term_structure(conn, symbols),
        }
    finally:
        conn.close()
    payload["headline"] = headline(payload)
    return payload


def headline(p):
    dte = p["days_to_expiry"]
    bits = [f"Monthly rollover into {p['anchor_expiry']} - {dte} calendar days left"]
    nifty = (p.get("current") or {}).get("NIFTY") or {}
    if nifty.get("roll_pct") is not None:
        seg = f"NIFTY roll {nifty['roll_pct']:.0f}%"
        if nifty.get("roll_pct_prev") is not None:
            seg += f" (5s {nifty['roll_pct_prev']:.0f}%)"
        avg = ((p.get("cycle_history") or {}).get("NIFTY") or {}).get("avg") or {}
        if avg.get("offset_avg") is not None:
            seg += f"; {avg['offset_n']}-cycle avg at T-{dte}: {avg['offset_avg']:.0f}%"
        bits.append(seg)
    return "; ".join(bits)


# ---------------------------------------------------------------- rendering

def render_text(p):
    L = [f"NSE MONTHLY ROLLOVER SPECIAL - {p['date']}",
         f"Headline: {p['headline']}", ""]
    L.append(f"CURRENT STATE (data {p['last_data_date']}, expiry {p['anchor_expiry']})")
    for sym, e in (p.get("current") or {}).items():
        if e.get("missing") or e.get("roll_pct") is None:
            continue
        seg = (f"  {sym:<11} roll {e['roll_pct']:.0f}%")
        if e.get("roll_pct_prev") is not None:
            seg += f" (5s {e['roll_pct_prev']:.0f}%)"
        if e.get("spread") is not None:
            seg += f" | spread {e['spread']:+.1f}pts ({e.get('spread_pct', 0):+.2f}%)"
        if e.get("near_oi_chg") is not None:
            seg += f" | 5s nearOI {mon.signed(e['near_oi_chg'], 0)}, nextOI {mon.signed(e.get('next_oi_chg'), 0)}"
        seg += f" | {e.get('note', '')}"
        L.append(seg)
    L.append("")
    L.append("ROLL TRAJECTORY (roll %, last sessions)")
    traj = p.get("trajectories") or {}
    dates = sorted({st["date"] for st in traj.get("NIFTY", [])})[-8:]
    syms = [s for s in traj if traj[s]]
    if dates and syms:
        header = "  " + "session".ljust(12) + "".join(f"{s[:9]:>11}" for s in syms)
        L.append(header)
        for d in dates:
            row = "  " + d.ljust(12)
            for s in syms:
                val = next((st["roll_pct"] for st in traj[s] if st["date"] == d), None)
                row += f"{val:>10.0f}%" if val is not None else f"{'-':>11}"
            L.append(row)
    hist = p.get("cycle_history") or {}
    if hist:
        L.append("")
        L.append(f"ROLL HISTORY (final % | at T-{p['days_to_expiry']}d %)")
        syms = list(hist.keys())
        L.append("  " + "cycle".ljust(12) + "".join(f"{s[:10]:>12}" for s in syms))

        def cell(f_, o_):
            if f_ is None and o_ is None:
                return "-"
            fs = f"{f_:.0f}" if f_ is not None else "-"
            os_ = f"{o_:.0f}" if o_ is not None else "-"
            return f"{fs}|{os_}"

        cycles = sorted({c["expiry"] for h in hist.values() for c in h["cycles"]},
                        reverse=True)
        for e in cycles:
            row = "  " + e.ljust(12)
            for s in syms:
                c = next((c for c in hist[s]["cycles"] if c["expiry"] == e), None)
                f_ = (c.get("final") or {}).get("roll_pct") if c else None
                o_ = (c.get("at_offset") or {}).get("roll_pct") if c else None
                row += f"{cell(f_, o_):>12}"
            L.append(row)
        row = "  " + "AVE".ljust(12)
        for s in syms:
            a = hist[s]["avg"]
            row += f"{cell(a.get('final_avg'), a.get('offset_avg')):>12}"
        L.append(row)
        L.append("    (AVE = average of available completed cycles for that symbol)")
    pos = p.get("positioning")
    if pos:
        fii = (pos.get("rows") or {}).get("FII") or {}
        cli = (pos.get("rows") or {}).get("Client") or {}
        L.append("")
        L.append(f"POSITIONING (5s): FII net {mon.fmt(fii.get('net'), 0)} "
                 f"({mon.signed(fii.get('net_chg'), 0)}); Client net {mon.fmt(cli.get('net'), 0)} "
                 f"({mon.signed(cli.get('net_chg'), 0)})")
    vix = p.get("vix")
    if vix:
        L.append(f"VOL: India VIX {vix['last']:.2f} ({vix['chg']:+.2f}), rank {mon.fmt(vix.get('rank'), 0)}")
    ts = p.get("term_structure") or {}
    for sym, items in ts.items():
        parts = " -> ".join(f"{k}:{mon.fmt(v)}" for k, v in items)
        L.append(f"  {sym} term: {parts}")
    L.append("")
    L.append("Full page: https://posu303-tech.github.io/nse-deriv-monitor/rollover.html")
    return "\n".join(L)


def render_markdown(p):
    L = [f"### NSE Monthly Rollover Special - {p['date']}", "", f"**{p['headline']}**", ""]
    L.append(f"**Current state** (data {p['last_data_date']})")
    L.append("")
    L.append("| Symbol | Roll % | 5s ago | Spread (pts) | 5s near OI Δ | 5s next OI Δ | Note |")
    L.append("|---|---|---|---|---|---|---|")
    for sym, e in (p.get("current") or {}).items():
        if e.get("missing") or e.get("roll_pct") is None:
            continue
        L.append(f"| {sym} | {e['roll_pct']:.0f}% | {mon.fmt(e.get('roll_pct_prev'), 0)}% | "
                 f"{mon.signed(e.get('spread'), 1)} | {mon.signed(e.get('near_oi_chg'), 0)} | "
                 f"{mon.signed(e.get('next_oi_chg'), 0)} | {e.get('note', '')} |")
    L.append("")
    traj = p.get("trajectories") or {}
    dates = sorted({st["date"] for st in traj.get("NIFTY", [])})[-8:]
    syms = [s for s in traj if traj[s]]
    if dates and syms:
        L.append("**Roll trajectory (roll %)**")
        L.append("")
        L.append("| Session | " + " | ".join(syms) + " |")
        L.append("|" + "---|" * (len(syms) + 1))
        for d in dates:
            cells = []
            for s in syms:
                val = next((st["roll_pct"] for st in traj[s] if st["date"] == d), None)
                cells.append(f"{val:.0f}%" if val is not None else "-")
            L.append(f"| {d} | " + " | ".join(cells) + " |")
        L.append("")
    hist = p.get("cycle_history") or {}
    if hist:
        syms = list(hist.keys())
        cycles = sorted({c["expiry"] for h in hist.values() for c in h["cycles"]},
                        reverse=True)
        L.append(f"**Roll history** (final % | at T-{p['days_to_expiry']}d %)")
        L.append("")
        L.append("| Cycle | " + " | ".join(syms) + " |")
        L.append("|" + "---|" * (len(syms) + 1))
        for e in cycles:
            cells = []
            for s in syms:
                c = next((c for c in hist[s]["cycles"] if c["expiry"] == e), None)
                f_ = (c.get("final") or {}).get("roll_pct") if c else None
                o_ = (c.get("at_offset") or {}).get("roll_pct") if c else None
                parts = []
                if f_ is not None:
                    parts.append(f"{f_:.0f}%")
                if o_ is not None:
                    parts.append(f"{o_:.0f}%")
                cells.append(" | ".join(parts) if parts else "-")
            L.append(f"| {e} | " + " | ".join(cells) + " |")
        cells = []
        for s in syms:
            a = hist[s]["avg"]
            parts = []
            if a.get("final_avg") is not None:
                parts.append(f"{a['final_avg']:.0f}%")
            if a.get("offset_avg") is not None:
                parts.append(f"{a['offset_avg']:.0f}%")
            cells.append(" | ".join(parts) if parts else "-")
        L.append(f"| **Average** | " + " | ".join(cells) + " |")
        L.append("")
    pos = p.get("positioning")
    if pos:
        fii = (pos.get("rows") or {}).get("FII") or {}
        L.append(f"**Positioning (5s)**: FII net {mon.fmt(fii.get('net'), 0)} "
                 f"({mon.signed(fii.get('net_chg'), 0)})")
        L.append("")
    L.append("[Full page](https://posu303-tech.github.io/nse-deriv-monitor/rollover.html) | "
             "[Dashboard](https://posu303-tech.github.io/nse-deriv-monitor/)")
    return "\n".join(L)


def render_html(p):
    def esc(x):
        return str(x).replace("&", "&amp;").replace("<", "&lt;")

    h = ["""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NSE Monthly Rollover Special</title><style>
body{background:#0f1420;color:#e8edf7;font-family:Segoe UI,Arial,sans-serif;margin:28px;max-width:1080px}
h1{font-size:22px;margin-bottom:2px} h2{font-size:15px;color:#9fb3d1;border-bottom:1px solid #26314a;
padding-bottom:6px;margin-top:26px}
a{color:#6ea8fe} .muted{color:#7d8fa9;font-size:12px}
.hl{background:#182035;border-left:3px solid #f7b731;border-radius:8px;padding:10px 14px;
margin:14px 0;font-size:14px;font-weight:600}
table{border-collapse:collapse;width:100%;margin:8px 0}
th,td{padding:7px 10px;border-bottom:1px solid #26314a;text-align:right;font-size:13px}
th{color:#9fb3d1} td:first-child,th:first-child{text-align:left}
.note{color:#9fb3d1;text-align:left}
</style></head><body>"""]
    h.append(f"<h1>NSE Monthly Rollover Special</h1><div class='muted'>"
             f"{esc(p['date'])} &middot; expiry {esc(p['anchor_expiry'])} "
             f"({p['days_to_expiry']} calendar days) &middot; data {esc(str(p['last_data_date']))} "
             f"&middot; <a href='index.html'>daily report</a> &middot; "
             f"<a href='trends.html'>trends</a> &middot; <a href='weekly.html'>weekly digest</a></div>")
    h.append(f"<div class='hl'>{esc(p['headline'])}</div>")

    h.append("<h2>Current state</h2>")
    h.append("<table><tr><th>Symbol</th><th>Roll %</th><th>5s ago</th><th>Spread (pts)</th>"
             "<th>Spread %</th><th>5s near OI &Delta;</th><th>5s next OI &Delta;</th><th>Note</th></tr>")
    for sym, e in (p.get("current") or {}).items():
        if e.get("missing") or e.get("roll_pct") is None:
            continue
        h.append(f"<tr><td>{esc(sym)}</td><td>{e['roll_pct']:.0f}%</td>"
                 f"<td>{mon.fmt(e.get('roll_pct_prev'), 0)}%</td>"
                 f"<td>{mon.signed(e.get('spread'), 1)}</td>"
                 f"<td>{mon.signed(e.get('spread_pct'), 2)}%</td>"
                 f"<td>{mon.signed(e.get('near_oi_chg'), 0)}</td>"
                 f"<td>{mon.signed(e.get('next_oi_chg'), 0)}</td>"
                 f"<td class='note'>{esc(e.get('note', ''))}</td></tr>")
    h.append("</table>")

    traj = p.get("trajectories") or {}
    syms = [s for s in traj if traj[s]]
    dates = sorted({st["date"] for st in traj.get("NIFTY", [])})[-8:]
    if dates and syms:
        h.append("<h2>Roll trajectory (roll % by session)</h2>")
        h.append("<table><tr><th>Session</th>" +
                 "".join(f"<th>{esc(s)}</th>" for s in syms) + "</tr>")
        for d in dates:
            h.append(f"<tr><td>{esc(d)}</td>")
            for s in syms:
                val = next((st["roll_pct"] for st in traj[s] if st["date"] == d), None)
                h.append(f"<td>{val:.0f}%</td>" if val is not None else "<td>-</td>")
            h.append("</tr>")
        h.append("</table>")

    hist = p.get("cycle_history") or {}
    if hist:
        syms = list(hist.keys())
        cycles = sorted({c["expiry"] for h in hist.values() for c in h["cycles"]},
                        reverse=True)
        h.append(f"<h2>Cycle history (final % | at T-{p['days_to_expiry']}d %)</h2>")
        h.append("<table><tr><th>Cycle</th>" +
                 "".join(f"<th>{esc(s)}</th>" for s in syms) + "</tr>")
        for e in cycles:
            h.append(f"<tr><td>{esc(e)}</td>")
            for s in syms:
                c = next((c for c in hist[s]["cycles"] if c["expiry"] == e), None)
                f_ = (c.get("final") or {}).get("roll_pct") if c else None
                o_ = (c.get("at_offset") or {}).get("roll_pct") if c else None
                parts = []
                if f_ is not None:
                    parts.append(f"{f_:.0f}%")
                if o_ is not None:
                    parts.append(f"{o_:.0f}%")
                h.append(f"<td>{' | '.join(parts) if parts else '-'}</td>")
            h.append("</tr>")
        h.append("<tr><td><b>Average</b></td>")
        for s in syms:
            a = hist[s]["avg"]
            parts = []
            if a.get("final_avg") is not None:
                parts.append(f"{a['final_avg']:.0f}%")
            if a.get("offset_avg") is not None:
                parts.append(f"{a['offset_avg']:.0f}%")
            h.append(f"<td><b>{' | '.join(parts) if parts else '-'}</b></td>")
        h.append("</tr></table>")
        h.append("<p class='muted'>Averages cover the completed cycles available in the "
                 "database for each symbol.</p>")

    pos = p.get("positioning")
    if pos:
        h.append("<h2>Positioning into expiry (5-session changes)</h2>")
        h.append("<table><tr><th>Participant</th><th>Net fut</th><th>5s change</th><th>PCR</th></tr>")
        for name in mon.PARTICIPANTS:
            r = pos["rows"].get(name, {})
            h.append(f"<tr><td>{name}</td><td>{mon.fmt(r.get('net'), 0)}</td>"
                     f"<td>{mon.signed(r.get('net_chg'), 0)}</td>"
                     f"<td>{mon.fmt(r.get('pcr'), 2)}</td></tr>")
        h.append("</table>")

    vix = p.get("vix")
    ts = p.get("term_structure") or {}
    if vix or ts:
        h.append("<h2>Volatility into expiry</h2>")
        if vix:
            h.append(f"<p>India VIX <b>{vix['last']:.2f}</b> ({vix['chg']:+.2f}), "
                     f"1y rank {mon.fmt(vix.get('rank'), 0)}</p>")
        for sym, items in ts.items():
            seg = " &rarr; ".join(f"{esc(k)}: {mon.fmt(v)}%" for k, v in items)
            h.append(f"<p class='muted'>{esc(sym)} ATM IV term: {seg}</p>")

    h.append("<p class='muted'>Roll % = next-month OI / (near + next OI). Spread = next minus near "
             "settle (carry). Sources: NSE F&amp;O bhavcopy archive. Educational tool, "
             "not investment advice.</p></body></html>")
    return "".join(h)


# ---------------------------------------------------------------- main

def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    script_dir = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="NSE monthly rollover special")
    ap.add_argument("--data-dir", default=os.path.join(script_dir, "data"))
    ap.add_argument("--out-dir", default=os.path.join(script_dir, "output"))
    ap.add_argument("--symbols", nargs="+", default=mon.DEFAULT_SYMBOLS)
    ap.add_argument("--window", type=int, default=7,
                    help="generate within N calendar days of monthly expiry (default 7)")
    ap.add_argument("--cycles", type=int, default=4,
                    help="completed cycles to average in the history table (default 4)")
    ap.add_argument("--force", action="store_true",
                    help="generate regardless of the expiry window (testing)")
    ap.add_argument("--no-deliver", action="store_true")
    ap.add_argument("--deliver-test", action="store_true",
                    help="send a test delivery through the configured channels")
    args = ap.parse_args()

    if not os.path.exists(os.path.join(args.data_dir, "nse_monitor.db")):
        print("rollover special: no database - run the monitor first")
        return 1

    payload = build_payload(args.data_dir, args.symbols, n_cycles=args.cycles)
    if not payload:
        print("rollover special: no futures history - backfill with --backfill-fut first")
        return 0

    dte = payload["days_to_expiry"]
    in_window = 0 <= dte <= args.window
    if not in_window and not args.force:
        print(f"rollover special: {dte} days to monthly expiry ({payload['anchor_expiry']}) "
              f"- outside {args.window}-day window, skipping")
        return 0

    text = render_text(payload)
    markdown = render_markdown(payload)
    html = render_html(payload)
    payload["text"] = text

    os.makedirs(args.out_dir, exist_ok=True)
    stamp = datetime.now(IST).strftime("%Y%m%d")
    dated_html = os.path.join(args.out_dir, f"rollover_{stamp}.html")
    dated_json = os.path.join(args.out_dir, f"rollover_{stamp}.json")
    latest_html = os.path.join(args.out_dir, "rollover.html")
    for path in (dated_html, latest_html):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(html)
    with open(dated_json, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)
    print(text)
    print(f"\nSaved: {dated_html}")
    if not in_window:
        print("(forced run outside the expiry window: not touching delivery state)")

    # ------------------------------------------------------------ delivery
    cfg = {}
    try:
        with open(os.path.join(script_dir, "alerts_config.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)
    except Exception:  # noqa: BLE001
        pass
    rcfg = cfg.get("rollover_special", {})
    deliver_enabled = (not args.no_deliver) and rcfg.get("deliver", True)

    state_path = os.path.join(args.data_dir, "rollover_state.json")
    state = {}
    try:
        with open(state_path, encoding="utf-8") as fh:
            state = json.load(fh)
    except Exception:  # noqa: BLE001
        state = {}
    cycle = payload["anchor_expiry"]
    if state.get("cycle") != cycle:
        state = {"cycle": cycle, "first_sent": False, "final_sent": False}

    def deliver(title, body, kind):
        channels = []
        if deliver_enabled and send_telegram and rcfg.get("deliver_telegram", True):
            if send_telegram(body):
                channels.append("telegram")
        if deliver_enabled and send_github_issue and rcfg.get("deliver_github_issue", True):
            if send_github_issue(title, body, label="rollover", color="f7b731"):
                channels.append("github-issue")
        print(f"  delivery ({kind}): {channels or ['none configured - page only']}")
        return channels

    if args.deliver_test:
        deliver(f"TEST - NSE rollover special: {cycle}",
                markdown.replace("### ", "### TEST "),
                "test")
        return 0

    if in_window and not args.no_deliver:
        changed = False
        if not state.get("first_sent"):
            deliver(f"NSE rollover special: {cycle} cycle - {dte} days to expiry", markdown, "window-open")
            state["first_sent"] = True
            changed = True
        if dte <= 1 and not state.get("final_sent"):
            deliver(f"NSE rollover special: {cycle} cycle - final roll", markdown, "final")
            state["final_sent"] = True
            changed = True
        if changed:
            os.makedirs(args.data_dir, exist_ok=True)
            with open(state_path, "w", encoding="utf-8") as fh:
                json.dump(state, fh, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
