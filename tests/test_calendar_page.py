"""「日历」页面渲染测试: monkeypatch 取数, 离线走真实渲染路径."""

from __future__ import annotations

import pytest


@pytest.fixture
def fake_calendar_result():
    return {
        "window": {"start": "2026-10-01", "end": "2026-11-30", "months": 2},
        "events": [
            {
                "symbol": "600519.SS",
                "name": "贵州茅台",
                "type": "earnings",
                "date": "2026-10-31",
                "date_status": "planned",
                "source": "akshare",
                "market_label": "A股",
            },
            {
                "symbol": "AAPL",
                "name": "苹果公司",
                "type": "earnings",
                "date": "2026-11-03",
                "date_status": "estimate",
                "note": "EPS 估计 ~1.98",
                "source": "yfinance",
                "market_label": "美股",
            },
            {
                "symbol": "2877.HK",
                "name": "神威药业",
                "type": "ex_dividend",
                "date": "2026-10-02",
                "date_status": "planned",
                "source": "yfinance",
                "market_label": "港股",
            },
        ],
        "issues": [],
        "sources": {"yfinance": 2, "akshare": 1},
        "names": {"AAPL": "苹果公司", "600519.SS": "贵州茅台"},
    }


def _run_page(monkeypatch, fake_calendar_result):
    from streamlit.testing.v1 import AppTest

    import tracker.ui.calendar_page as calendar_page

    monkeypatch.setattr(calendar_page, "cached_calendar", lambda *a, **kw: fake_calendar_result)
    monkeypatch.setattr(calendar_page, "_resolve_scope", lambda: ["AAPL", "600519.SS", "2877.HK"])
    monkeypatch.setattr(calendar_page, "_watch_lists", lambda: [])
    monkeypatch.setattr(calendar_page, "load_watchlist", lambda *a, **kw: {"watchlist": []})

    at = AppTest.from_string(
        "import tracker.ui.calendar_page as cp\ncp.render_calendar_page()"
    )
    at.run(timeout=60)
    return at


def test_calendar_page_renders(monkeypatch, fake_calendar_result):
    """月历网格 + 明细表: 空数据依赖下仍完整渲染 (不炸)."""
    at = _run_page(monkeypatch, fake_calendar_result)
    assert not at.exception, at.exception
    texts = " ".join(m.value for m in at.markdown)
    assert "财经日历" in texts
    # 月历标题 (两个月)
    assert "2026 年 10 月" in texts
    assert "2026 年 11 月" in texts
    # 单元格事件 (标记: 报=财报 息=除权除息)
    assert "报 600519.SS" in texts
    assert "报 AAPL" in texts
    assert "息 2877.HK" in texts
    # 事件图例/窗口 caption 在 at.caption
    caps = " ".join(c.value for c in at.caption)
    assert "报=财报" in caps
    assert "窗口 2026-10-01 → 2026-11-30" in caps


def test_calendar_page_detail_table(monkeypatch, fake_calendar_result):
    """明细表: dataframe 元素存在且含事件列 (行数 = 事件数)."""
    at = _run_page(monkeypatch, fake_calendar_result)
    assert not at.exception, at.exception
    assert at.dataframe, "应有明细 dataframe"
    # dataframe 元素 value == 原始 DataFrame (行数 = 事件数)
    assert len(at.dataframe[0].value) == 3


def test_calendar_page_empty_scope(monkeypatch):
    """范围内零代码 → info 提示, 不炸."""
    from streamlit.testing.v1 import AppTest

    import tracker.ui.calendar_page as calendar_page

    monkeypatch.setattr(calendar_page, "_resolve_scope", lambda: [])
    at = AppTest.from_string(
        "import tracker.ui.calendar_page as cp\ncp.render_calendar_page()"
    )
    at.run(timeout=60)
    assert not at.exception, at.exception
    assert any("范围内没有代码" in i.value for i in at.info)


def test_apply_names_overlays_watchlist(monkeypatch):
    """watchlist 改名后立即生效 (名称叠加在取数缓存之外)."""
    import tracker.ui.calendar_page as cp

    monkeypatch.setattr(cp, "_watch_names", lambda: {"AAPL": "苹果新名"})
    data = {
        "window": {"start": "2026-10-01", "end": "2026-11-30", "months": 2},
        "events": [
            {
                "symbol": "AAPL",
                "name": None,
                "type": "earnings",
                "date": "2026-11-03",
            },
            {
                "symbol": "600519.SS",
                "name": "贵州茅台",
                "type": "earnings",
                "date": "2026-10-31",
            },
        ],
        "issues": [],
        "sources": {},
        "names": {},
    }
    out = cp._apply_names(data)
    assert out["events"][0]["name"] == "苹果新名"
    assert out["events"][1]["name"] == "贵州茅台"  # 源名称优先
    assert data["events"][0]["name"] is None  # 不改动缓存对象


