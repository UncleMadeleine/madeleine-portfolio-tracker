import json
import types

import pandas as pd

import tracker.ibkr as ibkr_mod
import tracker.prices as prices_mod
import tracker.fx as fx
from tracker.ibkr import (
    ContractSpec,
    contract_spec,
    ibkr_to_yahoo,
    load_config,
    positions_to_rows,
    quote_from_ticker,
)
from tracker.prices import Quote
from tracker.symbols import parse


class FakeTicker:
    def __init__(self, price=None, last=None, close=None, currency="USD"):
        self._price = price
        self.last = last
        self.close = close
        self.contract = types.SimpleNamespace(currency=currency)

    def marketPrice(self):
        return self._price


class FakeContract:
    def __init__(self, symbol, exchange, currency, primaryExchange="", secType="STK"):
        self.symbol = symbol
        self.exchange = exchange
        self.currency = currency
        self.primaryExchange = primaryExchange
        self.secType = secType


class FakePosition:
    def __init__(self, contract, position, avgCost):
        self.contract = contract
        self.position = position
        self.avgCost = avgCost


def test_load_config_merge(tmp_path):
    f = tmp_path / "ibkr.json"
    f.write_text(
        json.dumps({"port": 9999, "exchanges": {"CN": "SHSE"}}), encoding="utf-8"
    )
    cfg = load_config(f)
    assert cfg["port"] == 9999
    assert cfg["host"] == "127.0.0.1"
    assert cfg["exchanges"]["CN"] == "SHSE"
    assert cfg["exchanges"]["US"] == "SMART"


def test_load_config_filters_comment_keys(tmp_path):
    f = tmp_path / "ibkr.json"
    f.write_text(json.dumps({"_comment": "note", "port": 1234}), encoding="utf-8")
    cfg = load_config(f)
    assert "_comment" not in cfg
    assert cfg["port"] == 1234


def test_load_config_env_override(tmp_path, monkeypatch):
    f = tmp_path / "custom.json"
    f.write_text(json.dumps({"port": 7777, "client_id": 99}), encoding="utf-8")
    monkeypatch.setenv("IBKR_CONFIG", str(f))
    cfg = load_config()
    assert cfg["port"] == 7777
    assert cfg["client_id"] == 99
    # 模板/兜底值继承
    assert cfg["host"] == "127.0.0.1"
    assert cfg["exchanges"]["US"] == "SMART"


def test_load_config_falls_back_to_example(tmp_path, monkeypatch):
    # 无真实配置且无 env 时, 回退到仓库中的 ibkr.example.json 模板
    monkeypatch.setattr(ibkr_mod, "DEFAULT_CONFIG", tmp_path / "nope.json")
    monkeypatch.delenv("IBKR_CONFIG", raising=False)
    monkeypatch.delenv("IBKR_MODE", raising=False)
    cfg = load_config()
    assert cfg["host"] == "127.0.0.1"
    assert cfg["mode"] == "paper"
    assert cfg["exchanges"]["CN"] == "SEHK"


def test_mode_port_resolution(tmp_path, monkeypatch):
    # mode=paper -> 4002, mode=live -> 4001; 显式 port 优先于 mode
    monkeypatch.delenv("IBKR_MODE", raising=False)
    f = tmp_path / "ibkr.json"
    f.write_text(json.dumps({"mode": "live"}), encoding="utf-8")
    cfg = load_config(f)
    assert ibkr_mod._mode_port(cfg) == 4001
    f.write_text(json.dumps({"mode": "paper"}), encoding="utf-8")
    assert ibkr_mod._mode_port(load_config(f)) == 4002
    f.write_text(json.dumps({"mode": "live", "port": 7497}), encoding="utf-8")
    assert ibkr_mod._mode_port(load_config(f)) == 7497


def test_mode_env_overrides_file(tmp_path, monkeypatch):
    f = tmp_path / "ibkr.json"
    f.write_text(json.dumps({"mode": "paper"}), encoding="utf-8")
    monkeypatch.setenv("IBKR_MODE", "LIVE")
    cfg = load_config(f)
    assert cfg["mode"] == "live"
    assert ibkr_mod._mode_port(cfg) == 4001


