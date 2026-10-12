"""Bout-en-bout du runner live avec un broker simulé (aucun réseau) :
données → setup → confirmation M1 → proposition → APPROVE strict → exécution DRY_RUN."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from approval.trade_approval import PENDING
from broker.models import AccountInfo, Quote
from config import StrategyParams
from data.database import Database
from interface.runner import BotRunner
from monitoring.alerts import Notifier
from tests.helpers import gold_spec, unlocked_settings
from tests.scenario import h4_bullish, m1_retrace_and_break, m15_series


class Recorder:
    def __init__(self):
        self.events = []

    def send(self, event, text):
        self.events.append((event.value, text))


class FakeCapital:
    def __init__(self):
        h4, m15 = h4_bullish(), m15_series()
        m1 = m1_retrace_and_break(m15)
        now = datetime.now(timezone.utc)
        shift = (now - timedelta(seconds=20)) - (m1[-1].time + timedelta(minutes=1))
        self.h4 = [replace(c, time=c.time + shift) for c in h4]
        self.m15 = [replace(c, time=c.time + shift) for c in m15]
        self.m1 = [replace(c, time=c.time + shift) for c in m1]
        self.connected = True
        self.consecutive_errors = 0

    def get_instrument(self, epic):
        return gold_spec(bid=98.8, offer=99.1)

    def get_quote(self, epic, allow_any_epic=False):
        if epic == "USDCHF":
            return Quote("USDCHF", 0.899, 0.901, datetime.now(timezone.utc), "TRADEABLE")
        return Quote("GOLD", 98.8, 99.1, datetime.now(timezone.utc) - timedelta(seconds=1), "TRADEABLE")

    def get_current_account(self):
        return AccountInfo("A1", "CHF", 5000.0, 5000.0, 0.0, 5000.0)

    def get_positions(self):
        return []

    def get_candles(self, epic, tf, max_bars=1000, start=None, end=None):
        return {"H4": self.h4, "M15": self.m15, "M1": self.m1}[tf][-max_bars:]

    def get_candles_range(self, epic, tf, start, end):
        return list(self.m15)


def test_runner_proposes_then_executes_after_strict_approve():
    s = unlocked_settings(strategy=StrategyParams(max_tp_atr_h4=50.0))
    db = Database(":memory:")
    rec = Recorder()
    runner = BotRunner(s, db, FakeCapital(), Notifier([rec]))
    runner.step()

    kinds = [k for k, _ in rec.events]
    assert "SETUP_DETECTED" in kinds and "PROPOSAL" in kinds, rec.events
    props = db.proposals()
    assert len(props) == 1 and props[0]["status"] == PENDING
    pid = props[0]["id"]
    text = dict(rec.events)["PROPOSAL"]
    assert f"APPROVE {pid}" in text and "SCORE" in text and "Conditions non remplies" in text

    # Rien n'est exécuté sans approbation explicite
    runner.last_m1 = None
    runner.step()
    assert db.open_trades() == []

    # Formulation ambiguë ignorée
    assert not runner.approvals.handle("ok vas-y", 99.1).ok
    # Approbation stricte → exécution DRY_RUN au prochain cycle
    assert runner.approvals.handle(f"APPROVE {pid}", 99.1).ok
    runner.step()
    trades = db.open_trades()
    assert len(trades) == 1 and trades[0]["mode"] == "DRY_RUN"
    assert db.get_proposal(pid)["status"] == "EXECUTED"
    # Statut écrit pour l'interface
    assert db.get("status") is not None


def test_runner_blocks_proposal_when_kill_switch_on():
    s = unlocked_settings(kill_switch=True, strategy=StrategyParams(max_tp_atr_h4=50.0))
    db = Database(":memory:")
    runner = BotRunner(s, db, FakeCapital(), Notifier([Recorder()]))
    runner.step()
    props = db.proposals()
    assert len(props) == 1 and props[0]["status"] == "BLOCKED"
    assert not runner.approvals.handle(f"APPROVE {props[0]['id']}", 99.1).ok
