"""AH 溢价率: 代码归一 / 曲线计算 / 摘要 / 股票对目录 (纯逻辑, 不联网)."""

from __future__ import annotations

import pandas as pd
import pytest

import tracker.ah_premium as ah


@pytest.fixture(autouse=True)
def _clear_pairs():
    ah.clear_pair_cache()
    yield
    ah.clear_pair_cache()


def _mk(rows):
    return pd.DataFrame(
        {
            "date": pd.to_datetime([r[0] for r in rows]),
            "close": [r[1] for r in rows],
        }
    )


class TestSymbolNormalize:
    @pytest.mark.parametrize(
        "raw,out",
        [
            ("601398", "601398.SS"),
            ("601398.SS", "601398.SS"),
            ("601398.SH", "601398.SS"),
            ("000001", "000001.SZ"),
            ("300750.SZ", "300750.SZ"),
            ("830799", "830799.BJ"),
        ],
    )
    def test_a_symbol(self, raw, out):
        assert ah.normalize_a_symbol(raw) == out

    @pytest.mark.parametrize(
        "raw,out",
        [
            ("1398", "1398.HK"),
            ("01398", "1398.HK"),
            ("1398.HK", "1398.HK"),
            ("00700.HK", "0700.HK"),
        ],
    )
    def test_h_symbol(self, raw, out):
        assert ah.normalize_h_symbol(raw) == out

    def test_invalid(self):
        with pytest.raises(ValueError):
            ah.normalize_a_symbol("1398.HK")
        with pytest.raises(ValueError):
            ah.normalize_h_symbol("601398.SS")
        with pytest.raises(ValueError):
            ah.normalize_a_symbol("")


class TestPremiumSeries:
    def test_fx_conversion_and_asof_alignment(self):
        a = _mk([("2024-01-02", 10.0), ("2024-01-03", 10.2), ("2024-01-04", 10.1)])
        h = _mk([("2024-01-02", 11.0), ("2024-01-04", 11.1)])
        fx = _mk([("2024-01-02", 0.9), ("2024-01-03", 0.91), ("2024-01-04", 0.92)])
        out = ah.calc_premium_series(a, h, fx, fx_required=True)
        assert list(out["date"].dt.strftime("%Y-%m-%d")) == [
            "2024-01-02",
            "2024-01-03",
            "2024-01-04",
        ]
        # 1/3 H 股休市: 沿用 1/2 的 11.0, 汇率取当日 0.91
        assert out.iloc[1]["h_close"] == pytest.approx(11.0)
        assert out.iloc[1]["fx_rate"] == pytest.approx(0.91)
        expected = (10.2 / (11.0 * 0.91) - 1) * 100
        assert out.iloc[1]["premium_pct"] == pytest.approx(expected)

    def test_same_currency_no_fx(self):
        a = _mk([("2024-01-02", 10.0)])
        h = _mk([("2024-01-02", 8.0)])
        out = ah.calc_premium_series(a, h)
        assert out.iloc[0]["fx_rate"] == pytest.approx(1.0)
        assert out.iloc[0]["premium_pct"] == pytest.approx(25.0)

    def test_fx_required_but_missing_raises(self):
        a = _mk([("2024-01-02", 10.0)])
        h = _mk([("2024-01-02", 8.0)])
        with pytest.raises(ValueError, match="汇率"):
            ah.calc_premium_series(a, h, None, fx_required=True)

    def test_no_overlap_raises(self):
        # A 股数据全部早于 H 股首日: 没有可用的 H 收盘, 不能倒填未来价格
        a = _mk([("2024-01-01", 10.0)])
        h = _mk([("2024-02-01", 8.0)])
        with pytest.raises(ValueError, match="重叠"):
            ah.calc_premium_series(a, h)

    def test_dirty_rows_are_dropped(self):
        a = pd.DataFrame(
            {
                "date": ["2024-01-02", "bad", "2024-01-03", "2024-01-03"],
                "close": [10.0, 1.0, -2.0, 10.5],
            }
        )
        h = _mk([("2024-01-02", 8.0), ("2024-01-03", 8.2)])
        out = ah.calc_premium_series(a, h)
        assert len(out) == 2
        assert out.iloc[-1]["a_close"] == pytest.approx(10.5)  # 同日保留最后一条


