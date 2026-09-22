"""Main loop: fetch -> indicators -> semantic state -> Jev -> policy -> execute -> log."""
from __future__ import annotations

import json
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings
from .exchange import LiveBroker, Market, PaperBroker, position_to_dict
from .judge import Judge
from .monitor import NewsMonitor
from .perspective import PerspectiveManager
from .policy import Decision, RuntimeState, decide
from .state import build_state
from .timeframes import next_close_time


class Bot:
    def __init__(self, settings: Settings):
        self.s = settings
        settings.validate()
        self.market = Market(settings)
        if settings.dry_run:
            self.broker = PaperBroker(settings, path=settings.log_dir / "paper_state.json")
        else:
            self.broker = LiveBroker(settings)
        self.judge = Judge(settings)
        self.perspective = PerspectiveManager(settings)
        self.monitor = NewsMonitor(settings, self.perspective, client=self.judge.client)
        if not self.perspective.enabled:
            print("[bot] ANTHROPIC_API_KEY not set: perspective layer disabled (Jev trades on indicators only)")
        self.runtime_path = settings.log_dir / "runtime_state.json"
        self.runtime = self._load_runtime()
        self.decisions_path = settings.log_dir / "decisions.jsonl"
        self._last_position = None   # position seen at the previous cycle (to detect exchange-side closes)
        self._last_price: float | None = None
        self._recount_trades_today()

    def _recount_trades_today(self) -> None:
        """Daily trade count from decisions.jsonl (robust to a lost or stale runtime file)."""
        today = time.strftime("%Y-%m-%d", time.gmtime())
        n = 0
        if self.decisions_path.exists():
            with self.decisions_path.open(encoding="utf-8") as f:
                for line in f:
                    if not line.startswith('{"time": "' + today):
                        continue
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if r.get("dry_run") == self.s.dry_run and (r.get("execution") or {}).get("opened"):
                        n += 1
        if n > self.runtime.trades_for(time.time()):
            self.runtime.trades_day, self.runtime.trades_today = today, n
            self._save_runtime()

    # ---------- persistence ----------
    def _load_runtime(self) -> RuntimeState:
        if self.runtime_path.exists():
            try:
                return RuntimeState(**json.loads(self.runtime_path.read_text()))
            except Exception:
                pass
        return RuntimeState()

    def _save_runtime(self) -> None:
        self.runtime_path.write_text(json.dumps(asdict(self.runtime)))

    def _log(self, record: dict) -> None:
        with self.decisions_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def _log_trade(self, pos, exit_price: float | None, reason: str, estimated: bool = True, pnl: float | None = None) -> None:
        """Append a closed trade to trades.jsonl (read by the local dashboard)."""
        if pos is None:
            return
        sign = 1 if pos.side == "long" else -1
        if pnl is None and exit_price and pos.entry_price:
            gross = (exit_price - pos.entry_price) * pos.qty * sign
            fees = (exit_price + pos.entry_price) * pos.qty * self.s.taker_fee_bps / 10_000
            pnl = gross - fees
        rec = {
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "side": pos.side, "qty": pos.qty, "entry_price": pos.entry_price, "exit_price": exit_price,
            "leverage": pos.leverage, "pnl_usd": pnl, "estimated": estimated, "reason": reason,
            "opened_at": pos.opened_at, "stop_loss": pos.stop_loss, "take_profit": pos.take_profit,
        }
        with (self.s.log_dir / "trades.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    # ---------- one decision cycle ----------
    def _price_shock(self, frames: dict) -> str | None:
        """Code-detected fast move on 1m closes: returns a description or None."""
        df = frames.get("1m")
        n = self.s.price_shock_window_min
        if df is None or len(df) < n + 1:
            return None
        move = (float(df["close"].iloc[-1]) / float(df["close"].iloc[-1 - n]) - 1) * 100
        if abs(move) >= self.s.price_shock_pct:
            return f"price move {move:+.2f}% in {n} minutes"
        return None

    def build(self, with_news: bool = True) -> tuple[dict, dict, object]:
        # 1) macro / intraday perspective: (re)built by Claude once per day or when stale
        # 2) news monitor: every NEWS_CHECK_MINUTES, or immediately after a fast price move,
        #    Jev screens new headlines and may trigger a perspective update
        if with_news and self.perspective.enabled:
            self.perspective.ensure_fresh()
        frames = self.market.fetch_frames(self.s.symbol, self.s.timeframes)
        if with_news:
            self.monitor.maybe_check(force=self._price_shock(frames))
        realtime = self.market.fetch_realtime(self.s.symbol)
        if isinstance(self.broker, PaperBroker) and realtime.get("last"):
            fill = self.broker.mark_to_market(realtime["last"])
            if fill:
                print(f"[paper] {fill['reason']} hit -> closed {fill['side']} pnl={fill['pnl']:.2f}")
                self.runtime.last_exit_at = time.time()
                self.runtime.position_opened_at = None
                self._save_runtime()
        position = self.broker.get_position(self.s.symbol)
        self._last_price = realtime.get("last") or self._last_price
        if position is None and self._last_position is not None:
            # the exchange closed it for us (stop or target hit) between cycles: read the actual fills
            lp = self._last_position
            fills = None
            if isinstance(self.broker, LiveBroker):
                since = int(((lp.opened_at or self.runtime.position_opened_at or time.time() - 86400) - 60) * 1000)
                fills = self.broker.closing_fills(self.s.symbol, lp.side, since)
            if fills and fills.get("exit_price"):
                entry_fee = lp.entry_price * lp.qty * self.s.taker_fee_bps / 10_000
                pnl = fills["realized_pnl"] - fills["fee"] - entry_fee
                which = "take-profit" if ((fills["exit_price"] > lp.entry_price) == (lp.side == "long")) else "stop-loss"
                self._log_trade(lp, fills["exit_price"], f"{which} hit at exchange", estimated=False, pnl=pnl)
                print(f"[bot] {which} hit at exchange: {lp.side} {lp.qty} exit {fills['exit_price']:.1f} pnl {pnl:+.2f}")
            else:
                self._log_trade(lp, self._last_price, "stop/target hit at exchange", estimated=True)
                print(f"[bot] position closed at exchange (stop/target): {lp.side} {lp.qty}")
            self.runtime.last_exit_at = time.time()
            self._save_runtime()
        self._last_position = position
        if position is None:
            if self.runtime.position_opened_at is not None:
                self.runtime.position_opened_at = None
                self._save_runtime()
        elif position.opened_at is None:
            # live positions carry no open time: use our own record, else reconstruct from fills
            if self.runtime.position_opened_at is None and isinstance(self.broker, LiveBroker):
                self.runtime.position_opened_at = self.broker.position_opened_at(self.s.symbol, position.side, position.qty)
                self._save_runtime()
            position.opened_at = self.runtime.position_opened_at
        perspective = self.perspective.for_jev()
        state, meta = build_state(frames, realtime, position, self.s, perspective=perspective)
        meta["perspective"] = perspective
        meta["shock_active"] = self.monitor.shock_active
        # exchange leverage bracket (private endpoint: use the broker's authenticated session when live)
        meta["leverage_limits"] = {}
        if isinstance(self.broker, LiveBroker):
            try:
                if not hasattr(self, "_authed_market"):
                    self._authed_market = Market(self.s, exchange=self.broker.ex)
                meta["leverage_limits"] = self._authed_market.leverage_limits(
                    self.s.symbol, max(self.broker.get_equity(), 1.0) * 5
                )
            except Exception as e:
                print(f"[bot] leverage bracket lookup failed: {e}")
        return state, meta, position

    def run_once(self, execute: bool = True) -> dict:
        t0 = time.time()
        state, meta, position = self.build()
        t1 = time.time()
        judgment = self.judge.judge(state, position is not None, position.side if position else None)
        t2 = time.time()
        equity = self.broker.get_equity()
        decision = decide(judgment, position, self.s, meta, equity, self.runtime)
        execution = None
        if execute:
            execution = self.execute(decision, position)
        t3 = time.time()

        record = {
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "symbol": self.s.symbol,
            "price": meta.get("price"),
            "atr": meta.get("atr"),
            "position_before": position_to_dict(position),
            "equity": equity,
            "judgment": judgment.to_dict(),
            "decision": decision.to_dict(),
            "execution": execution,
            "perspective": {
                "present": meta.get("perspective") is not None,
                "generated_at_utc": self.perspective.current.generated_at_utc if self.perspective.current else None,
                "kind": self.perspective.current.kind if self.perspective.current else None,
                "shock_active": meta.get("shock_active", False),
            },
            "state": state,
            "timing_sec": {"data": round(t1 - t0, 2), "jev": round(t2 - t1, 2), "exec": round(t3 - t2, 2)},
            "dry_run": self.s.dry_run,
        }
        self._log(record)
        self.print_summary(record)
        return record

    def execute(self, d: Decision, position) -> dict | None:
        if d.action in ("long", "short"):
            qty = self.market.amount_to_precision(self.s.symbol, d.qty or 0.0)
            min_amt = self.market.min_amount(self.s.symbol)
            min_notional = self.market.min_notional(self.s.symbol)
            price = d.notional / d.qty if d.qty else 0
            if qty < min_amt or qty * price < min_notional:
                return {"skipped": f"qty {qty} below exchange minimum (amount {min_amt}, notional {min_notional})"}
            sl = self.market.price_to_precision(self.s.symbol, d.stop_loss)
            tp = self.market.price_to_precision(self.s.symbol, d.take_profit)
            res = self.broker.open(self.s.symbol, d.action, qty, sl, tp, leverage=d.leverage)
            if "error" not in res:
                self.runtime.register_trade(time.time())
                self.runtime.position_opened_at = time.time()
                self._save_runtime()
            return {"opened": d.action, "qty": qty, "stop_loss": sl, "take_profit": tp, **res}
        if d.action == "exit":
            res = self.broker.close(self.s.symbol)
            if res.get("closed"):
                self.runtime.last_exit_at = time.time()
                self.runtime.position_opened_at = None
                self._save_runtime()
                exit_px = res.get("exit") or self._last_price
                self._log_trade(position, exit_px, "bot exit signal: " + "; ".join(d.reasons)[:120],
                                estimated=res.get("pnl") is None, pnl=res.get("pnl"))
                self._last_position = None
            return res
        return None

    @staticmethod
    def print_summary(r: dict) -> None:
        j, d = r["judgment"], r["decision"]
        pos = r["position_before"]
        pos_txt = "flat" if not pos else f"{pos['side']} {pos['qty']} @ {pos['entry_price']}"
        line = (
            f"{r['time']} {r['symbol']} px={r['price']} pos={pos_txt} | "
            f"jev entry={j['entry_action']}({j['entry_probs'].get(j['entry_action'], 0):.2f}, conf {j['entry_confidence']:.2f}) "
            f"setup={j['setup_score']:.2f} choppy={j['choppy']:.2f} overext={j['overextended']:.2f}"
        )
        if j.get("position_action"):
            line += f" pos_action={j['position_action']}({(j['position_probs'] or {}).get('exit', 0):.2f}) inval={j['thesis_invalidated']:.2f}"
        line += f" | ACTION={d['action'].upper()}"
        if r.get("execution"):
            line += f" exec={json.dumps(r['execution'], default=str)[:160]}"
        print(line)
        for reason in d["reasons"]:
            print(f"    - {reason}")

    # ---------- loop ----------
    def run(self) -> None:
        mode = "PAPER" if self.s.dry_run else ("TESTNET" if self.s.binance_testnet else "LIVE")
        print(f"[bot] {mode} {self.s.symbol} tfs={self.s.timeframes} decision_tf={self.s.decision_timeframe} "
              f"model={self.s.typesafe_model}")
        while True:
            try:
                self.run_once()
            except KeyboardInterrupt:
                raise
            except Exception as e:
                print(f"[bot] cycle error: {type(e).__name__}: {e}")
                self._log({"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "error": repr(e)})
            if self.s.loop_interval_sec > 0:
                time.sleep(self.s.loop_interval_sec)
            else:
                wake = next_close_time(self.s.decision_timeframe, time.time()) + 3
                delay = max(5.0, wake - time.time())
                print(f"[bot] next decision in {int(delay)}s (candle close of {self.s.decision_timeframe})")
                time.sleep(delay)
