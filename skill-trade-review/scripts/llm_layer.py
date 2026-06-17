"""LLM 复盘层 — 三段式生成: 市场研判 / 逐笔点评 / 决策综合。

硬性原则:
1. LLM 不接触原始 trades 字段，只看 summary/attribution/patterns/market_review/trade_reviews 摘要。
2. 输出永远不回流去修改任何数值字段。
3. 默认走 opus-4-7 (走 llmx.tqx.ai 网关)。
4. 单次复盘 LLM 调用 ≤6 次，输入 ≤8000 chars/次，输出 ≤1500 tokens/次。
5. 失败立即降级 None，不重试。
6. 缓存：每段独立 cache key，24h 内同输入命中。
7. 审计日志写文件。
8. evidence_keys 必须能在 attribution / context / market_review / trade_reviews 中实际找到。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

DEFAULT_MODEL = "opus-4-7"
MAX_INPUT_CHARS = 32000
MAX_INPUT_CHARS_MARKET = 80000  # 市场段含技术指标，输入更长
MAX_OUTPUT_TOKENS = 1500
MAX_OUTPUT_TOKENS_MARKET = 3000  # 5 小节 + 关键价位列表
MAX_OUTPUT_TOKENS_TRADES = 4096  # 逐笔点评段落更需要长输出
CACHE_TTL_SECONDS = 24 * 3600
MAX_LLM_CALLS = 6  # 单次复盘上限
LOG_FILE = Path.home() / ".claude" / "skills" / "build-r00-trade-review" / ".llm_audit.log"
CACHE_FILE = Path.home() / ".claude" / "skills" / "build-r00-trade-review" / ".llm_cache.json"


# ---------- 缓存 / 审计 ----------

def _cache_key(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _read_cache(key: str) -> dict | None:
    if not CACHE_FILE.exists():
        return None
    try:
        store = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    entry = store.get(key)
    if not entry:
        return None
    if (time.time() - entry.get("ts", 0)) > CACHE_TTL_SECONDS:
        return None
    return entry.get("value")


def _write_cache(key: str, value: dict) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        store = json.loads(CACHE_FILE.read_text(encoding="utf-8")) if CACHE_FILE.exists() else {}
    except Exception:
        store = {}
    store[key] = {"ts": time.time(), "value": value}
    if len(store) > 200:
        items = sorted(store.items(), key=lambda kv: kv[1].get("ts", 0))
        store = dict(items[-200:])
    CACHE_FILE.write_text(json.dumps(store, ensure_ascii=False), encoding="utf-8")


def _audit(record: dict) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    record["ts"] = time.time()
    with LOG_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------- evidence_keys 校验 ----------

def _deep_get(obj: Any, path: str) -> bool:
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return False
        else:
            return False
    return True


def _validate_evidence(keys: list[str], haystack: dict) -> bool:
    if not keys:
        return False
    for k in keys:
        if not isinstance(k, str):
            return False
        prefixed = "." in k and k.split(".", 1)[0] in haystack
        if prefixed:
            if not _deep_get(haystack, k):
                return False
        else:
            # 否则在任一子树找到即可
            if not any(_deep_get(v, k) for v in haystack.values() if isinstance(v, dict)):
                return False
    return True


# ---------- 通用 LLM 调用 ----------

def _check_sdk_and_key(cfg: dict) -> tuple[Any, str | None, str | None, str | None]:
    """返回 (Anthropic class, api_key, base_url, error)"""
    try:
        from anthropic import Anthropic
    except ImportError:
        return None, None, None, "anthropic SDK 未安装"
    api_key = cfg.get("api_key") or os.getenv("ANTHROPIC_API_KEY")
    base_url = cfg.get("base_url") or os.getenv("ANTHROPIC_BASE_URL")
    if not api_key:
        return Anthropic, None, base_url, "ANTHROPIC_API_KEY 未设置"
    return Anthropic, api_key, base_url, None


def _invoke(client, model: str, system: str, tools: list, user: str,
            tool_name: str, max_tokens: int = MAX_OUTPUT_TOKENS) -> tuple[dict | None, dict, str | None]:
    """单次 LLM 调用，返回 (tool_input, meta, error)。"""
    started = time.time()
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            tools=tools,
            tool_choice={"type": "tool", "name": tool_name},
            messages=[{"role": "user", "content": user}],
            timeout=60,
        )
    except Exception as exc:  # noqa: BLE001
        latency = int((time.time() - started) * 1000)
        return None, {"latency_ms": latency, "tokens": None}, f"call_failed: {exc}"

    latency = int((time.time() - started) * 1000)
    tool_use = next((b for b in resp.content if getattr(b, "type", None) == "tool_use"), None)
    if tool_use is None:
        return None, {"latency_ms": latency, "tokens": None}, "no_tool_use_in_response"

    usage = getattr(resp, "usage", None)
    tokens = ({"input": getattr(usage, "input_tokens", None),
               "output": getattr(usage, "output_tokens", None)} if usage else None)
    return (tool_use.input or {}), {"latency_ms": latency, "tokens": tokens}, None


# ---------- 三段 Prompt ----------

MARKET_SYSTEM = """你是量化交易复盘的市场研判 + 技术分析模块。基于结构化的 market_review（含 technical_daily/technical_weekly/technical_15min/key_levels/pattern），输出：
1) 市场环境综合判断（基本面 + 资金面）
2) **技术形态判断（必须明确说出形态名称及成立理由）**
3) **关键价位（支撑/阻力 + MA20/MA60 + 前高/前低 + 布林上下轨 + ATR 区间）**
4) **指标动态（MACD 金叉/死叉/背离、RSI 超买超卖、均线排列、量价配合）**
5) 短期方向预期 + 操作建议（3 个层级：偏多触发条件 / 偏空触发条件 / 中性观望区间）

