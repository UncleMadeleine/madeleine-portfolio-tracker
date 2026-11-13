"""数据源回退顺序: 配置归一 / provider 源链 / 批量前置 / UI 默认展示."""

from __future__ import annotations

import pandas as pd
import pytest

from tracker import eodhd
from tracker import source_order as so
from tracker import storage
from tracker.providers import orchestration as orch
from tracker.providers import global_stocks as gs
from tracker.providers.base import Quote
from tracker.providers.cn_stocks import CNStocksProvider
from tracker.providers.crypto import CryptoProvider
from tracker.providers.index import IndexProvider
from tracker.symbols import parse


@pytest.fixture(autouse=True)
def _clear_order_cache():
    so.clear_cache()
    yield
    so.clear_cache()


def _save_order(order=None, **domains):
    storage.save_settings(
        {"source_order": order if order is not None else domains},
        storage.SETTINGS_PATH,
    )
    so.clear_cache()


def _patch_eodhd(monkeypatch, *, available=True, quotes="fallback", priority="primary"):
    cfg = dict(eodhd.DEFAULTS)
    cfg.update(
        {
            "enabled": available,
            "token": "t" if available else "",
            "quotes": quotes,
            "priority": priority,
            "_path": "/tmp/eodhd.json",
        }
    )
    monkeypatch.setattr(eodhd, "load_config", lambda path=None: dict(cfg))
    monkeypatch.setattr(eodhd, "available", lambda c=None: available)
    return cfg


# ---------------------------------------------------------------------------
# 配置归一
# ---------------------------------------------------------------------------


def test_normalize_aliases_dedupes_and_appends_missing():
    assert so.normalize(
        "global", "quote", ["Yahoo", "gate.io", "AKSHARE", "yfinance", "bogus"]
    ) == ["yfinance", "akshare", "ibkr", "longport", "eodhd"]
    # crypto 只接受本域源; global 的 akshare 不能被塞进 crypto
    assert so.normalize("crypto", "quote", ["gate.io", "GATE", "binance"]) == [
        "gate",
        "binance",
        "hyperliquid",
        "yfinance",
    ]
    assert so.normalize("crypto", "quote", ["akshare"]) is None
    assert so.normalize("global", "quote", []) is None
    assert so.normalize("global", "quote", "yfinance") is None


def test_settings_roundtrip_and_invalid_shape(tmp_path):
    p = tmp_path / "settings.json"
    order = {"global": {"quote": ["yfinance", "eodhd"]}}
    storage.save_settings({"source_order": order}, p)
    assert storage.load_settings(p)["source_order"] == order

    p.write_text('{"source_order": ["not", "a", "dict"]}', encoding="utf-8")
    assert storage.load_settings(p)["source_order"] == {}


def test_configured_reads_settings_and_invalidates_on_rewrite():
    _save_order(order={"global": {"quote": ["yfinance", "eodhd"]}})
    # 未列出的 IBKR/长桥/akshare 自动补到末尾
    assert so.configured("global", "quote") == [
        "yfinance",
        "eodhd",
        "ibkr",
        "longport",
        "akshare",
    ]
    assert so.configured("global", "history") is None

    _save_order(order={"global": {"quote": ["akshare", "yfinance"]}})
    assert so.configured("global", "quote") == [
        "akshare",
        "yfinance",
        "ibkr",
        "longport",
        "eodhd",
    ]


def test_signature_changes_with_order():
    assert so.signature() == ""
    _save_order(crypto={"quote": ["binance", "yfinance"]})
    first = so.signature()
    assert first
    _save_order(crypto={"quote": ["yfinance", "binance"]})
    assert so.signature() != first


def test_display_order_keeps_legacy_prefer_consistent_with_provider():
    _save_order(order={"global": {"quote": ["yfinance", "eodhd"]}})
    order = so.display_order("global", "quote", prefer_akshare=True)
    # 用户没把 IBKR/长桥排在前面时, --akshare 把 akshare 提到最前
    assert order[0] == "akshare"
    assert order.index("akshare") < order.index("eodhd")
    assert order.index("akshare") < order.index("yfinance")

    # 已排在 akshare 前面的 IBKR/长桥保持不动
    _save_order(
        order={"global": {"quote": ["ibkr", "longport", "yfinance", "eodhd"]}}
    )
    order = so.display_order("global", "quote", prefer_akshare=True)
    assert order[:3] == ["ibkr", "longport", "akshare"]

    _save_order(order={"crypto": {"quote": ["binance", "hyperliquid"]}})
    assert so.display_order("crypto", "quote", crypto_source="gate")[0] == "gate"


