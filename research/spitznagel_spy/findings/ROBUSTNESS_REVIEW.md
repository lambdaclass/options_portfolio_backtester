# Robustness review: what survives (Oct 2026)

> **Update pending (Oct 2026).** The result CSVs in this directory were
> regenerated with Friday-dated expirations (`normalize_expirations`; older
> SPX monthlies are dated on Saturday, which shifted every DTE rule by a
> trading day). The tables below still show the earlier numbers; the exit-30
> rows in particular are overstated (2%: −0.09 → −0.44pp/yr, GFC −42% →
> −47%). Text to be updated from the new CSVs.

This is the current reference for the SPX tail-hedge study. It supersedes the
headline numbers in the other files in this directory and in
[`docs/SPITZNAGEL_RECONSTRUCTION.md`](../../../docs/SPITZNAGEL_RECONSTRUCTION.md)
wherever they disagree.

**Bottom line.** Over SPX 1996–2025, a deep-OTM put overlay does not
reliably beat buy & hold on return. Scored fairly (averaged over entry
calendars, full budget actually spent), the best configurations land between
about −0.1 and +0.25pp/yr versus buy & hold. It reliably cuts fast crashes
(COVID −34% → −15% at a 2% budget, at every exit date tested); its GFC
protection is smaller than it first looked (−57% → about −50% at exits 28
and 32; the −42% at exit 30 rides the 2008-11-20 sale date). Against the
fair alternative, holding less stock plus T-bills at the same drawdown or
volatility, the hedge is a wash: about −0.1 to +0.1pp/yr on the price index
(§7) and about +0.25pp/yr once dividends are counted (§8), with the worst
entry calendar negative either way. The earlier "+0.95pp"
optimum was an artifact of one entry calendar and one sale date.
Profit-taking ("sell at k× premium") never helped.

## What changed underneath the old numbers

Every earlier result was produced with at least one of these problems:

| Problem | Effect | Fix |
|---|---|---|
| Daily-exit balance rows back-filled from end-of-period holdings | Drawdown, Sharpe and windowed DDs wrong (CAGR ~fine) | Engine records each day at its close |
| Budget-funded premium paid at exit, not entry | NAV overstated by open premia | Engine debits premium at entry |
| Budget = *target value* of puts held, not a spend | Real spend < nominal; no new hedge after a payoff | Research scripts set `options_budget_fresh_spend = True` |
| One-day quote gap force-sold the put at intrinsic (≈0) | Hedge lost on data holes | Engine keeps it until expiry |
| CBOE SPX **binary options** (BSK/BSF, 2008–10) in the chain | Bought as $0.19 "puts" in Oct 2008, marked like puts | [`spx_clean.py`](../experiments/spx_clean.py) drops them + flat/garbage prints |
| Single entry calendar (1st trading day of odd months) | ±0.8pp swing from calendar alone | Score over 4 calendars, all starting invested on day 1 |

Engine fixes: see `CHANGELOG.md` (Unreleased). Data: see
[`DATA_COVERAGE.md`](DATA_COVERAGE.md).

## 1. The exit-30 optimum is luck

[`timing_robustness.py`](../experiments/timing_robustness.py), 25–30% OTM,
90–180 DTE entry, 1996–2025. Excess CAGR vs buy & hold (pp), **mean over four
entry calendars** (odd/even months × 1st/10th trading day):

| exit DTE | 1% | 2% | 3.3% |
|---|---|---|---|
| 21 | −0.37 | −0.84 | −1.49 |
| 25 | −0.23 | −0.59 | −1.13 |
| 28 | −0.29 | −0.67 | −1.23 |
| **30** | **+0.04** | **−0.09** | **−0.39** |
| 32 | −0.25 | −0.60 | −1.13 |
| 45 | −0.47 | −0.98 | −1.67 |

- **Calendar:** at exit 30 / 2%, odd-month entries give +0.72 / +0.30, even
  months −0.84 / −0.55. Every older result used the odd-month calendar.
- **One day:** exit 30 beats 28 and 32 in every calendar. The Dec-2008 puts
  hit DTE 30 on 2008-11-20, the SPX closing low; selling two days earlier or
  later roughly halves that payoff (2007–09 probe, 2% budget: $36.3k vs
  $19.7k / $18.8k of proceeds on $1M). Exits 28/32 are the honest neighbours: −0.6 to −0.7pp at 2%.
