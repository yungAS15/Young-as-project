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


def cmd_run(args) -> None:
    from interface.runner import BotRunner
    from monitoring.alerts import build_notifier
    s = _settings()
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
    p.set_defaults(fn=cmd_backtest)
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
