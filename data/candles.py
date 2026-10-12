"""Bougies, timeframes et ré-échantillonnage.

Convention de prix : OHLC = prix **BID**. Le prix ASK = BID + spread.
- Une entrée BUY se fait à l'ASK.
- Un SL/TP d'une position BUY se déclenche sur le BID.
`time` est l'heure d'**ouverture** de la bougie, en UTC (timezone-aware).
Une bougie n'est utilisable qu'une fois *close* : close_time = time + durée.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Sequence

TF_SECONDS = {
    "M1": 60,
    "M5": 300,
    "M15": 900,
    "M30": 1800,
    "H1": 3600,
    "H4": 14400,
    "D1": 86400,
}

# Correspondance avec les résolutions de l'API Capital.com
CAPITAL_RESOLUTION = {
    "M1": "MINUTE",
    "M5": "MINUTE_5",
    "M15": "MINUTE_15",
    "M30": "MINUTE_30",
    "H1": "HOUR",
    "H4": "HOUR_4",
    "D1": "DAY",
}


@dataclass(frozen=True, slots=True)
class Candle:
    time: datetime          # ouverture, UTC
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0     # Capital.com : lastTradedVolume = volume de ticks (pas un volume réel)
    spread: float = 0.0     # ASK - BID à la clôture (0 si inconnu)

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def is_bearish(self) -> bool:
        return self.close < self.open

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def range(self) -> float:
        return self.high - self.low

    def close_time(self, tf: str) -> datetime:
        return self.time + timedelta(seconds=TF_SECONDS[tf])


def ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def bucket_start(t: datetime, tf: str) -> datetime:
    """Début du créneau `tf` contenant t (alignement sur l'epoch UTC)."""
    sec = TF_SECONDS[tf]
    ts = int(ensure_utc(t).timestamp())
    return datetime.fromtimestamp(ts - ts % sec, tz=timezone.utc)


def closed_only(candles: Sequence[Candle], tf: str, now: datetime) -> list[Candle]:
    """Ne garde que les bougies closes à l'instant `now` (anti-lookahead)."""
    now = ensure_utc(now)
    return [c for c in candles if c.close_time(tf) <= now]


def resample(candles: Iterable[Candle], tf: str) -> list[Candle]:
    """Agrège des bougies (M1 typiquement) vers un TF supérieur.

    Produit aussi la dernière bougie éventuellement *incomplète* : l'appelant doit
    filtrer avec `closed_only` (le backtest le fait systématiquement).
    """
    out: list[Candle] = []
    cur: Candle | None = None
    cur_key: datetime | None = None
    for c in candles:
        key = bucket_start(c.time, tf)
        if cur is None or key != cur_key:
            if cur is not None:
                out.append(cur)
            cur_key = key
            cur = Candle(key, c.open, c.high, c.low, c.close, c.volume, c.spread)
        else:
            cur = replace(
                cur,
                high=max(cur.high, c.high),
                low=min(cur.low, c.low),
                close=c.close,
                volume=cur.volume + c.volume,
                spread=c.spread,
            )
    if cur is not None:
        out.append(cur)
    return out


def load_csv(path: str | Path, default_spread: float = 0.0) -> list[Candle]:
    """Charge un CSV OHLC(V).

    Colonnes acceptées (insensible à la casse) :
      time|timestamp|date , open, high, low, close, [volume], [spread]
    `time` : ISO-8601 (UTC si pas de fuseau) ou timestamp unix (s ou ms).
    Compatible avec l'export `dukascopy-node -f csv` et l'export `fetch-history` du bot.
    """
    rows: list[Candle] = []
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            return rows
        cols = {name.lower().strip(): name for name in reader.fieldnames}
        tcol = cols.get("time") or cols.get("timestamp") or cols.get("date")
        if tcol is None:
            raise ValueError("Colonne time/timestamp/date absente du CSV")
        for r in reader:
            raw_t = r[tcol].strip()
            if raw_t.replace(".", "", 1).isdigit():
                v = float(raw_t)
                t = datetime.fromtimestamp(v / 1000 if v > 1e11 else v, tz=timezone.utc)
            else:
                t = ensure_utc(datetime.fromisoformat(raw_t.replace("Z", "+00:00")))
            vol = float(r[cols["volume"]]) if "volume" in cols and r[cols["volume"]] not in ("", None) else 0.0
            spr = (float(r[cols["spread"]]) if "spread" in cols and r[cols["spread"]] not in ("", None)
                   else default_spread)
            rows.append(Candle(t, float(r[cols["open"]]), float(r[cols["high"]]),
                               float(r[cols["low"]]), float(r[cols["close"]]), vol, spr))
    rows.sort(key=lambda c: c.time)
    return rows


def save_csv(candles: Sequence[Candle], path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["time", "open", "high", "low", "close", "volume", "spread"])
        for c in candles:
            w.writerow([c.time.isoformat(), c.open, c.high, c.low, c.close, c.volume, c.spread])
