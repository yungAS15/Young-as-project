"""Protocole de recherche (fixé AVANT de regarder les résultats hors échantillon).

1. In-sample (IS) 2018-01-01 → 2023-01-01 : paramètres par défaut, puis petite grille.
2. Out-of-sample (OOS) 2023-01-01 → fin : exécuté UNE fois avec (a) les paramètres par défaut
   pré-enregistrés et (b) les meilleurs paramètres IS. Aucun réglage sur l'OOS.
3. Walk-forward : fenêtres glissantes (2 ans d'optimisation / 6 mois de test).
4. Sensibilité aux coûts : OOS avec spread doublé.

Usage : python scripts/research.py var/data/xauusd_m1_histdata.csv var/reports
"""
from __future__ import annotations

import csv
import itertools
import json
import multiprocessing as mp
import sys
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backtest.engine import BacktestEngine  # noqa: E402
from backtest.metrics import compute_report, format_report  # noqa: E402
from config import Settings  # noqa: E402
from data.candles import Candle  # noqa: E402

M1: list[Candle] = []
SPREAD = 0.40
GRID = {"min_score": [7.0, 8.0], "sl_atr_mult": [0.25, 0.5], "tp_mode": ["nearest_valid", "prefer_target"]}
IS_START = datetime(2018, 1, 1, tzinfo=timezone.utc)
OOS_START = datetime(2023, 1, 1, tzinfo=timezone.utc)


def load(path: str) -> list[Candle]:
    out = []
    with open(path) as fh:
        r = csv.reader(fh)
        next(r)
        for row in r:
            out.append(Candle(datetime.fromisoformat(row[0]), float(row[1]), float(row[2]), float(row[3]),
                              float(row[4]), float(row[5] or 0.0), SPREAD))
    return out


def base_settings(**kw) -> Settings:
    s = Settings(backtest_initial_capital_chf=10000.0, backtest_default_spread=SPREAD)
    return replace(s, **kw)


def with_params(s: Settings, params: dict) -> Settings:
    return replace(s, strategy=replace(s.strategy, **params))


def run_job(job: dict) -> dict:
    s = with_params(base_settings(), job.get("params", {}))
    data = M1
    if job.get("spread"):
        data = [replace(c, spread=job["spread"]) for c in M1]
    t = time.time()
    res = BacktestEngine(s).run(data, label=job["label"], start=job.get("start"), end=job.get("end"))
    rep = res.report
    rep["elapsed_s"] = round(time.time() - t, 1)
    trades = [{"entry_time": x.entry_time.isoformat(), "exit_time": x.exit_time.isoformat() if x.exit_time else None,
               "entry": x.entry, "sl": x.stop_loss, "tp": x.take_profit, "exit": x.exit_price,
               "reason": x.exit_reason, "r": x.r_multiple, "score": x.score, "rr": x.rr_planned, "flags": x.flags}
              for x in res.trades]
    return {"job": {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in job.items()},
            "report": rep, "trades": trades, "text": format_report(rep)}


def grid_jobs(label: str, start, end) -> list[dict]:
    keys = list(GRID)
    return [{"label": f"{label} {dict(zip(keys, combo))}", "params": dict(zip(keys, combo)), "start": start, "end": end}
            for combo in itertools.product(*(GRID[k] for k in keys))]


def best_of(results: list[dict], min_trades: int) -> dict:
    ok = [r for r in results if r["report"].get("trades", 0) >= min_trades]
    if not ok:
        return {}
    return max(ok, key=lambda r: r["report"]["expectancy_r"])["job"]["params"]


