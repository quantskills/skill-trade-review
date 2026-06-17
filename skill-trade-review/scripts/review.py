"""复盘主入口 — 串联 校验 → 拉环境 → 归因 → 模式 → 建议 → LLM → 输出。"""
from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

# 允许 scripts/ 目录直接 import 同级模块
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pnl_normalizer import validate_and_split, fill_missing_mark_and_unrealized
from context_loader import load_context, make_mark_price_fetcher
from patterns import detect_patterns
from advice import generate_advice
from llm_layer import call_llm
from market_review import build_market_review
from per_trade_review import review_all_trades
from strategy_doc import load_strategy
from strategy_review import call_strategy_review

BUILD_ID = "R00"
BUILD_NAME = "交易复盘"


# --- 评级 ---------------------------------------------------------

def _rate(score: float, n_closed: int) -> tuple[str, list[str]]:
    warnings: list[str] = []
    if n_closed < 5:
        warnings.append(f"已平仓样本数 {n_closed} < 5，评级降级为 neutral")
        return "neutral", warnings
    if score > 0.6:
        return "excellent", warnings
    if score > 0.3:
        return "good", warnings
    if score > -0.1:
        return "neutral", warnings
    if score > -0.4:
        return "poor", warnings
    return "bad", warnings


def _compute_score(summary: dict, asset_mix: dict[str, float], context: dict | None) -> float:
    wr = summary.get("win_rate_closed") or 0.0
    pf = summary.get("profit_factor")
    pf = 1.0 if pf is None else float(pf)

    fut_ratio = asset_mix.get("future_cn", 0.0)
    baseline = 0.4 * fut_ratio + 0.5 * (1 - fut_ratio)

    # vs benchmark 项: 用 total_pnl 相对 open_exposure 的小幅正负作为粗略 alpha 替代
    exposure = (summary.get("open_exposure") or 0.0) + abs(summary.get("realized_pnl") or 0.0)
    alpha = (summary.get("total_pnl") or 0.0) / exposure if exposure else 0.0

    score = (
        0.4 * (wr - baseline) * 2.0
        + 0.4 * math.tanh(pf - 1.0)
        + 0.2 * math.tanh(alpha * 50)
    )
    return float(round(score, 4))


# --- summary -----------------------------------------------------

