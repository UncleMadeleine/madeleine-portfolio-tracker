"""EODHD BYOK provider 单测: 配置 / 映射 / 行情与历史口径 / 源链接入.

不联网: HTTP 层用假响应或直接替换 `_http_json`; 真实连通性由
`tracker eodhd test` 手动 smoke 覆盖。
"""

from __future__ import annotations

import json
import os
import stat

import pandas as pd
import pytest

from tracker import eodhd
from tracker.providers import global_stocks as gs
from tracker.providers import orchestration as orch
from tracker.symbols import parse


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (eodhd.TOKEN_ENV, *eodhd.TOKEN_ENV_ALIASES, eodhd.CONFIG_ENV):
        monkeypatch.delenv(name, raising=False)
    eodhd.reset()
    yield
    eodhd.reset()


def _cfg_file(tmp_path, **overrides) -> object:
    data = {
        "token": "test-token-123456",
        "enabled": True,
        "priority": "primary",
        "quotes": "fallback",
        "adjustment": "adjusted",
        "lse_pence": True,
        "lse_gbp_symbols": [],
    }
    data.update(overrides)
    p = tmp_path / "eodhd.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# 代码映射
# ---------------------------------------------------------------------------


def test_yahoo_to_eodhd_mapping():
    cases = {
        "AAPL": "AAPL.US",
        "BRK-B": "BRK-B.US",
        "0700.HK": "0700.HK",
        "SAP.DE": "SAP.XETRA",
        "SAP.F": "SAP.F",
        "SAP.DU": "SAP.DU",
        "SAP.HM": "SAP.HA",
        "SAP.MU": "SAP.MU",
        "BP.L": "BP.LSE",
        "BP.AL": "BP.LSE",
        "KAP.IL": "KAP.LSE",
        "RY.TO": "RY.TO",
        "GOLD.V": "GOLD.V",
        "BHP.AX": "BHP.AU",
        "D05.SI": "D05.SG",
        "9984.T": "9984.TSE",
        "005930.KS": "005930.KO",
        "035720.KQ": "035720.KQ",
        "PETR4.SA": "PETR4.SA",
    }
    for yahoo, expected in cases.items():
        assert eodhd.yahoo_to_eodhd(yahoo) == expected, yahoo


def test_yahoo_to_eodhd_unsupported_domains_and_suffixes():
    for sym in ("600519.SS", "000001.SZ", "830799.BJ", "BTC-USD", "IX.DXY", "XXXX.ZZ"):
        assert eodhd.yahoo_to_eodhd(sym) is None, sym


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


def test_config_defaults_when_file_missing(tmp_path):
    cfg = eodhd.load_config(tmp_path / "nope.json")
    assert cfg["enabled"] is True
    assert cfg["token"] == ""
    assert cfg["priority"] == "primary"
    assert cfg["quotes"] == "fallback"
    assert cfg["adjustment"] == "adjusted"
    assert cfg["lse_pence"] is True
    assert eodhd.available(cfg) is False
    assert "token" in eodhd.unavailable_reason(cfg)


def test_config_env_token_wins(tmp_path, monkeypatch):
    p = _cfg_file(tmp_path, token="file-token")
    monkeypatch.setenv(eodhd.TOKEN_ENV, "env-token")
    cfg = eodhd.load_config(p)
    assert cfg["token"] == "env-token"
    assert cfg["_token_source"] == "env"
    assert eodhd.available(cfg)


def test_config_env_alias_and_validation(tmp_path, monkeypatch):
    p = _cfg_file(
        tmp_path,
        enabled="false",
        quotes="nonsense",
        priority="nonsense",
        adjustment="nonsense",
        timeout=999,
        lse_gbp_symbols="VUSA.L, VWRL.L",
        currency_overrides={"kap.il": "usd"},
    )
    monkeypatch.setenv("EODHD_API_TOKEN", "alias-token")
    cfg = eodhd.load_config(p)
    assert cfg["token"] == "alias-token"
    assert cfg["enabled"] is False
    assert cfg["quotes"] == "fallback"
    assert cfg["priority"] == "primary"
    assert cfg["adjustment"] == "adjusted"
    assert cfg["timeout"] == 60.0
    assert cfg["lse_gbp_symbols"] == ["VUSA.L", "VWRL.L"]
    assert cfg["currency_overrides"] == {"KAP.IL": "USD"}
    assert eodhd.available(cfg) is False
    assert "enabled=false" in eodhd.unavailable_reason(cfg)


