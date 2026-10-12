from datetime import datetime, timedelta, timezone

import pytest

from data.candles import Candle, closed_only, resample
from strategy.fibonacci import Zone, extension, ote_zone, retracement
from strategy.fvg import bullish_fvgs
from strategy.indicators import atr, ema
from strategy.liquidity import detect_sweep, sell_side_levels
from strategy.market_structure import Bias, find_bullish_choch, find_swings, h4_bias
from strategy.order_blocks import bullish_order_block
from strategy.vwap import VwapTracker, compute_vwaps, trading_day
from tests.helpers import T0, c, series


# ----------------------------------------------------------------- swings
def test_swing_detection_and_confirmation_delay():
    # creux net en index 3, sommet en index 6
    pts = [(10, 11, 9, 10), (10, 10.5, 8, 9), (9, 9.5, 7, 8), (8, 8.5, 5, 7), (7, 8, 6, 7.5),
           (7.5, 9, 6.5, 8.5), (8.5, 12, 8, 11), (11, 11.5, 9, 10), (10, 10.5, 8.5, 9)]
    cs = series(pts)
    sw = find_swings(cs, k=2)
    lows = [s for s in sw if s.kind == "low"]
    highs = [s for s in sw if s.kind == "high"]
    assert [(s.index, s.price, s.confirmed_at) for s in lows] == [(3, 5, 5)]
    assert [(s.index, s.price, s.confirmed_at) for s in highs] == [(6, 12, 8)]
    # Anti-lookahead : avec seulement 7 bougies (index 0..6), le sommet en 6 n'est pas confirmé
    assert not [s for s in find_swings(cs[:7], k=2) if s.kind == "high"]


def test_equal_highs_are_not_swings():
    pts = [(1, 2, 0, 1)] * 2 + [(1, 3, 0.5, 2), (2, 3, 1, 2)] + [(1, 2, 0, 1)] * 2
    assert not [s for s in find_swings(series(pts), 2) if s.kind == "high"]


# ----------------------------------------------------------------- ATR / EMA
def test_atr_constant_range():
    cs = series([(10, 11, 9, 10)] * 20)
    assert atr(cs, 14) == pytest.approx(2.0)
    assert atr(cs[:10], 14) is None


def test_ema_constant():
    assert ema([5.0] * 60, 50) == pytest.approx(5.0)


# ----------------------------------------------------------------- H4 bias
def _zigzag(start: float, legs: list[float], bars_per_leg: int = 4) -> list[Candle]:
    pts = []
    price = start
    for target in legs:
        step = (target - price) / bars_per_leg
        for _ in range(bars_per_leg):
            o = price
            price += step
            if price >= o:   # bougie haussière : extrême haut en clôture
                pts.append((o, price + 0.2, o - 0.1, price))
            else:            # bougie baissière : extrême bas en clôture
                pts.append((o, o + 0.1, price - 0.2, price))
    return series(pts, tf_min=240)


def test_h4_bias_bullish_on_higher_highs_and_lows():
    legs = []
    base = 100.0
    for i in range(10):
        legs += [base + 10 + i * 3, base + 4 + i * 3]
    cs = _zigzag(100, legs + [base + 45])
    r = h4_bias(cs, k=2, ema_period=50)
    assert r.bias == Bias.BULLISH


def test_h4_bias_bearish_on_lower_lows():
    legs = []
    for i in range(10):
        legs += [200 - 4 - i * 3, 200 - 10 - i * 3]
    cs = _zigzag(200, legs + [150])
    assert h4_bias(cs, 2, 50).bias == Bias.BEARISH_OR_UNCLEAR


def test_h4_bias_insufficient_data():
    assert h4_bias(series([(1, 2, 0, 1)] * 10, tf_min=240)).bias == Bias.INSUFFICIENT_DATA


