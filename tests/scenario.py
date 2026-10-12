"""Scénario synthétique complet BUY : H4 haussier, sweep M15 + CHOCH, retour M1 en zone."""
from __future__ import annotations

from datetime import timedelta

from data.candles import Candle
from tests.helpers import T0

SWEEP_SCENARIO = [
    (100, 101, 99, 100), (100, 100.5, 98, 98.5), (98.5, 99, 97, 97.5), (97.5, 98, 96, 96.5),
    (96.5, 97, 95, 96.8),        # swing low 95
    (96.8, 98, 96, 97.8), (97.8, 99.5, 97.5, 99), (99, 100.5, 98.5, 100),
    (100, 101, 99.5, 100.2),     # swing high 101 (lower high)
    (100.2, 100.4, 98, 98.3), (98.3, 98.6, 97, 97.2), (97.2, 97.5, 96, 96.2),
    (96.2, 96.5, 94, 96.0),      # sweep de 95 (mèche), clôture 96
    (96.0, 98.5, 95.8, 98.4),
    (98.4, 101.8, 98.6, 101.6),  # clôture > 101 : CHOCH ; FVG [96.5, 98.6]
    (101.6, 102.5, 101.0, 102.2),
    (102.2, 102.4, 100.8, 101.0),
]


def h4_bullish(n_legs: int = 12, start_time=None) -> list[Candle]:
    """Zigzag haussier HH/HL, loin des prix M15 (300+) pour ne pas interférer."""
    pts = []
    price = 300.0
    legs = []
    for i in range(n_legs):
        legs += [320 + 6 * i, 312 + 6 * i]
    legs.append(320 + 6 * n_legs)
    for target in legs:
        step = (target - price) / 4
        for _ in range(4):
            o = price
            price += step
            if price >= o:
                pts.append((o, price + 0.2, o - 0.1, price))
            else:
                pts.append((o, o + 0.1, price - 0.2, price))
    n = len(pts)
    t_start = (start_time or T0) - timedelta(hours=4 * n)
    return [Candle(t_start + timedelta(hours=4 * i), *p, 1000.0, 0.3) for i, p in enumerate(pts)]


def m15_prefix() -> list[tuple[float, float, float, float]]:
    """50 bougies : montée vers un swing high à 113 puis descente régulière vers 100."""
    pts = []
    price = 104.0
    for i in range(10):   # montée 104 → 112.5
        o = price
        price += 0.85
        pts.append((o, price + 0.1, o - 0.1, price))
    pts.append((price, 113.0, price - 0.2, price - 0.1))       # sommet 113
    price -= 0.1
    for i in range(39):   # descente vers 100 sans casser 99
        o = price
        price -= (price - 100.0) / max(1, 39 - i) if i < 38 else 0
        pts.append((o, o + 0.15, price - 0.15, price))
    return pts


def m15_series() -> list[Candle]:
    pts = m15_prefix() + SWEEP_SCENARIO
    return [Candle(T0 + timedelta(minutes=15 * i), *p, 100.0, 0.3) for i, p in enumerate(pts)]


def m1_retrace_and_break(m15: list[Candle]) -> list[Candle]:
    """Après la dernière M15 close : repli 101 → 97 (OTE + FVG) puis micro-CHOCH haussier."""
    t = m15[-1].time + timedelta(minutes=15)
    pts = []
    price = 101.0
    for _ in range(6):
        o = price
        price -= 0.65
        pts.append((o, o + 0.05, price - 0.05, price))
    for p in [(97.1, 97.5, 97.0, 97.3), (97.3, 97.6, 97.1, 97.2), (97.2, 97.5, 97.05, 97.4), (97.4, 97.6, 97.2, 97.3)]:
        pts.append(p)
    pts.append((97.3, 98.9, 97.25, 98.8))   # clôture > plus haut des 5 précédentes
    return [Candle(t + timedelta(minutes=i), *p, 10.0, 0.3) for i, p in enumerate(pts)]
