"""指数页面测试 (AppTest, 不联网): 选择即拉取渲染, 无查询按钮."""
from __future__ import annotations

import pytest
from streamlit.testing.v1 import AppTest

import tracker.prices as prices
import tracker.ui.index_page as index_page

_SCRIPT = """
import tracker.ui.index_page as ip
ip.render_index_page()
"""


def _fake_index_history(symbol, months=12, **kw):
    import pandas as pd

    idx = pd.date_range("2024-01-01", periods=30, freq="D")
    return pd.DataFrame(
        {
            "date": idx,
            "open": 1.0,
            "high": 1.2,
            "low": 0.9,
            "close": [1.0 + i * 0.01 for i in range(30)],
            "volume": 0.0,
        }
    )


@pytest.fixture
def index_app(monkeypatch):
    from tracker.ui import kline_page

    monkeypatch.setattr(prices, "get_index_history", _fake_index_history)
    monkeypatch.setattr(kline_page, "_KLINE_CHART", lambda **kw: None)
    index_page.cached_index_kline.clear()
    return AppTest.from_string(_SCRIPT, default_timeout=30).run()


def test_no_query_button(index_app):
    """指数页不再有查询按钮: 下拉切换即取数."""
    assert len(index_app.button) == 0


def test_selection_auto_fetches(index_app):
    """页面渲染即按当前选中指数取数并渲染 (4 个摘要指标)."""
    assert len(index_app.metric) == 4
    assert index_app.info == []


def test_switch_symbol_refetches(index_app):
    """切换指数/范围立即重新取数并保持图表可见."""
    assert len(index_app.metric) == 4
    index_app.selectbox[1].set_value(6).run()  # 范围
    assert len(index_app.metric) == 4
    assert index_app.info == []

    index_app.selectbox[0].select(index_app.selectbox[0].options[1]).run()  # 指数
    assert len(index_app.metric) == 4
    assert index_app.info == []
