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


@pytest.fixture
def gb_symbol():
    return parse("LSEG.L")


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


def test_nan_prev_and_change_become_none(us_symbol):
    """NaN prev_close/change_percent 不得漏进 Quote 变成 +NaN% 行情.

    回归背景: 价格字段有「有限正数」守卫而 prev/chg 只做 None 检查, 停牌/
    坏响应里的 NaN 会 float() 成 nan 直接漏进 Quote (其余数据源均有过滤)。
    """
    d = {
        "symbol": "GLD",
        "last_price": 377.91,
        "prev_close": float("nan"),
        "change_percent": float("nan"),
        "currency": "USD",
        "name": "SPDR Gold Shares",
    }
    q = _quote_from_dump(us_symbol, d)
    assert q is not None
    assert q.price == 377.91
    assert q.prev_close is None
    assert q.change_pct is None


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


def test_zero_last_price_falls_back_to_valid_close(us_symbol):
    """last_price=0 (停牌/坏响应) 不是有效价: 继续回退到有效的 close."""
    d = {
        "symbol": "GLD",
        "last_price": 0.0,
        "close": 393.41,
        "currency": "USD",
    }
    q = _quote_from_dump(us_symbol, d)
    assert q is not None
    assert q.price == pytest.approx(393.41)


def test_non_positive_or_non_finite_prices_return_none(us_symbol):
    """0/负/NaN/inf 都不能当有效行情 (会产出市值 0 并误触下限阈值)."""
    for bad in (0.0, -1.0, float("nan"), float("inf")):
        d = {"symbol": "GLD", "last_price": bad, "currency": "USD"}
        assert _quote_from_dump(us_symbol, d) is None, bad


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


def test_open_fallback_lseg_pence(gb_symbol):
    """英股主板样本 LSEG.L (伦交所集团): GBp 便士 → 英镑 (8768p = £87.68).

    与 BP.L 同一路径, 锁定一个非能源类的 LSE 大盘股口径。
    """
    d = {
        "symbol": "LSEG.L",
        "open": 8768.0,
        "prev_close": 8684.0,
        "currency": "GBp",
        "name": "London Stock Exchange Group plc",
    }
    q = _quote_from_dump(gb_symbol, d)
    assert q is not None
    assert q.currency == "GBP"
    assert q.price == pytest.approx(87.68)
    assert q.prev_close == pytest.approx(86.84)
    assert q.change_pct == pytest.approx((8768.0 / 8684.0 - 1) * 100)
    assert q.name == "London Stock Exchange Group plc"


def test_lse_missing_currency_falls_back_to_pence(gb_symbol):
    """回归: .L 主板币种字段缺失时按 LSE 惯例兜底 GBp, 不能透传 GBP 放大 100 倍."""
    d = {"symbol": "LSEG.L", "last_price": 8768.0, "prev_close": 8684.0}
    q = _quote_from_dump(gb_symbol, d)
    assert q is not None
    assert q.currency == "GBP"
    assert q.price == pytest.approx(87.68)
    assert q.prev_close == pytest.approx(86.84)


def test_lse_usd_quoted_etf_not_scaled():
    """.L 后缀 ≠ 一定便士: LSE 亦有美元计价的 UCITS ETF (HEAL.L, USD 10.935)。

    注意 HEAL.L 是 iShares Healthcare Innovation UCITS ETF, 不是希思罗机场 ——
    Heathrow Airport Holdings 2007 年已私有化退市, Yahoo 上无对应股票。
    币种以数据源自报为准: USD 报价不能再 ÷100。
    """
    p = parse("HEAL.L")
    d = {
        "symbol": "HEAL.L",
        "last_price": 10.935,
        "prev_close": 10.915,
        "currency": "USD",
        "name": "iShares Healthcare Innovation UCITS ETF USD (Acc)",
    }
    q = _quote_from_dump(p, d)
    assert q is not None
    assert q.price == 10.935
    assert q.currency == "USD"
    assert q.change_pct == pytest.approx((10.935 / 10.915 - 1) * 100)


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


def test_iob_symbol_usd_quote_not_scaled(us_symbol):
    """.IL (LSE 国际挂盘) 以 USD 计价: 不做 LSE 主板便士折算 (KAP.IL=USD 64.4)."""
    p = parse("KAP.IL")
    d = {
        "symbol": "KAP.IL",
        "last_price": 64.4,
        "prev_close": 63.2,
        "currency": "USD",
        "name": "NATIONAL ATOMIC CO KAZATOMPROM",
    }
    q = _quote_from_dump(p, d)
    assert q is not None
    assert q.price == 64.4
    assert q.currency == "USD"
    assert q.change_pct == pytest.approx((64.4 / 63.2 - 1) * 100)


def test_iob_symbol_missing_currency_not_pence(us_symbol):
    """回归: .IL 币种字段缺失时兜底 USD, 不能按 LSE 惯例兜底 GBp (会错除 100)."""
    p = parse("KAP.IL")
    d = {"symbol": "KAP.IL", "last_price": 64.4, "prev_close": 63.2}
    q = _quote_from_dump(p, d)
    assert q is not None
    assert q.price == 64.4
    assert q.currency == "USD"
