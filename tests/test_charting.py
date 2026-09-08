"""K线模块测试: 数据清洗 / 均线 / 重采样 / 图表构建 / 摘要 / OHLC 缓存. 纯离线."""
import json
import sqlite3

import pandas as pd
import plotly.graph_objects as go
import pytest

from tracker import cache as cache_mod
from tracker import charting
from tracker.cache import get_ohlc_cached, set_ohlc_cached


def _trading_dates() -> list[pd.Timestamp]:
    # 3 个交易周: 01-06~10, 01-13~17, 01-20~21 (共 12 个交易日, 中间隔周末)
    days = [pd.Timestamp(2025, 1, d) for d in range(6, 11)]
    days += [pd.Timestamp(2025, 1, d) for d in range(13, 18)]
    days += [pd.Timestamp(2025, 1, 20), pd.Timestamp(2025, 1, 21)]
    return days


def _mk_df(dates=None, base=100.0) -> pd.DataFrame:
    dates = dates if dates is not None else _trading_dates()
    rows = []
    for i, d in enumerate(dates):
        o = base + i
        c = o + (0.5 if i % 2 == 0 else -0.5)
        rows.append(
            {
                "date": d,
                "open": float(o),
                "high": max(o, c) + 0.3,
                "low": min(o, c) - 0.3,
                "close": float(c),
                "volume": 1000.0 + i * 100,
            }
        )
    return pd.DataFrame(rows)


@pytest.fixture(autouse=True)
def _isolate_cache(monkeypatch, tmp_path):
    db = tmp_path / "test_quotes_cache.db"
    monkeypatch.setattr("tracker.cache.CACHE_DB", db)
    cache_mod._ensure_db()


class TestCleanOclc:
    def test_clean_sorted_and_dedup(self):
        df = _mk_df()
        dup = df.iloc[3].copy()
        dup["close"] = dup["close"] + 5.0
        dup["high"] = dup["high"] + 5.0  # 保持蜡烛逻辑合法
        # 基础数据打乱顺序; 重复日期的两条都追加在尾部, 稳定排序后 keep=last 取到 dup
        dirty = pd.concat(
            [df.sample(frac=1.0, random_state=7), df.iloc[[8]], pd.DataFrame([dup])],
            ignore_index=True,
        )
        out = charting.clean_ohlc(dirty)
        assert len(out) == 12
        assert out["date"].is_monotonic_increasing
        # 重复日期保留最后一条 (close 被改成 +5)
        assert out.loc[out["date"] == dup["date"], "close"].iloc[0] == dup["close"]

    def test_clean_drops_invalid_rows(self):
        df = _mk_df()
        bad = pd.DataFrame(
            [
                # NaN close
                {"date": pd.Timestamp(2025, 2, 3), "open": 1, "high": 2, "low": 0.5, "close": None, "volume": 1},
                # high < low
                {"date": pd.Timestamp(2025, 2, 4), "open": 10, "high": 9, "low": 11, "close": 10, "volume": 1},
                # high < max(open, close)
                {"date": pd.Timestamp(2025, 2, 5), "open": 10, "high": 10.5, "low": 9.5, "close": 12, "volume": 1},
                # low > min(open, close)
                {"date": pd.Timestamp(2025, 2, 6), "open": 10, "high": 11, "low": 10.2, "close": 9.5, "volume": 1},
                # close <= 0
                {"date": pd.Timestamp(2025, 2, 7), "open": 1, "high": 2, "low": 0.5, "close": -1, "volume": 1},
            ]
        )
        out = charting.clean_ohlc(pd.concat([df, bad], ignore_index=True))
        assert len(out) == 12
        assert not (out["date"] >= pd.Timestamp(2025, 2, 3)).any()

    def test_clean_empty_and_missing_cols(self):
        assert charting.clean_ohlc(None).empty
        assert charting.clean_ohlc(pd.DataFrame()).empty
        # 缺 volume 列自动补 0
        df = _mk_df().drop(columns=["volume"])
        out = charting.clean_ohlc(df)
        assert (out["volume"] == 0.0).all()


class TestMa:
    def test_compute_ma_values(self):
        df = pd.DataFrame({"close": [float(i) for i in range(1, 11)]})
        mas = charting.compute_ma(df, "3")
        assert list(mas) == [3]
        s = mas[3]
        assert pd.isna(s.iloc[0]) and pd.isna(s.iloc[1])
        assert s.iloc[2] == pytest.approx(2.0)
        assert s.iloc[-1] == pytest.approx(9.0)

    def test_ma_window_too_large_skipped(self):
        df = _mk_df()
        assert charting.compute_ma(df, "50") == {}
        # 12 根刚好可算 MA12
        assert 12 in charting.compute_ma(df, "12")

    def test_parse_ma_periods(self):
        assert charting.parse_ma_periods("5,20,60") == (5, 20, 60)
        assert charting.parse_ma_periods(" 3, abc ,20,20 ") == (3, 20)
        assert charting.parse_ma_periods("0,1") == ()
        assert charting.parse_ma_periods([5, 20]) == (5, 20)
        assert charting.parse_ma_periods("") == ()


