"""Boucle principale du bot (live) : analyse → proposition → approbation → exécution → suivi.

Une itération par nouvelle bougie M1 close (≈ toutes les minutes), plus le traitement
des approbations toutes les quelques secondes. Le statut courant est écrit en base
(clé `status`) pour l'interface web / CLI, qui peut tourner dans un autre processus.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from approval.trade_approval import APPROVED, ApprovalManager, format_proposal
from broker.capital_api import CapitalAPIError, CapitalClient
from config import Mode, Settings
from data.database import Database, iso
from data.market_data import MarketDataService, MarketSnapshot
from execution.trade_executor import PositionMonitor, TradeExecutor, build_risk_context, effective_kill_switch
from monitoring.alerts import Event, Notifier
from monitoring.daily_report import build_daily_report
from risk.risk_manager import RiskManager
from strategy.signal_engine import NoTrade, Setup, SignalEngine, TradeSignal

log = logging.getLogger(__name__)


class BotRunner:
    def __init__(self, settings: Settings, db: Database, client: CapitalClient, notifier: Notifier):
        self.s = settings
        self.db = db
        self.client = client
        self.notifier = notifier
        self.market = MarketDataService(client, settings)
        self.engine = SignalEngine(settings.strategy, settings.weights)
        self.rm = RiskManager(settings)
        self.approvals = ApprovalManager(db, settings)
        self.executor = TradeExecutor(settings, db, self.market, client, self.approvals, notifier)
        self.monitor = PositionMonitor(settings, db, client, self.market, notifier)
        self.setup: Setup | None = None
        self.last_m15: datetime | None = None
        self.last_m1: datetime | None = None
        self.last_report_day: str | None = db.get("last_report_day")
        self.proposed_setups: set[str] = set()

    # --------------------------------------------------------------- status
    def write_status(self, snap: MarketSnapshot | None, extra: dict[str, Any] | None = None) -> None:
        daily = self.db.daily()
        q = snap.quote if snap else None
        st: dict[str, Any] = {
            "updated": iso(datetime.now(timezone.utc)),
            "mode": self.s.mode.value, "dry_run": self.s.dry_run, "env": self.s.capital_env,
            "kill_switch": effective_kill_switch(self.s, self.db),
            "connected": self.client.connected, "api_errors": self.client.consecutive_errors,
            "balance": snap.account.balance if snap and snap.account else None,
            "available": snap.account.available if snap and snap.account else None,
            "equity": (snap.account.balance + snap.account.profit_loss) if snap and snap.account else None,
            "pnl_today": daily["realized_pnl"], "trades_today": daily["trades_count"],
            "max_trades": self.s.max_trades_per_day,
            "bid": q.bid if q else None, "offer": q.offer if q else None,
            "spread": round(q.spread, 2) if q else None, "quote_time": iso(q.time) if q else None,
            "vwaps": {k: v.value for k, v in snap.vwaps.items()} if snap else {},
            "setup": self.setup.setup_id if self.setup else None,
            "bias": self.setup.bias.bias.value if self.setup else None,
            "open_trades": self.db.open_trades(),
            "errors": snap.errors if snap else [],
        }
        if extra:
            st.update(extra)
        self.db.set("status", json.dumps(st, default=str))

    # --------------------------------------------------------------- loop
    def step(self) -> None:
        now = datetime.now(timezone.utc)
        self.approvals.expire_stale()
        # Approbations en attente d'exécution
        for row in self.db.proposals(APPROVED, limit=5):
            res = self.executor.execute(row["id"])
            log.info("Exécution %s : %s", row["id"], res.message)

        minute = now.replace(second=0, microsecond=0)
        if self.last_m1 == minute:
            return
        self.last_m1 = minute
        snap = self.market.snapshot(with_candles=True)
        if snap.errors:
            self.notifier.notify(Event.API_ERROR, "Erreurs de données : " + "; ".join(snap.errors)[:800])
        self.monitor.check(snap)
        self.analyze(snap)
        self.write_status(snap)
        self.maybe_daily_report(snap)

    def analyze(self, snap: MarketSnapshot) -> None:
        if not snap.m15 or not snap.h4 or not snap.m1:
            return
        last15 = snap.m15[-1].time
        if last15 != self.last_m15:
            self.last_m15 = last15
            st = self.engine.find_setup(snap.h4, snap.m15)
            if isinstance(st, Setup):
                if not self.db.setup_used(st.setup_id) and (self.setup is None or self.setup.setup_id != st.setup_id):
                    self.setup = st
                    self.notifier.notify(Event.SETUP_DETECTED,
                                         f"Setup {st.setup_id} armé : sweep {st.sweep.level_kind} {st.sweep.level:.2f} "
                                         f"(low {st.sweep.sweep_low:.2f}), CHOCH > {st.choch.level:.2f}, OTE "
                                         f"{st.ote.bottom:.2f}–{st.ote.top:.2f}. Attente confirmation M1.")
            elif self.setup is not None and "invalidé" in st.reason.lower():
                self.setup = None
        if self.setup is None or self.setup.setup_id in self.proposed_setups:
            return
        spread = snap.quote.spread if snap.quote else self.s.max_spread_points
        out = self.engine.confirm_entry(self.setup, snap.h4, snap.m15, snap.m1, snap.vwaps, spread)
        if isinstance(out, NoTrade):
            if "expiré" in out.reason or "Invalidé" in out.reason:
                self.setup = None
            elif out.reason.startswith("SCORE") or "RR" in out.reason:
                self.db.record_signal(setup_id=self.setup.setup_id, timeframe="M1", decision="NO_TRADE",
                                      refusal_reason=out.reason, details=out.details)
                self.proposed_setups.add(self.setup.setup_id)
            return
        self.propose(out, snap)

    def propose(self, sig: TradeSignal, snap: MarketSnapshot) -> None:
        ctx = build_risk_context(self.s, self.db, snap, entry=sig.entry, stop_loss=sig.stop_loss,
                                 take_profit=sig.take_profit, score=sig.score, connected=self.client.connected,
                                 api_errors=self.client.consecutive_errors)
        dec = self.rm.evaluate(ctx, "proposal")
        payload = self.approvals.build_payload(
            sig, dec, epic=self.s.allowed_epics[0], bid=snap.quote.bid if snap.quote else None,
            offer=snap.quote.offer if snap.quote else None, capital=snap.capital_available,
            trades_today=ctx.trades_today)
        sid = self.db.record_signal(
            setup_id=sig.setup_id, timeframe="M1", price=snap.quote.bid if snap.quote else None,
            h4_bias=sig.analysis.get("H4"), structure=sig.analysis.get("M15"), liquidity=self.setup.sweep.level_kind
            if self.setup else None, sweep=1, vwap=sig.vwaps, fvg=int(sig.flags.get("fvg", False)),
            ob=int(sig.flags.get("ob", False)), fib_ote=int(sig.flags.get("ote", False)), score=sig.score,
            entry=sig.entry, stop_loss=sig.stop_loss, take_profit=sig.take_profit, rr=sig.rr, size=payload["size"],
            risk_ccy=payload["risk_ccy"], risk_pct=payload["risk_pct"],
            decision="PROPOSED" if dec.approved else "BLOCKED",
            refusal_reason=None if dec.approved else dec.summary(), details=payload)
        pid = self.approvals.create(payload, dec.approved, sid)
        self.proposed_setups.add(sig.setup_id)
        status = "PENDING" if dec.approved else "BLOCKED"
        self.notifier.notify(Event.PROPOSAL, format_proposal(pid, payload, status))
        if dec.approved and self.s.mode == Mode.AUTO and self.s.auto_trading and not self.s.manual_approval_required:
            self.db.update_proposal_status(pid, APPROVED, "AUTO", expected_status="PENDING")
            res = self.executor.execute(pid)
            log.info("AUTO %s : %s", pid, res.message)

    def maybe_daily_report(self, snap: MarketSnapshot) -> None:
        from zoneinfo import ZoneInfo
        local = datetime.now(ZoneInfo(self.s.trading_timezone))
        day = local.date().isoformat()
        if self.last_report_day == day or local.strftime("%H:%M") < self.s.daily_report_time:
            return
        daily = self.db.daily()
        txt = build_daily_report(self.s, snap.now, snap.h4, snap.m15, snap.vwaps, self.setup,
                                 daily["trades_count"], daily["realized_pnl"])
        self.notifier.notify(Event.DAILY_REPORT, txt)
        self.last_report_day = day
        self.db.set("last_report_day", day)

    def run_forever(self, poll_s: float = 5.0) -> None:
        if self.s.partial_close_at_1r and not self.s.dry_run:
            raise SystemExit("PARTIAL_CLOSE_AT_1R n'est supporté qu'en backtest/DRY_RUN pour l'instant.")
        self.notifier.notify(Event.BOT_STARTED,
                             f"Bot démarré — mode {self.s.mode.value}, DRY_RUN={self.s.dry_run}, "
                             f"env={self.s.capital_env}, kill switch={effective_kill_switch(self.s, self.db)}")
        try:
            while True:
                try:
                    self.step()
                except CapitalAPIError as exc:
                    self.notifier.notify(Event.API_ERROR, str(exc))
                    if self.client.consecutive_errors >= self.s.max_consecutive_api_errors:
                        self.notifier.notify(Event.API_ERROR, "Erreurs API consécutives : nouvelles positions refusées")
                except Exception:  # noqa: BLE001
                    log.exception("Erreur dans la boucle principale")
                time.sleep(poll_s)
        except KeyboardInterrupt:
            pass
        finally:
            self.notifier.notify(Event.BOT_STOPPED, "Bot arrêté")
