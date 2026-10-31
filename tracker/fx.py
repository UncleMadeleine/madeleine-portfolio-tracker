"""汇率获取: CFETS(akshare) 优先, OpenBB(yfinance) 货币对兜底."""

from __future__ import annotations

import math
import time
from datetime import date, timedelta

from .symbols import valuation_currency
from .util import with_timeout

_PAIR_TTL = 600
_pair_cache: dict[str, tuple[float, float]] = {}

_CFETS_TTL = 120
_CFETS_TIMEOUT = 8.0
_cfets_cache: tuple[float, dict[str, float]] = (0.0, {})

# 历史汇率 (比例线汇率调整用): 6 小时进程内缓存, 键含起止日期
_FX_HIST_TTL = 6 * 3600
_fx_hist_cache: dict[tuple[str, str, str, str], tuple[float, object]] = {}


def _obb():
    from openbb import obb

    return obb


def _yahoo_pair(pair: str) -> float | None:
    now = time.time()
    hit = _pair_cache.get(pair)
    if hit and now - hit[1] < _PAIR_TTL:
        return hit[0]
    rate = None
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
        candidate = float(s.iloc[-1])
        # 与 _cfets_table/_yahoo_pair_history/cache.set_fx_cached 同口径挡
        # <=0: 负汇率 (Yahoo 脏收盘) 会一路传到 build_view 的 mv = mv_local*rate,
        # 市值变负、权重归一与盈亏全错; _pair_rate 的 `if rate:` 只挡得住 0
        if math.isfinite(candidate) and candidate > 0:
            rate = candidate
    except Exception:
        return None
    if rate is not None:
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
    if df is None or df.empty:
        _cfets_cache = (now, out)
        return out
    for _, row in df.iterrows():
        parts = str(row["货币对"]).split("/")
        if len(parts) != 2:
            continue
        try:
            mid = (float(row["买报价"]) + float(row["卖报价"])) / 2
        except (TypeError, ValueError):
            continue
        if not math.isfinite(mid) or mid <= 0:
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
    if not table[src] or not table[dst]:
        return None
    r = table[src] / table[dst]
    return r if math.isfinite(r) else None


def _finite(r: float | None) -> float | None:
    if r is not None and math.isfinite(r):
        return r
    return None


def get_rate(src: str, dst: str, use_ibkr: bool = False) -> float | None:
    # 稳定币计价按 USD 等价: USDT/USDC/FDUSD… 与 USD 同值, 先归一再查表。
    # 不归一的后果: CFETS/Yahoo 法币对都没有 USDT→X 通道, 持仓/自选整行
    # 拿不到汇率被排除出视图; portfolio 的 base_currency 是稳定币时同样中招。
    src, dst = valuation_currency(src), valuation_currency(dst)
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


def _yahoo_pair_history(pair: str, start_date: str, end_date: str | None = None):
    """yfinance (OpenBB) 货币对日线 → DataFrame(date, close), 失败 None."""
    import pandas as pd

    kwargs: dict[str, object] = {
        "symbol": pair,
        "provider": "yfinance",
        "start_date": start_date,
    }
    if end_date:
        kwargs["end_date"] = end_date
    try:
        df = _obb().currency.price.historical(**kwargs).to_dataframe()
    except Exception:
        return None
    if df is None or df.empty or "close" not in df.columns:
        return None
    out = df.reset_index()
    date_col = "date" if "date" in out.columns else out.columns[0]
    out = out[[date_col, "close"]].rename(columns={date_col: "date"})
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    out["close"] = pd.to_numeric(out["close"], errors="coerce")
    out = out.dropna(subset=["date", "close"])
    out = out[out["close"] > 0].sort_values("date").reset_index(drop=True)
    return out if not out.empty else None


def get_fx_history(src: str, dst: str, start_date, end_date=None, refresh: bool = False):
    """历史汇率日线 (1 src → dst): DataFrame(date, close); 失败 None.

    供 K线对比的「比例线」做汇率调整: 跨币种两标的的价格按同一币种相除,
    得到的比值才反映相对估值而非汇率波动。先取 src+dst 直盘, 失败取反向
    再取倒数; 结果按 (src, dst, 起, 止) 进程内缓存 6 小时。
    refresh=True 跳过进程内缓存读取 (用户显式强制刷新)。
    """
    import pandas as pd

    # 与 get_rate 同口径: 稳定币计价按 USD 等价 (比例线汇率调整也要能折算)
    src, dst = valuation_currency(src), valuation_currency(dst)
    # 起止日期统一归一成 YYYY-MM-DD: 调用方传 pd.Timestamp / datetime 时
    # str() 会带上 " 00:00:00", 同一语义的两次调用缓存键不同 (6h 缓存永远
    # 命中不了), 且该字符串会作为 end_date 传给 OpenBB (schema 是 date 类型)
    start_s = (
        pd.Timestamp(start_date).strftime("%Y-%m-%d")
        if start_date
        else (date.today() - timedelta(days=30)).isoformat()
    )
    end_s = pd.Timestamp(end_date).strftime("%Y-%m-%d") if end_date else ""
    key = (src, dst, start_s, end_s)
    now = time.time()
    hit = _fx_hist_cache.get(key)
    if hit and not refresh and now - hit[0] < _FX_HIST_TTL:
        return hit[1].copy()

    if src == dst:
        df = pd.DataFrame({"date": pd.to_datetime([start_s]), "close": [1.0]})
    else:
        df = _yahoo_pair_history(f"{src}{dst}=X", start_s, end_s or None)
        if df is None:
            inv = _yahoo_pair_history(f"{dst}{src}=X", start_s, end_s or None)
            df = None if inv is None else inv.assign(close=1.0 / inv["close"])
        if df is None:
            return None
    _fx_hist_cache[key] = (now, df)
    return df.copy()


def get_fx_rates(
    base: str, currencies, use_ibkr: bool = False
) -> tuple[dict[str, float], list[str]]:
    """即期汇率 (磁盘读穿): 新鲜期内的直接返回, 缺失/过期才走源链并回写.

    首屏 stale-while-revalidate 的另一半: akshare 首次 import + CFETS 一锤子
    请求 ~10s, 冷进程每屏都付; 落盘后 CLI/页面重复调用秒回。回写只含本次
    新取的汇率, 缓存命中项不重写 (否则 fetched_at 被无限刷新)。
    """
    from . import cache as cache_mod

    base = base.upper()
    curs = list(dict.fromkeys(str(c).upper() for c in currencies))
    cached, _ages = cache_mod.get_fx_cached(base, curs, ttl=cache_mod.FX_FRESH_TTL)
    rates: dict[str, float] = {}
    missing: list[str] = []
    for ccy in curs:
        if ccy == base:
            rates[ccy] = 1.0
            continue
        if ccy in cached:
            rates[ccy] = cached[ccy]
            continue
        rate = get_rate(ccy, base, use_ibkr=use_ibkr)
        if rate:
            rates[ccy] = rate
        else:
            missing.append(ccy)
    fresh = {c: r for c, r in rates.items() if c != base and c not in cached}
    if fresh:
        cache_mod.set_fx_cached(base, fresh)
    return rates, missing