def test_config_corrupt_file_falls_back_to_defaults(tmp_path):
    p = tmp_path / "eodhd.json"
    p.write_text("{ broken", encoding="utf-8")
    cfg = eodhd.load_config(p)
    assert cfg["token"] == ""
    assert eodhd.available(cfg) is False


def test_set_token_and_enabled_preserve_other_keys(tmp_path):
    p = _cfg_file(tmp_path, token="old-token", priority="fallback")
    eodhd.set_token("new-token", p)
    eodhd.set_enabled(False, p)
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["token"] == "new-token"
    assert data["enabled"] is False
    assert data["priority"] == "fallback"
    # token 文件只允许属主读写
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


def test_set_enabled_creates_file_from_template(tmp_path):
    p = tmp_path / "eodhd.json"
    eodhd.set_enabled(False, p)
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["enabled"] is False
    assert data["token"] == ""
    assert data["priority"] == "primary"
    assert "_说明" in data
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


def test_status_masks_token(tmp_path):
    p = _cfg_file(tmp_path, token="abcdefghijklmnop")
    info = eodhd.status(p)
    assert info["available"] is True
    assert info["token_present"] is True
    assert info["token_masked"] == "abcd…mnop"
    assert "abcdefghijklmnop" not in json.dumps(info, ensure_ascii=False)


def test_cache_signature_changes_with_modes_and_overrides(tmp_path):
    p = _cfg_file(tmp_path)
    assert eodhd.cache_signature(eodhd.load_config(p)).startswith("|eodhd=")
    base = eodhd.cache_signature(eodhd.load_config(p))
    p2 = _cfg_file(tmp_path, adjustment="raw")
    assert eodhd.cache_signature(eodhd.load_config(p2)) != base
    p3 = _cfg_file(tmp_path, lse_gbp_symbols=["VUSA.L"])
    assert eodhd.cache_signature(eodhd.load_config(p3)) != base
    p4 = _cfg_file(tmp_path, token="")
    assert eodhd.cache_signature(eodhd.load_config(p4)) == ""


# ---------------------------------------------------------------------------
# 行情
# ---------------------------------------------------------------------------


def _mock_json(monkeypatch, payload):
    calls = []

    def fake(path, params, cfg):
        calls.append((path, params, cfg))
        return payload

    monkeypatch.setattr(eodhd, "_http_json", fake)
    return calls


def test_quote_parses_realtime_fields(tmp_path, monkeypatch):
    p = _cfg_file(tmp_path)
    calls = _mock_json(
        monkeypatch,
        {
            "code": "AAPL.US",
            "close": 100.63,
            "open": 100.0,
            "previousClose": 100.0,
            "change_p": 0.63,
        },
    )
    q = eodhd.quote("AAPL", p)
    assert q.symbol == "AAPL"
    assert q.price == 100.63
    assert q.prev_close == 100.0
    assert q.change_pct == 0.63  # EODHD 已是百分数, 不能 ×100
    assert q.currency == "USD"
    assert q.source == "eodhd"
    assert calls[0][0] == "real-time/AAPL.US"


def test_quote_lse_pence_and_il_exemption(tmp_path, monkeypatch):
    p = _cfg_file(tmp_path)
    _mock_json(monkeypatch, {"close": 612.7, "previousClose": 611.9, "change_p": 0.13})
    q = eodhd.quote("BP.L", p)
    assert q.price == pytest.approx(6.127)
    assert q.prev_close == pytest.approx(6.119)
    assert q.currency == "GBP"

    q2 = eodhd.quote("KAP.IL", p)
    assert q2.price == 612.7  # .IL 不做便士折算
    assert q2.currency == "GBP"  # 默认按 parse 的 GB 币种; 可用 currency_overrides 覆盖


def test_quote_lse_gbp_symbol_exemption(tmp_path, monkeypatch):
    p = _cfg_file(tmp_path, lse_gbp_symbols=["VUSA.L"])
    _mock_json(monkeypatch, {"close": 82.5, "previousClose": 82.0})
    q = eodhd.quote("VUSA.L", p)
    assert q.price == 82.5
    assert q.prev_close == 82.0


