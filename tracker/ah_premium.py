"""A+H 股票溢价率: 股票对目录、A/H 收盘价取数与溢价曲线计算.

「基本面分析」页的 **AH 溢价子视图**只做**单只 A+H 股票**的 AH 溢价率
曲线 (不是恒生 AH 股溢价指数, 后者在指数域 IX.HSAHP); 估值指标 (PE/PB/
股息率) 是同页另一个子视图, 见 tracker.valuation。口径:

    溢价率 = A股收盘价(CNY) / (H股收盘价(HKD) × HKD→CNY 汇率) − 1

- 正值 = A 股相对 H 股溢价; 0 = 两地同价。
- 以 A 股交易日为时间轴, H 股缺失的交易日取「当日或之前最近一个收盘价」,
  汇率同理 (与 charting.build_ratio_series 的跨市场对齐口径一致)。
- A/H 历史都优先走 akshare 前复权 (A 股域固定 akshare; H 股显式 prefer_akshare),
  保证两侧复权口径一致。取数失败由既有 provider 源链回落 yfinance。

股票对目录来自东财「沪深港通-AH股比价」实时行情 (akshare stock_zh_ah_spot_em),
1 小时进程内缓存; 接口不可用时退回内置常见 A+H 股票对, 页面仍可手动输入代码。
"""

from __future__ import annotations

import math
import time
from bisect import bisect_right
from dataclasses import dataclass
from threading import Lock

import pandas as pd

from . import fx as fx_mod
from . import prices
from .symbols import Market, normalize, parse
from .util import with_timeout

_PAIR_TTL = 3600  # 秒; AH 比价列表是低频参考数据
_PAIR_TIMEOUT = 10.0
_pair_lock = Lock()
_pairs_cache: tuple[float, list["AHPair"]] | None = None


@dataclass(frozen=True)
class AHPair:
    """一只 A+H 双重上市股票 (代码均为 Yahoo 规范形式)."""

    name: str
    a_symbol: str
    h_symbol: str
    premium_pct: float | None = None


# 东财接口不可用时的兜底目录 (只收确定长期 A+H 双重上市的大盘股)
_FALLBACK_PAIRS: tuple[tuple[str, str, str], ...] = (
    ("工商银行", "601398.SS", "1398.HK"),
    ("建设银行", "601939.SS", "939.HK"),
    ("中国平安", "601318.SS", "2318.HK"),
    ("招商银行", "600036.SS", "3968.HK"),
    ("中国石油", "601857.SS", "857.HK"),
    ("中国石化", "600028.SS", "386.HK"),
    ("中国人寿", "601628.SS", "2628.HK"),
    ("中国神华", "601088.SS", "1088.HK"),
    ("比亚迪", "002594.SZ", "1211.HK"),
    ("海螺水泥", "600585.SS", "914.HK"),
    ("潍柴动力", "000338.SZ", "2338.HK"),
    ("药明康德", "603259.SS", "2359.HK"),
    ("美的集团", "000333.SZ", "300.HK"),
    ("海尔智家", "600690.SS", "6690.HK"),
    ("中国移动", "600941.SS", "941.HK"),
    ("中信证券", "600030.SS", "6030.HK"),
)

# 默认选中的股票对 (存在时优先): 大盘、流动性好, 适合作为页面首屏
_DEFAULT_NAMES = ("工商银行", "建设银行", "中国平安", "招商银行")


def _ak():
    """惰性导入 akshare (CLI 冷启动 / 测试无网时不拖慢模块导入)."""
    import akshare as ak

    return ak


