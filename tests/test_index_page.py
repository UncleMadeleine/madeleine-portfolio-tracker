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

    # CEMPI 水泥网端点为周K原生数据, 其余指数日频 (与真实数据源一致)
    if "CEMPI" in symbol.upper() and "CEMPIPO" not in symbol.upper():
        idx = pd.date_range("2024-01-01", periods=12, freq="7D")
    else:
        idx = pd.date_range("2024-01-01", periods=30, freq="D")
    return pd.DataFrame(
        {
            "date": idx,
            "open": 1.0,
            "high": 1.2,
            "low": 0.9,
            "close": [1.0 + i * 0.01 for i in range(len(idx))],
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


def test_weekly_only_index_hides_daily_option(index_app):
    """CEMPI (周K原生数据): 周期下拉无日K选项, 默认周K, 提示数据源粒度."""
    app = index_app
    symbol_box = app.selectbox[0]
    cempi_label = next(
        o for o in symbol_box.options if "CEMPI" in o and "42.5" not in o
    )
    symbol_box.select(cempi_label).run()
    period_box = app.selectbox(key="index_period")
    assert "日K" not in period_box.options
    assert "周K" in period_box.options
    assert period_box.value == "weekly"
    assert period_box.help == "该指数数据源为周K, 更细周期不可用"
    assert len(app.metric) == 4  # 图表摘要照常渲染


def test_daily_ok_index_keeps_daily_default(monkeypatch):
    """普通指数 (DXY): 日K选项仍在且默认日K, 不受仅周K逻辑影响 (独立会话)."""
    from tracker.ui import kline_page

    monkeypatch.setattr(prices, "get_index_history", _fake_index_history)
    monkeypatch.setattr(kline_page, "_KLINE_CHART", lambda **kw: None)
    index_page.cached_index_kline.clear()
    app = AppTest.from_string(_SCRIPT, default_timeout=30).run()
    symbol_box = app.selectbox[0]
    dxy_label = next(o for o in symbol_box.options if "美元指数" in o)
    symbol_box.select(dxy_label).run()
    period_box = app.selectbox(key="index_period")
    assert "日K" in period_box.options
    assert not period_box.help  # 普通指数无「仅周K」提示
