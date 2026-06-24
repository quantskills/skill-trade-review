# 复盘方法论（R00 交易复盘）

## 整体流程

```
trades(list[dict])
    │
    ▼
[1] validate_and_split             校验字段、归一化 status/direction、识别品种
    │
    ▼
[2] fill_missing_mark_and_unrealized   未平仓笔自动补 mark_price 与 unrealized_pnl
    │
    ▼
[3] load_context                   按品种拉股票/期货市场环境
    │
    ▼
[4] _compute_summary               PnL/胜率/盈亏比/敞口
    │
    ▼
[5] _compute_attribution           多维 groupby 拆解 PnL
    │
    ▼
[6] detect_patterns                确定性规则池
    │
    ▼
[7] generate_advice                模式 → 建议模板
    │
    ▼
[8] host_model_fill                宿主模型按原插槽回填 narrative / insights / 策略点评
    │
    ▼
[9] _compute_score → _rate         评级 5 档
    │
    ▼
result_json (含 9 个顶层键)
```

## summary 字段

| 字段 | 计算 |
|---|---|
| n_trades / n_closed / n_open | 笔数 |
| n_winning_closed | closed 笔中 realized_pnl > 0 的笔数 |
| win_rate_closed | n_winning_closed / n_closed（默认仅看 closed 笔） |
| realized_pnl | Σ closed.realized_pnl |
| unrealized_pnl | Σ open.unrealized_pnl |
| total_pnl | realized + unrealized |
| avg_win / avg_loss | closed 笔正/负 PnL 的均值 |
| profit_factor | Σ 正 PnL / |Σ 负 PnL|；无亏损时 = ∞（输出为 null） |
| open_exposure | Σ open.(cost_price * volume * multiplier) |

`include_open_in_winrate=True` 时，open 笔按 unrealized_pnl 正负计入胜率分子分母。

## attribution 维度

每个分组同时报 `realized` / `unrealized` / `n_closed` / `n_open`：

- `by_asset_type`：stock_a / stock_hk / stock_us / future_cn
- `by_status`：仅含 closed 的 realized 和 open 的 unrealized 两个标量
- `by_direction`：long / short
- `by_variety`：期货品种代码（IF / RB / ...），股票笔的 variety 为 None
- `by_account`：account_id
- `by_reason_tag`：reason_tag 标签

会计恒等：`Σ by_status = total_pnl`、`Σ by_asset_type = total_pnl`。

## patterns 规则池（10 条）

| tag | 触发条件 | 默认 confidence |
|---|---|---|
| `risk_buildup` | 未平仓浮亏笔 ≥ 2 | min(1.0, n_open_loss / 4) |
| `low_winrate` | n_closed ≥ 5 且 win_rate < 0.35 | 0.8 |
| `direction_concentration` | 单方向 PnL 占总 PnL >90% 且为亏损 | 0.7 |
| `variety_concentration` | 单品种 |PnL| 占总 |PnL| >80% | 0.6 |
| `stop_loss_delay` | 持仓 ≥5 天且仍浮亏的笔数 ≥ 2 | 0.75 |
| `high_vol_long_bias` | 股票批次 + 5日波动 ≥25% + 做多胜率 <40% | 0.65 |
| `breakout_in_range` | 震荡市标签 + breakout 标签笔合计亏损 | 0.7 |
| `against_basis` | 期货 contango 仍裸多 / backwardation 仍裸空，亏损 ≥ 2 笔 | min(1.0, n / 4) |
| `fee_drag` | 期货 |PnL|<50 笔 ≥3 且批次合计亏损 | 0.55 |
| `cross_variety_loss` | 相关品种组合（黑色/有色/油脂/股指）同向共损 | 0.6 |

新增规则只需在 `scripts/patterns.py` 的 `ALL_RULES` 注册函数。

## advice 模板（priority + action + expected_impact）

模式 → 建议是 1:1 映射（同 tag 多次命中只生成一条建议）。优先级：`high` > `medium` > `low`，advice 按优先级排序。

详见 `scripts/advice.py` 的 `ADVICE_TEMPLATES`。

## 评级公式

```
fut_ratio = future_cn 占 total |PnL| 的比例
baseline  = 0.4 * fut_ratio + 0.5 * (1 - fut_ratio)   # 期货胜率基线 40%，股票 50%
alpha     = total_pnl / (open_exposure + |realized_pnl|)

score = 0.4 * (win_rate - baseline) * 2.0
      + 0.4 * tanh(profit_factor - 1.0)
      + 0.2 * tanh(alpha * 50)

档位:
  excellent : score > 0.6
  good      : 0.3 < score ≤ 0.6
  neutral   : -0.1 < score ≤ 0.3   或   n_closed < 5（强制降级）
  poor      : -0.4 < score ≤ -0.1
  bad       : score ≤ -0.4
```

## warnings 触发条件

- closed 样本 <5 → 评级降级为 neutral
- mark_price 自动补齐失败 → 浮盈记 0
- ts_code 无法识别品种 → 列出 unknown ts_codes
- panda_data 登录失败 → 跳过 context 拉取
- 任何 fetch 接口失败 → 接口名 + 异常信息

## 不允许的事

- **未来函数**：复盘 t 日仅使用 t 日及之前的数据；区间复盘仅使用结束日及之前的数据
- **流水重算 PnL**：调用方传入的 realized_pnl 必须信任，skill 不做 FIFO 二次配对
- **LLM 改写数值**：narrative / insights 不会、也不能修改 summary / attribution / patterns / advice / score / rating
- **明文凭据写代码**：账号密码 / API Key 仅通过环境变量或调用层 config 注入
