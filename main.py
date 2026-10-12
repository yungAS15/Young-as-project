"""Point d'entrée CLI du bot XAU/USD.

Exemples :
    python main.py check                    # environnement + configuration (sans secrets)
    python main.py init-db
    python main.py connect                  # test de connexion (lecture seule)
    python main.py instrument               # EPIC or + spécifications depuis l'API
    python main.py fetch-history --tf M1 --days 60 --out var/data/capital_m1.csv
    python main.py backtest --csv var/data/xauusd_m1.csv [--walk-forward]
    python main.py analyze                  # une analyse immédiate (aucune exécution)
    python main.py run                      # boucle du bot (DRY_RUN par défaut)
    python main.py web                      # tableau de bord local http://127.0.0.1:8765
    python main.py decide "APPROVE ab12cd34"
    python main.py proposals | status | kill | unkill
"""
from __future__ import annotations

import argparse
import json
import platform
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import PROJECT_ROOT, ConfigError, Settings, load_settings, resolve_path


def _settings() -> Settings:
    try:
        return load_settings()
    except ConfigError as exc:
        sys.exit(f"Configuration invalide : {exc}")


def _db(s: Settings):
    from data.database import Database
    return Database(resolve_path(s.db_path), s.trading_timezone)


def _client(s: Settings):
    from broker.capital_api import build_client
    if not s.has_credentials:
        sys.exit("Identifiants Capital.com absents : remplir CAPITAL_API_KEY / CAPITAL_IDENTIFIER / "
                 "CAPITAL_PASSWORD dans .env")
    return build_client(s)


def _logging(s: Settings) -> None:
    from monitoring.logger import setup_logging
    setup_logging(resolve_path(s.log_path), s.secrets)


# ---------------------------------------------------------------------- commandes
def cmd_check(args) -> None:
    ok = True
    print(f"Python : {platform.python_version()} ({sys.executable})")
    if sys.version_info < (3, 10):
        print("  ✘ Python 3.10+ requis")
        ok = False
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    print(f"Environnement virtuel : {'oui' if in_venv else 'NON (recommandé : .venv)'}")
    for mod in ("requests", "dotenv", "pytest"):
        try:
            __import__(mod)
            print(f"  ✔ module {mod}")
        except ImportError:
            print(f"  ✘ module {mod} manquant → pip install -r requirements.txt")
            ok = False
    env = PROJECT_ROOT / ".env"
    print(f".env : {'présent' if env.exists() else 'ABSENT (cp .env.example .env)'}")
    if env.exists() and (env.stat().st_mode & 0o077):
        print("  ! .env lisible par d'autres utilisateurs → chmod 600 .env")
    s = _settings()
    print("Configuration (secrets masqués) :")
    pub = s.public_dict()
    for k in ("mode", "dry_run", "kill_switch", "manual_approval_required", "auto_trading", "capital_env",
              "capital_api_key", "capital_identifier", "capital_password", "allowed_epics", "account_currency",
              "max_risk_per_trade_chf", "max_risk_per_trade_pct", "max_daily_loss_chf", "max_daily_loss_pct",
              "max_trades_per_day", "max_spread_points"):
        print(f"  {k} = {pub[k]}")
    print(f"  min_score = {s.strategy.min_score}  min_rr = {s.strategy.min_rr}  tp_mode = {s.strategy.tp_mode}")
    allowed, reasons = s.execution_allowed()
    print(f"Exécution d'ordres possible : {'OUI' if allowed else 'NON'}" + (f" ({'; '.join(reasons)})" if reasons else ""))
    if s.dry_run:
        print("DRY_RUN=true : aucun ordre ne sera envoyé au broker.")
    sys.exit(0 if ok else 1)


def cmd_init_db(args) -> None:
    s = _settings()
    db = _db(s)
    print(f"Base initialisée : {db.path}")


