"""K线模块测试: 数据清洗 / 均线 / 重采样 / 图表构建 / 摘要 / OHLC 缓存. 纯离线."""

import json
import sqlite3

import numpy as np
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
                {
                    "date": pd.Timestamp(2025, 2, 3),
                    "open": 1,
                    "high": 2,
                    "low": 0.5,
                    "close": None,
                    "volume": 1,
                },
                # high < low
                {
                    "date": pd.Timestamp(2025, 2, 4),
                    "open": 10,
                    "high": 9,
                    "low": 11,
                    "close": 10,
                    "volume": 1,
                },
                # high < max(open, close)
                {
                    "date": pd.Timestamp(2025, 2, 5),
                    "open": 10,
                    "high": 10.5,
                    "low": 9.5,
                    "close": 12,
                    "volume": 1,
                },
                # low > min(open, close)
                {
                    "date": pd.Timestamp(2025, 2, 6),
                    "open": 10,
                    "high": 11,
                    "low": 10.2,
                    "close": 9.5,
                    "volume": 1,
                },
                # close <= 0
                {
                    "date": pd.Timestamp(2025, 2, 7),
                    "open": 1,
                    "high": 2,
                    "low": 0.5,
                    "close": -1,
                    "volume": 1,
                },
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
                {
                    "date": pd.Timestamp(2025, 3, 3),
                    "open": 10,
                    "high": 12,
                    "low": 0,
                    "close": 11,
                    "volume": 1,
                },
                # open = 0
                {
                    "date": pd.Timestamp(2025, 3, 4),
                    "open": 0,
                    "high": 12,
                    "low": 1,
                    "close": 11,
                    "volume": 1,
                },
                # high = 0
                {
                    "date": pd.Timestamp(2025, 3, 5),
                    "open": 1,
                    "high": 0,
                    "low": 0,
                    "close": 1,
                    "volume": 1,
                },
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
        assert "全部" in labels and "1年" in labels and "5年" in labels

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

    def test_component_js_supports_trackpad_pinch_zoom(self):
        """触控板双指捏合: ctrl+wheel (Chromium/Firefox) 与 Safari gesture 事件."""
        js = charting.kline_component_js()
        # Chromium/Firefox 把捏合映射为 ctrlKey + wheel, 需要阻止整页缩放
        assert "onPinchWheel" in js and "ev.ctrlKey" in js
        # Safari 只发私有 gesture 事件
        assert "gesturestart" in js and "gesturechange" in js
        # 以光标为锚点缩放时间轴, 且用非 passive 监听才能 preventDefault
        assert "coordinateToLogical" in js
        assert "setVisibleLogicalRange" in js
        assert "passive: false" in js

    def test_component_js_has_right_click_menu(self):
        """图表右键: 自己的菜单而不是浏览器原生菜单 (重置/复制/导出)."""
        js = charting.kline_component_js()
        from tracker.ctx_menu_js import CTX_MENU_KIT_JS

        assert CTX_MENU_KIT_JS in js
        assert "contextmenu" in js
        assert "重置缩放" in js
        assert "复制数据 (CSV)" in js
        assert "导出图片 (PNG)" in js
        assert "takeScreenshot" in js
        # 触控板两指双击: 第二次 contextmenu 不重开菜单, 菜单上右键不叠原生菜单
        assert "两指双击" in js
        assert "ctxEl.addEventListener('contextmenu'" in js
        assert "ctxLastAt" in js
        assert "ev.button === 2 || ev.ctrlKey" in js

    def test_payload_copy_text_is_symbol(self):
        """右键「复制代码」只复制标的, 不把周期/币种一起带出去."""
        p = charting.kline_payload(_mk_df(), "AAPL", currency="USD")
        assert p["copyText"] == "AAPL"

    def test_compare_and_line_payload_copy_text(self):
        frames = {"AAPL": _mk_df(), "MSFT": _mk_df()}
        cmp = charting.compare_payload(frames)
        assert cmp["copyText"] == "AAPL MSFT"
        line = charting.line_payload(
            _mk_df(), value_col="close", series_name="AH 溢价率"
        )
        assert line["copyText"] == "AH 溢价率"

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

        def fake_history(
            symbol,
            months=12,
            start_date=None,
            end_date=None,
            prefer_akshare=False,
            use_ibkr=False,
            use_longport=False,
            crypto_source="auto",
        ):
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

        def fake_history(
            symbol,
            months=12,
            start_date=None,
            end_date=None,
            prefer_akshare=False,
            use_ibkr=False,
            use_longport=False,
            crypto_source="auto",
        ):
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

        def fake_history(
            symbol,
            months=12,
            start_date=None,
            end_date=None,
            prefer_akshare=False,
            use_ibkr=False,
            use_longport=False,
            crypto_source="auto",
        ):
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
        assert macd["dif"].iloc[-1] > macd["dea"].iloc[-1]
        # 前半段 DIF 在 DEA 下方 (死叉区间)
        assert macd["dif"].iloc[10] < macd["dea"].iloc[10]

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


