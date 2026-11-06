"""2026-11 代码审查修复的回归测试 (数据/成本/缓存/图表/CLI)."""

from __future__ import annotations

import json
from datetime import date

import pandas as pd
import pytest

from tracker import cache as cache_mod
from tracker import charting, importer, nav, storage
from tracker.ashare_sync import _to_float
from tracker.providers.base import Quote
from tracker.providers.orchestration import _cached_source_ok
from tracker.symbols import parse, type_for_symbol

# ---------- 存储 / 导入 ----------


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
