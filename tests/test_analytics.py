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


# ---------- 加密货币 + 股票混合组合 ----------


def test_build_view_crypto_stock_mixed():
    """BTC-USD 与 AAPL/600519.SS 共存: 各自按当地货币折算进同一 base."""
    holdings = [
        {"symbol": "AAPL", "quantity": 10, "avg_cost": 100.0},
        {"symbol": "600519.SS", "quantity": 100, "avg_cost": 10.0},
        {"symbol": "BTC-USD", "quantity": 0.5, "avg_cost": 30000.0},
    ]
    quotes = {
        "AAPL": make_quote("AAPL", 150.0, ccy="USD"),
        "600519.SS": make_quote("600519.SS", 20.0, ccy="CNY"),
        "BTC-USD": make_quote("BTC-USD", 60000.0, ccy="USD"),
    }
    fx = {"USD": 7.0, "CNY": 1.0}
    view, issues = build_view(holdings, quotes, fx)
    assert issues == []
    assert len(view) == 3
    assert set(view["symbol"]) == {"AAPL", "600519.SS", "BTC-USD"}
    # BTC 行: 0.5 * 60000 * 7.0 = 210000; 成本 0.5 * 30000 * 7.0 = 105000
    btc = view[view["symbol"] == "BTC-USD"].iloc[0]
    assert btc["market_value"] == 210000.0
    assert btc["cost"] == 105000.0
    assert btc["pnl"] == 105000.0
    assert btc["pnl_pct"] == 1.0
    assert btc["market"] == "加密货币"
    assert btc["currency"] == "USD"
    # 权重合计 100%; BTC 按市值排序应排第一 (210000 > 150000 > 2000)
    assert abs(view["weight_pct"].sum() - 100.0) < 1e-9
    assert view.iloc[0]["symbol"] == "BTC-USD"


def test_summarize_crypto_stock_mixed():
    """混合组合汇总: total 覆盖三市场, by_market/by_currency 聚合正确."""
    holdings = [
        {"symbol": "AAPL", "quantity": 10, "avg_cost": 100.0},
        {"symbol": "BTC-USD", "quantity": 0.5, "avg_cost": 30000.0},
    ]
    quotes = {
        "AAPL": make_quote("AAPL", 150.0, ccy="USD"),
        "BTC-USD": make_quote("BTC-USD", 60000.0, ccy="USD"),
    }
    fx = {"USD": 7.0}
    view, issues = build_view(holdings, quotes, fx)
    assert issues == []
    m = summarize(view)
    # AAPL 10*150*7 = 10500; BTC 0.5*60000*7 = 210000
    assert m["total_value"] == 220500.0
    assert m["total_cost"] == (10 * 100 + 0.5 * 30000) * 7.0
    assert m["total_pnl"] == 220500.0 - (10 * 100 + 0.5 * 30000) * 7.0
    assert m["cost_coverage"] == 1.0
    assert m["by_market"]["加密货币"] == 210000.0
    assert m["by_market"]["美股"] == 10500.0
    assert m["by_currency"]["USD"] == 220500.0


def test_build_view_crypto_quote_failure_flags_issue():
    """crypto 行情缺失降级: 该行剔除并产出 issue, 其余持仓不受影响."""
    holdings = [
        {"symbol": "AAPL", "quantity": 10, "avg_cost": 100.0},
        {"symbol": "BTC-USD", "quantity": 0.5, "avg_cost": 30000.0},
    ]
    quotes = {
        "AAPL": make_quote("AAPL", 150.0, ccy="USD"),
        # BTC-USD 缺行情
    }
    fx = {"USD": 7.0}
    view, issues = build_view(holdings, quotes, fx)
    assert view["symbol"].tolist() == ["AAPL"]
    assert issues == ["BTC-USD: 行情缺失"]


def test_build_view_crypto_eur_quote_needs_eur_fx():
    """BTC-EUR 计价: 用 EUR 汇率折算, 缺 EUR 汇率时报缺失并剔除该行."""
    holdings = [{"symbol": "BTC-EUR", "quantity": 1.0, "avg_cost": 50000.0}]
    quotes = {"BTC-EUR": make_quote("BTC-EUR", 55000.0, ccy="EUR")}
    fx = {"USD": 7.0, "EUR": 7.6}
    view, issues = build_view(holdings, quotes, fx)
    assert issues == []
    assert view.iloc[0]["market_value"] == 55000.0 * 7.6
    # 缺 EUR 汇率
    view2, issues2 = build_view(holdings, quotes, {"USD": 7.0})
    assert view2.empty
    assert issues2 == ["BTC-EUR: 缺少 EUR 汇率"]


def test_build_view_no_separator_crypto_input():
    """持仓文件里写 BTCUSD (无连字符) 也归一为 BTC-USD 行."""
    holdings = [{"symbol": "BTCUSD", "quantity": 1.0, "avg_cost": 30000.0}]
    quotes = {"BTC-USD": make_quote("BTC-USD", 60000.0, ccy="USD")}
    fx = {"USD": 7.0}
    view, issues = build_view(holdings, quotes, fx)
    assert issues == []
    assert view.iloc[0]["symbol"] == "BTC-USD"
    assert view.iloc[0]["market"] == "加密货币"
