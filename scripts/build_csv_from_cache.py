"""Assemble en CSV les fichiers Dukascopy déjà présents dans le cache (sans télécharger).

Usage : python scripts/build_csv_from_cache.py var/data/xauusd_m1.csv
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data.candles import save_csv  # noqa: E402
from data.sources import _decode_bi5_candles  # noqa: E402

dest = sys.argv[1] if len(sys.argv) > 1 else "var/data/xauusd_m1.csv"
out = []
for f in sorted(Path("var/data/dukascopy").glob("XAUUSD_BID_*.bi5")):
    day = datetime.strptime(f.stem.split("_")[-1], "%Y%m%d").replace(tzinfo=timezone.utc)
    out += _decode_bi5_candles(f.read_bytes(), day)
out.sort(key=lambda c: c.time)
save_csv(out, dest)
print(f"{len(out)} bougies → {dest} ({out[0].time if out else '-'} → {out[-1].time if out else '-'})")