def cmd_connect(args) -> None:
    s = _settings()
    _logging(s)
    c = _client(s)
    print(f"Environnement : {s.capital_env.upper()} ({s.base_url})")
    print(f"Heure serveur : {c.server_time()}")
    info = c.login()
    print(f"Session ouverte. Devise du compte : {info['account_currency']}")
    for a in c.get_accounts():
        print(f"  compte ***{a.account_id[-4:]} {a.currency} solde {a.balance:.2f} disponible {a.available:.2f}")
    if info["account_currency"] != s.account_currency:
        print(f"  ! ATTENTION : devise du compte {info['account_currency']} ≠ ACCOUNT_CURRENCY={s.account_currency}")
    c.logout()


def cmd_instrument(args) -> None:
    s = _settings()
    _logging(s)
    c = _client(s)
    c.login()
    print(f"Recherche '{s.instrument_search_term}' :")
    for m in c.search_markets(s.instrument_search_term)[:15]:
        flag = "  ← allowlist" if m.get("epic") in s.allowed_epics else ""
        print(f"  {m.get('epic'):<14} {m.get('instrumentName'):<30} {m.get('instrumentType'):<12} "
              f"{m.get('marketStatus')}{flag}")
    inst = c.get_instrument(s.allowed_epics[0])
    print(f"\nSpécifications {inst.epic} ({inst.name}) — lues depuis l'API :")
    for k in ("currency", "lot_size", "min_deal_size", "max_deal_size", "size_increment", "min_stop_distance",
              "min_stop_distance_unit", "margin_factor", "margin_factor_unit", "market_status", "scaling_factor",
              "bid", "offer"):
        print(f"  {k} = {getattr(inst, k)}")
    print(f"  opening_hours = {json.dumps(inst.opening_hours)}")
    amb = inst.ambiguities()
    print("Ambiguïtés : " + ("; ".join(amb) if amb else "aucune"))
    out = resolve_path(f"var/instrument_{inst.epic}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({**inst.__dict__}, default=str, indent=2))
    print(f"Enregistré : {out}")
    c.logout()


def cmd_fetch_history(args) -> None:
    from data.candles import save_csv
    s = _settings()
    _logging(s)
    c = _client(s)
    c.login()
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days)
    candles = c.get_candles_range(s.allowed_epics[0], args.tf, start, end)
    save_csv(candles, args.out)
    vol0 = sum(1 for x in candles if x.volume <= 0)
    print(f"{len(candles)} bougies {args.tf} → {args.out} (de {candles[0].time if candles else '-'} "
          f"à {candles[-1].time if candles else '-'}) ; bougies sans volume : {vol0}")
    c.logout()


def cmd_backtest(args) -> None:
    from backtest.engine import BacktestEngine
    from backtest.metrics import format_report
    from data.candles import load_csv
    s = _settings()
    if args.spread is not None:
        from dataclasses import replace
        s = replace(s, backtest_default_spread=args.spread)
    m1 = load_csv(args.csv, s.backtest_default_spread)
    if args.start:
        m1 = [c for c in m1 if c.time >= datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)]
    if args.end:
        m1 = [c for c in m1 if c.time < datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)]
    if not m1:
        sys.exit("Aucune donnée")
    print(f"{len(m1)} bougies M1 de {m1[0].time} à {m1[-1].time}")
    if args.walk_forward:
        from backtest.walkforward import walk_forward
        wf = walk_forward(s, m1, args.train_days, args.test_days)
        for f in wf["folds"]:
            print(f"  IS {f['is']} → OOS {f['oos']} params {f['params']} : {f['oos_trades']} trades, "
                  f"expectancy {f['oos_expectancy_r']}")
        print(format_report(wf["oos_report"]))
        return
    from backtest.walkforward import split_is_oos
    t0, cut, t1 = split_is_oos(m1, args.is_fraction)
    eng = BacktestEngine(s)
    is_res = eng.run(m1, label=f"IN-SAMPLE {t0.date()}→{cut.date()}", end=cut)
    oos_res = BacktestEngine(s).run(m1, label=f"OUT-OF-SAMPLE {cut.date()}→{t1.date()}", start=cut)
    print(format_report(is_res.report))
    print()
    print(format_report(oos_res.report))
    if not args.no_status and not args.start and not args.end:
        from dataclasses import asdict
        from backtest.validation import write_validation_status
        st = write_validation_status(oos_res.report, source=args.csv, params=asdict(s.strategy))
        print(f"\nStatut de validation enregistré (var/validation_status.json) : "
              f"{'VALIDÉ' if st['passed'] else 'NON VALIDÉ'}")
    if args.trades:
        for t in is_res.trades + oos_res.trades:
            print(f"{t.entry_time:%Y-%m-%d %H:%M} entry {t.entry:.2f} SL {t.stop_loss:.2f} TP {t.take_profit:.2f} "
                  f"→ {t.exit_reason:<14} {t.r_multiple:+.2f}R score {t.score:g}")


