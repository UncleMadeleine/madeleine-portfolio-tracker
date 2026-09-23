"""指数域 (IX.<KEY>) 测试: 代码解析/路由隔离/provider 源链/K线门面 (不联网)."""
from __future__ import annotations

import pandas as pd
import pytest

from tracker import prices
from tracker.providers import PROVIDERS, provider_for
from tracker.providers.base import resolve
from tracker.providers.index import IndexProvider
from tracker.symbols import INDEX_CATALOG, Market, index_key, index_label, parse, type_for_symbol


# ---------- 代码解析 ----------


def test_index_symbols_parse_to_index_domain():
    p = parse("IX.DXY")
    assert p.yahoo == "IX.DXY"
    assert p.market is Market.INDEX
    assert p.type == "index"
    assert p.currency == "USD"


def test_index_parse_case_insensitive():
    assert parse("ix.vix").yahoo == "IX.VIX"
    assert parse(" Ix.Csi300 ").yahoo == "IX.CSI300"


def test_index_cn_entries_expose_akshare_code():
    assert parse("IX.CSI300").ak_code == "000300" or INDEX_CATALOG["CSI300"]["ak"] == "sh000300"


def test_unknown_index_key_rejected():
    with pytest.raises(ValueError, match="未收录的指数代码"):
        parse("IX.NOPE")


def test_type_for_symbol_routes_index_exclusively():
    assert type_for_symbol("IX.NDX") == "index"
    # 指数与股票/加密货币互不误判
    assert type_for_symbol("AAPL") == "global"
    assert type_for_symbol("600519.SS") == "cn"
    assert type_for_symbol("BTC-USD") == "crypto"


def test_index_symbols_never_leak_into_stock_domains():
    for key in INDEX_CATALOG:
        assert parse(f"IX.{key}").type == "index"
        assert parse(f"IX.{key}").market is Market.INDEX


# ---------- provider 路由 ----------


def test_resolve_routes_index_domain():
    assert resolve("index").name == "index"
    assert isinstance(resolve("index"), IndexProvider)
    assert isinstance(provider_for(Market.INDEX), IndexProvider)
    assert PROVIDERS["index"] is resolve("index")


def test_index_provider_source_chains():
    provider = IndexProvider()
    # 中国指数: akshare 优先 (与 A 股域数据源惯例一致)
    cn = parse("IX.SSE")
    chains = provider.history_sources(cn, "2025-01-01", None)
    assert chains[0].__name__ == "ak"      # akshare 新浪源优先
    assert chains[1].__name__ == "yf"
    # 目录中无 ak 代码且不在美股新浪表的指数: 仅 yfinance
    vix = parse("IX.VIX")
    assert len(provider.history_sources(vix, "2025-01-01", None)) == 1


def test_ccement_catalog_entries_parse_to_index_domain():
    """水泥网 8 指数: 目录合法 + 统一带 ccement 路由字段 (无 ak/yf 源)."""
    ccement_keys = [k for k, e in INDEX_CATALOG.items() if "ccement" in e]
    assert len(ccement_keys) == 8
    for key in ccement_keys:
        p = parse(f"IX.{key}")
        assert p.type == "index"
        assert p.market is Market.INDEX
        e = INDEX_CATALOG[key]
        assert "ak" not in e and "yf" not in e


def test_ccement_source_chain_is_exclusive():
    """水泥网指数源链只有专属 cc 源, 不落入 yfinance/akshare 误查."""
    provider = IndexProvider()
    chains = provider.history_sources(parse("IX.CEMPI"), "2025-01-01", None)
    assert [f.__name__ for f in chains] == ["cc"]


def test_ccement_points_parses_dynamic_index_all(monkeypatch):
    """getPriceIndex 聚合载荷用 dynamicIndexAll (独立端点用 dynamicIndex), 都要能解析."""
    from tracker.providers import index as idx_mod

    d = {"dynamicIndexDate": ["2026-09-22", "2026-09-23"], "dynamicIndexAll": [95.77, 95.95]}
    df = idx_mod._ccement_points(d)
    assert list(df.columns) == ["date", "close", "open", "high", "low", "volume"]
    assert df.iloc[-1]["close"] == pytest.approx(95.95)
    assert (df["volume"] == 0.0).all()
    # 独立端点键名: dynamicIndex (可能是 JSON 字符串)
    d2 = {"dynamicIndexDate": ["2026-09-23"], "dynamicIndex": "[291.28]"}
    df2 = idx_mod._ccement_points(d2)
    assert df2.iloc[-1]["close"] == pytest.approx(291.28)