class TestSummary:
    def test_summary_values(self):
        df = pd.DataFrame(
            {
                "date": pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"]),
                "a_close": [10.0, 10.2, 10.1],
                "h_close": [8.0, 8.1, 8.0],
                "fx_rate": [0.9, 0.9, 0.9],
                "premium_pct": [10.0, 20.0, 5.0],
            }
        )
        s = ah.summarize_premium(df)
        assert s["bars"] == 3
        assert s["first_date"] == "2024-01-02"
        assert s["last_date"] == "2024-01-04"
        assert s["last_premium"] == pytest.approx(5.0)
        assert s["change_pp"] == pytest.approx(-5.0)
        assert s["high"] == pytest.approx(20.0)
        assert s["high_date"] == "2024-01-03"
        assert s["low"] == pytest.approx(5.0)
        assert s["mean"] == pytest.approx(35.0 / 3)
        assert s["percentile"] == pytest.approx(100.0 / 3)
        assert s["h_close_cny"] == pytest.approx(8.0 * 0.9)

    def test_summary_empty(self):
        assert ah.summarize_premium(pd.DataFrame()) == {}


class TestPairs:
    def test_parse_eastmoney_rows(self, monkeypatch):
        df = pd.DataFrame(
            {
                "名称": ["浙江世宝", "工商银行", "招商银行"],
                "H股代码": ["01057", "01398", "03968"],
                "A股代码": ["002703", "601398", "600036"],
                "溢价": [390.0, 20.5, 15.0],
            }
        )

        class FakeAk:
            def stock_zh_ah_spot_em(self):
                return df

        monkeypatch.setattr(ah, "_ak", lambda: FakeAk())
        pairs = ah.list_pairs()
        # 常见大盘股置顶 (默认选中项在第一屏), 其余按溢价率降序
        assert [p.name for p in pairs] == ["工商银行", "招商银行", "浙江世宝"]
        assert pairs[0].a_symbol == "601398.SS"
        assert pairs[0].h_symbol == "1398.HK"
        assert pairs[0].premium_pct == pytest.approx(20.5)

    def test_fallback_when_source_fails(self, monkeypatch):
        class BoomAk:
            def stock_zh_ah_spot_em(self):
                raise RuntimeError("network down")

        monkeypatch.setattr(ah, "_ak", lambda: BoomAk())
        pairs = ah.list_pairs()
        assert any(p.name == "工商银行" for p in pairs)
        assert ah.default_pair_index(pairs) == next(
            i for i, p in enumerate(pairs) if p.name == "工商银行"
        )

    def test_refresh_uses_new_source(self, monkeypatch):
        calls = []

        def fake_ak():
            class FakeAk:
                def stock_zh_ah_spot_em(self):
                    calls.append(1)
                    return pd.DataFrame(
                        {
                            "名称": [f"股票{len(calls)}"],
                            "H股代码": ["01398"],
                            "A股代码": ["601398"],
                        }
                    )

            return FakeAk()

        monkeypatch.setattr(ah, "_ak", fake_ak)
        assert ah.list_pairs()[0].name == "股票1"
        assert ah.list_pairs()[0].name == "股票1"  # 1h 缓存
        assert ah.list_pairs(refresh=True)[0].name == "股票2"


class TestFetch:
    def test_fetch_uses_consistent_akshare_chain(self, monkeypatch):
        a_df = _mk([("2024-01-02", 10.0), ("2024-01-03", 10.2)])
        h_df = _mk([("2024-01-02", 8.0), ("2024-01-03", 8.1)])
        fx_df = _mk([("2024-01-02", 0.9), ("2024-01-03", 0.9)])
        calls = []

        def fake_ohlc(symbol, months=12, prefer_akshare=False, refresh=False, **kw):
            calls.append((symbol, prefer_akshare, refresh, kw))
            return a_df if symbol.endswith((".SS", ".SZ", ".BJ")) else h_df

        monkeypatch.setattr(ah.prices, "get_ohlc", fake_ohlc)
        monkeypatch.setattr(ah.fx_mod, "get_fx_history", lambda *a, **kw: fx_df)
        series, meta = ah.fetch_ah_premium("601398", "01398", months=12)
        assert len(series) == 2
        assert meta["a_symbol"] == "601398.SS"
        assert meta["h_symbol"] == "1398.HK"
        assert meta["fx_pair"] == "HKDCNY"
        # H 股必须 prefer_akshare (与 A 股 qfq 同口径), 且不启用 IBKR/长桥
        h_call = next(c for c in calls if c[0] == "1398.HK")
        assert h_call[1] is True
        assert h_call[3]["use_ibkr"] is False
        assert h_call[3]["use_longport"] is False

    def test_fetch_missing_fx_raises(self, monkeypatch):
        a_df = _mk([("2024-01-02", 10.0)])
        h_df = _mk([("2024-01-02", 8.0)])

        def fake_ohlc(symbol, **kw):
            return a_df if symbol.endswith((".SS", ".SZ", ".BJ")) else h_df

        monkeypatch.setattr(ah.prices, "get_ohlc", fake_ohlc)
        monkeypatch.setattr(ah.fx_mod, "get_fx_history", lambda *a, **kw: None)
        with pytest.raises(RuntimeError, match="汇率"):
            ah.fetch_ah_premium("601398.SS", "1398.HK")