# ----------------------------------------------------------------- liquidity / sweep / CHOCH
def sweep_scenario() -> list[Candle]:
    """Swing low à 95 (index 4), lower high à 101 (index 8), sweep à 94 (index 12)
    avec mèche de rejet (clôture 96), puis déplacement haussier qui casse 101."""
    pts = [
        (100, 101, 99, 100), (100, 100.5, 98, 98.5), (98.5, 99, 97, 97.5), (97.5, 98, 96, 96.5),
        (96.5, 97, 95, 96.8),        # 4 : swing low 95
        (96.8, 98, 96, 97.8), (97.8, 99.5, 97.5, 99), (99, 100.5, 98.5, 100),
        (100, 101, 99.5, 100.2),     # 8 : swing high 101
        (100.2, 100.4, 98, 98.3), (98.3, 98.6, 97, 97.2), (97.2, 97.5, 96, 96.2),
        (96.2, 96.5, 94, 96.0),      # 12 : sweep sous 95, clôture 96 > 95
        (96.0, 98.5, 95.8, 98.4),    # 13
        (98.4, 101.8, 98.6, 101.6),  # 14 : clôture > 101 → CHOCH ; FVG 96.5..98.6 (11 vs 14? non)
        (101.6, 102.5, 101.0, 102.2),
        (102.2, 102.4, 100.8, 101.0),
    ]
    return series(pts)


def test_sell_side_levels_and_wick_sweep():
    m15 = sweep_scenario()
    levels = sell_side_levels(m15, [], k_m15=2, k_h4=2, atr_m15=1.5, equal_tol_atr=0.1,
                              lookback=200, range_lookback=48, upto_index=12)
    kinds = {(round(lv.price, 2), lv.kind) for lv in levels}
    assert (95.0, "M15_SWING_LOW") in kinds
    sw = detect_sweep(m15, [], levels, search_from=12, search_to=13)
    assert sw is not None
    assert sw.level == 95.0 and sw.sweep_index == 12 and sw.sweep_low == 94
    assert sw.reaction == "wick_rejection"


def test_reclaim_close_sweep():
    m15 = sweep_scenario()
    # La bougie de sweep clôture SOUS 95, la suivante réintègre
    m15[12] = Candle(m15[12].time, 96.2, 96.5, 94, 94.6, 100, 0.3)
    levels = sell_side_levels(m15, [], k_m15=2, k_h4=2, atr_m15=1.5, equal_tol_atr=0.1,
                              lookback=200, range_lookback=48, upto_index=12)
    sw = detect_sweep(m15, [], levels, search_from=12, search_to=13, reclaim_bars=2)
    assert sw is not None and sw.reaction == "reclaim_close" and sw.reclaim_index == 13


def test_no_sweep_without_reaction():
    m15 = sweep_scenario()[:13]
    m15[12] = Candle(m15[12].time, 96.2, 96.5, 94, 94.6, 100, 0.3)  # pas encore de réintégration
    levels = sell_side_levels(m15, [], k_m15=2, k_h4=2, atr_m15=1.5, equal_tol_atr=0.1,
                              lookback=200, range_lookback=48, upto_index=12)
    assert detect_sweep(m15, [], levels, search_from=12, search_to=13) is None


def test_equal_lows_detected():
    pts = [(100, 101, 99, 100), (100, 100, 98, 98.5), (98.5, 99, 95.0, 96), (96, 98, 96, 97.5), (97.5, 99, 97, 98.5),
           (98.5, 99.5, 97, 97.2), (97.2, 97.5, 95.05, 96), (96, 98, 96, 97.8), (97.8, 99, 97.5, 98.8)]
    m15 = series(pts)
    levels = sell_side_levels(m15, [], k_m15=2, k_h4=2, atr_m15=1.0, equal_tol_atr=0.1,
                              lookback=200, range_lookback=48, upto_index=len(m15))
    eq = [lv for lv in levels if lv.kind == "EQUAL_LOWS"]
    assert eq and eq[0].price == 95.0


def test_bullish_choch_after_sweep():
    m15 = sweep_scenario()
    ch = find_bullish_choch(m15, 12, 94, k=2, max_bars=12)
    assert ch is not None
    assert ch.level == 101 and ch.break_index == 14 and ch.impulse_high == 102.5


def test_choch_invalidated_by_new_low():
    m15 = sweep_scenario()
    m15[13] = Candle(m15[13].time, 96.0, 96.5, 93.5, 96.2, 100, 0.3)
    assert find_bullish_choch(m15, 12, 94, k=2, max_bars=12) is None


