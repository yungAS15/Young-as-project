"""Données M1 SYNTHÉTIQUES (marche aléatoire) — uniquement pour tester la mécanique.

Ces données n'ont AUCUNE valeur pour juger la stratégie : un résultat de backtest
sur données synthétiques ne dit rien sur l'or réel.
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone

from data.candles import Candle


def random_walk_m1(days: int = 30, start_price: float = 2000.0, seed: int = 42,
                   start: datetime | None = None, vol_per_min: float = 0.35, drift_per_min: float = 0.0,
                   spread: float = 0.30) -> list[Candle]:
    rng = random.Random(seed)
    t = start or datetime(2025, 1, 6, 0, 0, tzinfo=timezone.utc)
    price = start_price
    out: list[Candle] = []
    end = t + timedelta(days=days)
    regime = 0.0
    while t < end:
        # marché fermé le week-end (ven 21:00 → dim 22:00 UTC) et pause quotidienne 21:00-22:00
        wd, h = t.weekday(), t.hour
        closed = (wd == 4 and h >= 21) or wd == 5 or (wd == 6 and h < 22) or h == 21
        if not closed:
            if rng.random() < 0.002:
                regime = rng.gauss(0, 0.05)
            o = price
            path = [o]
            for _ in range(4):
                path.append(path[-1] + rng.gauss(drift_per_min + regime, vol_per_min / 2))
            c = path[-1]
            hi = max(path) + abs(rng.gauss(0, vol_per_min / 4))
            lo = min(path) - abs(rng.gauss(0, vol_per_min / 4))
            vol = max(1.0, rng.gauss(60, 20) * (1.5 if 7 <= h < 17 else 0.7))
            out.append(Candle(t, round(o, 2), round(hi, 2), round(lo, 2), round(c, 2), round(vol), spread))
            price = c
        t += timedelta(minutes=1)
    return out


def sine_trend_m1(days: int = 20, seed: int = 1) -> list[Candle]:
    """Tendance haussière + oscillations : produit des sweeps/CHOCH réguliers (tests e2e)."""
    rng = random.Random(seed)
    t0 = datetime(2025, 1, 6, 0, 0, tzinfo=timezone.utc)
    out: list[Candle] = []
    price = 2000.0
    for i in range(days * 1440):
        t = t0 + timedelta(minutes=i)
        target = 2000 + i * 0.004 + 12 * math.sin(i / 180) + 5 * math.sin(i / 37)
        o = price
        c = target + rng.gauss(0, 0.15)
        hi = max(o, c) + abs(rng.gauss(0, 0.2))
        lo = min(o, c) - abs(rng.gauss(0, 0.2))
        out.append(Candle(t, round(o, 2), round(hi, 2), round(lo, 2), round(c, 2), 50.0, 0.3))
        price = c
    return out
