"""K线模块测试: 数据清洗 / 均线 / 重采样 / 图表构建 / 摘要 / OHLC 缓存. 纯离线."""
import json
import sqlite3

import pandas as pd
from pandas.tseries.frequencies import to_offset
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

    def test_clean_drops_non_positive_prices(self):
        """任一 OHLC 价格 <=0 都是无效行情, 会污染区间高低点与绘图."""
        bad = pd.DataFrame(
            [
                # low = 0 (close 正常)
                {"date": pd.Timestamp(2025, 3, 3), "open": 10, "high": 12, "low": 0, "close": 11, "volume": 1},
                # open = 0
                {"date": pd.Timestamp(2025, 3, 4), "open": 0, "high": 12, "low": 1, "close": 11, "volume": 1},
                # high = 0
                {"date": pd.Timestamp(2025, 3, 5), "open": 1, "high": 0, "low": 0, "close": 1, "volume": 1},
            ]
        )
        out = charting.clean_ohlc(pd.concat([_mk_df(), bad], ignore_index=True))
        assert len(out) == 12
        assert (out[["open", "high", "low", "close"]] > 0).all().all()
        # 摘要不得被零价行拉低
        s = charting.summarize_ohlc(pd.concat([_mk_df(), bad], ignore_index=True))
        assert s["period_low"] > 0

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

    def test_month_rule_probe(self):
        # 行为探测而非版本字符串比较; 当前 pandas 下应返回可用规则且月K可重采样
        rule = charting._month_rule()
        to_offset(rule)  # 不抛异常即有效
        m = charting.resample_ohlc(_mk_df(), "monthly")
        assert len(m) == 1


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


class TestKlineComponent:
    """页面端 lightweight-charts 组件 (st.components.v2) 的 payload 与 JS 源."""

    def test_payload_contents(self):
        p = charting.kline_payload(_mk_df(), "AAPL", currency="USD", mas="5,20")
        assert len(p["candles"]) == 12
        assert p["candles"][0]["time"] == "2025-01-06"
        # 测试数据仅 12 根: MA5 有值, MA20 窗口过大被跳过
        assert [m["name"] for m in p["mas"]] == ["MA5"]
        assert "AAPL" in p["title"] and "USD" in p["title"]
        assert len(p["volume"]) == 12

    def test_no_volume(self):
        p = charting.kline_payload(_mk_df(), "AAPL", show_volume=False)
        assert p["volume"] == []

    def test_green_up_palette(self):
        p = charting.kline_payload(_mk_df(), "AAPL", green_up=True)
        assert p["up"] == charting.INTL_UP_COLOR
        assert p["candleOpts"]["upColor"] == charting.INTL_UP_COLOR

    def test_component_js_bundle(self):
        js = charting.kline_component_js()
        assert "window.LightweightCharts" in js
        assert "export default function" in js

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            charting.kline_payload(pd.DataFrame(), "AAPL")


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

        def fake_history(symbol, months=12, start_date=None, end_date=None, prefer_akshare=False, use_ibkr=False):
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

        def fake_history(symbol, months=12, start_date=None, end_date=None, prefer_akshare=False, use_ibkr=False):
            calls["n"] += 1
            return _mk_df(base=100.0 + calls["n"])

        monkeypatch.setattr(prices, "get_history", fake_history)
        prices.get_ohlc("AAPL", months=12)
        prices.get_ohlc("AAPL", months=12, refresh=True)
        assert calls["n"] == 2

    def test_end_date_only_does_not_poison_cache(self, monkeypatch):
        """只给 end_date 的区间查询结果被截断, 不得写入 months 缓存."""
        from tracker import prices

        calls = {"n": 0}

        def fake_history(symbol, months=12, start_date=None, end_date=None, prefer_akshare=False, use_ibkr=False):
            calls["n"] += 1
            return _mk_df()

        monkeypatch.setattr(prices, "get_history", fake_history)
        truncated = prices.get_ohlc("AAPL", months=12, end_date="2025-01-10")
        assert len(truncated) == 12
        # 再次请求完整窗口必须重新取数 (缓存里没有这次截断的数据)
        prices.get_ohlc("AAPL", months=12)
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
        from tracker.cli import kline as kline_mod

        monkeypatch.setattr(kline_mod, "VAR_DIR", tmp_path / "var")
        out = self._run(capsys, "kline", "600519.SS")
        assert "K线图已生成" in out
        assert (tmp_path / "var" / "kline_600519_SS.html").exists()