def test_display_order_defaults_follow_legacy_flags():
    assert so.default_order("global", "quote", eodhd_quotes="fallback") == [
        "ibkr",
        "longport",
        "yfinance",
        "akshare",
        "eodhd",
    ]
    assert so.default_order(
        "global", "history", prefer_akshare=True, eodhd_priority="primary"
    ) == ["ibkr", "longport", "akshare", "eodhd", "yfinance"]
    assert so.default_order("crypto", "quote", crypto_source="gate")[0] == "gate"
    assert so.default_order("index", "history", akshare_supported=False) == ["yfinance"]


# ---------------------------------------------------------------------------
# provider 源链
# ---------------------------------------------------------------------------


def test_global_quote_chain_honours_configured_order(monkeypatch):
    _patch_eodhd(monkeypatch, available=True, quotes="fallback")
    _save_order(
        **{"global": {"quote": ["akshare", "yfinance", "eodhd"]}}
    )
    provider = gs.GlobalStocksProvider()

    us = provider.quote_sources(parse("AAPL"))
    assert us == [gs._yahoo_quote, eodhd.quote]  # akshare 仅港股

    hk = provider.quote_sources(parse("0700.HK"))
    assert hk == [gs._akshare_quote, gs._yahoo_quote, eodhd.quote]


def test_global_quote_chain_appends_unlisted_sources(monkeypatch):
    _patch_eodhd(monkeypatch, available=True, quotes="primary")
    _save_order(**{"global": {"quote": ["yfinance"]}})
    chain = gs.GlobalStocksProvider().quote_sources(parse("AAPL"))
    # 手写只列 yfinance 时, 未列出的 eodhd/akshare 补到末尾 (akshare 仅港股)
    assert chain == [gs._yahoo_quote, eodhd.quote]


def test_global_quote_chain_skips_unavailable_eodhd(monkeypatch):
    _patch_eodhd(monkeypatch, available=False)
    _save_order(**{"global": {"quote": ["eodhd", "yfinance"]}})
    chain = gs.GlobalStocksProvider().quote_sources(parse("AAPL"))
    assert chain == [gs._yahoo_quote]


def test_global_history_chain_order(monkeypatch):
    _patch_eodhd(monkeypatch, available=True, priority="primary")
    calls: list[str] = []
    monkeypatch.setattr(
        eodhd, "history", lambda *a, **k: calls.append("eodhd") or pd.DataFrame()
    )
    monkeypatch.setattr(
        gs, "_yahoo_history", lambda *a, **k: calls.append("yfinance") or pd.DataFrame()
    )
    _save_order(**{"global": {"history": ["yfinance", "eodhd"]}})

    chain = gs.GlobalStocksProvider().history_sources(
        parse("AAPL"), "2025-01-01", None
    )
    for fn in chain:
        fn()
    assert calls == ["yfinance", "eodhd"]


def test_cn_and_crypto_chains_honour_order():
    _save_order(cn={"quote": ["yfinance", "akshare"]})
    assert CNStocksProvider().quote_sources(parse("600519.SS")) == [
        gs._yahoo_quote,
        gs._akshare_quote,
    ]

    _save_order(
        crypto={"quote": ["yfinance", "binance", "gate", "hyperliquid"]}
    )
    crypto = CryptoProvider()
    from tracker.providers import crypto as crypto_mod

    assert crypto.quote_sources(parse("BTC-USD")) == [
        crypto_mod._yf_quote,
        crypto_mod._binance_quote,
        crypto_mod._gate_quote,
        crypto_mod._hl_quote,
    ]


def test_index_history_chain_honours_order():
    _save_order(index={"history": ["yfinance", "akshare"]})
    chain = IndexProvider().history_sources(parse("IX.SSE"), "2025-01-01", None)
    assert [fn.__name__ for fn in chain] == ["yf", "ak"]
    # 没有 akshare 源的指数不能因为配置里写了 akshare 就拿到空链
    assert [
        fn.__name__
        for fn in IndexProvider().history_sources(parse("IX.VIX"), "2025-01-01", None)
    ] == ["yf"]


# ---------------------------------------------------------------------------
# 批量前置层
# ---------------------------------------------------------------------------


