"""CLI: python -m jev_trade <command>

  state    Build and print the semantic state Jev would see (no Jev call, no orders)
  once     One full decision cycle (Jev call; executes only if --execute)
  run      Continuous loop (paper unless DRY_RUN=false)
  replay   Walk-forward replay of the last N decision candles with the paper broker
"""
from __future__ import annotations

import argparse
import json
import sys

from .config import settings

# Windows consoles default to a legacy code page; headlines contain unicode punctuation.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def main() -> None:
    ap = argparse.ArgumentParser(prog="jev_trade")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("state")
    p_once = sub.add_parser("once")
    p_once.add_argument("--execute", action="store_true", help="place the order (paper or live per DRY_RUN)")
    sub.add_parser("run")
    p_rep = sub.add_parser("replay")
    p_rep.add_argument("--steps", type=int, default=50)
    p_rep.add_argument("--equity", type=float, default=10_000.0)
    p_rep.add_argument("--quiet", action="store_true")
    p_rep.add_argument("--with-perspective", action="store_true",
                       help="also generate a per-day historical perspective with Claude web search (needs ANTHROPIC_API_KEY)")
    p_eval = sub.add_parser("evaluate", help="re-apply the policy to recorded Jev answers with different thresholds (no Jev calls)")
    p_eval.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="override a setting, e.g. MIN_ENTRY_PROB=0.55")
    p_eval.add_argument("--file", default=None, help="decisions.jsonl path (default: LOG_DIR/decisions.jsonl)")
    p_eval.add_argument("--equity", type=float, default=10_000.0)
    p_eval.add_argument("--verbose", action="store_true")
    p_tn = sub.add_parser("testnet", help="smoke-test the live order path on Binance futures testnet with a tiny round trip")
    p_tn.add_argument("--side", choices=["long", "short"], default="long")
    p_tn.add_argument("--keep-open", action="store_true", help="leave the test position open (to watch the bot manage it)")
    p_per = sub.add_parser("perspective", help="build / show the Claude-written macro view")
    p_per.add_argument("action", choices=["build", "show", "jev"], help="build now, show stored JSON, or print the block Jev sees")
    p_news = sub.add_parser("news", help="news monitor")
    p_news.add_argument("action", choices=["fetch", "check"], help="fetch = list headlines; check = run one Jev screening now")
    args = ap.parse_args()

    if args.cmd == "testnet":
        from .testnet import run

        settings.validate()
        report = run(settings, side=args.side, keep_open=args.keep_open)
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
        print(f"\nRESULT: {report['result']}")
        return
    if args.cmd == "perspective":
        from .perspective import PerspectiveManager

        settings.validate()
        pm = PerspectiveManager(settings)
        if args.action == "build":
            if not pm.enabled:
                raise SystemExit("ANTHROPIC_API_KEY is not set")
            sp = pm.build(trigger="manual")
            print(sp.model_dump_json(indent=2))
        elif args.action == "show":
            print(pm.current.model_dump_json(indent=2) if pm.current else "no perspective stored yet")
        else:
            print(json.dumps(pm.for_jev(), indent=2, ensure_ascii=False))
        return
    if args.cmd == "news":
        from .news import fetch_headlines

        settings.validate()
        if args.action == "fetch":
            for h in fetch_headlines(settings.news_feeds)[:40]:
                print(f"{h['published_utc']}  {h['source'][:22]:<22}  {h['title']}")
        else:
            from .monitor import NewsMonitor
            from .perspective import PerspectiveManager

            pm = PerspectiveManager(settings)
            r = NewsMonitor(settings, pm).check()
            print(json.dumps(r.to_dict(), indent=2, ensure_ascii=False))
        return

    if args.cmd == "state":
        from .bot import Bot

        bot = Bot(settings)
        state, meta, position = bot.build()
        print(json.dumps(state, indent=2, ensure_ascii=False))
        print(f"\n[meta] price={meta.get('price')} atr({settings.decision_timeframe})={meta.get('atr')} "
              f"position={position}")
    elif args.cmd == "once":
        from .bot import Bot

        Bot(settings).run_once(execute=args.execute)
    elif args.cmd == "run":
        from .bot import Bot

        Bot(settings).run()
    elif args.cmd == "replay":
        from .replay import replay

        summary = replay(settings, steps=args.steps, equity=args.equity, verbose=not args.quiet,
                         with_perspective=args.with_perspective)
        print(json.dumps(summary, indent=2, default=str))
    elif args.cmd == "evaluate":
        from pathlib import Path

        from .evaluate import evaluate

        overrides = dict(kv.split("=", 1) for kv in args.set)
        summary = evaluate(settings, path=Path(args.file) if args.file else None, overrides=overrides,
                           equity=args.equity, verbose=args.verbose)
        print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