def cmd_analyze(args) -> None:
    from data.market_data import MarketDataService
    from monitoring.daily_report import build_daily_report
    from strategy.signal_engine import Setup, SignalEngine
    s = _settings()
    _logging(s)
    c = _client(s)
    c.login()
    md = MarketDataService(c, s)
    snap = md.snapshot()
    eng = SignalEngine(s.strategy, s.weights)
    st = eng.find_setup(snap.h4, snap.m15)
    print(build_daily_report(s, snap.now, snap.h4, snap.m15, snap.vwaps, st if isinstance(st, Setup) else None, 0, 0.0))
    if isinstance(st, Setup):
        out = eng.confirm_entry(st, snap.h4, snap.m15, snap.m1, snap.vwaps, snap.quote.spread if snap.quote else 9.9)
        print("\nConfirmation M1 :", getattr(out, "reason", None) or f"{out.score_label} entrée {out.entry}")
    else:
        print(f"\nNO TRADE : {st.reason}")
    if snap.errors:
        print("Erreurs : " + "; ".join(snap.errors))
    c.logout()


LIVE_ACTIVATIONS = [
    ("CAPITAL_ENV", "live", "compte RÉEL (https://api-capital.backend-capital.com)"),
    ("DRY_RUN", "false", "les ordres sont réellement envoyés au broker"),
    ("KILL_SWITCH", "false", "l'exécution est autorisée"),
    ("LIVE_TRADING_ACK", "J_ACCEPTE_LE_RISQUE_REEL", "accusé de risque réel"),
]
LIVE_UNCHANGED = [
    ("MODE", "MANUAL_APPROVAL"), ("MANUAL_APPROVAL_REQUIRED", "true"), ("AUTO_TRADING", "false"),
    ("ALLOWED_EPICS", "GOLD"), ("ALLOWED_DIRECTIONS", "BUY"), ("MAX_RISK_PER_TRADE_CHF", "100"),
    ("MAX_RISK_PER_TRADE_PCT", "1.0"), ("MAX_TRADES_PER_DAY", "2"), ("MAX_OPEN_POSITIONS", "1"),
]


def live_activation_text(s: Settings) -> str:
    from config import LIVE_UNVALIDATED_PHRASE, read_validation_status
    vs = read_validation_status()
    lines = ["CE QUI SERA ACTIVÉ POUR LE COMPTE RÉEL :"]
    lines += [f"  {k}={v:<28} → {why}" for k, v, why in LIVE_ACTIVATIONS]
    if not vs.get("passed"):
        lines.append(f"  LIVE_OVERRIDE_UNVALIDATED={LIVE_UNVALIDATED_PHRASE} → trader une stratégie NON validée")
    lines.append("INCHANGÉ (protections) :")
    lines += [f"  {k}={v}" for k, v in LIVE_UNCHANGED]
    lines.append(f"  perte journalière max = min({s.max_daily_loss_chf:g} CHF, {s.max_daily_loss_pct:g} %) ; "
                 f"spread max {s.max_spread_points:g} ; approbation APPROVE <id> à chaque trade")
    lines.append("STATUT DE VALIDATION BACKTEST : " + ("VALIDÉ" if vs.get("passed") else "NON VALIDÉ")
                 + (f" ({vs.get('trades')} trades, expectancy {vs.get('expectancy_r') or 0:+.3f} R, "
                    f"winrate {(vs.get('winrate') or 0):.1%})" if "trades" in vs
                    else f" ({vs.get('reason', '')})"))
    return "\n".join(lines)


