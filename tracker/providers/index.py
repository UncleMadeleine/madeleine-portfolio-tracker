"""宏观/风险指数 provider: 独立代码规范 (IX.<KEY>), 与股票数据源不混用.

代码目录见 tracker.symbols.INDEX_CATALOG (IX.DXY 美元指数 / IX.VIX 恐慌指数 /
IX.CSI300 沪深 300 ...)。源链:
- 中国指数: akshare 新浪源优先 (与 A 股域数据源惯例一致) → yfinance 兜底
- 其它指数: yfinance 为主源, 目录中带 ak 代码的走 akshare 新浪源兜底

指数无实时行情需求 (不参与持仓/自选聚合), 仅提供历史 K 线; 新增数据源 =
追加一个 _xxx_history 函数并插入 history_sources 源链。
中国水泥网 (IX.CEMPI 等, 目录条目带 ccement 字段): 前端 AJAX 接口免登录,
仅全国口径; 区域分解与水泥大数据中心需会员, 不接入。
恒生指数公司 (IX.HSAHP, 目录条目带 hsi 字段): 官网公开 API 零鉴权,
固定滚动约 5 年日收盘, 无开高低 (平K线)。
商品期货 (IX.LC 等, 目录条目带 futures 字段): akshare futures_main_sina
新浪主力连续 (LC0=广期所碳酸锂), 返回全量历史后本地切片; 主力连续含换月
跳空 (新浪标准构造), 单合约历史可改用 futures_zh_daily_sina (未接入)。
"""

from __future__ import annotations

import json

import pandas as pd

from ..symbols import INDEX_CATALOG, ParsedSymbol, index_key
from .base import Provider

__all__ = ["IndexProvider"]


def _obb():
    from openbb import obb

    return obb


def _ak():
    import akshare as ak

    return ak


def _requests():
    import requests

    return requests


def _catalog(p: ParsedSymbol) -> dict:
    """ParsedSymbol → 指数目录条目 (parse 已保证 key 合法)."""
    return INDEX_CATALOG[index_key(p.yahoo)]


def _slice_range(
    df: pd.DataFrame, start_date: str, end_date: str | None
) -> pd.DataFrame:
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


def _ccement_post(path: str, data: dict) -> dict:
    """POST 水泥网 AJAX 端点, Code==200 校验后返回 Data 字段."""
    requests = _requests()
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
    df = pd.DataFrame(
        {"date": pd.to_datetime(dates), "close": [float(v) for v in vals]}
    )
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


def _ccement_history(
    p: ParsedSymbol, start_date: str, end_date: str | None = None
) -> pd.DataFrame:
    """目录条目 ccement 字段路由到对应端点; timeType=5 取全部历史, 本地过滤."""
    kind = _catalog(p)["ccement"]
    if kind == "kline":
        # CEMPI 主指数: 周K OHLC (日线无 OHLC, 蜡烛图用周K)。
        # timeType=5 会忽略 start/end 参数返回全部历史, 本地按窗口切片。
        d = _ccement_post(
            "priceindex/cementkline",
            {
                "start_time": start_date,
                "end_time": end_date or "",
                "areaV": "country",
                "timeType": "5",
            },
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


# ---------- 恒生指数公司官方 API (hsi.com.hk) ----------

# valueHistoryPub: 公开零鉴权 (无需 cookie/token/验证码, 只要 Referer 头),
# 固定返回滚动约 5 年日收盘; 不接受日期参数 (多传即 400 Bad Request),
# 故全量取回后本地按窗口切片。接口偏慢 (实测 5~12s), 由
# prices.get_index_history 的 SQLite 缓存兜住 (只付一次)。
_HSI_HISTORY_URL = (
    "https://www.hsi.com.hk/api/wsit-hsi-ddoc-ea-public-website-proxy"
    "/v1/product-data/pub/indexes/metadata/v2"
)
_HSI_REFERER = (
    "https://www.hsi.com.hk/en-hk/indexes/all-indexes/index-details/"
    "?seriesCode=ahpremium"
)
_HSI_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Referer": _HSI_REFERER,
}
_HSI_TIMEOUT = 30.0


def _hsi_history(
    p: ParsedSymbol, start_date: str, end_date: str | None = None
) -> pd.DataFrame:
    """恒生 valueHistoryPub 日收盘序列 → 平K线 (open=high=low=close).

    目录 hsi 字段 = 恒生内部指数代码 (HSAHP → 01044.00, seriesCode=ahpremium)。
    恒生不发布该指数的开/高/低 (实时接口 todayOpn 恒为 null), 故与水泥网
    点位数指数一样作平K线处理, 不虚构振幅。
    """
    r = _requests().get(
        _HSI_HISTORY_URL,
        params={
            "data": "valueHistoryPub",
            "language": "zh-cn",
            "indexCodes": _catalog(p)["hsi"],
        },
        headers=_HSI_HEADERS,
        timeout=_HSI_TIMEOUT,
    )
    r.raise_for_status()
    payload = r.json()
    if payload.get("code") != "0":
        raise RuntimeError(f"恒生接口返回异常: {payload.get('message') or payload}")
    records = (payload.get("data") or [{}])[0].get("valueHistory") or []
    if not records:
        raise RuntimeError("恒生接口返回空序列")
    df = pd.DataFrame(records)
    df["date"] = pd.to_datetime(df["date"])
    df["close"] = df["close"].astype(float)
    df["open"] = df["high"] = df["low"] = df["close"]
    df["volume"] = 0.0
    return _slice_range(
        df[["date", "open", "high", "low", "close", "volume"]], start_date, end_date
    )


