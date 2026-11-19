"""估值指标: PE / PB / 股息率的最新快照与历史曲线.

定位与 `tracker.fundamentals` 区分:
  - fundamentals 只负责 PB/市值提醒所需的每股净资产与总市值, 结果落
    fundamentals 表 (6h), 供自选/阈值规则复用;
  - 本模块服务「基本面分析」页的估值子视图: 一次取回 PE(TTM)/Forward PE/
    PB/股息率/5 年平均股息率/每股收益/每股净资产/总市值, 并可按代码取
    PE/PB 的历史序列。这里不做提醒规则, 也不改组合快照。

数据源:
  - 最新快照: OpenBB `equity.fundamental.metrics(provider="yfinance")`;
    yfinance 不可用时, 若配置了 EODHD (BYOK), 回退到一次
    `eodhd.fundamentals` (只补快照字段, 不额外发请求)。
    yfinance 的 `dividendYield` 是百分数 (3.68 = 3.68%), 而 OpenBB 只对
    `dividend_yield_5y_avg` 做了 /100 归一, 两个口径不同, 读取时分别处理。
  - 历史 PE/PB: akshare 百度股市通 (A股/港股/美股)。百度接口按
    「近一年/近三年/…」返回日频估值, 这里再按用户选择的月数截断;
    结果写入通用 series_cache (6h), 切换页面/进程不重复拉取。
  - 快照落 json_cache (6h): 进程重启/CLI 与页面切换不再重复回源;
    refresh=True 跳过内存与磁盘缓存。

负值口径: PE/PB 为负 (亏损/负净资产) 时视为「无意义」返回 None, 历史曲线
同样只保留 >0 的点 —— 否则曲线会跨 0 轴, 分位/均值也会被负值污染。
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, fields, replace

import pandas as pd

from . import cache as cache_mod
from .deps import openbb as _obb
from .symbols import Market, ParsedSymbol, normalize, parse
from .util import finite_float as _finite
from .util import first_column as _column
from .util import non_negative_float as _non_negative
from .util import positive_float as _positive
from .util import with_timeout

#: 最新快照缓存 (秒); 估值倍数是低频报表数据, 6h 足够
_SNAPSHOT_TTL = 21600
#: json_cache 里的快照前缀 (与 filings: 等其它业务隔离, 便于按前缀清理)
_SNAPSHOT_KEY = "valuation_snapshot:"
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
    #: 东财实时「市盈率(动)」(Quote.pe_dynamic); 与 PE(TTM) 口径不同, 单独展示
    pe_dynamic: float | None = None
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


def normalize_stock_input(raw: str) -> str:
    """宽松股票代码输入 → Yahoo 规范代码 (估值页 / CLI 共用).

    支持 601398 (补 .SS) / 1398 (补 .HK) 这类纯数字简写; 带后缀或美股
    ticker 走统一 normalize。空输入/畸形代码/非股票市场抛 ValueError。
    """
    from . import ah_premium

    s = str(raw or "").strip()
    if not s:
        raise ValueError("股票代码不能为空")
    if "." not in s and s.isdigit():
        symbol = (
            ah_premium.normalize_a_symbol(s)
            if len(s) == 6
            else ah_premium.normalize_h_symbol(s)
        )
    else:
        symbol = normalize(s)
    p = parse(symbol)
    if p.market in (Market.CRYPTO, Market.INDEX):
        raise ValueError(f"{p.yahoo}: 估值指标仅支持股票, 不支持 {p.market_label}")
    return p.yahoo


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


def _snapshot_payload(snap: ValuationSnapshot) -> dict:
    """ValuationSnapshot → 可 JSON 序列化的 dict (字段名与 dataclass 一致)."""
    return {f.name: getattr(snap, f.name) for f in fields(ValuationSnapshot)}


def _snapshot_from_payload(payload: object) -> ValuationSnapshot | None:
    """json_cache 载荷 → ValuationSnapshot; 缺字段/类型损坏返回 None."""
    if not isinstance(payload, dict):
        return None
    names = {f.name for f in fields(ValuationSnapshot)}
    kwargs = {k: v for k, v in payload.items() if k in names}
    try:
        return ValuationSnapshot(**kwargs)
    except (TypeError, ValueError):
        return None


def _snapshot_from_disk(symbol: str) -> ValuationSnapshot | None:
    """读 6h 磁盘快照; 损坏/过期按未命中处理."""
    return _snapshot_from_payload(
        cache_mod.get_json_cached(f"{_SNAPSHOT_KEY}{symbol}", ttl=_SNAPSHOT_TTL)
    )


def _fetch_metrics_row(symbol: str) -> dict:
    """OpenBB yfinance metrics → 第一行 dict; 无结果时抛 RuntimeError."""
    ob = _obb()
    res = ob.equity.fundamental.metrics(symbol=symbol, provider="yfinance")
    for item in res.results:
        d = item.model_dump()
        if str(d.get("symbol") or "").strip().upper() == symbol.upper():
            return d
    if res.results:
        return res.results[0].model_dump()
    raise RuntimeError(f"{symbol}: 数据源未返回估值指标")


def _fetch_eodhd_snapshot(
    p: ParsedSymbol, *, fetched_at: float
) -> ValuationSnapshot:
    """EODHD 基本面 → 估值快照 (yfinance 失败时的可选兜底).

    EODHD 的比例字段是小数 (0.0034 = 0.34%), 统一用 _fraction_or_percent /
    _percent 归一; 只有配置了 token 且 yfinance 确实失败时才会走到这里,
    不会为正常快照额外消耗 EODHD 额度。
    """
    from . import eodhd as eodhd_mod

    data = eodhd_mod.fundamentals(p.yahoo)
    return ValuationSnapshot(
        symbol=p.yahoo,
        market_label=p.market_label,
        currency=str(data.get("currency") or p.currency),
        pe_ttm=_positive(data.get("pe_ratio")),
        forward_pe=_positive(data.get("forward_pe")),
        pb=_positive(data.get("price_to_book")),
        dividend_yield_pct=_fraction_or_percent(data.get("dividend_yield")),
        dividend_yield_5y_avg_pct=None,
        payout_ratio_pct=_percent(data.get("payout_ratio")),
        eps_ttm=_positive(data.get("eps_ttm")),
        book_value=_positive(data.get("book_value")),
        market_cap=_positive(data.get("market_cap")),
        source="eodhd",
        fetched_at=fetched_at,
    )


def _with_dynamic_pe(snap: ValuationSnapshot) -> ValuationSnapshot:
    """用行情缓存里的东财实时 PE(动) 补充快照 (无则原样返回).

    只读 5 分钟行情缓存, 不发网络请求; 组合页/自选页先打开时, 估值页可以
    顺带看到更实时的动态 PE。PE(TTM) 保持原口径, 两者不互相覆盖。
    """
    try:
        q = cache_mod.get_cached([snap.symbol]).get(snap.symbol)
    except Exception:  # noqa: BLE001 - 缓存读失败不影响估值快照
        return snap
    pe = getattr(q, "pe_dynamic", None) if q is not None else None
    if pe is None or pe == snap.pe_dynamic:
        return snap
    return replace(snap, pe_dynamic=pe)


def _fetch_akshare_snapshot(
    p: ParsedSymbol, *, fetched_at: float
) -> ValuationSnapshot:
    """A/H 股 yfinance 失败时的免费实时兜底: 东财现价 + PB/市值/PE(动).

    东财 push2 不提供 TTM 口径, 因此 pe_ttm 留空, 只写 pe_dynamic;
    页面会把它单独显示为「PE (动)」, 不冒充 PE(TTM)。
    """
    from . import prices

    quotes, errors, _notes = prices.get_quotes([p.yahoo], prefer_akshare=True)
    q = quotes.get(p.yahoo)
    if q is None:
        raise RuntimeError(errors.get(p.yahoo) or f"{p.yahoo}: akshare 行情不可用")
    pb = (
        q.price / q.book_value
        if q.book_value is not None and q.book_value > 0 and q.price > 0
        else None
    )
    return ValuationSnapshot(
        symbol=p.yahoo,
        market_label=p.market_label,
        currency=q.currency,
        pb=pb,
        market_cap=q.market_cap,
        pe_dynamic=getattr(q, "pe_dynamic", None),
        source="akshare",
        fetched_at=fetched_at,
    )


def fetch_snapshot(symbol: str, *, refresh: bool = False) -> ValuationSnapshot:
    """取单只股票的最新估值快照 (内存 + 磁盘 6h; refresh=True 穿透)."""
    p = parse(normalize(symbol))
    if p.market in (Market.CRYPTO, Market.INDEX):
        raise ValueError(f"{p.yahoo}: 估值指标仅支持股票, 不支持 {p.market_label}")

    now = time.time()
    if not refresh:
        with _snapshot_lock:
            hit = _snapshot_cache.get(p.yahoo)
        if hit is not None and now - hit[0] < _SNAPSHOT_TTL:
            return hit[1]

        # 进程重启 / CLI 与页面切换: 先吃 6h 磁盘快照, 避免重复回源。
        disk = _snapshot_from_disk(p.yahoo)
        if disk is not None:
            # 磁盘快照可能是在行情缓存为空时写入的; 若现在有实时 PE(动),
            # 顺带补上 (只改内存返回值, 不回写磁盘, 保持 6h 快照不变)。
            disk = _with_dynamic_pe(disk)
            # 内存新鲜度沿用原始抓取时间, 不能重置成 now: 否则会变相延长磁盘
            # 快照的 6h TTL, 磁盘过期后内存仍继续命中。损坏的时间戳按 now 处理,
            # 避免后续 `now - fetched_at` 直接抛 TypeError。
            ts = _finite(disk.fetched_at) or now
            with _snapshot_lock:
                _snapshot_cache[p.yahoo] = (ts, disk)
            return disk

    try:
        data = _fetch_metrics_row(p.yahoo)
    except Exception as primary_error:
        # yfinance 失败时的兜底顺序:
        #   A股/北交所/港股 → 东财实时 (免费, 有现价/PB/市值/PE动)
        #   其它市场        → EODHD (BYOK, 有 PE/PB/股息等)
        # 两个兜底都失败时抛回 yfinance 的原始异常, 保持页面/CLI 错误稳定。
        snap = None
        if p.market in (Market.CN, Market.BJ, Market.HK):
            try:
                snap = _fetch_akshare_snapshot(p, fetched_at=now)
            except Exception:  # noqa: BLE001 - 继续尝试 EODHD
                snap = None
        if snap is None:
            from . import eodhd as eodhd_mod

            if not eodhd_mod.available():
                raise
            try:
                snap = _fetch_eodhd_snapshot(p, fetched_at=now)
            except Exception as eodhd_error:
                raise primary_error from eodhd_error
    else:
        snap = _snapshot_from_metrics(p.yahoo, data, fetched_at=now)

    snap = _with_dynamic_pe(snap)
    with _snapshot_lock:
        _snapshot_cache[p.yahoo] = (now, snap)
    cache_mod.set_json_cached(f"{_SNAPSHOT_KEY}{p.yahoo}", _snapshot_payload(snap))
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
