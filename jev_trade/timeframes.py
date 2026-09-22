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

# "1y" is not a candle series: it is a long-term context block derived from daily (and weekly) bars.
CONTEXT_ONLY = {"1y": ("1d", 1500)}

# History depth. EMA200 needs several times its span to forget its starting value:
# with 1000 bars the residual weight of the first bar is (1-2/201)^1000 ~ 0.005%.
BARS_DEFAULT = 1000
BARS = {"1d": 1500, "1w": 1000}  # 1w returns everything Binance has (~370 weeks for BTC USDT-M)

_SECONDS = {"m": 60, "h": 3600, "d": 86400, "w": 604800, "M": 2592000, "y": 31536000}


def tf_seconds(tf: str) -> int:
    unit = tf[-1]
    return int(tf[:-1]) * _SECONDS[unit]


def source_for(tf: str) -> tuple[str, int]:
    """Return (native timeframe to fetch, number of native bars needed) for a requested tf."""
    if tf in DERIVED:
        src, _, mult = DERIVED[tf]
        return src, BARS_DEFAULT * mult
    if tf in CONTEXT_ONLY:
        return CONTEXT_ONLY[tf]
    return tf, BARS.get(tf, BARS_DEFAULT)


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
