"""全球股票 provider: 美股/港股/德英加澳 (IBKR 优先 → yfinance → 港股 akshare 兜底).

覆盖市场: 裸代码=美股, .HK=港股, .DE/.F/.BE/.DU/.HM/.SG/.MU=德股,
.L/.IL/.AL=英股, .TO/.V/.CN/.NE=加股, .AX=澳股。
"""
from __future__ import annotations

import pandas as pd

from ..symbols import PENCE_CURRENCIES, Market, ParsedSymbol, normalize
from ..util import with_timeout
from .base import Provider, Quote

_AK_TIMEOUT = 6.0
# 按市场独立缓存: 某市场接口失败不影响其它市场, 且失败不占用 TTL (可重试)
_ak_spot_cache: dict[Market, tuple[float, pd.DataFrame]] = {}
_AK_SPOT_TTL = 60


def _obb():
    from openbb import obb

    return obb


def _ak():
    import akshare as ak

    return ak


def _pick(d: dict, *keys):
    for k in keys:
        v = d.get(k)
        if v is not None:
            return v
    return None


# ---------- yfinance (OpenBB) 源 ----------


def _quote_from_dump(p: ParsedSymbol, d: dict) -> Quote | None:
    price = _pick(d, "last_price", "price", "close")
    if price is None:
        return None
    prev = _pick(d, "prev_close", "previous_close")
    chg = _pick(d, "change_percent", "percent_change")
    # yfinance 对 LSE 返回的价为便士且 currency=GBp; 若字段缺失, GB 市场按便士处理
    # 避免回退到 p.currency(GBP) 导致价格放大 100 倍
    raw_ccy = _pick(d, "currency")
    if raw_ccy is None:
        raw_ccy = "GBp" if p.market is Market.GB else p.currency
    raw_ccy = str(raw_ccy)
    if raw_ccy in PENCE_CURRENCIES:
        currency = "GBP"
        price = float(price) / 100
        if prev is not None:
            prev = float(prev) / 100
    else:
        currency = raw_ccy.upper()
        price = float(price)
        if prev is not None:
            prev = float(prev)
    if chg is None and prev is not None and prev > 0:
        chg = (price / prev - 1) * 100
    return Quote(
        symbol=p.yahoo,
        name=_pick(d, "name"),
        price=price,
        prev_close=prev,
        change_pct=float(chg) if chg is not None else None,
        currency=currency,
    )


def _yahoo_quote(p: ParsedSymbol) -> Quote:
    res = _obb().equity.price.quote(symbol=p.yahoo, provider="yfinance")
    if not res.results:
        raise RuntimeError(f"{p.yahoo}: yfinance 无行情结果")
    d = res.results[0].model_dump()
    q = _quote_from_dump(p, d)
    if q is None:
        raise RuntimeError(f"{p.yahoo}: yfinance 无有效价格")
    return q


def _yahoo_batch(parsed: list[ParsedSymbol]) -> dict[str, Quote]:
    out: dict[str, Quote] = {}
    if not parsed:
        return out
    by_yahoo = {p.yahoo: p for p in parsed}
    try:
        res = _obb().equity.price.quote(
            symbol=[p.yahoo for p in parsed], provider="yfinance"
        )
        for item in res.results:
            d = item.model_dump()
            p = by_yahoo.get(str(d.get("symbol")))
            if p is None:
                continue
            q = _quote_from_dump(p, d)
            if q is not None:
                out[p.yahoo] = q
    except Exception:
        pass
    return out


def _yahoo_history(p: ParsedSymbol, start_date: str, end_date: str | None = None) -> pd.DataFrame:
    kwargs = {"symbol": p.yahoo, "provider": "yfinance", "start_date": start_date}
    if end_date:
        kwargs["end_date"] = end_date
    res = _obb().equity.price.historical(**kwargs)
    df = res.to_dataframe().reset_index()
    df = df.rename(columns={df.columns[0]: "date"})
    df = df.dropna(subset=["close"])
    keep = [c for c in ("date", "open", "high", "low", "close", "volume") if c in df.columns]
    df = df[keep]
    if p.market is Market.GB:
        for c in ("open", "high", "low", "close"):
            if c in df.columns:
                df[c] = df[c].astype(float) / 100
    return df


