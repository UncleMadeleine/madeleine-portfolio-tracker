"""多股对比渲染门控测试 (AppTest, 不联网): 通过「K线」页入口验证走势对比子 tab.

对 tracker/ui/app.py 只做静态源码检查, 不 import —— 该模块是 Streamlit 入口
脚本, import 即执行整页 (含拉取全部自选行情), 同 tests/test_ui_imports.py
的处理方式; app.py 本体的 import 副作用 / 渲染回归见 tests/test_app_page.py。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

import tracker.prices as prices
import tracker.ui.kline_page as kline_page

_APP_SRC = Path(__file__).resolve().parent.parent / "tracker" / "ui" / "app.py"

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


def test_compare_gone_from_portfolio_page():
    """组合页不再提供走势对比 tab (迁移后属 K线子功能): app.py 无该分支."""
    src = _APP_SRC.read_text(encoding="utf-8")
    ast.parse(src)  # 语法有效 (解析失败先于下面的断言报错)
    assert "走势对比" not in src
    assert "render_compare_chart" not in src
