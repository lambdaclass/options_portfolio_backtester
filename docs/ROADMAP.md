# Roadmap

What has landed, what is in flight, and what is still missing. Reporting and
tearsheet work has its own plan in [`REPORTING_ROADMAP.md`](REPORTING_ROADMAP.md).
Pitfalls and the APIs that guard against them: [`METHODOLOGY.md`](METHODOLOGY.md).

## Landed on `master` (Oct 2026)

Engine correctness, found by a library audit and an independent pandas
re-implementation of the tail-hedge strategy (which matches the engine to the
cent):

| Change | Commit |
|---|---|
| Point-in-time daily balance, premium paid at entry, puts kept through quote gaps | `ff6b5d4` |
| Options settle on their expiry date (were settled at the next rebalance's spot) | `04ca889` |
| Quote gaps carry the last mark; thresholds only fire on quoted days | `04ca889` |
| Invalid filters raise; the parser covers the whole Python filter DSL | `c81731f` |
| Fill models price exits and daily marks too; `SpreadFraction` | `7471028` |
| `sizer=`, strategy-level models, `schema=`, `current_cash` wired up | `f25d087` |
| Round-trip P&L signed and paired across legs; `summary()` fixed | `7a740ce` |
| One definition per metric (Sortino, CAGR, Sharpe), annualized from timestamps | `fe3f9ec` |
| Risk-blocked entries counted separately from unfilled ones | `4ee6549` |
| `roll_exit_proceeds_pct` (roll put-sale proceeds into new puts) | `68db547` |

New library APIs: `clean_options_chain` / `flag_bad_quotes` and
`normalize_expirations` (data cleaning), `entry_calendars` /
`run_over_calendars` / `calendar_summary` (robustness to entry timing),
`apply_financing` (interest on cash and borrowing) — `8397659`, `7036bfd`,
`d3703d6`.

Tooling: ruff and mypy (129 → 0 errors) gate CI — `60315cd`, `8913c47`.

Research (`research/spitznagel_spy/`): calendar-robust tail-hedge studies,
leverage, monetization, re-hedging, IV timing, rolling, equal-risk tests,
Spitznagel's own recipe, a Tobin's Q switch, total return and execution
sensitivity. Current verdict: a 1–2%/yr deep-OTM put overlay costs
≈0.3–0.5pp/yr vs buy & hold and is a wash against holding less stock plus
T-bills at equal risk; its robust benefit is protection in fast crashes.

## In flight

1. **Tail-hedge review rewrite** — the result CSVs were regenerated on
   Friday-dated expirations (`59e7341`); `findings/ROBUSTNESS_REVIEW.md` and
   the research README still quote the pre-fix tables (banner in place).
   Rewrite every number from the new CSVs.
2. **Parameter landscape** — `experiments/parameter_landscape.py` is written
   (pre-registered OTM × DTE × exit × budget, four calendars, exit
   neighbours, split halves, equal-risk scoring) but not run yet.

## Missing

Library (from the audit, by impact):

3. **Leg relationships for spreads** — legs are filtered independently and
   paired by row; nothing enforces the same expiration or a strike/delta
   offset. `iron_condor` / `butterfly` build nonsense (buying and selling
   the same contract) and several README-listed presets do not exist. Needs
   per-leg constraints relative to leg 1 and quantity ratios.
4. **Short-option margin** — shorts are sized by premium received with no
   margin, and the default target-value budget grows them every rebalance
   (5% allocation → ~$6M notional on $1M). Needs cash-secured / Reg-T
   sizing, margin calls, and excluding the short liability from the budget.
5. **Native financing** — `apply_financing` is a post-hoc NAV adjustment;
   interest does not affect sizing inside the run.
6. **Risk constraints** — `MaxDelta` / `MaxVega` sum per-share greeks with
   no ×100 multiplier and ignore the stock leg; they reject instead of
   shrinking a trade; `MaxDrawdown` updates its peak only on rebalance days.
7. **Smaller gaps** — `events_dataframe()` is never populated by the Rust
   loop; multi-strategy runs ignore `roll_exit_proceeds_pct` and
   strategy-level model overrides; `FirstMatch` without an `entry_sort`
   picks from an unspecified row order; early assignment and physical
   delivery are not modeled.

Research:

8. **Article errata** — the published article's +6pp/yr (SPY 2008–2024)
   is in-sample and its Sharpe/drawdown columns moved after the engine
   fixes; the corrected numbers are pinned in
   `tests/oracles/test_article_reproduction.py`.