class TestMacd:
    """MACD 指标计算测试."""

    def test_columns_and_length(self):
        df = _mk_df()
        macd = charting.calc_macd(df)
        assert list(macd.columns) == ["dif", "dea", "macd"]
        assert len(macd) == len(df)

    def test_dif_is_ema_difference(self):
        # 单调上涨: DIF 应 >= 0 (首值为0, 后续为正)
        df = pd.DataFrame({"close": [float(i) for i in range(100, 200)]})
        macd = charting.calc_macd(df)
        assert (macd["dif"].dropna() >= 0).all()
        assert macd["dif"].iloc[-1] > 0  # 末值明确为正

    def test_macd_hist_formula(self):
        # MACD 柱 = 2 × (DIF - DEA)
        df = _mk_df()
        macd = charting.calc_macd(df)
        expected = 2 * (macd["dif"] - macd["dea"])
        pd.testing.assert_series_equal(macd["macd"], expected, check_names=False)

    def test_golden_cross(self):
        # 先跌后涨: DIF 从下方穿越 DEA (金叉), 至少存在 DIF > DEA 的点
        prices = [100 - i for i in range(30)] + [70 + i * 2 for i in range(30)]
        df = pd.DataFrame({"close": [float(p) for p in prices]})
        macd = charting.calc_macd(df)
        # 后半段 DIF 上穿 DEA
        assert (macd["dif"].iloc[-1] > macd["dea"].iloc[-1])
        # 前半段 DIF 在 DEA 下方 (死叉区间)
        assert (macd["dif"].iloc[10] < macd["dea"].iloc[10])

    def test_custom_params(self):
        df = _mk_df()
        m1 = charting.calc_macd(df, fast=5, slow=10, signal=3)
        m2 = charting.calc_macd(df)  # 默认 12/26/9
        # 不同参数应产生不同结果
        assert not m1["dif"].equals(m2["dif"])


class TestRsi:
    """RSI 指标计算测试."""

    def test_range_0_100(self):
        df = _mk_df()
        rsi = charting.calc_rsi(df)
        valid = rsi.dropna()
        assert (valid >= 0).all() and (valid <= 100).all()

    def test_all_up_rsi_high(self):
        # 持续上涨: RSI 应接近 100
        df = pd.DataFrame({"close": [100.0 + i for i in range(50)]})
        rsi = charting.calc_rsi(df)
        assert rsi.iloc[-1] > 90

    def test_all_down_rsi_low(self):
        # 持续下跌: RSI 应接近 0
        df = pd.DataFrame({"close": [200.0 - i for i in range(50)]})
        rsi = charting.calc_rsi(df)
        assert rsi.iloc[-1] < 10

    def test_custom_period(self):
        # 使用有涨有跌的数据, 不同周期应产生不同 RSI
        prices = [100, 102, 99, 103, 98, 105, 97, 108, 95, 110, 96, 109]
        df = pd.DataFrame({"close": [float(p) for p in prices]})
        r7 = charting.calc_rsi(df, period=7)
        r14 = charting.calc_rsi(df, period=14)
        assert not r7.equals(r14)

    def test_first_value_nan(self):
        # 第一根无前值, diff 为 NaN → RSI 首值应为 NaN
        df = _mk_df()
        rsi = charting.calc_rsi(df)
        assert pd.isna(rsi.iloc[0])


