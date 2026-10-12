from dataclasses import replace

import pytest

from config import ScoreWeights, StrategyParams
from strategy.market_structure import Bias
from strategy.scoring import ScoreBreakdown, score_bucket
from strategy.signal_engine import NoSetup, NoTrade, Setup, SignalEngine, TradeSignal
from strategy.vwap import compute_vwaps
from tests.scenario import h4_bullish, m1_retrace_and_break, m15_series

PARAMS = StrategyParams(max_tp_atr_h4=50.0)


def engine(**kw):
    return SignalEngine(replace(PARAMS, **kw), ScoreWeights())


def setup_and_data():
    h4 = h4_bullish()
    m15 = m15_series()
    eng = engine()
    st = eng.find_setup(h4, m15)
    return eng, h4, m15, st


def test_find_setup_on_known_scenario():
    _, h4, m15, st = setup_and_data()
    assert isinstance(st, Setup), st
    assert st.bias.bias == Bias.BULLISH
    assert st.sweep.sweep_low == 94 and st.sweep.level == 95
    assert st.sweep.level_kind in ("RANGE_LOW", "M15_SWING_LOW", "EQUAL_LOWS")
    assert st.choch.level == 101
    assert st.impulse_high == 102.5
    assert st.ote.top == pytest.approx(102.5 - 0.618 * 8.5)
    assert any(abs(f.zone.bottom - 96.5) < 1e-9 for f in st.fvgs)
    assert st.order_block is not None


def test_no_setup_when_h4_bearish():
    h4 = list(reversed(h4_bullish()))
    # inverser l'ordre des prix tout en gardant le temps croissant → tendance baissière
    h4 = [replace(c, time=h4[-1 - i].time) for i, c in enumerate(h4)]
    h4 = [replace(c, open=c.close, close=c.open) for c in h4]
    st = engine().find_setup(h4, m15_series())
    assert isinstance(st, NoSetup)
    assert "H4" in st.reason


def test_no_setup_without_sweep():
    m15 = m15_series()[:50]  # préfixe seul : pas de sweep
    st = engine().find_setup(h4_bullish(), m15 + m15[-15:])
    assert isinstance(st, NoSetup)


def test_confirm_entry_full_signal():
    eng, h4, m15, st = setup_and_data()
    m1 = m1_retrace_and_break(m15)
    sig = eng.confirm_entry(st, h4, m15, m1, compute_vwaps(m15), spread=0.3)
    assert isinstance(sig, TradeSignal), sig
    assert sig.entry == pytest.approx(98.8 + 0.3)
    assert sig.stop_loss < 94
    assert sig.rr >= 2.0
    assert sig.take_profit == pytest.approx(113.0 - 0.3)   # swing high M15 de 113
    assert sig.flags["ote"] and sig.flags["fvg"] and sig.flags["sweep"]
    assert sig.score >= 7
    assert sig.score_label.startswith("SCORE ")
    assert "%" not in sig.score_label
    assert sig.conditions_met and isinstance(sig.conditions_unmet, list)


def test_no_confirmation_before_break_bar():
    eng, h4, m15, st = setup_and_data()
    m1 = m1_retrace_and_break(m15)[:-1]
    out = eng.confirm_entry(st, h4, m15, m1, compute_vwaps(m15), spread=0.3)
    assert isinstance(out, NoTrade) and "confirmation" in out.reason


def test_rr_below_min_is_no_trade():
    eng, h4, m15, st = setup_and_data()
    eng2 = engine(max_tp_atr_h4=1.0)   # TP lointain jugé déraisonnable → pas de 2R
    out = eng2.confirm_entry(st, h4, m15, m1_retrace_and_break(m15), compute_vwaps(m15), spread=0.3)
    assert isinstance(out, NoTrade) and "RR" in out.reason


def test_score_threshold_blocks():
    eng, h4, m15, st = setup_and_data()
    out = engine(min_score=10.0).confirm_entry(st, h4, m15, m1_retrace_and_break(m15), compute_vwaps(m15), 0.3)
    assert isinstance(out, NoTrade) and "SCORE" in out.reason


def test_invalidation_below_sweep_low():
    eng, h4, m15, st = setup_and_data()
    m1 = m1_retrace_and_break(m15)
    m1[3] = replace(m1[3], low=93.0)
    out = eng.confirm_entry(st, h4, m15, m1, compute_vwaps(m15), 0.3)
    assert isinstance(out, NoTrade) and "Invalidé" in out.reason


def test_scoring_weights_and_label():
    sb = ScoreBreakdown(ScoreWeights())
    sb.add("h4_context", 0.5, "neutre")
    sb.add("sweep", 1.0, "ok")
    sb.add("vwap", 0.0, "non")
    assert sb.total == 3.0
    assert sb.label() == "SCORE 3/10"
    assert any("vwap" in x for x in sb.unmet())
    assert score_bucket(8.5) == "8-8.9" and score_bucket(7) == "7-7.9" and score_bucket(10) == "9-10"


def test_custom_weights_must_sum_to_10():
    from config import ConfigError, Settings
    s = Settings(weights=ScoreWeights(h4_context=3.0))
    with pytest.raises(ConfigError):
        s.validate()
