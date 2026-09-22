"""Assemble the compact JSON `state` that Jev reads.

Only semantic summaries go in; raw candles and most numbers stay in code.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import pandas as pd

from .config import Settings
from .exchange import Position
from .indicators import compute, yearly_context
from .semantics import (
    book_label,
    change_label,
    funding_label,
    liq_label,
    pnl_label,
    summarize_timeframe,
    summarize_yearly,
)
from .timeframes import CONTEXT_ONLY, tf_seconds

HIGHER_TFS = ("1h", "5h", "1d", "1w")
LOWER_TFS = ("1m", "5m", "10m", "15m", "30m")


def build_state(
    frames: dict[str, pd.DataFrame],
    realtime: dict | None,
    position: Position | None,
    settings: Settings,
    now: float | None = None,
    perspective: dict | None = None,
) -> tuple[dict, dict]:
    """Return (state_for_jev, meta_for_code).

    meta carries the numbers code needs later (ATR, price) without sending them to Jev twice.
    """
    now = now or time.time()
    timeframes: dict[str, dict] = {}
    meta: dict = {"indicators": {}}
    for tf, df in frames.items():
        if tf in CONTEXT_ONLY:
            y = yearly_context(df, weekly=frames.get("1w"))
            timeframes[tf] = summarize_yearly(y)
            meta["indicators"][tf] = y
            continue
        ind = compute(df)
        meta["indicators"][tf] = ind
        timeframes[tf] = summarize_timeframe(ind)

    dec = meta["indicators"].get(settings.decision_timeframe, {})
    meta["atr"] = dec.get("atr")
    meta["close"] = dec.get("close")

    price = (realtime or {}).get("last") or meta.get("close")
    meta["price"] = price

    state: dict = {
        "symbol": settings.symbol,
        "time_utc": datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "timeframe_groups": {
            "higher": [t for t in HIGHER_TFS if t in timeframes],
            "lower": [t for t in LOWER_TFS if t in timeframes],
            "context": [t for t in CONTEXT_ONLY if t in timeframes],
        },
        "timeframes": timeframes,
    }

    if realtime:
        state["market"] = {
            "last_price": realtime.get("last"),
            "change_24h": change_label(realtime.get("change_24h_pct")),
            "funding_rate": funding_label(realtime.get("funding_rate")),
            "order_book": book_label(realtime.get("bid_vol_20", 0.0), realtime.get("ask_vol_20", 0.0)),
        }
        hi, lo, last = realtime.get("high_24h"), realtime.get("low_24h"), realtime.get("last")
        if hi and lo and last and hi > lo:
            pos_in_day = (last - lo) / (hi - lo)
            where = "near the 24h high" if pos_in_day >= 0.85 else "near the 24h low" if pos_in_day <= 0.15 else "inside the 24h range"
            state["market"]["position_in_24h_range"] = where

    if perspective:
        state["perspective"] = perspective

    if position is None:
        state["position"] = {"status": "flat", "note": "no open position"}
    else:
        held = ""
        if position.opened_at:
            candles = int((now - position.opened_at) // tf_seconds(settings.decision_timeframe))
            held = f"{candles} candles of {settings.decision_timeframe}"
        liq_dist = None
        if position.liquidation_price and price:
            liq_dist = abs(price / position.liquidation_price - 1) * 100
        sl_txt = "none"
        if position.stop_loss and price:
            sl_txt = f"set, {abs(price / position.stop_loss - 1) * 100:.2f}% away"
        tp_txt = "none"
        if position.take_profit and price:
            tp_txt = f"set, {abs(position.take_profit / price - 1) * 100:.2f}% away"
        state["position"] = {
            "status": "open",
            "side": position.side,
            "unrealized_pnl": pnl_label(position.unrealized_pnl_pct, position.leverage),
            "held_for": held or "unknown",
            "distance_to_liquidation": liq_label(liq_dist),
            "stop_loss": sl_txt,
            "take_profit": tp_txt,
        }
    return state, meta