def test_quote_currency_override_also_exempts_pence(tmp_path, monkeypatch):
    p = _cfg_file(tmp_path, currency_overrides={"KAP.IL": "USD", "VUSA.L": "GBP"})
    _mock_json(monkeypatch, {"close": 82.5, "previousClose": 82.0})
    q = eodhd.quote("VUSA.L", p)
    assert q.price == 82.5
    assert q.currency == "GBP"
    q2 = eodhd.quote("KAP.IL", p)
    assert q2.currency == "USD"


def test_quote_invalid_price_raises(tmp_path, monkeypatch):
    p = _cfg_file(tmp_path)
    _mock_json(monkeypatch, {"close": 0, "open": 0, "previousClose": 0})
    with pytest.raises(RuntimeError, match="无有效价格"):
        eodhd.quote("AAPL", p)


def test_quote_negative_change_percent_preserved(tmp_path, monkeypatch):
    p = _cfg_file(tmp_path)
    _mock_json(monkeypatch, {"close": 98.5, "previousClose": 100.0, "change_p": -1.5})
    q = eodhd.quote("AAPL", p)
    assert q.change_pct == -1.5


def test_quote_unavailable_and_unsupported(tmp_path):
    p = _cfg_file(tmp_path, token="")
    with pytest.raises(RuntimeError, match="不可用"):
        eodhd.quote("AAPL", p)
    p2 = _cfg_file(tmp_path, token="t")
    with pytest.raises(RuntimeError, match="不支持该交易所"):
        eodhd.quote("600519.SS", p2)


def test_http_error_mapping_scrubs_token(tmp_path, monkeypatch):
    p = _cfg_file(tmp_path, token="super-secret-token")

    class Resp:
        status_code = 401
        text = "Unauthenticated"

        def json(self):
            return {}

    import requests

    def fake_get(url, params=None, timeout=None):
        assert params["api_token"] == "super-secret-token"
        raise requests.ConnectionError(f"boom {url}?api_token=super-secret-token")

    monkeypatch.setattr(requests, "get", fake_get)
    with pytest.raises(RuntimeError) as ei:
        eodhd._http_json("real-time/AAPL.US", {"fmt": "json"}, eodhd.load_config(p))
    msg = str(ei.value)
    assert "super-secret-token" not in msg
    assert "***" in msg

    def fake_get_401(url, params=None, timeout=None):
        return Resp()

    monkeypatch.setattr(requests, "get", fake_get_401)
    with pytest.raises(RuntimeError, match="认证失败"):
        eodhd._http_json("real-time/AAPL.US", {"fmt": "json"}, eodhd.load_config(p))


# ---------------------------------------------------------------------------
# 历史
# ---------------------------------------------------------------------------


def test_history_adjusted_scales_ohlc_and_keeps_order(tmp_path, monkeypatch):
    p = _cfg_file(tmp_path)
    _mock_json(
        monkeypatch,
        [
            {
                "date": "2024-01-02",
                "open": 100.0,
                "high": 110.0,
                "low": 90.0,
                "close": 100.0,
                "adjusted_close": 50.0,
                "volume": 1000,
            },
            {
                "date": "2024-01-03",
                "open": 102.0,
                "high": 112.0,
                "low": 92.0,
                "close": 104.0,
                "adjusted_close": 52.0,
                "volume": 1100,
            },
        ],
    )
    df = eodhd.history("AAPL", "2024-01-01", "2024-01-05", p)
    assert list(df.columns) == ["date", "open", "high", "low", "close", "volume"]
    assert df["close"].tolist() == [50.0, 52.0]
    assert df["open"].tolist() == [50.0, 51.0]
    assert df["high"].tolist() == [55.0, 56.0]
    assert df["low"].tolist() == [45.0, 46.0]
    assert (df["high"] >= df[["open", "close"]].max(axis=1)).all()
    assert (df["low"] <= df[["open", "close"]].min(axis=1)).all()


def test_history_missing_adjusted_falls_back_to_raw_close(tmp_path, monkeypatch):
    p = _cfg_file(tmp_path)
    _mock_json(
        monkeypatch,
        [
            {
                "date": "2024-01-02",
                "open": 100.0,
                "high": 110.0,
                "low": 90.0,
                "close": 100.0,
                "adjusted_close": None,
                "volume": 1000,
            }
        ],
    )
    df = eodhd.history("AAPL", "2024-01-01", "2024-01-05", p)
    assert df["close"].iloc[0] == 100.0
    assert df["open"].iloc[0] == 100.0


