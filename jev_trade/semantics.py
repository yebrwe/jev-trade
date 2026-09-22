"""Turn numeric indicator readings into short semantic summaries for Jev.

Jev reads numbers as text and is weak at arithmetic, so every comparison is done
here and the result is expressed as a named condition. Numbers are kept only as
parenthetical detail.
"""
from __future__ import annotations


def _f(v, nd=1):
    return "n/a" if v is None else f"{v:.{nd}f}"


def trend_label(ind: dict) -> str:
    c, e20, e50, e200 = ind.get("close"), ind.get("ema20"), ind.get("ema50"), ind.get("ema200")
    adx = ind.get("adx") or 0.0
    slope = ind.get("ema20_slope_pct")
    if e200 is None:
        # not enough bars for EMA200: use EMA20/50 only
        if e50 is None or e20 is None:
            return "unknown (insufficient history)"
        bull = e20 > e50 and c > e20
        bear = e20 < e50 and c < e20
    else:
        bull = e20 > e50 > e200 and c > e20
        bear = e20 < e50 < e200 and c < e20
    if bull:
        return "strong uptrend" if adx >= 25 else "uptrend"
    if bear:
        return "strong downtrend" if adx >= 25 else "downtrend"
    if slope is not None and adx >= 20:
        if slope > 0 and c > (e50 or c):
            return "uptrend (early, EMAs not fully stacked)"
        if slope < 0 and c < (e50 or c):
            return "downtrend (early, EMAs not fully stacked)"
    return "sideways / range-bound"


def price_vs_emas(ind: dict) -> str:
    c = ind["close"]
    parts = []
    for name in ("ema20", "ema50", "ema200"):
        v = ind.get(name)
        if v is None:
            continue
        rel = "above" if c > v else "below"
        parts.append(f"{rel} {name.upper()} by {abs(c / v - 1) * 100:.2f}%")
    return "; ".join(parts) if parts else "n/a"


def rsi_label(ind: dict) -> str:
    r, p = ind.get("rsi"), ind.get("rsi_prev")
    if r is None:
        return "n/a"
    if r >= 80:
        lab = "extremely overbought"
    elif r >= 70:
        lab = "overbought"
    elif r >= 60:
        lab = "bullish"
    elif r > 40:
        lab = "neutral"
    elif r > 30:
        lab = "bearish"
    elif r > 20:
        lab = "oversold"
    else:
        lab = "extremely oversold"
    direction = ""
    if p is not None:
        if r - p > 5:
            direction = ", rising fast"
        elif r - p > 1.5:
            direction = ", rising"
        elif p - r > 5:
            direction = ", falling fast"
        elif p - r > 1.5:
            direction = ", falling"
        else:
            direction = ", flat"
    return f"{lab} ({r:.0f}){direction}"


def macd_label(ind: dict) -> str:
    line = ind.get("macd")
    sig = ind.get("macd_signal")
    h = ind.get("macd_hist")
    hp = ind.get("macd_hist_prev")
    ago = ind.get("macd_cross_ago")
    if line is None or sig is None or h is None:
        return "n/a"
    side = "bullish (MACD above signal)" if h > 0 else "bearish (MACD below signal)"
    mom = ""
    if hp is not None:
        if abs(h) > abs(hp):
            mom = "; momentum strengthening"
        elif abs(h) < abs(hp):
            mom = "; momentum weakening"
    zero = "; MACD line above zero" if line > 0 else "; MACD line below zero"
    cross = ""
    if ago is not None:
        if ago == 0:
            cross = "; crossed on the last candle"
        elif ago <= 3:
            cross = f"; crossed {ago} candles ago (fresh)"
        else:
            cross = f"; last cross {ago} candles ago"
    return side + mom + zero + cross


