"""估值指标: PE / PB / 股息率的最新快照与历史曲线.

定位与 `tracker.fundamentals` 区分:
  - fundamentals 只负责 PB/市值提醒所需的每股净资产与总市值, 结果落
    fundamentals 表 (6h), 供自选/阈值规则复用;
  - 本模块服务「基本面分析」页的估值子视图: 一次取回 PE(TTM)/Forward PE/
    PB/股息率/5 年平均股息率/每股收益/每股净资产/总市值, 并可按代码取
    PE/PB 的历史序列。这里不做提醒规则, 也不改组合快照。

数据源:
  - 最新快照: OpenBB `equity.fundamental.metrics(provider="yfinance")`。
    yfinance 的 `dividendYield` 是百分数 (3.68 = 3.68%), 而 OpenBB 只对
    `dividend_yield_5y_avg` 做了 /100 归一, 两个口径不同, 读取时分别处理。
  - 历史 PE/PB: akshare 百度股市通 (A股/港股/美股)。百度接口按
    「近一年/近三年/…」返回日频估值, 这里再按用户选择的月数截断;
    结果写入通用 series_cache (6h), 切换页面/进程不重复拉取。

负值口径: PE/PB 为负 (亏损/负净资产) 时视为「无意义」返回 None, 历史曲线
同样只保留 >0 的点 —— 否则曲线会跨 0 轴, 分位/均值也会被负值污染。
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass

import pandas as pd

from . import cache as cache_mod
from .symbols import Market, normalize, parse
from .util import with_timeout

#: 最新快照缓存 (秒); 估值倍数是低频报表数据, 6h 足够
_SNAPSHOT_TTL = 21600
#: 历史估值曲线缓存 (秒)
_HISTORY_TTL = 21600
#: akshare 百度接口没有内置 timeout, 用守护线程硬超时避免页面卡死
_HISTORY_TIMEOUT = 15.0

#: 支持历史曲线的市场 (百度股市通覆盖)
_HISTORY_MARKETS = (Market.CN, Market.BJ, Market.HK, Market.US)

METRIC_PE = "pe"
METRIC_PB = "pb"
METRIC_LABELS = {
    METRIC_PE: "市盈率 (TTM)",
    METRIC_PB: "市净率",
}
METRIC_INDICATORS = {
    METRIC_PE: "市盈率(TTM)",
    METRIC_PB: "市净率",
}

_snapshot_lock = threading.Lock()
_snapshot_cache: dict[str, tuple[float, "ValuationSnapshot"]] = {}


@dataclass(frozen=True)
class ValuationSnapshot:
    """单只股票的最新估值快照 (比例类字段均为百分数)."""

    symbol: str
    market_label: str
    currency: str
    pe_ttm: float | None = None
    forward_pe: float | None = None
    pb: float | None = None
    dividend_yield_pct: float | None = None
    dividend_yield_5y_avg_pct: float | None = None
    payout_ratio_pct: float | None = None
    eps_ttm: float | None = None
    book_value: float | None = None
    market_cap: float | None = None
    source: str = "yfinance"
    fetched_at: float = 0.0


def _finite(value: object) -> float | None:
    """任意值 → 有限 float; 缺失/非数字/NaN/Inf 返回 None."""
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _positive(value: object) -> float | None:
    f = _finite(value)
    return f if f is not None and f > 0 else None


def _non_negative(value: object) -> float | None:
    f = _finite(value)
    return f if f is not None and f >= 0 else None


def _percent(value: object) -> float | None:
    """确定是比例的值 → 百分数 (payout_ratio 始终为比例, 可 >100%)."""
    f = _non_negative(value)
    return f * 100.0 if f is not None else None


def _fraction_or_percent(value: object) -> float | None:
    """兼容「小数比例」与「百分数」两种数据源口径.

    OpenBB 的 `dividend_yield_5y_avg` 在当前 provider 实现中会被 /100
    (0.0529 = 5.29%), 但不同版本/源可能直接给 5.29。绝对值 ≤1 视为
    比例 (股息率 100% 以上极罕见), 否则按百分数使用。
    """
    f = _non_negative(value)
    if f is None:
        return None
    return f * 100.0 if f <= 1.0 else f


def _snapshot_from_metrics(
    symbol: str, data: dict, *, fetched_at: float | None = None
) -> ValuationSnapshot:
    """OpenBB metrics 行 → ValuationSnapshot (纯函数, 便于离线测试)."""
    p = parse(symbol)
    # 市值/股本单位跟随报价币种 (A/H 两地代码各自以当地价格计市值)。
    return ValuationSnapshot(
        symbol=p.yahoo,
        market_label=p.market_label,
        currency=p.currency,
        pe_ttm=_positive(data.get("pe_ratio")),
        forward_pe=_positive(data.get("forward_pe")),
        pb=_positive(data.get("price_to_book")),
        # yfinance 的 dividendYield 已经是百分数, 不再 ×100
        dividend_yield_pct=_non_negative(data.get("dividend_yield")),
        dividend_yield_5y_avg_pct=_fraction_or_percent(
            data.get("dividend_yield_5y_avg")
        ),
        # OpenBB 的 payout_ratio 为比例 (0.2955 = 29.55%); 该指标可以
        # 合法地超过 100% (如 PFE 2.457), 不能套用「≤1 才 ×100」的启发式,
        # 否则 245.7% 会被显示成 2.46%。
        payout_ratio_pct=_percent(data.get("payout_ratio")),
        eps_ttm=_positive(data.get("eps_ttm")),
        book_value=_positive(data.get("book_value")),
        market_cap=_positive(data.get("market_cap")),
        source="yfinance",
        fetched_at=time.time() if fetched_at is None else fetched_at,
    )


def _fetch_metrics_row(symbol: str) -> dict:
    """OpenBB yfinance metrics → 第一行 dict; 无结果时抛 RuntimeError."""
    from .providers.global_stocks import _obb

    ob = _obb()
    res = ob.equity.fundamental.metrics(symbol=symbol, provider="yfinance")
    for item in res.results:
        d = item.model_dump()
        if str(d.get("symbol") or "").strip().upper() == symbol.upper():
            return d
    if res.results:
        return res.results[0].model_dump()
    raise RuntimeError(f"{symbol}: 数据源未返回估值指标")


def fetch_snapshot(symbol: str, *, refresh: bool = False) -> ValuationSnapshot:
    """取单只股票的最新估值快照 (6h 进程内缓存; refresh=True 穿透)."""
    p = parse(normalize(symbol))
    if p.market in (Market.CRYPTO, Market.INDEX):
        raise ValueError(f"{p.yahoo}: 估值指标仅支持股票, 不支持 {p.market_label}")

    now = time.time()
    with _snapshot_lock:
        hit = _snapshot_cache.get(p.yahoo)
    if hit is not None and not refresh and now - hit[0] < _SNAPSHOT_TTL:
        return hit[1]

    data = _fetch_metrics_row(p.yahoo)
    snap = _snapshot_from_metrics(p.yahoo, data, fetched_at=now)
    with _snapshot_lock:
        _snapshot_cache[p.yahoo] = (now, snap)
    return snap


def clear_cache() -> None:
    """清空最新估值快照的进程内缓存 (手动刷新/测试)."""
    with _snapshot_lock:
        _snapshot_cache.clear()


def history_supported(symbol: str) -> bool:
    """该代码是否有历史 PE/PB 数据 (百度股市通覆盖 A/BJ/HK/US)."""
    try:
        return parse(normalize(symbol)).market in _HISTORY_MARKETS
    except ValueError:
        return False


def _column(df: pd.DataFrame, *names: str) -> str | None:
    """按候选列名找列 (百度接口偶有列名漂移)."""
    by_name = {str(c).strip(): c for c in df.columns}
    for n in names:
        if n in by_name:
            return by_name[n]
    return None


def _baidu_request(
    symbol: str, metric: str, months: int
) -> tuple[object, str, str, str]:
    """(akshare 函数, 查询代码, 指标名, 周期) —— 便于测试替换/校验."""
    p = parse(normalize(symbol))
    if p.market not in _HISTORY_MARKETS:
        raise ValueError(f"{p.yahoo}: 历史估值暂只支持 A股/港股/美股")
    indicator = METRIC_INDICATORS.get(metric)
    if indicator is None:
        raise ValueError(f"未知估值指标: {metric}")

    if p.market in (Market.CN, Market.BJ):
        code = p.yahoo.split(".")[0]
        fn_name = "stock_zh_valuation_baidu"
    elif p.market is Market.HK:
        code = p.ak_code or ""
        if not code:
            raise ValueError(f"{p.yahoo}: 无法识别港股代码")
        fn_name = "stock_hk_valuation_baidu"
    else:
        code = p.yahoo
        fn_name = "stock_us_valuation_baidu"

    if months <= 12:
        period = "近一年"
    elif months <= 36:
        period = "近三年"
    elif p.market in (Market.CN, Market.BJ):
        period = "近五年" if months <= 60 else ("近十年" if months <= 120 else "全部")
    else:
        # 港股/美股百度只提供近一年/近三年/全部
        period = "全部"
    return fn_name, code, indicator, period


def _clean_history(df: pd.DataFrame | None, months: int) -> pd.DataFrame:
    """百度估值表 → 干净 (date, value) 两列, 按月份截断."""
    cols = ["date", "value"]
    if df is None or df.empty:
        return pd.DataFrame(columns=cols)
    date_col = _column(df, "date", "日期")
    value_col = _column(df, "value", "数值", "指标值")
    if not date_col or not value_col:
        return pd.DataFrame(columns=cols)
    out = pd.DataFrame(
        {
            "date": pd.to_datetime(df[date_col], errors="coerce"),
            "value": pd.to_numeric(df[value_col], errors="coerce"),
        }
    )
    out = out.dropna(subset=["date", "value"])
    # PE/PB 非正数没有估值含义 (亏损/负净资产), 不进入曲线与分位
    out = out[out["value"] > 0]
    out = out[out["value"].map(lambda v: math.isfinite(float(v)))]
    if out.empty:
        return pd.DataFrame(columns=cols)
    cutoff = pd.Timestamp.today().normalize() - pd.DateOffset(months=int(months))
    out = out[out["date"] >= cutoff]
    out = out.sort_values("date", kind="stable").drop_duplicates(
        subset="date", keep="last"
    )
    return out.reset_index(drop=True)


def fetch_history(
    symbol: str, metric: str = METRIC_PE, months: int = 36, *, refresh: bool = False
) -> pd.DataFrame:
    """历史 PE/PB 曲线 → DataFrame(date, value).

    百度股市通按「近一年/近三年/…」返回日频数据, 再按 months 精确截断;
    结果写通用序列缓存 (6h)。market/指标不支持或接口无数据时抛 RuntimeError
    (页面捕获后提示, 不影响当前估值快照)。
    """
    if metric not in METRIC_LABELS:
        raise ValueError(f"未知估值指标: {metric}")
    p = parse(normalize(symbol))
    fn_name, code, indicator, period = _baidu_request(p.yahoo, metric, months)
    key = f"valuation:{p.yahoo}:{metric}:{months}"
    if not refresh:
        hit = cache_mod.get_series_cached(key, ttl=_HISTORY_TTL)
        if hit is not None and not hit.empty:
            return hit.rename(columns={"close": "value"})

    from .ah_premium import _ak

    ak = _ak()  # 冷启动放在计时区外
    fn = getattr(ak, fn_name, None)
    if fn is None:
        raise RuntimeError(f"当前 akshare 版本缺少 {fn_name}")
    raw = with_timeout(
        fn,
        _HISTORY_TIMEOUT,
        symbol=code,
        indicator=indicator,
        period=period,
    )
    df = _clean_history(raw, months)
    if df.empty:
        raise RuntimeError(f"{p.yahoo}: 百度估值无 {METRIC_LABELS[metric]} 历史数据")
    cache_mod.set_series_cached(key, df.rename(columns={"value": "close"}))
    return df


def summarize_history(df: pd.DataFrame, value_col: str = "value") -> dict:
    """历史估值摘要 (最新/均值/中位数/高低点/分位)."""
    if df is None or df.empty or value_col not in df.columns:
        return {}
    d = df.dropna(subset=[value_col]).sort_values("date")
    if d.empty:
        return {}
    vals = d[value_col].astype(float)
    last = d.iloc[-1]
    first = d.iloc[0]
    high_idx = vals.idxmax()
    low_idx = vals.idxmin()

    def _date(i) -> str:
        return pd.Timestamp(d.loc[i, "date"]).strftime("%Y-%m-%d")

    latest = float(last[value_col])
    return {
        "bars": int(len(d)),
        "first_date": pd.Timestamp(first["date"]).strftime("%Y-%m-%d"),
        "last_date": pd.Timestamp(last["date"]).strftime("%Y-%m-%d"),
        "latest": latest,
        "first": float(first[value_col]),
        "change": latest - float(first[value_col]),
        "mean": float(vals.mean()),
        "median": float(vals.median()),
        "high": float(vals.max()),
        "high_date": _date(high_idx),
        "low": float(vals.min()),
        "low_date": _date(low_idx),
        "percentile": float((vals <= latest).mean() * 100.0),
    }
