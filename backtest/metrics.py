"""Statistiques de backtest et critères de validation."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Sequence

from strategy.scoring import score_bucket

DISCLAIMER = ("AVERTISSEMENT : les performances passées (réelles ou simulées) ne garantissent pas les "
              "performances futures. Un backtest est une simulation avec des hypothèses de coûts et "
              "d'exécution ; le marché réel peut différer.")


@dataclass
class SimTrade:
    setup_id: str
    signal_time: datetime
    entry_time: datetime
    entry: float               # prix de remplissage (ASK + slippage)
    stop_loss: float
    take_profit: float
    size: float
    risk_ccy: float            # risque prévu (devise du compte)
    score: float
    rr_planned: float
    flags: dict[str, bool] = field(default_factory=dict)
    exit_time: datetime | None = None
    exit_price: float | None = None
    exit_reason: str = ""
    pnl_ccy: float = 0.0
    costs_ccy: float = 0.0
    initial_sl: float = 0.0
    initial_size: float = 0.0
    realized_partial_ccy: float = 0.0
    be_moved: bool = False
    partial_done: bool = False

    @property
    def r_multiple(self) -> float:
        return self.pnl_ccy / self.risk_ccy if self.risk_ccy else 0.0

    @property
    def win(self) -> bool:
        return self.pnl_ccy > 0


def session_of(t: datetime) -> str:
    h = t.hour
    if 7 <= h < 12:
        return "Londres (07-12 UTC)"
    if 12 <= h < 17:
        return "New York / overlap (12-17 UTC)"
    if 17 <= h < 21:
        return "NY après-midi (17-21 UTC)"
    return "Asie / nuit (21-07 UTC)"


def setup_type(flags: dict[str, bool]) -> str:
    zone = "+".join(k.upper() for k in ("fvg", "ob", "ote") if flags.get(k)) or "aucune zone"
    return zone + (" +VWAP" if flags.get("vwap") else "")


def _stats(trades: Sequence[SimTrade]) -> dict[str, Any]:
    n = len(trades)
    if n == 0:
        return {"trades": 0}
    wins = [t for t in trades if t.win]
    losses = [t for t in trades if not t.win]
    gross_win = sum(t.pnl_ccy for t in wins)
    gross_loss = -sum(t.pnl_ccy for t in losses)
    rs = [t.r_multiple for t in trades]
    return {
        "trades": n,
        "winrate": len(wins) / n,
        "expectancy_r": sum(rs) / n,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else float("inf"),
        "net_pnl": sum(t.pnl_ccy for t in trades),
        "avg_rr_planned": sum(t.rr_planned for t in trades) / n,
        "avg_win_r": (sum(t.r_multiple for t in wins) / len(wins)) if wins else 0.0,
        "avg_loss_r": (sum(t.r_multiple for t in losses) / len(losses)) if losses else 0.0,
    }


def compute_report(trades: Sequence[SimTrade], initial_capital: float, *, settings: Any,
                   signals_seen: int = 0, refusals: dict[str, int] | None = None,
                   label: str = "") -> dict[str, Any]:
    rep: dict[str, Any] = {"label": label, "initial_capital": initial_capital, **_stats(trades)}
    # Drawdown sur la courbe d'équité (trades clôturés)
    eq = initial_capital
    peak = eq
    max_dd = 0.0
    max_dd_pct = 0.0
    streak = max_streak = 0
    for t in trades:
        eq += t.pnl_ccy
        peak = max(peak, eq)
        dd = peak - eq
        if dd > max_dd:
            max_dd = dd
            max_dd_pct = 100 * dd / peak if peak > 0 else 100.0
        streak = streak + 1 if not t.win else 0
        max_streak = max(max_streak, streak)
    rep.update({"final_equity": eq, "max_drawdown": max_dd, "max_drawdown_pct": max_dd_pct,
                "max_losing_streak": max_streak, "signals_seen": signals_seen,
                "refusals": dict(sorted((refusals or {}).items(), key=lambda kv: -kv[1]))})

    def group(key) -> dict[str, Any]:
        g: dict[str, list[SimTrade]] = defaultdict(list)
        for t in trades:
            g[key(t)].append(t)
        return {k: _stats(v) for k, v in sorted(g.items())}

    rep["by_score"] = group(lambda t: score_bucket(t.score))
    rep["by_session"] = group(lambda t: session_of(t.entry_time))
    rep["by_setup"] = group(lambda t: setup_type(t.flags))
    rep["by_exit"] = group(lambda t: t.exit_reason)
    rep["validation"] = validate(rep, settings)
    rep["disclaimer"] = DISCLAIMER
    return rep


def validate(rep: dict[str, Any], s: Any) -> dict[str, Any]:
    """Critères AVANT tout trade réel (configurables). Tous doivent être vrais."""
    n = rep.get("trades", 0)
    wr = rep.get("winrate", 0.0)
    crit = {
        f"au moins {s.validation_min_trades} trades": n >= s.validation_min_trades,
        "expectancy > 0 après coûts": rep.get("expectancy_r", 0.0) > 0,
        f"winrate dans [{s.validation_winrate_min:.0%}, {s.validation_winrate_max:.0%}]":
            s.validation_winrate_min <= wr <= s.validation_winrate_max if n else False,
        f"drawdown max ≤ {s.validation_max_drawdown_pct:g} %": rep.get("max_drawdown_pct", 100.0) <= s.validation_max_drawdown_pct,
    }
    warnings = []
    if n and wr > s.suspicious_winrate:
        warnings.append(f"ALERTE : winrate simulé {wr:.0%} > {s.suspicious_winrate:.0%} — suspect "
                        "(biais d'anticipation, sur-optimisation, données) : vérifier avant toute conclusion.")
    if n < s.validation_min_trades:
        warnings.append(f"Échantillon insuffisant ({n} trades < {s.validation_min_trades}) : "
                        "aucune conclusion statistique possible.")
    return {"criteria": crit, "passed": all(crit.values()), "warnings": warnings}


def format_report(rep: dict[str, Any]) -> str:
    def pct(x): return f"{100 * x:.1f} %"

    def line(name: str, st: dict[str, Any]) -> str:
        if not st.get("trades"):
            return f"  {name:<32} 0 trade"
        pf = st["profit_factor"]
        return (f"  {name:<32} {st['trades']:>4} trades | winrate {pct(st['winrate']):>7} | "
                f"expectancy {st['expectancy_r']:+.2f} R | PF {'∞' if pf == float('inf') else f'{pf:.2f}'}")

    out = [f"===== RAPPORT DE BACKTEST {rep.get('label', '')} ====="]
    if not rep.get("trades"):
        out.append("Aucun trade simulé.")
    else:
        pf = rep["profit_factor"]
        out += [
            f"Trades : {rep['trades']}   Winrate : {pct(rep['winrate'])}   "
            f"Profit factor : {'∞' if pf == float('inf') else f'{pf:.2f}'}",
            f"Expectancy : {rep['expectancy_r']:+.3f} R / trade   RR moyen prévu : {rep['avg_rr_planned']:.2f}",
            f"Gain moyen : {rep['avg_win_r']:+.2f} R   Perte moyenne : {rep['avg_loss_r']:+.2f} R",
            f"P&L net : {rep['net_pnl']:+.2f}   Capital : {rep['initial_capital']:.2f} → {rep['final_equity']:.2f}",
            f"Drawdown max : {rep['max_drawdown']:.2f} ({rep['max_drawdown_pct']:.1f} %)   "
            f"Série max de pertes : {rep['max_losing_streak']}",
            "Par tranche de SCORE (le score est-il discriminant ?) :",
            *[line(k, v) for k, v in rep["by_score"].items()],
            "Par session (heure d'entrée) :",
            *[line(k, v) for k, v in rep["by_session"].items()],
            "Par type de setup :",
            *[line(k, v) for k, v in rep["by_setup"].items()],
            "Par raison de sortie :",
            *[line(k, v) for k, v in rep["by_exit"].items()],
        ]
    out.append(f"Signaux M1 évalués : {rep.get('signals_seen', 0)}")
    if rep.get("refusals"):
        out.append("Principales raisons de NO TRADE :")
        out += [f"  {v:>6} × {k}" for k, v in list(rep["refusals"].items())[:12]]
    v = rep["validation"]
    out.append("Critères de validation avant réel :")
    out += [f"  [{'OK' if ok else 'NON'}] {k}" for k, ok in v["criteria"].items()]
    out.append(f"  → {'VALIDÉ (passer ensuite au forward test DRY_RUN/démo)' if v['passed'] else 'NON VALIDÉ'}")
    out += [f"  ! {w}" for w in v["warnings"]]
    out.append(rep["disclaimer"])
    return "\n".join(out)


def merge_refusals(items: Iterable[str]) -> dict[str, int]:
    d: dict[str, int] = defaultdict(int)
    for x in items:
        d[x] += 1
    return dict(d)
