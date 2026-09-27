"""组合主页面 (tracker/ui/app.py) 测试: import 无副作用 + AppTest 全链路渲染.

两个不变量:
1. import 模块只定义 main(), 不执行页面脚本 —— 顶层曾直接拉取全部自选行情,
   一处 import 就把整套测试拖死 (沙箱无网时无限挂起)。
2. AppTest.from_file 走与 `streamlit run` 相同的 __main__ 执行模型, 能完整
   渲染组合页 —— 保证 main() 入口守卫没有把页面"锁死"在 import 里。
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


@pytest.fixture
def app_boot(monkeypatch, tmp_path):
    wl = tmp_path / "watchlist.json"
    pf = tmp_path / "portfolio.json"
    storage.save_watchlist(
        {"watchlist": [{"symbol": "AAPL", "lists": ["测试"], "upper_1": 200.0}]}, wl
    )
    storage.save_portfolio(
        {
            "base_currency": "CNY",
            "holdings": [{"symbol": "AAPL", "quantity": 2, "avg_cost": 100}],
        },
        pf,
    )
    monkeypatch.setattr(app_mod, "WATCHLIST_PATH", wl)
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
    """import tracker.ui.app 只定义 main(), 不执行页面脚本 (不触发网络).

    回归护栏: 曾因模块顶层拉取全部自选行情, 仅这一处 import 就把整套测试
    拖死 (沙箱无网时无限挂起)。任何把页面逻辑收回模块顶层的改动都会撞红。
    """

    def _boom(*a, **kw):
        raise AssertionError("import tracker.ui.app 不得触发行情拉取")

    monkeypatch.setattr(prices, "get_quotes", _boom)
    t0 = time.monotonic()
    importlib.reload(app_mod)
    elapsed = time.monotonic() - t0
    assert callable(app_mod.main)
    assert elapsed < 15, (
        f"import tracker.ui.app 耗时 {elapsed:.1f}s, 疑似执行了页面脚本"
    )


def test_portfolio_page_renders(app_boot):
    """AppTest 跑真实 app.py: main() 完整渲染组合页 (KPI + 持仓 + 自选)."""
    assert any("组合总览" in m.value for m in app_boot.markdown)
    assert any("AAPL" in str(m.value) for m in app_boot.markdown)
    assert app_boot.session_state["app_page"] == "portfolio"
