"""模式识别规则池 — 确定性规则，不调 LLM。

每条规则签名: detect(df, summary, attribution, context) -> Optional[dict]
"""
from __future__ import annotations

from typing import Any, Callable

import pandas as pd


def _hit(pattern: str, evidence: str, tag: str, confidence: float, asset_scope: str = "all") -> dict:
    return {
        "pattern": pattern,
        "evidence": evidence,
        "tag": tag,
        "confidence": round(min(1.0, max(0.0, float(confidence))), 4),
        "asset_scope": asset_scope,
    }


# --------- 通用规则 ----------

def _rule_open_loss_buildup(df: pd.DataFrame, summary, attr, ctx) -> dict | None:
    """未平仓浮亏单堆积 ≥2 笔。"""
    sub = df[(df["status"] == "open") & (df["unrealized_pnl"] < 0)]
    if len(sub) >= 2:
        total = float(sub["unrealized_pnl"].sum())
        return _hit(
            "未平仓浮亏单堆积",
            f"{len(sub)} 笔未平仓亏损, 合计浮亏 {round(total, 2)}",
            "risk_buildup",
            min(1.0, len(sub) / 4),
        )
    return None


def _rule_low_winrate(df, summary, attr, ctx) -> dict | None:
    n_closed = summary.get("n_closed") or 0
    if n_closed >= 5 and (summary.get("win_rate_closed") or 0) < 0.35:
        return _hit(
            "已平仓胜率显著偏低",
            f"win_rate_closed={summary['win_rate_closed']}, n_closed={n_closed}",
            "low_winrate",
            0.8,
        )
    return None


def _rule_direction_concentration(df, summary, attr, ctx) -> dict | None:
    by_dir = attr.get("by_direction", {})
    total = sum(v["realized"] for v in by_dir.values())
    if total == 0:
        return None
    for d, stats in by_dir.items():
        if abs(stats["realized"] / total) > 0.9 and stats["realized"] < 0:
            return _hit(
                f"亏损集中于 {d} 方向",
                f"{d} 方向贡献 {stats['realized']}, 占总实现 PnL >90%",
                "direction_concentration",
                0.7,
            )
    return None


def _rule_concentration_single_variety(df, summary, attr, ctx) -> dict | None:
    by_var = attr.get("by_variety", {})
    realized_abs = sum(abs(v["realized"]) for v in by_var.values())
    if realized_abs == 0:
        return None
    for variety, stats in by_var.items():
        if variety in ("nan", "None", None):
            continue
        if abs(stats["realized"]) / realized_abs > 0.8:
            return _hit(
                f"PnL 高度集中于 {variety} 单品种",
                f"{variety} 占已实现 PnL 绝对值 {round(abs(stats['realized'])/realized_abs*100, 1)}%",
                "variety_concentration",
                0.6,
            )
    return None


def _rule_holding_long_loss(df, summary, attr, ctx) -> dict | None:
    if "open_date" not in df.columns:
        return None
    opens = df[df["status"] == "open"].copy()
    if opens.empty:
        return None
    opens = opens.dropna(subset=["open_date"])
    if opens.empty:
        return None
    opens["open_date"] = pd.to_datetime(opens["open_date"], errors="coerce")
    opens["snapshot_date"] = pd.to_datetime(opens["trade_date"], errors="coerce")
    opens["hold_days"] = (opens["snapshot_date"] - opens["open_date"]).dt.days
    bad = opens[(opens["hold_days"] >= 5) & (opens["unrealized_pnl"] < 0)]
    if len(bad) >= 2:
        return _hit(
            "持仓 5 天以上仍浮亏",
            f"{len(bad)} 笔持仓 ≥5 天且仍浮亏，合计 {round(float(bad['unrealized_pnl'].sum()), 2)}",
            "stop_loss_delay",
            0.75,
        )
    return None


# --------- 股票专属 ----------

def _rule_chase_high_in_high_vol(df, summary, attr, ctx) -> dict | None:
    """股票: 高波动日做多胜率显著低于均值。"""
    stock_df = df[(df["asset_type"] == "stock_a") & (df["status"] == "closed")]
    if len(stock_df) < 3:
        return None
    stock_ctx = (ctx or {}).get("stock") or {}
    if not stock_ctx or stock_ctx.get("index_volatility_5d") is None:
        return None
    if stock_ctx["index_volatility_5d"] < 25:
        return None
    longs = stock_df[stock_df["direction"] == "long"]
    if len(longs) < 3:
        return None
    win_rate = float((longs["realized_pnl"] > 0).mean())
    if win_rate < 0.4:
        return _hit(
            "高波动环境下做多胜率偏低",
            f"index_vol_5d={round(stock_ctx['index_volatility_5d'], 2)}%，做多 {len(longs)} 笔胜率 {round(win_rate*100,1)}%",
            "high_vol_long_bias",
            0.65,
            asset_scope="stock_a",
        )
    return None


