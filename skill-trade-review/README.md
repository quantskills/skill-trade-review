# Trade Review

[English](README.en.md)

把交易流水转成一份完整、可核查、可继续加工的复盘结果。  
这个 skill 的默认形态已经调整为：

```text
Python 负责确定性计算
宿主模型负责按原报告结构回填评价内容
```

它不再默认在脚本内部单独调用外部 LLM API。

## 1. 这个 skill 解决什么问题

`trade-review` 用来把一组已经归并好的交易记录变成结构化复盘结果，覆盖：

- 数据校验
- 未平仓补价与浮盈亏计算
- 市场环境提取
- 汇总指标与归因
- 逐笔交易质量评价
- 错误模式识别
- 可执行建议
- 按原报告结构预留的宿主模型回填插槽

适用场景：

- 日度复盘
- 区间复盘
- 策略复盘
- 账户级复盘
- 交易错误模式分析

## 2. 现在的默认架构

```mermaid
flowchart LR
    A["交易明细"] --> B["Python: validate_and_split"]
    B --> C["Python: fill_missing_mark_and_unrealized"]
    C --> D["Python: load_context"]
    D --> E["Python: summary / attribution / patterns / advice"]
    E --> F["Python: narrative_md(规则版) + result_json"]
    F --> G["Host Model"]
    G --> H["按原报告结构回填: 市场研判 / 逐笔点评 / 综合复盘 / 策略适配"]
```

### 核心原则

| 层 | 职责 |
|---|---|
| Python | 做确定性、可回归、可测试的计算 |
| Host Model | 读取 `result_json`，把需要模型判断的内容回填到原报告位置 |

这意味着：

- 没有外部 LLM 凭据时，skill 仍然能产出完整规则版复盘
- 宿主 AI 不应该再写一个“额外总结”
- 宿主 AI 必须把回答填回原报告插槽

## 3. 目录结构

```text
skill-trade-review/
├─ agents/
├─ references/
├─ scripts/
│  ├─ advice.py
│  ├─ asset_classifier.py
│  ├─ context_loader.py
│  ├─ future_context.py
│  ├─ market_review.py
│  ├─ per_trade_review.py
│  ├─ patterns.py
│  ├─ pnl_normalizer.py
│  ├─ review.py
│  ├─ stock_context.py
│  ├─ strategy_doc.py
│  ├─ technical_indicators.py
│  └─ test.py
├─ LICENSE
├─ README.md
├─ README.en.md
├─ requirements.txt
└─ SKILL.md
```

## 4. 输入契约

### 4.1 必需输入

`trades: list[dict]`

交易记录必须已经归并完成。  
这个 skill 不负责从逐笔成交重新匹配 FIFO，也不负责重算已实现 PnL。

### 4.2 字段定义

| 字段 | 类型 | closed | open | 说明 |
|---|---|---|---|---|
| `trade_date` | string | required | required | `YYYY-MM-DD` |
| `trade_time` | string | optional | optional | `HH:MM:SS` |
| `account_id` | string | optional | optional | 账户或策略 ID |
| `ts_code` | string | required | required | 标的代码 |
| `direction` | string | required | required | `long` / `short` |
| `status` | string | required | required | `closed` / `open` |
| `volume` | float | required | required | 数量 |
| `realized_pnl` | float | required | n/a | 已实现净收益 |
| `cost_price` | float | optional | required | 加权开仓成本 |
| `mark_price` | float | optional | optional | 当前价格 |
| `unrealized_pnl` | float | optional | optional | 浮盈亏 |
| `open_date` | string | optional | optional | 开仓日期 |
| `reason_tag` | string | optional | optional | 入场标签 |

### 4.3 校验规则

- `closed` 必须有 `realized_pnl`
- `open` 必须有 `cost_price`
- `direction` 只能是 `long` 或 `short`
- `status` 只能是 `closed` 或 `open`

## 5. 正式行情链路

默认正式链路使用 `panda_data`。

### 必需配置

```bash
set PANDA_DATA_USERNAME=
set PANDA_DATA_PASSWORD=
```

### 说明

| 项 | 说明 |
|---|---|
| 正式行情来源 | `panda_data` |
| 缺少凭据时 | 立即中止，不再静默降级 |
| 未平仓补价 | 可以通过正式行情自动补齐 |
| 结构化市场环境 | 默认从正式行情提取 |

