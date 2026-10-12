import json
import logging
import os
from datetime import datetime, timedelta, timezone

import pytest

from config import ConfigError, Mode, Settings, load_settings
from data.database import Database
from monitoring.logger import SecretFilter
from monitoring.macro_calendar import MacroEvent, blackout, load_events
from tests.scenario import h4_bullish, m15_series

SAFE_ENV_KEYS = ["DRY_RUN", "KILL_SWITCH", "MANUAL_APPROVAL_REQUIRED", "AUTO_TRADING", "MODE", "CAPITAL_ENV",
                 "MAX_TRADES_PER_DAY", "MIN_RR", "MAX_RISK_PER_TRADE_PCT"]


@pytest.fixture
def clean_env(monkeypatch):
    for k in SAFE_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def test_default_state_is_safe(clean_env):
    s = load_settings(load_dotenv_file=False)
    assert s.dry_run and s.kill_switch and s.manual_approval_required and not s.auto_trading
    assert s.mode == Mode.MANUAL_APPROVAL and s.capital_env == "demo"
    allowed, reasons = s.execution_allowed()
    assert not allowed and any("KILL_SWITCH" in r for r in reasons)


def test_env_example_matches_safe_defaults():
    txt = open(os.path.join(os.path.dirname(__file__), "..", ".env.example")).read()
    for line in ("DRY_RUN=true", "KILL_SWITCH=true", "MANUAL_APPROVAL_REQUIRED=true", "AUTO_TRADING=false",
                 "CAPITAL_ENV=demo", "MAX_TRADES_PER_DAY=2", "MAX_RISK_PER_TRADE_CHF=100"):
        assert line in txt
    for secret in ("CAPITAL_API_KEY=", "CAPITAL_PASSWORD=", "CAPITAL_IDENTIFIER="):
        assert f"{secret}\n" in txt   # vide


def test_gitignore_excludes_env():
    txt = open(os.path.join(os.path.dirname(__file__), "..", ".gitignore")).read()
    assert ".env" in txt.splitlines()


@pytest.mark.parametrize("env,expected", [
    ({"DRY_RUN": "peut-etre"}, "booléenne"),
    ({"MODE": "YOLO"}, "MODE"),
    ({"MAX_TRADES_PER_DAY": "5"}, "MAX_TRADES_PER_DAY"),
    ({"MIN_RR": "1.5"}, "MIN_RR"),
    ({"MODE": "AUTO"}, "AUTO"),                         # AUTO exige MANUAL_APPROVAL_REQUIRED=false
    ({"MANUAL_APPROVAL_REQUIRED": "false"}, "AUTO"),    # et inversement
    ({"MAX_RISK_PER_TRADE_PCT": "10"}, "5 %"),
    ({"CAPITAL_ENV": "prod"}, "CAPITAL_ENV"),
])
def test_invalid_or_ambiguous_config_refused(clean_env, env, expected):
    for k, v in env.items():
        clean_env.setenv(k, v)
    with pytest.raises(ConfigError, match=expected):
        load_settings(load_dotenv_file=False)


def test_auto_mode_requires_explicit_flags(clean_env):
    clean_env.setenv("MODE", "AUTO")
    clean_env.setenv("MANUAL_APPROVAL_REQUIRED", "false")
    s = load_settings(load_dotenv_file=False)
    allowed, reasons = s.execution_allowed()
    assert not allowed and any("AUTO_TRADING=false" in r for r in reasons)


def test_secrets_never_in_repr_or_public_dict():
    s = Settings(capital_api_key="KEY123456", capital_password="PWD123456", capital_identifier="me@x.ch")
    r = repr(s)
    assert "KEY123456" not in r and "PWD123456" not in r and "me@x.ch" not in r
    pub = json.dumps(s.public_dict(), default=str)
    assert "KEY123456" not in pub and "PWD123456" not in pub


def test_log_filter_redacts_secrets(caplog):
    flt = SecretFilter(["SUPERSECRETKEY"])
    rec = logging.LogRecord("x", logging.INFO, "", 0, "key=%s and CST: abcdef password=hunter2", ("SUPERSECRETKEY",), None)
    flt.filter(rec)
    msg = rec.getMessage()
    assert "SUPERSECRETKEY" not in msg and "abcdef" not in msg and "hunter2" not in msg


