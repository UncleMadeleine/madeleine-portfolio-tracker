"""加密货币 provider: 完全独立走加密货币 API, 不与股票数据源混用.

优先 Binance 公开 REST API (spot, 无需 key), 失败降级 yfinance 直连
(OpenBB equity.quote 对 crypto 缺 last_price, 必须直连)。
代码规范: BASE-QUOTE (BTC-USD / ETH-USDT), 计价货币见 symbols._CRYPTO_QUOTES。
"""
from __future__ import annotations

import time

import pandas as pd

from ..symbols import ParsedSymbol
from ..util import with_timeout
from .base import Provider, Quote

_BINANCE_HOSTS = ("https://api.binance.com", "https://data-api.binance.vision")
_HTTP_TIMEOUT = 8.0
_KLINES_LIMIT = 1000

# 实时行情进程内缓存: 短 TTL, 批量混查同币对时合并请求
_spot_ttl = 5.0
_spot_cache: dict[str, tuple[float, dict]] = {}


def _requests():
    import requests

    return requests


def binance_pair(p: ParsedSymbol) -> str:
    """BTC-USD → BTCUSDT 形态的 Binance 交易对符号."""
    base, _, quote = p.yahoo.rpartition("-")
    if not base:
        raise ValueError(f"无效加密货币代码: {p.yahoo}")
    q = {"USD": "USDT", "USDT": "USDT", "USDC": "USDC", "BUSD": "BUSD"}.get(quote.upper(), quote.upper())
    return f"{base.upper()}{q}"


def _get(path: str, params: dict | None = None):
    req = _requests()
    last_err: Exception | None = None
    for host in _BINANCE_HOSTS:
        try:
            r = req.get(f"{host}{path}", params=params, timeout=_HTTP_TIMEOUT)
            if r.status_code == 200:
                return r.json()
            last_err = RuntimeError(f"Binance {r.status_code}: {r.text[:120]}")
        except Exception as e:  # noqa: BLE001 - 尝试下一个域名
            last_err = e
    raise RuntimeError(f"Binance API 不可达 ({last_err})")


# ---------- 实时行情 ----------


def _binance_spot_raw(pair: str) -> dict:
    """24hr ticker (含 lastPrice/prevClosePrice/priceChangePercent)."""
    now = time.time()
    hit = _spot_cache.get(pair)
    if hit and now - hit[0] < _spot_ttl:
        return hit[1]
    d = _get("/api/v3/ticker/24hr", {"symbol": pair})
    _spot_cache[pair] = (now, d)
    return d


def _binance_quote(p: ParsedSymbol) -> Quote:
    pair = binance_pair(p)
    d = _binance_spot_raw(pair)
    price = float(d["lastPrice"])
    if price <= 0:
        raise RuntimeError(f"{pair}: Binance 无有效最新价")
    prev = None
    try:
        prev = float(d.get("prevClosePrice")) if d.get("prevClosePrice") else None
    except (TypeError, ValueError):
        prev = None
    chg = None
    try:
        chg = float(d["priceChangePercent"]) if d.get("priceChangePercent") not in (None, "") else None
    except (TypeError, ValueError):
        chg = None
    if chg is None and prev:
        chg = (price / prev - 1) * 100
    return Quote(
        symbol=p.yahoo,
        name=pair,
        price=price,
        prev_close=prev,
        change_pct=chg,
        currency=p.currency,
    )


def _yf():
    """惰性导入 yfinance (OpenBB equity.quote 对 crypto 返回 last_price=None, 需直连)."""
    import yfinance

    return yfinance


def _yf_quote(p: ParsedSymbol) -> Quote:
    """yfinance 直连降级: fast_info 含 lastPrice."""
    info = _yf().Ticker(p.yahoo).fast_info
    price = info.get("lastPrice")
    if price is None or not pd.notna(price):
        raise RuntimeError(f"{p.yahoo}: yfinance 无最新价")
    prev = info.get("previousClose")
    chg = None
    if prev and prev > 0:
        chg = (float(price) / float(prev) - 1) * 100
    return Quote(
        symbol=p.yahoo,
        name=None,
        price=float(price),
        prev_close=float(prev) if prev and pd.notna(prev) else None,
        change_pct=float(chg) if chg is not None and pd.notna(chg) else None,
        currency=str(info.get("currency") or p.currency).upper(),
    )


# ---------- 历史K线 ----------


def _binance_history(p: ParsedSymbol, start_date: str, end_date: str | None) -> pd.DataFrame:
    """Binance 1d klines → date/open/high/low/close/volume (升序)."""
    pair = binance_pair(p)
    start_ms = int(pd.Timestamp(start_date, tz="UTC").timestamp() * 1000)
    end_ms = (
        int(pd.Timestamp(end_date, tz="UTC").timestamp() * 1000 + 86_399_000)
        if end_date
        else int(time.time() * 1000)
    )
    rows: list[dict] = []
    cursor = start_ms
    while cursor < end_ms:
        batch = _get(
            "/api/v3/klines",
            {"symbol": pair, "interval": "1d", "startTime": cursor, "endTime": end_ms, "limit": _KLINES_LIMIT},
        )
        if not batch:
            break
        for k in batch:
            rows.append(
                {
                    "date": pd.Timestamp(k[0], unit="ms", tz="UTC").date(),
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                    "volume": float(k[5]),
                }
            )
        cursor = int(batch[-1][0]) + 86_400_000
        if len(batch) < _KLINES_LIMIT:
            break
    df = pd.DataFrame(rows)
    if df.empty:
        raise RuntimeError(f"{pair}: Binance 无历史K线")
    df["date"] = pd.to_datetime(df["date"])
    return df


def _yf_history(p: ParsedSymbol, start_date: str, end_date: str | None) -> pd.DataFrame:
    """yfinance (OpenBB) 历史降级: equity.price.historical 支持 crypto."""
    obb_mod = _obb()
    kwargs = {"symbol": p.yahoo, "provider": "yfinance", "start_date": start_date}
    if end_date:
        kwargs["end_date"] = end_date
    res = obb_mod.equity.price.historical(**kwargs)
    df = res.to_dataframe().reset_index()
    df = df.rename(columns={df.columns[0]: "date"})
    df = df.dropna(subset=["close"])
    keep = [c for c in ("date", "open", "high", "low", "close", "volume") if c in df.columns]
    return df[keep]


def _obb():
    from openbb import obb

    return obb


class CryptoProvider(Provider):
    """加密货币域: Binance 优先, yfinance 兜底; 与股票数据源完全隔离."""

    name = "crypto"

    def quote_sources(self, p: ParsedSymbol, prefer_first: bool = False) -> list:
        return [_binance_quote, _yf_quote]

    def history_sources(self, p: ParsedSymbol, start_date: str, end_date: str | None, prefer_first: bool = False) -> list:
        return [
            lambda: _binance_history(p, start_date, end_date),
            lambda: _yf_history(p, start_date, end_date),
        ]
