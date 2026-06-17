# LLM 调用边界（R00 交易复盘）

## 硬性原则

> **所有可量化的结论由确定性代码产出（summary/attribution/patterns/advice/score/rating）；LLM 只产出 narrative（自然语言小结）和 insights（深层假设）。LLM 输出永远不会回流去修改任何数值字段。**

## 默认行为

- 默认状态：**开启**（opus-4-7）
- 默认模型：`opus-4-7`（对接 `llmx.tqx.ai` 网关的别名；该网关 distributor 内可用）
- 走当前会话的 `ANTHROPIC_BASE_URL` / `ANTHROPIC_API_KEY` 环境变量
- 调用层可在 `config.llm` 显式覆盖

```python
config = {
    "llm": {
        "enabled": True,
        "model": "opus-4-7",                    # 可切 sonnet-4-6 / haiku-4-6 / deepseek-r1
        "base_url": "https://llmx.tqx.ai",        # 不传则用 ANTHROPIC_BASE_URL
        "api_key": "sk-...",                       # 不传则用 ANTHROPIC_API_KEY
    }
}
run(trades, config=config)
```

关闭 LLM：
```python
config = {"llm": {"enabled": False}}
# 或：set REVIEW_LLM_DISABLED=1
```

## 8 条硬约束

1. **预算上限**：单次复盘 LLM 调用 ≤ 3 次；输入 ≤ 8000 tokens；输出 ≤ 1500 tokens
2. **缓存**：`summary + attribution + patterns` 哈希作 cache key，24 小时内同输入命中缓存（写入 `~/.claude/skills/build-r00-trade-review/.llm_cache.json`）
3. **不看原始 trades**：LLM 接收的是脱敏后的 summary / attribution / patterns / context，不接触具体价格 / 原始下单时间 / 账户号
4. **强制 tool use**：用 `emit_review` 工具强制返回 JSON；纯文本回复一律降级为 null
5. **schema 校验**：narrative 字符串 ≤200 字、insights 至多 3 条、每条必须含 evidence_keys
6. **evidence_keys 校验**：每条 insight 的 `evidence_keys` 必须能在传入的 attribution / context 中实际定位（点分隔路径），否则该 insight 被丢弃
7. **失败立刻降级**：任何异常（网络 / 429 / 解析 / schema）→ narrative=None，insights=None，不重试
8. **审计日志**：每次调用写入 `~/.claude/skills/build-r00-trade-review/.llm_audit.log`，记录 model / tokens / latency_ms / cache_hit

## prompt 结构

```
SYSTEM:
  你是一个量化交易复盘助手...
  约束:
    - 你只能看到结构化的 summary/attribution/patterns 信息
    - 你不能修改任何数值字段
    - narrative ≤ 200 字
    - insights 最多 3 条，每条必须含 evidence_keys
    - 永远使用 emit_review 工具输出

USER:
  以下是已计算好的复盘数据，请基于它产出 narrative 和 insights：
  <脱敏后的 summary + attribution + patterns + context JSON>
```

## tool 定义

```json
{
  "name": "emit_review",
  "input_schema": {
    "type": "object",
    "required": ["narrative", "insights"],
    "properties": {
      "narrative": {"type": "string"},
      "insights": {
        "type": "array",
        "maxItems": 3,
        "items": {
          "type": "object",
          "required": ["hypothesis", "evidence_keys", "confidence"],
          "properties": {
            "hypothesis":     {"type": "string"},
            "evidence_keys":  {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 4},
            "confidence":     {"type": "number"},
            "actionability":  {"type": "string", "enum": ["high","medium","low"]}
          }
        }
      }
    }
  }
}
```

## evidence_keys 校验规则

每条 evidence_key 必须满足：

- 字符串类型
- 用点分隔的路径，如 `attribution.by_variety.RB.realized` 或 `context.stock.index_volatility_5d`
- 路径必须能在 `{"attribution": <attribution>, "context": <context>}` 中实际找到（dict 键 / list 索引）
- 不带前缀也允许，会在 attribution 和 context 中分别尝试

校验失败 → 该 insight 被丢弃，audit log 记 `n_total` 与 `n_valid`。

## llm_meta 字段

每次复盘的 result_json.llm_meta 必填：

| 字段 | 含义 |
|---|---|
| `enabled` | 是否启用 LLM |
| `model` | 实际命中模型；缓存命中时为 cache 标记 |
| `cache_hit` | 是否命中本地缓存 |
| `tokens` | `{"input": int, "output": int}`；缓存或失败时可为 null |
| `latency_ms` | 实际等待毫秒；缓存命中=0 |
| `skipped_reason` | 失败/跳过的原因（disabled / SDK 缺失 / API_KEY 缺失 / 其它） |
| `n_insights_total` / `n_insights_valid` | LLM 返回数 vs 通过 evidence 校验数 |

## 排查 FAQ

- **LLM 跑了但 narrative=null**：看 audit log；常见原因是网关返回非 tool_use 文本，或 schema 不合
- **LLM 没跑**：检查 `llm_meta.skipped_reason`；常见原因是 ANTHROPIC_API_KEY 未设、anthropic SDK 未装、`config.llm.enabled=False`、`REVIEW_LLM_DISABLED=1`
- **insights 全被丢**：模型生成的 evidence_keys 找不到 → 检查 attribution 是否包含 LLM 引用的 key，必要时优化 prompt 让模型只引用确定存在的字段
- **预算超**：调小 trades 量、关闭 context 拉取、或临时切 haiku 而非 sonnet