class TestKdj:
    """KDJ 指标计算测试."""

    def test_columns_and_length(self):
        df = _mk_df()
        kdj = charting.calc_kdj(df)
        assert list(kdj.columns) == ["k", "d", "j"]
        assert len(kdj) == len(df)

    def test_j_formula(self):
        # J = 3K - 2D
        df = _mk_df()
        kdj = charting.calc_kdj(df)
        expected = 3 * kdj["k"] - 2 * kdj["d"]
        pd.testing.assert_series_equal(kdj["j"], expected, check_names=False)

    def test_kd_range(self):
        # K/D 由 RSV (0~100) EMA 而来, 应在合理范围内
        df = _mk_df()
        kdj = charting.calc_kdj(df)
        valid_k = kdj["k"].dropna()
        valid_d = kdj["d"].dropna()
        assert (valid_k >= -20).all() and (valid_k <= 120).all()
        assert (valid_d >= -20).all() and (valid_d <= 120).all()

    def test_custom_params(self):
        df = _mk_df()
        k1 = charting.calc_kdj(df, n=5, m1=2, m2=2)
        k2 = charting.calc_kdj(df)  # 默认 9/3/3
        assert not k1["k"].equals(k2["k"])


class TestBoll:
    """布林带指标计算测试."""

    def test_columns_and_length(self):
        df = _mk_df()
        boll = charting.calc_boll(df, period=5)
        assert list(boll.columns) == ["upper", "middle", "lower"]
        assert len(boll) == len(df)

    def test_middle_is_ma(self):
        # 中轨 = 收盘价 N 周期 SMA
        df = _mk_df()
        period = 5
        boll = charting.calc_boll(df, period=period)
        expected_ma = df["close"].rolling(window=period, min_periods=period).mean()
        pd.testing.assert_series_equal(boll["middle"], expected_ma, check_names=False)

    def test_upper_lower_symmetry(self):
        # 上轨 - 中轨 = 中轨 - 下轨 = std × std_mult
        df = _mk_df()
        boll = charting.calc_boll(df, period=5, std=2.0)
        diff_up = boll["upper"] - boll["middle"]
        diff_lo = boll["middle"] - boll["lower"]
        valid = diff_up.dropna()
        pd.testing.assert_series_equal(
            diff_up[valid.index], diff_lo[valid.index], check_names=False
        )

    def test_upper_above_middle_above_lower(self):
        df = _mk_df()
        boll = charting.calc_boll(df, period=5)
        valid = boll.dropna()
        assert (valid["upper"] >= valid["middle"]).all()
        assert (valid["middle"] >= valid["lower"]).all()

    def test_nan_before_period(self):
        # 不足一个窗口的前段为 NaN
        df = _mk_df()
        boll = charting.calc_boll(df, period=5)
        assert pd.isna(boll["middle"].iloc[0])
        assert pd.isna(boll["middle"].iloc[3])
        assert pd.notna(boll["middle"].iloc[4])


class TestKlinePayloadIndicators:
    """kline_payload 技术指标集成测试."""

    def test_payload_with_all_indicators(self):
        df = _mk_df()
        indicators = {
            "macd": {"fast": 5, "slow": 10, "signal": 3},
            "rsi": {"period": 7},
            "kdj": {"n": 5, "m1": 3, "m2": 3},
            "boll": {"period": 5, "std": 2.0},
        }
        p = charting.kline_payload(df, "AAPL", indicators=indicators)
        # 布林带: 3 条线 (上/中/下)
        assert len(p["boll"]) == 3
        assert p["boll"][0]["name"] == "BOLL上轨"
        # MACD: dif/dea/hist 数据 + 颜色
        assert p["macd"] is not None
        assert len(p["macd"]["dif"]) > 0
        assert len(p["macd"]["dea"]) > 0
        assert len(p["macd"]["hist"]) > 0
        assert "difColor" in p["macd"] and "deaColor" in p["macd"]
        # RSI
        assert p["rsi"] is not None
        assert len(p["rsi"]["data"]) > 0
        assert "color" in p["rsi"]
        # KDJ
        assert p["kdj"] is not None
        assert len(p["kdj"]["k"]) > 0
        assert len(p["kdj"]["d"]) > 0
        assert len(p["kdj"]["j"]) > 0

    def test_payload_no_indicators(self):
        p = charting.kline_payload(_mk_df(), "AAPL")
        assert p["boll"] == []
        assert p["macd"] is None
        assert p["rsi"] is None
        assert p["kdj"] is None

    def test_payload_partial_indicators(self):
        df = _mk_df()
        p = charting.kline_payload(df, "AAPL", indicators={"rsi": {"period": 7}})
        assert p["boll"] == []
        assert p["macd"] is None
        assert p["rsi"] is not None
        assert p["kdj"] is None
