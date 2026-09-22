"""Testnet smoke test: prove the live order path end to end with a tiny round trip.

    python -m jev_trade testnet

Refuses to run unless BINANCE_TESTNET=true. Steps:
  1. connect with the testnet keys, print balance and position mode
  2. set margin mode / leverage on SYMBOL
  3. open a minimum-notional LONG with a wide stop and target (same code path as the bot)
  4. verify the position and the two protective orders exist
  5. close it (reduce-only market) and verify the account is flat with no open orders
"""
from __future__ import annotations

import json
import time

from .config import Settings
from .exchange import LiveBroker, Market


def run(settings: Settings, side: str = "long", keep_open: bool = False) -> dict:
    if not settings.binance_testnet:
        raise SystemExit("refusing: set BINANCE_TESTNET=true (this test places real orders on whatever endpoint is configured)")
    if not (settings.binance_api_key and settings.binance_api_secret):
        raise SystemExit("BINANCE_API_KEY / BINANCE_API_SECRET are not set (testnet keys from https://testnet.binancefuture.com)")
    sym = settings.symbol
    report: dict = {"symbol": sym, "steps": []}

    def step(name: str, **kw):
        report["steps"].append({"step": name, **kw})
        print(f"[testnet] {name}: {json.dumps(kw, default=str)[:300]}")

    broker = LiveBroker(settings)
    step("connect", equity_usdt=broker.get_equity(), hedge_mode=broker.hedge_mode)
    existing = broker.get_position(sym)
    if existing:
        step("existing position found; closing first", side=existing.side, qty=existing.qty)
        broker.close(sym)
        time.sleep(1.5)

    market = Market(settings, exchange=broker.ex)  # price/precision from the same (testnet) venue we trade on
    px = market.fetch_realtime(sym)["last"]
    min_notional = market.min_notional(sym)
    qty = market.amount_to_precision(sym, (min_notional * 1.2) / px)
    if qty < market.min_amount(sym):
        qty = market.min_amount(sym)
    sl = market.price_to_precision(sym, px * (0.97 if side == "long" else 1.03))
    tp = market.price_to_precision(sym, px * (1.03 if side == "long" else 0.97))
    step("sizing", price=px, qty=qty, notional=round(qty * px, 2), stop_loss=sl, take_profit=tp)

    res = broker.open(sym, side, qty, sl, tp)
    step("open", **res)
    time.sleep(2.0)

    pos = broker.get_position(sym)
    open_orders = broker.fetch_trigger_orders(sym) + broker.ex.fetch_open_orders(sym)
    step("verify", position=None if pos is None else {"side": pos.side, "qty": pos.qty, "entry": pos.entry_price,
                                                       "sl_seen": pos.stop_loss, "tp_seen": pos.take_profit},
         open_orders=[{"side": o.get("side"), "trigger": o.get("stopPrice") or o.get("triggerPrice"), "reduce_only": o.get("reduceOnly")} for o in open_orders])
    ok = pos is not None and pos.side == side and abs(pos.qty - qty) / qty < 0.01
    protective = len(open_orders)
    if keep_open:
        report["result"] = "left open" if ok else "open failed"
        return report

    closed = broker.close(sym)
    step("close", **closed)
    time.sleep(2.0)
    after = broker.get_position(sym)
    remaining = broker.fetch_trigger_orders(sym) + broker.ex.fetch_open_orders(sym)
    step("verify_flat", position=None if after is None else {"side": after.side, "qty": after.qty}, open_orders=len(remaining))
    report["result"] = (
        "PASS" if ok and protective >= 2 and after is None and not remaining
        else f"CHECK: opened={ok} protective_orders={protective} flat_after={after is None} leftover_orders={len(remaining)}"
    )
    return report
