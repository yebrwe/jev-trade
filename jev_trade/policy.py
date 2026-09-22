"""Code-owned trading policy: turns Jev's judgments into one of long / short / hold / exit.

Everything here is deterministic and tunable without re-running inference:
confidence gates, position awareness, cooldown, daily limits, and ATR-based sizing.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from .config import Settings
from .exchange import Position
from .judge import Judgment


@dataclass
class RuntimeState:
    last_exit_at: float | None = None
    trades_day: str = ""
    trades_today: int = 0
    position_opened_at: float | None = None  # when this bot opened the current live position

    def register_trade(self, now: float) -> None:
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        if day != self.trades_day:
            self.trades_day, self.trades_today = day, 0
        self.trades_today += 1

    def trades_for(self, now: float) -> int:
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        return self.trades_today if day == self.trades_day else 0


@dataclass
class Decision:
    action: str  # long | short | hold | exit
    reasons: list[str] = field(default_factory=list)
    size_scale: float = 0.0  # 0..1 fraction of the risk budget to deploy
    qty: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    notional: float | None = None

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def _entry_gate(j: Judgment, s: Settings, reasons: list[str]) -> bool:
    ok = True
    if j.entry_action not in ("long", "short"):
        reasons.append("jev prefers hold")
        return False
    p = j.entry_probs.get(j.entry_action, 0.0)
    if j.entry_confidence < s.min_entry_confidence:
        reasons.append(f"entry confidence {j.entry_confidence:.2f} < {s.min_entry_confidence}")
        ok = False
    if p < s.min_entry_prob:
        reasons.append(f"P({j.entry_action}) {p:.2f} < {s.min_entry_prob}")
        ok = False
    if j.setup_score < s.min_setup_score:
        reasons.append(f"setup_quality {j.setup_score:.2f} < {s.min_setup_score}")
        ok = False
    if j.choppy >= s.choppy_max:
        reasons.append(f"choppy {j.choppy:.2f} >= {s.choppy_max}")
        ok = False
    if j.overextended >= 0.7:
        reasons.append(f"overextended {j.overextended:.2f} >= 0.70")
        ok = False
    return ok


def _perspective_gate(j: Judgment, s: Settings, meta: dict, reasons: list[str]) -> bool:
    """Code-owned rules over the macro perspective and the news monitor. Only applies to new entries."""
    ok = True
    if meta.get("shock_active"):
        reasons.append("news monitor flagged a market shock; no new entries until the perspective is refreshed")
        return False
    p = meta.get("perspective") or {}
    if not p:
        return True
    side = j.entry_action
    if s.block_entry_on_high_event_risk and p.get("event_risk_level") == "high":
        reasons.append("perspective event_risk_level=high; new entries blocked")
        ok = False
    stance = p.get("trading_stance")
    if s.respect_trading_stance and stance:
        if stance == "stay flat":
            reasons.append("perspective trading_stance=stay flat")
            ok = False
        elif stance == "favor longs" and side == "short":
            reasons.append("perspective favors longs; short entry blocked")
            ok = False
        elif stance == "favor shorts" and side == "long":
            reasons.append("perspective favors shorts; long entry blocked")
            ok = False
    against = j.macro_against_long if side == "long" else j.macro_against_short
    if against is not None and against >= s.macro_conflict_max:
        reasons.append(f"jev: perspective argues against {side} (P={against:.2f} >= {s.macro_conflict_max})")
        ok = False
    return ok


def size_position(
    side: str, price: float, atr: float | None, equity: float, s: Settings, scale: float
) -> tuple[float, float, float, float]:
    """Return (qty, stop_loss, take_profit, notional). Risk = RISK_PER_TRADE_PCT of equity at the stop."""
    if atr is None or atr <= 0:
        atr = price * 0.005  # fallback 0.5%
    stop_dist = atr * s.atr_stop_mult
    tp_dist = atr * s.atr_tp_mult
    risk_usd = equity * s.risk_per_trade_pct / 100 * scale
    qty = risk_usd / stop_dist
    max_notional = equity * s.max_position_pct / 100 * s.leverage
    qty = min(qty, max_notional / price)
    if side == "long":
        sl, tp = price - stop_dist, price + tp_dist
    else:
        sl, tp = price + stop_dist, price - tp_dist
    return qty, sl, tp, qty * price


def decide(
    j: Judgment,
    position: Position | None,
    s: Settings,
    meta: dict,
    equity: float,
    runtime: RuntimeState,
    now: float | None = None,
) -> Decision:
    now = now or time.time()
    reasons: list[str] = []
    price = meta.get("price") or meta.get("close")
    atr = meta.get("atr")

    # ---------- position open: keep or exit ----------
    if position is not None:
        exit_votes: list[str] = []
        if j.position_action == "exit":
            p_exit = (j.position_probs or {}).get("exit", 0.0)
            if p_exit >= s.min_exit_prob:
                exit_votes.append(f"jev position_action=exit P={p_exit:.2f}")
        if j.thesis_invalidated is not None and j.thesis_invalidated >= s.thesis_invalidated_threshold:
            exit_votes.append(f"thesis_invalidated {j.thesis_invalidated:.2f}")
        opposite = "short" if position.side == "long" else "long"
        if (
            j.entry_action == opposite
            and j.entry_probs.get(opposite, 0.0) >= s.min_entry_prob
            and j.entry_confidence >= s.min_entry_confidence
        ):
            exit_votes.append(f"opposite entry signal {opposite} P={j.entry_probs.get(opposite, 0.0):.2f}")
        if exit_votes:
            return Decision(action="exit", reasons=exit_votes)
        reasons.append(f"keep {position.side}: position_action={j.position_action} "
                       f"P(exit)={(j.position_probs or {}).get('exit', 0.0):.2f}, "
                       f"thesis_invalidated={j.thesis_invalidated}")
        return Decision(action="hold", reasons=reasons)

    # ---------- flat: enter or hold ----------
    if not _entry_gate(j, s, reasons):
        return Decision(action="hold", reasons=reasons)
    if not _perspective_gate(j, s, meta, reasons):
        return Decision(action="hold", reasons=reasons)
    if runtime.last_exit_at is not None:
        from .timeframes import tf_seconds

        cooldown = s.cooldown_candles * tf_seconds(s.decision_timeframe)
        if now - runtime.last_exit_at < cooldown:
            reasons.append(f"cooldown: {int(cooldown - (now - runtime.last_exit_at))}s remaining")
            return Decision(action="hold", reasons=reasons)
    if runtime.trades_for(now) >= s.max_trades_per_day:
        reasons.append(f"daily trade limit {s.max_trades_per_day} reached")
        return Decision(action="hold", reasons=reasons)
    if price is None:
        reasons.append("no price available")
        return Decision(action="hold", reasons=reasons)

    # size by setup quality (decent -> 60%, strong -> 100%) and higher/lower agreement
    scale = 0.6 if j.setup_score < 2.5 else 1.0
    if j.higher_lower_agree < 0.5:
        scale *= 0.7
        reasons.append(f"higher/lower agreement low ({j.higher_lower_agree:.2f}); size reduced")
    qty, sl, tp, notional = size_position(j.entry_action, price, atr, equity, s, scale)
    reasons.append(
        f"entry {j.entry_action}: P={j.entry_probs.get(j.entry_action, 0):.2f} conf={j.entry_confidence:.2f} "
        f"setup={j.setup_score:.2f} choppy={j.choppy:.2f} overextended={j.overextended:.2f}"
    )
    return Decision(
        action=j.entry_action, reasons=reasons, size_scale=scale, qty=qty,
        stop_loss=sl, take_profit=tp, notional=notional,
    )
