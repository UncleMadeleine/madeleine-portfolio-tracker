"""Provider 源链与数据形状的离线契约测试.

不联网, 验证两件事:
1. 每条源链上的函数都能被健康观测识别 (有 source id, 不是 unknown);
2. 数据契约断言本身能识别坏数据 (价格<=0 / 币种缺失 / 历史乱序等)。
真正打接口的字段契约在 ``test_provider_contract_live.py`` (默认不跑)。
"""

from __future__ import annotations

import pandas as pd
import pytest

from tests._contracts import (
    assert_fx_contract,
    assert_history_contract,
    assert_quote_contract,
)

from tracker import health
from tracker.providers.base import Quote
from tracker.providers.cn_stocks import CNStocksProvider
from tracker.providers.crypto import CryptoProvider
from tracker.providers.global_stocks import GlobalStocksProvider
from tracker.providers.index import IndexProvider
from tracker.symbols import parse

_ALLOWED = {
    "global": {"ibkr", "longport", "eodhd", "yfinance", "akshare"},
    "cn": {"longport", "akshare", "yfinance"},
    "crypto": {"binance", "gate", "hyperliquid", "yfinance"},
    "index": {"akshare", "yfinance", "ccement", "hsi", "cameco"},
}


def _ids(functions) -> list[str]:
    return [health.source_id(fn) for fn in functions]


def test_global_source_ids_are_known():
    provider = GlobalStocksProvider()
    for symbol in ("AAPL", "0700.HK", "SAP.DE"):
        p = parse(symbol)
        for fn in provider.quote_sources(p):
            assert health.source_id(fn) in _ALLOWED["global"]
        for fn in provider.history_sources(p, "2024-01-01", None):
            assert health.source_id(fn) in _ALLOWED["global"]


def test_cn_and_crypto_source_ids_are_known():
    cn = CNStocksProvider()
    p = parse("600519.SS")
    assert set(_ids(cn.quote_sources(p))) <= _ALLOWED["cn"]
    assert set(_ids(cn.history_sources(p, "2024-01-01", None))) <= _ALLOWED["cn"]

    crypto = CryptoProvider()
    p = parse("BTC-USD")
    assert set(_ids(crypto.quote_sources(p))) <= _ALLOWED["crypto"]
    assert set(_ids(crypto.history_sources(p, "2024-01-01", None))) <= _ALLOWED["crypto"]


@pytest.mark.parametrize(
    "key", ["DXY", "VIX", "SPX", "HSI", "HSTECH", "CEMPI", "AU", "URANIUM"]
)
def test_index_source_ids_are_known(key: str):
    provider = IndexProvider()
    p = parse(f"IX.{key}")
    ids = _ids(provider.history_sources(p, "2024-01-01", None))
    assert ids, f"IX.{key}: 没有可用历史源"
    assert set(ids) <= _ALLOWED["index"], f"IX.{key}: 未知来源 {ids}"


def test_no_source_is_unknown():
    """所有域的源链都必须能被 health.source_id 归名."""
    chains = []
    gs = GlobalStocksProvider()
    for symbol in ("AAPL", "0700.HK"):
        chains.extend(gs.quote_sources(parse(symbol)))
        chains.extend(gs.history_sources(parse(symbol), "2024-01-01", None))
    cn = CNStocksProvider()
    chains.extend(cn.quote_sources(parse("600519.SS")))
    chains.extend(cn.history_sources(parse("600519.SS"), "2024-01-01", None))
    crypto = CryptoProvider()
    chains.extend(crypto.quote_sources(parse("BTC-USD")))
    chains.extend(crypto.history_sources(parse("BTC-USD"), "2024-01-01", None))
    for key in ("DXY", "HSI", "CEMPI", "AU", "URANIUM"):
        chains.extend(
            IndexProvider().history_sources(parse(f"IX.{key}"), "2024-01-01", None)
        )
    unknown = [health.source_id(fn) for fn in chains if health.source_id(fn) == "unknown"]
    assert not unknown


def test_quote_contract_accepts_valid_quote():
    assert_quote_contract(
        Quote(
            symbol="AAPL",
            name="Apple",
            price=190.5,
            prev_close=189.0,
            change_pct=0.79,
            currency="USD",
            source="yfinance",
        ),
        "AAPL",
    )


@pytest.mark.parametrize(
    "quote",
    [
        Quote("AAPL", None, -1.0, None, None, "USD", source="yfinance"),
        Quote("AAPL", None, float("nan"), None, None, "USD", source="yfinance"),
        Quote("AAPL", None, 1.0, None, None, "", source="yfinance"),
        Quote("AAPL", None, 1.0, None, None, "USD", source=None),
    ],
)
def test_quote_contract_rejects_bad_data(quote):
    with pytest.raises(AssertionError):
        assert_quote_contract(quote, "AAPL")


def test_history_contract_accepts_valid_frame():
    df = pd.DataFrame(
        {
            "date": pd.to_datetime(["2024-01-02", "2024-01-03"]),
            "open": [1.0, 1.1],
            "high": [1.2, 1.3],
            "low": [0.9, 1.0],
            "close": [1.1, 1.2],
            "volume": [100, 200],
        }
    )
    assert_history_contract(df, "AAPL")


@pytest.mark.parametrize(
    "df",
    [
        pd.DataFrame(),
        pd.DataFrame({"date": ["2024-01-02"], "close": [1.0]}),
        pd.DataFrame(
            {
                "date": pd.to_datetime(["2024-01-03", "2024-01-02"]),
                "open": [1, 1],
                "high": [1, 1],
                "low": [1, 1],
                "close": [1, 1],
            }
        ),
        pd.DataFrame(
            {
                "date": pd.to_datetime(["2024-01-02"]),
                "open": [1],
                "high": [0.5],
                "low": [1],
                "close": [-1],
            }
        ),
    ],
)
def test_history_contract_rejects_bad_frames(df):
    with pytest.raises(AssertionError):
        assert_history_contract(df, "AAPL")


def test_fx_contract_rejects_non_positive():
    assert_fx_contract(7.2, "USD/CNY")
    for bad in (None, 0.0, -1.0, float("nan")):
        with pytest.raises(AssertionError):
            assert_fx_contract(bad, "USD/CNY")
