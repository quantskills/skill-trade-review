# 数据接入与字段口径（R00 交易复盘）

## 数据来源

- **trades**：由调用方提供，必须按"已平仓 / 未平仓"两类规整后传入。skill **不做 FIFO 配对**，直接信任传入的 PnL。
- **市场环境（context）**：由 skill 自动通过 PandaAI `panda_data` SDK 拉取；调用方也可显式传入预先准备好的 context。
- **未平仓笔的 mark_price**：缺失时 skill 自动通过 panda_data 补齐（见下方"自动补齐"）。

正式生产时，行情数据应来自 PandaAI data 数据拉取库或项目指定数据源。不得使用来源不明、字段不稳定、手工整理的临时表作为正式输入。

## 环境变量

```bash
set PANDA_DATA_USERNAME= 
set PANDA_DATA_PASSWORD= 
```

可选，控制 LLM 层：

```bash
set ANTHROPIC_BASE_URL= 
set ANTHROPIC_API_KEY= 
set REVIEW_LLM_DISABLED=1     # 等同 config.llm.enabled=False
```

LLM 也可通过 `config.llm.api_key` / `config.llm.base_url` 在调用层覆盖（最高优先级）。

## trades 字段口径

### 通用字段（必填）

| 字段 | 类型 | 说明 |
|---|---|---|
| trade_date | string | 业务日期；已平=平仓日，未平=快照日 |
| ts_code | string | 标的代码，自动识别品种 |
| direction | string | 持仓方向：`long` / `short`（不是下单方向） |
| status | string | `closed` / `open` |
| volume | float | 数量（股票=股，期货=手） |

### closed 笔（已平仓）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| realized_pnl | float | ✅ | 已实现盈亏，含费用扣除后的净 PnL（调用方负责费用计算） |

### open 笔（未平仓）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| cost_price | float | ✅ | 加权开仓成本价 |
| mark_price | float | 可选 | 当前市价；缺则 skill 自动补 |
| unrealized_pnl | float | 可选 | 浮动盈亏；缺则 skill 用 cost/mark/multiplier 计算 |
| open_date | string | 可选 | 用于持仓时长归因 |

### 可选字段（任意笔）

| 字段 | 类型 | 说明 |
|---|---|---|
| trade_time | string | HH:MM:SS，时段归因用 |
| account_id | string | 账户/策略ID，缺则填 default |
| reason_tag | string | 入场理由标签，归因用 |

## 品种自动识别（asset_classifier.py）

| ts_code 模式 | asset_type | 说明 |
|---|---|---|
| `\d{6}\.(SZ\|SH\|BJ)` | `stock_a` | A 股 |
| `\d{4,5}\.HK` | `stock_hk` | 港股 |
| `[A-Z]{1,5}` 或 `XXX.US` | `stock_us` | 美股 |
| `[A-Z]{1,2}\d{3,4}\.(CFE\|SHFE\|DCE\|CZCE\|INE\|GFEX)` | `future_cn` | 期货合约 |
| `[A-Z]{1,2}\.(CFE\|SHFE\|...)` | `future_cn` | 期货品种主力 |
| 其他 | `unknown` | 报警告 |

期货合约乘数自动从内置字典查询，已收录 27 个常见品种（IF/IH/IC/IM/T/TF, RB/HC/I, CU/AL/ZN/NI/AU/AG, SC, M/Y/P/C, MA/TA/PP/L/V）。新增品种请补充到 `scripts/asset_classifier.py` 的 `FUTURE_SPECS`。

## 自动补齐 mark_price（pnl_normalizer.py）

```
open 笔
  ├─ 调用方传入 mark_price?
  │     ├─ 是 → 直接用
  │     └─ 否 → 调 panda_data 拉 ts_code 在 trade_date 当日收盘:
  │             ├─ stock → get_stock_daily
  │             └─ future → get_future_daily; 失败则用品种主力 get_future_dominant 兜底
  ├─ 仍缺失 → unrealized_pnl 记 0，warning 提示
  └─ 计算 unrealized_pnl:
        long  : (mark - cost) * volume * multiplier
        short : (cost - mark) * volume * multiplier
```

## 市场环境拉取（context_loader.py）

按 trades 命中的 asset_type / variety 分支拉取：

### 股票分支（`stock_context.py`）
- `get_index_daily`：沪深 300 收盘、当日涨跌、5 日年化波动
- `get_hsgt_hold`：北向持仓金额（接口不可用时跳过）
- 推断 regime_label：`high_vol` / `trend_up` / `trend_down` / `range_bound`

### 期货分支（`future_context.py`）
按命中品种分别拉：
- `get_future_dominant`：主力合约日线（close、当日涨跌、5 日年化波动、主力代码）
- `get_future_basis`：基差及基差率
- `get_future_warehouse_receipt`：仓单
- `get_future_ls_ratio`：多空持仓比
- 推断 commodity_regime：`backwardation` / `flat` / `contango`

每个失败接口都做容错降级，不阻塞复盘流程；失败信息写入 `context.fetch_failures` 并在 result_json.warnings 里报告。

## 清洗规则

- `volume > 0`、`cost_price > 0`、价格非负（mark_price 允许 NaN）
- closed 笔必须有 realized_pnl；open 笔必须有 cost_price
- direction 仅限 `long` / `short`；status 仅限 `closed` / `open`
- account_id 缺则填 `default`
- ts_code 解析为 unknown 时报 warning，不阻塞

## 正式接入提醒

- 期货品种乘数和默认基准可通过修改 `FUTURE_SPECS` 自定义
- 多账户合并复盘建议每个账户单独跑一次 `run()`，再合并结果
- 区间复盘 `mode='range'`，period 必须显式传入 start/end
