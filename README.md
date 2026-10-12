# Bot de trading semi-automatisé XAU/USD — Capital.com (v2)

Système **BUY uniquement** sur l'or (allowlist stricte `GOLD`), fondé sur la confluence
prise de liquidité → CHOCH → zone OTE/FVG/OB → VWAP → confirmation M1.
**Aucun ordre réel sans votre approbation explicite.** Le Risk Manager a toujours le dernier mot.

> ⚠️ Le trading avec effet de levier comporte un risque de perte élevé. Les résultats de backtest
> ne garantissent pas les résultats futurs. Ce logiciel ne constitue pas un conseil en investissement.

## Résultat actuel du backtest — à lire d'abord

Sur 2018 → 09/2026 (XAU/USD M1, 404 trades, coûts inclus) : **winrate 29 %, profit factor 0,90,
expectancy −0,06 R (IC 95 % [−0,20 ; +0,10]), drawdown max 40 %**. Hors échantillon 2023+ :
+0,08 R, non significatif. Le score n'est pas discriminant.
**La stratégie n'a pas d'avantage statistique démontré → ne pas passer en réel.**
Détails, méthode et ajustements proposés : [docs/BACKTEST_RESULTS.md](docs/BACKTEST_RESULTS.md).

## État de sécurité par défaut

```
DRY_RUN=true                    # aucun ordre n'est envoyé au broker
KILL_SWITCH=true                # aucune exécution (même simulée)
MANUAL_APPROVAL_REQUIRED=true   # APPROVE <id> obligatoire
AUTO_TRADING=false
CAPITAL_ENV=demo
```

Profil compte réel prêt à l'emploi : `.env.live.example`. Lancez `python main.py go-live-check` pour
la liste exacte de ce qui sera activé et le verdict GO/NO-GO. La stratégie n'étant pas validée, les
ordres réels restent refusés sans la ligne `LIVE_OVERRIDE_UNVALIDATED` (voir docs/SECURITY.md).

Un ordre **réel** exige en plus `CAPITAL_ENV=live`, `DRY_RUN=false`, `KILL_SWITCH=false` **et**
`LIVE_TRADING_ACK=J_ACCEPTE_LE_RISQUE_REEL`. Voir [docs/SECURITY.md](docs/SECURITY.md) pour la
checklist complète de passage en réel — à ne suivre **qu'après** validation backtest + forward test.

## Depuis un iPhone : connexion LIVE en lecture seule

Voir [docs/GUIDE_IPHONE_LIVE.md](docs/GUIDE_IPHONE_LIVE.md). Les identifiants sont saisis dans les
variables de l'environnement cloud, jamais dans la conversation. Ensuite :
`bash scripts/setup_cloud.sh && .venv/bin/python main.py live-check`. Le client est en lecture seule
(`READ_ONLY=true` par défaut) : aucun ordre ne peut partir.

## Installation (MacBook, macOS, zsh)

Vérifier d'abord ce qui est installé :

```zsh
python3 --version        # 3.10 ou plus requis
git --version
python3 -m venv --help >/dev/null && echo "venv OK"
```

Si `python3` manque ou est < 3.10 : installer Python depuis https://www.python.org/downloads/macos/
(ou `brew install python@3.12` si Homebrew est installé). Si `git` manque : `xcode-select --install`.

```zsh
git clone https://github.com/yungAS15/Young-as-project.git trading_bot
cd trading_bot
git checkout claude/xau-usd-trading-bot-v2-1xo9og
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env && chmod 600 .env
python main.py check          # vérifie Python, venv, modules, .env, configuration (secrets masqués)
pytest                        # tous les tests doivent passer
```

Si une commande échoue, lisez le message : `command not found: python3` → Python absent ;
`No module named …` → venv non activé (`source .venv/bin/activate`) ou dépendances non installées.

## Clé API Capital.com

Plateforme Capital.com → *Settings → API integrations → Generate new key* (2FA requis).
Choisir un mot de passe personnalisé pour la clé. Renseigner dans `.env` **uniquement** :
`CAPITAL_API_KEY`, `CAPITAL_IDENTIFIER` (e-mail du compte), `CAPITAL_PASSWORD` (mot de passe de la clé).
Commencez par une clé du **compte démo** (`CAPITAL_ENV=demo`). Il n'existe pas de clé « lecture seule »
chez Capital.com : traitez la clé comme un secret donnant accès au trading.

## Ordre de travail

