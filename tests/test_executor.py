from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from approval.trade_approval import ApprovalManager
from broker.capital_api import CapitalAPIError
from broker.models import AccountInfo, DealConfirmation, Position, Quote
from data.candles import Candle
from data.database import Database
from data.market_data import MarketSnapshot
from execution.trade_executor import PositionMonitor, TradeExecutor
from monitoring.alerts import Notifier
from tests.helpers import gold_spec, unlocked_settings


class Clock:
    def __init__(self):
        self.t = datetime.now(timezone.utc)

    def __call__(self):
        return self.t


class FakeClient:
    def __init__(self):
        self.connected = True
        self.consecutive_errors = 0
        self.orders = []
        self.closed = []
        self.updates = []
        self.confirm_status = "ACCEPTED"
        self.position_has_sl = True
        self.raise_on_order = False

    def open_position(self, epic, direction, size, stop_level, profit_level):
        if self.raise_on_order:
            raise CapitalAPIError("timeout")
        self.orders.append((epic, direction, size, stop_level, profit_level))
        return "o_ref"

    def confirm(self, ref):
        return DealConfirmation(ref, self.confirm_status, "OPEN", "D1", 4000.4, self.orders[-1][2], "BUY", "GOLD",
                                None if self.confirm_status == "ACCEPTED" else "REJECTED",
                                [{"dealId": "D1", "status": "OPENED"}])

    def get_position(self, deal_id):
        o = self.orders[-1]
        sl = o[3] if self.position_has_sl else None
        return Position(deal_id, "GOLD", "BUY", o[2], 4000.4, sl, o[4], 0.0, "USD", None)

    def update_position(self, deal_id, stop_level=None, profit_level=None):
        self.updates.append((deal_id, stop_level, profit_level))
        raise CapitalAPIError("cannot set")

    def close_position(self, deal_id):
        self.closed.append(deal_id)
        return "p_close"


class FakeMarket:
    def __init__(self, bid=4000.0, offer=4000.4, capital=5000.0):
        self.bid, self.offer, self.capital = bid, offer, capital
        self.positions = []

    def instrument(self, force=False):
        return gold_spec(bid=self.bid, offer=self.offer)

    def snapshot(self, with_candles=True):
        now = datetime.now(timezone.utc)
        m1 = [Candle(now - timedelta(minutes=2), self.bid, self.bid, self.bid, self.bid, 1, 0.4)]
        return MarketSnapshot(
            now=now, m1=m1, quote=Quote("GOLD", self.bid, self.offer, now - timedelta(seconds=1), "TRADEABLE"),
            instrument=self.instrument(), account=AccountInfo("A", "CHF", self.capital, self.capital, 0, self.capital),
            positions=list(self.positions), fx_rate=0.9)


def payload(size, risk):
    return {"epic": "GOLD", "instrument": "XAU/USD", "direction": "BUY", "bid": 4000.0, "offer": 4000.4,
            "entry": 4000.4, "stop_loss": 3990.0, "take_profit": 4022.0, "rr": 2.08, "size": size,
            "capital": 5000.0, "account_currency": "CHF", "risk_ccy": risk, "risk_pct": 1.0, "trades_today": 0,
            "max_trades": 2, "score": 8.0, "score_label": "SCORE 8/10", "analysis": {}, "flags": {}, "vwaps": {},
            "trade_reason": "", "sl_reason": "", "tp_reason": "", "conditions_met": [], "conditions_unmet": [],
            "setup_id": "BUY-1", "mode": "DRY_RUN"}


def make(settings=None, market=None, client=None):
    s = settings or unlocked_settings()
    db = Database(":memory:")
    clock = Clock()
    am = ApprovalManager(db, s, clock)
    market = market or FakeMarket()
    client = client or FakeClient()
    ex = TradeExecutor(s, db, market, client, am, Notifier([]))
    return ex, db, am, market, client


# capital 5000 → risque 50 CHF ; distance (4000.4-3990+0.1)=10.5 × 0.9 = 9.45 CHF/unité → 5.29
SIZE, RISK = 5.29, 49.99


def approved(am, size=SIZE, risk=RISK):
    pid = am.create(payload(size, risk), True)
    assert am.handle(f"APPROVE {pid}", 4000.4).ok
    return pid


def test_dry_run_executes_without_sending_order():
    ex, db, am, _, client = make()
    pid = approved(am)
    res = ex.execute(pid)
    assert res.ok, res.message
    assert client.orders == []
    t = db.open_trades()[0]
    assert t["mode"] == "DRY_RUN" and t["size"] == SIZE
    assert db.daily()["trades_count"] == 1
    assert db.get_proposal(pid)["status"] == "EXECUTED"
    # réutilisation impossible
    assert not ex.execute(pid).ok


