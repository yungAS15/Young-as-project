"""Exécute le protocole pré-enregistré de docs/HYPOTHESES_V2.md, sans intervention manuelle.

Usage : python scripts/research_v2.py var/data/xauusd_m1_histdata.csv var/reports
"""
import json
import multiprocessing as mp
import random
import statistics as st
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import scripts.research as R  # noqa: E402

HYPOTHESES = {
    "H1": {"confirm_tf": "M15"},
    "H2": {"require_significant_sweep": True},
    "H3": {"h4_strict_bullish": True, "require_d1_bullish": True},
    "H4": {"entry_mode": "limit"},
    "H5": {"tp_mode": "first_liquidity"},
}
V1_IS_EXPECTANCY = -0.181
MIN_GAIN_R = 0.10
MIN_IS_TRADES = 80


def ci95(rs):
    if len(rs) < 2:
        return None
    rnd = random.Random(0)
    boots = sorted(st.mean(rnd.choices(rs, k=len(rs))) for _ in range(5000))
    return [round(boots[125], 3), round(boots[-126], 3)]


def summary(res):
    r = res["report"]
    rs = [t["r"] for t in res["trades"]]
    return {"trades": r.get("trades", 0), "winrate": r.get("winrate"), "expectancy_r": r.get("expectancy_r"),
            "profit_factor": r.get("profit_factor"), "max_dd_pct": r.get("max_drawdown_pct"), "ci95": ci95(rs)}


if __name__ == "__main__":
    R.M1 = R.load(sys.argv[1])
    out = Path(sys.argv[2] if len(sys.argv) > 2 else "var/reports")
    end = R.M1[-1].time + timedelta(minutes=1)
    res = {"rule": {"v1_is_expectancy": V1_IS_EXPECTANCY, "min_gain_r": MIN_GAIN_R, "min_is_trades": MIN_IS_TRADES}}
    with mp.get_context("fork").Pool(4) as pool:
        jobs = [{"label": f"IS {h}", "params": p, "start": R.IS_START, "end": R.OOS_START} for h, p in HYPOTHESES.items()]
        is_res = pool.map(R.run_job, jobs)
    res["is"] = {}
    kept = {}
    for (h, p), r in zip(HYPOTHESES.items(), is_res):
        sm = summary(r)
        keep = sm["trades"] >= MIN_IS_TRADES and (sm["expectancy_r"] or -9) >= V1_IS_EXPECTANCY + MIN_GAIN_R
        sm["retained"] = keep
        res["is"][h] = sm
        print(h, sm, flush=True)
        if keep:
            kept.update(p)
    res["kept_params"] = kept
    print("Retenues :", kept, flush=True)
    if kept:
        with mp.get_context("fork").Pool(2) as pool:
            combo = pool.map(R.run_job, [
                {"label": "v2 IS (combinaison)", "params": kept, "start": R.IS_START, "end": R.OOS_START},
                {"label": "v2 OOS (UNE fois)", "params": kept, "start": R.OOS_START, "end": end},
            ])
        res["v2_is"], res["v2_oos"] = summary(combo[0]), summary(combo[1])
        (out / "research_v2.txt").write_text(combo[0]["text"] + "\n\n" + combo[1]["text"])
        print("v2 IS", res["v2_is"], flush=True)
        print("v2 OOS", res["v2_oos"], flush=True)
    (out / "research_v2.json").write_text(json.dumps(res, indent=1, default=str))
    print("ok", flush=True)
