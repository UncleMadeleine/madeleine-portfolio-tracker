"""估值指标: 快照归一 / 历史序列清洗 / 摘要 / 百度接口映射 (纯逻辑, 不联网)."""

from __future__ import annotations

import pandas as pd
import pytest

import tracker.ah_premium as ah
import tracker.valuation as valuation


def _recent_days(n: int = 30) -> pd.DatetimeIndex:
    end = pd.Timestamp.today().normalize()
    return pd.date_range(end=end, periods=n, freq="D")


class TestSnapshot:
    def test_normalizes_metric_units_and_negative_values(self):
        data = {
            "pe_ratio": 8.4,
            "forward_pe": -2.0,
            "price_to_book": 0.0,
            "dividend_yield": 3.68,  # yfinance 已是百分数
            "dividend_yield_5y_avg": 0.0529,  # yahoo_data 统一输出小数比例
            "payout_ratio": 0.2955,
            "eps_ttm": 1.05,
            "book_value": 11.2,
            "market_cap": 3.1e12,
        }
        snap = valuation._snapshot_from_metrics("601398.SS", data, fetched_at=1.0)
        assert snap.symbol == "601398.SS"
        assert snap.pe_ttm == pytest.approx(8.4)
        assert snap.forward_pe is None  # 负 PE 无意义
        assert snap.pb is None  # 0/负 PB 无意义
        assert snap.dividend_yield_pct == pytest.approx(3.68)
        assert snap.dividend_yield_5y_avg_pct == pytest.approx(5.29)
        assert snap.payout_ratio_pct == pytest.approx(29.55)
        assert snap.book_value == pytest.approx(11.2)
        assert snap.market_cap == pytest.approx(3.1e12)

    def test_5y_avg_accepts_already_percent(self):
        # 历史缓存/其它源可能直接给 5.29; 不能再 ×100
        snap = valuation._snapshot_from_metrics(
            "AAPL", {"dividend_yield_5y_avg": 5.29}
        )
        assert snap.dividend_yield_5y_avg_pct == pytest.approx(5.29)

    def test_payout_ratio_over_100_percent_is_not_scaled_down(self):
        # PFE 等公司的 payout ratio 可 >1 (亏损/一次性项目); 比例→百分数
        # 必须始终 ×100, 不能套用「≤1 才乘」的启发式。
        snap = valuation._snapshot_from_metrics(
            "PFE", {"payout_ratio": 2.4571}
        )
        assert snap.payout_ratio_pct == pytest.approx(245.71)

    def test_snapshot_cache_and_refresh(self, monkeypatch):
        calls = []

        def fake_fetch(symbol):
            calls.append(symbol)
            return {"pe_ratio": 10.0}

        monkeypatch.setattr(valuation, "_fetch_metrics_row", fake_fetch)
        valuation.clear_cache()
        a = valuation.fetch_snapshot("601398.SS")
        b = valuation.fetch_snapshot("601398.SS")
        assert a.pe_ttm == b.pe_ttm == 10.0
        assert calls == ["601398.SS"]
        valuation.fetch_snapshot("601398.SS", refresh=True)
        assert calls == ["601398.SS", "601398.SS"]

    def test_snapshot_disk_cache_survives_memory_clear(self, monkeypatch):
        calls = []

        def fake_fetch(symbol):
            calls.append(symbol)
            return {"pe_ratio": 9.0}

        monkeypatch.setattr(valuation, "_fetch_metrics_row", fake_fetch)
        valuation.clear_cache()
        a = valuation.fetch_snapshot("AAPL")
        # 模拟进程重启: 只清内存, 磁盘 6h 快照仍应命中
        valuation.clear_cache()
        b = valuation.fetch_snapshot("AAPL")
        assert a.pe_ttm == b.pe_ttm == 9.0
        assert calls == ["AAPL"]
        # 显式 refresh 必须穿透磁盘快照
        valuation.fetch_snapshot("AAPL", refresh=True)
        assert calls == ["AAPL", "AAPL"]

    def test_snapshot_falls_back_to_eodhd(self, monkeypatch):
        import tracker.eodhd as eodhd

        def boom(symbol):
            raise RuntimeError("yfinance unavailable")

        monkeypatch.setattr(valuation, "_fetch_metrics_row", boom)
        monkeypatch.setattr(eodhd, "available", lambda cfg=None: True)
        monkeypatch.setattr(
            eodhd,
            "fundamentals",
            lambda symbol, path=None: {
                "pe_ratio": 33.15,
                "forward_pe": 30.77,
                "price_to_book": 60.72,
                "dividend_yield": 0.0034,
                "payout_ratio": 0.127,
                "eps_ttm": 6.6,
                "book_value": 7.36,
                "market_cap": 5.0e12,
                "currency": "USD",
            },
        )
        valuation.clear_cache()
        snap = valuation.fetch_snapshot("AAPL")
        assert snap.source == "eodhd"
        assert snap.pe_ttm == pytest.approx(33.15)
        assert snap.pb == pytest.approx(60.72)
        assert snap.dividend_yield_pct == pytest.approx(0.34)
        assert snap.payout_ratio_pct == pytest.approx(12.7)

    def test_snapshot_does_not_call_eodhd_when_yfinance_works(self, monkeypatch):
        import tracker.eodhd as eodhd

        monkeypatch.setattr(
            valuation, "_fetch_metrics_row", lambda symbol: {"pe_ratio": 10.0}
        )
        monkeypatch.setattr(
            eodhd,
            "fundamentals",
            lambda *a, **kw: pytest.fail("EODHD should not be called"),
        )
        valuation.clear_cache()
        assert valuation.fetch_snapshot("AAPL").pe_ttm == 10.0

    def test_snapshot_enriched_with_cached_dynamic_pe(self, monkeypatch):
        from tracker.prices import Quote

        monkeypatch.setattr(
            valuation, "_fetch_metrics_row", lambda symbol: {"pe_ratio": 10.0}
        )
        monkeypatch.setattr(
            valuation.cache_mod,
            "get_cached",
            lambda symbols, ttl=300: {
                "AAPL": Quote(
                    symbol="AAPL",
                    name="Apple",
                    price=100.0,
                    prev_close=99.0,
                    change_pct=1.0,
                    currency="USD",
                    pe_dynamic=9.5,
                )
            },
        )
        valuation.clear_cache()
        snap = valuation.fetch_snapshot("AAPL")
        assert snap.pe_ttm == pytest.approx(10.0)
        assert snap.pe_dynamic == pytest.approx(9.5)

    def test_snapshot_falls_back_to_akshare_for_cn(self, monkeypatch):
        import tracker.eodhd as eodhd
        from tracker.prices import Quote

        def boom(symbol):
            raise RuntimeError("yfinance unavailable")

        class FakeQuote(Quote):
            pass

        monkeypatch.setattr(valuation, "_fetch_metrics_row", boom)
        monkeypatch.setattr(eodhd, "available", lambda cfg=None: False)
        monkeypatch.setattr(
            "tracker.prices.get_quotes",
            lambda symbols, **kw: (
                {
                    "600519.SS": FakeQuote(
                        symbol="600519.SS",
                        name="贵州茅台",
                        price=100.0,
                        prev_close=99.0,
                        change_pct=1.0,
                        currency="CNY",
                        book_value=50.0,
                        market_cap=1.0e12,
                        pe_dynamic=21.24,
                        source="akshare_em",
                    )
                },
                {},
                [],
            ),
        )
        valuation.clear_cache()
        snap = valuation.fetch_snapshot("600519.SS")
        assert snap.source == "akshare_em"
        assert snap.pe_ttm is None
        assert snap.pe_dynamic == pytest.approx(21.24)
        assert snap.pb == pytest.approx(2.0)
        assert snap.market_cap == pytest.approx(1.0e12)

    def test_snapshot_keeps_sina_fallback_source(self, monkeypatch):
        """东财不可达时估值兜底实际命中新浪, source 不能仍写死 akshare."""
        import tracker.eodhd as eodhd
        from tracker.providers.base import Quote

        def boom(symbol):
            raise RuntimeError("yfinance unavailable")

        monkeypatch.setattr(valuation, "_fetch_metrics_row", boom)
        monkeypatch.setattr(eodhd, "available", lambda cfg=None: False)
        monkeypatch.setattr(
            "tracker.prices.get_quotes",
            lambda symbols, **kw: (
                {
                    "600519.SS": Quote(
                        symbol="600519.SS",
                        name="贵州茅台",
                        price=100.0,
                        prev_close=99.0,
                        change_pct=1.0,
                        currency="CNY",
                        source="akshare_sina",
                    )
                },
                {},
                [],
            ),
        )
        valuation.clear_cache()
        snap = valuation.fetch_snapshot("600519.SS")
        assert snap.source == "akshare_sina"
        assert snap.pb is None
        assert snap.market_cap is None

    def test_crypto_rejected(self):
        with pytest.raises(ValueError, match="估值指标"):
            valuation.fetch_snapshot("BTC-USD")


