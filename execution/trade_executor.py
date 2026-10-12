"""Exécution des propositions approuvées et surveillance des positions.

Séquence `execute(proposal_id)` :
1. kill switch (config OU runtime en base) → refus immédiat ;
2. données fraîches (prix, spread, compte, positions, instrument, FX, connexion) ;
3. Risk Manager stage="execution" avec l'entrée = ASK actuel et les SL/TP approuvés ;
   la taille approuvée doit rester ≤ à la taille autorisée maintenant (le risque ne
   peut jamais augmenter) ; RR recalculé ≥ minimum ;
4. consommation atomique de l'approbation (expiration temps/prix/empreinte) ;
5. une seule entrée par setup ;
6. DRY_RUN → trade simulé ; sinon ordre réel AVEC SL+TP, puis confirmation
   (GET /confirms) ET vérification de la position réelle (SL/TP attachés).
   Si SL/TP absents → tentative de pose, sinon fermeture immédiate + kill switch.
Une seule condition qui échoue → ordre annulé.
"""
from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

from approval.trade_approval import EXECUTED, FAILED, ApprovalManager
from broker.capital_api import CapitalAPIError, CapitalClient
from config import Settings
from data.database import Database, parse_iso
from data.market_data import MarketDataService, MarketSnapshot
from monitoring.alerts import Event, Notifier
from monitoring.macro_calendar import blackout, load_events
from config import resolve_path
from risk.risk_manager import RiskContext, RiskDecision, RiskManager

log = logging.getLogger(__name__)


@dataclass
class ExecutionResult:
    ok: bool
    message: str
    trade_id: int | None = None
    deal_id: str | None = None


def effective_kill_switch(settings: Settings, db: Database) -> bool:
    return settings.kill_switch or db.runtime_kill_switch()


def build_risk_context(settings: Settings, db: Database, snap: MarketSnapshot, *, entry: float, stop_loss: float,
                       take_profit: float, score: float, connected: bool, api_errors: int) -> RiskContext:
    daily = db.daily()
    open_db = len(db.open_trades())
    open_broker = None if snap.positions is None else len(
        [p for p in snap.positions if p.epic in settings.allowed_epics])
    open_positions = None if open_broker is None else max(open_db, open_broker)
    events = load_events(resolve_path(settings.macro_calendar_path)) if settings.macro_pause_enabled else []
    q = snap.quote
    return RiskContext(
        now=snap.now, epic=settings.allowed_epics[0], direction="BUY", entry=entry, stop_loss=stop_loss,
        take_profit=take_profit, score=score, instrument=snap.instrument, capital_available=snap.capital_available,
        quote_to_account_rate=snap.fx_rate, bid=q.bid if q else None, offer=q.offer if q else None,
        quote_time=q.time if q else None, market_status=q.market_status if q else None,
        last_candle_close=snap.last_m1_close, trades_today=int(daily["trades_count"]),
        realized_pnl_today=float(daily["realized_pnl"]), open_positions=open_positions, connected=connected,
        consecutive_api_errors=api_errors,
        macro_blackout=blackout(snap.now, events, settings.macro_pause_minutes) if settings.macro_pause_enabled else False,
    )