- The start-year matrix ([`start_year_robustness.py`](../experiments/start_year_robustness.py))
  cannot detect this: every start year begins in January, so all of them
  share the odd-month calendar.

## 2. Leverage and monetization (Spitznagel's actual claim)

[`leverage_monetize.py`](../experiments/leverage_monetize.py): 1.0/1.03/1.05×
equity (borrowing at T-bill + 0.5%, cash earning T-bill), budgets 1%/2%,
exits 30/45/60, sell at 3×/5×/10× premium or hold; all calendar-averaged.

| config | CAGR | vs B&H | vs unhedged same lev | max DD | GFC DD | COVID DD | Sharpe |
|---|---|---|---|---|---|---|---|
| unhedged 1.00× (B&H) | 8.33 | — | — | −56.8 | −56.8 | −33.9 | 0.436 |
| unhedged 1.05× | 8.54 | +0.21 | — | −58.8 | −58.8 | −35.5 | 0.424 |
| 1.05×, 1%, exit 30, hold | 8.57 | **+0.24** | +0.03 | −54.7 | −51.7 | −24.3 | 0.464 |
| 1.05×, 2%, exit 30, hold | 8.48 | +0.14 | −0.06 | −53.0 | −44.3 | −15.8 | 0.480 |
| 1.00×, 2%, exit 30, hold | 8.24 | −0.09 | −0.09 | −50.6 | **−42.3** | **−14.6** | **0.492** |
| 1.00×, 2%, exit 60, hold | 8.12 | −0.21 | −0.21 | −52.9 | −52.9 | −24.0 | 0.457 |

- **Small leverage + a cheap hedge** does edge out buy & hold with smaller
  drawdowns, but by ~0.1–0.25pp/yr, and only vs B&H — versus the same
  leverage unhedged it is ≈ 0.
- **Profit-taking hurts in every cell** (−0.3 to −0.75pp): selling at 3–10×
  monetizes before the crash finishes and gives back the COVID protection
  (COVID DD ≈ −34%, same as unhedged).
- **Exit 60 (selling well before expiry)** costs ~0.1–0.2pp vs exit 30 and
  loses most of the GFC protection.
- The exit-30 rows carry the one-day luck of §1; discount them accordingly.

## 3. What the hedge does buy

Crash protection at roughly break-even cost. At 2%/yr, calendar-averaged:
COVID drawdown −15% vs −34% at every exit date; GFC −42% vs −57% at exit
30, but about −50% at exits 28 and 32 (§7), for −0.09pp/yr (exit 30). That
is a risk-reduction result, not a return-enhancement one, and the robust
part of it is protection against *fast* crashes.

## 4. Spitznagel-style mechanisms

[`spitznagel_variants.py`](../experiments/spitznagel_variants.py): pre-registered
families built on what Spitznagel / Universa describe, 1.0× equity, full budget
spent, scored over entry calendars (4 bi-monthly, 2 monthly). Excess vs B&H,
pp/yr, mean (worst…best calendar):

| strategy | 1% | 2% | GFC DD @2% | COVID DD @2% |
|---|---|---|---|---|
| **core** 25–30% OTM, 90–180 DTE, sell at DTE 30, bi-monthly | **+0.04** (−0.39…+0.50) | −0.09 (−0.84…+0.72) | **−42%** | **−15%** |
| core, monthly ladder (half size) | −0.08 (−0.26…+0.09) | −0.26 (−0.57…+0.04) | −47% | −13% |
| + monetize when strike ≥ 0.95·spot | −0.09 | −0.28 | −52% | −18% |
| + monetize when strike ≥ 0.90·spot | −0.17 | −0.41 | −54% | −21% |
| + monetize when strike ≥ 0.85·spot | −0.41 | −0.86 | −55% | −28% |
| short-dated 50–100 DTE, sell at DTE 14 (20–30% OTM, ± monetize) | −0.51…−0.57 | −1.06…−1.18 | −54…−56% | −19…−31% |

- **Monetizing in the crash hurts, and the earlier it fires the worse.**
  Selling once SPX nears the strike cashes out partway down; in 2008 the
  market kept falling for months while the next hedge waited for the next
  scheduled entry. Re-hedging the same day (§5) and rolling the proceeds
  into new puts (§6) do not rescue it.
