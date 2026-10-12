"""Sources de données historiques pour la recherche/backtest.

1. **Capital.com** (`main.py fetch-history`) : la source de référence (même flux que le live),
   bid/ask + tick-volume. Nécessite des identifiants.
2. **CSV** (`data.candles.load_csv`) : par ex. export Dukascopy XAUUSD M1 obtenu sur votre Mac :
   `npx dukascopy-node -i xauusd -from 2022-01-01 -to 2025-12-31 -t m1 -f csv --volumes`
   Plusieurs années de M1 → échantillon suffisant (≥ 100 trades).
3. **Yahoo Finance `GC=F`** (ci-dessous) : contrat FUTURES or COMEX, pas le spot/CFD XAU/USD.
   Utile uniquement comme approximation : base futures/spot, horaires et volume différents.
   Historique M1 limité à ~30 jours (fenêtres de 8 jours) → échantillon trop petit pour conclure.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

import requests

from data.candles import Candle

log = logging.getLogger(__name__)

YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"


def _yahoo_chunk(symbol: str, interval: str, start: datetime, end: datetime) -> list[Candle]:
    params = {"interval": interval, "period1": int(start.timestamp()), "period2": int(end.timestamp()),
              "includePrePost": "true"}
    resp = requests.get(YAHOO_URL.format(symbol=symbol), params=params, timeout=30,
                        headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    res = resp.json()["chart"]["result"]
    if not res:
        return []
    r = res[0]
    ts = r.get("timestamp") or []
    q = r["indicators"]["quote"][0]
    out = []
    for i, t in enumerate(ts):
        o, h, lo, c, v = q["open"][i], q["high"][i], q["low"][i], q["close"][i], q["volume"][i]
        if None in (o, h, lo, c):
            continue
        out.append(Candle(datetime.fromtimestamp(t, tz=timezone.utc), float(o), float(h), float(lo), float(c),
                          float(v or 0.0), 0.0))
    return out


def fetch_yahoo(symbol: str = "GC=F", interval: str = "1m", days: int = 29) -> list[Candle]:
    """Bougies Yahoo (UTC). 1m : ≤ 30 jours en fenêtres de 8 jours."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    chunk = timedelta(days=7) if interval == "1m" else timedelta(days=59)
    seen: dict[datetime, Candle] = {}
    cur = start
    while cur < end:
        nxt = min(cur + chunk, end)
        for c in _yahoo_chunk(symbol, interval, cur, nxt):
            seen[c.time] = c
        cur = nxt
        time.sleep(0.5)
    return [seen[k] for k in sorted(seen)]


# ---------------------------------------------------------------------------
# Dukascopy (XAU/USD spot, BID, tick-volume) — fichiers journaliers bi5 publics.
# ---------------------------------------------------------------------------
DUKA_URL = "https://datafeed.dukascopy.com/datafeed/{sym}/{y:04d}/{m:02d}/{d:02d}/{side}_candles_min_1.bi5"


