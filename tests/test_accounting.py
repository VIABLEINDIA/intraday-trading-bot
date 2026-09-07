"""Tests for the arithmetic that decides whether a strategy looks profitable.

Position sizing, transaction costs and slippage are what turned a strategy that
appeared to make +54% a year into one that loses money. An error anywhere here
does not crash -- it quietly changes the conclusion -- so the sign and rough
magnitude of each piece is pinned down.

The signal-replay rules are covered too, including the deliberate choice to
charge a bar that spans both stop and target as the stop.
"""

import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from src.analysis.transaction_costs import TransactionCostCalculator
from src.utils.timezone import IST

ROOT = Path(__file__).resolve().parents[1]


def _load(name, relpath):
    """Import a scripts/ module, which is not an installed package."""
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


backtest = _load("_bt", "scripts/backtest.py")
evaluate = _load("_ev", "scripts/evaluate_signals.py")


@pytest.fixture
def costs():
    return TransactionCostCalculator("zerodha")


class TestPositionSizing:
    def test_quantity_fits_the_allocation(self, costs):
        trade = {"entry_price": 100.0, "exit_price": 101.0, "direction": "LONG", "pnl": 1.0}
        out = backtest.size_and_cost(trade, capital=100000, alloc_pct=25, costs=costs)
        assert out["quantity"] == 250  # 25,000 / 100

    def test_quantity_never_exceeds_the_notional(self, costs):
        trade = {"entry_price": 333.0, "exit_price": 334.0, "direction": "LONG", "pnl": 1.0}
        out = backtest.size_and_cost(trade, capital=100000, alloc_pct=25, costs=costs)
        assert out["quantity"] * 333.0 <= 25000

    def test_unpriced_trade_is_sized_to_zero_not_crashed(self, costs):
        trade = {"entry_price": 0.0, "exit_price": 0.0, "direction": "LONG", "pnl": 0.0}
        out = backtest.size_and_cost(trade, capital=100000, alloc_pct=25, costs=costs)
        assert out["quantity"] == 0 and out["fees"] == 0.0


class TestCosts:
    def test_costs_are_charged_and_reduce_net(self, costs):
        trade = {"entry_price": 100.0, "exit_price": 101.0, "direction": "LONG", "pnl": 1.0}
        out = backtest.size_and_cost(trade, capital=100000, alloc_pct=25, costs=costs)
        assert out["fees"] > 0
        assert out["net_rupees"] == pytest.approx(out["gross_rupees"] - out["fees"])

    def test_intraday_round_trip_is_around_ten_basis_points(self, costs):
        # The figure the whole project's conclusion rests on.
        result = costs.calculate_costs(quantity=250, buy_price=100.0, sell_price=100.0)
        assert 0.0005 < result["total"] / 25000 < 0.0015

    def test_short_and_long_of_equal_size_cost_the_same(self, costs):
        long_t = {"entry_price": 100.0, "exit_price": 99.0, "direction": "LONG", "pnl": -1.0}
        short_t = {"entry_price": 100.0, "exit_price": 99.0, "direction": "SHORT", "pnl": 1.0}
        a = backtest.size_and_cost(long_t, 100000, 25, costs)
        b = backtest.size_and_cost(short_t, 100000, 25, costs)
        assert a["fees"] == pytest.approx(b["fees"], rel=0.02)

    def test_brokerage_caps_so_larger_trades_cost_relatively_less(self, costs):
        small = costs.calculate_costs(250, 100.0, 100.0)["total"] / 25000
        large = costs.calculate_costs(4000, 100.0, 100.0)["total"] / 400000
        assert large < small


class TestSlippage:
    def test_slippage_reduces_a_winning_long(self, costs):
        trade = {"entry_price": 100.0, "exit_price": 101.0, "direction": "LONG", "pnl": 1.0}
        clean = backtest.size_and_cost(trade, 100000, 25, costs, slippage_bps=0)
        slipped = backtest.size_and_cost(trade, 100000, 25, costs, slippage_bps=5)
        assert slipped["net_rupees"] < clean["net_rupees"]

    def test_slippage_also_hurts_a_short(self, costs):
        """It is a cost of crossing the spread, not a directional bet."""
        trade = {"entry_price": 100.0, "exit_price": 99.0, "direction": "SHORT", "pnl": 1.0}
        clean = backtest.size_and_cost(trade, 100000, 25, costs, slippage_bps=0)
        slipped = backtest.size_and_cost(trade, 100000, 25, costs, slippage_bps=5)
        assert slipped["net_rupees"] < clean["net_rupees"]

    def test_charge_is_per_leg(self, costs):
        # 10bp on each of two ~100-rupee legs is ~0.20 points per share.
        trade = {"entry_price": 100.0, "exit_price": 100.0, "direction": "LONG", "pnl": 0.0}
        out = backtest.size_and_cost(trade, 100000, 25, costs, slippage_bps=10)
        assert out["points"] == pytest.approx(-0.20, abs=1e-9)

    def test_enough_slippage_flips_a_winner_to_a_loser(self, costs):
        trade = {"entry_price": 100.0, "exit_price": 100.3, "direction": "LONG", "pnl": 0.3}
        assert backtest.size_and_cost(trade, 100000, 25, costs, 0)["gross_rupees"] > 0
        assert backtest.size_and_cost(trade, 100000, 25, costs, 30)["gross_rupees"] < 0


