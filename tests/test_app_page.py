"""组合主页面 (tracker/ui/app.py 路由 + portfolio_page.py) 测试.

三个不变量:
1. import 路由模块/页面模块只定义函数, 不执行页面脚本 —— 顶层曾直接拉取全部
   自选行情, 一处 import 就把整套测试拖死 (沙箱无网时无限挂起)。
2. AppTest.from_file 走与 `streamlit run` 相同的 __main__ 执行模型, 默认页
   (组合) 完整渲染 —— 保证路由入口没有把页面"锁死"在 import 里。
3. st.navigation 多页面路由: 九页各有独立 url_path (组合 / · K线 /kline ·
   指数 /index · 基本面 /fundamentals · 日历 /calendar · 钱包净值 /wallet ·
   导入 /import · 数据健康 /health · 设置 /settings), 刷新/直链不丢页。
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
import tracker.ui.portfolio_data as portfolio_data
import tracker.ui.portfolio_page as portfolio_page

_URL_PATHS = {
    "": "组合",
    "kline": "K线",
    "index": "指数K线",
    "fundamentals": "基本面",
    "calendar": "日历",
    "wallet": "钱包净值",
    "import": "导入",
    "health": "数据健康",
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
        portfolio_data,
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


def test_watch_scope_picker_marks_its_effect(monkeypatch, tmp_path):
    """「自选范围」只筛自选: 选择结果必须可见, 且不影响持仓/KPI."""
    at = _boot_app(
        monkeypatch,
        tmp_path,
        holdings=[{"symbol": "AAPL", "quantity": 2, "avg_cost": 100}],
        watchlist=[
            {"symbol": "AAPL", "lists": ["测试A"], "upper_1": 140.0},
            {"symbol": "0700.HK", "lists": ["测试B"], "upper_1": 500.0},
        ],
    )

    box = at.selectbox(key="watch_scope")
    assert box.label == "自选范围"
    assert box.format_func("测试B") == "测试B (1)"

    box.set_value("测试B").run()
    assert not at.exception, at.exception
    # 持仓与 KPI 不受自选范围影响: 仍为 AAPL 2 × 150 × 7.1 = 2,130 CNY
    assert any("2,130" in str(m.value) for m in at.metric)
    # KPI 标签带上范围, 让"只筛自选"这件事在首屏就能看到
    assert any("持仓 / 自选 · 测试B" in str(m.label) for m in at.metric)
    # 自选 tab 标题带范围与条数, 选择效果一眼可见
    assert any("自选观察 · 测试B (1)" in str(t.label) for t in at.tabs)
    assert any("仅筛选自选观察" in str(c.value) for c in at.caption)


def test_context_delete_watchlist_from_sidebar_editor(monkeypatch, tmp_path):
    """侧栏自选编辑器右键删除: trigger → 确认弹窗 → 锁内删除."""
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
        "nonce": "sidebar-delete-test",
    }
    at.run(timeout=30)
    assert not at.exception, at.exception
    at.button(key="confirm_delete_watchlist").click()
    at.run(timeout=30)
    assert not at.exception, at.exception
    left = storage.load_watchlist(portfolio_page.WATCHLIST_PATH)["watchlist"]
    assert [e["symbol"] for e in left] == ["AAPL"]


def test_context_delete_target_outside_current_scope_still_confirms(
    monkeypatch, tmp_path
):
    """侧栏编辑器右键删不在当前自选范围内的代码时, 确认框仍要弹出."""
    from tracker.ui import theme

    at = _boot_app(
        monkeypatch,
        tmp_path,
        holdings=[{"symbol": "AAPL", "quantity": 2, "avg_cost": 100}],
        watchlist=[
            {"symbol": "AAPL", "lists": ["测试A"], "upper_1": 140.0},
            {"symbol": "MSFT", "lists": ["测试B"], "upper_1": 500.0},
        ],
    )
    at.session_state["watch_scope"] = "测试A"
    at.run(timeout=30)
    # MSFT 不在当前范围 (测试A) 里, 但右键删除仍应走确认弹窗
    at.session_state[theme.CONTEXT_ACTION_KEY] = {
        "action": "delete_watchlist",
        "symbol": "MSFT",
        "nonce": "outside-scope-delete",
    }
    at.run(timeout=30)
    assert not at.exception, at.exception
    at.button(key="confirm_delete_watchlist").click()
    at.run(timeout=30)
    assert not at.exception, at.exception
    left = storage.load_watchlist(portfolio_page.WATCHLIST_PATH)["watchlist"]
    assert [e["symbol"] for e in left] == ["AAPL"]


def test_context_delete_holdings_from_sidebar_editor(monkeypatch, tmp_path):
    """侧栏持仓编辑器右键删除: 与自选共用确认弹窗与锁内删除."""
    from tracker.ui import theme

    at = _boot_app(
        monkeypatch,
        tmp_path,
        holdings=[
            {"symbol": "AAPL", "quantity": 2, "avg_cost": 100},
            {"symbol": "MSFT", "quantity": 3, "avg_cost": 200},
        ],
    )
    at.session_state[theme.CONTEXT_ACTION_KEY] = {
        "action": "delete_holdings",
        "symbol": "MSFT",
        "nonce": "holdings-sidebar-delete-test",
    }
    at.run(timeout=30)
    assert not at.exception, at.exception
    at.button(key="confirm_delete_holdings").click()
    at.run(timeout=30)
    assert not at.exception, at.exception
    left = storage.load_portfolio(storage.PORTFOLIO_PATH)["holdings"]
    assert [h["symbol"] for h in left] == ["AAPL"]


def test_context_delete_unsaved_watchlist_row_is_rejected(monkeypatch, tmp_path):
    """编辑器里新增但未保存的行: 右键删除不能假删, 也不清空编辑器."""
    from tracker.ui import theme

    at = _boot_app(
        monkeypatch,
        tmp_path,
        holdings=[{"symbol": "AAPL", "quantity": 2, "avg_cost": 100}],
        watchlist=[{"symbol": "AAPL", "lists": ["测试"], "upper_1": 140.0}],
    )
    at.session_state[theme.CONTEXT_ACTION_KEY] = {
        "action": "delete_watchlist",
        "symbol": "ZZZZ",
        "nonce": "unsaved-delete-test",
    }
    at.run(timeout=30)
    assert not at.exception, at.exception
    at.button(key="confirm_delete_watchlist").click()
    at.run(timeout=30)
    assert not at.exception, at.exception
    left = storage.load_watchlist(portfolio_page.WATCHLIST_PATH)["watchlist"]
    assert [e["symbol"] for e in left] == ["AAPL"]
    assert any("尚未保存" in str(w.value) for w in at.warning)


def test_context_delete_still_confirms_when_files_are_empty(monkeypatch, tmp_path):
    """磁盘持仓/自选为空 (只剩编辑器未保存行) 时, 右键删除也不能被静默吞掉."""
    from tracker.ui import theme

    at = _boot_app(monkeypatch, tmp_path, holdings=[], watchlist=[])
    at.session_state[theme.CONTEXT_ACTION_KEY] = {
        "action": "delete_holdings",
        "symbol": "ZZZZ",
        "nonce": "empty-files-delete-test",
    }
    at.run(timeout=30)

    assert not at.exception, at.exception
    # 页面在表格之前 st.stop() 了, 但确认弹窗仍必须存在
    assert at.button(key="confirm_delete_holdings")
    at.button(key="confirm_delete_holdings").click()
    at.run(timeout=30)
    assert not at.exception, at.exception
    assert any("尚未保存" in str(w.value) for w in at.warning)


@pytest.mark.parametrize(
    ("button_key", "edit_name", "error_text"),
    [
        ("save_holdings", "edit_portfolio", "持仓保存失败"),
        ("save_watchlist", "edit_watchlist", "自选保存失败"),
    ],
)
def test_sidebar_save_failure_still_renders_page(
    monkeypatch, tmp_path, button_key, edit_name, error_text
):
    """写盘失败只应显示可读错误, 不能因 render_sidebar 返回 None 整页崩掉."""
    at = _boot_app(
        monkeypatch,
        tmp_path,
        holdings=[{"symbol": "AAPL", "quantity": 2, "avg_cost": 100}],
    )

    def _locked(*args, **kwargs):
        raise storage.DataFileLocked("模拟：数据文件被其它进程占用")

    monkeypatch.setattr(storage, edit_name, _locked)
    at.button(key=button_key).click()
    at.run(timeout=30)

    assert not at.exception, at.exception
    assert any(error_text in str(e.value) for e in at.error)


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
    """九页各有独立 url_path: 当前页由 URL 决定, 刷新不回落组合页.

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


