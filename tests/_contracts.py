"""行情/历史/汇率的数据契约断言 (离线 schema 测试与线上联调共用).

「数据健康」之外的第二道防线: 数据源可以失败, 但**成功返回的形状必须一致**。
这里只校验与估值/曲线正确性强相关的字段, 不锁死可选字段。
"""

from __future__ import annotations

import math
import re

import pandas as pd

_CURRENCY_RE = re.compile(r"^[A-Z]{2,6}$")


def assert_quote_contract(quote, symbol: str) -> None:
    """单条行情: 代码一致 / 价格为有限正数 / 币种规范 / 来源已打标."""
    from tracker.symbols import parse

    expected = parse(symbol).yahoo
    assert quote.symbol == expected, f"{symbol}: symbol={quote.symbol!r}"
    assert isinstance(quote.price, (int, float)), f"{symbol}: price 非数值"
    assert math.isfinite(quote.price) and quote.price > 0, (
        f"{symbol}: 价格无效 {quote.price!r}"
    )
    assert isinstance(quote.currency, str) and _CURRENCY_RE.match(quote.currency), (
        f"{symbol}: 币种不规范 {quote.currency!r}"
    )
    if quote.prev_close is not None:
        assert math.isfinite(quote.prev_close) and quote.prev_close > 0, (
            f"{symbol}: 昨收无效 {quote.prev_close!r}"
        )
    if quote.change_pct is not None:
        assert math.isfinite(quote.change_pct), (
            f"{symbol}: 涨跌幅非有限值 {quote.change_pct!r}"
        )
    assert quote.source, f"{symbol}: source 未打标 (健康观测会归到 unknown)"
    # 负净资产在业务上可能真实存在, 这里只挡非有限值 (NaN/Inf 会污染提醒)
    if quote.book_value is not None:
        assert math.isfinite(quote.book_value), (
            f"{symbol}: 每股净资产非有限值 {quote.book_value!r}"
        )
    if quote.market_cap is not None:
        assert math.isfinite(quote.market_cap) and quote.market_cap >= 0, (
            f"{symbol}: 市值无效 {quote.market_cap!r}"
        )


def assert_history_contract(df: pd.DataFrame, symbol: str) -> None:
    """历史K线: 非空 / 必需列 / 日期升序 / 收盘为正 / 高低价自洽."""
    assert isinstance(df, pd.DataFrame), f"{symbol}: 历史结果不是 DataFrame"
    assert not df.empty, f"{symbol}: 历史结果为空"
    required = {"date", "open", "high", "low", "close"}
    missing = required - set(df.columns)
    assert not missing, f"{symbol}: 缺少列 {sorted(missing)}"

    dates = pd.to_datetime(df["date"], errors="coerce")
    assert dates.notna().all(), f"{symbol}: 存在无法解析的日期"
    assert dates.is_monotonic_increasing, f"{symbol}: 日期未按升序排列"

    close = pd.to_numeric(df["close"], errors="coerce")
    assert close.notna().all(), f"{symbol}: 收盘价含 NaN"
    assert (close > 0).all(), f"{symbol}: 存在非正收盘价"
    assert math.isfinite(float(close.iloc[-1])), f"{symbol}: 最新收盘价非有限值"

    high = pd.to_numeric(df["high"], errors="coerce")
    low = pd.to_numeric(df["low"], errors="coerce")
    both = high.notna() & low.notna()
    if both.any():
        assert (high[both] >= low[both]).all(), f"{symbol}: 最高价低于最低价"

    if "volume" in df.columns:
        volume = pd.to_numeric(df["volume"], errors="coerce").fillna(0)
        assert (volume >= 0).all(), f"{symbol}: 成交量为负"


def assert_fx_contract(rate: float | None, pair: str) -> None:
    """汇率: 有限正数 (负汇率会把市值算成负数)."""
    assert rate is not None, f"{pair}: 未取到汇率"
    assert math.isfinite(rate) and rate > 0, f"{pair}: 汇率无效 {rate!r}"
