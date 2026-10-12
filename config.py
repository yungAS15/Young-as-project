"""Configuration centrale du bot XAU/USD.

Toutes les valeurs viennent de variables d'environnement (fichier `.env` chargé
par python-dotenv). Les valeurs par défaut sont volontairement les plus sûres :
DRY_RUN=true, KILL_SWITCH=true, MANUAL_APPROVAL_REQUIRED=true, AUTO_TRADING=false.

Les secrets (clé API, identifiant, mot de passe) ne sont jamais affichés :
`Settings.__repr__` les masque et `Settings.public_dict()` les exclut.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from enum import Enum
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent

# Phrase exacte exigée pour autoriser un ordre réel (CAPITAL_ENV=live et DRY_RUN=false).
LIVE_ACK_PHRASE = "J_ACCEPTE_LE_RISQUE_REEL"
# Phrase exigée EN PLUS pour trader en réel une stratégie dont le backtest n'est pas validé.
LIVE_UNVALIDATED_PHRASE = "J_ACCEPTE_UNE_STRATEGIE_NON_VALIDEE"
VALIDATION_STATUS_PATH = "var/validation_status.json"


def read_validation_status(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Statut de validation écrit par le backtest. Absent/illisible → non validé (fail-closed)."""
    import json
    p = Path(path) if path else PROJECT_ROOT / VALIDATION_STATUS_PATH
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else {"passed": False}
    except (OSError, ValueError):
        return {"passed": False, "reason": "aucun statut de validation (lancer le backtest)"}


class ConfigError(ValueError):
    """Configuration invalide ou ambiguë : le bot refuse de démarrer en trading."""


class Mode(str, Enum):
    ANALYSIS_ONLY = "ANALYSIS_ONLY"
    MANUAL_APPROVAL = "MANUAL_APPROVAL"
    AUTO = "AUTO"


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    low = raw.lower()
    if low in ("1", "true", "yes", "on"):
        return True
    if low in ("0", "false", "no", "off"):
        return False
    # Valeur ambiguë : on refuse plutôt que de deviner.
    raise ConfigError(f"Valeur booléenne invalide pour {name}: {raw!r}")


def _float(name: str, default: float) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"Valeur numérique invalide pour {name}: {raw!r}") from exc


def _int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"Valeur entière invalide pour {name}: {raw!r}") from exc


def _list(name: str, default: str) -> tuple[str, ...]:
    raw = _env(name, default) or ""
    return tuple(x.strip() for x in raw.split(",") if x.strip())


@dataclass(frozen=True)
class ScoreWeights:
    """Poids du score sur 10 (configurables). Voir docs/STRATEGY.md §Scoring."""

    h4_context: float = 2.0
    liquidity_zone: float = 1.0
    sweep: float = 2.0
    m15_structure: float = 1.0
    fvg_or_ob: float = 1.0
    vwap: float = 1.0
    ote_fib: float = 1.0
    m1_confirmation: float = 1.0

    @property
    def total(self) -> float:
        return sum(getattr(self, f.name) for f in fields(self))


