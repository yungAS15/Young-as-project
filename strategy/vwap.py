"""VWAP ancrés : session, hebdomadaire, mensuel.

Méthode (documentée aussi dans docs/STRATEGY.md §VWAP) :
- Prix typique = (high + low + close) / 3 de chaque bougie M15 **close** (BID).
- Pondération = `volume` de la bougie. Sur Capital.com, `lastTradedVolume` est un
  **volume de ticks** (nombre de mises à jour de prix du CFD), PAS un volume échangé
  réel : l'or spot/CFD est un marché OTC sans volume centralisé. Le VWAP obtenu est
  donc un « tick-VWAP », une approximation. Il est affiché comme tel.
- Jour de trading : commence à `reset_hour_utc` (22:00 UTC par défaut, après la pause
  quotidienne du CFD or). Une bougie ouverte à t appartient au jour
  `date(t + (24 - reset_hour_utc) h)`.
  - Session : reset à chaque nouveau jour de trading.
  - Hebdo : reset au premier jour de trading de la semaine ISO.
  - Mensuel : reset au premier jour de trading du mois.
- Période incomplète : si le DÉBUT de la période n'a pas été observé (historique trop
  court, ex. 10 jours de M15 pour un VWAP mensuel), le VWAP est INDISPONIBLE.
- Fiabilité : si moins de `min_coverage` (90 %) des bougies de la période ont un
  volume > 0, ou si la période contient < 4 bougies, le VWAP est déclaré
  **INDISPONIBLE** (value=None) avec la raison. On ne calcule jamais un faux niveau
  (pas de repli silencieux sur une moyenne non pondérée).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable, Sequence

from data.candles import Candle


def trading_day(t: datetime, reset_hour_utc: int = 22) -> date:
    return (t + timedelta(hours=(24 - reset_hour_utc) % 24)).date()


def anchor_key(t: datetime, kind: str, reset_hour_utc: int = 22) -> tuple[int, ...]:
    d = trading_day(t, reset_hour_utc)
    if kind == "session":
        return (d.year, d.month, d.day)
    if kind == "weekly":
        iso = d.isocalendar()
        return (iso[0], iso[1])
    if kind == "monthly":
        return (d.year, d.month)
    raise ValueError(kind)


@dataclass(frozen=True)
class VwapValue:
    kind: str
    value: float | None
    bars: int
    coverage: float
    reason: str = ""

    @property
    def available(self) -> bool:
        return self.value is not None


class VwapTracker:
    """VWAP incrémental (même code en live et en backtest)."""

    KINDS = ("session", "weekly", "monthly")

    def __init__(self, reset_hour_utc: int = 22, min_coverage: float = 0.90, min_bars: int = 4):
        self.reset_hour_utc = reset_hour_utc
        self.min_coverage = min_coverage
        self.min_bars = min_bars
        self._key: dict[str, tuple[int, ...] | None] = {k: None for k in self.KINDS}
        self._pv: dict[str, float] = {k: 0.0 for k in self.KINDS}
        self._v: dict[str, float] = {k: 0.0 for k in self.KINDS}
        self._n: dict[str, int] = {k: 0 for k in self.KINDS}
        self._nvol: dict[str, int] = {k: 0 for k in self.KINDS}
        self._complete: dict[str, bool] = {k: False for k in self.KINDS}

    def update(self, c: Candle) -> None:
        tp = (c.high + c.low + c.close) / 3.0
        for k in self.KINDS:
            key = anchor_key(c.time, k, self.reset_hour_utc)
            if key != self._key[k]:
                # Le début de période n'est observé que si l'on a vu la transition.
                self._complete[k] = self._key[k] is not None
                self._key[k] = key
                self._pv[k] = self._v[k] = 0.0
                self._n[k] = self._nvol[k] = 0
            self._n[k] += 1
            if c.volume > 0:
                self._nvol[k] += 1
                self._pv[k] += tp * c.volume
                self._v[k] += c.volume

    def feed(self, candles: Iterable[Candle]) -> "VwapTracker":
        for c in candles:
            self.update(c)
        return self

    def value(self, kind: str) -> VwapValue:
        n = self._n[kind]
        cov = (self._nvol[kind] / n) if n else 0.0
        if not self._complete[kind]:
            return VwapValue(kind, None, n, cov, "début de période non couvert par l'historique")
        if n < self.min_bars:
            return VwapValue(kind, None, n, cov, "période trop courte (< 4 bougies)")
        if self._v[kind] <= 0:
            return VwapValue(kind, None, n, cov, "aucun volume disponible")
        if cov < self.min_coverage:
            return VwapValue(kind, None, n, cov, f"volume insuffisant ({cov:.0%} des bougies)")
        return VwapValue(kind, self._pv[kind] / self._v[kind], n, cov, "tick-volume")

    def values(self) -> dict[str, VwapValue]:
        return {k: self.value(k) for k in self.KINDS}


def compute_vwaps(candles: Sequence[Candle], reset_hour_utc: int = 22, min_coverage: float = 0.90) -> dict[str, VwapValue]:
    return VwapTracker(reset_hour_utc, min_coverage).feed(candles).values()
