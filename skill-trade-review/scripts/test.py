"""测试 6 个用例：股票 / 期货 / 混合 / 空 / 缺字段 / LLM-disabled。"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from review import run

# 全部测试在禁用 panda_data context 拉取的环境下进行；真实数据用例独立标注
BASE_CFG = {"llm": {"enabled": False}, "disable_context_fetch": True}


def test_preflight_requires_credentials_by_default() -> None:
    old_env = {k: os.environ.get(k) for k in (
        "ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY",
        "PANDA_DATA_USERNAME", "PANDA_DATA_PASSWORD",
    )}
    for key in old_env:
        os.environ.pop(key, None)
    trades = [
        {"trade_date": "2026-06-04", "ts_code": "RB2610.SHFE", "direction": "long",
         "status": "closed", "volume": 5, "realized_pnl": 1200.0},
    ]
    try:
        try:
            run(trades, mode="daily")
        except EnvironmentError as exc:
            msg = str(exc)
            assert "ANTHROPIC_BASE_URL" in msg
            assert "ANTHROPIC_API_KEY" in msg
            assert "PANDA_DATA_USERNAME" in msg
            assert "PANDA_DATA_PASSWORD" in msg
            print("[OK] preflight_requires_credentials_by_default")
            return
        raise AssertionError("默认运行缺凭据时必须提示配置")
    finally:
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_preflight_accepts_config_credentials() -> None:
    old_env = {k: os.environ.get(k) for k in (
        "ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY",
        "PANDA_DATA_USERNAME", "PANDA_DATA_PASSWORD",
    )}
    for key in old_env:
        os.environ.pop(key, None)
    trades = [
        {"trade_date": "2026-06-04", "ts_code": "RB2610.SHFE", "direction": "long",
         "status": "closed", "volume": 5, "realized_pnl": 1200.0},
    ]
    cfg = {
        "llm": {"enabled": False, "base_url": " ", "api_key": " "},
        "panda_data": {"username": " ", "password": " "},
        "disable_context_fetch": True,
    }
    try:
        out = run(trades, mode="daily", config=cfg)
        parsed = json.loads(out["result_json"].iloc[0])
        assert parsed["summary"]["n_closed"] == 1
        print("[OK] preflight_accepts_config_credentials")
    finally:
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_stock_only() -> None:
    trades = [
        {"trade_date": "2026-06-04", "ts_code": "000001.SZ", "direction": "long",
         "status": "closed", "volume": 1000, "realized_pnl": 580.0, "reason_tag": "momentum",
         "cost_price": 12.5},
        {"trade_date": "2026-06-04", "ts_code": "600519.SH", "direction": "long",
         "status": "closed", "volume": 100, "realized_pnl": -240.0, "reason_tag": "value",
         "cost_price": 1700.0},
        {"trade_date": "2026-06-04", "ts_code": "000333.SZ", "direction": "long",
         "status": "closed", "volume": 500, "realized_pnl": 120.0, "reason_tag": "momentum",
         "cost_price": 60.0},
        {"trade_date": "2026-06-04", "ts_code": "600519.SH", "direction": "long",
         "status": "open", "volume": 100, "cost_price": 1700.0, "mark_price": 1690.0},
    ]
    out = run(trades, mode="daily", config=BASE_CFG)
    parsed = json.loads(out["result_json"].iloc[0])
    assert out["build_id"].iloc[0] == "R00"
    assert parsed["summary"]["n_closed"] == 3
    assert parsed["summary"]["n_open"] == 1
    assert "stock_a" in parsed["attribution"]["by_asset_type"]
    # 新结构断言
    assert "market_review" in parsed and "trade_reviews" in parsed
    assert "narrative_md" in parsed and parsed["narrative_md"]
    # advice 是 dict, 含三个 bucket + flat
    adv = parsed["advice"]
    assert isinstance(adv, dict)
    for k in ("actions_now", "actions_next_session", "process_changes", "flat"):
        assert k in adv
    # 逐笔点评每笔都被覆盖
    tr = parsed["trade_reviews"]
    assert len(tr["closed"]) == 3 and len(tr["open"]) == 1
    print("[OK] stock_only")


def test_future_only() -> None:
    trades = [
        {"trade_date": "2026-06-04", "ts_code": "RB2610.SHFE", "direction": "long",
         "status": "closed", "volume": 5, "realized_pnl": 1500.0, "reason_tag": "breakout"},
        {"trade_date": "2026-06-04", "ts_code": "RB2610.SHFE", "direction": "short",
         "status": "closed", "volume": 3, "realized_pnl": -900.0, "reason_tag": "scalp"},
        {"trade_date": "2026-06-04", "ts_code": "IF2606.CFE", "direction": "long",
         "status": "closed", "volume": 1, "realized_pnl": 200.0},
        {"trade_date": "2026-06-04", "ts_code": "IF2606.CFE", "direction": "short",
         "status": "open", "volume": 2, "cost_price": 3850.0, "mark_price": 3870.0,
         "open_date": "2026-06-02"},
    ]
    out = run(trades, mode="daily", config=BASE_CFG)
    parsed = json.loads(out["result_json"].iloc[0])
    assert "future_cn" in parsed["attribution"]["by_asset_type"]
    assert "RB" in parsed["attribution"]["by_variety"] and "IF" in parsed["attribution"]["by_variety"]
    # IF 短头浮亏: (3850-3870) * 2 * 300 = -12000
    by_var_if = parsed["attribution"]["by_variety"]["IF"]
    assert by_var_if["unrealized"] == -12000.0, f"unexpected IF unrealized: {by_var_if}"
    print("[OK] future_only")


def test_mixed() -> None:
    trades = [
        {"trade_date": "2026-06-04", "ts_code": "RB2610.SHFE", "direction": "long",
         "status": "closed", "volume": 5, "realized_pnl": 1200.0},
        {"trade_date": "2026-06-04", "ts_code": "000001.SZ", "direction": "long",
         "status": "closed", "volume": 1000, "realized_pnl": -300.0},
        {"trade_date": "2026-06-04", "ts_code": "600519.SH", "direction": "long",
         "status": "open", "volume": 100, "cost_price": 1700.0, "mark_price": 1685.0},
        {"trade_date": "2026-06-04", "ts_code": "IF2606.CFE", "direction": "short",
         "status": "open", "volume": 1, "cost_price": 3850.0, "mark_price": 3870.0},
    ]
    out = run(trades, mode="daily", config=BASE_CFG)
    parsed = json.loads(out["result_json"].iloc[0])
    by_at = parsed["attribution"]["by_asset_type"]
    assert "future_cn" in by_at and "stock_a" in by_at
    assert parsed["summary"]["n_open"] == 2 and parsed["summary"]["n_closed"] == 2
    print("[OK] mixed")


def test_empty_input() -> None:
    try:
        run([], config=BASE_CFG)
    except ValueError as exc:
        assert "不能为空表" in str(exc)
        print("[OK] empty_input")
        return
    raise AssertionError("空数据必须抛 ValueError")


def test_missing_field() -> None:
    try:
        run([{"trade_date": "2026-06-04", "ts_code": "RB2610.SHFE", "direction": "long",
              "status": "closed", "volume": 5}], config=BASE_CFG)
    except ValueError as exc:
        assert "realized_pnl" in str(exc) or "缺" in str(exc)
        print("[OK] missing_field")
        return
    raise AssertionError("缺 realized_pnl 必须抛 ValueError")


def test_llm_disabled() -> None:
    trades = [
        {"trade_date": "2026-06-04", "ts_code": "RB2610.SHFE", "direction": "long",
         "status": "closed", "volume": 5, "realized_pnl": 1200.0},
        {"trade_date": "2026-06-04", "ts_code": "RB2610.SHFE", "direction": "short",
         "status": "closed", "volume": 5, "realized_pnl": -700.0},
    ]
    out = run(trades, mode="daily", config={"llm": {"enabled": False}, "disable_context_fetch": True})
    parsed = json.loads(out["result_json"].iloc[0])
    assert parsed.get("narrative") is None
    assert parsed.get("insights") is None
    assert parsed["llm_meta"]["enabled"] is False
    # narrative_md 在 LLM 关闭时仍由规则降级生成（非空）
    assert parsed["narrative_md"]
    # market_review / trade_reviews 即使 LLM 关闭也必须输出结构
    assert "market_review" in parsed and "trade_reviews" in parsed
    print("[OK] llm_disabled")


def test_real_panda_data() -> None:
    """真实数据用例: 用 panda_data 拉 mark_price 自动补齐。"""
    if not (os.getenv("PANDA_DATA_USERNAME") and os.getenv("PANDA_DATA_PASSWORD")):
        print("[SKIP] real_panda_data — 缺凭据")
        return
    trades = [
        {"trade_date": "2026-05-28", "ts_code": "000001.SZ", "direction": "long",
         "status": "closed", "volume": 1000, "realized_pnl": 580.0},
        {"trade_date": "2026-05-28", "ts_code": "600519.SH", "direction": "long",
         "status": "open", "volume": 100, "cost_price": 1700.0},  # 故意不传 mark_price
    ]
    out = run(trades, mode="daily",
              config={"llm": {"enabled": False}})  # 允许 context 拉取
    parsed = json.loads(out["result_json"].iloc[0])
    # 茅台浮盈应该被自动算出来（非 0）
    open_unreal = parsed["attribution"]["by_status"]["open"]
    assert open_unreal != 0.0, f"未平仓浮盈应被补齐自动计算, got {open_unreal}"
    print(f"[OK] real_panda_data — 浮盈补齐成功: {open_unreal}")


def test_with_mock_context() -> None:
    """带模拟 context 的用例: 验证 market_review + per_trade_review 规则路径。"""
    trades = [
        {"trade_date": "2026-06-04", "ts_code": "RB2610.SHFE", "direction": "long",
         "status": "closed", "volume": 5, "realized_pnl": -1500.0,
         "cost_price": 3500.0, "reason_tag": "breakout"},
        {"trade_date": "2026-06-04", "ts_code": "600519.SH", "direction": "long",
         "status": "open", "volume": 100, "cost_price": 1700.0, "mark_price": 1620.0,
         "open_date": "2026-05-28"},
    ]
    mock_ctx = {
        "stock": {"benchmark": "000300.SH", "index_close": 4000.0,
                  "index_pct_pct": -1.8, "index_volatility_5d": 32.0,
                  "north_hold_amount": -2.5e9},
        "futures": {
            "RB": {"variety": "RB", "dominant_close": 3450.0,
                   "dominant_pct_pct": -0.8, "volatility_5d": 22.0,
                   "basis_rate": -0.012, "commodity_regime": "contango",
                   "ls_ratio": 0.65}
        },
        "regime_label": "high_vol",
        "fetch_failures": [],
    }
    out = run(trades, context=mock_ctx, mode="daily",
              config={"llm": {"enabled": False}, "disable_context_fetch": True})
    parsed = json.loads(out["result_json"].iloc[0])

    # 1. market_review 应有 stock + futures + 综合 regime
    mr = parsed["market_review"]
    assert mr["stock"]["trend"] == "strong_down", mr["stock"]
    assert mr["stock"]["volatility"] == "high"
    assert "RB" in mr["futures"]
    assert mr["futures"]["RB"]["basis_regime"] == "contango"
    assert mr["overall_regime"] == "high_vol_market"

    # 2. per_trade_review: RB 多单逆基差; 茅台浮亏 -4.7% (high_vol_market 下逆趋势)
    tr = parsed["trade_reviews"]
    rb_rev = next(r for r in tr["closed"] if r["ts_code"] == "RB2610.SHFE")
    assert rb_rev["against_basis"] is True, rb_rev
    assert rb_rev["alignment"] in ("against", "mixed"), rb_rev
    assert rb_rev["outcome"] in ("loss", "big_loss"), rb_rev
    assert rb_rev["rating"] in ("bad", "poor"), rb_rev  # 兼容字段

    mt_rev = next(r for r in tr["open"] if r["ts_code"] == "600519.SH")
    assert mt_rev["alignment"] == "against", mt_rev  # 高波动 + 做多
    assert mt_rev["outcome"] in ("loss", "big_loss"), mt_rev
    assert mt_rev["execution"] in ("poor", "questionable"), mt_rev
    assert mt_rev["risk_level"] == "high", mt_rev  # 兼容字段
    assert mt_rev["hold_days"] == 7

    # 三维度摘要必须存在
    s = tr["summary"]
    for k in ("outcome_dist", "alignment_dist", "execution_dist"):
        assert k in s, f"missing summary key: {k}"

    # 3. advice: 应当至少出现 actions_now 中的高优单笔风险建议 + 高波动市建议
    adv = parsed["advice"]
    actions_now = adv["actions_now"]
    assert any(a.get("target_ts_code") == "600519.SH" for a in actions_now), actions_now
    assert any(a.get("tag") == "high_vol_market" for a in adv["actions_next_session"])

    # 4. narrative_md 含三段以上小节标题
    md = parsed["narrative_md"]
    for section in ("市场行情研判", "逐笔交易点评", "决策建议"):
        assert section in md, f"narrative_md missing section: {section}"
    print("[OK] with_mock_context")


def _real_panda_data_keep() -> None:
    pass


def test_strategy_doc_yaml(tmp_dir: str = ".") -> None:
    """策略文件 YAML 解析。"""
    import tempfile, os
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from strategy_doc import load_strategy

    yaml_text = """
