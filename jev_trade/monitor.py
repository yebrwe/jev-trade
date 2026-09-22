"""Periodic news check: Jev decides whether new headlines warrant a perspective update.

Every NEWS_CHECK_MINUTES (or immediately after a fast price move, see bot.py)
the monitor pulls fresh headlines from publisher RSS feeds, keeps only ones it
has not evaluated before, and asks Jev two literal questions per headline in a
single request:

  material_<i>  Is this a new development, not already in `perspective`, that
                could materially change crypto/market direction or risk today?
  shock_<i>     Is this a sudden major market shock rather than routine news?

Code then decides: enough material headlines -> ask Claude to update the
perspective; any shock -> flag `shock_active` so policy blocks new entries
until the perspective has been refreshed.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from typesafe_sdk import Noul, TypeSafeClient

from .config import Settings
from .news import NewsStore, fetch_headlines, utc_now_str
from .perspective import PerspectiveManager

MAX_HEADLINES_PER_CHECK = 40


@dataclass
class CheckResult:
    checked_at: float
    new_headlines: int
    material: list[dict] = field(default_factory=list)
    shocks: list[dict] = field(default_factory=list)
    updated: bool = False
    update_error: str | None = None
    skipped: str | None = None

    def to_dict(self) -> dict:
        return {
            "checked_at_utc": time.strftime("%Y-%m-%d %H:%M", time.gmtime(self.checked_at)),
            "new_headlines": self.new_headlines,
            "material": [{"title": m["title"], "p_material": m["p_material"], "p_shock": m["p_shock"]} for m in self.material],
            "shocks": [{"title": m["title"], "p_shock": m["p_shock"]} for m in self.shocks],
            "updated": self.updated,
            "update_error": self.update_error,
            "skipped": self.skipped,
        }


def build_questions(n: int) -> dict:
    q: dict = {}
    for i in range(n):
        q[f"material_{i}"] = Noul(
            instructions=(
                f"Does `headlines[{i}].title` report a new development that is NOT already reflected in "
                "`perspective` and that could materially change the direction or risk of the crypto market "
                "or US stock market today?"
            ),
            criteria={
                "true": "a new, concrete development (policy decision, major announcement, large price shock, "
                        "enforcement action, hack, outage, geopolitical escalation) that `perspective` does not mention",
                "false": "routine commentary, price recap, opinion, minor news, or something `perspective` already covers",
            },
        )
        q[f"shock_{i}"] = Noul(
            instructions=(
                f"Does `headlines[{i}].title` report a sudden, major market shock rather than routine news?"
            ),
            criteria={
                "true": "exchange hack or insolvency, stablecoin depeg, emergency central bank action, surprise "
                        "regulatory ban or enforcement, flash crash or liquidation cascade, war or attack, "
                        "major government shutdown or default event",
                "false": "scheduled data releases, ordinary price moves, analysis, forecasts, product news",
            },
        )
    return q


class NewsMonitor:
    def __init__(self, settings: Settings, perspective: PerspectiveManager, client: TypeSafeClient | None = None):
        self.s = settings
        self.pm = perspective
        self.client = client or TypeSafeClient(api_key=settings.typesafe_api_key, model=settings.typesafe_model)
        self.store = NewsStore(settings.log_dir / "news_seen.json")
        self.log_path = settings.log_dir / "news_checks.jsonl"
        self.state_path = settings.log_dir / "monitor_state.json"
        self.last_check: float = 0.0
        self.shock_active_until: float = 0.0
        self.last_result: CheckResult | None = None
        self._load_state()

    def _load_state(self) -> None:
        if self.state_path.exists():
            try:
                d = json.loads(self.state_path.read_text())
                self.last_check = float(d.get("last_check", 0.0))
                self.shock_active_until = float(d.get("shock_active_until", 0.0))
            except Exception:
                pass

    def _save_state(self) -> None:
        self.state_path.write_text(json.dumps({"last_check": self.last_check, "shock_active_until": self.shock_active_until}))

    # ---------- scheduling ----------
    def due(self, now: float | None = None) -> bool:
        now = now or time.time()
        return (now - self.last_check) >= self.s.news_check_minutes * 60

    def maybe_check(self, force: str | None = None) -> CheckResult | None:
        """Run a check when due, or immediately when `force` names an event (e.g. a price shock)."""
        if not force and not self.due():
            return None
        if force:
            print(f"[monitor] immediate check triggered by {force}")
        try:
            return self.check()
        except Exception as e:
            print(f"[monitor] check failed: {type(e).__name__}: {e}")
            self.last_check = time.time()
            self._save_state()
            return None

    @property
    def shock_active(self) -> bool:
        return time.time() < self.shock_active_until

    # ---------- one check ----------
    def evaluate(self, headlines: list[dict]) -> list[dict]:
        """Ask Jev about each headline; returns headlines annotated with p_material / p_shock."""
        if not headlines:
            return []
        perspective_block = self.pm.for_jev() or {"note": "no perspective has been written yet"}
        state = {
            "time_utc": utc_now_str(),
            "perspective": perspective_block,
            "headlines": [{"title": h["title"], "source": h["source"], "published_utc": h["published_utc"]} for h in headlines],
        }
        resp = self.client.system_one(state, build_questions(len(headlines)))
        out = []
        for i, h in enumerate(headlines):
            out.append({**h, "p_material": resp.nouls[f"material_{i}"].noul, "p_shock": resp.nouls[f"shock_{i}"].noul})
        return out

    def check(self, force_headlines: list[dict] | None = None) -> CheckResult:
        now = time.time()
        self.last_check = now
        items = force_headlines if force_headlines is not None else fetch_headlines(self.s.news_feeds)
        new = self.store.new_only(items)[:MAX_HEADLINES_PER_CHECK]
        result = CheckResult(checked_at=now, new_headlines=len(new))
        if not new:
            result.skipped = "no new headlines"
            self._log(result)
            self._save_state()
            return result

        annotated = self.evaluate(new)
        self.store.mark(new)
        result.material = [h for h in annotated if h["p_material"] >= self.s.news_material_threshold]
        result.shocks = [h for h in annotated if h["p_shock"] >= self.s.news_shock_threshold]

        if result.shocks:
            self.shock_active_until = now + self.s.shock_block_minutes * 60
            print(f"[monitor] SHOCK headline(s): " + " | ".join(h["title"][:80] for h in result.shocks[:3]))

        trigger = None
        if result.shocks:
            trigger = "shock: " + result.shocks[0]["title"][:120]
        elif len(result.material) >= self.s.news_material_min_count:
            trigger = f"{len(result.material)} material headlines"

        if trigger:
            if not self.pm.enabled:
                result.update_error = "perspective updates disabled (no ANTHROPIC_API_KEY)"
            else:
                try:
                    to_send = sorted(result.material + [s for s in result.shocks if s not in result.material],
                                     key=lambda h: h["p_material"], reverse=True)
                    self.pm.update(to_send, reason=trigger)
                    result.updated = True
                    self.shock_active_until = 0.0  # refreshed view lifts the block
                except Exception as e:
                    result.update_error = f"{type(e).__name__}: {e}"
                    print(f"[monitor] perspective update failed: {result.update_error}")
        self._log(result)
        self._save_state()
        self.last_result = result
        print(f"[monitor] checked {len(new)} new headlines: material={len(result.material)} shocks={len(result.shocks)} updated={result.updated}")
        return result

    def _log(self, r: CheckResult) -> None:
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(r.to_dict(), ensure_ascii=False) + "\n")
