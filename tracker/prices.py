"""统一行情获取: OpenBB(yfinance) 为主, A股/港股 akshare 降级路由."""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd

from .symbols import PENCE_CURRENCIES, Market, ParsedSymbol, parse


@dataclass
class Quote:
    symbol: str
    name: str | None
    price: float
    prev_close: float | None
    change_pct: float | None
    currency: str


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


def _quote_from_dump(p: ParsedSymbol, d: dict) -> Quote | None:
    price = _pick(d, "last_price", "price", "close")
    if price is None:
        return None
    prev = _pick(d, "prev_close", "previous_close")
    chg = _pick(d, "change_percent", "percent_change")
    raw_ccy = str(_pick(d, "currency") or p.currency)
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
        raise RuntimeError("empty result")
    q = _quote_from_dump(p, res.results[0].model_dump())
    if q is None:
        raise RuntimeError(f"{p.yahoo}: no price")
    return q


_AK_SPOT_TTL = 60
_ak_spot_cache: tuple[float, dict[Market, pd.DataFrame]] = (0.0, {})


def _ak_spot(market: Market) -> pd.DataFrame:
    global _ak_spot_cache
    now = time.time()
    if now - _ak_spot_cache[0] < _AK_SPOT_TTL:
        return _ak_spot_cache[1].get(market, pd.DataFrame())
    ak = _ak()
    tables: dict[Market, pd.DataFrame] = {}
    try:
        tables[Market.CN] = ak.stock_zh_a_spot_em()
    except Exception:
        pass
    try:
        tables[Market.HK] = ak.stock_hk_spot_em()
    except Exception:
        pass
    _ak_spot_cache = (now, tables)
    return tables.get(market, pd.DataFrame())


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


def _quote_route(p: ParsedSymbol, prefer_akshare: bool) -> list:
    if p.market in (Market.CN, Market.HK):
        if prefer_akshare:
            return [_akshare_quote, _yahoo_quote]
        return [_yahoo_quote, _akshare_quote]
    return [_yahoo_quote]


def _fetch_quote(p: ParsedSymbol, prefer_akshare: bool = False) -> Quote:
    last_err: Exception | None = None
    for fn in _quote_route(p, prefer_akshare):
        try:
            return fn(p)
        except Exception as e:
            last_err = e
    raise RuntimeError(f"行情获取失败 ({last_err})")


def get_quote(symbol: str, prefer_akshare: bool = False) -> Quote:
    return _fetch_quote(parse(symbol), prefer_akshare)


def get_quotes(symbols, prefer_akshare: bool = False) -> tuple[dict[str, Quote], dict[str, str]]:
    quotes: dict[str, Quote] = {}
    errors: dict[str, str] = {}
    by_yahoo: dict[str, ParsedSymbol] = {}
    for s in symbols:
        try:
            p = parse(s)
            by_yahoo.setdefault(p.yahoo, p)
        except ValueError as e:
            errors[str(s)] = str(e)
    if not by_yahoo:
        return quotes, errors

    for p in list(by_yahoo.values()):
        if prefer_akshare and p.market in (Market.CN, Market.HK):
            try:
                quotes[p.yahoo] = _akshare_quote(p)
            except Exception:
                pass

    rest = [p for y, p in by_yahoo.items() if y not in quotes]
    if rest:
        try:
            res = _obb().equity.price.quote(
                symbol=[p.yahoo for p in rest], provider="yfinance"
            )
            for item in res.results:
                d = item.model_dump()
                sym = d.get("symbol")
                p = by_yahoo.get(str(sym))
                if p is None:
                    continue
                q = _quote_from_dump(p, d)
                if q is not None:
                    quotes[p.yahoo] = q
        except Exception:
            pass

        missing = [p for p in rest if p.yahoo not in quotes]
        if missing:
            time.sleep(2.0)
            for p in missing:
                try:
                    quotes[p.yahoo] = _fetch_quote(p, prefer_akshare=False)
                except Exception as e:
                    errors[p.yahoo] = str(e)
    return quotes, errors


def _yahoo_history(p: ParsedSymbol, months: int) -> pd.DataFrame:
    start = (date.today() - timedelta(days=months * 31)).isoformat()
    res = _obb().equity.price.historical(
        symbol=p.yahoo, provider="yfinance", start_date=start
    )
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


def _akshare_history(p: ParsedSymbol, months: int) -> pd.DataFrame:
    ak = _ak()
    start = (date.today() - timedelta(days=months * 31)).strftime("%Y%m%d")
    if p.market is Market.CN:
        df = ak.stock_zh_a_hist(
            symbol=p.yahoo.split(".")[0], period="daily", start_date=start, adjust="qfq"
        )
    else:
        df = ak.stock_hk_hist(
            symbol=p.ak_code, period="daily", start_date=start, adjust="qfq"
        )
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


def get_history(symbol: str, months: int = 12, prefer_akshare: bool = False) -> pd.DataFrame:
    p = parse(symbol)
    if p.market in (Market.CN, Market.HK):
        fns = (
            [_akshare_history, _yahoo_history]
            if prefer_akshare
            else [_yahoo_history, _akshare_history]
        )
    else:
        fns = [_yahoo_history]
    last_err: Exception | None = None
    for fn in fns:
        try:
            df = fn(p, months)
            if not df.empty:
                return df
        except Exception as e:
            last_err = e
    raise RuntimeError(f"{p.yahoo}: 历史数据获取失败 ({last_err})")
