"""Score de setup sur 10. **Ce n'est pas une probabilité de gain.**

Affichage autorisé : « SCORE 8/10 ». Jamais « 80 % de chances ». Seul le backtest,
via le winrate mesuré par tranche de score, peut dire si le score est discriminant.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from config import ScoreWeights


@dataclass
class ScoreBreakdown:
    weights: ScoreWeights
    items: dict[str, float] = field(default_factory=dict)
    notes: dict[str, str] = field(default_factory=dict)

    def add(self, key: str, fraction: float, note: str) -> None:
        """fraction ∈ [0, 1] du poids `key`."""
        fraction = max(0.0, min(1.0, fraction))
        self.items[key] = getattr(self.weights, key) * fraction
        self.notes[key] = note

    @property
    def total(self) -> float:
        return round(sum(self.items.values()), 2)

    @property
    def max_total(self) -> float:
        return self.weights.total

    def label(self) -> str:
        t = self.total
        return f"SCORE {t:g}/{self.max_total:g}"

    def met(self) -> list[str]:
        return [f"{k}: {self.notes[k]} (+{v:g})" for k, v in self.items.items() if v > 0]

    def unmet(self) -> list[str]:
        return [f"{k}: {self.notes[k]}" for k, v in self.items.items() if v <= 0]


def score_bucket(score: float) -> str:
    """Tranche de score pour les statistiques de backtest."""
    if score >= 9:
        return "9-10"
    if score >= 8:
        return "8-8.9"
    if score >= 7:
        return "7-7.9"
    if score >= 6:
        return "6-6.9"
    return "<6"