def _q(symbol: str, source: str) -> Quote:
    return Quote(
        symbol=symbol,
        name=symbol,
        price=1.0,
        prev_close=1.0,
        change_pct=0.0,
        currency="USD",
        source=source,
    )


def test_cached_quote_must_match_first_ordered_source(monkeypatch):
    _patch_eodhd(monkeypatch, available=False)
    _save_order(**{"global": {"quote": ["yfinance", "ibkr"]}})
    p = parse("AAPL")
    # 排在后面的 IBKR 缓存不能短路排在首位的 yfinance
    assert not orch._cached_source_ok(
        _q("AAPL", "ibkr"), p, False, True, False, "auto"
    )
    assert orch._cached_source_ok(
        _q("AAPL", "yfinance"), p, False, True, False, "auto"
    )

    # IBKR 未启用时跳过它, 顺位到 yfinance
    _save_order(**{"global": {"quote": ["ibkr", "yfinance"]}})
    assert orch._cached_source_ok(
        _q("AAPL", "yfinance"), p, False, False, False, "auto"
    )


def test_eodhd_after_yfinance_runs_after_yfinance_batch(monkeypatch):
    _patch_eodhd(monkeypatch, available=True, quotes="primary")
    _save_order(**{"global": {"quote": ["yfinance", "eodhd"]}})
    calls: list[str] = []

    def fake_eod(parsed):
        calls.append("eod:" + ",".join(p.yahoo for p in parsed))
        return {}, []

    def fake_yahoo(parsed):
        calls.append("yahoo:" + ",".join(p.yahoo for p in parsed))
        return {}

    monkeypatch.setattr(eodhd, "quotes_batch", fake_eod)
    monkeypatch.setattr(orch, "_yahoo_batch", fake_yahoo)
    monkeypatch.setattr(orch.cache_mod, "get_cached", lambda *a, **k: {})
    monkeypatch.setattr(orch.cache_mod, "set_cached", lambda *a, **k: None)

    orch.get_quotes(["AAPL"], refresh=True)
    # yfinance 先跑; EODHD 只为 yfinance 未命中的 leftovers 兜底
    assert calls == ["yahoo:AAPL", "eod:AAPL"]


def test_eodhd_before_yfinance_is_batched(monkeypatch):
    _patch_eodhd(monkeypatch, available=True, quotes="fallback")
    _save_order(**{"global": {"quote": ["eodhd", "yfinance"]}})
    calls: list[str] = []

    def fake_eod(parsed):
        calls.append("eod:" + ",".join(p.yahoo for p in parsed))
        return {"AAPL": _q("AAPL", "eodhd")}, []

    def fake_yahoo(parsed):
        calls.append("yahoo:" + ",".join(p.yahoo for p in parsed))
        return {}

    monkeypatch.setattr(eodhd, "quotes_batch", fake_eod)
    monkeypatch.setattr(orch, "_yahoo_batch", fake_yahoo)
    monkeypatch.setattr(orch.cache_mod, "get_cached", lambda *a, **k: {})
    monkeypatch.setattr(orch.cache_mod, "set_cached", lambda *a, **k: None)

    quotes, _errors, _notes = orch.get_quotes(["AAPL"], refresh=True)
    assert calls == ["eod:AAPL"]  # 已命中, 不再落 yfinance
    assert quotes["AAPL"].source == "eodhd"


def test_akshare_before_yfinance_keeps_hk_out_of_batch(monkeypatch):
    _patch_eodhd(monkeypatch, available=False)
    _save_order(**{"global": {"quote": ["akshare", "yfinance"]}})
    batched: list[str] = []

    def fake_yahoo(parsed):
        batched.extend(p.yahoo for p in parsed)
        return {}

    monkeypatch.setattr(
        gs, "_akshare_quote", lambda p: _q(p.yahoo, "akshare")
    )
    monkeypatch.setattr(orch, "_yahoo_batch", fake_yahoo)
    monkeypatch.setattr(orch.cache_mod, "get_cached", lambda *a, **k: {})
    monkeypatch.setattr(orch.cache_mod, "set_cached", lambda *a, **k: None)

    quotes, _errors, _notes = orch.get_quotes(["0700.HK", "AAPL"], refresh=True)
    # 港股先由 akshare 命中, yfinance 批量只处理剩余美股
    assert batched == ["AAPL"]
    assert quotes["0700.HK"].source == "akshare"