- **Short-dated puts cost too much.** Rolling ~2-month puts pays more decay
  per year for similar crash payoffs.
- **The monthly ladder is the realistic number:** a narrower calendar range
  and ≈ −0.1 to −0.3pp/yr for real crash protection.

## 5. Re-hedging after a monetization, and IV-aware buying

**Same-day re-hedge** ([`rehedge.py`](../experiments/rehedge.py)): emulated
without engine changes by adding each monetization day as an entry date and
iterating to a fixed point (converges in 3–5 passes; 4–30 extra entries over
30 years). It moves every cell by ≤ 0.05pp and leaves the GFC drawdown
unchanged (e.g. bi-monthly, 2%, m = 0.90: −0.41 → −0.45pp, GFC −53.7% →
−53.8%). The re-hedge spends one regular entry's budget (~0.2–0.3% of NAV),
far too small to replace the convexity that was sold: in 2008 the m = 0.90
rule sold puts near SPX 1030 in early October, and SPX then fell to 752.
Universa rolls the *proceeds* into new puts, keeping the hedge's size; see §6.

**IV-aware monthly ladder** ([`vol_aware.py`](../experiments/vol_aware.py)):
skip a month's purchase when ~30-day ATM implied vol (built from the chain;
corr 0.986 with VIX) is expensive, at equal total premium. Mean (worst…best
of 2 calendars), pp/yr vs B&H:

| rule | kept | 1% | 2% | GFC DD @2% | COVID DD @2% |
|---|---|---|---|---|---|
| always | 100% | −0.09 (−0.26…+0.09) | −0.26 (−0.57…+0.04) | −47.0% | −13.0% |
| skip IV > 1y 80th pct | 77% | −0.07 (−0.13…−0.02) | −0.23 (−0.34…−0.12) | −47.3% | −16.1% |
| skip IV > 1y median | 52% | +0.06 (−0.25…+0.38) | +0.01 (−0.54…+0.56) | −47.9% | −15.1% |
| skip IV > 25% | 88% | −0.13 (−0.20…−0.05) | −0.38 (−0.51…−0.25) | −49.4% | −12.7% |

The median rule's +0.15–0.27pp edge over `always` is inside its own
calendar spread; no rule is a reliable improvement.

## 6. Rolling sale proceeds into new puts

[`roll_proceeds.py`](../experiments/roll_proceeds.py), using the engine's
`roll_exit_proceeds_pct = f`: on every put sale (the DTE-30 exit, or a
monetization) a fraction f of the proceeds buys new 25–30% OTM, 90–180 DTE
puts that day, struck off the new spot; the rest goes to stock. Mean
(worst…best calendar), pp/yr vs B&H:

| strategy | f | 1% | 2% | GFC DD @2% | COVID DD @2% |
|---|---|---|---|---|---|
| core, bi-monthly | 0 | **+0.04** (−0.39…+0.50) | **−0.09** (−0.84…+0.72) | **−42.3%** | −14.6% |
| | 0.25 | −0.17 (−0.49…+0.18) | −0.48 (−1.04…+0.16) | −45.5% | −15.0% |
| | 0.50 | −0.41 (−0.61…−0.16) | −0.91 (−1.29…−0.47) | −49.4% | −15.6% |
| | 1.00 | −0.91 (−0.94…−0.88) | −1.89 (−1.93…−1.85) | −57.2% | −19.0% |
| core, monthly | 0 | −0.08 | −0.26 | −47.0% | **−13.0%** |
| | 0.50 | −0.42 | −0.92 | −52.0% | −14.2% |
| monetize 0.95, bi-monthly | 0 | −0.09 | −0.28 | −51.7% | −18.3% |
| | 0.50 | −0.42 | −0.91 | −53.9% | −14.9% |

Rolling hurts in every cell, roughly in proportion to f, and it makes the
GFC drawdown *worse*. With f = 1 the hedge's GFC benefit disappears
entirely (−57%, the same as buy & hold). The 2008 trade log shows why
(odd/1st calendar, 2%, f = 1):

- On 2008-11-20, the closing low, the DTE-30 sale raised $112k, and all of
  it bought 32 SPX 550 puts expiring Feb-09.
