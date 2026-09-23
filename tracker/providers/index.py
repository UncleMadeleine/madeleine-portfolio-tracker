"""宏观/风险指数 provider: 独立代码规范 (IX.<KEY>), 与股票数据源不混用.

代码目录见 tracker.symbols.INDEX_CATALOG (IX.DXY 美元指数 / IX.VIX 恐慌指数 /
IX.CSI300 沪深 300 ...)。源链:
- 中国指数: akshare 新浪源优先 (与 A 股域数据源惯例一致) → yfinance 兜底
- 其它指数: yfinance 为主源, 目录中带 ak 代码的走 akshare 新浪源兜底

指数无实时行情需求 (不参与持仓/自选聚合), 仅提供历史 K 线; 新增数据源 =
追加一个 _xxx_history 函数并插入 history_sources 源链。
中国水泥网 (IX.CEMPI 等, 目录条目带 ccement 字段): 前端 AJAX 接口免登录,
仅全国口径; 区域分解与水泥大数据中心需会员, 不接入。
"""
from __future__ import annotations

import json

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


# ---------- 中国水泥网源 (index.ccement.com) ----------

_CCEMENT_BASE = "https://index.ccement.com"
_CCEMENT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://index.ccement.com/",
}
_CCEMENT_TIMEOUT = 15.0


def _ccement_session():
    import requests

    return requests


def _ccement_post(path: str, data: dict) -> dict:
    """POST 水泥网 AJAX 端点, Code==200 校验后返回 Data 字段."""
    requests = _ccement_session()
    r = requests.post(
        f"{_CCEMENT_BASE}/index/{path}",
        headers=_CCEMENT_HEADERS,
        data=data,
        timeout=_CCEMENT_TIMEOUT,
    )
    r.raise_for_status()
    j = r.json()
    if j.get("Code") != 200:
        raise RuntimeError(f"水泥网接口返回异常: {j.get('Msg')} ({path})")
    return j["Data"]


def _ccement_points(d: dict) -> pd.DataFrame:
    """dynamicIndexDate + dynamicIndex(All) 点位序列 → 日线 DataFrame (volume=0).

    独立端点用 dynamicIndex; getPriceIndex 聚合载荷用 dynamicIndexAll。
    """
    dates = d.get("dynamicIndexDate") or []
    vals = d.get("dynamicIndexAll") or d.get("dynamicIndex") or []
    if isinstance(vals, str):
        vals = json.loads(vals)
    if not dates or not vals:
        raise RuntimeError("水泥网接口返回空序列")
    df = pd.DataFrame({"date": pd.to_datetime(dates), "close": [float(v) for v in vals]})
    df["open"] = df["high"] = df["low"] = df["close"]
    df["volume"] = 0.0
    return df


def _ccement_kline(d: dict | str) -> pd.DataFrame:
    """cementkline 周K 行 [ts, open, high, low, close, prev_close, chg, chg_pct] → DataFrame."""
    rows = json.loads(d) if isinstance(d, str) else d
    if not rows:
        raise RuntimeError("水泥网接口返回空K线")
    df = pd.DataFrame(
        rows,
        columns=["ts", "open", "high", "low", "close", "prev_close", "chg", "chg_pct"],
    )
    df["date"] = pd.to_datetime(df["ts"], unit="ms")
    df["volume"] = 0.0
    return df[["date", "open", "high", "low", "close", "volume"]]



def _ccement_history(p: ParsedSymbol, start_date: str, end_date: str | None = None) -> pd.DataFrame:
    """目录条目 ccement 字段路由到对应端点; timeType=5 取全部历史, 本地过滤."""
    kind = _catalog(p)["ccement"]
    if kind == "kline":
        # CEMPI 主指数: 周K OHLC (日线无 OHLC, 蜡烛图用周K)。
        # timeType=5 会忽略 start/end 参数返回全部历史, 本地按窗口切片。
        d = _ccement_post(
            "priceindex/cementkline",
            {"start_time": start_date, "end_time": end_date or "", "areaV": "country", "timeType": "5"},
        )
        return _slice_range(_ccement_kline(d), start_date, end_date)
    if kind == "coal":
        # CCPDI 煤价差: getPriceIndex 中 coal_price 序列
        d = _ccement_post("priceindex/getPriceIndex", {"timeType": "5"})
        return _slice_range(_ccement_points(d["coal_price"]), start_date, end_date)
    if kind == "priceindex/po425zsline":
        d = _ccement_post(kind, {"areaV": "country", "timeType": "5"})
        return _slice_range(_ccement_points(d), start_date, end_date)
    # 熟料/混凝土/碎石/机制砂/砂浆: 各自端点, areaV=country + indexSign=1
    d = _ccement_post(kind, {"areaV": "country", "indexSign": "1", "timeType": "5"})
    return _slice_range(_ccement_points(d), start_date, end_date)


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

        def cc():
            return _ccement_history(p, start_date, end_date)

        # 水泥网指数: 目录带 ccement 字段, 专属源 (无第二数据源)
        if _catalog(p).get("ccement"):
            return [cc]
        ak_supported = bool(_catalog(p).get("ak")) or index_key(p.yahoo) in _AK_US_SINA
        if not ak_supported:
            return [yf]
        # 中国指数: akshare 优先 (与 A 股域惯例一致); 其余 yfinance 主源
        if prefer_first or _catalog(p).get("ak"):
            return [ak, yf]
        return [yf, ak]