# ----------------------------------------------------------------- FVG / OB
def test_bullish_fvg_and_invalidation():
    pts = [(10, 10.5, 9.5, 10.2), (10.2, 12, 10.1, 11.9), (11.9, 12.5, 11, 12.3), (12.3, 12.6, 11.8, 12)]
    cs = series(pts)
    fv = bullish_fvgs(cs, 0, 3)
    assert len(fv) == 1 and fv[0].index == 2 and fv[0].zone == Zone(10.5, 11)
    # clôture sous le bas de la zone → invalidé
    cs.append(c(4, 12, 12, 10.0, 10.2))
    assert bullish_fvgs(cs, 0, 3) == []


def test_order_block_last_bearish_before_choch():
    m15 = sweep_scenario()
    ob = bullish_order_block(m15, 12, 14)
    # bougies 12 (96.2→96.0 baissière), 13 haussière → OB = 12
    assert ob is not None and ob.index == 12 and ob.zone == Zone(94, 96.5)


# ----------------------------------------------------------------- Fibonacci
def test_fibonacci_levels():
    assert retracement(100, 200, 0.5) == 150
    z = ote_zone(100, 200)
    assert z.top == pytest.approx(138.2) and z.bottom == pytest.approx(121.4)
    assert extension(100, 200, 1.272) == pytest.approx(227.2)
    assert z.contains(130) and not z.contains(140)


# ----------------------------------------------------------------- VWAP
def test_trading_day_reset_at_22_utc():
    assert trading_day(datetime(2025, 3, 3, 21, 59, tzinfo=timezone.utc)).day == 3
    assert trading_day(datetime(2025, 3, 3, 22, 0, tzinfo=timezone.utc)).day == 4


def test_vwap_known_values_and_session_reset():
    t0 = datetime(2025, 3, 3, 20, 0, tzinfo=timezone.utc)
    cs = [Candle(t0 + timedelta(minutes=15 * i), 10, 12, 8, 10, 100) for i in range(4)]
    cs += [Candle(t0 + timedelta(minutes=15 * i), 20, 21, 19, 20, 300) for i in range(8, 12)]  # après 22:00
    v = compute_vwaps(cs)
    assert v["session"].value == pytest.approx(20.0)   # reset à 22:00
    # hebdo : (4×10×100 + 4×20×300) / (400 + 1200) = 17.5
    assert v["weekly"].value == pytest.approx(17.5)


def test_vwap_unavailable_without_volume():
    t0 = datetime(2025, 3, 4, 0, 0, tzinfo=timezone.utc)
    cs = [Candle(t0 + timedelta(minutes=15 * i), 10, 11, 9, 10, 0.0) for i in range(10)]
    v = compute_vwaps(cs)
    assert v["session"].value is None and "volume" in v["session"].reason
    # couverture partielle insuffisante
    cs2 = [Candle(t0 + timedelta(minutes=15 * i), 10, 11, 9, 10, 100.0 if i < 5 else 0.0) for i in range(10)]
    assert compute_vwaps(cs2)["session"].value is None


def test_vwap_incremental_equals_batch():
    cs = series([(10 + i * 0.1, 10.5 + i * 0.1, 9.5 + i * 0.1, 10 + i * 0.1) for i in range(300)])
    tr = VwapTracker()
    for x in cs:
        tr.update(x)
    assert tr.value("weekly").value == pytest.approx(compute_vwaps(cs)["weekly"].value)


# ----------------------------------------------------------------- candles
def test_resample_and_closed_only():
    m1 = [Candle(T0 + timedelta(minutes=i), 10 + i, 11 + i, 9 + i, 10.5 + i, 1) for i in range(30)]
    m15 = resample(m1, "M15")
    assert len(m15) == 2
    assert m15[0].open == 10 and m15[0].high == 25 and m15[0].low == 9 and m15[0].close == 24.5
    assert m15[0].volume == 15
    now = T0 + timedelta(minutes=20)
    assert len(closed_only(m15, "M15", now)) == 1
