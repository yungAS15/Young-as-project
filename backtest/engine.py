"""Moteur de backtest événementiel sur bougies M1.

Garanties :
- **Mêmes règles que le live** : `SignalEngine.find_setup/confirm_entry` et
  `RiskManager.evaluate(stage="backtest")` sont exactement les objets utilisés en live.
- **Aucun lookahead** : M15 et H4 sont reconstruites à partir des M1 ; à l'instant t
  (clôture de la M1 j) seules les bougies dont la clôture ≤ t sont visibles. Une
  décision prise à la clôture de la M1 j est exécutée à l'OUVERTURE de la M1 j+1.
- **Coûts** : entrée à l'ASK (= BID + spread de la bougie) + slippage ; SL exécuté au
  niveau moins le slippage (ou à l'ouverture si gap) ; commission configurable.
- **Ambiguïté intra-bougie** : si SL et TP sont touchés dans la même M1, on suppose le
  SL (hypothèse pessimiste).
- 1 position max, 2 trades/jour max, perte journalière max : appliqués par le Risk Manager.
- Approbation manuelle supposée instantanée (hypothèse optimiste, à garder en tête).
- Frais overnight (swap) NON simulés : à ajouter via BACKTEST_COMMISSION_CHF si besoin.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Callable, Sequence

from broker.models import InstrumentSpec
from config import Settings
from data.candles import TF_SECONDS, Candle, resample
from risk.risk_manager import RiskContext, RiskManager
from strategy.signal_engine import NoSetup, NoTrade, Setup, SignalEngine, TradeSignal
from strategy.vwap import VwapTracker
from backtest.metrics import SimTrade, compute_report, merge_refusals

log = logging.getLogger(__name__)

H4_WINDOW = 200
M15_WINDOW = 400
M1_WINDOW = 120


def backtest_instrument(settings: Settings) -> InstrumentSpec:
    """Spécifications utilisées en backtest. En live, elles viennent TOUJOURS de l'API.

    Valeurs par défaut : celles observées pour GOLD sur Capital.com (à recouper avec
    `python main.py instrument`), surchargées si un cache `var/instrument_GOLD.json` existe.
    """
    return InstrumentSpec(
        epic=settings.allowed_epics[0], name="Gold (backtest)", instrument_type="COMMODITIES", currency="USD",
        lot_size=1.0, min_deal_size=0.01, max_deal_size=1000.0, size_increment=0.01,
        min_stop_distance=0.0, min_stop_distance_unit="POINTS", market_status="TRADEABLE",
        scaling_factor=1.0, margin_factor=5.0, margin_factor_unit="PERCENTAGE",
    )


def normalize_reason(reason: str) -> str:
    r = re.sub(r"[-+]?\d+(?:\.\d+)?", "#", reason)
    return r.split(" — ")[0][:90]


@dataclass
class BacktestResult:
    trades: list[SimTrade]
    report: dict[str, Any]
    refusals: list[str] = field(default_factory=list)
    signals: list[TradeSignal] = field(default_factory=list)


class BacktestEngine:
    def __init__(self, settings: Settings, instrument: InstrumentSpec | None = None,
                 engine: SignalEngine | None = None, progress: Callable[[float], None] | None = None):
        self.s = settings
        self.inst = instrument or backtest_instrument(settings)
        self.engine = engine or SignalEngine(settings.strategy, settings.weights)
        self.rm = RiskManager(settings)
        self.progress = progress

    def run(self, m1: Sequence[Candle], label: str = "", start: datetime | None = None,
            end: datetime | None = None) -> BacktestResult:
        s = self.s
        p = s.strategy
        fx = s.backtest_quote_to_account_rate
        vpp = self.inst.value_per_point
        slip = s.backtest_slippage_points
        m1 = [c if c.spread > 0 else replace(c, spread=s.backtest_default_spread) for c in m1]
        m15 = resample(m1, "M15")
        h4 = resample(m1, "H4")
        vwap = VwapTracker(p.session_reset_hour_utc, p.vwap_min_volume_coverage)

        from zoneinfo import ZoneInfo
        tz = ZoneInfo(s.trading_timezone)

        equity = s.backtest_initial_capital_chf
        trades: list[SimTrade] = []
        refusals: list[str] = []
        signals: list[TradeSignal] = []
        day_trades: dict[Any, int] = {}
        day_pnl: dict[Any, float] = {}
        used_setups: set[str] = set()
        armed: Setup | None = None
        pending: tuple[TradeSignal, float, float] | None = None   # (signal, size, risk)
        position: SimTrade | None = None
        i15 = i4 = 0
        n = len(m1)
        last_j = n - 1
        if end is not None:
            last_j = max((k for k in range(n) if m1[k].time < end), default=-1)
        m15_sec = TF_SECONDS["M15"]
        h4_sec = TF_SECONDS["H4"]

        for j, bar in enumerate(m1):
            if j > last_j:
                break
            t_close = bar.time.timestamp() + 60
            # --- 1) exécution de l'entrée décidée à la bougie précédente (open de j)
            if pending is not None:
                sig, size, risk = pending
                pending = None
                fill = bar.open + bar.spread + slip
                if fill <= sig.stop_loss or fill >= sig.take_profit:
                    refusals.append("Entrée annulée : gap au-delà du SL/TP à l'ouverture")
                else:
                    position = SimTrade(sig.setup_id, sig.time, bar.time, fill, sig.stop_loss, sig.take_profit,
                                        size, risk, sig.score, sig.rr, dict(sig.flags),
                                        initial_sl=sig.stop_loss, initial_size=size)
                    day = bar.time.astimezone(tz).date()
                    day_trades[day] = day_trades.get(day, 0) + 1

            # --- 2) gestion de la position (BID)
            if position is not None:
                exit_px = None
                reason = ""
                if bar.open <= position.stop_loss:
                    exit_px, reason = bar.open - slip, "SL (gap)"
                elif bar.low <= position.stop_loss:
                    exit_px, reason = position.stop_loss - slip, "SL"
                elif bar.open >= position.take_profit:
                    exit_px, reason = bar.open, "TP (gap)"
                elif bar.high >= position.take_profit:
                    exit_px, reason = position.take_profit, "TP"
                elif j == last_j:
                    exit_px, reason = bar.close, "FIN DE PÉRIODE"
                if exit_px is None:
                    # Gestion optionnelle à 1R (décidée à la clôture, effective dès la bougie suivante)
                    one_r = position.entry + (position.entry - position.initial_sl)
                    if bar.high >= one_r:
                        if s.partial_close_at_1r and not position.partial_done:
                            half = position.size / 2
                            position.realized_partial_ccy += (one_r - position.entry) * half * vpp * fx
                            position.size -= half
                            position.partial_done = True
                        if s.breakeven_at_1r and not position.be_moved:
                            position.stop_loss = position.entry
                            position.be_moved = True
                if exit_px is not None:
                    gross = (exit_px - position.entry) * position.size * vpp * fx + position.realized_partial_ccy
                    position.costs_ccy = s.backtest_commission_chf
                    position.pnl_ccy = gross - s.backtest_commission_chf
                    if position.be_moved and reason.startswith("SL"):
                        reason = "BE"   # stop au point d'entrée (perte = spread + slippage)
                    position.size = position.initial_size
                    position.exit_price = exit_px
                    position.exit_time = bar.time
                    position.exit_reason = reason
                    equity += position.pnl_ccy
                    d = bar.time.astimezone(tz).date()
                    day_pnl[d] = day_pnl.get(d, 0.0) + position.pnl_ccy
                    trades.append(position)
                    position = None

            # --- 3) nouvelles bougies M15 / H4 closes à t_close
            new_m15 = False
            while i15 < len(m15) and m15[i15].time.timestamp() + m15_sec <= t_close:
                vwap.update(m15[i15])
                i15 += 1
                new_m15 = True
            while i4 < len(h4) and h4[i4].time.timestamp() + h4_sec <= t_close:
                i4 += 1
            if (start is not None and bar.time < start) or j == last_j:
                continue

            h4_win = h4[max(0, i4 - H4_WINDOW):i4]
            m15_win = m15[max(0, i15 - M15_WINDOW):i15]
            if new_m15:
                st = self.engine.find_setup(h4_win, m15_win)
                if isinstance(st, Setup):
                    if st.setup_id not in used_setups and (armed is None or armed.setup_id != st.setup_id):
                        armed = st
                elif armed is not None and isinstance(st, NoSetup) and "invalidé" in st.reason.lower():
                    armed = None

            # --- 4) confirmation M1 et décision
            if armed is None or position is not None or pending is not None:
                continue
            m1_win = m1[max(0, j - M1_WINDOW + 1):j + 1]
            out = self.engine.confirm_entry(armed, h4_win, m15_win, m1_win, vwap.values(), bar.spread)
            if isinstance(out, NoTrade):
                if "expiré" in out.reason or "Invalidé" in out.reason:
                    refusals.append(normalize_reason(out.reason))
                    armed = None
                elif out.reason.startswith("SCORE") or "RR" in out.reason or "SL" in out.reason:
                    refusals.append(normalize_reason(out.reason))
                    used_setups.add(armed.setup_id)
                    armed = None
                continue
            signals.append(out)
            day = bar.time.astimezone(tz).date()
            now = datetime.fromtimestamp(t_close, tz=bar.time.tzinfo)
            ctx = RiskContext(
                now=now, epic=self.inst.epic, direction=out.direction, entry=out.entry, stop_loss=out.stop_loss,
                take_profit=out.take_profit, score=out.score, instrument=self.inst, capital_available=equity,
                quote_to_account_rate=fx, bid=bar.close, offer=bar.close + bar.spread, quote_time=now,
                market_status="TRADEABLE", last_candle_close=now, trades_today=day_trades.get(day, 0),
                realized_pnl_today=day_pnl.get(day, 0.0), open_positions=0, connected=True,
                consecutive_api_errors=0, macro_blackout=False,
            )
            dec = self.rm.evaluate(ctx, "backtest")
            used_setups.add(armed.setup_id)    # une seule entrée par signal
            armed = None
            if not dec.approved:
                refusals += ["RISK " + normalize_reason(c.name + ": " + c.detail) for c in dec.failed]
                continue
            pending = (out, dec.size, dec.sizing.risk_effective)  # type: ignore[union-attr]
            if self.progress and j % 5000 == 0:
                self.progress(j / n)

        report = compute_report(trades, s.backtest_initial_capital_chf, settings=s, signals_seen=len(signals),
                                refusals=merge_refusals(refusals), label=label)
        return BacktestResult(trades, report, refusals, signals)