# ---------- yfinance (OpenBB) 源 ----------


def _yf_history(
    p: ParsedSymbol, start_date: str, end_date: str | None = None
) -> pd.DataFrame:
    yf_symbol = _catalog(p)["yf"]
    kwargs = {"symbol": yf_symbol, "provider": "yfinance", "start_date": start_date}
    if end_date:
        kwargs["end_date"] = end_date
    df = _obb().equity.price.historical(**kwargs).to_dataframe().reset_index()
    df = df.rename(columns={df.columns[0]: "date"})
    df = df.dropna(subset=["close"])
    keep = [
        c for c in ("date", "open", "high", "low", "close", "volume") if c in df.columns
    ]
    if "volume" not in keep:
        df["volume"] = 0.0
        keep.append("volume")
    return df[keep]


# ---------- akshare 源 (新浪商品期货主力连续) ----------


def _ak_futures_history(
    p: ParsedSymbol, start_date: str, end_date: str | None = None
) -> pd.DataFrame:
    """新浪期货主力连续 (目录 futures 字段, 如 LC0=碳酸锂): 全量历史, 本地切片.

    主力连续按新浪规则拼接 (近月合约序列), 换月处有跳空, 属正常构造;
    单合约历史不在目录口径内 (futures_zh_daily_sina 需指定具体合约月).
    """
    symbol = _catalog(p)["futures"]
    raw = _ak().futures_main_sina(symbol=symbol)
    if raw is None or raw.empty:
        raise RuntimeError(f"主力连续 {symbol}: akshare 返回空数据")
    df = raw.rename(
        columns={
            "日期": "date",
            "开盘价": "open",
            "最高价": "high",
            "最低价": "low",
            "收盘价": "close",
            "成交量": "volume",
        }
    )
    keep = [
        c for c in ("date", "open", "high", "low", "close", "volume") if c in df.columns
    ]
    return _slice_range(df[keep], start_date, end_date)


# ---------- akshare 源 (新浪指数) ----------


def _ak_index_history(
    p: ParsedSymbol, start_date: str, end_date: str | None = None
) -> pd.DataFrame:
    entry = _catalog(p)
    ak_code = entry.get("ak") or _AK_US_SINA.get(index_key(p.yahoo))
    if not ak_code:
        raise ValueError(f"{p.yahoo}: akshare 源未收录该指数")
    if ak_code.startswith(("sh", "sz")):
        raw = _ak().stock_zh_index_daily(symbol=ak_code)
    else:
        raw = _ak().index_us_stock_sina(symbol=ak_code)
    df = raw.rename(columns={raw.columns[0]: "date"})
    keep = [
        c for c in ("date", "open", "high", "low", "close", "volume") if c in df.columns
    ]
    if "volume" not in keep:
        df["volume"] = 0.0
        keep.append("volume")
    return _slice_range(df[keep], start_date, end_date)


# 美股主要指数的新浪代码 (index_us_stock_sina); 标普 500 新浪用 .INX
# 注意: index_us_stock_sina 仅支持 {.IXIC, .DJI, .INX, .NDX}, 故不收 RUT
# (Yahoo 无对应新浪码, 收了也只会在兜底时失败)
_AK_US_SINA = {
    "SPX": ".INX",
    "NDX": ".IXIC",
    "DJI": ".DJI",
}


class IndexProvider(Provider):
    """宏观/风险指数域: 中国指数 akshare 优先, 其余 yfinance 主源 + 新浪兜底.

    商品期货 (futures 字段) 与水泥网 (ccement 字段) 为各自专属源, 无兜底;
    恒生官方 API (hsi 字段) 同理, 官网零鉴权但仅约 5 年滚动窗口。
    """

    name = "index"

    def quote_sources(self, p: ParsedSymbol, prefer_first: bool = False) -> list:
        raise NotImplementedError("指数域仅提供历史K线, 无实时行情")

    def history_sources(
        self,
        p: ParsedSymbol,
        start_date: str,
        end_date: str | None,
        prefer_first: bool = False,
    ) -> list:
        def ak():
            return _ak_index_history(p, start_date, end_date)

        def yf():
            return _yf_history(p, start_date, end_date)

        def cc():
            return _ccement_history(p, start_date, end_date)

        def hs():
            return _hsi_history(p, start_date, end_date)

        def ft():
            return _ak_futures_history(p, start_date, end_date)

        # 水泥网指数: 目录带 ccement 字段, 专属源 (无第二数据源)
        if _catalog(p).get("ccement"):
            return [cc]
        # 恒生指数公司官方 API: 目录带 hsi 字段, 专属源 (无第二数据源)
        if _catalog(p).get("hsi"):
            return [hs]
        # 商品期货: 目录带 futures 字段 (新浪主力连续), 专属源 (无第二数据源)
        if _catalog(p).get("futures"):
            return [ft]
        ak_supported = bool(_catalog(p).get("ak")) or index_key(p.yahoo) in _AK_US_SINA
        if not ak_supported:
            return [yf]
        # 中国指数: akshare 优先 (与 A 股域惯例一致); 其余 yfinance 主源
        if prefer_first or _catalog(p).get("ak"):
            return [ak, yf]
        return [yf, ak]