def test_unknown_mode_reports_error(monkeypatch):
    # 非法 mode 不抛异常, 记录不可用原因供上层展示
    monkeypatch.setattr(ibkr_mod, "_client", None)
    monkeypatch.setattr(ibkr_mod, "_unavailable", None)
    ib = ibkr_mod._get_client({"host": "127.0.0.1", "mode": "bogus", "client_id": 1,
                               "connect_timeout": 1, "market_data_type": 3})
    assert ib is None
    assert ibkr_mod._unavailable is not None
    assert "未知 mode" in ibkr_mod._unavailable[0]


def test_contract_spec_all_markets():
    cases = [
        ("AAPL", ContractSpec("AAPL", "SMART", "USD")),
        ("600519.SS", ContractSpec("600519", "SEHK", "CNY", "600519")),
        ("000001.SZ", ContractSpec("000001", "SEHK", "CNY", "000001")),
        ("002594.SZ", ContractSpec("002594", "SEHK", "CNY", "002594")),
        ("900902.SS", ContractSpec("900902", "SHSE", "USD", "900902")),
        ("200012.SZ", ContractSpec("200012", "SZSE", "HKD", "200012")),
        ("0700.HK", ContractSpec("700", "SEHK", "HKD")),
        ("SAP.DE", ContractSpec("SAP", "IBIS", "EUR")),
        ("BP.L", ContractSpec("BP", "LSE", "GBP")),
        ("RY.TO", ContractSpec("RY", "TSE", "CAD")),
        ("X.V", ContractSpec("X", "TSXV", "CAD")),
        ("BHP.AX", ContractSpec("BHP", "ASX", "AUD")),
    ]
    for sym, expected in cases:
        assert contract_spec(parse(sym)) == expected, sym


def test_contract_spec_exchange_override():
    spec = contract_spec(parse("600519.SS"), exchanges={"CN": "SHSE"})
    assert spec == ContractSpec("600519", "SHSE", "CNY", "600519")


def test_quote_from_ticker_normal():
    q = quote_from_ticker("AAPL", FakeTicker(price=150.0, close=100.0))
    assert q.price == 150.0
    assert q.prev_close == 100.0
    assert q.change_pct == 50.0
    assert q.currency == "USD"


def test_quote_from_ticker_gbx_pence():
    q = quote_from_ticker("BP.L", FakeTicker(price=539.7, close=528.0, currency="GBX"))
    assert q.currency == "GBP"
    assert abs(q.price - 5.397) < 1e-9
    assert abs(q.prev_close - 5.28) < 1e-9


def test_quote_from_ticker_no_data():
    assert quote_from_ticker("X", FakeTicker(price=float("nan"))) is None
    assert quote_from_ticker("X", FakeTicker(price=None, last=None, close=None)) is None


def test_quote_from_ticker_fallback_to_last_and_close():
    q = quote_from_ticker("X", FakeTicker(price=float("nan"), last=155.0, close=150.0))
    assert q.price == 155.0
    q2 = quote_from_ticker("X", FakeTicker(price=float("nan"), last=None, close=120.0))
    assert q2.price == 120.0


def test_ibkr_to_yahoo_mappings():
    cases = [
        (("700", "SEHK", "", "HKD"), "0700.HK"),
        (("0005", "SEHK", "", "HKD"), "0005.HK"),
        (("600519", "SEHK", "", "CNY"), "600519.SS"),
        (("000001", "SEHK", "", "CNY"), "000001.SZ"),
        (("688981", "SEHK", "", "CNY"), "688981.SS"),
        (("900902", "SHSE", "", "USD"), "900902.SS"),
        (("920100", "SEHK", "", "CNY"), "920100.BJ"),
        (("430047", "SEHK", "", "CNY"), "430047.BJ"),
        (("200012", "SZSE", "", "HKD"), "200012.SZ"),
        (("SAP", "IBIS", "", "EUR"), "SAP.DE"),
        (("SAP", "FWB", "", "EUR"), "SAP.DE"),
        (("BP", "LSE", "", "GBX"), "BP.L"),
        (("RY", "TSE", "", "CAD"), "RY.TO"),
        (("X", "TSXV", "", "CAD"), "X.V"),
        (("BHP", "ASX", "", "AUD"), "BHP.AX"),
        (("BRK B", "SMART", "", "USD"), "BRK-B"),
        (("AAPL", "SMART", "", "USD"), "AAPL"),
        (("AAPL", "", "NASDAQ", "USD"), "AAPL"),
        (("ZZZ", "FOO", "", "ZZZ"), None),
    ]
    for args, expected in cases:
        assert ibkr_to_yahoo(*args) == expected, args


