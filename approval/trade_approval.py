"""Validation manuelle des propositions de trade.

Règles :
- Seules les commandes EXACTES `APPROVE <id>`, `REJECT <id>`, `CANCEL <id>` (majuscules,
  id de 8 caractères hexadécimaux) sont reconnues. Toute autre formulation
  (« ok », « vas-y », « approve », « APPROVE » sans id…) est IGNORÉE : ce n'est pas une
  approbation.
- Une proposition expire si : délai `APPROVAL_TTL_S` dépassé, prix éloigné de plus de
  `APPROVAL_MAX_PRICE_DRIFT_POINTS`, si l'empreinte (setup, SL, TP, taille, score) a
  changé, ou (vérifié par l'exécuteur) si le risque recalculé dépasse le risque approuvé
  de plus de `APPROVAL_MAX_RISK_INCREASE_PCT` ou le risque autorisé.
  Une proposition expirée n'est jamais réactivée.
- Chaque transition d'état est atomique (compare-and-set en base) : une approbation
  ne peut être consommée qu'une seule fois.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from config import Settings
from data.database import Database, parse_iso, utcnow
from risk.risk_manager import RiskDecision
from strategy.signal_engine import TradeSignal

COMMAND_RE = re.compile(r"^(APPROVE|REJECT|CANCEL) ([0-9a-f]{8})$")

PENDING, APPROVED, REJECTED, CANCELLED, EXPIRED, BLOCKED = (
    "PENDING", "APPROVED", "REJECTED", "CANCELLED", "EXPIRED", "BLOCKED")
EXECUTING, EXECUTED, FAILED = "EXECUTING", "EXECUTED", "FAILED"


@dataclass(frozen=True)
class Command:
    action: str
    proposal_id: str


@dataclass(frozen=True)
class DecisionResult:
    ok: bool
    message: str
    status: str | None = None


def parse_command(text: str) -> Command | None:
    """Renvoie la commande si et seulement si elle est exacte et non ambiguë."""
    if not isinstance(text, str):
        return None
    m = COMMAND_RE.match(text.strip())
    if not m:
        return None
    return Command(m.group(1), m.group(2))


def fingerprint(payload: dict[str, Any]) -> str:
    # L'entrée (prix) est couverte par le seuil de dérive de prix ; le risque est revérifié
    # explicitement à l'exécution (taille approuvée ≤ taille autorisée, hausse ≤ tolérance).
    keys = ("setup_id", "direction", "stop_loss", "take_profit", "size", "score", "epic")
    core = {k: (round(payload[k], 4) if isinstance(payload.get(k), float) else payload.get(k)) for k in keys}
    return hashlib.sha256(json.dumps(core, sort_keys=True).encode()).hexdigest()


class ApprovalManager:
    def __init__(self, db: Database, settings: Settings, clock: Callable[[], datetime] = utcnow,
                 notifier: Any = None):
        self.db = db
        self.s = settings
        self.clock = clock
        self.notifier = notifier

    def _notify(self, kind: str, text: str) -> None:
        if self.notifier is not None:
            from monitoring.alerts import Event
            self.notifier.notify(Event(kind), text)

    # --------------------------------------------------------------- create
    def build_payload(self, signal: TradeSignal, decision: RiskDecision, *, epic: str, bid: float | None,
                      offer: float | None, capital: float | None, trades_today: int | None) -> dict[str, Any]:
        sizing = decision.sizing
        unmet = list(signal.conditions_unmet) + [f"RISK {c.name}: {c.detail}" for c in decision.failed]
        return {
            "epic": epic,
            "instrument": "XAU/USD",
            "direction": signal.direction,
            "bid": bid,
            "offer": offer,
            "entry": signal.entry,
            "stop_loss": signal.stop_loss,
            "take_profit": signal.take_profit,
            "rr": signal.rr,
            "size": sizing.size if sizing and sizing.ok else 0.0,
            "capital": capital,
            "account_currency": self.s.account_currency,
            "risk_ccy": round(sizing.risk_effective, 2) if sizing and sizing.ok else 0.0,
            "risk_pct": round(sizing.risk_pct_of_capital, 3) if sizing and sizing.ok else 0.0,
            "trades_today": trades_today,
            "max_trades": self.s.max_trades_per_day,
            "score": signal.score,
            "score_label": signal.score_label,
            "analysis": signal.analysis,
            "flags": signal.flags,
            "vwaps": signal.vwaps,
            "trade_reason": signal.trade_reason,
            "sl_reason": signal.sl_reason,
            "tp_reason": signal.tp_reason,
            "conditions_met": list(signal.conditions_met),
            "conditions_unmet": unmet,
            "risk_checks": [{"name": c.name, "passed": c.passed, "detail": c.detail} for c in decision.checks],
            "setup_id": signal.setup_id,
            "signal_time": signal.time.isoformat(),
            "mode": "DRY_RUN" if self.s.dry_run else self.s.capital_env.upper(),
        }

    def create(self, payload: dict[str, Any], approved_by_risk: bool, signal_id: int | None = None) -> str:
        pid = secrets.token_hex(4)
        now = self.clock()
        ref_price = payload.get("offer") or payload["entry"]
        self.db.insert_proposal(
            pid, now, now + timedelta(seconds=self.s.approval_ttl_s), payload["setup_id"], signal_id,
            fingerprint(payload), float(ref_price), payload, PENDING if approved_by_risk else BLOCKED)
        return pid

    # --------------------------------------------------------------- decide
    def _expiry_reason(self, row: dict[str, Any], now: datetime, current_price: float | None) -> str | None:
        exp = parse_iso(row["expires_at"])
        if exp is None or now > exp:
            return "délai d'approbation dépassé"
        if current_price is None:
            return "prix courant indisponible"
        drift = abs(current_price - float(row["price_at_creation"]))
        if drift > self.s.approval_max_price_drift_points:
            return f"prix éloigné de {drift:.2f} (> {self.s.approval_max_price_drift_points:.2f})"
        return None

    def handle(self, text: str, current_price: float | None) -> DecisionResult:
        cmd = parse_command(text)
        if cmd is None:
            return DecisionResult(False, "Commande non reconnue : seules 'APPROVE <id>', 'REJECT <id>', "
                                         "'CANCEL <id>' sont acceptées. Rien n'a été approuvé.")
        row = self.db.get_proposal(cmd.proposal_id)
        if row is None:
            return DecisionResult(False, f"Proposition {cmd.proposal_id} inconnue")
        now = self.clock()
        if cmd.action == "REJECT":
            ok = self.db.update_proposal_status(cmd.proposal_id, REJECTED, "rejetée par l'utilisateur",
                                                expected_status=PENDING)
            if ok:
                self._notify("REJECTED", f"Proposition {cmd.proposal_id} rejetée")
            return DecisionResult(ok, "Rejetée" if ok else f"Impossible : statut {row['status']}",
                                  REJECTED if ok else row["status"])
        if cmd.action == "CANCEL":
            for st in (PENDING, APPROVED):
                if self.db.update_proposal_status(cmd.proposal_id, CANCELLED, "annulée par l'utilisateur",
                                                  expected_status=st):
                    return DecisionResult(True, "Annulée", CANCELLED)
            return DecisionResult(False, f"Impossible d'annuler : statut {row['status']}", row["status"])
        # APPROVE
        if row["status"] != PENDING:
            return DecisionResult(False, f"Approbation impossible : statut {row['status']} "
                                         "(une proposition ne peut être approuvée qu'une fois)", row["status"])
        reason = self._expiry_reason(row, now, current_price)
        if reason:
            self.db.update_proposal_status(cmd.proposal_id, EXPIRED, reason, expected_status=PENDING)
            return DecisionResult(False, f"Proposition expirée : {reason}", EXPIRED)
        ok = self.db.update_proposal_status(cmd.proposal_id, APPROVED, "approuvée par l'utilisateur",
                                            expected_status=PENDING)
        if ok:
            self._notify("APPROVED", f"Proposition {cmd.proposal_id} approuvée — revérification avant ordre")
        return DecisionResult(ok, "Approuvée — revérification complète avant ordre" if ok else "Conflit d'état",
                              APPROVED if ok else None)

    def claim_for_execution(self, pid: str, current_price: float | None,
                            current_payload: dict[str, Any] | None) -> DecisionResult:
        """Vérifie qu'une approbation est encore valable puis la consomme (APPROVED→EXECUTING)."""
        row = self.db.get_proposal(pid)
        if row is None:
            return DecisionResult(False, "proposition inconnue")
        if row["status"] != APPROVED:
            return DecisionResult(False, f"statut {row['status']} : pas d'approbation valable", row["status"])
        reason = self._expiry_reason(row, self.clock(), current_price)
        if reason is None and current_payload is not None and fingerprint(current_payload) != row["fingerprint"]:
            reason = "setup/SL/TP/taille/risque/score modifié depuis la proposition"
        if reason:
            self.db.update_proposal_status(pid, EXPIRED, reason, expected_status=APPROVED)
            return DecisionResult(False, f"approbation expirée : {reason}", EXPIRED)
        if not self.db.update_proposal_status(pid, EXECUTING, "revérification OK", expected_status=APPROVED):
            return DecisionResult(False, "approbation déjà consommée")
        return DecisionResult(True, "ok", EXECUTING)

    def expire_stale(self) -> int:
        now = self.clock()
        n = 0
        for st in (PENDING, APPROVED):
            for row in self.db.proposals(st, limit=500):
                exp = parse_iso(row["expires_at"])
                if exp is None or now > exp:
                    if self.db.update_proposal_status(row["id"], EXPIRED, "délai dépassé", expected_status=st):
                        n += 1
        return n


