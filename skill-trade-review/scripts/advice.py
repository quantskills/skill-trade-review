"""决策建议生成 — 三档输出: actions_now / actions_next_session / process_changes。

每条建议都带:
  - target_ts_code (可选)
  - action (动作)
  - reason (依据)
  - trigger (触发条件)
  - priority (high/medium/low)
  - tag (与 patterns 联动)
"""
from __future__ import annotations

from typing import Any

PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}


# ---------- 模板：模式 → 建议（兼容旧 narrative 用） ----------

PATTERN_TEMPLATES: dict[str, dict] = {
    "risk_buildup": {
        "priority": "high",
        "bucket": "actions_now",
        "action": "评估未平仓亏损单是否仍符合原始入场逻辑，对偏离逻辑的笔执行止损或减仓",
        "trigger": "明日开盘前完成全部未平仓笔的逻辑回看",
        "expected_impact": "降低尾部风险敞口",
    },
    "low_winrate": {
        "priority": "high",
        "bucket": "process_changes",
        "action": "回看入场信号过滤条件，提高胜率门槛或降低交易频次",
        "trigger": "下一交易日盘前固化至 trade plan",
        "expected_impact": "把胜率拉回基线以上",
    },
    "direction_concentration": {
        "priority": "medium",
        "bucket": "actions_next_session",
        "action": "对集中亏损方向的新增仓位做对冲或减仓，分散单边敞口",
        "trigger": "下一交易日开盘前调整持仓 beta",
        "expected_impact": "降低单边波动敞口",
    },
    "variety_concentration": {
        "priority": "medium",
        "bucket": "actions_next_session",
        "action": "评估对该品种的特异性敞口是否过高，必要时减仓或加对冲",
        "trigger": "在该品种敞口 > 账户 30% 时执行",
        "expected_impact": "降低单品种集中风险",
    },
    "stop_loss_delay": {
        "priority": "high",
        "bucket": "actions_now",
        "action": "对持仓 ≥5 天且仍浮亏的笔执行强制复盘，按规则止损或调仓",
        "trigger": "今晚收盘后逐笔过审",
        "expected_impact": "缩短亏损周期，避免拖累后续表现",
    },
    "high_vol_long_bias": {
        "priority": "medium",
        "bucket": "process_changes",
        "action": "在指数 5 日波动率高于 25% 时，要么提高做多门槛、要么改用日内或卖权策略",
        "trigger": "下一交易日开盘前更新交易门槛",
        "expected_impact": "改善高波动日的胜率分布",
    },
    "breakout_in_range": {
        "priority": "medium",
        "bucket": "process_changes",
        "action": "震荡市标签下关闭 breakout 入场，转用 mean_reversion 或观望",
        "trigger": "regime 标签为 range_bound 时立即生效",
        "expected_impact": "避免在震荡市追高被打回",
    },
    "against_basis": {
        "priority": "high",
        "bucket": "actions_now",
        "action": "在 contango 市避免裸多 / backwardation 市避免裸空，必要时跨期对冲",
        "trigger": "明日开盘前对相关品种持仓加跨期对冲",
        "expected_impact": "对齐基差结构，提高期货胜率",
    },
    "fee_drag": {
        "priority": "low",
        "bucket": "process_changes",
        "action": "评估当日是否需要平今或换隔日单，避免被平今手续费侵蚀微利",
        "trigger": "策略层加入 fee-aware 出场规则",
        "expected_impact": "降低高频交易的费用占比",
    },
    "cross_variety_loss": {
        "priority": "medium",
        "bucket": "actions_next_session",
        "action": "对相关品种组合做仓位上限管理，单组合敞口不超账户的 30%",
        "trigger": "下一交易日开盘前重置仓位上限",
        "expected_impact": "降低相关性失效带来的连锁亏损",
    },
}


# ---------- 逐笔点评 → 决策 ----------

def _advice_from_open_trade(rev: dict) -> dict | None:
    """根据 open trade 的 risk_level + against_* 输出 actions_now。"""
    if rev.get("risk_level") != "high":
        return None
    ts_code = rev["ts_code"]
    direction = rev.get("direction")
    pct = rev.get("unrealized_pct_of_cost")
    hold_d = rev.get("hold_days")
    against_basis = rev.get("against_basis")
    against_trend = rev.get("against_trend")

    reasons: list[str] = []
    if pct is not None and pct <= -3:
        reasons.append(f"浮亏占成本 {pct}%")
    if hold_d and hold_d >= 5:
        reasons.append(f"已持仓 {hold_d} 天仍未盈利")
    if against_basis:
        reasons.append("方向逆基差结构")
    if against_trend:
        reasons.append("方向逆当日大盘趋势")
    if not reasons:
        return None

    action = f"对 {ts_code} {direction} 持仓减仓 50% 或执行止损"
    if against_basis:
        action = f"对 {ts_code} {direction} 仓加跨期对冲，或减仓 50%"

    trigger = (f"次日开盘 30 分钟内，若价格未回到成本价 -1% 区间则执行"
               if pct is not None else "次日开盘前完成逻辑回看")

    return {
        "priority": "high",
        "bucket": "actions_now",
        "target_ts_code": ts_code,
        "action": action,
        "reason": "; ".join(reasons),
        "trigger": trigger,
        "expected_impact": "止住浮亏扩大",
        "tag": "single_trade_risk",
    }


