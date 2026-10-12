# Connecter la clé API du compte RÉEL depuis un iPhone, en lecture seule

Objectif : vérifier la connexion LIVE et lire le solde. **Aucun ordre ne peut partir.**

## Où tourne le bot

Le bot n'a pas d'hébergement permanent. Il tourne dans une session cloud Claude Code : un conteneur
temporaire, recréé à chaque session à partir du dépôt GitHub `yungAS15/Young-as-project`, branche
`claude/xau-usd-trading-bot-v2-1xo9og`. C'est suffisant pour les tests. Pour faire tourner le bot en
continu, il faudra plus tard un hôte permanent : votre Mac ou un petit serveur.

## Où stocker les identifiants : variables de l'environnement cloud

Les identifiants sont saisis dans les **réglages de l'environnement** de Claude Code. Le bot les lit
au démarrage d'une session.
- Ils ne passent **jamais** par la conversation : ne les collez jamais dans le chat.
- Ils ne sont **jamais** écrits dans le dépôt ni affichés par le bot (`live-check` affiche
  seulement « présente » ou « ABSENTE »).
- Ils restent visibles pour quiconque a accès à cet environnement et sont lisibles par la session.
  Gardez l'environnement privé.

Il n'existe pas de clé « lecture seule » chez Capital.com. Votre clé permet donc de trader. Le bot
compense avec **trois verrous indépendants**, tous actifs par défaut :

| Verrou | Effet |
|---|---|
| `READ_ONLY=true` | Le client API refuse toute requête autre que GET **avant l'envoi**, sauf l'ouverture et la fermeture de session. |
| `DRY_RUN=true` | L'exécuteur simule les trades, sans aucun appel d'ordre. |
| `KILL_SWITCH=true` | Aucune exécution, même simulée. |

`live-check` force en plus la lecture seule, même si la configuration dit le contraire. Sur le compte
réel, la validation manuelle (`APPROVE <id>`) est obligatoire : `MODE=AUTO`, `AUTO_TRADING=true` ou
`MANUAL_APPROVAL_REQUIRED=false` font refuser le démarrage.

## Étapes sur iPhone

### 1. Donner l'accès GitHub (pour que le code survive à la session)
1. Ouvrez https://claude.ai/connect-github dans Safari et connectez votre compte GitHub.
2. Installez l'application **Claude** sur le dépôt `yungAS15/Young-as-project`, depuis la même page.
3. Revenez dans cette conversation et écrivez : « accès GitHub fait ». Je pousserai le code et
   vérifierai qu'il est bien en ligne.

### 2. Sécuriser la clé chez Capital.com (recommandé)
Dans l'app ou le site Capital.com : *Settings → API integrations*. Si ce n'est pas déjà fait, donnez
une **date d'expiration** courte à la clé (par exemple 30 jours). Vous pourrez la révoquer à tout
moment.

### 3. Saisir les variables dans l'environnement cloud
Dans l'app Claude, ouvrez la session Claude Code, touchez le **nom de l'environnement cloud** dans la
barre de titre, puis **Edit**. Dans **Environment variables**, ajoutez une ligne par variable :

```
CAPITAL_ENV=live
CAPITAL_API_KEY=<votre clé API>
CAPITAL_IDENTIFIER=<e-mail du compte Capital.com>
CAPITAL_PASSWORD=<mot de passe personnalisé de la clé API>
READ_ONLY=true
DRY_RUN=true
KILL_SWITCH=true
MODE=MANUAL_APPROVAL
ACCOUNT_CURRENCY=CHF
```

`CAPITAL_PASSWORD` est le **mot de passe choisi à la création de la clé API**, pas forcément celui de
votre compte. Enregistrez.

### 4. Démarrer une nouvelle session
Les variables ne s'appliquent qu'aux **nouvelles** sessions. Créez une session sur le dépôt
`yungAS15/Young-as-project` et la branche `claude/xau-usd-trading-bot-v2-1xo9og`, avec cet
environnement. Puis écrivez :

> Installe les dépendances (`bash scripts/setup_cloud.sh`) puis lance `python main.py live-check`.
> Ne lance aucun ordre.

### 5. Lire le résultat
`live-check` affiche :
- variables présentes ou absentes, sans jamais leur valeur ;
- authentification ;
- compte masqué (`***1234`), devise, solde et capital disponible ;
- spécifications GOLD lues depuis l'API, prix, spread et taux USD→CHF ;
- positions ouvertes et mode hedging ;
- risque adapté au capital : risque max par trade = min(100 CHF, 1 % du disponible), perte
  journalière max = min(150 CHF, 2 %), et taille calculée pour des SL de 1, 2 et 3 × ATR(M15).
  Si la taille minimale de Capital.com dépasse ce risque, le bot affiche NO TRADE.

La dernière ligne doit être : « Aucun ordre envoyé (client en lecture seule) ».

| Message | Cause probable |
|---|---|
| `Authentification refusée (HTTP 401)` | Mauvais `CAPITAL_IDENTIFIER` ou `CAPITAL_PASSWORD`, ou clé expirée ou désactivée. |
| `ABSENTE` | Variable mal nommée ou session démarrée avant l'enregistrement. |
| `Devise … ≠ ACCOUNT_CURRENCY` | Le compte actif n'est pas en CHF : ajustez `ACCOUNT_CURRENCY` ou changez de compte actif. |
| `Connexion impossible` | Le réseau de l'environnement bloque `api-capital.backend-capital.com`. |

### 6. Ensuite (toujours sans ordre réel)
- `python main.py analyze` : analyse du marché en direct, sans exécution.
- Forward test : `KILL_SWITCH=false` en gardant `DRY_RUN=true` et `READ_ONLY=true`. Les trades sont
  simulés et chaque proposition exige `APPROVE <id>`. Il faut pour cela un hôte qui tourne en
  continu.
- Tout passage en ordres réels reste soumis à `docs/SECURITY.md`, à `python main.py go-live-check`
  et à votre confirmation écrite.
