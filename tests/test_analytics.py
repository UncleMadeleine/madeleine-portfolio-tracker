import pandas as pd
import pytest

from tracker.analytics import build_view, by_symbol, summarize
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


def test_build_view_weights_shorts_outweigh_longs():
    """总市值 ≤ 0 (空头压过多头) 时多头权重仍按多头市值归一, 空头行权重 0.

    修复前条件是 total_mv > 0, 空头压过多头时所有行 (含真实多头) 权重
    一律清零, UI 权重列全部显示 0.0%。
    """
    holdings = [
        {"symbol": "LONG", "quantity": 5, "avg_cost": 10.0},  # 市值 +100
        {"symbol": "SHORT", "quantity": -10, "avg_cost": 10.0},  # 市值 -400
    ]
    quotes = {
        "LONG": make_quote("LONG", 20.0),
        "SHORT": make_quote("SHORT", 40.0),
    }
    view, issues = build_view(holdings, quotes, {"USD": 1.0})
    assert issues == []
    assert float(view["market_value"].sum()) == -300.0
    by = view.set_index("symbol")
    assert by.loc["LONG", "weight_pct"] == pytest.approx(100.0)
    assert by.loc["SHORT", "weight_pct"] == pytest.approx(0.0)



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


# ---------- 按代码聚合 (资产配置饼图: Top N + 其他) ----------


def _symbol_view(values: dict[str, float]) -> pd.DataFrame:
    return pd.DataFrame({"symbol": list(values), "market_value": list(values.values())})


def test_by_symbol_top_with_other():
    """25 个代码只留前 20, 尾部 5 个合计为「其他」且恒在末位."""
    view = _symbol_view({f"S{i:02d}": float(100 - i) for i in range(25)})
    out = by_symbol(view, top=20)
    assert list(out.columns) == ["symbol", "market_value"]
    assert len(out) == 21
    # 前 20 名 = 原视图市值前 20 大, 保持降序
    assert out["symbol"].tolist()[:20] == [f"S{i:02d}" for i in range(20)]
    assert out["market_value"].tolist()[:20] == sorted(
        out["market_value"].tolist()[:20], reverse=True
    )
    assert out.iloc[-1]["symbol"] == "其他"
    assert out.iloc[-1]["market_value"] == sum(float(100 - i) for i in range(20, 25))
    # 聚合不重不漏: 总额守恒
    assert out["market_value"].sum() == view["market_value"].sum()


def test_by_symbol_custom_other_label():
    view = _symbol_view({"AAPL": 10.0, "MSFT": 5.0, "NVDA": 1.0})
    out = by_symbol(view, top=2, other_label="其它")
    assert out["symbol"].tolist() == ["AAPL", "MSFT", "其它"]
    assert out.iloc[-1]["market_value"] == 1.0


def test_by_symbol_under_top_no_other():
    """代码数不超过 top 时全量展示, 不产生「其他」行."""
    view = _symbol_view({"AAPL": 100.0, "MSFT": 50.0, "BTC-USD": 25.0})
    out = by_symbol(view)
    assert out["symbol"].tolist() == ["AAPL", "MSFT", "BTC-USD"]
    assert out["market_value"].tolist() == [100.0, 50.0, 25.0]
    assert "其他" not in set(out["symbol"])


def test_by_symbol_duplicate_rows_summed():
    """同一代码多行 (重复持仓) 先求和再参与排名."""
    view = pd.DataFrame(
        {
            "symbol": ["AAPL", "AAPL", "MSFT"],
            "market_value": [60.0, 40.0, 50.0],
        }
    )
    out = by_symbol(view)
    assert out["symbol"].tolist() == ["AAPL", "MSFT"]
    assert out["market_value"].tolist() == [100.0, 50.0]


def test_by_symbol_empty():
    out = by_symbol(pd.DataFrame())
    assert out.empty
    assert list(out.columns) == ["symbol", "market_value"]


def test_by_symbol_from_built_view():
    """接 build_view 的真实输出 (含 weight 等列): 只按 symbol/market_value 聚合."""
    holdings = [
        {"symbol": "AAPL", "quantity": 10, "avg_cost": 100.0},
        {"symbol": "BTC-USD", "quantity": 0.5, "avg_cost": 30000.0},
    ]
    quotes = {
        "AAPL": make_quote("AAPL", 150.0, ccy="USD"),
        "BTC-USD": make_quote("BTC-USD", 60000.0, ccy="USD"),
    }
    view, _ = build_view(holdings, quotes, {"USD": 7.0})
    out = by_symbol(view, top=1)
    assert out["symbol"].tolist() == ["BTC-USD", "其他"]
    assert out["market_value"].tolist() == [210000.0, 10500.0]


def test_build_view_stablecoin_quote_valued_as_usd():
    """稳定币计价的持仓按 USD 等价估值, 不因「缺少 USDT 汇率」被排除出视图。

    修复前 parse 给出 currency=USDT, 汇率链没有该通道 → 整行 skip,
    snapshot 只留一条「缺少 USDT 汇率」, 总市值静默少算。
    """
    holdings = [{"symbol": "BTC-USDT", "quantity": 0.5, "avg_cost": 90000.0}]
    quotes = {"BTC-USDT": make_quote("BTC-USDT", 100000.0, ccy="USD")}
    view, issues = build_view(holdings, quotes, {"USD": 7.1, "CNY": 1.0})
    assert issues == []
    assert len(view) == 1
    row = view.iloc[0]
    assert row["market_value"] == pytest.approx(0.5 * 100000.0 * 7.1)
    assert row["cost"] == pytest.approx(0.5 * 90000.0 * 7.1)
    assert row["currency"] == "USD"


def test_build_view_coin_quote_reports_reason():
    """币本位计价 (ETH-BTC) 无汇率通道: 明说原因而不是笼统的「缺少 BTC 汇率」。"""
    holdings = [{"symbol": "ETH-BTC", "quantity": 10}]
    quotes = {"ETH-BTC": make_quote("ETH-BTC", 0.05, ccy="BTC")}
    view, issues = build_view(holdings, quotes, {"USD": 7.1})
    assert view.empty
    assert any("币本位计价 (BTC)" in i for i in issues)
