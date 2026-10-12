"""Moteur de signal BUY XAU/USD — identique en live et en backtest.

Deux étapes, toutes deux **sans état** (elles ne regardent que les bougies closes
fournies, ce qui les rend reproductibles, testables et sans lookahead) :

1. `find_setup(h4, m15)` — à chaque clôture M15 :
   biais H4 (porte) → sweep sell-side M15 → CHOCH M15 → impulsion, OTE, FVG, OB.
   Renvoie un `Setup` armé ou un `NoSetup` avec la raison.
2. `confirm_entry(setup, h4, m15, m1, vwaps, spread)` — à chaque clôture M1 :
   touche de zone (OTE/FVG/OB) → micro-CHOCH ou rejet M1 → entrée, SL, TP, RR, score.
   Renvoie un `TradeSignal` (avec conditions remplies / non remplies) ou un `NoTrade`.

Aucun indicateur ne déclenche seul : sweep, CHOCH et confirmation M1 sont des portes
obligatoires ; le reste alimente le score (minimum configurable, 7/10 par défaut).
La direction est un paramètre (`BUY`) : un module SELL symétrique pourra être ajouté
sans toucher au Risk Manager.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Sequence

from config import ScoreWeights, StrategyParams
from data.candles import Candle
from strategy.fibonacci import Zone, extension, ote_zone
from strategy.fvg import FVG, bullish_fvgs
from strategy.indicators import atr
from strategy.liquidity import Sweep, detect_sweep, sell_side_levels
from strategy.market_structure import Bias, d1_bullish, BiasResult, Choch, find_bullish_choch, find_swings, h4_bias, swing_highs
from strategy.order_blocks import OrderBlock, bullish_order_block
from strategy.scoring import ScoreBreakdown
from strategy.vwap import VwapValue

DIRECTION = "BUY"


@dataclass(frozen=True)
class NoSetup:
    reason: str


@dataclass(frozen=True)
class Setup:
    setup_id: str                 # identifiant stable = heure du sweep
    direction: str
    bias: BiasResult
    sweep: Sweep
    sweep_time: datetime
    choch: Choch
    choch_time: datetime          # clôture de la bougie de CHOCH
    impulse_low: float
    impulse_high: float
    ote: Zone
    fvgs: tuple[FVG, ...]
    order_block: OrderBlock | None
    atr_m15: float
    atr_h4: float
    armed_at: datetime            # clôture de la dernière M15 utilisée
    expires_at: datetime

    @property
    def zones(self) -> list[tuple[str, Zone]]:
        out: list[tuple[str, Zone]] = [("OTE", self.ote)]
        out += [("FVG", f.zone) for f in self.fvgs]
        if self.order_block:
            out.append(("OB", self.order_block.zone))
        return out


@dataclass(frozen=True)
class NoTrade:
    reason: str
    setup_id: str | None = None
    details: dict = field(default_factory=dict)


@dataclass(frozen=True)
class TradeSignal:
    setup_id: str
    direction: str
    time: datetime                # clôture de la bougie M1 de confirmation
    entry: float                  # ASK estimé
    stop_loss: float
    take_profit: float
    rr: float
    score: float
    score_label: str
    breakdown: dict[str, float]
    conditions_met: list[str]
    conditions_unmet: list[str]
    sl_reason: str
    tp_reason: str
    trade_reason: str
    analysis: dict[str, str]
    flags: dict[str, bool]        # sweep/fvg/ob/vwap/ote/... pour la base et les stats
    vwaps: dict[str, float | None]
    spread: float


class SignalEngine:
    def __init__(self, params: StrategyParams, weights: ScoreWeights):
        self.p = params
        self.w = weights

    # ------------------------------------------------------------------ M15
    def find_setup(self, h4: Sequence[Candle], m15: Sequence[Candle]) -> Setup | NoSetup:
        p = self.p
        if len(m15) < max(p.atr_period + 2, 60) or len(h4) < p.h4_ema_period:
            return NoSetup("Historique insuffisant (M15/H4)")
        bias = h4_bias(h4, p.swing_k_h4, p.h4_ema_period)
        if bias.bias not in (Bias.BULLISH, Bias.NEUTRAL_BULLISH):
            return NoSetup(f"Biais H4 non haussier : {bias.bias.value} — {bias.detail}")
        if p.h4_strict_bullish and bias.bias != Bias.BULLISH:
            return NoSetup(f"Biais H4 non haussier : {bias.bias.value} (mode strict)")
        if p.require_d1_bullish:
            ok, why = d1_bullish(h4)
            if not ok:
                return NoSetup(f"Biais H4 non haussier : D1 {why}")
        atr_m15 = atr(m15, p.atr_period)
        atr_h4 = atr(h4, p.atr_period)
        if not atr_m15 or not atr_h4:
            return NoSetup("ATR indisponible")

        n = len(m15)
        last = n - 1
        window = p.choch_max_bars_after_sweep + p.sweep_reclaim_bars + p.setup_expiry_m15_bars
        m15_swings = find_swings(m15, p.swing_k_m15)   # filtrés par confirmation < s plus bas
        h4_all_swings = find_swings(h4, p.swing_k_h4)
        h4_close_ts = [c.time.timestamp() + 14400 for c in h4]
        # Parcours du plus récent au plus ancien : le sweep le plus récent ayant un CHOCH gagne.
        for s in range(last, max(p.swing_k_m15 + 1, last - window) - 1, -1):
            # Condition nécessaire (optimisation sans effet sur le résultat) : un niveau balayé
            # est confirmé au plus tard en s-1-k et non pris jusqu'à s-1, donc le low de s doit
            # être sous les lows de s-1 et s-2.
            if not m15[s].low < min(m15[s - 1].low, m15[s - 2].low):
                continue
            sweep_open = m15[s].time
            nb = bisect_right(h4_close_ts, sweep_open.timestamp())
            h4_before = h4[:nb]
            levels = sell_side_levels(
                m15, h4_before, k_m15=p.swing_k_m15, k_h4=p.swing_k_h4, atr_m15=atr_m15,
                equal_tol_atr=p.equal_lows_tolerance_atr, lookback=p.liquidity_lookback_m15,
                range_lookback=p.range_lookback_m15, upto_index=s, m15_swings=m15_swings,
                h4_swings=[x for x in h4_all_swings if x.confirmed_at < nb],
            )
            # Ne garder que les niveaux SOUS le prix au moment du sweep (sell-side).
            levels = [lv for lv in levels if lv.price < m15[s - 1].close] if s > 0 else []
            sweep = detect_sweep(m15, h4_before, levels, search_from=s, search_to=s + 1,
                                 reclaim_bars=p.sweep_reclaim_bars,
                                 min_penetration=p.sweep_min_penetration_atr * atr_m15)
            if sweep is None:
                continue
            if p.require_significant_sweep and not sweep.significant:
                continue
            choch = find_bullish_choch(m15, sweep.sweep_index, sweep.sweep_low, p.swing_k_m15,
                                       p.choch_max_bars_after_sweep, swings=m15_swings)
            if choch is None:
                continue
            # Invalidation : un plus bas sous le sweep après le CHOCH.
            if any(m15[j].low < sweep.sweep_low for j in range(sweep.sweep_index + 1, n)):
                return NoSetup("Setup invalidé : plus bas sous le low du sweep")
            if last - choch.break_index > p.setup_expiry_m15_bars:
                return NoSetup("Setup expiré (CHOCH trop ancien)")
            imp_low, imp_high = sweep.sweep_low, choch.impulse_high
            if imp_high <= imp_low:
                continue
            ote = ote_zone(imp_low, imp_high, p.ote_low, p.ote_high)
            fvgs = tuple(bullish_fvgs(m15, sweep.sweep_index, last, p.fvg_min_size_atr * atr_m15))
            ob = bullish_order_block(m15, sweep.sweep_index, choch.break_index)
            choch_time = m15[choch.break_index].close_time("M15")
            armed_at = m15[last].close_time("M15")
            return Setup(
                setup_id=f"BUY-{m15[sweep.sweep_index].time.strftime('%Y%m%dT%H%M')}",
                direction=DIRECTION, bias=bias, sweep=sweep, sweep_time=m15[sweep.sweep_index].time,
                choch=choch, choch_time=choch_time, impulse_low=imp_low, impulse_high=imp_high,
                ote=ote, fvgs=fvgs, order_block=ob, atr_m15=atr_m15, atr_h4=atr_h4, armed_at=armed_at,
                expires_at=choch_time + timedelta(minutes=15 * p.setup_expiry_m15_bars),
            )
        return NoSetup("Aucun sweep de liquidité sell-side suivi d'un CHOCH M15")

    # ------------------------------------------------------------------- M1
    def confirm_entry(self, setup: Setup, h4: Sequence[Candle], m15: Sequence[Candle],
                      m1: Sequence[Candle], vwaps: dict[str, VwapValue], spread: float) -> TradeSignal | NoTrade:
        p = self.p
        sid = setup.setup_id
        if len(m1) < p.m1_break_lookback + 2:
            return NoTrade("Historique M1 insuffisant", sid)
        now_bar = m1[-1]
        now = now_bar.close_time("M1")
        if now > setup.expires_at:
            return NoTrade("Setup expiré", sid)
        # Seules les M1 postérieures au CHOCH comptent pour la confirmation.
        post = [c for c in m1 if c.time >= setup.choch_time]
        if any(c.low < setup.sweep.sweep_low for c in post):
            return NoTrade("Invalidé : M1 sous le low du sweep", sid)
        if not post:
            return NoTrade("Pas encore de M1 après le CHOCH", sid)

        # 1) Touche de zone dans les m1_touch_max_age dernières M1 (avant la bougie courante incluse)
        recent = post[-p.m1_touch_max_age:]
        touched: list[str] = []
        touch_low = min(c.low for c in recent)
        for name, z in setup.zones:
            if any(z.touched_by(c.low, c.high) for c in recent):
                touched.append(name)
        if not touched:
            return NoTrade("Prix pas encore revenu dans la zone (OTE/FVG/OB)", sid)

        if p.confirm_tf == "M15":
            return self._confirm_m15(setup, h4, m15, m1, vwaps, spread)

        # 2) Confirmation M1 : micro-CHOCH (clôture > plus haut des N M1 précédentes) ou rejet.
        prev = m1[-1 - p.m1_break_lookback:-1]
        micro_level = max(c.high for c in prev)
        micro_choch = now_bar.close > micro_level and now_bar.is_bullish
        lower_wick = min(now_bar.open, now_bar.close) - now_bar.low
        in_zone_now = any(z.touched_by(now_bar.low, now_bar.high) for _, z in setup.zones)
        rejection = (in_zone_now and now_bar.is_bullish and now_bar.range > 0
                     and lower_wick >= 2 * max(now_bar.body, 1e-9)
                     and (now_bar.close - now_bar.low) >= 0.66 * now_bar.range)
        if not (micro_choch or rejection):
            return NoTrade("Pas de confirmation M1 (micro-CHOCH / rejet)", sid)
        m1_note = (f"micro-CHOCH M1 : clôture {now_bar.close:.2f} > {micro_level:.2f}" if micro_choch
                   else "rejet M1 (mèche basse ≥ 2× corps, clôture dans le tiers haut)")

        return self._build_signal(setup, h4, m15, now, now_bar.close + spread, touched, touch_low,
                                  now_bar.close, vwaps, spread, m1_note,
                                  {"m1_micro_choch": micro_choch, "m1_rejection": rejection})

    def _build_signal(self, setup: Setup, h4: Sequence[Candle], m15: Sequence[Candle], now: datetime,
                      entry: float, touched: list[str], touch_low: float, ref_close: float,
                      vwaps: dict[str, VwapValue], spread: float, confirm_note: str | None,
                      extra_flags: dict[str, bool]) -> TradeSignal | NoTrade:
        """Calcule SL/TP/RR/score à partir d'une entrée (commun M1, M15, limite)."""
        p = self.p
        sid = setup.setup_id
        # 3) Prix, SL, TP
        sl = setup.sweep.sweep_low - (p.sl_atr_mult * setup.atr_m15 + spread)
        risk = entry - sl
        if risk <= 0:
            return NoTrade("SL impossible (au-dessus de l'entrée)", sid)
        tp_choice = self._choose_tp(setup, h4, m15, entry, risk, spread)
        if isinstance(tp_choice, NoTrade):
            return tp_choice
        tp, tp_reason, rr = tp_choice

        # 4) Score
        sb = ScoreBreakdown(self.w)
        sb.add("h4_context", 1.0 if setup.bias.bias == Bias.BULLISH else 0.5,
               f"H4 {setup.bias.bias.value} ({setup.bias.detail})")
        sb.add("liquidity_zone", 1.0 if setup.sweep.significant else 0.0,
               f"niveau balayé {setup.sweep.level_kind} @ {setup.sweep.level:.2f}"
               + ("" if setup.sweep.significant else " (mineur)"))
        sb.add("sweep", 1.0, f"sweep {setup.sweep.reaction} low {setup.sweep.sweep_low:.2f}")
        sb.add("m15_structure", 1.0, f"CHOCH M15 au-dessus de {setup.choch.level:.2f}")
        fvg_hit = "FVG" in touched
        ob_hit = "OB" in touched
        sb.add("fvg_or_ob", 1.0 if (fvg_hit or ob_hit) else 0.0,
               "zone " + "+".join(x for x in ("FVG", "OB") if x in touched) + " touchée" if (fvg_hit or ob_hit)
               else f"FVG présents={len(setup.fvgs)}, OB={'oui' if setup.order_block else 'non'}, non touchés")
        ote_hit = "OTE" in touched
        sb.add("ote_fib", 1.0 if ote_hit else 0.0,
               f"OTE {setup.ote.bottom:.2f}–{setup.ote.top:.2f} " + ("touchée" if ote_hit else "non atteinte"))
        tol = p.vwap_tolerance_atr * setup.atr_m15
        vwap_hits = [k for k, v in vwaps.items()
                     if v.available and touch_low <= v.value + tol and ref_close >= v.value - tol]  # type: ignore[operator]
        unavailable = [f"{k}: {v.reason}" for k, v in vwaps.items() if not v.available]
        vnote = ("réaction sur VWAP " + ",".join(vwap_hits)) if vwap_hits else "pas de réaction sur un VWAP"
        if unavailable:
            vnote += " | indisponible: " + "; ".join(unavailable)
        sb.add("vwap", 1.0 if vwap_hits else 0.0, vnote)
        sb.add("m1_confirmation", 1.0 if confirm_note else 0.0,
               confirm_note or "pas de confirmation (ordre limite placé à l'avance)")

        if sb.total < p.min_score:
            return NoTrade(f"{sb.label()} < minimum {p.min_score:g}", sid,
                           {"score": sb.total, "unmet": sb.unmet()})

        analysis = {
            "H4": f"{setup.bias.bias.value} — {setup.bias.detail}",
            "M15": (f"sweep {setup.sweep.level_kind} {setup.sweep.level:.2f} (low {setup.sweep.sweep_low:.2f}, "
                    f"{setup.sweep.reaction}) puis CHOCH > {setup.choch.level:.2f} ; impulsion "
                    f"{setup.impulse_low:.2f}→{setup.impulse_high:.2f}"),
            "M1": confirm_note or "ordre limite (pas de confirmation)",
        }
        return TradeSignal(
            setup_id=sid, direction=DIRECTION, time=now, entry=round(entry, 2), stop_loss=round(sl, 2),
            take_profit=round(tp, 2), rr=round(rr, 2), score=sb.total, score_label=sb.label(),
            breakdown=dict(sb.items), conditions_met=sb.met(), conditions_unmet=sb.unmet(),
            sl_reason=(f"sous le low du sweep {setup.sweep.sweep_low:.2f} - ({p.sl_atr_mult:g}×ATR15 "
                       f"{setup.atr_m15:.2f} + spread {spread:.2f})"),
            tp_reason=tp_reason,
            trade_reason=("Prise de liquidité sell-side M15 + CHOCH haussier, retour en zone "
                          + "/".join(dict.fromkeys(touched)) + ", confirmation M1, biais H4 haussier"),
            analysis=analysis,
            flags={"sweep": True, "choch": True, "fvg": fvg_hit, "ob": ob_hit, "ote": ote_hit,
                   "vwap": bool(vwap_hits), "liquidity_significant": setup.sweep.significant,
                   **extra_flags},
            vwaps={k: (round(v.value, 2) if v.value is not None else None) for k, v in vwaps.items()},
            spread=spread,
        )

    def _confirm_m15(self, setup: Setup, h4: Sequence[Candle], m15: Sequence[Candle], m1: Sequence[Candle],
                     vwaps: dict[str, VwapValue], spread: float) -> TradeSignal | NoTrade:
        """H1 : confirmation sur la clôture M15 (évaluée uniquement à l'instant de cette clôture).

        La dernière M15 close, postérieure au CHOCH, touche une zone, est haussière et clôture
        au-dessus du haut de la zone touchée la plus basse (rejet / réintégration)."""
        sid = setup.setup_id
        now = m1[-1].close_time("M1")
        if not m15 or m15[-1].close_time("M15") != now:
            return NoTrade("Attente de la clôture M15", sid)
        bar = m15[-1]
        if bar.time < setup.choch_time:
            return NoTrade("Pas encore de M15 après le CHOCH", sid)
        touched = [(n, z) for n, z in setup.zones if z.touched_by(bar.low, bar.high)]
        if not touched:
            return NoTrade("Prix pas encore revenu dans la zone (OTE/FVG/OB)", sid)
        lowest_top = min(z.top for _, z in touched)
        if not (bar.is_bullish and bar.close > lowest_top):
            return NoTrade("Pas de confirmation M15 (rejet haussier)", sid)
        note = f"rejet M15 : clôture {bar.close:.2f} > haut de zone {lowest_top:.2f}"
        return self._build_signal(setup, h4, m15, now, bar.close + spread, [n for n, _ in touched], bar.low,
                                  bar.close, vwaps, spread, note, {"m15_confirmation": True})

    def limit_plan(self, setup: Setup, h4: Sequence[Candle], m15: Sequence[Candle], now: datetime,
                   vwaps: dict[str, VwapValue], spread: float) -> TradeSignal | NoTrade:
        """H4 : ordre limite BUY (prix ASK) au niveau `limit_retracement` de l'impulsion."""
        p = self.p
        bid_level = setup.impulse_high - p.limit_retracement * (setup.impulse_high - setup.impulse_low)
        entry = bid_level + spread
        touched = [n for n, z in setup.zones if z.contains(bid_level)]
        return self._build_signal(setup, h4, m15, now, entry, touched, bid_level, bid_level, vwaps, spread, None,
                                  {"limit_order": True})

    # ------------------------------------------------------------------ TP
    def _choose_tp(self, setup: Setup, h4: Sequence[Candle], m15: Sequence[Candle], entry: float,
                   risk: float, spread: float) -> tuple[float, str, float] | NoTrade:
        p = self.p
        max_dist = p.max_tp_atr_h4 * setup.atr_h4
        liquidity: list[tuple[float, str]] = [(setup.impulse_high, "haut de l'impulsion M15 (liquidité buy-side)")]
        m15_start = max(0, len(m15) - p.liquidity_lookback_m15)
        for s in swing_highs(find_swings(m15, p.swing_k_m15)):
            if s.index >= m15_start:
                liquidity.append((s.price, "swing high M15 (liquidité buy-side)"))
        for s in swing_highs(find_swings(h4, p.swing_k_h4)):
            liquidity.append((s.price, "swing high H4 (liquidité buy-side)"))
        exts = [(extension(setup.impulse_low, setup.impulse_high, r), f"extension Fibonacci {r}")
                for r in (1.272, 1.618)]

        def to_tp(level: float) -> float:
            return level - spread  # TP déclenché au BID : on vise juste sous la liquidité

        liq_above = sorted((lv, why) for lv, why in liquidity if to_tp(lv) > entry)
        all_above = sorted((lv, why) for lv, why in liquidity + exts if to_tp(lv) > entry)
        reasonable = [(lv, why) for lv, why in all_above if to_tp(lv) - entry <= max_dist]

        def rr_of(level: float) -> float:
            return (to_tp(level) - entry) / risk

        if p.tp_mode == "first_liquidity":
            if liq_above:
                lv, why = liq_above[0]
                if to_tp(lv) - entry > max_dist or rr_of(lv) < p.min_rr:
                    return NoTrade(f"Première liquidité {lv:.2f} → RR {rr_of(lv):.2f} < {p.min_rr:g}", setup.setup_id)
                return to_tp(lv), f"{why} @ {lv:.2f}", rr_of(lv)
            reasonable = [(lv, why) for lv, why in reasonable if why.startswith("extension")]

        valid = [(lv, why) for lv, why in reasonable if rr_of(lv) >= p.min_rr]
        if not valid:
            best = max((rr_of(lv) for lv, _ in reasonable), default=0.0)
            return NoTrade(f"Aucun TP raisonnable n'offre RR ≥ {p.min_rr:g} (meilleur {best:.2f})", setup.setup_id)
        if p.tp_mode == "prefer_target":
            tgt = [(lv, why) for lv, why in valid if rr_of(lv) >= p.target_rr]
            if tgt:
                valid = tgt
        lv, why = valid[0]
        return to_tp(lv), f"{why} @ {lv:.2f}", rr_of(lv)