def bar(hhmm, high, low, close, date="2026-09-04"):
    return {"timestamp": f"{date}T{hhmm}:00+05:30",
            "open": close, "high": high, "low": low, "close": close, "volume": 1000}


class TestSignalReplay:
    start = datetime(2026, 9, 4, 9, 30, tzinfo=IST)

    def test_target_is_taken_when_only_target_is_touched(self):
        out = evaluate.replay([bar("09:31", 105, 99, 104)], self.start,
                              "LONG", entry=100, stop=95, target=103)
        assert (out["exit"], out["reason"]) == (103, "TARGET")

    def test_stop_is_taken_when_only_stop_is_touched(self):
        out = evaluate.replay([bar("09:31", 101, 94, 96)], self.start,
                              "LONG", entry=100, stop=95, target=110)
        assert (out["exit"], out["reason"]) == (95, "STOP")

    def test_ambiguous_bar_is_charged_as_the_stop(self):
        """Intrabar order is unknowable; assuming the good fill flatters results."""
        out = evaluate.replay([bar("09:31", 111, 94, 100)], self.start,
                              "LONG", entry=100, stop=95, target=110)
        assert out["reason"] == "STOP"

    def test_short_side_levels_are_inverted(self):
        out = evaluate.replay([bar("09:31", 101, 89, 90)], self.start,
                              "SHORT", entry=100, stop=105, target=90)
        assert (out["exit"], out["reason"]) == (90, "TARGET")

    def test_untouched_levels_square_off_at_1515(self):
        session = [bar("09:31", 101, 99, 100), bar("15:15", 101, 99, 100.5)]
        out = evaluate.replay(session, self.start, "LONG", 100, stop=90, target=110)
        assert out["reason"] == "SQUARE_OFF" and out["exit"] == 100.5

    def test_bars_before_the_signal_are_ignored(self):
        session = [bar("09:20", 120, 80, 100), bar("09:31", 101, 99, 100.2)]
        out = evaluate.replay(session, self.start, "LONG", 100, stop=95, target=110)
        assert out["reason"] != "STOP"

    def test_missing_levels_run_to_the_end(self):
        session = [bar("09:31", 101, 99, 100), bar("09:32", 102, 98, 101)]
        out = evaluate.replay(session, self.start, "LONG", 100, stop=None, target=None)
        assert out["reason"] == "SESSION_END" and out["exit"] == 101


class TestSessionSlicing:
    @staticmethod
    def _candles():
        out = []
        for d in ("2026-09-02", "2026-09-03", "2026-09-04"):
            out += [bar("09:15", 101, 99, 100, d), bar("09:18", 102, 98, 101, d)]
        return out

    def test_slice_returns_target_plus_prior_session(self):
        idx = backtest.index_by_session(self._candles())
        ordered = sorted(idx)
        got = backtest.slice_session(idx, ordered, "2026-09-04")
        assert sorted({c["timestamp"][:10] for c in got}) == ["2026-09-03", "2026-09-04"]

    def test_target_session_is_last_so_the_engine_infers_it(self):
        idx = backtest.index_by_session(self._candles())
        got = backtest.slice_session(idx, sorted(idx), "2026-09-04")
        assert got[-1]["timestamp"][:10] == "2026-09-04"

    def test_first_session_has_no_prior_and_is_skipped(self):
        idx = backtest.index_by_session(self._candles())
        assert backtest.slice_session(idx, sorted(idx), "2026-09-02") == []

    def test_unknown_date_yields_nothing(self):
        idx = backtest.index_by_session(self._candles())
        assert backtest.slice_session(idx, sorted(idx), "2026-09-07") == []