@dataclass(frozen=True)
class StrategyParams:
    """Paramètres de détection. Identiques en live et en backtest."""

    swing_k_h4: int = 2            # fractale : k bougies de chaque côté
    swing_k_m15: int = 2
    swing_k_m1: int = 2
    atr_period: int = 14
    equal_lows_tolerance_atr: float = 0.10   # deux creux "égaux" si écart <= 0.10 ATR
    range_lookback_m15: int = 48             # bas de range = plus bas des 48 dernières M15 (12 h)
    liquidity_lookback_m15: int = 192        # creux M15 considérés : 2 derniers jours
    sweep_reclaim_bars: int = 2              # la réintégration doit arriver en <= 2 bougies M15
    sweep_min_penetration_atr: float = 0.0   # pénétration minimale sous le niveau
    choch_max_bars_after_sweep: int = 12     # CHOCH doit arriver <= 12 M15 (3 h) après le sweep
    fvg_min_size_atr: float = 0.10
    ote_low: float = 0.618
    ote_high: float = 0.786
    vwap_tolerance_atr: float = 0.25
    vwap_min_volume_coverage: float = 0.90   # >= 90 % des bougies avec volume > 0
    setup_expiry_m15_bars: int = 16          # un setup armé expire après 4 h sans entrée
    m1_break_lookback: int = 5               # micro-CHOCH : clôture > plus haut des 5 M1 précédentes
    m1_touch_max_age: int = 15               # la zone doit avoir été touchée dans les 15 dernières M1
    sl_atr_mult: float = 0.25                # marge SL sous le sweep = 0.25 ATR(M15) + spread
    min_rr: float = 2.0
    target_rr: float = 3.0
    tp_mode: str = "nearest_valid"           # "nearest_valid" | "first_liquidity" | "prefer_target"
    max_tp_atr_h4: float = 3.0               # TP "raisonnable" : <= 3 ATR(H4) de l'entrée
    h4_ema_period: int = 50
    session_reset_hour_utc: int = 22         # VWAP session : reset au début du jour de trading
    min_score: float = 7.0
    # --- Hypothèses d'ajustement (docs/BACKTEST_RESULTS.md) — défauts = règles v1 ---
    confirm_tf: str = "M1"                    # H1 : "M1" (micro-CHOCH) | "M15" (clôture M15 de rejet)
    require_significant_sweep: bool = False   # H2 : sweep d'un niveau H4 / equal lows / range low obligatoire
    h4_strict_bullish: bool = False           # H3 : biais H4 BULLISH uniquement (pas neutre)
    require_d1_bullish: bool = False          # H3 : D1 au-dessus d'une EMA20 montante
    entry_mode: str = "market"                # H4 : "market" | "limit"
    limit_retracement: float = 0.705          # H4 : niveau de l'ordre limite dans l'impulsion


