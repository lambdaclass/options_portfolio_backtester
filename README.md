Options Portfolio Backtester
============================

Backtest options strategies with realistic execution, Greeks-aware risk management, and contract-level inventory. Also handles equities and multi-asset portfolios. Built on a Rust compute core.

Before trusting an options-overlay result, read
[`docs/METHODOLOGY.md`](docs/METHODOLOGY.md): entry-calendar luck, dirty
chains, budget semantics, fill rate and path accounting, each with the API
that guards against it.
What is in flight and still missing: [`docs/ROADMAP.md`](docs/ROADMAP.md).

## Get started

### Install

With Nix:
```shell
nix develop
```

Without Nix (Python >= 3.12):
```shell
python -m venv .venv && source .venv/bin/activate
make install-dev
```

### Get data

```shell
python scripts/fetch_data.py all --symbols SPY
```

Downloads SPY stock prices and options chains to `data/processed/`. Supports 104+ symbols. See [`data/README.md`](data/README.md) for details.

The canonical SPY parquets are pinned by SHA-256 in `scripts/fetch_data.py`:

- `SPY_options.parquet`: `a7152991b45b81f090f970e945bf88def8093b8ecb9b250e9891cb6d88041f0a`
- `SPY_underlying.parquet`: `847e60a441eb10969d87cd4a6da604257b782d9076168f68d5730b84096c79db`

A hash mismatch warning means the source has been updated; published-article reproductions are pinned to the hashes above. Provenance and redistribution posture: [`data/DATA_NOTICE.md`](data/DATA_NOTICE.md). Verify any copy with `python scripts/fetch_data.py verify`. Pass `--allow-fallback` to permit the secondary mirrors (options-data CDN / yfinance) when the canonical source is unreachable. Fallbacks return different bytes and break bit-for-bit reproducibility.

### Run your first backtest

```python
from options_portfolio_backtester import (
    BacktestEngine, Stock,
    HistoricalOptionsData, TiingoData,
    deep_otm_put,
)

# Load data. Parquet is ~5x smaller on disk and ~30x faster to load than CSV.
options_data = HistoricalOptionsData("data/processed/options.parquet")
stocks_data = TiingoData("data/processed/stocks.csv")

# Build the strategy with a canonical preset — the Spitznagel tail-hedge leg
strategy = deep_otm_put(options_data.schema, "SPY")

# Configure the framing: 100% SPY + 0.5% external put budget (Spitznagel)
engine = BacktestEngine({"stocks": 1.0, "options": 0.0, "cash": 0.0},
                       initial_capital=1_000_000)
engine.use_external_budget(annual_pct=0.005)
engine.stocks = [Stock("SPY", 1.0)]
engine.stocks_data = stocks_data
engine.options_data = options_data
engine.options_strategy = strategy
engine.run(rebalance_freq=1, rebalance_unit="BMS")

# Inspect the results — the dataclass bundles balance, trades, config, and engine version
results = engine.get_results()
print(results.summary())
# {'annual_return': 10.42, 'max_drawdown': -51.9, 'volatility': 19.53, 'sharpe': 0.61, 'trades': 147, ...}
```

The AQR / allocation-reducing framing is one helper away:

```python
engine.use_allocation(stocks=0.99, options=0.01, cash=0.0)
engine.options_strategy = near_atm_put_protection(schema, "SPY")
```

