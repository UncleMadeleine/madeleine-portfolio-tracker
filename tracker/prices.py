"""统一行情获取: OpenBB(yfinance) 为主, A股/港股 akshare 降级路由."""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd

from .symbols import PENCE_CURRENCIES, Market, ParsedSymbol, parse
from . import cache as cache_mod
from .util import with_timeout


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
        raise RuntimeError("empty result")
    q = _quote_from_dump(p, res.results[0].model_dump())
    if q is None:
        raise RuntimeError(f"{p.yahoo}: no price")
    return q


_AK_SPOT_TTL = 60
_AK_TIMEOUT = 6.0
_ak_spot_cache: tuple[float, dict[Market, pd.DataFrame]] = (0.0, {})


def _ak_spot(market: Market) -> pd.DataFrame:
    global _ak_spot_cache
    now = time.time()
    if now - _ak_spot_cache[0] < _AK_SPOT_TTL:
        return _ak_spot_cache[1].get(market, pd.DataFrame())
    ak = _ak()
    tables: dict[Market, pd.DataFrame] = {}
    try:
        tables[Market.CN] = with_timeout(ak.stock_zh_a_spot_em, _AK_TIMEOUT)
    except Exception:
        pass
    try:
        tables[Market.HK] = with_timeout(ak.stock_hk_spot_em, _AK_TIMEOUT)
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


# ---------- 加密货币行情 ----------


def _yf():
    """惰性导入 yfinance (OpenBB equity.quote 对 crypto 返回 last_price=None, 需直连)."""
    import yfinance as yf

    return yf


def _yf_crypto_quote(p: ParsedSymbol) -> Quote:
    """yfinance 直连获取加密货币实时行情 (fast_info 含 lastPrice)."""
    info = _yf().Ticker(p.yahoo).fast_info
    price = info.get("lastPrice")
    if price is None or not pd.notna(price):
        raise RuntimeError(f"{p.yahoo}: yfinance 无最新价")
    prev = info.get("previousClose")
    chg = info.get("yearChange")  # 年涨跌 (小数比例), 非日内涨跌
    # 日内涨跌幅用 lastPrice / previousClose 计算
    if prev and prev > 0:
        chg = (float(price) / float(prev) - 1) * 100
    else:
        chg = None
    return Quote(
        symbol=p.yahoo,
        name=None,
        price=float(price),
        prev_close=float(prev) if prev and pd.notna(prev) else None,
        change_pct=float(chg) if chg is not None and pd.notna(chg) else None,
        currency=str(info.get("currency") or p.currency).upper(),
    )


def _akshare_crypto_quote(p: ParsedSymbol) -> Quote:
    """akshare 降级: crypto_js_spot 返回多交易所聚合行情 (数据可能延迟)."""
    ak = _ak()
    df = with_timeout(ak.crypto_js_spot, _AK_TIMEOUT)
    if df.empty:
        raise RuntimeError("akshare crypto spot 不可用")
    # 交易品种列格式: BTCUSD / ETHUSD (无连字符)
    pair = p.yahoo.replace("-", "")
    col = "交易品种" if "交易品种" in df.columns else df.columns[1]
    row = df[df[col].astype(str).str.upper() == pair]
    if row.empty:
        raise RuntimeError(f"akshare 未找到 {pair}")
    r = row.iloc[0]
    price = float(r["最近报价"])
    chg = r.get("涨跌幅")

    def _num(v):
        try:
            f = float(v)
            return f if pd.notna(f) else None
        except (TypeError, ValueError):
            return None

    return Quote(
        symbol=p.yahoo,
        name=None,
        price=price,
        prev_close=None,
        change_pct=_num(chg),
        currency=p.currency,
    )


def _quote_route(p: ParsedSymbol, prefer_akshare: bool) -> list:
    # 加密货币: yfinance 直连 (OpenBB equity.quote 对 crypto 缺 last_price), akshare 降级
    if p.market is Market.CRYPTO:
        return [_yf_crypto_quote, _akshare_crypto_quote]
    if p.market in (Market.CN, Market.BJ, Market.HK):
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