def _digits(value: object) -> str:
    """取出代码中的数字 (兼容 601398 / '601398' / 601398.0 / 'SH601398')."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    s = str(value or "").strip()
    if s.endswith(".0") and s[:-2].isdigit():
        return s[:-2]
    return "".join(ch for ch in s if ch.isdigit())


def normalize_a_symbol(raw: object) -> str:
    """宽松 A 股代码 → Yahoo 规范 (.SS/.SZ/.BJ); 无法识别时抛 ValueError."""
    s = str(raw or "").strip()
    if not s:
        raise ValueError("A 股代码不能为空")
    # 已带交易所后缀的走统一归一 (600519.SH → 600519.SS 等)
    if "." in s:
        p = parse(normalize(s))
        if p.market in (Market.CN, Market.BJ):
            return p.yahoo
        raise ValueError(f"{raw}: 不是 A 股代码 (需要 .SS/.SZ/.BJ)")
    code = _digits(s)
    if len(code) != 6:
        raise ValueError(f"{raw}: 无法识别的 A 股代码")
    if code.startswith("6") or code.startswith("900"):
        # 6 = 沪市 A 股; 900 = 沪市 B 股 (仅 900xxx, 不能把所有 9 开头
        # 代码都归上海: 北交所新号段 920xxx 也以 9 开头)
        return f"{code}.SS"
    if code.startswith(("0", "2", "3")):
        # 2xxxxx = 深市 B 股 (200xxx/201xxx); 0/3 = 深市 A 股
        return f"{code}.SZ"
    if code.startswith(("4", "8", "92")):
        # 4/8 = 北交所老号段; 92xxxx = 北交所新号段
        return f"{code}.BJ"
    raise ValueError(f"{raw}: 无法识别的 A 股代码")


def normalize_h_symbol(raw: object) -> str:
    """宽松港股代码 → Yahoo 规范 (.HK); 无法识别时抛 ValueError."""
    s = str(raw or "").strip()
    if not s:
        raise ValueError("H 股代码不能为空")
    if "." in s:
        p = parse(normalize(s))
        if p.market is Market.HK:
            return p.yahoo
        raise ValueError(f"{raw}: 不是港股代码 (需要 .HK)")
    code = _digits(s)
    if not code:
        raise ValueError(f"{raw}: 无法识别的港股代码")
    return normalize(f"{code}.HK")


def _fallback_pairs() -> list[AHPair]:
    return [AHPair(name, a, h) for name, a, h in _FALLBACK_PAIRS]


def _column(df: pd.DataFrame, *names: str) -> str | None:
    """按候选列名找列 (数据源列名偶有空格/别名漂移)."""
    by_name = {str(c).strip(): c for c in df.columns}
    for n in names:
        if n in by_name:
            return by_name[n]
    return None


def _parse_pair_rows(df: pd.DataFrame) -> list[AHPair]:
    """东财 AH 比价表 → AHPair 列表; 列缺失/代码异常返回空列表."""
    if df is None or df.empty:
        return []
    name_col = _column(df, "名称", "name")
    a_col = _column(df, "A股代码", "A股", "a_code")
    h_col = _column(df, "H股代码", "H股", "h_code")
    prem_col = _column(df, "溢价", "溢价率", "premium")
    if not (name_col and a_col and h_col):
        return []
    out: list[AHPair] = []
    seen: set[tuple[str, str]] = set()
    for _, row in df.iterrows():
        try:
            a = normalize_a_symbol(row[a_col])
            h = normalize_h_symbol(row[h_col])
        except (ValueError, TypeError):
            continue
        key = (a, h)
        if key in seen:
            continue
        seen.add(key)
        raw_name = row[name_col]
        name = ("" if pd.isna(raw_name) else str(raw_name).strip()) or f"{a} / {h}"
        prem = None
        if prem_col is not None:
            try:
                v = float(row[prem_col])
                if math.isfinite(v):
                    prem = v
            except (TypeError, ValueError):
                prem = None
        out.append(AHPair(name, a, h, prem))
    return out


def list_pairs(refresh: bool = False) -> list[AHPair]:
    """A+H 股票对目录 (东财实时比价, 1h 缓存).

    接口失败时返回陈旧缓存 (若有), 否则退回内置目录; 永不抛异常 ——
    页面即使第一次启动就断网, 也能从兜底目录选到常见股票对。
    """
    global _pairs_cache
    now = time.time()
    with _pair_lock:
        hit = _pairs_cache
    if hit is not None and not refresh and now - hit[0] < _PAIR_TTL:
        return list(hit[1])

    pairs: list[AHPair] = []
    try:
        # import akshare 冷启动 ~10s, 必须在计时区外; 否则首次打开 AH 页
        # 会被硬超时打断, 静默退回内置目录。
        ak = _ak()
        df = with_timeout(ak.stock_zh_ah_spot_em, _PAIR_TIMEOUT)
        pairs = _parse_pair_rows(df)
    except Exception:  # noqa: BLE001 - 目录失败不阻断页面, 用兜底
        pairs = []

    if not pairs:
        pairs = list(hit[1]) if hit is not None else _fallback_pairs()
    else:
        # 常见大盘股置顶 (默认选中项在列表第一屏), 其余按溢价率降序
        # 方便浏览高溢价标的; 溢价缺失的排最后
        pairs.sort(
            key=lambda p: (
                _DEFAULT_NAMES.index(p.name) if p.name in _DEFAULT_NAMES else 99,
                p.premium_pct is None,
                -(p.premium_pct or 0.0),
            )
        )
    with _pair_lock:
        _pairs_cache = (now, list(pairs))
    return list(pairs)


def clear_pair_cache() -> None:
    """清空股票对目录进程内缓存 (测试/手动刷新)."""
    global _pairs_cache
    with _pair_lock:
        _pairs_cache = None


def default_pair_index(pairs: list[AHPair]) -> int:
    """默认选中的股票对下标 (优先大盘股, 否则第一只)."""
    for name in _DEFAULT_NAMES:
        for i, p in enumerate(pairs):
            if p.name == name:
                return i
    return 0


def _clean_close(df: pd.DataFrame | None) -> pd.DataFrame:
    """DataFrame → 干净 (date, close) 两列, 空表返回空 frame."""
    cols = ["date", "close"]
    if df is None or df.empty or "date" not in df.columns or "close" not in df.columns:
        return pd.DataFrame(columns=cols)
    out = pd.DataFrame(
        {
            "date": pd.to_datetime(df["date"], errors="coerce"),
            "close": pd.to_numeric(df["close"], errors="coerce"),
        }
    )
    out = out.dropna(subset=["date", "close"])
    out = out[out["close"] > 0]
    out = out[out["close"].map(lambda v: math.isfinite(float(v)))]
    out = out.sort_values("date", kind="stable").drop_duplicates(
        subset="date", keep="last"
    )
    return out.reset_index(drop=True)


def calc_premium_series(
    a_df: pd.DataFrame,
    h_df: pd.DataFrame,
    fx_df: pd.DataFrame | None = None,
    *,
    fx_required: bool = False,
) -> pd.DataFrame:
    """A/H 收盘价 + 汇率历史 → 溢价率曲线.

    返回列: date / a_close / h_close / fx_rate / premium_pct。
    以 A 股交易日为基准, H 收盘与汇率取「≤ 当日」的最近值 (跨市场休市日
    不产生缺口, 也不会把未来数据带到过去)。fx_df 为 1 H股币种 → A股币种
    的历史汇率 (date/close); fx_required=True 且 fx_df 为空时抛 ValueError
    (跨币种没有汇率就不该静默按 1:1 计算)。
    """
    a = _clean_close(a_df)
    h = _clean_close(h_df)
    if a.empty:
        raise ValueError("A 股无有效收盘价")
    if h.empty:
        raise ValueError("H 股无有效收盘价")

    fx_clean = _clean_close(fx_df)
    if fx_required and fx_clean.empty:
        raise ValueError("汇率历史缺失, 无法折算 H 股价格")

    def _times_vals(df: pd.DataFrame) -> tuple[list[str], list[float]]:
        times = [pd.Timestamp(t).strftime("%Y-%m-%d") for t in df["date"]]
        vals = [float(v) for v in df["close"]]
        return times, vals

    h_times, h_vals = _times_vals(h)
    fx_times, fx_vals = _times_vals(fx_clean) if not fx_clean.empty else ([], [])

    rows: list[dict] = []
    for t, a_close in zip(a["date"], a["close"]):
        key = pd.Timestamp(t).strftime("%Y-%m-%d")
        j = bisect_right(h_times, key) - 1
        if j < 0:
            continue
        rate = 1.0
        if fx_times:
            k = bisect_right(fx_times, key) - 1
            if k < 0:
                continue
            rate = fx_vals[k]
        if rate <= 0:
            continue
        h_cny = h_vals[j] * rate
        if h_cny <= 0:
            continue
        premium = (float(a_close) / h_cny - 1.0) * 100.0
        if not math.isfinite(premium):
            continue
        rows.append(
            {
                "date": pd.Timestamp(t),
                "a_close": float(a_close),
                "h_close": h_vals[j],
                "fx_rate": rate,
                "premium_pct": premium,
            }
        )
    if not rows:
        raise ValueError("A/H 无重叠交易日, 无法计算溢价率")
    return pd.DataFrame(rows).sort_values("date").reset_index(drop=True)


def summarize_premium(df: pd.DataFrame) -> dict:
    """溢价率曲线摘要 (最新/区间涨跌/高低点/均值/分位)."""
    if df is None or df.empty or "premium_pct" not in df.columns:
        return {}
    d = df.dropna(subset=["premium_pct"]).sort_values("date")
    if d.empty:
        return {}
    vals = d["premium_pct"].astype(float)
    last = d.iloc[-1]
    first = d.iloc[0]
    high_idx = vals.idxmax()
    low_idx = vals.idxmin()

    def _date(i) -> str:
        return pd.Timestamp(d.loc[i, "date"]).strftime("%Y-%m-%d")

    last_premium = float(last["premium_pct"])
    return {
        "bars": int(len(d)),
        "first_date": pd.Timestamp(first["date"]).strftime("%Y-%m-%d"),
        "last_date": pd.Timestamp(last["date"]).strftime("%Y-%m-%d"),
        "last_premium": last_premium,
        "first_premium": float(first["premium_pct"]),
        "change_pp": last_premium - float(first["premium_pct"]),
        "mean": float(vals.mean()),
        "median": float(vals.median()),
        "high": float(vals.max()),
        "high_date": _date(high_idx),
        "low": float(vals.min()),
        "low_date": _date(low_idx),
        "percentile": float((vals <= last_premium).mean() * 100.0),
        "a_close": float(last["a_close"]),
        "h_close": float(last["h_close"]),
        "fx_rate": float(last["fx_rate"]),
        "h_close_cny": float(last["h_close"]) * float(last["fx_rate"]),
    }


def fetch_ah_premium(
    a_symbol: str,
    h_symbol: str,
    months: int = 12,
    *,
    refresh: bool = False,
) -> tuple[pd.DataFrame, dict]:
    """取 A/H 历史并计算单只股票 AH 溢价率曲线.

    返回 (溢价曲线 DataFrame, 元信息 dict)。A/H 均优先 akshare 前复权
    (H 股显式 prefer_first), 不启用 IBKR/长桥 —— 两侧必须来自同口径源,
    混用复权方式会让溢价率出现与行情软件不一致的台阶。
    """
    a = normalize_a_symbol(a_symbol)
    h = normalize_h_symbol(h_symbol)
    pa, ph = parse(a), parse(h)
    if pa.market not in (Market.CN, Market.BJ):
        raise ValueError(f"{a_symbol}: 不是 A 股代码")
    if ph.market is not Market.HK:
        raise ValueError(f"{h_symbol}: 不是港股代码")

    a_df = prices.get_ohlc(
        a,
        months=months,
        prefer_akshare=False,
        refresh=refresh,
        use_ibkr=False,
        use_longport=False,
    )
    h_df = prices.get_ohlc(
        h,
        months=months,
        prefer_akshare=True,
        refresh=refresh,
        use_ibkr=False,
        use_longport=False,
    )
    if a_df is None or a_df.empty:
        raise RuntimeError(f"{a}: A 股无有效历史 K 线")
    if h_df is None or h_df.empty:
        raise RuntimeError(f"{h}: H 股无有效历史 K 线")

    a_ccy, h_ccy = pa.currency, ph.currency
    fx_df = None
    fx_pair = None
    if a_ccy != h_ccy:
        start = min(
            pd.to_datetime(a_df["date"]).min(), pd.to_datetime(h_df["date"]).min()
        )
        start_s = (start - pd.Timedelta(days=10)).strftime("%Y-%m-%d")
        end_s = pd.Timestamp.today().strftime("%Y-%m-%d")
        fx_pair = f"{h_ccy}{a_ccy}"
        fx_df = fx_mod.get_fx_history(
            h_ccy,
            a_ccy,
            start_s,
            end_s,
            refresh=refresh,
        )
        if fx_df is None or fx_df.empty:
            raise RuntimeError(f"汇率历史 {fx_pair} 取数失败, 无法计算 AH 溢价率")

    series = calc_premium_series(
        a_df,
        h_df,
        fx_df=fx_df,
        fx_required=(a_ccy != h_ccy),
    )
    meta = {
        "a_symbol": a,
        "h_symbol": h,
        "a_currency": a_ccy,
        "h_currency": h_ccy,
        "fx_pair": fx_pair,
        "fx_adjusted": bool(fx_df is not None and not fx_df.empty),
        "months": int(months),
    }
    return series, meta
