import json
import types

import pytest

import tracker.ibkr as ibkr_mod
import tracker.prices as prices_mod
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


def test_contract_spec_all_markets():
    cases = [
        ("AAPL", ContractSpec("AAPL", "SMART", "USD")),
        ("600519.SS", ContractSpec("600519", "SEHK", "CNY", "600519")),
        ("000001.SZ", ContractSpec("000001", "SEHK", "CNY", "000001")),
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
        FakePosition(FakeContract("WEIRD", "XX", "YY"), 1.0, 1.0),
        FakePosition(FakeContract("GONE", "SMART", "USD"), 0.0, 10.0),
    ]
    rows, skipped = positions_to_rows(positions)
    assert rows == [
        {"symbol": "AAPL", "quantity": 10.0, "avg_cost": 150.0},
        {"symbol": "0700.HK", "quantity": 100.0, "avg_cost": 330.0},
        {"symbol": "600519.SS", "quantity": 5.0, "avg_cost": 1400.0},
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
