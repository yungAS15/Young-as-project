"""Tableau de bord web LOCAL (127.0.0.1 uniquement), sans dépendance externe.

Affiche le statut écrit par le runner et permet APPROVE / REJECT / CANCEL.
Sécurité :
- écoute sur 127.0.0.1 par défaut (refus de démarrer sur une autre interface sans
  `--allow-remote`) ;
- jeton anti-CSRF aléatoire par démarrage ;
- l'utilisateur doit TAPER la commande exacte (« APPROVE ab12cd34 ») : un clic seul
  ne suffit pas ; la commande passe par le même ApprovalManager (règles strictes) ;
- le prix courant utilisé pour vérifier la dérive vient du statut du runner, seulement
  s'il a moins de 30 s ; sinon l'approbation est refusée.
"""
from __future__ import annotations

import html
import json
import secrets
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from approval.trade_approval import PENDING, ApprovalManager, format_proposal
from config import Settings
from data.database import Database, parse_iso


def current_price_from_status(db: Database, max_age_s: float = 30.0) -> float | None:
    raw = db.get("status")
    if not raw:
        return None
    st = json.loads(raw)
    t = parse_iso(st.get("quote_time"))
    if t is None or st.get("offer") is None:
        return None
    if (datetime.now(timezone.utc) - t).total_seconds() > max_age_s:
        return None
    return float(st["offer"])


def render(settings: Settings, db: Database, token: str, message: str = "") -> str:
    st = json.loads(db.get("status") or "{}")
    e = html.escape

    def row(k, v):
        return f"<tr><th>{e(str(k))}</th><td>{e(str(v))}</td></tr>"

    rows = "".join(row(k, st.get(k)) for k in (
        "updated", "mode", "dry_run", "env", "kill_switch", "connected", "api_errors", "balance", "equity",
        "available", "pnl_today", "bid", "offer", "spread", "bias", "setup"))
    rows += row("trades du jour", f"{st.get('trades_today')}/{st.get('max_trades')}")
    rows += row("VWAP", ", ".join(f"{k}={v:.2f}" if v else f"{k}=indisponible" for k, v in (st.get("vwaps") or {}).items()))
    rows += row("position ouverte", "; ".join(f"#{t['id']} {t['size']} @ {t['entry']} SL {t['stop_loss']} TP {t['take_profit']}"
                                              for t in st.get("open_trades") or []) or "aucune")
    props = ""
    for p in db.proposals(limit=10):
        props += f"<pre class='{e(p['status'])}'>{e(format_proposal(p['id'], p['payload'], p['status']))}</pre>"
    events = "".join(f"<li>{e(x['ts'][:19])} [{e(x['kind'])}] {e(x['message'][:200])}</li>" for x in db.recent_events(15))
    return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta http-equiv="refresh" content="15">
<title>Bot XAU/USD</title><style>
body{{font-family:-apple-system,system-ui,sans-serif;margin:16px;background:#fafafa;color:#222}}
table{{border-collapse:collapse}} th,td{{border:1px solid #ccc;padding:4px 8px;text-align:left}}
pre{{background:#fff;border:1px solid #ddd;padding:8px;white-space:pre-wrap}} .PENDING{{border-color:#e69500}}
.msg{{padding:8px;background:#eef;border:1px solid #99c}} input[type=text]{{width:22em;font-family:monospace}}
</style></head><body><h1>Bot XAU/USD — {e(settings.mode.value)} {'(DRY_RUN)' if settings.dry_run else ''}</h1>
{f'<p class="msg">{e(message)}</p>' if message else ''}
<table>{rows}</table>
<h2>Décision</h2><form method="post" action="/decide">
<input type="hidden" name="token" value="{token}">
<input type="text" name="command" placeholder="APPROVE ab12cd34" autocomplete="off">
<button type="submit">Envoyer</button></form>
<p>Tapez exactement <code>APPROVE &lt;id&gt;</code>, <code>REJECT &lt;id&gt;</code> ou <code>CANCEL &lt;id&gt;</code>.
Toute autre formulation est ignorée. Le Risk Manager revérifie tout avant l'ordre.</p>
<h2>Propositions récentes</h2>{props or '<p>Aucune.</p>'}
<h2>Événements</h2><ul>{events}</ul></body></html>"""


def serve(settings: Settings, db: Database, host: str | None = None, port: int | None = None,
          allow_remote: bool = False) -> None:
    host = host or settings.web_host
    port = port or settings.web_port
    if host not in ("127.0.0.1", "localhost", "::1") and not allow_remote:
        raise SystemExit("Refus : l'interface ne doit écouter que sur 127.0.0.1")
    token = secrets.token_urlsafe(16)
    from monitoring.alerts import build_notifier
    approvals = ApprovalManager(db, settings, notifier=build_notifier(settings, db, console=False))

    class Handler(BaseHTTPRequestHandler):
        def _send(self, body: str, code: int = 200) -> None:
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            if self.path not in ("/", "/index.html"):
                return self._send("not found", 404)
            self._send(render(settings, db, token))

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0") or 0)
            form = parse_qs(self.rfile.read(min(length, 4096)).decode())
            if form.get("token", [""])[0] != token:
                return self._send("jeton invalide", 403)
            cmd = form.get("command", [""])[0]
            res = approvals.handle(cmd, current_price_from_status(db))
            self._send(render(settings, db, token, res.message))

        def log_message(self, *args):  # silence
            return

    print(f"Interface locale : http://{host}:{port}  (Ctrl+C pour arrêter)")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


__all__ = ["serve", "render", "PENDING"]
