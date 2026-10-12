from dataclasses import replace
from datetime import timedelta

from config import LIVE_ACK_PHRASE, Mode, Settings
from risk.risk_manager import RiskManager
from tests.helpers import NOW, ctx, gold_spec, unlocked_settings


def failed_names(decision):
    return {c.name for c in decision.failed}


def test_all_good_is_approved():
    d = RiskManager(unlocked_settings()).evaluate(ctx(), "proposal")
    assert d.approved, d.summary()
    assert d.size > 0
    assert d.rr == 2.0


def test_default_settings_kill_switch_blocks():
    d = RiskManager(Settings()).evaluate(ctx(), "proposal")
    assert not d.approved
    assert "kill_switch" in failed_names(d)


def test_analysis_only_blocks():
    d = RiskManager(unlocked_settings(mode=Mode.ANALYSIS_ONLY)).evaluate(ctx(), "proposal")
    assert "mode" in failed_names(d)


def test_instrument_allowlist():
    d = RiskManager(unlocked_settings()).evaluate(ctx(epic="SILVER"), "proposal")
    assert "allowlist" in failed_names(d)


def test_sell_not_allowed_by_default():
    d = RiskManager(unlocked_settings()).evaluate(
        ctx(direction="SELL", entry=2000.0, stop_loss=2005.0, take_profit=1990.0), "proposal")
    assert "direction" in failed_names(d)


def test_rr_below_two_refused():
    d = RiskManager(unlocked_settings()).evaluate(ctx(take_profit=2009.0), "proposal")
    assert "rr_min" in failed_names(d)


def test_sl_wrong_side_and_tp_wrong_side():
    d = RiskManager(unlocked_settings()).evaluate(ctx(stop_loss=2001.0), "proposal")
    assert "sl_cote" in failed_names(d)
    d = RiskManager(unlocked_settings()).evaluate(ctx(take_profit=1999.0), "proposal")
    assert "tp_cote" in failed_names(d)


def test_sl_below_broker_min_distance():
    d = RiskManager(unlocked_settings()).evaluate(
        ctx(instrument=gold_spec(min_stop_distance=10.0)), "proposal")
    assert "sl_distance_min" in failed_names(d)


def test_two_trades_per_day_limit():
    rm = RiskManager(unlocked_settings())
    assert rm.evaluate(ctx(trades_today=1), "proposal").approved
    assert "trades_jour" in failed_names(rm.evaluate(ctx(trades_today=2), "proposal"))
    assert "trades_jour" in failed_names(rm.evaluate(ctx(trades_today=None), "proposal"))


def test_one_open_position_max():
    d = RiskManager(unlocked_settings()).evaluate(ctx(open_positions=1), "proposal")
    assert "position_ouverte" in failed_names(d)
    d = RiskManager(unlocked_settings()).evaluate(ctx(open_positions=None), "proposal")
    assert "position_ouverte" in failed_names(d)


def test_daily_loss_limit_blocks():
    # capital 5000 → limite = min(150, 2 % = 100) = 100
    rm = RiskManager(unlocked_settings())
    assert "perte_journaliere" in failed_names(rm.evaluate(ctx(realized_pnl_today=-100.0), "proposal"))
    d = rm.evaluate(ctx(realized_pnl_today=-60.0), "proposal")
    # budget restant 40 → la taille est réduite pour ne pas dépasser 40
    assert d.approved and d.sizing.risk_effective <= 40.0 + 1e-9


def test_spread_too_wide():
    d = RiskManager(unlocked_settings()).evaluate(ctx(offer=2001.5), "proposal")
    assert "spread" in failed_names(d)


def test_stale_data_and_quote():
    rm = RiskManager(unlocked_settings())
    assert "donnees_fraiches" in failed_names(rm.evaluate(ctx(last_candle_close=NOW - timedelta(minutes=10)), "proposal"))
    assert "prix_frais" in failed_names(rm.evaluate(ctx(quote_time=NOW - timedelta(minutes=5)), "proposal"))
    assert "donnees_fraiches" in failed_names(rm.evaluate(ctx(last_candle_close=None), "proposal"))


def test_market_closed_and_connection_lost():
    rm = RiskManager(unlocked_settings())
    assert "marche_ouvert" in failed_names(rm.evaluate(ctx(market_status="CLOSED"), "proposal"))
    assert "connexion" in failed_names(rm.evaluate(ctx(connected=False), "proposal"))
    assert "erreurs_api" in failed_names(rm.evaluate(ctx(consecutive_api_errors=3), "proposal"))


def test_ambiguous_instrument_refused():
    d = RiskManager(unlocked_settings()).evaluate(ctx(instrument=gold_spec(scaling_factor=100)), "proposal")
    assert "instrument" in failed_names(d)
    d = RiskManager(unlocked_settings()).evaluate(ctx(instrument=None), "proposal")
    assert "instrument" in failed_names(d)


def test_min_size_too_big_for_small_capital():
    d = RiskManager(unlocked_settings()).evaluate(
        ctx(capital_available=60.0, instrument=gold_spec(min_deal_size=1.0, size_increment=1.0)), "proposal")
    assert "taille" in failed_names(d)


def test_score_min():
    d = RiskManager(unlocked_settings()).evaluate(ctx(score=6.5), "proposal")
    assert "score_min" in failed_names(d)


def test_macro_pause_fail_closed():
    rm = RiskManager(unlocked_settings(macro_pause_enabled=True))
    assert "macro" in failed_names(rm.evaluate(ctx(macro_blackout=None), "proposal"))
    assert "macro" in failed_names(rm.evaluate(ctx(macro_blackout=True), "proposal"))
    assert rm.evaluate(ctx(macro_blackout=False), "proposal").approved


def test_live_requires_ack_phrase():
    s = unlocked_settings(capital_env="live", dry_run=False, read_only=False)
    d = RiskManager(s).evaluate(ctx(), "execution")
    assert "execution_guard" in failed_names(d)
    s2 = replace(s, live_trading_ack=LIVE_ACK_PHRASE, validation_passed=True)
    assert RiskManager(s2).evaluate(ctx(), "execution").approved


def test_live_unvalidated_strategy_requires_second_phrase():
    from config import LIVE_UNVALIDATED_PHRASE
    s = unlocked_settings(capital_env="live", dry_run=False, read_only=False, live_trading_ack=LIVE_ACK_PHRASE,
                          validation_passed=False)
    d = RiskManager(s).evaluate(ctx(), "execution")
    assert "execution_guard" in failed_names(d)
    assert any("NON validée" in c.detail for c in d.failed)
    s2 = replace(s, live_override_unvalidated="oui")          # formulation approximative refusée
    assert not RiskManager(s2).evaluate(ctx(), "execution").approved
    s3 = replace(s, live_override_unvalidated=LIVE_UNVALIDATED_PHRASE)
    assert RiskManager(s3).evaluate(ctx(), "execution").approved
    # démo et DRY_RUN ne sont pas concernés
    assert RiskManager(replace(s, capital_env="demo")).evaluate(ctx(), "execution").approved


def test_internal_error_is_no_trade():
    rm = RiskManager(unlocked_settings())
    bad = ctx()
    bad.entry = "oops"  # type: ignore[assignment]
    d = rm.evaluate(bad, "proposal")
    assert not d.approved
    assert "risk_manager" in failed_names(d)
