"""汇率获取: CFETS(akshare) 优先, OpenBB(yfinance) 货币对兜底."""
from __future__ import annotations

import math
import time
from datetime import date, timedelta

from .util import with_timeout

_PAIR_TTL = 600
_pair_cache: dict[str, tuple[float, float]] = {}

_CFETS_TTL = 120
_CFETS_TIMEOUT = 8.0
_cfets_cache: tuple[float, dict[str, float]] = (0.0, {})


def _obb():
    from openbb import obb

    return obb


def _yahoo_pair(pair: str) -> float | None:
    now = time.time()
    hit = _pair_cache.get(pair)
    if hit and now - hit[1] < _PAIR_TTL:
        return hit[0]
    try:
        start = (date.today() - timedelta(days=30)).isoformat()
        res = _obb().currency.price.historical(
            symbol=pair, provider="yfinance", start_date=start
        )
        df = res.to_dataframe()
        if "close" not in df.columns or df.empty:
            return None
        s = df["close"].dropna()
        if s.empty:
            return None
        rate = float(s.iloc[-1])
    except Exception:
        return None
    _pair_cache[pair] = (rate, now)
    return rate


def _pair_rate(a: str, b: str) -> float | None:
    rate = _yahoo_pair(f"{a}{b}=X")
    if rate:
        return rate
    rate = _yahoo_pair(f"{b}{a}=X")
    if rate:
        return 1.0 / rate
    return None


def _cfets_table() -> dict[str, float]:
    global _cfets_cache
    now = time.time()
    if _cfets_cache[1] and now - _cfets_cache[0] < _CFETS_TTL:
        return _cfets_cache[1]
    import akshare as ak

    df = with_timeout(ak.fx_spot_quote, _CFETS_TIMEOUT)
    out: dict[str, float] = {"CNY": 1.0}
    for _, row in df.iterrows():
        parts = str(row["货币对"]).split("/")
        if len(parts) != 2:
            continue
        try:
            mid = (float(row["买报价"]) + float(row["卖报价"])) / 2
        except (TypeError, ValueError):
            continue
        if not math.isfinite(mid):
            continue
        base, quote = parts
        if base == "100JPY":
            out["JPY"] = mid / 100
        elif quote == "CNY":
            out[base] = mid
    _cfets_cache = (now, out)
    return out


def _cfets_rate(src: str, dst: str) -> float | None:
    try:
        table = _cfets_table()
    except Exception:
        return None
    if src not in table or dst not in table:
        return None
    r = table[src] / table[dst]
    return r if math.isfinite(r) else None


def _finite(r: float | None) -> float | None:
    if r is not None and math.isfinite(r):
        return r
    return None


def get_rate(src: str, dst: str, use_ibkr: bool = False) -> float | None:
    src, dst = src.upper(), dst.upper()
    if src == dst:
        return 1.0
    if use_ibkr:
        try:
            from . import ibkr as ibkr_mod
            rate, reason = ibkr_mod.get_fx_rate_ibkr(src, dst)
            if rate is not None:
                return rate
        except Exception:
            pass
    cf = _finite(_cfets_rate(src, dst))
    if cf is not None:
        return cf
    direct = _finite(_pair_rate(src, dst))
    if direct is not None:
        return direct
    if src != "USD" and dst != "USD":
        r1 = _finite(_pair_rate(src, "USD"))
        r2 = _finite(_pair_rate("USD", dst))
        if r1 is not None and r2 is not None:
            return r1 * r2
    return None


def get_fx_rates(base: str, currencies, use_ibkr: bool = False) -> tuple[dict[str, float], list[str]]:
    base = base.upper()
    rates: dict[str, float] = {}
    missing: list[str] = []
    for ccy in dict.fromkeys(str(c).upper() for c in currencies):
        if ccy == base:
            rates[ccy] = 1.0
            continue
        rate = get_rate(ccy, base, use_ibkr=use_ibkr)
        if rate:
            rates[ccy] = rate
        else:
            missing.append(ccy)
    return rates, missing