def _ratio_frame(dates, base: float) -> pd.DataFrame:
    """合法 OHLC (high ≥ close, low ≤ close), 收盘价 base, base+1, ..."""
    rows = []
    for i, d in enumerate(dates):
        c = base + i
        rows.append(
            {
                "date": d,
                "open": c - 0.5,
                "high": c + 0.5,
                "low": c - 1.0,
                "close": c,
                "volume": 10.0,
            }
        )
    return pd.DataFrame(rows)


class TestRatioSeries:
    """比例线 (可选功能): 两标的收盘价比值 + 跨币种汇率调整."""

    def _frames(self):
        dates = pd.date_range("2025-01-06", periods=5, freq="B")
        # 分母缺一个交易日: 比例线应按「≤ 当日」的最近收盘价对齐
        den_dates = [d for d in dates if d != dates[2]]
        return (
            {
                "AAA": _ratio_frame(dates, 100.0),
                "BBB": _ratio_frame(den_dates, 10.0),
            },
            dates,
        )

    def test_ratio_aligns_missing_trading_day(self):
        frames, dates = self._frames()
        r = charting.build_ratio_series(frames, "AAA", "BBB")
        assert r["name"] == "AAA / BBB"
        assert r["fxAdjusted"] is False
        assert len(r["data"]) == len(dates)
        # 第 3 天分母无数据 → 沿用前一日收盘 11; 分子收盘 102
        assert r["data"][2]["time"] == dates[2].strftime("%Y-%m-%d")
        assert r["data"][2]["value"] == pytest.approx(round(102 / 11, r["precision"]))
        assert r["mean"] == pytest.approx(
            sum(p["value"] for p in r["data"]) / len(r["data"]), abs=1e-3
        )

    def test_ratio_fx_adjustment(self):
        frames, dates = self._frames()
        fx = pd.DataFrame(
            {"date": [dates[0] - pd.Timedelta(days=1)], "close": [0.5]}
        )
        r = charting.build_ratio_series(
            frames, "AAA", "BBB", fx_df=fx, fx_pair="HKDCNY"
        )
        assert r["fxAdjusted"] is True
        assert r["fxPair"] == "HKDCNY"
        # 比例 = 分子价 / (分母价 × 汇率); 首日 100 / (10 × 0.5)
        assert r["data"][0]["value"] == pytest.approx(20.0)
        assert r["precision"] >= 4

    def test_ratio_rejects_same_or_missing_code(self):
        frames, _ = self._frames()
        with pytest.raises(ValueError):
            charting.build_ratio_series(frames, "AAA", "AAA")
        with pytest.raises(ValueError):
            charting.build_ratio_series(frames, "AAA", "CCC")

    def test_compare_payload_embeds_ratio(self):
        frames, _ = self._frames()
        ratio = charting.build_ratio_series(frames, "AAA", "BBB")
        p = charting.compare_payload(frames, ratio=ratio)
        assert p["ratio"]["name"] == "AAA / BBB"
        assert p["mode"] == "compare"
        # 时间轴并集包含比例线日期
        assert set(p["ratio"]["data"][0]) == {"time", "value"}
        assert p["axis"] == sorted(set(p["axis"]))


def _wilder_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    d = close.diff()
    g = d.clip(lower=0)
    lo = -d.clip(upper=0)
    n = len(close)
    ag = np.full(n, np.nan)
    al = np.full(n, np.nan)
    if n <= period:
        return pd.Series(ag, index=close.index)
    ag[period] = g.iloc[1 : period + 1].mean()
    al[period] = lo.iloc[1 : period + 1].mean()
    for i in range(period + 1, n):
        ag[i] = (ag[i - 1] * (period - 1) + g.iloc[i]) / period
        al[i] = (al[i - 1] * (period - 1) + lo.iloc[i]) / period
    with np.errstate(all="ignore"):
        rs = ag / al
        r = 100 - 100 / (1 + rs)
    r[al == 0] = 100.0
    r[(ag == 0) & (al == 0)] = 50.0
    return pd.Series(r, index=close.index)


