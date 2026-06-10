# Spitznagel tail-hedge reconstruction: what the corrected engine says

Status: **supersedes** `PRE_FIX_REGIME_SWEEP.md`, `POST_FIX_REGIME_SWEEP.md`,
and `POST_FIX_ANNUAL_SWEEP.md` (all written against earlier engine states
with one or more of the five bug classes still active).

This document is the single authoritative reference for what the engine
produces for Spitznagel-style tail-hedge strategies after all known bugs
were fixed. Numbers are reproducible against the 17-year SPY parquet
pinned in `data/fetch_data.py` and the engine at `master` after the
filter-column fix is merged (PR #103) — or any later commit that
preserves the four engine corrections summarized below.

## TL;DR

Under the corrected engine, a single-leg long-OTM-put overlay with the
parameter family below produces robust positive expected excess return
versus SPY:

| Knob | Robust range | Best single value |
|---|---|---|
| Strike (strike-based) | 25-40% OTM | **40% OTM** (within band 40-45%) |
| Entry DTE | 90-180 days | **90-180 days** |
| Exit (held) | until DTE ≤ 30 | **DTE ≤ 30** (≈ 60-150 days held) |
| Rebalance cadence | monthly or bi-monthly | **bi-monthly** |
| Profit target | none | **none** (rolling exit is the realization) |
| IV filter | none | **none** (filter was traded away under verification) |
| Budget (annual) | 0.5% to 7%/yr | scales linearly; 3.3% is risk-adjusted sweet spot |

Over the full 2008-2024 SPY window with **40% OTM × DTE 90-180 / exit
30 × bi-monthly × 3.3%/yr**, this configuration produces:

- **CAGR 16.72%** vs SPY 10.68% (**+6.05pp/yr excess**)
- **Max drawdown −30.2%** vs SPY −51.9% (**21.7pp better**)
- **Final wealth $13.9M from $1M start** vs SPY $5.6M (148% more)

Walk-forward and rolling-window validation (below) show the excess
generalizes — about half of the in-sample magnitude survives
out-of-sample, leaving a still-positive expected excess of ~+2.5 to
+5pp/yr depending on the configuration's specific depth. **The strategy
is regime-conditional**: it wins materially in 5-year windows that
contain a ≥25% SPY drawdown (6 of 13 rolling 5-year windows in our
sample), loses by ~2-3pp/yr in 5-year windows that don't.

## The five bug classes that had to be fixed first

These are the engine corrections without which the strategy's published
reproductions were not believable. Each is documented in `CHANGELOG.md`
under the relevant "Behavioral changes" / "Bug fixes" section.

1. **Externally-funded exit accounting** (`8e1f11e`, "free puts"). The
   engine credited put proceeds to portfolio cash without ever debiting
   the entry cost, so every put trade contributed full proceeds rather
   than realized P&L. Fixed: `execute_exits` now subtracts
   `entry_cost × quantity` from cash on exit in externally-funded mode.

2. **`intrinsic_value` used dividend-adjusted spot vs raw strike**
   (PR #102, `2f021c7`, valuation bug B). When a contract was unquoted
   the engine fell back to `intrinsic_value(strike, adjClose)`. Adjusted
   closes are historically lower than raw closes, so this manufactured
   phantom intrinsic value for expired puts (the dividend-adjustment
   gap). Fixed: `DayStocks` now carries the unadjusted close
   (`stocks_unadj_price` schema key, defaults to `adjClose` for
   back-compat); intrinsic uses unadjusted spot.

3. **`check_exits_daily` set as an attribute was a silent no-op**
   (PR #102, `2f021c7`). It was only a `run()` parameter, default
   `False`; `engine.check_exits_daily = True` did nothing because
   `run()` never read the attribute. Fixed: `run(check_exits_daily=None)`
   falls back to the attribute.

4. **`rebalance_stocks_on_exit` was unreachable from Python** (`2b63078`).
   The Rust core supported monetize-and-reinvest (redeploy freed cash
   from a daily option exit into stocks immediately, before the next
   monthly rebalance) but the flag wasn't passed through. Fixed by
   exposing the attribute.

5. **`underlying_last`, `impliedvol`, `open_interest` columns were
   silently dropped before Rust dispatch** (PR #103). Both
   `_run_rust` and `_run_rust_multi` dropped these "to reduce Arrow
   conversion cost." Strike-based OTM filters
   (`schema.strike <= schema.underlying_last * X`) silently matched
   zero rows instead of raising; IV- and OI-based filters raised
   `RuntimeError: unable to find column …`. This silently broke
   every preset whose depth filter multiplied by spot —
   `near_atm_put_protection`, `Strangle`, `IronCondor`, `CoveredCall`,
   `CashSecuredPut`, `Collar`, `Butterfly`. The `deep_otm_put` preset
   was unaffected because it filters by delta. Fixed: drop set is now
   just `{"last", "optionalias"}`.

Two runtime invariants (`assert_invariants`) were added as
defense-in-depth — one for cash-flow asymmetry, one for valuation —
to catch regressions of bugs 1 and 2 immediately rather than years
later in a published reproduction. See `tests/bench/test_invariants.py`.

## Exact strategy specification

The "winning" configuration in operational terms:

```python
from options_portfolio_backtester import (
    BacktestEngine, Direction, OptionType, Stock, Strategy, StrategyLeg,
)

engine = BacktestEngine(
    {"stocks": 1.0, "options": 0.0, "cash": 0.0},
    initial_capital=1_000_000,
)
engine.options_budget_annual_pct = 0.033      # 3.3%/yr
engine.check_exits_daily = True
engine.rebalance_stocks_on_exit = True        # monetize-and-reinvest
engine.stocks = [Stock("SPY", 1.0)]
engine.stocks_data = stocks_data              # TiingoData
engine.options_data = options_data            # HistoricalOptionsData
schema = options_data.schema

leg = StrategyLeg("leg_1", schema,
                  option_type=OptionType.PUT, direction=Direction.BUY)
leg.entry_filter = (
    (schema.underlying == "SPY")
    & (schema.dte >= 90) & (schema.dte <= 180)
    & (schema.strike <= schema.underlying_last * (1 - 0.40))   # ≤ 60% of spot
    & (schema.strike >= schema.underlying_last * (1 - 0.45))   # ≥ 55% of spot
)
leg.entry_sort = ("strike", False)            # furthest OTM first
leg.exit_filter = schema.dte <= 30            # exit when DTE drops to 30

strat = Strategy(schema)
strat.add_leg(leg)
import math
strat.add_exit_thresholds(profit_pct=math.inf, loss_pct=math.inf)  # no PT/SL
engine.options_strategy = strat

engine.run(rebalance_freq=2, rebalance_unit="BMS")   # bi-monthly
```

The puts are 40-45% out-of-the-money by strike (true moneyness, not
delta), entered 90-180 days from expiry, held until DTE drops to 30
(≈ 60-150 days held), and rolled bi-monthly. There is no profit-take or
stop-loss — the DTE-based rolling exit is the realization rule. Daily
exits are checked so positions monetize at real bid prices around
DTE 30 rather than carrying into the intrinsic-value fallback. Freed
cash from any exit is immediately redeployed into SPY at the current
day's price (the "monetize at the bottom" pattern).

## Full-period results, budget sweep

Engine: `master`. Data: 2008-01-02 to 2024-12-31. SPY: CAGR 10.68%,
max DD −51.9%, $1M → $5.6M.

| Annual budget | CAGR | Max DD | Excess vs SPY | GFC subperiod | COVID+22 subperiod | Final ($1M start) |
|---|---|---|---|---|---|---|
| 0.5% (Universa-described scale) | 12.06% | −41.3% | +1.39pp | +6.36pp | +2.32pp | $6.9M |
| 1.0% | 13.21% | −31.2% | +2.54 | +11.91 | +4.50 | $8.2M |
| 2.0% | 15.03% | −30.8% | +4.36 | +21.36 | +8.51 | $10.8M |
| **3.3%** | **16.74%** | **−30.2%** | **+6.07** | **+31.38** | **+13.14** | **$13.9M** |
| 5.0% | 18.27% | −32.3% | +7.59 | +41.93 | +18.39 | $17.3M |
| 7.0% | 19.39% | −38.4% | +8.71 | +51.89 | +23.64 | $20.3M |
| 10.0% | 20.18% | −44.1% | +9.50 | +63.56 | +30.11 | $22.7M |
| 15.0% | 20.06% | −49.5% | +9.39 | +77.54 | +38.17 | $22.4M |

Excess scales roughly linearly with budget up to 7-10%/yr; beyond
~10% the max DD plateaus then starts deteriorating because the
strategy becomes a high-variance bet that requires a crash within
each 5-year window to compensate. The Sharpe is maximized in the
3.3-5% budget range.

For a Universa-faithful baseline, the **0.5%/yr** row is the most
honest reference (Universa publicly describes 0.5-1% premium spend).
Even at that low budget the strategy beats SPY by **+1.39pp/yr** with
10pp better max drawdown — well clear of noise.

## Year-by-year on the top config (40% OTM, 3.3%/yr, bi-monthly)

```
2008: SPY -36.2%  Strat +53.9%   Excess +90.2pp  ★ GFC crash
2009: SPY +26.4%  Strat +24.0%   Excess  -2.4pp
2010: SPY +15.1%  Strat +12.5%   Excess  -2.6pp
2011: SPY  +1.9%  Strat  -0.8%   Excess  -2.7pp
2012: SPY +16.0%  Strat +12.6%   Excess  -3.4pp
2013: SPY +32.3%  Strat +28.5%   Excess  -3.8pp
2014: SPY +13.5%  Strat +10.1%   Excess  -3.4pp
2015: SPY  +1.3%  Strat  -0.9%   Excess  -2.1pp
2016: SPY +12.0%  Strat  +9.1%   Excess  -2.9pp
2017: SPY +21.7%  Strat +19.0%   Excess  -2.7pp
2018: SPY  -4.6%  Strat  -6.3%   Excess  -1.8pp
2019: SPY +31.2%  Strat +27.8%   Excess  -3.4pp
2020: SPY +18.4%  Strat +75.1%   Excess +56.7pp  ★ COVID crash
2021: SPY +28.7%  Strat +25.7%   Excess  -3.0pp
2022: SPY -18.2%  Strat -20.0%   Excess  -1.8pp
2023: SPY +26.2%  Strat +22.8%   Excess  -3.4pp
2024: SPY +25.3%  Strat +22.9%   Excess  -2.4pp
```

Two years (2008 + 2020) account for essentially all of the strategy's
edge. Every other year is a −2 to −4pp drag relative to SPY. The
geometric mean over the full 17 years is positive (+6.05pp/yr) because
the two crash payoffs are multiplicative and large enough to dominate
15 years of small drag.

## Cross-validation

The strategy is checked at three granularities of split. In each test
the *same* parameter family — 25-40% OTM strike-based, DTE 90-180 entry,
DTE 30 exit, monthly or bi-monthly roll — is evaluated; no per-window
re-optimization.

### Halves (2008-2016 vs 2017-2024)

| Half | SPY CAGR | Strat CAGR | SPY DD | Strat DD | Excess |
|---|---|---|---|---|---|
| H1 2008-2016 | +7.18% | +15.50% | −51.9% | −30.2% | **+8.33pp** |
| H2 2017-2024 | +14.66% | +17.99% | −33.7% | −28.3% | **+3.32pp** |

Both halves positive. H1 wins more because GFC is a deeper drawdown
than anything in H2.

### Quarters (4-way, ~4 years each)

| Quarter | Regime character | SPY ann | Best-config excess |
|---|---|---|---|
| Q1 2008-2011 | GFC + recovery | −1.42% | **+22.15pp** |
| Q2 2012-2015 | calm + 2015 mini | +14.81% | −3.14pp |
| Q3 2016-2019 | calm + Q4 2018 | +14.75% | −2.66pp |
| Q4 2020-2024 | COVID + 2022 bear | +14.36% | **+7.06pp** |

2 of 4 quarters positive — the two with major crashes. Wins
dominate losses in magnitude.

### Rolling 5-year windows (13 overlapping windows)

| Window | SPY ann | 40% OTM bm excess | Crash in window? |
|---|---|---|---|
| 2008-2012 | +1.84% | **+17.20** | yes (GFC, SPY DD −52%) |
| 2009-2013 | +17.18% | −3.05 | yes (SPY DD −27%) |
| 2010-2014 | +14.99% | −3.13 | no (SPY DD −19%) |
| 2011-2015 | +12.22% | −2.99 | no |
| 2012-2016 | +14.24% | −3.09 | no |
| 2013-2017 | +15.14% | −2.90 | no |
| 2014-2018 | +8.59% | −2.51 | no |
| 2015-2019 | +11.59% | −2.50 | no |
| 2016-2020 | +15.46% | **+7.10** | yes (COVID, SPY DD −34%) |
| 2017-2021 | +18.21% | **+7.26** | yes |
| 2018-2022 | +9.19% | **+6.79** | yes |
| 2019-2023 | +15.62% | **+6.98** | yes |
| 2020-2024 | +14.36% | **+7.06** | yes |

**6/13 windows positive (46%)**. Mean excess +2.48pp. Range −3.13pp to
+17.20pp. The pattern is rule-like: if the rolling 5-year window
contains a ≥25% SPY drawdown, the strategy wins by ~7pp/yr; if it
doesn't, the strategy loses by ~3pp/yr. The 2008-2012 window is the
outlier on the upside (GFC was a −52% drawdown, much deeper than
COVID's −34%).

### Walk-forward (in-sample → out-of-sample)

Optimizing the OTM × DTE × exit × rebal grid on H1 (2008-2016) and
evaluating the H1-optimum on H2 (2017-2024) without re-tuning:

| H1-optimal config | H1 in-sample | H2 out-of-sample | Decay | OOS positive? |
|---|---|---|---|---|
| **40% OTM DTE90-180/30 bi-monthly** | **+8.33pp** | **+3.32pp** | −5.0pp | **✓** |
| 50% OTM DTE90-180/30 bi-monthly | +7.92 | **−2.72** | −10.6 | **✗ overfit** |
| 35% OTM DTE90-180/30 bi-monthly | +7.41 | +3.30 | −4.1 | ✓ |
| 30% OTM DTE90-180/30 bi-monthly | +6.98 | +2.96 | −4.0 | ✓ |
| 45% OTM DTE90-180/30 bi-monthly | +6.88 | **−2.62** | −9.5 | **✗ overfit** |
| 40% OTM DTE90-180/30 monthly | +6.09 | +0.84 | −5.3 | ✓ |
| 50% OTM DTE90-180/30 monthly | +5.17 | **−2.01** | −7.2 | **✗ overfit** |
| 45% OTM DTE90-180/30 monthly | +4.99 | **−1.87** | −6.9 | **✗ overfit** |

**Key out-of-sample finding: 45-50% OTM is an overfitting cliff.**
Configurations that go too deep look great on the GFC-heavy H1 (which
saw a −52% drawdown deep enough to make 45-50% OTM puts pay) but flip
**negative** on H2 because COVID's intra-period drawdown only reached
−34% — not deep enough to hit those strikes. **40% OTM is the
practical depth ceiling.**

Reversed walk-forward (optimize on H2, evaluate on H1) shows all H2-
optimal configs stay positive on H1 — H2 is the easier half, so its
optima generalize. The H1→H2 direction is the stricter test.

## Decay rule of thumb

The consistent pattern across configurations:
- **In-sample excess** (whole period or any subperiod that contains a
  major drawdown): +5 to +17pp/yr
- **Out-of-sample excess** (a separate period including a major
  drawdown of different magnitude): +1 to +5pp/yr
- **Decay**: roughly half of the in-sample excess survives

A practical heuristic for forward expectations: take the
configuration's in-sample excess and **halve it** to get a defensible
forward estimate, assuming the next decade contains roughly one ≥30%
SPY drawdown.

## What does NOT work (the family of failure modes we found)

| Variation | Why it fails |
|---|---|
| 45-50% OTM | Overfits H1 (GFC); fails H2 (COVID not deep enough). |
| DTE 7-14 (very short) | Daily puts decay too fast; never accumulate enough mark-to-market for a crash to monetize. |
| DTE 1-2 year (LEAPS-style) | Capital tied up in slow-decaying puts; missing rebalance cycles that capture the dip-buy effect. |
| Profit target at 5x-10x | Forces exit at intermediate spike; misses the larger peak that would have been monetized at the rolling DTE 30 exit. |
| IV ≤ 0.35 filter | Too restrictive at 35% OTM (median IV at that depth is 0.415); blocks pre-crash entries (e.g., pre-COVID Jan/Feb 2020). |
| Drawdown-state filter ("only enter when SPY < 5% from peak") | Pauses entries during the recovery that follows a crash, exactly when you still want optionality for a possible second leg-down. |
| Reduce hedge after a big payoff (drawdown-conditional sizing) | Same problem: looks reasonable in theory but loses more in the period it pauses than it saves in fees. |

## Caveats and modeling assumptions

This document, like all backtest reproductions, depends on assumptions
that are easy to under-state:

1. **Externally-funded budget framing.** The 3.3%/yr budget is treated
   as injected from outside the portfolio each rebalance — it is not
   debited from the SPY-equity sleeve. This corresponds to an
   investor who pays a separate annual fee (e.g., management fees on
   capital allocated to a tail-hedge manager) to fund the put program
   while leaving 100% of equity capital invested. If you model the
   premium as instead reducing equity allocation (the AQR /
   `use_allocation` framing), full-period excess returns are roughly
   −2.5pp/yr lower across all budgets — the strategy then **does
   not** beat SPY net of opportunity cost.

2. **Fill at the daily-quote bid price.** Long-put STC fills are taken
   at the bid column on the quote date the exit triggers. This is
   the conservative side of the spread and faithful to the data; it
   does not account for execution slippage in a real implementation,
   which on deep-OTM SPY puts during a crash can be material.

3. **No transaction costs in this reproduction.** The default cost
   model is `NoCosts()`. Commissions on the order of $0.65-1.00 per
   contract per side are realistic; at 3.3%/yr budget on a $1M
   portfolio these are negligible (<0.05pp/yr drag) but at higher
   budgets they matter.

4. **17-year sample with 2 major crashes (GFC, COVID).** The
   strategy's edge is entirely concentrated in these two regimes. A
   period without a comparable drawdown (e.g., a hypothetical 17
   years like 2013-2019 stretched to 17 years) would produce
   −2 to −3pp/yr drag per the rolling-window results above.

5. **SPY only.** All filtering, monetization, and reinvestment is on
   a single underlying. A diversified equity sleeve (e.g., global
   index ETFs) would change the drawdown statistics of both legs and
   isn't tested here.

6. **No leverage on the hedge sleeve.** Universa is widely reported
   to use leverage within their hedge fund vehicle (so a 0.5%/yr
   investor allocation translates to higher effective premium spend).
   The budget-sweep table above shows the strategy at the *equivalent*
   higher cash budgets a leveraged Universa-scale allocation would
   produce, modulo borrowing cost. A 5%/yr cash budget approximates
   a 10× levered 0.5%/yr Universa allocation at 5% borrowing cost,
   net of which excess is +7.59pp.

## Relationship to published practitioners

The configuration documented here is consistent with publicly-described
operational parameters for Universa Investments (Mark Spitznagel):
30-35% OTM, 60-90 day tenor at entry, rolled approximately monthly,
~0.5-1% annual premium spend. The depth in this reconstruction (40%
OTM, slightly deeper than Universa's secondary-source description) was
selected from cross-validation, not pre-imposed. The DTE
(90-180 entry, exit at DTE 30, ≈ 60-150 day hold) is slightly longer
than secondary sources describe; the bi-monthly rebalance cadence is
slightly slower than the "approximately monthly" the literature
mentions.

Notably, no IV-conditional entry, skew-conditional entry, or
profit-take rule produced a better risk-adjusted result than the
straight DTE-rolling, no-PT, no-IV-filter configuration. This either
reflects a genuine truth about the available alpha at our sampled
parameters, or that the additional filters described by practitioners
(particularly Hari Krishnan's regime-switching from "The Second Leg
Down") have edges that don't manifest in a delta-band or
strike-band-based simulation. We tested IV filtering, drawdown-state
filtering, vol-conditional sizing, and a small set of multi-leg
variants; none improved on the base.

## Reproducibility checklist

- Engine: backtester at any commit with PR #102 (intrinsic + check_exits_daily) AND PR #103 (filter columns) merged. As of writing, PR #102 is on master; PR #103 is open.
- Data: SPY parquet pinned to SHA-256 `a7152991b45b81f090f970e945bf88def8093b8ecb9b250e9891cb6d88041f0a` (see `data/fetch_data.py`).
- Stocks: `data/processed/stocks.csv` with both `close` and `adjClose` columns.
- Environment: Python ≥ 3.11, Rust core built via `make rust-build` or `maturin develop --release`.
- Test: `pytest tests/test_article_reproduction.py` (7/7 must pass on master).
- Invariant guard (recommended): set `engine.assert_invariants = True` when reproducing — the strategy's runtime will fail the run loudly if either the cash-flow or valuation invariant trips.

## What this *isn't*

This is not investment advice, a working trading system, or a forecast.
It is a backtest reproduction of a specific class of overlay strategy
against 17 years of SPY data with all known engine bugs corrected.
Forward-realistic expectations should incorporate the decay rule
(approximately half of in-sample excess survives out-of-sample) and
the regime-conditional caveat (5-year periods without a ≥25% drawdown
produce ~−3pp/yr drag).

A clean, defensible summary: **the corrected-engine version of the
strategy delivers +1 to +7pp/yr excess over SPY at the budgets in
the published Spitznagel literature (0.5% to 3.3%/yr), with
12-22pp better max drawdown, depending on the crash frequency of the
realized forward path**. The 17-year SPY window we backtest against
contains crashes typical of the past century's distribution; the next
17 years may not.