def bollinger_label(ind: dict) -> str:
    c, up, mid, lo = ind["close"], ind.get("bb_upper"), ind.get("bb_mid"), ind.get("bb_lower")
    if up is None:
        return "n/a"
    if c > up:
        pos = "price above the upper band (stretched)"
    elif c > mid:
        pos = "price in the upper half of the bands"
    elif c > lo:
        pos = "price in the lower half of the bands"
    else:
        pos = "price below the lower band (stretched)"
    pct = ind.get("bb_width_percentile")
    if pct is None:
        w = ""
    elif pct <= 20:
        w = "; bands squeezed (very narrow, breakout may follow)"
    elif pct >= 80:
        w = "; bands expanded (wide, move already extended)"
    else:
        w = "; band width normal"
    return pos + w


def volatility_label(ind: dict) -> str:
    ap, pct = ind.get("atr_pct"), ind.get("atr_percentile")
    if ap is None:
        return "n/a"
    if pct is None:
        lab = "unknown regime"
    elif pct >= 80:
        lab = "high"
    elif pct <= 20:
        lab = "low"
    else:
        lab = "normal"
    return f"{lab} (ATR {ap:.2f}% of price, {_f(pct, 0)}th percentile of last 100 candles)"


def adx_label(ind: dict) -> str:
    a, p, m = ind.get("adx"), ind.get("plus_di"), ind.get("minus_di")
    if a is None:
        return "n/a"
    if a >= 40:
        s = "very strong trend"
    elif a >= 25:
        s = "trending"
    elif a >= 20:
        s = "weak trend"
    else:
        s = "no trend / ranging"
    dom = ""
    if p is not None and m is not None:
        dom = "; buyers dominate (+DI > -DI)" if p > m else "; sellers dominate (-DI > +DI)"
    return f"{s} (ADX {a:.0f}){dom}"


def stoch_label(ind: dict) -> str:
    k, d, kp = ind.get("stoch_k"), ind.get("stoch_d"), ind.get("stoch_k_prev")
    if k is None:
        return "n/a"
    if k >= 80:
        lab = "overbought"
    elif k <= 20:
        lab = "oversold"
    else:
        lab = "mid-range"
    turn = ""
    if kp is not None and d is not None:
        if k > d and kp <= d:
            turn = ", just crossed up"
        elif k < d and kp >= d:
            turn = ", just crossed down"
        elif k > kp:
            turn = ", rising"
        elif k < kp:
            turn = ", falling"
    return f"{lab} ({k:.0f}){turn}"


def volume_label(ind: dict) -> str:
    r = ind.get("volume_ratio")
    if r is None:
        return "n/a"
    if r >= 2.0:
        lab = "surging"
    elif r >= 1.3:
        lab = "above average"
    elif r >= 0.7:
        lab = "average"
    else:
        lab = "thin"
    return f"{lab} ({r:.1f}x the 20-candle average)"


def price_action_label(ind: dict) -> str:
    chg, g = ind.get("chg_5_pct"), ind.get("green_5")
    if chg is None:
        return "n/a"
    struct = ""
    if ind.get("higher_highs"):
        struct = ", making higher highs"
    elif ind.get("lower_lows"):
        struct = ", making lower lows"
    return f"last 5 closed candles net {chg:+.2f}%, {g} of 5 bullish{struct}"


def last_candle_label(ind: dict) -> str:
    if ind.get("last_range_pct") is None:
        return "n/a"
    side = "bullish" if ind["last_bullish"] else "bearish"
    pos = ind.get("last_close_pos")
    body = ind.get("last_body_ratio")
    where = ""
    if pos is not None:
        if pos >= 0.75:
            where = ", closed near its high"
        elif pos <= 0.25:
            where = ", closed near its low"
        else:
            where = ", closed mid-range"
    shape = ""
    if body is not None:
        if body >= 0.6:
            shape = " with a strong body"
        elif body <= 0.25:
            shape = " with a small body (indecision)"
    return f"{side}{shape}{where}, range {ind['last_range_pct']:.2f}%"


def swing_label(ind: dict) -> str:
    h, l = ind.get("swing_high_dist_pct"), ind.get("swing_low_dist_pct")
    if h is None or l is None:
        return "n/a"
    if h < 0:
        hs = f"price broke above the 50-candle swing high by {-h:.2f}%"
    elif h <= 0.5:
        hs = f"price is right at the 50-candle swing high ({h:.2f}% below)"
    else:
        hs = f"price is {h:.2f}% below the 50-candle swing high"
    if l < 0:
        ls = f"price broke below the 50-candle swing low by {-l:.2f}%"
    elif l <= 0.5:
        ls = f"price is right at the 50-candle swing low ({l:.2f}% above)"
    else:
        ls = f"price is {l:.2f}% above the 50-candle swing low"
    return hs + "; " + ls


