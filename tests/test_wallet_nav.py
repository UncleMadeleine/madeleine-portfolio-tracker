"""钱包净值估值 + CLI (网络全部 mock)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pandas as pd
import pytest

from tracker import cli, wallet_history, wallet_nav


ADDR = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"


def _asset_eth() -> wallet_history.Asset:
    return wallet_history.Asset(
        key="native", symbol="ETH", decimals=18, kind="native", chain="eth"
    )


def _asset_usdc() -> wallet_history.Asset:
    return wallet_history.Asset(
        key="0xusdc",
        symbol="USDC",
        decimals=6,
        kind="token",
        chain="eth",
    )


def _ledger() -> wallet_history.Ledger:
    today = datetime.now(timezone.utc).date()
    ts = (
        int(
            datetime.combine(
                today, datetime.min.time(), tzinfo=timezone.utc
            ).timestamp()
        )
        - 3600
    )
    return wallet_history.Ledger(
        chain="eth",
        address=ADDR,
        assets={_asset_eth().key: _asset_eth(), _asset_usdc().key: _asset_usdc()},
        current={_asset_eth().key: 2.0, _asset_usdc().key: 100.0},
        transfers=[wallet_history.Transfer(ts, _asset_eth().key, 1.0, "0xbuy")],
        providers=["Routescan"],
    )


def _fake_ohlc(days: int):
    def _get(symbol, months=12, refresh=False, crypto_source="auto", **kw):
        if symbol != "ETH-USD":
            raise AssertionError(symbol)
        end = pd.Timestamp(datetime.now(timezone.utc).date())
        dates = pd.date_range(end=end, periods=days, freq="D")
        return pd.DataFrame({"date": dates, "close": [1000.0] * days})

    return _get


class TestQuoteHelpers:
    def test_stable_and_quote_symbols(self):
        assert wallet_nav._stable_symbol("USDC")
        assert wallet_nav._stable_symbol("USDt")
        assert wallet_nav._stable_symbol("USDT.e")
        assert not wallet_nav._stable_symbol("ETH")
        assert wallet_nav._quote_symbol("btc") == "BTC-USD"
        assert wallet_nav._quote_symbol("WETH.e") == "ETH-USD"
        assert wallet_nav._quote_symbol("JITOSOL") == "SOL-USD"
        assert wallet_nav._quote_symbol("BAD SYMBOL") is None


class TestCompute:
    def test_curve_uses_historical_balances(self, monkeypatch):
        monkeypatch.setattr(wallet_history, "fetch_ledger", lambda *a, **k: _ledger())
        monkeypatch.setattr(wallet_nav.prices, "get_ohlc", _fake_ohlc(3))
        result = wallet_nav.compute_wallet_nav("eth", ADDR, base="USD", days=3)
        assert result["chain"] == "eth"
        assert result["base"] == "USD"
        assert result["providers"] == ["Routescan"]
        assert len(result["rows"]) == 3
        values = [r["total_value"] for r in result["rows"]]
        assert values == pytest.approx([1100.0, 2100.0, 2100.0])
        assert result["rows"][0]["source"] == "wallet-replay"
        # 稳定币按 1 USD 计入, ETH 取历史价
        priced = {a["symbol"]: a["priced"] for a in result["assets"]}
        assert priced == {"ETH": True, "USDC": True}
        assert result["issues"] == []

    def test_missing_price_is_reported_and_excluded(self, monkeypatch):
        today = datetime.now(timezone.utc).date()
        ts = (
            int(
                datetime.combine(
                    today, datetime.min.time(), tzinfo=timezone.utc
                ).timestamp()
            )
            - 3600
        )
        ledger = wallet_history.Ledger(
            chain="eth",
            address=ADDR,
            assets={_asset_eth().key: _asset_eth()},
            current={_asset_eth().key: 1.0},
            transfers=[wallet_history.Transfer(ts, _asset_eth().key, 1.0)],
        )
        monkeypatch.setattr(wallet_history, "fetch_ledger", lambda *a, **k: ledger)

        def boom(*a, **k):
            raise RuntimeError("no data")

        monkeypatch.setattr(wallet_nav.prices, "get_ohlc", boom)
        result = wallet_nav.compute_wallet_nav("eth", ADDR, base="USD", days=3)
        assert result["rows"] == []
        assert any("ETH-USD" in i for i in result["issues"])
        assert any("没有可定价" in i for i in result["issues"])
        assert result["assets"][0]["priced"] is False

    def test_bad_chain_is_readable(self):
        with pytest.raises(wallet_history.WalletHistoryError, match="不支持"):
            wallet_nav.compute_wallet_nav("cosmos", ADDR, days=3)

    def test_max_assets_keeps_largest_by_current_usd(self, monkeypatch):
        today = datetime.now(timezone.utc).date()
        ts = (
            int(
                datetime.combine(
                    today, datetime.min.time(), tzinfo=timezone.utc
                ).timestamp()
            )
            - 3600
        )
        small = wallet_history.Asset(
            key="small", symbol="AAA", decimals=0, kind="token", chain="eth"
        )
        big = wallet_history.Asset(
            key="big", symbol="BBB", decimals=0, kind="token", chain="eth"
        )
        ledger = wallet_history.Ledger(
            chain="eth",
            address=ADDR,
            assets={small.key: small, big.key: big},
            current={small.key: 100.0, big.key: 1.0},
            current_usd={small.key: 1.0, big.key: 10000.0},
            transfers=[
                wallet_history.Transfer(ts, small.key, 1.0),
                wallet_history.Transfer(ts, big.key, 1.0),
            ],
            trusted={small.key, big.key},
        )
        monkeypatch.setattr(wallet_history, "fetch_ledger", lambda *a, **k: ledger)

        def any_ohlc(symbol, **kw):
            return pd.DataFrame(
                {
                    "date": pd.date_range(end=pd.Timestamp(today), periods=3),
                    "close": [10.0, 10.0, 10.0],
                }
            )

        monkeypatch.setattr(wallet_nav.prices, "get_ohlc", any_ohlc)
        result = wallet_nav.compute_wallet_nav(
            "eth", ADDR, base="USD", days=3, max_assets=1
        )
        assert [a["symbol"] for a in result["assets"]] == ["BBB"]
        assert result["skipped_count"] == 1
        assert any("估值上限" in n for n in result["notes"])


class TestCli:
    def test_parser_registers_wallet_nav(self):
        args = cli.build_parser().parse_args(
            ["wallet-nav", "eth", ADDR, "--days", "30", "--json"]
        )
        assert callable(args.func)
        assert args.chain == "eth"
        assert args.days == 30

    def test_json_output(self, capsys, monkeypatch):
        import tracker.cli.wallet_nav as cli_wn

        payload = {
            "chain": "eth",
            "address": ADDR,
            "base": "USD",
            "days": 3,
            "start": "2026-01-01",
            "end": "2026-01-03",
            "rows": [{"day": "2026-01-01", "total_value": 100.0, "holdings": 1}],
            "assets": [],
            "issues": [],
            "notes": [],
            "providers": ["Routescan"],
            "truncated": False,
        }
        monkeypatch.setattr(cli_wn, "compute_wallet_nav", lambda *a, **k: payload)
        cli.main(["wallet-nav", "eth", ADDR, "--base", "USD", "--days", "3", "--json"])
        out = capsys.readouterr().out
        assert json.loads(out)["chain"] == "eth"

    def test_table_output(self, capsys, monkeypatch):
        import tracker.cli.wallet_nav as cli_wn

        payload = {
            "chain": "eth",
            "address": ADDR,
            "base": "USD",
            "days": 2,
            "start": "2026-01-01",
            "end": "2026-01-02",
            "rows": [
                {"day": "2026-01-01", "total_value": 100.0, "holdings": 1},
                {"day": "2026-01-02", "total_value": 110.0, "holdings": 1},
            ],
            "assets": [
                {
                    "symbol": "ETH",
                    "quantity": 1.0,
                    "price_symbol": "ETH-USD",
                    "priced": True,
                }
            ],
            "issues": ["测试问题"],
            "notes": [],
            "providers": ["Routescan"],
            "truncated": False,
        }
        monkeypatch.setattr(cli_wn, "compute_wallet_nav", lambda *a, **k: payload)
        cli.main(["wallet-nav", "eth", ADDR, "--base", "USD", "--days", "2"])
        out = capsys.readouterr().out
        assert "总市值" in out
        assert "Routescan" in out
        assert "测试问题" in out
