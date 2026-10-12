"""Séparation optimisation / test hors échantillon et walk-forward.

Règle d'or : les paramètres sont choisis UNIQUEMENT sur la période d'optimisation
(in-sample), puis appliqués tels quels sur la période de test (out-of-sample) qui suit.
La grille est volontairement petite (peu de degrés de liberté = moins de sur-optimisation).
"""
from __future__ import annotations

import itertools
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, Sequence

from backtest.engine import BacktestEngine
from backtest.metrics import SimTrade, compute_report
from config import Settings
from data.candles import Candle

DEFAULT_GRID: dict[str, list[Any]] = {
    "min_score": [7.0, 8.0],
    "sl_atr_mult": [0.25, 0.5],
    "tp_mode": ["nearest_valid", "prefer_target"],
}


def _with_params(s: Settings, params: dict[str, Any]) -> Settings:
    return replace(s, strategy=replace(s.strategy, **params))


def objective(rep: dict[str, Any], min_trades: int) -> float:
    """Expectancy en R, pénalisée si trop peu de trades (évite de choisir le hasard)."""
    n = rep.get("trades", 0)
    if n < min_trades:
        return float("-inf")
    return rep["expectancy_r"]


def split_is_oos(m1: Sequence[Candle], is_fraction: float = 0.7) -> tuple[datetime, datetime, datetime]:
    t0, t1 = m1[0].time, m1[-1].time
    cut = t0 + (t1 - t0) * is_fraction
    return t0, cut, t1


def optimize(settings: Settings, m1: Sequence[Candle], start: datetime, end: datetime,
             grid: dict[str, list[Any]] | None = None, min_trades: int = 20) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    grid = grid or DEFAULT_GRID
    keys = list(grid)
    results = []
    best: tuple[float, dict[str, Any]] | None = None
    for combo in itertools.product(*(grid[k] for k in keys)):
        params = dict(zip(keys, combo))
        res = BacktestEngine(_with_params(settings, params)).run(m1, start=start, end=end)
        score = objective(res.report, min_trades)
        results.append({"params": params, "trades": res.report.get("trades", 0),
                        "expectancy_r": res.report.get("expectancy_r"), "winrate": res.report.get("winrate")})
        if best is None or score > best[0]:
            best = (score, params)
    return (best[1] if best else {}), results


def walk_forward(settings: Settings, m1: Sequence[Candle], train_days: int = 180, test_days: int = 60,
                 grid: dict[str, list[Any]] | None = None, min_trades: int = 15) -> dict[str, Any]:
    """Fenêtres glissantes : optimise sur `train_days`, teste sur les `test_days` suivants."""
    t0, t_end = m1[0].time, m1[-1].time
    folds = []
    oos_trades: list[SimTrade] = []
    cur = t0
    while cur + timedelta(days=train_days + test_days) <= t_end + timedelta(days=1):
        is_start, is_end = cur, cur + timedelta(days=train_days)
        oos_end = min(is_end + timedelta(days=test_days), t_end)
        params, _ = optimize(settings, m1, is_start, is_end, grid, min_trades)
        if not params:
            params = {}
        res = BacktestEngine(_with_params(settings, params)).run(m1, start=is_end, end=oos_end)
        oos_trades += res.trades
        folds.append({"is": (is_start.date().isoformat(), is_end.date().isoformat()),
                      "oos": (is_end.date().isoformat(), oos_end.date().isoformat()),
                      "params": params, "oos_trades": res.report.get("trades", 0),
                      "oos_expectancy_r": res.report.get("expectancy_r"), "oos_winrate": res.report.get("winrate")})
        cur += timedelta(days=test_days)
    oos_trades.sort(key=lambda t: t.entry_time)
    rep = compute_report(oos_trades, settings.backtest_initial_capital_chf, settings=settings,
                         label="WALK-FORWARD (hors échantillon concaténé)")
    return {"folds": folds, "oos_report": rep}