class TradeExecutor:
    def __init__(self, settings: Settings, db: Database, market: MarketDataService, client: CapitalClient,
                 approvals: ApprovalManager, notifier: Notifier):
        self.s = settings
        self.db = db
        self.market = market
        self.client = client
        self.approvals = approvals
        self.notifier = notifier
        self.rm = RiskManager(settings)

    # ------------------------------------------------------------------ helpers
    def _fail(self, pid: str, msg: str, claimed: bool = False) -> ExecutionResult:
        if claimed:
            self.db.update_proposal_status(pid, FAILED, msg)
        self.notifier.notify(Event.EXECUTION_FAILED, f"Proposition {pid} : ordre annulé — {msg}")
        return ExecutionResult(False, msg)

    def _trip_kill_switch(self, reason: str) -> None:
        self.db.set("kill_switch", "1")
        self.notifier.notify(Event.KILL_SWITCH, f"KILL SWITCH ACTIVÉ automatiquement : {reason}")

    # ------------------------------------------------------------------ execute
    def execute(self, pid: str) -> ExecutionResult:
        row = self.db.get_proposal(pid)
        if row is None:
            return ExecutionResult(False, "proposition inconnue")
        p: dict[str, Any] = row["payload"]
        if effective_kill_switch(self.s, self.db):
            self.db.update_proposal_status(pid, FAILED, "kill switch actif", expected_status="APPROVED")
            return self._fail(pid, "KILL_SWITCH actif")

        snap = self.market.snapshot(with_candles=True)
        connected = self.client.connected and not snap.errors
        if snap.quote is None:
            return self._fail(pid, "prix indisponible : " + "; ".join(snap.errors))
        ask = snap.quote.offer
        ctx = build_risk_context(self.s, self.db, snap, entry=ask, stop_loss=p["stop_loss"],
                                 take_profit=p["take_profit"], score=p["score"], connected=connected,
                                 api_errors=self.client.consecutive_errors)
        decision: RiskDecision = self.rm.evaluate(ctx, "execution")
        if not decision.approved:
            self.db.update_proposal_status(pid, FAILED, decision.summary(), expected_status="APPROVED")
            return self._fail(pid, decision.summary())
        approved_size = float(p["size"])
        if approved_size <= 0 or decision.size + 1e-12 < approved_size:
            msg = (f"risque modifié : taille approuvée {approved_size:g} > taille autorisée maintenant "
                   f"{decision.size:g}")
            self.db.update_proposal_status(pid, FAILED, msg, expected_status="APPROVED")
            return self._fail(pid, msg)

        risk_now = approved_size * decision.sizing.loss_per_unit  # type: ignore[union-attr]
        risk_cap = float(p.get("risk_ccy") or 0.0) * (1 + self.s.approval_max_risk_increase_pct / 100.0)
        if risk_now > risk_cap + 1e-9:
            msg = f"risque modifié : {risk_now:.2f} > risque approuvé {p.get('risk_ccy')} (+{self.s.approval_max_risk_increase_pct:g} %)"
            self.db.update_proposal_status(pid, "EXPIRED", msg, expected_status="APPROVED")
            return self._fail(pid, msg)

        claim = self.approvals.claim_for_execution(pid, ask, p)
        if not claim.ok:
            return self._fail(pid, claim.message)
        if not self.db.mark_setup_used(p["setup_id"]):
            return self._fail(pid, "setup déjà utilisé (une seule entrée par signal)", claimed=True)

        if self.s.dry_run:
            tid = self.db.open_trade(
                proposal_id=pid, setup_id=p["setup_id"], deal_id=f"DRY-{secrets.token_hex(4)}", deal_reference=None,
                mode="DRY_RUN", direction="BUY", epic=p["epic"], opened_at=snap.now, entry=ask, size=approved_size,
                stop_loss=p["stop_loss"], take_profit=p["take_profit"],
                risk_ccy=approved_size * decision.sizing.loss_per_unit)  # type: ignore[union-attr]
            self.db.update_proposal_status(pid, EXECUTED, "DRY_RUN : ordre simulé", expected_status="EXECUTING")
            self.notifier.notify(Event.EXECUTED, f"[DRY_RUN] BUY {approved_size:g} {p['epic']} @ {ask:.2f} "
                                                 f"SL {p['stop_loss']:.2f} TP {p['take_profit']:.2f} (aucun ordre envoyé)")
            return ExecutionResult(True, "DRY_RUN exécuté (simulation)", tid)

        return self._execute_real(pid, p, approved_size, decision, snap)

    def _execute_real(self, pid: str, p: dict[str, Any], size: float, decision: RiskDecision,
                      snap: MarketSnapshot) -> ExecutionResult:
        try:
            ref = self.client.open_position(p["epic"], "BUY", size, p["stop_loss"], p["take_profit"])
        except CapitalAPIError as exc:
            # Un échec réseau sur POST peut masquer un ordre passé : on vérifie les positions.
            self._trip_kill_switch(f"erreur à l'envoi de l'ordre ({exc}) — vérifier les positions manuellement")
            return self._fail(pid, f"erreur API à l'envoi : {exc}", claimed=True)
        try:
            conf = self.client.confirm(ref)
        except CapitalAPIError as exc:
            self._trip_kill_switch(f"confirmation impossible pour {ref}")
            return self._fail(pid, f"confirmation introuvable ({exc}) — état inconnu, kill switch activé", claimed=True)
        if not conf.accepted:
            return self._fail(pid, f"ordre REJETÉ par le broker : {conf.reason or conf.deal_status}", claimed=True)
        deal_id = next((d.get("dealId") for d in conf.affected_deals if d.get("status") in ("OPENED", "OPEN")),
                       conf.deal_id)
        pos = self.client.get_position(str(deal_id)) if deal_id else None
        if pos is None:
            self._trip_kill_switch(f"ordre accepté mais position {deal_id} introuvable")
            return self._fail(pid, "position introuvable après confirmation", claimed=True)
        problems = []
        if pos.epic != p["epic"] or pos.direction != "BUY":
            problems.append(f"position inattendue {pos.epic} {pos.direction}")
        if abs(pos.size - size) > 1e-9:
            problems.append(f"taille {pos.size} ≠ {size}")
        if pos.stop_level is None or pos.profit_level is None:
            try:
                self.client.update_position(pos.deal_id, stop_level=p["stop_loss"], profit_level=p["take_profit"])
                pos = self.client.get_position(pos.deal_id) or pos
            except CapitalAPIError:
                pass
            if pos.stop_level is None or pos.profit_level is None:
                problems.append("SL/TP non attachés")
        if problems:
            try:
                self.client.close_position(pos.deal_id)
                closed = "position fermée"
            except CapitalAPIError as exc:
                closed = f"FERMETURE ÉCHOUÉE ({exc}) — INTERVENIR MANUELLEMENT"
            self._trip_kill_switch("; ".join(problems) + f" — {closed}")
            return self._fail(pid, "; ".join(problems) + f" — {closed}", claimed=True)

        tid = self.db.open_trade(
            proposal_id=pid, setup_id=p["setup_id"], deal_id=pos.deal_id, deal_reference=ref,
            mode=self.s.capital_env.upper(), direction="BUY", epic=p["epic"], opened_at=snap.now, entry=pos.level,
            size=pos.size, stop_loss=pos.stop_level or p["stop_loss"], take_profit=pos.profit_level or p["take_profit"],
            risk_ccy=size * decision.sizing.loss_per_unit)  # type: ignore[union-attr]
        self.db.update_proposal_status(pid, EXECUTED, f"deal {pos.deal_id}", expected_status="EXECUTING")
        self.notifier.notify(Event.EXECUTED, f"[{self.s.capital_env.upper()}] BUY {pos.size:g} {pos.epic} @ "
                                             f"{pos.level:.2f} SL {pos.stop_level} TP {pos.profit_level} deal {pos.deal_id}")
        return ExecutionResult(True, "ordre exécuté et vérifié", tid, pos.deal_id)


