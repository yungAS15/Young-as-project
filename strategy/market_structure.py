"""Structure de marché : swings, biais H4, CHOCH/MSS.

Définitions (reproductibles, voir docs/STRATEGY.md) :

* **Swing high (fractale k)** : bougie i dont le high est STRICTEMENT supérieur aux
  highs des k bougies précédentes ET des k bougies suivantes. Idem (strictement
  inférieur) pour un swing low avec les lows.
  Un swing en i n'est **confirmé** qu'à la clôture de la bougie i+k : avant, il est
  invisible (pas de lookahead). `confirmed_at` = index de la bougie de confirmation.

* **Biais H4** (sur swings H4 confirmés + EMA50 H4) :
  - BULLISH : deux derniers swing highs croissants ET deux derniers swing lows croissants
    (HH + HL), et clôture > dernier swing low.
  - NEUTRAL_BULLISH : dernier swing low > avant-dernier (HL) OU clôture > dernier swing
    high (cassure haussière), ET clôture > EMA50, ET clôture > dernier swing low.
  - sinon BEARISH/UNCLEAR → NO TRADE (on ne trade jamais contre le biais H4).

* **CHOCH/MSS haussier (M15)** après un sweep à l'index s : le dernier swing high
  confirmé dont l'index est < s (le "lower high" qui a précédé le sweep) est le niveau
  de structure. CHOCH = première clôture M15 (index > s) STRICTEMENT au-dessus de ce
  niveau, dans les `max_bars` bougies qui suivent le sweep, sans nouveau plus bas
  sous le low du sweep entre-temps.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Sequence

from data.candles import Candle
from strategy.indicators import ema


@dataclass(frozen=True)
class Swing:
    index: int
    price: float
    kind: str           # "high" | "low"
    confirmed_at: int   # index de la bougie qui confirme le swing


def find_swings(candles: Sequence[Candle], k: int = 2) -> list[Swing]:
    """Tous les swings confirmés dans `candles` (la dernière bougie fournie est supposée close)."""
    out: list[Swing] = []
    n = len(candles)
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    for i in range(k, n - k):
        h = highs[i]
        lo = lows[i]
        left_h = highs[i - k:i]
        right_h = highs[i + 1:i + k + 1]
        if h > max(left_h) and h > max(right_h):
            out.append(Swing(i, h, "high", i + k))
        left_l = lows[i - k:i]
        right_l = lows[i + 1:i + k + 1]
        if lo < min(left_l) and lo < min(right_l):
            out.append(Swing(i, lo, "low", i + k))
    return out


def swing_highs(swings: Sequence[Swing]) -> list[Swing]:
    return [s for s in swings if s.kind == "high"]


def swing_lows(swings: Sequence[Swing]) -> list[Swing]:
    return [s for s in swings if s.kind == "low"]


class Bias(str, Enum):
    BULLISH = "BULLISH"
    NEUTRAL_BULLISH = "NEUTRAL_BULLISH"
    BEARISH_OR_UNCLEAR = "BEARISH_OR_UNCLEAR"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


@dataclass(frozen=True)
class BiasResult:
    bias: Bias
    detail: str
    last_swing_high: float | None
    last_swing_low: float | None
    ema: float | None


def h4_bias(candles: Sequence[Candle], k: int = 2, ema_period: int = 50) -> BiasResult:
    if len(candles) < max(ema_period, 4 * k + 2):
        return BiasResult(Bias.INSUFFICIENT_DATA, "Historique H4 insuffisant", None, None, None)
    sw = find_swings(candles, k)
    highs = swing_highs(sw)
    lows = swing_lows(sw)
    e = ema([c.close for c in candles], ema_period)
    close = candles[-1].close
    if len(highs) < 2 or len(lows) < 2 or e is None:
        return BiasResult(Bias.INSUFFICIENT_DATA, "Pas assez de swings H4", None, None, e)
    h1, h2 = highs[-2].price, highs[-1].price
    l1, l2 = lows[-2].price, lows[-1].price
    hh = h2 > h1
    hl = l2 > l1
    above_last_low = close > l2
    bos_up = close > h2
    if hh and hl and above_last_low:
        return BiasResult(Bias.BULLISH, f"HH ({h2:.2f}>{h1:.2f}) + HL ({l2:.2f}>{l1:.2f})", h2, l2, e)
    if (hl or bos_up) and close > e and above_last_low:
        why = "HL" if hl else "cassure du dernier swing high"
        return BiasResult(Bias.NEUTRAL_BULLISH, f"{why}, clôture > EMA{ema_period} ({e:.2f})", h2, l2, e)
    return BiasResult(Bias.BEARISH_OR_UNCLEAR,
                      f"Structure non haussière (HH={hh}, HL={hl}, close>EMA={close > e}, close>dernier low={above_last_low})",
                      h2, l2, e)


@dataclass(frozen=True)
class Choch:
    level: float          # swing high cassé
    level_index: int
    break_index: int      # bougie dont la clôture casse le niveau
    impulse_high: float   # plus haut atteint entre le sweep et la bougie courante
    impulse_high_index: int


def find_bullish_choch(candles: Sequence[Candle], sweep_index: int, sweep_low: float,
                       k: int = 2, max_bars: int = 12, swings: Sequence[Swing] | None = None) -> Choch | None:
    """CHOCH haussier après le sweep. N'utilise que les swings confirmés au plus tard à la
    bougie de sweep (`swings` : liste précalculée sur toute la fenêtre, filtrée ici — résultat
    identique à find_swings(candles[:sweep_index+1]))."""
    if swings is None:
        swings = find_swings(candles[: sweep_index + 1], k)
    sw = [s for s in swing_highs(swings) if s.index < sweep_index and s.confirmed_at <= sweep_index]
    if not sw:
        return None
    level = sw[-1]
    end = min(len(candles), sweep_index + 1 + max_bars)
    for j in range(sweep_index + 1, end):
        if candles[j].low < sweep_low:
            return None   # nouveau plus bas : le sweep est invalidé
        if candles[j].close > level.price:
            hi_idx = max(range(sweep_index, len(candles)), key=lambda x: candles[x].high)
            return Choch(level.price, level.index, j, candles[hi_idx].high, hi_idx)
    return None


def d1_bullish(h4: Sequence[Candle], ema_period: int = 20, slope_lookback: int = 5) -> tuple[bool, str]:
    """D1 reconstruit depuis les H4 closes : clôture > EMA20 et EMA20 montante sur 5 jours.

    Seules les journées complètes (6 bougies H4) sont utilisées, pas de lookahead."""
    from data.candles import resample
    if not h4:
        return False, "historique vide"
    d1 = resample(h4, "D1")
    last_close = h4[-1].close_time("H4")
    d1 = [d for d in d1 if d.close_time("D1") <= last_close]
    closes = [d.close for d in d1]
    if len(closes) < ema_period + slope_lookback:
        return False, "historique D1 insuffisant"
    e_now = ema(closes, ema_period)
    e_prev = ema(closes[:-slope_lookback], ema_period)
    if e_now is None or e_prev is None:
        return False, "EMA indisponible"
    ok = closes[-1] > e_now and e_now > e_prev
    return ok, f"clôture {closes[-1]:.2f} vs EMA{ema_period} {e_now:.2f} ({'montante' if e_now > e_prev else 'descendante'})"
