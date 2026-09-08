import pandas as pd

from tracker.analytics import build_view, summarize
from tracker.prices import Quote


def make_quote(sym, price, ccy="USD", chg=1.0):
    return Quote(
        symbol=sym,
        name=sym,
        price=price,
        prev_close=price / (1 + chg / 100) if chg is not None else None,
        change_pct=chg,
        currency=ccy,
    )


def test_build_view_basic():
    holdings = [{"symbol": "AAPL", "quantity": 10, "avg_cost": 100.0}]
    quotes = {"AAPL": make_quote("AAPL", 150.0)}
    fx = {"USD": 7.0}
    view, issues = build_view(holdings, quotes, fx)
    assert issues == []
    row = view.iloc[0]
    assert row["market_value"] == 150.0 * 10 * 7.0
    assert row["cost"] == 100.0 * 10 * 7.0
    assert row["pnl"] == 50.0 * 10 * 7.0
    assert row["pnl_pct"] == 0.5
    assert row["weight_pct"] == 100.0
    assert row["today_pnl"] > 0


def test_build_view_multi_market_weights():
    holdings = [
        {"symbol": "AAPL", "quantity": 10, "avg_cost": 100.0},
        {"symbol": "600519.SS", "quantity": 100, "avg_cost": 10.0},
    ]
    quotes = {
        "AAPL": make_quote("AAPL", 100.0, ccy="USD"),
        "600519.SS": make_quote("600519.SS", 20.0, ccy="CNY"),
    }
    fx = {"USD": 7.0, "CNY": 1.0}
    view, issues = build_view(holdings, quotes, fx)
    assert issues == []
    assert len(view) == 2
    total = view["market_value"].sum()
    assert total == 100.0 * 10 * 7.0 + 20.0 * 100 * 1.0
    assert abs(view["weight_pct"].sum() - 100.0) < 1e-9
    assert view.iloc[0]["symbol"] == "AAPL"


def test_build_view_zero_cost_keeps_pnl():
    # avg_cost=0 (如 IBKR avgCost=0) 时 pnl 应为全额市值而非缺失
    holdings = [{"symbol": "AAPL", "quantity": 10, "avg_cost": 0.0}]
    quotes = {"AAPL": make_quote("AAPL", 150.0)}
    view, issues = build_view(holdings, quotes, {"USD": 1.0})
    assert issues == []
    row = view.iloc[0]
    assert row["cost"] == 0.0
    assert row["pnl"] == 1500.0
    assert pd.isna(row["pnl_pct"])


def test_build_view_missing_quote_and_bad_symbol():
    holdings = [
        {"symbol": "AAPL", "quantity": 10, "avg_cost": 100.0},
        {"symbol": "NOQUOTE.HK", "quantity": 10, "avg_cost": 10.0},
        {"symbol": "BAD.ZZ", "quantity": 1, "avg_cost": 1.0},
    ]
    quotes = {"AAPL": make_quote("AAPL", 150.0)}
    view, issues = build_view(holdings, quotes, {"USD": 1.0})
    assert len(view) == 1
    assert len(issues) == 2
    assert any("BAD.ZZ" in i for i in issues)
    assert any("NOQUOTE.HK" in i for i in issues)


def test_build_view_missing_cost():
    holdings = [{"symbol": "AAPL", "quantity": 10}]
    quotes = {"AAPL": make_quote("AAPL", 150.0)}
    view, issues = build_view(holdings, quotes, {"USD": 1.0})
    assert issues == []
    row = view.iloc[0]
    assert row["market_value"] == 1500.0
    assert pd.isna(row["pnl"])
    assert pd.isna(row["pnl_pct"])


def test_build_view_missing_fx():
    holdings = [{"symbol": "0700.HK", "quantity": 100, "avg_cost": 300.0}]
    quotes = {"0700.HK": make_quote("0700.HK", 400.0, ccy="HKD")}
    view, issues = build_view(holdings, quotes, {})
    assert view.empty
    assert any("HKD" in i for i in issues)


def test_summarize_partial_cost_coverage():
    # 部分持仓缺成本: total_pnl_pct 只反映有成本部分, cost_coverage 标注口径覆盖
    holdings = [
        {"symbol": "AAPL", "quantity": 10, "avg_cost": 100.0},
        {"symbol": "NOHDR.HK", "quantity": 10},  # 无成本
    ]
    quotes = {
        "AAPL": make_quote("AAPL", 150.0, ccy="USD"),
        "NOHDR.HK": make_quote("NOHDR.HK", 100.0, ccy="HKD"),
    }
    view, _ = build_view(holdings, quotes, {"USD": 1.0, "HKD": 1.0})
    m = summarize(view)
    assert m["total_value"] == 1500.0 + 1000.0
    assert m["total_cost"] == 1000.0
    assert m["total_pnl"] == 500.0
    assert m["total_pnl_pct"] == 0.5
    assert m["cost_coverage"] == 1500.0 / 2500.0


def test_summarize_no_cost_at_all():
    holdings = [{"symbol": "AAPL", "quantity": 10}]
    quotes = {"AAPL": make_quote("AAPL", 150.0)}
    view, _ = build_view(holdings, quotes, {"USD": 1.0})
    m = summarize(view)
    assert m["total_cost"] is None
    assert m["total_pnl"] is None
    assert m["total_pnl_pct"] is None
    assert m["cost_coverage"] == 0.0

def test_summarize():
    holdings = [
        {"symbol": "AAPL", "quantity": 10, "avg_cost": 100.0},
        {"symbol": "SAP.DE", "quantity": 5, "avg_cost": 150.0},
    ]
    quotes = {
        "AAPL": make_quote("AAPL", 150.0, ccy="USD"),
        "SAP.DE": make_quote("SAP.DE", 100.0, ccy="EUR"),
    }
    fx = {"USD": 7.0, "EUR": 7.8}
    view, _ = build_view(holdings, quotes, fx)
    m = summarize(view)
    assert m["total_value"] == 150.0 * 10 * 7.0 + 100.0 * 5 * 7.8
    expected_cost = 100.0 * 10 * 7.0 + 150.0 * 5 * 7.8
    assert m["total_cost"] == expected_cost
    assert abs(m["total_pnl"] - (m["total_value"] - expected_cost)) < 1e-9
    assert m["cost_coverage"] == 1.0
    assert set(m["by_market"].index) == {"美股", "德股"}
    assert set(m["by_currency"].index) == {"USD", "EUR"}


def test_summarize_empty():
    m = summarize(pd.DataFrame())
    assert m["total_value"] == 0.0
    assert m["total_pnl"] is None
    assert m["cost_coverage"] is None
    assert m["by_market"].empty
