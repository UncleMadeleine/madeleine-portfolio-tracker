"""K线页面渲染门控测试 (AppTest, 不联网).

自定义组件 (st.components.v2) 无法在 AppTest 中承载, 用桩替代 —— 这里验证的是
「何时进入渲染分支」与双模式数据流: 滑动模式启动即渲染默认代码 + 拖到左缘自动
向前扩展; 范围模式保持旧流程 (提交驱动)。
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


def _fake_ohlc(symbol, months=12, prefer_akshare=False, start_date=None, end_date=None, **kw):
    """2014-2024 十年日K; 区间查询返回 [start_date, end_date) 内的数据."""
    idx = pd.date_range("2014-01-01", periods=3600, freq="D")
    close = [1.0 + (i % 50) * 0.01 for i in range(len(idx))]
    df = pd.DataFrame(
        {
            "date": idx,
            "open": close,
            "high": [c + 0.1 for c in close],   # 保持 high >= max(open,close) 不变式
            "low": [c - 0.1 for c in close],
            "close": close,
            "volume": 100.0,
        }
    )
    if months and not start_date and not end_date:
        df = df.tail(months * 31)  # 近 N 个月截断 (与真实数据源行为一致)
    if start_date is not None:
        df = df[df["date"] >= pd.Timestamp(start_date)]
    if end_date is not None:
        df = df[df["date"] < pd.Timestamp(end_date)]
    return df.reset_index(drop=True)


@pytest.fixture
def kline_app(monkeypatch):
    """每个测试独立: 桩掉组件与网络, 清空两层缓存."""
    calls = {"n": 0}

    def _fake_ohlc(symbol, months=12, prefer_akshare=False, start_date=None, end_date=None, **kw):
        calls["n"] += 1
        idx = pd.date_range("2024-01-01", periods=360, freq="D")
        df = pd.DataFrame(
            {
                "date": idx,
                "open": 1.0,
                "high": 1.2,
                "low": 0.9,
                "close": [1.0 + (i % 50) * 0.01 for i in range(len(idx))],
                "volume": 100.0,
            }
        )
        if start_date is not None:
            df = df[df["date"] >= pd.Timestamp(start_date)]
        if end_date is not None:
            df = df[df["date"] < pd.Timestamp(end_date)]
        return df.reset_index(drop=True)

    monkeypatch.setattr(prices, "get_ohlc", _fake_ohlc)
    monkeypatch.setattr(prices, "get_history", _fake_ohlc)
    monkeypatch.setattr(kline_page, "_KLINE_CHART", lambda **kw: {"stub": True})
    kline_page.cached_kline.clear()
    kline_page.cached_kline_years.clear()
    app = AppTest.from_string(_SCRIPT, default_timeout=30).run()
    app._calls = calls  # 供断言取数次数
    return app


def test_slide_mode_renders_default_symbol(kline_app):
    """滑动模式 (默认): 输入代码即渲染深度历史, 无需点「查询」; 空代码只提示."""
    kline_app.text_input[1].set_value("AAPL").run()
    assert len(kline_app.metric) == 4


def test_slide_mode_idle_rerun_keeps_chart(kline_app):
    """滑动模式: need_more 空转 rerun 与参数变化都不清图表、不重取数."""
    kline_app.text_input[1].set_value("AAPL").run()
    assert len(kline_app.metric) == 4
    kline_app.run()  # 组件 setValue(need_more) 触发的空转 rerun
    assert len(kline_app.metric) == 4
    assert kline_app._calls["n"] == 1
    kline_app.selectbox[1].set_value("weekly").run()  # 切周期只重绘
    assert len(kline_app.metric) == 4
    assert kline_app._calls["n"] == 1


def test_slide_mode_depth_change_refetches(kline_app):
    """滑动模式: 切换加载深度重新取数, 图表保持可见."""
    kline_app.text_input[1].set_value("AAPL").run()
    assert len(kline_app.metric) == 4
    kline_app.selectbox[0].set_value(5).run()  # 加载深度: 近10年 → 近5年
    assert len(kline_app.metric) == 4
    assert kline_app._calls["n"] == 2


def test_slide_mode_need_more_extends(kline_app):
    """滑动模式: 组件 need_more (拖近左缘) → 向前补数据后仍渲染, 取数次数增加."""
    kline_app.text_input[1].set_value("AAPL").run()
    kline_app.selectbox[0].set_value(2).run()  # 加载深度: 近2年 → fake 数据(2014起)更早可扩展
    assert len(kline_app.metric) == 4
    base_calls = kline_app._calls["n"]
    # 直接以组件视角模拟: 组件 setTriggerValue("need_more", {seq, qid}) →
    # mount 返回 {"need_more": {seq, qid}} (AppTest 无法触发真实 JS 回调)
    orig = kline_page.render_kline_view
    kline_page.render_kline_view = (
        lambda *a, **kw: {"need_more": {"seq": 1234.0, "qid": kw.get("query_id")}}
    )
    try:
        kline_app.run()
    finally:
        kline_page.render_kline_view = orig
    kline_app.run()  # 恢复渲染桩后重跑: AppTest 中 st.rerun 的重跑仍走 patch 函数
    assert kline_app._calls["n"] > base_calls  # 触发了一次向前补数
    assert len(kline_app.metric) == 4


def test_range_mode_requires_query_or_symbol_change(kline_app):
    """范围模式: 输入代码回车 (代码变化) 即查询."""
    kline_app.segmented_control[0].set_value("范围").run()
    kline_app.text_input[1].set_value("AAPL").run()
    assert len(kline_app.metric) == 4


def test_range_mode_param_change_redraws_not_refetches(kline_app):
    """范围模式: 查询后改均线/周期只重绘; 改范围重取数.

    切到范围模式本身就取数一次 (当前范围与滑动模式数据段不同), 记为基准。
    """
    kline_app.segmented_control[0].set_value("范围").run()
    kline_app.text_input[1].set_value("AAPL").run()
    assert len(kline_app.metric) == 4
    base = kline_app._calls["n"]
    kline_app.multiselect[0].set_value([5, 10]).run()
    assert len(kline_app.metric) == 4
    assert kline_app._calls["n"] == base
    kline_app.selectbox[1].set_value("weekly").run()
    assert len(kline_app.metric) == 4
    assert kline_app._calls["n"] == base
    kline_app.selectbox[0].set_value(6).run()  # 范围: 12 → 6 个月
    assert len(kline_app.metric) == 4
    assert kline_app._calls["n"] == base + 1


def test_invalid_symbol_rejected(kline_app):
    """无法识别后缀的代码报错不渲染."""
    kline_app.text_input[1].set_value("600519.XX").run()  # 无法识别的交易所后缀
    assert len(kline_app.metric) == 0
    assert any("无法识别" in e.value for e in kline_app.error)
