"""Risk Manager — il a TOUJOURS le dernier mot (sur la stratégie, sur Claude, sur l'utilisateur).

Principe « fail-closed » : toute vérification qui ne peut pas être faite (donnée
manquante, exception) compte comme un ÉCHEC → NO TRADE.

`evaluate(ctx, stage)` :
- stage="proposal"  : avant de proposer un trade (kill switch / mode signalés, bloquants)
- stage="execution" : revérification complète juste avant l'envoi de l'ordre
Le même code est utilisé par le backtest (stage="backtest", sans contrôles de connexion).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from broker.models import InstrumentSpec
from config import Mode, Settings
from risk.position_sizing import SizingResult, compute_position_size

log = logging.getLogger(__name__)


@dataclass
class RiskContext:
    now: datetime
    epic: str
    direction: str
    entry: float
    stop_loss: float
    take_profit: float
    score: float
    instrument: InstrumentSpec | None
    capital_available: float | None
    quote_to_account_rate: float | None
    bid: float | None
    offer: float | None
    quote_time: datetime | None
    market_status: str | None
    last_candle_close: datetime | None
    trades_today: int | None
    realized_pnl_today: float | None        # devise du compte, négatif = perte
    open_positions: int | None
    connected: bool
    consecutive_api_errors: int
    macro_blackout: bool | None = False     # None = calendrier indisponible


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str


@dataclass
class RiskDecision:
    approved: bool
    checks: list[Check] = field(default_factory=list)
    sizing: SizingResult | None = None
    rr: float | None = None

    @property
    def size(self) -> float:
        return self.sizing.size if self.sizing and self.sizing.ok else 0.0

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.passed]

    def summary(self) -> str:
        if self.approved:
            return "RISK OK"
        return "NO TRADE — " + " | ".join(f"{c.name}: {c.detail}" for c in self.failed)


class RiskManager:
    def __init__(self, settings: Settings):
        self.s = settings

    def daily_loss_limit(self, capital: float) -> float:
        return min(self.s.max_daily_loss_chf, capital * self.s.max_daily_loss_pct / 100.0)

    def evaluate(self, ctx: RiskContext, stage: str = "proposal") -> RiskDecision:
        try:
            return self._evaluate(ctx, stage)
        except Exception as exc:  # fail-closed
            log.exception("Erreur dans le Risk Manager")
            return RiskDecision(False, [Check("risk_manager", False, f"erreur interne {type(exc).__name__} → NO TRADE")])

    def _evaluate(self, ctx: RiskContext, stage: str) -> RiskDecision:
        s = self.s
        checks: list[Check] = []

        def add(name: str, ok: bool, detail: str) -> None:
            checks.append(Check(name, bool(ok), detail))

        live_like = stage in ("proposal", "execution")

        add("allowlist", ctx.epic in s.allowed_epics, f"instrument {ctx.epic}")
        add("direction", ctx.direction in s.allowed_directions, f"direction {ctx.direction}")

        if live_like:
            add("kill_switch", not s.kill_switch, "KILL_SWITCH actif" if s.kill_switch else "inactif")
            add("mode", s.mode != Mode.ANALYSIS_ONLY, f"mode {s.mode.value}")
        if stage == "execution":
            allowed, reasons = s.execution_allowed()
            add("execution_guard", allowed, "; ".join(reasons) or "ok")

        # Connexion / API
        if live_like:
            add("connexion", ctx.connected, "connecté" if ctx.connected else "connexion perdue")
            add("erreurs_api", ctx.consecutive_api_errors < s.max_consecutive_api_errors,
                f"{ctx.consecutive_api_errors} erreurs consécutives (max {s.max_consecutive_api_errors - 1})")

        # Marché et données
        add("marche_ouvert", ctx.market_status == "TRADEABLE", f"statut {ctx.market_status}")
        if ctx.last_candle_close is None:
            add("donnees_fraiches", False, "horodatage des données inconnu")
        else:
            age = (ctx.now - ctx.last_candle_close).total_seconds()
            add("donnees_fraiches", 0 <= age <= s.max_data_age_s, f"dernière bougie il y a {age:.0f}s")
        if live_like:
            if ctx.quote_time is None:
                add("prix_frais", False, "heure du prix inconnue")
            else:
                qage = (ctx.now - ctx.quote_time).total_seconds()
                add("prix_frais", qage <= s.max_quote_age_s, f"prix mis à jour il y a {qage:.0f}s")
        if ctx.bid is None or ctx.offer is None:
            add("spread", False, "bid/ask indisponibles")
            spread = None
        else:
            spread = ctx.offer - ctx.bid
            add("spread", 0 <= spread <= s.max_spread_points, f"spread {spread:.2f} (max {s.max_spread_points:.2f})")

        # Instrument
        inst = ctx.instrument
        if inst is None:
            add("instrument", False, "spécifications indisponibles")
        else:
            amb = inst.ambiguities()
            add("instrument", not amb, "; ".join(amb) or f"{inst.epic} min {inst.min_deal_size:g} pas {inst.size_increment:g}")

        # SL / TP / RR
        rr: float | None = None
        if ctx.direction == "BUY":
            sl_ok = ctx.stop_loss < ctx.entry
            tp_ok = ctx.take_profit > ctx.entry
            dist = ctx.entry - ctx.stop_loss
        else:
            sl_ok = ctx.stop_loss > ctx.entry
            tp_ok = ctx.take_profit < ctx.entry
            dist = ctx.stop_loss - ctx.entry
        add("sl_cote", sl_ok, f"SL {ctx.stop_loss:.2f} / entrée {ctx.entry:.2f}")
        add("tp_cote", tp_ok, f"TP {ctx.take_profit:.2f} / entrée {ctx.entry:.2f}")
        if inst is not None:
            if inst.min_stop_distance is None:
                add("sl_distance_min", stage == "backtest", "distance minimale de stop inconnue")
            else:
                add("sl_distance_min", dist >= inst.min_stop_distance,
                    f"distance SL {dist:.2f} (min broker {inst.min_stop_distance:.2f})")
        if sl_ok and tp_ok and dist > 0:
            rr = abs(ctx.take_profit - ctx.entry) / dist
            add("rr_min", rr + 1e-9 >= s.strategy.min_rr, f"RR {rr:.2f} (min {s.strategy.min_rr:g})")
        else:
            add("rr_min", False, "RR non calculable")

        add("score_min", ctx.score + 1e-9 >= s.strategy.min_score, f"score {ctx.score:g}/10 (min {s.strategy.min_score:g})")

        # Limites journalières / positions
        if ctx.trades_today is None:
            add("trades_jour", False, "compteur journalier indisponible")
        else:
            add("trades_jour", ctx.trades_today < s.max_trades_per_day,
                f"{ctx.trades_today}/{s.max_trades_per_day} trades aujourd'hui")
        if ctx.open_positions is None:
            add("position_ouverte", False, "état des positions inconnu")
        else:
            add("position_ouverte", ctx.open_positions < s.max_open_positions,
                f"{ctx.open_positions} position(s) ouverte(s) (max {s.max_open_positions})")

        cap = ctx.capital_available
        remaining_budget: float | None = None
        if cap is None:
            add("capital", False, "capital disponible inconnu")
        elif ctx.realized_pnl_today is None:
            add("perte_journaliere", False, "P&L du jour inconnu")
        else:
            limit = self.daily_loss_limit(max(cap, 0.0))
            loss = max(0.0, -ctx.realized_pnl_today)
            remaining_budget = limit - loss
            add("perte_journaliere", remaining_budget > 0,
                f"perte du jour {loss:.2f} / limite {limit:.2f}")

        if s.macro_pause_enabled:
            if ctx.macro_blackout is None:
                add("macro", False, "calendrier macro indisponible (protection activée)")
            else:
                add("macro", not ctx.macro_blackout, "fenêtre d'annonce macro" if ctx.macro_blackout else "pas d'annonce")

        # Taille de position
        sizing: SizingResult | None = None
        if inst is not None and cap is not None and remaining_budget is not None and remaining_budget > 0 and sl_ok:
            sizing = compute_position_size(
                entry=ctx.entry, stop_loss=ctx.stop_loss, capital_available=cap,
                max_risk_ccy=min(s.max_risk_per_trade_chf, remaining_budget),
                max_risk_pct=s.max_risk_per_trade_pct, value_per_point=inst.value_per_point,
                quote_to_account_rate=ctx.quote_to_account_rate, min_size=inst.min_deal_size,
                max_size=inst.max_deal_size, size_step=inst.size_increment,
                slippage_buffer=s.sl_slippage_buffer_points, direction=ctx.direction,
            )
            add("taille", sizing.ok, sizing.reason or
                f"taille {sizing.size:g}, risque {sizing.risk_effective:.2f} {s.account_currency} "
                f"({sizing.risk_pct_of_capital:.2f} %)")
        else:
            add("taille", False, "taille non calculable (capital/instrument/SL/budget)")

        approved = all(c.passed for c in checks)
        return RiskDecision(approved, checks, sizing, rr)


def safe_call(fn: Callable[[], object], default: object = None) -> object:
    """Utilitaire : exécute fn et renvoie `default` en cas d'exception (→ check en échec)."""
    try:
        return fn()
    except Exception:  # noqa: BLE001
        log.exception("Échec d'une récupération de donnée pour le Risk Manager")
        return default
