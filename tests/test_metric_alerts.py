"""metric (阈值基准指标) 测试: price=现价 / pb=市净率。

覆盖: metric 推断与归一 (rules + storage)、PB 状态判定 (services)、
PB 缓存 (含负缓存语义)、PB 缺失降级、build_watchlist_view 组装。
全部离线 (Quote 手工构造, fundamentals monkeypatch)。
"""

import json

import pandas as pd
import pytest

from tracker.prices import Quote
from tracker.services.rules import (
    METRIC_PB,
    METRIC_PRICE,
    STATUS_LOWER_1,
    STATUS_NO_DATA,
    STATUS_UPPER_1,
    STATUS_WITHIN,
    metric_for_entry,
    metric_value_for_quote,
)
from tracker.services.watchlist import build_watchlist_view, triggered_entries
from tracker.storage import normalize_watch_entry


def q(sym, price, ccy="USD", book_value=None):
    return Quote(
        symbol=sym,
        name=sym,
        price=price,
        prev_close=price,
        change_pct=1.0,
        currency=ccy,
        book_value=book_value,
    )


class TestMetricForEntry:
    def test_default_price(self):
        assert metric_for_entry({"symbol": "AAPL"}) == METRIC_PRICE

    def test_explicit_pb(self):
        assert metric_for_entry({"metric": "pb"}) == METRIC_PB

    def test_explicit_price(self):
        assert metric_for_entry({"metric": "price"}) == METRIC_PRICE

    def test_case_insensitive_and_trimmed(self):
        assert metric_for_entry({"metric": " PB "}) == METRIC_PB

    def test_invalid_falls_back_to_price(self):
        assert metric_for_entry({"metric": "roe"}) == METRIC_PRICE

    def test_legacy_pb_key_infers_metric(self):
        """旧手写 pb 阈值 (无 metric) 自动按 PB 解释, 不误判为价格阈值."""
        assert metric_for_entry({"pb": 5.0}) == METRIC_PB
        assert metric_for_entry({"price_to_book": 5}) == METRIC_PB

    def test_empty_pb_key_stays_price(self):
        assert metric_for_entry({"pb": None}) == METRIC_PRICE


class TestMetricNormalization:
    def test_metric_pb_kept(self):
        e = normalize_watch_entry({"symbol": "AAPL", "metric": "PB", "upper_1": 5})
        assert e["metric"] == "pb"

    def test_metric_price_dropped(self):
        """缺省基准不落盘."""
        e = normalize_watch_entry({"symbol": "AAPL", "metric": "price"})
        assert "metric" not in e

    def test_metric_invalid_dropped(self):
        e = normalize_watch_entry({"symbol": "AAPL", "metric": "roe"})
        assert "metric" not in e

    def test_legacy_pb_key_gets_metric_stamped(self):
        e = normalize_watch_entry({"symbol": "AAPL", "pb": 5.0})
        assert e["metric"] == "pb"


class TestMetricValueForQuote:
    def test_price_metric_returns_price(self):
        assert metric_value_for_quote(METRIC_PRICE, q("AAPL", 100)) == 100

    def test_pb_metric_divides_book_value(self):
        assert metric_value_for_quote(METRIC_PB, q("AAPL", 100, book_value=4)) == 25

    def test_pb_missing_book_value_is_none(self):
        assert metric_value_for_quote(METRIC_PB, q("AAPL", 100)) is None

    def test_pb_nonpositive_book_value_is_none(self):
        """负净资产 PB 无意义."""
        assert metric_value_for_quote(METRIC_PB, q("AAPL", 100, book_value=0)) is None
        assert metric_value_for_quote(METRIC_PB, q("AAPL", 100, book_value=-2)) is None