def _decode_bi5_candles(raw: bytes, day: datetime, divisor: float = 1000.0) -> list[Candle]:
    import lzma
    import struct
    if not raw:
        return []
    data = lzma.decompress(raw)
    out = []
    for i in range(len(data) // 24):
        sec, o, c, lo, h, v = struct.unpack(">5if", data[i * 24:(i + 1) * 24])
        if v <= 0 and o == c == lo == h:
            continue   # minute sans cotation (marché fermé) : ignorée
        out.append(Candle(day + timedelta(seconds=sec), o / divisor, h / divisor, lo / divisor, c / divisor,
                          float(v), 0.0))
    return out


def _download_day(url: str, dest, max_retries: int, pause_s: float) -> bool:
    import random
    for _ in range(max_retries):
        try:
            r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200:
                tmp = dest.with_suffix(".part")
                tmp.write_bytes(r.content)
                tmp.replace(dest)
                return True
            if r.status_code == 404:
                dest.write_bytes(b"")
                return True
        except requests.RequestException:
            pass
        time.sleep(pause_s + random.random() * 2 * pause_s)   # 503 = limitation de débit
    return False


def fetch_dukascopy_m1(start: datetime, end: datetime, cache_dir: str = "var/data/dukascopy",
                       symbol: str = "XAUUSD", side: str = "BID", pause_s: float = 1.5,
                       max_retries: int = 40, workers: int = 3, log_fn=print) -> list[Candle]:
    """Télécharge (avec cache par jour) les M1 Dukascopy entre start et end (UTC).

    Le serveur limite le débit (HTTP 503) : quelques requêtes en parallèle, pauses
    aléatoires et nombreux essais. Relancer la commande reprend là où elle s'est arrêtée.
    """
    from concurrent.futures import ThreadPoolExecutor
    from pathlib import Path
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    days = []
    day = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    while day < end:
        if day.weekday() != 5:   # samedi : marché fermé
            days.append(day)
        day += timedelta(days=1)
    todo = [(d, cache / f"{symbol}_{side}_{d:%Y%m%d}.bi5") for d in days]
    todo = [(d, f) for d, f in todo if not f.exists()][::-1]   # le plus récent d'abord

    def job(item):
        d, f = item
        url = DUKA_URL.format(sym=symbol, y=d.year, m=d.month - 1, d=d.day, side=side)
        ok = _download_day(url, f, max_retries, pause_s)
        if not ok:
            log_fn(f"échec {d:%Y-%m-%d}")
        return ok

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for _ in ex.map(job, todo):
            done += 1
            if done % 25 == 0:
                log_fn(f"{done}/{len(todo)} jours téléchargés")
    out: list[Candle] = []
    for d in days:
        f = cache / f"{symbol}_{side}_{d:%Y%m%d}.bi5"
        if f.exists():
            out += _decode_bi5_candles(f.read_bytes(), d)
    out.sort(key=lambda c: c.time)
    return out


# ---------------------------------------------------------------------------
# HistData.com (XAU/USD M1, BID, SANS volume) — un fichier par année (ou par mois
# pour l'année en cours). Horodatage : heure de New York AVEC heure d'été (vérifié
# contre Dukascopy : décalage UTC-5 en hiver, UTC-4 en été).
# Volume toujours 0 → les VWAP sont « indisponibles » sur cette source (honnête).
# ---------------------------------------------------------------------------
HISTDATA_PAGE = "https://www.histdata.com/download-free-forex-historical-data/?/ascii/1-minute-bar-quotes/{pair}/{path}"


def fetch_histdata_zip(pair: str, year: int, month: int | None, dest_dir: str = "var/data/histdata") -> "Path":
    import re
    from pathlib import Path
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    tag = f"{year}{month:02d}" if month else f"{year}"
    out = dest / f"{pair}_{tag}.zip"
    if out.exists() and out.stat().st_size > 1000:
        return out
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0"
    page_url = HISTDATA_PAGE.format(pair=pair.lower(), path=f"{year}/{month}" if month else f"{year}")
    html = s.get(page_url, timeout=60).text
    fields = dict(re.findall(r'<input type="hidden" name="(\w+)" id="\w+" value="([^"]*)"', html))
    if "tk" not in fields:
        raise RuntimeError(f"Formulaire HistData introuvable pour {tag}")
    r = s.post("https://www.histdata.com/get.php", data=fields, headers={"Referer": page_url}, timeout=180)
    r.raise_for_status()
    if not r.content.startswith(b"PK"):
        raise RuntimeError(f"Réponse HistData inattendue pour {tag}")
    out.write_bytes(r.content)
    return out


def parse_histdata_zip(path) -> list[Candle]:
    import io
    import zipfile
    from zoneinfo import ZoneInfo
    ny = ZoneInfo("America/New_York")
    out: list[Candle] = []
    with zipfile.ZipFile(path) as z:
        name = next(n for n in z.namelist() if n.endswith(".csv"))
        for line in io.TextIOWrapper(z.open(name), encoding="utf-8"):
            parts = line.strip().split(";")
            if len(parts) < 5:
                continue
            t = datetime.strptime(parts[0], "%Y%m%d %H%M%S").replace(tzinfo=ny).astimezone(timezone.utc)
            o, h, lo, c = (float(x) for x in parts[1:5])
            out.append(Candle(t, o, h, lo, c, 0.0, 0.0))
    return out
