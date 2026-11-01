"""券商持仓导入 (IBKR / 长桥): 改码迁移 / 未挂牌占位码过滤."""

from __future__ import annotations

from tracker import ibkr, longport


class TestBrokerPositionFilters:
    def test_ibkr_positions_migrate_changed_symbols(self):
        """改码/退市标的 (ERJ→EMBJ) 迁移到接替码, 死码不入账."""
        from types import SimpleNamespace as NS

        def C(sym):
            return NS(symbol=sym, exchange="NYSE", primaryExchange="", currency="USD")

        rows, skipped = ibkr.positions_to_rows(
            [NS(contract=C("ERJ"), position=10, avgCost=20.0)]
        )
        assert [r["symbol"] for r in rows] == ["EMBJ"]
        assert any("ERJ → EMBJ" in s for s in skipped)

    def test_longport_positions_skip_unquoted_placeholder(self):
        """未挂牌占位代码 (N22117.HK) 过滤, 导入即死码."""
        from types import SimpleNamespace as NS

        rows, skipped = longport.positions_to_rows(
            [NS(symbol="N22117.HK", quantity=100, cost_price=5.0, currency="HKD")]
        )
        assert rows == []
        assert any("占位代码" in s for s in skipped)