- Deep-OTM spreads were at their widest then, so the position lost about
  $25k the same day, from buying at the ask and marking at the bid.
- SPX bottomed at 676, never got near 550, and the puts expired worthless.
- Without the roll, that $112k went into stock at the low.

The only gain is COVID with monetization (−18% → −15%), which is
too small for the cost. Reinvesting crash payoffs in stock, not in more
insurance at crisis prices, is what makes the core strategy work.

## 7. Equal-risk test: the hedge vs holding less stock

[`risk_matched.py`](../experiments/risk_matched.py) tests Spitznagel's actual
claim: not that a hedge beats 100% stocks, but that it reduces risk more
cheaply than the obvious alternative, holding less stock and more T-bills,
and so lets you carry more equity (even leverage) for the same risk.

- **Frontier:** unhedged stock weight 0.40–1.50, the rest in T-bills (or
  borrowed at T-bill + 0.5%), on the same four bi-monthly calendars.
- **Hedged:** 25–30% OTM, 90–180 DTE puts at 1 / 2 / 3.3%/yr, leverage 1.0–1.5×,
  exits at DTE 28 / 30 / 32, all financed the same way.
- **Edge:** hedged CAGR minus the CAGR of the frontier mix with the same mean
  max drawdown (or the same volatility), interpolated.

Edge in pp/yr over **exits 28 and 32** -- the neighbours of the lucky exit
30, which is ~4× them; an earlier version of this table averaged in exit 30
and overstated every cell by ~0.2–0.4pp. Mean (worst…best of 4 calendars),
exit 30 alone in brackets:

| leverage | budget | CAGR | max DD | edge at equal max DD | edge at equal vol |
|---|---|---|---|---|---|
| 1.0× | 1% | 8.07 | −55.1 | −0.07 (−0.51…+0.47) [+0.59] | +0.12 (−0.22…+0.44) [+0.43] |
| 1.0× | 2% | 7.70 | −54.9 | −0.42 (−1.21…+0.61) [+0.61] | −0.06 (−0.59…+0.48) [+0.49] |
| 1.0× | 3.3% | 7.15 | −55.5 | −1.03 [+0.25] | −0.59 [+0.20] |
| 1.2× | 1% | 8.88 | −65.3 | −0.18 [+0.38] | +0.10 [+0.42] |
| 1.2× | 2% | 8.54 | −64.8 | −0.47 [+0.48] | −0.04 [+0.57] |
| 1.5× | 1% | 9.86 | −77.0 | −0.12 [+0.38] | +0.12 [+0.45] |
| 1.5× | 2% | 9.60 | −76.3 | −0.32 [+0.67] | +0.05 [+0.73] |
| buy & hold 1.0× | — | 8.33 | −56.8 | — | — |

- **At equal risk the hedge is a wash or slightly worse.** At 1%/yr it is
  −0.07pp/yr vs the equal-drawdown stock/T-bill mix and +0.12 at equal
  volatility; at 2–3.3% it loses. Every positive edge in this study that
  looked material came from exit 30, the 2008-11-20 sale date.
- **Leverage does not change the verdict.** Levering a hedged portfolio
  raises return and drawdown together, and lands on the same place relative
  to the frontier: at 1.5× and 2% the CAGR is 9.8% with a −75% drawdown,
  about what 1.45× unhedged stock gives.
- **The hedge barely lowers the full-period max drawdown** (−55% vs −57% at
  1×) without the exit-30 luck; the hedged worst drawdown is then often the
  2000–2002 grind, which puts do not help. Most of its risk reduction is in
  fast crashes (COVID), which show up more in volatility than in max
  drawdown — hence the slightly better equal-vol edge.
- **Verdict on the article's thesis:** on SPX 1996–2025, small deep-OTM put
  overlays are a fair-priced way to cut fast-crash risk, about as good as
  holding cash and maybe slightly better. They are not the large free lunch
  the 2008–2024 SPY table showed.

## 8. Spitznagel's own recipe, his valuation switch, dividends, execution