def _compute_summary(df: pd.DataFrame, include_open_in_winrate: bool = False) -> dict:
    closed = df[df["status"] == "closed"]
    opens = df[df["status"] == "open"]

    realized = float(closed["realized_pnl"].sum()) if not closed.empty else 0.0
    unrealized = float(opens["unrealized_pnl"].sum()) if not opens.empty else 0.0

    if include_open_in_winrate:
        wins = int((closed["realized_pnl"] > 0).sum() if not closed.empty else 0) + \
               int((opens["unrealized_pnl"] > 0).sum() if not opens.empty else 0)
        denom = len(closed) + len(opens)
    else:
        wins = int((closed["realized_pnl"] > 0).sum()) if not closed.empty else 0
        denom = len(closed)

    win_rate = float(wins) / denom if denom else 0.0
    avg_win = float(closed.loc[closed["realized_pnl"] > 0, "realized_pnl"].mean()) if not closed.empty and (closed["realized_pnl"] > 0).any() else 0.0
    avg_loss = float(closed.loc[closed["realized_pnl"] < 0, "realized_pnl"].mean()) if not closed.empty and (closed["realized_pnl"] < 0).any() else 0.0

    if not closed.empty and (closed["realized_pnl"] < 0).any():
        profit_factor = float(closed.loc[closed["realized_pnl"] > 0, "realized_pnl"].sum() /
                              abs(closed.loc[closed["realized_pnl"] < 0, "realized_pnl"].sum()))
    elif not closed.empty and (closed["realized_pnl"] > 0).any():
        profit_factor = float("inf")
    else:
        profit_factor = 1.0

    if not opens.empty:
        open_exposure = float((opens["cost_price"].astype(float) *
                               opens["volume"].astype(float) *
                               opens["multiplier"].astype(float)).sum())
    else:
        open_exposure = 0.0

    return {
        "n_trades": int(len(df)),
        "n_closed": int(len(closed)),
        "n_open": int(len(opens)),
        "n_winning_closed": int(wins),
        "win_rate_closed": round(win_rate, 4),
        "realized_pnl": round(realized, 2),
        "unrealized_pnl": round(unrealized, 2),
        "total_pnl": round(realized + unrealized, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "profit_factor": None if math.isinf(profit_factor) else round(profit_factor, 4),
        "open_exposure": round(open_exposure, 2),
    }


# --- attribution -------------------------------------------------

def _group_pnl(df: pd.DataFrame, by: str) -> dict:
    if by not in df.columns or df.empty:
        return {}
    out: dict[str, dict] = {}
    for key, sub in df.groupby(by, dropna=False):
        closed = sub[sub["status"] == "closed"]
        opens = sub[sub["status"] == "open"]
        out[str(key)] = {
            "realized": round(float(closed["realized_pnl"].sum()) if not closed.empty else 0.0, 2),
            "unrealized": round(float(opens["unrealized_pnl"].sum()) if not opens.empty else 0.0, 2),
            "n_closed": int(len(closed)),
            "n_open": int(len(opens)),
        }
    return out


def _compute_attribution(df: pd.DataFrame) -> dict:
    return {
        "by_asset_type": _group_pnl(df, "asset_type"),
        "by_status": {
            "closed": round(float(df.loc[df["status"] == "closed", "realized_pnl"].sum()) if (df["status"] == "closed").any() else 0.0, 2),
            "open": round(float(df.loc[df["status"] == "open", "unrealized_pnl"].sum()) if (df["status"] == "open").any() else 0.0, 2),
        },
        "by_direction": _group_pnl(df, "direction"),
        "by_variety": _group_pnl(df, "variety"),
        "by_account": _group_pnl(df, "account_id"),
        "by_reason_tag": _group_pnl(df, "reason_tag") if "reason_tag" in df.columns else {},
    }


# --- narrative_md (人类可读复盘报告) -----------------------------

def _fmt_money(x: float | None) -> str:
    if x is None:
        return "—"
    sign = "+" if x > 0 else ""
    return f"{sign}{x:,.2f}"


def _format_key_levels(market_review: dict, llm_klu: list[dict] | None) -> list[str]:
    """把 market_review 的 key_levels + LLM 标注的 key_levels_used 合成可读列表。"""
    out: list[str] = []

    # LLM 主动标注的优先（含角色 support / resistance / trigger / neutral_zone）
    if llm_klu:
        out.append("- **LLM 标注（按角色）**")
        role_zh = {"support": "支撑", "resistance": "阻力",
                   "trigger_long": "偏多触发位", "trigger_short": "偏空触发位",
                   "neutral_zone": "中性区"}
        for item in llm_klu:
            role = role_zh.get(item.get("role"), item.get("role") or "-")
            out.append(f"  - {role} `{item.get('name')}` = {item.get('value')}")

    # 规则层结构化价位（股票 + 各期货品种）
    def _emit_for(label: str, kl: dict | None) -> None:
        if not kl:
            return
        atom = []
        for k_zh, k in [("当前价", "last"), ("最近支撑", "nearest_support"),
                        ("最近阻力", "nearest_resistance"),
                        ("MA20", "ma20"), ("MA60", "ma60"),
                        ("近 N 日高", "recent_high"), ("近 N 日低", "recent_low"),
                        ("布林上轨", "bb_upper"), ("布林下轨", "bb_lower"),
                        ("ATR", "atr"), ("周线 MA20", "weekly_ma20")]:
            if kl.get(k) is not None:
                atom.append(f"{k_zh}={kl[k]}")
        if atom:
            out.append(f"- **{label}**: " + " / ".join(atom))

    stock = (market_review.get("stock") or {})
    if stock.get("key_levels"):
        _emit_for(f"{stock.get('benchmark', '股票基准')}", stock.get("key_levels"))
    for v, fctx in (market_review.get("futures") or {}).items():
        if fctx.get("key_levels"):
            _emit_for(f"{v} 主力", fctx.get("key_levels"))

    return out


def _format_strategy_review(sr: dict | None) -> list[str]:
    """策略意图层 → markdown 段落。sr 缺/失败时返回空列表。"""
    if not sr:
        return []
    md = sr.get("narrative_md")
    fitness = sr.get("fitness")
    fitness_reason = sr.get("fitness_reason")
    obs = sr.get("observations") or []
    adv = sr.get("decision_advice") or []
    meta_status = (sr.get("meta") or {}).get("status")

    if not md and not obs and not adv:
        return []

    out: list[str] = ["", "## 二·补充：策略适配复盘"]
    if fitness:
        zh = {"fit": "✅ 完全契合", "partial": "⚠️ 部分契合",
              "unfit": "❌ 明显不适合", "unknown": "❓ 信息不足"}.get(fitness, fitness)
        out.append(f"- **适配判断**: {zh}" + (f" — {fitness_reason}" if fitness_reason else ""))

    if md:
        out.append("")
        out.append(md)

    if obs:
        out.append("")
        out.append("### 关键观察")
        kind_zh = {"match": "✓ 命中", "violation": "✗ 违反",
                   "warning": "⚠ 警告", "info": "· 信息"}
        for o in obs:
            kind = kind_zh.get(o.get("kind"), o.get("kind") or "-")
            rule = o.get("rule_ref") or ""
            data = o.get("data_ref") or ""
            comment = o.get("comment") or ""
            extras = " / ".join(x for x in (rule, data) if x)
            extras_str = f" *(对照: {extras})*" if extras else ""
            out.append(f"- {kind} {comment}{extras_str}")

    if adv:
        out.append("")
        out.append("### 策略级决策建议")
        action_zh = {"continue": "继续执行", "pause": "暂停", "adjust": "调整",
                     "take_profit": "止盈", "stop_loss": "止损",
                     "switch_target": "换标"}
        for a in adv:
            action = action_zh.get(a.get("action"), a.get("action") or "-")
            prio = (a.get("priority") or "medium").upper()
            out.append(f"- [{prio}] **{action}** — {a.get('reason')}")
            if a.get("trigger"):
                out.append(f"  - 触发: {a['trigger']}")

    if meta_status and meta_status not in ("ok", "cache_hit"):
        out.append("")
        out.append(f"_(策略层状态: {meta_status})_")

    return out


def _compose_narrative_md(summary: dict, market_review: dict, trade_reviews: dict,
                          advice: dict, llm_out: dict,
                          strategy_review_out: dict | None = None) -> str:
    """把所有素材拼成一份完整的 markdown 复盘报告。"""
    lines: list[str] = []

    # ---- 1. 总览 ----
    lines.append("# 交易复盘报告")
    lines.append("")
    lines.append("## 一、当日盘面概览")
    lines.append(
        f"- 总笔数: {summary['n_trades']}（已平 {summary['n_closed']} / 未平 {summary['n_open']}）"
    )
    lines.append(
        f"- 已实现 PnL: {_fmt_money(summary['realized_pnl'])}；"
        f"浮动 PnL: {_fmt_money(summary['unrealized_pnl'])}；"
        f"合计: **{_fmt_money(summary['total_pnl'])}**"
    )
    lines.append(
        f"- 已平仓胜率: {summary['win_rate_closed']*100:.1f}%；"
        f"盈亏比: {summary.get('profit_factor') if summary.get('profit_factor') is not None else '∞'}；"
        f"未平仓敞口: {_fmt_money(summary['open_exposure'])}"
    )
    # 三维度分布速览
    tr_summary = trade_reviews.get("summary") or {}
    od = tr_summary.get("outcome_dist") or {}
    al = tr_summary.get("alignment_dist") or {}
    ex = tr_summary.get("execution_dist") or {}
    if od or al or ex:
        def _fmt_dist(d: dict) -> str:
            return ", ".join(f"{k}={v}" for k, v in sorted(d.items(), key=lambda kv: -kv[1])) if d else "-"
        lines.append(f"- 结果分布: {_fmt_dist(od)}")
        lines.append(f"- 方向合规: {_fmt_dist(al)}")
        lines.append(f"- 执行质量: {_fmt_dist(ex)}")
        n_a_w = tr_summary.get("n_against_but_win", 0)
        n_a_l = tr_summary.get("n_aligned_but_loss", 0)
        if n_a_w or n_a_l:
            lines.append(f"- 逆势盈利 {n_a_w} 笔 / 顺势亏损 {n_a_l} 笔")
    lines.append("")

    # ---- 2. 市场行情研判 ----
    lines.append("## 二、市场行情研判")
    if llm_out.get("market_narrative_md"):
        lines.append(llm_out["market_narrative_md"])
    else:
        # 规则降级版
        stock = market_review.get("stock") or {}
        if stock:
            lines.append(f"### 股票市场")
            lines.append(f"- {stock.get('comment', '数据缺失')}")
            if stock.get("pattern"):
                lines.append(f"- 形态标签: `{stock['pattern']}`")
        fut = market_review.get("futures") or {}
        if fut:
            lines.append(f"### 期货品种")
            for v, fctx in fut.items():
                lines.append(f"- **{v}**: {fctx.get('comment', '数据缺失')}")
                if fctx.get("pattern"):
                    lines.append(f"  - 形态标签: `{fctx['pattern']}`")
        if not stock and not fut:
            lines.append("- 市场环境数据缺失（context 未拉取或品种未识别）")
        lines.append(f"- 综合标签: `{market_review.get('overall_regime')}`")

    # 关键价位汇总（无论 LLM 是否成功都展示）
    key_lines = _format_key_levels(market_review, llm_out.get("market_key_levels_used"))
    if key_lines:
        lines.append("")
        lines.append("### 关键价位汇总")
        lines.extend(key_lines)
    lines.append("")

    # ---- 2.5 策略适配复盘（可选，仅在策略文件启用时显示） ----
    sr_lines = _format_strategy_review(strategy_review_out)
    if sr_lines:
        lines.extend(sr_lines)
        lines.append("")

    # ---- 3. 逐笔交易点评 ----
    lines.append("## 三、逐笔交易点评")
    # LLM 点评按 idx (C0/C1.../O0/O1...) 直接对齐索引
    llm_by_idx: dict[str, dict] = {}
    for c in (llm_out.get("trade_review_comments") or []):
        idx = c.get("idx")
        if idx:
            llm_by_idx[idx] = c

    closed = trade_reviews.get("closed", [])
    if closed:
        lines.append("### 已平仓笔")
        for i, r in enumerate(closed):
            outcome = (r.get("outcome") or "-").upper()
            alignment = (r.get("alignment") or "-").upper()
            execution = (r.get("execution") or "-").upper()
            three = f"[{outcome}|{alignment}|{execution}]"
            pct = f"({r['pnl_pct_of_cost']}%)" if r.get("pnl_pct_of_cost") is not None else ""
            llm_c = llm_by_idx.get(f"C{i}")
            llm_part = f" — *{llm_c['verdict']}*: {llm_c.get('tag', '')}" if llm_c else ""
            lines.append(
                f"- {three} **{r['ts_code']}** {r['direction']} ×{int(r['volume'])} → "
                f"PnL {_fmt_money(r['realized_pnl'])} {pct}{llm_part}"
            )

    opens = trade_reviews.get("open", [])
    if opens:
        lines.append("### 未平仓笔")
        for i, r in enumerate(opens):
            outcome = (r.get("outcome") or "-").upper()
            alignment = (r.get("alignment") or "-").upper()
            execution = (r.get("execution") or "-").upper()
            three = f"[{outcome}|{alignment}|{execution}]"
            pct = f"({r['unrealized_pct_of_cost']}%)" if r.get("unrealized_pct_of_cost") is not None else ""
            hold = f"持仓{r['hold_days']}天" if r.get("hold_days") is not None else ""
            llm_c = llm_by_idx.get(f"O{i}")
            llm_part = f" — *{llm_c['verdict']}*: {llm_c.get('tag', '')}" if llm_c else ""
            lines.append(
                f"- {three} **{r['ts_code']}** {r['direction']} ×{int(r['volume'])} @ "
                f"{r.get('cost_price')} → mark {r.get('mark_price')}, "
                f"浮 {_fmt_money(r['unrealized_pnl'])} {pct} {hold}{llm_part}"
            )

    if not closed and not opens:
        lines.append("- (无交易)")
    lines.append("")

    # ---- 4. 错误模式 ----
    lines.append("## 四、命中错误模式")
    patterns_used = trade_reviews.get("summary", {})
    has_meta = patterns_used.get("n_against_basis", 0) or patterns_used.get("n_against_trend", 0)
    if has_meta:
        lines.append(
            f"- 逆基差笔数 {patterns_used.get('n_against_basis', 0)}；"
            f"逆大盘笔数 {patterns_used.get('n_against_trend', 0)}"
        )
    if advice.get("flat"):
        # 把 patterns 触发的 advice 单独罗列
        for entry in advice["flat"]:
            tag = entry.get("tag")
            if tag in ("single_trade_risk", "trade_lesson", "llm_extra"):
                continue
            reason = entry.get("reason") or ""
            lines.append(f"- **{tag}**: {reason}")
    if not has_meta and not any(
        e.get("tag") not in ("single_trade_risk", "trade_lesson", "llm_extra")
        for e in advice.get("flat") or []
    ):
        lines.append("- 无显著错误模式")
    lines.append("")

    # ---- 5. 决策建议 ----
    lines.append("## 五、决策建议")

    def _emit_bucket(title: str, items: list[dict]) -> None:
        if not items:
            return
        lines.append(f"### {title}")
        for a in items:
            target = f" `{a['target_ts_code']}`" if a.get("target_ts_code") else ""
            prio = f"[{a.get('priority', 'medium').upper()}]"
            lines.append(f"- {prio}{target} **{a.get('action')}**")
            if a.get("reason"):
                lines.append(f"  - 依据: {a['reason']}")
            if a.get("trigger"):
                lines.append(f"  - 触发: {a['trigger']}")
            if a.get("expected_impact"):
                lines.append(f"  - 预期: {a['expected_impact']}")

    _emit_bucket("立即执行 (今日/明早盘前)", advice.get("actions_now", []))
    _emit_bucket("下一交易日操作", advice.get("actions_next_session", []))
    _emit_bucket("流程改进", advice.get("process_changes", []))

    if not any(advice.get(b) for b in ("actions_now", "actions_next_session", "process_changes")):
        lines.append("- 无显著建议（数据不足或表现稳健）")
    lines.append("")

    # ---- 6. LLM 综合复盘 ----
    if llm_out.get("decision_narrative_md"):
        lines.append("## 六、综合复盘 (LLM)")
        lines.append(llm_out["decision_narrative_md"])
        lines.append("")

    if llm_out.get("insights"):
        lines.append("## 七、深层洞察 (LLM)")
        for ins in llm_out["insights"]:
            conf = ins.get("confidence", "-")
            act = ins.get("actionability", "-")
            lines.append(f"- **{ins['hypothesis']}** (置信 {conf}, 可执行性 {act})")
        lines.append("")

    return "\n".join(lines).strip()


# --- 主入口 ------------------------------------------------------

def run(
    trades: Any,
    context: Any = None,
    mode: str = "daily",
    period: dict | None = None,
    config: dict | None = None,
) -> pd.DataFrame:
    config = config or {}
    update_time = config.get("update_time", datetime.now().isoformat(timespec="seconds"))
    data_version = config.get("data_version", "real-v1")
    include_open_wr = bool(config.get("include_open_in_winrate", False))

    df, warnings = validate_and_split(trades)

    # period 推断（在拉 context 前，因 context_loader 需要它）
    if period is None:
        dates = sorted(df["trade_date"].astype(str).unique())
        period = {"start": dates[0], "end": dates[-1], "trade_days": len(dates)}
    else:
        period = dict(period)
        if "trade_days" not in period:
            period["trade_days"] = len(sorted(df["trade_date"].astype(str).unique()))

    # 注入 mark_price 拉取器（默认用 panda_data；上游可在 config 提供自定义）
    mark_fetcher = config.get("fetch_mark_price")
    if mark_fetcher is None and not config.get("disable_panda_lookup"):
        try:
            mark_fetcher = make_mark_price_fetcher()
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"mark_price fetcher 初始化失败: {exc}")
            mark_fetcher = None

    df = fill_missing_mark_and_unrealized(df, warnings, fetch_mark_price=mark_fetcher)

    # 拉市场环境（除非显式传入）
    if context is None:
        if config.get("disable_context_fetch"):
            context = {"stock": None, "futures": {}, "regime_label": None, "fetch_failures": ["context fetch disabled"]}
        else:
            try:
                context = load_context(df, period)
            except Exception as exc:  # noqa: BLE001
                context = {"stock": None, "futures": {}, "regime_label": None, "fetch_failures": [f"load_context 异常: {exc}"]}
    if context.get("fetch_failures"):
        warnings.extend(context["fetch_failures"])

    summary = _compute_summary(df, include_open_in_winrate=include_open_wr)
    attribution = _compute_attribution(df)
    patterns = detect_patterns(df, summary, attribution, context)

    market_review = build_market_review(context)
    trade_reviews = review_all_trades(df, market_review)

    advice = generate_advice(patterns, trade_reviews=trade_reviews,
                             market_review=market_review)

    llm_out = call_llm(summary, attribution, patterns, context,
                       market_review, trade_reviews, advice, config)

    # ---- 策略意图层（可选，开关式） ----
    strategy_doc = None
    strategy_review_out = None
    strategy_path = (config.get("strategy_path")
                     or (config.get("strategy_review") or {}).get("path"))
    if strategy_path:
        try:
            strategy_doc = load_strategy(strategy_path)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"加载策略文件失败: {exc}")
            strategy_doc = None

        if strategy_doc is not None and not strategy_doc.is_empty():
            strategy_review_out = call_strategy_review(
                strategy_doc, summary, attribution, market_review,
                trade_reviews.get("summary", {}), context, config,
            )
            # 把策略层 decision_advice 合并入 advice（标 tag=strategy_intent）
            if strategy_review_out and strategy_review_out.get("decision_advice"):
                for a in strategy_review_out["decision_advice"]:
                    entry = {
                        "priority": a.get("priority", "medium"),
                        "bucket": "actions_next_session",
                        "target_ts_code": None,
                        "action": f"[{a.get('action', '-').upper()}] {a.get('reason')}",
                        "reason": a.get("reason"),
                        "trigger": a.get("trigger"),
                        "expected_impact": "策略意图层建议",
                        "tag": "strategy_intent",
                    }
                    advice["actions_next_session"].append(entry)
                    advice["flat"].append(entry)

    asset_mix_pnl = {k: abs(v["realized"]) + abs(v["unrealized"])
                     for k, v in attribution["by_asset_type"].items()}
    total_abs = sum(asset_mix_pnl.values()) or 1.0
    asset_mix = {k: v / total_abs for k, v in asset_mix_pnl.items()}

    score = _compute_score(summary, asset_mix, context)
    rating, rating_warnings = _rate(score, summary["n_closed"])
    warnings.extend(rating_warnings)

    # 如果 LLM 给了额外建议，合并入 advice
    if llm_out.get("extra_actions"):
        for a in llm_out["extra_actions"]:
            entry = {
                "priority": a.get("priority", "medium"),
                "bucket": "actions_next_session",
                "target_ts_code": None,
                "action": a.get("action"),
                "reason": a.get("reason"),
                "trigger": a.get("trigger"),
                "expected_impact": "LLM 增强建议",
                "tag": "llm_extra",
            }
            advice["actions_next_session"].append(entry)
            advice["flat"].append(entry)

    # 如果 market 段给了 trigger_long / trigger_short，注入对应 advice 条目
    market_tl = llm_out.get("market_trigger_long")
    market_ts = llm_out.get("market_trigger_short")
    if market_tl:
        entry = {
            "priority": "medium",
            "bucket": "actions_next_session",
            "target_ts_code": None,
            "action": "市场偏多触发：满足条件后逐步加多 / 减空",
            "reason": "由技术形态 + 关键价位综合得出",
            "trigger": market_tl,
            "expected_impact": "顺势捕捉反弹/突破",
            "tag": "market_trigger_long",
        }
        advice["actions_next_session"].append(entry)
        advice["flat"].append(entry)
    if market_ts:
        entry = {
            "priority": "medium",
            "bucket": "actions_next_session",
            "target_ts_code": None,
            "action": "市场偏空触发：满足条件后逐步加空 / 减多",
            "reason": "由技术形态 + 关键价位综合得出",
            "trigger": market_ts,
            "expected_impact": "顺势捕捉破位/下行",
            "tag": "market_trigger_short",
        }
        advice["actions_next_session"].append(entry)
        advice["flat"].append(entry)

    narrative_md = _compose_narrative_md(summary, market_review, trade_reviews,
                                         advice, llm_out, strategy_review_out)

    result_json = {
        "period": period,
        "summary": summary,
        "attribution": attribution,
        "market_review": market_review,
        "trade_reviews": trade_reviews,
        "patterns": patterns,
        "advice": advice,
        "narrative_md": narrative_md,
        "narrative": llm_out.get("decision_narrative_md"),
        "insights": llm_out.get("insights"),
        "strategy_review": strategy_review_out,
        "strategy_doc": strategy_doc.to_dict() if strategy_doc else None,
        "warnings": warnings,
        "score": score,
        "regime_label": context.get("regime_label") if context else None,
        "llm_meta": llm_out.get("llm_meta"),
    }

    target_id = str(df["account_id"].iloc[0]) if not df.empty else "default"
    result_type = "daily_review" if mode == "daily" else "range_review"
    trade_date = period["end"]

    return pd.DataFrame([{
        "trade_date": trade_date,
        "build_id": BUILD_ID,
        "build_name": BUILD_NAME,
        "target_id": target_id,
        "result_type": result_type,
        "result_value": rating,
        "result_json": json.dumps(result_json, ensure_ascii=False),
        "data_version": data_version,
        "update_time": update_time,
    }])


