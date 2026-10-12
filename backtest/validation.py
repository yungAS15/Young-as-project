"""Statut de validation de la stratégie (porte du passage en réel).

Écrit après un backtest hors échantillon. Lu par `config.load_settings` (fail-closed :
fichier absent = non validé).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import PROJECT_ROOT, VALIDATION_STATUS_PATH


def write_validation_status(report: dict[str, Any], *, source: str, params: dict[str, Any],
                            path: str | Path | None = None) -> dict[str, Any]:
    v = report.get("validation", {})
    status = {
        "passed": bool(v.get("passed", False)),
        "date": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "label": report.get("label"),
        "trades": report.get("trades", 0),
        "winrate": report.get("winrate"),
        "expectancy_r": report.get("expectancy_r"),
        "max_drawdown_pct": report.get("max_drawdown_pct"),
        "criteria": v.get("criteria", {}),
        "strategy_params": params,
    }
    p = Path(path) if path else PROJECT_ROOT / VALIDATION_STATUS_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(status, indent=2, default=str, ensure_ascii=False))
    return status
