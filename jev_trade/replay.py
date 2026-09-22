"""Walk-forward replay on recent history with the paper broker.

For each of the last N closed decision-timeframe candles, rebuild every timeframe
as it would have looked at that moment, ask Jev, apply the policy, and simulate
fills. Compared with a naive replay this one restores:

- funding rate from Binance's funding history (the `market` block, minus order book,
  which has no public history)
- intra-candle fills on 1m bars: stops are checked minute by minute, gaps fill at the
  bar open, stops before targets when both are touched
- slippage (REPLAY_SLIPPAGE_BPS) and taker fees (REPLAY_FEE_BPS) on every fill
- optionally a per-day macro perspective written by Claude from web search as of that
  date (`--with-perspective`), so the perspective gates are exercised too

Still idealized: no partial fills, no latency, no exchange downtime.
"""
from __future__ import annotations

import math
import time
from collections import Counter
from datetime import datetime, timezone

import pandas as pd

from .config import Settings
from .exchange import Market, PaperBroker
from .judge import Judge
from .policy import RuntimeState, decide
from .semantics import change_label, funding_label
from .state import build_state
from .timeframes import CONTEXT_ONLY, DERIVED, resample, source_for, tf_seconds


def _slice_closed(df: pd.DataFrame, tf: str, t: pd.Timestamp) -> pd.DataFrame:
    """Candles that were fully closed at time t (open + tf <= t)."""
    step = pd.Timedelta(seconds=tf_seconds(tf) if tf != "1M" else 30 * 86400)
    return df[df.index + step <= t]


def _funding_at(history: list[tuple[int, float]], t_ms: int) -> float | None:
    rate = None
    for ts, r in history:
        if ts <= t_ms:
            rate = r
        else:
            break
    return rate


