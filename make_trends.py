#!/usr/bin/env python3
"""
Trends dashboard generator for the NSE Derivatives Monitor.

Reads the accumulated SQLite history and produces a self-contained HTML page
(output/trends.html) with interactive charts (inline SVG + vanilla JS - no CDN,
no dependencies, works offline):

  Panel 1: participant net position in index futures (FII / DII / Client / Pro)
  Panel 2: participant index-options PCR over time
  Panel 3: India VIX vs NIFTY realized vol (21d) + ATM IV snapshots per symbol

Usage:
  python make_trends.py [--data-dir DIR] [--out-dir DIR]
"""

import argparse
import json
import math
import os
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
PARTICIPANTS = ["FII", "DII", "Client", "Pro"]
COLORS = {
    "FII": "#ff6b6b", "DII": "#6ea8fe", "Client": "#53d769", "Pro": "#f7b731",
    "NIFTY": "#53d769", "BANKNIFTY": "#f7b731", "FINNIFTY": "#6ea8fe",
    "MIDCPNIFTY": "#c792ea", "HDFCBANK": "#ff9ff3", "RELIANCE": "#8ce99a",
    "India VIX": "#f7b731", "NIFTY RV21": "#6ea8fe",
}
FALLBACK_COLORS = ["#53d769", "#f7b731", "#6ea8fe", "#ff6b6b", "#c792ea", "#ff9ff3"]


# ------------------------------------------------------------------ data

def participant_series(db_path):
    conn = sqlite3.connect(db_path)
    rows = list(conn.execute(
        "SELECT date, participant, idx_fut_long, idx_fut_short, "
        "idx_opt_call_long, idx_opt_put_long, idx_opt_call_short, idx_opt_put_short "
        "FROM participant_oi ORDER BY date"))
    conn.close()
    dates = sorted({r[0] for r in rows})
    net, pcr = {}, {}
    for date, p, fl, fs, cl, pl, cs, ps in rows:
        net.setdefault(p, {})[date] = (fl or 0) - (fs or 0)
        ce = (cl or 0) + (cs or 0)
        pe = (pl or 0) + (ps or 0)
        pcr.setdefault(p, {})[date] = round(pe / ce, 3) if ce else None
    return dates, net, pcr


def iv_series(db_path):
    conn = sqlite3.connect(db_path)
    rows = list(conn.execute(
        "SELECT symbol, date, atm_iv FROM iv_snapshot WHERE atm_iv IS NOT NULL "
        "ORDER BY date"))
    conn.close()
    out = {}
    for sym, date, iv in rows:
        out.setdefault(sym, {})[date] = iv
    return out


def cached_json_series(path):
    try:
        with open(path, encoding="utf-8") as fh:
            c = json.load(fh)
        return c.get("series", [])
    except Exception:
        return []


def realized_vol_series(closes, window=21):
    """-> {date: annualised RV} for the last `window` returns at each date."""
    if len(closes) < window + 1:
        return {}
    out = {}
    for i in range(window, len(closes)):
        rets = []
        for a, b in zip(closes[i - window:i], closes[i - window + 1:i + 1]):
            if a.get("close") and b.get("close"):
                rets.append(b["close"] / a["close"] - 1.0)
        if len(rets) >= window - 2:
            out[closes[i]["date"]] = round(statistics.stdev(rets) * (252 ** 0.5) * 100, 2)
    return out


def aligned(dates, values_map):
    return [values_map.get(d) for d in dates]


