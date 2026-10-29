"""稳定币计价按 USD 等价估值 (symbols.valuation_currency 单一权威点).

背景: 汇率链 (CFETS + Yahoo 法币对) 没有 USDT→X 通道。修复前
`parse("BTC-USDT").currency == "USDT"`, snapshot 把 USDT 丢给 get_fx_rates
→ missing → analytics.build_view 整行 skip, 用户只看到一条「缺少 USDT 汇率」,
总市值/权重/盈亏静默少算。

修复后三级归一 ( belt & braces ):
  1. symbols.parse: 稳定币计价的 ParsedSymbol.currency = USD (主路径);
  2. providers.crypto._quote_ccy: yfinance 自报 currency=USDT 也归一
     (磁盘缓存里的旧 Quote / 数据源自报不会绕过);
  3. fx.get_rate / get_fx_history: 稳定币按 USD 等价查表 (base_currency 是
     稳定币、CLI `tracker fx USDT CNY` 都覆盖)。
"""

import pandas as pd
import pytest

from tracker import fx
from tracker.analytics import build_view
from tracker.prices import Quote
from tracker.services.snapshot import take_snapshot
from tracker.symbols import parse, valuation_currency

# 1 USD = 7.1 CNY (方向感知的 CFETS mock, 表语义 = 1 单位货币兑 CNY)
_TABLE = {"USD": 7.1, "CNY": 1.0}


@pytest.fixture
def cfets(monkeypatch):
    monkeypatch.setattr(
        fx,
        "_cfets_rate",
        lambda s, d: _TABLE[s] / _TABLE[d] if s in _TABLE and d in _TABLE else None,
    )


class TestValuationCurrency:
    def test_stablecoins_map_to_usd(self):
        for ccy in ("USDT", "USDC", "FDUSD", "BUSD", "TUSD", "DAI"):
            assert valuation_currency(ccy) == "USD"
            assert valuation_currency(ccy.lower()) == "USD"

    def test_fiat_and_coin_untouched(self):
        for ccy in ("USD", "EUR", "CNY", "HKD", "BTC", "ETH", "BNB"):
            assert valuation_currency(ccy) == ccy

    def test_empty(self):
        assert valuation_currency("") == ""
        assert valuation_currency(None) == ""


class TestParse:
    def test_stablecoin_pair_keeps_code_usd_valuation(self):
        p = parse("BTC-USDT")
        assert p.yahoo == "BTC-USDT"  # 代码形态保留 (数据源交易对)
        assert p.currency == "USD"  # 估值口径归一
        assert p.type == "crypto"

    def test_usd_and_stablecoin_pairs_are_equivalent(self):
        assert parse("BTC-USD").currency == parse("BTC-USDT").currency == "USD"

    def test_non_usd_quote_untouched(self):
        assert parse("BTC-EUR").currency == "EUR"
        assert parse("ETH-BTC").currency == "BTC"


