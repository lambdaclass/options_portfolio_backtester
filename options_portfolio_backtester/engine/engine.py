"""BacktestEngine — thin orchestrator composing all framework components.

Replaces the monolithic Backtest class with a clean composition of:
- Data providers (stocks, options)
- Strategy (legs, filters, thresholds)
- Execution (cost model, fill model, sizer, signal selector)
- Portfolio (positions, cash, holdings)
- Risk management (constraints)
- Analytics (trade log, balance sheet)
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from options_portfolio_backtester.core.types import (
    Direction, OptionType, Order, Signal, Greeks, Stock, StockAllocation,
    get_order,
)
from options_portfolio_backtester.execution.cost_model import TransactionCostModel, NoCosts
from options_portfolio_backtester.execution.fill_model import FillModel, MarketAtBidAsk
from options_portfolio_backtester.execution.sizer import PositionSizer, CapitalBased
from options_portfolio_backtester.execution.signal_selector import SignalSelector, FirstMatch
from options_portfolio_backtester.portfolio.risk import RiskManager
from options_portfolio_backtester.portfolio.portfolio import Portfolio
from options_portfolio_backtester import _ob_rust
from options_portfolio_backtester.engine.algo_adapters import (
    EngineAlgo,
    EnginePipelineContext,
)

from options_portfolio_backtester.data.providers import HistoricalOptionsData, TiingoData
from options_portfolio_backtester.data.schema import Schema
from options_portfolio_backtester.strategy.strategy import Strategy
from options_portfolio_backtester.strategy.strategy_leg import StrategyLeg

logger = logging.getLogger(__name__)


def _intrinsic_value(option_type: str, strike: float, underlying_price: float) -> float:
    """Compute intrinsic value of an option given spot price.

    For puts:  max(0, strike - spot)
    For calls: max(0, spot - strike)
    """
    if option_type == OptionType.CALL.value:
        return max(0.0, underlying_price - strike)
    return max(0.0, strike - underlying_price)


@dataclass
class _StrategySlot:
    """Configuration and runtime state for one strategy within a multi-strategy engine."""
    strategy: Strategy
    weight: float
    rebalance_freq: int
    rebalance_unit: str = 'BMS'
    check_exits_daily: bool = False
    name: str = ""
    inventory: pd.DataFrame = field(default=None, repr=False)
    rebalance_dates: pd.DatetimeIndex = field(default=None, repr=False)


class BacktestEngine:
    """Orchestrates backtest with pluggable execution components.

    Composes data providers, strategy legs, cost/fill/sizer/selector models,
    and risk constraints into a single backtest loop.  Dispatches to Rust
    for all supported configurations.
    """

    def __init__(
        self,
        allocation: dict[str, float],
        initial_capital: int = 1_000_000,
        shares_per_contract: int = 100,
        cost_model: TransactionCostModel | None = None,
        fill_model: FillModel | None = None,
        sizer: PositionSizer | None = None,
        signal_selector: SignalSelector | None = None,
        risk_manager: RiskManager | None = None,
        algos: list[EngineAlgo] | None = None,
        stop_if_broke: bool = False,
        max_notional_pct: float | None = None,
    ) -> None:
        assets = ("stocks", "options", "cash")
        self._raw_allocation = {a: allocation.get(a, 0.0) for a in assets}
        total_allocation = sum(self._raw_allocation.values())

        self.allocation: dict[str, float] = {}
        for asset in assets:
            self.allocation[asset] = self._raw_allocation[asset] / total_allocation

        self.initial_capital = initial_capital
        self.shares_per_contract = shares_per_contract
        self.cost_model = cost_model or NoCosts()
        self.fill_model = fill_model or MarketAtBidAsk()
        self.sizer = sizer or CapitalBased()
        self.signal_selector = signal_selector or FirstMatch()
        self.risk_manager = risk_manager or RiskManager()
        self.algos = list(algos or [])
        self.stop_if_broke = stop_if_broke
        self.max_notional_pct = max_notional_pct
        # Default exit-checking cadence. `run(check_exits_daily=...)` overrides
        # this when passed explicitly; otherwise run() falls back to this
        # attribute so `engine.check_exits_daily = True` is honored rather than
        # silently ignored.
        self.check_exits_daily: bool = False
        # Immediately redeploy freed cash into stocks after a daily option exit
        # (monetize-and-reinvest), instead of waiting for the next rebalance
        # date. Requires check_exits_daily=True to have any effect.
        self.rebalance_stocks_on_exit: bool = False
        # Enable runtime self-checks (cash-flow + valuation invariants) in the
        # Rust engine. Off by default; intended for tests/debugging.
        self.assert_invariants: bool = False

        self.options_budget_pct: float | None = None
        self.options_budget_annual_pct: float | None = None
        self._stocks: list[Stock] = []
        self._options_strategy: Strategy | None = None
        self._stocks_data: TiingoData | None = None
        self._options_data: HistoricalOptionsData | None = None
        self.run_metadata: dict[str, Any] = {}
        self._event_log_rows: list[dict[str, Any]] = []

    # -- Framing helpers ---------------------------------------------------
    # The two put-overlay framings in the literature map to two
    # different configurations of this engine. Both are configurable via
    # the lower-level allocation + options_budget_pct attributes; these
    # helpers name them in the literature's vocabulary so the caller's
    # intent is unambiguous. See research/spitznagel_spy/FRAMINGS.md in
    # the finance_research repo for the full discussion.

    def use_external_budget(self, annual_pct: float) -> "BacktestEngine":
        """Spitznagel framing: keep 100% in stocks and spend ``annual_pct``
        of portfolio value per year on options on top of that, funded
        externally (treated as a separate budget line item).

        ``annual_pct`` is expressed as a fraction, e.g. ``0.005`` for a
        0.5% annual premium budget. The per-rebalance budget is derived
        from the annual budget and the rebalance frequency. Returns
        ``self`` so calls can be chained.
        """
        total = sum(self._raw_allocation.values()) if self._raw_allocation else 1.0
        self._raw_allocation = {"stocks": 1.0, "options": 0.0, "cash": 0.0}
        self.allocation = dict(self._raw_allocation)
        self.options_budget_annual_pct = annual_pct
        # Keep options_budget_pct in sync for per-rebalance accounting
        # (the engine derives per-rebalance from annual when run()).
        return self

    def use_allocation(self, stocks: float, options: float, cash: float = 0.0) -> "BacktestEngine":
        """Allocation-reducing framing (the configuration AQR / Israelov
        actually test): hold ``stocks`` fraction of the portfolio in
        stocks, ``options`` fraction in puts, and any remainder in cash.
        Fractions must sum to 1.0.

        Returns ``self`` so calls can be chained. Equivalent to setting
        the ``allocation`` dict at construction time, but names the
        intent.
        """
        total = stocks + options + cash
        if not np.isclose(total, 1.0, atol=1e-6):
            raise ValueError(
                f"Allocation must sum to 1.0, got {total:.4f} "
                f"(stocks={stocks}, options={options}, cash={cash})"
            )
        self._raw_allocation = {"stocks": stocks, "options": options, "cash": cash}
        self.allocation = dict(self._raw_allocation)
        self.options_budget_annual_pct = None
        self.options_budget_pct = None
        return self

    # -- Properties (same API as original Backtest) --

    @property
    def stocks(self) -> list[Stock]:
        return self._stocks

    @stocks.setter
    def stocks(self, stocks: list[Stock]) -> None:
        assert np.isclose(sum(s.percentage for s in stocks), 1.0, atol=1e-6)
        self._stocks = list(stocks)

    @property
    def options_strategy(self) -> Strategy | None:
        return self._options_strategy

    @options_strategy.setter
    def options_strategy(self, strat: Strategy) -> None:
        self._options_strategy = strat

    @property
    def stocks_data(self) -> TiingoData | None:
        return self._stocks_data

    @stocks_data.setter
    def stocks_data(self, data: TiingoData) -> None:
        self._stocks_schema = data.schema
        self._stocks_data = data

    @property
    def options_data(self) -> HistoricalOptionsData | None:
        return self._options_data

    @options_data.setter
    def options_data(self, data: HistoricalOptionsData) -> None:
        self._options_schema = data.schema
        self._options_data = data

    # -- Multi-strategy API --

    def add_strategy(
        self,
        strategy: Strategy,
        weight: float,
        rebalance_freq: int,
        rebalance_unit: str = 'BMS',
        check_exits_daily: bool = False,
        name: str | None = None,
    ) -> None:
        """Register a strategy slot for multi-strategy mode.

        Args:
            strategy: The Strategy object (legs + exit thresholds).
            weight: Fraction of options allocation for this strategy.
            rebalance_freq: Rebalance every N periods.
            rebalance_unit: Pandas offset alias (default 'BMS').
            check_exits_daily: Check exits on non-rebalance days.
            name: Human-readable name (auto-generated if omitted).
        """
        if not hasattr(self, '_strategy_slots'):
            self._strategy_slots: list[_StrategySlot] = []
        slot_name = name or f"strategy_{len(self._strategy_slots)}"
        self._strategy_slots.append(_StrategySlot(
            strategy=strategy,
            weight=weight,
            rebalance_freq=rebalance_freq,
            rebalance_unit=rebalance_unit,
            check_exits_daily=check_exits_daily,
            name=slot_name,
        ))

    @property
    def _is_multi_strategy(self) -> bool:
        return hasattr(self, '_strategy_slots') and len(self._strategy_slots) > 0

    # -- Main entry point --

    def run(self, rebalance_freq: int = 0, monthly: bool = False,
            sma_days: int | None = None,
            rebalance_unit: str = 'BMS',
            check_exits_daily: bool | None = None) -> pd.DataFrame:
        """Run the backtest. Returns the trade log DataFrame.

        Args:
            check_exits_daily: When True, evaluate exit filters on every trading
                day (not just rebalancing days).  Positions that match the exit
                filter are closed and cash is updated, but no new entries or
                stock reallocation occurs outside rebalancing days.  When left
                as None (the default), falls back to ``self.check_exits_daily``
                so the attribute form is honored; pass an explicit bool to
                override the attribute for this run.
        """
        if check_exits_daily is None:
            check_exits_daily = self.check_exits_daily
        self._event_log_rows = []
        for algo in self.algos:
            if hasattr(algo, "reset"):
                algo.reset()
        assert self._stocks_data, "Stock data not set"
        assert all(
            stock.symbol in self._stocks_data["symbol"].values
            for stock in self._stocks
        ), "Ensure all stocks in portfolio are present in the data"
        assert self._options_data, "Options data not set"

        # Multi-strategy mode
        if self._is_multi_strategy:
            total_weight = sum(s.weight for s in self._strategy_slots)
            assert abs(total_weight - 1.0) < 1e-6, (
                f"Strategy weights must sum to 1.0, got {total_weight}"
            )
            for slot in self._strategy_slots:
                assert self._options_data.schema == slot.strategy.schema
            return self._run_rust_multi(
                monthly=monthly, sma_days=sma_days,
                check_exits_daily=check_exits_daily,
            )

        assert self._options_strategy, "Options Strategy not set"
        assert self._options_data.schema == self._options_strategy.schema

        option_dates = self._options_data["date"].unique()
        stock_dates = self.stocks_data["date"].unique()
        assert np.array_equal(stock_dates, option_dates)

        # Translate algos to Rust-compatible config fields before dispatch.
        if self.algos:
            self._translate_algos_to_config()

        return self._run_rust(
            rebalance_freq,
            monthly=monthly,
            sma_days=sma_days,
            rebalance_unit=rebalance_unit,
            check_exits_daily=check_exits_daily,
        )

    def get_results(self, data_hash: str | None = None):
        """Return a :class:`BacktestResults` bundling balance, trade log,
        engine config, engine version, and an optional data hash. Call after
        :meth:`run` to capture everything needed to reproduce or audit this
        run.
        """
        from options_portfolio_backtester.results import BacktestResults
        return BacktestResults(
            balance=getattr(self, "balance", pd.DataFrame()),
            trade_log=getattr(self, "trade_log", None),
            config={
                "allocation": dict(self.allocation),
                "initial_capital": self.initial_capital,
                "options_budget_pct": self.options_budget_pct,
                "options_budget_annual_pct": self.options_budget_annual_pct,
                "max_notional_pct": self.max_notional_pct,
                "stop_if_broke": self.stop_if_broke,
            },
            data_hash=data_hash,
        )

    def events_dataframe(self) -> pd.DataFrame:
        """Structured execution event log for debugging and audit.

        The ``data`` dict from each event is flattened into top-level columns
        so that the result can be filtered directly (e.g.
        ``df[df["cash"] > 0]``).
        """
        if not self._event_log_rows:
            return pd.DataFrame(columns=["date", "event", "status"])
        flat = []
        for row in self._event_log_rows:
            entry = {"date": row["date"], "event": row["event"], "status": row["status"]}
            entry.update(row.get("data", {}))
            flat.append(entry)
        return pd.DataFrame(flat)

    def _translate_algos_to_config(self) -> None:
        """Translate algo pipeline into Rust-compatible engine config fields.

        Each algo type maps to an existing Rust feature:
          - EngineRunMonthly → rebalance_unit='BMS' + rebalance_freq=1 (already handled)
          - BudgetPercent → options_budget_pct
          - RangeFilter/SelectByDelta/SelectByDTE/IVRankFilter → entry filter conjunction
          - MaxGreekExposure → risk_constraints (MaxDelta/MaxVega)
          - ExitOnThreshold → profit_pct/loss_pct on strategy

        After translation, self.algos is cleared so the Rust gate passes.
        """
        from options_portfolio_backtester.engine.algo_adapters import (
            EngineRunMonthly, BudgetPercent, RangeFilter,
            MaxGreekExposure, ExitOnThreshold,
        )
        from options_portfolio_backtester.portfolio.risk import RiskManager

        for algo in self.algos:
            if isinstance(algo, EngineRunMonthly):
                # Already handled by rebalance_unit='BMS' + rebalance_freq=1.
                # If user set algos=[EngineRunMonthly()], it's a no-op for Rust.
                pass
            elif isinstance(algo, BudgetPercent):
                self.options_budget_pct = algo.pct
            elif isinstance(algo, RangeFilter):
                # Append range condition to each leg's entry filter as conjunction.
                col, lo, hi = algo.column, algo.min_val, algo.max_val
                clause = f"({col} >= {lo}) & ({col} <= {hi})"
                for leg in self._options_strategy.legs:
                    existing = leg.entry_filter.query
                    if existing:
                        leg.entry_filter.query = f"({existing}) & ({clause})"
                    else:
                        leg.entry_filter.query = clause
            elif isinstance(algo, MaxGreekExposure):
                if algo.max_abs_delta is not None:
                    self.risk_manager.add_constraint(
                        type("MaxDelta", (), {
                            "to_rust_config": lambda self_: {"type": "MaxDelta", "limit": algo.max_abs_delta},
                            "is_allowed": lambda self_, cg, pg, pv, pk: (
                                abs(cg.delta + pg.delta) <= algo.max_abs_delta, ""
                            ),
                        })()
                    )
                if algo.max_abs_vega is not None:
                    self.risk_manager.add_constraint(
                        type("MaxVega", (), {
                            "to_rust_config": lambda self_: {"type": "MaxVega", "limit": algo.max_abs_vega},
                            "is_allowed": lambda self_, cg, pg, pv, pk: (
                                abs(cg.vega + pg.vega) <= algo.max_abs_vega, ""
                            ),
                        })()
                    )
            elif isinstance(algo, ExitOnThreshold):
                import math
                if not math.isinf(algo.profit_pct):
                    self._options_strategy.add_exit_thresholds(
                        profit_pct=algo.profit_pct,
                        loss_pct=self._options_strategy.exit_thresholds[1],
                    )
                if not math.isinf(algo.loss_pct):
                    self._options_strategy.add_exit_thresholds(
                        profit_pct=self._options_strategy.exit_thresholds[0],
                        loss_pct=algo.loss_pct,
                    )
            else:
                raise ValueError(
                    f"Unsupported algo type for Rust dispatch: {type(algo).__name__}. "
                    f"All execution runs through Rust; translate to config fields."
                )
        self.algos.clear()

    def _run_rust(
        self,
        rebalance_freq: int,
        monthly: bool,
        sma_days: int | None,
        rebalance_unit: str = 'BMS',
        check_exits_daily: bool = False,
    ) -> pd.DataFrame:
        """Run the backtest using the Rust full-loop implementation."""
        import math
        import pyarrow as pa
        import polars as pl

        strategy = self._options_strategy

        # Compute rebalance dates for the Rust backtest loop.
        dates_df = (
            pd.DataFrame(self.options_data._data[["quotedate", "volume"]])
            .drop_duplicates("quotedate")
            .set_index("quotedate")
        )
        if rebalance_freq:
            rebalancing_days = pd.to_datetime(
                dates_df.groupby(pd.Grouper(freq=f"{rebalance_freq}{rebalance_unit}"))
                .apply(lambda x: x.index.min())
                .values
            )
            # Pass rebalance dates as i64 nanoseconds (matching Polars Datetime(ns))
            rb_date_ns = [int(d.value) for d in rebalancing_days if not pd.isna(d)]
        else:
            rb_date_ns = []

        opts_date_col = self._options_schema["date"]
        stocks_date_col = self._stocks_schema["date"]
        exp_col = self._options_schema["expiration"]

        # Drop columns Rust never accesses to reduce Arrow conversion cost.
        # Keep underlying_last, impliedvol, openinterest — they can appear in
        # leg filters (strike <= underlying_last * X, impliedvol-based entries,
        # OI-based selectors). Dropping them silently broke any strategy that
        # referenced them with a "column not found" Rust error rather than a
        # clear API error. The only safe drops are columns that no filter or
        # selector ever inspects.
        _drop_cols = {"last", "optionalias"}
        opts_df = self._options_data._data
        to_drop = [c for c in _drop_cols if c in opts_df.columns]
        opts_src = opts_df.drop(columns=to_drop) if to_drop else opts_df

        # Convert pandas → PyArrow → Polars (avoids intermediate copies).
        opts_pl = pl.from_arrow(pa.Table.from_pandas(opts_src, preserve_index=False))
        stocks_pl = pl.from_arrow(
            pa.Table.from_pandas(self._stocks_data._data, preserve_index=False)
        )

        leg_configs = []
        for leg in strategy.legs:
            lc = {
                "name": leg.name,
                "entry_filter": leg.entry_filter.query,
                "exit_filter": leg.exit_filter.query,
                "direction": leg.direction.price_column,
                "type": leg.type.value,
                "entry_sort_col": leg.entry_sort[0] if leg.entry_sort else None,
                "entry_sort_asc": leg.entry_sort[1] if leg.entry_sort else True,
            }
            # Per-leg overrides
            leg_sel = getattr(leg, 'signal_selector', None)
            if leg_sel is not None and hasattr(leg_sel, 'to_rust_config'):
                lc["signal_selector"] = leg_sel.to_rust_config()
            leg_fill = getattr(leg, 'fill_model', None)
            if leg_fill is not None and hasattr(leg_fill, 'to_rust_config'):
                lc["fill_model"] = leg_fill.to_rust_config()
            leg_configs.append(lc)

        config = {
            "allocation": self.allocation,
            "initial_capital": float(self.initial_capital),
            "shares_per_contract": self.shares_per_contract,
            "rebalance_dates": rb_date_ns,
            "legs": leg_configs,
            "profit_pct": (
                strategy.exit_thresholds[0]
                if strategy.exit_thresholds[0] != math.inf else None
            ),
            "loss_pct": (
                strategy.exit_thresholds[1]
                if strategy.exit_thresholds[1] != math.inf else None
            ),
            "stocks": [(s.symbol, s.percentage) for s in self._stocks],
            "cost_model": self.cost_model.to_rust_config(),
            "fill_model": self.fill_model.to_rust_config(),
            "signal_selector": self.signal_selector.to_rust_config(),
            "risk_constraints": [c.to_rust_config() for c in self.risk_manager.constraints],
            "sma_days": sma_days,
            "options_budget_pct": self.options_budget_pct,
            "options_budget_annual_pct": self.options_budget_annual_pct,
            "stop_if_broke": self.stop_if_broke,
            "max_notional_pct": self.max_notional_pct,
            "check_exits_daily": check_exits_daily,
            "rebalance_stocks_on_exit": self.rebalance_stocks_on_exit,
            "assert_invariants": self.assert_invariants,
        }

        schema_mapping = {
            "contract": self._options_schema["contract"],
            "date": opts_date_col,
            "stocks_date": stocks_date_col,
            "stocks_symbol": self._stocks_schema["symbol"],
            "stocks_price": self._stocks_schema["adjClose"],
            # Unadjusted close for option intrinsic value (strikes are raw
            # prices). Falls back to adjClose if the data lacks a raw close.
            "stocks_unadj_price": (
                self._stocks_schema["close"]
                if "close" in self._stocks_schema
                else self._stocks_schema["adjClose"]
            ),
            "underlying": self._options_schema["underlying"],
            "expiration": self._options_schema["expiration"],
            "type": self._options_schema["type"],
            "strike": self._options_schema["strike"],
        }

        balance_pl, trade_log_pl, stats = _ob_rust.run_backtest_py(
            opts_pl, stocks_pl, config, schema_mapping,
        )

        # Convert trade log from flat columns to MultiIndex
        trade_log_pd = trade_log_pl.to_pandas()
        self.trade_log = self._flat_trade_log_to_multiindex(trade_log_pd)

        # Convert balance
        self.balance = balance_pl.to_pandas()
        if "date" in self.balance.columns:
            self.balance["date"] = pd.to_datetime(self.balance["date"])
            self.balance.set_index("date", inplace=True)

        # Add initial balance row (day before first rebalance) — matches Python
        initial_date = self.stocks_data.start_date - pd.Timedelta(1, unit="day")
        initial_row = pd.DataFrame(
            {"total capital": self.initial_capital, "cash": float(self.initial_capital)},
            index=[initial_date],
        )
        self.balance = pd.concat([initial_row, self.balance], sort=False)
        for col_name in self.balance.columns:
            self.balance[col_name] = pd.to_numeric(self.balance[col_name], errors="coerce")

        # Ensure per-stock columns exist (match Python's balance format)
        for stock in self._stocks:
            sym = stock.symbol
            if sym not in self.balance.columns:
                self.balance[sym] = 0.0
            if f"{sym} qty" not in self.balance.columns:
                self.balance[f"{sym} qty"] = 0.0
        for col_name in ["options qty", "stocks qty", "calls capital", "puts capital"]:
            if col_name not in self.balance.columns:
                self.balance[col_name] = 0.0

        # Add derived columns matching Python output
        self.balance["options capital"] = (
            self.balance["calls capital"] + self.balance["puts capital"]
        ).fillna(0)
        stock_cols = [s.symbol for s in self._stocks]
        self.balance["stocks capital"] = sum(
            self.balance.get(c, 0) for c in stock_cols
        )
        first_idx = self.balance.index[0]
        self.balance.loc[first_idx, "stocks capital"] = 0
        self.balance.loc[first_idx, "options capital"] = 0
        self.balance["total capital"] = (
            self.balance["cash"]
            + self.balance["stocks capital"]
            + self.balance["options capital"]
        )
        self.balance["% change"] = self.balance["total capital"].pct_change()
        self.balance["accumulated return"] = (1.0 + self.balance["% change"]).cumprod()

        # Set current_cash to match Python loop's final state after rebalancing
        # (after the loop, all capital is allocated to stocks/options/cash per allocation)
        final_total = self.balance["total capital"].iloc[-1]
        self.current_cash = self.allocation["cash"] * final_total
        self._initialize_inventories()
        self._portfolio = Portfolio(initial_cash=self.current_cash)
        self._attach_run_metadata(
            rebalance_freq=rebalance_freq,
            monthly=monthly,
            sma_days=sma_days,
        )

        return self.trade_log

    def _run_rust_multi(
        self,
        monthly: bool = False,
        sma_days: int | None = None,
        check_exits_daily: bool = False,
    ) -> pd.DataFrame:
        """Run multi-strategy backtest using Rust backend."""
        import math
        import pyarrow as pa
        import polars as pl

        opts_date_col = self._options_schema["date"]
        stocks_date_col = self._stocks_schema["date"]

        # Drop unused columns for Arrow conversion speed. Keep underlying_last,
        # impliedvol, openinterest — see comment in _run_rust for why dropping
        # them silently broke filter columns and selectors.
        _drop_cols = {"last", "optionalias"}
        opts_df = self._options_data._data
        to_drop = [c for c in _drop_cols if c in opts_df.columns]
        opts_src = opts_df.drop(columns=to_drop) if to_drop else opts_df

        opts_pl = pl.from_arrow(pa.Table.from_pandas(opts_src, preserve_index=False))
        stocks_pl = pl.from_arrow(
            pa.Table.from_pandas(self._stocks_data._data, preserve_index=False)
        )

        # Compute per-slot rebalance dates
        dates_df = (
            pd.DataFrame(self.options_data._data[["quotedate", "volume"]])
            .drop_duplicates("quotedate")
            .set_index("quotedate")
        )

        slot_configs = []
        for slot in self._strategy_slots:
            if slot.rebalance_freq:
                rb_dates = pd.to_datetime(
                    dates_df.groupby(
                        pd.Grouper(freq=f"{slot.rebalance_freq}{slot.rebalance_unit}")
                    ).apply(lambda x: x.index.min()).values
                )
                rb_date_ns = [int(d.value) for d in rb_dates if not pd.isna(d)]
            else:
                rb_date_ns = []

            leg_configs = []
            for leg in slot.strategy.legs:
                lc = {
                    "name": leg.name,
                    "entry_filter": leg.entry_filter.query,
                    "exit_filter": leg.exit_filter.query,
                    "direction": leg.direction.price_column,
                    "type": leg.type.value,
                    "entry_sort_col": leg.entry_sort[0] if leg.entry_sort else None,
                    "entry_sort_asc": leg.entry_sort[1] if leg.entry_sort else True,
                }
                leg_sel = getattr(leg, 'signal_selector', None)
                if leg_sel is not None and hasattr(leg_sel, 'to_rust_config'):
                    lc["signal_selector"] = leg_sel.to_rust_config()
                leg_fill = getattr(leg, 'fill_model', None)
                if leg_fill is not None and hasattr(leg_fill, 'to_rust_config'):
                    lc["fill_model"] = leg_fill.to_rust_config()
                leg_configs.append(lc)

            slot_configs.append({
                "name": slot.name,
                "legs": leg_configs,
                "weight": slot.weight,
                "rebalance_dates": rb_date_ns,
                "profit_pct": (
                    slot.strategy.exit_thresholds[0]
                    if slot.strategy.exit_thresholds[0] != math.inf else None
                ),
                "loss_pct": (
                    slot.strategy.exit_thresholds[1]
                    if slot.strategy.exit_thresholds[1] != math.inf else None
                ),
                "check_exits_daily": slot.check_exits_daily,
            })

        config = {
            "allocation": self.allocation,
            "initial_capital": float(self.initial_capital),
            "shares_per_contract": self.shares_per_contract,
            "rebalance_dates": [],  # Not used for multi-strategy; per-slot instead
            "legs": [],  # Not used for multi-strategy; per-slot instead
            "stocks": [(s.symbol, s.percentage) for s in self._stocks],
            "cost_model": self.cost_model.to_rust_config(),
            "fill_model": self.fill_model.to_rust_config(),
            "signal_selector": self.signal_selector.to_rust_config(),
            "risk_constraints": [c.to_rust_config() for c in self.risk_manager.constraints],
            "sma_days": sma_days,
            "options_budget_pct": self.options_budget_pct,
            "options_budget_annual_pct": self.options_budget_annual_pct,
            "stop_if_broke": self.stop_if_broke,
            "max_notional_pct": self.max_notional_pct,
            "check_exits_daily": check_exits_daily,
            "rebalance_stocks_on_exit": self.rebalance_stocks_on_exit,
            "assert_invariants": self.assert_invariants,
        }

        schema_mapping = {
            "contract": self._options_schema["contract"],
            "date": opts_date_col,
            "stocks_date": stocks_date_col,
            "stocks_symbol": self._stocks_schema["symbol"],
            "stocks_price": self._stocks_schema["adjClose"],
            # Unadjusted close for option intrinsic value (strikes are raw
            # prices). Falls back to adjClose if the data lacks a raw close.
            "stocks_unadj_price": (
                self._stocks_schema["close"]
                if "close" in self._stocks_schema
                else self._stocks_schema["adjClose"]
            ),
            "underlying": self._options_schema["underlying"],
            "expiration": self._options_schema["expiration"],
            "type": self._options_schema["type"],
            "strike": self._options_schema["strike"],
        }

        balance_pl, trade_log_pl, stats = _ob_rust.run_multi_strategy_py(
            opts_pl, stocks_pl, config, schema_mapping, slot_configs,
        )

        # Convert trade log
        trade_log_pd = trade_log_pl.to_pandas()
        self.trade_log = self._flat_trade_log_to_multiindex(trade_log_pd)

        # Convert balance
        self.balance = balance_pl.to_pandas()
        if "date" in self.balance.columns:
            self.balance["date"] = pd.to_datetime(self.balance["date"])
            self.balance.set_index("date", inplace=True)

        # Add initial balance row
        initial_date = self.stocks_data.start_date - pd.Timedelta(1, unit="day")
        initial_row = pd.DataFrame(
            {"total capital": self.initial_capital, "cash": float(self.initial_capital)},
            index=[initial_date],
        )
        self.balance = pd.concat([initial_row, self.balance], sort=False)
        for col_name in self.balance.columns:
            self.balance[col_name] = pd.to_numeric(self.balance[col_name], errors="coerce")

        # Ensure per-stock columns exist
        for stock in self._stocks:
            sym = stock.symbol
            if sym not in self.balance.columns:
                self.balance[sym] = 0.0
            if f"{sym} qty" not in self.balance.columns:
                self.balance[f"{sym} qty"] = 0.0
        for col_name in ["options qty", "stocks qty", "calls capital", "puts capital"]:
            if col_name not in self.balance.columns:
                self.balance[col_name] = 0.0

        # Add derived columns
        self.balance["options capital"] = (
            self.balance["calls capital"] + self.balance["puts capital"]
        ).fillna(0)
        stock_cols = [s.symbol for s in self._stocks]
        self.balance["stocks capital"] = sum(
            self.balance.get(c, 0) for c in stock_cols
        )
        first_idx = self.balance.index[0]
        self.balance.loc[first_idx, "stocks capital"] = 0
        self.balance.loc[first_idx, "options capital"] = 0
        self.balance["total capital"] = (
            self.balance["cash"]
            + self.balance["stocks capital"]
            + self.balance["options capital"]
        )
        self.balance["% change"] = self.balance["total capital"].pct_change()
        self.balance["accumulated return"] = (1.0 + self.balance["% change"]).cumprod()

        final_total = self.balance["total capital"].iloc[-1]
        self.current_cash = self.allocation["cash"] * final_total
        self._attach_run_metadata(
            rebalance_freq=0,
            monthly=monthly,
            sma_days=sma_days,
        )

        return self.trade_log

    def _attach_run_metadata(
        self,
        rebalance_freq: int,
        monthly: bool,
        sma_days: int | None,
    ) -> None:
        metadata = self._build_run_metadata(
            rebalance_freq=rebalance_freq,
            monthly=monthly,
            sma_days=sma_days,
        )
        self.run_metadata = metadata
        self.balance.attrs["run_metadata"] = metadata
        self.trade_log.attrs["run_metadata"] = metadata

    def _build_run_metadata(
        self,
        rebalance_freq: int,
        monthly: bool,
        sma_days: int | None,
    ) -> dict[str, Any]:
        stocks = [
            {"symbol": stock.symbol, "percentage": float(stock.percentage)}
            for stock in self._stocks
        ]
        run_config = {
            "allocation": {k: float(v) for k, v in self.allocation.items()},
            "initial_capital": float(self.initial_capital),
            "shares_per_contract": int(self.shares_per_contract),
            "rebalance_freq": int(rebalance_freq),
            "monthly": bool(monthly),
            "sma_days": int(sma_days) if sma_days is not None else None,
            "stocks": stocks,
        }
        data_snapshot = self._data_snapshot()
        return {
            "framework": "options_portfolio_backtester.engine.BacktestEngine",
            "git_sha": self._git_sha(),
            "run_at_utc": datetime.now(timezone.utc).isoformat(),
            "config_hash": self._sha256_json(run_config),
            "data_snapshot_hash": self._sha256_json(data_snapshot),
            "data_snapshot": data_snapshot,
        }

    def _data_snapshot(self) -> dict[str, Any]:
        options_dates = self._options_data["date"]
        stocks_dates = self._stocks_data["date"]
        return {
            "options_rows": int(len(self._options_data._data)),
            "stocks_rows": int(len(self._stocks_data._data)),
            "options_date_start": pd.Timestamp(options_dates.min()).isoformat(),
            "options_date_end": pd.Timestamp(options_dates.max()).isoformat(),
            "stocks_date_start": pd.Timestamp(stocks_dates.min()).isoformat(),
            "stocks_date_end": pd.Timestamp(stocks_dates.max()).isoformat(),
            "options_columns": list(self._options_data._data.columns),
            "stocks_columns": list(self._stocks_data._data.columns),
        }

    @staticmethod
    def _sha256_json(payload: dict[str, Any]) -> str:
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    @staticmethod
    def _git_sha() -> str:
        repo_root = Path(__file__).resolve().parents[2]
        try:
            proc = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo_root,
                check=True,
                capture_output=True,
                text=True,
            )
            return proc.stdout.strip()
        except Exception:
            return "unknown"

    def _flat_trade_log_to_multiindex(self, flat_df: pd.DataFrame) -> pd.DataFrame:
        """Convert flat 'leg__field' columns from Rust to MultiIndex DataFrame."""
        if flat_df.empty:
            return pd.DataFrame()
        tuples = []
        for c in flat_df.columns:
            if "__" in c:
                parts = c.split("__", 1)
                tuples.append((parts[0], parts[1]))
            else:
                tuples.append(("", c))
        flat_df.columns = pd.MultiIndex.from_tuples(tuples)
        return flat_df

    # -- Internals (same logic as original, with pluggable components) --

    def _initialize_inventories(self) -> None:
        columns = pd.MultiIndex.from_product(
            [
                [leg.name for leg in self._options_strategy.legs],
                ["contract", "underlying", "expiration", "type", "strike", "cost", "order"],
            ]
        )
        totals = pd.MultiIndex.from_product([["totals"], ["cost", "qty", "date"]])
        self._options_inventory: pd.DataFrame = pd.DataFrame(
            columns=pd.Index(columns.tolist() + totals.tolist())
        )
        self._stocks_inventory: pd.DataFrame = pd.DataFrame(
            columns=["symbol", "price", "qty"]
        )
        # Portfolio dataclass — dual-write alongside legacy DataFrames
        self._portfolio = Portfolio(initial_cash=0.0)

    def _current_options_capital(self, options, stocks):
        options_value = self._get_current_option_quotes(options)
        values_by_row: Any = [0] * len(options_value[0])
        if len(options_value[0]) != 0:
            sym_col = self._stocks_schema["symbol"]
            # Use unadjusted close for intrinsic value — strikes are raw prices
            _close_col = self._stocks_schema["close"] if "close" in self._stocks_schema else None
            price_col = _close_col if (_close_col and _close_col in stocks.columns) else self._stocks_schema["adjClose"]
            for i, leg in enumerate(self._options_strategy.legs):
                cost_series = options_value[i]["cost"].copy()
                # Replace NaN (missing contracts) with intrinsic value
                if cost_series.isna().any():
                    inv_leg = self._options_inventory[leg.name]
                    for idx in cost_series.index[cost_series.isna()]:
                        opt_type = inv_leg.at[idx, "type"]
                        strike = inv_leg.at[idx, "strike"]
                        underlying = inv_leg.at[idx, "underlying"]
                        spot_match = stocks.loc[stocks[sym_col] == underlying, price_col]
                        spot = spot_match.iloc[0] if len(spot_match) > 0 else 0.0
                        iv = _intrinsic_value(opt_type, float(strike), float(spot))
                        cash_sign = -1.0 if ~leg.direction == Direction.SELL else 1.0
                        cost_series.at[idx] = cash_sign * iv * self.shares_per_contract
                values_by_row += cost_series.values
            total: float = -sum(values_by_row * self._options_inventory["totals"]["qty"].values)
        else:
            total = 0
        return total

    def _get_current_option_quotes(self, options):
        current_options_quotes: list[pd.DataFrame] = []
        for leg in self._options_strategy.legs:
            inventory_leg = self._options_inventory[leg.name]
            leg_options = inventory_leg[["contract"]].merge(
                options, how="left",
                left_on="contract", right_on=leg.schema["contract"],
            )
            leg_options.index = self._options_inventory.index
            leg_options["order"] = get_order(leg.direction, Signal.EXIT)
            leg_options["cost"] = leg_options[self._options_schema[(~leg.direction).price_column]]

            if ~leg.direction == Direction.SELL:
                leg_options["cost"] = -leg_options["cost"]
            leg_options["cost"] *= self.shares_per_contract
            current_options_quotes.append(leg_options)
        return current_options_quotes

    def __repr__(self) -> str:
        return (
            f"BacktestEngine(capital={self.initial_capital}, "
            f"allocation={self.allocation}, "
            f"cost_model={self.cost_model.__class__.__name__})"
        )