def replay(
    settings: Settings,
    steps: int = 50,
    equity: float = 10_000.0,
    verbose: bool = True,
    with_perspective: bool = False,
) -> dict:
    settings.validate()
    market = Market(settings)
    judge = Judge(settings)
    dec_tf = settings.decision_timeframe
    dec_sec = tf_seconds(dec_tf)

    # native frames with enough history for `steps` walk-forward points
    native: dict[str, pd.DataFrame] = {}
    for tf in settings.timeframes:
        src, base = source_for(tf)
        extra = math.ceil(steps * dec_sec / tf_seconds(src)) + 5
        need = base + extra
        if src not in native or len(native[src]) < need:
            native[src] = market.fetch_native(settings.symbol, src, need)
    # 1m bars for intra-candle fills (and 24h change), independent of the timeframe list
    need_1m = math.ceil((steps + 2) * dec_sec / 60) + 24 * 60 + 10
    if "1m" not in native or len(native["1m"]) < need_1m:
        native["1m"] = market.fetch_native(settings.symbol, "1m", need_1m)
    m1 = native["1m"]

    dec_df = native[dec_tf] if dec_tf in native else resample(native[DERIVED[dec_tf][0]], dec_tf)
    dec_df = dec_df.iloc[:-1]  # drop forming candle
    points = dec_df.index[-steps - 1 : -1]  # decide at the close of each of these candles

    funding = market.fetch_funding_history(settings.symbol, int(points[0].timestamp() * 1000) - 9 * 3600 * 1000)

    perspectives: dict[str, dict | None] = {}
    if with_perspective:
        from .perspective import PerspectiveManager

        pm = PerspectiveManager(settings)
        if not pm.enabled:
            raise SystemExit("--with-perspective needs ANTHROPIC_API_KEY")
        for day in sorted({p.strftime("%Y-%m-%d") for p in points}):
            sp = pm.build_historical(day)
            pm.current = sp
            perspectives[day] = pm.for_jev(now=sp.generated_at)
            if verbose:
                print(f"[perspective {day}] bias={sp.perspective.macro_bias} risk={sp.perspective.event_risk_level} stance={sp.perspective.trading_stance}")

    broker = PaperBroker(settings, path=None, equity=equity,
                         slippage_bps=settings.replay_slippage_bps, fee_bps=settings.replay_fee_bps)
    runtime = RuntimeState()
    actions: Counter = Counter()
    tokens = 0
    t_start = time.time()

    for i, open_ts in enumerate(points):
        t = open_ts + pd.Timedelta(seconds=dec_sec)  # this candle's close time
        frames: dict[str, pd.DataFrame] = {}
        for tf in settings.timeframes:
            src, _ = source_for(tf)
            base = _slice_closed(native[src], src, t)
            if tf in DERIVED:
                base = resample(base, tf)
                base = _slice_closed(base, tf, t)
            frames[tf] = base
        close_px = float(frames[dec_tf]["close"].iloc[-1])
        broker.mark_to_market(close_px)
        position = broker.get_position(settings.symbol)

        # realtime block reconstructed from history (no order book)
        m1_closed = _slice_closed(m1, "1m", t)
        px_24h_ago = float(m1_closed["close"].iloc[-24 * 60]) if len(m1_closed) > 24 * 60 else None
        realtime = {
            "last": close_px,
            "change_24h_pct": (close_px / px_24h_ago - 1) * 100 if px_24h_ago else None,
            "funding_rate": _funding_at(funding, int(t.timestamp() * 1000)),
            "high_24h": float(m1_closed["high"].iloc[-24 * 60 :].max()) if len(m1_closed) else None,
            "low_24h": float(m1_closed["low"].iloc[-24 * 60 :].min()) if len(m1_closed) else None,
            "bid_vol_20": 0.0, "ask_vol_20": 0.0,
        }
        perspective = perspectives.get(t.strftime("%Y-%m-%d")) if with_perspective else None
        state, meta = build_state(frames, realtime, position, settings, now=t.timestamp(), perspective=perspective)
        state["market"].pop("order_book", None)  # no historical order book
        meta["price"] = close_px
        meta["perspective"] = perspective
        meta["shock_active"] = False

        j = judge.judge(state, position is not None, position.side if position else None)
        tokens += j.input_tokens
        d = decide(j, position, settings, meta, broker.get_equity(), runtime, now=t.timestamp())
        actions[d.action] += 1
        exec_txt = ""
        if d.action in ("long", "short"):
            r = broker.open(settings.symbol, d.action, d.qty, d.stop_loss, d.take_profit)
            if "error" not in r:
                broker.state["position"]["opened_at"] = t.timestamp()
                runtime.register_trade(t.timestamp())
            exec_txt = f" -> open {d.action} @ {r.get('filled_at', 0):.1f} sl={d.stop_loss:.1f} tp={d.take_profit:.1f}"
        elif d.action == "exit":
            r = broker.close(settings.symbol)
            runtime.last_exit_at = t.timestamp()
            exec_txt = f" -> exit pnl {r.get('pnl', 0):+.2f}"

        # walk the next decision candle minute by minute for SL/TP fills
        nxt_end = t + pd.Timedelta(seconds=dec_sec)
        window = m1[(m1.index >= t) & (m1.index < nxt_end)]
        for ts, bar in window.iterrows():
            fill = broker.mark_to_market(float(bar["close"]), float(bar["high"]), float(bar["low"]), float(bar["open"]))
            if fill:
                runtime.last_exit_at = (ts + pd.Timedelta(minutes=1)).timestamp()
                exec_txt += f" | {fill['reason']} @ {ts:%H:%M} pnl {fill['pnl']:+.2f}"
                break
        if verbose:
            pos_txt = "flat" if position is None else position.side
            print(
                f"[{i + 1:>3}/{steps}] {t:%m-%d %H:%M} px={close_px:.1f} pos={pos_txt} "
                f"entry={j.entry_action}({j.entry_probs.get(j.entry_action, 0):.2f}) setup={j.setup_score:.2f} "
                f"choppy={j.choppy:.2f} -> {d.action.upper()}{exec_txt}"
            )

    trades = broker.state["trades"]
    wins = [x for x in trades if x["pnl"] > 0]
    return {
        "steps": steps,
        "window_utc": f"{points[0]:%Y-%m-%d %H:%M} -> {points[-1]:%Y-%m-%d %H:%M}",
        "symbol": settings.symbol,
        "decision_timeframe": dec_tf,
        "with_perspective": with_perspective,
        "actions": dict(actions),
        "trades": len(trades),
        "win_rate": (len(wins) / len(trades)) if trades else None,
        "total_pnl": round(sum(x["pnl"] for x in trades), 2),
        "final_equity": round(broker.get_equity(), 2),
        "open_position": broker.state["position"],
        "slippage_bps": settings.replay_slippage_bps,
        "fee_bps": settings.replay_fee_bps,
        "jev_input_tokens": tokens,
        "elapsed_sec": round(time.time() - t_start, 1),
    }