class TestHistory:
    def test_clean_filters_non_positive_and_cuts_range(self):
        dates = _recent_days(40)
        df = pd.DataFrame(
            {
                "date": dates.strftime("%Y-%m-%d"),
                "value": [10.0, -1.0, 0.0, 9.5] + [8.0] * 36,
            }
        )
        out = valuation._clean_history(df, months=1)
        assert not out.empty
        assert (out["value"] > 0).all()
        assert out["date"].min() >= pd.Timestamp.today().normalize() - pd.Timedelta(
            days=32
        )

    def test_clean_drops_bad_rows_and_duplicates(self):
        today = pd.Timestamp.today().normalize()
        df = pd.DataFrame(
            {
                "date": [today, today, "bad", today - pd.Timedelta(days=1)],
                "value": [8.0, 8.2, 9.0, None],
            }
        )
        out = valuation._clean_history(df, months=12)
        assert len(out) == 1
        assert out.iloc[0]["value"] == pytest.approx(8.2)

    def test_baidu_request_mapping(self):
        fn, code, indicator, period = valuation._baidu_request(
            "601398.SS", valuation.METRIC_PE, 24
        )
        assert (fn, code, indicator, period) == (
            "stock_zh_valuation_baidu",
            "601398",
            "市盈率(TTM)",
            "近三年",
        )
        fn, code, _, period = valuation._baidu_request(
            "1398.HK", valuation.METRIC_PB, 60
        )
        assert (fn, code, period) == ("stock_hk_valuation_baidu", "01398", "全部")
        fn, code, _, period = valuation._baidu_request(
            "AAPL", valuation.METRIC_PE, 12
        )
        assert (fn, code, period) == ("stock_us_valuation_baidu", "AAPL", "近一年")

    def test_fetch_history_uses_akshare_and_cache(self, monkeypatch):
        calls = []

        class FakeAk:
            def stock_zh_valuation_baidu(self, symbol, indicator, period):
                calls.append((symbol, indicator, period))
                return pd.DataFrame(
                    {
                        "date": _recent_days(10).strftime("%Y-%m-%d"),
                        "value": [8.0 + i * 0.1 for i in range(10)],
                    }
                )

        monkeypatch.setattr(ah, "_ak", lambda: FakeAk())
        first = valuation.fetch_history(
            "601398.SS", valuation.METRIC_PE, months=12
        )
        second = valuation.fetch_history(
            "601398.SS", valuation.METRIC_PE, months=12
        )
        assert len(first) == 10
        assert first.equals(second)
        assert calls == [("601398", "市盈率(TTM)", "近一年")]

    def test_fetch_history_empty_source_raises(self, monkeypatch):
        class FakeAk:
            def stock_hk_valuation_baidu(self, symbol, indicator, period):
                return pd.DataFrame()

        monkeypatch.setattr(ah, "_ak", lambda: FakeAk())
        with pytest.raises(RuntimeError, match="历史数据"):
            valuation.fetch_history("1398.HK", valuation.METRIC_PE, months=12)

    def test_history_supported(self):
        assert valuation.history_supported("601398.SS")
        assert valuation.history_supported("1398.HK")
        assert valuation.history_supported("AAPL")
        assert not valuation.history_supported("SAP.DE")


class TestSummary:
    def test_summary_values(self):
        df = pd.DataFrame(
            {
                "date": pd.to_datetime(
                    ["2024-01-02", "2024-01-03", "2024-01-04"]
                ),
                "value": [10.0, 12.0, 8.0],
            }
        )
        s = valuation.summarize_history(df)
        assert s["bars"] == 3
        assert s["latest"] == pytest.approx(8.0)
        assert s["mean"] == pytest.approx(10.0)
        assert s["median"] == pytest.approx(10.0)
        assert s["high"] == pytest.approx(12.0)
        assert s["high_date"] == "2024-01-03"
        assert s["low"] == pytest.approx(8.0)
        assert s["percentile"] == pytest.approx(100.0 / 3)

    def test_summary_empty(self):
        assert valuation.summarize_history(pd.DataFrame()) == {}