# ---------- akshare 源 (港股兜底; A股由 CN provider 专属使用) ----------


def _ak_spot(market: Market) -> pd.DataFrame:
    global _ak_spot_cache
    now = pd.Timestamp.now().timestamp()
    hit = _ak_spot_cache.get(market)
    if hit and now - hit[0] < _AK_SPOT_TTL:
        return hit[1]
    ak = _ak()
    fetcher = {Market.CN: ak.stock_zh_a_spot_em, Market.HK: ak.stock_hk_spot_em}.get(market)
    if fetcher is None:
        return pd.DataFrame()
    try:
        df = with_timeout(fetcher, _AK_TIMEOUT)
    except Exception:
        return pd.DataFrame()
    _ak_spot_cache[market] = (now, df)
    return df


def _akshare_quote(p: ParsedSymbol) -> Quote:
    df = _ak_spot(p.market)
    if df.empty:
        raise RuntimeError(f"akshare spot 不可用 ({p.market.value})")
    code = p.ak_code or ""
    col = "代码" if "代码" in df.columns else df.columns[0]
    row = df[df[col].astype(str) == code]
    if row.empty:
        raise RuntimeError(f"akshare 未找到 {code}")
    r = row.iloc[0]
    price = float(r["最新价"])
    prev = r.get("昨收")
    chg = r.get("涨跌幅")

    def _num(v):
        try:
            f = float(v)
            return f if pd.notna(f) else None
        except (TypeError, ValueError):
            return None

    return Quote(
        symbol=p.yahoo,
        name=str(r.get("名称") or "") or None,
        price=price,
        prev_close=_num(prev),
        change_pct=_num(chg),
        currency=p.currency,
    )


def _akshare_history(p: ParsedSymbol, start_date: str, end_date: str | None = None) -> pd.DataFrame:
    ak = _ak()
    start = start_date.replace("-", "")
    end = end_date.replace("-", "") if end_date else None
    if p.market in (Market.CN, Market.BJ):
        kwargs = {"symbol": p.yahoo.split(".")[0], "period": "daily", "start_date": start, "adjust": "qfq"}
        if end:
            kwargs["end_date"] = end
        df = ak.stock_zh_a_hist(**kwargs)
    else:
        kwargs = {"symbol": p.ak_code, "period": "daily", "start_date": start, "adjust": "qfq"}
        if end:
            kwargs["end_date"] = end
        df = ak.stock_hk_hist(**kwargs)
    df = df.rename(
        columns={
            "日期": "date",
            "开盘": "open",
            "收盘": "close",
            "最高": "high",
            "最低": "low",
            "成交量": "volume",
        }
    )
    df["date"] = pd.to_datetime(df["date"])
    df = df.dropna(subset=["close"])
    keep = [c for c in ("date", "open", "high", "low", "close", "volume") if c in df.columns]
    return df[keep]


class GlobalStocksProvider(Provider):
    """美股/港股/全球股票域: IBKR 在批量层前置 (orchestration), 此处 yfinance 为主源,
    港股附 akshare 兜底。A股 (.SS/.SZ/.BJ) 不属于本域。"""

    name = "global"

    def quote_sources(self, p: ParsedSymbol, prefer_first: bool = False) -> list:
        if p.market is Market.HK:
            # prefer_first=True (--akshare / 国内网络): akshare 在前
            if prefer_first:
                return [_akshare_quote, _yahoo_quote]
            return [_yahoo_quote, _akshare_quote]
        return [_yahoo_quote]

    def history_sources(self, p: ParsedSymbol, start_date: str, end_date: str | None, prefer_first: bool = False) -> list:
        if p.market is Market.HK:
            if prefer_first:
                return [
                    lambda: _akshare_history(p, start_date, end_date),
                    lambda: _yahoo_history(p, start_date, end_date),
                ]
            return [
                lambda: _yahoo_history(p, start_date, end_date),
                lambda: _akshare_history(p, start_date, end_date),
            ]
        return [lambda: _yahoo_history(p, start_date, end_date)]
