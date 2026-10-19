"""metric (阈值基准指标) 测试: price=现价 / pb=市净率 / mcap=总市值。

覆盖: metric 推断与归一 (rules + storage)、PB/mcap 状态判定 (services)、
缓存 (含负缓存语义)、指标缺失降级、build_watchlist_view 组装。
全部离线 (Quote 手工构造, fundamentals monkeypatch)。
"""

import json

import pandas as pd
import pytest

from tracker.prices import Quote
from tracker.services.rules import (
    METRIC_MCAP,
    METRIC_PB,
    METRIC_PRICE,
    STATUS_LOWER_1,
    STATUS_NO_DATA,
    STATUS_UPPER_1,
    STATUS_WITHIN,
    entry_market_cap,
    metric_for_entry,
    metric_value_for_quote,
)
from tracker.services.watchlist import build_watchlist_view, triggered_entries
from tracker.storage import normalize_watch_entry


def q(sym, price, ccy="USD", book_value=None, market_cap=None):
    return Quote(
        symbol=sym,
        name=sym,
        price=price,
        prev_close=price,
        change_pct=1.0,
        currency=ccy,
        book_value=book_value,
        market_cap=market_cap,
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

    def test_explicit_mcap(self):
        assert metric_for_entry({"metric": "mcap"}) == METRIC_MCAP

    def test_legacy_mcap_key_infers_metric(self):
        """旧手写 mcap/market_cap (总市值) 无 metric: 自动按市值基准解释."""
        assert metric_for_entry({"mcap": 1e12}) == METRIC_MCAP
        assert metric_for_entry({"market_cap": 5e10}) == METRIC_MCAP

    def test_empty_mcap_key_stays_price(self):
        assert metric_for_entry({"mcap": None}) == METRIC_PRICE

    def test_pb_key_wins_over_mcap_key(self):
        """两条手写键并存时 PB 优先 (历史上 PB 键先落地)."""
        assert metric_for_entry({"pb": 4.0, "mcap": 1e12}) == METRIC_PB

    def test_non_finite_or_garbage_override_does_not_switch_metric(self):
        """NaN/inf/非数字覆盖值不算有效数据, 不得把基准切到 pb/mcap.

        否则 data_editor 空单元格 (NaN) 或手改 JSON 的垃圾值会让价格阈值被
        当成市净率/市值比较, 每次评估都误报。
        """
        assert metric_for_entry({"symbol": "AAPL", "mcap": float("nan")}) == (
            METRIC_PRICE
        )
        assert metric_for_entry({"symbol": "AAPL", "pb": float("inf")}) == (
            METRIC_PRICE
        )
        assert metric_for_entry({"symbol": "AAPL", "market_cap": "abc"}) == (
            METRIC_PRICE
        )
        # 有效数值仍按键推断
        assert metric_for_entry({"symbol": "AAPL", "pb": 5.0}) == METRIC_PB
        assert metric_for_entry({"symbol": "AAPL", "mcap": 1e12}) == METRIC_MCAP


class TestMetricNormalization:
    def test_metric_pb_kept(self):
        e = normalize_watch_entry({"symbol": "AAPL", "metric": "PB", "upper_1": 5})
        assert e["metric"] == "pb"

    def test_metric_price_dropped(self):
        """缺省基准 (无 metric 键) 不落盘, 文件保持干净."""
        e = normalize_watch_entry({"symbol": "AAPL"})
        assert "metric" not in e

    def test_metric_price_explicit_kept(self):
        """显式 price 落盘钉死: 遗留 mcap/pb 覆盖键不会在下次加载时把基准推断回去."""
        e = normalize_watch_entry({"symbol": "AAPL", "metric": "price", "mcap": 1e12})
        assert e["metric"] == "price"
        # 往返稳定: 对归一结果再归一一次仍是 price (修复前会被推断回 mcap)
        assert normalize_watch_entry(e)["metric"] == "price"

    def test_metric_invalid_dropped(self):
        """非法 metric 归一为 price 并落盘 (不再静默丢弃后靠按键推断)."""
        e = normalize_watch_entry({"symbol": "AAPL", "metric": "roe"})
        assert e["metric"] == "price"

    def test_legacy_pb_key_gets_metric_stamped(self):
        e = normalize_watch_entry({"symbol": "AAPL", "pb": 5.0})
        assert e["metric"] == "pb"

    def test_metric_mcap_kept(self):
        e = normalize_watch_entry({"symbol": "AAPL", "metric": "mcap", "upper_1": 1e12})
        assert e["metric"] == "mcap"

    def test_legacy_market_cap_key_converges_to_mcap(self):
        """别名 market_cap 收敛为 mcap 键并保留数值, metric 落 mcap."""
        e = normalize_watch_entry({"symbol": "AAPL", "market_cap": 1e12})
        assert e["metric"] == "mcap"
        assert e["mcap"] == 1e12
        assert "market_cap" not in e

    def test_empty_market_cap_key_dropped(self):
        e = normalize_watch_entry({"symbol": "AAPL", "market_cap": None})
        assert "mcap" not in e and "market_cap" not in e
        assert "metric" not in e  # 仍是 price 基准, 不落盘


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

    def test_mcap_metric_returns_market_cap(self):
        assert metric_value_for_quote(METRIC_MCAP, q("AAPL", 100, market_cap=3e12)) == 3e12

    def test_mcap_missing_is_none(self):
        assert metric_value_for_quote(METRIC_MCAP, q("AAPL", 100)) is None
        assert metric_value_for_quote(METRIC_MCAP, q("AAPL", 100, market_cap=0)) is None
        assert metric_value_for_quote(METRIC_MCAP, q("AAPL", 100, market_cap=-5)) is None


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


@pytest.fixture
def no_fundamentals_fixture():
    """基本面数据源返回空 (BVPS/mcap 均缺), 验证缺失降级路径."""
    import tracker.fundamentals as f

    saved = (f.fetch_book_values, f.fetch_market_caps)
    f.fetch_book_values = lambda symbols: {s: None for s in symbols}
    f.fetch_market_caps = lambda symbols: {s: None for s in symbols}
    yield
    (f.fetch_book_values, f.fetch_market_caps) = saved


class TestMcapView:
    def test_mcap_threshold_triggers_upper(self, no_fundamentals_fixture):
        """总市值 4e12, upper_1=3.5e12 → 触发上限 I."""
        entries = [{"symbol": "AAPL", "metric": "mcap", "upper_1": 3.5e12, "lists": []}]
        view, issues = build_watchlist_view(
            entries, {"AAPL": q("AAPL", 200, market_cap=4e12)}
        )
        assert not issues
        row = view.iloc[0]
        assert row["status"] == STATUS_UPPER_1
        assert row["metric"] == METRIC_MCAP
        assert row["base_value"] == 4e12
        assert row["market_cap"] == 4e12
        assert bool(row["triggered"])

    def test_mcap_lower_trigger_and_price_unaffected(self, no_fundamentals_fixture):
        """mcap 条目按市值判定; 同表 price 条目不受 market_cap 影响."""
        entries = [
            {"symbol": "AAA", "metric": "mcap", "lower_1": 5e12},
            {"symbol": "BBB", "upper_1": 50},
        ]
        quotes = {
            "AAA": q("AAA", 100, market_cap=4e12),  # 4e12 < 5e12 → 下限 I
            "BBB": q("BBB", 60, market_cap=9e12),   # 现价 60 > 50 → 上限 I
        }
        view, issues = build_watchlist_view(entries, quotes)
        assert not issues
        m = view.set_index("symbol")
        assert m.loc["AAA", "status"] == STATUS_LOWER_1
        assert m.loc["AAA", "base_value"] == 4e12
        assert m.loc["BBB", "status"] == STATUS_UPPER_1
        assert pd.isna(m.loc["BBB", "base_value"])

    def test_mcap_no_data_marks_no_data_not_triggered(self, no_fundamentals_fixture):
        """总市值缺失 (crypto/无数据源) → 指标无数据, 不误判区间内."""
        entries = [{"symbol": "BTC-USD", "metric": "mcap", "upper_1": 1e12}]
        view, issues = build_watchlist_view(entries, {"BTC-USD": q("BTC-USD", 100)})
        assert view.iloc[0]["status"] == STATUS_NO_DATA
        assert not bool(view.iloc[0]["triggered"])
        assert any("市值缺失" in i for i in issues)

    def test_mcap_without_thresholds_no_fundamentals_call(self, monkeypatch):
        """无阈值的 mcap 条目不拉基本面 (仅跟踪)."""
        import tracker.fundamentals as f

        calls = {"n": 0}

        def boom(symbols):
            calls["n"] += 1
            return {s: 1e12 for s in symbols}

        monkeypatch.setattr(f, "fetch_market_caps", boom)
        monkeypatch.setattr(f, "fetch_book_values", boom)
        entries = [{"symbol": "BTC-USD", "metric": "mcap"}]
        view, issues = build_watchlist_view(
            entries, {"BTC-USD": q("BTC-USD", 100, market_cap=2e12)}
        )
        assert calls["n"] == 0
        assert not issues
        assert view.iloc[0]["status"] == STATUS_WITHIN

    def test_quote_supplied_mcap_wins_over_fundamentals(self, monkeypatch):
        """行情自带总市值 (akshare/长桥实时口径) 优先于 fundamentals."""
        import tracker.fundamentals as f

        monkeypatch.setattr(f, "fetch_market_caps", lambda symbols: {s: 1.0 for s in symbols})
        entries = [{"symbol": "600519.SS", "metric": "mcap", "upper_1": 1.9e12}]
        view, issues = build_watchlist_view(
            entries,
            {"600519.SS": q("600519.SS", 1500.0, ccy="CNY", market_cap=1.9e12)},
        )
        assert not issues
        row = view.iloc[0]
        assert row["market_cap"] == 1.9e12
        assert row["status"] == STATUS_UPPER_1  # 边界含等号

    def test_entry_written_mcap_wins_over_quote(self, monkeypatch):
        """条目手写 mcap 覆盖 (离线固定口径) 优先于行情自带."""
        import tracker.fundamentals as f

        monkeypatch.setattr(f, "fetch_market_caps", lambda symbols: {s: 1.0 for s in symbols})
        entries = [{"symbol": "AAPL", "mcap": 2e12, "upper_1": 1.5e12}]
        view, issues = build_watchlist_view(
            entries, {"AAPL": q("AAPL", 200, market_cap=3e12)}
        )
        assert not issues
        assert view.iloc[0]["market_cap"] == 2e12
        assert view.iloc[0]["base_value"] == 2e12
        assert view.iloc[0]["status"] == STATUS_UPPER_1

    def test_fundamentals_used_when_quote_has_no_mcap(self, monkeypatch):
        """行情无 market_cap (yfinance/IBKR 源) 时回落 fundamentals."""
        import tracker.fundamentals as f

        monkeypatch.setattr(f, "fetch_market_caps", lambda symbols: {s: 8e12 for s in symbols})
        entries = [{"symbol": "AAPL", "metric": "mcap", "upper_1": 7e12}]
        view, issues = build_watchlist_view(entries, {"AAPL": q("AAPL", 200)})
        assert not issues
        assert view.iloc[0]["market_cap"] == 8e12
        assert view.iloc[0]["status"] == STATUS_UPPER_1

    def test_mcap_and_pb_entries_share_one_batch(self, monkeypatch):
        """同批 PB + mcap 条目: 基本面按指标各取一次缓存 (进程内去重)."""
        import tracker.fundamentals as f

        calls = {"bv": 0, "mc": 0}

        def bv(symbols):
            calls["bv"] += 1
            return {s: 4.0 for s in symbols}

        def mc(symbols):
            calls["mc"] += 1
            return {s: 5e12 for s in symbols}

        monkeypatch.setattr(f, "fetch_book_values", bv)
        monkeypatch.setattr(f, "fetch_market_caps", mc)
        entries = [
            {"symbol": "AAA", "metric": "pb", "upper_1": 20},
            {"symbol": "BBB", "metric": "mcap", "upper_1": 4e12},
        ]
        quotes = {"AAA": q("AAA", 100), "BBB": q("BBB", 200)}
        view, issues = build_watchlist_view(entries, quotes)
        assert not issues
        assert calls == {"bv": 1, "mc": 1}
        m = view.set_index("symbol")
        assert m.loc["AAA", "status"] == STATUS_UPPER_1  # PB 25 > 20
        assert m.loc["BBB", "status"] == STATUS_UPPER_1  # 5e12 > 4e12

    def test_legacy_market_cap_key_entry_evaluates_as_mcap(self):
        """手写 {"market_cap": 4e12} 无 metric: 归一为 mcap 后按市值判定."""
        entries = [{"symbol": "AAPL", "market_cap": 4e12, "upper_1": 3.5e12}]
        view, _ = build_watchlist_view(
            entries, {"AAPL": q("AAPL", 200, market_cap=9e12)}
        )
        row = view.iloc[0]
        assert row["metric"] == METRIC_MCAP
        assert row["base_value"] == 4e12
        assert row["status"] == STATUS_UPPER_1

    def test_entry_market_cap_helper_reads_both_keys(self):
        assert entry_market_cap({"mcap": 1e12}) == 1e12
        assert entry_market_cap({"market_cap": 2e12}) == 2e12
        assert entry_market_cap({}) is None
        assert entry_market_cap({"market_cap": "x"}) is None

    def test_mcap_distance_metrics(self):
        """距离按市值阈值口径: 距上限 I% = (上限/市值 - 1)*100."""
        entries = [{"symbol": "AAA", "metric": "mcap", "upper_1": 5e12}]
        view, _ = build_watchlist_view(
            entries, {"AAA": q("AAA", 100, market_cap=4e12)}
        )
        row = view.iloc[0]
        assert row["status"] == STATUS_WITHIN
        assert abs(row["dist_upper_1_pct"] - (5e12 / 4e12 - 1) * 100) < 1e-9

    def test_triggered_entries_includes_mcap(self):
        entries = [{"symbol": "AAPL", "metric": "mcap", "upper_1": 1e12}]
        view, _ = build_watchlist_view(
            entries, {"AAPL": q("AAPL", 200, market_cap=4e12)}
        )
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

    def test_akshare_quote_backfills_market_cap(self, monkeypatch):
        """spot 总市值列 (本地币种) → 填入 Quote.market_cap."""
        from tracker.providers import global_stocks as gs
        from tracker.symbols import parse

        df = pd.DataFrame(
            {
                "代码": ["600519"],
                "名称": ["贵州茅台"],
                "最新价": [1500.0],
                "昨收": [1490.0],
                "涨跌幅": [0.67],
                "总市值": [1.9e12],
            }
        )
        monkeypatch.setattr(gs, "_ak_spot", lambda market: df)
        quote = gs._akshare_quote(parse("600519.SS"))
        assert quote.currency == "CNY"
        assert quote.market_cap == 1.9e12

    def test_akshare_quote_mcap_zero_or_missing(self, monkeypatch):
        """总市值缺失/非正 → market_cap=None (回落 fundamentals)."""
        from tracker.providers import global_stocks as gs
        from tracker.symbols import parse

        df = pd.DataFrame(
            {
                "代码": ["600519", "000001"],
                "名称": ["贵州茅台", "平安银行"],
                "最新价": [1500.0, 12.0],
                "昨收": [1490.0, 12.1],
                "涨跌幅": [0.67, -0.83],
                "总市值": [None, 0.0],
            }
        )
        monkeypatch.setattr(gs, "_ak_spot", lambda market: df)
        assert gs._akshare_quote(parse("600519.SS")).market_cap is None
        assert gs._akshare_quote(parse("000001.SZ")).market_cap is None

    def test_akshare_quote_no_mcap_column(self, monkeypatch):
        """无总市值列 (表结构变化) → market_cap=None, 不报错."""
        from tracker.providers import global_stocks as gs
        from tracker.symbols import parse

        df = pd.DataFrame(
            {"代码": ["00700"], "名称": ["腾讯控股"], "最新价": [430.0], "昨收": [431.0]}
        )
        monkeypatch.setattr(gs, "_ak_spot", lambda market: df)
        quote = gs._akshare_quote(parse("0700.HK"))
        assert quote.market_cap is None
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
        monkeypatch.setattr(
            f,
            "_fund_metrics",
            lambda syms: {s: {"book_value": 12.5, "market_cap": None} for s in syms},
        )
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

    def test_market_cap_positive_cache_roundtrip(self, monkeypatch, tmp_path):
        """_fund_metrics 一次返回 book_value + market_cap, 两个入口各取所需."""
        import tracker.cache as cache_mod
        import tracker.fundamentals as f

        monkeypatch.setattr(cache_mod, "CACHE_DB", tmp_path / "t.db")
        cache_mod._ensure_db()
        monkeypatch.setattr(
            f,
            "_fund_metrics",
            lambda syms: {
                s: {"book_value": 12.5, "market_cap": 3.0e12} for s in syms
            },
        )
        assert f.fetch_market_caps(["AAPL"]) == {"AAPL": 3.0e12}
        assert f.fetch_market_caps(["AAPL"]) == {"AAPL": 3.0e12}

    def test_market_cap_negative_cache_roundtrip(self, monkeypatch, tmp_path):
        """mcap 缺失 (crypto 等) 同样落负缓存, TTL 内不重复请求."""
        import tracker.cache as cache_mod
        import tracker.fundamentals as f

        monkeypatch.setattr(cache_mod, "CACHE_DB", tmp_path / "t.db")
        cache_mod._ensure_db()
        calls = {"n": 0}

        def fake_metrics(symbols):
            calls["n"] += 1
            return {s: {"book_value": None, "market_cap": None} for s in symbols}

        monkeypatch.setattr(f, "_fund_metrics", fake_metrics)
        assert f.fetch_market_caps(["BTC-USD"]) == {"BTC-USD": None}
        assert f.fetch_market_caps(["BTC-USD"]) == {"BTC-USD": None}
        assert calls["n"] == 1

    def test_bvps_write_does_not_erase_cached_mcap(self, monkeypatch, tmp_path):
        """只写 BVPS 的旧调用方不得把已缓存市值冲掉 (同表同 TTL)."""
        import tracker.cache as cache_mod

        monkeypatch.setattr(cache_mod, "CACHE_DB", tmp_path / "t.db")
        cache_mod._ensure_db()
        cache_mod.set_fundamental_cached("AAPL", 12.5, market_cap=3.0e12)
        cache_mod.set_fundamental_cached("AAPL", 13.0)  # 只补 BVPS
        cached = cache_mod.get_fundamental_cached("AAPL")
        assert cached == {"book_value": 13.0, "market_cap": 3.0e12}


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

    def test_add_with_metric_mcap(self, tmp_path, capsys):
        """watchlist add --metric mcap 落盘 metric=mcap, 阈值为绝对金额."""
        from tracker.cli import build_parser

        f = tmp_path / "w.json"
        f.write_text(json.dumps({"watchlist": []}), encoding="utf-8")
        parser = build_parser()
        args = parser.parse_args(
            ["watchlist", "add", "AAPL", "--file", str(f),
             "--metric", "mcap", "--upper1", "4000000000000",
             "--lower1", "3000000000000"]
        )
        args.func(args)
        e = json.loads(f.read_text(encoding="utf-8"))["watchlist"][0]
        assert e["symbol"] == "AAPL"
        assert e["metric"] == "mcap"
        assert e["upper_1"] == 4.0e12
        assert e["lower_1"] == 3.0e12

    def test_metric_choices_include_mcap(self):
        """--metric 接受 mcap (parser 层不因未知选项拒签)."""
        from tracker.cli import build_parser

        args = build_parser().parse_args(
            ["watchlist", "add", "AAPL", "--metric", "mcap"]
        )
        assert args.metric == "mcap"