约束：
- 只能用 emit_market_review 工具返回。
- 不得编造数据；narrative 中提到的所有价位必须在 market_review.*.key_levels 里能定位到，所有指标值必须在 technical_daily/weekly/15min 里能找到。
- evidence_keys 必须使用从顶层字段开始的完整点分路径，例如 `market_review.stock.technical_daily.macd.cross`、`market_review.futures.IM.key_levels.nearest_support`、`market_review.futures.IM.pattern`。裸字段名（如 `pattern`、`ma20`）会被判定为无效证据。
- key_levels_used 字段输出本次叙述里实际引用的关键价位数值（带名称），用于下游 advice trigger 联动。
- narrative_md 用 markdown，包含 「### 市场环境」「### 技术形态」「### 关键价位」「### 指标动态」「### 短期展望」 五个小节，总长 ≤ 700 字。
"""

MARKET_TOOL = {
    "name": "emit_market_review",
    "description": "输出市场研判 + 技术分析段落",
    "input_schema": {
        "type": "object",
        "properties": {
            "narrative_md": {"type": "string"},
            "evidence_keys": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 12},
            "directional_view": {"type": "string", "enum": ["bullish", "bearish", "neutral", "high_vol_caution"]},
            "pattern_label": {"type": "string", "description": "形态最终标签，如 uptrend_continuation/breakdown_low/ranging 等"},
            "key_levels_used": {
                "type": "array",
                "description": "narrative 里引用的关键价位，每条 {name, value, role}",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "value": {"type": "number"},
                        "role": {"type": "string", "enum": ["support", "resistance", "trigger_long", "trigger_short", "neutral_zone"]},
                    },
                    "required": ["name", "value", "role"],
                },
                "maxItems": 8,
            },
            "trigger_long": {"type": "string", "description": "偏多触发条件，必须包含具体价位"},
            "trigger_short": {"type": "string", "description": "偏空触发条件，必须包含具体价位"},
        },
        "required": ["narrative_md", "evidence_keys", "directional_view"],
    },
}

TRADES_SYSTEM = """你是量化交易复盘的逐笔点评模块。每笔交易已被规则层打了三维度独立标签：
- outcome: big_win / win / breakeven / loss / big_loss   纯结果（按当日波动率归一化）
- alignment: aligned / mixed / against                   方向是否合规市场
- execution: clean / questionable / poor                 执行质量（是否硬扛/超时）

