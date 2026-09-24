"""走势对比搜索接口集成验证 (AppTest, 不联网): 搜索 → 选中 → 追加进对比代码."""
from __future__ import annotations

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

import tracker.prices as prices
import tracker.search as search_mod
import tracker.ui.kline_page as kline_page

_SCRIPT = """
import tracker.ui.kline_page as kp
kp.set_quick_symbols(["AAPL"])
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


def _fake_grouped(query, limit_per_domain=8):
    return {
        "cn": [{"code": "600519.SS", "name": "贵州茅台", "market": "A", "type": "cn"}],
        "global": [{"code": "NVDA", "name": "NVIDIA", "market": "US", "type": "global"}],
        "crypto": [],
    }


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(prices, "get_ohlc", _fake_ohlc)
    monkeypatch.setattr(search_mod, "search_grouped", _fake_grouped)
    monkeypatch.setattr(kline_page, "_KLINE_CHART", lambda **kw: None)
    kline_page.cached_kline.clear()
    return AppTest.from_string(_SCRIPT, default_timeout=30).run()


def _compare_tab(app):
    return app.tabs[1]


def test_search_pick_appends_to_compare(app):
    """对比 tab: 搜索下拉选中 NVDA → 自动追加进对比代码并取数渲染."""
    app.text_input("cmp_search_query").set_value("NVIDIA").run()
    tab = _compare_tab(app)
    glb = next(s for s in tab.selectbox if any("NVDA" in o for o in s.options))
    glb.set_value(next(o for o in glb.options if "NVDA" in o)).run()
    tab = _compare_tab(app)
    ms = tab.multiselect[0]
    assert "NVDA" in ms.value
    assert any("NVDA" in m.value for m in tab.markdown)


def test_repick_same_result_no_dup(app):
    """同一搜索结果二次选中: 不产生重复代码."""
    app.text_input("cmp_search_query").set_value("NVIDIA").run()
    tab = _compare_tab(app)
    glb = next(s for s in tab.selectbox if any("NVDA" in o for o in s.options))
    glb.set_value(next(o for o in glb.options if "NVDA" in o)).run()
    app.text_input("cmp_search_query").set_value("NVIDIA").run()
    tab = _compare_tab(app)
    glb = next(s for s in tab.selectbox if any("NVDA" in o for o in s.options))
    if glb.value is not None:
        glb.set_value(next(o for o in glb.options if "NVDA" in o)).run()
    tab = _compare_tab(app)
    ms = tab.multiselect[0]
    assert ms.value.count("NVDA") == 1


def test_single_tab_search_unchanged(app):
    """单只查询 tab 搜索行为不变: 选中回填代码输入框 (kline_symbol)."""
    app.text_input("kline_search_query").set_value("NVIDIA").run()
    tab = app.tabs[0]
    glb = next(s for s in tab.selectbox if any("NVDA" in o for o in s.options))
    glb.set_value(next(o for o in glb.options if "NVDA" in o)).run()
    ti = [t for t in app.text_input if t.key == "kline_symbol"][0]
    assert ti.value == "NVDA"