def build_payload(data_dir):
    db = os.path.join(data_dir, "nse_monitor.db")
    p_dates, net, pcr = participant_series(db)
    ivs = iv_series(db)

    vix_hist = cached_json_series(os.path.join(data_dir, "india_vix_cache.json"))
    vix_map = {h["date"]: h["close"] for h in vix_hist}
    nifty_hist = cached_json_series(os.path.join(data_dir, "nifty_px_cache.json"))
    rv_map = realized_vol_series(nifty_hist, 21)

    # last ~260 sessions of VIX for the vol panel
    vix_dates = sorted(vix_map)[-260:]
    vol_dates = sorted(set(vix_dates) | {d for m in ivs.values() for d in m})

    panels = []

    if p_dates:
        panels.append({
            "id": "positioning",
            "title": "Participant net position - index futures",
            "sub": "contracts, long minus short. Weekly cadence: compare the last 5 sessions",
            "dates": p_dates,
            "yfmt": "contracts",
            "zero": True,
            "height": 320,
            "series": [
                {"name": p, "color": COLORS.get(p, "#ffffff"),
                 "values": aligned(p_dates, net.get(p, {}))}
                for p in PARTICIPANTS
            ],
        })
        panels.append({
            "id": "options_pcr",
            "title": "Participant option positioning - index PCR",
            "sub": "put OI / call OI across long + short positions; >1 = put-heavy",
            "dates": p_dates,
            "yfmt": "ratio",
            "zero": False,
            "height": 260,
            "series": [
                {"name": p, "color": COLORS.get(p, "#ffffff"),
                 "values": aligned(p_dates, pcr.get(p, {}))}
                for p in PARTICIPANTS
            ],
        })

    if vol_dates:
        series = [{"name": "India VIX", "color": COLORS["India VIX"],
                   "values": aligned(vol_dates, vix_map)}]
        if rv_map:
            series.append({"name": "NIFTY RV21", "color": COLORS["NIFTY RV21"],
                           "values": aligned(vol_dates, rv_map)})
        for i, (sym, m) in enumerate(sorted(ivs.items())):
            series.append({
                "name": f"{sym} ATM IV",
                "color": COLORS.get(sym, FALLBACK_COLORS[i % len(FALLBACK_COLORS)]),
                "values": aligned(vol_dates, m),
                "dots": True,
            })
        panels.append({
            "id": "volatility",
            "title": "Volatility - India VIX vs NIFTY realized (21d) and ATM IV",
            "sub": "vol points, annualised. ATM IV dots accumulate with every run "
                   "(bhavcopy EOD or live chain)",
            "dates": vol_dates,
            "yfmt": "vol",
            "zero": False,
            "height": 300,
            "series": series,
        })

    return {
        "generated_at": datetime.now(IST).isoformat(timespec="seconds"),
        "panels": panels,
    }


# ------------------------------------------------------------------ html

HTML_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NSE Derivatives Monitor - Trends</title>
<style>
body{background:#0f1420;color:#e8edf7;font-family:Segoe UI,Arial,sans-serif;
     margin:28px;max-width:1120px}