class TestSnapshot:
    def _portfolio(self):
        return {
            "base_currency": "CNY",
            "holdings": [{"symbol": "BTC-USDT", "quantity": 0.5, "avg_cost": 90000.0}],
        }

    def test_stablecoin_holding_valued(self, cfets, monkeypatch):
        """BTC-USDT 持仓进视图并正确估值, 无「汇率缺失」问题。"""
        q = Quote("BTC-USDT", None, 100000.0, None, None, "USD", source="binance")
        monkeypatch.setattr(
            "tracker.services.snapshot.prices.get_quotes",
            lambda syms, **kw: ({"BTC-USDT": q}, {}, []),
        )
        base, view, summary, _wview, issues = take_snapshot(self._portfolio())
        assert base == "CNY"
        assert len(view) == 1
        assert view.iloc[0]["market_value"] == pytest.approx(0.5 * 100000.0 * 7.1)
        assert summary["total_value"] == pytest.approx(0.5 * 100000.0 * 7.1)
        assert not [i for i in issues if "汇率" in i], issues

    def test_stale_quote_reporting_usdt_still_valued(self, cfets, monkeypatch):
        """磁盘缓存/数据源自报 currency=USDT 的旧 Quote 也按 USD 等价折算
        (parse 归一之外的第三道防线, fx.get_rate 的替代)。"""
        q = Quote("BTC-USDT", None, 100000.0, None, None, "USDT", source="cache")
        monkeypatch.setattr(
            "tracker.services.snapshot.prices.get_quotes",
            lambda syms, **kw: ({"BTC-USDT": q}, {}, []),
        )
        _base, view, _summary, _wview, issues = take_snapshot(self._portfolio())
        assert len(view) == 1
        assert view.iloc[0]["market_value"] == pytest.approx(0.5 * 100000.0 * 7.1)
        assert not [i for i in issues if "汇率" in i], issues

    def test_stablecoin_base_currency(self, cfets, monkeypatch):
        """portfolio.base_currency 是稳定币 (钱包导入 --base-currency USDT)
        时也能取到汇率。"""
        q = Quote("BTC-USDT", None, 100000.0, None, None, "USD", source="binance")
        monkeypatch.setattr(
            "tracker.services.snapshot.prices.get_quotes",
            lambda syms, **kw: ({"BTC-USDT": q}, {}, []),
        )
        portfolio = {
            "base_currency": "USDT",
            "holdings": [{"symbol": "BTC-USDT", "quantity": 1.0}],
        }
        base, view, _summary, _wview, issues = take_snapshot(portfolio)
        assert base == "USDT"
        assert len(view) == 1
        assert view.iloc[0]["market_value"] == pytest.approx(100000.0)
        assert not [i for i in issues if "汇率" in i], issues

    def test_coin_quote_reports_explicit_reason(self, cfets, monkeypatch):
        """币本位计价 (ETH-BTC) 不折算, 但原因要说清楚。"""
        q = Quote("ETH-BTC", None, 0.05, None, None, "BTC", source="binance")
        monkeypatch.setattr(
            "tracker.services.snapshot.prices.get_quotes",
            lambda syms, **kw: ({"ETH-BTC": q}, {}, []),
        )
        portfolio = {"base_currency": "CNY", "holdings": [{"symbol": "ETH-BTC", "quantity": 10}]}
        _base, view, _summary, _wview, issues = take_snapshot(portfolio)
        assert view.empty
        assert any("币本位计价 (BTC)" in i for i in issues), issues


class TestWalletSymbol:
    def test_stablecoin_base_normalized_to_usd(self):
        from tracker.wallet import _symbol_for_token

        assert _symbol_for_token("ETH", "eth", "USDT") == "ETH-USD"
        assert _symbol_for_token("USDT", "eth", "USDC") == "USDT-USD"
        # 非法币计价仍按请求的币种 (USDT-EUR 是合法交易对)
        assert _symbol_for_token("USDT", "eth", "EUR") == "USDT-EUR"


class TestAnalyticsView:
    def test_mixed_stablecoin_and_fiat_holdings(self, cfets):
        """稳定币与法币持仓混合: 都进视图, 权重按各自汇率归一。"""
        holdings = [
            {"symbol": "BTC-USDT", "quantity": 0.5, "avg_cost": 90000.0},
            {"symbol": "AAPL", "quantity": 10, "avg_cost": 150.0},
        ]
        quotes = {
            "BTC-USDT": Quote("BTC-USDT", None, 100000.0, None, None, "USD"),
            "AAPL": Quote("AAPL", "Apple", 200.0, None, None, "USD"),
        }
        view, issues = build_view(holdings, quotes, {"USD": 7.1, "CNY": 1.0})
        assert issues == []
        assert len(view) == 2
        total = view["market_value"].sum()
        assert total == pytest.approx((0.5 * 100000.0 + 10 * 200.0) * 7.1)
        assert view["weight_pct"].sum() == pytest.approx(100.0)


def test_kline_ratio_fx_for_stablecoin_pair(cfets, monkeypatch):
    """K线对比比例线的汇率调整: 稳定币计价代码也能拿到历史汇率。"""
    monkeypatch.setattr(fx, "_fx_hist_cache", {})
    seen = []

    def fake(pair, start, end=None):
        seen.append(pair)
        return pd.DataFrame(
            {"date": pd.date_range("2026-01-01", periods=2), "close": [7.1, 7.1]}
        )

    monkeypatch.setattr(fx, "_yahoo_pair_history", fake)
    df = fx.get_fx_history("USDT", "CNY", "2026-01-01")
    assert df is not None and list(df["close"]) == [7.1, 7.1]
    assert seen == ["USDCNY=X"]  # 请求的是 USD 直盘, 不是不存在的 USDTCNY=X
