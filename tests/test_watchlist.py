import json

import pandas as pd

from tracker.prices import Quote
from tracker.watchlist import (
    STATUS_LOWER_1,
    STATUS_LOWER_2,
    STATUS_UPPER_1,
    STATUS_UPPER_2,
    STATUS_WITHIN,
    build_watchlist_view,
    entries_for,
    list_names,
    load_watchlist,
    merge_entries,
    sort_watchlist,
    triggered_entries,
)


def q(sym, price, ccy="USD"):
    return Quote(
        symbol=sym, name=sym, price=price, prev_close=price,
        change_pct=1.0, currency=ccy,
    )


def test_statuses_and_distances_backward_compat():
    entries = [
        {"symbol": "AAA", "upper": 100.0, "lower": 50.0},
        {"symbol": "BBB", "upper": 200.0, "lower": 100.0},
        {"symbol": "CCC", "lower": 90.0},
    ]
    quotes = {"AAA": q("AAA", 110.0), "BBB": q("BBB", 150.0), "CCC": q("CCC", 80.0)}
    view, issues = build_watchlist_view(entries, quotes)
    assert issues == []
    m = view.set_index("symbol")["status"]
    assert m["AAA"] == STATUS_UPPER_1
    assert m["BBB"] == STATUS_WITHIN
    assert m["CCC"] == STATUS_LOWER_1
    row = view.set_index("symbol").loc["BBB"]
    assert abs(row["dist_upper_1_pct"] - (200 / 150 - 1) * 100) < 1e-9
    assert abs(row["dist_lower_1_pct"] - (150 / 100 - 1) * 100) < 1e-9
    assert view.iloc[-1]["symbol"] == "BBB"
    trig = triggered_entries(view)
    assert set(trig["symbol"]) == {"AAA", "CCC"}


def test_two_level_thresholds():
    entries = [
        {"symbol": "AAA", "upper_1": 100.0, "upper_2": 120.0},
        {"symbol": "BBB", "upper_1": 100.0, "upper_2": 120.0},
        {"symbol": "CCC", "lower_1": 100.0, "lower_2": 80.0},
        {"symbol": "DDD", "lower_1": 100.0, "lower_2": 80.0},
    ]
    quotes = {
        "AAA": q("AAA", 125.0),
        "BBB": q("BBB", 110.0),
        "CCC": q("CCC", 90.0),
        "DDD": q("DDD", 75.0),
    }
    view, _ = build_watchlist_view(entries, quotes)
    m = view.set_index("symbol")["status"]
    assert m["AAA"] == STATUS_UPPER_2
    assert m["BBB"] == STATUS_UPPER_1
    assert m["CCC"] == STATUS_LOWER_1
    assert m["DDD"] == STATUS_LOWER_2
    row = view.set_index("symbol").loc["BBB"]
    assert abs(row["dist_upper_1_pct"] - (100 / 110 - 1) * 100) < 1e-9
    assert abs(row["dist_upper_2_pct"] - (120 / 110 - 1) * 100) < 1e-9


def test_two_level_one_side_only():
    entries = [
        {"symbol": "AAA", "upper_2": 100.0},
        {"symbol": "BBB", "lower_1": 100.0},
        {"symbol": "CCC", "lower_2": 80.0},
    ]
    quotes = {"AAA": q("AAA", 105.0), "BBB": q("BBB", 90.0), "CCC": q("CCC", 70.0)}
    view, _ = build_watchlist_view(entries, quotes)
    m = view.set_index("symbol")["status"]
    assert m["AAA"] == STATUS_UPPER_2
    assert m["BBB"] == STATUS_LOWER_1
    assert m["CCC"] == STATUS_LOWER_2


def test_backward_compat_old_keys():
    view, _ = build_watchlist_view(
        [{"symbol": "AAA", "upper": 100.0, "lower": 50.0}], {"AAA": q("AAA", 110.0)}
    )
    assert view.iloc[0]["upper_1"] == 100.0
    assert view.iloc[0]["lower_1"] == 50.0
    assert view.iloc[0]["status"] == STATUS_UPPER_1
    assert pd.isna(view.iloc[0]["upper_2"])
    assert pd.isna(view.iloc[0]["lower_2"])


def test_boundary_equality_inclusive():
    view, _ = build_watchlist_view(
        [{"symbol": "AAA", "upper_1": 100.0}], {"AAA": q("AAA", 100.0)}
    )
    assert view.iloc[0]["status"] == STATUS_UPPER_1
    view2, _ = build_watchlist_view(
        [{"symbol": "AAA", "lower_1": 100.0}], {"AAA": q("AAA", 100.0)}
    )
    assert view2.iloc[0]["status"] == STATUS_LOWER_1
    view3, _ = build_watchlist_view(
        [{"symbol": "AAA", "upper_2": 100.0}], {"AAA": q("AAA", 100.0)}
    )
    assert view3.iloc[0]["status"] == STATUS_UPPER_2
    view4, _ = build_watchlist_view(
        [{"symbol": "AAA", "lower_2": 100.0}], {"AAA": q("AAA", 100.0)}
    )
    assert view4.iloc[0]["status"] == STATUS_LOWER_2


