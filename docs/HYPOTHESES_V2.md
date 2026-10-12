# Pré-enregistrement des hypothèses v2 (écrit AVANT les tests)

Date : 2026-10-12. Données : HistData XAU/USD M1 2018-01 → 2026-09, spread 0,40, slippage 0,10.
Référence v1, in-sample (IS) 2018–2022 : 219 trades, expectancy −0,181 R.

## Hypothèses (une option de configuration chacune ; les défauts = v1)

| # | Option | Règle |
|---|---|---|
| H1 | `CONFIRM_TF=M15` | Confirmation à la clôture d'une M15 post-CHOCH qui touche une zone, est haussière et clôture au-dessus du haut de la zone touchée la plus basse. |
| H2 | `REQUIRE_SIGNIFICANT_SWEEP=true` | Le niveau balayé doit être H4_SWING_LOW, EQUAL_LOWS ou RANGE_LOW (porte). |
| H3 | `H4_STRICT_BULLISH=true` + `REQUIRE_D1_BULLISH=true` | Biais H4 BULLISH uniquement, et D1 (reconstruit) au-dessus d'une EMA20 montante. |
| H4 | `ENTRY_MODE=limit` | Ordre limite BUY au retracement 0,705 de l'impulsion, placé dès le setup armé ; annulé à l'expiration du setup. |
| H5 | `TP_MODE=first_liquidity` | TP = première liquidité buy-side ; NO TRADE si elle n'offre pas RR ≥ 2. |

## Règle de décision (fixée maintenant)

1. Chaque hypothèse seule est testée **uniquement sur l'IS 2018–2022**.
2. Elle est **retenue** si son expectancy IS dépasse celle de v1 d'au moins **+0,10 R** **et** si elle
   produit au moins **80 trades** en IS.
3. La version v2 combine toutes les hypothèses retenues. Elle est testée en IS, puis **une seule
   fois** hors échantillon (OOS) sur 2023-01 → 2026-09.
4. Aucun paramètre (seuils, niveaux, poids) n'est modifié après avoir vu l'OOS.
5. Validation inchangée : au moins 100 trades OOS, expectancy > 0 après coûts, winrate 45–65 %,
   drawdown ≤ 20 %. On rapporte aussi l'intervalle de confiance à 95 % de l'expectancy.
6. Six configurations sont testées sur l'IS (5 hypothèses et la combinaison). Ce nombre d'essais
   sera rappelé avec les résultats, car il augmente le risque de faux positif.