def test_ccement_kline_parses_weekly_rows(monkeypatch):
    """cementkline 行结构 → date/open/high/low/close/volume, 涨跌列丢弃."""
    from tracker.providers import index as idx_mod

    rows = [[1789948800000, 95.77, 95.95, 95.77, 95.95, 95.77, 0.18, 0.19]]
    df = idx_mod._ccement_kline(rows)
    assert list(df.columns) == ["date", "open", "high", "low", "close", "volume"]
    assert df.iloc[0]["close"] == pytest.approx(95.95)
    assert df.iloc[0]["date"] == pd.Timestamp("2026-09-21")


def test_ccement_history_rejects_empty_series(monkeypatch):
    """接口返回空序列必须报错, 不能产出空 DataFrame 静默通过."""
    from tracker.providers import index as idx_mod

    monkeypatch.setattr(idx_mod, "_ccement_post", lambda path, data: {"dynamicIndexDate": [], "dynamicIndex": []})
    with pytest.raises(RuntimeError, match="空序列"):
        idx_mod._ccement_history(parse("IX.CSPI"), "2025-01-01", None)


def test_index_provider_rejects_quote():
    with pytest.raises(NotImplementedError):
        IndexProvider().fetch_quote(parse("IX.DXY"), prefer_first=False)

def test_get_quotes_routes_index_to_explicit_error(monkeypatch):
    """IX.* 混入实时行情查询: 记入明确 errors, 不落入 global 域误查 yfinance."""
    import tracker.providers.orchestration as orch

    def no_batch(parsed):
        raise AssertionError("指数代码不得进入全球域批量行情")

    monkeypatch.setattr(orch, "_yahoo_batch", no_batch)
    monkeypatch.setattr(orch.cache_mod, "get_cached", lambda syms, ttl=300: {})
    monkeypatch.setattr(orch.cache_mod, "set_cached", lambda q: None)

    quotes, errors, notes = orch.get_quotes(["IX.DXY", "IX.VIX"])
    assert quotes == {}
    assert set(errors) == {"IX.DXY", "IX.VIX"}
    assert all("无实时行情" in msg for msg in errors.values())


def test_slice_range_end_date_is_inclusive_without_extra_day():
    """end_date 为闭区间: 不许多返回结束日次日那根 K 线."""
    from tracker.providers.index import _slice_range

    df = pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=5, freq="D"),
            "open": [1.0] * 5, "high": [1.0] * 5,
            "low": [1.0] * 5, "close": [1.0] * 5,
        }
    )
    out = _slice_range(df, "2026-01-01", "2026-01-03")
    assert [d.strftime("%Y-%m-%d") for d in out["date"]] == [
        "2026-01-01", "2026-01-02", "2026-01-03",
    ]
    # 无 end_date: 取到最新
    out2 = _slice_range(df, "2026-01-04", None)
    assert len(out2) == 2


# ---------- K线门面 ----------


def _fake_history(symbol, months=12, start_date=None, end_date=None, **kw):
    idx = pd.date_range("2024-01-01", periods=30, freq="D")
    return pd.DataFrame(
        {
            "date": idx,
            "open": 1.0,
            "high": 1.2,
            "low": 0.9,
            "close": [1.0 + i * 0.01 for i in range(30)],
            "volume": 0.0,  # 指数无成交量
        }
    )


def test_kline_payload_keeps_real_volume():
    from tracker.charting import clean_ohlc, kline_payload

    df = _fake_history("AAPL")
    df["volume"] = 100.0
    payload = kline_payload(df, "AAPL", show_volume=True)
    assert len(payload["volume"]) == len(clean_ohlc(df))


def test_get_index_history_rejects_non_index_symbol():
    with pytest.raises(ValueError, match="不是指数代码"):
        prices.get_index_history("AAPL")


def test_get_index_history_routes_through_index_provider(monkeypatch):
    calls = []

    def fake_get_history(symbol, months=12, start_date=None, end_date=None, **kw):
        calls.append(symbol)
        return _fake_history(symbol)

    monkeypatch.setattr("tracker.prices.get_history", fake_get_history)
    df = prices.get_index_history("IX.DXY", months=6, refresh=True)
    assert calls == ["IX.DXY"]
    assert list(df.columns) == ["date", "open", "high", "low", "close", "volume"]


# ---------- 绘图: 零成交量省略 ----------


def test_kline_payload_omits_all_zero_volume():
    from tracker.charting import kline_payload

    payload = kline_payload(_fake_history("IX.DXY"), "IX.DXY", show_volume=True)
    assert payload["volume"] == []


def test_kline_payload_keeps_real_volume_with_positive_volume():
    from tracker.charting import clean_ohlc, kline_payload

    df = _fake_history("AAPL")
    df["volume"] = 100.0
    payload = kline_payload(df, "AAPL", show_volume=True)
    assert len(payload["volume"]) == len(clean_ohlc(df))
