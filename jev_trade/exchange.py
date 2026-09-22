"""Binance USDT-M futures access via ccxt: market data, live broker, paper broker."""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import ccxt
import pandas as pd

from .config import Settings
from .timeframes import CONTEXT_ONLY, DERIVED, resample, source_for


@dataclass
class Position:
    side: str  # "long" | "short"
    qty: float
    entry_price: float
    leverage: float
    mark_price: float | None = None
    unrealized_pnl_pct: float | None = None  # % of notional (not leveraged)
    liquidation_price: float | None = None
    opened_at: float | None = None  # epoch seconds
    stop_loss: float | None = None
    take_profit: float | None = None

    @property
    def notional(self) -> float:
        return self.qty * (self.mark_price or self.entry_price)


def _use_testnet(ex: ccxt.Exchange, host: str) -> None:
    """Switch a ccxt binanceusdm instance to the futures testnet ("Demo Trading").

    ccxt's sandbox mode still points at the legacy testnet.binancefuture.com host, which now
    redirects; rewrite every API url to the official demo host.
    """
    ex.set_sandbox_mode(True)
    def rewrite(u):
        if isinstance(u, str):
            return u.replace("testnet.binancefuture.com", host)
        if isinstance(u, dict):
            return {k: rewrite(v) for k, v in u.items()}
        return u
    ex.urls["api"] = rewrite(ex.urls["api"])


def _ohlcv_to_frame(rows: list) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.set_index("ts").astype(float)
    return df


class Market:
    """Public market data (no keys needed)."""

    def __init__(self, settings: Settings, exchange: ccxt.Exchange | None = None):
        self.s = settings
        self.ex = exchange or ccxt.binanceusdm({"enableRateLimit": True})
        if settings.market_data_testnet and exchange is None:
            _use_testnet(self.ex, settings.binance_testnet_host)
        self.ex.load_markets()

    def fetch_native(self, symbol: str, tf: str, limit: int) -> pd.DataFrame:
        """Fetch the most recent `limit` candles, paging backwards (Binance caps ~1000-1500 per call)."""
        tf_ms = self.ex.parse_timeframe(tf) * 1000
        batch = 1000
        rows: list = self.ex.fetch_ohlcv(symbol, tf, limit=min(limit, batch))
        oldest = rows[0][0] if rows else None
        while rows and len(rows) < limit and oldest is not None:
            since = oldest - batch * tf_ms
            chunk = self.ex.fetch_ohlcv(symbol, tf, since=since, limit=batch)
            chunk = [c for c in chunk if c[0] < oldest]
            if not chunk:
                break
            rows = chunk + rows
            oldest = chunk[0][0]
        df = _ohlcv_to_frame(rows)
        df = df[~df.index.duplicated(keep="last")].sort_index()
        return df.iloc[-limit:]

    def fetch_funding_history(self, symbol: str, since_ms: int) -> list[tuple[int, float]]:
        """[(timestamp_ms, rate), ...] ascending, from `since_ms` to now (paged)."""
        out: list[tuple[int, float]] = []
        cursor = since_ms
        for _ in range(50):
            chunk = self.ex.fetch_funding_rate_history(symbol, since=cursor, limit=1000)
            if not chunk:
                break
            out.extend((int(c["timestamp"]), float(c["fundingRate"])) for c in chunk)
            if len(chunk) < 1000:
                break
            cursor = int(chunk[-1]["timestamp"]) + 1
        return sorted(set(out))

    def fetch_frames(self, symbol: str, timeframes: list[str]) -> dict[str, pd.DataFrame]:
        """Return CLOSED candles for every requested timeframe (last forming candle dropped).

        Derived timeframes (10m, 5h, ...) are resampled from native ones; '1y' maps to
        365 daily candles for the yearly context block.
        """
        native_cache: dict[tuple[str, int], pd.DataFrame] = {}
        out: dict[str, pd.DataFrame] = {}
        for tf in timeframes:
            src, n = source_for(tf)
            key = (src, n)
            if key not in native_cache:
                native_cache[key] = self.fetch_native(symbol, src, n)
            df = native_cache[key]
            if tf in DERIVED:
                df = resample(df, tf)
            out[tf] = df.iloc[:-1]  # drop the forming candle
        return out

    def fetch_realtime(self, symbol: str) -> dict:
        t = self.ex.fetch_ticker(symbol)
        rt = {
            "last": t.get("last"),
            "change_24h_pct": t.get("percentage"),
            "high_24h": t.get("high"),
            "low_24h": t.get("low"),
            "quote_volume_24h": t.get("quoteVolume"),
            "timestamp": t.get("timestamp"),
        }
        try:
            fr = self.ex.fetch_funding_rate(symbol)
            rt["funding_rate"] = fr.get("fundingRate")
            rt["mark_price"] = fr.get("markPrice")
        except Exception:
            rt["funding_rate"] = None
            rt["mark_price"] = None
        try:
            ob = self.ex.fetch_order_book(symbol, 20)
            rt["bid_vol_20"] = sum(b[1] for b in ob["bids"])
            rt["ask_vol_20"] = sum(a[1] for a in ob["asks"])
            rt["spread_pct"] = (
                (ob["asks"][0][0] - ob["bids"][0][0]) / ob["asks"][0][0] * 100
                if ob["asks"] and ob["bids"]
                else None
            )
        except Exception:
            rt["bid_vol_20"] = rt["ask_vol_20"] = 0.0
            rt["spread_pct"] = None
        return rt

    # precision helpers
    def amount_to_precision(self, symbol: str, qty: float) -> float:
        return float(self.ex.amount_to_precision(symbol, qty))

    def price_to_precision(self, symbol: str, price: float) -> float:
        return float(self.ex.price_to_precision(symbol, price))

    def min_notional(self, symbol: str) -> float:
        m = self.ex.market(symbol)
        return float((m.get("limits", {}).get("cost", {}) or {}).get("min") or 5.0)

    def min_amount(self, symbol: str) -> float:
        m = self.ex.market(symbol)
        return float((m.get("limits", {}).get("amount", {}) or {}).get("min") or 0.0)

    def leverage_limits(self, symbol: str, notional: float) -> dict:
        """Exchange bracket for this notional: {max_leverage, maint_rate}. Cached per process."""
        tiers = getattr(self, "_tiers_cache", None)
        if tiers is None:
            try:
                tiers = self.ex.fetch_leverage_tiers([symbol]).get(symbol, [])
            except Exception as e:
                print(f"[market] fetch_leverage_tiers failed: {e}")
                tiers = []
            self._tiers_cache = tiers
        for t in tiers:
            lo, hi = float(t.get("minNotional") or 0), float(t.get("maxNotional") or 1e18)
            if lo <= notional <= hi:
                return {"max_leverage": float(t.get("maxLeverage") or 0) or None,
                        "maint_rate": float(t.get("maintenanceMarginRate") or 0) or None}
        return {}


