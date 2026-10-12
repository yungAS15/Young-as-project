"""Order Block haussier.

Définition : dernière bougie baissière (close < open) M15 entre la bougie de sweep
(incluse) et la bougie de CHOCH (exclue), c.-à-d. la dernière bougie de vente avant
le déplacement haussier qui casse la structure. Zone = [low, high] de cette bougie.
Invalidé si une bougie postérieure CLÔTURE sous le low de l'OB.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from data.candles import Candle
from strategy.fibonacci import Zone


@dataclass(frozen=True)
class OrderBlock:
    index: int
    zone: Zone


def bullish_order_block(candles: Sequence[Candle], sweep_index: int, choch_index: int) -> OrderBlock | None:
    for i in range(choch_index - 1, sweep_index - 1, -1):
        c = candles[i]
        if c.is_bearish:
            if any(candles[j].close < c.low for j in range(i + 1, len(candles))):
                return None
            return OrderBlock(i, Zone(c.low, c.high))
    return None
