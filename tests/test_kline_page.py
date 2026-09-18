"""K线页面渲染门控测试 (AppTest, 不联网).

自定义组件 (st.components.v2) 无法在 AppTest 中承载, 用桩替代 —— 这里验证的是
「何时进入渲染分支」, 即提交/参数变化/组件回调触发的 rerun 是否仍画出图表。
"""
from __future__ import annotations

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

import tracker.prices as prices
import tracker.ui.kline_page as kline_page

_SCRIPT = """
import tracker.ui.kline_page as kp
kp.set_quick_symbols(["AAPL"])
kp.render_kline_controls(False)
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
def kline_app(monkeypatch):
    monkeypatch.setattr(prices, "get_ohlc", _fake_ohlc)
    monkeypatch.setattr(kline_page, "_KLINE_CHART", lambda **kw: None)
    kline_page.cached_kline.clear()
    return AppTest.from_string(_SCRIPT, default_timeout=30).run()


def test_no_preload_before_submit(kline_app):
    """页面启动不预加载: 未提交时不取数、不渲染图表."""
    assert len(kline_app.metric) == 0
    assert any("不会预加载" in i.value for i in kline_app.info)


def test_chart_survives_rerun_without_new_submit(kline_app):
    """提交后, 空转 rerun 与参数变化必须继续渲染图表 (旧实现会清空图表)."""
    kline_app.button[0].click().run()
    assert len(kline_app.metric) == 4

    kline_app.run()  # 组件 setValue(need_more) 触发的 rerun
    assert len(kline_app.metric) == 4

    kline_app.multiselect[0].set_value([5, 10]).run()  # 改均线: 只重绘, 不重新取数
    assert len(kline_app.metric) == 4
    assert kline_app.info == []


def test_range_change_refetches(kline_app):
    """改范围是数据参数: 重新取数并保持图表可见."""
    kline_app.button[0].click().run()
    kline_app.selectbox[0].set_value(6).run()
    assert len(kline_app.metric) == 4
    assert kline_app.info == []
