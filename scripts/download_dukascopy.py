"""Télécharge XAU/USD M1 (Dukascopy, BID) et l'enregistre en CSV.

Usage : python scripts/download_dukascopy.py 2024-01-01 2026-10-01 var/data/xauusd_m1.csv
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data.candles import save_csv  # noqa: E402
from data.sources import fetch_dukascopy_m1  # noqa: E402

start = datetime.fromisoformat(sys.argv[1]).replace(tzinfo=timezone.utc)
end = datetime.fromisoformat(sys.argv[2]).replace(tzinfo=timezone.utc)
dest = sys.argv[3] if len(sys.argv) > 3 else "var/data/xauusd_m1.csv"
import os
candles = fetch_dukascopy_m1(start, end, workers=int(os.environ.get("WORKERS", "3")),
                             log_fn=lambda m: print(m, flush=True))
save_csv(candles, dest)
print(f"{len(candles)} bougies M1 → {dest}")