name: test_breakout_strategy
description: 高波动日内突破策略
applicable_regimes: [high_vol, breakout_high]
entry_rules:
  - "ATR > 100 且 价格突破前 N 日高点"
  - "成交量放大 1.5 倍以上"
exit_rules:
  - "回到 MA20 下方止损"
risk_rules:
  - "单笔风险敞口不超过账户 2%"
key_indicators: [ATR, MA20, recent_high]
reason_tags: [breakout, momentum]
"""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as f:
        f.write(yaml_text)
        p = f.name
    try:
        doc = load_strategy(p)
        assert doc.name == "test_breakout_strategy"
        assert "high_vol" in doc.applicable_regimes
        assert len(doc.entry_rules) == 2
        assert "ATR" in doc.key_indicators
        assert doc.source_format == "yaml"
        print("[OK] strategy_doc_yaml")
    finally:
        os.unlink(p)


def test_strategy_doc_markdown() -> None:
    import tempfile, os
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from strategy_doc import load_strategy

    md_text = """# 突破跟随策略

适合在 high_vol 环境下捕捉日内突破。

## 适用环境
high_vol, breakout_high

## 入场规则
- 价格突破前 20 日高点
- 成交量放大

## 出场规则
- 跌破 MA20

## 风控
- 单笔不超过 2% 账户

