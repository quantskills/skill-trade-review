"""逐笔交易点评 — 规则层先给每笔打三维度结构化标签，LLM 在外层做自然语言点评。

三维度（避免单信号偏置）：
  - outcome:    big_win / win / breakeven / loss / big_loss   纯结果好坏（按 0.5×日波动 归一化）
  - alignment:  aligned / mixed / against                     方向是否合规市场
  - execution:  clean / questionable / poor                   执行质量（是否硬扛、是否超时）

兼容：保留旧 rating/risk_level 字段，避免破坏其它模块；新字段为额外补充。

输入: df (validate_and_split + fill_missing_mark_and_unrealized 后的)
      market_review (build_market_review 输出)
输出: list[dict] — 每笔一行，附 evidence_keys
"""
from __future__ import annotations

import math
from typing import Any

import pandas as pd

# 标准动作系数：以 0.5 × 5 日年化波动率 为「一个标准动作」的盈亏幅度
STD_MOVE_COEF = 0.5
# 兜底波动率（缺数据时用）：股票 20%、期货 25%
DEFAULT_VOL_STOCK = 20.0
DEFAULT_VOL_FUTURE = 25.0


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _pct_of_cost(pnl: float, cost: float, vol: float, mult: float) -> float | None:
    base = cost * vol * mult
    if base <= 0:
        return None
    return round(pnl / base * 100, 2)


def _hold_days(open_date: Any, ref_date: Any) -> int | None:
    if open_date is None or pd.isna(open_date) or ref_date is None:
        return None
    try:
        od = pd.to_datetime(open_date)
        rd = pd.to_datetime(ref_date)
        return int((rd - od).days)
    except Exception:
        return None


def _get_daily_vol(asset_type: str, variety: str | None, market_review: dict) -> float:
    """返回该笔对应的日内波动幅度参考（百分比，已转成日度量纲）。

    输入 vol_5d 是 5 日年化波动率（%）。日波动 ≈ 年化 / sqrt(252)。
    """
    if asset_type == "future_cn" and variety:
        fut = (market_review.get("futures") or {}).get(variety) or {}
        vol_5d = fut.get("volatility_5d")
        if vol_5d is None:
            vol_5d = DEFAULT_VOL_FUTURE
    elif asset_type.startswith("stock"):
        stock = market_review.get("stock") or {}
        vol_5d = stock.get("index_volatility_5d")
        if vol_5d is None:
            vol_5d = DEFAULT_VOL_STOCK
    else:
        vol_5d = DEFAULT_VOL_STOCK

    # 年化 → 日波动: vol_5d% / sqrt(252) ≈ vol_5d / 15.87
    daily_vol_pct = float(vol_5d) / math.sqrt(252)
    return daily_vol_pct


# ---------- outcome 维度 ----------

def _rate_outcome(pct: float | None, pnl: float, daily_vol_pct: float) -> str:
    """按 0.5×日波动 为 1 个标准动作衡量结果。

    daily_vol_pct=2.0 (年化 32% 这种高波动) 时:
      std_move = 1.0%
      big_win  = pct ≥ 1.0%
      win      = pct ≥ 0.3%
      loss     = pct ≤ -0.3%
      big_loss = pct ≤ -1.0%

    daily_vol_pct=0.6 (年化 10% 低波动) 时:
      std_move = 0.3%
      big_win  = pct ≥ 0.3%
      win      = pct ≥ 0.09%
    """
    if pct is None:
        # 无 cost 信息时，退化为按 pnl 正负分桶
        if pnl > 0:
            return "win"
        if pnl < 0:
            return "loss"
        return "breakeven"

    std_move = STD_MOVE_COEF * daily_vol_pct  # 单位 %
    if std_move <= 0:
        std_move = 0.3  # 极端兜底

    if pct >= std_move:
        return "big_win"
    if pct >= 0.3 * std_move:
        return "win"
    if pct > -0.3 * std_move:
        return "breakeven"
    if pct > -std_move:
        return "loss"
    return "big_loss"


# ---------- alignment 维度 ----------

def _rate_alignment(against_basis: bool, against_trend: bool,
                    asset_type: str) -> str:
    """方向 vs 市场。期货同时看趋势 + 基差，股票只看趋势。"""
    if asset_type == "future_cn":
        if against_basis and against_trend:
            return "against"     # 双逆
        if against_basis or against_trend:
            return "mixed"       # 单逆（仅一项）
        return "aligned"
    # 股票/其它：只看趋势
    return "against" if against_trend else "aligned"