class TestPBView:
    def test_pb_threshold_triggers_upper(self, no_bvps_fixture):
        """BVPS 4, 现价 100 → PB 25; upper_1=20 触发上限 I."""
        import tracker.fundamentals as f

        f.fetch_book_values = lambda symbols: {s: 4.0 for s in symbols}
        entries = [{"symbol": "AAPL", "metric": "pb", "upper_1": 20, "lists": []}]
        view, issues = build_watchlist_view(entries, {"AAPL": q("AAPL", 100, book_value=4)})
        assert not issues
        assert view.iloc[0]["status"] == STATUS_UPPER_1
        assert view.iloc[0]["base_value"] == 25
        assert view.iloc[0]["book_value"] == 4
        assert bool(view.iloc[0]["triggered"])

    def test_pb_lower_trigger_and_price_unaffected(self):
        entries = [
            {"symbol": "AAA", "metric": "pb", "lower_1": 1.0},
            {"symbol": "BBB", "upper_1": 50},  # price metric 不受 BVPS 影响
        ]
        quotes = {
            "AAA": q("AAA", 100, book_value=4),  # PB 25 → 区间内
            "BBB": q("BBB", 60, book_value=4),   # 现价 60 > 50 → 上限 I
        }
        view, issues = build_watchlist_view(entries, quotes)
        assert not issues
        m = view.set_index("symbol")
        assert m.loc["AAA", "status"] == STATUS_WITHIN
        assert m.loc["BBB", "status"] == STATUS_UPPER_1

    def test_no_pb_data_marks_no_data_not_triggered(self, no_bvps_fixture):
        """BVPS 缺失 (fetch 返回 None) → 指标无数据, 不误判区间内."""
        entries = [{"symbol": "BTC-USD", "metric": "pb", "upper_1": 1}]
        view, issues = build_watchlist_view(
            entries, {"BTC-USD": q("BTC-USD", 100)}
        )
        assert any("PB 缺失" in i for i in issues)

    def test_quote_supplied_bvps_wins_over_fundamentals(self, no_bvps_fixture):
        """行情自带 BVPS (akshare 东财口径) 优先于 fundamentals 数据源."""
        import tracker.fundamentals as f

        f.fetch_book_values = lambda symbols: {
            s: 999.0 for s in symbols
        }  # 若被调用会污染结果
        entries = [{"symbol": "600036.SS", "metric": "pb", "upper_1": 1.0}]
        view, issues = build_watchlist_view(
            entries,
            {"600036.SS": q("600036.SS", 45.0, ccy="CNY", book_value=45.4)},
        )
        assert not issues
        row = view.iloc[0]
        assert abs(row["book_value"] - 45.4) < 1e-9
        assert abs(row["base_value"] - 45.0 / 45.4) < 1e-9

    def test_fundamentals_used_when_quote_has_no_bvps(self, no_bvps_fixture):
        """行情无 BVPS (yfinance/长桥/IBKR 源) 时回落 fundamentals 数据源."""
        import tracker.fundamentals as f

        f.fetch_book_values = lambda symbols: {s: 10.0 for s in symbols}
        entries = [{"symbol": "AAPL", "metric": "pb", "upper_1": 5.0}]
        view, issues = build_watchlist_view(entries, {"AAPL": q("AAPL", 100)})
        assert not issues
        assert view.iloc[0]["book_value"] == 10.0
        assert view.iloc[0]["base_value"] == 10.0

    def test_pb_without_thresholds_no_issue(self):
        """无阈值的 PB 条目不触发取 BVPS (仅跟踪)."""
        entries = [{"symbol": "BTC-USD", "metric": "pb"}]
        view, issues = build_watchlist_view(
            entries, {"BTC-USD": q("BTC-USD", 100)}
        )
        assert not issues
        assert view.iloc[0]["status"] == STATUS_WITHIN

    def test_legacy_pb_key_entry_evaluates_as_pb(self):
        """手写 {"pb": 20} (BVPS) 无 metric: 现价100 → PB 5; upper_1=4 触发上限 I."""
        entries = [{"symbol": "AAPL", "pb": 20, "upper_1": 4}]
        view, issues = build_watchlist_view(entries, {"AAPL": q("AAPL", 100)})
        assert not issues
        assert view.iloc[0]["status"] == STATUS_UPPER_1
        assert view.iloc[0]["metric"] == METRIC_PB
        assert view.iloc[0]["base_value"] == 5

    def test_triggered_entries_includes_pb(self):
        entries = [{"symbol": "AAPL", "metric": "pb", "upper_1": 10}]
        view, _ = build_watchlist_view(entries, {"AAPL": q("AAPL", 100, book_value=4)})
        assert set(triggered_entries(view)["symbol"]) == {"AAPL"}