def get_quotes(
    symbols, prefer_akshare: bool = False, use_ibkr: bool = False
) -> tuple[dict[str, Quote], dict[str, str], list[str]]:
    quotes: dict[str, Quote] = {}
    errors: dict[str, str] = {}
    notes: list[str] = []
    by_yahoo: dict[str, ParsedSymbol] = {}
    for s in symbols:
        try:
            p = parse(s)
            by_yahoo.setdefault(p.yahoo, p)
        except ValueError as e:
            errors[str(s)] = str(e)
    if not by_yahoo:
        return quotes, errors, notes

    # 优先读本地缓存
    cached = cache_mod.get_cached(list(by_yahoo.keys()))
    if cached:
        quotes.update(cached)
    if len(quotes) == len(by_yahoo):
        return quotes, errors, notes

    if use_ibkr:
        try:
            from . import ibkr as ibkr_mod

            ib_quotes, reason = ibkr_mod.get_quotes_ibkr(list(by_yahoo.values()))
            quotes.update(ib_quotes)
            if reason:
                notes.append(f"IBKR 不可用: {reason} (已回退默认数据源)")
        except Exception as e:
            notes.append(f"IBKR 接入异常: {e}")

    for p in list(by_yahoo.values()):
        if p.yahoo not in quotes and prefer_akshare and p.market in (Market.CN, Market.BJ, Market.HK):
            try:
                quotes[p.yahoo] = _akshare_quote(p)
            except Exception:
                pass

    rest = [p for y, p in by_yahoo.items() if y not in quotes]
    if rest:
        # 加密货币单独走 yfinance 直连 (OpenBB equity.batch.quote 对 crypto 缺 last_price)
        crypto_rest = [p for p in rest if p.market is Market.CRYPTO]
        stock_rest = [p for p in rest if p.market is not Market.CRYPTO]
        for p in crypto_rest:
            try:
                quotes[p.yahoo] = _fetch_quote(p, prefer_akshare=False)
            except Exception as e:
                errors[p.yahoo] = str(e)
        if stock_rest:
            batch = _yahoo_batch(stock_rest)
            quotes.update(batch)
            missing = [p for p in stock_rest if p.yahoo not in quotes]
            if missing and len(batch) == 0:
                for p in missing:
                    errors[p.yahoo] = "批量行情获取失败, 请检查网络"
            elif missing:
                for p in missing:
                    try:
                        quotes[p.yahoo] = _fetch_quote(p, prefer_akshare=False)
                    except Exception as e:
                        errors[p.yahoo] = str(e)

    fresh = {sym: q for sym, q in quotes.items() if sym in by_yahoo}
    cache_mod.set_cached(fresh)
    return quotes, errors, notes


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
    if p.market in (Market.CN, Market.BJ):
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


def _akshare_crypto_history(p: ParsedSymbol, months: int) -> pd.DataFrame:
    """akshare 降级: 用 crypto_js_spot 仅能取最新价, 历史数据暂不可用, 直接抛异常回退."""
    raise RuntimeError("akshare 暂不支持加密货币历史数据")


def get_history(symbol: str, months: int = 12, prefer_akshare: bool = False, use_ibkr: bool = False) -> pd.DataFrame:
    p = parse(symbol)
    if use_ibkr:
        try:
            from . import ibkr as ibkr_mod
            ib_hist, reason = ibkr_mod.get_history_ibkr([p], months)
            if p.yahoo in ib_hist:
                return ib_hist[p.yahoo]
            if reason:
                import warnings
                warnings.warn(f"IBKR 历史数据不可用: {reason} (已回退)")
        except Exception as e:
            import warnings
            warnings.warn(f"IBKR 历史数据异常: {e}")
    if p.market in (Market.CN, Market.BJ, Market.HK):
        fns = (
            [_akshare_history, _yahoo_history]
            if prefer_akshare
            else [_yahoo_history, _akshare_history]
        )
    elif p.market is Market.CRYPTO:
        # 加密货币: yfinance 历史数据 (equity.price.historical 支持 crypto), akshare 暂无历史接口
        fns = [_yahoo_history, _akshare_crypto_history]
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


def get_ohlc(
    symbol: str,
    months: int = 12,
    prefer_akshare: bool = False,
    refresh: bool = False,
    use_ibkr: bool = False,
) -> pd.DataFrame:
    """K线日线数据 (date/open/high/low/close/volume, 升序), 带磁盘缓存与清洗.

    惰性导入 charting (plotly 较重), 避免拖慢其它子命令.
    """
    from .charting import clean_ohlc

    p = parse(symbol)
    if not refresh:
        cached = cache_mod.get_ohlc_cached(p.yahoo, months)
        if cached is not None:
            return cached
    df = clean_ohlc(get_history(symbol, months=months, prefer_akshare=prefer_akshare, use_ibkr=use_ibkr))
    if not df.empty:
        cache_mod.set_ohlc_cached(p.yahoo, months, df)
    return df
