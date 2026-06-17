"""策略意图层 — 给定用户策略 + 市场环境 + 复盘聚合数据，产出综合性分析。

不做逐笔点评，仅产出:
  1. fitness:  当前市场环境 vs 策略适用条件 — fit / partial / unfit + 理由
  2. observations: 关键观察（多条）— 命中策略哪些规则、违反哪些规则
  3. decision_advice: 针对性决策建议（多条）— 继续执行/暂停/调整/止损止盈

LLM 调用受限，单次最多 1 次。失败 / 配置缺失 → 整段降级为 None。
所有 evidence_keys 必须在 haystack（market_review / summary / attribution / strategy）中定位。
"""
from __future__ import annotations

import json
from typing import Any

# 复用 llm_layer 的工具函数
from llm_layer import (
    DEFAULT_MODEL,
    MAX_OUTPUT_TOKENS,
    _audit,
    _cache_key,
    _check_sdk_and_key,
    _invoke,
    _read_cache,
    _truncate,
    _validate_evidence,
    _write_cache,
)

MAX_INPUT_CHARS_STRATEGY = 40000
MAX_OUTPUT_TOKENS_STRATEGY = 4000


SYSTEM_PROMPT = """你是量化交易复盘的策略意图分析模块。用户给出他声明的交易策略，你需要把当前市场环境 + 复盘聚合数据 与 策略 做对照，得出三件事：

1) **fitness**：当前市场环境是否适合该策略
   - fit: 完全契合
   - partial: 部分契合（说明哪部分契合、哪部分不契合）
   - unfit: 明显不适合（说明为什么）

2) **observations**：3-6 条关键观察。每条要求：
   - 引用策略中的某条具体规则（applicable_regimes / entry_rules / exit_rules / risk_rules / key_indicators 之一）
   - 引用市场或聚合数据中的某个具体值（market_review.* / summary.* / attribution.* / strategy.*）
   - 简明指出"命中规则"或"违反规则"

3) **decision_advice**：2-5 条针对该策略的决策建议。每条必须包含：
   - action: 继续/暂停/调整/止盈/止损/换标 之一
   - reason: 一句话理由
   - trigger: 具体触发条件（含价位或指标值）
   - priority: high / medium / low

约束：
- 只能用 emit_strategy_review 工具返回。
- 所有 evidence_keys 必须使用从顶层字段开始的完整路径，例如 `strategy.applicable_regimes.0`、`market_review.overall_regime`、`summary.win_rate_closed`、`attribution.by_direction.long.realized`。裸字段名（如 `overall_regime`）会被判定无效。
- 不得编造策略中没有的规则；如果策略文件信息不足，应在 observations 中说明"策略未声明 X 规则"，而不是凭空补充。
- narrative_md 用 markdown，包含 ### 适配判断 / ### 关键观察 / ### 决策建议 三个小节，总长 ≤ 600 字。
"""

TOOL_DEF = {
    "name": "emit_strategy_review",
    "description": "输出策略适配判断 + 关键观察 + 决策建议",
    "input_schema": {
        "type": "object",
        "properties": {
            "narrative_md": {"type": "string"},
            "fitness": {"type": "string", "enum": ["fit", "partial", "unfit", "unknown"]},
            "fitness_reason": {"type": "string"},
            "observations": {
                "type": "array",
                "minItems": 1,
                "maxItems": 6,
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": ["match", "violation", "warning", "info"]},
                        "rule_ref": {"type": "string", "description": "引用了策略中的哪条规则"},
                        "data_ref": {"type": "string", "description": "引用的市场或复盘数据点"},
                        "comment": {"type": "string"},
                        "evidence_keys": {"type": "array", "items": {"type": "string"},
                                          "minItems": 1, "maxItems": 4},
                    },
                    "required": ["kind", "comment", "evidence_keys"],
                },
            },
            "decision_advice": {
                "type": "array",
                "minItems": 1,
                "maxItems": 5,
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string",
                                   "enum": ["continue", "pause", "adjust", "take_profit", "stop_loss", "switch_target"]},
                        "reason": {"type": "string"},
                        "trigger": {"type": "string"},
                        "priority": {"type": "string", "enum": ["high", "medium", "low"]},
                        "evidence_keys": {"type": "array", "items": {"type": "string"},
                                          "minItems": 1, "maxItems": 4},
                    },
                    "required": ["action", "reason", "trigger", "priority", "evidence_keys"],
                },
            },
        },
        "required": ["narrative_md", "fitness", "observations", "decision_advice"],
    },
}


def _slim_strategy(strategy_doc) -> dict:
    """把 StrategyDoc 转 dict 给 LLM。raw_text 只保留前 4000 字符。"""
    d = strategy_doc.to_dict()
    if d.get("raw_text") and len(d["raw_text"]) > 4000:
        d["raw_text"] = d["raw_text"][:4000] + "...<truncated>"
    return d


