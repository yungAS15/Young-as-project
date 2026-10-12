"""Fair Value Gap haussier (FVG / imbalance).

Définition : pour trois bougies consécutives (i-2, i-1, i), un FVG haussier existe si
`low[i] > high[i-2]`. Zone = [high[i-2], low[i]]. Taille minimale = `min_size`
(0.10 × ATR M15 par défaut). Le FVG est invalidé si une bougie postérieure (jusqu'à la
dernière bougie close fournie) CLÔTURE sous le bas de la zone.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from data.candles import Candle
from strategy.fibonacci import Zone


@dataclass(frozen=True)
class FVG:
    index: int          # bougie i (la troisième)
    zone: Zone


def bullish_fvgs(candles: Sequence[Candle], start: int, end: int, min_size: float = 0.0) -> list[FVG]:
    """FVG haussiers dont la 3e bougie est dans [start, end], non invalidés à ce jour."""
    out: list[FVG] = []
    for i in range(max(2, start), min(end, len(candles) - 1) + 1):
        bottom = candles[i - 2].high
        top = candles[i].low
        if top - bottom > 0 and top - bottom >= min_size:
            if all(candles[j].close >= bottom for j in range(i + 1, len(candles))):
                out.append(FVG(i, Zone(bottom, top)))
    return out
