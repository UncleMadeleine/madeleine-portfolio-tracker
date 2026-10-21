"""历史市值序列 (mcap) 测试: 纯函数 + mock 网络层, 全程离线."""

import pandas as pd
import pytest

from tracker import mcap


@pytest.fixture(autouse=True)
def _isolate_cache(monkeypatch, tmp_path):
    """序列缓存落临时 DB, 不污染真实缓存."""
    monkeypatch.setattr("tracker.cache.CACHE_DB", tmp_path / "mcap_cache.db")
    import tracker.cache as cache_mod

    cache_mod._ensure_db()


def price_df(dates, closes):
    d = pd.to_datetime(dates)
    o = pd.Series(closes, index=range(len(closes)))
    frame = pd.DataFrame({"date": d, "open": o, "high": o, "low": o, "close": o})
    frame["volume"] = 1.0
    return frame.reset_index(drop=True)[
        ["date", "open", "high", "low", "close", "volume"]
    ]


PRICE = price_df(
    ["2025-01-06", "2025-01-07", "2025-01-08", "2025-01-09", "2025-01-10"],
    [10.0, 11.0, 12.0, 13.0, 14.0],
)


class TestApproxFrame:
    def test_scaled_to_snapshot(self):
        out = mcap.approx_frame(1.44e12, PRICE)
        vals = out["close"].tolist()
        assert abs(vals[0] - 1.44e12 * 10 / 14) < 1
        assert abs(vals[-1] - 1.44e12) < 1

    def test_only_valid_prices(self):
        df = PRICE.copy()
        df.loc[2, "close"] = float("nan")
        out = mcap.approx_frame(1.44e12, df)
        assert len(out) == 4  # NaN 收盘行剔除


class TestOhlcvShim:
    def test_kline_isomorphism(self):
        caps = pd.DataFrame(
            {
                "date": pd.to_datetime(["2025-01-06", "2025-01-07"]),
                "close": [100.0, 110.0],
            }
        )
        shim = mcap.ohlcv_shim(caps)
        assert list(shim.columns) == [
            "date",
            "open",
            "high",
            "low",
            "close",
            "volume",
        ]
        assert (shim["open"] == shim["close"]).all()
        assert (shim["high"] == shim["low"]).all()


class TestMapByClosestDate:
    def test_exact_dates_only_no_fabrication(self):
        # em 只覆盖部分交易日: 命中日用真值, 缺失日丢弃 (不前向填充虚构)
        pairs = pd.Series(
            [100.0, 110.0, 120.0],
            index=pd.to_datetime(["2025-01-06", "2025-01-07", "2025-01-08"]),
        )
        out = mcap._map_by_closest_date(PRICE, pairs)
        assert out["close"].tolist() == [100.0, 110.0, 120.0]

    def test_dates_outside_price_span_dropped(self):
        pairs = pd.Series(
            [99.0, 300.0],
            index=pd.to_datetime(["2025-01-05", "2025-01-10"]),
        )
        out = mcap._map_by_closest_date(PRICE, pairs)
        assert out["close"].tolist() == [300.0]


