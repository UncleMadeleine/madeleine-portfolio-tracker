import pandas as pd

from tracker.prices import Quote
from tracker.watchlist import (
    STATUS_LOWER,
    STATUS_UPPER,
    STATUS_WITHIN,
    build_watchlist_view,
    triggered_entries,
)


def q(sym, price, ccy="USD"):
    return Quote(
        symbol=sym, name=sym, price=price, prev_close=price,
        change_pct=1.0, currency=ccy,
    )


def test_statuses_and_distances():
    entries = [
        {"symbol": "AAA", "upper": 100.0, "lower": 50.0},
        {"symbol": "BBB", "upper": 200.0, "lower": 100.0},
        {"symbol": "CCC", "lower": 90.0},
    ]
    quotes = {"AAA": q("AAA", 110.0), "BBB": q("BBB", 150.0), "CCC": q("CCC", 80.0)}
    view, issues = build_watchlist_view(entries, quotes)
    assert issues == []
    m = view.set_index("symbol")["status"]
    assert m["AAA"] == STATUS_UPPER
    assert m["BBB"] == STATUS_WITHIN
    assert m["CCC"] == STATUS_LOWER
    row = view.set_index("symbol").loc["BBB"]
    assert abs(row["dist_upper_pct"] - (200 / 150 - 1) * 100) < 1e-9
    assert abs(row["dist_lower_pct"] - (150 / 100 - 1) * 100) < 1e-9
    assert view.iloc[-1]["symbol"] == "BBB"
    trig = triggered_entries(view)
    assert set(trig["symbol"]) == {"AAA", "CCC"}


def test_boundary_equality_inclusive():
    view, _ = build_watchlist_view(
        [{"symbol": "AAA", "upper": 100.0}], {"AAA": q("AAA", 100.0)}
    )
    assert view.iloc[0]["status"] == STATUS_UPPER
    view2, _ = build_watchlist_view(
        [{"symbol": "AAA", "lower": 100.0}], {"AAA": q("AAA", 100.0)}
    )
    assert view2.iloc[0]["status"] == STATUS_LOWER


def test_no_thresholds():
    view, issues = build_watchlist_view(
        [{"symbol": "BRK-B"}], {"BRK-B": q("BRK-B", 300.0)}
    )
    assert issues == []
    row = view.iloc[0]
    assert row["status"] == STATUS_WITHIN
    assert pd.isna(row["upper"])
    assert pd.isna(row["lower"])
    assert not row["triggered"]


def test_missing_quote_and_bad_symbol():
    entries = [
        {"symbol": "NOPE.HK", "lower": 1.0},
        {"symbol": "BAD.ZZ", "upper": 1.0},
        {"symbol": "AAPL", "upper": 999.0},
    ]
    view, issues = build_watchlist_view(entries, {"AAPL": q("AAPL", 100.0)})
    assert len(view) == 1
    assert len(issues) == 2
    assert any("NOPE.HK" in i for i in issues)
    assert any("BAD.ZZ" in i for i in issues)


def test_numeric_string_thresholds():
    view, _ = build_watchlist_view(
        [{"symbol": "AAA", "upper": "120", "lower": "80"}], {"AAA": q("AAA", 100.0)}
    )
    row = view.iloc[0]
    assert row["upper"] == 120.0
    assert row["lower"] == 80.0
    assert row["status"] == STATUS_WITHIN
