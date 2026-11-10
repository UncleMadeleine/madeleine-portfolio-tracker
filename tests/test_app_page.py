"""组合主页面 (tracker/ui/app.py 路由 + portfolio_page.py) 测试.

三个不变量:
1. import 路由模块/页面模块只定义函数, 不执行页面脚本 —— 顶层曾直接拉取全部
   自选行情, 一处 import 就把整套测试拖死 (沙箱无网时无限挂起)。
2. AppTest.from_file 走与 `streamlit run` 相同的 __main__ 执行模型, 默认页
   (组合) 完整渲染 —— 保证路由入口没有把页面"锁死"在 import 里。
3. st.navigation 多页面路由: 七页各有独立 url_path (组合 / · K线 /kline ·
   指数 /index · 基本面 /fundamentals · 日历 /calendar · 导入 /import ·
   设置 /settings), 刷新/直链不丢页。
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
    "fundamentals": "基本面",
    "calendar": "日历",
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


def _boot_app(monkeypatch, tmp_path, holdings, watchlist=None):
    """以指定持仓启动 app.py 并完整渲染默认页 (组合); 行情/FX 全部 mock."""
    wl = tmp_path / "watchlist.json"
    pf = tmp_path / "portfolio.json"
    if watchlist is None:
        watchlist = [{"symbol": "AAPL", "lists": ["测试"], "upper_1": 140.0}]
    storage.save_watchlist({"watchlist": watchlist}, wl)
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


def test_nav_snapshot_skips_unchanged_rerun(app_boot, monkeypatch):
    """数值未变的 rerun 不重复写历史净值 (交互频繁, 避免每次渲染都写盘)."""
    calls: list[list[dict]] = []
    real = portfolio_page.nav.save_rows

    def counting(rows, **kw):
        calls.append(list(rows))
        return real(rows, **kw)

    monkeypatch.setattr(portfolio_page.nav, "save_rows", counting)
    # fixture 的首轮渲染已写入并记录指纹: 之后数值不变的重跑应全部跳过
    app_boot.run(timeout=30)
    app_boot.run(timeout=30)
    assert calls == []


def test_cli_main_starts_background_warmers(monkeypatch, tmp_path):
    """tracker-ui 启动即拉起保温线程, 不等第一个浏览器会话触发 main()."""
    import streamlit.web.cli as st_cli

    events: list[str] = []
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(app_mod, "ensure_warmers", lambda: events.append("warmers"))
    monkeypatch.setattr(st_cli, "main", lambda: events.append("streamlit"))
    app_mod.cli_main()
    assert events == ["warmers", "streamlit"]


def test_navigation_registers_independent_urls(app_boot):
    """七页各有独立 url_path: 当前页由 URL 决定, 刷新不回落组合页.

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


def test_quantity_format_is_valid_printf():
    """数量列格式必须是合法 printf (%,!.6f 会让前端抛 SyntaxError)."""
    import re

    src = open("tracker/ui/portfolio_page.py", encoding="utf-8").read()
    assert "%,!.6f" not in src
    # 所有 format 串只含合法 flag: % 后跟 符号/千分位/宽度/精度/类型,
    # 尾部 %% 是字面百分号 (合法); ! 不是合法 flag
    bad = [
        f
        for f in re.findall(r'format="([^"]+)"', src)
        if f.startswith("%")
        and not re.fullmatch(
            r"%[+0\-,'\s#]*\d*(\.\d+)?[bcdeEfFgGiosxX](%%)?", f
        )
    ]
    assert not bad, f"非法 printf 格式: {bad}"

def test_pnl_pct_uses_percent_format():
    """pnl_pct 是小数比值: 用 percent 预定义格式 (自动乘 100)."""
    src = open("tracker/ui/portfolio_page.py", encoding="utf-8").read()
    assert '"盈亏%", format="percent"' in src
    assert '"盈亏%", format="%+.2f%%"' not in src


def test_merged_issue_lines_dedupes_fetch_and_view_errors():
    """解析错误在 errors/issues 各出现一次时只显示一行, 计数不翻倍."""
    line = "BAD.ZZ: 无法识别的交易所后缀: BAD.ZZ"
    merged = portfolio_page._merged_issue_lines(
        {"BAD.ZZ": "无法识别的交易所后缀: BAD.ZZ"}, [line]
    )
    assert merged == [line]


def test_context_menu_delete_watchlist_action(monkeypatch, tmp_path):
    """右键「从自选删除」: 复用现有确认弹窗, 不绕过确认直接落盘."""
    from tracker.ui import theme

    at = _boot_app(
        monkeypatch,
        tmp_path,
        holdings=[{"symbol": "AAPL", "quantity": 2, "avg_cost": 100}],
        watchlist=[
            {"symbol": "AAPL", "lists": ["测试"], "upper_1": 140.0},
            {"symbol": "MSFT", "lists": ["测试"], "upper_1": 500.0},
        ],
    )
    at.session_state[theme.CONTEXT_ACTION_KEY] = {
        "action": "delete_watchlist",
        "symbol": "MSFT",
    }
    at.run(timeout=30)
    assert not at.exception, at.exception
    assert at.session_state["_delete_watchlist_pending"] == ["MSFT"]
    # 未点确认前不写盘
    assert [
        e["symbol"]
        for e in storage.load_watchlist(portfolio_page.WATCHLIST_PATH)["watchlist"]
    ] == ["AAPL", "MSFT"]

    at.button(key="confirm_delete_watchlist").click()
    at.run(timeout=30)
    assert not at.exception, at.exception
    assert [
        e["symbol"]
        for e in storage.load_watchlist(portfolio_page.WATCHLIST_PATH)["watchlist"]
    ] == ["AAPL"]
    # 持仓不受影响
    assert [
        h["symbol"]
        for h in storage.load_portfolio(storage.PORTFOLIO_PATH)["holdings"]
    ] == ["AAPL"]