class LiveBroker:
    """Real order execution on Binance USDT-M futures (one-way position mode assumed)."""

    def __init__(self, settings: Settings):
        self.s = settings
        self.ex = ccxt.binanceusdm(
            {
                "apiKey": settings.binance_api_key,
                "secret": settings.binance_api_secret,
                "enableRateLimit": True,
                "options": {"defaultType": "future"},
            }
        )
        if settings.binance_testnet:
            _use_testnet(self.ex, settings.binance_testnet_host)
            print(f"[broker] TESTNET / Demo Trading orders -> https://{settings.binance_testnet_host}")
        self.ex.load_markets()
        self._configured: set[str] = set()
        self.hedge_mode = self._detect_hedge_mode()
        if self.hedge_mode and not settings.allow_hedge_mode:
            raise SystemExit(
                "Binance futures account is in HEDGE position mode. Switch it to one-way mode "
                "(Futures > Preferences > Position Mode) or set ALLOW_HEDGE_MODE=true to use positionSide orders."
            )
        if self.hedge_mode:
            print("[broker] hedge mode: orders will carry positionSide=LONG/SHORT")

    def _detect_hedge_mode(self) -> bool:
        try:
            r = self.ex.fapiPrivateGetPositionSideDual()
            return str(r.get("dualSidePosition", "false")).lower() == "true"
        except Exception as e:
            print(f"[broker] could not read position mode ({e}); assuming one-way")
            return False

    def _pos_params(self, side: str, closing: bool) -> dict:
        """Order params for one-way vs hedge mode. `side` is the position side (long/short)."""
        if self.hedge_mode:
            return {"positionSide": "LONG" if side == "long" else "SHORT"}  # reduceOnly is rejected in hedge mode
        return {"reduceOnly": True} if closing else {}

    def position_opened_at(self, symbol: str, side: str, qty: float) -> float | None:
        """Estimate when the current position was opened by walking recent fills backwards
        until they account for the whole position size (Binance does not expose an open time)."""
        try:
            trades = self.ex.fetch_my_trades(symbol, limit=500)
        except Exception as e:
            print(f"[broker] fetch_my_trades failed: {e}")
            return None
        sign = 1 if side == "long" else -1
        acc = 0.0
        for t in sorted(trades, key=lambda x: x["timestamp"] or 0, reverse=True):
            signed = float(t["amount"]) * (1 if t["side"] == "buy" else -1) * sign
            acc += signed
            if acc >= qty * 0.999:
                return (t["timestamp"] or 0) / 1000 or None
        return None

    def closing_fills(self, symbol: str, side: str, since_ms: int) -> dict | None:
        """Actual exit of a position closed at the exchange: fills on the closing side since `since_ms`.

        Returns {exit_price (VWAP), qty, realized_pnl (exchange-reported, net of nothing), fee, time}.
        """
        try:
            trades = self.ex.fetch_my_trades(symbol, since=since_ms, limit=200)
        except Exception as e:
            print(f"[broker] fetch_my_trades failed: {e}")
            return None
        closing_side = "sell" if side == "long" else "buy"
        fills = [t for t in trades if t.get("side") == closing_side]
        if not fills:
            return None
        qty = sum(float(t["amount"]) for t in fills)
        vwap = sum(float(t["price"]) * float(t["amount"]) for t in fills) / qty if qty else None
        realized = sum(float((t.get("info") or {}).get("realizedPnl") or 0) for t in fills)
        fee = sum(float((t.get("fee") or {}).get("cost") or 0) for t in fills)
        return {"exit_price": vwap, "qty": qty, "realized_pnl": realized, "fee": fee,
                "time": max(int(t["timestamp"] or 0) for t in fills) / 1000}

    def _configure(self, symbol: str) -> None:
        if symbol in self._configured:
            return
        try:
            self.ex.set_margin_mode(self.s.margin_mode, symbol)
        except Exception as e:  # already set / not modifiable with open position
            print(f"[broker] set_margin_mode skipped: {e}")
        self._configured.add(symbol)

    def get_equity(self) -> float:
        bal = self.ex.fetch_balance()
        usdt = bal.get("USDT", {}) or {}
        return float(usdt.get("total") or bal.get("total", {}).get("USDT") or 0.0)

    def get_position(self, symbol: str) -> Position | None:
        open_legs = [p for p in self.ex.fetch_positions([symbol]) if float(p.get("contracts") or 0) > 0]
        if len(open_legs) > 1:  # hedge mode with both sides open: manage the larger leg
            open_legs.sort(key=lambda p: float(p.get("contracts") or 0), reverse=True)
            print(f"[broker] WARNING: {len(open_legs)} open legs on {symbol}; managing the largest only")
        for p in open_legs[:1]:
            qty = float(p.get("contracts") or 0)
            entry = float(p.get("entryPrice") or 0)
            mark = float(p.get("markPrice") or entry)
            side = p.get("side") or ("long" if float(p.get("info", {}).get("positionAmt", 0)) > 0 else "short")
            pnl_pct = None
            if entry:
                pnl_pct = (mark / entry - 1) * 100 * (1 if side == "long" else -1)
            liq = p.get("liquidationPrice")
            sl = tp = None
            try:
                # Binance keeps conditional (STOP_MARKET / TAKE_PROFIT_MARKET) orders on the algo endpoint;
                # ccxt exposes them with params={"trigger": True}. Classify by trigger price vs entry.
                for o in self.fetch_trigger_orders(symbol):
                    price = o.get("stopPrice") or o.get("triggerPrice") or (o.get("info", {}) or {}).get("stopPrice")
                    if price is None:
                        continue
                    price = float(price)
                    is_stop = (price < entry) if side == "long" else (price > entry)
                    if is_stop:
                        sl = price
                    else:
                        tp = price
            except Exception:
                pass
            lev = p.get("leverage")
            if not lev:  # Binance no longer reports leverage on the position; derive it from margin
                notional_v = abs(float(p.get("notional") or 0)) or qty * mark
                im = float(p.get("initialMargin") or 0) or float((p.get("info", {}) or {}).get("isolatedWallet") or 0)
                lev = round(notional_v / im) if im > 0 else self.s.leverage
            return Position(
                side=side,
                qty=qty,
                entry_price=entry,
                leverage=float(lev),
                mark_price=mark,
                unrealized_pnl_pct=pnl_pct,
                liquidation_price=float(liq) if liq else None,
                opened_at=None,
                stop_loss=sl,
                take_profit=tp,
            )
        return None

    def fetch_trigger_orders(self, symbol: str) -> list:
        return self.ex.fetch_open_orders(symbol, params={"trigger": True})

    def cancel_everything(self, symbol: str) -> None:
        """Cancel regular AND conditional orders; a plain cancel_all leaves conditional orders alive."""
        for params in ({}, {"trigger": True}):
            try:
                self.ex.cancel_all_orders(symbol, params=params)
            except Exception as e:
                print(f"[broker] cancel_all_orders{params or ''} failed: {e}")

    def set_leverage(self, symbol: str, leverage: int) -> int:
        """Set per-trade leverage; returns the leverage actually in effect."""
        try:
            self.ex.set_leverage(int(leverage), symbol)
            return int(leverage)
        except Exception as e:
            print(f"[broker] set_leverage({leverage}) failed: {e}; keeping the current setting")
            return int(self.s.leverage)

    def open(self, symbol: str, side: str, qty: float, stop_loss: float, take_profit: float,
             leverage: int | None = None) -> dict:
        # stale reduce-only stops from an earlier position would fire against the new one: clear them first
        try:
            if self.fetch_trigger_orders(symbol):
                print("[broker] clearing stale conditional orders before entry")
                self.cancel_everything(symbol)
        except Exception as e:
            print(f"[broker] could not check stale orders: {e}")
        self._configure(symbol)
        lev_in_effect = self.set_leverage(symbol, leverage or self.s.leverage)
        order_side = "buy" if side == "long" else "sell"
        exit_side = "sell" if side == "long" else "buy"
        entry = self.ex.create_order(symbol, "market", order_side, qty, None, self._pos_params(side, closing=False))
        result = {"entry_order_id": entry.get("id"), "stop_order_id": None, "tp_order_id": None,
                  "opened_at": time.time(), "leverage": lev_in_effect}
        try:
            sl = self.ex.create_order(
                symbol, "market", exit_side, qty, None,
                {"stopLossPrice": self.ex.price_to_precision(symbol, stop_loss), **self._pos_params(side, closing=True)},
            )
            result["stop_order_id"] = sl.get("id")
        except Exception as e:
            result["stop_error"] = str(e)
        try:
            tp = self.ex.create_order(
                symbol, "market", exit_side, qty, None,
                {"takeProfitPrice": self.ex.price_to_precision(symbol, take_profit), **self._pos_params(side, closing=True)},
            )
            result["tp_order_id"] = tp.get("id")
        except Exception as e:
            result["take_profit_error"] = str(e)
        return result

    def close(self, symbol: str) -> dict:
        pos = self.get_position(symbol)
        if pos is None:
            return {"closed": False, "reason": "no position"}
        self.cancel_everything(symbol)
        side = "sell" if pos.side == "long" else "buy"
        o = self.ex.create_order(symbol, "market", side, pos.qty, None, self._pos_params(pos.side, closing=True))
        return {"closed": True, "order": o.get("id"), "qty": pos.qty, "side": pos.side}