# ---------- execution 维度（仅对 open 笔有完整意义；closed 笔退化判断） ----------

def _rate_execution_closed(pct: float | None, pnl: float,
                           daily_vol_pct: float, hold_days: int | None) -> str:
    """已平仓的执行质量：止损是否果断、是否在合理幅度兑现。"""
    if pct is None:
        return "clean" if pnl >= 0 else "questionable"
    std_move = STD_MOVE_COEF * daily_vol_pct
    # 大亏 + 长持仓 → 硬扛 → poor
    if pct <= -2 * std_move and (hold_days or 0) >= 2:
        return "poor"
    # 单笔大亏 → 止损迟疑
    if pct <= -2 * std_move:
        return "questionable"
    # 普通区间：盈利或小亏均算执行干净（按预案离场）
    return "clean"


def _rate_execution_open(pct: float | None, hold_days: int | None,
                         daily_vol_pct: float, alignment: str) -> str:
    """未平仓的执行质量：是否在硬扛、是否超时。"""
    if pct is None:
        return "questionable"
    std_move = STD_MOVE_COEF * daily_vol_pct
    # 浮亏超过 2 个标准动作 + alignment != aligned → 硬扛
    if pct <= -2 * std_move and alignment != "aligned":
        return "poor"
    # 持仓 ≥3 天 + 浮亏 → 拖泥带水
    if (hold_days or 0) >= 3 and pct < 0:
        return "questionable"
    if pct <= -2 * std_move:
        return "questionable"
    return "clean"


# ---------- 兼容用：旧 rating / risk_level（保留以免破坏其它模块） ----------

def _legacy_rating_from_three(outcome: str, alignment: str, execution: str) -> str:
    """把三维度回填成旧 best/good/avg/poor/bad 标签，仅供兼容。"""
    if outcome == "big_win":
        return "best"
    if outcome == "win":
        return "good"
    if outcome == "breakeven":
        return "avg"
    if outcome == "loss":
        return "poor" if execution != "poor" else "bad"
    return "bad"


def _legacy_risk_from_three(outcome: str, alignment: str, execution: str) -> str:
    if execution == "poor" or outcome == "big_loss":
        return "high"
    if execution == "questionable" or outcome == "loss" or alignment == "against":
        return "medium"
    return "low"


# ---------- 方向/基差判定 ----------

def _check_against_basis(asset_type: str, variety: str | None, direction: str,
                         market_review: dict) -> bool:
    if asset_type != "future_cn" or not variety:
        return False
    fut = (market_review.get("futures") or {}).get(variety) or {}
    regime = fut.get("basis_regime")
    if regime == "contango" and direction == "long":
        return True
    if regime == "backwardation" and direction == "short":
        return True
    return False


def _check_against_trend(asset_type: str, direction: str, market_review: dict) -> bool:
    if asset_type.startswith("stock"):
        trend = (market_review.get("stock") or {}).get("trend")
    else:
        trend = market_review.get("overall_regime", "")
    if not trend:
        return False
    if direction == "long" and "down" in str(trend):
        return True
    if direction == "short" and "up" in str(trend):
        return True
    return False


# ---------- 主入口 ----------

