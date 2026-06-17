"""市场行情研判 — 把 context 转成结构化 regime 标签 + per-asset 简评 + 技术形态。

输入: load_context 的输出 (stock + futures + regime_label, 含 technical_daily/weekly/15min)
输出: {
    "stock": {trend, volatility, money_flow, regime, comment, technical: {...}, key_levels: {...}},
    "futures": {variety: {trend, ..., technical: {...}, key_levels: {...}}},
    "overall_regime": str,
    "regime_keys": list[str],
}

确定性规则；不调 LLM。LLM 在外层用 narrative 字段做技术形态叙述与延展。
"""
from __future__ import annotations

from typing import Any


def _bucket_pct(pct: float | None) -> str:
    if pct is None:
        return "unknown"
    if pct >= 1.5:
        return "strong_up"
    if pct >= 0.3:
        return "mild_up"
    if pct <= -1.5:
        return "strong_down"
    if pct <= -0.3:
        return "mild_down"
    return "flat"


def _bucket_vol(vol: float | None) -> str:
    if vol is None:
        return "unknown"
    if vol >= 30:
        return "high"
    if vol >= 18:
        return "medium"
    return "low"


def _stock_money_flow(stock_ctx: dict) -> str:
    amt = stock_ctx.get("north_hold_amount")
    if amt is None:
        return "unknown"
    if amt > 0:
        return "inflow"
    if amt < 0:
        return "outflow"
    return "neutral"


# ---------- 技术形态合成 ----------

def _classify_pattern(tech_daily: dict, tech_weekly: dict | None = None) -> str:
    """把日线技术指标合成成一个易读的形态标签。"""
    if not tech_daily:
        return "no_data"
    trend = tech_daily.get("trend_pattern")
    macd_state = (tech_daily.get("macd") or {}).get("status")
    boll_pos = (tech_daily.get("bollinger") or {}).get("position")
    swing = tech_daily.get("swing") or {}
    vp = (tech_daily.get("volume_price") or {}).get("status")

    pos_in_range = swing.get("position_in_range")

    # 突破/跌破
    if pos_in_range is not None:
        if pos_in_range > 0.95 and (vp == "volume_up_price_up" or boll_pos == "above_upper"):
            return "breakout_high"
        if pos_in_range < 0.05 and (vp == "volume_up_price_down" or boll_pos == "below_lower"):
            return "breakdown_low"

    # 多头 / 空头排列
    if trend == "bull_arrangement" and macd_state in ("bull_strengthening", "bull_weakening"):
        return "uptrend_continuation"
    if trend == "bear_arrangement" and macd_state in ("bear_strengthening", "bear_weakening"):
        return "downtrend_continuation"

    # 趋势减弱预警
    if trend == "bull_arrangement" and macd_state == "bull_weakening" and vp == "volume_down_price_up":
        return "uptrend_weakening"
    if trend == "bear_arrangement" and macd_state == "bear_weakening" and vp == "volume_down_price_down":
        return "downtrend_weakening"

    # 反弹/回调
    if trend == "bear_rebound":
        return "bear_rebound"
    if trend == "bull_pullback":
        return "bull_pullback"

    # 震荡
    if trend in ("ma_tangled", "ma_mixed"):
        return "ranging"

    return trend or "ranging"


def _key_levels_from_tech(tech_daily: dict, tech_weekly: dict | None = None) -> dict:
    """提取关键价位：支撑/阻力/MA20/MA60/前高前低/布林上下轨/ATR 区间。"""
    out: dict = {}
    if not tech_daily:
        return out
    last = tech_daily.get("last_close")
    out["last"] = last

    ma = tech_daily.get("ma") or {}
    for k in ("ma5", "ma10", "ma20", "ma60"):
        if ma.get(k) is not None:
            out[k] = ma[k]

    boll = tech_daily.get("bollinger") or {}
    if boll.get("upper") is not None:
        out["bb_upper"] = boll["upper"]
    if boll.get("lower") is not None:
        out["bb_lower"] = boll["lower"]

    swing = tech_daily.get("swing") or {}
    if swing.get("recent_high") is not None:
        out["recent_high"] = swing["recent_high"]
    if swing.get("recent_low") is not None:
        out["recent_low"] = swing["recent_low"]

    atr_v = tech_daily.get("atr")
    if atr_v is not None and last is not None:
        out["atr"] = atr_v
        out["atr_upper_1x"] = round(last + atr_v, 4)
        out["atr_lower_1x"] = round(last - atr_v, 4)

    # 周线 MA20 作为大级别参考
    if tech_weekly:
        wma = (tech_weekly.get("ma") or {})
        if wma.get("ma20") is not None:
            out["weekly_ma20"] = wma["ma20"]

    # 由价格离均线/前高低的距离推断「最近支撑」「最近阻力」
    if last is not None:
        below = sorted([v for v in (out.get("ma5"), out.get("ma10"), out.get("ma20"),
                                    out.get("ma60"), out.get("bb_lower"),
                                    out.get("recent_low"))
                        if v is not None and v < last], reverse=True)
        above = sorted([v for v in (out.get("ma5"), out.get("ma10"), out.get("ma20"),
                                    out.get("ma60"), out.get("bb_upper"),
                                    out.get("recent_high"))
                        if v is not None and v > last])
        if below:
            out["nearest_support"] = below[0]
        if above:
            out["nearest_resistance"] = above[0]

    return out


