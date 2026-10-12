"""Options d'ajustement v2 (H1–H5)."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from backtest.engine import BacktestEngine
from config import ConfigError, ScoreWeights, Settings, StrategyParams
from data.candles import Candle
from strategy.market_structure import d1_bullish
from strategy.signal_engine import NoSetup, NoTrade, Setup, SignalEngine, TradeSignal
from strategy.vwap import compute_vwaps
from tests.scenario import h4_bullish, m1_retrace_and_break, m15_series

BASE = StrategyParams(max_tp_atr_h4=50.0)


def eng(**kw):
    return SignalEngine(replace(BASE, **kw), ScoreWeights())


def test_config_validation_of_new_options():
    with pytest.raises(ConfigError):
        Settings(strategy=StrategyParams(confirm_tf="H1")).validate()
    with pytest.raises(ConfigError):
        Settings(strategy=StrategyParams(entry_mode="stop")).validate()


def test_d1_bullish_on_uptrend_and_not_on_downtrend():
    h4 = h4_bullish(n_legs=40)
    assert d1_bullish(h4)[0]
    down = [replace(c, open=700 - c.open, high=700 - c.low, low=700 - c.high, close=700 - c.close) for c in h4]
    assert not d1_bullish(down)[0]
    assert not d1_bullish(h4[:30])[0]   # historique insuffisant → refus


def test_h2_significant_sweep_gate():
    m15 = m15_series()
    st = eng(require_significant_sweep=True).find_setup(h4_bullish(), m15)
    assert isinstance(st, Setup) and st.sweep.significant   # RANGE_LOW → significatif


def test_h3_requires_d1_history():
    st = eng(require_d1_bullish=True).find_setup(h4_bullish(n_legs=4), m15_series())
    assert isinstance(st, NoSetup)


def test_h1_m15_confirmation_only_at_m15_close():
    h4, m15 = h4_bullish(), m15_series()
    e = eng(confirm_tf="M15")
    st = e.find_setup(h4, m15)
    m1 = m1_retrace_and_break(m15)            # se termine au milieu d'une M15
    out = e.confirm_entry(st, h4, m15, m1, compute_vwaps(m15), 0.3)
    assert isinstance(out, NoTrade) and "M15" in out.reason
    # Ajout d'une M15 de rejet qui clôture exactement à la fin de la M1 courante
    t = m15[-1].time + timedelta(minutes=15)
    rej = Candle(t, 97.4, 98.9, 96.9, 98.8, 100, 0.3)
    m15b = m15 + [rej]
    m1b = [Candle(t + timedelta(minutes=i), 98, 98.5, 97.5, 98.2, 1, 0.3) for i in range(14)]
    m1b.append(Candle(t + timedelta(minutes=14), 98.2, 98.9, 98.1, 98.8, 1, 0.3))
    out = e.confirm_entry(st, h4, m15b, m1b, compute_vwaps(m15b), 0.3)
    assert isinstance(out, TradeSignal), out
    assert out.entry == pytest.approx(99.1) and "rejet M15" in out.analysis["M1"]


def test_h4_limit_plan_level_and_score():
    h4, m15 = h4_bullish(), m15_series()
    e = eng(entry_mode="limit", min_score=6)
    st = e.find_setup(h4, m15)
    out = e.limit_plan(st, h4, m15, datetime.now(timezone.utc), compute_vwaps(m15), 0.3)
    assert isinstance(out, TradeSignal)
    level = 102.5 - 0.705 * (102.5 - 94)
    assert out.entry == pytest.approx(round(level + 0.3, 2))
    assert out.breakdown["m1_confirmation"] == 0 and out.flags["limit_order"]


def test_limit_order_fill_in_backtest():
    """Ordre limite simulé : rempli quand l'ASK touche le niveau, pas de TP sur la bougie de remplissage."""
    from tests.test_backtest import T0, flat_m1, make_setup

    class LimitStub:
        def find_setup(self, h4, m15):
            return make_setup(m15[-1].close_time("M15")) if m15 else NoSetup("vide")

        def limit_plan(self, setup, h4, m15, now, vwaps, spread):
            return TradeSignal(setup.setup_id, "BUY", now, 1998.0, 1993.0, 2009.0, 2.2, 8.0, "SCORE 8/10", {}, [],
                               [], "", "", "", {}, {"limit_order": True}, {}, spread)

    s = replace(Settings(backtest_slippage_points=0.1, backtest_quote_to_account_rate=1.0,
                         backtest_initial_capital_chf=10000.0, max_spread_points=1.0),
                strategy=StrategyParams(entry_mode="limit"))
    m1 = flat_m1(80)
    m1[40] = replace(m1[40], low=1997.0, high=2012.0)    # remplissage ET plus haut > TP sur la même bougie
    m1[60] = replace(m1[60], high=2010.0)
    res = BacktestEngine(s, engine=LimitStub()).run(m1)
    t = res.trades[0]
    assert t.entry_time == m1[40].time and t.entry == pytest.approx(1998.0)
    assert t.exit_time == m1[60].time and t.exit_reason == "TP"     # pas le TP de la bougie 40