def summarize_timeframe(ind: dict) -> dict:
    if ind.get("insufficient"):
        return {"note": f"insufficient history ({ind.get('bars')} candles)"}
    return {
        "trend": trend_label(ind),
        "price_vs_emas": price_vs_emas(ind),
        "adx": adx_label(ind),
        "rsi": rsi_label(ind),
        "macd": macd_label(ind),
        "stochastic": stoch_label(ind),
        "bollinger": bollinger_label(ind),
        "volatility": volatility_label(ind),
        "volume": volume_label(ind),
        "recent_price_action": price_action_label(ind),
        "last_candle": last_candle_label(ind),
        "swing_levels": swing_label(ind),
    }


def summarize_yearly(y: dict) -> dict:
    pos = y.get("pos_in_range")
    if pos is None:
        where = "n/a"
    elif pos >= 0.9:
        where = "at / near the 52-week high"
    elif pos >= 0.7:
        where = "upper part of the 52-week range"
    elif pos >= 0.3:
        where = "middle of the 52-week range"
    elif pos >= 0.1:
        where = "lower part of the 52-week range"
    else:
        where = "at / near the 52-week low"
    r1y = y["ret_1y_pct"]
    if r1y >= 50:
        regime = "strong bull year"
    elif r1y >= 15:
        regime = "bull year"
    elif r1y > -15:
        regime = "flat year"
    elif r1y > -40:
        regime = "bear year"
    else:
        regime = "deep bear year"
    slope = y.get("ema200_slope_pct")
    lt = "n/a"
    if y.get("ema200") is not None and slope is not None:
        above = y["close"] > y["ema200"]
        if slope > 0.5:
            s = "rising"
        elif slope < -0.5:
            s = "falling"
        else:
            s = "flat"
        lt = ("above" if above else "below") + " the 200-day EMA, which is " + s
    # ---- multi-year ----
    ath_d = y.get("ath_dist_pct")
    if ath_d is None:
        ath_txt = "n/a"
    elif ath_d >= -3:
        ath_txt = f"at the all-time high ({ath_d:+.1f}%)"
    elif ath_d >= -20:
        ath_txt = f"near the all-time high ({ath_d:+.1f}%, set {y.get('ath_days_ago')} days ago)"
    elif ath_d >= -50:
        ath_txt = f"well below the all-time high ({ath_d:+.1f}%, set {y.get('ath_days_ago')} days ago)"
    else:
        ath_txt = f"deep below the all-time high ({ath_d:+.1f}%, set {y.get('ath_days_ago')} days ago)"
    cyc = "n/a"
    if y.get("sma200w_dist_pct") is not None:
        dist = y["sma200w_dist_pct"]
        if dist < 0:
            cyc = f"below the 200-week average by {-dist:.0f}% (historically a cycle-bottom zone)"
        elif dist < 30:
            cyc = f"just above the 200-week average (+{dist:.0f}%)"
        elif dist < 100:
            cyc = f"comfortably above the 200-week average (+{dist:.0f}%)"
        else:
            cyc = f"far above the 200-week average (+{dist:.0f}%, historically late-cycle territory)"
        if y.get("weekly_stack_bull"):
            cyc += "; weekly EMAs stacked bullish (price > EMA20w > EMA50w)"
        elif y.get("weekly_stack_bear"):
            cyc += "; weekly EMAs stacked bearish (price < EMA20w < EMA50w)"
        else:
            cyc += "; weekly EMAs not stacked (transition)"
    years = y.get("calendar_years") or []
    years_txt = ", ".join(f"{yr}: {r:+.0f}%" for yr, r in years) or "n/a"
    if years:
        years_txt += " (last entry is year-to-date)"
    md = y.get("monthly_dirs")
    if md:
        ups = sum(md)
        month_txt = ("three up months in a row" if ups == 3 else "three down months in a row" if ups == 0
                     else f"{ups} of the last 3 months up")
    else:
        month_txt = "n/a"
    multi = []
    if y.get("ret_2y_pct") is not None:
        multi.append(f"2y {y['ret_2y_pct']:+.0f}%")
    if y.get("ret_3y_pct") is not None:
        multi.append(f"3y {y['ret_3y_pct']:+.0f}%")
    return {
        "note": (
            f"long-term context from {y.get('history_days', 365)} daily candles and weekly candles "
            "(Binance has no native 1y candles); the 1-year figures use the last 365 days"
        ),
        "all_time_high": ath_txt,
        "cycle_position": cyc,
        "calendar_year_returns": years_txt,
        "multi_year_returns": ", ".join(multi) or "n/a",
        "monthly_trend": month_txt,
        "yearly_regime": f"{regime} ({r1y:+.0f}% over 1 year)",
        "position_in_52w_range": (
            f"{where} ({y['dist_from_high_pct']:+.1f}% from high, {y['dist_from_low_pct']:+.1f}% from low)"
        ),
        "returns": f"1m {y['ret_1m_pct']:+.1f}%, 3m {y['ret_3m_pct']:+.1f}%, 6m {y['ret_6m_pct']:+.1f}%",
        "long_term_trend": lt,
        "drawdown": (
            f"currently {y['current_drawdown_pct']:.1f}% from the yearly peak "
            f"(worst this year {y['max_drawdown_pct']:.1f}%)"
        ),
    }


