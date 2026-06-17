"""股票市场环境拉取（沪深 300 指数 + 北向 + 宽度 + 多周期技术指标）。"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

from technical_indicators import all_indicators


def _safe_pct_change(series) -> float:
    if series is None or len(series) < 2:
        return 0.0
    last, prev = float(series.iloc[-1]), float(series.iloc[-2])
    if prev == 0:
        return 0.0
    return round((last / prev - 1) * 100, 4)


def _annualized_vol(series) -> float:
    if series is None or len(series) < 2:
        return 0.0
    pct = series.pct_change().dropna()
    if pct.empty:
        return 0.0
    return float(pct.std() * math.sqrt(252) * 100)


def _shift_yyyymmdd(yyyymmdd: str, days: int) -> str:
    dt = datetime.strptime(yyyymmdd, "%Y%m%d") + timedelta(days=days)
    return dt.strftime("%Y%m%d")


def load_stock_context(start_yyyymmdd: str, end_yyyymmdd: str, benchmark: str = "000300.SH") -> dict:
    """拉沪深 300 / 北向资金 / 5 日波动 + 多周期技术指标。"""
    import panda_data

    out: dict = {"benchmark": benchmark}

    # --- 1. 日线（≥ 80 个交易日，覆盖 MA60 + 缓冲） ---
    daily_start = _shift_yyyymmdd(start_yyyymmdd, -150)
    try:
        idx = panda_data.get_index_daily(symbol=[benchmark],
                                         start_date=daily_start,
                                         end_date=end_yyyymmdd)
        if idx is not None and not idx.empty:
            idx = idx.sort_values("date").reset_index(drop=True)
            out["index_close"] = float(idx["close"].iloc[-1])
            out["index_pct_pct"] = _safe_pct_change(idx["close"])
            out["index_volatility_5d"] = _annualized_vol(idx["close"].tail(6))
            # 技术指标 — 日线
            out["technical_daily"] = all_indicators(idx.tail(80))
    except Exception as exc:  # noqa: BLE001
        out["_index_error"] = str(exc)

    # --- 2. 周线（最近 26 周 ≈ 半年） ---
    week_start = _shift_yyyymmdd(start_yyyymmdd, -210)
    try:
        idx_w = panda_data.get_index_daily(symbol=[benchmark],
                                           start_date=week_start,
                                           end_date=end_yyyymmdd)
        if idx_w is not None and not idx_w.empty:
            idx_w = idx_w.sort_values("date").set_index("date")
            idx_w.index = pd_to_datetime(idx_w.index)
            weekly = idx_w.resample("W-FRI").agg({
                "open": "first", "high": "max", "low": "min", "close": "last",
                **({"volume": "sum"} if "volume" in idx_w.columns else {}),
            }).dropna(subset=["close"])
            if not weekly.empty:
                out["technical_weekly"] = all_indicators(weekly.tail(26))
    except Exception as exc:  # noqa: BLE001
        out["_weekly_error"] = str(exc)

    # --- 3. 北向资金 ---
    try:
        north = panda_data.get_hsgt_hold(start_date=start_yyyymmdd, end_date=end_yyyymmdd)
        if north is not None and not north.empty and "hold_amount" in north.columns:
            out["north_hold_amount"] = float(north["hold_amount"].sum())
    except Exception:
        pass

    return out


def fetch_stock_mark_price(ts_code: str, date_str: str) -> float | None:
    """补齐未平仓股票的 mark_price: 拉 [date] 当日收盘。"""
    import panda_data

    yyyymmdd = date_str.replace("-", "")
    try:
        df = panda_data.get_stock_daily(symbol=[ts_code], start_date=yyyymmdd, end_date=yyyymmdd)
        if df is not None and not df.empty:
            return float(df["close"].iloc[-1])
    except Exception:
        return None
    return None


def pd_to_datetime(idx):
    """惰性导入 pandas 避免 import 期触发"""
    import pandas as pd
    return pd.to_datetime(idx)
