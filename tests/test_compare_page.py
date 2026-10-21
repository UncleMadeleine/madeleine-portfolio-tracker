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
    # 比例线默认关闭; 测试打开时汇率历史不得打真实网络
    monkeypatch.setattr(
        kline_page.fx,
        "get_fx_history",
        lambda *a, **kw: pd.DataFrame(
            {
                "date": pd.date_range("2024-01-01", periods=10, freq="D"),
                "close": [0.9] * 10,
            }
        ),
    )
    kline_page.cached_kline.clear()
    return AppTest.from_string(_SCRIPT, default_timeout=30).run()


def _subview(app, name: str):
    """切到指定 K线子功能 (懒加载: 仅激活时才渲染其控件, 返回 app)."""
    next(
        s for s in app.segmented_control if s.key == "kline_subview"
    ).set_value(name).run()
    return app


def test_compare_renders_chart(compare_app):
    """走势对比子功能: 默认选中前两个候选代码, 激活后取数渲染 (区间涨跌 + 归一化说明)."""
    _subview(compare_app, "走势对比")
    captions = [c.value for c in compare_app.caption]
    assert any("区间首个收盘归一化" in c for c in captions)
    assert any("区间涨跌" in m.value for m in compare_app.markdown)


def test_compare_gone_from_portfolio_page():
    """组合页不再提供走势对比 tab (迁移后属 K线子功能): app.py 无该分支."""
    src = _APP_SRC.read_text(encoding="utf-8")
    ast.parse(src)  # 语法有效 (解析失败先于下面的断言报错)
    assert "走势对比" not in src
    assert "render_compare_chart" not in src


def test_compare_ratio_toggle(compare_app):
    """比例线是可选开关: 默认关闭, 打开后出现分子/分母/汇率调整控件并渲染."""
    _subview(compare_app, "走势对比")
    ratio_toggles = [t for t in compare_app.toggle if t.key == "compare_ratio"]
    assert ratio_toggles and ratio_toggles[0].value is False
    assert not any(t.key == "compare_ratio_fx" for t in compare_app.toggle)

    compare_app.toggle(key="compare_ratio").set_value(True).run()
    captions = [c.value for c in compare_app.caption]
    assert any("比例线 =" in c for c in captions)
    assert any(t.key == "compare_ratio_fx" for t in compare_app.toggle)


def _fake_mcap_frames():
    """假的「历史市值 frames」(K线同构, close=已折算的亿级市值)."""
    from tracker.mcap import ohlcv_shim

    def _df(scale):
        idx = pd.date_range("2024-01-01", periods=30, freq="D")
        return ohlcv_shim(
            pd.DataFrame(
                {"date": idx, "close": [scale * (1 + i / 100) for i in range(30)]}
            )
        )

    return {"AAPL": _df(1e12), "0700.HK": _df(5e11)}


def test_compare_mcap_mode(compare_app, monkeypatch):
    """市值模式: 指标切「市值」后走市值序列路径 (市值标题说明 + 最新市值摘要)."""
    from tracker import storage

    app = _subview(compare_app, "走势对比")
    metric_sel = next(s for s in app.selectbox if s.key == "compare_metric")
    assert metric_sel.value == "股价"

    captured = {}

    def fake_cached_mcap_compare(
        symbols,
        months,
        prefer_akshare,
        use_ibkr=False,
        use_longport=False,
        crypto_source="auto",
        target_currency=None,
    ):
        captured["symbols"] = symbols
        captured["target"] = target_currency
        capsule = _fake_mcap_frames()
        warnings_list = ["AAPL: 无历史股本数据, 按当前市值近似折算"]
        meta = {
            "AAPL": {
                "source": "approx",
                "currency": "CNY",
                "source_label": "当前市值近似折算",
            },
            "0700.HK": {
                "source": "shares",
                "currency": "CNY",
                "source_label": "历史股本×收盘价",
            },
        }
        return capsule, warnings_list, meta

    monkeypatch.setattr(
        kline_page, "cached_mcap_compare", fake_cached_mcap_compare
    )
    metric_sel.set_value("市值").run()
    assert captured["symbols"] == ("AAPL", "0700.HK")
    # 默认折算币种 = 组合基础货币 (读本地 portfolio.json, 不硬编码)
    assert captured["target"] == storage.load_portfolio()["base_currency"].upper()
    texts = [w.value for w in compare_app.warning]
    assert any("按当前市值近似折算" in t for t in texts)
    assert any("最新市值" in m.value for m in compare_app.markdown)
    assert any("市值按统一币种折算" in c.value for c in compare_app.caption)
    # 比例线在市值模式下仍可开: 比例 = 两市值之比 (汇率调整禁用)
    compare_app.toggle(key="compare_ratio").set_value(True).run()
    fx_toggle = next(t for t in compare_app.toggle if t.key == "compare_ratio_fx")
    assert fx_toggle.disabled
    assert any("市值比例线 =" in c.value for c in compare_app.caption)