def change_label(pct: float | None) -> str:
    if pct is None:
        return "n/a"
    mag = abs(pct)
    if mag < 0.3:
        return f"flat ({pct:+.2f}%)"
    if mag < 1.5:
        size = "slightly"
    elif mag < 4:
        size = "moderately"
    else:
        size = "sharply"
    return f"{size} {'up' if pct > 0 else 'down'} ({pct:+.2f}%)"


def funding_label(rate: float | None) -> str:
    if rate is None:
        return "n/a"
    pct = rate * 100
    if pct > 0.05:
        return f"high positive ({pct:.3f}%): longs crowded, paying shorts"
    if pct > 0.01:
        return f"positive ({pct:.3f}%): longs pay shorts"
    if pct < -0.05:
        return f"high negative ({pct:.3f}%): shorts crowded, paying longs"
    if pct < -0.01:
        return f"negative ({pct:.3f}%): shorts pay longs"
    return f"neutral ({pct:.3f}%)"


def book_label(bid_vol: float, ask_vol: float) -> str:
    if ask_vol <= 0 or bid_vol <= 0:
        return "n/a"
    ratio = bid_vol / ask_vol
    if ratio >= 1.5:
        return f"bid-heavy (bids {ratio:.1f}x asks in top 20 levels)"
    if ratio <= 0.67:
        return f"ask-heavy (asks {1 / ratio:.1f}x bids in top 20 levels)"
    return f"balanced (bid/ask {ratio:.2f})"


def pnl_label(pnl_pct_on_notional: float | None, leverage: float) -> str:
    if pnl_pct_on_notional is None:
        return "n/a"
    p = pnl_pct_on_notional
    roe = p * leverage
    if p > 0.05:
        if p < 1:
            s = "small profit"
        elif p < 3:
            s = "solid profit"
        else:
            s = "large profit"
    elif p < -0.05:
        if p > -1:
            s = "small loss"
        elif p > -3:
            s = "meaningful loss"
        else:
            s = "large loss"
    else:
        s = "breakeven"
    return f"{s} ({p:+.2f}% of notional, {roe:+.1f}% on margin)"


def liq_label(dist_pct: float | None) -> str:
    if dist_pct is None:
        return "n/a"
    if dist_pct < 3:
        return f"DANGER: liquidation within {dist_pct:.1f}%"
    if dist_pct < 8:
        return f"close ({dist_pct:.1f}% away)"
    return f"far ({dist_pct:.1f}% away)"