def _read_trades_csv(path: str) -> list[dict]:
    df = pd.read_csv(path)
    return df.to_dict(orient="records")


def _cli():
    import argparse

    parser = argparse.ArgumentParser(description="R00 交易复盘")
    parser.add_argument("--trades", required=True, help="trades CSV 路径")
    parser.add_argument("--mode", choices=["daily", "range"], default="daily")
    parser.add_argument("--start", help="区间开始 YYYY-MM-DD")
    parser.add_argument("--end", help="区间结束 YYYY-MM-DD")
    parser.add_argument("--no-llm", action="store_true", help="禁用 LLM")
    parser.add_argument("--no-context", action="store_true", help="禁用 panda_data context 拉取")
    parser.add_argument("--out", help="输出 parquet 路径，缺省打印到 stdout")
    parser.add_argument("--md", action="store_true", help="只打印 narrative_md（人类可读报告）")
    args = parser.parse_args()

    trades = _read_trades_csv(args.trades)
    period = None
    if args.start and args.end:
        period = {"start": args.start, "end": args.end}
    config = {"llm": {"enabled": not args.no_llm}}
    if args.no_context:
        config["disable_context_fetch"] = True
    out = run(trades, mode=args.mode, period=period, config=config)
    if args.out:
        out.to_parquet(args.out)
        print(f"saved to {args.out}, rows={len(out)}")
    elif args.md:
        sys.stdout.reconfigure(encoding="utf-8")
        parsed = json.loads(out["result_json"].iloc[0])
        print(parsed.get("narrative_md", ""))
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        print(out.to_string(index=False))


