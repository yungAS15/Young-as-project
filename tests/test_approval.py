from datetime import timedelta

import pytest

from approval.trade_approval import (APPROVED, BLOCKED, CANCELLED, EXECUTING, EXPIRED, PENDING, REJECTED,
                                     ApprovalManager, fingerprint, format_proposal, parse_command)
from data.database import Database
from tests.helpers import NOW, unlocked_settings


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def payload(**kw):
    p = {"epic": "GOLD", "instrument": "XAU/USD", "direction": "BUY", "bid": 2000.0, "offer": 2000.3,
         "entry": 2000.3, "stop_loss": 1995.0, "take_profit": 2011.0, "rr": 2.02, "size": 0.5,
         "capital": 5000.0, "account_currency": "CHF", "risk_ccy": 40.0, "risk_pct": 0.8, "trades_today": 0,
         "max_trades": 2, "score": 8.0, "score_label": "SCORE 8/10", "analysis": {"H4": "x", "M15": "y", "M1": "z"},
         "flags": {"sweep": True, "fvg": True, "ob": False, "ote": True, "vwap": False},
         "vwaps": {"session": 1999.0, "weekly": None, "monthly": 1990.0}, "trade_reason": "r", "sl_reason": "s",
         "tp_reason": "t", "conditions_met": ["a"], "conditions_unmet": ["vwap: non"], "setup_id": "BUY-1",
         "mode": "DRY_RUN"}
    p.update(kw)
    return p


@pytest.fixture
def env():
    clock = Clock(NOW)
    db = Database(":memory:")
    am = ApprovalManager(db, unlocked_settings(approval_ttl_s=120, approval_max_price_drift_points=1.5), clock)
    return am, db, clock


@pytest.mark.parametrize("text", [
    "ok", "OK", "vas-y", "vas-y peut-être", "oui", "approve", "Approve", "APPROVE", "APPROVE  ",
    "approve abcd1234", "APPROVE abcd123", "APPROVE ABCD1234", "APPROVE abcd1234 maybe", "APPROVE abcd1234?",
    "je pense APPROVE abcd1234", "", None, "APPROVED abcd1234", "APPROVE,abcd1234",
])
def test_ambiguous_commands_are_not_approvals(text):
    assert parse_command(text) is None


def test_exact_commands_parse():
    assert parse_command("APPROVE abcd1234").action == "APPROVE"
    assert parse_command(" REJECT 0f0f0f0f ").action == "REJECT"
    assert parse_command("CANCEL 12345678").proposal_id == "12345678"


def test_ambiguous_text_does_not_change_status(env):
    am, db, _ = env
    pid = am.create(payload(), True)
    for t in ("ok", "vas-y peut-être", "approve " + pid, "APPROVE"):
        res = am.handle(t, 2000.3)
        assert not res.ok
    assert db.get_proposal(pid)["status"] == PENDING


def test_approve_then_claim_once(env):
    am, db, _ = env
    p = payload()
    pid = am.create(p, True)
    assert am.handle(f"APPROVE {pid}", 2000.4).status == APPROVED
    assert am.claim_for_execution(pid, 2000.4, p).ok
    assert db.get_proposal(pid)["status"] == EXECUTING
    # Une approbation ne peut pas être réutilisée
    assert not am.claim_for_execution(pid, 2000.4, p).ok
    assert not am.handle(f"APPROVE {pid}", 2000.4).ok


def test_reject_and_cancel(env):
    am, db, _ = env
    pid = am.create(payload(), True)
    assert am.handle(f"REJECT {pid}", 2000.3).status == REJECTED
    assert not am.handle(f"APPROVE {pid}", 2000.3).ok
    pid2 = am.create(payload(), True)
    am.handle(f"APPROVE {pid2}", 2000.3)
    assert am.handle(f"CANCEL {pid2}", 2000.3).status == CANCELLED
    assert not am.claim_for_execution(pid2, 2000.3, payload()).ok


def test_expiry_by_time(env):
    am, db, clock = env
    pid = am.create(payload(), True)
    clock.t = NOW + timedelta(seconds=121)
    res = am.handle(f"APPROVE {pid}", 2000.3)
    assert not res.ok and res.status == EXPIRED


def test_expiry_by_time_after_approval(env):
    am, db, clock = env
    p = payload()
    pid = am.create(p, True)
    am.handle(f"APPROVE {pid}", 2000.3)
    clock.t = NOW + timedelta(seconds=200)
    res = am.claim_for_execution(pid, 2000.3, p)
    assert not res.ok and db.get_proposal(pid)["status"] == EXPIRED


def test_expiry_by_price_drift(env):
    am, db, _ = env
    pid = am.create(payload(), True)
    res = am.handle(f"APPROVE {pid}", 2002.0)  # dérive 1.7 > 1.5
    assert not res.ok and res.status == EXPIRED


def test_expiry_when_setup_changes(env):
    am, db, _ = env
    p = payload()
    pid = am.create(p, True)
    am.handle(f"APPROVE {pid}", 2000.3)
    res = am.claim_for_execution(pid, 2000.3, payload(stop_loss=1994.0))
    assert not res.ok and res.status == EXPIRED


def test_fingerprint_sensitive_to_each_field():
    base = fingerprint(payload())
    for change in ({"stop_loss": 1994.0}, {"take_profit": 2012.0}, {"size": 0.6}, {"score": 7.0},
                   {"setup_id": "BUY-2"}, {"epic": "SILVER"}, {"direction": "SELL"}):
        assert fingerprint(payload(**change)) != base


def test_blocked_proposal_cannot_be_approved(env):
    am, db, _ = env
    pid = am.create(payload(), False)
    assert db.get_proposal(pid)["status"] == BLOCKED
    assert not am.handle(f"APPROVE {pid}", 2000.3).ok


def test_format_contains_mandatory_fields():
    txt = format_proposal("abcd1234", payload())
    for needle in ("XAU/USD", "BUY", "Prix actuel", "Entrée", "SL", "TP", "RR", "Taille", "Capital",
                   "Risque", "CHF", "%", "Trades du jour", "SCORE 8/10", "H4", "M15", "M1", "Sweep", "FVG", "OB",
                   "VWAP", "OTE", "Raison du trade", "Raison du SL", "Raison du TP", "Conditions remplies",
                   "Conditions non remplies", "APPROVE abcd1234"):
        assert needle in txt, needle
    assert "chances" not in txt.lower()
