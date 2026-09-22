"""Timeframe helpers: Binance-native vs derived (resampled) timeframes."""
from __future__ import annotations

import pandas as pd

# Derived timeframes: target -> (source native timeframe, pandas resample rule, multiplier)
DERIVED: dict[str, tuple[str, str, int]] = {
    "10m": ("5m", "10min", 2),
    "5h": ("1h", "5h", 5),
    "2h": ("1h", "2h", 2),
    "4h": ("1h", "4h", 4),
}

# "1y" is not a candle series: it is a one-year context block derived from daily bars.
CONTEXT_ONLY = {"1y": ("1d", 365)}

_SECONDS = {"m": 60, "h": 3600, "d": 86400, "w": 604800, "M": 2592000, "y": 31536000}


def tf_seconds(tf: str) -> int:
    unit = tf[-1]
    return int(tf[:-1]) * _SECONDS[unit]


def source_for(tf: str) -> tuple[str, int]:
    """Return (native timeframe to fetch, number of native bars needed) for a requested tf."""
    if tf in DERIVED:
        src, _, mult = DERIVED[tf]
        return src, 300 * mult
    if tf in CONTEXT_ONLY:
        src, n = CONTEXT_ONLY[tf]
        return src, n + 40
    if tf == "1d":
        return tf, 400
    return tf, 300


def resample(df: pd.DataFrame, tf: str) -> pd.DataFrame:
    """Resample a native OHLCV frame (UTC DatetimeIndex) into a derived timeframe."""
    _, rule, _ = DERIVED[tf]
    out = df.resample(rule, label="left", closed="left", origin="epoch").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    )
    return out.dropna(subset=["open"])


def next_close_time(tf: str, now_ts: float) -> float:
    """Epoch seconds of the next candle close for tf (UTC-aligned, like Binance)."""
    step = tf_seconds(tf)
    return (int(now_ts) // step + 1) * step
