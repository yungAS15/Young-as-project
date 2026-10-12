"""Client REST officiel Capital.com (https://open-api.capital.com/).

Points clés de la documentation (lue le 2026-10-12) :
- Base URL live : https://api-capital.backend-capital.com ; démo : https://demo-api-capital.backend-capital.com
- POST /api/v1/session (header X-CAP-API-KEY, body identifier/password) → headers CST et
  X-SECURITY-TOKEN, valables 10 min après la dernière utilisation.
- Limites : 10 req/s par utilisateur ; 1 req / 0.1 s pour les ordres ; POST /session 1 req/s.
- POST /positions renvoie un dealReference : **ce n'est pas une exécution**. Il faut
  vérifier GET /confirms/{dealReference} (dealStatus ACCEPTED + affectedDeals) puis la position.
- GET /markets/{epic} : dealingRules (minDealSize, maxDealSize, minSizeIncrement,
  minStopOrProfitDistance…), instrument (lotSize, currency, openingHours), snapshot (bid/offer).
- GET /prices/{epic}?resolution=&max=(≤1000)&from=&to= : bougies bid/ask + lastTradedVolume.

Les secrets ne sont jamais journalisés ; les erreurs sont nettoyées avant d'être levées.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import requests

from broker.models import AccountInfo, DealConfirmation, InstrumentSpec, Position, Quote
from data.candles import CAPITAL_RESOLUTION, TF_SECONDS, Candle, ensure_utc

log = logging.getLogger(__name__)

SESSION_LIFETIME_S = 9 * 60      # on renouvelle avant les 10 min de la doc
API_DATE_FMT = "%Y-%m-%dT%H:%M:%S"


class CapitalAPIError(Exception):
    """Erreur API (HTTP, réseau, réponse invalide). Message sans secret."""

    def __init__(self, message: str, status: int | None = None, error_code: str | None = None):
        super().__init__(message)
        self.status = status
        self.error_code = error_code


class InstrumentNotAllowed(CapitalAPIError):
    pass


class RateLimiter:
    """Espacement minimal entre requêtes (thread-safe)."""

    def __init__(self, min_interval_s: float, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.min_interval_s = min_interval_s
        self._last = 0.0
        self._lock = threading.Lock()
        self._clock = clock
        self._sleep = sleep

    def wait(self) -> None:
        with self._lock:
            now = self._clock()
            delta = now - self._last
            if delta < self.min_interval_s:
                self._sleep(self.min_interval_s - delta)
            self._last = self._clock()


def _parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    raw = raw.replace("Z", "")
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _rule_value(rules: dict[str, Any], key: str) -> tuple[float | None, str]:
    item = rules.get(key) or {}
    val = item.get("value")
    return (float(val) if val is not None else None, str(item.get("unit", "")))


class CapitalClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        identifier: str,
        password: str,
        *,
        allowed_epics: tuple[str, ...] = ("GOLD",),
        timeout_s: float = 10.0,
        max_retries: int = 2,
        http: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._identifier = identifier
        self._password = password
        self.allowed_epics = tuple(allowed_epics)
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.http = http or requests.Session()
        self._sleep = sleep
        self._cst: str | None = None
        self._token: str | None = None
        self._session_ts: float = 0.0
        self._limiter = RateLimiter(0.11, sleep=sleep)          # < 10 req/s
        self._order_limiter = RateLimiter(0.25, sleep=sleep)    # ordres : >= 0.1 s, marge
        self._session_limiter = RateLimiter(1.1, sleep=sleep)   # POST /session : 1 req/s
        self.consecutive_errors = 0
        self.last_success_ts: float | None = None
        self.account_currency: str | None = None
        self.current_account_id: str | None = None

    # ------------------------------------------------------------------ utils
    def _secrets(self) -> tuple[str, ...]:
        return tuple(s for s in (self._api_key, self._identifier, self._password, self._cst, self._token) if s)

    def _scrub(self, text: str) -> str:
        for s in self._secrets():
            text = text.replace(s, "***")
        return text[:500]

    def _check_epic(self, epic: str) -> None:
        if epic not in self.allowed_epics:
            raise InstrumentNotAllowed(f"Instrument hors allowlist: {epic}")

    @property
    def connected(self) -> bool:
        return bool(self._cst and self._token) and (time.monotonic() - self._session_ts) < SESSION_LIFETIME_S

    # --------------------------------------------------------------- session
    def login(self) -> dict[str, Any]:
        if not (self._api_key and self._identifier and self._password):
            raise CapitalAPIError("Identifiants Capital.com absents (.env)")
        self._session_limiter.wait()
        try:
            resp = self.http.post(
                f"{self.base_url}/api/v1/session",
                json={"identifier": self._identifier, "password": self._password, "encryptedPassword": False},
                headers={"X-CAP-API-KEY": self._api_key, "Content-Type": "application/json"},
                timeout=self.timeout_s,
            )
        except requests.RequestException as exc:
            self.consecutive_errors += 1
            raise CapitalAPIError(self._scrub(f"Connexion impossible: {type(exc).__name__}")) from None
        if resp.status_code != 200:
            self.consecutive_errors += 1
            raise CapitalAPIError(self._scrub(f"Authentification refusée (HTTP {resp.status_code}): {resp.text}"),
                                  status=resp.status_code)
        cst = resp.headers.get("CST")
        token = resp.headers.get("X-SECURITY-TOKEN")
        if not cst or not token:
            self.consecutive_errors += 1
            raise CapitalAPIError("Réponse de session sans CST/X-SECURITY-TOKEN")
        self._cst, self._token = cst, token
        self._session_ts = time.monotonic()
        self.consecutive_errors = 0
        body = resp.json() if resp.content else {}
        self.account_currency = body.get("currencyIsoCode")
        self.current_account_id = body.get("currentAccountId")
        log.info("Session Capital.com ouverte (compte %s, devise %s)",
                 "***" + str(self.current_account_id)[-4:] if self.current_account_id else "?",
                 self.account_currency)
        return {"account_currency": self.account_currency, "account_id": self.current_account_id}

    def ensure_session(self) -> None:
        if not self.connected:
            self.login()

    def logout(self) -> None:
        if self._cst:
            try:
                self._request("DELETE", "/api/v1/session", retry_auth=False)
            except CapitalAPIError:
                pass
        self._cst = self._token = None

    # --------------------------------------------------------------- request
    def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None,
                 json: dict[str, Any] | None = None, retry_auth: bool = True,
                 is_order: bool = False) -> Any:
        self.ensure_session()
        attempt = 0
        while True:
            (self._order_limiter if is_order else self._limiter).wait()
            headers = {"CST": self._cst or "", "X-SECURITY-TOKEN": self._token or "",
                       "Content-Type": "application/json"}
            try:
                resp = self.http.request(method, f"{self.base_url}{path}", params=params, json=json,
                                         headers=headers, timeout=self.timeout_s)
            except requests.RequestException as exc:
                self.consecutive_errors += 1
                # Jamais de retry automatique sur un ordre : risque de doublon.
                if is_order or attempt >= self.max_retries:
                    raise CapitalAPIError(self._scrub(f"Erreur réseau {type(exc).__name__} sur {method} {path}")) from None
                attempt += 1
                self._sleep(min(2 ** attempt, 8))
                continue

            if resp.status_code == 401 and retry_auth:
                # Session expirée : on se reconnecte une fois.
                self._cst = self._token = None
                self.login()
                retry_auth = False
                continue
            if resp.status_code == 429 and not is_order and attempt < self.max_retries:
                attempt += 1
                self._sleep(min(2 ** attempt, 8))
                continue
            if resp.status_code >= 500 and not is_order and attempt < self.max_retries:
                attempt += 1
                self.consecutive_errors += 1
                self._sleep(min(2 ** attempt, 8))
                continue
            if resp.status_code >= 400:
                self.consecutive_errors += 1
                code = None
                try:
                    code = resp.json().get("errorCode")
                except ValueError:
                    pass
                raise CapitalAPIError(self._scrub(f"HTTP {resp.status_code} sur {method} {path}: {code or resp.text}"),
                                      status=resp.status_code, error_code=code)
            self._session_ts = time.monotonic()
            self.consecutive_errors = 0
            self.last_success_ts = time.time()
            if not resp.content:
                return {}
            try:
                return resp.json()
            except ValueError:
                self.consecutive_errors += 1
                raise CapitalAPIError(f"Réponse non JSON sur {method} {path}") from None

    # ------------------------------------------------------------- read-only
    def server_time(self) -> datetime | None:
        resp = self.http.get(f"{self.base_url}/api/v1/time", timeout=self.timeout_s)
        ms = resp.json().get("serverTime")
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc) if ms else None

    def ping(self) -> bool:
        self._request("GET", "/api/v1/ping")
        return True

    def get_preferences(self) -> dict[str, Any]:
        """GET /accounts/preferences : mode hedging et leviers (lecture seule)."""
        return dict(self._request("GET", "/api/v1/accounts/preferences"))

    def get_accounts(self) -> list[AccountInfo]:
        data = self._request("GET", "/api/v1/accounts")
        out = []
        for a in data.get("accounts", []):
            b = a.get("balance") or {}
            out.append(AccountInfo(
                account_id=str(a.get("accountId")), currency=str(a.get("currency")),
                balance=float(b.get("balance", 0.0)), available=float(b.get("available", 0.0)),
                profit_loss=float(b.get("profitLoss", 0.0)), deposit=float(b.get("deposit", 0.0)),
            ))
        return out

    def get_current_account(self) -> AccountInfo:
        self.ensure_session()
        accounts = self.get_accounts()
        for a in accounts:
            if a.account_id == str(self.current_account_id):
                return a
        raise CapitalAPIError("Compte actif introuvable dans GET /accounts")

    def search_markets(self, term: str) -> list[dict[str, Any]]:
        data = self._request("GET", "/api/v1/markets", params={"searchTerm": term})
        return list(data.get("markets", []))

    def get_instrument(self, epic: str) -> InstrumentSpec:
        self._check_epic(epic)
        data = self._request("GET", f"/api/v1/markets/{epic}")
        return self.parse_instrument(data)

    @staticmethod
    def parse_instrument(data: dict[str, Any]) -> InstrumentSpec:
        inst = data.get("instrument") or {}
        rules = data.get("dealingRules") or {}
        snap = data.get("snapshot") or {}
        min_size, _ = _rule_value(rules, "minDealSize")
        max_size, _ = _rule_value(rules, "maxDealSize")
        incr, _ = _rule_value(rules, "minSizeIncrement")
        min_stop, min_stop_unit = _rule_value(rules, "minStopOrProfitDistance")
        if None in (min_size, max_size, incr) or inst.get("lotSize") is None:
            raise CapitalAPIError("Spécifications instrument incomplètes (dealingRules/lotSize)")
        bid, offer = snap.get("bid"), snap.get("offer")
        # Distance mini de stop : convertie en points si l'unité est un pourcentage.
        min_stop_pts: float | None = None
        if min_stop is not None:
            if min_stop_unit == "PERCENTAGE" and offer:
                min_stop_pts = float(offer) * min_stop / 100.0
            elif min_stop_unit == "POINTS":
                min_stop_pts = min_stop
        return InstrumentSpec(
            epic=str(inst.get("epic")),
            name=str(inst.get("name", "")),
            instrument_type=str(inst.get("type", "")),
            currency=str(inst.get("currency", "")),
            lot_size=float(inst.get("lotSize")),
            min_deal_size=float(min_size),
            max_deal_size=float(max_size),
            size_increment=float(incr),
            min_stop_distance=min_stop_pts,
            min_stop_distance_unit=min_stop_unit,
            market_status=str(snap.get("marketStatus", "UNKNOWN")),
            scaling_factor=float(snap.get("scalingFactor", 1) or 1),
            margin_factor=inst.get("marginFactor"),
            margin_factor_unit=inst.get("marginFactorUnit"),
            opening_hours=inst.get("openingHours") or {},
            bid=float(bid) if bid is not None else None,
            offer=float(offer) if offer is not None else None,
            raw_dealing_rules=rules,
        )

    def get_quote(self, epic: str, *, allow_any_epic: bool = False) -> Quote:
        """Prix courant. `allow_any_epic` sert uniquement aux paires FX de conversion (lecture seule)."""
        if not allow_any_epic:
            self._check_epic(epic)
        data = self._request("GET", f"/api/v1/markets/{epic}")
        snap = data.get("snapshot") or {}
        if snap.get("bid") is None or snap.get("offer") is None:
            raise CapitalAPIError(f"Pas de prix pour {epic}")
        upd = _parse_dt(snap.get("updateTimeUTC")) or _parse_dt(snap.get("updateTime"))
        if upd is None:
            raise CapitalAPIError(f"Horodatage du prix absent pour {epic}")
        return Quote(epic=epic, bid=float(snap["bid"]), offer=float(snap["offer"]), time=upd,
                     market_status=str(snap.get("marketStatus", "UNKNOWN")))

    def get_candles(self, epic: str, tf: str, *, max_bars: int = 1000,
                    start: datetime | None = None, end: datetime | None = None) -> list[Candle]:
        """Bougies BID + spread (ASK-BID à la clôture). `time` = ouverture UTC."""
        self._check_epic(epic)
        params: dict[str, Any] = {"resolution": CAPITAL_RESOLUTION[tf], "max": min(max_bars, 1000)}
        if start is not None:
            params["from"] = ensure_utc(start).strftime(API_DATE_FMT)
        if end is not None:
            params["to"] = ensure_utc(end).strftime(API_DATE_FMT)
        data = self._request("GET", f"/api/v1/prices/{epic}", params=params)
        return self.parse_prices(data)

    @staticmethod
    def parse_prices(data: dict[str, Any]) -> list[Candle]:
        out: list[Candle] = []
        for p in data.get("prices", []):
            t = _parse_dt(p.get("snapshotTimeUTC"))
            if t is None:
                continue
            o, h, lo, c = (p.get(k) or {} for k in ("openPrice", "highPrice", "lowPrice", "closePrice"))
            if None in (o.get("bid"), h.get("bid"), lo.get("bid"), c.get("bid")):
                continue
            spread = (float(c["ask"]) - float(c["bid"])) if c.get("ask") is not None else 0.0
            out.append(Candle(t, float(o["bid"]), float(h["bid"]), float(lo["bid"]), float(c["bid"]),
                              float(p.get("lastTradedVolume") or 0.0), spread))
        out.sort(key=lambda x: x.time)
        return out

    def get_candles_range(self, epic: str, tf: str, start: datetime, end: datetime) -> list[Candle]:
        """Historique paginé (fenêtres de 1000 bougies max)."""
        step = timedelta(seconds=TF_SECONDS[tf] * 1000)
        cur = ensure_utc(start)
        end = ensure_utc(end)
        seen: dict[datetime, Candle] = {}
        while cur < end:
            nxt = min(cur + step, end)
            try:
                for c in self.get_candles(epic, tf, max_bars=1000, start=cur, end=nxt):
                    seen[c.time] = c
            except CapitalAPIError as exc:
                if exc.status in (400, 404):   # pas de données sur la fenêtre (week-end…)
                    log.debug("Pas de données %s %s→%s", tf, cur, nxt)
                else:
                    raise
            cur = nxt
        return [seen[k] for k in sorted(seen)]

    def get_positions(self) -> list[Position]:
        data = self._request("GET", "/api/v1/positions")
        return [self._parse_position(p) for p in data.get("positions", [])]

    def get_position(self, deal_id: str) -> Position | None:
        try:
            data = self._request("GET", f"/api/v1/positions/{deal_id}")
        except CapitalAPIError as exc:
            if exc.status == 404:
                return None
            raise
        return self._parse_position(data)

    @staticmethod
    def _parse_position(p: dict[str, Any]) -> Position:
        pos = p.get("position") or {}
        mkt = p.get("market") or {}
        return Position(
            deal_id=str(pos.get("dealId")), epic=str(mkt.get("epic")), direction=str(pos.get("direction")),
            size=float(pos.get("size", 0.0)), level=float(pos.get("level", 0.0)),
            stop_level=float(pos["stopLevel"]) if pos.get("stopLevel") is not None else None,
            profit_level=float(pos["profitLevel"]) if pos.get("profitLevel") is not None else None,
            upl=float(pos["upl"]) if pos.get("upl") is not None else None,
            currency=str(pos.get("currency", "")), created_utc=_parse_dt(pos.get("createdDateUTC")),
        )

    def get_activity(self, deal_id: str | None = None, last_period_s: int = 86400) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"lastPeriod": last_period_s, "detailed": "true"}
        if deal_id:
            params["dealId"] = deal_id
        return list(self._request("GET", "/api/v1/history/activity", params=params).get("activities", []))

    def get_transactions(self, last_period_s: int = 86400) -> list[dict[str, Any]]:
        return list(self._request("GET", "/api/v1/history/transactions",
                                  params={"lastPeriod": last_period_s}).get("transactions", []))

    # ---------------------------------------------------------------- trading
    def open_position(self, epic: str, direction: str, size: float, stop_level: float,
                      profit_level: float) -> str:
        """Envoie un ordre au marché AVEC SL et TP attachés. Renvoie le dealReference.

        Un dealReference n'est PAS une preuve d'exécution : appeler `confirm()`.
        """
        self._check_epic(epic)
        if direction not in ("BUY", "SELL"):
            raise CapitalAPIError(f"Direction invalide: {direction}")
        if stop_level is None or profit_level is None:
            raise CapitalAPIError("SL et TP obligatoires")
        body = {"epic": epic, "direction": direction, "size": size, "guaranteedStop": False,
                "stopLevel": stop_level, "profitLevel": profit_level}
        data = self._request("POST", "/api/v1/positions", json=body, is_order=True)
        ref = data.get("dealReference")
        if not ref:
            raise CapitalAPIError("Pas de dealReference dans la réponse d'ordre")
        return str(ref)

    def confirm(self, deal_reference: str, attempts: int = 5, delay_s: float = 0.5) -> DealConfirmation:
        last_exc: CapitalAPIError | None = None
        for _ in range(attempts):
            try:
                d = self._request("GET", f"/api/v1/confirms/{deal_reference}")
                return DealConfirmation(
                    deal_reference=deal_reference, deal_status=str(d.get("dealStatus")),
                    status=d.get("status"), deal_id=d.get("dealId"),
                    level=float(d["level"]) if d.get("level") is not None else None,
                    size=float(d["size"]) if d.get("size") is not None else None,
                    direction=d.get("direction"), epic=d.get("epic"), reason=d.get("reason"),
                    affected_deals=list(d.get("affectedDeals") or []),
                )
            except CapitalAPIError as exc:
                last_exc = exc
                if exc.status != 404:
                    raise
                self._sleep(delay_s)
        raise last_exc or CapitalAPIError("Confirmation introuvable")

    def close_position(self, deal_id: str) -> str:
        data = self._request("DELETE", f"/api/v1/positions/{deal_id}", is_order=True)
        return str(data.get("dealReference", ""))

    def update_position(self, deal_id: str, stop_level: float | None = None,
                        profit_level: float | None = None) -> str:
        body: dict[str, Any] = {}
        if stop_level is not None:
            body["stopLevel"] = stop_level
        if profit_level is not None:
            body["profitLevel"] = profit_level
        data = self._request("PUT", f"/api/v1/positions/{deal_id}", json=body, is_order=True)
        return str(data.get("dealReference", ""))


def build_client(settings: Any) -> CapitalClient:
    return CapitalClient(
        settings.base_url, settings.capital_api_key, settings.capital_identifier, settings.capital_password,
        allowed_epics=settings.allowed_epics, timeout_s=settings.http_timeout_s,
    )
