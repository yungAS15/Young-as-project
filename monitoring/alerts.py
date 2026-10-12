"""Notifications (console + base, Telegram optionnel).

Types d'événements : setup détecté, proposition, approuvé/refusé, exécuté, SL/TP touché,
erreur API, limite journalière, kill switch, bot arrêté, rapport quotidien.
Une notification qui échoue ne bloque jamais la logique de sécurité (et n'est jamais
considérée comme une approbation).
"""
from __future__ import annotations

import logging
from enum import Enum
from typing import Protocol

import requests

log = logging.getLogger(__name__)


class Event(str, Enum):
    SETUP_DETECTED = "SETUP_DETECTED"
    PROPOSAL = "PROPOSAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTED = "EXECUTED"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    SL_HIT = "SL_HIT"
    TP_HIT = "TP_HIT"
    POSITION_CLOSED = "POSITION_CLOSED"
    API_ERROR = "API_ERROR"
    DAILY_LIMIT = "DAILY_LIMIT"
    KILL_SWITCH = "KILL_SWITCH"
    BOT_STOPPED = "BOT_STOPPED"
    BOT_STARTED = "BOT_STARTED"
    DAILY_REPORT = "DAILY_REPORT"


class Channel(Protocol):
    def send(self, event: Event, text: str) -> None: ...


class ConsoleChannel:
    def send(self, event: Event, text: str) -> None:
        print(f"\n[{event.value}]\n{text}\n", flush=True)


class DatabaseChannel:
    def __init__(self, db) -> None:
        self.db = db

    def send(self, event: Event, text: str) -> None:
        level = "ERROR" if event in (Event.API_ERROR, Event.EXECUTION_FAILED) else "INFO"
        self.db.log_event(event.value, text[:4000], level)


class TelegramChannel:
    """Envoi seul (pas de lecture de commandes : l'approbation passe par CLI/web local)."""

    def __init__(self, token: str, chat_id: str, timeout: float = 10.0):
        self._token = token
        self.chat_id = chat_id
        self.timeout = timeout

    def send(self, event: Event, text: str) -> None:
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        try:
            requests.post(url, json={"chat_id": self.chat_id, "text": f"[{event.value}]\n{text}"[:4000]},
                          timeout=self.timeout)
        except requests.RequestException as exc:
            log.warning("Telegram indisponible (%s)", type(exc).__name__)


class Notifier:
    def __init__(self, channels: list[Channel] | None = None):
        self.channels = channels or [ConsoleChannel()]

    def notify(self, event: Event, text: str) -> None:
        for ch in self.channels:
            try:
                ch.send(event, text)
            except Exception:  # noqa: BLE001
                log.exception("Canal de notification en échec")


def build_notifier(settings, db=None, console: bool = True) -> Notifier:
    chans: list[Channel] = []
    if console:
        chans.append(ConsoleChannel())
    if db is not None:
        chans.append(DatabaseChannel(db))
    if settings.telegram_bot_token and settings.telegram_chat_id:
        chans.append(TelegramChannel(settings.telegram_bot_token, settings.telegram_chat_id))
    return Notifier(chans)