def test_rsi_warmup_is_nan():
    rng = np.random.default_rng(7)
    close = pd.Series(100 + np.cumsum(rng.standard_normal(40)))
    rsi = charting.calc_rsi(pd.DataFrame({"close": close}), 14)
    # 前 period 根为预热段, 应全 NaN (旧实现在这里产出伪造值)
    assert rsi.iloc[:14].isna().all()
    assert rsi.iloc[14:].notna().all()


def test_rsi_matches_wilder_steady_state():
    rng = np.random.default_rng(11)
    close = pd.Series(100 + np.cumsum(rng.standard_normal(60)))
    rsi = charting.calc_rsi(pd.DataFrame({"close": close}), 14)
    ref = _wilder_rsi(close, 14)
    assert float((rsi.iloc[14:] - ref.iloc[14:]).abs().max()) < 1e-9


def _shib_df():
    idx = pd.date_range("2024-01-01", periods=5, freq="D")
    return pd.DataFrame(
        {
            "date": idx,
            "open": [1.2e-5] * 5,
            "high": [1.6e-5] * 5,
            "low": [1.1e-5] * 5,
            "close": [1.234e-5, 1.301e-5, 1.255e-5, 1.402e-5, 1.5e-5],
            "volume": [1, 2, 3, 4, 5],
        }
    )


def test_kline_payload_preserves_low_price_precision():
    p = charting.kline_payload(_shib_df(), "SHIB-USD")
    close0 = p["candles"][0]["close"]
    assert abs(close0 - 1.234e-5) < 1e-12  # 未被 round(,6) 截成 1.2e-5
    assert p["precision"] >= 8


def test_compare_payload_raw_mode_not_zeroed():
    cp = charting.compare_payload({"SHIB-USD": _shib_df()}, normalize=False)
    v0 = cp["lines"][0]["data"][0]["value"]
    assert abs(v0 - 1.234e-5) < 1e-12  # 未被 round(,4) 归零
    assert cp["precision"] >= 8


# D4: clean_ohlc 过滤 volume 的 inf/负值
def test_clean_ohlc_filters_inf_and_negative_volume():
    df = pd.DataFrame(
        {
            "date": ["2025-01-06", "2025-01-07"],
            "open": [10.0, 11.0],
            "high": [12.0, 13.0],
            "low": [9.0, 10.0],
            "close": [11.0, 12.0],
            "volume": [np.inf, -5.0],
        }
    )
    out = charting.clean_ohlc(df)
    assert list(out["volume"]) == [0.0, 0.0]
    assert np.isfinite(out["volume"]).all()


def _low_price_df(n: int = 40) -> pd.DataFrame:
    import math as _math

    close = [1.2e-5 + _math.sin(i / 3) * 2e-6 for i in range(n)]
    return pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=n, freq="D"),
            "open": close,
            "high": [c * 1.01 for c in close],
            "low": [c * 0.99 for c in close],
            "close": close,
            "volume": [0.0] * n,
        }
    )


def test_summarize_low_price_not_rounded_to_zero():
    from tracker import charting

    s = charting.summarize_ohlc(_low_price_df())
    assert s["last_close"] > 0
    assert s["period_high"] > 0
    assert s["period_low"] > 0
    assert s["ma"]["ma5"] > 0


def test_kline_payload_low_price_indicators_not_zero():
    from tracker import charting

    payload = charting.kline_payload(
        _low_price_df(), "SHIB-USD", indicators={"macd": {}, "boll": {}}
    )
    assert payload["precision"] >= 8
    assert any(p["value"] != 0 for p in payload["macd"]["dif"])
    assert any(p["value"] != 0 for p in payload["macd"]["hist"])
    assert any(
        p["value"] != 0 for series in payload["boll"] for p in series["data"]
    )


