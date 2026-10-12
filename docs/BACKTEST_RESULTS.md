# Résultats de backtest — conclusions honnêtes

> **Les performances passées, réelles ou simulées, ne garantissent pas les performances futures.**

## Verdict

**Dans sa version actuelle, la stratégie n'a PAS d'avantage statistique démontré.**
Elle ne remplit pas les critères de validation. **Aucun passage en réel n'est recommandé.**
On ne passe pas non plus au forward test démo, sauf pour tester la mécanique d'exécution.

| Période (paramètres par défaut) | Trades | Winrate | Profit factor | Expectancy | IC 95 % de l'expectancy | DD max |
|---|---|---|---|---|---|---|
| In-sample 2018–2022 | 219 | 25,6 % | 0,75 | −0,181 R | [−0,36 ; +0,02] | 35,7 % |
| Out-of-sample 2023 → 09/2026 | 185 | 33,5 % | 1,14 | +0,084 R | [−0,14 ; +0,32] | 13,5 % |
| **Période complète 2018 → 09/2026** | **404** | **29,2 %** | **0,90** | **−0,056 R** | **[−0,20 ; +0,10]** | **39,7 %** |
| Walk-forward (OOS concaténés, 14 fenêtres) | 220 | 26,8 % | 0,87 | −0,098 R | — | 44,6 % |
| Dukascopy 2024 → 10/2026, **avec VWAP** | 140 | 32,9 % | 1,05 | +0,049 R | — | 15,0 % |

Lecture :
- Le gain moyen est d'environ +2,3 R et la perte moyenne d'environ −1,03 R. Le winrate d'équilibre
  est donc d'environ **30 %**. Le winrate mesuré (25–34 %) **tourne autour de ce point mort**.
- L'expectancy est **statistiquement indiscernable de zéro** : l'intervalle de confiance contient 0
  sur chaque période. Le résultat positif hors échantillon (2023+) n'est pas significatif
  (t ≈ 0,7), et l'in-sample est négatif.
- **Sans aucun coût** (spread 0, slippage 0), l'expectancy n'est que de +0,045 R (PF 1,09). Les règles
  d'entrée elles-mêmes n'apportent presque rien. Avec un spread de 0,80, on tombe à −0,174 R.
- **Le winrate visé de 45–65 % n'est jamais approché.**

## Le score n'est pas discriminant

| Tranche | Complet 2018–2026 | OOS 2023+ | Dukascopy + VWAP |
|---|---|---|---|
| 7–7,9 | 172 tr. · 31,4 % · +0,03 R | 78 · 32,1 % · +0,02 R | 36 · 36,1 % · +0,19 R |
| 8–8,9 | 184 · 28,3 % · −0,10 R | 87 · 37,9 % · +0,25 R | 54 · 33,3 % · +0,03 R |
| 9–10 | 48 · 25,0 % · −0,17 R | 20 · 20,0 % · −0,36 R | 50 · 30,0 % · −0,04 R |

Un score plus élevé ne donne **pas** de meilleurs résultats. Dans cette implémentation, les
confluences FVG, OB, OTE et VWAP n'ajoutent pas d'information mesurable. C'est exactement pour cela
que le score n'est jamais présenté comme une probabilité.

## Autres constats

- **Optimisation in-sample** (grille de 8 combinaisons : score min 7/8, marge SL 0,25/0,5 ATR, mode
  TP) : **toutes les combinaisons sont négatives en in-sample**, de −0,16 à −0,37 R. La « meilleure »
  (score 7, SL 0,5 ATR) donne +0,034 R hors échantillon, moins que les paramètres par défaut. Il n'y a
  rien de robuste à optimiser.
- **Walk-forward** : 9 fenêtres sur 14 sont négatives hors échantillon, et les paramètres choisis
  changent d'une fenêtre à l'autre. Pas de stabilité.
- **Break-even à 1R** : −0,110 R. **Clôture partielle à 1R** : −0,100 R. Les deux options sont
  **pires** que la gestion simple (−0,056 R) et doivent **rester désactivées**.
- **Sessions** : Londres et l'overlap New York sont négatifs sur les périodes récentes. Mais l'Asie
  est positive en OOS et négative en IS. C'est **incohérent**, donc on n'en tire pas de filtre
  (ce serait de la sur-optimisation).
- **Fréquence** : environ 45 trades par an, soit moins d'un par semaine. Le plafond de 2 par jour
  n'est presque jamais atteint. La première cause de non-trade est « setup expiré » : après le CHOCH,
  le prix ne revient pas dans la zone en moins de 4 h.
- **Série de pertes** : jusqu'à 23 pertes consécutives sur la période complète. Au risque de 1 % par
  trade, le drawdown dépasse 35 %.
- **Alerte winrate > 75 %** : jamais déclenchée. Aucun signe de biais d'anticipation.

## Méthode

