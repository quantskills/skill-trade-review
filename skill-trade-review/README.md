# Trade Review

**简体中文** | [English](README.en.md)

> 把交易流水转成完整复盘结果：数据校验 → 市场环境补充 → 归因统计 → 逐笔点评 → 错误模式识别 → 建议与可选 LLM 总结。

## Skill 定位

`trade-review` 是一个交易复盘 skill，面向日度复盘、区间复盘、策略复盘和账户复盘。

## 目录结构

```text
skill-trade-review/
├─ agents/
├─ references/
├─ scripts/
├─ LICENSE
├─ README.md
├─ README.en.md
├─ requirements.txt
└─ SKILL.md
```

## 流程

```mermaid
flowchart LR
    A["交易流水"] --> B["校验与标准化"]
    B --> C["拉取市场环境"]
    C --> D["统计 summary / attribution"]
    D --> E["市场点评 + 逐笔点评"]
    E --> F["模式识别 + 建议"]
    F --> G["可选 LLM 总结"]
```

## 主要产出

| 模块 | 内容 |
|---|---|
| `summary` | 胜率、盈亏、暴露、profit factor |
| `attribution` | 按资产、方向、账户、品种、标签归因 |
| `market_review` | 股票/期货市场环境点评 |
| `trade_reviews` | 逐笔交易点评 |
| `patterns` | 错误模式识别 |
| `advice` | 可执行建议 |
| `narrative_md` | 可读 markdown 复盘 |

## 快速使用

```bash
python scripts/test.py
python scripts/review.py --trades trades.csv --mode daily --date 2026-06-04
```

## 参考文件

```text
references/method_guide.md
references/data_guide.md
references/llm_policy.md
references/output_contract.md
references/review_checklist.md
```