def cmd_go_live_check(args) -> None:
    """Liste précise de ce qui serait activé + vérifications. N'active RIEN."""
    import statistics
    from config import read_validation_status
    s = _settings()
    print(live_activation_text(s))
    vs = read_validation_status()
    go = bool(vs.get("passed"))
    db = _db(s)
    fw = [t for t in db.iter_closed_trades() if t["mode"] == "DRY_RUN"]
    demo = [t for t in db.iter_closed_trades() if t["mode"] == "DEMO"]
    print(f"\nFORWARD TEST DRY_RUN : {len(fw)} trades clôturés", end="")
    if fw:
        rs = [t["r_multiple"] or 0.0 for t in fw]
        print(f" depuis {fw[0]['opened_at'][:10]}, expectancy {statistics.mean(rs):+.2f} R", end="")
    print(f"\nTEST D'EXÉCUTION DÉMO : {len(demo)} trades clôturés")
    if len(fw) < 20:
        print("  ! forward test insuffisant (recommandé : plusieurs semaines, ≥ 20 trades)")
        go = False
    if not demo:
        print("  ! aucun ordre exécuté sur compte démo : SL/TP attachés et P&L en CHF non vérifiés")
        go = False
    if s.has_credentials:
        try:
            c = _client(s)
            info = c.login()
            print(f"\nCONNEXION {s.capital_env.upper()} OK — devise du compte {info['account_currency']}")
            if info["account_currency"] != s.account_currency:
                print(f"  ! devise {info['account_currency']} ≠ ACCOUNT_CURRENCY={s.account_currency}")
                go = False
            inst = c.get_instrument(s.allowed_epics[0])
            amb = inst.ambiguities()
            print(f"  instrument {inst.epic} : min {inst.min_deal_size:g}, pas {inst.size_increment:g}, "
                  f"lotSize {inst.lot_size:g}, statut {inst.market_status}" + (f" ; AMBIGU : {amb}" if amb else ""))
            go = go and not amb
            pos = c.get_positions()
            print(f"  positions ouvertes : {len(pos)}")
            prefs = c.get_preferences()
            print(f"  hedgingMode : {prefs.get('hedgingMode')}")
            c.logout()
        except Exception as exc:  # noqa: BLE001
            print(f"  ! vérification API impossible : {exc}")
            go = False
    else:
        print("\n  ! identifiants absents : vérifications API non faites")
        go = False
    print("\nVERDICT : " + ("GO technique (confirmation écrite requise)" if go else
                            "NO-GO — conditions de passage en réel non remplies"))