## 6. 输出契约

主函数返回一行 DataFrame，对应生产结构。

| 字段 | 说明 |
|---|---|
| `trade_date` | 复盘日期 |
| `build_id` | `R00` |
| `build_name` | `交易复盘` |
| `target_id` | 账户或策略 ID |
| `result_type` | `daily_review` / `range_review` |
| `result_value` | `excellent` / `good` / `neutral` / `poor` / `bad` |
| `result_json` | 结构化复盘主体 |
| `data_version` | 数据版本 |
| `update_time` | 更新时间 |

### 6.1 `result_json` 主要字段

| 字段 | 说明 |
|---|---|
| `period` | 复盘区间 |
| `summary` | 汇总指标 |
| `attribution` | 归因 |
| `market_review` | 市场环境 |
| `trade_reviews` | 逐笔评价 |
| `patterns` | 错误模式 |
| `advice` | 可执行建议 |
| `narrative_md` | 规则版完整报告 |
| `narrative` | 宿主模型回填的综合复盘段 |
| `insights` | 宿主模型回填的洞察 |
| `strategy_review` | 宿主模型回填的策略适配段 |
| `llm_meta` | 宿主模型 / 外部 LLM 运行状态 |
| `host_handoff` | 宿主接管说明 |
| `host_fill_contract` | 宿主必须遵守的回填契约 |

## 7. 宿主模型回填规则

这是现在最重要的一层。

### 7.1 不允许做什么

- 不允许另起一段“总结式收尾”替代原 LLM 插槽
- 不允许改报告结构
- 不允许把原本应该逐笔点评的位置，挪成报告末尾集中点评
- 不允许改写数值字段

### 7.2 必须回填的位置

| 原位置 | 宿主必须提供的内容 |
|---|---|
| `二、市场行情研判` | 市场叙事、形态判断、关键价位解释、方向触发条件 |
| `三、逐笔交易点评` | 每笔交易的针对性点评 |
| `六、综合复盘` | 综合叙事、关键失误、下一步行动 |
| `二补充：策略适配复盘` | 策略适配判断、关键观察、策略级建议 |

### 7.3 契约字段

`result_json.host_fill_contract`

这个字段明确告诉宿主模型：

- 报告结构不能变
- 哪些原插槽要回填
- 不能追加“单独总结”

## 8. 评分与归因逻辑

### 8.1 summary

包括但不限于：

- 交易笔数
- 胜率
- 已实现 / 未实现 PnL
- 平均盈利 / 平均亏损
- profit factor
- open exposure

### 8.2 attribution

归因维度包括：

- `by_asset_type`
- `by_status`
- `by_direction`
- `by_variety`
- `by_account`
- `by_reason_tag`

### 8.3 patterns

当前规则层会识别多类可重复模式，例如：

- 方向集中亏损
- 品种集中亏损
- 逆期限结构交易
- 未平仓风险累积
- 止损拖延

## 9. 快速开始

### 9.1 跑测试

```bash
python scripts/test.py
```

### 9.2 脚本调用

```python
from scripts.review import run

result = run(
    trades,
    context=None,
    mode="range",
    period={"start": "2026-06-04", "end": "2026-06-10"},
    config={
        "panda_data": {
            "username": "...",
            "password": "...",
        },
        "llm": {
            "enabled": True
        }
    },
)
```

### 9.3 CLI 调用

```bash
python scripts/review.py --trades trades.csv --mode daily
```

## 10. 推荐使用方式

```text
1. 准备标准化交易输入
2. 用 review.py 生成 result_json
3. 让宿主模型读取 result_json
4. 按 host_fill_contract 回填原报告位置
5. 输出最终复盘报告
```

## 11. 开发与维护

### 11.1 回归测试

当前测试覆盖：

- stock only
- futures only
- mixed
- missing field
- host model mode by default
- mock context
- strategy doc parsing

### 11.2 修改原则

- 不要把已实现 PnL 重新按逐笔成交重算
- 不要绕过正式 `panda_data` 链路做正式复盘
- 如果原因未证实，必须写 `not yet proven`
- 如果用户要求逐笔对比，不允许偷换成高层总结

## 12. 参考文档

```text
references/method_guide.md
references/data_guide.md
references/llm_policy.md
references/output_contract.md
references/review_checklist.md
```