class PositionMonitor:
    """Suit les trades ouverts (DRY_RUN : simulation sur le prix live ; réel : état broker)."""

    def __init__(self, settings: Settings, db: Database, client: CapitalClient | None, market: MarketDataService | None,
                 notifier: Notifier):
        self.s = settings
        self.db = db
        self.client = client
        self.market = market
        self.notifier = notifier

    def _close(self, t: dict[str, Any], exit_price: float, reason: str, fx: float | None, now: datetime) -> None:
        vpp = 1.0
        if self.market is not None:
            try:
                vpp = self.market.instrument().value_per_point
            except Exception:  # noqa: BLE001
                pass
        rate = fx if fx else self.s.backtest_quote_to_account_rate
        pnl = (exit_price - t["entry"]) * t["size"] * vpp * rate
        self.db.close_trade(t["id"], closed_at=now, exit_price=exit_price, pnl_ccy=pnl, exit_reason=reason)
        ev = Event.TP_HIT if reason.startswith("TP") else Event.SL_HIT if reason.startswith("SL") else Event.POSITION_CLOSED
        self.notifier.notify(ev, f"Trade #{t['id']} clôturé : {reason} @ {exit_price:.2f}, P&L ≈ {pnl:+.2f} "
                                 f"{self.s.account_currency}" + ("" if fx else " (taux FX de repli)"))
        daily = self.db.daily()
        if daily["realized_pnl"] <= -self.s.max_daily_loss_chf:
            self.notifier.notify(Event.DAILY_LIMIT, "Perte journalière maximale atteinte : nouvelles positions bloquées")

    def check(self, snap: MarketSnapshot | None = None) -> None:
        trades = self.db.open_trades()
        if not trades:
            return
        if snap is None and self.market is not None:
            snap = self.market.snapshot(with_candles=False)
        now = snap.now if snap else datetime.now(timezone.utc)
        for t in trades:
            if t["mode"] == "DRY_RUN":
                if snap is None or snap.quote is None:
                    continue
                bid = snap.quote.bid
                if bid <= t["stop_loss"]:
                    self._close(t, t["stop_loss"], "SL", snap.fx_rate, now)
                elif bid >= t["take_profit"]:
                    self._close(t, t["take_profit"], "TP", snap.fx_rate, now)
                elif self.s.breakeven_at_1r and bid >= t["entry"] + (t["entry"] - t["stop_loss"]) \
                        and t["stop_loss"] < t["entry"]:
                    self.db._exec("UPDATE trades SET stop_loss=? WHERE id=?", (t["entry"], t["id"]))
                continue
            if self.client is None:
                continue
            try:
                pos = self.client.get_position(t["deal_id"])
            except CapitalAPIError as exc:
                self.notifier.notify(Event.API_ERROR, f"Surveillance position {t['deal_id']} : {exc}")
                continue
            if pos is not None:
                if self.s.breakeven_at_1r and snap and snap.quote and pos.stop_level is not None \
                        and pos.stop_level < t["entry"] and snap.quote.bid >= 2 * t["entry"] - t["stop_loss"]:
                    try:
                        self.client.update_position(pos.deal_id, stop_level=t["entry"])
                        self.db._exec("UPDATE trades SET stop_loss=? WHERE id=?", (t["entry"], t["id"]))
                    except CapitalAPIError as exc:
                        self.notifier.notify(Event.API_ERROR, f"Break-even impossible : {exc}")
                continue
            # Position disparue : clôturée par SL/TP (ou manuellement). Niveau estimé.
            bid = snap.quote.bid if snap and snap.quote else None
            if bid is None:
                continue
            if abs(bid - t["stop_loss"]) <= abs(bid - t["take_profit"]):
                level, reason = t["stop_loss"], "SL (estimé)"
            else:
                level, reason = t["take_profit"], "TP (estimé)"
            self._close(t, level, reason, snap.fx_rate if snap else None, now)


def opened_at(t: dict[str, Any]) -> datetime | None:
    return parse_iso(t.get("opened_at"))