def test_history_raw_mode_and_pence(tmp_path, monkeypatch):
    p = _cfg_file(tmp_path, adjustment="raw")
    _mock_json(
        monkeypatch,
        [
            {
                "date": "2024-01-02",
                "open": 610.0,
                "high": 620.0,
                "low": 600.0,
                "close": 612.7,
                "adjusted_close": 300.0,
                "volume": 10,
            }
        ],
    )
    df = eodhd.history("BP.L", "2024-01-01", "2024-01-05", p)
    assert df["close"].iloc[0] == pytest.approx(6.127)
    assert df["open"].iloc[0] == pytest.approx(6.10)


def test_history_empty_raises(tmp_path, monkeypatch):
    p = _cfg_file(tmp_path)
    _mock_json(monkeypatch, [])
    with pytest.raises(RuntimeError, match="无历史数据"):
        eodhd.history("AAPL", "2024-01-01", "2024-01-05", p)


def test_history_error_object_raises(tmp_path, monkeypatch):
    p = _cfg_file(tmp_path)
    _mock_json(monkeypatch, {"error": "Ticker Not Found"})
    with pytest.raises(RuntimeError, match="Ticker Not Found"):
        eodhd.history("AAPL", "2024-01-01", "2024-01-05", p)


# ---------------------------------------------------------------------------
# 源链接入
# ---------------------------------------------------------------------------


def _patched_cfg(monkeypatch, **overrides):
    cfg = dict(eodhd.DEFAULTS)
    cfg.update(
        {
            "token": "t",
            "enabled": True,
            "priority": "primary",
            "quotes": "fallback",
            "adjustment": "adjusted",
            "lse_pence": True,
            "lse_gbp_symbols": [],
            "currency_overrides": {},
            "_path": "/tmp/eodhd.json",
            "_token_source": "config",
        }
    )
    cfg.update(overrides)
    monkeypatch.setattr(eodhd, "load_config", lambda path=None: cfg)
    return cfg


def test_quote_chain_primary_and_fallback(monkeypatch):
    _patched_cfg(monkeypatch, quotes="primary")
    chain = gs.GlobalStocksProvider().quote_sources(parse("AAPL"))
    assert chain[0] is eodhd.quote
    assert chain[1] is gs._yahoo_quote

    _patched_cfg(monkeypatch, quotes="primary", priority="fallback")
    chain = gs.GlobalStocksProvider().quote_sources(parse("0700.HK"), prefer_first=True)
    assert chain[0] is gs._akshare_quote
    assert chain[1] is eodhd.quote

    _patched_cfg(monkeypatch, quotes="fallback")
    chain = gs.GlobalStocksProvider().quote_sources(parse("AAPL"))
    assert chain[-1] is eodhd.quote
    assert chain[0] is gs._yahoo_quote

    _patched_cfg(monkeypatch, quotes="off")
    chain = gs.GlobalStocksProvider().quote_sources(parse("AAPL"))
    assert eodhd.quote not in chain


def test_history_chain_primary_and_fallback(monkeypatch):
    sentinel = pd.DataFrame({"date": [], "close": []})
    monkeypatch.setattr(eodhd, "history", lambda *a, **k: sentinel)
    _patched_cfg(monkeypatch, priority="primary")
    chain = gs.GlobalStocksProvider().history_sources(parse("AAPL"), "2024-01-01", None)
    assert chain[0]() is sentinel

    _patched_cfg(monkeypatch, priority="fallback")
    chain = gs.GlobalStocksProvider().history_sources(parse("AAPL"), "2024-01-01", None)
    assert chain[-1]() is sentinel


def test_quote_primary_uses_eodhd_batch_before_yahoo(monkeypatch):
    from tracker.providers.base import Quote

    called = {"batch": False}

    def fake_batch(parsed):
        called["batch"] = True
        return {}

    monkeypatch.setattr(orch, "_yahoo_batch", fake_batch)
    monkeypatch.setattr(orch, "_fetch_domain", lambda *a, **k: {})
    monkeypatch.setattr(eodhd, "quote_primary", lambda cfg=None: True)
    monkeypatch.setattr(
        eodhd,
        "quotes_batch",
        lambda parsed, path=None: (
            {"AAPL": Quote("AAPL", "Apple", 100.0, 99.0, 1.0, "USD", source="eodhd")},
            [],
        ),
    )
    quotes, errors, notes = orch.get_quotes(["AAPL"], refresh=True)
    assert quotes["AAPL"].source == "eodhd"
    assert called["batch"] is False