def _rule_loss_after_breakout_in_range(df, summary, attr, ctx) -> dict | None:
    """股票: 震荡市追突破 reason_tag 亏损。"""
    if "reason_tag" not in df.columns:
        return None
    regime = (ctx or {}).get("regime_label")
    if regime != "range_bound":
        return None
    sub = df[(df["status"] == "closed") &
             (df["reason_tag"].astype(str).str.contains("breakout", case=False, na=False))]
    if len(sub) >= 3 and float(sub["realized_pnl"].sum()) < 0:
        return _hit(
            "震荡市追突破整体亏损",
            f"震荡市({regime})下 breakout 标签 {len(sub)} 笔, 合计 {round(float(sub['realized_pnl'].sum()),2)}",
            "breakout_in_range",
            0.7,
            asset_scope="stock_a",
        )
    return None


# --------- 期货专属 ----------

def _rule_against_basis_trend(df, summary, attr, ctx) -> dict | None:
    """期货: 在 contango 市仍做多 / backwardation 仍做空 → 逆基差。"""
    fut_ctxs = (ctx or {}).get("futures") or {}
    if not fut_ctxs:
        return None
    bad_count = 0
    examples: list[str] = []
    for variety, vctx in fut_ctxs.items():
        regime = vctx.get("commodity_regime")
        if not regime or regime == "flat":
            continue
        sub = df[(df["status"] == "closed") & (df["variety"] == variety)]
        if sub.empty:
            continue
        if regime == "contango":
            against = sub[(sub["direction"] == "long") & (sub["realized_pnl"] < 0)]
        else:  # backwardation
            against = sub[(sub["direction"] == "short") & (sub["realized_pnl"] < 0)]
        if len(against) >= 2:
            bad_count += len(against)
            examples.append(f"{variety}({regime}) {len(against)} 笔逆基差亏损")
    if bad_count >= 2:
        return _hit(
            "期货逆基差结构持仓亏损",
            "; ".join(examples),
            "against_basis",
            min(1.0, bad_count / 4),
            asset_scope="future_cn",
        )
    return None


def _rule_close_today_drag(df, summary, attr, ctx) -> dict | None:
    """期货: 单笔利润极小（疑似被平今手续费吞噬）— 骨架版用阈值近似。"""
    fut_closed = df[(df["asset_type"] == "future_cn") & (df["status"] == "closed")]
    if len(fut_closed) < 5:
        return None
    tiny = fut_closed[fut_closed["realized_pnl"].abs() < 50]
    if len(tiny) >= 3 and (fut_closed["realized_pnl"].sum()) < 0:
        return _hit(
            "期货单笔利润过小，疑被手续费拖累",
            f"{len(tiny)} 笔 |PnL|<50 出现在亏损批次中",
            "fee_drag",
            0.55,
            asset_scope="future_cn",
        )
    return None


def _rule_cross_variety_correlated_loss(df, summary, attr, ctx) -> dict | None:
    """期货: 同向相关品种共同亏损（如 RB+HC 同时做多并亏）。"""
    correlated_groups = [
        {"RB", "HC", "I"},
        {"M", "Y", "P"},
        {"CU", "AL", "ZN"},
        {"IF", "IH", "IC", "IM"},
    ]
    fut_closed = df[(df["asset_type"] == "future_cn") & (df["status"] == "closed")]
    if fut_closed.empty:
        return None
    for group in correlated_groups:
        sub = fut_closed[fut_closed["variety"].isin(group)]
        if sub["variety"].nunique() < 2:
            continue
        total = float(sub["realized_pnl"].sum())
        if total < 0 and len(sub) >= 2:
            return _hit(
                f"相关品种 {sorted(set(sub['variety']))} 同向共损",
                f"该组合合计 {round(total,2)}, 笔数 {len(sub)}",
                "cross_variety_loss",
                0.6,
                asset_scope="future_cn",
            )
    return None


# --------- 注册表 ----------

ALL_RULES: list[Callable[[pd.DataFrame, dict, dict, Any], dict | None]] = [
    _rule_open_loss_buildup,
    _rule_low_winrate,
    _rule_direction_concentration,
    _rule_concentration_single_variety,
    _rule_holding_long_loss,
    _rule_chase_high_in_high_vol,
    _rule_loss_after_breakout_in_range,
    _rule_against_basis_trend,
    _rule_close_today_drag,
    _rule_cross_variety_correlated_loss,
]


def detect_patterns(df: pd.DataFrame, summary: dict, attribution: dict, context: dict | None) -> list[dict]:
    hits: list[dict] = []
    for rule in ALL_RULES:
        try:
            r = rule(df, summary, attribution, context)
        except Exception:
            r = None
        if r:
            hits.append(r)
    return hits