**点评规则（必须遵守）**：
1. **逆势赚钱不算错**：alignment=against + outcome∈{win, big_win} 应给 verdict=acceptable 或 correct，标签可写「短线反转」「兑现及时」「侥幸但执行干净」等，不应写「逆势硬扛」。
2. **顺势小亏属正常**：alignment=aligned + outcome=loss + execution=clean 应给 verdict=acceptable，标签写「顺势小亏」「合理交易成本」，不要写"顺势却亏"贬低它。
3. **真正错的是 execution=poor 的**：硬扛、超时、追单后不止损 —— 这些才是 wrong / risky。
4. **outcome=big_loss + alignment=against + execution=poor** 是最严重组合，verdict=wrong + 标签写「逆势硬扛」。

约束：
- 只能用 emit_trade_reviews 工具返回。
- **必须为每笔输出一条 review，idx 与输入完全一一对应**。
- 字段：idx、verdict（correct/acceptable/wrong/risky）、tag（≤8 字短标签）、evidence_keys（≥1 条）。
- evidence_keys 必须是从顶层字段开始的完整点分路径，例如 `trade_reviews.closed.0.outcome`、`trade_reviews.closed.0.alignment`、`market_review.overall_regime`。禁止裸字段名。
"""

TRADES_TOOL = {
    "name": "emit_trade_reviews",
    "description": "对每笔交易给出短标签",
    "input_schema": {
        "type": "object",
        "properties": {
            "reviews": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "idx": {"type": "string", "description": "如 C0/C1.../O0/O1..."},
                        "verdict": {"type": "string", "enum": ["correct", "acceptable", "wrong", "risky"]},
                        "tag": {"type": "string", "description": "≤8 字短标签"},
                        "evidence_keys": {"type": "array", "items": {"type": "string"},
                                          "minItems": 1, "maxItems": 3},
                    },
                    "required": ["idx", "verdict", "tag", "evidence_keys"],
                },
            },
        },
        "required": ["reviews"],
    },
}

DECISION_SYSTEM = """你是量化交易复盘的决策综合模块。整合 patterns、market_review、per_trade summary、规则建议，输出 1) 一段中文综合复盘 2) 1-3 条深度 insights 3) 0-3 条 LLM 增强 actions。
约束：
- 只能用 emit_decision 工具返回。
- narrative_md ≤ 300 字, markdown 格式，含 ### 复盘综述 / ### 关键失误 / ### 下一步行动 三段。
- 每条 insight / extra_action 都必须有 ≥1 evidence_key，且使用从顶层字段开始的完整点分路径。合法示例：`summary.win_rate_closed`、`attribution.by_direction.long.realized`、`market_review.overall_regime`、`trade_reviews.summary.n_against_basis`。裸字段名（如 `win_rate_closed`）会被判定为无效证据。
- LLM actions 不允许给具体止损价位，只能给方向 + 触发条件 + 理由。
"""

DECISION_TOOL = {
    "name": "emit_decision",
    "description": "输出综合复盘 narrative + insights + 增强 actions",
    "input_schema": {
        "type": "object",
        "properties": {
            "narrative_md": {"type": "string"},
            "insights": {
                "type": "array",
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "properties": {
                        "hypothesis": {"type": "string"},
                        "evidence_keys": {"type": "array", "items": {"type": "string"},
                                          "minItems": 1, "maxItems": 4},
                        "confidence": {"type": "number"},
                        "actionability": {"type": "string", "enum": ["high", "medium", "low"]},
                    },
                    "required": ["hypothesis", "evidence_keys", "confidence"],
                },
            },
            "extra_actions": {
                "type": "array",
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string"},
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
        "required": ["narrative_md"],
    },
}


# ---------- 内容裁剪 ----------

def _truncate(text: str, limit: int = MAX_INPUT_CHARS) -> str:
    if len(text) > limit:
        return text[:limit] + "...<truncated>"
    return text


def _market_user(market_review: dict) -> str:
    """构造市场研判 prompt — 把技术指标 + 关键价位一并喂给 LLM。"""
    payload = {
        "market_review": {
            "stock": market_review.get("stock"),
            "futures": market_review.get("futures"),
            "overall_regime": market_review.get("overall_regime"),
        },
    }
    return ("以下是已计算好的市场环境与技术指标数据，请结合这些数据输出 5 个小节的研判（市场环境 / 技术形态 / 关键价位 / 指标动态 / 短期展望）。"
            "narrative 里提到的每个价位必须能在 key_levels 中找到，每个指标值必须能在 technical_daily/weekly/15min 中找到。\n"
            "evidence_keys 使用 `market_review.` 前缀的完整路径：\n\n"
            f"{json.dumps(payload, ensure_ascii=False)}")


def _trades_user(trade_reviews: dict, market_review: dict) -> str:
    """构造逐笔点评 prompt — 给每笔加 idx 编号与三维度标签。"""
    closed = trade_reviews.get("closed", [])
    opens = trade_reviews.get("open", [])

    def _slim_closed(items):
        out = []
        for i, r in enumerate(items):
            out.append({
                "idx": f"C{i}",
                "direction": r.get("direction"),
                "outcome": r.get("outcome"),
                "alignment": r.get("alignment"),
                "execution": r.get("execution"),
                "pnl_pct_of_cost": r.get("pnl_pct_of_cost"),
                "std_move_pct": r.get("std_move_pct"),
                "reason_tag": r.get("reason_tag"),
            })
        return out

    def _slim_open(items):
        out = []
        for i, r in enumerate(items):
            out.append({
                "idx": f"O{i}",
                "direction": r.get("direction"),
                "outcome": r.get("outcome"),
                "alignment": r.get("alignment"),
                "execution": r.get("execution"),
                "unrealized_pct_of_cost": r.get("unrealized_pct_of_cost"),
                "std_move_pct": r.get("std_move_pct"),
                "hold_days": r.get("hold_days"),
                "reason_tag": r.get("reason_tag"),
            })
        return out

    payload = {
        "market_review": {
            "overall_regime": market_review.get("overall_regime"),
            "stock": {"trend": (market_review.get("stock") or {}).get("trend"),
                      "volatility": (market_review.get("stock") or {}).get("volatility")},
            "futures": {v: {"trend": f.get("trend"),
                            "basis_regime": f.get("basis_regime")}
                        for v, f in (market_review.get("futures") or {}).items()},
        },
        "trade_reviews": {
            "summary": trade_reviews.get("summary", {}),
            "closed": _slim_closed(closed),
            "open": _slim_open(opens),
        },
    }
    nc, no = len(closed), len(opens)
    return ("以下是已编号的逐笔交易数据，每笔附带三维度标签 (outcome/alignment/execution)。\n"
            "请综合三维度判断 verdict，**不要因 alignment=against 就判 wrong，也不要因 alignment=aligned 就判 correct**。\n"
            f"输出 idx 必须与输入完全一一对应（共 {nc} 笔 closed: C0~C{max(nc-1,0)}, "
            f"{no} 笔 open: O0~O{max(no-1,0)}），不能遗漏。\n"
            "evidence_keys 使用形如 `trade_reviews.closed.0.outcome` 的完整路径：\n\n"
            f"{json.dumps(payload, ensure_ascii=False)}")


def _decision_user(summary, attribution, patterns, market_review, trade_summary, advice) -> str:
    payload = {
        "summary": summary,
        "attribution": attribution,
        "patterns": [{"tag": p["tag"], "pattern": p["pattern"], "evidence": p["evidence"]} for p in patterns],
        "market_review": {
            "overall_regime": market_review.get("overall_regime"),
            "stock": market_review.get("stock"),
            "futures": market_review.get("futures"),
        },
        "trade_reviews": {"summary": trade_summary},
        "advice": {
            "actions_now": [a["action"] for a in advice.get("actions_now", [])],
            "actions_next_session": [a["action"] for a in advice.get("actions_next_session", [])],
            "process_changes": [a["action"] for a in advice.get("process_changes", [])],
        },
    }
    return ("以下是已计算好的复盘数据。"
            "evidence_keys 必须使用 `summary.*` / `attribution.*` / `market_review.*` / `trade_reviews.*` 等完整路径：\n\n"
            f"{json.dumps(payload, ensure_ascii=False)}")


# ---------- 主入口 ----------

def call_llm(summary: dict, attribution: dict, patterns: list[dict],
             context: dict | None, market_review: dict, trade_reviews: dict,
             advice: dict, config: dict | None = None) -> dict:
    """三段式 LLM 调用。

    返回 dict:
        {
            "market_narrative_md": str | None,
            "market_directional_view": str | None,
            "trade_review_comments": list[dict] | None,
            "decision_narrative_md": str | None,
            "insights": list[dict] | None,
            "extra_actions": list[dict] | None,
            "llm_meta": {...},
        }
    """
    cfg = (config or {}).get("llm", {}) if config else {}
    enabled = cfg.get("enabled", True)
    if os.getenv("REVIEW_LLM_DISABLED", "0").lower() in ("1", "true", "yes"):
        enabled = False

    empty_meta = {"enabled": enabled, "model": None, "calls": 0, "cache_hits": 0,
                  "skipped_reason": "disabled" if not enabled else None,
                  "segment_status": {}, "tokens_total": None, "latency_ms_total": 0}

    if not enabled:
        return {
            "market_narrative_md": None, "market_directional_view": None,
            "market_key_levels_used": None, "market_trigger_long": None,
            "market_trigger_short": None,
            "trade_review_comments": None,
            "decision_narrative_md": None, "insights": None, "extra_actions": None,
            "llm_meta": empty_meta,
        }

    Anthropic, api_key, base_url, err = _check_sdk_and_key(cfg)
    if err:
        empty_meta["skipped_reason"] = err
        return {
            "market_narrative_md": None, "market_directional_view": None,
            "market_key_levels_used": None, "market_trigger_long": None,
            "market_trigger_short": None,
            "trade_review_comments": None,
            "decision_narrative_md": None, "insights": None, "extra_actions": None,
            "llm_meta": empty_meta,
        }

    model = cfg.get("model", DEFAULT_MODEL)
    client = Anthropic(api_key=api_key, base_url=base_url) if base_url else Anthropic(api_key=api_key)

    haystack_for_validation = {
        "summary": summary,
        "attribution": attribution,
        "context": context or {},
        "market_review": market_review,
        "trade_reviews": trade_reviews,
    }

    # 段 1: 市场研判 + 技术分析
    market_md, market_view, market_klu, market_tl, market_ts, m_meta = _segment_market(
        client, model, market_review, haystack_for_validation)
    # 段 2: 逐笔点评
    trade_comments, t_meta = _segment_trades(client, model, trade_reviews, market_review,
                                             haystack_for_validation)
    # 段 3: 决策综合
    dec_md, insights, extra_actions, d_meta = _segment_decision(
        client, model, summary, attribution, patterns, market_review,
        trade_reviews.get("summary", {}), advice, haystack_for_validation)

    calls = m_meta["calls"] + t_meta["calls"] + d_meta["calls"]
    cache_hits = m_meta["cache_hits"] + t_meta["cache_hits"] + d_meta["cache_hits"]
    latency_total = m_meta["latency_ms"] + t_meta["latency_ms"] + d_meta["latency_ms"]

    llm_meta = {
        "enabled": True,
        "model": model,
        "calls": calls,
        "cache_hits": cache_hits,
        "max_calls": MAX_LLM_CALLS,
        "latency_ms_total": latency_total,
        "segment_status": {
            "market": m_meta["status"],
            "trades": t_meta["status"],
            "decision": d_meta["status"],
        },
    }

    return {
        "market_narrative_md": market_md,
        "market_directional_view": market_view,
        "market_key_levels_used": market_klu,
        "market_trigger_long": market_tl,
        "market_trigger_short": market_ts,
        "trade_review_comments": trade_comments,
        "decision_narrative_md": dec_md,
        "insights": insights,
        "extra_actions": extra_actions,
        "llm_meta": llm_meta,
    }


# ---------- 段实现 ----------

def _segment_market(client, model, market_review, haystack):
    meta = {"calls": 0, "cache_hits": 0, "latency_ms": 0, "status": "skipped"}
    if not (market_review.get("stock") or market_review.get("futures")):
        meta["status"] = "no_data"
        return None, None, None, None, None, meta

    payload = {"segment": "market_v2", "model": model, "market_review": market_review}
    ck = _cache_key(payload)
    cached = _read_cache(ck)
    if cached:
        meta["cache_hits"] = 1
        meta["status"] = "cache_hit"
        return (cached.get("narrative_md"), cached.get("directional_view"),
                cached.get("key_levels_used"), cached.get("trigger_long"),
                cached.get("trigger_short"), meta)

    user = _truncate(_market_user(market_review), MAX_INPUT_CHARS_MARKET)
    args, m, err = _invoke(client, model, MARKET_SYSTEM, [MARKET_TOOL], user,
                           "emit_market_review", max_tokens=MAX_OUTPUT_TOKENS_MARKET)
    meta["calls"] = 1
    meta["latency_ms"] = m["latency_ms"]
    if err:
        _audit({"segment": "market", "error": err, "model": model})
        meta["status"] = f"failed:{err}"
        return None, None, None, None, None, meta

    md = args.get("narrative_md")
    view = args.get("directional_view")
    keys = args.get("evidence_keys") or []
    if not _validate_evidence(keys, haystack):
        _audit({"segment": "market", "error": "evidence_invalid", "keys": keys})
        meta["status"] = "evidence_invalid"
        return None, None, None, None, None, meta

    klu = args.get("key_levels_used") or []
    tl = args.get("trigger_long")
    ts = args.get("trigger_short")

    _write_cache(ck, {"narrative_md": md, "directional_view": view,
                      "key_levels_used": klu, "trigger_long": tl, "trigger_short": ts})
    _audit({"segment": "market", "ok": True, "tokens": m["tokens"],
            "latency_ms": m["latency_ms"], "n_key_levels": len(klu)})
    meta["status"] = "ok"
    return md, view, klu, tl, ts, meta


def _segment_trades(client, model, trade_reviews, market_review, haystack):
    meta = {"calls": 0, "cache_hits": 0, "latency_ms": 0, "status": "skipped"}
    closed = trade_reviews.get("closed", [])
    opens = trade_reviews.get("open", [])
    if not closed and not opens:
        meta["status"] = "no_trades"
        return None, meta

    expected_idx = [f"C{i}" for i in range(len(closed))] + \
                   [f"O{i}" for i in range(len(opens))]

    payload = {"segment": "trades_v2", "model": model,
               "expected_idx": expected_idx,
               "regime": market_review.get("overall_regime"),
               "ratings": [c.get("rating") for c in closed],
               "risks": [o.get("risk_level") for o in opens],
               "against": [(c.get("against_basis"), c.get("against_trend")) for c in closed]}
    ck = _cache_key(payload)
    cached = _read_cache(ck)
    if cached:
        meta["cache_hits"] = 1
        meta["status"] = "cache_hit"
        return cached.get("reviews"), meta

    base_user = _truncate(_trades_user(trade_reviews, market_review))
    collected: dict[str, dict] = {}

    # 自动续跑：最多 4 轮，确保覆盖全部笔
    for round_idx in range(4):
        missing = [i for i in expected_idx if i not in collected]
        if not missing:
            break
        if round_idx == 0:
            user = base_user
        else:
            user = (base_user + f"\n\n上一轮缺失 {len(missing)} 笔，"
                    f"请只补这些 idx：{missing}。每条仍需 idx + verdict + tag + evidence_keys。")
        args, m, err = _invoke(client, model, TRADES_SYSTEM, [TRADES_TOOL], user,
                               "emit_trade_reviews", max_tokens=MAX_OUTPUT_TOKENS_TRADES)
        meta["calls"] += 1
        meta["latency_ms"] += m["latency_ms"]
        if err:
            _audit({"segment": "trades", "round": round_idx, "error": err, "model": model})
            break
        n_returned = len(args.get("reviews") or [])
        for rev in (args.get("reviews") or []):
            idx = rev.get("idx")
            keys = rev.get("evidence_keys") or []
            if not idx or idx in collected:
                continue
            if not _validate_evidence(keys, haystack):
                continue
            collected[idx] = rev
        _audit({"segment": "trades", "round": round_idx, "got": n_returned,
                "kept_total": len(collected), "missing": len(expected_idx) - len(collected),
                "tokens": m["tokens"], "latency_ms": m["latency_ms"]})

    if not collected:
        meta["status"] = "evidence_invalid"
        return None, meta

    valid = [collected[i] for i in expected_idx if i in collected]
    _write_cache(ck, {"reviews": valid})
    meta["status"] = "ok" if len(valid) == len(expected_idx) else "partial"
    _audit({"segment": "trades", "final": meta["status"],
            "n_expected": len(expected_idx), "n_valid": len(valid)})
    return valid, meta


def _segment_decision(client, model, summary, attribution, patterns,
                      market_review, trade_summary, advice, haystack):
    meta = {"calls": 0, "cache_hits": 0, "latency_ms": 0, "status": "skipped"}
    payload = {"segment": "decision", "model": model,
               "summary": summary, "attribution": attribution,
               "patterns": [{"tag": p["tag"]} for p in patterns],
               "market_regime": market_review.get("overall_regime"),
               "trade_summary": trade_summary,
               "rule_actions": [a["tag"] for a in advice.get("flat", []) if "tag" in a]}
    ck = _cache_key(payload)
    cached = _read_cache(ck)
    if cached:
        meta["cache_hits"] = 1
        meta["status"] = "cache_hit"
        return (cached.get("narrative_md"), cached.get("insights"),
                cached.get("extra_actions"), meta)

    user = _truncate(_decision_user(summary, attribution, patterns, market_review,
                                    trade_summary, advice))
    args, m, err = _invoke(client, model, DECISION_SYSTEM, [DECISION_TOOL],
                           user, "emit_decision")
    meta["calls"] = 1
    meta["latency_ms"] = m["latency_ms"]
    if err:
        _audit({"segment": "decision", "error": err, "model": model})
        meta["status"] = f"failed:{err}"
        return None, None, None, meta

    md = args.get("narrative_md")
    raw_insights = args.get("insights") or []
    raw_actions = args.get("extra_actions") or []

    insights = [i for i in raw_insights
                if _validate_evidence(i.get("evidence_keys") or [], haystack)] or None
    actions = [a for a in raw_actions
               if _validate_evidence(a.get("evidence_keys") or [], haystack)] or None

    _write_cache(ck, {"narrative_md": md, "insights": insights, "extra_actions": actions})
    _audit({"segment": "decision", "ok": True,
            "n_insights": len(insights or []), "n_actions": len(actions or []),
            "tokens": m["tokens"], "latency_ms": m["latency_ms"]})
    meta["status"] = "ok"
    return md, insights, actions, meta