def review_single_trade(row: pd.Series, market_review: dict) -> dict:
    asset_type = row.get("asset_type") or "unknown"
    variety = row.get("variety")
    direction = row.get("direction")
    status = row.get("status")
    ts_code = row.get("ts_code")
    vol = _safe_float(row.get("volume"))
    mult = _safe_float(row.get("multiplier"), 1.0)

    against_basis = _check_against_basis(asset_type, variety, direction, market_review)
    against_trend = _check_against_trend(asset_type, direction, market_review)
    daily_vol_pct = _get_daily_vol(asset_type, variety, market_review)
    alignment = _rate_alignment(against_basis, against_trend, asset_type)

    base = {
        "ts_code": ts_code,
        "trade_date": str(row.get("trade_date")),
        "asset_type": asset_type,
        "variety": variety,
        "direction": direction,
        "status": status,
        "volume": vol,
        "reason_tag": row.get("reason_tag") if pd.notna(row.get("reason_tag")) else None,
        "against_basis": against_basis,
        "against_trend": against_trend,
        "alignment": alignment,
        "daily_vol_pct": round(daily_vol_pct, 3),
        "std_move_pct": round(STD_MOVE_COEF * daily_vol_pct, 3),
    }

    # evidence_keys 给 LLM 用
    ek: list[str] = []
    if asset_type.startswith("stock"):
        ek.append("market_review.stock.trend")
    if asset_type == "future_cn" and variety:
        ek.append(f"market_review.futures.{variety}.basis_regime")
        ek.append(f"market_review.futures.{variety}.trend")

    if status == "closed":
        pnl = _safe_float(row.get("realized_pnl"))
        cost = _safe_float(row.get("cost_price"))
        pct = _pct_of_cost(pnl, cost, vol, mult) if cost and vol else None
        hold_d = _hold_days(row.get("open_date"), row.get("trade_date"))

        outcome = _rate_outcome(pct, pnl, daily_vol_pct)
        execution = _rate_execution_closed(pct, pnl, daily_vol_pct, hold_d)
        rating = _legacy_rating_from_three(outcome, alignment, execution)

        base.update({
            "realized_pnl": round(pnl, 2),
            "pnl_pct_of_cost": pct,
            "outcome": outcome,
            "execution": execution,
            "rating": rating,  # 兼容
            "evidence_keys": ek + [f"attribution.by_asset_type.{asset_type}"],
        })
    else:
        cost = _safe_float(row.get("cost_price"))
        mark = _safe_float(row.get("mark_price"))
        unr = _safe_float(row.get("unrealized_pnl"))
        pct = _pct_of_cost(unr, cost, vol, mult) if cost and vol else None
        hold_d = _hold_days(row.get("open_date"), row.get("trade_date"))

        outcome = _rate_outcome(pct, unr, daily_vol_pct)
        execution = _rate_execution_open(pct, hold_d, daily_vol_pct, alignment)
        risk_level = _legacy_risk_from_three(outcome, alignment, execution)

        base.update({
            "cost_price": round(cost, 4) if cost else None,
            "mark_price": round(mark, 4) if mark else None,
            "unrealized_pnl": round(unr, 2),
            "unrealized_pct_of_cost": pct,
            "hold_days": hold_d,
            "outcome": outcome,
            "execution": execution,
            "risk_level": risk_level,  # 兼容
            "evidence_keys": ek + [f"attribution.by_status.open"],
        })

    return base


def review_all_trades(df: pd.DataFrame, market_review: dict) -> dict:
    if df.empty:
        return {"closed": [], "open": [], "summary": {}}

    closed_reviews: list[dict] = []
    open_reviews: list[dict] = []

    for _, row in df.iterrows():
        rev = review_single_trade(row, market_review)
        if rev["status"] == "closed":
            closed_reviews.append(rev)
        else:
            open_reviews.append(rev)

    def _bucket(items: list[dict], key: str) -> dict[str, int]:
        out: dict[str, int] = {}
        for it in items:
            v = it.get(key)
            if v:
                out[v] = out.get(v, 0) + 1
        return out

    closed_sorted = sorted(closed_reviews, key=lambda x: x.get("realized_pnl") or 0)
    open_sorted = sorted(open_reviews, key=lambda x: x.get("unrealized_pnl") or 0)

    summary = {
        # 三维度分布
        "outcome_dist": _bucket(closed_reviews + open_reviews, "outcome"),
        "alignment_dist": _bucket(closed_reviews + open_reviews, "alignment"),
        "execution_dist": _bucket(closed_reviews + open_reviews, "execution"),
        # 兼容旧字段
        "rating_dist": _bucket(closed_reviews, "rating"),
        "risk_dist": _bucket(open_reviews, "risk_level"),
        "top_loser": closed_sorted[0] if closed_sorted else None,
        "top_winner": closed_sorted[-1] if closed_sorted and closed_sorted[-1].get("realized_pnl", 0) > 0 else None,
        "top_risk_open": next((o for o in open_sorted if o.get("execution") == "poor"), None),
        "n_against_basis": sum(1 for r in closed_reviews + open_reviews if r.get("against_basis")),
        "n_against_trend": sum(1 for r in closed_reviews + open_reviews if r.get("against_trend")),
        # 关键交叉统计：逆势赚 / 顺势亏
        "n_against_but_win": sum(1 for r in closed_reviews
                                  if r.get("alignment") != "aligned"
                                  and r.get("outcome") in ("win", "big_win")),
        "n_aligned_but_loss": sum(1 for r in closed_reviews
                                   if r.get("alignment") == "aligned"
                                   and r.get("outcome") in ("loss", "big_loss")),
    }

    return {
        "closed": closed_reviews,
        "open": open_reviews,
        "summary": summary,
    }
