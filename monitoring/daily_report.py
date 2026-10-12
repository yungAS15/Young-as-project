"""Rapport quotidien (avant l'ouverture de Londres) : biais H4, liquidités, zones, VWAP, macro."""
from __future__ import annotations

from datetime import datetime
from typing import Sequence

from config import Settings, resolve_path
from data.candles import Candle
from monitoring.macro_calendar import load_events, today_events
from strategy.indicators import atr
from strategy.market_structure import find_swings, h4_bias, swing_highs, swing_lows
from strategy.signal_engine import Setup
from strategy.vwap import VwapValue


def build_daily_report(settings: Settings, now: datetime, h4: Sequence[Candle], m15: Sequence[Candle],
                       vwaps: dict[str, VwapValue], setup: Setup | None, trades_today: int,
                       pnl_today: float) -> str:
    p = settings.strategy
    lines = [f"RAPPORT QUOTIDIEN XAU/USD — {now:%Y-%m-%d %H:%M} UTC"]
    if not h4 or not m15:
        lines.append("Données indisponibles : rapport incomplet.")
        return "\n".join(lines)
    price = m15[-1].close
    bias = h4_bias(h4, p.swing_k_h4, p.h4_ema_period)
    lines.append(f"Prix (BID, dernière M15 close) : {price:.2f}")
    lines.append(f"Biais H4 : {bias.bias.value} — {bias.detail}")
    if bias.bias.value not in ("BULLISH", "NEUTRAL_BULLISH"):
        lines.append("→ Pas de BUY tant que le biais H4 n'est pas haussier/neutre-haussier.")
    sw15 = find_swings(m15[-p.liquidity_lookback_m15:], p.swing_k_m15)
    sw4 = find_swings(h4, p.swing_k_h4)
    below = sorted({round(s.price, 2) for s in swing_lows(sw15) + swing_lows(sw4) if s.price < price}, reverse=True)[:4]
    above = sorted({round(s.price, 2) for s in swing_highs(sw15) + swing_highs(sw4) if s.price > price})[:4]
    lines.append("Liquidité sell-side (sous le prix, candidates au sweep) : " + (", ".join(map(str, below)) or "—"))
    lines.append("Liquidité buy-side (au-dessus, candidates TP) : " + (", ".join(map(str, above)) or "—"))
    a15 = atr(m15, p.atr_period)
    a4 = atr(h4, p.atr_period)
    lines.append(f"ATR M15 : {a15:.2f}   ATR H4 : {a4:.2f}" if a15 and a4 else "ATR indisponible")
    vt = []
    for k, v in vwaps.items():
        vt.append(f"{k} {v.value:.2f}" if v.available else f"{k} indisponible ({v.reason})")
    lines.append("VWAP (tick-volume) : " + "; ".join(vt))
    if setup:
        zones = "; ".join(f"{n} {z.bottom:.2f}–{z.top:.2f}" for n, z in setup.zones)
        lines.append(f"Setup armé {setup.setup_id} : sweep {setup.sweep.sweep_low:.2f}, zones d'entrée : {zones}")
    else:
        lines.append("Aucun setup armé pour l'instant (zones d'entrée potentielles : après un sweep des niveaux "
                     "sell-side ci-dessus + CHOCH M15).")
    if settings.macro_pause_enabled:
        evs = load_events(resolve_path(settings.macro_calendar_path))
        if evs is None:
            lines.append("Macro : calendrier ABSENT → trading bloqué par la protection macro.")
        else:
            ev = today_events(now, evs)
            lines.append("Macro aujourd'hui : " + (", ".join(f"{e.name} {e.time:%H:%M} UTC" for e in ev) or "aucun"))
    else:
        lines.append("Macro : pause désactivée (MACRO_PAUSE_ENABLED=false) — vérifier le calendrier manuellement.")
    lines.append(f"Trades du jour : {trades_today}/{settings.max_trades_per_day}   P&L du jour : {pnl_today:+.2f} "
                 f"{settings.account_currency}")
    lines.append("Rappel : le score est un indicateur de confluence, pas une probabilité. 0 trade est un bon "
                 "résultat sans setup valide.")
    return "\n".join(lines)