def test_refresh_button_bypasses_cache(monkeypatch, fake_calendar_result):
    """刷新按钮: 清页面缓存并直接穿透磁盘缓存取数."""
    from streamlit.testing.v1 import AppTest

    import tracker.ui.calendar_page as cp

    class FakeCache:
        def __init__(self):
            self.cleared = False

        def __call__(self, *a, **kw):
            raise AssertionError("刷新时不应走缓存")

        def clear(self):
            self.cleared = True

    fake = FakeCache()
    calls = {}

    def fake_fetch(symbols, months, use_longport, refresh, progress=None):
        calls["refresh"] = refresh
        if progress is not None:
            progress(1, 1, "测试")
        return fake_calendar_result

    monkeypatch.setattr(cp, "cached_calendar", fake)
    monkeypatch.setattr(cp, "_fetch_calendar", fake_fetch)
    monkeypatch.setattr(cp, "_resolve_scope", lambda: ["AAPL", "600519.SS"])
    monkeypatch.setattr(cp, "_watch_lists", lambda: [])
    monkeypatch.setattr(cp, "load_watchlist", lambda *a, **kw: {"watchlist": []})
    at = AppTest.from_string(
        "import tracker.ui.calendar_page as cp\ncp.render_calendar_page()"
    )
    at.run(timeout=60)
    assert not at.exception, at.exception
    at.button[0].click().run(timeout=60)
    assert not at.exception, at.exception
    assert fake.cleared is True
    assert calls["refresh"] is True


def test_cache_miss_blocks_with_progress_fetch(monkeypatch, fake_calendar_result):
    """完全无缓存: 探针返回 None → 阻塞取数 (带 progress 回调) 并正常渲染."""
    from streamlit.testing.v1 import AppTest

    import tracker.ui.calendar_page as cp

    seen = {}

    def fake_cached(*a, **kw):
        return None

    fake_cached.clear = lambda: seen.setdefault("cleared", True)  # type: ignore[attr-defined]

    def fake_fetch(symbols, months, use_longport, refresh, progress=None):
        seen["refresh"] = refresh
        seen["has_progress"] = progress is not None
        if progress is not None:
            progress(1, 2, "AAPL")
            progress(2, 2, None)
        return fake_calendar_result

    monkeypatch.setattr(cp, "cached_calendar", fake_cached)
    monkeypatch.setattr(cp, "_fetch_calendar", fake_fetch)
    monkeypatch.setattr(cp, "_resolve_scope", lambda: ["AAPL", "600519.SS"])
    monkeypatch.setattr(cp, "_watch_lists", lambda: [])
    monkeypatch.setattr(cp, "load_watchlist", lambda *a, **kw: {"watchlist": []})
    at = AppTest.from_string(
        "import tracker.ui.calendar_page as cp\ncp.render_calendar_page()"
    )
    at.run(timeout=60)
    assert not at.exception, at.exception
    assert seen["refresh"] is False
    assert seen["has_progress"] is True
    assert seen["cleared"] is True  # 取完后清探针缓存, 下次直接读新盘
    texts = " ".join(m.value for m in at.markdown)
    assert "报 AAPL" in texts


def test_stale_peek_shows_banner(monkeypatch, fake_calendar_result):
    """缓存探针命中陈旧数据: 立即渲染 + 提示后台刷新 (不触发阻塞取数)."""
    from streamlit.testing.v1 import AppTest

    import tracker.ui.calendar_page as cp

    data = {
        **fake_calendar_result,
        "cache": {"used": True, "stale": True, "stale_age": 7200.0, "partial": False},
    }

    def fake_cached(*a, **kw):
        return data

    fake_cached.clear = lambda: None  # type: ignore[attr-defined]

    def boom(*a, **kw):
        raise AssertionError("陈旧缓存命中时不应阻塞取数")

    monkeypatch.setattr(cp, "cached_calendar", fake_cached)
    monkeypatch.setattr(cp, "_fetch_calendar", boom)
    # 不真正起后台线程 (本用例只验证陈旧缓存横幅)
    monkeypatch.setattr(cp, "_start_calendar_refresh", lambda *a, **kw: False)
    monkeypatch.setattr(cp, "_resolve_scope", lambda: ["AAPL", "600519.SS"])
    monkeypatch.setattr(cp, "_watch_lists", lambda: [])
    monkeypatch.setattr(cp, "load_watchlist", lambda *a, **kw: {"watchlist": []})
    at = AppTest.from_string(
        "import tracker.ui.calendar_page as cp\ncp.render_calendar_page()"
    )
    at.run(timeout=60)
    assert not at.exception, at.exception
    caps = " ".join(c.value for c in at.caption)
    assert "先展示约 2.0 小时前的缓存" in caps
    texts = " ".join(m.value for m in at.markdown)
    assert "报 AAPL" in texts


def test_calendar_page_fetch_failure_shows_warning(monkeypatch):
    """取数抛错 → warning 而不是裸 traceback."""
    from streamlit.testing.v1 import AppTest

    import tracker.ui.calendar_page as calendar_page

    def boom(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(calendar_page, "cached_calendar", boom)
    monkeypatch.setattr(calendar_page, "_resolve_scope", lambda: ["AAPL"])
    monkeypatch.setattr(calendar_page, "_watch_lists", lambda: [])
    monkeypatch.setattr(calendar_page, "load_watchlist", lambda *a, **kw: {"watchlist": []})
    at = AppTest.from_string(
        "import tracker.ui.calendar_page as cp\ncp.render_calendar_page()"
    )
    at.run(timeout=60)
    assert not at.exception, at.exception
    assert any("日历获取失败" in w.value for w in at.warning)