if __name__ == "__main__":
    if len(sys.argv) > 1:
        _cli()
    else:
        # 自检
        sample = [
            {"trade_date": "2026-06-04", "ts_code": "RB2610.SHFE", "direction": "long",
             "status": "closed", "volume": 5, "realized_pnl": 1200.0, "reason_tag": "breakout"},
            {"trade_date": "2026-06-04", "ts_code": "IF2606.CFE", "direction": "short",
             "status": "closed", "volume": 1, "realized_pnl": -800.0, "reason_tag": "mean_reversion"},
            {"trade_date": "2026-06-04", "ts_code": "600519.SH", "direction": "long",
             "status": "open", "volume": 100, "cost_price": 1700.0, "mark_price": 1685.0,
             "reason_tag": "value"},
        ]
        out = run(sample, mode="daily",
                  config={"llm": {"enabled": False}, "disable_context_fetch": True})
        sys.stdout.reconfigure(encoding="utf-8")
        parsed = json.loads(out["result_json"].iloc[0])
        print(f"rating={out['result_value'].iloc[0]} score={parsed['score']}")
        print(f"summary={parsed['summary']}")
        print(f"patterns={parsed['patterns']}")
        print(f"advice={parsed['advice']}")
        print(f"warnings={parsed['warnings']}")
