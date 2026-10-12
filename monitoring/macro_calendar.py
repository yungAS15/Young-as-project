"""Calendrier macro local (NFP, CPI, FOMC…) pour la pause autour des annonces.

Aucune API gratuite et fiable n'étant garantie, le calendrier est un fichier CSV que
vous maintenez (`var/macro_events.csv`) :

    datetime_utc,event,impact
    2026-11-06T13:30:00,NFP,high
    2026-11-12T13:30:00,CPI,high
    2026-11-18T19:00:00,FOMC,high

Fail-closed : si la pause est activée et que le fichier est absent, illisible, ou ne
contient aucun événement futur (calendrier périmé), `blackout()` renvoie None → le
Risk Manager refuse le trade.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from data.candles import ensure_utc


@dataclass(frozen=True)
class MacroEvent:
    time: datetime
    name: str
    impact: str


def load_events(path: str | Path) -> list[MacroEvent] | None:
    p = Path(path)
    if not p.exists():
        return None
    try:
        with open(p, newline="") as fh:
            out = [MacroEvent(ensure_utc(datetime.fromisoformat(r["datetime_utc"].strip())), r["event"].strip(),
                              (r.get("impact") or "high").strip().lower()) for r in csv.DictReader(fh)]
    except (KeyError, ValueError, OSError):
        return None
    return sorted(out, key=lambda e: e.time)


def blackout(now: datetime, events: list[MacroEvent] | None, minutes: int) -> bool | None:
    if events is None:
        return None
    if not any(e.time >= now for e in events):
        return None   # calendrier périmé
    win = timedelta(minutes=minutes)
    return any(e.impact == "high" and abs(e.time - now) <= win for e in events)


def today_events(now: datetime, events: list[MacroEvent] | None) -> list[MacroEvent]:
    if not events:
        return []
    return [e for e in events if e.time.date() == now.date()]
