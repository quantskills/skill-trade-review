"""PnL 规范化 — 校验 closed/open 字段，自动补 mark_price 与 unrealized_pnl。"""
from __future__ import annotations

from typing import Any

import pandas as pd

from asset_classifier import classify

VALID_STATUS = {"closed", "open"}
VALID_DIRECTION = {"long", "short"}

REQUIRED_COMMON = ["trade_date", "ts_code", "direction", "status", "volume"]


def _coerce_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def validate_and_split(trades: Any) -> tuple[pd.DataFrame, list[str]]:
    """校验 trades，返回 (规范化后的 DataFrame, warnings 列表)。

    入口允许传 list[dict] 或 DataFrame。
    """
    warnings: list[str] = []

    if trades is None:
        raise ValueError("trades 不能为空")

    df = pd.DataFrame(trades).copy()
    if df.empty:
        raise ValueError("trades 不能为空表")

    # 1. 必填字段
    missing_common = [c for c in REQUIRED_COMMON if c not in df.columns]
    if missing_common:
        raise ValueError(f"trades 缺少必要字段: {sorted(missing_common)}")

    # 2. 取值约束
    df["status"] = df["status"].map(_coerce_str).str.lower()
    df["direction"] = df["direction"].map(_coerce_str).str.lower()
    if not set(df["status"]).issubset(VALID_STATUS):
        raise ValueError(f"status 取值仅限 closed/open，实际: {sorted(set(df['status']))}")
    if not set(df["direction"]).issubset(VALID_DIRECTION):
        raise ValueError(f"direction 取值仅限 long/short，实际: {sorted(set(df['direction']))}")

    # 3. closed 笔必填 realized_pnl
    closed = df[df["status"] == "closed"]
    if not closed.empty and "realized_pnl" not in df.columns:
        raise ValueError("存在 closed 笔但缺少 realized_pnl 字段")
    if not closed.empty:
        miss_pnl = closed["realized_pnl"].isna().sum()
        if miss_pnl:
            raise ValueError(f"{int(miss_pnl)} 笔 closed 缺 realized_pnl")

    # 4. open 笔必填 cost_price
    opens = df[df["status"] == "open"]
    if not opens.empty:
        if "cost_price" not in df.columns:
            raise ValueError("存在 open 笔但缺少 cost_price 字段")
        miss_cost = opens["cost_price"].isna().sum()
        if miss_cost:
            raise ValueError(f"{int(miss_cost)} 笔 open 缺 cost_price")

    # 5. 派生 asset_type / variety / multiplier / benchmark
    asset_info = df["ts_code"].astype(str).map(classify)
    df["asset_type"] = asset_info.map(lambda x: x.asset_type)
    df["variety"] = asset_info.map(lambda x: x.variety)
    df["multiplier"] = asset_info.map(lambda x: x.multiplier)
    df["benchmark_default"] = asset_info.map(lambda x: x.benchmark)

    if (df["asset_type"] == "unknown").any():
        unknown_codes = sorted(df.loc[df["asset_type"] == "unknown", "ts_code"].unique())
        warnings.append(f"以下 ts_code 无法识别品种，按 unknown 处理: {unknown_codes}")

    # 6. account_id 缺省
    if "account_id" not in df.columns:
        df["account_id"] = "default"
    df["account_id"] = df["account_id"].fillna("default").astype(str)

    return df, warnings


def fill_missing_mark_and_unrealized(
    df: pd.DataFrame,
    warnings: list[str],
    fetch_mark_price=None,
) -> pd.DataFrame:
    """补齐未平仓笔的 mark_price 与 unrealized_pnl。

    fetch_mark_price: callable(ts_code, date) -> float 或 None；为 None 时不拉取，
    缺失则触发 warning 并令 unrealized_pnl = 0（按 §决策3：自动补齐 — 这里若调用方
    没注入数据源，则降级为 0 并 warning，避免阻塞。)
    """
    df = df.copy()
    if "mark_price" not in df.columns:
        df["mark_price"] = pd.NA
    if "unrealized_pnl" not in df.columns:
        df["unrealized_pnl"] = pd.NA

    open_mask = df["status"] == "open"
    if not open_mask.any():
        return df

    # 1. 缺 mark_price → 拉
    need_mark = open_mask & df["mark_price"].isna()
    if need_mark.any() and fetch_mark_price is not None:
        for idx in df.index[need_mark]:
            ts_code = df.at[idx, "ts_code"]
            asof = df.at[idx, "trade_date"]
            try:
                price = fetch_mark_price(ts_code, asof)
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"补齐 {ts_code} mark_price 失败: {exc}; 浮盈记 0")
                price = None
            if price is None:
                warnings.append(f"{ts_code} 无法获取 mark_price, 浮盈记 0")
            df.at[idx, "mark_price"] = price

    # 2. 计算 unrealized_pnl
    need_unreal = open_mask & df["unrealized_pnl"].isna()
    for idx in df.index[need_unreal]:
        cost = df.at[idx, "cost_price"]
        mark = df.at[idx, "mark_price"]
        vol = df.at[idx, "volume"]
        mult = df.at[idx, "multiplier"]
        direction = df.at[idx, "direction"]
        if pd.isna(mark) or pd.isna(cost) or pd.isna(vol):
            df.at[idx, "unrealized_pnl"] = 0.0
            continue
        diff = (mark - cost) if direction == "long" else (cost - mark)
        df.at[idx, "unrealized_pnl"] = float(diff) * float(vol) * float(mult)

    return df
