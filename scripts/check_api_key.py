"""Compare localement CAPITAL_API_KEY à une clé de référence, sans jamais l'afficher.

Rien n'est affiché, journalisé, écrit sur disque ni envoyé sur le réseau : ni les clés,
ni leurs empreintes, ni leurs fragments. Seul le verdict IDENTIQUES / DIFFÉRENTES sort,
avec des indices de format (longueur égale ou non, caractères invisibles).

Source de la clé de référence (au choix) :
  1. variable CAPITAL_API_KEY_CHECK (à ajouter temporairement dans les réglages de
     l'environnement cloud, puis à supprimer après le test) ;
  2. sinon, saisie masquée au clavier (getpass) si un terminal interactif est disponible.

Usage : python scripts/check_api_key.py
"""
from __future__ import annotations

import getpass
import hmac
import os
import sys


def _format(label: str, raw: str) -> list[str]:
    notes = []
    if raw != raw.strip():
        notes.append(f"{label} : espaces ou retours à la ligne en début/fin")
    if not raw.isascii():
        notes.append(f"{label} : caractères non ASCII (copier-coller suspect)")
    if not raw.strip().isalnum():
        notes.append(f"{label} : caractères non alphanumériques")
    return notes


def main() -> int:
    loaded = os.environ.get("CAPITAL_API_KEY", "")
    if not loaded.strip():
        print("CAPITAL_API_KEY absente de l'environnement chargé.")
        return 2
    ref = os.environ.get("CAPITAL_API_KEY_CHECK")
    source = "variable CAPITAL_API_KEY_CHECK"
    if not ref:
        if not sys.stdin.isatty():
            print("Aucune clé de référence : définir CAPITAL_API_KEY_CHECK ou lancer depuis un terminal.")
            return 2
        ref = getpass.getpass("Collez la clé « yungAS 2 » (saisie masquée) : ")
        source = "saisie masquée"
    if not ref.strip():
        print("Clé de référence vide.")
        return 2

    # Le programme utilise la valeur nettoyée (config._env fait .strip()).
    a, b = loaded.strip().encode(), ref.strip().encode()
    same = hmac.compare_digest(a, b)
    print(f"Référence : {source}")
    print(f"Longueurs identiques : {'oui' if len(a) == len(b) else 'non'}")
    for note in _format("CAPITAL_API_KEY", loaded) + _format("référence", ref):
        print("  ! " + note)
    print("VERDICT : " + ("IDENTIQUES — la clé chargée est bien celle de référence"
                          if same else "DIFFÉRENTES — la clé chargée n'est PAS celle de référence"))
    del loaded, ref, a, b
    return 0 if same else 1


if __name__ == "__main__":
    sys.exit(main())