def format_proposal(pid: str, p: dict[str, Any], status: str = PENDING) -> str:
    """Format de proposition obligatoire (texte, compatible Telegram/CLI)."""
    yn = lambda b: "oui" if b else "non"  # noqa: E731
    f = p.get("flags", {})
    v = p.get("vwaps", {})
    vtxt = ", ".join(f"{k}={'%.2f' % x if x is not None else 'indisponible'}" for k, x in v.items())
    cur = p.get("account_currency", "CHF")
    lines = [
        f"=== PROPOSITION {pid} [{status}] — mode {p.get('mode')} ===",
        f"Instrument : {p['instrument']} ({p['epic']})   Direction : {p['direction']}",
        f"Prix actuel : bid {p.get('bid')} / ask {p.get('offer')}",
        f"Entrée : {p['entry']:.2f}   SL : {p['stop_loss']:.2f}   TP : {p['take_profit']:.2f}   RR : {p['rr']:.2f}",
        f"Taille : {p['size']:g}   Capital disponible : {p.get('capital')} {cur}",
        f"Risque : {p['risk_ccy']:.2f} {cur} ({p['risk_pct']:.2f} %)   Trades du jour : "
        f"{p.get('trades_today')}/{p.get('max_trades')}",
        f"{p['score_label']}  (score de setup, PAS une probabilité de gain)",
        f"H4 : {p['analysis'].get('H4')}",
        f"M15 : {p['analysis'].get('M15')}",
        f"M1 : {p['analysis'].get('M1')}",
        f"Sweep : {yn(f.get('sweep'))}   FVG : {yn(f.get('fvg'))}   OB : {yn(f.get('ob'))}   "
        f"OTE Fibonacci : {yn(f.get('ote'))}",
        f"VWAP : {vtxt} (tick-volume) — réaction : {yn(f.get('vwap'))}",
        f"Raison du trade : {p['trade_reason']}",
        f"Raison du SL : {p['sl_reason']}",
        f"Raison du TP : {p['tp_reason']}",
        "Conditions remplies :",
        *[f"  ✔ {x}" for x in p.get("conditions_met", [])],
        "Conditions non remplies :",
        *([f"  ✘ {x}" for x in p.get("conditions_unmet", [])] or ["  (aucune)"]),
    ]
    if status == PENDING:
        lines.append(f"Répondre exactement : APPROVE {pid}  |  REJECT {pid}  |  CANCEL {pid}")
    return "\n".join(lines)
