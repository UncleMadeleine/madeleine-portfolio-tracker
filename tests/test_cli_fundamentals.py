"""fundamentals 子命令: 估值快照 / 历史 / 财报链接 (离线, 不联网)."""

from __future__ import annotations

import json

import pandas as pd
import pytest

import tracker.cli.fundamentals as cf
import tracker.filings as filings
from tracker import cli, valuation


def _run(capsys, *argv):
    cli.main(list(argv))
    return capsys.readouterr()


def _snap(symbol: str = "AAPL") -> valuation.ValuationSnapshot:
    return valuation.ValuationSnapshot(
        symbol=symbol,
        market_label="美股",
        currency="USD",
        pe_ttm=33.15,
        forward_pe=30.77,
        pb=60.72,
        dividend_yield_pct=0.34,
        dividend_yield_5y_avg_pct=0.31,
        payout_ratio_pct=12.7,
        eps_ttm=6.6,
        book_value=7.36,
        market_cap=5.0e12,
        source="yfinance",
        fetched_at=1_700_000_000.0,
    )


def test_json_snapshot(monkeypatch, capsys):
    monkeypatch.setattr(
        cf.valuation, "fetch_snapshot", lambda symbol, refresh=False: _snap(symbol)
    )
    cap = _run(capsys, "fundamentals", "AAPL", "--json")

    payload = json.loads(cap.out)
    row = payload["results"][0]
    assert row["symbol"] == "AAPL"
    assert row["snapshot"]["pe_ttm"] == pytest.approx(33.15)
    assert row["snapshot"]["pb"] == pytest.approx(60.72)
    assert row["snapshot"]["currency"] == "USD"
    assert row["snapshot"]["fetched_at_iso"]


def test_text_snapshot_and_refresh(monkeypatch, capsys):
    seen: dict[str, bool] = {}

    def fake(symbol, refresh=False):
        seen["refresh"] = refresh
        return _snap(symbol)

    monkeypatch.setattr(cf.valuation, "fetch_snapshot", fake)
    cap = _run(capsys, "fundamentals", "AAPL", "--refresh")

    assert "PE(TTM) 33.15" in cap.out
    assert "PB 60.72" in cap.out
    assert "股息率 0.34%" in cap.out
    assert "总市值 5.00万亿 USD" in cap.out
    assert seen["refresh"] is True


def test_history_summary(monkeypatch, capsys):
    monkeypatch.setattr(
        cf.valuation, "fetch_snapshot", lambda symbol, refresh=False: _snap(symbol)
    )
    df = pd.DataFrame(
        {
            "date": pd.date_range("2025-01-01", periods=5, freq="D"),
            "value": [10.0, 11.0, 12.0, 13.0, 14.0],
        }
    )
    monkeypatch.setattr(cf.valuation, "fetch_history", lambda *a, **kw: df)
    cap = _run(
        capsys,
        "fundamentals",
        "AAPL",
        "--history",
        "pe",
        "--months",
        "12",
        "--json",
    )

    hist = json.loads(cap.out)["results"][0]["history"]
    assert hist["metric"] == "pe"
    assert hist["months"] == 12
    assert hist["summary"]["latest"] == pytest.approx(14.0)
    assert hist["points"][0]["date"] == "2025-01-01"


def test_filings_included(monkeypatch, capsys):
    monkeypatch.setattr(
        cf.valuation, "fetch_snapshot", lambda symbol, refresh=False: _snap(symbol)
    )
    monkeypatch.setattr(
        cf.filings,
        "fetch_filings",
        lambda symbol, refresh=False: [
            filings.Filing(
                title="2025 年年度报告",
                date="2026-03-28",
                url="https://www.sec.gov/example",
                kind="年度报告",
                source="SEC EDGAR",
            )
        ],
    )
    cap = _run(capsys, "fundamentals", "AAPL", "--filings", "--json")

    row = json.loads(cap.out)["results"][0]
    assert row["filings"][0]["title"] == "2025 年年度报告"
    assert row["filings"][0]["source"] == "SEC EDGAR"


def test_crypto_rejected(monkeypatch, capsys):
    monkeypatch.setattr(
        cf.valuation,
        "fetch_snapshot",
        lambda *a, **kw: pytest.fail("should not fetch"),
    )
    with pytest.raises(SystemExit) as exc:
        _run(capsys, "fundamentals", "BTC-USD", "--json")

    assert exc.value.code != 0
    payload = json.loads(capsys.readouterr().out)
    assert "估值指标仅支持股票" in payload["results"][0]["error"]


def test_partial_failure_keeps_successful_rows(monkeypatch, capsys):
    calls = []

    def fake(symbol, refresh=False):
        calls.append(symbol)
        if symbol == "BAD":
            raise RuntimeError("取数失败")
        return _snap(symbol)

    monkeypatch.setattr(cf.valuation, "fetch_snapshot", fake)
    cap = _run(capsys, "fundamentals", "BAD", "AAPL", "--json")

    rows = json.loads(cap.out)["results"]
    assert rows[0]["error"] == "取数失败"
    assert rows[1]["snapshot"]["pe_ttm"] == pytest.approx(33.15)
    assert calls == ["BAD", "AAPL"]