class TestResample:
    def test_weekly(self):
        df = _mk_df()
        w = charting.resample_ohlc(df, "weekly")
        assert len(w) == 3
        w1 = w.iloc[0]
        assert w1["date"] == pd.Timestamp(2025, 1, 10)  # W-FRI
        assert w1["open"] == df.iloc[0]["open"]
        assert w1["close"] == df.iloc[4]["close"]
        assert w1["high"] == df.iloc[:5]["high"].max()
        assert w1["low"] == df.iloc[:5]["low"].min()
        assert w1["volume"] == df.iloc[:5]["volume"].sum()

    def test_monthly(self):
        df = _mk_df()
        m = charting.resample_ohlc(df, "monthly")
        assert len(m) == 1
        assert m.iloc[0]["open"] == df.iloc[0]["open"]
        assert m.iloc[0]["close"] == df.iloc[-1]["close"]
        assert m.iloc[0]["volume"] == df["volume"].sum()

    def test_daily_passthrough(self):
        df = _mk_df()
        assert charting.resample_ohlc(df, "daily").equals(df)


class TestBuildFig:
    def test_fig_traces(self):
        df = _mk_df()
        fig = charting.build_candlestick_fig(df, "AAPL", currency="USD", mas="5,10")
        assert isinstance(fig, go.Figure)
        kinds = [t.type for t in fig.data]
        assert kinds.count("candlestick") == 1
        assert kinds.count("bar") == 1
        assert kinds.count("scatter") == 2  # MA5 + MA10

    def test_no_volume(self):
        fig = charting.build_candlestick_fig(_mk_df(), "AAPL", show_volume=False)
        kinds = [t.type for t in fig.data]
        assert "bar" not in kinds
        assert kinds.count("candlestick") == 1

    def test_ma_longer_than_data_skipped(self):
        fig = charting.build_candlestick_fig(_mk_df(), "AAPL", mas="50")
        assert [t.type for t in fig.data] == ["candlestick", "bar"]

    def test_rangebreaks_remove_non_trading_days(self):
        df = _mk_df()
        fig = charting.build_candlestick_fig(df, "AAPL")
        breaks = fig.layout.xaxis.rangebreaks
        assert len(breaks) == 1
        values = [pd.Timestamp(v) for v in breaks[0].values]
        # 12 个交易日, 日期跨度 01-06 → 01-21 共 16 天, 缺 4 个周末日
        assert len(values) == 4
        assert pd.Timestamp(2025, 1, 11) in values
        assert pd.Timestamp(2025, 1, 19) in values

    def test_rangeselector_buttons(self):
        fig = charting.build_candlestick_fig(_mk_df(), "AAPL")
        labels = [b.label for b in fig.layout.xaxis.rangeselector.buttons]
        assert "全部" in labels and "1年" in labels

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            charting.build_candlestick_fig(pd.DataFrame(), "AAPL")

    def test_fig_to_html_embeds_plotly(self):
        fig = charting.build_candlestick_fig(_mk_df(), "AAPL")
        html = charting.fig_to_html(fig)
        assert "plotly" in html and "AAPL" in html and "<html>" in html


class TestSummarize:
    def test_summary_values(self):
        df = _mk_df()
        s = charting.summarize_ohlc(df, "5,50")
        assert s["bars"] == 12
        assert s["first_date"] == "2025-01-06"
        assert s["last_date"] == "2025-01-21"
        expected = (df.iloc[-1]["close"] / df.iloc[0]["close"] - 1) * 100
        assert s["change_pct"] == pytest.approx(expected, abs=1e-3)
        assert s["period_high"] == pytest.approx(df["high"].max())
        assert s["period_low"] == pytest.approx(df["low"].min())
        assert s["last_volume"] == pytest.approx(df.iloc[-1]["volume"])
        assert set(s["ma"]) == {"ma5"}  # ma50 窗口过大被跳过
        assert s["ma"]["ma5"] == pytest.approx(df["close"].tail(5).mean())

    def test_summary_empty(self):
        assert charting.summarize_ohlc(pd.DataFrame()) == {"bars": 0}


