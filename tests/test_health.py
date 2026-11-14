"""数据健康模块: 事件记录 / 聚合 / 缓存覆盖 / 完整性."""

from __future__ import annotations

import time

import pandas as pd
import pytest

from tracker import cache as cache_mod
from tracker import health, storage
from tracker.providers.base import Quote


@pytest.fixture(autouse=True)
def _clean_health(monkeypatch):
    monkeypatch.delenv(health.DISABLE_ENV, raising=False)
    health.flush()
    cache_mod.clear_health_events()
    yield
    health.flush()


def _add(rows):
    """rows: (ts_offset_seconds, source, ok, latency_ms, symbol, error)."""
    now = time.time()
    cache_mod.add_health_events(
        [
            (
                now + offset,
                "quote",
                source,
                1 if ok else 0,
                latency,
                symbol,
                error,
            )
            for offset, source, ok, latency, symbol, error in rows
        ]
    )


def test_summary_aggregates_and_degrades():
    now = time.time()
    _add(
        [
            (-10, "yfinance", True, 100.0, "AAPL", None),
            (-20, "yfinance", True, 300.0, "MSFT", None),
            (-30, "yfinance", False, 50.0, "BAD", "HTTP 429"),
        ]
    )
    summary = health.summary(24, now=now)
    row = next(
        r for r in summary["sources"]
        if r["source"] == "yfinance" and r["kind"] == "quote"
    )
    assert row["attempts"] == 3
    assert row["ok"] == 2
    assert row["fail"] == 1
    assert row["success_rate"] == pytest.approx(2 / 3)
    assert row["p50_ms"] == pytest.approx(200.0)
    assert row["status"] == "degraded"
    assert summary["overall"] == "degraded"
    assert summary["recent_failures"][0]["error"] == "HTTP 429"


def test_summary_down_when_all_fail():
    now = time.time()
    _add([(-i, "akshare", False, 10.0, "X", "boom") for i in range(1, 4)])
    summary = health.summary(24, now=now)
    row = next(r for r in summary["sources"] if r["source"] == "akshare")
    assert row["status"] == "down"
    assert summary["overall"] == "down"


def test_summary_ignores_events_outside_window():
    now = time.time()
    _add([(-2 * 86400, "yfinance", True, 10.0, "AAPL", None)])
    summary = health.summary(1, now=now)
    row = next(
        r for r in summary["sources"]
        if r["source"] == "yfinance" and r["kind"] == "quote"
    )
    assert row["attempts"] == 0
    assert row["status"] == "idle"
    assert summary["overall"] == "unknown"


def test_span_records_failure_and_reraises():
    with pytest.raises(ValueError, match="boom"):
        with health.span("quote", "akshare", "600519.SS"):
            raise ValueError("boom")
    health.flush()
    events = cache_mod.get_health_events(0)
    assert len(events) == 1
    assert events[0]["source"] == "akshare"
    assert events[0]["ok"] is False
    assert "boom" in events[0]["error"]


def test_span_can_mark_empty_result_as_failure():
    with health.span("history", "eodhd", "AAPL") as sp:
        sp.ok = False
        sp.error = "空结果"
    health.flush()
    events = cache_mod.get_health_events(0)
    assert events[0]["ok"] is False
    assert events[0]["error"] == "空结果"


def test_disabled_env_skips_recording(monkeypatch):
    monkeypatch.setenv(health.DISABLE_ENV, "1")
    health.record("quote", "yfinance", True)
    with health.span("quote", "yfinance", "AAPL") as sp:
        sp.ok = False
    health.flush()
    assert cache_mod.get_health_events(0) == []


def test_coverage_reports_holding_gap_but_not_empty_watchlist():
    storage.save_portfolio(
        {
            "base_currency": "CNY",
            "holdings": [{"symbol": "0700.HK", "quantity": 10, "avg_cost": 300}],
        },
        storage.PORTFOLIO_PATH,
    )
    storage.save_watchlist(
        {"watchlist": [{"symbol": f"000{i}.HK"} for i in range(1, 6)]},
        storage.WATCHLIST_PATH,
    )
    cov = health.coverage()
    assert cov["symbols"]["holdings"] == 1
    assert cov["symbols"]["watchlist"] == 5
    assert [g["symbol"] for g in cov["gaps"]] == ["0700.HK"]
    assert any("无行情缓存" in r for r in cov["gaps"][0]["reasons"])


