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

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

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
    return _boot_app(
        monkeypatch,
        tmp_path,
        holdings=[{"symbol": "AAPL", "quantity": 2, "avg_cost": 100}],
    )


def _boot_app(monkeypatch, tmp_path, holdings):
    """以指定持仓启动 app.py 并完整渲染默认页 (组合); 行情/FX 全部 mock."""
    wl = tmp_path / "watchlist.json"
    pf = tmp_path / "portfolio.json"
    storage.save_watchlist(
        {"watchlist": [{"symbol": "AAPL", "lists": ["测试"], "upper_1": 140.0}]}, wl
    )
    storage.save_portfolio(
        {
            "base_currency": "CNY",
            "holdings": holdings,
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
    # FX 按持仓币种配对 (USD→CNY): 视图非空, 持仓明细表才真正渲染
    # (此前只返回 CNY 且 patch 的是 fx 模块而页面是直接 import 函数, 从未
    # 生效, view 为空, data_editor 整段不执行, 列格式问题被掩盖)
    portfolio_page.cached_fx.clear()
    monkeypatch.setattr(
        portfolio_page,
        "get_fx_rates",
        lambda base, curs, **kw: ({"USD": 7.1}, []),
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
    """AppTest 跑真实 app.py: 默认页 (组合) 完整渲染 (KPI + 持仓 + 自选).

    持仓视图非空 (FX 按币种配对): KPI 与持仓明细表都走真实渲染路径,
    列配置 (数量/盈亏% 等 format) 在该路径上生效 —— 曾因 view 为空整段
    data_editor 不执行, 非法的 printf 格式被掩盖。
    """
    assert any("组合总览" in m.value for m in app_boot.markdown)
    assert any("AAPL" in str(m.value) for m in app_boot.markdown)
    # 总市值 = 2 股 × 150 USD × 7.1 = 2130 CNY (KPI 走非空视图; st.metric
    # 在 AppTest 里是 metric 元素而非 markdown)
    assert any("2,130" in str(m.value) for m in app_boot.metric)


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


def test_asset_allocation_symbol_pie_top20_other(monkeypatch, tmp_path):
    """25 个持仓: 「按代码」饼图走 Top 20 + 其他 聚合路径, 完整渲染不炸.

    AppTest 拿不到 plotly figure, 这里守住的是渲染路径本身: by_symbol 聚合、
    图题条件、切片标注计算与 st.plotly_chart 调用 (ZeroDivision/列名错等
    都会以 exception 形式暴露)。
    """
    holdings = [
        {"symbol": f"SY{i:02d}", "quantity": i + 1, "avg_cost": 10} for i in range(25)
    ]
    at = _boot_app(monkeypatch, tmp_path, holdings=holdings)
    assert not at.exception, at.exception
    # 总市值 = Σ(i+1) × 150 USD × 7.1 = 1065 × 325 = 346,125 CNY:
    # 25 行全部流过 build_view (行情/汇率 mock 全覆盖), 饼图聚合路径真实执行
    assert any("346,125" in str(m.value) for m in at.metric)


def test_symbol_pie_top20_merges_tail_into_other():
    """25 个代码: 饼图只留前 20 + 其他 (尾部求和), 图题标注聚合口径."""
    view = pd.DataFrame(
        {
            "symbol": [f"S{i:02d}" for i in range(25)],
            "market_value": [float(100 - i) for i in range(25)],
        }
    )
    fig = portfolio_page._symbol_pie(view)
    trace = fig.data[0]
    assert list(trace.labels)[:20] == [f"S{i:02d}" for i in range(20)]
    assert list(trace.labels)[-1] == "其他"
    assert list(trace.values)[-1] == sum(float(100 - i) for i in range(20, 25))
    assert fig.layout.title.text == "按代码 (Top 20 + 其他)"
    # 图例 21 条: 字号调小, 避免 1/3 栏宽下挤压饼图
    assert fig.layout.legend.font.size == 10


def test_symbol_pie_small_portfolio_shows_all_symbols():
    """代码数不超过 20: 全量展示, 无「其他」, 图题不带 Top 说明."""
    view = pd.DataFrame({"symbol": ["AAPL", "MSFT"], "market_value": [100.0, 50.0]})
    fig = portfolio_page._symbol_pie(view)
    assert list(fig.data[0].labels) == ["AAPL", "MSFT"]
    assert fig.layout.title.text == "按代码"


def test_symbol_pie_labels_only_for_wide_slices():
    """窄切片不标文字 (防压字): 仅占比 >= SYMBOL_LABEL_MIN_SHARE 的切片有标注."""
    view = pd.DataFrame(
        {"symbol": ["A", "B", "C"], "market_value": [100.0, 2.0, 1.0]}
    )  # 97.1% / 1.9% / 1.0%
    fig = portfolio_page._symbol_pie(view)
    texts = list(fig.data[0].text)
    assert texts == ["97.1%", "", ""]
    assert fig.data[0].textinfo == "text"


def test_symbol_pie_zero_market_value_no_crash():
    """市值全 0 (数量清空): 跳过文字计算 (除零), 仍返回空饼不炸."""
    view = pd.DataFrame({"symbol": ["AAPL"], "market_value": [0.0]})
    fig = portfolio_page._symbol_pie(view)
    assert list(fig.data[0].labels) == ["AAPL"]
