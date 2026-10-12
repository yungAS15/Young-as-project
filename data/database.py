"""Persistance SQLite : signaux, propositions, trades, compteurs journaliers, événements.

Le compteur de trades du jour et le P&L réalisé du jour sont persistants : un
redémarrage du bot ne remet jamais le compteur à zéro.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    setup_id TEXT,
    timeframe TEXT,
    price REAL,
    h4_bias TEXT,
    structure TEXT,
    liquidity TEXT,
    sweep INTEGER,
    vwap TEXT,
    fvg INTEGER,
    ob INTEGER,
    fib_ote INTEGER,
    score REAL,
    entry REAL,
    stop_loss REAL,
    take_profit REAL,
    rr REAL,
    size REAL,
    risk_ccy REAL,
    risk_pct REAL,
    decision TEXT NOT NULL,
    refusal_reason TEXT,
    details TEXT
);
CREATE TABLE IF NOT EXISTS proposals (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    setup_id TEXT,
    signal_id INTEGER,
    fingerprint TEXT NOT NULL,
    price_at_creation REAL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL,
    decided_at TEXT,
    decision_note TEXT,
    executed_at TEXT
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id TEXT,
    setup_id TEXT,
    deal_id TEXT,
    deal_reference TEXT,
    mode TEXT NOT NULL,
    direction TEXT NOT NULL,
    epic TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    trading_day TEXT NOT NULL,
    entry REAL,
    size REAL,
    stop_loss REAL,
    take_profit REAL,
    risk_ccy REAL,
    status TEXT NOT NULL,
    closed_at TEXT,
    exit_price REAL,
    pnl_ccy REAL,
    r_multiple REAL,
    duration_s REAL,
    result TEXT,
    exit_reason TEXT
);
CREATE TABLE IF NOT EXISTS daily_counters (
    day TEXT PRIMARY KEY,
    trades_count INTEGER NOT NULL DEFAULT 0,
    realized_pnl REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS used_setups (
    setup_id TEXT PRIMARY KEY,
    used_at TEXT NOT NULL
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def parse_iso(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


class Database:
    def __init__(self, path: str | Path, tz: str = "Europe/Zurich"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.tz = ZoneInfo(tz)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    # ------------------------------------------------------------- helpers
    def _exec(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, params)

    def _rows(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def trading_day(self, when: datetime | None = None) -> date:
        return (when or utcnow()).astimezone(self.tz).date()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------- kv / state
    def get(self, key: str, default: str | None = None) -> str | None:
        r = self._rows("SELECT value FROM kv WHERE key=?", (key,))
        return r[0]["value"] if r else default

    def set(self, key: str, value: str | None) -> None:
        self._exec("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, value))

    def runtime_kill_switch(self) -> bool:
        return self.get("kill_switch") == "1"

    # ------------------------------------------------------------- events
    def log_event(self, kind: str, message: str, level: str = "INFO") -> None:
        self._exec("INSERT INTO events(ts, level, kind, message) VALUES(?,?,?,?)",
                   (iso(utcnow()), level, kind, message))

    def recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))

    # ------------------------------------------------------------- signals
    def record_signal(self, **fields: Any) -> int:
        fields.setdefault("ts", iso(utcnow()))
        for k in ("vwap", "details"):
            if k in fields and not isinstance(fields[k], str):
                fields[k] = json.dumps(fields[k], default=str, ensure_ascii=False)
        cols = ", ".join(fields)
        qs = ", ".join("?" for _ in fields)
        cur = self._exec(f"INSERT INTO signals({cols}) VALUES({qs})", tuple(fields.values()))
        return int(cur.lastrowid)

    def signals(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,))

    # ------------------------------------------------------------- setups
    def mark_setup_used(self, setup_id: str) -> bool:
        """Une seule entrée par signal. Renvoie False si déjà utilisé."""
        try:
            self._exec("INSERT INTO used_setups(setup_id, used_at) VALUES(?, ?)", (setup_id, iso(utcnow())))
            return True
        except sqlite3.IntegrityError:
            return False

    def setup_used(self, setup_id: str) -> bool:
        return bool(self._rows("SELECT 1 FROM used_setups WHERE setup_id=?", (setup_id,)))

    # ------------------------------------------------------------- proposals
    def insert_proposal(self, pid: str, created_at: datetime, expires_at: datetime, setup_id: str,
                        signal_id: int | None, fingerprint: str, price: float, payload: dict[str, Any],
                        status: str) -> None:
        self._exec(
            "INSERT INTO proposals(id, created_at, expires_at, setup_id, signal_id, fingerprint, "
            "price_at_creation, payload, status) VALUES(?,?,?,?,?,?,?,?,?)",
            (pid, iso(created_at), iso(expires_at), setup_id, signal_id, fingerprint, price,
             json.dumps(payload, default=str, ensure_ascii=False), status))

    def get_proposal(self, pid: str) -> dict[str, Any] | None:
        r = self._rows("SELECT * FROM proposals WHERE id=?", (pid,))
        if not r:
            return None
        row = r[0]
        row["payload"] = json.loads(row["payload"])
        return row

    def update_proposal_status(self, pid: str, status: str, note: str = "", *,
                               expected_status: str | None = None) -> bool:
        """Transition d'état atomique (compare-and-set) : empêche toute réutilisation."""
        now = iso(utcnow())
        col = "executed_at" if status in ("EXECUTED", "FAILED") else "decided_at"
        if expected_status is None:
            cur = self._exec(f"UPDATE proposals SET status=?, decision_note=?, {col}=? WHERE id=?",
                             (status, note, now, pid))
        else:
            cur = self._exec(f"UPDATE proposals SET status=?, decision_note=?, {col}=? WHERE id=? AND status=?",
                             (status, note, now, pid, expected_status))
        return cur.rowcount == 1

    def proposals(self, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if status:
            rows = self._rows("SELECT * FROM proposals WHERE status=? ORDER BY created_at DESC LIMIT ?", (status, limit))
        else:
            rows = self._rows("SELECT * FROM proposals ORDER BY created_at DESC LIMIT ?", (limit,))
        for r in rows:
            r["payload"] = json.loads(r["payload"])
        return rows

    # ------------------------------------------------------------- trades
    def open_trade(self, *, proposal_id: str | None, setup_id: str, deal_id: str | None,
                   deal_reference: str | None, mode: str, direction: str, epic: str, opened_at: datetime,
                   entry: float, size: float, stop_loss: float, take_profit: float, risk_ccy: float) -> int:
        day = self.trading_day(opened_at).isoformat()
        with self._lock:
            self.conn.execute("BEGIN")
            try:
                cur = self.conn.execute(
                    "INSERT INTO trades(proposal_id, setup_id, deal_id, deal_reference, mode, direction, epic, "
                    "opened_at, trading_day, entry, size, stop_loss, take_profit, risk_ccy, status) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'OPEN')",
                    (proposal_id, setup_id, deal_id, deal_reference, mode, direction, epic, iso(opened_at), day,
                     entry, size, stop_loss, take_profit, risk_ccy))
                self.conn.execute(
                    "INSERT INTO daily_counters(day, trades_count, realized_pnl) VALUES(?, 1, 0) "
                    "ON CONFLICT(day) DO UPDATE SET trades_count = trades_count + 1", (day,))
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return int(cur.lastrowid)

    def close_trade(self, trade_id: int, *, closed_at: datetime, exit_price: float | None, pnl_ccy: float,
                    exit_reason: str) -> None:
        rows = self._rows("SELECT * FROM trades WHERE id=?", (trade_id,))
        if not rows:
            raise KeyError(trade_id)
        t = rows[0]
        if t["status"] == "CLOSED":
            return
        opened = parse_iso(t["opened_at"])
        dur = (closed_at - opened).total_seconds() if opened else None
        r_mult = pnl_ccy / t["risk_ccy"] if t["risk_ccy"] else None
        result = "WIN" if pnl_ccy > 0 else ("LOSS" if pnl_ccy < 0 else "BREAKEVEN")
        day = self.trading_day(closed_at).isoformat()
        with self._lock:
            self.conn.execute("BEGIN")
            try:
                self.conn.execute(
                    "UPDATE trades SET status='CLOSED', closed_at=?, exit_price=?, pnl_ccy=?, r_multiple=?, "
                    "duration_s=?, result=?, exit_reason=? WHERE id=?",
                    (iso(closed_at), exit_price, pnl_ccy, r_mult, dur, result, exit_reason, trade_id))
                self.conn.execute(
                    "INSERT INTO daily_counters(day, trades_count, realized_pnl) VALUES(?, 0, ?) "
                    "ON CONFLICT(day) DO UPDATE SET realized_pnl = realized_pnl + ?", (day, pnl_ccy, pnl_ccy))
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def open_trades(self) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM trades WHERE status='OPEN' ORDER BY id")

    def trades(self, limit: int = 200) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,))

    def daily(self, day: date | None = None) -> dict[str, Any]:
        d = (day or self.trading_day()).isoformat()
        r = self._rows("SELECT * FROM daily_counters WHERE day=?", (d,))
        return r[0] if r else {"day": d, "trades_count": 0, "realized_pnl": 0.0}

    def iter_closed_trades(self) -> Iterator[dict[str, Any]]:
        yield from self._rows("SELECT * FROM trades WHERE status='CLOSED' ORDER BY id")
