"""指数页面门控测试 (AppTest, 不联网): 提交/重跑/改范围是否进入渲染分支."""
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


def test_no_preload_before_query(index_app):
    """页面启动不预加载: 无指标渲染, 显示提示."""
    assert len(index_app.metric) == 0
    assert any("不会预加载" in i.value for i in index_app.info)


def test_query_renders_metrics(index_app):
    """点查询后进入渲染分支 (4 个摘要指标)."""
    index_app.button[0].click().run()
    assert len(index_app.metric) == 4


def test_rerun_keeps_chart(index_app):
    """空转 rerun 与参数变化只重绘, 不清空图表."""
    index_app.button[0].click().run()
    index_app.run()
    assert len(index_app.metric) == 4
    assert index_app.info == []


def test_month_change_refetches_and_keeps_chart(index_app):
    """改范围重新取数并保持图表可见."""
    index_app.button[0].click().run()
    index_app.selectbox[1].select(6).run()
    assert len(index_app.metric) == 4
    assert index_app.info == []
