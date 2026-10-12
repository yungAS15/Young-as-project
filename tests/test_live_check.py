"""`main.py live-check` avec un broker simulé : lecture seule, aucun secret affiché."""
from datetime import datetime, timedelta, timezone

import pytest

import main
from broker.models import AccountInfo, Quote
from tests.helpers import gold_spec
from tests.scenario import m15_series


class FakeLive:
    def __init__(self, read_only):
        self.read_only = read_only
        self.calls = []

    def server_time(self):
        return datetime.now(timezone.utc)

    def login(self):
        return {"account_currency": "CHF", "account_id": "123456789"}

    def get_current_account(self):
        return AccountInfo("123456789", "CHF", 850.0, 800.0, 0.0, 850.0)

    def get_instrument(self, epic):
        return gold_spec()

    def get_quote(self, epic, allow_any_epic=False):
        now = datetime.now(timezone.utc)
        return Quote(epic, 0.89, 0.91, now, "TRADEABLE") if epic == "USDCHF" else Quote(epic, 4000.0, 4000.3, now, "TRADEABLE")

    def get_positions(self):
        return []

    def get_preferences(self):
        return {"hedgingMode": False}

    def get_candles(self, epic, tf, max_bars=100):
        return [c for c in m15_series()][-max_bars:]

    def logout(self):
        self.calls.append("logout")


def test_live_check_reads_only_and_hides_secrets(monkeypatch, capsys):
    for k, v in {"CAPITAL_ENV": "live", "CAPITAL_API_KEY": "SECRETKEY123", "CAPITAL_IDENTIFIER": "me@mail.ch",
                 "CAPITAL_PASSWORD": "SECRETPWD456", "READ_ONLY": "false", "DRY_RUN": "true"}.items():
        monkeypatch.setenv(k, v)
    built = {}

    def fake_build(s):
        built["read_only"] = s.read_only
        return FakeLive(s.read_only)

    monkeypatch.setattr("broker.capital_api.build_client", fake_build)
    monkeypatch.setattr("main._logging", lambda s: None)
    main.main(["live-check"])
    out = capsys.readouterr().out
    assert built["read_only"] is True                       # forcé même si READ_ONLY=false
    for secret in ("SECRETKEY123", "SECRETPWD456", "me@mail.ch", "123456789"):
        assert secret not in out
    assert "***6789" in out and "Disponible 800.00" in out
    assert "min(100 CHF, 1 % × 800.00) = 8.00 CHF" in out
    assert "Aucun ordre envoyé" in out