def test_coverage_has_no_gap_when_quote_and_ohlc_cached():
    storage.save_portfolio(
        {
            "base_currency": "CNY",
            "holdings": [{"symbol": "0700.HK", "quantity": 10, "avg_cost": 300}],
        },
        storage.PORTFOLIO_PATH,
    )
    cache_mod.set_cached(
        {
            "0700.HK": Quote(
                symbol="0700.HK",
                name="腾讯控股",
                price=400.0,
                prev_close=395.0,
                change_pct=1.2,
                currency="HKD",
                source="yfinance",
            )
        }
    )
    cache_mod.set_ohlc_cached(
        "0700.HK",
        12,
        pd.DataFrame(
            {
                "date": pd.to_datetime(["2024-01-02", "2024-01-03"]),
                "open": [1.0, 1.0],
                "high": [1.0, 1.0],
                "low": [1.0, 1.0],
                "close": [1.0, 1.0],
            }
        ),
    )
    cov = health.coverage()
    assert cov["gaps"] == []
    assert cov["symbols"]["fresh"] == 1


def test_coverage_reports_missing_fx():
    storage.save_portfolio(
        {
            "base_currency": "USD",
            "holdings": [{"symbol": "0700.HK", "quantity": 10, "avg_cost": 300}],
        },
        storage.PORTFOLIO_PATH,
    )
    cov = health.coverage()
    assert cov["fx"]["base"] == "USD"
    assert "HKD" in cov["fx"]["missing"]


def test_coverage_ignores_non_currency_index_units():
    """指数币种是「点/%」占位符, 不应算成缺失汇率."""
    storage.save_portfolio(
        {
            "base_currency": "USD",
            "holdings": [{"symbol": "IX.DXY", "quantity": 1, "avg_cost": 100}],
        },
        storage.PORTFOLIO_PATH,
    )
    cov = health.coverage()
    assert cov["fx"]["needed"] == 0
    assert cov["fx"]["missing"] == []


def test_integrity_ok():
    storage.save_portfolio({"base_currency": "CNY", "holdings": []}, storage.PORTFOLIO_PATH)
    result = health.integrity()
    assert result["ok"] is True
    assert any(c["name"] == "缓存数据库" and c["status"] == "ok"
               for c in result["checks"])


def test_report_overall_degrades_on_coverage_gap():
    storage.save_portfolio(
        {
            "base_currency": "CNY",
            "holdings": [{"symbol": "0700.HK", "quantity": 10, "avg_cost": 300}],
        },
        storage.PORTFOLIO_PATH,
    )
    report = health.report(24)
    assert report["overall"] == "degraded"
    assert report["coverage"]["gaps"]


def test_report_is_json_serializable():
    import json

    storage.save_portfolio(
        {"base_currency": "CNY", "holdings": []}, storage.PORTFOLIO_PATH
    )
    report = health.report(24)
    # allow_nan=False: 页面/CLI --json 与 Agent 消费都要求严格 JSON
    text = json.dumps(report, ensure_ascii=False, allow_nan=False)
    assert '"overall"' in text


def test_health_events_prune_by_age_and_cap():
    now = time.time()
    cache_mod.add_health_events(
        [
            (now - 8 * 86400, "quote", "yfinance", 1, 1.0, None, None),
            (now - 60, "quote", "akshare", 1, 1.0, None, None),
        ]
    )
    removed = cache_mod.prune_health_events(now - 7 * 86400, max_rows=20000)
    assert removed == 1
    events = cache_mod.get_health_events(0)
    assert [e["source"] for e in events] == ["akshare"]

    cache_mod.add_health_events(
        [(now - i, "quote", "yfinance", 1, 1.0, None, None) for i in range(5)]
    )
    cache_mod.prune_health_events(0, max_rows=2)
    assert len(cache_mod.get_health_events(0)) == 2


def test_source_distribution_reads_cache():
    cache_mod.set_cached(
        {
            "AAPL": Quote("AAPL", "Apple", 1.0, None, None, "USD", source="yfinance"),
            "MSFT": Quote("MSFT", "Microsoft", 2.0, None, None, "USD", source="yfinance"),
            "0700.HK": Quote("0700.HK", "腾讯", 3.0, None, None, "HKD",
                             source="akshare"),
        }
    )
    dist = cache_mod.source_distribution()
    assert dist["yfinance"]["total"] == 2
    assert dist["akshare"]["total"] == 1