- **Données principales** : XAU/USD M1 BID, HistData.com, 01/2018 → 09/2026, soit 3,05 M de bougies.
  Les données ont été vérifiées identiques à Dukascopy sur les jours communs, en hiver comme en été
  (heure de New York avec heure d'été, convertie en UTC). Cette source **n'a pas de volume**, donc
  les VWAP sont « indisponibles » : le point VWAP n'est jamais attribué et le score maximum est 9/10.
- **Données VWAP** : Dukascopy XAU/USD M1 BID avec tick-volume, 01/2024 → 10/2026, soit 985 k de
  bougies.
- **Protocole fixé avant de regarder les résultats hors échantillon** :
  1. in-sample 2018–2022 seul pour choisir les paramètres ;
  2. un seul passage hors échantillon (2023+) avec les paramètres par défaut pré-enregistrés et avec
     les meilleurs paramètres IS ;
  3. walk-forward : 2 ans d'optimisation, puis 6 mois de test, en glissant.
- **Coûts** : spread fixe de 0,40 USD et slippage de 0,10 USD à l'entrée et au SL. Pas de commission.
  **Swap overnight non simulé** : la réalité serait légèrement pire pour les positions tenues la nuit.
- **Exécution simulée** : entrée à l'ouverture de la M1 suivant la confirmation, à l'ASK plus le
  slippage. Si le SL et le TP sont touchés dans la même M1, on compte le SL (hypothèse pessimiste).
  L'approbation est supposée instantanée (hypothèse optimiste).
- **Même code que le live** : `SignalEngine` et `RiskManager` sont identiques. Les M15 et H4 sont
  reconstruites depuis les M1, et seules les bougies closes sont visibles (testé).
- **Risque** : capital de 10 000 CHF, donc risque = min(100 CHF, 1 %) = 100 CHF. Les R-multiples ne
  dépendent pas de ce choix.
- **Limites** : les données de mars à juillet 2023 sont plus clairsemées (~20 k bougies par mois au
  lieu de ~29 k). Le spread réel de Capital.com sur l'or reste à mesurer : `fetch-history` récupère
  le bid et l'ask réels.
- Rapports bruts : [`docs/backtest_reports/`](backtest_reports/). Pour reproduire :
  `python scripts/download_histdata.py 2018 2026`, puis `python scripts/research.py
  var/data/xauusd_m1_histdata.csv var/reports` (environ 1 h sur 4 cœurs).

## Propositions d'ajustement (sans sur-optimisation)

Chaque proposition est **une hypothèse unique, motivée par la logique du marché**. Il ne s'agit
pas de chercher le meilleur chiffre. Procédure obligatoire pour chacune :

1. la formuler par écrit ;
2. la tester **uniquement sur 2018–2022** ;
3. si elle améliore nettement l'in-sample, la passer **une seule fois** sur 2023+ ;
4. abandonner sinon.

Tester une hypothèse après l'autre sur les mêmes données revient à sur-optimiser. Il faut limiter
le nombre d'essais et les compter.

| # | Hypothèse | Pourquoi |
|---|---|---|
| H1 | **Confirmation sur M15** au lieu du micro-CHOCH M1 : clôture M15 haussière qui réintègre la zone | La confirmation M1 est très bruitée. 70 % des trades finissent au SL, souvent après un nouveau balayage proche. |
| H2 | **Sweep d'un niveau significatif obligatoire** (H4, equal lows, bas de range) : porte, pas point de score | Le score n'étant pas discriminant, il vaut mieux garder peu de portes fortes que beaucoup de points faibles. |
| H3 | **Biais H4 strictement BULLISH** (le neutre-haussier est exclu) **et D1 haussier** | Réduit les trades contre une structure supérieure fragile. |
| H4 | **Entrée limite dans la zone** (ex. niveau 0,705 de l'OTE ou milieu du FVG) au lieu d'une entrée au marché après confirmation | Améliore le RR réel et met le SL plus près de l'entrée, mais réduit le taux de remplissage. À mesurer. |
| H5 | **TP sur la première liquidité opposée** (`TP_MODE=first_liquidity`, jamais testé en grille) | Des TP plus proches et plus souvent atteints augmentent le winrate. Le RR ≥ 2 reste exigé. |

Si aucune hypothèse ne donne une expectancy hors échantillon **significativement** positive
(IC 95 % au-dessus de 0, au moins 100 trades), la conclusion honnête sera : **cette famille de setups
n'a pas d'avantage exploitable sur l'or M1/M15 avec ces coûts**.

## Ce qui reste valable

L'infrastructure ne dépend pas de la stratégie : risque, approbation, exécution vérifiée, journal,
backtest sans lookahead et métriques par score. Elle sert à tester rigoureusement toute autre
version de la stratégie.

## Hypothèses v2 : résultat du test pré-enregistré (2026-10-12)

Protocole : [HYPOTHESES_V2.md](HYPOTHESES_V2.md), commité **avant** l'exécution. Chaque hypothèse
est testée sur l'IS 2018–2022. Référence v1 : 219 trades, −0,181 R. Seuil de rétention :
amélioration d'au moins +0,10 R et au moins 80 trades.

| Hypothèse | Trades IS | Winrate | Expectancy IS | IC 95 % | Retenue ? |
|---|---|---|---|---|---|
| H1 confirmation M15 | 174 | 26,4 % | −0,099 R | [−0,33 ; +0,14] | non (+0,08 R < +0,10) |
| H2 sweep significatif obligatoire | 143 | 25,2 % | −0,212 R | [−0,44 ; +0,02] | non (pire) |
| H3 H4 strict + D1 haussier | 79 | 20,3 % | −0,272 R | [−0,60 ; +0,08] | non (pire) |
| H4 ordre limite à 0,705 | 167 | 27,5 % | −0,108 R | [−0,33 ; +0,12] | non (+0,07 R < +0,10) |
| H5 TP sur la première liquidité | 1 | — | — | — | non (la 1re liquidité n'offre presque jamais 2R) |

**Aucune hypothèse n'est retenue. Il n'y a donc pas de version v2 à tester hors échantillon.**
H1 et H4 vont dans le bon sens, mais restent négatives et sous le seuil fixé à l'avance. Abaisser
le seuil après coup, ou les passer sur l'OOS « pour voir », serait de la sur-optimisation.
Les options restent disponibles dans la configuration (désactivées), pour un futur test sur de
nouvelles données.

Statut de validation (`var/validation_status.json`) : **NON VALIDÉ**. Le meilleur résultat obtenu
est l'OOS v1 2023+ : +0,08 R, non significatif, avec un winrate de 33,5 % hors de la plage 45–65 %.
