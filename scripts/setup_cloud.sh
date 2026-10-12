#!/usr/bin/env bash
# Installation rapide dans une session cloud (ou sur Mac) : venv + dépendances + tests.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m venv .venv
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
.venv/bin/python -m pytest -q
echo "Prêt. Exemple : .venv/bin/python main.py live-check"