class TestChartingFixes:
    def test_low_price_precision(self):
        """SHIB/PEPE (~1e-5) 单只K线精度自适应, 不显示成 0.0000."""
        idx = pd.date_range("2024-01-01", periods=5, freq="D")
        df = pd.DataFrame(
            {
                "date": idx,
                "open": 1e-5,
                "high": 1.1e-5,
                "low": 0.9e-5,
                "close": 1e-5,
                "volume": 1.0,
            }
        )
        payload = charting.kline_payload(df, "SHIB-USD", currency="USD")
        assert payload["precision"] >= 6
        # 高价股仍是 2 位
        df2 = df.assign(open=100.0, high=101.0, low=99.0, close=100.0)
        assert charting.kline_payload(df2, "AAPL")["precision"] == 2

    def test_crypto_compare_low_price_precision(self):
        """加密货币对比: 蜡烛取整位数不得低于声明的 precision.

        回归背景: 蜡烛硬编码 round(,8) 而 precision 可达 9~10 (中位价 ~1e-6),
        声明的 minMove=1e-10 却没有数据位可显示, 与 kline_payload 的
        nd=max(6, precision) 约定不一致。
        """
        idx = pd.date_range("2024-01-01", periods=5, freq="D")
        # 中位价 5.432189e-6 → _price_precision = 9 (> 旧实现的硬编码 8)
        df = pd.DataFrame(
            {
                "date": idx,
                "open": 5.432189e-6,
                "high": 5.442189e-6,
                "low": 5.422189e-6,
                "close": 5.432189e-6,
                "volume": 1.0,
            }
        )
        payload = charting.crypto_compare_payload({"PEPE": df})
        coin = payload["coins"][0]
        assert coin["precision"] == 9
        # 9 位小数: 第 9 位 (…432) 保留; 旧代码 round(,8) 会截成 0.00000543
        assert coin["candles"][0]["close"] == round(5.432189e-6, 9)
        assert coin["candles"][0]["close"] != round(5.432189e-6, 8)

    def test_line_payload_reference_and_unit(self):
        """单序列折线: 保留原始值, 带 0% 参考线与单位后缀 (AH 溢价页用)."""
        df = pd.DataFrame(
            {
                "date": pd.date_range("2024-01-01", periods=3),
                "premium_pct": [1.0, -2.5, 3.25],
            }
        )
        p = charting.line_payload(
            df,
            value_col="premium_pct",
            title="AH 溢价率",
            series_name="AH 溢价率",
            reference=0.0,
            reference_label="0% 同价",
            unit="%",
        )
        assert p["mode"] == "compare"
        assert p["reference"] == 0.0
        assert p["referenceLabel"] == "0% 同价"
        assert p["unit"] == "%"
        assert p["showChange"] is False
        assert p["lines"][0]["data"] == [
            {"time": "2024-01-01", "value": 1.0},
            {"time": "2024-01-02", "value": -2.5},
            {"time": "2024-01-03", "value": 3.25},
        ]
        assert p["axis"] == ["2024-01-01", "2024-01-02", "2024-01-03"]

    def test_line_payload_drops_dirty_rows(self):
        df = pd.DataFrame(
            {
                "date": ["2024-01-02", "bad", "2024-01-02", "2024-01-03"],
                "premium_pct": [1.0, 2.0, 1.5, float("inf")],
            }
        )
        p = charting.line_payload(df, value_col="premium_pct")
        assert p["lines"][0]["data"] == [{"time": "2024-01-02", "value": 1.5}]

    def test_compare_payloads_carry_axis(self):
        """对比/加密货币对比 payload 带 axis 时间轴 (视口锚点索引空间)."""
        f1 = pd.DataFrame(
            {
                "date": pd.date_range("2024-01-01", periods=3),
                "open": 1.0,
                "high": 5.0,
                "low": 0.5,
                "close": [1.0, 2.0, 3.0],
                "volume": 0.0,
            }
        )
        f2 = pd.DataFrame(
            {
                "date": pd.date_range("2024-01-02", periods=3),
                "open": 2.0,
                "high": 6.0,
                "low": 1.5,
                "close": [2.0, 3.0, 4.0],
                "volume": 0.0,
            }
        )
        cp = charting.compare_payload({"AAA": f1, "BBB": f2})
        cc = charting.crypto_compare_payload({"AAA": f1, "BBB": f2})
        assert cp["axis"] == [
            "2024-01-01",
            "2024-01-02",
            "2024-01-03",
            "2024-01-04",
        ]
        assert cc["axis"] == cp["axis"]