def cmd_live_check(args) -> None:
    """Test de connexion LECTURE SEULE (aucun ordre possible, quelle que soit la configuration)."""
    import os
    from dataclasses import replace
    from broker.capital_api import build_client
    from risk.position_sizing import allowed_risk, compute_position_size
    from risk.risk_manager import RiskManager
    from strategy.indicators import atr
    s = _settings()
    print(f"Environnement : {s.capital_env.upper()} — {s.base_url}")
    print("Variables (valeurs jamais affichées) :")
    missing = []
    for name in ("CAPITAL_API_KEY", "CAPITAL_IDENTIFIER", "CAPITAL_PASSWORD"):
        ok = bool(os.environ.get(name, "").strip())
        print(f"  {name:<20} {'présente' if ok else 'ABSENTE'}")
        if not ok:
            missing.append(name)
    if missing:
        sys.exit("Identifiants incomplets : ajoutez les variables manquantes dans les réglages de l'environnement.")
    print(f"Sécurité : READ_ONLY={s.read_only} DRY_RUN={s.dry_run} KILL_SWITCH={s.kill_switch} "
          f"MODE={s.mode.value} (ce test force de toute façon la lecture seule)")
    s_ro = replace(s, read_only=True)
    c = build_client(s_ro)
    assert c.read_only
    _logging(s_ro)
    print(f"Heure serveur : {c.server_time()}")
    info = c.login()
    print("✔ Authentification réussie")
    acct = c.get_current_account()
    print(f"Compte ***{acct.account_id[-4:]} — devise {acct.currency}")
    print(f"  Solde {acct.balance:.2f} | Disponible {acct.available:.2f} | P&L latent {acct.profit_loss:+.2f}")
    if acct.currency != s.account_currency:
        print(f"  ! Devise {acct.currency} ≠ ACCOUNT_CURRENCY={s.account_currency} → le bot refusera de trader")
    inst = c.get_instrument(s.allowed_epics[0])
    q = c.get_quote(s.allowed_epics[0])
    print(f"Instrument {inst.epic} ({inst.name}) : statut {inst.market_status}, devise {inst.currency}, "
          f"taille min {inst.min_deal_size:g}, pas {inst.size_increment:g}, lotSize {inst.lot_size:g}")
    print(f"  Prix bid {q.bid:.2f} / ask {q.offer:.2f} — spread {q.spread:.2f} (max accepté {s.max_spread_points:g})")
    amb = inst.ambiguities()
    if amb:
        print("  ! Ambiguïtés → NO TRADE : " + "; ".join(amb))
    from data.market_data import MarketDataService
    fx = MarketDataService(c, s_ro).fx_rate(inst.currency)
    print(f"Taux {inst.currency}→{s.account_currency} : {fx if fx else 'INDISPONIBLE → NO TRADE'}")
    pos = c.get_positions()
    print(f"Positions ouvertes : {len(pos)}")
    try:
        print(f"Mode hedging : {c.get_preferences().get('hedgingMode')}")
    except Exception as exc:  # noqa: BLE001
        print(f"Préférences illisibles : {exc}")

    print("\nGESTION DU RISQUE ADAPTÉE AU CAPITAL DISPONIBLE")
    cap = acct.available
    r_allowed = allowed_risk(cap, s.max_risk_per_trade_chf, s.max_risk_per_trade_pct)
    print(f"  Risque max par trade = min({s.max_risk_per_trade_chf:g} CHF, {s.max_risk_per_trade_pct:g} % × {cap:.2f}) "
          f"= {r_allowed:.2f} {s.account_currency}")
    print(f"  Perte journalière max = {RiskManager(s).daily_loss_limit(cap):.2f} {s.account_currency}, "
          f"{s.max_trades_per_day} trades/jour, {s.max_open_positions} position max")
    m15 = c.get_candles(inst.epic, "M15", max_bars=100)
    a = atr(m15, 14)
    if a and fx:
        for mult in (1.0, 2.0, 3.0):
            dist = mult * a
            r = compute_position_size(
                entry=q.offer, stop_loss=q.offer - dist, capital_available=cap, max_risk_ccy=s.max_risk_per_trade_chf,
                max_risk_pct=s.max_risk_per_trade_pct, value_per_point=inst.value_per_point, quote_to_account_rate=fx,
                min_size=inst.min_deal_size, max_size=inst.max_deal_size, size_step=inst.size_increment,
                slippage_buffer=s.sl_slippage_buffer_points)
            verdict = (f"taille {r.size:g} → risque {r.risk_effective:.2f} {s.account_currency} "
                       f"({r.risk_pct_of_capital:.2f} %)" if r.ok else f"NO TRADE ({r.reason})")
            print(f"  SL à {dist:5.2f} USD ({mult:g}×ATR M15) : {verdict}")
    else:
        print("  Calcul d'exemple impossible (ATR ou taux de change indisponible)")
    c.logout()
    print("\n✔ Test terminé. Aucun ordre envoyé (client en lecture seule).")


def confirm_live_interactively(s: Settings) -> None:
    """Dernière barrière humaine au démarrage avec ordres réels."""
    if not (s.capital_env == "live" and not s.dry_run):
        return
    print("=" * 70)
    print(live_activation_text(s))
    print("=" * 70)
    if not sys.stdin.isatty():
        sys.exit("Refus : démarrage en réel uniquement depuis un terminal interactif.")
    typed = input('Pour démarrer avec des ORDRES RÉELS, tapez exactement : ORDRES REELS\n> ')
    if typed.strip() != "ORDRES REELS":
        sys.exit("Confirmation absente : démarrage annulé.")


