"""线上契约测试: 真实调用各数据源, 校验返回字段仍符合估值/绘图口径.

默认测试集通过 ``-m "not network"`` 排除本文件。需要时显式运行::

    pytest -m network tests/test_provider_contract_live.py -q

它不追求「所有源都可用」(免费源本就会限流/改字段), 而是验证**当前成功的
源**返回的数据形状正确: 价格为有限正数、币种规范、历史日期升序且收盘为正、
汇率为正。失败即说明某个上游的 schema 变了, 需要更新对应 provider。
"""

from __future__ import annotations

import pandas as pd
import pytest

from tests._contracts import (
    assert_fx_contract,
    assert_history_contract,
    assert_quote_contract,
)

from tracker import fx as fx_mod
from tracker import prices
from tracker import filings as filings_mod

pytestmark = pytest.mark.network

_QUOTE_SYMBOLS = ["AAPL", "0700.HK", "600519.SS", "BTC-USD"]
_HISTORY_SYMBOLS = ["AAPL", "0700.HK", "600519.SS", "BTC-USD"]
_INDEX_KEYS = ["DXY", "SPX", "HSI", "CEMPI", "AU", "URANIUM"]


@pytest.mark.timeout(180)
@pytest.mark.parametrize("symbol", _QUOTE_SYMBOLS)
def test_live_quote_contract(symbol: str):
    quote = prices.get_quote(symbol, refresh=True)
    assert_quote_contract(quote, symbol)


@pytest.mark.timeout(300)
@pytest.mark.parametrize("symbol", _HISTORY_SYMBOLS)
def test_live_history_contract(symbol: str):
    df = prices.get_history(symbol, months=3)
    assert_history_contract(df, symbol)
    last = pd.to_datetime(df["date"]).max()
    # 交易日历差异 (周末/长假) 留足余量; 目的是抓「接口返回了半年前的旧数据」
    assert last >= pd.Timestamp.today().normalize() - pd.Timedelta(days=14), (
        f"{symbol}: 最新一根 K线过旧 ({last.date()})"
    )


@pytest.mark.timeout(300)
@pytest.mark.parametrize("key", _INDEX_KEYS)
def test_live_index_history_contract(key: str):
    df = prices.get_index_history(f"IX.{key}", months=12)
    assert_history_contract(df, f"IX.{key}")


@pytest.mark.timeout(120)
@pytest.mark.parametrize("src,dst", [("USD", "CNY"), ("HKD", "CNY")])
def test_live_fx_contract(src: str, dst: str):
    assert_fx_contract(fx_mod.get_rate(src, dst), f"{src}/{dst}")


@pytest.mark.timeout(240)
@pytest.mark.parametrize(
    "symbol",
    ["600519.SS", "0700.HK", "CBA.AX", "BP.L", "7203.T", "005930.KS", "AAPL"],
)
def test_live_filings_contract(symbol: str):
    """官方财报链接仍可解析: 标题/日期/URL/来源齐全, 且至少有一条."""
    items = filings_mod.fetch_filings(symbol, limit=3, refresh=True)
    assert items, f"{symbol}: 未取到财报链接"
    for item in items:
        assert item.title and item.date and item.url.startswith("http")
        assert item.kind and item.source
        assert len(item.date) == 10
