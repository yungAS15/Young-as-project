from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from backtest.engine import BacktestEngine
from backtest.metrics import SimTrade, compute_report, format_report, validate
from config import ScoreWeights, Settings, StrategyParams
from data.candles import Candle
from data.synthetic import sine_trend_m1
from strategy.fibonacci import Zone
from strategy.liquidity import Sweep
from strategy.market_structure import Bias, BiasResult, Choch
from strategy.signal_engine import NoSetup, NoTrade, Setup, TradeSignal

T0 = datetime(2025, 1, 6, 8, 0, tzinfo=timezone.utc)


def flat_m1(n: int, price: float = 2000.0) -> list[Candle]:
    return [Candle(T0 + timedelta(minutes=i), price, price + 0.2, price - 0.2, price, 10, 0.3) for i in range(n)]


def make_setup(now: datetime) -> Setup:
    return Setup(
        setup_id="BUY-TEST", direction="BUY", bias=BiasResult(Bias.BULLISH, "", None, None, None),
        sweep=Sweep(1995, "RANGE_LOW", 0, 1994, 0, "wick_rejection"), sweep_time=T0,
        choch=Choch(2001, 0, 0, 2003, 0), choch_time=T0, impulse_low=1994, impulse_high=2003,
        ote=Zone(1996, 1998), fvgs=(), order_block=None, atr_m15=2.0, atr_h4=10.0, armed_at=now,
        expires_at=now + timedelta(days=1))