## 关键指标
- ATR
- MA20

## reason_tag
- breakout
- momentum
"""
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(md_text)
        p = f.name
    try:
        doc = load_strategy(p)
        assert doc.name == "突破跟随策略"
        assert "high_vol" in doc.applicable_regimes
        assert len(doc.entry_rules) == 2
        assert "ATR" in doc.key_indicators
        assert "breakout" in doc.reason_tags
        assert doc.source_format == "markdown"
        print("[OK] strategy_doc_markdown")
    finally:
        os.unlink(p)


def test_strategy_doc_python() -> None:
    import tempfile, os
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from strategy_doc import load_strategy

    py_text = """
def register_strategy():
    return {
        "name": "py_breakout",
        "description": "Python 注册的突破策略",
        "applicable_regimes": ["high_vol"],
        "entry_rules": ["ATR>100 且 突破前高"],
        "exit_rules": ["跌破 MA20"],
        "risk_rules": ["单笔 2%"],
        "key_indicators": ["ATR", "MA20"],
        "reason_tags": ["breakout"],
    }
"""
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(py_text)
        p = f.name
    try:
        doc = load_strategy(p)
        assert doc.name == "py_breakout"
        assert "ATR" in doc.key_indicators
        assert doc.source_format == "python"
        print("[OK] strategy_doc_python")
    finally:
        os.unlink(p)


def test_strategy_review_disabled_when_no_path() -> None:
    """没传 strategy_path 时，result_json 中 strategy_review 应为 None，输出与现状一致。"""
    trades = [
        {"trade_date": "2026-06-04", "ts_code": "RB2610.SHFE", "direction": "long",
         "status": "closed", "volume": 5, "realized_pnl": 1200.0,
         "cost_price": 3500.0, "reason_tag": "breakout"},
    ]
    out = run(trades, mode="daily", config=BASE_CFG)
    parsed = json.loads(out["result_json"].iloc[0])
    assert parsed.get("strategy_review") is None, "未传 strategy_path 时应为 None"
    assert parsed.get("strategy_doc") is None
    # narrative_md 不应包含「策略适配复盘」
    assert "策略适配复盘" not in parsed.get("narrative_md", "")
    print("[OK] strategy_review_disabled_when_no_path")


if __name__ == "__main__":
    test_preflight_requires_credentials_by_default()
    test_preflight_accepts_config_credentials()
    test_stock_only()
    test_future_only()
    test_mixed()
    test_empty_input()
    test_missing_field()
    test_llm_disabled()
    test_with_mock_context()
    test_strategy_doc_yaml()
    test_strategy_doc_markdown()
    test_strategy_doc_python()
    test_strategy_review_disabled_when_no_path()
    test_real_panda_data()
    print("=== 全部测试通过 ===")
