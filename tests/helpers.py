"""Fabriques de données de test."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from broker.models import InstrumentSpec
from config import Settings
from data.candles import Candle
from risk.risk_manager import RiskContext

T0 = datetime(2025, 3, 3, 0, 0, tzinfo=timezone.utc)  # lundi


def c(i: int, o: float, h: float, lo: float, cl: float, tf_min: int = 15, vol: float = 100.0,
      spread: float = 0.3, t0: datetime = T0) -> Candle:
    return Candle(t0 + timedelta(minutes=tf_min * i), o, h, lo, cl, vol, spread)


def series(points: list[tuple[float, float, float, float]], tf_min: int = 15, t0: datetime = T0,
           vol: float = 100.0) -> list[Candle]:
    return [c(i, *p, tf_min=tf_min, vol=vol, t0=t0) for i, p in enumerate(points)]


def gold_spec(**kw) -> InstrumentSpec:
    base = InstrumentSpec(
        epic="GOLD", name="Gold", instrument_type="COMMODITIES", currency="USD", lot_size=1.0,
        min_deal_size=0.01, max_deal_size=100.0, size_increment=0.01, min_stop_distance=0.5,
        min_stop_distance_unit="POINTS", market_status="TRADEABLE", scaling_factor=1.0,
        margin_factor=5.0, margin_factor_unit="PERCENTAGE", bid=2000.0, offer=2000.3,
    )
    return replace(base, **kw)


def unlocked_settings(**kw) -> Settings:
    """Réglages déverrouillés pour tester la logique (jamais l'état par défaut)."""
    base = Settings(kill_switch=False, dry_run=True)
    return replace(base, **kw)


NOW = datetime(2025, 3, 5, 10, 0, tzinfo=timezone.utc)


def ctx(**kw) -> RiskContext:
    base = RiskContext(
        now=NOW, epic="GOLD", direction="BUY", entry=2000.30, stop_loss=1995.30, take_profit=2010.30,
        score=8.0, instrument=gold_spec(), capital_available=5000.0, quote_to_account_rate=0.9,
        bid=2000.0, offer=2000.3, quote_time=NOW - timedelta(seconds=2), market_status="TRADEABLE",
        last_candle_close=NOW - timedelta(seconds=30), trades_today=0, realized_pnl_today=0.0,
        open_positions=0, connected=True, consecutive_api_errors=0, macro_blackout=False,
    )
    return replace(base, **kw)