def test_no_thresholds():
    view, issues = build_watchlist_view(
        [{"symbol": "BRK-B"}], {"BRK-B": q("BRK-B", 300.0)}
    )
    assert issues == []
    row = view.iloc[0]
    assert row["status"] == STATUS_WITHIN
    assert pd.isna(row["upper_1"])
    assert pd.isna(row["lower_1"])
    assert not row["triggered"]


def test_missing_quote_and_bad_symbol():
    entries = [
        {"symbol": "NOPE.HK", "lower_1": 1.0},
        {"symbol": "BAD.ZZ", "upper_1": 1.0},
        {"symbol": "AAPL", "upper_1": 999.0},
    ]
    view, issues = build_watchlist_view(entries, {"AAPL": q("AAPL", 100.0)})
    assert len(view) == 1
    assert len(issues) == 2
    assert any("NOPE.HK" in i for i in issues)
    assert any("BAD.ZZ" in i for i in issues)


def test_numeric_string_thresholds():
    view, _ = build_watchlist_view(
        [{"symbol": "AAA", "upper_1": "120", "lower_1": "80"}], {"AAA": q("AAA", 100.0)}
    )
    row = view.iloc[0]
    assert row["upper_1"] == 120.0
    assert row["lower_1"] == 80.0
    assert row["status"] == STATUS_WITHIN


def test_sort_default_triggered_first():
    view = pd.DataFrame(
        [
            {"symbol": "BBB", "triggered": False, "change_pct": 1.0},
            {"symbol": "AAA", "triggered": True, "change_pct": 2.0},
        ]
    )
    out = sort_watchlist(view, "default")
    assert list(out["symbol"]) == ["AAA", "BBB"]


def test_sort_severity_order():
    view = pd.DataFrame(
        [
            {"symbol": "A", "status": STATUS_WITHIN, "change_pct": 0.0},
            {"symbol": "B", "status": STATUS_UPPER_2, "change_pct": 0.0},
            {"symbol": "C", "status": STATUS_LOWER_1, "change_pct": 0.0},
            {"symbol": "D", "status": STATUS_UPPER_1, "change_pct": 0.0},
            {"symbol": "E", "status": STATUS_LOWER_2, "change_pct": 0.0},
        ]
    )
    out = sort_watchlist(view, "severity")
    assert list(out["symbol"]) == ["B", "D", "C", "E", "A"]
    assert "status_rank" not in out.columns


def test_sort_change_desc_and_asc():
    view = pd.DataFrame(
        [
            {"symbol": "A", "change_pct": 2.0},
            {"symbol": "B", "change_pct": -3.0},
            {"symbol": "C", "change_pct": 1.0},
        ]
    )
    desc = sort_watchlist(view, "change_desc")
    assert list(desc["symbol"]) == ["A", "C", "B"]
    asc = sort_watchlist(view, "change_asc")
    assert list(asc["symbol"]) == ["B", "C", "A"]


def test_sort_empty():
    out = sort_watchlist(pd.DataFrame(), "severity")
    assert out.empty


def test_load_migrates_old_flat_format(tmp_path):
    f = tmp_path / "w.json"
    f.write_text(
        json.dumps({"watchlist": [{"symbol": "AAPL", "upper_1": 100}]}),
        encoding="utf-8",
    )
    data = load_watchlist(f)
    assert list_names(data) == ["默认"]
    assert data["watchlists"]["默认"] == [{"symbol": "AAPL", "upper_1": 100}]


def test_load_new_format(tmp_path):
    f = tmp_path / "w.json"
    f.write_text(
        json.dumps({"watchlists": {"科技": [{"symbol": "AAPL"}]}}), encoding="utf-8"
    )
    data = load_watchlist(f)
    assert list_names(data) == ["科技"]
    assert entries_for(data, "科技") == [{"symbol": "AAPL"}]


def test_load_missing_file(tmp_path):
    data = load_watchlist(tmp_path / "nope.json")
    assert list_names(data) == ["默认"]


def test_list_names_and_entries():
    data = {"watchlists": {"默认": [{"symbol": "A"}], "科技": [{"symbol": "B"}]}}
    assert list_names(data) == ["默认", "科技"]
    assert entries_for(data, "科技") == [{"symbol": "B"}]
    assert entries_for(data, "不存在") == [{"symbol": "A"}, {"symbol": "B"}]
    assert entries_for(data) == [{"symbol": "A"}, {"symbol": "B"}]


def test_merge_entries_dedup():
    data = {
        "watchlists": {
            "默认": [{"symbol": "A"}, {"symbol": "B"}],
            "科技": [{"symbol": "B"}, {"symbol": "C"}],
        }
    }
    merged = merge_entries(data)
    assert [e["symbol"] for e in merged] == ["A", "B", "C"]
