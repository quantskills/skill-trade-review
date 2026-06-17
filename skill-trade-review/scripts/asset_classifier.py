"""品种自动识别 — 从 ts_code 推断 asset_type 和默认基准。"""
from __future__ import annotations

import re
from dataclasses import dataclass

# 期货品种 → 合约乘数 / 默认基准
FUTURE_SPECS = {
    # 中金所
    "IF": {"multiplier": 300, "benchmark": "000300.SH", "exchange": "CFE"},
    "IH": {"multiplier": 300, "benchmark": "000016.SH", "exchange": "CFE"},
    "IC": {"multiplier": 200, "benchmark": "000905.SH", "exchange": "CFE"},
    "IM": {"multiplier": 200, "benchmark": "000852.SH", "exchange": "CFE"},
    "T":  {"multiplier": 10000, "benchmark": "T.CFE",   "exchange": "CFE"},
    "TF": {"multiplier": 10000, "benchmark": "TF.CFE",  "exchange": "CFE"},
    # 上期所黑色
    "RB": {"multiplier": 10, "benchmark": "RB.SHFE", "exchange": "SHFE"},
    "HC": {"multiplier": 10, "benchmark": "HC.SHFE", "exchange": "SHFE"},
    "I":  {"multiplier": 100, "benchmark": "I.DCE",  "exchange": "DCE"},
    # 上期所有色
    "CU": {"multiplier": 5,  "benchmark": "CU.SHFE", "exchange": "SHFE"},
    "AL": {"multiplier": 5,  "benchmark": "AL.SHFE", "exchange": "SHFE"},
    "ZN": {"multiplier": 5,  "benchmark": "ZN.SHFE", "exchange": "SHFE"},
    "NI": {"multiplier": 1,  "benchmark": "NI.SHFE", "exchange": "SHFE"},
    "AU": {"multiplier": 1000, "benchmark": "AU.SHFE", "exchange": "SHFE"},
    "AG": {"multiplier": 15, "benchmark": "AG.SHFE", "exchange": "SHFE"},
    # 能化
    "SC": {"multiplier": 1000, "benchmark": "SC.INE", "exchange": "INE"},
    # 农产品
    "M":  {"multiplier": 10, "benchmark": "M.DCE",  "exchange": "DCE"},
    "Y":  {"multiplier": 10, "benchmark": "Y.DCE",  "exchange": "DCE"},
    "P":  {"multiplier": 10, "benchmark": "P.DCE",  "exchange": "DCE"},
    "C":  {"multiplier": 10, "benchmark": "C.DCE",  "exchange": "DCE"},
    # 化工
    "MA": {"multiplier": 10, "benchmark": "MA.CZCE", "exchange": "CZCE"},
    "TA": {"multiplier": 5,  "benchmark": "TA.CZCE", "exchange": "CZCE"},
    "PP": {"multiplier": 5,  "benchmark": "PP.DCE",  "exchange": "DCE"},
    "L":  {"multiplier": 5,  "benchmark": "L.DCE",   "exchange": "DCE"},
    "V":  {"multiplier": 5,  "benchmark": "V.DCE",   "exchange": "DCE"},
}


@dataclass
class AssetInfo:
    asset_type: str          # stock_a / stock_hk / stock_us / future_cn / unknown
    variety: str | None      # 期货品种代码 (RB / IF / ...)，股票为 None
    multiplier: float        # 合约乘数；股票=1
    benchmark: str           # 默认基准代码
    exchange: str | None


_RE_STOCK_A = re.compile(r"^\d{6}\.(SZ|SH|BJ)$", re.IGNORECASE)
_RE_STOCK_HK = re.compile(r"^\d{4,5}\.HK$", re.IGNORECASE)
_RE_STOCK_US = re.compile(r"^[A-Z]{1,5}(\.US)?$")
_RE_FUTURE_CONTRACT = re.compile(
    r"^([A-Z]{1,2})\d{3,4}\.(CFE|SHFE|DCE|CZCE|INE|GFEX)$", re.IGNORECASE
)
_RE_FUTURE_VARIETY = re.compile(
    r"^([A-Z]{1,2})\.(CFE|SHFE|DCE|CZCE|INE|GFEX)$", re.IGNORECASE
)


def classify(ts_code: str) -> AssetInfo:
    """根据 ts_code 推断资产类型与合约元信息。"""
    code = (ts_code or "").strip()

    if not code:
        return AssetInfo("unknown", None, 1.0, "000300.SH", None)

    # 期货合约（带年月）或品种连续合约
    for regex in (_RE_FUTURE_CONTRACT, _RE_FUTURE_VARIETY):
        m = regex.match(code)
        if m:
            variety = m.group(1).upper()
            exchange = m.group(2).upper()
            spec = FUTURE_SPECS.get(variety)
            if spec:
                return AssetInfo(
                    asset_type="future_cn",
                    variety=variety,
                    multiplier=float(spec["multiplier"]),
                    benchmark=spec["benchmark"],
                    exchange=exchange,
                )
            # 无字典命中：仍归为期货但 multiplier 缺省 1，并由上游 warning
            return AssetInfo("future_cn", variety, 1.0, "000300.SH", exchange)

    if _RE_STOCK_A.match(code):
        return AssetInfo("stock_a", None, 1.0, "000300.SH", code.split(".")[-1].upper())
    if _RE_STOCK_HK.match(code):
        return AssetInfo("stock_hk", None, 1.0, "HSI.HK", "HK")
    if _RE_STOCK_US.match(code):
        return AssetInfo("stock_us", None, 1.0, "SPX.US", "US")

    return AssetInfo("unknown", None, 1.0, "000300.SH", None)


def classify_batch(ts_codes: list[str]) -> dict[str, AssetInfo]:
    """批量识别，返回 ts_code -> AssetInfo 映射。"""
    return {code: classify(code) for code in ts_codes}
