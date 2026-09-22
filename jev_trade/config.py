from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# .env.local overrides .env; both optional. Existing process env wins.
for name in (".env", ".env.local"):
    p = Path(name)
    if p.exists():
        load_dotenv(p, override=False)


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _float(name: str, default: float) -> float:
    v = os.getenv(name)
    return float(v) if v not in (None, "") else default


def _int(name: str, default: int) -> int:
    v = os.getenv(name)
    return int(v) if v not in (None, "") else default


@dataclass
class Settings:
    typesafe_api_key: str = field(default_factory=lambda: os.getenv("TYPESAFE_API_KEY", ""))
    typesafe_model: str = field(default_factory=lambda: os.getenv("TYPESAFE_MODEL", "jev-latest"))

    binance_api_key: str = field(default_factory=lambda: os.getenv("BINANCE_API_KEY", ""))
    binance_api_secret: str = field(default_factory=lambda: os.getenv("BINANCE_API_SECRET", ""))
    binance_testnet: bool = field(default_factory=lambda: _bool("BINANCE_TESTNET", False))
    # market data always comes from mainnet (real prices, deep history) unless explicitly asked otherwise;
    # BINANCE_TESTNET only decides where ORDERS go.
    market_data_testnet: bool = field(default_factory=lambda: _bool("MARKET_DATA_TESTNET", False))
    # Binance Futures Testnet is now "Demo Trading": official REST host demo-fapi.binance.com
    # (testnet.binancefuture.com redirects there). Override only if Binance changes it again.
    binance_testnet_host: str = field(default_factory=lambda: os.getenv("BINANCE_TESTNET_HOST", "demo-fapi.binance.com"))

    symbol: str = field(default_factory=lambda: os.getenv("SYMBOL", "BTC/USDT:USDT"))
    timeframes: list[str] = field(
        default_factory=lambda: [
            t.strip()
            for t in os.getenv("TIMEFRAMES", "1m,5m,10m,15m,30m,1h,5h,1d,1w,1y").split(",")
            if t.strip()
        ]
    )
    decision_timeframe: str = field(default_factory=lambda: os.getenv("DECISION_TIMEFRAME", "5m"))
    loop_interval_sec: int = field(default_factory=lambda: _int("LOOP_INTERVAL_SEC", 0))

    dry_run: bool = field(default_factory=lambda: _bool("DRY_RUN", True))
    leverage: int = field(default_factory=lambda: _int("LEVERAGE", 10))  # fallback when no conviction answer
    # Jev `conviction` (0 weak .. 3 very strong) -> leverage tier and risk-per-trade tier (code-owned mapping)
    leverage_tiers: list[int] = field(
        default_factory=lambda: [int(x) for x in os.getenv("LEVERAGE_TIERS", "10,20,35,50").split(",")]
    )
    risk_tiers: list[float] = field(
        default_factory=lambda: [float(x) for x in os.getenv("RISK_TIERS", "0.5,1.0,1.5,2.0").split(",")]
    )
    conviction_min_confidence: float = field(default_factory=lambda: _float("CONVICTION_MIN_CONFIDENCE", 0.35))
    # safety guards applied after the tier is chosen
    stop_liq_ratio_max: float = field(default_factory=lambda: _float("STOP_LIQ_RATIO_MAX", 0.5))  # stop dist <= 50% of liq dist
    max_cost_pct_of_margin: float = field(default_factory=lambda: _float("MAX_COST_PCT_OF_MARGIN", 10.0))  # fees+funding
    maint_margin_rate_fallback: float = field(default_factory=lambda: _float("MAINT_MARGIN_RATE", 0.004))
    taker_fee_bps: float = field(default_factory=lambda: _float("TAKER_FEE_BPS", 5.0))
    funding_periods_est: int = field(default_factory=lambda: _int("FUNDING_PERIODS_EST", 3))
    margin_mode: str = field(default_factory=lambda: os.getenv("MARGIN_MODE", "isolated"))
    risk_per_trade_pct: float = field(default_factory=lambda: _float("RISK_PER_TRADE_PCT", 1.0))
    max_position_pct: float = field(default_factory=lambda: _float("MAX_POSITION_PCT", 30.0))
    atr_stop_mult: float = field(default_factory=lambda: _float("ATR_STOP_MULT", 2.0))
    atr_tp_mult: float = field(default_factory=lambda: _float("ATR_TP_MULT", 3.0))
    cooldown_candles: int = field(default_factory=lambda: _int("COOLDOWN_CANDLES", 3))
    max_trades_per_day: int = field(default_factory=lambda: _int("MAX_TRADES_PER_DAY", 30))

    # entry gates (intraday, conviction-tiered sizing makes weak entries small rather than forbidden)
    min_entry_confidence: float = field(default_factory=lambda: _float("MIN_ENTRY_CONFIDENCE", 0.05))  # soft floor
    min_entry_prob: float = field(default_factory=lambda: _float("MIN_ENTRY_PROB", 0.40))
    min_direction_edge: float = field(default_factory=lambda: _float("MIN_DIRECTION_EDGE", 0.15))  # P(side)-P(opposite)
    min_setup_score: float = field(default_factory=lambda: _float("MIN_SETUP_SCORE", 1.0))
    choppy_max: float = field(default_factory=lambda: _float("CHOPPY_MAX", 0.60))
    overextended_max: float = field(default_factory=lambda: _float("OVEREXTENDED_MAX", 0.80))
    min_exit_prob: float = field(default_factory=lambda: _float("MIN_EXIT_PROB", 0.55))
    thesis_invalidated_threshold: float = field(
        default_factory=lambda: _float("THESIS_INVALIDATED_THRESHOLD", 0.70)
    )

    # --- perspective (Claude) and news monitor (Jev) ---
    # backend "cli" = Claude Code CLI (`claude -p`, uses your Claude login, no API key);
    # backend "api" = Anthropic SDK with ANTHROPIC_API_KEY
    perspective_backend: str = field(default_factory=lambda: os.getenv("PERSPECTIVE_BACKEND", "cli").strip().lower())
    perspective_cli_model: str = field(default_factory=lambda: os.getenv("PERSPECTIVE_CLI_MODEL", "opus"))
    perspective_cli_budget_usd: float = field(default_factory=lambda: _float("PERSPECTIVE_CLI_BUDGET_USD", 1.5))
    perspective_cli_timeout_sec: int = field(default_factory=lambda: _int("PERSPECTIVE_CLI_TIMEOUT_SEC", 300))
    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    perspective_model: str = field(default_factory=lambda: os.getenv("PERSPECTIVE_MODEL", "claude-opus-5"))
    perspective_ttl_hours: float = field(default_factory=lambda: _float("PERSPECTIVE_TTL_HOURS", 24.0))
    perspective_web_search: bool = field(default_factory=lambda: _bool("PERSPECTIVE_WEB_SEARCH", True))
    anthropic_fallbacks: bool = field(default_factory=lambda: _bool("ANTHROPIC_FALLBACKS", True))
    news_check_minutes: int = field(default_factory=lambda: _int("NEWS_CHECK_MINUTES", 10))
    news_feeds: list[str] | None = field(
        default_factory=lambda: [q.strip() for q in os.getenv("NEWS_FEEDS", "").split(",") if q.strip()] or None
    )
    # event-driven news check: a fast price move triggers an immediate screening
    price_shock_pct: float = field(default_factory=lambda: _float("PRICE_SHOCK_PCT", 1.5))
    price_shock_window_min: int = field(default_factory=lambda: _int("PRICE_SHOCK_WINDOW_MIN", 5))
    # live account safety
    allow_hedge_mode: bool = field(default_factory=lambda: _bool("ALLOW_HEDGE_MODE", False))
    # replay realism
    replay_slippage_bps: float = field(default_factory=lambda: _float("REPLAY_SLIPPAGE_BPS", 3.0))
    replay_fee_bps: float = field(default_factory=lambda: _float("REPLAY_FEE_BPS", 5.0))
    news_material_threshold: float = field(default_factory=lambda: _float("NEWS_MATERIAL_THRESHOLD", 0.60))
    news_material_min_count: int = field(default_factory=lambda: _int("NEWS_MATERIAL_MIN_COUNT", 2))
    news_shock_threshold: float = field(default_factory=lambda: _float("NEWS_SHOCK_THRESHOLD", 0.70))
    shock_block_minutes: int = field(default_factory=lambda: _int("SHOCK_BLOCK_MINUTES", 60))
    block_entry_on_high_event_risk: bool = field(default_factory=lambda: _bool("BLOCK_ENTRY_ON_HIGH_EVENT_RISK", True))
    macro_conflict_max: float = field(default_factory=lambda: _float("MACRO_CONFLICT_MAX", 0.60))
    respect_trading_stance: bool = field(default_factory=lambda: _bool("RESPECT_TRADING_STANCE", True))

    log_dir: Path = field(default_factory=lambda: Path(os.getenv("LOG_DIR", "logs")))

    def validate(self) -> None:
        if not self.typesafe_api_key:
            raise SystemExit("TYPESAFE_API_KEY is not set (put it in .env.local)")
        if not self.dry_run and not (self.binance_api_key and self.binance_api_secret):
            raise SystemExit("DRY_RUN=false requires BINANCE_API_KEY and BINANCE_API_SECRET")
        if self.decision_timeframe not in self.timeframes:
            raise SystemExit(
                f"DECISION_TIMEFRAME {self.decision_timeframe} must be one of TIMEFRAMES {self.timeframes}"
            )
        self.log_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