def _stock_review(stock_ctx: dict | None, regime_label: str | None) -> dict:
    if not stock_ctx:
        return {}
    pct = stock_ctx.get("index_pct_pct")
    vol = stock_ctx.get("index_volatility_5d")
    trend = _bucket_pct(pct)
    volatility = _bucket_vol(vol)
    flow = _stock_money_flow(stock_ctx)

    parts: list[str] = []
    bench = stock_ctx.get("benchmark", "基准指数")
    if pct is not None:
        parts.append(f"{bench} 当日 {pct:+.2f}%")
    if vol is not None:
        parts.append(f"5 日年化波动 {vol:.1f}%")
    if flow != "unknown":
        parts.append(f"北向资金{ {'inflow':'净流入','outflow':'净流出','neutral':'持平'}[flow] }")
    if regime_label:
        parts.append(f"环境标签 {regime_label}")
    comment = "；".join(parts) if parts else "市场环境数据缺失"

    tech_daily = stock_ctx.get("technical_daily") or {}
    tech_weekly = stock_ctx.get("technical_weekly") or {}
    pattern = _classify_pattern(tech_daily, tech_weekly)
    key_levels = _key_levels_from_tech(tech_daily, tech_weekly)

    return {
        "benchmark": bench,
        "index_pct_pct": pct,
        "index_volatility_5d": vol,
        "trend": trend,
        "volatility": volatility,
        "money_flow": flow,
        "regime": regime_label,
        "comment": comment,
        "technical_daily": tech_daily or None,
        "technical_weekly": tech_weekly or None,
        "pattern": pattern,
        "key_levels": key_levels,
    }


def _ls_signal(ls_ratio: float | None) -> str:
    if ls_ratio is None:
        return "unknown"
    if ls_ratio >= 1.3:
        return "long_dominant"
    if ls_ratio <= 0.77:
        return "short_dominant"
    return "balanced"


def _future_review_one(variety: str, fctx: dict) -> dict:
    pct = fctx.get("dominant_pct_pct")
    vol = fctx.get("volatility_5d")
    basis = fctx.get("basis_rate") if fctx.get("basis_rate") is not None else fctx.get("basis")
    regime = fctx.get("commodity_regime")
    ls = fctx.get("ls_ratio")
    ls_sig = _ls_signal(ls)

    parts: list[str] = []
    if pct is not None:
        parts.append(f"{variety} 主力当日 {pct:+.2f}%")
    if vol is not None:
        parts.append(f"5 日年化波动 {vol:.1f}%")
    if regime:
        zh = {"contango": "贴水/远月升水(contango)",
              "backwardation": "近月升水(backwardation)",
              "flat": "基差平稳"}.get(regime, regime)
        parts.append(f"基差结构 {zh}")
    if ls is not None:
        parts.append(f"多空比 {ls:.2f}({ {'long_dominant':'多头主导','short_dominant':'空头主导','balanced':'均衡'}.get(ls_sig,'-') })")
    comment = "；".join(parts) if parts else f"{variety} 行情数据缺失"

    tech_daily = fctx.get("technical_daily") or {}
    tech_weekly = fctx.get("technical_weekly") or {}
    tech_15min = fctx.get("technical_15min") or {}
    pattern = _classify_pattern(tech_daily, tech_weekly)
    key_levels = _key_levels_from_tech(tech_daily, tech_weekly)

    return {
        "variety": variety,
        "dominant_close": fctx.get("dominant_close"),
        "dominant_pct_pct": pct,
        "volatility_5d": vol,
        "trend": _bucket_pct(pct),
        "volatility": _bucket_vol(vol),
        "basis_regime": regime,
        "ls_ratio": ls,
        "ls_signal": ls_sig,
        "warehouse_receipt": fctx.get("warehouse_receipt"),
        "comment": comment,
        "technical_daily": tech_daily or None,
        "technical_weekly": tech_weekly or None,
        "technical_15min": tech_15min or None,
        "pattern": pattern,
        "key_levels": key_levels,
    }


def _overall_regime(stock_review: dict, future_reviews: dict[str, dict]) -> str:
    s_trend = stock_review.get("trend") if stock_review else None
    s_vol = stock_review.get("volatility") if stock_review else None

    if s_vol == "high":
        return "high_vol_market"
    if s_trend in ("strong_up", "mild_up"):
        return "stock_up_trend"
    if s_trend in ("strong_down", "mild_down"):
        return "stock_down_trend"

    fut_trends = [f["trend"] for f in future_reviews.values() if f.get("trend") not in (None, "unknown")]
    if fut_trends:
        ups = sum(1 for t in fut_trends if "up" in t)
        downs = sum(1 for t in fut_trends if "down" in t)
        if ups > downs and ups >= len(fut_trends) * 0.6:
            return "futures_up_trend"
        if downs > ups and downs >= len(fut_trends) * 0.6:
            return "futures_down_trend"

    return stock_review.get("regime") if stock_review else "unknown"


def build_market_review(context: dict | None) -> dict:
    ctx = context or {}
    stock_ctx = ctx.get("stock")
    fut_ctxs = ctx.get("futures") or {}
    regime_label = ctx.get("regime_label")

    stock_review = _stock_review(stock_ctx, regime_label)
    future_reviews = {v: _future_review_one(v, fctx) for v, fctx in fut_ctxs.items()}
    overall = _overall_regime(stock_review, future_reviews)

    keys: list[str] = []
    if stock_ctx:
        keys.extend([f"context.stock.{k}" for k in
                     ("benchmark", "index_pct_pct", "index_volatility_5d") if k in stock_ctx])
    for v, fctx in fut_ctxs.items():
        keys.extend([f"context.futures.{v}.{k}" for k in
                     ("dominant_pct_pct", "volatility_5d", "commodity_regime", "ls_ratio") if k in fctx])

    return {
        "stock": stock_review,
        "futures": future_reviews,
        "overall_regime": overall,
        "regime_keys": keys,
        "fetch_failures": ctx.get("fetch_failures") or [],
    }
