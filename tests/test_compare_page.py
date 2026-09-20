"""多股对比渲染门控测试 (AppTest, 不联网): 通过「K线」页入口验证走势对比子 tab."""
from __future__ import annotations

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

import tracker.prices as prices
import tracker.ui.kline_page as kline_page

_SCRIPT = """
import tracker.ui.kline_page as kp
kp.set_quick_symbols(["AAPL", "0700.HK"])
kp.render_kline_page(False)
"""


def _fake_ohlc(symbol, months=12, prefer_akshare=False, **kw):
    idx = pd.date_range("2024-01-01", periods=30, freq="D")
    return pd.DataFrame(
        {
            "date": idx,
            "open": 1.0,
            "high": 1.2,
            "low": 0.9,
            "close": [1.0 + i * 0.01 for i in range(30)],
            "volume": 100.0,
        }
    )


@pytest.fixture
def compare_app(monkeypatch):
    monkeypatch.setattr(prices, "get_ohlc", _fake_ohlc)
    monkeypatch.setattr(kline_page, "_KLINE_CHART", lambda **kw: None)
    kline_page.cached_kline.clear()
    return AppTest.from_string(_SCRIPT, default_timeout=30).run()


def test_compare_renders_chart(compare_app):
    """走势对比 tab 默认选中前两个候选代码, 直接取数渲染 (区间涨跌 + 归一化说明)."""
    captions = [c.value for c in compare_app.caption]
    assert any("区间首个收盘归一化" in c for c in captions)
    assert any("区间涨跌" in m.value for m in compare_app.markdown)


def test_compare_gone_from_portfolio_page(compare_app):
    """组合页不再提供走势对比 tab (迁移后属 K线子功能): app.py 无该分支."""
    import inspect

    import tracker.ui.app as app_mod

    src = inspect.getsource(app_mod)
    assert "走势对比" not in src
    assert "render_compare_chart" not in src