class TestAkshareQuoteBookValue:
    def test_akshare_quote_backfills_bvps_from_pb(self, monkeypatch):
        """akshare spot 市净率列 → 反推每股净资产填入 Quote (东财实时口径)."""
        from tracker.providers import global_stocks as gs
        from tracker.symbols import parse

        df = pd.DataFrame(
            {
                "代码": ["600519", "000001"],
                "名称": ["贵州茅台", "平安银行"],
                "最新价": [1500.0, 12.0],
                "昨收": [1490.0, 12.1],
                "涨跌幅": [0.67, -0.83],
                "市净率": [7.5, 0.5],
            }
        )
        monkeypatch.setattr(gs, "_ak_spot", lambda market: df)
        quote = gs._akshare_quote(parse("600519.SS"))
        assert quote.book_value == pytest.approx(1500.0 / 7.5)

    def test_akshare_quote_pb_zero_or_missing(self, monkeypatch):
        """市净率缺失/非正 → book_value=None (不除零, 回落 fundamentals 链路)."""
        from tracker.providers import global_stocks as gs
        from tracker.symbols import parse


        df = pd.DataFrame(
            {
                "代码": ["600519", "000001"],
                "名称": ["贵州茅台", "平安银行"],
                "最新价": [1500.0, 12.0],
                "昨收": [1490.0, 12.1],
                "涨跌幅": [0.67, -0.83],
                "市净率": [None, 0.0],
            }
        )
        monkeypatch.setattr(gs, "_ak_spot", lambda market: df)
        quote = gs._akshare_quote(parse("600519.SS"))
        assert quote.book_value is None
        quote2 = gs._akshare_quote(parse("000001.SZ"))
        assert quote2.book_value is None

    def test_akshare_quote_no_pb_column(self, monkeypatch):
        """spot 表无市净率列 (港股兜底表) → book_value=None, 不报错."""
        from tracker.providers import global_stocks as gs
        from tracker.symbols import parse


        df = pd.DataFrame(
            {
                "代码": ["00700"],
                "名称": ["腾讯控股"],
                "最新价": [430.0],
                "昨收": [431.0],
                "涨跌幅": [-0.37],
            }
        )
        monkeypatch.setattr(gs, "_ak_spot", lambda market: df)
        quote = gs._akshare_quote(parse("0700.HK"))
        assert quote.book_value is None
        assert quote.price == 430.0

@pytest.fixture
def no_bvps_fixture():
    import tracker.fundamentals as f

    saved = f.fetch_book_values
    f.fetch_book_values = lambda symbols: {s: None for s in symbols}
    yield
    f.fetch_book_values = saved


class TestPBViewFixture:
    def test_no_data_via_fixture(self, no_bvps_fixture):
        entries = [{"symbol": "BTC-USD", "metric": "pb", "upper_1": 1}]
        view, issues = build_watchlist_view(
            entries, {"BTC-USD": q("BTC-USD", 100)}
        )
        assert view.iloc[0]["status"] == STATUS_NO_DATA
        assert any("PB 缺失" in i for i in issues)


class TestFundamentalsCache:
    def test_negative_cache_roundtrip(self, monkeypatch, tmp_path):
        """负缓存: 无数据标的落库后, TTL 内不再发请求."""
        import tracker.cache as cache_mod
        import tracker.fundamentals as f

        monkeypatch.setattr(cache_mod, "CACHE_DB", tmp_path / "t.db")
        cache_mod._ensure_db()
        calls = {"n": 0}

        def fake_metrics(symbols):
            calls["n"] += 1
            return {s: None for s in symbols}

        monkeypatch.setattr(f, "_fund_metrics", fake_metrics)
        assert f.fetch_book_values(["BTC-USD"]) == {"BTC-USD": None}
        assert f.fetch_book_values(["BTC-USD"]) == {"BTC-USD": None}
        assert calls["n"] == 1  # 第二次走负缓存, 不再请求

    def test_positive_cache_roundtrip(self, monkeypatch, tmp_path):
        import tracker.cache as cache_mod
        import tracker.fundamentals as f

        monkeypatch.setattr(cache_mod, "CACHE_DB", tmp_path / "t.db")
        cache_mod._ensure_db()
        monkeypatch.setattr(f, "_fund_metrics", lambda syms: {s: 12.5 for s in syms})
        assert f.fetch_book_values(["AAPL"]) == {"AAPL": 12.5}
        assert f.fetch_book_values(["AAPL"]) == {"AAPL": 12.5}

    def test_fetch_failure_is_silent_none(self, monkeypatch, tmp_path):
        import tracker.cache as cache_mod
        import tracker.fundamentals as f

        monkeypatch.setattr(cache_mod, "CACHE_DB", tmp_path / "t.db")
        cache_mod._ensure_db()

        def boom(symbols):
            raise RuntimeError("network down")

        monkeypatch.setattr(f, "_fund_metrics", boom)
        assert f.fetch_book_values(["AAPL"]) == {"AAPL": None}


class TestMetricCli:
    def test_add_with_metric_pb(self, tmp_path, capsys):
        """watchlist add --metric pb 落盘 metric=pb (列表顺序无关)."""
        from tracker.cli import build_parser

        f = tmp_path / "w.json"
        f.write_text(json.dumps({"watchlist": []}), encoding="utf-8")
        parser = build_parser()
        args = parser.parse_args(
            ["watchlist", "add", "600036.SS", "--file", str(f),
             "--metric", "pb", "--upper1", "1.2", "--lower1", "0.8"]
        )
        args.func(args)
        e = json.loads(f.read_text(encoding="utf-8"))["watchlist"][0]
        assert e["symbol"] == "600036.SS"
        assert e["metric"] == "pb"
        assert e["upper_1"] == 1.2