def test_positions_to_rows():
    positions = [
        FakePosition(FakeContract("AAPL", "SMART", "USD"), 10.0, 150.0),
        FakePosition(FakeContract("700", "SEHK", "HKD"), 100.0, 330.0),
        FakePosition(FakeContract("600519", "SEHK", "CNY"), 5.0, 1400.0),
        FakePosition(FakeContract("900902", "SHSE", "USD"), 200.0, 12.5),
        FakePosition(FakeContract("200012", "SZSE", "HKD"), 300.0, 8.3),
        FakePosition(FakeContract("WEIRD", "XX", "YY"), 1.0, 1.0),
        FakePosition(FakeContract("GONE", "SMART", "USD"), 0.0, 10.0),
    ]
    rows, skipped = positions_to_rows(positions)
    assert rows == [
        {"symbol": "AAPL", "quantity": 10.0, "avg_cost": 150.0},
        {"symbol": "0700.HK", "quantity": 100.0, "avg_cost": 330.0},
        {"symbol": "600519.SS", "quantity": 5.0, "avg_cost": 1400.0},
        {"symbol": "900902.SS", "quantity": 200.0, "avg_cost": 12.5},
        {"symbol": "200012.SZ", "quantity": 300.0, "avg_cost": 8.3},
    ]
    assert len(skipped) == 1 and "WEIRD" in skipped[0]


def _make_quote(sym, price):
    return Quote(symbol=sym, name=sym, price=price, prev_close=price,
                 change_pct=0.0, currency="USD")


def test_get_quotes_ibkr_first_then_fallback(monkeypatch):
    ibkr_result = {"AAPL": _make_quote("AAPL", 333.0)}

    def fake_ibkr(parsed, cfg=None):
        return dict(ibkr_result), None

    def fake_batch(parsed):
        return {p.yahoo: _make_quote(p.yahoo, 1.0) for p in parsed}

    monkeypatch.setattr(ibkr_mod, "get_quotes_ibkr", fake_ibkr)
    monkeypatch.setattr(prices_mod, "_yahoo_batch", fake_batch)
    # 清空缓存避免干扰
    monkeypatch.setattr(prices_mod.cache_mod, "get_cached", lambda syms, ttl=300: {})
    monkeypatch.setattr(prices_mod.cache_mod, "set_cached", lambda q: None)

    def no_retry(p, prefer_akshare=False):
        raise AssertionError("不应触发逐个重试")

    monkeypatch.setattr(prices_mod, "_fetch_quote", no_retry)

    quotes, errors, notes = prices_mod.get_quotes(
        ["AAPL", "NVDA"], use_ibkr=True
    )
    assert quotes["AAPL"].price == 333.0
    assert quotes["NVDA"].price == 1.0
    assert errors == {} and notes == []


def test_get_quotes_ibkr_unavailable_note(monkeypatch):
    def fake_ibkr(parsed, cfg=None):
        return {}, "ConnectionRefusedError: 拒绝"

    def fake_batch(parsed):
        return {p.yahoo: _make_quote(p.yahoo, 2.0) for p in parsed}

    monkeypatch.setattr(ibkr_mod, "get_quotes_ibkr", fake_ibkr)
    monkeypatch.setattr(prices_mod, "_yahoo_batch", fake_batch)
    monkeypatch.setattr(prices_mod.cache_mod, "get_cached", lambda syms, ttl=300: {})
    monkeypatch.setattr(prices_mod.cache_mod, "set_cached", lambda q: None)
    monkeypatch.setattr(
        prices_mod, "_fetch_quote", lambda p, prefer_akshare=False: _make_quote(p.yahoo, 2.0)
    )

    quotes, errors, notes = prices_mod.get_quotes(["AAPL"], use_ibkr=True)
    assert quotes["AAPL"].price == 2.0
    assert len(notes) == 1 and "IBKR 不可用" in notes[0]


def test_get_quotes_ibkr_not_used_by_default(monkeypatch):
    def fake_ibkr(parsed, cfg=None):
        raise AssertionError("未启用 IBKR 时不应调用")

    monkeypatch.setattr(ibkr_mod, "get_quotes_ibkr", fake_ibkr)
    monkeypatch.setattr(
        prices_mod, "_yahoo_batch", lambda parsed: {p.yahoo: _make_quote(p.yahoo, 3.0) for p in parsed}
    )
    monkeypatch.setattr(prices_mod.cache_mod, "get_cached", lambda syms, ttl=300: {})
    monkeypatch.setattr(prices_mod.cache_mod, "set_cached", lambda q: None)
    monkeypatch.setattr(
        prices_mod, "_fetch_quote", lambda p, prefer_akshare=False: _make_quote(p.yahoo, 3.0)
    )
    quotes, errors, notes = prices_mod.get_quotes(["AAPL"])
    assert quotes["AAPL"].price == 3.0
    assert notes == []


