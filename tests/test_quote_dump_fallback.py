"""yfinance 批量行情字段回退测试 (离线, 纯数据).

回归背景: OpenBB yfinance quote 对部分证券 (多为 ETF: GLD/TLT/SCHD/3032.HK…)
的 quoteSummary 缺 last_price/close, 只有 open; _yahoo_batch 整批只按
last_price/price/close 取价, 55/682 只自选跌入逐代码源链 (单只 10s+, 8 并发
也拖 30s+), 首页冷加载分钟级。修复: _quote_from_dump 逐级回退到 open。
"""

from __future__ import annotations

import pytest

from tracker.providers.global_stocks import _quote_from_dump
from tracker.symbols import parse


@pytest.fixture
def us_symbol():
    return parse("GLD")


@pytest.fixture
def hk_symbol():
    return parse("3032.HK")


def test_full_dump_unchanged(us_symbol):
    """常规 dump (有 last_price): 输出与既往行为一致, 回退不改变取值顺序."""
    d = {
        "symbol": "GLD",
        "last_price": 377.91,
        "prev_close": 393.41,
        "change_percent": -3.95,
        "currency": "USD",
        "name": "SPDR Gold Shares",
    }
    q = _quote_from_dump(us_symbol, d)
    assert q is not None
    assert q.price == 377.91
    assert q.prev_close == 393.41
    assert q.change_pct == pytest.approx(-3.95)
    assert q.currency == "USD"


def test_missing_last_falls_back_to_open(us_symbol):
    """核心回归: 无 last_price/price/close 时用 open, 不再丢弃整条行情."""
    d = {
        "symbol": "GLD",
        "open": 379.76,
        "prev_close": 393.41,
        "currency": "USD",
        "name": "SPDR Gold Shares",
    }
    q = _quote_from_dump(us_symbol, d)
    assert q is not None
    assert q.price == 379.76
    assert q.prev_close == 393.41
    # 无自报涨跌幅时按 open/prev 推导
    assert q.change_pct == pytest.approx((379.76 / 393.41 - 1) * 100)
    assert q.name == "SPDR Gold Shares"


def test_open_only_still_extracts(us_symbol):
    """连 prev_close 也缺 (最极端 dump): 至少给出价格, 涨跌幅留空."""
    d = {"symbol": "GLD", "open": 100.0, "currency": "USD"}
    q = _quote_from_dump(us_symbol, d)
    assert q is not None
    assert q.price == 100.0
    assert q.prev_close is None
    assert q.change_pct is None


def test_all_price_fields_missing_returns_none(us_symbol):
    """纯死代码 dump (无任何价格字段): 仍返回 None, 交给源链/负缓存."""
    d = {"symbol": "GLD", "currency": "USD", "name": "SPDR Gold Shares"}
    assert _quote_from_dump(us_symbol, d) is None


def test_open_fallback_respects_pence(us_symbol):
    """英股 (GBp) 走 open 回退时同样要 ÷100, 不能放大 100 倍."""
    d = {
        "symbol": "BP.L",
        "open": 450.0,
        "prev_close": 44800,
        "currency": "GBp",
    }
    q = _quote_from_dump(us_symbol, d)
    assert q is not None
    assert q.currency == "GBP"
    assert q.price == pytest.approx(4.5)
    assert q.prev_close == pytest.approx(448.0)


def test_open_fallback_hk_currency_passthrough(hk_symbol):
    """港股 dump: 货币按数据源自报 (HKD) 透传."""
    d = {
        "symbol": "3032.HK",
        "open": 4.288,
        "prev_close": 4.304,
        "currency": "HKD",
    }
    q = _quote_from_dump(hk_symbol, d)
    assert q is not None
    assert q.price == 4.288
    assert q.currency == "HKD"