def _build_user_prompt(strategy_doc, summary, attribution, market_review,
                       trade_summary) -> str:
    payload = {
        "strategy": _slim_strategy(strategy_doc),
        "market_review": {
            "overall_regime": market_review.get("overall_regime"),
            "stock": market_review.get("stock"),
            "futures": market_review.get("futures"),
        },
        "summary": summary,
        "attribution": attribution,
        "trade_summary": trade_summary,
    }
    return ("以下是用户声明的策略文件 + 当前市场环境 + 复盘聚合数据。"
            "请按 system 中的三步骤产出 fitness / observations / decision_advice。\n"
            "evidence_keys 使用形如 `strategy.entry_rules.0` 或 `market_review.overall_regime` 的完整路径：\n\n"
            f"{json.dumps(payload, ensure_ascii=False, default=str)}")


def call_strategy_review(strategy_doc, summary, attribution, market_review,
                          trade_summary, context, config) -> dict:
    """主入口。

    返回 dict:
      {
        "narrative_md": str | None,
        "fitness": str | None,
        "fitness_reason": str | None,
        "observations": list[dict] | None,
        "decision_advice": list[dict] | None,
        "meta": {...},
      }
    任意失败/禁用 → narrative_md=None, meta.status 反映原因。
    """
    cfg = (config or {}).get("strategy_review", {}) if config else {}
    enabled = cfg.get("enabled", True)
    meta = {"enabled": enabled, "status": "skipped", "calls": 0,
            "cache_hits": 0, "latency_ms": 0, "model": None}
    empty = {"narrative_md": None, "fitness": None, "fitness_reason": None,
             "observations": None, "decision_advice": None, "meta": meta}

    if not enabled:
        meta["status"] = "disabled"
        return empty
    if strategy_doc is None or strategy_doc.is_empty():
        meta["status"] = "no_strategy"
        return empty

    Anthropic, api_key, base_url, err = _check_sdk_and_key((config or {}).get("llm", {}))
    if err:
        meta["status"] = err
        return empty

    model = ((config or {}).get("llm", {}) or {}).get("model", DEFAULT_MODEL)
    meta["model"] = model

    haystack = {
        "summary": summary,
        "attribution": attribution,
        "market_review": market_review,
        "context": context or {},
        "strategy": _slim_strategy(strategy_doc),
        "trade_summary": trade_summary or {},
    }

    payload_for_cache = {
        "segment": "strategy_review_v1",
        "model": model,
        "strategy": _slim_strategy(strategy_doc),
        "regime": market_review.get("overall_regime"),
        "summary": summary,
        "trade_summary": trade_summary or {},
    }
    ck = _cache_key(payload_for_cache)
    cached = _read_cache(ck)
    if cached:
        meta["cache_hits"] = 1
        meta["status"] = "cache_hit"
        return {
            "narrative_md": cached.get("narrative_md"),
            "fitness": cached.get("fitness"),
            "fitness_reason": cached.get("fitness_reason"),
            "observations": cached.get("observations"),
            "decision_advice": cached.get("decision_advice"),
            "meta": meta,
        }

    client = Anthropic(api_key=api_key, base_url=base_url) if base_url else Anthropic(api_key=api_key)
    user = _truncate(_build_user_prompt(strategy_doc, summary, attribution,
                                         market_review, trade_summary),
                     MAX_INPUT_CHARS_STRATEGY)
    args, m, err2 = _invoke(client, model, SYSTEM_PROMPT, [TOOL_DEF], user,
                            "emit_strategy_review", max_tokens=MAX_OUTPUT_TOKENS_STRATEGY)
    meta["calls"] = 1
    meta["latency_ms"] = m["latency_ms"]
    if err2:
        _audit({"segment": "strategy_review", "error": err2, "model": model})
        meta["status"] = f"failed:{err2}"
        return empty

    md = args.get("narrative_md")
    fitness = args.get("fitness")
    fitness_reason = args.get("fitness_reason")
    raw_obs = args.get("observations") or []
    raw_adv = args.get("decision_advice") or []

    obs = [o for o in raw_obs
           if _validate_evidence(o.get("evidence_keys") or [], haystack)]
    adv = [a for a in raw_adv
           if _validate_evidence(a.get("evidence_keys") or [], haystack)]

    if not obs and not adv:
        meta["status"] = "evidence_invalid"
        _audit({"segment": "strategy_review", "status": "evidence_invalid",
                "n_obs": len(raw_obs), "n_adv": len(raw_adv)})
        return empty

    cached_value = {
        "narrative_md": md, "fitness": fitness, "fitness_reason": fitness_reason,
        "observations": obs, "decision_advice": adv,
    }
    _write_cache(ck, cached_value)
    _audit({"segment": "strategy_review", "ok": True,
            "n_obs": len(obs), "n_adv": len(adv),
            "tokens": m["tokens"], "latency_ms": m["latency_ms"]})
    meta["status"] = "ok"
    return {**cached_value, "meta": meta}
