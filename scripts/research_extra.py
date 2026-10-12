"""Analyses complémentaires (diagnostic, pas d'optimisation) sur la période complète, paramètres par défaut.

- coûts nuls (spread 0, slippage 0) : la règle a-t-elle un avantage AVANT coûts ?
- spread 0.80 (plafond de spread relevé à 1.0 pour que le test soit valide)
- options de gestion : break-even à 1R, clôture partielle à 1R

Usage : python scripts/research_extra.py var/data/xauusd_m1_histdata.csv var/reports
"""
import json
import multiprocessing as mp
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import scripts.research as R  # noqa: E402
from backtest.engine import BacktestEngine  # noqa: E402
from backtest.metrics import format_report  # noqa: E402

JOBS = [
    ("COMPLET coûts nuls (spread 0, slippage 0)", dict(backtest_slippage_points=0.0), 1e-9),
    ("COMPLET spread 0.80 (plafond 1.0)", dict(max_spread_points=1.0), 0.80),
    ("COMPLET break-even à 1R", dict(breakeven_at_1r=True), None),
    ("COMPLET clôture partielle 50 % à 1R", dict(partial_close_at_1r=True), None),
]


def run(job):
    label, kw, spread = job
    s = R.base_settings(**kw)
    data = R.M1 if spread is None else [replace(c, spread=spread) for c in R.M1]
    res = BacktestEngine(s).run(data, label=label, start=R.IS_START)
    return label, res.report, format_report(res.report)


if __name__ == "__main__":
    R.M1 = R.load(sys.argv[1])
    out = Path(sys.argv[2] if len(sys.argv) > 2 else "var/reports")
    with mp.get_context("fork").Pool(3) as pool:
        res = pool.map(run, JOBS)
    (out / "research_extra.txt").write_text("\n\n".join(r[2] for r in res))
    (out / "research_extra.json").write_text(json.dumps({r[0]: r[1] for r in res}, default=str, indent=1))
    print("ok")