See [`research/spitznagel_spy/FRAMINGS.md`](https://github.com/unbalancedparentheses/finance_research/blob/main/research/spitznagel_spy/FRAMINGS.md) in the companion repo for the difference between the two framings and what each one produces.

### Reports and tearsheets

One call produces a single self-contained HTML report with every panel —
equity curve vs benchmark, underwater plot, rolling Sharpe/volatility,
monthly heatmap, plus the options-specific panels (P&L attribution by leg,
premium spend vs budget, crash-window zooms, per-trade P&L on a symlog
scale, options exposure):

```python
from options_portfolio_backtester.analytics.tearsheet import build_tearsheet

report = build_tearsheet(
    engine.balance,
    benchmark_balance=spy_engine.balance,   # optional
    trade_log=engine.trade_log,             # optional, powers the trade panels
    budget_annual_pct=0.033,                # optional, draws the budget rule
)
report.to_file("tearsheet.html")
```

Charts embed as static SVG when `vl-convert-python` is installed (the
`charts` extra) — fully offline — and fall back to interactive vega-embed
otherwise. For the full generic quantstats report (install the `reports`
extra), every backtest exposes a clean daily-returns series:

```python
import quantstats as qs
qs.reports.html(results.returns, benchmark_results.returns)
```

### Strategy presets

Instead of building legs manually:

```python
from options_portfolio_backtester import (
    Strangle, deep_otm_put, near_atm_put_protection,
)

# Tail-hedge primitives (function style, return a configured Strategy)
spitznagel_leg = deep_otm_put(schema, "SPY")   # delta -0.10 to -0.02, DTE 90-180
aqr_leg = near_atm_put_protection(schema, "SPY")  # 5% OTM, monthly

# Other presets (class style)
strangle = Strangle(schema, "short", "SPY",
                    dte_entry_range=(30, 60), dte_exit=7,
                    otm_pct=5, pct_tolerance=1,
                    exit_thresholds=(0.2, 0.2))
```

Available presets: `deep_otm_put`, `near_atm_put_protection`, `Strangle`, `IronCondor`, `CoveredCall`, `CashSecuredPut`, `Collar`, `Butterfly`.

### Stock-only backtest with algo pipeline

For equity portfolios without options, use the pipeline API:

```python
from options_portfolio_backtester.engine.pipeline import (
    AlgoPipelineBacktester,
    RunMonthly, SelectAll, WeighInvVol, LimitWeights, Rebalance,
)
import pandas as pd

prices = pd.read_csv("data/processed/stocks.csv", parse_dates=["date"])
prices = prices.pivot(index="date", columns="symbol", values="adjClose")

bt = AlgoPipelineBacktester(
    prices=prices,
    initial_capital=1_000_000,
    algos=[
        RunMonthly(),
        SelectAll(),
        WeighInvVol(lookback=252),
        LimitWeights(limit=0.25),
        Rebalance(),
    ],
)
bt.run()
```

## Execution models

Every component is swappable. Pass them to `BacktestEngine(...)` or override per-leg.

**Signal selectors** — which contract to pick from candidates:
`FirstMatch()`, `NearestDelta(target)`, `MaxOpenInterest()`

**Cost models** — commissions and fees:
`NoCosts()`, `PerContractCommission(rate)`, `TieredCommission(tiers)`

**Fill models** — execution price:
`MarketAtBidAsk()`, `MidPrice()`, `SpreadFraction(fraction)`, `VolumeAwareFill(threshold)` — applied to entries, exits and daily marks

**Position sizers** — how many contracts:
`CapitalBased()`, `FixedQuantity(qty)`, `FixedDollar(amount)`, `PercentOfPortfolio(pct)`

**Risk constraints** — pre-trade gating:
`MaxDelta(limit)`, `MaxVega(limit)`, `MaxDrawdown(max_dd_pct)`

## Rebalancing model

At each rebalance date the engine:

1. **Runs exits** — open option positions whose exit filter matches, or that
   crossed `profit_pct` / `loss_pct`, are closed at market (bid for long,
   ask for short). Other positions are **kept** across rebalances.
2. **Values the portfolio** — `total capital = cash + stocks + open options`
   at current marks.
3. **Sets the options budget** — either an allocation share
   (`{"stocks": 0.97, "options": 0.03}`) or an external premium budget
   (`options_budget_annual_pct`, %/yr split over the schedule, or
   `options_budget_per_rebalance_pct`). By default the budget is a target for
   the *value* of options held; `options_budget_fresh_spend = True` spends
   the full budget every rebalance instead.
4. **Rebalances stocks** — sells and rebuys stocks at the target allocation
   of the capital not locked in options (with an external budget, after
   setting the budget aside).
5. **Buys new options** — contracts matching the entry filter, sized to the
   budget, paid from cash at the entry price.

Between rebalance dates positions are held and marked to market. With
`check_exits_daily = True` exits run every day, each day's balance row is
recorded at that day's close, and `rebalance_stocks_on_exit = True` puts the
proceeds into stocks the same day. New entries wait for the next rebalance,
unless `roll_exit_proceeds_pct` is set: then that fraction of each sale's
proceeds buys new contracts the same day.

For the **Spitznagel** framing, the put budget sits on top of a fully
invested portfolio: `{"stocks": 1.0, "options": 0.0, "cash": 0.0}` plus
`use_external_budget(0.005)` means 100% equity plus 0.5%/yr of premium.
Leverage is a stock allocation above 1 financed by negative cash
(`{"stocks": 1.05, "cash": -0.05}`); the engine charges no interest on it.

## Rust acceleration

Required: the backtest engine imports the Rust core at load time, so it must be built before use. There is no Python fallback.

```shell
make rust-build
```

| Benchmark | Time |
|-----------|------|
| Full options backtest (17-year SPY chain, ~22M rows) | ~4s |
| Stock-only monthly rebalance | ~0.6s |
| Parallel grid sweep (100 configs) | 5-8x vs sequential (Rayon, bypasses GIL) |

(The former Python-engine comparison column was removed along with the
Python engine path itself; those numbers are no longer reproducible.)

## Data

```shell
# SPY stock + options data
python scripts/fetch_data.py all --symbols SPY

# Multiple symbols
python scripts/fetch_data.py all --symbols SPY IWM QQQ --start 2020-01-01 --end 2023-01-01

# FRED macro signals (VIX, GDP, Buffett Indicator, etc.)
python scripts/fetch_signals.py

# Convert OptionsDX format
python scripts/convert_optionsdx.py data/raw/spx_eod_2020.csv --output data/processed/spx_options.csv
```

You can also bring your own CSVs. Required columns:
- **Stocks**: `date`, `symbol`, `adjClose`
- **Options**: `quotedate`, `underlying`, `type`, `strike`, `expiration`, `dte`, `bid`, `ask`, `volume`, `openinterest`, `delta`

## Tests

```shell
make test            # default suite (~1300 tests: unit + oracles + convexity)
make test-heavy      # data-heavy at-scale correctness tests (needs SPY data)
make verify-articles # reproduce all published-article tables and assert they match
make lint            # ruff
make typecheck       # mypy (informational; typing debt documented in CI)
make rust-test       # Rust unit tests
make rust-bench      # criterion performance benchmarks
```

See [`tests/README.md`](tests/README.md) for the test-tier model (unit /
oracles / heavy / convexity) and what each tier guards against.

## Architecture

```
options_portfolio_backtester/
├── core/            # Types: Direction, OptionType, Greeks, Fill, Order
├── data/            # Schema DSL, CSV providers
├── strategy/        # Strategy, StrategyLeg, presets
├── execution/       # CostModel, FillModel, Sizer, SignalSelector
├── portfolio/       # Portfolio, OptionPosition, RiskManager
├── engine/          # BacktestEngine, AlgoPipelineBacktester, StrategyTreeEngine
├── convexity/       # Convexity scoring/backtest (used by presets)
└── analytics/       # BacktestStats, BacktestResults, TradeLog, TearsheetReport, charts

rust/
├── ob_core/         # Backtest loop, stats, execution models, filter parser
└── ob_python/       # PyO3 bindings, parallel sweep, Arrow bridge
```

## Pipeline algos

40+ composable algos for the `AlgoPipelineBacktester`. All follow `__call__(ctx) -> StepDecision`.

**Scheduling**: `RunDaily`, `RunWeekly`, `RunMonthly`, `RunQuarterly`, `RunYearly`, `RunOnce`, `RunOnDate`, `RunAfterDate`, `RunAfterDays`, `RunEveryNPeriods`, `RunIfOutOfBounds`, `Or`, `Not`, `Require`

**Selection**: `SelectAll`, `SelectThese`, `SelectHasData`, `SelectN`, `SelectMomentum`, `SelectWhere`, `SelectRandomly`, `SelectActive`, `SelectRegex`

**Weighting**: `WeighEqually`, `WeighSpecified`, `WeighTarget`, `WeighInvVol`, `WeighMeanVar`, `WeighERC`, `TargetVol`, `WeighRandomly`

**Risk & rebalancing**: `LimitWeights`, `LimitDeltas`, `ScaleWeights`, `HedgeRisks`, `Margin`, `MaxDrawdownGuard`, `Rebalance`, `RebalanceOverTime`, `CapitalFlow`, `CloseDead`, `ClosePositionsAfterDates`, `ReplayTransactions`, `CouponPayingPosition`

## Research

[`research/spitznagel_spy/`](research/spitznagel_spy/) holds the tail-hedge
study behind the published articles: a reproduction script, the experiments,
and the written-up findings. Start with
[`findings/ROBUSTNESS_REVIEW.md`](research/spitznagel_spy/findings/ROBUSTNESS_REVIEW.md).
Further notebooks and analysis: [finance_research](https://github.com/unbalancedparentheses/finance_research).