class TestHistoryFor:
    def test_crypto_skipped(self):
        res = mcap.history_for("BTC-USD", PRICE)
        assert res.df is None
        assert res.reason == "加密货币无总市值语义"

    def test_index_skipped(self):
        res = mcap.history_for("IX.DXY", PRICE)
        assert res.df is None
        assert res.reason == "指数无总市值语义"

    def test_empty_price_skipped(self):
        res = mcap.history_for("AAPL", pd.DataFrame())
        assert res.df is None

    def test_cn_em_path(self, monkeypatch):
        em = pd.DataFrame(
            {
                "date": pd.to_datetime(
                    ["2025-01-06", "2025-01-07", "2025-01-08"]
                ),
                "close": [100.0, 110.0, 120.0],
            }
        )
        monkeypatch.setattr(mcap, "_em_history", lambda bare: em)
        res = mcap.history_for("600519.SS", PRICE)
        assert res.source == "em"
        assert res.currency == "CNY"
        assert res.df["close"].iloc[0] == 100.0

    def test_global_shares_path(self, monkeypatch):
        shares = pd.DataFrame(
            {"date": pd.to_datetime(["2025-01-07", "2025-01-09"]), "close": [7.0, 7.0]}
        )
        monkeypatch.setattr(mcap, "_shares_history", lambda y: shares)
        res = mcap.history_for("AAPL", PRICE)
        assert res.source == "shares"
        # 股本是稀疏事件序列: 事件间前向填充 (01-07 事件股数 7 一直有效);
        # 首个事件之前 (01-06) 无已知股数 → 丢弃
        assert res.df["close"].tolist() == [77.0, 84.0, 91.0, 98.0]

    def test_global_shares_ffill_on_buybacks(self, monkeypatch):
        # 回购事件: 股数下降, 前向填充逐段反映
        shares = pd.DataFrame(
            {
                "date": pd.to_datetime(["2025-01-06", "2025-01-08"]),
                "close": [10.0, 8.0],
            }
        )
        monkeypatch.setattr(mcap, "_shares_history", lambda y: shares)
        res = mcap.history_for("AAPL", PRICE)
        assert res.df["close"].tolist() == [100.0, 110.0, 96.0, 104.0, 112.0]

    def test_approx_fallback(self, monkeypatch):
        from tracker import fundamentals

        monkeypatch.setattr(mcap, "_em_history", lambda bare: None)
        monkeypatch.setattr(
            fundamentals, "fetch_market_caps", lambda syms: {s: 1.44e12 for s in syms}
        )
        res = mcap.history_for("600519.SS", PRICE)
        assert res.source == "approx"
        assert abs(res.df["close"].iloc[-1] - 1.44e12) < 1

    def test_no_data_anywhere(self, monkeypatch):
        from tracker import fundamentals

        monkeypatch.setattr(mcap, "_em_history", lambda bare: None)
        monkeypatch.setattr(
            fundamentals, "fetch_market_caps", lambda syms: {s: None for s in syms}
        )
        res = mcap.history_for("600519.SS", PRICE)
        assert res.df is None
        assert res.reason == "无市值数据"


class TestToCommonCurrency:
    def test_same_currency_no_fx(self, monkeypatch):
        frames = {"600519.SS": PRICE.assign(close=PRICE["close"] * 1e11)}
        out, warns = mcap.to_common_currency(frames, {"600519.SS": "CNY"}, "CNY")
        assert not warns
        assert out["600519.SS"]["close"].iloc[0] == 10.0 * 1e11

    def test_fx_conversion_per_day(self, monkeypatch):
        rates = pd.DataFrame(
            {
                "date": pd.to_datetime(
                    [
                        "2025-01-05",
                        "2025-01-06",
                        "2025-01-07",
                        "2025-01-08",
                        "2025-01-09",
                        "2025-01-10",
                    ]
                ),
                "close": [0.9, 0.9, 0.9, 0.9, 0.8, 0.8],
            }
        )
        import tracker.fx as fx_mod

        monkeypatch.setattr(fx_mod, "get_fx_history", lambda *a, **k: rates)
        frames = {"AAPL": PRICE}
        out, warns = mcap.to_common_currency(frames, {"AAPL": "USD"}, "HKD")
        assert not warns
        vals = out["AAPL"]["close"].tolist()
        # 逐日折算: 10×0.9, 11×0.9, 12×0.9, 13×0.8, 14×0.8
        assert vals == pytest.approx([9.0, 9.9, 10.8, 10.4, 11.2])

    def test_fx_missing_keeps_original(self, monkeypatch):
        import tracker.fx as fx_mod

        monkeypatch.setattr(fx_mod, "get_fx_history", lambda *a, **k: None)
        frames = {"AAPL": PRICE}
        out, warns = mcap.to_common_currency(frames, {"AAPL": "USD"}, "HKD")
        assert len(warns) == 1
        assert out["AAPL"]["close"].tolist() == PRICE["close"].tolist()


class TestDescribeAmount:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (1.637219e12, "1.64万亿"),
            (4.31e12, "4.31万亿"),
            (8e9, "80.00亿"),
            (None, "—"),
            (0.0, "—"),
        ],
    )
    def test_format(self, value, expected):
        assert mcap.describe_amount(value) == expected


class TestCompareMarketCaps:
    def test_warnings_for_crypto_mixed_in(self, monkeypatch):
        em = pd.DataFrame(
            {
                "date": pd.to_datetime(["2025-01-06", "2025-01-07", "2025-01-08"]),
                "close": [100.0, 110.0, 120.0],
            }
        )
        monkeypatch.setattr(mcap, "_em_history", lambda bare: em)
        frames = {"600519.SS": PRICE, "BTC-USD": PRICE}
        caps, warns, meta = mcap.compare_market_caps(frames, "CNY")
        assert set(caps) == {"600519.SS"}
        assert any("BTC-USD" in w for w in warns)
        assert meta["600519.SS"]["source"] == "em"