def test_quote_primary_yahoo_batch_falls_back_for_leftovers(monkeypatch):
    called = {"batch": False}

    def fake_batch(parsed):
        called["batch"] = True
        return {}

    monkeypatch.setattr(orch, "_yahoo_batch", fake_batch)
    monkeypatch.setattr(orch, "_fetch_domain", lambda *a, **k: {})
    monkeypatch.setattr(eodhd, "quote_primary", lambda cfg=None: True)
    monkeypatch.setattr(
        eodhd, "quotes_batch", lambda parsed, path=None: ({}, ["无结果"])
    )
    quotes, errors, notes = orch.get_quotes(["AAPL"], refresh=True)
    assert quotes == {}
    assert called["batch"] is True
    assert any("EODHD" in n for n in notes)


# ---------------------------------------------------------------------------
# 批量行情
# ---------------------------------------------------------------------------


def test_quotes_batch_chunks_and_maps(tmp_path, monkeypatch):
    p = _cfg_file(tmp_path)
    calls = []

    def fake(path, params, cfg):
        calls.append((path, dict(params)))
        chunk = [path.rsplit("/", 1)[-1]]
        if params.get("s"):
            chunk += params["s"].split(",")
        return [
            {"code": code, "close": 10.0, "previousClose": 9.0, "change_p": 11.11}
            for code in chunk
        ]

    monkeypatch.setattr(eodhd, "_http_json", fake)
    monkeypatch.setattr(eodhd.time, "sleep", lambda *_: None)
    syms = [
        "AAPL",
        "MSFT",
        "NVDA",
        "AMZN",
        "GOOGL",
        "META",
        "TSLA",
        "AVGO",
        "BRK-B",
        "JPM",
        "UNH",
        "XOM",
        "WMT",
        "LLY",
        "ORCL",
        "COST",
    ]
    out, errors = eodhd.quotes_batch([parse(s) for s in syms], p)
    assert errors == []
    assert len(out) == 16
    assert len(calls) == 2
    assert calls[0][0] == "real-time/AAPL.US"
    assert len(calls[0][1]["s"].split(",")) == 14
    assert calls[1][0] == "real-time/COST.US"
    assert "s" not in calls[1][1]
    assert out["BRK-B"].price == 10.0
    assert out["BRK-B"].source == "eodhd"


def test_quotes_batch_skips_unsupported_domains(tmp_path, monkeypatch):
    p = _cfg_file(tmp_path)
    calls = []
    monkeypatch.setattr(eodhd, "_http_json", lambda *a, **k: calls.append(a) or [])
    out, errors = eodhd.quotes_batch([parse("600519.SS"), parse("BTC-USD")], p)
    assert out == {}
    assert errors == []
    assert calls == []


def test_quotes_batch_stops_after_two_failures(tmp_path, monkeypatch):
    p = _cfg_file(tmp_path)
    calls = []

    def boom(path, params, cfg):
        calls.append(path)
        raise RuntimeError("EODHD 认证失败 (401)")

    monkeypatch.setattr(eodhd, "_http_json", boom)
    monkeypatch.setattr(eodhd.time, "sleep", lambda *_: None)
    syms = [f"T{i}" for i in range(32)]
    out, errors = eodhd.quotes_batch([parse(s) for s in syms], p)
    assert out == {}
    assert len(calls) == 2
    assert len(errors) == 2


# ---------------------------------------------------------------------------
# 基本面 (PB / 市值)
# ---------------------------------------------------------------------------


def test_eodhd_fundamentals_parses_sections(tmp_path, monkeypatch):
    p = _cfg_file(tmp_path)
    calls = _mock_json(
        monkeypatch,
        {
            "Highlights": {"BookValue": 7.36, "MarketCapitalization": 5.0e12},
            "General": {"CurrencyCode": "USD", "Name": "Apple Inc."},
        },
    )
    f = eodhd.fundamentals("AAPL", p)
    assert f["book_value"] == 7.36
    assert f["market_cap"] == 5.0e12
    assert f["currency"] == "USD"
    assert f["name"] == "Apple Inc."
    assert calls[0][0] == "v1.1/fundamentals/AAPL.US"
    assert calls[0][1]["filter"] == "Highlights,General"