def test_selected_holdings_delete_action(monkeypatch, tmp_path):
    """持仓明细表选中行 + 「删除选中」: 弹窗确认后写盘."""
    at = _boot_app(
        monkeypatch,
        tmp_path,
        holdings=[
            {"symbol": "AAPL", "quantity": 2, "avg_cost": 100},
            {"symbol": "MSFT", "quantity": 1, "avg_cost": 300},
        ],
    )
    at.session_state["holdings_detail_select"] = {"selection": {"rows": [0]}}
    at.run(timeout=30)
    assert not at.exception, at.exception
    at.button(key="delete_holdings_selected").click()
    at.run(timeout=30)
    assert not at.exception, at.exception
    at.button(key="confirm_delete_holdings").click()
    at.run(timeout=30)
    assert not at.exception, at.exception
    # 显式传路径: load_portfolio 的默认参数在 import 时已绑定, 不吃
    # monkeypatch 后的 storage.PORTFOLIO_PATH (会读到仓库里的真实组合)
    left = storage.load_portfolio(storage.PORTFOLIO_PATH)["holdings"]
    assert [h["symbol"] for h in left] == ["MSFT"]


def test_detail_table_keeps_unpriced_holdings_deletable():
    """无行情/代码写错的持仓也要出现在明细表, 否则页面上没有删除入口."""
    view = pd.DataFrame(
        [
            {
                "symbol": "AAPL",
                "quantity": 2,
                "avg_cost": 100,
                "market_value": 200,
            }
        ]
    )
    detail = portfolio_page._detail_with_unpriced(
        view,
        [
            {"symbol": "AAPL", "quantity": 2, "avg_cost": 100},
            {"symbol": "BAD!!", "quantity": 1, "avg_cost": 10},
        ],
    )
    assert list(detail["symbol"]) == ["AAPL", "BAD!!"]
    assert detail.iloc[1]["source"] == "行情缺失"
    assert detail.iloc[1]["quantity"] == 1


def test_style_chart_does_not_inject_empty_title():
    """无标题的图不注入空 title 对象: 否则图中央会凭空多出 "undefined"."""
    import plotly.graph_objects as go

    from tracker.ui import theme

    plain = theme.style_chart(go.Figure(go.Scatter(x=[1, 2], y=[1, 2])))
    assert "title" not in plain.to_plotly_json()["layout"]
    titled = theme.style_chart(
        go.Figure(go.Scatter(x=[1], y=[1])).update_layout(title="T")
    )
    assert titled.layout.title.text == "T"


def test_theme_table_right_click_menu_js():
    """表格右键菜单: 复制单元格/整行/含表头 + 跳转 K线, 但不劫持输入框."""
    from tracker.ui import theme

    js = theme._TABLE_CONTEXT_MENU_JS
    assert "contextmenu" in js
    assert "复制单元格" in js
    assert "复制整行" in js
    assert "复制含表头" in js
    assert "/kline?symbol=" in js
    # 特定表格 (持仓/自选) 提供删除, 并经组件 trigger 交回 Python 确认
    assert "从自选删除" in js
    assert "从持仓删除" in js
    assert "watchlist_detail_select" in js
    assert "setTriggerValue" in js
    assert "ctx_action" in js
    # Glide 单元格列号从 1 开始而 th DOM 顺序从 0 开始: 代码列不能错位
    assert "headersByAria[String(col)]" in js
    assert "headers[String(col - 1)]" in js
    # 输入/编辑状态必须保留浏览器原生菜单 (复制/粘贴/拼写检查)
    assert 'input, textarea, [contenteditable="true"]' in js


def test_theme_trackpad_guard_blocks_history_swipe():
    """触控板双指横滑到边界不应触发浏览器后退 (侧边栏持仓表最易误触)."""
    from tracker.ui import theme

    css = theme._CSS
    assert "overscroll-behavior-x: none" in css
    assert "overscroll-behavior-x: contain" in css

    js = theme._TRACKPAD_GUARD_JS
    assert "wheel" in js
    assert "preventDefault" in js
    assert "composedPath" in js
    # 只在 CSS 不生效的 Safari / 旧浏览器挂非 passive 监听, 不拖慢 Chrome
    assert "Safari" in js or "safari" in js
    assert "passive: false" in js


def test_selected_watchlist_delete_action(monkeypatch, tmp_path):
    """自选观察表选中行 + 「删除选中」: 弹窗确认后写盘."""
    at = _boot_app(
        monkeypatch,
        tmp_path,
        holdings=[{"symbol": "AAPL", "quantity": 2, "avg_cost": 100}],
        watchlist=[
            {"symbol": "AAPL", "lists": ["测试"], "upper_1": 140.0},
            {"symbol": "MSFT", "lists": ["测试"], "upper_1": 500.0},
        ],
    )
    at.session_state["watchlist_detail_select"] = {"selection": {"rows": [0]}}
    at.run(timeout=30)
    assert not at.exception, at.exception
    at.button(key="delete_watchlist_selected").click()
    at.run(timeout=30)
    assert not at.exception, at.exception
    at.button(key="confirm_delete_watchlist").click()
    at.run(timeout=30)
    assert not at.exception, at.exception
    left = storage.load_watchlist(portfolio_page.WATCHLIST_PATH)["watchlist"]
    assert [e["symbol"] for e in left] == ["MSFT"]
    # 持仓不受自选删除影响
    assert [
        h["symbol"]
        for h in storage.load_portfolio(storage.PORTFOLIO_PATH)["holdings"]
    ] == ["AAPL"]