h1{font-size:22px;margin-bottom:2px}
a{color:#6ea8fe}
.muted{color:#7d8fa9;font-size:12px;margin-bottom:18px}
.muted a{margin-right:10px}
.panel{background:#161d2e;border:1px solid #26314a;border-radius:12px;
       padding:14px 16px 6px;margin:16px 0}
.ptitle{font-size:15px;font-weight:600}
.psub{font-size:12px;color:#7d8fa9;margin:3px 0 8px}
.legend{display:flex;flex-wrap:wrap;gap:14px;font-size:12px;color:#9fb3d1;margin-bottom:6px}
.legend .sw{display:inline-block;width:10px;height:10px;border-radius:2px;
            margin-right:5px;vertical-align:middle}
.legend b{color:#e8edf7;font-weight:600}
.plotwrap{position:relative;width:100%}
.tooltip{display:none;position:absolute;background:#1d2637;border:1px solid #31405f;
         border-radius:8px;padding:8px 10px;font-size:12px;pointer-events:none;
         box-shadow:0 4px 14px rgba(0,0,0,.4);min-width:150px;z-index:5}
.trow{margin-top:3px;color:#cdd9ee}
.trow b{color:#fff}
.tdate{color:#8ea3c4;font-size:11px;margin-bottom:2px}
.empty{color:#7d8fa9;padding:30px;text-align:center}
</style></head>
<body>
<h1>NSE Derivatives Monitor - Trends</h1>
<div class="muted">
  Generated <span id="gen"></span> &middot;
  <a href="index.html">latest report</a>
  <a href="latest.json">latest.json</a>
  <a href="https://github.com/__REPO__">source</a>
</div>
<div id="charts"></div>
<p class="muted">Sources: NSE participant-wise OI archive, NSE option chain /
bhavcopy, Yahoo Finance. Educational tool, not investment advice.</p>
<script>
const DATA = __DATA__;

function niceStep(raw){
  if (raw <= 0) return 1;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  const step = norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10;
  return step * mag;
}
function fmtVal(v, kind){
  if (v === null || v === undefined) return "\\u2013";
  if (kind === "contracts") {
    const s = Math.round(v).toLocaleString("en-IN");
    return (v > 0 ? "+" : "") + s;
  }
  if (kind === "ratio") return v.toFixed(2);
  return v.toFixed(2);
}
function fmtTick(v, kind){
  const a = Math.abs(v);
  if (kind === "contracts") {
    if (a >= 100000) return (v/100000).toFixed(a % 100000 ? 1 : 0) + "L";
    if (a >= 1000) return Math.round(v/1000) + "k";
    return Math.round(v).toString();
  }
  if (kind === "ratio") return v.toFixed(1);
  return a >= 100 ? Math.round(v).toString() : v.toFixed(0);
}
function drawPanel(wrap, panel){
  const W = wrap.clientWidth || 980, H = panel.height || 300;
  const M = {l:74, r:16, t:12, b:30};
  const dates = panel.dates, n = dates.length;
  const series = panel.series;
  const all = [];
  series.forEach(s => s.values.forEach(v => { if (v !== null && v !== undefined) all.push(v); }));
  if (!all.length) { wrap.innerHTML = '<div class="empty">no data yet</div>'; return; }
  let vmin = Math.min.apply(null, all), vmax = Math.max.apply(null, all);
  if (panel.zero) { vmin = Math.min(0, vmin); vmax = Math.max(0, vmax); }
  if (vmin === vmax) { vmin -= 1; vmax += 1; }
  const pad = (vmax - vmin) * 0.06;
  vmin -= pad; vmax += pad;
  const step = niceStep((vmax - vmin) / 4);
  const x0 = M.l, x1 = W - M.r, y0 = H - M.b, y1 = M.t;
  const sx = i => n <= 1 ? (x0 + x1) / 2 : x0 + (x1 - x0) * i / (n - 1);
  const sy = v => y0 - (v - vmin) * (y0 - y1) / (vmax - vmin);
  let svg = '<svg width="' + W + '" height="' + H + '" style="display:block">';
  for (let t = Math.ceil(vmin / step) * step; t <= vmax; t += step) {
    const y = sy(t).toFixed(1);
    svg += '<line x1="' + x0 + '" y1="' + y + '" x2="' + x1 + '" y2="' + y +
           '" stroke="#26314a" stroke-width="1"/>';
    svg += '<text x="' + (x0 - 8) + '" y="' + (Number(y) + 4) +
           '" fill="#8ea3c4" font-size="11" text-anchor="end">' + fmtTick(t, panel.yfmt) + '</text>';
  }
  if (panel.zero && vmin < 0 && vmax > 0) {
    const y = sy(0).toFixed(1);
    svg += '<line x1="' + x0 + '" y1="' + y + '" x2="' + x1 + '" y2="' + y +
           '" stroke="#5a6b8c" stroke-width="1" stroke-dasharray="4 3"/>';
  }
  const nticks = Math.min(6, n);
  for (let k = 0; k < nticks; k++) {
    const i = nticks <= 1 ? 0 : Math.round(k * (n - 1) / (nticks - 1));
    if (!dates[i]) continue;
    svg += '<text x="' + sx(i).toFixed(1) + '" y="' + (y0 + 18) +
           '" fill="#8ea3c4" font-size="11" text-anchor="middle">' +
           dates[i].slice(2) + '</text>';
  }
  series.forEach(s => {
    let d = "", started = false;
    s.values.forEach((v, i) => {
      if (v === null || v === undefined) { started = false; return; }
      const X = sx(i).toFixed(1), Y = sy(v).toFixed(1);
      d += (started ? " L " : " M ") + X + " " + Y;
      started = true;
    });
    if (d) svg += '<path d="' + d + '" fill="none" stroke="' + s.color +
                  '" stroke-width="' + (s.dots ? 1.5 : 1.8) + '"/>';
    if (s.dots) {
      s.values.forEach((v, i) => {
        if (v === null || v === undefined) return;
        svg += '<circle cx="' + sx(i).toFixed(1) + '" cy="' + sy(v).toFixed(1) +
               '" r="2.6" fill="' + s.color + '"/>';
      });
    }
  });
  svg += '</svg><div class="tooltip"></div>';
  wrap.innerHTML = svg;
  const svgEl = wrap.querySelector("svg");
  const tip = wrap.querySelector(".tooltip");
  const guide = document.createElementNS("http://www.w3.org/2000/svg", "line");
  guide.setAttribute("y1", y1); guide.setAttribute("y2", y0);
  guide.setAttribute("stroke", "#9fb3d1"); guide.setAttribute("stroke-dasharray", "3 3");
  guide.setAttribute("visibility", "hidden");
  svgEl.appendChild(guide);
  wrap.addEventListener("mousemove", ev => {
    const rect = svgEl.getBoundingClientRect();
    const px = ev.clientX - rect.left;
    let i = Math.round((px - x0) / ((x1 - x0) / Math.max(1, n - 1)));
    i = Math.max(0, Math.min(n - 1, i));
    const gx = sx(i);
    guide.setAttribute("x1", gx); guide.setAttribute("x2", gx);
    guide.setAttribute("visibility", "visible");
    let rows = "";
    series.forEach(s => {
      const v = s.values[i];
      if (v === null || v === undefined) return;
      rows += '<div class="trow"><span class="sw" style="background:' + s.color +
              ';display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:5px">' +
              '</span>' + s.name + ": <b>" + fmtVal(v, panel.yfmt) + "</b></div>";
    });
    tip.innerHTML = '<div class="tdate">' + dates[i] + "</div>" +
                    (rows || '<div class="trow">no value</div>');
    tip.style.display = "block";
    const tw = tip.offsetWidth || 160;
    let lx = gx + 14;
    if (lx + tw > W) lx = gx - tw - 14;
    tip.style.left = Math.max(4, lx) + "px";
    tip.style.top = "8px";
  });
  wrap.addEventListener("mouseleave", () => {
    guide.setAttribute("visibility", "hidden");
    tip.style.display = "none";
  });
}
document.getElementById("gen").textContent = DATA.generated_at.replace("T", " ");
const container = document.getElementById("charts");
DATA.panels.forEach(panel => {
  const card = document.createElement("div");
  card.className = "panel";
  const head = document.createElement("div");
  head.innerHTML = '<div class="ptitle">' + panel.title + '</div><div class="psub">' +
                   panel.sub + '</div>';
  card.appendChild(head);
  const legend = document.createElement("div");
  legend.className = "legend";
  panel.series.forEach(s => {
    let last = null;
    for (let i = s.values.length - 1; i >= 0; i--) {
      if (s.values[i] !== null && s.values[i] !== undefined) { last = s.values[i]; break; }
    }
    const item = document.createElement("span");
    item.innerHTML = '<span class="sw" style="background:' + s.color + '"></span>' +
                     s.name + " <b>" + fmtVal(last, panel.yfmt) + "</b>";
    legend.appendChild(item);
  });
  card.appendChild(legend);
  const wrap = document.createElement("div");
  wrap.className = "plotwrap";
  card.appendChild(wrap);
  container.appendChild(card);
  drawPanel(wrap, panel);
});
window.addEventListener("resize", () => {
  container.innerHTML = "";
  DATA.panels.forEach(panel => {
    const card = document.createElement("div");
    card.className = "panel";
    const head = document.createElement("div");
    head.innerHTML = '<div class="ptitle">' + panel.title + '</div><div class="psub">' +
                     panel.sub + '</div>';
    card.appendChild(head);
    const legend = document.createElement("div");
    legend.className = "legend";
    panel.series.forEach(s => {
      let last = null;
      for (let i = s.values.length - 1; i >= 0; i--) {
        if (s.values[i] !== null && s.values[i] !== undefined) { last = s.values[i]; break; }
      }
      const item = document.createElement("span");
      item.innerHTML = '<span class="sw" style="background:' + s.color + '"></span>' +
                       s.name + " <b>" + fmtVal(last, panel.yfmt) + "</b>";
      legend.appendChild(item);
    });
    card.appendChild(legend);
    const wrap = document.createElement("div");
    wrap.className = "plotwrap";
    card.appendChild(wrap);
    container.appendChild(card);
    drawPanel(wrap, panel);
  });
});
</script>
</body></html>
"""


def render_html(payload, repo="posu303-tech/nse-deriv-monitor"):
    data_js = json.dumps(payload, ensure_ascii=False)
    html = HTML_TEMPLATE.replace("__DATA__", data_js).replace("__REPO__", repo)
    return html


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    script_dir = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="Generate trends.html from monitor history")
    ap.add_argument("--data-dir", default=os.path.join(script_dir, "data"))
    ap.add_argument("--out-dir", default=os.path.join(script_dir, "output"))
    args = ap.parse_args()

    if not os.path.exists(os.path.join(args.data_dir, "nse_monitor.db")):
        print(f"no database at {args.data_dir}; run nse_deriv_monitor.py first")
        return 1

    payload = build_payload(args.data_dir)
    os.makedirs(args.out_dir, exist_ok=True)
    out = os.path.join(args.out_dir, "trends.html")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(render_html(payload))

    n_panels = len(payload["panels"])
    n_points = sum(len(p["dates"]) for p in payload["panels"])
    size_kb = os.path.getsize(out) / 1024
    print(f"trends.html written: {n_panels} panels, {n_points} date points, {size_kb:.0f} KB")
    for p in payload["panels"]:
        print(f"  - {p['id']}: {len(p['dates'])} points, {len(p['series'])} series")
    return 0


if __name__ == "__main__":
    sys.exit(main())
