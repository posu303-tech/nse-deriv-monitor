# How to Read the Monitor Report

The report answers **five questions**, in order. Read them top to bottom — that is
also the order professional desks use (positioning → volatility → microstructure).

1. **Who is positioned how?** (L1 positioning)
2. **Is volatility cheap or expensive?** (L2 volatility)
3. **Where are the pain points and magnets?** (L3 microstructure)
4. **What changed this week?** (the deltas, not the levels)
5. **What would flip the picture?** (trigger conditions)

> A worked example using the 14-Oct-2026 report appears in each section. Treat it
> as a reading exercise, not a recommendation.

---

## L1 — Positioning (participant OI + cash flows)

### What you are looking at

| Column | Meaning |
|---|---|
| Fut L / Fut S / Net | Participant's index-futures long, short and net contracts |
| Δ vs ref | Change in net vs ~5 sessions earlier (**the actionable number**) |
| L/S | Share of long contracts in their futures book (50% = neutral) |
| Opt PCR | Puts ÷ calls across their **entire** index-options book (long+short OI) |

### How desks read it

- **FII net short is usually hedging, not a bearish bet.** FIIs hold cash equities
  and offshore books; they short index futures to hedge. The LEVEL is structural.
  The **change** is the signal:
  - FII sharply **adding shorts** → de-risking / hedging pressure building
  - FII sharply **cutting shorts (net rising)** → short covering; often fuel for rallies
- **Client (retail) long ratio extremes are contrarian markers**, not timing tools.
  When retail is ~80%+ long while FIIs are max-short, the market can grind up (retail
  gets paid) but is vulnerable to sharp shakes.
- **DII long + FII selling in cash** = the classic domestic-absorption pattern.
  It cushions declines but does not stop them.
- **Option PCR caveat:** FII put-heavy books are partly *written* puts (income/hedging
  programs), so PCR is a composition metric, not a direction. Use it for *change*.

### Worked example (14-Oct-2026 report)

- FII net **−289,495 contracts**, 9.8% long ratio, improving **+4,230 w/w** →
  near-max hedged, but marginally *less* short than a week ago.
- Client **+229,857 net**, 83% long → retail positioned long.
- DII 77% long, Pro 64% long → domestic side constructive.
- Cash: FII **−1,305 cr** selling, DII **+2,439 cr** buying → absorption in progress.

**Read:** a defensively hedged market with weak hands long. Neither panic nor
euphoria. Watch the FII delta: a >50K w/w improvement would be short-covering fuel
(and fires an alert).

---

## L2 — Volatility (per symbol)

### The numbers

| Metric | What it is | How to read |
|---|---|---|
| ATM IV | Priced annualised move (at-the-money options) | Compare with (a) its own history (IV rank), (b) realized vol, (c) other symbols |
| Skew (5% OTM put−call) | Price of downside vs upside | Positive = fear skew (normal). Collapse = complacency/upside chase. Steepening = hedging demand |
| Term structure | ATM IV across expiries | Upward = calm. Inverted front = event/tension near. Kink = dated event |
| IV rank (local) | Where today's IV sits in stored history | Builds after ~2-4 weeks of runs; use India VIX rank meanwhile |

### Worked example

- **NIFTY IV 12.91% vs RV21 14.84% / RV10 17.09%** → options slightly *cheap* vs
  recent realized movement; straddle buyers have been getting paid.
- **India VIX 14.63 at 71st percentile (1y)** → moderate absolute level, but on the
  higher side of the past year. Vol has crept UP from very low levels; not complacent.
- **Skew +4.39 (NIFTY), +3.63 (BANKNIFTY)** → normal fear skew.
  Stock skews are *negative* (HDFCBANK −0.04, RELIANCE −2.66): upside calls are
  richer → upside-chasing in single names (matches their call-heavy PCR 0.56/0.60).
- **Term structure:** NIFTY 12.91 → 12.60 → 12.90 (near flat);
  BANKNIFTY 14.08 → 13.34 → 13.87 (front slightly richer ≈ near-term event premium).
- **IV hierarchy sanity:** indices 13–17% < stocks 22–24%. If that inverts, something
  is wrong with the data or something extraordinary is priced.

---

## L3 — Microstructure (per symbol)

- **PCR (chain):** total put OI ÷ call OI for the nearest expiry.
  - >1.2: put writers dominant (floor being sold; supportive *until it breaks* —
    then forced unwinding accelerates falls)
  - <0.7: call writers dominant (ceiling being sold; rally-capping *until it breaks*,
    then short-squeeze acceleration)
- **OI walls:** strikes with the largest call/put open interest.
  - Writers defend walls; price often stalls at/near them.
  - **Migration matters:** a rising call wall = writers following the market up
    (trend confirmation); a crumbling wall (OI dropping) = stop being a barrier.
- **Max pain:** strike minimising option writers' aggregate payout. Magnet *into
  expiry*, weakest heuristic in the report — treat as context only.
- **Basis:** futures − spot. Positive = cost of carry + mild bullish sentiment.
  Large deviations from the prevailing carry = positioning skew.

### Worked example

- NIFTY: spot 22,722, PCR **1.39** (put-heavy), walls **22,500 / 23,000**,
  max pain **22,700** — tightly sandwiched around spot; base case is range/chop
  into expiry unless a wall breaks.
- BANKNIFTY: PCR 1.07, walls 55,000 / 58,000 — floor close, ceiling far.
- HDFCBANK/RELIANCE: PCR 0.56/0.60 — call writers dominate; upside capped unless
  their call walls (750 / 1,250) give way.

---

## Putting it together

| Positioning (L1) | Volatility (L2) | Read |
|---|---|---|
| FII max-short, improving | IV < RV, rank elevated | Hedged market, cheap directional vol → rallies can be violent on short-covering |
| FII adding shorts | IV rising + skew steepening | De-risking regime → respect downside |
| FII flat/covering | IV low + rank <15 | Complacency → hedges cheap, tail-risk cheap |
| Retail max-long (any) | — | Contrarian undertone; widen stops |

**Current composite (14-Oct):** hedged positioning, marginally improving; vol
slightly cheap vs realized with elevated rank; tight walls around spot → expect
chop, watch 22,500 / 23,000 (NIFTY) for the first meaningful break.

---

## What would flip the read (trigger conditions)

- FII net futures `Δ w/w` beyond ±50K (alert `fii_futures_shift`)
- India VIX rank ≥85 (stress) or ≤15 (complacency) (alert `vix_rank_extreme`)
- NIFTY IV − RV21 gap beyond ±5 vol pts (alert `iv_rv_gap`)
- FII option PCR ≥1.5 or ≤0.6 (alert `fii_option_pcr_extreme`)
- Walls migrating decisively; term structure flipping; skew collapse/steepening

## Common misreads to avoid

1. "FII net short = market will fall." No — it's structural hedging.
2. "PCR > 1 = bullish." Only while the floor holds; crowded put writing amplifies
   falls when it breaks.
3. "Max pain = target." It is a soft magnet heuristic, nothing more.
4. Reading levels without deltas — the report's value is in *change*.
5. Forgetting data vintage: participant OI and bhavcopy are **EOD** (yesterday's
   session when you read it in the morning). Intraday reads use the live chain
   only when available.

## Data hygiene

- IVs: Black-76 from settlement prices (risk-free 6.5%) on bhavcopy runs;
  exchange-published IVs when the live chain responds.
- Participant OI: NSE archive CSV, published after market close.
- Stock options: monthly-only expiries (post-SEBI regime); limited strike sets.
- This is an educational monitoring tool, not investment advice.