def cmd_run(args) -> None:
    from interface.runner import BotRunner
    from monitoring.alerts import build_notifier
    s = _settings()
    confirm_live_interactively(s)
    _logging(s)
    db = _db(s)
    c = _client(s)
    c.login()
    BotRunner(s, db, c, build_notifier(s, db)).run_forever()


def cmd_web(args) -> None:
    from interface.web import serve
    s = _settings()
    serve(s, _db(s))


def cmd_decide(args) -> None:
    from approval.trade_approval import ApprovalManager
    from interface.web import current_price_from_status
    s = _settings()
    db = _db(s)
    from monitoring.alerts import build_notifier
    res = ApprovalManager(db, s, notifier=build_notifier(s, db, console=False)).handle(
        args.command, current_price_from_status(db))
    print(res.message)
    sys.exit(0 if res.ok else 1)


def cmd_proposals(args) -> None:
    from approval.trade_approval import format_proposal
    s = _settings()
    db = _db(s)
    for p in db.proposals(limit=args.limit):
        print(format_proposal(p["id"], p["payload"], p["status"]))
        print(f"  créée {p['created_at']} expire {p['expires_at']} note : {p.get('decision_note') or ''}\n")


def cmd_status(args) -> None:
    s = _settings()
    db = _db(s)
    st = json.loads(db.get("status") or "{}")
    st["kill_switch_config"] = s.kill_switch
    st["kill_switch_runtime"] = db.runtime_kill_switch()
    st["daily"] = db.daily()
    print(json.dumps(st, indent=2, default=str, ensure_ascii=False))


def cmd_kill(args) -> None:
    s = _settings()
    db = _db(s)
    db.set("kill_switch", "1")
    db.log_event("KILL_SWITCH", "activé manuellement")
    print("KILL SWITCH runtime ACTIVÉ : aucune exécution possible.")


def cmd_unkill(args) -> None:
    s = _settings()
    db = _db(s)
    db.set("kill_switch", "0")
    db.log_event("KILL_SWITCH", "kill switch runtime désactivé manuellement")
    print("Kill switch runtime désactivé." + (" ATTENTION : KILL_SWITCH=true dans .env reste actif." if s.kill_switch else ""))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Bot de trading semi-automatisé XAU/USD (Capital.com)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check").set_defaults(fn=cmd_check)
    sub.add_parser("init-db").set_defaults(fn=cmd_init_db)
    sub.add_parser("connect").set_defaults(fn=cmd_connect)
    sub.add_parser("instrument").set_defaults(fn=cmd_instrument)
    p = sub.add_parser("fetch-history")
    p.add_argument("--tf", default="M1", choices=["M1", "M5", "M15", "H1", "H4"])
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--out", default="var/data/capital_m1.csv")
    p.set_defaults(fn=cmd_fetch_history)
    p = sub.add_parser("backtest")
    p.add_argument("--csv", required=True)
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--spread", type=float, default=None, help="spread fixe si absent du CSV")
    p.add_argument("--is-fraction", type=float, default=0.7)
    p.add_argument("--walk-forward", action="store_true")
    p.add_argument("--train-days", type=int, default=180)
    p.add_argument("--test-days", type=int, default=60)
    p.add_argument("--trades", action="store_true")
    p.add_argument("--no-status", action="store_true", help="ne pas écrire var/validation_status.json")
    p.set_defaults(fn=cmd_backtest)
    sub.add_parser("go-live-check").set_defaults(fn=cmd_go_live_check)
    sub.add_parser("live-check", help="test de connexion et de solde en lecture seule").set_defaults(fn=cmd_live_check)
    sub.add_parser("analyze").set_defaults(fn=cmd_analyze)
    sub.add_parser("run").set_defaults(fn=cmd_run)
    sub.add_parser("web").set_defaults(fn=cmd_web)
    p = sub.add_parser("decide")
    p.add_argument("command")
    p.set_defaults(fn=cmd_decide)
    p = sub.add_parser("proposals")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(fn=cmd_proposals)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("kill").set_defaults(fn=cmd_kill)
    sub.add_parser("unkill").set_defaults(fn=cmd_unkill)
    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
