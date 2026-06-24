"""市场环境拉取 — 按 trades 命中的品种类型分别调用股票 / 期货 context。"""
from __future__ import annotations

import os
from typing import Any

import pandas as pd

from stock_context import load_stock_context, fetch_stock_mark_price
from future_context import load_future_context, fetch_future_mark_price


def _ensure_panda_login(username: str | None = None, password: str | None = None) -> bool:
    """惰性登录 panda_data, 凭据缺失时返回 False（caller 决定是否报错或降级）。"""
    try:
        import panda_data
    except ImportError:
        return False
    username = username or os.getenv("PANDA_DATA_USERNAME")
    password = password or os.getenv("PANDA_DATA_PASSWORD")
    if not username or not password:
        return False
    try:
        panda_data.init_token(username=username, password=password)
        return True
    except Exception:
        return False


def load_context(df: pd.DataFrame, period: dict, credentials: dict | None = None) -> dict:
    """根据 trades 命中的 asset_type/variety 拉取相应市场环境。

    返回 dict: {
        "stock": {...} 或 None,
        "futures": {variety: {...}, ...} 或 {},
        "regime_label": "...",
        "fetch_failures": [...],
    }
    """
    out: dict[str, Any] = {"stock": None, "futures": {}, "regime_label": None, "fetch_failures": []}

    credentials = credentials or {}
    if not _ensure_panda_login(credentials.get("username"), credentials.get("password")):
        out["fetch_failures"].append("panda_data 未登录或未安装，跳过 context 拉取")
        return out

    asset_types = set(df["asset_type"].unique())
    start, end = period["start"].replace("-", ""), period["end"].replace("-", "")

    if "stock_a" in asset_types:
        try:
            out["stock"] = load_stock_context(start, end)
        except Exception as exc:  # noqa: BLE001
            out["fetch_failures"].append(f"stock context 拉取失败: {exc}")

    fut_codes = df.loc[df["asset_type"] == "future_cn", "variety"].dropna().unique().tolist()
    for variety in fut_codes:
        try:
            out["futures"][variety] = load_future_context(variety, start, end)
        except Exception as exc:  # noqa: BLE001
            out["fetch_failures"].append(f"future context [{variety}] 拉取失败: {exc}")

    out["regime_label"] = _infer_regime(out)
    return out


def _infer_regime(ctx: dict) -> str | None:
    """简单 regime 标签：基于股票指数涨跌 + 5 日波动。"""
    stock = ctx.get("stock") or {}
    pct = stock.get("index_pct_pct")
    vol = stock.get("index_volatility_5d")
    if pct is None or vol is None:
        return None
    if vol > 0.30:
        return "high_vol"
    if pct > 0.5:
        return "trend_up"
    if pct < -0.5:
        return "trend_down"
    return "range_bound"


def make_mark_price_fetcher(credentials: dict | None = None):
    """返回一个 callable(ts_code, date_str) → float 或 None, 供 pnl_normalizer 使用。"""
    credentials = credentials or {}
    if not _ensure_panda_login(credentials.get("username"), credentials.get("password")):
        return None

    def _fetch(ts_code: str, date_str: str) -> float | None:
        from asset_classifier import classify

        info = classify(ts_code)
        if info.asset_type == "future_cn":
            return fetch_future_mark_price(ts_code, date_str)
        if info.asset_type.startswith("stock"):
            return fetch_stock_mark_price(ts_code, date_str)
        return None

    return _fetch