def test_get_history_ibkr(monkeypatch):
    import pandas as pd

    ibkr_df = pd.DataFrame({
        "date": pd.to_datetime(["2024-01-02", "2024-01-03"]),
        "open": [100.0, 101.0],
        "high": [102.0, 103.0],
        "low": [99.0, 100.0],
        "close": [101.0, 102.0],
        "volume": [1000.0, 1100.0],
    })

    def fake_ibkr(parsed, months, cfg=None):
        return {p.yahoo: ibkr_df for p in parsed}, None

    monkeypatch.setattr(ibkr_mod, "get_history_ibkr", fake_ibkr)
    monkeypatch.setattr(
        prices_mod, "_yahoo_history",
        lambda p, months: (_ for _ in ()).throw(AssertionError("不应调用 yahoo"))
    )

    df = prices_mod.get_history("AAPL", months=3, use_ibkr=True)
    assert len(df) == 2
    assert float(df["close"].iloc[-1]) == 102.0


def test_get_history_ibkr_fallback(monkeypatch):
    def fake_ibkr(parsed, months, cfg=None):
        return {}, "连接失败"

    yahoo_df = pd.DataFrame({
        "date": pd.to_datetime(["2024-01-02", "2024-01-03"]),
        "open": [100.0, 101.0],
        "high": [102.0, 103.0],
        "low": [99.0, 100.0],
        "close": [101.0, 102.0],
        "volume": [1000.0, 1100.0],
    })

    monkeypatch.setattr(ibkr_mod, "get_history_ibkr", fake_ibkr)
    monkeypatch.setattr(prices_mod, "_yahoo_history", lambda p, months: yahoo_df)

    df = prices_mod.get_history("AAPL", months=3, use_ibkr=True)
    assert len(df) == 2


def test_get_fx_rate_ibkr(monkeypatch):
    monkeypatch.setattr(ibkr_mod, "get_fx_rate_ibkr", lambda src, dst, cfg=None: (7.15, None))
    rate = fx.get_rate("USD", "CNY", use_ibkr=True)
    assert abs(rate - 7.15) < 1e-9


def test_get_fx_rate_ibkr_fallback(monkeypatch):
    monkeypatch.setattr(ibkr_mod, "get_fx_rate_ibkr", lambda src, dst, cfg=None: (None, "连接失败"))
    monkeypatch.setattr(fx, "_cfets_rate", lambda s, d: 7.1)
    rate = fx.get_rate("USD", "CNY", use_ibkr=True)
    assert abs(rate - 7.1) < 1e-9


def test_run_sync_json_writes_portfolio(tmp_path, capsys, monkeypatch):
    # --json 非 dry-run 模式必须实际写入 portfolio 文件
    import tracker.ibkr_sync as sync_mod

    positions = [
        FakePosition(FakeContract("AAPL", "SMART", "USD"), 10.0, 150.0),
    ]
    monkeypatch.setattr(sync_mod, "fetch_positions", lambda cfg: positions)
    monkeypatch.setattr(sync_mod, "load_config", lambda: {})
    p = tmp_path / "p.json"
    p.write_text(json.dumps({"base_currency": "USD", "holdings": []}), encoding="utf-8")
    sync_mod.run_sync(
        types.SimpleNamespace(dry_run=False, json=True, portfolio=str(p))
    )
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["written"] is True
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["base_currency"] == "USD"
    assert data["holdings"][0]["symbol"] == "AAPL"


def test_run_sync_json_dry_run_no_write(tmp_path, capsys, monkeypatch):
    import tracker.ibkr_sync as sync_mod

    positions = [
        FakePosition(FakeContract("AAPL", "SMART", "USD"), 10.0, 150.0),
    ]
    monkeypatch.setattr(sync_mod, "fetch_positions", lambda cfg: positions)
    monkeypatch.setattr(sync_mod, "load_config", lambda: {})
    p = tmp_path / "p.json"
    sync_mod.run_sync(
        types.SimpleNamespace(dry_run=True, json=True, portfolio=str(p))
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["written"] is False
    assert not p.exists()