def test_not_approved_cannot_execute():
    ex, db, am, _, client = make()
    pid = am.create(payload(SIZE, RISK), True)  # PENDING
    assert not ex.execute(pid).ok
    assert db.open_trades() == []


def test_kill_switch_config_and_runtime_block():
    ex, db, am, _, _ = make(settings=unlocked_settings(kill_switch=True))
    assert not ex.execute(approved(am)).ok
    ex, db, am, _, _ = make()
    db.set("kill_switch", "1")
    assert not ex.execute(approved(am)).ok
    assert db.open_trades() == []


def test_reverification_fails_on_spread():
    ex, db, am, market, _ = make(market=FakeMarket(bid=4000.0, offer=4001.0))
    res = ex.execute(approved(am))
    assert not res.ok and "spread" in res.message


def test_reverification_fails_when_capital_drops():
    ex, db, am, market, _ = make()
    pid = approved(am)
    market.capital = 1000.0     # risque autorisé 10 CHF < risque approuvé
    res = ex.execute(pid)
    assert not res.ok and "risque" in res.message
    assert db.open_trades() == []


def test_open_position_blocks_second_trade():
    ex, db, am, market, _ = make()
    market.positions = [Position("X", "GOLD", "BUY", 1, 4000, 3990, 4020, 0, "USD", None)]
    res = ex.execute(approved(am))
    assert not res.ok and "position_ouverte" in res.message


def test_daily_trade_limit_persistent():
    ex, db, am, _, _ = make()
    now = datetime.now(timezone.utc)
    for _ in range(2):
        tid = db.open_trade(proposal_id=None, setup_id="old", deal_id="d", deal_reference=None, mode="DRY_RUN",
                            direction="BUY", epic="GOLD", opened_at=now, entry=1, size=1, stop_loss=0.5,
                            take_profit=2, risk_ccy=1)
        db.close_trade(tid, closed_at=now, exit_price=2, pnl_ccy=1, exit_reason="TP")
    res = ex.execute(approved(am))
    assert not res.ok and "trades_jour" in res.message


def test_one_entry_per_setup():
    ex, db, am, _, _ = make()
    db.mark_setup_used("BUY-1")
    res = ex.execute(approved(am))
    assert not res.ok and "setup déjà utilisé" in res.message


def test_price_drift_expires_approval():
    ex, db, am, market, _ = make()
    pid = approved(am)
    market.bid, market.offer = 4002.0, 4002.4
    res = ex.execute(pid)
    assert not res.ok
    assert db.get_proposal(pid)["status"] in ("EXPIRED", "FAILED")


def live_settings(**kw):
    from config import LIVE_ACK_PHRASE
    return unlocked_settings(dry_run=False, capital_env="demo", **kw)


def test_real_order_path_verified():
    ex, db, am, _, client = make(settings=live_settings())
    res = ex.execute(approved(am))
    assert res.ok, res.message
    assert client.orders == [("GOLD", "BUY", SIZE, 3990.0, 4022.0)]
    assert db.open_trades()[0]["deal_id"] == "D1"


def test_live_account_requires_ack():
    ex, db, am, _, client = make(settings=unlocked_settings(dry_run=False, capital_env="live"))
    res = ex.execute(approved(am))
    assert not res.ok and "LIVE_TRADING_ACK" in res.message
    assert client.orders == []


def test_rejected_order_is_failure():
    c = FakeClient()
    c.confirm_status = "REJECTED"
    ex, db, am, _, _ = make(settings=live_settings(), client=c)
    res = ex.execute(approved(am))
    assert not res.ok and "REJETÉ" in res.message
    assert db.open_trades() == []


def test_missing_sl_closes_position_and_trips_kill_switch():
    c = FakeClient()
    c.position_has_sl = False
    ex, db, am, _, _ = make(settings=live_settings(), client=c)
    res = ex.execute(approved(am))
    assert not res.ok
    assert c.closed == ["D1"]
    assert db.runtime_kill_switch()


def test_order_network_error_trips_kill_switch():
    c = FakeClient()
    c.raise_on_order = True
    ex, db, am, _, _ = make(settings=live_settings(), client=c)
    assert not ex.execute(approved(am)).ok
    assert db.runtime_kill_switch()


def test_dry_run_monitor_closes_on_sl_and_updates_daily_pnl():
    ex, db, am, market, _ = make()
    ex.execute(approved(am))
    mon = PositionMonitor(ex.s, db, None, market, Notifier([]))
    market.bid, market.offer = 3989.0, 3989.4
    mon.check()
    t = db.trades()[0]
    assert t["status"] == "CLOSED" and t["exit_reason"] == "SL" and t["pnl_ccy"] < 0
    assert db.daily()["realized_pnl"] == pytest.approx(t["pnl_ccy"])
