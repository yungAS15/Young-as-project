"""Indicateurs de base (ATR, EMA). Calculés uniquement sur les bougies fournies."""
from __future__ import annotations

from typing import Sequence

from data.candles import Candle


def true_ranges(candles: Sequence[Candle]) -> list[float]:
    out: list[float] = []
    for i, c in enumerate(candles):
        if i == 0:
            out.append(c.high - c.low)
        else:
            pc = candles[i - 1].close
            out.append(max(c.high - c.low, abs(c.high - pc), abs(c.low - pc)))
    return out


def atr(candles: Sequence[Candle], period: int = 14) -> float | None:
    """ATR de Wilder sur la dernière bougie. None si historique insuffisant."""
    if len(candles) < period + 1:
        return None
    tr = true_ranges(candles)
    value = sum(tr[1:period + 1]) / period
    for x in tr[period + 1:]:
        value = (value * (period - 1) + x) / period
    return value


def ema(values: Sequence[float], period: int) -> float | None:
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e