class TestOhlcCache:
    def test_roundtrip(self):
        df = _mk_df()
        set_ohlc_cached("AAPL", 12, df)
        out = get_ohlc_cached("AAPL", 12)
        assert out is not None
        pd.testing.assert_frame_equal(out, df)

    def test_ttl_expiry(self):
        set_ohlc_cached("AAPL", 12, _mk_df())
        assert get_ohlc_cached("AAPL", 12, ttl=60) is not None
        assert get_ohlc_cached("AAPL", 12, ttl=-1) is None

    def test_months_is_separate_key(self):
        set_ohlc_cached("AAPL", 12, _mk_df())
        assert get_ohlc_cached("AAPL", 6) is None

    def test_overwrite(self):
        set_ohlc_cached("AAPL", 12, _mk_df(base=100.0))
        set_ohlc_cached("AAPL", 12, _mk_df(base=200.0))
        out = get_ohlc_cached("AAPL", 12)
        assert out.iloc[0]["open"] == pytest.approx(200.0)

    def test_corrupted_payload_returns_none(self):
        with sqlite3.connect(cache_mod.CACHE_DB) as con:
            con.execute(
                "INSERT INTO ohlc_cache (symbol, months, payload, fetched_at) VALUES (?, ?, ?, ?)",
                ("BAD", 12, "{not json", 9e9),
            )
            con.commit()
        assert get_ohlc_cached("BAD", 12) is None

    def test_empty_not_cached(self):
        set_ohlc_cached("EMPTY", 12, pd.DataFrame())
        assert get_ohlc_cached("EMPTY", 12) is None

    def test_clear_removes_ohlc(self):
        set_ohlc_cached("AAPL", 12, _mk_df())
        assert cache_mod.clear() >= 1
        assert get_ohlc_cached("AAPL", 12) is None
        assert cache_mod.info()["ohlc_total"] == 0


class TestGetOhlc:
    def test_clean_and_cache(self, monkeypatch):
        from tracker import prices

        calls = {"n": 0}
        raw = pd.concat([_mk_df(), _mk_df(base=300.0).iloc[[0]]], ignore_index=True)
        raw.loc[len(raw) - 1, "date"] = pd.Timestamp(2025, 2, 10)  # 合法新行
        raw_broken = raw.copy()
        raw_broken.loc[0, "high"] = 1.0  # high < body → 应被清洗掉

        def fake_history(symbol, months=12, prefer_akshare=False, use_ibkr=False):
            calls["n"] += 1
            return raw_broken

        monkeypatch.setattr(prices, "get_history", fake_history)
        df1 = prices.get_ohlc("AAPL", months=12)
        assert len(df1) == 12  # 12 根有效 + 1 根坏行被清洗
        assert calls["n"] == 1
        df2 = prices.get_ohlc("AAPL", months=12)
        assert calls["n"] == 1  # 第二次命中磁盘缓存, 不再请求
        pd.testing.assert_frame_equal(df1, df2)

    def test_refresh_bypasses_cache(self, monkeypatch):
        from tracker import prices

        calls = {"n": 0}

        def fake_history(symbol, months=12, prefer_akshare=False, use_ibkr=False):
            calls["n"] += 1
            return _mk_df(base=100.0 + calls["n"])

        monkeypatch.setattr(prices, "get_history", fake_history)
        prices.get_ohlc("AAPL", months=12)
        prices.get_ohlc("AAPL", months=12, refresh=True)
        assert calls["n"] == 2

    def test_invalid_symbol_raises(self):
        from tracker import prices

        with pytest.raises(ValueError):
            prices.get_ohlc("BAD.ZZ")


class TestKlineCli:
    @pytest.fixture(autouse=True)
    def _mock_ohlc(self, monkeypatch):
        from tracker import prices

        self.df = _mk_df()
        monkeypatch.setattr(prices, "get_ohlc", lambda *a, **kw: self.df.copy())
        # CLI 的 --open 等不应触发网络/浏览器
        monkeypatch.setattr("webbrowser.open", lambda *a, **kw: True)

    def _run(self, capsys, *argv):
        from tracker import cli

        cli.main(list(argv))
        return capsys.readouterr().out

    def test_kline_generates_html(self, tmp_path, capsys):
        out_f = tmp_path / "k.html"
        out = self._run(capsys, "kline", "AAPL", "-o", str(out_f), "--ma", "5,20")
        assert "K线 AAPL" in out
        assert "区间最高" in out
        assert out_f.exists()
        html = out_f.read_text(encoding="utf-8")
        assert "AAPL" in html and "plotly" in html

    def test_kline_json(self, capsys):
        out = self._run(capsys, "kline", "AAPL", "--json")
        data = json.loads(out)
        assert data["symbol"] == "AAPL"
        assert data["currency"] == "USD"
        assert data["bars"] == 12
        assert len(data["data"]) == 12
        assert data["data"][-1]["date"] == "2025-01-21"
        assert "ma5" in data["summary"]["ma"]

    def test_kline_weekly_json(self, capsys):
        out = self._run(capsys, "kline", "AAPL", "--period", "weekly", "--json")
        data = json.loads(out)
        assert data["period"] == "weekly"
        assert data["bars"] == 3

    def test_kline_default_output_path(self, capsys, monkeypatch, tmp_path):
        import tracker.cli as cli_mod

        monkeypatch.setattr(cli_mod, "DATA_DIR", tmp_path / "data")
        out = self._run(capsys, "kline", "600519.SS")
        assert "K线图已生成" in out
        assert (tmp_path / "data" / "kline_600519_SS.html").exists()
