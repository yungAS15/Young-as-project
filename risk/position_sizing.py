"""Calcul de la taille de position.

    risque_autorisé (CHF) = min(MAX_RISK_PER_TRADE_CHF, MAX_RISK_PER_TRADE_PCT % × capital disponible)
    perte_par_unité (CHF) = (entrée - SL + buffer_slippage) × valeur_du_point × taux(devise_cotation→CHF)
    taille = plancher( risque_autorisé / perte_par_unité , pas_de_taille )

- Arrondi TOUJOURS vers le bas (Decimal, pas d'erreur binaire).
- Si taille < taille minimale du broker → NO TRADE (jamais d'augmentation du risque).
- Si taille > taille maximale → plafonnée (le risque baisse, il n'augmente pas).
- Vérification finale : risque_effectif ≤ risque_autorisé, sinon NO TRADE.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal


@dataclass(frozen=True)
class SizingResult:
    ok: bool
    size: float
    risk_allowed: float          # devise du compte
    risk_effective: float        # devise du compte, pour la taille retenue
    risk_pct_of_capital: float
    loss_per_unit: float         # devise du compte
    reason: str = ""


def allowed_risk(capital_available: float, max_risk_ccy: float, max_risk_pct: float) -> float:
    if capital_available <= 0:
        return 0.0
    return max(0.0, min(max_risk_ccy, capital_available * max_risk_pct / 100.0))


def floor_to_step(value: float, step: float) -> float:
    if step <= 0:
        raise ValueError("pas de taille invalide")
    d_val = Decimal(str(value))
    d_step = Decimal(str(step))
    return float((d_val / d_step).to_integral_value(rounding=ROUND_FLOOR) * d_step)


def compute_position_size(
    *,
    entry: float,
    stop_loss: float,
    capital_available: float,
    max_risk_ccy: float,
    max_risk_pct: float,
    value_per_point: float,
    quote_to_account_rate: float | None,
    min_size: float,
    max_size: float,
    size_step: float,
    slippage_buffer: float = 0.0,
    direction: str = "BUY",
) -> SizingResult:
    def fail(reason: str, risk_allowed: float = 0.0, lpu: float = 0.0) -> SizingResult:
        return SizingResult(False, 0.0, risk_allowed, 0.0, 0.0, lpu, reason)

    if direction == "BUY":
        distance = entry - stop_loss
    elif direction == "SELL":
        distance = stop_loss - entry
    else:
        return fail(f"direction inconnue {direction}")
    if distance <= 0:
        return fail("SL du mauvais côté de l'entrée")
    if quote_to_account_rate is None or quote_to_account_rate <= 0:
        return fail("taux de conversion devise de cotation → devise du compte indisponible")
    if value_per_point <= 0 or min_size <= 0 or size_step <= 0 or max_size < min_size:
        return fail("spécifications d'instrument invalides")
    risk_allowed = allowed_risk(capital_available, max_risk_ccy, max_risk_pct)
    if risk_allowed <= 0:
        return fail("capital disponible nul : risque autorisé = 0")
    loss_per_unit = (distance + max(0.0, slippage_buffer)) * value_per_point * quote_to_account_rate
    raw = risk_allowed / loss_per_unit
    size = floor_to_step(min(raw, max_size), size_step)
    if size > max_size:
        size = floor_to_step(max_size, size_step)
    if size < min_size:
        return fail(
            f"taille minimale {min_size:g} ({min_size * loss_per_unit:.2f} de risque) > risque autorisé "
            f"{risk_allowed:.2f} → NO TRADE", risk_allowed, loss_per_unit)
    risk_eff = size * loss_per_unit
    if risk_eff > risk_allowed + 1e-9:
        return fail("risque effectif > risque autorisé (arrondi)", risk_allowed, loss_per_unit)
    pct = 100.0 * risk_eff / capital_available
    return SizingResult(True, size, risk_allowed, risk_eff, pct, loss_per_unit)
