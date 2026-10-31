#!/usr/bin/env python3
"""Where in parameter space does a put overlay do best -- honestly scored?

Earlier sweeps (spx_sweep.py, parallel_sweep.py) used one entry calendar, the
Saturday-dated SPX chain and single exit days, so their winners were partly
calendar and sale-date luck. This sweep re-maps the space with every
safeguard the robustness review arrived at:

  - Friday-dated expirations (spx_clean normalizes the old Saturday dates)
  - four bi-monthly entry calendars, all starting on day 1
  - each exit rule at two neighbouring days (base - 2, base + 2), never one
    exact day
  - SPX total return (dividends) and financing on every run
  - scored against buy & hold AND against the unhedged stock/T-bill mix of
    equal max drawdown and of equal volatility
  - split halves: 1996-2010 and 2011-2025 excess CAGR, measured inside each
    full run -- a parameter that is truly better should be better in both

Pre-registered grid (do not tune after seeing results):
  OTM band     10-15, 15-20, 20-25, 25-30, 30-35, 35-40% below spot
  entry DTE    60-120, 90-180, 180-365 days
  exit         DTE about 30 / 60 / 90 (each run at base -/+ 2)
  budget       1% and 2% a year, spent in full, bid/ask fills

Exits must leave time before expiry, so an exit is only paired with entry
windows that start above it.

Usage:  python research/spitznagel_spy/experiments/parameter_landscape.py [n_workers]
Needs the local licensed SPX parquet (and FRED / Shiller data, see spx_common).
"""

import numpy as np
import pandas as pd
import spx_common as C

from options_portfolio_backtester import apply_financing

OUT = "parameter_landscape_results.csv"
SPLIT = pd.Timestamp("2011-01-01")

# Pre-registered grid -- do not tune after seeing results.
OTM = [(0.10, 0.15), (0.15, 0.20), (0.20, 0.25), (0.25, 0.30), (0.30, 0.35), (0.35, 0.40)]
DTE = [(60, 120), (90, 180), (180, 365)]
EXIT_BASE = [30, 60, 90]
EXIT_OFFSETS = [-2, 2]
BUDGETS = [0.010, 0.020]
FRONTIER = [round(w, 2) for w in np.arange(0.40, 1.501, 0.05)]


def _half_cagr(b, lo=None, hi=None):
    s = b.loc[(b.index >= lo) if lo is not None else slice(None)]
    s = s.loc[(s.index < hi) if hi is not None else slice(None)]
    return C.ann(s) if len(s) > 2 else float("nan")


def _run(job):
    kind, cal, dates, otm, dte, exit_base, exit_dte, bud, w = job
    if kind == "frontier":
        st = C.put_strategy(C.W["o"].schema, (0.25, 0.30), (90, 180), 30)
        bt = C.backtest(st, dates, annual=0.0, leverage=w)
    else:
        st = C.put_strategy(C.W["o"].schema, otm, dte, exit_dte)
        bt = C.backtest(st, dates, annual=bud)
    b = apply_financing(bt.balance, C.W["r"])
    d = b.pct_change().dropna()
    m = C.metrics(b, 0.0)
    del m["excess"]
    return {"kind": kind, "calendar": cal, "otm": f"{otm[0]:.2f}-{otm[1]:.2f}" if otm else "-",
            "dte": f"{dte[0]}-{dte[1]}" if dte else "-", "exit": exit_base, "exit_dte": exit_dte,
            "bud": bud, "w": w, **m, "vol": d.std() * np.sqrt(252) * 100,
            "cagr_1": _half_cagr(b, hi=SPLIT), "cagr_2": _half_cagr(b, lo=SPLIT),
            "fill_rate": bt.option_fill_rate if kind == "hedged" else float("nan")}


def _match(f, col, x):
    f = f.sort_values(col)
    return float(np.interp(x, f[col], f["cagr"])) if f[col].min() <= x <= f[col].max() else float("nan")


