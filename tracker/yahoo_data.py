"""Yahoo Finance 直连取数: 历史 OHLCV 与基本面指标快照.

历史上这两类数据经 OpenBB 的 yfinance provider 封装, 但 OpenBB 本身只是一层
薄适配 (底层仍是 ``yfinance.Ticker.history`` / ``Ticker.get_info``), 却带来
~5s 冷启动与 30+ 个 openbb-* 依赖。这里直接调用 yfinance, 并保持与旧封装
一致的口径:

- 历史K线: 不复权 (``auto_adjust=False``), 与旧 OpenBB 默认 ``splits_only``
  一致; 索引统一成 tz-naive 的 ``date`` 列, 列名小写;
- 基本面: 字段名沿用旧 OpenBB metrics 口径 (``pe_ratio`` / ``book_value`` …
  以及 ``dividend_yield_5y_avg`` 的小数比例), 调用方无需改字段映射。

网络调用仍由调用方通过 ``with_timeout`` 或本模块默认超时兜底, 避免
半开连接/内部重试把 CLI/页面主线程无限挂住。
"""

from __future__ import annotations

import math

import pandas as pd

from .deps import yfinance as _yf
from .util import with_timeout

#: 历史K线单码请求硬超时 (yfinance 内部可能重试/半开连接)
HISTORY_TIMEOUT = 60.0

_OHLC_COLUMNS = ("date", "open", "high", "low", "close", "volume")


def _download(symbol: str, start_date: str, end_date: str | None):
    """单代码日线原始下载 (与旧 OpenBB yfinance provider 同参数口径)."""
    kwargs: dict[str, object] = {
        "tickers": symbol.upper(),
        "start": start_date,
        "interval": "1d",
        "auto_adjust": False,
        "back_adjust": False,
        "actions": False,
        "progress": False,
        "ignore_tz": True,
        "keepna": False,
        "group_by": "ticker",
        "threads": False,
    }
    if end_date:
        # 项目约定 end_date 为闭区间 (见 providers/index._slice_range), 而
        # yfinance 的 end 是开区间 → 加一天, 保证结束日当天那根 K 线不丢。
        kwargs["end"] = (
            pd.Timestamp(end_date) + pd.Timedelta(days=1)
        ).strftime("%Y-%m-%d")
    return _yf().download(**kwargs)


def history(
    symbol: str,
    start_date: str,
    end_date: str | None = None,
    *,
    ensure_volume: bool = False,
    timeout: float = HISTORY_TIMEOUT,
) -> pd.DataFrame:
    """日线历史 → DataFrame(date/open/high/low/close/volume), 升序.

    网络调用放在守护线程里硬超时; 无数据时抛 RuntimeError (调用方按源链降级)。
    """
    df = with_timeout(_download, timeout, symbol, start_date, end_date)
    if df is None or df.empty:
        raise RuntimeError(f"{symbol}: Yahoo 无历史数据")
    if isinstance(df.columns, pd.MultiIndex):
        # 单代码 + group_by="ticker": level 0 只有该代码, 拍平即可
        df = df.droplevel(0, axis=1)
    df = df.rename(columns=str.lower).reset_index()
    date_col = "date" if "date" in df.columns else df.columns[0]
    df = df.rename(columns={date_col: "date"})
    dates = pd.to_datetime(df["date"], errors="coerce")
    if getattr(dates.dt, "tz", None) is not None:
        dates = dates.dt.tz_localize(None)
    df["date"] = dates
    df = df.dropna(subset=["date", "close"])
    keep = [c for c in _OHLC_COLUMNS if c in df.columns]
    if ensure_volume and "volume" not in keep:
        df["volume"] = 0.0
        keep.append("volume")
    return df[keep].sort_values("date").reset_index(drop=True)


def info(symbol: str) -> dict:
    """单代码 ``Ticker.get_info()`` 原始快照 (空响应返回 {})."""
    return _yf().Ticker(symbol).get_info() or {}


def _finite(value: object) -> float | None:
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def metrics_from_info(symbol: str, data: dict) -> dict | None:
    """yfinance info → 旧 OpenBB metrics 行 (纯函数, 便于离线测试).

    缺 ``52WeekChange`` 的快照按无效处理 —— 旧 OpenBB provider 同样以该字段
    判断数据是否完整, 保留此口径避免基本面覆盖范围发生漂移。
    ``fiveYearAvgDividendYield`` 是百分数 (0.46 = 0.46%), 旧 OpenBB 会 /100
    成小数比例, 这里保持一致 (valuation 的 _fraction_or_percent 依赖该口径)。
    """
    if not data or data.get("52WeekChange") is None:
        return None
    five_year = _finite(data.get("fiveYearAvgDividendYield"))
    return {
        "symbol": symbol,
        "currency": data.get("currency"),
        "market_cap": _finite(data.get("marketCap")),
        "pe_ratio": _finite(data.get("trailingPE")),
        "forward_pe": _finite(data.get("forwardPE")),
        "price_to_book": _finite(data.get("priceToBook")),
        "dividend_yield": _finite(data.get("dividendYield")),
        "dividend_yield_5y_avg": None if five_year is None else five_year / 100.0,
        "payout_ratio": _finite(data.get("payoutRatio")),
        "eps_ttm": _finite(data.get("trailingEps")),
        "book_value": _finite(data.get("bookValue")),
    }


def metrics_row(symbol: str) -> dict:
    """单代码 yfinance 指标行; 无有效数据时抛 RuntimeError."""
    row = metrics_from_info(symbol, info(symbol))
    if row is None:
        raise RuntimeError(f"{symbol}: 数据源未返回估值指标")
    return row
