"""「基本面分析」页 (AppTest, 不联网): AH 溢价曲线与估值指标子视图."""

from __future__ import annotations

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

import tracker.ah_premium as ah
import tracker.prices as prices
import tracker.ui.fundamentals_page as fundamentals_page
from tracker import valuation

_SCRIPT = """
import tracker.ui.fundamentals_page as fp
fp.render_fundamentals_page()
"""


def _fake_ohlc(symbol, months=12, prefer_akshare=False, refresh=False, **kw):
    idx = pd.date_range("2024-01-01", periods=30, freq="D")
    close = 10.0 if symbol.endswith((".SS", ".SZ", ".BJ")) else 8.0
    return pd.DataFrame(
        {
            "date": idx,
            "open": close,
            "high": close * 1.02,
            "low": close * 0.98,
            "close": [close + i * 0.01 for i in range(30)],
            "volume": 100.0,
        }
    )


def _fake_fx(*a, **kw):
    return pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=30, freq="D"),
            "close": [0.9] * 30,
        }
    )


def _fake_snapshot(symbol, refresh=False):
    return valuation.ValuationSnapshot(
        symbol=symbol,
        market_label="A股",
        currency="CNY",
        pe_ttm=8.4,
        forward_pe=8.2,
        pb=0.79,
        dividend_yield_pct=3.68,
        dividend_yield_5y_avg_pct=5.29,
        book_value=11.2,
        eps_ttm=1.05,
        source="yfinance",
        fetched_at=1_700_000_000.0,
    )


def _fake_history(symbol, metric="pe", months=36, refresh=False):
    idx = pd.date_range("2024-01-01", periods=30, freq="D")
    return pd.DataFrame({"date": idx, "value": [8.0 + i * 0.02 for i in range(30)]})


@pytest.fixture
def kv_pairs(monkeypatch):
    pairs = [
        ah.AHPair("工商银行", "601398.SS", "1398.HK", 20.0),
        ah.AHPair("招商银行", "600036.SS", "3968.HK", 15.0),
    ]
    monkeypatch.setattr(ah, "list_pairs", lambda refresh=False: list(pairs))
    monkeypatch.setattr(prices, "get_ohlc", _fake_ohlc)
    monkeypatch.setattr(ah.fx_mod, "get_fx_history", _fake_fx)
    monkeypatch.setattr(
        fundamentals_page, "cached_valuation", _fake_snapshot
    )
    monkeypatch.setattr(
        fundamentals_page, "cached_valuation_history", _fake_history
    )
    fundamentals_page.cached_ah_pairs.clear()
    fundamentals_page.cached_ah_premium.clear()
    return pairs


def _rendered(monkeypatch, kv_pairs, view="AH 溢价"):
    captured = {}

    def fake_chart(**kw):
        captured["data"] = kw.get("data")
        return None

    monkeypatch.setattr(fundamentals_page, "_LINE_CHART", fake_chart)
    at = AppTest.from_string(_SCRIPT, default_timeout=30).run()
    assert not at.exception, at.exception
    if view == "AH 溢价":
        at.segmented_control(key="fundamentals_view").set_value("AH 溢价").run()
        assert not at.exception, at.exception
    return at, captured


def test_renders_premium_curve(monkeypatch, kv_pairs):
    at, captured = _rendered(monkeypatch, kv_pairs)
    assert len(at.metric) == 4
    assert any("AH 溢价率" in str(m.value) for m in at.markdown) or any(
        "AH 溢价率" in c.value for c in at.caption
    )
    payload = captured["data"]
    assert payload["mode"] == "compare"
    assert payload["reference"] == 0.0
    assert payload["referenceLabel"] == "0% 同价"
    assert payload["unit"] == "%"
    assert payload["lines"][0]["name"] == "AH 溢价率"
    assert payload["lines"][0]["data"]
    assert at.selectbox(key="ah_months").value == 12


