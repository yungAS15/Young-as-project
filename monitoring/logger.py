"""Journalisation avec masquage automatique des secrets."""
from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Iterable

_PATTERNS = [
    re.compile(r"(?i)(X-CAP-API-KEY|CST|X-SECURITY-TOKEN|password|identifier|token)([\"']?\s*[:=]\s*[\"']?)([^\s\"',}]+)"),
]


class SecretFilter(logging.Filter):
    """Remplace toute occurrence d'un secret connu par *** dans les messages."""

    def __init__(self, secrets: Iterable[str] = ()):
        super().__init__()
        self.secrets = [s for s in secrets if s and len(s) >= 4]

    def scrub(self, text: str) -> str:
        for s in self.secrets:
            text = text.replace(s, "***")
        for p in _PATTERNS:
            text = p.sub(lambda m: m.group(1) + m.group(2) + "***", text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        record.msg = self.scrub(msg)
        record.args = ()
        return True


def setup_logging(log_path: str | Path | None, secrets: Iterable[str] = (), level: int = logging.INFO) -> SecretFilter:
    flt = SecretFilter(secrets)
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.addFilter(flt)
    root.addHandler(console)
    if log_path:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(log_path, maxBytes=5_000_000, backupCount=5)
        fh.setFormatter(fmt)
        fh.addFilter(flt)
        root.addHandler(fh)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return flt
