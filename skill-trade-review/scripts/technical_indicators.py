"""技术指标计算 — 纯 numpy/pandas 实现，不依赖 talib。

提供:
  - moving_averages(close, periods)     -> dict[str, float]   各 MA 末值
  - macd(close)                          -> dict               diff/dea/hist 末值 + 状态
  - rsi(close, period=14)                -> float
  - bollinger(close, period=20, k=2)     -> dict               upper/mid/lower/位置
  - atr(high, low, close, period=14)     -> float
  - swing_points(high, low, close, lookback=20) -> dict        近期高低点 + 距离
  - volume_price(close, volume)          -> dict               量价配合
  - trend_pattern(close, mas)            -> str                 多头排列/空头排列/纠缠
  - all_indicators(df)                   -> dict                一站式输出
"""
from __future__ import annotations

import math
from typing import Iterable

import pandas as pd


def _last_or_none(series: pd.Series):
    if series is None or len(series) == 0:
        return None
    val = series.iloc[-1]
    if pd.isna(val):
        return None
    return float(val)


def moving_averages(close: pd.Series, periods: Iterable[int] = (5, 10, 20, 60)) -> dict:
    out: dict[str, float | None] = {}
    for p in periods:
        if len(close) >= p:
            out[f"ma{p}"] = round(float(close.tail(p).mean()), 4)
        else:
            out[f"ma{p}"] = None
    return out


def trend_pattern_from_mas(close_last: float, mas: dict) -> str:
    """根据末值价格与 MA 排列给出形态标签。"""
    ma5, ma10, ma20, ma60 = (mas.get(k) for k in ("ma5", "ma10", "ma20", "ma60"))
    if None in (close_last, ma5, ma10, ma20):
        return "unknown"
    bull_align = ma5 > ma10 > ma20 and (ma60 is None or ma20 >= ma60 * 0.99)
    bear_align = ma5 < ma10 < ma20 and (ma60 is None or ma20 <= ma60 * 1.01)
    if bull_align and close_last > ma5:
        return "bull_arrangement"        # 多头排列 + 价格在均线上方
    if bear_align and close_last < ma5:
        return "bear_arrangement"        # 空头排列 + 价格在均线下方
    if bull_align:
        return "bull_pullback"           # 多头排列但价格回踩
    if bear_align:
        return "bear_rebound"            # 空头排列但价格反弹
    # 均线交织
    spread = max(ma5, ma10, ma20) - min(ma5, ma10, ma20)
    if ma20 and spread / ma20 < 0.005:
        return "ma_tangled"              # 均线纠缠 0.5% 以内
    return "ma_mixed"


def macd(close: pd.Series, short: int = 12, long: int = 26, signal: int = 9) -> dict:
    if len(close) < long + signal:
        return {"diff": None, "dea": None, "hist": None, "status": "insufficient_data"}
    ema_short = close.ewm(span=short, adjust=False).mean()
    ema_long = close.ewm(span=long, adjust=False).mean()
    diff = ema_short - ema_long
    dea = diff.ewm(span=signal, adjust=False).mean()
    hist = (diff - dea) * 2
    diff_last = float(diff.iloc[-1])
    dea_last = float(dea.iloc[-1])
    hist_last = float(hist.iloc[-1])
    hist_prev = float(hist.iloc[-2]) if len(hist) >= 2 else hist_last

    # 金叉/死叉判断（看最近 2 根）
    if len(diff) >= 2:
        diff_prev, dea_prev = float(diff.iloc[-2]), float(dea.iloc[-2])
        if diff_prev <= dea_prev and diff_last > dea_last:
            cross = "golden_cross"
        elif diff_prev >= dea_prev and diff_last < dea_last:
            cross = "death_cross"
        else:
            cross = "none"
    else:
        cross = "none"

    if hist_last > 0 and hist_last >= hist_prev:
        status = "bull_strengthening"
    elif hist_last > 0 and hist_last < hist_prev:
        status = "bull_weakening"
    elif hist_last < 0 and hist_last <= hist_prev:
        status = "bear_strengthening"
    else:
        status = "bear_weakening"

    return {
        "diff": round(diff_last, 4),
        "dea": round(dea_last, 4),
        "hist": round(hist_last, 4),
        "cross": cross,
        "status": status,
    }


def rsi(close: pd.Series, period: int = 14) -> dict:
    if len(close) < period + 1:
        return {"value": None, "status": "insufficient_data"}
    delta = close.diff()
    up = delta.clip(lower=0).rolling(period).mean()
    down = (-delta.clip(upper=0)).rolling(period).mean()
    rs = up / down.replace(0, 1e-9)
    rsi_series = 100 - 100 / (1 + rs)
    val = _last_or_none(rsi_series)
    if val is None:
        return {"value": None, "status": "insufficient_data"}
    if val >= 70:
        status = "overbought"
    elif val <= 30:
        status = "oversold"
    elif val >= 55:
        status = "bull_dominant"
    elif val <= 45:
        status = "bear_dominant"
    else:
        status = "neutral"
    return {"value": round(val, 2), "status": status}


