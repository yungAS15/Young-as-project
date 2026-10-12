# Stratégie BUY XAU/USD — définitions reproductibles

Tout ce qui suit est implémenté dans `strategy/` et testé dans `tests/test_strategy_primitives.py`
et `tests/test_signal_engine.py`. Les mêmes fonctions sont appelées en live et en backtest.

## Conventions de données

| Élément | Convention |
|---|---|
| Prix des bougies | **BID** (OHLC). ASK = BID + spread. |
| Heure | `time` = ouverture de la bougie, UTC. Une bougie n'est visible qu'après sa clôture. |
| Entrée BUY | à l'ASK. SL/TP d'un BUY déclenchés sur le BID. |
| Volume | Capital.com `lastTradedVolume` = **volume de ticks** (pas un volume échangé). |

## 1. Swing (fractale)

Swing high en `i` : `high[i]` **strictement** supérieur aux `k` highs précédents et aux `k` suivants
(k = 2 par défaut sur H4, M15, M1). Swing low : symétrique avec les lows.
Un swing n'est **confirmé** qu'à la clôture de la bougie `i + k` : avant, il n'existe pas pour le bot
(aucun lookahead). Deux sommets égaux ne forment pas de swing.

## 2. Biais H4 (porte obligatoire)

Sur les swings H4 confirmés et l'EMA 50 H4 :

- **BULLISH** : deux derniers swing highs croissants (HH) **et** deux derniers swing lows croissants (HL),
  clôture > dernier swing low → 2 points.
- **NEUTRAL_BULLISH** : (HL **ou** clôture > dernier swing high) **et** clôture > EMA50 **et**
  clôture > dernier swing low → 1 point.
- Sinon **BEARISH_OR_UNCLEAR** → **NO TRADE**. On ne trade jamais contre le biais H4.

## 3. Liquidité sell-side

Niveaux formés **et confirmés** avant la bougie de sweep, situés sous le prix :

| Type | Définition | « significatif » |
|---|---|---|
| `H4_SWING_LOW` | swing low H4 confirmé | oui |
| `EQUAL_LOWS` | ≥ 2 swing lows M15 à moins de 0,10 × ATR(M15) l'un de l'autre ; niveau = le plus bas | oui |
| `RANGE_LOW` | plus bas des 48 dernières M15 (12 h) | oui |
| `M15_SWING_LOW` | swing low M15 des 192 dernières bougies (2 jours) | non |

Un niveau n'est candidat que s'il n'a **pas déjà été pris** (aucun low inférieur entre sa formation et
le sweep). Point « zone de liquidité » du score : niveau balayé significatif.

## 4. Sweep (M15)

Bougie `s` dont le low passe sous un niveau `L`, puis réaction :
- **mèche de rejet** : la bougie `s` clôture au-dessus de `L` ; ou
- **clôture de réintégration** : une des 2 bougies suivantes clôture au-dessus de `L`, sans faire de
  plus bas que `s` entre-temps.

`sweep_low` = low de la bougie `s`. Le sweep le plus récent est retenu (à égalité : le niveau le plus
significatif).

## 5. CHOCH / MSS haussier (M15)

Niveau de structure = **dernier swing high confirmé avant le sweep** (le « lower high » de la jambe
baissière). CHOCH = première **clôture** M15 strictement au-dessus de ce niveau, dans les 12 bougies
(3 h) qui suivent le sweep, sans nouveau plus bas sous `sweep_low`.
Impulsion = de `sweep_low` au plus haut atteint depuis le sweep.
Le setup expire 16 M15 (4 h) après le CHOCH ; il est invalidé si le prix repasse sous `sweep_low`.

## 6. Zones d'intérêt

- **OTE Fibonacci** : retracement 0,618 – 0,786 de l'impulsion (`impulse_high - r × (high - low)`).
- **FVG haussier** : 3 bougies `i-2, i-1, i` avec `low[i] > high[i-2]` ; zone `[high[i-2], low[i]]` ;
  taille ≥ 0,10 × ATR(M15) ; invalidé si une clôture ultérieure passe sous le bas de la zone.
  Seuls les FVG formés depuis le sweep sont retenus.
