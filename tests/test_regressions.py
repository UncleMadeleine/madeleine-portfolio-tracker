"""代码审查回归测试：按修复主题收拢的历史边界用例（全部离线）.

原先按修复批次拆成多个日期命名的文件，现合并为一个按批次分段的回归套件，
避免新增修复时不断产生日期命名的测试文件。
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from tracker import cache as cache_mod
from tracker import charting, fundamentals, importer, mcap, nav, storage, symbols
from tracker.ashare_sync import _to_float
from tracker.providers.base import Quote
from tracker.providers.orchestration import _cached_source_ok
from tracker.symbols import parse, type_for_symbol

# ===========================================================================
# 历史修复批次 A：CLI/图表/指数/长桥/首屏/设置
# ===========================================================================

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


# ===========================================================================
# 历史修复批次 B：存储/导入/行情/缓存/NAV/CLI
# ===========================================================================

def test_save_watchlist_keeps_existing_rows_when_key_absent(tmp_path):
    p = tmp_path / "watchlist.json"
    storage.save_watchlist({"watchlist": [{"symbol": "AAPL"}]}, p)
    # 只改其它字段 (如 _说明) 不应把整份自选静默清空
    storage.save_watchlist({"_说明": "维护中"}, p)
    rows = storage.load_watchlist(p)["watchlist"]
    assert [r["symbol"] for r in rows] == ["AAPL"]
    # 显式 null 仍按「清空」处理
    storage.save_watchlist({"watchlist": None}, p)
    assert storage.load_watchlist(p)["watchlist"] == []


def test_aggregate_rows_uses_weighted_average_cost():
    merged, _stats = importer.merge_holdings(
        [],
        [
            {"symbol": "AAPL", "quantity": 100, "avg_cost": 10},
            {"symbol": "AAPL", "quantity": 200, "avg_cost": 20},
        ],
        importer.MODE_APPEND,
    )
    assert len(merged) == 1
    assert merged[0]["quantity"] == 300.0
    assert merged[0]["avg_cost"] == pytest.approx(50 / 3)


def test_aggregate_rows_marks_cost_unknown_if_any_leg_missing():
    merged, _stats = importer.merge_holdings(
        [],
        [
            {"symbol": "AAPL", "quantity": 100, "avg_cost": 10},
            {"symbol": "AAPL", "quantity": 200},
        ],
        importer.MODE_APPEND,
    )
    assert merged[0]["quantity"] == 300.0
    assert merged[0]["avg_cost"] is None


def test_wallet_reimport_prunes_positions_that_went_to_zero():
    src = "wallet:eth:0xabc"
    existing = [
        {
            "symbol": "USDT-USD",
            "quantity": 100.0,
            "import_source": src,
            "source_quantities": {src: 100.0},
        },
        {
            # 同一代码还有手动录入分量, 只清钱包分量, 不能整行删除
            "symbol": "ETH-USD",
            "quantity": 3.0,
            "source_quantities": {"": 1.0, src: 2.0},
        },
    ]
    merged, stats = importer.merge_holdings(
        existing, [], importer.MODE_APPEND, prune_source=src
    )
    assert [h["symbol"] for h in merged] == ["ETH-USD"]
    assert merged[0]["quantity"] == 1.0
    assert "USDT-USD" in stats["updated"]


def test_wallet_reimport_without_prune_keeps_stale_positions():
    src = "wallet:eth:0xabc"
    existing = [
        {
            "symbol": "USDT-USD",
            "quantity": 100.0,
            "import_source": src,
            "source_quantities": {src: 100.0},
        }
    ]
    merged, _stats = importer.merge_holdings(
        existing, [], importer.MODE_APPEND
    )
    assert len(merged) == 1


def test_ashare_float_handles_parentheses_with_currency():
    assert _to_float("(¥1,000.00)") == -1000.0
    assert _to_float("($1,234.50)") == -1234.5
    assert _to_float("¥1,680.50") == 1680.5


# ---------- 行情校验 / 缓存 ----------


def test_binance_non_finite_price_raises(monkeypatch):
    import tracker.providers.crypto as crypto_mod

    monkeypatch.setattr(
        crypto_mod, "_binance_spot_raw", lambda pair: {"lastPrice": "nan"}
    )
    with pytest.raises(RuntimeError):
        crypto_mod._binance_quote(parse("BTC-USD"))


def test_gate_non_finite_price_raises(monkeypatch):
    import tracker.providers.crypto as crypto_mod

    monkeypatch.setattr(
        crypto_mod, "_gate_get", lambda *a, **k: [{"last": "inf"}]
    )
    with pytest.raises(RuntimeError):
        crypto_mod._gate_quote(parse("ETH-USD"))


def test_hyperliquid_non_finite_price_raises(monkeypatch):
    import tracker.providers.crypto as crypto_mod

    monkeypatch.setattr(
        crypto_mod,
        "_hl_post",
        lambda payload: (
            {"universe": [{"name": "BTC"}]},
            [{"markPx": "inf", "prevDayPx": "1"}],
        ),
    )
    with pytest.raises(RuntimeError):
        crypto_mod._hl_quote(parse("BTC-USD"))


def test_crypto_non_finite_change_and_prev_are_dropped(monkeypatch):
    """涨跌幅/昨收为 inf/nan 时归一成 None, 不能污染今日盈亏与 JSON 输出."""
    import tracker.providers.crypto as crypto_mod
    from tracker.symbols import parse

    monkeypatch.setattr(
        crypto_mod,
        "_binance_spot_raw",
        lambda pair: {
            "lastPrice": "100",
            "prevClosePrice": "inf",
            "priceChangePercent": "inf",
        },
    )
    q = crypto_mod._binance_quote(parse("BTC-USD"))
    assert q.prev_close is None
    assert q.change_pct is None

    monkeypatch.setattr(
        crypto_mod,
        "_gate_get",
        lambda *a, **k: [{"last": "100", "change_percentage": "nan"}],
    )
    q = crypto_mod._gate_quote(parse("BTC-USD"))
    assert q.prev_close is None
    assert q.change_pct is None

    monkeypatch.setattr(
        crypto_mod,
        "_hl_post",
        lambda payload: (
            {"universe": [{"name": "BTC"}]},
            [{"markPx": "100", "prevDayPx": "inf"}],
        ),
    )
    q = crypto_mod._hl_quote(parse("BTC-USD"))
    assert q.prev_close is None
    assert q.change_pct is None


def test_ohlc_cache_rejects_payload_missing_price_columns(monkeypatch):
    monkeypatch.setattr(
        cache_mod, "_read_one", lambda *a, **k: ('[{"date": "2025-01-01"}]',)
    )
    assert cache_mod.get_ohlc_cached("AAPL", 1) is None


def test_akshare_quote_uses_reported_currency(monkeypatch):
    import tracker.providers.global_stocks as gs

    monkeypatch.setattr(
        gs,
        "_ak_quote_raw",
        lambda p: {"f43": 6.315, "f58": "测试", "f172": "USD"},
    )
    q = gs._akshare_quote(parse("09188.HK"))
    assert q.currency == "USD"

    monkeypatch.setattr(gs, "_ak_quote_raw", lambda p: {"f43": 6.315})
    assert gs._akshare_quote(parse("09188.HK")).currency == "HKD"


def test_akshare_quote_a_share_ignores_placeholder_currency(monkeypatch):
    """东财 A 股 f172="-": 必须用代码权威币种, 不能污染行情缓存与汇率链."""
    import tracker.providers.global_stocks as gs

    monkeypatch.setattr(
        gs,
        "_ak_quote_raw",
        lambda p: {"f43": 1500.0, "f58": "贵州茅台", "f172": "-"},
    )
    assert gs._akshare_quote(parse("600519.SS")).currency == "CNY"
    # B 股同理: 沪 B 美元 / 深 B 港币, 不能被 f172 的占位符打回 CNY。
    assert gs._akshare_quote(parse("900901.SS")).currency == "USD"
    assert gs._akshare_quote(parse("200012.SZ")).currency == "HKD"


# ---------- 代码规范 ----------


def test_unsupported_crypto_quote_is_rejected_consistently():
    assert type_for_symbol("BTC-PLN") != "crypto"
    with pytest.raises(ValueError, match="不支持的加密货币计价货币"):
        parse("BTC-PLN")
    # 支持的法币/稳定币计价不受影响
    assert parse("BTC-EUR").type == "crypto"
    assert parse("ETH-USDT").currency == "USD"


# ---------- 数据源优先级 ----------
def test_cached_source_requires_highest_priority_source():
    q = Quote(
        symbol="AAPL",
        name="Apple",
        price=1.0,
        prev_close=None,
        change_pct=None,
        currency="USD",
        source="longport",
    )
    assert not _cached_source_ok(
        q,
        parse("AAPL"),
        prefer_akshare=False,
        use_ibkr=True,
        use_longport=True,
        crypto_source="auto",
    )


def test_fx_cache_does_not_short_circuit_explicit_ibkr(monkeypatch):
    from tracker import fx

    cache_mod.set_fx_cached("CNY", {"USD": 7.0})
    # 非 --ibkr 路径仍吃缓存
    rates, _ = fx.get_fx_rates("CNY", ["USD"])
    assert rates["USD"] == pytest.approx(7.0)

    monkeypatch.setattr(
        fx,
        "get_rate",
        lambda src, dst, use_ibkr=False: 7.3 if use_ibkr else 7.0,
    )
    # 显式 --ibkr 必须回源 (即使磁盘缓存仍新鲜)
    rates, _ = fx.get_fx_rates("CNY", ["USD"], use_ibkr=True)
    assert rates["USD"] == pytest.approx(7.3)


# ---------- NAV 回填 ----------

def _nav_mock(monkeypatch, ohlc):
    def fake_quotes(symbols, **kwargs):
        return (
            {
                s: Quote(
                    symbol=s,
                    name=s,
                    price=1.0,
                    prev_close=None,
                    change_pct=None,
                    currency="USD",
                )
                for s in symbols
            },
            {},
            [],
        )

    monkeypatch.setattr("tracker.prices.get_quotes", fake_quotes)
    monkeypatch.setattr(
        "tracker.prices.get_ohlc",
        lambda symbol, **kwargs: ohlc[symbol],
    )


def test_backfill_ffills_prior_close_for_window_first_day(monkeypatch):
    """窗口首日某市场休市时, 用窗口前最后一个收盘价, 不能凭空少算市值."""
    _nav_mock(
        monkeypatch,
        {
            # AAPL 在 01-01 休市, 只有 2025-12-31 和 01-02
            "AAPL": pd.DataFrame(
                {
                    "date": ["2025-12-31", "2026-01-02"],
                    "close": [10.0, 11.0],
                }
            ),
            "MSFT": pd.DataFrame(
                {
                    "date": ["2026-01-01", "2026-01-02"],
                    "close": [100.0, 100.0],
                }
            ),
        },
    )
    portfolio = {
        "holdings": [
            {"symbol": "AAPL", "quantity": 10, "avg_cost": 100},
            {"symbol": "MSFT", "quantity": 1, "avg_cost": 100},
        ]
    }
    rows, issues = nav.backfill(
        portfolio, "USD", days=2, today=date(2026, 1, 3)
    )
    assert issues == []
    first = rows[0]
    assert first["day"] == "2026-01-01"
    # AAPL: 10×10 ffill + MSFT: 1×100 (无汇率, base=USD)
    assert first["total_value"] == pytest.approx(200.0)


def test_backfill_total_cost_is_gross_for_mixed_long_short(monkeypatch):
    _nav_mock(
        monkeypatch,
        {
            "AAPL": pd.DataFrame(
                {"date": ["2026-01-02"], "close": [100.0]}
            ),
            "TSLA": pd.DataFrame(
                {"date": ["2026-01-02"], "close": [200.0]}
            ),
        },
    )
    portfolio = {
        "holdings": [
            {"symbol": "AAPL", "quantity": 10, "avg_cost": 100},
            {"symbol": "TSLA", "quantity": -5, "avg_cost": 200},
        ]
    }
    rows, issues = nav.backfill(
        portfolio, "USD", days=2, today=date(2026, 1, 3)
    )
    assert issues == []
    assert rows[0]["total_cost"] == pytest.approx(2000.0)
    assert rows[0]["total_pnl"] == pytest.approx(0.0)


# ---------- 图表 payload ----------


def test_chart_payloads_always_have_query_id():
    df = pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=3),
            "open": [1.0, 2.0, 3.0],
            "high": [1.0, 2.0, 3.0],
            "low": [1.0, 2.0, 3.0],
            "close": [1.0, 2.0, 3.0],
            "volume": [0.0, 0.0, 0.0],
        }
    )
    assert charting.kline_payload(df, "AAPL")["queryId"] == "AAPL"
    assert charting.compare_payload({"AAPL": df})["queryId"]
    assert charting.crypto_compare_payload({"BTC-USD": df})["queryId"]


# ---------- CLI ----------


def test_nav_save_base_override_affects_valuation(capsys, monkeypatch, tmp_path):
    from tracker import cli

    monkeypatch.setattr(
        "tracker.prices.get_quotes",
        lambda *a, **k: (
            {
                "AAPL": Quote(
                    symbol="AAPL",
                    name="Apple",
                    price=190.0,
                    prev_close=188.0,
                    change_pct=1.0,
                    currency="USD",
                )
            },
            {},
            [],
        ),
    )

    def fake_fx(base, currencies, **kwargs):
        return (
            {c: (1.0 if c == base else 7.0) for c in currencies},
            [],
        )

    monkeypatch.setattr("tracker.services.snapshot.get_fx_rates", fake_fx)
    p = tmp_path / "portfolio.json"
    p.write_text(
        json.dumps(
            {
                "base_currency": "CNY",
                "holdings": [{"symbol": "AAPL", "quantity": 1, "avg_cost": 100}],
            }
        ),
        encoding="utf-8",
    )
    cli.main(["nav", "--portfolio", str(p), "--save", "--base", "USD"])
    rows = nav.load_history("USD")
    assert len(rows) == 1
    # 若估值仍按 CNY 计算, 这里会是 1330; 正确口径应是 USD 190
    assert rows[0]["total_value"] == pytest.approx(190.0)


# ===========================================================================
# 历史修复批次 C：存储/符号/缓存/IBKR/基本面/市值/来源
# ===========================================================================

def test_load_watchlist_null_symbol_is_dropped(tmp_path):
    """JSON null 不能被 str(None) 变成幽灵美股 "NONE"."""
    p = tmp_path / "watchlist.json"
    p.write_text(
        json.dumps({"watchlist": [{"symbol": None, "note": "x"}]}),
        encoding="utf-8",
    )
    assert storage.load_watchlist(p)["watchlist"] == []


def test_save_settings_partial_keeps_existing_values(tmp_path):
    """局部保存只覆盖传入键, 不能把其它开关重置成默认值."""
    p = tmp_path / "settings.json"
    p.write_text(
        json.dumps(
            {
                "color_scheme": "intl",
                "use_ibkr": True,
                "crypto_source": "binance",
            }
        ),
        encoding="utf-8",
    )
    storage.save_settings({"prefer_akshare": True}, p)
    data = storage.load_settings(p)
    assert data["prefer_akshare"] is True
    assert data["color_scheme"] == "intl"
    assert data["use_ibkr"] is True
    assert data["crypto_source"] == "binance"


@pytest.mark.parametrize("raw", ["false", "0", "no", "off"])
def test_load_settings_string_false_is_false(tmp_path, raw):
    """手改 settings.json 写 "false" 时 bool() 不能把它当 True."""
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"use_ibkr": raw}), encoding="utf-8")
    assert storage.load_settings(p)["use_ibkr"] is False


def test_load_watchlist_legacy_group_with_string_lists(tmp_path):
    """旧 watchlists 分组里 lists 是逗号字符串/null 时不能崩, 组名要并集."""
    p = tmp_path / "watchlist.json"
    p.write_text(
        json.dumps(
            {
                "watchlists": {
                    "科技": [{"symbol": "AAPL", "lists": "成长"}],
                    "美股": [{"symbol": "AAPL", "lists": None}],
                }
            }
        ),
        encoding="utf-8",
    )
    entry = storage.load_watchlist(p)["watchlist"][0]
    assert set(entry["lists"]) == {"成长", "科技", "美股"}


def test_save_portfolio_rejects_non_list_holdings(tmp_path):
    """畸形 holdings 不能静默清空已有持仓."""
    p = tmp_path / "portfolio.json"
    storage.save_portfolio({"holdings": [{"symbol": "AAPL"}]}, p)
    with pytest.raises(storage.CorruptDataError):
        storage.save_portfolio({"holdings": "AAPL"}, p)
    assert storage.load_portfolio(p)["holdings"][0]["symbol"] == "AAPL"


def test_save_watchlist_rejects_non_list(tmp_path):
    p = tmp_path / "watchlist.json"
    storage.save_watchlist({"watchlist": [{"symbol": "AAPL"}]}, p)
    with pytest.raises(storage.CorruptDataError):
        storage.save_watchlist({"watchlist": {"symbol": "MSFT"}}, p)
    assert storage.load_watchlist(p)["watchlist"][0]["symbol"] == "AAPL"


# ---------------------------------------------------------------------------
# symbols
# ---------------------------------------------------------------------------


def test_normalize_hk_does_not_pad_non_numeric_codes():
    assert symbols.normalize("ABC.HK") == "ABC.HK"
    assert symbols.normalize("A.HK") == "A.HK"
    assert symbols.normalize("700.HK") == "0700.HK"


# ---------------------------------------------------------------------------
# cache: 只读库不能拖垮取数
# ---------------------------------------------------------------------------


def test_cache_write_failure_is_best_effort(tmp_path, monkeypatch):
    db = tmp_path / "cache.db"
    monkeypatch.setattr(cache_mod, "CACHE_DB", db)
    cache_mod._ensure_db()
    os.chmod(db, 0o444)
    try:
        cache_mod.set_cached(
            {
                "AAPL": Quote(
                    symbol="AAPL",
                    name="Apple",
                    price=1.0,
                    prev_close=1.0,
                    change_pct=0.0,
                    currency="USD",
                )
            }
        )
    finally:
        os.chmod(db, 0o644)


# ---------------------------------------------------------------------------
# IBKR: 历史便士折算 + qualify 失败不串位
# ---------------------------------------------------------------------------


class _Bar:
    def __init__(self, d, close):
        self.date = d
        self.open = close
        self.high = close
        self.low = close
        self.close = close
        self.volume = 1


def test_ibkr_history_lse_pence_is_converted(monkeypatch):
    import tracker.ibkr as ibkr_mod

    class FakeIB:
        def qualifyContracts(self, *contracts):
            return list(contracts)

        def reqHistoricalData(self, *a, **kw):
            return [_Bar(date(2025, 1, 2), 8768.0)]

    monkeypatch.setattr(ibkr_mod, "_get_client", lambda cfg: FakeIB())
    data, reason = ibkr_mod.get_history_ibkr(
        [symbols.parse("LSEG.L")],
        months=1,
        start_date="2025-01-01",
        end_date="2025-01-10",
        cfg={"exchanges": {}},
    )
    assert reason is None
    assert data["LSEG.L"]["close"].iloc[0] == pytest.approx(87.68)


def test_ibkr_quote_pairing_survives_filtered_qualify(monkeypatch):
    """旧版 qualifyContracts 过滤失败项时, 后续合约不能整体错位."""
    import tracker.ibkr as ibkr_mod

    class Contract:
        def __init__(self):
            self.symbol = ""
            self.exchange = ""
            self.currency = ""
            self.secType = ""
            self.tradingClass = ""
            self.conId = 0

    class Ticker:
        def __init__(self, contract, price):
            self.contract = contract
            self._price = price

        def marketPrice(self):
            return self._price

        def __getattr__(self, name):
            return None

    class FakeIB:
        def qualifyContracts(self, *contracts):
            # 模拟旧版: 只返回成功项, 中间 MSFT 失败被过滤掉
            return [contracts[0], contracts[2]]

        def reqTickers(self, *contracts):
            return [Ticker(c, 10.0 + i) for i, c in enumerate(contracts)]

    fake_mod = SimpleNamespace(Contract=Contract)
    out = ibkr_mod._ibkr_chunk_quotes(
        FakeIB(),
        fake_mod,
        [symbols.parse("AAPL"), symbols.parse("MSFT"), symbols.parse("GOOG")],
        {"exchanges": {}},
    )
    assert set(out) == {"AAPL", "GOOG"}
    assert out["AAPL"].price == pytest.approx(10.0)
    assert out["GOOG"].price == pytest.approx(11.0)


# ---------------------------------------------------------------------------
# 基本面批量失败不写负缓存
# ---------------------------------------------------------------------------


def test_fundamentals_batch_failure_does_not_negative_cache(monkeypatch):
    monkeypatch.setattr(fundamentals, "_eodhd_metrics", lambda symbols: {})
    monkeypatch.setattr(
        fundamentals, "_fund_metrics", lambda symbols: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    written = []
    monkeypatch.setattr(
        fundamentals.cache_mod,
        "set_fundamental_cached",
        lambda *a, **k: written.append(a),
    )
    monkeypatch.setattr(
        fundamentals.cache_mod, "get_fundamental_cached", lambda *a, **k: None
    )
    out = fundamentals.fetch_book_values(["AAPL"])
    assert out == {"AAPL": None}
    assert written == []


# ---------------------------------------------------------------------------
# mcap: 股本变更日期保留交易所本地日历日
# ---------------------------------------------------------------------------


def test_shares_history_keeps_exchange_local_date(monkeypatch, tmp_path):
    monkeypatch.setattr("tracker.cache.CACHE_DB", tmp_path / "mcap.db")
    cache_mod._ensure_db()

    class FakeTicker:
        def __init__(self, symbol):
            pass

        def get_shares_full(self, start="1980-01-01"):
            return pd.Series(
                [100.0],
                index=pd.DatetimeIndex(["2025-01-06"], tz="Asia/Hong_Kong"),
            )

    monkeypatch.setattr("yfinance.Ticker", FakeTicker)
    df = mcap._shares_history("0700.HK", refresh=True)
    assert df is not None
    assert df["date"].iloc[0] == pd.Timestamp("2025-01-06")


# ---------------------------------------------------------------------------
# 长桥: CNH 归一为 CNY
# ---------------------------------------------------------------------------


def test_longport_cnh_normalized_to_cny(monkeypatch):
    import tracker.longport as lp
    from unittest.mock import patch

    class FakeCtx:
        def quote(self, symbols):
            return [
                SimpleNamespace(
                    symbol="700.HK", last_done=Decimal("10.0"), prev_close=Decimal("9.0")
                )
            ]

        def calc_indexes(self, symbols, indexes):
            return []

    monkeypatch.setattr(lp, "static_names_longport", lambda syms, cfg=None: ({}, {"700.HK": "CNH"}))
    with patch.object(lp, "_get_quote_ctx", return_value=FakeCtx()):
        quotes, reason = lp.get_quotes_longport([symbols.parse("0700.HK")])
    assert reason is None
    assert quotes["0700.HK"].currency == "CNY"


def test_quote_cache_respects_explicit_source(monkeypatch, tmp_path):
    """`quote --ibkr` 不能被 TTL 内 yfinance 缓存短路 (source_strict)."""
    import tracker.providers.orchestration as orch

    monkeypatch.setattr(cache_mod, "CACHE_DB", tmp_path / "quotes.db")
    cache_mod._ensure_db()
    cache_mod.set_cached(
        {
            "AAPL": Quote(
                symbol="AAPL",
                name="Apple",
                price=100.0,
                prev_close=99.0,
                change_pct=1.0,
                currency="USD",
                source="yfinance",
            )
        }
    )

    def fake_ibkr(parsed, cfg=None):
        p = parsed[0]
        return {
            p.yahoo: Quote(
                symbol=p.yahoo,
                name="Apple",
                price=200.0,
                prev_close=199.0,
                change_pct=0.5,
                currency="USD",
                source="ibkr",
            )
        }, None

    monkeypatch.setattr("tracker.ibkr.get_quotes_ibkr", fake_ibkr)
    monkeypatch.setattr(orch, "_yahoo_batch", lambda plist: {})

    quotes, _errors, _notes = orch.get_quotes(
        ["AAPL"], use_ibkr=True, source_strict=True
    )
    assert quotes["AAPL"].source == "ibkr"
    assert quotes["AAPL"].price == pytest.approx(200.0)

    # UI/设置页路径 (source_strict=False) 仍优先用缓存, 首屏不被拖慢
    cache_mod.set_cached(
        {
            "AAPL": Quote(
                symbol="AAPL",
                name="Apple",
                price=100.0,
                prev_close=99.0,
                change_pct=1.0,
                currency="USD",
                source="yfinance",
            )
        }
    )
    quotes, _errors, _notes = orch.get_quotes(["AAPL"], use_ibkr=True)
    assert quotes["AAPL"].price == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# CLI: 空导入不覆盖 / 空 CSV 有表头 / 比例线混合时区
# ---------------------------------------------------------------------------


def test_watchlist_import_empty_does_not_overwrite(tmp_path, capsys, monkeypatch):
    from tracker import cli

    p = tmp_path / "watchlist.json"
    p.write_text(
        json.dumps({"watchlist": [{"symbol": "AAPL", "lists": ["默认"]}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr("tracker.longport.fetch_watchlist_groups", lambda cfg=None: [])
    cli.main(["watchlist", "import-longport", "--file", str(p), "--overwrite"])
    assert storage.load_watchlist(p)["watchlist"][0]["symbol"] == "AAPL"
    assert "未修改文件" in capsys.readouterr().out


def test_export_empty_csv_keeps_header():
    from tracker.cli.export import _export_csv

    out = _export_csv(pd.DataFrame())
    assert out.startswith("\ufeffsymbol,")
    assert "market_value" in out.splitlines()[0]


def test_compare_ratio_handles_mixed_timezone(monkeypatch):
    from tracker import charting
    from tracker.cli import compare

    naive = pd.DataFrame(
        {"date": pd.to_datetime(["2025-01-06"]), "close": [1.0]}
    )
    aware = pd.DataFrame(
        {
            "date": pd.to_datetime(["2025-01-07"]).tz_localize("Asia/Hong_Kong"),
            "close": [2.0],
        }
    )
    monkeypatch.setattr(compare, "resolve_inputs", lambda args: (["AAPL", "0700.HK"], []))
    monkeypatch.setattr("tracker.fx.get_fx_history", lambda *a, **k: None)
    monkeypatch.setattr(charting, "build_ratio_series", lambda *a, **k: {"ok": True})
    out = compare._build_ratio(
        {"AAPL": naive, "0700.HK": aware},
        ["AAPL", "0700.HK"],
        "daily",
        fx_adjust=True,
    )
    assert out == {"ok": True}


# ===========================================================================
# 历史修复批次 D：缓存/基本面/来源顺序/导入/钱包/UI
# ===========================================================================

@pytest.fixture
def cache_db(tmp_path, monkeypatch):
    """隔离缓存库, 避免测试污染真实 var/quotes_cache.db."""
    from tracker import cache as cache_mod

    db = tmp_path / "cache.db"
    monkeypatch.setattr(cache_mod, "CACHE_DB", db)
    cache_mod._db_ready.pop(str(db), None)
    cache_mod._ensure_db()
    yield cache_mod
    cache_mod._db_ready.pop(str(db), None)


# ---------------------------------------------------------------------------
# 缓存: 截断自愈 / 非有限涨跌幅
# ---------------------------------------------------------------------------


def test_cache_info_and_clear_recover_truncated_db(cache_db):
    """库被就地截断后, cache info/clear 也要能自愈 (不能永久报 no such table)."""
    cache_db.set_cached(
        {"AAPL": Quote("AAPL", "Apple", 100.0, 99.0, 1.0, "USD")}
    )
    # 保留 inode, 模拟外部截断 (0 字节有效空库)。WAL 边车必须一起清掉,
    # 否则连接可能重放旧 schema, 测不到「缺表」路径。
    with open(cache_db.CACHE_DB, "r+b") as f:
        f.truncate(0)
    for suffix in ("-wal", "-shm", "-journal"):
        cache_db.CACHE_DB.with_name(cache_db.CACHE_DB.name + suffix).unlink(
            missing_ok=True
        )

    assert cache_db.info()["total"] == 0
    assert cache_db.clear() == 0
    # 自愈后正常可写
    cache_db.set_cached(
        {"MSFT": Quote("MSFT", "Microsoft", 200.0, 198.0, 1.0, "USD")}
    )
    assert cache_db.get_cached(["MSFT"])["MSFT"].price == 200.0


def test_cached_non_finite_change_pct_normalized(cache_db):
    """历史脏行里的 inf 涨跌幅/昨收读取时必须归一为 None."""
    cache_db.set_cached(
        {"AAPL": Quote("AAPL", "Apple", 100.0, 99.0, 1.0, "USD")}
    )
    with sqlite3.connect(cache_db.CACHE_DB) as con:
        con.execute(
            "UPDATE quotes SET prev_close=?, change_pct=? WHERE symbol='AAPL'",
            (float("inf"), float("-inf")),
        )
        con.commit()
    q = cache_db.get_cached(["AAPL"])["AAPL"]
    assert q.prev_close is None
    assert q.change_pct is None


# ---------------------------------------------------------------------------
# 基本面: 半行缓存不阻塞另一指标 / 刷新可清旧值
# ---------------------------------------------------------------------------


def test_openbb_omitted_symbol_is_not_negative_cached(cache_db, monkeypatch):
    """数据源未返回该代码 ≠ 确认无数据: 不能写 6h 负缓存阻止重试."""
    from tracker import eodhd, fundamentals as f

    monkeypatch.setattr(eodhd, "fundamentals_mode", lambda cfg=None: "off")
    calls: list[list[str]] = []

    def fake_metrics(symbols):
        calls.append(list(symbols))
        return {}

    monkeypatch.setattr(f, "_fund_metrics", fake_metrics)
    assert f.fetch_book_values(["AAPL"]) == {"AAPL": None}
    assert f.fetch_book_values(["AAPL"]) == {"AAPL": None}
    assert len(calls) == 2


def test_eodhd_transient_failure_keeps_field_unconfirmed(cache_db, monkeypatch):
    """EODHD 临时失败时, 其缺失字段不能标记为 checked, 下轮仍应重试."""
    from tracker import eodhd, fundamentals as f

    monkeypatch.setattr(eodhd, "fundamentals_mode", lambda cfg=None: "fallback")
    monkeypatch.setattr(eodhd, "yahoo_to_eodhd", lambda s: "AAPL.US")
    calls = {"eodhd": 0}

    def boom(symbol, path=None):
        calls["eodhd"] += 1
        raise RuntimeError("network down")

    monkeypatch.setattr(eodhd, "fundamentals", boom)
    monkeypatch.setattr(
        f,
        "_fund_metrics",
        lambda symbols: {
            s: {"book_value": 10.0, "market_cap": None} for s in symbols
        },
    )
    assert f.fetch_market_caps(["AAPL"]) == {"AAPL": None}
    assert f.fetch_market_caps(["AAPL"]) == {"AAPL": None}
    assert calls["eodhd"] == 2


def test_cached_non_finite_book_value_and_market_cap_normalized(cache_db):
    """quote 缓存里的 ±Infinity 基本面字段读写都要归一, 不能进 JSON/计算."""
    cache_db.set_cached(
        {
            "AAPL": Quote(
                "AAPL",
                "Apple",
                100.0,
                99.0,
                1.0,
                "USD",
                book_value=float("inf"),
                market_cap=float("inf"),
            )
        }
    )
    q = cache_db.get_cached(["AAPL"])["AAPL"]
    assert q.book_value is None
    assert q.market_cap is None


def test_partial_fundamental_cache_does_not_block_other_metric(
    cache_db, monkeypatch
):
    from tracker import eodhd, fundamentals as f

    monkeypatch.setattr(eodhd, "fundamentals_mode", lambda cfg=None: "off")
    cache_db.set_fundamental_cached("AAPL", 12.5)  # 旧调用方只写了 BVPS
    calls = {"n": 0}

    def fake_metrics(symbols):
        calls["n"] += 1
        return {s: {"book_value": None, "market_cap": 3.0e12} for s in symbols}

    monkeypatch.setattr(f, "_fund_metrics", fake_metrics)
    assert f.fetch_market_caps(["AAPL"]) == {"AAPL": 3.0e12}
    assert calls["n"] == 1


def test_confirmed_negative_fundamental_cache_is_not_retried(cache_db, monkeypatch):
    from tracker import eodhd, fundamentals as f

    monkeypatch.setattr(eodhd, "fundamentals_mode", lambda cfg=None: "off")
    cache_db.set_fundamental_cached("BTC-USD", None, market_cap=None)
    calls = {"n": 0}

    def fake_metrics(symbols):
        calls["n"] += 1
        return {s: None for s in symbols}

    monkeypatch.setattr(f, "_fund_metrics", fake_metrics)
    assert f.fetch_market_caps(["BTC-USD"]) == {"BTC-USD": None}
    assert f.fetch_market_caps(["BTC-USD"]) == {"BTC-USD": None}
    assert calls["n"] == 0


def test_refresh_clears_stale_fundamental_values(cache_db, monkeypatch):
    from tracker import eodhd, fundamentals as f

    monkeypatch.setattr(eodhd, "fundamentals_mode", lambda cfg=None: "off")
    cache_db.set_fundamental_cached("AAPL", 10.0, market_cap=1000.0)
    monkeypatch.setattr(
        f,
        "_fund_metrics",
        lambda syms: {s: {"book_value": None, "market_cap": None} for s in syms},
    )
    assert f.fetch_book_values(["AAPL"], refresh=True) == {"AAPL": None}
    assert f.fetch_market_caps(["AAPL"], refresh=True) == {"AAPL": None}


# ---------------------------------------------------------------------------
# 数据源顺序: 能力过滤 / 缓存命中不重复批量取数
# ---------------------------------------------------------------------------


def test_legacy_required_longport_skips_unsupported_market(monkeypatch):
    """未配置 source_order 时, 长桥不支持的代码不能被要求必须来自长桥."""
    import tracker.longport as lp
    from tracker.providers import orchestration as orch

    monkeypatch.setattr(lp, "yahoo_to_longport", lambda y: None)
    assert (
        orch._legacy_required_quote_source(parse("9984.T"), False, False, True, "auto")
        is None
    )
    # 长桥能映射的代码仍按长桥优先
    monkeypatch.setattr(lp, "yahoo_to_longport", lambda y: "AAPL.US")
    assert (
        orch._legacy_required_quote_source(parse("AAPL"), False, False, True, "auto")
        == "longport"
    )


def test_cached_source_ok_skips_longport_unsupported_market(monkeypatch):
    import tracker.source_order as so
    from tracker.providers import orchestration as orch

    monkeypatch.setattr(
        so, "configured", lambda domain, channel: ["longport", "yfinance"]
    )
    q = Quote("9984.T", "SoftBank", 9000.0, 8900.0, 1.0, "JPY", source="yfinance")
    assert orch._cached_source_ok(
        q, parse("9984.T"), False, False, True, "auto"
    )


def test_cached_source_ok_skips_longport_b_share(monkeypatch):
    import tracker.source_order as so
    from tracker.providers import orchestration as orch

    monkeypatch.setattr(
        so, "configured", lambda domain, channel: ["longport", "akshare"]
    )
    q = Quote("900901.SS", "B-share", 1.0, 0.99, 1.0, "USD", source="akshare")
    assert orch._cached_source_ok(
        q, parse("900901.SS"), False, False, True, "auto"
    )


def test_cached_source_ok_skips_eodhd_unsupported_market(monkeypatch):
    import tracker.eodhd as eodhd
    import tracker.source_order as so
    from tracker.providers import orchestration as orch

    monkeypatch.setattr(
        so, "configured", lambda domain, channel: ["eodhd", "yfinance"]
    )
    monkeypatch.setattr(eodhd, "load_config", lambda: {"quotes": "fallback"})
    monkeypatch.setattr(eodhd, "available", lambda cfg: True)
    q = Quote("SAP.BE", "SAP", 100.0, 99.0, 1.0, "EUR", source="yfinance")
    assert orch._cached_source_ok(
        q, parse("SAP.BE"), False, False, False, "auto"
    )


def test_legacy_prefetch_excludes_fresh_cache(monkeypatch):
    """未配置顺序时, 已从磁盘缓存命中的代码不应再打长桥批量行情."""
    from tracker.providers import orchestration as orch

    fresh = Quote("AAPL", "Apple", 100.0, 99.0, 1.0, "USD")
    monkeypatch.setattr(
        orch.cache_mod, "get_cached", lambda syms, ttl=300: {"AAPL": fresh}
    )
    seen: list[list[str]] = []

    def fake_longport(plist):
        seen.append([p.yahoo for p in plist])
        return {}, None

    monkeypatch.setattr(
        "tracker.longport.get_quotes_longport", fake_longport
    )
    # 只关心 prefetch 列表; global 批量/逐代码阶段全部返回空, 不联网。
    monkeypatch.setattr(orch, "_yahoo_batch", lambda plist: {})
    from tracker.providers.global_stocks import GlobalStocksProvider

    monkeypatch.setattr(
        GlobalStocksProvider,
        "fetch_quote",
        lambda self, p, prefer_first=False: (_ for _ in ()).throw(
            RuntimeError("no network")
        ),
    )
    orch.get_quotes(["AAPL", "MSFT"], use_longport=True)
    assert seen == [["MSFT"]]


# ---------------------------------------------------------------------------
# 其余收敛: 极端字段 / 多空抵消 / ICS NaT / 钱包 RPC
# ---------------------------------------------------------------------------


def test_dotted_us_class_and_preferred_symbols_are_normalized():
    from tracker.symbols import normalize, parse

    assert normalize("BRK.B") == "BRK-B"
    assert normalize("WFC.PL") == "WFC-PL"
    assert parse("BRK.B").yahoo == "BRK-B"
    assert parse("WFC.PL").yahoo == "WFC-PL"
    assert parse("BRK.B").type == "global"


def test_finite_float_shared_helper():
    from tracker.util import finite_float

    assert finite_float(None) is None
    assert finite_float("") is None
    assert finite_float("nan") is None
    assert finite_float("inf") is None
    assert finite_float(object()) is None
    assert finite_float("12.5") == 12.5
    assert finite_float(0) == 0.0


def test_build_view_non_scalar_quantity_is_issue_not_crash():
    from tracker.analytics import build_view

    q = Quote("AAPL", "Apple", 100.0, 99.0, 1.0, "USD")
    view, issues = build_view(
        [{"symbol": "AAPL", "quantity": [1]}], {"AAPL": q}, {"USD": 1.0}
    )
    assert view.empty
    assert any("数量无法解析" in i for i in issues)


def test_nav_holdings_net_zero_dropped():
    from tracker.nav import _holdings_by_symbol

    agg, issues = _holdings_by_symbol(
        {
            "holdings": [
                {"symbol": "AAPL", "quantity": 10, "avg_cost": 100},
                {"symbol": "AAPL", "quantity": -10, "avg_cost": 200},
            ]
        }
    )
    assert agg == {}
    assert any("抵消" in i for i in issues)


def test_importer_mixed_long_short_cost_is_unknown():
    from tracker import importer

    merged, _ = importer.merge_holdings(
        [],
        [
            {"symbol": "AAPL", "quantity": 10, "avg_cost": 100},
            {"symbol": "AAPL", "quantity": -5, "avg_cost": 200},
        ],
    )
    assert merged[0]["quantity"] == 5.0
    assert merged[0]["avg_cost"] is None


def test_importer_cross_source_fills_missing_cost():
    from tracker import importer

    merged, _ = importer.merge_holdings(
        [{"symbol": "BTC-USD", "quantity": 1, "import_source": "wallet:a"}],
        [{"symbol": "BTC-USD", "quantity": 1, "avg_cost": 50000}],
    )
    assert merged[0]["avg_cost"] == 50000
    assert merged[0]["quantity"] == 2.0


def test_ics_skips_nat_date():
    from tracker.ics import build_ics

    text = build_ics(
        [{"symbol": "AAPL", "type": "earnings", "date": pd.NaT}]
    )
    assert "DTSTART" not in text


def test_ashare_to_float_rejects_inf_and_handles_signed_currency():
    from tracker.ashare_sync import _to_float

    assert _to_float("inf") is None
    assert _to_float("1e999") is None
    assert _to_float("-¥1,000") == -1000.0


def test_wallet_rpc_error_wins_over_null_result(monkeypatch):
    from tracker import wallet

    class Resp:
        status_code = 200
        text = ""

        def json(self):
            return {
                "jsonrpc": "2.0",
                "result": None,
                "error": {"code": -32000, "message": "boom"},
            }

    class Req:
        def post(self, *args, **kwargs):
            return Resp()

    monkeypatch.setattr(wallet, "_requests", lambda: Req())
    with pytest.raises(RuntimeError, match="RPC 错误"):
        wallet._rpc_post("https://rpc.example", {"method": "eth_call"})


def test_wallet_token_decimals_inf_is_readable_error():
    from tracker import wallet

    with pytest.raises(ValueError, match="decimals"):
        wallet._validate_token_entry(
            {"symbol": "X", "decimals": float("inf")}, "test"
        )


def test_partial_longport_history_is_not_disk_cached(monkeypatch):
    """长桥翻页中途失败的截断序列只展示, 不能按完整窗口写 30 分钟缓存."""
    from tracker import prices

    df = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-01-02", "2026-01-05"]),
            "open": [10.0, 11.0],
            "high": [10.5, 11.5],
            "low": [9.5, 10.5],
            "close": [10.0, 11.0],
            "volume": [1.0, 1.0],
        }
    )
    df.attrs["partial"] = True
    writes: list = []
    monkeypatch.setattr(prices, "get_history", lambda *a, **k: df)
    monkeypatch.setattr(prices.cache_mod, "get_ohlc_cached", lambda *a, **k: None)
    monkeypatch.setattr(
        prices.cache_mod, "set_ohlc_cached", lambda *a, **k: writes.append(a)
    )
    out = prices.get_ohlc("0700.HK", months=60, use_longport=True)
    assert len(out) == 2
    assert writes == []


def test_snapshot_forwards_source_strict(monkeypatch):
    from tracker.services import snapshot as snap

    q = Quote("AAPL", "Apple", 100.0, 99.0, 1.0, "USD", source="ibkr")
    seen: dict = {}

    def fake_quotes(symbols, **kwargs):
        seen.update(kwargs)
        return {s: q for s in symbols}, {}, []

    monkeypatch.setattr(snap.prices, "get_quotes", fake_quotes)
    monkeypatch.setattr(snap, "get_fx_rates", lambda *a, **k: ({"USD": 1.0}, []))
    snap.take_snapshot({"holdings": [{"symbol": "AAPL", "quantity": 1}]})
    assert seen["source_strict"] is False
    snap.take_snapshot(
        {"holdings": [{"symbol": "AAPL", "quantity": 1}]}, source_strict=True
    )
    assert seen["source_strict"] is True


def test_clear_source_caches_clears_ah_page(monkeypatch):
    from tracker import cache as cache_mod
    from tracker.providers import orchestration
    from tracker.ui import fundamentals_page, index_page, kline_page, portfolio_page
    from tracker.ui import settings_page

    called: list[str] = []

    class DummyCache:
        def clear(self):
            called.append("dummy")

    for name in (
        "clear_quotes",
        "clear_fundamentals",
        "clear_fx",
        "clear_ohlc",
    ):
        monkeypatch.setattr(cache_mod, name, lambda: 0)
    monkeypatch.setattr(orchestration, "neg_clear_all", lambda: None)
    monkeypatch.setattr(kline_page, "clear_kline_caches", lambda: None)
    monkeypatch.setattr(index_page, "cached_index_kline", DummyCache())
    monkeypatch.setattr(portfolio_page, "cached_quotes", DummyCache())
    monkeypatch.setattr(portfolio_page, "cached_fx", DummyCache())
    monkeypatch.setattr(
        fundamentals_page, "clear_ah_caches", lambda: called.append("ah")
    )

    settings_page._clear_source_caches()
    assert "ah" in called


def test_portfolio_nav_guard_ignores_unsaved_editor_changes():
    from tracker.ui.portfolio_page import _holdings_unchanged

    saved = [{"symbol": "AAPL", "quantity": 2, "avg_cost": 100}]
    assert _holdings_unchanged(
        [{"symbol": "AAPL", "quantity": 2.0, "avg_cost": 100.0}], saved
    )
    # 别名归一后仍算同一持仓
    assert _holdings_unchanged(
        [{"symbol": "600519.SH", "quantity": 1}],
        [{"symbol": "600519.SS", "quantity": 1}],
    )
    assert not _holdings_unchanged(
        [{"symbol": "AAPL", "quantity": 100, "avg_cost": 100}], saved
    )


def test_init_restricts_longport_credentials(tmp_path, monkeypatch):
    import stat

    from tracker.cli import init as init_cli

    monkeypatch.setattr(init_cli, "data_home", lambda: tmp_path)
    init_cli.cmd_init(SimpleNamespace(force=True, json=True))
    mode = stat.S_IMODE((tmp_path / "longport.json").stat().st_mode)
    assert mode == 0o600


def test_history_summary_cleans_inconsistent_ohlc(monkeypatch, capsys):
    from tracker.cli import history as history_cli

    df = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-01-02", "2026-01-05"]),
            "open": [10.0, 11.0],
            "high": [10.5, 11.5],
            "low": [9.5, 10.5],
            "close": [10.0, 12.0],  # 第二行 close > high, 属脏数据
            "volume": [1.0, 1.0],
        }
    )
    monkeypatch.setattr(history_cli.prices, "get_history", lambda *a, **k: df)
    history_cli.cmd_history(
        SimpleNamespace(
            symbol="AAPL",
            months=1,
            akshare=False,
            ibkr=False,
            longport=False,
            crypto_source="auto",
            json=False,
            rows=5,
        )
    )
    out = capsys.readouterr().out
    assert "共 1 个交易日" in out
    assert "区间高 10.500 / 低 9.500" in out
