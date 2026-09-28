"""组合主页面 (tracker/ui/app.py 路由 + portfolio_page.py) 测试.

三个不变量:
1. import 路由模块/页面模块只定义函数, 不执行页面脚本 —— 顶层曾直接拉取全部
   自选行情, 一处 import 就把整套测试拖死 (沙箱无网时无限挂起)。
2. AppTest.from_file 走与 `streamlit run` 相同的 __main__ 执行模型, 默认页
   (组合) 完整渲染 —— 保证路由入口没有把页面"锁死"在 import 里。
3. st.navigation 多页面路由: 五页各有独立 url_path (组合 / · K线 /kline ·
   指数 /index · 导入 /import · 设置 /settings), 刷新/直链不丢页。
"""

from __future__ import annotations

import importlib
import time

import pytest
from streamlit.testing.v1 import AppTest

import tracker.fx as fx_mod
import tracker.prices as prices
import tracker.storage as storage
import tracker.ui.app as app_mod
import tracker.ui.portfolio_page as portfolio_page

_URL_PATHS = {
    "": "组合",
    "kline": "K线",
    "index": "指数K线",
    "import": "导入",
    "settings": "设置",
}


@pytest.fixture
def app_boot(monkeypatch, tmp_path):
    wl = tmp_path / "watchlist.json"
    pf = tmp_path / "portfolio.json"
    storage.save_watchlist(
        {"watchlist": [{"symbol": "AAPL", "lists": ["测试"], "upper_1": 140.0}]}, wl
    )
    storage.save_portfolio(
        {
            "base_currency": "CNY",
            "holdings": [{"symbol": "AAPL", "quantity": 2, "avg_cost": 100}],
        },
        pf,
    )
    monkeypatch.setattr(portfolio_page, "WATCHLIST_PATH", wl)
    monkeypatch.setattr(storage, "PORTFOLIO_PATH", pf)

    def fake_quotes(symbols, **kw):
        from tracker.providers.base import Quote

        return (
            {
                s: Quote(
                    symbol=s,
                    name="Apple",
                    price=150.0,
                    prev_close=148.0,
                    change_pct=0.01,
                    currency="USD",
                )
                for s in symbols
            },
            {},
            [],
        )

    monkeypatch.setattr(prices, "get_quotes", fake_quotes)
    monkeypatch.setattr(
        fx_mod, "get_fx_rates", lambda base, curs, **kw: ({"CNY": 7.1}, [])
    )
    at = AppTest.from_file(app_mod.__file__).run(timeout=30)
    assert not at.exception, at.exception
    return at


def test_app_import_is_side_effect_free(monkeypatch):
    """import 路由与页面模块只定义函数, 不执行页面脚本 (不触发网络).

    回归护栏: 曾因模块顶层拉取全部自选行情, 仅这一处 import 就把整套测试
    拖死 (沙箱无网时无限挂起)。任何把页面逻辑收回模块顶层的改动都会撞红。
    """

    def _boom(*a, **kw):
        raise AssertionError("import 不得触发行情拉取")

    monkeypatch.setattr(prices, "get_quotes", _boom)
    t0 = time.monotonic()
    importlib.reload(portfolio_page)
    importlib.reload(app_mod)
    elapsed = time.monotonic() - t0
    assert callable(app_mod.main)
    assert callable(portfolio_page.render_portfolio_page)
    assert elapsed < 15, f"import 耗时 {elapsed:.1f}s, 疑似执行了页面脚本"


def test_portfolio_page_renders(app_boot):
    """AppTest 跑真实 app.py: 默认页 (组合) 完整渲染 (KPI + 持仓 + 自选)."""
    assert any("组合总览" in m.value for m in app_boot.markdown)
    assert any("AAPL" in str(m.value) for m in app_boot.markdown)


def test_navigation_registers_independent_urls(app_boot):
    """五页各有独立 url_path: 当前页由 URL 决定, 刷新不回落组合页.

    st.navigation 的注册表是唯一权威: 断言 url_path 全部注册且互不重复,
    组合页为默认页 (空路径 = 根 URL /)。
    """
    reg = app_boot._registered_pages
    by_url = {info.get("url_pathname"): info for info in reg.values()}
    assert set(by_url) == set(_URL_PATHS), f"注册的 url_path: {set(by_url)}"
    for url, title in _URL_PATHS.items():
        assert by_url[url]["page_name"] == title, url
    # 组合页 = 默认页 (空 pathname 即根 URL)
    default = [i for i in reg.values() if i.get("url_pathname") == ""]
    assert default and default[0]["page_name"] == "组合"


def test_portfolio_page_module_renders_directly(app_boot):
    """页面模块自身可独立渲染 (与 CLI/其他入口复用路径一致)."""
    at = AppTest.from_string(
        "import tracker.ui.portfolio_page as pp\npp.render_portfolio_page()",
        default_timeout=30,
    ).run()
    assert not at.exception, at.exception
    assert any("组合总览" in m.value for m in at.markdown)
