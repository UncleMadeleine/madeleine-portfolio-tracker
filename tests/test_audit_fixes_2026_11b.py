"""2026-11 第二轮代码审查回归 (全部离线)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from tracker.providers.base import Quote
from tracker.symbols import parse


# ---------------------------------------------------------------------------
# history: 区间高/低取 high/low, 不取收盘价
# ---------------------------------------------------------------------------


def test_history_interval_high_low_uses_ohlc(monkeypatch, capsys):
    from tracker.cli import history as history_cli

    df = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-01-02", "2026-01-05"]),
            "open": [100.0, 100.0],
            "high": [120.0, 101.0],
            "low": [99.0, 80.0],
            "close": [100.0, 100.0],
            "volume": [1.0, 1.0],
        }
    )
    monkeypatch.setattr(history_cli.prices, "get_history", lambda *a, **k: df)
    args = SimpleNamespace(
        symbol="AAPL",
        months=1,
        akshare=False,
        ibkr=False,
        longport=False,
        crypto_source="auto",
        json=False,
        rows=5,
    )
    history_cli.cmd_history(args)
    out = capsys.readouterr().out
    assert "区间高 120.000 / 低 80.000" in out


def test_history_ignores_trailing_nan_close(monkeypatch, capsys):
    """末根 close=NaN 时用最后有效收盘, 不能打印 "收 10 → nan (+nan%)"."""
    from tracker.cli import history as history_cli

    df = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-01-02", "2026-01-05"]),
            "open": [10.0, 12.0],
            "high": [10.5, 12.5],
            "low": [9.5, 11.5],
            "close": [10.0, float("nan")],
            "volume": [1.0, 1.0],
        }
    )
    monkeypatch.setattr(history_cli.prices, "get_history", lambda *a, **k: df)
    args = SimpleNamespace(
        symbol="AAPL",
        months=1,
        akshare=False,
        ibkr=False,
        longport=False,
        crypto_source="auto",
        json=False,
        rows=5,
    )
    history_cli.cmd_history(args)
    out = capsys.readouterr().out
    assert "收 10.000 → 10.000" in out
    assert "+nan%" not in out.lower()


# ---------------------------------------------------------------------------
# K线摘要: 低价币精度随价格量级
# ---------------------------------------------------------------------------


def test_summarize_ohlc_exposes_precision():
    from tracker import charting

    close = [1.2e-5 + i * 1e-7 for i in range(20)]
    df = pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=20, freq="D"),
            "open": close,
            "high": [c * 1.01 for c in close],
            "low": [c * 0.99 for c in close],
            "close": close,
            "volume": [0.0] * 20,
        }
    )
    s = charting.summarize_ohlc(df)
    assert s["precision"] >= 8
    assert s["last_close"] > 0


def test_resample_ohlc_does_not_resample_native_weekly_twice():
    """原生周K再调用 resample(weekly) 必须保持原日期, 不能被改成 W-FRI."""
    from tracker import charting

    df = pd.DataFrame(
        {
            # 周一 (非周五) 的周K, 若被二次重采样会挪到周五
            "date": pd.to_datetime(["2026-01-05", "2026-01-12", "2026-01-19"]),
            "open": [1.0, 2.0, 3.0],
            "high": [1.1, 2.1, 3.1],
            "low": [0.9, 1.9, 2.9],
            "close": [1.0, 2.0, 3.0],
            "volume": [1.0, 1.0, 1.0],
        }
    )
    out = charting.resample_ohlc(df, "weekly")
    assert list(out["date"]) == list(df["date"])


# ---------------------------------------------------------------------------
# 水泥网: 时间戳按中国本地日期解释 + 同日重复值收敛
# ---------------------------------------------------------------------------


def test_ccement_kline_uses_china_local_date():
    from tracker.providers import index as idx_mod

    # 2026-01-09 00:00 Asia/Shanghai == 2026-01-08 16:00 UTC
    rows = [[1767888000000, 1.0, 1.0, 1.0, 1.0, 0.0, 0.0, 0.0]]
    df = idx_mod._ccement_kline(rows)
    assert df.iloc[0]["date"] == pd.Timestamp("2026-01-09")


def test_ccement_points_dedupes_same_date_keep_last():
    from tracker.providers import index as idx_mod

    d = {
        "dynamicIndexDate": ["2016-11-15", "2016-11-15", "2016-11-16"],
        "dynamicIndex": [115.0, 225.63, 230.0],
    }
    df = idx_mod._ccement_points(d)
    assert len(df) == 2
    assert df.iloc[0]["close"] == pytest.approx(225.63)


# ---------------------------------------------------------------------------
# 长桥: SG 不进 quote 批 / 历史 >1000 根翻页
# ---------------------------------------------------------------------------


def test_longport_quote_excludes_singapore(monkeypatch):
    from tracker import longport as lp

    calls = []

    def fake_ctx(fn, *args, **kwargs):
        calls.append(args)
        return []

    monkeypatch.setattr(lp, "_ctx_call", fake_ctx)
    quotes, reason = lp.get_quotes_longport([parse("D05.SI"), parse("AAPL")])
    assert quotes == {}
    assert reason is None
    # 第一次 _ctx_call 就是 quote 批: SG 已剔除, 只发 AAPL.US
    assert calls[0] == (["AAPL.US"],)


def _fake_candle(d: date, close: float = 1.0):
    return SimpleNamespace(
        # 与真实 SDK 对偶: 美股日线时间戳是交易所本地 00:00。这里用 aware
        # datetime, 避免 CI 进程时区 (UTC/HKT) 改变日期断言。
        timestamp=datetime(d.year, d.month, d.day, tzinfo=ZoneInfo("America/New_York")),
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1,
    )


def test_longport_history_pages_past_1000_bars(monkeypatch):
    from tracker import longport as lp

    end = date(2026, 6, 30)
    first_page_start = end - timedelta(days=999)
    requested_ends = []

    def fake_ctx(fn, *args, **kwargs):
        ctx = SimpleNamespace()

        def history_candlesticks_by_date(symbol, period, adjust, start, cursor_end):
            requested_ends.append(cursor_end)
            if len(requested_ends) == 1:
                dates = [first_page_start + timedelta(days=i) for i in range(1000)]
            else:
                # 第二页 30 根, 全部早于第一页起点
                dates = [
                    first_page_start - timedelta(days=i) for i in range(30, 0, -1)
                ]
            return [_fake_candle(d) for d in dates]

        ctx.history_candlesticks_by_date = history_candlesticks_by_date
        return fn(ctx)

    monkeypatch.setattr(lp, "_ctx_call", fake_ctx)
    monkeypatch.setattr(
        lp,
        "_sdk",
        lambda: SimpleNamespace(
            Period=SimpleNamespace(Day="day"),
            AdjustType=SimpleNamespace(ForwardAdjust="forward"),
        ),
    )
    df = lp.get_history_longport(parse("AAPL"), "2023-01-01", "2026-06-30")
    assert df is not None
    assert len(df) == 1030
    assert requested_ends == [end, first_page_start - timedelta(days=1)]
    assert df["date"].is_monotonic_increasing


# ---------------------------------------------------------------------------
# UI 后台保温: 别名代码先归一, 不能永久 dirty
# ---------------------------------------------------------------------------


def test_disk_refresh_plan_normalizes_symbols(monkeypatch):
    from tracker import cache as cache_mod
    from tracker.services import warm as warm_mod

    monkeypatch.setattr(
        warm_mod.storage,
        "load_settings",
        lambda: {
            "prefer_akshare": False,
            "use_ibkr": False,
            "use_longport": False,
            "crypto_source": "auto",
        },
    )
    monkeypatch.setattr(
        warm_mod.storage,
        "load_portfolio",
        lambda *a, **k: {
            "base_currency": "CNY",
            "holdings": [{"symbol": "600519.SH", "quantity": 1}],
        },
    )
    monkeypatch.setattr(
        warm_mod.storage, "load_watchlist", lambda *a, **k: {"watchlist": []}
    )
    monkeypatch.setattr(warm_mod, "merge_entries", lambda w: [])
    quote = Quote(
        symbol="600519.SS",
        name="贵州茅台",
        price=1500.0,
        prev_close=1490.0,
        change_pct=1.0,
        currency="CNY",
    )
    monkeypatch.setattr(
        cache_mod,
        "get_cached_with_age",
        lambda symbols, ttl=None: ({"600519.SS": quote}, {"600519.SS": 0.0}),
    )
    plan = warm_mod.disk_refresh_plan()
    assert plan is not None
    assert plan["quotes"][0] == ("600519.SS",)


# ---------------------------------------------------------------------------
# 设置页: 数据源切换同时清 K线内存缓存
# ---------------------------------------------------------------------------


def test_settings_clear_source_caches_clears_kline_memory(monkeypatch):
    from tracker import cache as cache_mod
    from tracker.providers import orchestration
    from tracker.ui import kline_page, portfolio_page, settings_page

    called = []
    for name in ("clear_quotes", "clear_fundamentals", "clear_fx", "clear_ohlc"):
        monkeypatch.setattr(
            cache_mod, name, (lambda n: lambda: called.append(n))(name)
        )
    monkeypatch.setattr(orchestration, "neg_clear_all", lambda: called.append("neg"))
    monkeypatch.setattr(
        kline_page, "clear_kline_caches", lambda: called.append("kline")
    )
    monkeypatch.setattr(
        portfolio_page.cached_quotes, "clear", lambda: called.append("quotes_mem")
    )
    monkeypatch.setattr(
        portfolio_page.cached_fx, "clear", lambda: called.append("fx_mem")
    )
    settings_page._clear_source_caches()
    assert "kline" in called
    assert "quotes_mem" in called
    assert "fx_mem" in called