def _advice_from_closed_bad(rev: dict) -> dict | None:
    """根据 closed bad rating 输出 process_changes。"""
    if rev.get("rating") not in ("bad", "poor"):
        return None
    ts_code = rev["ts_code"]
    pnl = rev.get("realized_pnl")
    pct = rev.get("pnl_pct_of_cost")
    against_basis = rev.get("against_basis")
    against_trend = rev.get("against_trend")
    reason_tag = rev.get("reason_tag")

    reasons: list[str] = []
    if pct is not None:
        reasons.append(f"亏损占成本 {pct}%")
    elif pnl is not None and pnl < 0:
        reasons.append(f"实现亏损 {pnl}")
    if against_basis:
        reasons.append("逆基差结构入场")
    if against_trend:
        reasons.append("逆当日大盘趋势入场")
    if reason_tag:
        reasons.append(f"入场理由 {reason_tag}")
    if not reasons:
        return None

    return {
        "priority": "medium",
        "bucket": "process_changes",
        "target_ts_code": ts_code,
        "action": f"复盘 {ts_code} 入场逻辑，{'修正基差/趋势过滤器' if (against_basis or against_trend) else '提高入场置信门槛'}",
        "reason": "; ".join(reasons),
        "trigger": "下一交易日盘前固化至 trade plan",
        "expected_impact": "避免重复同类亏损",
        "tag": "trade_lesson",
    }


# ---------- 主入口 ----------

def generate_advice(patterns: list[dict], trade_reviews: dict | None = None,
                    market_review: dict | None = None) -> dict:
    """生成三档建议，返回 dict 形态。

    返回:
        {
            "actions_now": [...],            # 今日/未平仓笔的具体动作
            "actions_next_session": [...],   # 下一交易日盘前要做的事
            "process_changes": [...],        # 流程改进项
            "flat": [...],                   # 兼容旧版 advice 列表（按 priority 排序）
        }
    """
    buckets = {"actions_now": [], "actions_next_session": [], "process_changes": []}
    seen: set[tuple[str, str]] = set()  # (tag, target_ts_code)

    # 1. patterns → 建议
    for p in patterns or []:
        tag = p.get("tag")
        tpl = PATTERN_TEMPLATES.get(tag)
        if not tpl:
            continue
        key = (tag, "")
        if key in seen:
            continue
        seen.add(key)
        buckets[tpl["bucket"]].append({
            "priority": tpl["priority"],
            "bucket": tpl["bucket"],
            "target_ts_code": None,
            "action": tpl["action"],
            "reason": p.get("evidence"),
            "trigger": tpl["trigger"],
            "expected_impact": tpl["expected_impact"],
            "tag": tag,
        })

    # 2. trade_reviews → 单笔级建议
    tr = trade_reviews or {}
    for rev in tr.get("open", []):
        adv = _advice_from_open_trade(rev)
        if not adv:
            continue
        key = (adv["tag"], adv["target_ts_code"] or "")
        if key in seen:
            continue
        seen.add(key)
        buckets["actions_now"].append(adv)

    for rev in tr.get("closed", []):
        adv = _advice_from_closed_bad(rev)
        if not adv:
            continue
        key = (adv["tag"], adv["target_ts_code"] or "")
        if key in seen:
            continue
        seen.add(key)
        buckets["process_changes"].append(adv)

    # 3. market_review → 大盘级建议
    if market_review:
        overall = market_review.get("overall_regime")
        stock = market_review.get("stock") or {}
        if overall == "high_vol_market" and ("high_vol_long_bias", "") not in seen:
            buckets["actions_next_session"].append({
                "priority": "medium",
                "bucket": "actions_next_session",
                "target_ts_code": None,
                "action": "高波动市下统一降低 beta，单笔最大仓位下调 30%",
                "reason": f"指数 5 日年化波动 {stock.get('index_volatility_5d')}%",
                "trigger": "次日开盘前统一调整仓位上限",
                "expected_impact": "降低高波动尾部风险",
                "tag": "high_vol_market",
            })

    # 4. 排序 & 输出
    for bucket in buckets.values():
        bucket.sort(key=lambda x: PRIORITY_ORDER.get(x.get("priority"), 9))

    flat = sorted(
        [a for items in buckets.values() for a in items],
        key=lambda x: (PRIORITY_ORDER.get(x.get("priority"), 9), x.get("bucket", "")),
    )

    return {
        "actions_now": buckets["actions_now"],
        "actions_next_session": buckets["actions_next_session"],
        "process_changes": buckets["process_changes"],
        "flat": flat,
    }
