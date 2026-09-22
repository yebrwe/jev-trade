"""Technical indicators computed in code (Jev is not a calculator).

All functions take an OHLCV DataFrame with columns open/high/low/close/volume
and a UTC DatetimeIndex, and return a dict of the latest numeric readings.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(s: pd.Series, n: int) -> pd.Series:
    """EMA with normalized weights (adjust=True): no arbitrary seed, unbiased on a finite window.

    Combined with >= 1000 bars of history this matches a fully warmed-up recursive EMA
    to well under 0.01%; on short series (weekly, ~370 bars) it is the honest estimate.
    """
    return s.ewm(span=n, adjust=True, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0.0)
    dn = -d.clip(upper=0.0)
    au = up.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    ad = dn.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = au / ad.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, sig: int = 9):
    line = ema(close, fast) - ema(close, slow)
    signal = line.ewm(span=sig, adjust=False, min_periods=sig).mean()
    return line, signal, line - signal


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    prev = df["close"].shift(1)
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def adx(df: pd.DataFrame, n: int = 14):
    up = df["high"].diff()
    dn = -df["low"].diff()
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = atr(df, n)  # already smoothed TR
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / tr
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / tr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False, min_periods=n).mean(), plus_di, minus_di


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0):
    mid = close.rolling(n).mean()
    sd = close.rolling(n).std(ddof=0)
    return mid + k * sd, mid, mid - k * sd


def stochastic(df: pd.DataFrame, n: int = 14, d: int = 3):
    lo = df["low"].rolling(n).min()
    hi = df["high"].rolling(n).max()
    k = 100 * (df["close"] - lo) / (hi - lo).replace(0, np.nan)
    return k, k.rolling(d).mean()


def _last(s: pd.Series, i: int = -1) -> float | None:
    if len(s) < abs(i):
        return None
    v = s.iloc[i]
    return None if pd.isna(v) else float(v)


def compute(df: pd.DataFrame) -> dict:
    """Latest readings on CLOSED candles. Returns numbers; semantics.py turns them into words."""
    n = len(df)
    c = df["close"]
    out: dict = {"bars": n, "close": _last(c)}
    if n < 30:
        out["insufficient"] = True
        return out

    e20, e50, e200 = ema(c, 20), ema(c, 50), ema(c, 200)
    out.update(ema20=_last(e20), ema50=_last(e50), ema200=_last(e200))
    out["ema20_slope_pct"] = (
        None if _last(e20, -6) is None else (e20.iloc[-1] / e20.iloc[-6] - 1) * 100
    )

    r = rsi(c)
    out.update(rsi=_last(r), rsi_prev=_last(r, -4))

    line, sig, hist = macd(c)
    out.update(macd=_last(line), macd_signal=_last(sig), macd_hist=_last(hist), macd_hist_prev=_last(hist, -2))
    # candles since last cross
    sign = np.sign((line - sig).dropna().to_numpy())
    cross_ago = None
    for i in range(len(sign) - 1, 0, -1):
        if sign[i] != sign[i - 1]:
            cross_ago = len(sign) - 1 - i
            break
    out["macd_cross_ago"] = cross_ago

    a = atr(df)
    out["atr"] = _last(a)
    atr_pct = (a / c) * 100
    out["atr_pct"] = _last(atr_pct)
    hist_window = atr_pct.dropna().iloc[-100:]
    out["atr_percentile"] = (
        None if len(hist_window) < 20 else float((hist_window < hist_window.iloc[-1]).mean() * 100)
    )

    ax, pdi, mdi = adx(df)
    out.update(adx=_last(ax), plus_di=_last(pdi), minus_di=_last(mdi))

    up, mid, lo = bollinger(c)
    out.update(bb_upper=_last(up), bb_mid=_last(mid), bb_lower=_last(lo))
    bw = ((up - lo) / mid * 100).dropna()
    out["bb_width_pct"] = _last(bw)
    bw_hist = bw.iloc[-100:]
    out["bb_width_percentile"] = (
        None if len(bw_hist) < 20 else float((bw_hist < bw_hist.iloc[-1]).mean() * 100)
    )

    k, d = stochastic(df)
    out.update(stoch_k=_last(k), stoch_d=_last(d), stoch_k_prev=_last(k, -2))

    vol = df["volume"]
    vavg = vol.rolling(20).mean()
    out["volume_ratio"] = None if _last(vavg) in (None, 0) else _last(vol) / _last(vavg)

    # recent price action (last 5 closed candles)
    last5 = df.iloc[-5:]
    out["chg_5_pct"] = (last5["close"].iloc[-1] / last5["open"].iloc[0] - 1) * 100
    out["green_5"] = int((last5["close"] > last5["open"]).sum())
    highs, lows = last5["high"].to_numpy(), last5["low"].to_numpy()
    out["higher_highs"] = bool(np.all(np.diff(highs[-3:]) > 0))
    out["lower_lows"] = bool(np.all(np.diff(lows[-3:]) < 0))

    # last candle anatomy
    lc = df.iloc[-1]
    rng = lc["high"] - lc["low"]
    out["last_range_pct"] = rng / lc["close"] * 100
    out["last_bullish"] = bool(lc["close"] > lc["open"])
    out["last_close_pos"] = None if rng == 0 else float((lc["close"] - lc["low"]) / rng)  # 0=low,1=high
    out["last_body_ratio"] = None if rng == 0 else float(abs(lc["close"] - lc["open"]) / rng)

    # swing levels over last 50 candles (excluding the last candle itself)
    ref = df.iloc[-51:-1]
    if len(ref) >= 10:
        sh, sl = float(ref["high"].max()), float(ref["low"].min())
        # both expressed as a percentage of the current price: "price is X% below the swing high",
        # "price is X% above the swing low" (negative = price already beyond the level)
        out["swing_high_dist_pct"] = (1 - lc["close"] / sh) * 100
        out["swing_low_dist_pct"] = (lc["close"] / sl - 1) * 100
    return out


def yearly_context(daily: pd.DataFrame, weekly: pd.DataFrame | None = None) -> dict:
    """Long-term context (used for the '1y' timeframe).

    `daily` should carry several years (1500 bars); the one-year statistics use the last 365,
    the multi-year statistics (all-time high, yearly returns, 200-week average) use everything.
    """
    d = daily.iloc[-365:]
    c = d["close"]
    last = float(c.iloc[-1])
    hi, lo = float(d["high"].max()), float(d["low"].min())
    out = {
        "bars": len(d),
        "history_days": len(daily),
        "close": last,
        "high_52w": hi,
        "low_52w": lo,
        "pos_in_range": None if hi == lo else (last - lo) / (hi - lo),
        "dist_from_high_pct": (last / hi - 1) * 100,
        "dist_from_low_pct": (last / lo - 1) * 100,
        "ret_1y_pct": (last / float(c.iloc[0]) - 1) * 100,
        "ret_6m_pct": (last / float(c.iloc[-min(182, len(c))]) - 1) * 100,
        "ret_3m_pct": (last / float(c.iloc[-min(91, len(c))]) - 1) * 100,
        "ret_1m_pct": (last / float(c.iloc[-min(30, len(c))]) - 1) * 100,
    }
    e200 = ema(c, 200)
    out["ema200"] = _last(e200)
    out["ema200_slope_pct"] = (
        None if _last(e200, -21) is None else (e200.iloc[-1] / e200.iloc[-21] - 1) * 100
    )
    # max drawdown from running peak over the year
    peak = c.cummax()
    out["max_drawdown_pct"] = float(((c / peak) - 1).min() * 100)
    out["current_drawdown_pct"] = float((last / float(peak.iloc[-1]) - 1) * 100)

    # ---- multi-year context from the full daily history ----
    call = daily["close"]
    ath = float(daily["high"].max())
    ath_date = daily["high"].idxmax()
    out["ath"] = ath
    out["ath_dist_pct"] = (last / ath - 1) * 100
    out["ath_days_ago"] = int((daily.index[-1] - ath_date).days)
    out["ret_2y_pct"] = (last / float(call.iloc[-min(730, len(call))]) - 1) * 100 if len(call) > 400 else None
    out["ret_3y_pct"] = (last / float(call.iloc[-min(1095, len(call))]) - 1) * 100 if len(call) > 800 else None
    # calendar-year returns (last 3 completed + current year to date)
    yearly = call.resample("YS").agg(["first", "last"]).dropna()
    out["calendar_years"] = [
        (int(ts.year), float((row["last"] / row["first"] - 1) * 100)) for ts, row in yearly.tail(4).iterrows()
    ]
    # monthly closes: direction of the last 3 completed months
    monthly = call.resample("MS").last().dropna()
    if len(monthly) >= 4:
        m = monthly.iloc[-4:]  # 3 completed months + current partial
        out["monthly_dirs"] = [bool(m.iloc[i] > m.iloc[i - 1]) for i in range(1, 4)]
    # 200-week average (classic BTC cycle floor) from weekly closes when available
    if weekly is not None and len(weekly) >= 200:
        wc = weekly["close"]
        out["sma200w"] = float(wc.rolling(200).mean().iloc[-1])
        out["sma200w_dist_pct"] = (last / out["sma200w"] - 1) * 100
        e20w, e50w = ema(wc, 20), ema(wc, 50)
        out["weekly_stack_bull"] = bool(last > e20w.iloc[-1] > e50w.iloc[-1])
        out["weekly_stack_bear"] = bool(last < e20w.iloc[-1] < e50w.iloc[-1])
    return out