def main():
    workers = C.n_workers()
    rate = C.tbill()
    puts, stk = C.load_spx()
    stk = C.with_total_return(stk)
    cals = {lbl: C.schedule(stk["date"], step, par, off) for _, lbl, step, par, off in C.BIMONTHLY}

    jobs = [("frontier", cal, d, None, None, 0, 0, 0.0, w) for cal, d in cals.items() for w in FRONTIER]
    for otm in OTM:
        for dte in DTE:
            for base in EXIT_BASE:
                if base + 15 > dte[0]:
                    continue  # exit must come well after entry
                jobs += [("hedged", cal, d, otm, dte, base, base + off, bud, 1.0)
                         for cal, d in cals.items() for off in EXIT_OFFSETS for bud in BUDGETS]

    print(f"=== Parameter landscape (Friday expirations, total return): {len(jobs)} runs on {workers} workers ===\n")
    df = pd.DataFrame(C.run_pool(_run, jobs, puts, stk, workers, {"r": rate}))
    C.write(df, OUT)

    fr = df[df["kind"] == "frontier"]
    bh = fr[fr["w"] == 1.0][["calendar", "cagr", "cagr_1", "cagr_2"]].set_index("calendar")
    h = df[df["kind"] == "hedged"]
    rows = []
    keys = ["otm", "dte", "exit", "bud"]
    for cal, hc in h.groupby("calendar"):
        f = fr[fr["calendar"] == cal]
        for k, g in hc.groupby(keys):
            m = g[["cagr", "dd", "vol", "gfc", "covid", "cagr_1", "cagr_2", "fill_rate"]].mean()
            rows.append({**dict(zip(keys, k)), "calendar": cal, **m.to_dict(),
                         "vs_bh": m.cagr - bh.loc[cal, "cagr"],
                         "vs_bh_1": m.cagr_1 - bh.loc[cal, "cagr_1"],
                         "vs_bh_2": m.cagr_2 - bh.loc[cal, "cagr_2"],
                         "edge_dd": m.cagr - _match(f, "dd", m.dd),
                         "edge_vol": m.cagr - _match(f, "vol", m.vol)})
    r = pd.DataFrame(rows)
    g = r.groupby(keys).agg(
        vs_bh=("vs_bh", "mean"), vs_bh_1=("vs_bh_1", "mean"), vs_bh_2=("vs_bh_2", "mean"),
        edge_dd=("edge_dd", "mean"), edge_dd_worst=("edge_dd", "min"),
        edge_vol=("edge_vol", "mean"), edge_vol_worst=("edge_vol", "min"),
        dd=("dd", "mean"), gfc=("gfc", "mean"), covid=("covid", "mean"),
        fill=("fill_rate", "min"),
    ).reset_index().sort_values("edge_vol", ascending=False)
    g.to_csv(C.FINDINGS / "parameter_landscape_summary.csv", index=False)

    print("Excess CAGR vs buy & hold (TR) overall and per half; edge vs the equal-risk stock/T-bill mix")
    print(f"  {'otm':9s} {'dte':7s} {'exit':>4s} {'bud':>4s} {'vsBH':>6s} {'96-10':>6s} {'11-25':>6s} "
          f"{'edgeDD':>6s} {'worst':>6s} {'edgeV':>6s} {'worst':>6s} {'maxDD':>6s} {'GFC':>6s} {'COVID':>6s} {'fill':>5s}")
    for x in g.itertuples():
        flag = "" if x.fill >= C.FILL_FLOOR else " LOW"
        print(f"  {x.otm:9s} {x.dte:7s} {x.exit:>4d} {x.bud*100:>4.1f} {x.vs_bh:>+6.2f} {x.vs_bh_1:>+6.2f} "
              f"{x.vs_bh_2:>+6.2f} {x.edge_dd:>+6.2f} {x.edge_dd_worst:>+6.2f} {x.edge_vol:>+6.2f} "
              f"{x.edge_vol_worst:>+6.2f} {x.dd:>6.1f} {x.gfc:>6.1f} {x.covid:>6.1f} {x.fill:>4.0%}{flag}")


if __name__ == "__main__":
    main()
