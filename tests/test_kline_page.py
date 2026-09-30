"""K线页面渲染门控测试 (AppTest, 不联网).

自定义组件 (st.components.v2) 无法在 AppTest 中承载, 用桩替代 —— 这里验证的是
「何时进入渲染分支」与双模式数据流: 滑动模式一次加载上市以来全量历史 (换代码
重取, 参数只重绘); 范围模式保持旧流程 (提交驱动)。
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


def _fake_ohlc(
    symbol, months=12, prefer_akshare=False, start_date=None, end_date=None, **kw
):
    """2014-2024 十年日K; 区间查询返回 [start_date, end_date) 内的数据."""
    idx = pd.date_range("2014-01-01", periods=3600, freq="D")
    close = [1.0 + (i % 50) * 0.01 for i in range(len(idx))]
    df = pd.DataFrame(
        {
            "date": idx,
            "open": close,
            "high": [c + 0.1 for c in close],  # 保持 high >= max(open,close) 不变式
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

    def _fake_ohlc(
        symbol, months=12, prefer_akshare=False, start_date=None, end_date=None, **kw
    ):
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
    # 组件桩必须在 AppTest 首次 run 前打上: 滑动模式空代码不渲染, 但后续测试在
    # run() 前已 patch 的话, from_string 的初始 run 就可能走到取数渲染路径。
    monkeypatch.setattr(kline_page, "_KLINE_CHART", lambda **kw: {"stub": True})
    kline_page.cached_kline.clear()
    kline_page.cached_kline_all.clear()
    app = AppTest.from_string(_SCRIPT, default_timeout=30).run()
    app._calls = calls  # 供断言取数次数
    return app


def test_slide_mode_renders_default_symbol(kline_app):
    """滑动模式 (默认): 输入代码即渲染上市以来历史, 无需点「查询」; 空代码只提示."""
    kline_app.text_input[1].set_value("AAPL").run()
    assert len(kline_app.metric) == 4


def test_slide_mode_param_change_redraws_not_refetches(kline_app):
    """滑动模式: 空转 rerun 与周期/均线变化都只重绘, 不清图表、不重取数."""
    kline_app.text_input[1].set_value("AAPL").run()
    assert len(kline_app.metric) == 4
    kline_app.run()  # 无交互 rerun (如其他组件事件) 不重取数
    assert len(kline_app.metric) == 4
    assert kline_app._calls["n"] == 1
    kline_app.selectbox[0].set_value("weekly").run()  # 切周期只重绘
    assert len(kline_app.metric) == 4
    assert kline_app._calls["n"] == 1


def test_slide_mode_symbol_change_refetches(kline_app):
    """滑动模式: 换代码重新取数, 图表保持可见."""
    kline_app.text_input[1].set_value("AAPL").run()
    assert len(kline_app.metric) == 4
    kline_app.text_input[1].set_value("600519.SS").run()
    assert len(kline_app.metric) == 4
    assert kline_app._calls["n"] == 2


def test_slide_mode_full_history_single_fetch(kline_app):
    """滑动模式: 全量语义 = 一次取数渲染, 无渐进补数 (need_more 机制已删)."""
    kline_app.text_input[1].set_value("AAPL").run()
    assert len(kline_app.metric) == 4
    assert kline_app._calls["n"] == 1
    kline_app.run()
    assert kline_app._calls["n"] == 1


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
    kline_app.selectbox[1].set_value(
        "weekly"
    ).run()  # 周期切换只重绘 (selectbox[0]=范围)
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


def test_quick_pills_truncated_to_limit():
    """常用 pills 只展示前 QUICK_SYMBOLS_LIMIT 个 (持仓/自选很长时页面保持整洁)."""
    script = """
import tracker.ui.kline_page as kp
kp.set_quick_symbols([f"SYM{i}.HK" for i in range(30)])
kp.render_kline_controls(False)
"""
    app = AppTest.from_string(script, default_timeout=30).run()
    assert app.pills[0].label == "常用 (持仓/自选)"
    assert app.pills[0].options == [
        f"SYM{i}.HK" for i in range(kline_page.QUICK_SYMBOLS_LIMIT)
    ]


def test_record_recent_dedupes_and_caps():
    """最近查看: 最新在前、去重、上限 RECENT_LIMIT (AppTest 会话内调用真实函数)."""
    script = """
import tracker.ui.kline_page as kp
for s in ["MSFT", "AAPL", "NVDA", "MSFT"]:
    kp.record_recent(s)
for i in range(kp.RECENT_LIMIT + 3):
    kp.record_recent(f"S{i}.HK")
"""
    app = AppTest.from_string(script, default_timeout=30).run()
    recent = app.session_state["kline_recent_symbols"]
    assert len(recent) == kline_page.RECENT_LIMIT
    assert recent[0] == f"S{kline_page.RECENT_LIMIT + 2}.HK"  # 最新在前
    assert "S0.HK" not in recent  # 超出上限的旧记录被截断


def test_record_recent_skips_empty():
    """空串/None 不进入最近查看 (state 键保持未初始化, 展示层回退空列表)."""
    script = """
import tracker.ui.kline_page as kp
kp.record_recent("")
kp.record_recent("  ")
kp.record_recent(None)
"""
    app = AppTest.from_string(script, default_timeout=30).run()
    assert "kline_recent_symbols" not in app.session_state


def test_slide_mode_query_records_recent(kline_app):
    """滑动模式取数成功后, 代码进入最近查看 (record_recent 挂在成功路径)."""
    kline_app.text_input[1].set_value("MSFT").run()
    assert kline_app.session_state["kline_recent_symbols"][0] == "MSFT"


def test_recent_pills_rendered_and_click_fills_symbol(kline_app):
    """最近查看 pills 点击后回填代码输入框 (查询以输入框值为准)."""
    kline_app.session_state["kline_quick_symbols"] = []
    kline_app.session_state["kline_recent_symbols"] = ["0700.HK", "MSFT"]
    kline_app.run()
    recent_pills = [p for p in kline_app.pills if p.label.startswith("最近查看")]
    assert len(recent_pills) == 1
    assert recent_pills[0].options == ["0700.HK", "MSFT"]  # 最新在前
    recent_pills[0].set_value("MSFT").run()
    assert kline_app.text_input[1].value == "MSFT"


def test_recent_excluded_when_already_in_quick_shown(kline_app):
    """最近查看与常用展示行去重: 出现在常用前 10 的代码不再重复展示."""
    kline_app.session_state["kline_quick_symbols"] = ["AAPL", "MSFT"] + [
        f"X{i}.HK" for i in range(8)
    ]
    kline_app.session_state["kline_recent_symbols"] = ["ZZZ.HK", "AAPL"]
    kline_app.run()
    recent_pills = [p for p in kline_app.pills if p.label.startswith("最近查看")]
    assert len(recent_pills) == 1
    assert recent_pills[0].options == ["ZZZ.HK"]