def test_eodhd_fundamentals_missing_fields_are_none(tmp_path, monkeypatch):
    p = _cfg_file(tmp_path)
    _mock_json(monkeypatch, {"Highlights": {}, "General": {}})
    f = eodhd.fundamentals("AAPL", p)
    assert f["book_value"] is None
    assert f["market_cap"] is None


def test_fundamentals_primary_uses_eodhd_before_openbb(monkeypatch):
    import tracker.fundamentals as f

    called = {"ob": False}

    def fake_ob(symbols):
        called["ob"] = True
        return {s: {"book_value": 99.0, "market_cap": 99.0} for s in symbols}

    monkeypatch.setattr(eodhd, "fundamentals_mode", lambda cfg=None: "primary")
    monkeypatch.setattr(eodhd, "yahoo_to_eodhd", lambda s: "AAPL.US")
    monkeypatch.setattr(
        eodhd,
        "fundamentals",
        lambda s, path=None: {"book_value": 7.36, "market_cap": 5.0e12},
    )
    monkeypatch.setattr(f, "_fund_metrics", fake_ob)
    assert f.fetch_book_values(["AAPL"])["AAPL"] == 7.36
    assert f.fetch_market_caps(["AAPL"])["AAPL"] == 5.0e12
    assert called["ob"] is False


def test_fundamentals_fallback_only_fills_missing_fields(monkeypatch):
    import tracker.fundamentals as f

    monkeypatch.setattr(eodhd, "fundamentals_mode", lambda cfg=None: "fallback")
    monkeypatch.setattr(eodhd, "yahoo_to_eodhd", lambda s: "AAPL.US")
    monkeypatch.setattr(
        eodhd,
        "fundamentals",
        lambda s, path=None: {"book_value": 7.36, "market_cap": 5.0e12},
    )
    monkeypatch.setattr(
        f,
        "_fund_metrics",
        lambda symbols: {s: {"book_value": 10.0, "market_cap": None} for s in symbols},
    )
    assert f.fetch_book_values(["AAPL"])["AAPL"] == 10.0  # yfinance 已有, 保留
    assert f.fetch_market_caps(["AAPL"])["AAPL"] == 5.0e12  # 缺失字段由 EODHD 补


def test_fundamentals_off_does_not_call_eodhd(monkeypatch):
    import tracker.fundamentals as f

    called = {"eodhd": False}

    def fake_eodhd(s, path=None):
        called["eodhd"] = True
        raise AssertionError("should not be called")

    monkeypatch.setattr(eodhd, "fundamentals_mode", lambda cfg=None: "off")
    monkeypatch.setattr(eodhd, "fundamentals", fake_eodhd)
    monkeypatch.setattr(
        f,
        "_fund_metrics",
        lambda symbols: {s: {"book_value": 1.0, "market_cap": 2.0} for s in symbols},
    )
    assert f.fetch_book_values(["AAPL"])["AAPL"] == 1.0
    assert called["eodhd"] is False


# ---------------------------------------------------------------------------
# CLI / 模板 / 自检
# ---------------------------------------------------------------------------


def test_cli_status_json_masks_token(tmp_path, capsys):
    from tracker import cli

    p = _cfg_file(tmp_path, token="abcdefghijklmnop")
    cli.main(["eodhd", "status", "--config", str(p), "--json"])
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["available"] is True
    assert payload["token_masked"] == "abcd…mnop"
    assert "abcdefghijklmnop" not in out


def test_cli_test_unavailable_reports_reason(tmp_path, capsys):
    from tracker import cli

    p = _cfg_file(tmp_path, token="")
    with pytest.raises(SystemExit) as ei:
        cli.main(["eodhd", "test", "AAPL", "--config", str(p), "--json"])
    assert ei.value.code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["results"][0]["available"] is False


def test_init_includes_eodhd_template():
    from tracker.cli.init import _TEMPLATES

    assert _TEMPLATES["eodhd"] == "eodhd.example.json"


def test_init_creates_eodhd_config_with_owner_only_mode(tmp_path, monkeypatch):
    from tracker import cli

    home = tmp_path / "home"
    monkeypatch.setenv("TRACKER_HOME", str(home))
    cli.main(["init"])
    p = home / "eodhd.json"
    assert p.exists()
    assert json.loads(p.read_text(encoding="utf-8"))["token"] == ""
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


def test_probe_unavailable_does_not_network(tmp_path):
    p = _cfg_file(tmp_path, token="")
    result = eodhd.probe("AAPL", p)
    assert result["available"] is False
    assert result["errors"]
