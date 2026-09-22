"""Re-apply the policy to recorded Jev answers without calling Jev again.

`logs/decisions.jsonl` keeps every judgment (probabilities, scores, nouls) next to
the price at that moment. Changing a threshold does not change what Jev would
have answered, so thresholds can be tuned offline by replaying the recorded
answers through `policy.decide` and simulating fills on the recorded prices.

    python -m jev_trade evaluate --set MIN_ENTRY_PROB=0.55 --set MIN_SETUP_SCORE=1.8

Fills are approximate: entries at the recorded price, stops/targets checked
against later recorded prices only (no intra-cycle highs/lows).
"""
from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import replace
from pathlib import Path

from .config import Settings
from .exchange import PaperBroker
from .judge import Judgment
from .policy import RuntimeState, decide


def _judgment(d: dict) -> Judgment:
    fields = {k: v for k, v in d.items() if k in Judgment.__dataclass_fields__ and k != "raw"}
    return Judgment(**fields)


def apply_overrides(settings: Settings, overrides: dict[str, str]) -> Settings:
    """KEY=VALUE overrides use the same names as .env (case-insensitive attribute match)."""
    values = {}
    for k, v in overrides.items():
        attr = k.lower()
        if not hasattr(settings, attr):
            raise SystemExit(f"unknown setting {k}")
        cur = getattr(settings, attr)
        if isinstance(cur, bool):
            values[attr] = v.strip().lower() in ("1", "true", "yes", "on")
        elif isinstance(cur, int):
            values[attr] = int(v)
        elif isinstance(cur, float):
            values[attr] = float(v)
        else:
            values[attr] = v
    return replace(settings, **values)


def evaluate(settings: Settings, path: Path | None = None, overrides: dict[str, str] | None = None,
             equity: float = 10_000.0, verbose: bool = False) -> dict:
    path = path or settings.log_dir / "decisions.jsonl"
    if not path.exists():
        raise SystemExit(f"{path} not found; run the bot first")
    s = apply_overrides(settings, overrides or {})
    records = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("judgment") and r.get("price"):
                records.append(r)
    if not records:
        raise SystemExit("no usable records")

    broker = PaperBroker(s, path=None, equity=equity, slippage_bps=s.replay_slippage_bps, fee_bps=s.replay_fee_bps)
    runtime = RuntimeState()
    actions: Counter = Counter()
    agree = 0
    for r in records:
        price = float(r["price"])
        ts = _ts(r["time"])
        fill = broker.mark_to_market(price)
        if fill:
            runtime.last_exit_at = ts
        position = broker.get_position(s.symbol)
        j = _judgment(r["judgment"])
        # a recorded position-side answer only exists if a position was open at record time;
        # if our simulated position state differs, the position questions are missing -> treat as keep
        if position is not None and j.position_action is None:
            j.position_action, j.position_probs = "keep", {"keep": 1.0, "exit": 0.0}
        state_persp = (r.get("state") or {}).get("perspective")
        meta = {"price": price, "atr": _atr_from_record(r, price), "perspective": state_persp,
                "shock_active": bool((r.get("perspective") or {}).get("shock_active"))}
        d = decide(j, position, s, meta, broker.get_equity(), runtime, now=ts)
        actions[d.action] += 1
        if d.action == (r.get("decision") or {}).get("action"):
            agree += 1
        if d.action in ("long", "short"):
            res = broker.open(s.symbol, d.action, d.qty, d.stop_loss, d.take_profit)
            if "error" not in res:
                broker.state["position"]["opened_at"] = ts
                runtime.register_trade(ts)
        elif d.action == "exit":
            broker.close(s.symbol)
            runtime.last_exit_at = ts
        if verbose:
            print(f"{r['time']} px={price:.1f} -> {d.action} | {'; '.join(d.reasons)[:120]}")

    trades = broker.state["trades"]
    wins = [t for t in trades if t["pnl"] > 0]
    return {
        "records": len(records),
        "overrides": overrides or {},
        "actions": dict(actions),
        "agreement_with_recorded_decisions": round(agree / len(records), 3),
        "trades": len(trades),
        "win_rate": round(len(wins) / len(trades), 3) if trades else None,
        "total_pnl": round(sum(t["pnl"] for t in trades), 2),
        "final_equity": round(broker.get_equity(), 2),
        "open_position": broker.state["position"],
    }


def _ts(iso: str) -> float:
    from datetime import datetime

    return datetime.fromisoformat(iso).timestamp()


def _atr_from_record(r: dict, price: float) -> float:
    if r.get("atr"):
        return float(r["atr"])
    return price * 0.005  # older records without ATR: 0.5% fallback
