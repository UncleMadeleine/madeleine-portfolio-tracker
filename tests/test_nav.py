"""历史净值: 持久化 / 查询 / 回填 / CLI."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pandas as pd
import pytest

from tracker import cli, nav
from tracker.prices import Quote


def _summary(value=100.0, cost=80.0, pnl=20.0):
    return {
        "total_value": value,
        "total_cost": cost,
        "total_pnl": pnl,
        "total_pnl_pct": pnl / cost if cost else None,
        "today_pnl": 1.0,
        "cost_coverage": 1.0,
    }


def _run(capsys, *argv):
    cli.main(list(argv))
    return capsys.readouterr().out


class TestNavStore:
    def test_save_load_and_same_day_upsert(self):
        day = "2026-01-02"
        nav.save_rows([nav.record_from_summary(day, "cny", _summary())])
        rows = nav.load_history("CNY")
        assert len(rows) == 1
        assert rows[0]["base"] == "CNY"
        assert rows[0]["total_value"] == 100.0
        assert rows[0]["source"] == "snapshot"

        nav.save_rows([nav.record_from_summary(day, "CNY", _summary(value=120.0))])
        rows = nav.load_history("CNY")
        assert len(rows) == 1
        assert rows[0]["total_value"] == 120.0

    def test_base_filter_and_days_window(self):
        today = date.today()
        for offset in (5, 2, 0):
            day = (today - timedelta(days=offset)).isoformat()
            nav.save_rows(
                [
                    {
                        "day": day,
                        "base": "CNY",
                        "total_value": 100.0 + offset,
                    }
                ]
            )
        nav.save_rows([{"day": today.isoformat(), "base": "USD", "total_value": 50.0}])

        assert len(nav.load_history("CNY")) == 3
        assert len(nav.load_history("CNY", days=3)) == 2
        assert [r["total_value"] for r in nav.load_history("USD")] == [50.0]
        assert nav.latest_day("CNY") == today.isoformat()

    def test_invalid_rows_are_skipped(self):
        count = nav.save_rows(
            [
                {"day": "not-a-date", "base": "CNY", "total_value": 1},
                {"day": "2026-01-01", "base": "", "total_value": 1},
                {"day": "2026-01-01", "base": "CNY", "total_value": float("nan")},
                {"day": "2026-01-01", "base": "CNY", "total_value": 1},
            ]
        )
        assert count == 1
        assert len(nav.load_history("CNY")) == 1

    def test_payload_is_json_safe(self):
        view = pd.DataFrame(
            [
                {
                    "symbol": "AAPL",
                    "name": "Apple",
                    "market": "美股",
                    "currency": "USD",
                    "quantity": 2.0,
                    "price": 190.0,
                    "market_value": 380.0,
                    "cost": 300.0,
                    "pnl": 80.0,
                },
                {
                    "symbol": "BAD",
                    "name": None,
                    "market": "美股",
                    "currency": "USD",
                    "quantity": float("nan"),
                    "price": None,
                    "market_value": float("nan"),
                    "cost": None,
                    "pnl": float("nan"),
                },
            ]
        )
        record = nav.record_from_summary("2026-01-02", "CNY", _summary(), view)
        payload = json.loads(record["payload"])
        assert payload[0]["quantity"] == 2.0
        assert payload[1]["quantity"] is None
        assert payload[1]["market_value"] is None

    def test_backfill_does_not_overwrite_real_snapshot(self):
        day = "2026-01-02"
        nav.save_rows([nav.record_from_summary(day, "CNY", _summary(value=100.0))])
        nav.save_rows(
            [
                {
                    "day": day,
                    "base": "CNY",
                    "total_value": 999.0,
                    "source": "backfill",
                }
            ],
            overwrite=False,
        )
        rows = nav.load_history("CNY")
        assert rows[0]["total_value"] == 100.0
        assert rows[0]["source"] == "snapshot"

    def test_rebackfill_updates_prior_backfill_row(self):
        day = "2026-01-02"
        nav.save_rows(
            [{"day": day, "base": "CNY", "total_value": 999.0, "source": "backfill"}],
            overwrite=False,
        )
        changed = nav.save_rows(
            [{"day": day, "base": "CNY", "total_value": 111.0, "source": "backfill"}],
            overwrite=False,
        )
        assert changed == 1
        rows = nav.load_history("CNY")
        assert rows[0]["total_value"] == 111.0
        assert rows[0]["source"] == "backfill"

    def test_cache_clear_keeps_nav_history(self):
        from tracker import cache as cache_mod

        nav.save_rows(
            [
                {
                    "day": date.today().isoformat(),
                    "base": "CNY",
                    "total_value": 42.0,
                }
            ]
        )
        cache_mod.clear()
        rows = nav.load_history("CNY")
        assert len(rows) == 1
        assert rows[0]["total_value"] == 42.0


class TestNavBackfill:
    def _mock(self, monkeypatch):
        def fake_quotes(symbols, **kwargs):
            quotes = {
                "AAPL": Quote(
                    symbol="AAPL",
                    name="Apple",
                    price=11.0,
                    prev_close=10.0,
                    change_pct=10.0,
                    currency="USD",
                ),
                "600519.SS": Quote(
                    symbol="600519.SS",
                    name="贵州茅台",
                    price=1600.0,
                    prev_close=1500.0,
                    change_pct=6.6,
                    currency="CNY",
                ),
            }
            return quotes, {}, []

        def fake_ohlc(symbol, **kwargs):
            if symbol == "AAPL":
                return pd.DataFrame(
                    {
                        "date": ["2026-01-01", "2026-01-02"],
                        "close": [10.0, 11.0],
                    }
                )
            return pd.DataFrame(
                {
                    "date": ["2026-01-01", "2026-01-02"],
                    "close": [1500.0, 1600.0],
                }
            )

        def fake_fx(src, dst, start, end=None, refresh=False):
            assert (src, dst) == ("USD", "CNY")
            return pd.DataFrame(
                {"date": ["2026-01-01", "2026-01-02"], "close": [7.0, 7.1]}
            )

        monkeypatch.setattr("tracker.prices.get_quotes", fake_quotes)
        monkeypatch.setattr("tracker.prices.get_ohlc", fake_ohlc)
        monkeypatch.setattr("tracker.fx.get_fx_history", fake_fx)

    def test_backfill_uses_current_holdings_and_fx(self, monkeypatch):
        self._mock(monkeypatch)
        portfolio = {
            "base_currency": "CNY",
            "holdings": [
                {"symbol": "AAPL", "quantity": 10, "avg_cost": 100},
                {"symbol": "600519.SS", "quantity": 100, "avg_cost": 1500},
            ],
        }
        rows, issues = nav.backfill(portfolio, "CNY", days=2, today=date(2026, 1, 3))
        assert issues == []
        assert [r["day"] for r in rows] == ["2026-01-01", "2026-01-02"]
        assert rows[0]["total_value"] == pytest.approx(150700.0)
        assert rows[1]["total_value"] == pytest.approx(160781.0)
        assert rows[0]["total_cost"] == pytest.approx(157000.0)
        assert rows[0]["cost_coverage"] == pytest.approx(1.0)
        assert rows[0]["source"] == "backfill"
        assert rows[0]["holdings"] == 2

    def test_backfill_mixed_long_short_coverage_uses_absolute_exposure(
        self, monkeypatch
    ):
        """多空市值互相抵消时, 成本覆盖率仍应为 100% (不是 None/负数)."""
        from tracker.prices import Quote

        day = pd.Timestamp("2026-01-01")
        prices = {"AAPL": 100.0, "TSLA": 200.0}

        def _quotes(symbols, **kwargs):
            return (
                {
                    s: Quote(s, s, prices[s], prices[s], 0.0, "USD")
                    for s in symbols
                },
                {},
                [],
            )

        def _ohlc(symbol, **kwargs):
            return pd.DataFrame({"date": [day], "close": [prices[symbol]]})

        monkeypatch.setattr("tracker.prices.get_quotes", _quotes)
        monkeypatch.setattr("tracker.prices.get_ohlc", _ohlc)
        portfolio = {
            "holdings": [
                {"symbol": "AAPL", "quantity": 10, "avg_cost": 100},
                {"symbol": "TSLA", "quantity": -5, "avg_cost": 200},
            ]
        }
        rows, issues = nav.backfill(
            portfolio, "USD", days=1, today=date(2026, 1, 2)
        )
        assert issues == []
        assert rows[0]["cost_coverage"] == pytest.approx(1.0)

    def test_backfill_falls_back_to_spot_fx(self, monkeypatch):
        self._mock(monkeypatch)
        monkeypatch.setattr("tracker.fx.get_fx_history", lambda *a, **k: None)
        monkeypatch.setattr("tracker.fx.get_rate", lambda *a, **k: 7.0)
        portfolio = {"holdings": [{"symbol": "AAPL", "quantity": 10, "avg_cost": 100}]}
        rows, _issues = nav.backfill(portfolio, "CNY", days=2, today=date(2026, 1, 3))
        assert rows[0]["total_value"] == pytest.approx(700.0)
        assert rows[1]["total_value"] == pytest.approx(770.0)


class TestNavCli:
    def test_read_json(self, capsys):
        nav.save_rows(
            [
                {
                    "day": date.today().isoformat(),
                    "base": "CNY",
                    "total_value": 123.0,
                }
            ]
        )
        out = _run(capsys, "nav", "--json")
        payload = json.loads(out)
        assert payload["base"] == "CNY"
        assert payload["rows"][0]["total_value"] == 123.0

    def test_read_empty_hint(self, capsys):
        out = _run(capsys, "nav", "--base", "CNY")
        assert "暂无 CNY 净值历史" in out
        assert "nav --save" in out

    def test_snapshot_save_writes_nav(self, capsys, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "tracker.prices.get_quotes",
            lambda *a, **k: (
                {
                    "AAPL": Quote(
                        symbol="AAPL",
                        name="Apple",
                        price=190.0,
                        prev_close=188.0,
                        change_pct=1.0,
                        currency="USD",
                    )
                },
                {},
                [],
            ),
        )
        monkeypatch.setattr(
            "tracker.services.snapshot.get_fx_rates",
            lambda *a, **k: ({"USD": 7.0}, []),
        )
        p = tmp_path / "portfolio.json"
        p.write_text(
            json.dumps(
                {
                    "base_currency": "CNY",
                    "holdings": [{"symbol": "AAPL", "quantity": 1, "avg_cost": 100}],
                }
            ),
            encoding="utf-8",
        )
        out = _run(capsys, "snapshot", "--portfolio", str(p), "--save")
        assert "历史净值已保存" in out
        rows = nav.load_history("CNY")
        assert len(rows) == 1
        assert rows[0]["total_value"] == pytest.approx(1330.0)

    def test_snapshot_save_skips_empty_portfolio(self, capsys, tmp_path):
        p = tmp_path / "portfolio.json"
        p.write_text(
            json.dumps({"base_currency": "CNY", "holdings": []}), encoding="utf-8"
        )
        _run(capsys, "snapshot", "--portfolio", str(p), "--save")
        assert nav.load_history("CNY") == []

    def test_backfill_dry_run_json(self, capsys, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "tracker.nav.backfill",
            lambda *a, **k: (
                [{"day": "2026-01-02", "base": "CNY", "total_value": 10.0}],
                ["AAPL: 示例问题"],
            ),
        )
        p = tmp_path / "portfolio.json"
        p.write_text(
            json.dumps({"base_currency": "CNY", "holdings": []}), encoding="utf-8"
        )
        out = _run(
            capsys,
            "nav",
            "--portfolio",
            str(p),
            "--backfill",
            "--dry-run",
            "--json",
        )
        payload = json.loads(out)
        assert payload["generated"] == 1
        assert payload["inserted"] == 0
        assert payload["dry_run"] is True
        assert payload["rows"][0]["total_value"] == 10.0
        assert nav.load_history("CNY") == []

    def test_days_rejects_zero(self):
        with pytest.raises(SystemExit):
            cli.main(["nav", "--days", "0"])