- **Order Block** : dernière bougie baissière (close < open) entre la bougie de sweep (incluse) et la
  bougie de CHOCH (exclue) ; zone `[low, high]` ; invalidé par une clôture sous son low.

## 7. VWAP (confluence uniquement)

- Prix typique `(H+L+C)/3` des bougies **M15 closes**, pondéré par le volume de ticks.
- Jour de trading : commence à 22:00 UTC (après la pause quotidienne de l'or). Une bougie ouverte à `t`
  appartient au jour `date(t + 2 h)`.
- **Session** : reset à chaque jour de trading. **Hebdo** : reset au 1er jour de trading de la semaine
  ISO. **Mensuel** : reset au 1er jour de trading du mois.
- **Limite assumée** : l'or spot/CFD n'a pas de volume centralisé. Capital.com fournit un *volume de
  ticks* (activité de cotation). Le VWAP calculé est donc un **tick-VWAP**, une approximation affichée
  comme telle. Si < 90 % des bougies d'une période ont un volume > 0, ou si la période a < 4 bougies,
  le VWAP est déclaré **indisponible** (aucun faux niveau, aucun repli silencieux).
- Point VWAP du score : un VWAP disponible se trouve dans `[plus bas du repli − 0,25 ATR ; clôture de
  confirmation + 0,25 ATR]` (le prix est venu le chercher et a clôturé au-dessus).

## 8. Confirmation M1 (porte obligatoire)

Dans les 15 dernières M1 postérieures au CHOCH, le prix a touché au moins une zone (OTE, FVG ou OB), puis :
- **micro-CHOCH** : la dernière M1 est haussière et clôture au-dessus du plus haut des 5 M1
  précédentes ; ou
- **rejet** : la dernière M1 touche une zone, est haussière, mèche basse ≥ 2 × corps et clôture dans
  le tiers haut.

## 9. Entrée, SL, TP

- Entrée = clôture M1 de confirmation + spread (ASK). En backtest : ouverture de la M1 suivante + spread
  + slippage.
- **SL** = `sweep_low − (0,25 × ATR(M15) + spread)` (sous le point bas du sweep, marge volatilité + spread).
- **TP** candidats au-dessus de l'entrée : haut de l'impulsion, swing highs M15 (2 jours), swing highs H4,
  extensions Fibonacci 1,272 et 1,618. TP = niveau − spread. Seuls les TP à moins de 3 × ATR(H4) sont
  « raisonnables ».
  - `nearest_valid` (défaut) : le plus proche offrant RR ≥ 2.
  - `first_liquidity` : la première liquidité au-dessus doit offrir RR ≥ 2, sinon NO TRADE.
  - `prefer_target` : le plus proche offrant RR ≥ 3, sinon le plus proche ≥ 2.
- Aucun TP raisonnable à RR ≥ 2 → **NO TRADE**.

## 10. Score (sur 10, poids configurables)

| Élément | Poids | Attribution |
|---|---|---|
| Contexte H4 | 2 | 2 si BULLISH, 1 si NEUTRAL_BULLISH |
| Zone de liquidité | 1 | niveau balayé significatif |
| Sweep | 2 | porte obligatoire |
| Structure M15 (CHOCH) | 1 | porte obligatoire |
| FVG ou OB | 1 | zone FVG/OB touchée |
| VWAP | 1 | réaction sur un VWAP disponible |
| OTE Fibonacci | 1 | OTE touchée |
| Confirmation M1 | 1 | porte obligatoire |

Seuil par défaut : 7/10. Affichage « SCORE 8/10 » — **jamais un pourcentage de réussite**.
Le backtest mesure le winrate **par tranche de score** pour vérifier s'il est discriminant.

## 11. Ce qui n'est PAS fait (volontairement)

- Pas de SELL (architecture prête : `direction` est un paramètre partout ; le Risk Manager et le
  position sizing gèrent déjà SELL ; il faudra un module de détection symétrique).
- Pas de martingale, pas de ré-entrée sur le même setup (`used_setups` en base).
- Pas de « probabilité de gain ».
