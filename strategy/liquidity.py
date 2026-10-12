"""Zones de liquidité sell-side et détection du sweep.

Définitions :

* **Niveaux de liquidité sell-side** (sous le prix) :
  - `H4_SWING_LOW`  : swings lows H4 confirmés (fractale k_h4)
  - `M15_SWING_LOW` : swings lows M15 confirmés dans `liquidity_lookback_m15`
  - `EQUAL_LOWS`    : ≥ 2 swing lows M15 dont l'écart ≤ tolérance (0.10 ATR M15) ;
                      niveau = le plus bas du groupe
  - `RANGE_LOW`     : plus bas des `range_lookback_m15` bougies M15 (bas de range)
  Un niveau n'est candidat que s'il n'a PAS été pris (aucun low plus bas) entre sa
  formation et la bougie qui le balaie.
  Niveaux "significatifs" (point "zone de liquidité" du score) : H4_SWING_LOW,
  EQUAL_LOWS, RANGE_LOW.

* **Sweep sell-side (M15)** : bougie s dont le low passe sous le niveau L
  (L - low ≥ pénétration minimale), puis réaction :
  - mèche de rejet : clôture de s au-dessus de L, ou
  - clôture de réintégration au-dessus de L dans les `reclaim_bars` bougies suivantes,
    sans plus bas inférieur au low de s entre-temps.
  Le sweep retenu est le plus récent ; à égalité, celui qui balaie le niveau le plus
  significatif.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from data.candles import Candle
from strategy.market_structure import Swing, find_swings, swing_lows

SIGNIFICANT = {"H4_SWING_LOW", "EQUAL_LOWS", "RANGE_LOW"}
_PRIORITY = {"H4_SWING_LOW": 3, "EQUAL_LOWS": 2, "RANGE_LOW": 1, "M15_SWING_LOW": 0}


@dataclass(frozen=True)
class LiquidityLevel:
    price: float
    kind: str
    formed_time: datetime     # clôture de la bougie qui a formé le niveau (UTC)
    formed_index: int         # index M15 de formation (-1 si niveau H4)


@dataclass(frozen=True)
class Sweep:
    level: float
    level_kind: str
    sweep_index: int          # bougie M15 qui balaie
    sweep_low: float          # plus bas atteint
    reclaim_index: int        # bougie dont la clôture réintègre au-dessus du niveau
    reaction: str             # "wick_rejection" | "reclaim_close"

    @property
    def significant(self) -> bool:
        return self.level_kind in SIGNIFICANT


def sell_side_levels(m15: Sequence[Candle], h4: Sequence[Candle], *, k_m15: int, k_h4: int,
                     atr_m15: float, equal_tol_atr: float, lookback: int,
                     range_lookback: int, upto_index: int, m15_swings: Sequence[Swing] | None = None,
                     h4_swings: Sequence[Swing] | None = None) -> list[LiquidityLevel]:
    """Niveaux sell-side formés ET confirmés AVANT la bougie M15 `upto_index` (exclue).

    `h4` ne doit contenir que des bougies H4 closes avant m15[upto_index].time.
    `m15_swings` (optionnel, optimisation) : swings de toute la fenêtre m15 ; seuls ceux
    confirmés avant `upto_index` sont retenus — résultat identique à find_swings(m15[:upto_index]).
    """
    hist = m15[:upto_index]
    if not hist:
        return []
    start = max(0, len(hist) - lookback)
    levels: list[LiquidityLevel] = []
    all_sw = m15_swings if m15_swings is not None else find_swings(hist, k_m15)
    lows = [s for s in swing_lows(all_sw) if s.index >= start and s.confirmed_at < upto_index]
    for s in lows:
        levels.append(LiquidityLevel(s.price, "M15_SWING_LOW", hist[s.index].close_time("M15"), s.index))
    tol = equal_tol_atr * atr_m15
    used: set[int] = set()
    for i, a in enumerate(lows):
        if i in used:
            continue
        group = [a]
        for j in range(i + 1, len(lows)):
            if j not in used and abs(lows[j].price - a.price) <= tol:
                group.append(lows[j])
                used.add(j)
        if len(group) >= 2:
            last = max(g.index for g in group)
            levels.append(LiquidityLevel(min(g.price for g in group), "EQUAL_LOWS",
                                         hist[last].close_time("M15"), last))
    rstart = max(0, len(hist) - range_lookback)
    rng = hist[rstart:]
    if rng:
        ridx = rstart + min(range(len(rng)), key=lambda x: rng[x].low)
        levels.append(LiquidityLevel(hist[ridx].low, "RANGE_LOW", hist[ridx].close_time("M15"), ridx))
    h4_sw = h4_swings if h4_swings is not None else find_swings(h4, k_h4)
    for s in swing_lows(h4_sw):
        if s.confirmed_at >= len(h4):
            continue
        levels.append(LiquidityLevel(s.price, "H4_SWING_LOW", h4[s.index].close_time("H4"), -1))
    return levels


def level_untaken_before(level: LiquidityLevel, m15: Sequence[Candle], h4: Sequence[Candle], s: int) -> bool:
    """Vrai si aucun prix n'est passé sous le niveau entre sa formation et la bougie M15 s."""
    first_m15_time = m15[0].time if m15 else None
    for c in h4:
        if c.time >= level.formed_time and first_m15_time is not None and c.close_time("H4") <= first_m15_time:
            if c.low < level.price:
                return False
    lo_from = level.formed_index + 1 if level.formed_index >= 0 else 0
    for j in range(lo_from, s):
        if m15[j].time >= level.formed_time and m15[j].low < level.price:
            return False
    return True


def detect_sweep(m15: Sequence[Candle], h4: Sequence[Candle], levels: Sequence[LiquidityLevel], *,
                 search_from: int, search_to: int | None = None, reclaim_bars: int = 2,
                 min_penetration: float = 0.0) -> Sweep | None:
    """Sweep le plus récent (bougie s dans [search_from, search_to)) dont la réaction est
    confirmée par des bougies M15 closes présentes dans `m15`."""
    best: Sweep | None = None
    n = len(m15)
    for s in range(max(1, search_from), min(n, search_to if search_to is not None else n)):
        bar = m15[s]
        for lvl in levels:
            if lvl.formed_time > bar.time:
                continue
            if lvl.formed_index >= s:
                continue
            if not (lvl.price - bar.low >= max(min_penetration, 1e-9)):
                continue
            if not level_untaken_before(lvl, m15, h4, s):
                continue
            reaction: str | None = None
            reclaim_idx = -1
            if bar.close > lvl.price:
                reaction, reclaim_idx = "wick_rejection", s
            else:
                for j in range(s + 1, min(n, s + 1 + reclaim_bars)):
                    if m15[j].low < bar.low:
                        break
                    if m15[j].close > lvl.price:
                        reaction, reclaim_idx = "reclaim_close", j
                        break
            if reaction is None:
                continue
            cand = Sweep(lvl.price, lvl.kind, s, bar.low, reclaim_idx, reaction)
            if (best is None or s > best.sweep_index
                    or (s == best.sweep_index and _PRIORITY[lvl.kind] > _PRIORITY[best.level_kind])):
                best = cand
    return best
