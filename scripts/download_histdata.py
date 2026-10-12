"""Télécharge XAU/USD M1 depuis HistData.com (années complètes + mois de l'année en cours).

Usage : python scripts/download_histdata.py 2018 2026 var/data/xauusd_m1_histdata.csv
Source BID sans volume (VWAP indisponibles) ; heures converties de EST fixe vers UTC.
"""
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data.candles import save_csv  # noqa: E402
from data.sources import fetch_histdata_zip, parse_histdata_zip  # noqa: E402

y0, y1 = int(sys.argv[1]), int(sys.argv[2])
dest = sys.argv[3] if len(sys.argv) > 3 else "var/data/xauusd_m1_histdata.csv"
now = datetime.now(timezone.utc)
candles = []
for y in range(y0, y1 + 1):
    periods = [None] if y < now.year else list(range(1, now.month))
    for m in periods:
        for attempt in range(5):
            try:
                z = fetch_histdata_zip("XAUUSD", y, m)
                part = parse_histdata_zip(z)
                candles += part
                print(f"{y}{'' if m is None else f'-{m:02d}'} : {len(part)} bougies", flush=True)
                break
            except Exception as exc:  # noqa: BLE001
                print(f"{y}-{m} essai {attempt + 1} : {exc}", flush=True)
                time.sleep(5 * (attempt + 1))
        time.sleep(2)
candles.sort(key=lambda c: c.time)
dedup = {c.time: c for c in candles}
candles = [dedup[k] for k in sorted(dedup)]
save_csv(candles, dest)
print(f"{len(candles)} bougies M1 → {dest}")