def test_health_page_renders_without_network(app_boot):
    """数据健康页只读本地: 空库/无事件也应完整渲染."""
    from tracker.ui import health_page

    health_page.cached_report.clear()
    at = AppTest.from_string(
        "import tracker.ui.health_page as hp\nhp.render_health_page()",
        default_timeout=30,
    ).run()
    assert not at.exception, at.exception
    assert any("数据健康" in m.value for m in at.markdown)


def test_health_page_renders_with_events_and_gaps(app_boot):
    """有事件/有缺口时表格与摘要也能完整渲染 (DataFrame 列名/NaN 兜底)."""
    import tracker.cache as cache_mod
    from tracker.ui import health_page

    # st.cache_data 是进程级全局: 前面的空库用例可能已缓存 window=24 的报告
    health_page.cached_report.clear()
    now = time.time()
    cache_mod.add_health_events(
        [
            (now - 5, "quote", "yfinance", 1, 120.0, "AAPL", None),
            (now - 3, "quote", "yfinance", 0, 80.0, "BAD", "HTTP 429"),
            (now - 2, "fx", "cfets", 1, 40.0, "CNY", None),
        ]
    )
    storage.save_portfolio(
        {
            "base_currency": "USD",
            "holdings": [{"symbol": "0700.HK", "quantity": 10, "avg_cost": 300}],
        },
        storage.PORTFOLIO_PATH,
    )
    at = AppTest.from_string(
        "import tracker.ui.health_page as hp\nhp.render_health_page()",
        default_timeout=30,
    ).run()
    assert not at.exception, at.exception
    # 缺口摘要出现 (持仓缺行情/K线)
    assert any("0700.HK" in str(df.value) for df in at.dataframe)


