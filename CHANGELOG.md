# Changelog

All notable changes to this project are documented here. Entries are
grouped by the kind of change so a reader can quickly find what
matters to them:

- **Behavioral changes** move documented backtest outputs (annual
  return, max drawdown, Sharpe, etc.). If you depend on a published
  reproduction, check this section first when upgrading.
- **API changes** add, remove, or rename public functions, classes, or
  attributes.
- **Tooling** changes the build, install, or CI surface.
- **Performance** changes runtime characteristics without changing
  results (within numeric noise).
- **Internal** refactors, formatting, and documentation that should not
  affect users.

The format follows [Keep a Changelog](https://keepachangelog.com/);
this project does not currently emit version tags, so each section is
anchored on the commit hash that introduced the change.

## Unreleased

### Behavioral changes
- **Fix externally-funded exit accounting (the "free puts" bug).** In
  externally-funded budget mode (``options_budget_pct`` or
  ``options_budget_annual_pct`` set), ``execute_exits`` now subtracts
  the entry cost from cash on exit. Before this change the engine
  credited put proceeds in full but never debited the entry cost
  anywhere, so every put trade contributed (proceeds) to cash instead
  of (proceeds − cost) = realized P&L. Lifetime cash flow per trade
  is now the realized P&L as it should be.

  Effect on published reproductions: the Spitznagel deep-OTM table
  in ``tests/test_article_reproduction.py`` shifts substantially.
  At 0.5% per-rebalance budget on the 17-year SPY data, annual
  return drops from 13.54% to 10.52%; at 1.0% from 16.29% to 10.11%;
  at 2.0% from 21.34% to 8.71%; at 3.3% from 27.23% to 5.81%. The
  qualitative shape — Sharpe peaks at the lowest budgets and
  degrades monotonically as budget grows — survives the fix.

  Also resolves what was previously documented as "Budget-mode
  capital leak with non-deep-OTM puts" in this CHANGELOG. The
  near-ATM 3.3%-per-rebalance "$3.5B from $1M" case was an
  amplified-by-time symptom of the same exit accounting bug, plus
  the per-rebalance interpretation of ``options_budget_pct`` being
  treated as if it were annual (3.3% per month is ~40%/yr of
  premium spend, not 3.3%/yr). Documented in
  ``docs/PRE_FIX_REGIME_SWEEP.md`` and ``docs/POST_FIX_REGIME_SWEEP.md``.
  ``tests/test_known_bugs.py`` removed.

- **Option intrinsic value now uses the unadjusted close (valuation
  bug B).** When a contract is not quoted on a given day (expired or
  missing from the chain), the engine falls back to the option's
  intrinsic value. Previously it computed intrinsic from the
  dividend-*adjusted* close (``stocks_price`` → ``adjClose``) against
  the raw (unadjusted) strike. Because adjusted closes are lower than
  raw closes in the past, this manufactured phantom intrinsic value
  for any expired put whose strike sat inside the adjustment gap. The
  engine now carries the unadjusted close (``close``) alongside the
  adjusted close and uses it for intrinsic value; stock capital / P&L
  still uses the adjusted close. A missing spot now yields $0 intrinsic
  (untradeable) rather than ``strike`` from a 0.0 spot. New
  ``stocks_unadj_price`` schema key (defaults to ``adjClose`` for
  back-compat). Unlike the cash-flow fix above, this is a *valuation*
  bug: both the exit proceeds and the held mark-to-market used the same
  wrong number, so the books stayed balanced — a cash-conservation
  check cannot see it, which is why it needs its own guard.

  Effect: configurations that let contracts reach the intrinsic
  fallback (profit-taking, shorter DTE, closer-to-ATM puts, or any run
  with ``check_exits_daily=False``) lose phantom value. The article's
  deep-OTM 90-180 DTE config exits at real bids near DTE 14, so its
  numbers are largely unaffected.

- **``test_article_reproduction.py`` moved to true-annual budget
  framing.** The reproduction now configures the put overlay with
  ``options_budget_annual_pct`` (and ``check_exits_daily=True``), matching
  the article's "X%/yr" language and ``use_external_budget``, instead of
  the per-rebalance ``options_budget_pct``. Under the corrected engine
  the deep-OTM overlay TRACKS SPY with a small monotonic premium drag
  and Sharpe ≈ buy-and-hold; the earlier "overlay beats SPY / Sharpe
  sweet spot" result was an artifact of the two bugs above. Re-pinned
  table (annual budget, 2008-2024): 0.5%/yr 10.50%, 1.0%/yr 10.34%,
  2.0%/yr 9.99%, 3.3%/yr 9.52% (SPY buy-and-hold 10.65%); full-period
  max drawdown is essentially unchanged from SPY (≈ −51.9%) at these
  budgets. The ``test_spitznagel_sweet_spot_shape`` test is replaced by
  ``test_spitznagel_monotone_drag_and_tracking``.

### Bug fixes
- **``check_exits_daily`` set as an attribute is now honored.** It was
  only a ``run()`` parameter (default ``False``); assigning
  ``engine.check_exits_daily = True`` silently did nothing because
  ``run()`` never read it. ``BacktestEngine`` now has a
  ``check_exits_daily`` attribute and ``run(check_exits_daily=None)``
  falls back to it, so the attribute and the explicit argument agree
  (the argument still overrides per-run). The global default remains
  ``False``.
- **`rebalance_stocks_on_exit` is now reachable from Python.** The Rust
  core already supported redeploying freed cash into stocks immediately
  after a daily option exit (monetize-and-reinvest), but the flag was
  never passed through from `BacktestEngine`, so it was dead. Exposed as
  `BacktestEngine.rebalance_stocks_on_exit` (default ``False``; only
  meaningful with `check_exits_daily=True`).
- **Filter columns ``underlying_last``, ``impliedvol``, and
  ``open_interest`` are no longer silently dropped before Rust
  dispatch.** ``_run_rust`` and ``_run_rust_multi`` previously dropped
  these three options-dataframe columns "to reduce Arrow conversion
  cost." That broke every leg filter referencing them: strike-based
  OTM filters (``schema.strike <= schema.underlying_last * X``)
  silently matched zero rows so no entries fired at all; IV- and
  OI-based filters raised ``RuntimeError: unable to find column …``.
  Silently broke every preset whose depth filter expressed OTM as a
  multiple of spot — ``near_atm_put_protection``, ``Strangle``,
  ``IronCondor``, ``CoveredCall``, ``CashSecuredPut``, ``Collar``,
  ``Butterfly``. The ``deep_otm_put`` preset was not affected because
  it filters by delta. The drop set is now ``{"last", "optionalias"}``
  (only columns that no filter or selector inspects). On the 17-year
  SPY parquet, conversion cost is unchanged within noise.

### Invariants / defense-in-depth
- **Optional runtime self-checks (`assert_invariants`).** New
  `BacktestEngine.assert_invariants` attribute (and `assert_invariants`
  config key; default ``False``, zero cost when off) turns on two
  in-engine guards that fail the run loudly on violation:
  - *Class A — cash flow:* on every option exit, the change in portfolio
    cash must equal realized P&L net of commission, computed
    independently of the mutation. Catches the "free puts" regression
    (crediting full proceeds without returning the externally-funded
    entry cost). A cash-conservation check is the right shape for this
    class — a per-trade cash-flow bug unbalances the ledger.
  - *Class B — valuation:* when an unquoted (expired/missing) contract
    falls back to intrinsic value, that value must equal the
    unadjusted-spot intrinsic, recomputed independently. Catches a
    regression back to the adjusted close. A cash check cannot see this
    class — both proceeds and mark use the same wrong price, so the
    ledger stays balanced; it needs its own valuation guard.
  Exercised by ``tests/bench/test_invariants.py::TestRuntimeInvariantChecks``
  across the budget configurations where both bugs originally lived.

### Budget API clarification
- ``options_budget_pct`` is **per-rebalance**, not annual. On monthly
  rebalancing, ``options_budget_pct = 0.033`` means 3.3% of NAV
  spent on options each month (~40%/yr of premium), not 3.3%/yr.
  Use ``options_budget_annual_pct`` if you want true annual
  semantics, which is what most of the tail-hedge literature
  (including Spitznagel's *Safe Haven*) actually describes. The
  README's first-backtest example and ``BacktestEngine.use_external_budget``
  already use annual semantics. ``docs/POST_FIX_ANNUAL_SWEEP.md``
  reports the regime sweep at true-annual semantics for comparison.

### API changes
- `BacktestEngine.use_external_budget(annual_pct)` and
  `BacktestEngine.use_allocation(stocks, options, cash)` —
  first-class helpers that configure the two put-overlay framings in
  the literature's vocabulary. `use_external_budget` is the
  Spitznagel framing (100% stocks + external put budget);
  `use_allocation` is the AQR / allocation-reducing framing.
  Equivalent to the existing attribute-level configuration but
  self-documenting at the call site. (commit `60e6e91`)
- `options_portfolio_backtester.results.BacktestResults` —
  dataclass returned by `BacktestEngine.get_results()`. Bundles
  balance, trade log, config, engine version (read from
  `importlib.metadata`), and an optional data hash. Exposes computed
  properties for annual return, max drawdown, annualized volatility,
  and Sharpe ratio, plus a `summary()` dict. The companion
  `hash_data_file` helper computes the SHA-256 used in `data_hash`.
  (commit `e6605ec`)
- `options_portfolio_backtester.strategy.presets.deep_otm_put` and
  `near_atm_put_protection` — function-style presets for the two
  canonical tail-hedge configurations. `deep_otm_put` matches the
  Spitznagel configuration (delta -0.10 to -0.02, DTE 90-180, exit
  DTE 14). `near_atm_put_protection` matches AQR's PPUT-style
  configuration (~5% OTM, monthly DTE). Compose with the framing
  helpers above. (commit `e6605ec`)
- `HistoricalOptionsData` and `TiingoData` now accept `.parquet`
  files in addition to `.csv` and `.h5`. The SPY option chain loads
  in ~0.4s as parquet vs ~15s as CSV. (commit `e6605ec`)
- `data/fetch_data.py` writes both `processed/options.csv` and
  `processed/options.parquet`. Existing CSV-using code continues to
  work unchanged. Pass `--allow-fallback` to permit the secondary
  mirrors (options-data CDN, dataset-hist repo, yfinance) when the
  canonical GitHub Releases source is unreachable; default behaviour
  is now to fail loudly rather than silently substitute different
  bytes. The canonical SPY parquets are pinned by SHA-256 and the
  downloader warns on hash mismatch. (commit `60e6e91`)

### Tooling
- `PyO3 abi3-py311` feature enabled in `rust/ob_python/Cargo.toml`.
  The same extension now runs on Python 3.11+ including 3.14. The
  `make install-dev` target also sets
  `PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1` as a belt-and-braces fix
  for users on the latest Pythons. (commit `60e6e91`)
- `tests/test_article_reproduction.py` — pins the published
  Spitznagel article's SPY baseline and Spitznagel-framing budget
  table (0.5%, 1.0%, 2.0%, 3.3%) at the engine's current output,
  with tolerance 0.5pp on annual return, 1.0pp on max drawdown, and
  0.05 on Sharpe. Includes a sweet-spot shape test that asserts
  Sharpe peaks at 0.5%-1.0% budget. The CI workflow runs this as a
  dedicated step after the regular pytest pass. `make
  verify-articles` and `make verify-articles-smoke` run the full
  table and the fast qualitative check respectively from the shell.
  (commits `60e6e91`, `e6605ec`)
- `tests/test_results.py` — 8 unit tests for `BacktestResults` and
  `hash_data_file`. Synthetic balance series, no engine or data
  dependencies, runs in ~0.4s. (commit `00070d1`)
- `README.md` first-backtest example switched to the new ergonomic
  API: `deep_otm_put` preset + `use_external_budget` framing +
  `get_results()`. Roughly half the lines of the previous example,
  and every step names its own intent. (commit `6324a32`)

### Performance
- Parquet loading: a 17-year SPY option chain (22M rows) loads in
  ~0.4 seconds via `HistoricalOptionsData("options.parquet")`
  versus ~15.4 seconds via `options.csv`. Disk footprint drops from
  3.2 GB to 593 MB. CI / smoke runs against article reproductions
  speed up accordingly. (commit `e6605ec`)

### Internal
- Module-level logger added to `options_portfolio_backtester.data.providers`.
  Foundation for follow-up structured-logging conversions; the
  package itself does not emit `print()` calls and behaviour is
  unchanged. (commit `e6605ec`)

---

## Behavioral changes pre-Unreleased (historical, from git log)

These are the engine commits that moved documented backtest outputs
in the period before this CHANGELOG was started. Listed here so
downstream reproductions can audit which engine they were generated
against.

- `523ba10` **Fix cash leakage in externally-funded budget path.**
  The externally-funded budget path (Spitznagel framing) was
  inflating returns due to cash leaking in across rebalances. The
  fix correctly accounts cash; post-fix annual returns for typical
  budgets dropped by 2-20 percentage points relative to the pre-fix
  values. Articles that depended on pre-fix numbers — notably the
  Spitznagel tail-hedge piece at federicocarrone.com — have since
  been updated against the corrected engine, and
  `tests/test_article_reproduction.py` pins the post-fix numbers.
- `d42eec0` **Fix budget mode: 100% stock allocation + credit sale
  proceeds.** Earlier the budget mode misallocated proceeds from
  option sales; the fix credits them correctly into the cash bucket.
  Affects any backtest using `options_budget_pct`.
- `5840620` **Fix budget-mode stock allocation: use liquid capital,
  not total capital.** The stock-position sizing in budget mode
  previously sized off total capital including the value of open
  option positions, which double-counted. The fix uses liquid
  capital. Small effect at small budgets, larger as budget grows.
- `b78602d` **Fix budget-mode double-counting of options capital in
  Python engine.** Companion to `5840620`, in the Python engine
  rather than the Rust path. (Now obsolete since the Python engine
  was removed in `0951478` — Rust is the only backend.)
- `ffdfd1d` **Revert full liquidation, fix accounting with cash =
  total - options_capital.** The earlier "full liquidation at
  rebalance" behaviour was reverted in favour of a partial trim;
  cash accounting was also corrected. Affects any multi-rebalance
  backtest.
- `62c4fd5` **Restore `_sell_some_options` to trim excess puts at
  rebalance.** Restores incremental trim behaviour that was lost
  during the Rust port.
- `264e440` **Full options liquidation at rebalance.** The earlier
  step before `ffdfd1d` reverted this. Listed for completeness.

For complete history, see `git log`. The convention going forward
is: any commit that moves a documented backtest output gets a
`Behavioral changes` entry above and a `tests/test_article_reproduction.py`
update in the same PR.