[`spitznagel_recipe.py`](../experiments/spitznagel_recipe.py) closes the gaps
between our reconstruction and what Spitznagel published (his example: 0.5% a
month on 2-month ~30% OTM puts, sold after a month; his backtested edge is
conditional on Tobin's Q being in its top quartile). Everything here uses SPX
**total return** (Shiller dividends) and financing, scored at equal risk
against the stock/T-bill mix like §7. Fills: bid/ask, half the half-spread
(`SpreadFraction(0.5)`), mid -- on entries, exits and marks.

**Core recipe (25–30% OTM, 90–180 DTE), exits 28 and 32** (exit 30 excluded,
as in §7), edge in pp/yr, mean (worst…best of four calendars):

| budget | fills | vs B&H (TR) | edge at equal max DD | edge at equal vol |
|---|---|---|---|---|
| 1% | bid/ask | −0.28 | +0.26 (−0.28…+0.99) | +0.26 (−0.09…+0.62) |
| 1% | mid | −0.17 | +0.50 (−0.15…+1.27) | +0.45 (+0.04…+0.80) |
| 2% | bid/ask | −0.66 | +0.23 (−0.58…+1.00) | +0.16 (−0.35…+0.70) |
| 2% | mid | −0.44 | +0.62 (−0.31…+1.33) | +0.45 (−0.13…+1.00) |
| 3.3% | bid/ask | −1.23 | −0.36 (−1.40…+0.60) | −0.40 |

- **Dividends help the hedge a little, not enough.** Holding T-bills
  instead of stock also forgoes dividends, which the price-index runs (§7)
  ignored. On total return a 1–2% hedge beats the equal-drawdown
  stock/T-bill mix by about +0.25pp/yr at bid/ask fills (+0.5 at mid), but
  the worst calendar is negative in every bid/ask cell: within noise. It
  does not beat 100% stock on return (−0.3 to −0.7).
- **Spitznagel's monthly 2-month-put recipe is worse** than holding longer
  puts: at bid/ask −0.2 to −2.9pp/yr at equal drawdown (1% to his 6%
  budget), about +0.2 to +0.6 only with mid fills. Rolling every month pays
  the spread twelve times a year.
- **The Tobin's Q switch hurts.** Top-quartile Q was off before the GFC (Q
  was 0.88 in 2007, below its 1.07 threshold), so the gated hedge's GFC
  drawdown equals unhedged (−55%): −0.4 to −1.7pp/yr. The median gate is on
  95% of the time and matches always-on.
- **Execution is worth ~0.3pp/yr** for the core recipe (bid/ask → mid), more
  for short-dated recipes. Real fills on deep-OTM SPX puts sit somewhere in
  between.
- **Caveat:** the edge is about the size of the calendar spread, rests on
  two crashes, and its max-drawdown version leans on 2008.

## Status of earlier findings

| Claim (file) | Status |
|---|---|
| "+0.95pp over full history at 25–30/90–180/exit 30" (README, REGIME_RESULTS) | **Superseded** — single calendar + one sale date; calendar-averaged ≈ 0 |
| Article table 2008–2024 SPY, +6pp/yr (RECONSTRUCTION) | **In-sample** — starts just before the GFC; Sharpe/DD columns also moved after the balance fix (see `tests/oracles/test_article_reproduction.py`) |
| "Monthly rolling halved the excess" (full_sweep.py) | **Unreliable** — monthly = both bi-monthly calendars, so halving is what calendar luck predicts |
| "No timing signal beats always-on" (SIGNAL_EXPERIMENTS) | **Direction plausible, numbers superseded** — baseline and signals all on the odd calendar; gated runs also spend more per entry |
| "Monetization hurt −0.7pp" (SIGNAL_EXPERIMENTS) | **Confirmed** on the fixed engine, calendar-averaged (§2) |
| Crash shape decides it (README) | **Stands qualitatively** — fast crashes pay, slow grinds and calm decades bleed |
| Deep-OTM bands unlisted pre-2003 (DATA_COVERAGE) | **Stands** |
| OTM/DTE/budget optima (parallel_sweep_results.csv) | **Single-calendar** — use for shape, not for picking a config |

## Caveats

- Only ~2 crashes paid (2008, 2020) in 30 years; every number here leans on
  them. Treat differences under ~0.3pp/yr as noise.
- SPX price index, no dividends, on both sides.
- Margin calls are not modeled for the levered runs.
- Well over 1,000 backtests have been run on this one history; any single
  "best" row is selected, not discovered.