def test_wallet_page_renders_without_query():
    """钱包净值页首屏不触发链上/行情请求, 只渲染输入框与说明."""
    at = AppTest.from_string(
        "import tracker.ui.wallet_page as wp\nwp.render_wallet_page()",
        default_timeout=30,
    ).run()
    assert not at.exception, at.exception
    assert any("钱包净值" in m.value for m in at.markdown)
    assert any("查询净值曲线" in str(b.label) for b in at.button)


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


def test_style_chart_disables_drag_for_mobile_page_scroll():
    """移动端单指在图上拖动应滚动页面, 而不是被 plotly 的框选/缩放截走.

    plotly.js 只有在 dragmode 非 False 时才 preventDefault 触摸移动; 汇总小图
    统一关掉拖动交互, 悬停/点按查看数值仍保留。
    """
    import plotly.graph_objects as go

    from tracker.ui import theme

    fig = theme.style_chart(go.Figure(go.Scatter(x=[1, 2], y=[1, 2])))
    assert fig.layout.dragmode is False


def test_nav_range_options_follow_available_history_span():
    """净值区间选项只保留有意义的档位: 3 天数据时不展示 90/180/365 天."""
    from tracker.ui import portfolio_nav

    short = [
        {"day": "2026-11-06"},
        {"day": "2026-11-09"},
        {"day": "2026-11-23"},
        {"day": "2026-11-28"},
    ]
    assert portfolio_nav._range_labels(short) == ["全部"]

    medium = [{"day": "2026-08-01"}, {"day": "2026-11-08"}]  # 100 天
    assert portfolio_nav._range_labels(medium) == ["30 天", "90 天", "全部"]
    assert portfolio_nav._default_range_label(portfolio_nav._range_labels(medium)) == (
        "90 天"
    )

    long = [{"day": "2025-11-01"}, {"day": "2026-11-28"}]  # 393 天
    assert portfolio_nav._range_labels(long) == list(portfolio_nav.NAV_RANGES)
    assert portfolio_nav._default_range_label(list(portfolio_nav.NAV_RANGES)) == "180 天"