def test_switch_pair_and_range_refetch(monkeypatch, kv_pairs):
    at, _ = _rendered(monkeypatch, kv_pairs)
    at.selectbox(key="ah_pair").select(at.selectbox(key="ah_pair").options[1]).run()
    assert not at.exception
    assert len(at.metric) == 4
    at.selectbox(key="ah_months").set_value(6).run()
    assert not at.exception
    assert len(at.metric) == 4


def test_force_refresh_bypasses_caches(monkeypatch, kv_pairs):
    at, _ = _rendered(monkeypatch, kv_pairs)
    calls = []

    def spy_ohlc(symbol, months=12, prefer_akshare=False, refresh=False, **kw):
        calls.append(bool(refresh))
        return _fake_ohlc(symbol, months, prefer_akshare, refresh=refresh, **kw)

    monkeypatch.setattr(prices, "get_ohlc", spy_ohlc)
    fundamentals_page.cached_ah_premium.clear()
    at.button(key="ah_force").click().run()
    assert not at.exception
    assert calls and all(c is True for c in calls), calls


def test_manual_code_entry(monkeypatch, kv_pairs):
    at, _ = _rendered(monkeypatch, kv_pairs)
    at.toggle(key="ah_manual").set_value(True).run()
    assert not at.exception
    assert any(t.key == "ah_a_code" for t in at.text_input)
    at.text_input(key="ah_a_code").set_value("600036.SS").run()
    at.text_input(key="ah_h_code").set_value("3968.HK").run()
    assert not at.exception
    assert len(at.metric) == 4


def test_invalid_manual_code_shows_error(monkeypatch, kv_pairs):
    at, _ = _rendered(monkeypatch, kv_pairs)
    at.toggle(key="ah_manual").set_value(True).run()
    at.text_input(key="ah_a_code").set_value("not-a-code").run()
    assert not at.exception
    assert at.error
    assert "无法识别" in at.error[0].value


def test_valuation_view_renders_pe_pb_dividend(monkeypatch, kv_pairs):
    at, captured = _rendered(monkeypatch, kv_pairs, view="估值指标")
    assert not at.exception
    labels = [m.label for m in at.metric]
    assert "PE (TTM)" in labels
    assert "PB" in labels
    assert "股息率" in labels
    assert "5 年平均股息率" in labels
    payload = captured["data"]
    assert payload["lines"][0]["name"] == "市盈率 (TTM)"
    assert payload["unit"] == "x"
    assert payload["lines"][0]["data"]


def test_valuation_view_switches_history_metric(monkeypatch, kv_pairs):
    at, captured = _rendered(monkeypatch, kv_pairs, view="估值指标")
    at.segmented_control(key="valuation_metric").set_value("市净率").run()
    assert not at.exception
    assert captured["data"]["lines"][0]["name"] == "市净率"


def test_valuation_invalid_code_shows_error(monkeypatch, kv_pairs):
    at, _ = _rendered(monkeypatch, kv_pairs, view="估值指标")
    at.text_input(key="valuation_symbol").set_value("not-a-code").run()
    assert not at.exception
    assert at.error
    assert "无法识别" in at.error[0].value


def test_valuation_force_refresh_propagates(monkeypatch, kv_pairs):
    at, _ = _rendered(monkeypatch, kv_pairs, view="估值指标")
    calls = []

    def spy_snapshot(symbol, refresh=False):
        calls.append(bool(refresh))
        return _fake_snapshot(symbol, refresh)

    monkeypatch.setattr(fundamentals_page, "cached_valuation", spy_snapshot)
    # 这里的 spy 没有 st.cache_data 的 .clear(); 强制刷新本身由下面的
    # refresh=True 断言覆盖, 清缓存动作在其它用例中验证。
    monkeypatch.setattr(fundamentals_page, "clear_valuation_caches", lambda: None)
    at.button(key="valuation_force").click().run()
    assert not at.exception
    assert calls and all(c is True for c in calls), calls
