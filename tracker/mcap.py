"""历史市值序列: 市值对比 (compare --metric mcap / 走势对比页「市值」指标) 用.

口径分域 (与 provider 分域一致, 数据源固定不走源链偏好):
- cn 域 (A股/北交所): 东财估值分析 ak.stock_value_em — 真实日频历史总市值
  (CNY, 含总股本变动), 2018 年至今; B股/em 覆盖不到的自动近似兜底
- global 域 (美股/港股/德英加澳日韩新巴): yfinance 历史股本 get_shares_full
  × 历史收盘价 (能反映真实增发/回购; 股本落在已知事件上按日期前向填充)
- 股本/估值历史不可得时近似兜底: mcap_t = 当前市值 × price_t / price_now
  (只含价格维度, 不反映股本变动; source="approx" 供调用方标注)
- crypto / index: 无总市值语义 → df=None + 原因 (调用方跳过并提示)

序列磁盘缓存 (cache.series_cache 表): em 6h / 股本 7d; cache clear 全部自愈。
输出与 K线 df 同构 (date/open/high/low/close/volume, close=市值, 升序),
下游 resample/compare_payload/比例线全部复用现有纯函数路径。
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass

import pandas as pd

from . import cache as cache_mod
from .symbols import Market, is_pence, parse

# 东财估值分析: 报表级低频数据, 6h 与 fundamentals 同口径
MCAP_EM_TTL = 21600
# 历史股本: 变动由增发/回购公告驱动, 7 天内不影响历史形状
MCAP_SHARES_TTL = 7 * 86400

SOURCE_LABELS = {
    "em": "东财历史总市值",
    "shares": "历史股本×收盘价",
    "approx": "当前市值近似折算",
}


@dataclass
class McapResult:
    code: str
    df: pd.DataFrame | None  # date/open/high/low/close/volume, close=市值 (当地币)
    source: str | None  # em / shares / approx
    currency: str | None
    reason: str | None = None  # df=None 时的不可得原因


# ---------- 纯函数 (单元测试用, 无网络无缓存) ----------


def ohlcv_shim(mcap_df: pd.DataFrame) -> pd.DataFrame:
    """date/close 市值序列 → K线同构 df (open/high/low/close=市值, volume=0).

    下游 clean_ohlc/resample_ohlc/compare_payload/比例线按 OHLC 消费,
    复用全部现有纯函数路径而无需为市值写第二套重采样。
    """
    out = mcap_df.copy()
    for c in ("open", "high", "low"):
        out[c] = out["close"]
    if "volume" not in out.columns:
        out["volume"] = 0.0
    return out.reset_index(drop=True)[
        ["date", "open", "high", "low", "close", "volume"]
    ]


def _map_by_closest_date(price_df: pd.DataFrame, pairs: pd.Series) -> pd.DataFrame:
    """按日期精确对位: price 每个交易日取 pairs (date→value) 的同日值, 缺日丢弃.

    em 与 A股K线同属 A 股交易日历 (东财/akshare/yf 对同一标的日期一致),
    股本事件按日期直接对位即可; 不做前向填充 —— 中间缺日不虚构市值。
    返回 date (Timestamp) / close (value) 两列, 升序去重。
    """
    lookup = {pd.Timestamp(k).normalize(): v for k, v in pairs.items()}
    dates = pd.to_datetime(price_df["date"]).dt.normalize()
    out = pd.DataFrame(
        {"date": dates, "close": dates.map(lookup)},
    ).dropna(subset=["close"])
    return (
        out.drop_duplicates(subset="date", keep="last")
        .sort_values("date")
        .reset_index(drop=True)
    )


def approx_frame(snapshot_mcap: float, price_df: pd.DataFrame) -> pd.DataFrame:
    """近似口径: mcap_t = 当前市值 × price_t / price_now (最后一个有效收盘)."""
    close = pd.to_numeric(price_df["close"], errors="coerce")
    ok = close.notna() & (close > 0)
    price_df = price_df[ok]
    close = close[ok]
    if close.empty:
        return pd.DataFrame(columns=["date", "close"])
    base = float(close.iloc[-1])
    if base <= 0:
        return pd.DataFrame(columns=["date", "close"])
    scaled = snapshot_mcap * close.to_numpy() / base
    pairs = pd.Series(scaled, index=price_df["date"].to_numpy())
    return _map_by_closest_date(price_df, pairs)


def to_common_currency(
    df_map: dict[str, pd.DataFrame],
    currencies: dict[str, str],
    target: str,
) -> tuple[dict[str, pd.DataFrame], list[str]]:
    """各代码市值序列统一折算到 target 币种; 汇率历史不可得的代码保持原币.

    同币种序列原样返回 (不发汇率请求); 每个外币只发一次历史汇率请求
    (与比例线汇率调整同一 fx.get_fx_history 缓存)。返回 warnings 列表
    (调用方 CLI 走 stderr / UI 走 st.warning, 不在本函数打印)。
    """
    if not target:
        return df_map, []
    from . import fx as fx_mod  # 惰性: 只在市值对比路径用到

    target = str(target).upper()
    out: dict[str, pd.DataFrame] = {}
    warnings: list[str] = []
    if not df_map:
        return out, warnings
    start = (
        min(df["date"].min() for df in df_map.values()) - pd.Timedelta(days=10)
    ).strftime("%Y-%m-%d")
    end = pd.Timestamp.today().strftime("%Y-%m-%d")
    rate_tables: dict[str, pd.DataFrame | None] = {}
    for ccy in dict.fromkeys(currencies.values()):
        if ccy != target:
            rate_tables[ccy] = fx_mod.get_fx_history(ccy, target, start, end)
    for code, df in df_map.items():
        ccy = currencies.get(code)
        fx_df = rate_tables.get(ccy)
        if ccy == target or ccy is None:
            out[code] = df
            continue
        if fx_df is None:
            warnings.append(f"{code}: {ccy}→{target} 历史汇率不可得, 保持原币")
            out[code] = df
            continue
        conv = df.copy()
        rates = {pd.Timestamp(r["date"]).normalize(): float(r["close"]) for _, r in fx_df.iterrows()}
        factor = [
            rates[d.normalize()] if d.normalize() in rates else None
            for d in conv["date"]
        ]
        # 汇率折算必须作用于全部 OHLC 列 (市值 df 为 K线同构, open/high/low/close
        # 同为市值口径; 只改 close 会让 clean_ohlc 的一致性校验整行丢弃)
        price_cols = [c for c in ("open", "high", "low", "close") if c in conv.columns]
        for col in price_cols:
            conv[col] = [
                v * rate if rate is not None else None for v, rate in zip(conv[col], factor)
            ]
        conv = conv.dropna(subset=price_cols + ["date"] if "date" in conv.columns else price_cols)
        out[code] = (
            conv.drop_duplicates(subset="date", keep="last")
            .sort_values("date")
            .reset_index(drop=True)
            if not conv.empty
            else df
        )
    return out, warnings


def describe_amount(v: float | None) -> str:
    """市值摘要格式化: ≥1e12 → 万亿, ≥1e8 → 亿 (中文场景习惯口径)."""
    if v is None or v <= 0:
        return "—"
    if v >= 1e12:
        return f"{v / 1e12:.2f}万亿"
    if v >= 1e8:
        return f"{v / 1e8:.2f}亿"
    if v >= 1e4:
        return f"{v / 1e4:.2f}万"
    return f"{v:.0f}"


# ---------- 网络取数层 ----------


def _cache_get(key: str, ttl: int) -> pd.DataFrame | None:
    return cache_mod.get_series_cached(key, ttl=ttl)


def _cache_put(key: str, df: pd.DataFrame) -> None:
    cache_mod.set_series_cached(key, df)


def _em_history(bare: str) -> pd.DataFrame | None:
    """东财估值分析 → date/close (总市值, CNY); 缓存命中率优先, 失败 None."""
    key = f"em_value:{bare}"
    hit = _cache_get(key, MCAP_EM_TTL)
    if hit is not None:
        return hit
    import akshare as ak

    try:
        raw = ak.stock_value_em(symbol=bare)
    except Exception:
        return None
    if raw is None or raw.empty or "总市值" not in raw.columns:
        return None
    df = pd.DataFrame(
        {
            "date": pd.to_datetime(raw["数据日期"], errors="coerce"),
            "close": pd.to_numeric(raw["总市值"], errors="coerce"),
        }
    ).dropna()
    df = df[df["close"] > 0].sort_values("date").reset_index(drop=True)
    if df.empty:
        return None
    _cache_put(key, df)
    return df


def _shares_history(yahoo: str) -> pd.DataFrame | None:
    """yfinance 历史股本 (date/close=股本数); 全量历史一次性缓存 7 天."""
    key = f"shares_full:{yahoo}"
    hit = _cache_get(key, MCAP_SHARES_TTL)
    if hit is not None:
        return hit
    try:
        import yfinance as yf

        srs = yf.Ticker(yahoo).get_shares_full(start="1980-01-01")
    except Exception:
        return None
    if srs is None or len(srs) == 0:
        return None
    df = pd.DataFrame(
        {
            "date": pd.to_datetime(srs.index, errors="coerce", utc=True),
            "close": pd.to_numeric(list(srs.values), errors="coerce"),
        }
    ).dropna()
    df["date"] = df["date"].dt.tz_convert(None)  # 剥时区 (与 price date 对齐)
    df = (
        df[df["close"] > 0]
        .groupby("date", as_index=False)["close"]
        .last()
        .sort_values("date")
        .reset_index(drop=True)
    )
    if df.empty:
        return None
    _cache_put(key, df)
    return df


# ---------- 编排入口 (CLI / 页面共用) ----------


def _price_currency_for(p) -> str:
    """市值序列的报价币种 (跟随价格实际币种, 而不是市场静态币种).

    parse() 对 .L/.IL 一律给 GBP, 但 LSE 主板/IOB 存在 USD/EUR 计价线
    (CSPX.L / VWRA.L / KAP.IL): 历史 K 线按实际报价币种保留 (见
    providers/global_stocks._yahoo_history 的便士折算判据)。市值 = 股本 ×
    该价格, 币种必须与价格一致, 否则 compare --metric mcap 会拿 USD 数字按
    GBP 汇率折算 (约 1.3 倍偏差) 且不告警。
    """
    if p.market is not Market.GB:
        return p.currency
    from .providers.global_stocks import _yahoo_quote_currency

    ccy = _yahoo_quote_currency(p.yahoo).strip()
    # 便士 (GBp/GBX) 的 K 线已按 LSE 惯例 ÷100 折成英镑; 未知币种同样按
    # 英镑兜底 (与 parse 的市场静态币种一致, 维持既有行为)
    if ccy and not is_pence(ccy) and ccy.upper() not in ("GBP", "GBX"):
        return ccy.upper()
    return "GBP"


def history_for(symbol: str, price_df: pd.DataFrame) -> McapResult:
    """单代码历史市值序列 → McapResult (df 为 K线同构, close=当地币市值)."""
    try:
        p = parse(symbol)
    except ValueError as e:
        return McapResult(symbol, None, None, None, reason=str(e))

    if p.type == "crypto":
        return McapResult(symbol, None, None, None, reason="加密货币无总市值语义")
    if p.type == "index":
        return McapResult(symbol, None, None, None, reason="指数无总市值语义")
    if price_df is None or price_df.empty:
        return McapResult(symbol, None, None, p.currency, reason="无K线数据")
    price_ccy = _price_currency_for(p)

    mcap_pairs: pd.Series | None = None
    source = None

    if p.type == "cn":
        em = _em_history(p.yahoo.split(".")[0])
        if em is not None:
            mcap_pairs = pd.Series(em["close"].to_numpy(), index=em["date"])
            source = "em"
    else:  # global
        sh = _shares_history(p.yahoo)
        if sh is not None:
            close = pd.to_numeric(price_df["close"], errors="coerce")
            # 股本是稀疏事件序列 (增发/回购公告日): 事件间股数不变 → 前向填充,
            # 每个交易日取 ≤ 该日期 的最近事件股数; 首个事件之前 (无已知股数) 丢弃
            events = pd.DataFrame(
                {
                    "date": pd.to_datetime(sh["date"]).dt.normalize(),
                    "count": pd.to_numeric(sh["close"], errors="coerce"),
                }
            ).dropna().sort_values("date")
            if not events.empty:
                ev_dates = list(events["date"])
                ev_vals = list(events["count"])
                counts = {}
                for d in pd.to_datetime(price_df["date"]).dt.normalize():
                    j = bisect_right(ev_dates, d) - 1
                    if j >= 0:
                        counts[d] = ev_vals[j]
                share_counts = pd.to_datetime(price_df["date"]).map(
                    lambda d: counts.get(d.normalize())
                )
                mcap_pairs = pd.Series(
                    (close * share_counts).to_numpy(),
                    index=price_df["date"].to_numpy(),
                )
                source = "shares"

    if mcap_pairs is None:
        # 近似兜底: 当前市值 (fundamentals 6h 缓存批量) × 价格比例
        from . import fundamentals

        try:
            snapshot = fundamentals.fetch_market_caps([p.yahoo]).get(p.yahoo)
        except Exception:
            snapshot = None
        if snapshot is None or snapshot <= 0:
            return McapResult(symbol, None, None, price_ccy, reason="无市值数据")
        approx = approx_frame(float(snapshot), price_df)
        if approx.empty:
            return McapResult(symbol, None, None, price_ccy, reason="无有效价格")
        return McapResult(symbol, ohlcv_shim(approx), "approx", price_ccy)

    attached = _map_by_closest_date(price_df, mcap_pairs)
    if attached.empty:
        return McapResult(symbol, None, None, price_ccy, reason="市值与K线无重叠日期")
    return McapResult(symbol, ohlcv_shim(attached), source, price_ccy)


def compare_market_caps(
    frames: dict[str, pd.DataFrame],
    target_currency: str | None,
) -> tuple[dict[str, pd.DataFrame], list[str], dict[str, dict]]:
    """对比入口: 股价K线 frames → 历史市值 frames (统一折算 target 币种).

    返回 (市值 frames (K线同构, close=市值, 已折算), warnings, 元信息).
    元信息按代码给 {source, currency}; 无视 target (None) 则不换算 (原币).
    """
    originals: dict[str, pd.DataFrame] = {}
    currencies: dict[str, str] = {}
    sources: dict[str, dict] = {}
    warnings: list[str] = []
    for code, df in frames.items():
        res = history_for(code, df)
        if res.df is None:
            warnings.append(
                f"{code}: 市值序列不可得 ({res.reason or '无数据'}), 已跳过"
            )
            continue
        originals[code] = res.df
        currencies.setdefault(code, res.currency or parse(code).currency)
        sources[code] = {
            "source": res.source,
            "currency": res.currency,
            "source_label": SOURCE_LABELS.get(res.source, res.source),
        }
    if len(originals) < 1:
        return {}, warnings, {}
    converted, fx_warnings = to_common_currency(originals, currencies, target_currency)
    warnings.extend(fx_warnings)
    return converted, warnings, sources
