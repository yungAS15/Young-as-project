"""Fibonacci : retracements, zone OTE et extensions d'un mouvement haussier low→high."""
from __future__ import annotations

from dataclasses import dataclass


def retracement(low: float, high: float, ratio: float) -> float:
    """Niveau de retracement d'une impulsion haussière (0 = high, 1 = low)."""
    return high - ratio * (high - low)


def extension(low: float, high: float, ratio: float) -> float:
    """Extension au-dessus du high : ratio 1.272 → low + 1.272 × (high - low)."""
    return low + ratio * (high - low)


@dataclass(frozen=True)
class Zone:
    bottom: float
    top: float

    def contains(self, price: float) -> bool:
        return self.bottom <= price <= self.top

    def overlaps(self, other: "Zone") -> bool:
        return self.bottom <= other.top and other.bottom <= self.top

    def intersection(self, other: "Zone") -> "Zone | None":
        b, t = max(self.bottom, other.bottom), min(self.top, other.top)
        return Zone(b, t) if b <= t else None

    def touched_by(self, low: float, high: float) -> bool:
        return low <= self.top and high >= self.bottom


def ote_zone(low: float, high: float, ote_low: float = 0.618, ote_high: float = 0.786) -> Zone:
    """Zone OTE = retracement entre 0.618 et 0.786 de l'impulsion."""
    return Zone(bottom=retracement(low, high, ote_high), top=retracement(low, high, ote_low))
