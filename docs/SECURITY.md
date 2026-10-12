# Sécurité, protections et passage en réel

## Protections (toutes testées)

| Protection | Où | Test |
|---|---|---|
| DRY_RUN / KILL_SWITCH / approbation manuelle par défaut | `config.py` | `test_default_state_is_safe` |
| Configuration ambiguë ou dangereuse refusée au démarrage (booléen flou, MODE inconnu, >2 trades/j, RR<2, risque >5 %, AUTO incohérent) | `config.py` | `test_invalid_or_ambiguous_config_refused` |
| Allowlist d'instrument (GOLD) côté client API **et** Risk Manager | `broker/capital_api.py`, `risk/risk_manager.py` | `test_allowlist_blocks_other_epics`, `test_instrument_allowlist` |
| BUY uniquement (SELL refusé par `ALLOWED_DIRECTIONS`) | Risk Manager | `test_sell_not_allowed_by_default` |
| Risque = min(100 CHF, 1 % du capital), taille arrondie vers le bas | `risk/position_sizing.py` | `test_position_sizing.py` |
| Taille min broker > risque autorisé → NO TRADE | idem | `test_min_size_exceeds_risk_is_no_trade` |
| Conversion USD→CHF obligatoire (absente → NO TRADE) | idem + `data/market_data.py` | `test_missing_fx_rate_is_no_trade` |
| Spécifications instrument lues depuis l'API ; ambiguïté (scalingFactor ≠ 1…) → NO TRADE | `broker/models.py` | `test_ambiguous_instrument_refused` |
| Max 2 trades/jour (compteur persistant SQLite) | Risk Manager + `data/database.py` | `test_two_trades_per_day_limit`, `test_daily_counter_persists_across_restarts` |
| Perte journalière max (CHF et %) ; le budget restant réduit la taille | Risk Manager | `test_daily_loss_limit_blocks` |
| 1 position ouverte max (base ET broker) | Risk Manager | `test_one_open_position_max`, `test_open_position_blocks_second_trade` |
| Une seule entrée par signal | `used_setups` | `test_one_entry_per_setup`, `test_setup_used_only_once` |
| Spread anormal, données/prix périmés, marché fermé, connexion perdue, erreurs API consécutives | Risk Manager | `test_spread_too_wide`, `test_stale_data_and_quote`, `test_market_closed_and_connection_lost` |
| SL du mauvais côté / sous la distance minimale broker ; RR < 2 | Risk Manager | `test_sl_*`, `test_rr_below_two_refused` |
| Pause macro optionnelle, fail-closed si calendrier absent/périmé | `monitoring/macro_calendar.py` | `test_macro_calendar`, `test_macro_pause_fail_closed` |
| Erreur interne du Risk Manager → NO TRADE | Risk Manager | `test_internal_error_is_no_trade` |
| Formulations ambiguës ≠ approbation | `approval/trade_approval.py` | `test_ambiguous_commands_are_not_approvals` (19 cas) |
| Expiration (temps, prix, setup/SL/TP/taille/score, hausse du risque) ; usage unique | idem + exécuteur | `test_expiry_*`, `test_approve_then_claim_once`, `test_reverification_fails_when_capital_drops` |
| Revérification complète après APPROVE | `execution/trade_executor.py` | `test_executor.py` |
| Pas de retry automatique sur un ordre (anti-doublon) | client API | `test_no_retry_on_order_network_error` |
| Succès HTTP ≠ exécution : confirmation + lecture de la position ; SL/TP absents → fermeture + kill switch | exécuteur | `test_real_order_path_verified`, `test_missing_sl_closes_position_and_trips_kill_switch` |
| Compte réel : phrase `LIVE_TRADING_ACK` exigée | `config.py` | `test_live_requires_ack_phrase`, `test_live_account_requires_ack` |
| Secrets jamais affichés (repr, logs, erreurs API) | `config.py`, `monitoring/logger.py`, client | `test_secrets_never_in_repr_or_public_dict`, `test_log_filter_redacts_secrets`, `test_login_failure_does_not_leak_secrets` |
| Interface web liée à 127.0.0.1, jeton anti-CSRF, prix frais exigé | `interface/web.py` | `test_web_refuses_remote_bind`, `test_web_render_and_price_freshness` |
| Pas de martingale : le risque ne dépend jamais des pertes précédentes (seulement réduit par le budget journalier) | sizing | par construction |

## Kill switch

- `KILL_SWITCH=true` dans `.env` (défaut) **ou** `python main.py kill` (persistant en base, effet immédiat).
- Activé automatiquement si : erreur réseau à l'envoi d'un ordre, confirmation introuvable, position
  introuvable après acceptation, SL/TP non attachés.
- `python main.py unkill` ne lève que le kill switch runtime ; celui du `.env` reste prioritaire.

## Étapes avant tout trade réel

1. Backtest multi-années **validé** (≥ 100 trades, expectancy > 0 après coûts, winrate plausible
   45–65 %, drawdown acceptable) **sur la période hors échantillon**, pas seulement in-sample.
2. Forward test de plusieurs semaines en `DRY_RUN=true` avec `KILL_SWITCH=false` (aucun ordre n'est
   envoyé ; les trades sont simulés sur les prix live et journalisés).
3. Test d'exécution sur **compte démo** : `CAPITAL_ENV=demo`, `DRY_RUN=false`, `KILL_SWITCH=false`.
   Vérifier dans la plateforme : taille, SL et TP attachés, P&L en CHF cohérent avec le calcul du bot
   (la valeur du point = `lotSize` est une hypothèse à confirmer ici).
4. Seulement ensuite, sur votre confirmation écrite explicite, la liste précise de ce qui sera activé :
   - `CAPITAL_ENV=live` (URL https://api-capital.backend-capital.com, compte réel)
   - `DRY_RUN=false` (ordres réels envoyés)
   - `KILL_SWITCH=false`
   - `LIVE_TRADING_ACK=J_ACCEPTE_LE_RISQUE_REEL`
   - inchangés : `MODE=MANUAL_APPROVAL`, `MANUAL_APPROVAL_REQUIRED=true`, `AUTO_TRADING=false`,
     risque min(100 CHF, 1 %), 2 trades/jour, 1 position, perte journalière max.

## Profil compte réel (outillage prêt, NON activé)

- `.env.live.example` : profil réel pré-rempli (`CAPITAL_ENV=live`, `DRY_RUN=false`, `KILL_SWITCH=false`,
  `LIVE_TRADING_ACK`). Les protections restent inchangées.
- `python main.py go-live-check` : affiche **exactement** ce qui sera activé, le statut de validation,
  le forward test DRY_RUN, les trades démo, puis vérifie l'API (devise du compte, instrument,
  positions ouvertes, hedgingMode). Rend un verdict GO / NO-GO. Cette commande n'active rien.
- **Porte de validation** : tant que `var/validation_status.json` (écrit par `main.py backtest`)
  n'indique pas `passed: true`, les ordres réels sont refusés, sauf si **vous** ajoutez
  `LIVE_OVERRIDE_UNVALIDATED=J_ACCEPTE_UNE_STRATEGIE_NON_VALIDEE`. Une formulation approchante est
  refusée (testé).
- **Confirmation au démarrage** : avec des ordres réels, `python main.py run` affiche la liste des
  activations et exige de taper `ORDRES REELS` dans un terminal interactif.
- Ensuite, chaque trade exige toujours `APPROVE <id>` et une revérification complète.
