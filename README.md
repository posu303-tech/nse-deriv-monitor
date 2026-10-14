# NSE Derivatives Monitor

An on-demand monitoring workflow for NSE index derivatives (NIFTY / BANKNIFTY),
built around the three layers professional desks track:

| Layer | What it watches | Sources |
|---|---|---|
| **L1 Positioning** | Participant-wise OI (FII / DII / Client / Pro), FII-DII cash flows | NSE archives CSV, NSE FII/DII API |
| **L2 Volatility** | ATM IV, IV rank, India VIX rank, IV term structure, 25Δ skew | NSE F&O bhavcopy (Black-76 IV), Yahoo Finance |
| **L3 Microstructure** | PCR, OI walls (max-OI strikes), max pain, futures basis | Bhavcopy chain, live option-chain-v3 (best effort) |

All data is public — **no API keys, no accounts**. Standard library only (Python 3.10+).

## Quick start

```
python nse_deriv_monitor.py
```

or double-click / schedule `run.cmd` (runs the full monitor and writes reports).

### Options

```
python nse_deriv_monitor.py                  # full run: industry layers, both indices
python nse_deriv_monitor.py --symbols NIFTY  # single index
python nse_deriv_monitor.py --no-fetch       # re-render from local DB (no network)
python nse_deriv_monitor.py --days 30        # how far back to fill participant OI (default 21)
python nse_deriv_monitor.py --data-dir D:\nse-data --out-dir D:\nse-reports
```

## What you get

- **Console report** (the three layers, ready to read over a coffee)
- **JSON summary** in `output/monitor_YYYYMMDD_HHMM.json` (machine-readable for further analysis)
- **HTML dashboard** in `output/monitor_YYYYMMDD_HHMM.html` (open in browser)
- **SQLite history** in `data/nse_monitor.db` — grows with every run:
  - `participant_oi` — daily per-participant futures/options OI (FII, DII, Client, Pro)
  - `cash_flow` — FII/DII cash-market buy/sell/net
  - `iv_snapshot` — daily per-symbol ATM IV, skew, PCR, walls, max pain, term structure, basis

## How the data pipeline works

```
NSE archives (participant OI CSV) ──────► participant_oi table ─┐
NSE API (FII/DII cash) ─────────────────► cash_flow table ──────┤
                                                                ├──► report (console/JSON/HTML)
NSE bhavcopy (option chain EOD) ─┐                              │
   or live option-chain-v3 ──────┴──────► iv_snapshot table ────┘
Yahoo Finance (India VIX, NIFTY) ───────► vol context (rank, realvol)
```

**Reliability design:** NSE's live APIs throttle aggressively (they return
`HTTP 200` with an *empty* payload instead of 429). The tool detects an empty
chain response, backs off, and falls back to the daily **bhavcopy** on
`archives.nseindia.com`, which publishes the full EOD option chain (OI,
settlement prices, volumes) and is far less throttled. IV is then computed
locally with Black-76 from settlement prices (risk-free 6.5%).

Notes:
- The bhavcopy for day *T* is published after market close (~18:00–19:00 IST).
  A run at 10:00 IST therefore shows data as of the previous session.
- The live chain, when it responds, is marked `[live]` and its IVs come from
  NSE's own published IVs.
- IV rank (`iv_rank_local`) needs ~8 sessions of history to appear, and a few
  weeks to be genuinely informative. India VIX rank (1y) is available from day one.

## Scheduled / recurring use (optional)

Register a Windows scheduled task to run the monitor on weekdays after
NSE publishes EOD data (e.g. 19:30 IST):

```powershell
schtasks /Create /TN "NSE Deriv Monitor" /TR "\"C:\Users\hp\Toolbox\NSE-Deriv-Monitor\run.cmd\"" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 19:30 /F
```

Remove it with: `schtasks /Delete /TN "NSE Deriv Monitor" /F`

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `live chain unavailable (throttled)` | Normal. The tool uses bhavcopy instead. Don't hammer the API; for a live intraday chain, open nseindia.com/option-chain in a browser. |
| Participant OI missing for latest day | NSE publishes the archive after EOD. Run later in the evening. |
| `india_vix_cache.json` stale | Cache auto-refreshes after 20h; check internet access to Yahoo Finance. |
| Wrong day's data | Check the `snapshot <date>` line — bhavcopy is always the *latest published* session. |

## Extending

- **More indices**: pass `--symbols` with any index that has derivatives (e.g. `MIDCPNIFTY`, `FINNIFTY` for live; bhavcopy covers all).
- **History is yours**: query `data/nse_monitor.db` directly for backtests of
  positioning/IV signals, or to build charts.
- **Thresholds/alerts**: the JSON output is designed to be consumed by a simple
  script that messages you when e.g. FII net futures move more than X contracts
  week-over-week, or India VIX rank crosses a level.