def test_akshare_failure_falls_through_to_yfinance(monkeypatch):
    _patch_eodhd(monkeypatch, available=False)
    _save_order(**{"global": {"quote": ["akshare", "yfinance"]}})
    batched: list[str] = []

    def fake_yahoo(parsed):
        batched.extend(p.yahoo for p in parsed)
        return {p.yahoo: _q(p.yahoo, "yfinance") for p in parsed}

    def boom(_p):
        raise RuntimeError("akshare down")

    monkeypatch.setattr(gs, "_akshare_quote", boom)
    monkeypatch.setattr(orch, "_yahoo_batch", fake_yahoo)
    monkeypatch.setattr(orch.cache_mod, "get_cached", lambda *a, **k: {})
    monkeypatch.setattr(orch.cache_mod, "set_cached", lambda *a, **k: None)

    quotes, _errors, _notes = orch.get_quotes(["0700.HK", "AAPL"], refresh=True)
    assert batched == ["0700.HK", "AAPL"]
    assert quotes["0700.HK"].source == "yfinance"


def test_ibkr_after_yfinance_only_sees_leftovers(monkeypatch):
    _patch_eodhd(monkeypatch, available=False)
    _save_order(**{"global": {"quote": ["yfinance", "ibkr"]}})
    ibkr_calls: list[list[str]] = []

    def fake_yahoo(parsed):
        return {p.yahoo: _q(p.yahoo, "yfinance") for p in parsed if p.yahoo == "AAPL"}

    def fake_ibkr(parsed):
        ibkr_calls.append([p.yahoo for p in parsed])
        return {p.yahoo: _q(p.yahoo, "ibkr") for p in parsed}, None

    monkeypatch.setattr(orch, "_yahoo_batch", fake_yahoo)
    monkeypatch.setattr("tracker.ibkr.get_quotes_ibkr", fake_ibkr)
    monkeypatch.setattr(orch.cache_mod, "get_cached", lambda *a, **k: {})
    monkeypatch.setattr(orch.cache_mod, "set_cached", lambda *a, **k: None)

    quotes, _errors, _notes = orch.get_quotes(
        ["AAPL", "MSFT"], use_ibkr=True, refresh=True
    )
    assert ibkr_calls == [["MSFT"]]
    assert quotes["AAPL"].source == "yfinance"
    assert quotes["MSFT"].source == "ibkr"


def test_longport_before_yfinance_settles_first(monkeypatch):
    _patch_eodhd(monkeypatch, available=False)
    _save_order(**{"global": {"quote": ["longport", "yfinance"]}})
    batched: list[str] = []
    lp_calls: list[list[str]] = []

    def fake_lp(parsed, cfg=None):
        lp_calls.append([p.yahoo for p in parsed])
        return {p.yahoo: _q(p.yahoo, "longport") for p in parsed if p.yahoo == "AAPL"}, None

    def fake_yahoo(parsed):
        batched.extend(p.yahoo for p in parsed)
        return {p.yahoo: _q(p.yahoo, "yfinance") for p in parsed}

    monkeypatch.setattr("tracker.longport.get_quotes_longport", fake_lp)
    monkeypatch.setattr(orch, "_yahoo_batch", fake_yahoo)
    monkeypatch.setattr(orch.cache_mod, "get_cached", lambda *a, **k: {})
    monkeypatch.setattr(orch.cache_mod, "set_cached", lambda *a, **k: None)

    quotes, _errors, _notes = orch.get_quotes(
        ["AAPL", "MSFT"], use_longport=True, refresh=True
    )
    assert lp_calls == [["AAPL", "MSFT"]]
    assert batched == ["MSFT"]
    assert quotes["AAPL"].source == "longport"
    assert quotes["MSFT"].source == "yfinance"


def test_cn_longport_participates_in_order(monkeypatch):
    _save_order(cn={"quote": ["longport", "akshare", "yfinance"]})
    seen: list[list[str]] = []

    def fake_lp(parsed, cfg=None):
        seen.append([p.yahoo for p in parsed])
        return {p.yahoo: _q(p.yahoo, "longport") for p in parsed}, None

    monkeypatch.setattr("tracker.longport.get_quotes_longport", fake_lp)
    monkeypatch.setattr(
        gs,
        "_akshare_quote",
        lambda p: (_ for _ in ()).throw(AssertionError("akshare should be skipped")),
    )
    monkeypatch.setattr(orch.cache_mod, "get_cached", lambda *a, **k: {})
    monkeypatch.setattr(orch.cache_mod, "set_cached", lambda *a, **k: None)

    quotes, _errors, _notes = orch.get_quotes(
        ["600519.SS"], use_longport=True, refresh=True
    )
    assert seen == [["600519.SS"]]
    assert quotes["600519.SS"].source == "longport"