def main() -> None:
    global M1
    src, outdir = sys.argv[1], Path(sys.argv[2] if len(sys.argv) > 2 else "var/reports")
    outdir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    M1 = load(src)
    end = M1[-1].time + timedelta(minutes=1)
    print(f"{len(M1)} M1 de {M1[0].time} à {M1[-1].time} chargées en {time.time() - t0:.0f}s", flush=True)
    results: dict = {"source": src, "spread": SPREAD, "grid": GRID}

    with mp.get_context("fork").Pool(4) as pool:
        # 1) IS : défauts + grille
        is_jobs = [{"label": "IS défauts", "params": {}, "start": IS_START, "end": OOS_START}] + \
            grid_jobs("IS grille", IS_START, OOS_START)
        is_res = pool.map(run_job, is_jobs)
        results["is_default"] = is_res[0]
        results["is_grid"] = [{"params": r["job"]["params"], "trades": r["report"].get("trades"),
                               "winrate": r["report"].get("winrate"), "expectancy_r": r["report"].get("expectancy_r"),
                               "profit_factor": r["report"].get("profit_factor"),
                               "max_dd_pct": r["report"].get("max_drawdown_pct")} for r in is_res[1:]]
        best = best_of(is_res[1:], min_trades=100)
        results["is_best_params"] = best
        print("IS terminé, meilleurs paramètres IS :", best, flush=True)

        # 2) OOS (une seule fois) + 4) sensibilité aux coûts + période complète
        oos_jobs = [
            {"label": "OOS défauts (pré-enregistrés)", "params": {}, "start": OOS_START, "end": end},
            {"label": "OOS meilleurs paramètres IS", "params": best, "start": OOS_START, "end": end},
            {"label": "OOS défauts, spread 0.80", "params": {}, "start": OOS_START, "end": end, "spread": 0.80},
            {"label": "PÉRIODE COMPLÈTE défauts", "params": {}, "start": IS_START, "end": end},
        ]
        oos_res = pool.map(run_job, oos_jobs)
        results["oos_default"], results["oos_best"], results["oos_cost_x2"], results["full_default"] = oos_res
        print("OOS terminé", flush=True)

        # 3) Walk-forward : 2 ans IS / 6 mois OOS
        folds = []
        cur = IS_START
        while cur + timedelta(days=730 + 30) < end:
            is_s, is_e = cur, cur + timedelta(days=730)
            oos_e = min(is_e + timedelta(days=182), end)
            fold_res = pool.map(run_job, grid_jobs(f"WF IS {is_s.date()}", is_s, is_e))
            params = best_of(fold_res, min_trades=30)
            folds.append({"is": [is_s.isoformat(), is_e.isoformat()], "oos": [is_e.isoformat(), oos_e.isoformat()],
                          "params": params})
            cur += timedelta(days=182)
        wf_res = pool.map(run_job, [{"label": f"WF OOS {f['oos'][0][:10]}", "params": f["params"],
                                     "start": datetime.fromisoformat(f["oos"][0]),
                                     "end": datetime.fromisoformat(f["oos"][1])} for f in folds])
        for f, r in zip(folds, wf_res):
            f["oos_trades"] = r["report"].get("trades", 0)
            f["oos_expectancy_r"] = r["report"].get("expectancy_r")
            f["oos_winrate"] = r["report"].get("winrate")
        results["walk_forward_folds"] = folds
        print("Walk-forward terminé", flush=True)

    # Rapport walk-forward concaténé (trades OOS de chaque fenêtre)
    from backtest.metrics import SimTrade
    wf_trades = []
    for r in wf_res:
        for t in r["trades"]:
            st = SimTrade("wf", datetime.fromisoformat(t["entry_time"]), datetime.fromisoformat(t["entry_time"]),
                          t["entry"], t["sl"], t["tp"], 1.0, 100.0, t["score"], t["rr"], t["flags"])
            st.pnl_ccy = t["r"] * 100.0
            st.exit_reason = t["reason"]
            wf_trades.append(st)
    wf_trades.sort(key=lambda x: x.entry_time)
    wf_rep = compute_report(wf_trades, 10000.0, settings=base_settings(), label="WALK-FORWARD OOS concaténé (R × 100)")
    results["walk_forward_report"] = {"report": wf_rep, "text": format_report(wf_rep)}

    (outdir / "research.json").write_text(json.dumps(results, default=str, indent=1, ensure_ascii=False))
    txt = []
    for key in ("is_default", "oos_default", "oos_best", "oos_cost_x2", "full_default"):
        txt.append(results[key]["text"])
    txt.append("GRILLE IS :\n" + "\n".join(json.dumps(g, ensure_ascii=False) for g in results["is_grid"]))
    txt.append("WALK-FORWARD :\n" + "\n".join(json.dumps(f, ensure_ascii=False) for f in folds))
    txt.append(results["walk_forward_report"]["text"])
    (outdir / "research.txt").write_text("\n\n".join(txt))
    print(f"Terminé en {time.time() - t0:.0f}s → {outdir}/research.txt", flush=True)


if __name__ == "__main__":
    main()