def test_nav_window_anchors_to_latest_snapshot():
    """区间以最后一个快照为锚, 长期没打开页面时不会切出空白窗口."""
    from tracker.ui import portfolio_nav

    rows = [
        {"day": "2026-11-06"},
        {"day": "2026-11-23"},
        {"day": "2026-11-28"},
    ]
    assert portfolio_nav._rows_in_range(rows, None) == rows
    assert portfolio_nav._rows_in_range(rows, 3) == [{"day": "2026-11-28"}]
    assert portfolio_nav._rows_in_range(rows, 7) == rows[1:]
    # 窗口内无数据时回退全部, 不画空图
    assert portfolio_nav._rows_in_range(rows, 0) == rows


def test_theme_table_right_click_menu_js():
    """表格右键菜单: 复制单元格/整行/含表头 + 跳转 K线, 但不劫持输入框."""
    from tracker.ctx_menu_js import CTX_MENU_KIT_JS
    from tracker.ui import theme

    js = theme._TABLE_CONTEXT_MENU_JS
    assert CTX_MENU_KIT_JS in js
    assert "contextmenu" in js
    assert "复制单元格" in js
    assert "复制整行" in js
    assert "复制含表头" in js
    assert "'kline?symbol='" in js
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
    # 触控板两指双击: 同位置短时间内的第二次 contextmenu 是同一手势,
    # 不能重开菜单/叠出浏览器原生菜单
    assert "两指双击" in js
    assert "document.querySelector('.fp-ctx-menu')" in js
    assert "fp-ctx-menu, .fp-ctx-toast" in js
    assert "lastContextAt" in js
    # 单次右键 (菜单尚未打开) 不能被去重逻辑吞掉
    assert "var openMenu = menu || document.querySelector('.fp-ctx-menu')" in js
    # Chromium/Edge 的 Glide 用 canvas 绘制, 无障碍 td 是 0×0:
    # 必须从 canvas 的 React props 反推行列, 否则 Edge 上表格右键完全失效
    assert "data-grid-canvas" in js
    assert "__reactFiber$" in js
    assert "cellFromCanvas" in js
    assert "cellYOffset" in js
    assert "freezeColumns" in js
    # 整行/含表头复制不受横向虚拟化影响: 从 canvas props 取全部列
    assert "rowFromGrid" in js
    assert "getCellContent" in js
    # 相对 URL: 反向代理 / --server.baseUrlPath 下保留挂载前缀
    assert "window.location.href" in js
    # 侧栏 data_editor 也要能右键删除 (未保存的新增行由 Python 端拦截)
    assert "watchlist_editor: ['delete_watchlist'" in js
    assert "holdings_editor: ['delete_holdings'" in js
    # 键盘 Tab 离开菜单时收起
    assert "focusin" in js
    # 右键/Control+左键的 pointerdown 不能先把菜单关掉 (两指双击第二下)
    assert "ev.button === 2 || ev.ctrlKey" in js


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
    # 移动端选择页面后自动收起侧边栏 (否则一直遮住内容)
    assert "stSidebarNavLink" in js
    assert "stSidebarCollapseButton" in js


def test_theme_css_has_no_raw_lt():
    """CSS 里出现 "<" 会让 st.html 的 DOMPurify 丢弃整个 style 标签.

    表现是主题和全部移动端样式静默失效 (页面退回 Streamlit 默认外观), 且
    Python/浏览器都不报错。注释里请写「小于 16px」而不是「<16px」。
    """
    from tracker.ui import theme

    assert "<" not in theme._CSS
    assert "<" not in theme._CONTEXT_MENU_CSS


def test_theme_mobile_rules():
    """移动端: 防止 iOS 聚焦缩放, 指标卡固定 2×2, 健康页标签折行."""
    from tracker.ui import theme

    css = theme._CSS
    assert "@media (max-width: 640px)" in css
    assert "font-size: 16px !important" in css
    assert "grid-template-columns: repeat(2, minmax(0, 1fr))" in css
    assert ".st-key-health_tabs [role=\"tablist\"]" in css


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
