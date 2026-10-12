"""Service de données de marché live (Capital.com) : bougies closes, prix, compte, FX.

Tout ce qui ne peut pas être obtenu proprement renvoie None et est signalé : le Risk
Manager transforme ces None en NO TRADE (fail-closed).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from broker.capital_api import CapitalAPIError, CapitalClient
from broker.models import AccountInfo, InstrumentSpec, Position, Quote
from config import Settings
from data.candles import Candle, closed_only
from strategy.vwap import VwapTracker, VwapValue

log = logging.getLogger(__name__)


@dataclass
class MarketSnapshot:
    now: datetime
    h4: list[Candle] = field(default_factory=list)
    m15: list[Candle] = field(default_factory=list)
    m1: list[Candle] = field(default_factory=list)
    quote: Quote | None = None
    instrument: InstrumentSpec | None = None
    account: AccountInfo | None = None
    positions: list[Position] | None = None
    fx_rate: float | None = None
    vwaps: dict[str, VwapValue] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def last_m1_close(self) -> datetime | None:
        return self.m1[-1].close_time("M1") if self.m1 else None

    @property
    def capital_available(self) -> float | None:
        return self.account.available if self.account else None


class MarketDataService:
    def __init__(self, client: CapitalClient, settings: Settings):
        self.client = client
        self.s = settings
        self.epic = settings.allowed_epics[0]
        self._inst: InstrumentSpec | None = None
        self._inst_ts = 0.0

    def instrument(self, force: bool = False) -> InstrumentSpec:
        if force or self._inst is None or time.monotonic() - self._inst_ts > 300:
            self._inst = self.client.get_instrument(self.epic)
            self._inst_ts = time.monotonic()
        return self._inst

    def resolve_epic(self) -> list[dict[str, Any]]:
        """Recherche les marchés « or » (pour vérifier l'EPIC, jamais pour le deviner)."""
        return self.client.search_markets(self.s.instrument_search_term)

    def fx_rate(self, quote_ccy: str) -> float | None:
        acct = self.s.account_currency
        if quote_ccy == acct:
            return 1.0
        pair = self.s.fx_epic_quote_to_account
        try:
            q = self.client.get_quote(pair, allow_any_epic=True)
        except CapitalAPIError as exc:
            log.warning("Taux FX %s indisponible : %s", pair, exc)
            return None
        if pair == f"{quote_ccy}{acct}":
            return q.mid
        if pair == f"{acct}{quote_ccy}":
            return 1.0 / q.mid if q.mid else None
        log.error("Paire FX %s incohérente avec %s→%s", pair, quote_ccy, acct)
        return None

    def account(self) -> AccountInfo | None:
        a = self.client.get_current_account()
        if a.currency != self.s.account_currency:
            log.error("Devise du compte %s ≠ ACCOUNT_CURRENCY %s", a.currency, self.s.account_currency)
            return None
        return a

    def snapshot(self, with_candles: bool = True) -> MarketSnapshot:
        now = datetime.now(timezone.utc)
        snap = MarketSnapshot(now=now)

        def attempt(name: str, fn):
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001 — fail-closed, l'erreur est remontée
                snap.errors.append(f"{name}: {exc}")
                return None

        snap.instrument = attempt("instrument", lambda: self.instrument(force=True))
        snap.quote = attempt("quote", lambda: self.client.get_quote(self.epic))
        snap.account = attempt("account", self.account)
        snap.positions = attempt("positions", self.client.get_positions)
        if snap.instrument is not None:
            snap.fx_rate = attempt("fx", lambda: self.fx_rate(snap.instrument.currency))
        if with_candles:
            h4 = attempt("h4", lambda: self.client.get_candles(self.epic, "H4", max_bars=250))
            m15 = attempt("m15", lambda: self.client.get_candles(self.epic, "M15", max_bars=1000))
            m1 = attempt("m1", lambda: self.client.get_candles(self.epic, "M1", max_bars=300))
            snap.h4 = closed_only(h4 or [], "H4", now)
            snap.m15 = closed_only(m15 or [], "M15", now)
            snap.m1 = closed_only(m1 or [], "M1", now)
            p = self.s.strategy
            snap.vwaps = VwapTracker(p.session_reset_hour_utc, p.vwap_min_volume_coverage).feed(snap.m15).values()
        return snap