class PaperBroker:
    """Simulated broker: fills at the current price, enforces SL/TP on each mark-to-market."""

    def __init__(
        self, settings: Settings, path: Path | None = None, equity: float = 10_000.0,
        slippage_bps: float = 0.0, fee_bps: float = 5.0,
    ):
        self.s = settings
        self.path = path
        self.slippage = slippage_bps / 10_000
        self.fee = fee_bps / 10_000
        self.state = {"equity": equity, "position": None, "trades": []}
        if path and path.exists():
            self.state = json.loads(path.read_text())
        self.last_price: float | None = None

    def _save(self) -> None:
        if self.path:
            self.path.write_text(json.dumps(self.state, indent=2))

    def get_equity(self) -> float:
        eq = float(self.state["equity"])
        pos = self.get_position("")
        if pos and pos.unrealized_pnl_pct is not None:
            eq += pos.notional * pos.unrealized_pnl_pct / 100
        return eq

    def mark_to_market(
        self, price: float, high: float | None = None, low: float | None = None, open_: float | None = None
    ) -> dict | None:
        """Update mark price; close the paper position if SL/TP was hit inside this bar.

        With `open_` given, a bar that opens beyond the stop fills at the open (gap), otherwise at the
        stop level plus slippage. Stops are checked before targets (conservative).
        """
        self.last_price = price
        p = self.state.get("position")
        if not p:
            return None
        hi, lo = (high if high is not None else price), (low if low is not None else price)
        sl, tp = p.get("stop_loss"), p.get("take_profit")
        long = p["side"] == "long"
        hit = None
        if sl and ((lo <= sl) if long else (hi >= sl)):
            fill = sl
            if open_ is not None and ((open_ < sl) if long else (open_ > sl)):
                fill = open_  # gapped through the stop
            fill = fill * (1 - self.slippage) if long else fill * (1 + self.slippage)
            hit = ("stop_loss", fill)
        elif tp and ((hi >= tp) if long else (lo <= tp)):
            hit = ("take_profit", tp)  # resting limit: filled at the level
        if hit:
            return self._close_at(hit[1], reason=hit[0])
        return None

    def get_position(self, symbol: str) -> Position | None:
        p = self.state.get("position")
        if not p:
            return None
        mark = self.last_price or p["entry_price"]
        sign = 1 if p["side"] == "long" else -1
        pnl_pct = (mark / p["entry_price"] - 1) * 100 * sign
        liq = p["entry_price"] * (1 - sign * 0.95 / p["leverage"])  # rough isolated-margin estimate
        return Position(
            side=p["side"], qty=p["qty"], entry_price=p["entry_price"], leverage=p["leverage"],
            mark_price=mark, unrealized_pnl_pct=pnl_pct, liquidation_price=liq,
            opened_at=p.get("opened_at"), stop_loss=p.get("stop_loss"), take_profit=p.get("take_profit"),
        )

    def open(self, symbol: str, side: str, qty: float, stop_loss: float, take_profit: float,
             leverage: int | None = None) -> dict:
        if self.state.get("position"):
            return {"error": "position already open"}
        price = self.last_price
        if price is None:
            return {"error": "no mark price"}
        price = price * (1 + self.slippage) if side == "long" else price * (1 - self.slippage)
        self.state["position"] = {
            "side": side, "qty": qty, "entry_price": price, "leverage": int(leverage or self.s.leverage),
            "opened_at": time.time(), "stop_loss": stop_loss, "take_profit": take_profit,
        }
        self._save()
        return {"paper": True, "filled_at": price, "qty": qty, "side": side}

    def _close_at(self, price: float, reason: str) -> dict:
        p = self.state["position"]
        sign = 1 if p["side"] == "long" else -1
        pnl = (price - p["entry_price"]) * p["qty"] * sign
        fee = (price + p["entry_price"]) * p["qty"] * self.fee  # taker fee on both legs
        self.state["equity"] = float(self.state["equity"]) + pnl - fee
        trade = {
            "side": p["side"], "qty": p["qty"], "entry": p["entry_price"], "exit": price,
            "pnl": pnl - fee, "reason": reason, "opened_at": p.get("opened_at"), "closed_at": time.time(),
        }
        self.state["trades"].append(trade)
        self.state["position"] = None
        self._save()
        return {"paper": True, "closed": True, **trade}

    def close(self, symbol: str) -> dict:
        p = self.state.get("position")
        if not p:
            return {"closed": False, "reason": "no position"}
        px = self.last_price * (1 - self.slippage) if p["side"] == "long" else self.last_price * (1 + self.slippage)
        return self._close_at(px, reason="signal")


def position_to_dict(p: Position | None) -> dict | None:
    return None if p is None else asdict(p)
