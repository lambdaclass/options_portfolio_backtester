# Reporting & Tearsheet Roadmap

(Part of the project [`ROADMAP.md`](ROADMAP.md).)

Goal: a pyfolio-style reporting experience — one call, one self-contained
report — built on the pieces that already exist in `analytics/`, plus the
options-specific panels no generic library provides.

## Where we are

The building blocks exist but are disconnected:

- `analytics/tearsheet.py` — `build_tearsheet()` → `TearsheetReport` with
  stats table, monthly returns table, drawdown series. `to_html()` emits
  **tables only**, no charts.
- `analytics/charts.py` — four standalone Altair charts (`returns_chart`,
  `returns_histogram`, `monthly_returns_heatmap`, `weights_chart`) that
  nothing assembles into a report. `weights_chart` is matplotlib-styled
  (takes `figsize`) — inconsistent with the rest.
- `analytics/results.py` — `BacktestResults` holds `balance` and computes
  `annual_return` / `max_drawdown` / `sharpe_ratio` from
  `balance["total capital"]`.
- `analytics/trade_log.py` — `TradeLog` with per-trade `net_pnl`,
  `winners` / `losers`.
- `analytics/stats.py` — `BacktestStats` with lookback tables and summary.

Decision: **do not depend on pyfolio** (unmaintained since Quantopian shut
down, breaks on modern pandas). Get its generic functionality via a
quantstats adapter; spend in-house effort only on options-specific panels.

## Phase 1 — Returns adapter (quantstats compatibility) ✅ done

Smallest change with the biggest payoff: expose clean daily-returns series
so the maintained pyfolio successor works out of the box.

- Add `BacktestResults.returns` → daily `pd.Series` (DatetimeIndex, name set)
  derived from `balance["total capital"].pct_change()`.
- Add `BacktestResults.benchmark_returns` when a benchmark balance is
  available (e.g. plain SPY leg).
- Optional dependency group `[reports]` with `quantstats`; document
  `qs.reports.html(results.returns, results.benchmark_returns)` in README.
- Tests: shape/index/NaN-handling of the accessor; smoke test that
  quantstats accepts it (skipped when extra not installed).

Exit criteria: a user can produce a full quantstats HTML report from any
`BacktestResults` in two lines.

## Phase 2 — Self-contained HTML tearsheet ✅ done

Make `TearsheetReport.to_html()` produce one shareable file with charts
embedded (Altair serializes to self-contained HTML/vega-lite easily).

- Embed existing charts: cumulative returns (vs benchmark when present),
  return histogram, monthly heatmap.
- Add the standard missing panels:
  - underwater (drawdown) plot from `drawdown_series`
  - top-5 drawdowns table (peak, trough, recovery, depth, duration)
  - rolling Sharpe and rolling volatility (e.g. 126d window)
- Port `weights_chart` to Altair so the report has one chart stack.
- `build_tearsheet(...).to_html()` and a `to_file(path)` convenience.
- Tests: golden-ish structural checks (panels present, no external network
  deps in the HTML), plus a rendering smoke test.

Exit criteria: `build_tearsheet(balance, trade_pnls).to_file("report.html")`
yields a single offline-viewable document covering everything pyfolio's
returns tearsheet covered.

## Phase 3 — Options-specific panels ✅ done (greeks exposure pending)

The differentiator. None of this comes from quantstats/pyfolio.

- **P&L attribution**: equity leg vs options leg over time, so
  premium-bleed-vs-crash-payoff structure is visible instead of buried in
  the blended curve. Source: `TradeLog` + balance decomposition.
- **Premium spend vs budget**: realized annualized premium spend against the
  configured budget rate (`options_budget_per_rebalance_pct`); drift here is
  a config-bug detector.
- **Crash-window zooms**: configurable event windows (defaults: GFC, COVID,
  2022 bear) showing put payoff and proceeds redeployment day by day.
- **Trade-level P&L distribution**: deep-OTM puts have a signature
  many-small-losses / rare-huge-winner shape; render as log-scale histogram
  or per-trade waterfall, not a plain histogram.
- **Exposure over time**: options notional and (where greeks are available)
  delta/vega exposure.
- Tests: each panel against a small synthetic backtest with known structure.

Exit criteria: the tearsheet answers "where did the convexity pay and what
did it cost" without the user writing any analysis code.

## Phase 4 — Article & CI integration 🔶 partial

Done: `research/spitznagel_spy/make_figures.py` generates the tearsheet and
every figure spec from the exact pinned engine configuration (it reuses
`build_article_engine` / `build_spy_engine` from reproduce_article.py).
Remaining: the CI chart-input pin and the blog asset regeneration below.

- ~~`research/spitznagel_spy/reproduce_article.py` emits the article figures
  via the tearsheet/chart code instead of ad-hoc plotting, so published
  charts regenerate from the engine.~~ (done via make_figures.py)
- Extend the article-reproduction CI gate
  (`tests/oracles/test_article_reproduction.py`) to cover chart *inputs*
  (the dataframes feeding each figure), keeping figures under the same pin
  as the published numbers.
- Blog workflow: regenerate `federicocarrone.com` chart assets from the
  reproduction script.

Exit criteria: an engine change that would alter a published figure fails CI
the same way a numeric drift does today.

## Sequencing & effort

| Phase | Depends on | Rough size |
|---|---|---|
| 1. Returns adapter | — | small (1 PR) |
| 2. HTML tearsheet | 1 (benchmark series) | medium (1–2 PRs) |
| 3. Options panels | 2 | large (PR per panel group) |
| 4. Article/CI integration | 2, partially 3 | medium |

Phases 1 and 2 can start immediately and independently of any engine work.
