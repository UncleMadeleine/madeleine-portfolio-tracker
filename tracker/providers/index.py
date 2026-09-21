"""宏观/风险指数 provider: 独立代码规范 (IX.<KEY>), 与股票数据源不混用.

代码目录见 tracker.symbols.INDEX_CATALOG (IX.DXY 美元指数 / IX.VIX 恐慌指数 /
IX.CSI300 沪深 300 ...)。源链:
- 中国指数: akshare 新浪源优先 (与 A 股域数据源惯例一致) → yfinance 兜底
- 其它指数: yfinance 为主源, 目录中带 ak 代码的走 akshare 新浪源兜底

指数无实时行情需求 (不参与持仓/自选聚合), 仅提供历史 K 线; 新增数据源 =
追加一个 _xxx_history 函数并插入 history_sources 源链。
"""
from __future__ import annotations

import pandas as pd

from ..symbols import INDEX_CATALOG, ParsedSymbol, index_key
from .base import Provider, Quote

__all__ = ["IndexProvider"]


def _obb():
    from openbb import obb

    return obb


def _ak():
    import akshare as ak

    return ak


def _catalog(p: ParsedSymbol) -> dict:
    """ParsedSymbol → 指数目录条目 (parse 已保证 key 合法)."""
    return INDEX_CATALOG[index_key(p.yahoo)]


def _slice_range(df: pd.DataFrame, start_date: str, end_date: str | None) -> pd.DataFrame:
    """akshare 返回全量历史, 本地按 start/end 过滤 (升序).

    end_date 为闭区间端点 (与调用方语义一致): 只取 <= end_date 的行,
    不能再 +1 天, 否则结束日会多返回一根 K 线。
    """
    out = df.copy()
    out["date"] = pd.to_datetime(out["date"]).dt.tz_localize(None)
    mask = out["date"] >= pd.Timestamp(start_date)
    if end_date:
        mask &= out["date"] <= pd.Timestamp(end_date)
    return out[mask].sort_values("date").reset_index(drop=True)


# ---------- yfinance (OpenBB) 源 ----------


def _yf_history(p: ParsedSymbol, start_date: str, end_date: str | None = None) -> pd.DataFrame:
    yf_symbol = _catalog(p)["yf"]
    kwargs = {"symbol": yf_symbol, "provider": "yfinance", "start_date": start_date}
    if end_date:
        kwargs["end_date"] = end_date
    df = _obb().equity.price.historical(**kwargs).to_dataframe().reset_index()
    df = df.rename(columns={df.columns[0]: "date"})
    df = df.dropna(subset=["close"])
    keep = [c for c in ("date", "open", "high", "low", "close", "volume") if c in df.columns]
    if "volume" not in keep:
        df["volume"] = 0.0
        keep.append("volume")
    return df[keep]


# ---------- akshare 源 (新浪指数) ----------


def _ak_index_history(p: ParsedSymbol, start_date: str, end_date: str | None = None) -> pd.DataFrame:
    entry = _catalog(p)
    ak_code = entry.get("ak") or _AK_US_SINA.get(index_key(p.yahoo))
    if not ak_code:
        raise ValueError(f"{p.yahoo}: akshare 源未收录该指数")
    if ak_code.startswith(("sh", "sz")):
        raw = _ak().stock_zh_index_daily(symbol=ak_code)
    else:
        raw = _ak().index_us_stock_sina(symbol=ak_code)
    df = raw.rename(columns={raw.columns[0]: "date"})
    keep = [c for c in ("date", "open", "high", "low", "close", "volume") if c in df.columns]
    if "volume" not in keep:
        df["volume"] = 0.0
        keep.append("volume")
    return _slice_range(df[keep], start_date, end_date)


# 美股主要指数的新浪代码 (index_us_stock_sina); 标普 500 新浪用 .INX
_AK_US_SINA = {
    "SPX": ".INX",
    "NDX": ".IXIC",
    "DJI": ".DJI",
    "RUT": ".RUT",
}


class IndexProvider(Provider):
    """宏观/风险指数域: 中国指数 akshare 优先, 其余 yfinance 主源 + 新浪兜底."""

    name = "index"

    def quote_sources(self, p: ParsedSymbol, prefer_first: bool = False) -> list:
        raise NotImplementedError("指数域仅提供历史K线, 无实时行情")

    def history_sources(self, p: ParsedSymbol, start_date: str, end_date: str | None, prefer_first: bool = False) -> list:
        def ak():
            return _ak_index_history(p, start_date, end_date)

        def yf():
            return _yf_history(p, start_date, end_date)

        ak_supported = bool(_catalog(p).get("ak")) or index_key(p.yahoo) in _AK_US_SINA
        if not ak_supported:
            return [yf]
        # 中国指数: akshare 优先 (与 A 股域惯例一致); 其余 yfinance 主源
        if prefer_first or _catalog(p).get("ak"):
            return [ak, yf]
        return [yf, ak]