class StubEngine:
    """Arme un setup à la première M15 close, signale à la M1 `signal_at` (index)."""

    def __init__(self, signal_at: int, sl: float, tp: float, entry: float = 2000.3):
        self.signal_at = signal_at
        self.sl, self.tp, self.entry = sl, tp, entry
        self.calls = 0
        self.lookahead_violations = 0

    def find_setup(self, h4, m15):
        if not m15:
            return NoSetup("vide")
        return make_setup(m15[-1].close_time("M15"))

    def confirm_entry(self, setup, h4, m15, m1, vwaps, spread):
        now = m1[-1].close_time("M1")
        if m15 and m15[-1].close_time("M15") > now:
            self.lookahead_violations += 1
        if h4 and h4[-1].close_time("H4") > now:
            self.lookahead_violations += 1
        idx = int((m1[-1].time - T0).total_seconds() // 60)
        self.calls += 1
        if idx != self.signal_at:
            return NoTrade("Pas de confirmation M1", setup.setup_id)
        return TradeSignal(setup.setup_id, "BUY", now, self.entry, self.sl, self.tp,
                           (self.tp - self.entry) / (self.entry - self.sl), 8.0, "SCORE 8/10", {}, [], [], "", "",
                           "", {}, {"fvg": True, "ote": True}, {}, spread)


def settings(**kw):
    base = Settings(backtest_slippage_points=0.1, backtest_default_spread=0.3, backtest_quote_to_account_rate=1.0,
                    backtest_initial_capital_chf=10000.0, max_spread_points=1.0,
                    strategy=StrategyParams(min_score=7))
    return replace(base, **kw)


def run(m1, eng, **kw):
    return BacktestEngine(settings(**kw), engine=eng).run(m1)


def test_entry_next_bar_open_with_spread_and_slippage_then_tp():
    m1 = flat_m1(60)
    m1[31] = replace(m1[31], open=2000.0)
    m1[40] = replace(m1[40], high=2011.5)          # TP touché
    eng = StubEngine(signal_at=30, sl=1995.0, tp=2011.0)
    res = run(m1, eng)
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t.entry_time == m1[31].time
    assert t.entry == pytest.approx(2000.0 + 0.3 + 0.1)   # open + spread + slippage
    assert t.exit_reason == "TP" and t.exit_price == 2011.0
    assert t.pnl_ccy > 0
    assert eng.lookahead_violations == 0


def test_stop_loss_with_slippage():
    m1 = flat_m1(60)
    m1[45] = replace(m1[45], low=1994.0)
    res = run(m1, StubEngine(30, 1995.0, 2011.0))
    t = res.trades[0]
    assert t.exit_reason == "SL" and t.exit_price == pytest.approx(1994.9)
    assert t.r_multiple < -0.95


def test_same_bar_sl_and_tp_assumes_sl():
    m1 = flat_m1(60)
    m1[45] = replace(m1[45], low=1994.0, high=2012.0)
    res = run(m1, StubEngine(30, 1995.0, 2011.0))
    assert res.trades[0].exit_reason == "SL"


def test_gap_through_stop_exits_at_open():
    m1 = flat_m1(60)
    m1[45] = replace(m1[45], open=1990.0, low=1989.0, high=1991.0, close=1990.5)
    res = run(m1, StubEngine(30, 1995.0, 2011.0))
    assert res.trades[0].exit_reason == "SL (gap)" and res.trades[0].exit_price == pytest.approx(1989.9)


def test_risk_manager_applies_in_backtest_rr_below_min():
    res = run(flat_m1(60), StubEngine(30, 1995.0, 2005.0))   # RR < 2
    assert res.trades == []
    assert any("rr_min" in r for r in res.refusals)


def test_position_size_uses_risk_rule():
    m1 = flat_m1(60)
    m1[40] = replace(m1[40], high=2011.5)
    res = run(m1, StubEngine(30, 1995.0, 2011.0))
    t = res.trades[0]
    # capital 10000 → risque min(100, 1 %=100)=100 ; distance 5.3+0.1 buffer → 18.51
    assert t.size == pytest.approx(18.51)
    assert t.risk_ccy <= 100.0


def test_real_engine_on_synthetic_data_runs_without_lookahead_crash():
    m1 = sine_trend_m1(days=12)
    res = BacktestEngine(Settings()).run(m1)
    assert isinstance(res.report, dict)
    assert "validation" in res.report


def _mk(r: float, score: float = 8.0, h: int = 10) -> SimTrade:
    t = SimTrade("x", T0, T0.replace(hour=h), 2000, 1995, 2010, 1, 100.0, score, 2.0, {"fvg": True})
    t.pnl_ccy = r * 100.0
    t.exit_reason = "TP" if r > 0 else "SL"
    return t


def test_metrics_values():
    trades = [_mk(2), _mk(-1), _mk(2), _mk(-1), _mk(-1, score=9)]
    rep = compute_report(trades, 10000, settings=Settings())
    assert rep["trades"] == 5
    assert rep["winrate"] == pytest.approx(0.4)
    assert rep["expectancy_r"] == pytest.approx(0.2)
    assert rep["profit_factor"] == pytest.approx(400 / 300)
    assert rep["max_losing_streak"] == 2
    assert rep["max_drawdown"] == pytest.approx(200.0)
    assert set(rep["by_score"]) == {"8-8.9", "9-10"}
    txt = format_report(rep)
    assert "ne garantissent pas" in txt and "SCORE" in txt


def test_validation_criteria_and_suspicious_winrate():
    s = Settings()
    good = {"trades": 150, "winrate": 0.5, "expectancy_r": 0.3, "max_drawdown_pct": 10.0}
    assert validate(good, s)["passed"]
    assert not validate({**good, "trades": 50}, s)["passed"]
    assert not validate({**good, "expectancy_r": -0.1}, s)["passed"]
    sus = validate({**good, "winrate": 0.8}, s)
    assert not sus["passed"] and any("suspect" in w for w in sus["warnings"])


def test_breakeven_option_moves_stop_after_1r():
    m1 = flat_m1(80)
    m1[40] = replace(m1[40], high=2006.0)        # +1R (R ≈ 5.4)
    m1[50] = replace(m1[50], low=1999.0)         # retour sous l'entrée
    res = run(m1, StubEngine(30, 1995.0, 2011.0), breakeven_at_1r=True)
    t = res.trades[0]
    assert t.be_moved and t.exit_reason == "BE"
    assert -0.15 < t.r_multiple < 0   # perte limitée au spread + slippage
    res2 = run(m1, StubEngine(30, 1995.0, 2011.0))   # sans option : position toujours ouverte → fin
    assert res2.trades[0].exit_reason == "FIN DE PÉRIODE"


def test_partial_close_option():
    m1 = flat_m1(80)
    m1[40] = replace(m1[40], high=2006.0)
    m1[50] = replace(m1[50], high=2011.5)
    res = run(m1, StubEngine(30, 1995.0, 2011.0), partial_close_at_1r=True)
    t = res.trades[0]
    assert t.partial_done and t.exit_reason == "TP"
    full = run(m1, StubEngine(30, 1995.0, 2011.0)).trades[0]
    assert 0 < t.pnl_ccy < full.pnl_ccy