@dataclass(frozen=True)
class Settings:
    # --- Sécurité / modes ---
    dry_run: bool = True
    read_only: bool = True     # client API en lecture seule : aucun POST/PUT/DELETE (hors session)
    kill_switch: bool = True
    manual_approval_required: bool = True
    auto_trading: bool = False
    mode: Mode = Mode.MANUAL_APPROVAL
    live_trading_ack: str = ""
    live_override_unvalidated: str = ""
    validation_passed: bool = False   # lu depuis var/validation_status.json

    # --- Broker ---
    capital_env: str = "demo"   # demo | live
    capital_api_key: str = field(default="", repr=False)
    capital_identifier: str = field(default="", repr=False)
    capital_password: str = field(default="", repr=False)
    http_timeout_s: float = 10.0
    max_consecutive_api_errors: int = 3

    # --- Instrument ---
    allowed_epics: tuple[str, ...] = ("GOLD",)
    instrument_search_term: str = "gold"
    allowed_directions: tuple[str, ...] = ("BUY",)
    account_currency: str = "CHF"
    fx_epic_quote_to_account: str = "USDCHF"

    # --- Risque ---
    max_risk_per_trade_chf: float = 100.0
    max_risk_per_trade_pct: float = 1.0
    max_daily_loss_chf: float = 150.0
    max_daily_loss_pct: float = 2.0
    max_trades_per_day: int = 2
    max_open_positions: int = 1
    max_spread_points: float = 0.60          # spread XAU/USD max accepté (USD)
    max_data_age_s: float = 180.0            # dernière M1 close il y a < 3 min
    max_quote_age_s: float = 30.0
    sl_slippage_buffer_points: float = 0.10  # ajouté à la distance SL pour la taille
    trading_timezone: str = "Europe/Zurich"  # pour le compteur journalier
    macro_pause_enabled: bool = False
    macro_pause_minutes: int = 30
    macro_calendar_path: str = "var/macro_events.csv"

    # --- Approbation ---
    approval_ttl_s: int = 180
    approval_max_price_drift_points: float = 1.50
    approval_max_risk_increase_pct: float = 10.0

    # --- Gestion (désactivée par défaut, à backtester avant activation) ---
    breakeven_at_1r: bool = False
    partial_close_at_1r: bool = False

    # --- Backtest ---
    backtest_slippage_points: float = 0.10
    backtest_commission_chf: float = 0.0
    backtest_default_spread: float = 0.35
    backtest_quote_to_account_rate: float = 0.80   # USD->CHF fixe pour le backtest (à ajuster)
    backtest_initial_capital_chf: float = 1000.0
    validation_min_trades: int = 100
    validation_winrate_min: float = 0.45
    validation_winrate_max: float = 0.65
    validation_max_drawdown_pct: float = 20.0
    suspicious_winrate: float = 0.75

    # --- Alertes / interface ---
    telegram_bot_token: str = field(default="", repr=False)
    telegram_chat_id: str = field(default="", repr=False)
    daily_report_time: str = "07:30"    # heure locale (trading_timezone), avant Londres
    web_host: str = "127.0.0.1"
    web_port: int = 8765

    # --- Fichiers ---
    db_path: str = "var/trading_bot.sqlite"
    log_path: str = "var/trading_bot.log"

    strategy: StrategyParams = field(default_factory=StrategyParams)
    weights: ScoreWeights = field(default_factory=ScoreWeights)

    # ------------------------------------------------------------------
    @property
    def base_url(self) -> str:
        if self.capital_env == "live":
            return "https://api-capital.backend-capital.com"
        return "https://demo-api-capital.backend-capital.com"

    @property
    def has_credentials(self) -> bool:
        return bool(self.capital_api_key and self.capital_identifier and self.capital_password)

    @property
    def secrets(self) -> tuple[str, ...]:
        return tuple(
            s for s in (self.capital_api_key, self.capital_identifier, self.capital_password,
                        self.telegram_bot_token) if s
        )

    def execution_allowed(self) -> tuple[bool, list[str]]:
        """Indique si un ordre réel (non simulé) peut techniquement partir.

        Retourne (autorisé, raisons_du_blocage). Le Risk Manager l'appelle aussi.
        """
        reasons: list[str] = []
        if self.mode == Mode.ANALYSIS_ONLY:
            reasons.append("Mode ANALYSIS_ONLY : aucune exécution possible")
        if self.kill_switch:
            reasons.append("KILL_SWITCH actif")
        if self.mode == Mode.AUTO and not self.auto_trading:
            reasons.append("Mode AUTO demandé mais AUTO_TRADING=false")
        if not self.dry_run and self.read_only:
            reasons.append("READ_ONLY=true : client API en lecture seule, aucun ordre possible")
        if self.capital_env == "live" and not self.dry_run and self.live_trading_ack != LIVE_ACK_PHRASE:
            reasons.append("Compte réel : LIVE_TRADING_ACK absent ou incorrect")
        if (self.capital_env == "live" and not self.dry_run and not self.validation_passed
                and self.live_override_unvalidated != LIVE_UNVALIDATED_PHRASE):
            reasons.append("Compte réel : stratégie NON validée par le backtest "
                           "(LIVE_OVERRIDE_UNVALIDATED absent ou incorrect)")
        return (not reasons, reasons)

    def public_dict(self) -> dict[str, Any]:
        hidden = {"capital_api_key", "capital_identifier", "capital_password",
                  "telegram_bot_token", "telegram_chat_id", "live_trading_ack", "live_override_unvalidated"}
        out: dict[str, Any] = {}
        for f in fields(self):
            if f.name in hidden:
                out[f.name] = "***" if getattr(self, f.name) else ""
            else:
                v = getattr(self, f.name)
                out[f.name] = v.value if isinstance(v, Enum) else v
        return out

    def validate(self) -> None:
        if self.capital_env not in ("demo", "live"):
            raise ConfigError("CAPITAL_ENV doit valoir 'demo' ou 'live'")
        if not self.allowed_epics:
            raise ConfigError("ALLOWED_EPICS vide")
        for d in self.allowed_directions:
            if d not in ("BUY", "SELL"):
                raise ConfigError(f"Direction inconnue: {d}")
        if self.max_risk_per_trade_chf <= 0 or self.max_risk_per_trade_pct <= 0:
            raise ConfigError("Les limites de risque doivent être > 0")
        if self.max_risk_per_trade_pct > 5:
            raise ConfigError("MAX_RISK_PER_TRADE_PCT > 5 % refusé (garde-fou)")
        if self.max_trades_per_day < 0 or self.max_trades_per_day > 2:
            raise ConfigError("MAX_TRADES_PER_DAY doit être entre 0 et 2")
        if self.max_open_positions != 1:
            raise ConfigError("MAX_OPEN_POSITIONS doit valoir 1")
        if self.strategy.min_rr < 2.0:
            raise ConfigError("MIN_RR < 2 refusé")
        if self.capital_env == "live" and (self.mode == Mode.AUTO or self.auto_trading
                                          or not self.manual_approval_required):
            raise ConfigError("Compte réel : validation manuelle obligatoire (MODE=AUTO / AUTO_TRADING refusés)")
        if self.mode == Mode.AUTO and self.manual_approval_required:
            raise ConfigError("Mode AUTO incompatible avec MANUAL_APPROVAL_REQUIRED=true")
        if self.mode != Mode.AUTO and not self.manual_approval_required:
            raise ConfigError("MANUAL_APPROVAL_REQUIRED=false exige MODE=AUTO explicite")
        if not 0 < self.strategy.ote_low < self.strategy.ote_high < 1:
            raise ConfigError("Zone OTE invalide")
        if self.strategy.confirm_tf not in ("M1", "M15"):
            raise ConfigError("CONFIRM_TF doit valoir M1 ou M15")
        if self.strategy.entry_mode not in ("market", "limit"):
            raise ConfigError("ENTRY_MODE doit valoir market ou limit")
        if self.strategy.tp_mode not in ("nearest_valid", "first_liquidity", "prefer_target"):
            raise ConfigError("TP_MODE invalide")
        if abs(self.weights.total - 10.0) > 1e-9:
            raise ConfigError(f"La somme des poids du score doit valoir 10 (actuel {self.weights.total})")


