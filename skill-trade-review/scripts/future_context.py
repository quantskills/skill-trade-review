"""期货市场环境拉取（主力合约日线 + 基差 + 持仓 + 仓单 + 多周期技术指标）。"""
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


def load_future_context(variety: str, start_yyyymmdd: str, end_yyyymmdd: str) -> dict:
    """拉某品种的期货市场环境，返回 dict。所有接口失败都做容错降级。"""
    import panda_data
    import pandas as pd

    out: dict = {"variety": variety}

    # --- 1. 主力合约日线（≥ 80 日） ---
    daily_start = _shift_yyyymmdd(start_yyyymmdd, -150)
    dom_df = None
    try:
        dom = panda_data.get_future_dominant(underlying_symbol=[variety],
                                             start_date=daily_start,
                                             end_date=end_yyyymmdd)
        if dom is not None and not dom.empty:
            dom = dom.sort_values("date").reset_index(drop=True)
            out["dominant_symbol"] = str(dom["symbol"].iloc[-1]) if "symbol" in dom.columns else None
            if "symbol" in dom.columns:
                symbols = dom["symbol"].dropna().astype(str).unique().tolist()
                if symbols:
                    px = panda_data.get_future_daily(
                        symbol=symbols,
                        start_date=daily_start,
                        end_date=end_yyyymmdd,
                    )
                    if px is not None and not px.empty:
                        merge_key = "date" if "date" in px.columns else ("trade_date" if "trade_date" in px.columns else None)
                        symbol_key = "symbol" if "symbol" in px.columns else None
                        if merge_key and symbol_key:
                            dom_df = dom.merge(
                                px,
                                left_on=["date", "symbol"],
                                right_on=[merge_key, symbol_key],
                                how="left",
                            )
                            dom_df = dom_df.sort_values("date").reset_index(drop=True)
            if dom_df is not None and "close" in dom_df.columns and dom_df["close"].notna().any():
                dom_df = dom_df.dropna(subset=["close"]).reset_index(drop=True)
                out["dominant_close"] = float(dom_df["close"].iloc[-1])
                out["dominant_pct_pct"] = _safe_pct_change(dom_df["close"])
                out["volatility_5d"] = _annualized_vol(dom_df["close"].tail(6))
                out["technical_daily"] = all_indicators(dom_df.tail(80))
    except Exception as exc:  # noqa: BLE001
        out["_dominant_error"] = str(exc)

    # --- 2. 周线 ---
    if dom_df is not None and not dom_df.empty and "date" in dom_df.columns:
        try:
            dw = dom_df.copy()
            dw["date"] = pd.to_datetime(dw["date"])
            dw = dw.set_index("date")
            agg = {"close": "last"}
            for c in ("open", "high", "low", "volume"):
                if c in dw.columns:
                    agg[c] = "first" if c == "open" else ("max" if c == "high"
                                                          else "min" if c == "low" else "sum")
            weekly = dw.resample("W-FRI").agg(agg).dropna(subset=["close"])
            if not weekly.empty:
                out["technical_weekly"] = all_indicators(weekly.tail(26))
        except Exception as exc:  # noqa: BLE001
            out["_weekly_error"] = str(exc)

    # --- 3. 最后一交易日 15min（仅给最后一日） ---
    try:
        last_day = end_yyyymmdd
        minute_symbol = out.get("dominant_symbol") or f"{variety}_DOMINANT"
        m15 = panda_data.get_future_min(symbol=[minute_symbol],
                                        start_date=last_day,
                                        end_date=last_day,
                                        frequency="15m")
        if m15 is not None and not m15.empty:
            m15 = m15.sort_values(m15.columns[0]).reset_index(drop=True)
            out["technical_15min"] = all_indicators(m15.tail(20))
    except Exception:
        pass  # 15min 接口失败不阻塞

    # --- 4. 基差 ---
    try:
        basis = panda_data.get_future_basis(underlying_symbol=[variety],
                                            start_date=start_yyyymmdd,
                                            end_date=end_yyyymmdd)
        if basis is not None and not basis.empty:
            basis = basis.sort_values("date")
            for col in ("basis", "basis_rate"):
                if col in basis.columns:
                    out[col] = float(basis[col].iloc[-1])
    except Exception:
        pass

    # --- 5. 仓单 ---
    try:
        wr = panda_data.get_future_warehouse_receipt(underlying_symbol=[variety],
                                                     start_date=start_yyyymmdd,
                                                     end_date=end_yyyymmdd)
        if wr is not None and not wr.empty:
            for col in ("warehouse_receipt", "receipt"):
                if col in wr.columns:
                    out["warehouse_receipt"] = float(wr[col].iloc[-1])
                    break
    except Exception:
        pass

    # --- 6. 多空持仓比 ---
    try:
        ls = panda_data.get_future_ls_ratio(symbol=[variety],
                                            start_date=start_yyyymmdd,
                                            end_date=end_yyyymmdd)
        if ls is not None and not ls.empty:
            for col in ("ls_ratio", "long_short_ratio"):
                if col in ls.columns:
                    out["ls_ratio"] = float(ls[col].iloc[-1])
                    break
    except Exception:
        pass

    # --- 7. 商品 regime 推断 ---
    basis_val = out.get("basis") or out.get("basis_rate")
    if basis_val is not None:
        if basis_val > 0.005:
            out["commodity_regime"] = "backwardation"
        elif basis_val < -0.005:
            out["commodity_regime"] = "contango"
        else:
            out["commodity_regime"] = "flat"

    return out


def fetch_future_mark_price(ts_code: str, date_str: str) -> float | None:
    """期货 mark_price: 优先拉合约本身, 失败则拉品种主力。"""
    import panda_data

    yyyymmdd = date_str.replace("-", "")
    try:
        df = panda_data.get_future_daily(symbol=[ts_code],
                                         start_date=yyyymmdd, end_date=yyyymmdd)
        if df is not None and not df.empty and "close" in df.columns:
            return float(df["close"].iloc[-1])
    except Exception:
        pass

    try:
        from asset_classifier import classify
        info = classify(ts_code)
        if info.variety:
            dom = panda_data.get_future_dominant(underlying_symbol=[info.variety],
                                                 start_date=yyyymmdd, end_date=yyyymmdd)
            if dom is not None and not dom.empty and "close" in dom.columns:
                return float(dom["close"].iloc[-1])
    except Exception:
        return None
    return None