def test_history_ibkr_after_yfinance_is_not_called(monkeypatch):
    _save_order(**{"global": {"history": ["yfinance", "ibkr"]}})
    df = pd.DataFrame(
        [{"date": "2026-01-02", "open": 1, "high": 2, "low": 1, "close": 2}]
    )
    monkeypatch.setattr(gs, "_yahoo_history", lambda *a, **k: df)
    monkeypatch.setattr(
        "tracker.ibkr.get_history_ibkr",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("IBKR should be skipped")),
    )
    out = orch.get_history("AAPL", months=1, use_ibkr=True)
    assert out is df


def test_history_ibkr_before_yfinance_is_used(monkeypatch):
    _save_order(**{"global": {"history": ["ibkr", "yfinance"]}})
    df_ib = pd.DataFrame(
        [{"date": "2026-01-02", "open": 1, "high": 2, "low": 1, "close": 2}]
    )
    monkeypatch.setattr(
        "tracker.ibkr.get_history_ibkr",
        lambda *a, **k: ({"AAPL": df_ib}, None),
    )
    monkeypatch.setattr(
        gs,
        "_yahoo_history",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("yfinance should be skipped")),
    )
    out = orch.get_history("AAPL", months=1, use_ibkr=True)
    assert out is df_ib


# ---------------------------------------------------------------------------
# 设置写入
# ---------------------------------------------------------------------------


def test_drag_component_asset_is_present():
    from tracker.ui import source_order_ui as ui

    html = (ui._COMPONENT_DIR / "index.html").read_text(encoding="utf-8")
    assert "dragstart" in html
    assert "streamlit:setComponentValue" in html
    assert "streamlit:componentReady" in html


def test_drag_component_height_uses_content_not_viewport():
    """回归: 不能用 documentElement.scrollHeight 上报 iframe 高度。

    iframe 的 documentElement 至少等于当前视口高度, 而 Streamlit 会按上报值
    继续放大 iframe, 形成每轮 rerun 都长高的正反馈 (设置页被撑出几万像素空白)。
    """
    from tracker.ui import source_order_ui as ui

    html = (ui._COMPONENT_DIR / "index.html").read_text(encoding="utf-8")
    assert "document.documentElement.scrollHeight" not in html
    assert "getBoundingClientRect().height" in html
    assert "ResizeObserver" in html


def test_store_order_resets_only_relevant_legacy_preference(monkeypatch):
    from tracker.ui import source_order_ui as ui

    monkeypatch.setattr(ui, "_clear_source_caches", lambda: None)
    storage.save_settings(
        {"prefer_akshare": True, "crypto_source": "gate"}, storage.SETTINGS_PATH
    )

    ui._store_order("crypto", "quote", ["yfinance", "binance"])
    settings = storage.load_settings(storage.SETTINGS_PATH)
    assert settings["source_order"]["crypto"]["quote"] == ["yfinance", "binance"]
    # 未编辑的 crypto 历史链按旧偏好固化 (gate 前置), 不能因复位开关而丢失
    assert settings["source_order"]["crypto"]["history"][0] == "gate"
    assert settings["crypto_source"] == "auto"
    # 只编辑 crypto, 不能顺手把 global 的 akshare 偏好重置
    assert settings["prefer_akshare"] is True

    ui._store_order("global", "quote", ["yfinance", "eodhd"])
    settings = storage.load_settings(storage.SETTINGS_PATH)
    assert settings["prefer_akshare"] is False
    # 未编辑的 global 历史链保留 akshare 前置的旧偏好 (IBKR/长桥仍在最前)
    history = settings["source_order"]["global"]["history"]
    assert history[:2] == ["ibkr", "longport"]
    assert history.index("akshare") < history.index("eodhd")

    # 没有旧偏好时不要冻结另一条链, 否则之后改 eodhd.json 角色不会生效
    storage.save_settings(
        {"source_order": {}, "prefer_akshare": False, "crypto_source": "auto"},
        storage.SETTINGS_PATH,
    )
    so.clear_cache()
    ui._store_order("global", "history", ["eodhd", "yfinance"])
    saved = storage.load_settings(storage.SETTINGS_PATH)["source_order"]
    assert "quote" not in saved["global"]