def bollinger(close: pd.Series, period: int = 20, k: float = 2.0) -> dict:
    if len(close) < period:
        return {"upper": None, "mid": None, "lower": None, "position": "insufficient_data"}
    mid = close.rolling(period).mean()
    std = close.rolling(period).std()
    upper = mid + k * std
    lower = mid - k * std
    last_close = float(close.iloc[-1])
    upper_v, mid_v, lower_v = (_last_or_none(upper), _last_or_none(mid), _last_or_none(lower))
    if None in (upper_v, mid_v, lower_v):
        return {"upper": None, "mid": None, "lower": None, "position": "insufficient_data"}
    if last_close >= upper_v:
        position = "above_upper"
    elif last_close <= lower_v:
        position = "below_lower"
    elif last_close >= mid_v:
        position = "upper_half"
    else:
        position = "lower_half"
    width = (upper_v - lower_v) / mid_v if mid_v else None
    return {
        "upper": round(upper_v, 4),
        "mid": round(mid_v, 4),
        "lower": round(lower_v, 4),
        "width_pct": round(width * 100, 3) if width is not None else None,
        "position": position,
    }


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> float | None:
    if len(close) < period + 1:
        return None
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    val = tr.rolling(period).mean()
    return _last_or_none(val)


def swing_points(high: pd.Series, low: pd.Series, close: pd.Series, lookback: int = 20) -> dict:
    """近期 lookback 根高低点 + 当前价相对位置。"""
    if len(close) < lookback:
        lookback = len(close)
    if lookback == 0:
        return {}
    hh = float(high.tail(lookback).max())
    ll = float(low.tail(lookback).min())
    last = float(close.iloc[-1])
    if hh == ll:
        position = 0.5
    else:
        position = (last - ll) / (hh - ll)
    return {
        "lookback": lookback,
        "recent_high": round(hh, 4),
        "recent_low": round(ll, 4),
        "current": round(last, 4),
        "position_in_range": round(position, 3),
        "dist_to_high_pct": round((hh - last) / last * 100, 3) if last else None,
        "dist_to_low_pct": round((last - ll) / last * 100, 3) if last else None,
    }


def volume_price(close: pd.Series, volume: pd.Series | None) -> dict:
    """量价配合: 看最近 1 根与 5 日均量的对比 + 价格方向。"""
    if volume is None or len(volume) < 6:
        return {"status": "no_volume_data"}
    last_vol = float(volume.iloc[-1])
    avg5 = float(volume.tail(6).iloc[:-1].mean())  # 不含当日
    if avg5 <= 0:
        return {"status": "no_volume_data"}
    vol_ratio = last_vol / avg5
    if len(close) < 2:
        price_dir = "flat"
    else:
        chg = float(close.iloc[-1]) - float(close.iloc[-2])
        price_dir = "up" if chg > 0 else "down" if chg < 0 else "flat"

    if vol_ratio >= 1.5 and price_dir == "up":
        status = "volume_up_price_up"           # 量增价升，趋势确认
    elif vol_ratio >= 1.5 and price_dir == "down":
        status = "volume_up_price_down"         # 放量下跌，恐慌
    elif vol_ratio <= 0.7 and price_dir == "up":
        status = "volume_down_price_up"         # 缩量上涨，乏力
    elif vol_ratio <= 0.7 and price_dir == "down":
        status = "volume_down_price_down"       # 缩量下跌，止跌
    else:
        status = "neutral"

    return {
        "vol_ratio_to_5d_avg": round(vol_ratio, 2),
        "price_direction": price_dir,
        "status": status,
    }


def all_indicators(df: pd.DataFrame, lookback_swing: int = 20) -> dict:
    """一站式：传入含 close/high/low/volume 的 DataFrame，输出全部指标。"""
    if df is None or df.empty or "close" not in df.columns:
        return {}
    close = df["close"].astype(float)
    high = df["high"].astype(float) if "high" in df.columns else close
    low = df["low"].astype(float) if "low" in df.columns else close
    volume = df["volume"].astype(float) if "volume" in df.columns else None

    last_close = float(close.iloc[-1])
    mas = moving_averages(close)
    return {
        "last_close": round(last_close, 4),
        "ma": mas,
        "trend_pattern": trend_pattern_from_mas(last_close, mas),
        "macd": macd(close),
        "rsi": rsi(close),
        "bollinger": bollinger(close),
        "atr": round(atr(high, low, close), 4) if atr(high, low, close) is not None else None,
        "swing": swing_points(high, low, close, lookback=lookback_swing),
        "volume_price": volume_price(close, volume),
    }