def test_macro_calendar(tmp_path):
    now = datetime(2026, 11, 6, 13, 10, tzinfo=timezone.utc)
    f = tmp_path / "m.csv"
    f.write_text("datetime_utc,event,impact\n2026-11-06T13:30:00,NFP,high\n2026-12-10T19:00:00,FOMC,high\n")
    evs = load_events(f)
    assert blackout(now, evs, 30) is True
    assert blackout(now - timedelta(hours=2), evs, 30) is False
    assert blackout(now, None, 30) is None                     # absent → inconnu → NO TRADE
    assert load_events(tmp_path / "absent.csv") is None
    old = [MacroEvent(now - timedelta(days=10), "CPI", "high")]
    assert blackout(now, old, 30) is None                      # calendrier périmé


def test_daily_counter_persists_across_restarts(tmp_path):
    path = tmp_path / "db.sqlite"
    now = datetime.now(timezone.utc)
    db = Database(path)
    tid = db.open_trade(proposal_id=None, setup_id="s", deal_id="d", deal_reference=None, mode="DRY_RUN",
                        direction="BUY", epic="GOLD", opened_at=now, entry=10, size=1, stop_loss=9, take_profit=12,
                        risk_ccy=1)
    db.close_trade(tid, closed_at=now, exit_price=9, pnl_ccy=-1.0, exit_reason="SL")
    db.close()
    db2 = Database(path)
    d = db2.daily()
    assert d["trades_count"] == 1 and d["realized_pnl"] == -1.0
    assert db2.trades()[0]["result"] == "LOSS"


def test_setup_used_only_once():
    db = Database(":memory:")
    assert db.mark_setup_used("A")
    assert not db.mark_setup_used("A")


def test_daily_report_content():
    from monitoring.daily_report import build_daily_report
    from strategy.vwap import compute_vwaps
    m15 = m15_series()
    txt = build_daily_report(Settings(), datetime.now(timezone.utc), h4_bullish(), m15, compute_vwaps(m15), None, 0, 0.0)
    for needle in ("Biais H4", "Liquidité sell-side", "VWAP", "Macro", "Trades du jour"):
        assert needle in txt


def test_web_render_and_price_freshness():
    from interface.web import current_price_from_status, render
    db = Database(":memory:")
    s = Settings()
    assert current_price_from_status(db) is None
    db.set("status", json.dumps({"offer": 4000.4, "quote_time": datetime.now(timezone.utc).isoformat(),
                                 "vwaps": {"session": 3999.0, "weekly": None}, "open_trades": []}))
    assert current_price_from_status(db) == 4000.4
    db.set("status", json.dumps({"offer": 4000.4, "quote_time": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()}))
    assert current_price_from_status(db) is None   # prix périmé → pas d'approbation possible
    html = render(s, db, "tok")
    assert "APPROVE" in html and "tok" in html


def test_web_refuses_remote_bind():
    from interface.web import serve
    with pytest.raises(SystemExit):
        serve(Settings(), Database(":memory:"), host="0.0.0.0")


def test_validation_status_fail_closed_and_roundtrip(tmp_path):
    from backtest.validation import write_validation_status
    from config import read_validation_status
    assert read_validation_status(tmp_path / "absent.json")["passed"] is False
    bad = tmp_path / "bad.json"
    bad.write_text("{oops")
    assert read_validation_status(bad)["passed"] is False
    rep = {"label": "OOS", "trades": 150, "winrate": 0.5, "expectancy_r": 0.2, "max_drawdown_pct": 10,
           "validation": {"passed": True, "criteria": {"x": True}}}
    write_validation_status(rep, source="t.csv", params={}, path=tmp_path / "v.json")
    assert read_validation_status(tmp_path / "v.json")["passed"] is True


def test_live_run_requires_interactive_confirmation(monkeypatch):
    import main
    s = Settings(capital_env="live", dry_run=False, kill_switch=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    with pytest.raises(SystemExit):
        main.confirm_live_interactively(s)
    main.confirm_live_interactively(Settings())   # DRY_RUN : aucune question
