"""Modèles de données renvoyés par le client broker (indépendants de l'API brute)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class InstrumentSpec:
    """Spécifications de l'instrument, TOUJOURS lues depuis l'API (jamais devinées).

    value_per_point : variation du P&L (en devise de cotation) pour 1 unité de taille
    et un mouvement de prix de 1.0. Sur Capital.com (CFD) : P&L = taille × Δprix × lotSize
    (lotSize=1 pour l'or : 1 unité = 1 once). **À vérifier sur compte démo** avant le réel
    (commande `verify-pnl` du README).
    """

    epic: str
    name: str
    instrument_type: str
    currency: str                  # devise de cotation (USD pour l'or)
    lot_size: float
    min_deal_size: float
    max_deal_size: float
    size_increment: float
    min_stop_distance: float | None   # en points de prix (None si inconnu)
    min_stop_distance_unit: str
    market_status: str
    scaling_factor: float
    margin_factor: float | None
    margin_factor_unit: str | None
    opening_hours: dict[str, Any] = field(default_factory=dict)
    bid: float | None = None
    offer: float | None = None
    raw_dealing_rules: dict[str, Any] = field(default_factory=dict)

    @property
    def value_per_point(self) -> float:
        return self.lot_size

    @property
    def tradeable(self) -> bool:
        return self.market_status == "TRADEABLE"

    def ambiguities(self) -> list[str]:
        """Liste les paramètres ambigus/manquants → NO TRADE si non vide."""
        out: list[str] = []
        if self.lot_size <= 0:
            out.append("lotSize invalide")
        if self.min_deal_size <= 0:
            out.append("minDealSize invalide")
        if self.size_increment <= 0:
            out.append("minSizeIncrement invalide")
        if self.max_deal_size < self.min_deal_size:
            out.append("maxDealSize < minDealSize")
        if self.scaling_factor != 1:
            out.append(f"scalingFactor={self.scaling_factor} ≠ 1 (calcul de valeur du point ambigu)")
        if not self.currency:
            out.append("devise de cotation inconnue")
        return out


@dataclass(frozen=True)
class Quote:
    epic: str
    bid: float
    offer: float
    time: datetime          # heure de mise à jour (UTC)
    market_status: str

    @property
    def spread(self) -> float:
        return self.offer - self.bid

    @property
    def mid(self) -> float:
        return (self.bid + self.offer) / 2


@dataclass(frozen=True)
class AccountInfo:
    account_id: str
    currency: str
    balance: float
    available: float
    profit_loss: float
    deposit: float


@dataclass(frozen=True)
class Position:
    deal_id: str
    epic: str
    direction: str
    size: float
    level: float
    stop_level: float | None
    profit_level: float | None
    upl: float | None
    currency: str
    created_utc: datetime | None


@dataclass(frozen=True)
class DealConfirmation:
    deal_reference: str
    deal_status: str            # ACCEPTED / REJECTED
    status: str | None          # OPEN / CLOSED / ...
    deal_id: str | None
    level: float | None
    size: float | None
    direction: str | None
    epic: str | None
    reason: str | None
    affected_deals: list[dict[str, Any]] = field(default_factory=list)

    @property
    def accepted(self) -> bool:
        return self.deal_status == "ACCEPTED"