def load_settings(env_file: str | os.PathLike[str] | None = None, *, load_dotenv_file: bool = True) -> Settings:
    """Charge la configuration depuis l'environnement (et `.env` si présent)."""
    if load_dotenv_file:
        try:
            from dotenv import load_dotenv
            load_dotenv(env_file or PROJECT_ROOT / ".env", override=False)
        except ImportError:  # pragma: no cover - dépendance listée dans requirements
            pass

    mode_raw = (_env("MODE", "MANUAL_APPROVAL") or "").upper()
    try:
        mode = Mode(mode_raw)
    except ValueError as exc:
        raise ConfigError(f"MODE invalide: {mode_raw!r}") from exc

    sp = StrategyParams(
        swing_k_h4=_int("SWING_K_H4", 2),
        swing_k_m15=_int("SWING_K_M15", 2),
        swing_k_m1=_int("SWING_K_M1", 2),
        sl_atr_mult=_float("SL_ATR_MULT", 0.25),
        min_rr=_float("MIN_RR", 2.0),
        target_rr=_float("TARGET_RR", 3.0),
        tp_mode=_env("TP_MODE", "nearest_valid") or "nearest_valid",
        max_tp_atr_h4=_float("MAX_TP_ATR_H4", 3.0),
        min_score=_float("MIN_SCORE", 7.0),
        confirm_tf=(_env("CONFIRM_TF", "M1") or "M1").upper(),
        require_significant_sweep=_bool("REQUIRE_SIGNIFICANT_SWEEP", False),
        h4_strict_bullish=_bool("H4_STRICT_BULLISH", False),
        require_d1_bullish=_bool("REQUIRE_D1_BULLISH", False),
        entry_mode=(_env("ENTRY_MODE", "market") or "market").lower(),
        limit_retracement=_float("LIMIT_RETRACEMENT", 0.705),
        setup_expiry_m15_bars=_int("SETUP_EXPIRY_M15_BARS", 16),
        session_reset_hour_utc=_int("SESSION_RESET_HOUR_UTC", 22),
    )
    w = ScoreWeights(
        h4_context=_float("W_H4_CONTEXT", 2.0),
        liquidity_zone=_float("W_LIQUIDITY_ZONE", 1.0),
        sweep=_float("W_SWEEP", 2.0),
        m15_structure=_float("W_M15_STRUCTURE", 1.0),
        fvg_or_ob=_float("W_FVG_OR_OB", 1.0),
        vwap=_float("W_VWAP", 1.0),
        ote_fib=_float("W_OTE_FIB", 1.0),
        m1_confirmation=_float("W_M1_CONFIRMATION", 1.0),
    )
    s = Settings(
        dry_run=_bool("DRY_RUN", True),
        read_only=_bool("READ_ONLY", True),
        kill_switch=_bool("KILL_SWITCH", True),
        manual_approval_required=_bool("MANUAL_APPROVAL_REQUIRED", True),
        auto_trading=_bool("AUTO_TRADING", False),
        mode=mode,
        live_trading_ack=_env("LIVE_TRADING_ACK", "") or "",
        live_override_unvalidated=_env("LIVE_OVERRIDE_UNVALIDATED", "") or "",
        validation_passed=bool(read_validation_status().get("passed", False)),
        capital_env=(_env("CAPITAL_ENV", "demo") or "demo").lower(),
        capital_api_key=_env("CAPITAL_API_KEY", "") or "",
        capital_identifier=_env("CAPITAL_IDENTIFIER", "") or "",
        capital_password=_env("CAPITAL_PASSWORD", "") or "",
        http_timeout_s=_float("HTTP_TIMEOUT_S", 10.0),
        max_consecutive_api_errors=_int("MAX_CONSECUTIVE_API_ERRORS", 3),
        allowed_epics=_list("ALLOWED_EPICS", "GOLD"),
        instrument_search_term=_env("INSTRUMENT_SEARCH_TERM", "gold") or "gold",
        allowed_directions=_list("ALLOWED_DIRECTIONS", "BUY"),
        account_currency=(_env("ACCOUNT_CURRENCY", "CHF") or "CHF").upper(),
        fx_epic_quote_to_account=_env("FX_EPIC_QUOTE_TO_ACCOUNT", "USDCHF") or "USDCHF",
        max_risk_per_trade_chf=_float("MAX_RISK_PER_TRADE_CHF", 100.0),
        max_risk_per_trade_pct=_float("MAX_RISK_PER_TRADE_PCT", 1.0),
        max_daily_loss_chf=_float("MAX_DAILY_LOSS_CHF", 150.0),
        max_daily_loss_pct=_float("MAX_DAILY_LOSS_PCT", 2.0),
        max_trades_per_day=_int("MAX_TRADES_PER_DAY", 2),
        max_open_positions=_int("MAX_OPEN_POSITIONS", 1),
        max_spread_points=_float("MAX_SPREAD_POINTS", 0.60),
        max_data_age_s=_float("MAX_DATA_AGE_S", 180.0),
        max_quote_age_s=_float("MAX_QUOTE_AGE_S", 30.0),
        sl_slippage_buffer_points=_float("SL_SLIPPAGE_BUFFER_POINTS", 0.10),
        trading_timezone=_env("TRADING_TIMEZONE", "Europe/Zurich") or "Europe/Zurich",
        macro_pause_enabled=_bool("MACRO_PAUSE_ENABLED", False),
        macro_pause_minutes=_int("MACRO_PAUSE_MINUTES", 30),
        macro_calendar_path=_env("MACRO_CALENDAR_PATH", "var/macro_events.csv") or "var/macro_events.csv",
        approval_ttl_s=_int("APPROVAL_TTL_S", 180),
        approval_max_price_drift_points=_float("APPROVAL_MAX_PRICE_DRIFT_POINTS", 1.50),
        approval_max_risk_increase_pct=_float("APPROVAL_MAX_RISK_INCREASE_PCT", 10.0),
        breakeven_at_1r=_bool("BREAKEVEN_AT_1R", False),
        partial_close_at_1r=_bool("PARTIAL_CLOSE_AT_1R", False),
        backtest_slippage_points=_float("BACKTEST_SLIPPAGE_POINTS", 0.10),
        backtest_commission_chf=_float("BACKTEST_COMMISSION_CHF", 0.0),
        backtest_default_spread=_float("BACKTEST_DEFAULT_SPREAD", 0.35),
        backtest_quote_to_account_rate=_float("BACKTEST_QUOTE_TO_ACCOUNT_RATE", 0.80),
        backtest_initial_capital_chf=_float("BACKTEST_INITIAL_CAPITAL_CHF", 1000.0),
        validation_min_trades=_int("VALIDATION_MIN_TRADES", 100),
        validation_max_drawdown_pct=_float("VALIDATION_MAX_DRAWDOWN_PCT", 20.0),
        telegram_bot_token=_env("TELEGRAM_BOT_TOKEN", "") or "",
        telegram_chat_id=_env("TELEGRAM_CHAT_ID", "") or "",
        daily_report_time=_env("DAILY_REPORT_TIME", "07:30") or "07:30",
        web_host=_env("WEB_HOST", "127.0.0.1") or "127.0.0.1",
        web_port=_int("WEB_PORT", 8765),
        db_path=_env("DB_PATH", "var/trading_bot.sqlite") or "var/trading_bot.sqlite",
        log_path=_env("LOG_PATH", "var/trading_bot.log") or "var/trading_bot.log",
        strategy=sp,
        weights=w,
    )
    s.validate()
    return s


def resolve_path(p: str) -> Path:
    path = Path(p)
    return path if path.is_absolute() else PROJECT_ROOT / path