| # | Commande | Effet |
|---|---|---|
| 1 | `python main.py check` | environnement + configuration |
| 2 | `python main.py init-db` | base SQLite (`var/trading_bot.sqlite`) |
| 3 | `python main.py connect` | connexion en lecture seule, solde, devise du compte |
| 4 | `python main.py instrument` | EPIC de l'or + spécifications **lues depuis l'API** (taille min/max, pas, distance de stop, horaires) |
| 5 | `python main.py fetch-history --tf M1 --days 90` | historique Capital.com (bid/ask + tick-volume) |
| 6 | `python main.py backtest --csv var/data/capital_m1.csv` | backtest IS/OOS (voir ci-dessous) |
| 7 | `python main.py analyze` | analyse immédiate : biais H4, liquidités, VWAP, setup éventuel |
| 8 | `python main.py run` | boucle du bot (propose, n'exécute qu'après APPROVE) |
| 9 | `python main.py web` | tableau de bord local http://127.0.0.1:8765 |
| 10 | `python main.py decide "APPROVE ab12cd34"` | décision en CLI (aussi REJECT / CANCEL) |

Autres : `proposals`, `status`, `kill` (kill switch immédiat, persistant en base), `unkill`.

### Approbation

Chaque proposition affiche : instrument, direction, prix, entrée, SL, TP, RR, taille, capital, risque
(CHF et %), trades du jour X/2, SCORE x/10, analyse H4/M15/M1, sweep/FVG/OB, VWAP, OTE, raisons du
trade/SL/TP, **conditions remplies et non remplies**. Seules `APPROVE <id>`, `REJECT <id>`,
`CANCEL <id>` sont acceptées ; « ok », « vas-y », « approve » sans id… sont ignorés.
L'approbation expire après `APPROVAL_TTL_S` (180 s), si le prix dérive de plus de
`APPROVAL_MAX_PRICE_DRIFT_POINTS`, si setup/SL/TP/taille/score changent, ou si le risque recalculé
augmente de plus de 10 %. Après APPROVE, tout est revérifié avant l'ordre.

## Données de backtest

Le backtest doit porter sur plusieurs années de M1 pour obtenir ≥ 100 trades.

```zsh
# Capital.com (référence, même flux que le live ; profondeur d'historique limitée par l'API)
python main.py fetch-history --tf M1 --days 365 --out var/data/capital_m1.csv
# HistData.com : XAU/USD M1 BID depuis 2018 (sans volume → VWAP indisponibles)
python scripts/download_histdata.py 2018 2026 var/data/xauusd_m1_histdata.csv
# Dukascopy : XAU/USD M1 BID + tick-volume (lent : limitation de débit, reprise automatique)
python scripts/download_dukascopy.py 2024-01-01 2026-10-01 var/data/xauusd_m1.csv

python main.py backtest --csv var/data/xauusd_m1_histdata.csv --spread 0.40 --trades
python main.py backtest --csv var/data/xauusd_m1_histdata.csv --spread 0.40 --walk-forward
```

Le rapport donne : winrate, profit factor, expectancy (R), drawdown max, nombre de trades, RR moyen,
série max de pertes, résultats **par tranche de score**, par session, par type de setup, critères de
validation (≥ 100 trades, expectancy > 0 après coûts, winrate 45–65 %, drawdown ≤ 20 %) et une
**alerte si le winrate dépasse 75 %** (suspect). Résultats et conclusions : [docs/BACKTEST_RESULTS.md](docs/BACKTEST_RESULTS.md).

## Architecture

```
main.py  config.py  requirements.txt  .env.example
broker/      capital_api.py (REST officiel), models.py
data/        candles.py, database.py (SQLite), market_data.py (live), sources.py (historique)
strategy/    market_structure, liquidity, vwap, fibonacci, fvg, order_blocks, scoring, signal_engine
risk/        position_sizing.py, risk_manager.py
approval/    trade_approval.py
execution/   trade_executor.py (exécution + surveillance)
backtest/    engine.py, metrics.py, walkforward.py
monitoring/  logger.py (masquage des secrets), alerts.py (console/base/Telegram), daily_report.py, macro_calendar.py
interface/   runner.py (boucle live), web.py (tableau de bord local)
tests/       tests unitaires (pytest)
docs/        STRATEGY.md, SECURITY.md, BACKTEST_RESULTS.md
```

Le serveur MCP Capital.com n'est pas nécessaire : l'exécution passe par le client Python direct.

## Modes

1. `ANALYSIS_ONLY` : analyse et propositions, aucune exécution possible.
2. `MANUAL_APPROVAL` (défaut) : approbation obligatoire.
3. `AUTO` : exige `MODE=AUTO`, `MANUAL_APPROVAL_REQUIRED=false` **et** `AUTO_TRADING=true`. Non
   recommandé avant validation complète (backtest + forward test + historique réel).
