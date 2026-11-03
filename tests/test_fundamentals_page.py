"""「基本面分析」页 (AppTest, 不联网): AH 溢价曲线渲染与交互门控."""

from __future__ import annotations

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

import tracker.ah_premium as ah
import tracker.prices as prices
import tracker.ui.fundamentals_page as fundamentals_page

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


@pytest.fixture
def kv_pairs(monkeypatch):
    pairs = [
        ah.AHPair("工商银行", "601398.SS", "1398.HK", 20.0),
        ah.AHPair("招商银行", "600036.SS", "3968.HK", 15.0),
    ]
    monkeypatch.setattr(ah, "list_pairs", lambda refresh=False: list(pairs))
    monkeypatch.setattr(prices, "get_ohlc", _fake_ohlc)
    monkeypatch.setattr(ah.fx_mod, "get_fx_history", _fake_fx)
    fundamentals_page.cached_ah_pairs.clear()
    fundamentals_page.cached_ah_premium.clear()
    return pairs


def _rendered(monkeypatch, kv_pairs):
    captured = {}

    def fake_chart(**kw):
        captured["data"] = kw.get("data")
        return None

    monkeypatch.setattr(fundamentals_page, "_LINE_CHART", fake_chart)
    at = AppTest.from_string(_SCRIPT, default_timeout=30).run()
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
