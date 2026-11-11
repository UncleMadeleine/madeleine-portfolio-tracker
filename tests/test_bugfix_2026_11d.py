"""2026-11 第四轮代码审查回归 (全部离线)."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pandas as pd
import pytest

from tracker.providers.base import Quote
from tracker.symbols import parse


@pytest.fixture
def cache_db(tmp_path, monkeypatch):
    """隔离缓存库, 避免测试污染真实 var/quotes_cache.db."""
    from tracker import cache as cache_mod

    db = tmp_path / "cache.db"
    monkeypatch.setattr(cache_mod, "CACHE_DB", db)
    cache_mod._db_ready.pop(str(db), None)
    cache_mod._ensure_db()
    yield cache_mod
    cache_mod._db_ready.pop(str(db), None)


# ---------------------------------------------------------------------------
# 缓存: 截断自愈 / 非有限涨跌幅
# ---------------------------------------------------------------------------


def test_cache_info_and_clear_recover_truncated_db(cache_db):
    """库被就地截断后, cache info/clear 也要能自愈 (不能永久报 no such table)."""
    cache_db.set_cached(
        {"AAPL": Quote("AAPL", "Apple", 100.0, 99.0, 1.0, "USD")}
    )
    # 保留 inode, 模拟外部截断 (0 字节有效空库)。WAL 边车必须一起清掉,
    # 否则连接可能重放旧 schema, 测不到「缺表」路径。
    with open(cache_db.CACHE_DB, "r+b") as f:
        f.truncate(0)
    for suffix in ("-wal", "-shm", "-journal"):
        cache_db.CACHE_DB.with_name(cache_db.CACHE_DB.name + suffix).unlink(
            missing_ok=True
        )

    assert cache_db.info()["total"] == 0
    assert cache_db.clear() == 0
    # 自愈后正常可写
    cache_db.set_cached(
        {"MSFT": Quote("MSFT", "Microsoft", 200.0, 198.0, 1.0, "USD")}
    )
    assert cache_db.get_cached(["MSFT"])["MSFT"].price == 200.0


def test_cached_non_finite_change_pct_normalized(cache_db):
    """历史脏行里的 inf 涨跌幅/昨收读取时必须归一为 None."""
    cache_db.set_cached(
        {"AAPL": Quote("AAPL", "Apple", 100.0, 99.0, 1.0, "USD")}
    )
    with sqlite3.connect(cache_db.CACHE_DB) as con:
        con.execute(
            "UPDATE quotes SET prev_close=?, change_pct=? WHERE symbol='AAPL'",
            (float("inf"), float("-inf")),
        )
        con.commit()
    q = cache_db.get_cached(["AAPL"])["AAPL"]
    assert q.prev_close is None
    assert q.change_pct is None


# ---------------------------------------------------------------------------
# 基本面: 半行缓存不阻塞另一指标 / 刷新可清旧值
# ---------------------------------------------------------------------------


def test_partial_fundamental_cache_does_not_block_other_metric(
    cache_db, monkeypatch
):
    from tracker import eodhd, fundamentals as f

    monkeypatch.setattr(eodhd, "fundamentals_mode", lambda cfg=None: "off")
    cache_db.set_fundamental_cached("AAPL", 12.5)  # 旧调用方只写了 BVPS
    calls = {"n": 0}

    def fake_metrics(symbols):
        calls["n"] += 1
        return {s: {"book_value": None, "market_cap": 3.0e12} for s in symbols}

    monkeypatch.setattr(f, "_fund_metrics", fake_metrics)
    assert f.fetch_market_caps(["AAPL"]) == {"AAPL": 3.0e12}
    assert calls["n"] == 1


def test_confirmed_negative_fundamental_cache_is_not_retried(cache_db, monkeypatch):
    from tracker import eodhd, fundamentals as f

    monkeypatch.setattr(eodhd, "fundamentals_mode", lambda cfg=None: "off")
    cache_db.set_fundamental_cached("BTC-USD", None, market_cap=None)
    calls = {"n": 0}

    def fake_metrics(symbols):
        calls["n"] += 1
        return {s: None for s in symbols}

    monkeypatch.setattr(f, "_fund_metrics", fake_metrics)
    assert f.fetch_market_caps(["BTC-USD"]) == {"BTC-USD": None}
    assert f.fetch_market_caps(["BTC-USD"]) == {"BTC-USD": None}
    assert calls["n"] == 0


def test_refresh_clears_stale_fundamental_values(cache_db, monkeypatch):
    from tracker import eodhd, fundamentals as f

    monkeypatch.setattr(eodhd, "fundamentals_mode", lambda cfg=None: "off")
    cache_db.set_fundamental_cached("AAPL", 10.0, market_cap=1000.0)
    monkeypatch.setattr(
        f,
        "_fund_metrics",
        lambda syms: {s: {"book_value": None, "market_cap": None} for s in syms},
    )
    assert f.fetch_book_values(["AAPL"], refresh=True) == {"AAPL": None}
    assert f.fetch_market_caps(["AAPL"], refresh=True) == {"AAPL": None}


# ---------------------------------------------------------------------------
# 数据源顺序: 能力过滤 / 缓存命中不重复批量取数
# ---------------------------------------------------------------------------


def test_cached_source_ok_skips_longport_unsupported_market(monkeypatch):
    import tracker.source_order as so
    from tracker.providers import orchestration as orch

    monkeypatch.setattr(
        so, "configured", lambda domain, channel: ["longport", "yfinance"]
    )
    q = Quote("9984.T", "SoftBank", 9000.0, 8900.0, 1.0, "JPY", source="yfinance")
    assert orch._cached_source_ok(
        q, parse("9984.T"), False, False, True, "auto"
    )


def test_cached_source_ok_skips_longport_b_share(monkeypatch):
    import tracker.source_order as so
    from tracker.providers import orchestration as orch

    monkeypatch.setattr(
        so, "configured", lambda domain, channel: ["longport", "akshare"]
    )
    q = Quote("900901.SS", "B-share", 1.0, 0.99, 1.0, "USD", source="akshare")
    assert orch._cached_source_ok(
        q, parse("900901.SS"), False, False, True, "auto"
    )


def test_cached_source_ok_skips_eodhd_unsupported_market(monkeypatch):
    import tracker.eodhd as eodhd
    import tracker.source_order as so
    from tracker.providers import orchestration as orch

    monkeypatch.setattr(
        so, "configured", lambda domain, channel: ["eodhd", "yfinance"]
    )
    monkeypatch.setattr(eodhd, "load_config", lambda: {"quotes": "fallback"})
    monkeypatch.setattr(eodhd, "available", lambda cfg: True)
    q = Quote("SAP.BE", "SAP", 100.0, 99.0, 1.0, "EUR", source="yfinance")
    assert orch._cached_source_ok(
        q, parse("SAP.BE"), False, False, False, "auto"
    )


def test_legacy_prefetch_excludes_fresh_cache(monkeypatch):
    """未配置顺序时, 已从磁盘缓存命中的代码不应再打长桥批量行情."""
    from tracker.providers import orchestration as orch

    fresh = Quote("AAPL", "Apple", 100.0, 99.0, 1.0, "USD")
    monkeypatch.setattr(
        orch.cache_mod, "get_cached", lambda syms, ttl=300: {"AAPL": fresh}
    )
    seen: list[list[str]] = []

    def fake_longport(plist):
        seen.append([p.yahoo for p in plist])
        return {}, None

    monkeypatch.setattr(
        "tracker.longport.get_quotes_longport", fake_longport
    )
    # 只关心 prefetch 列表; global 批量/逐代码阶段全部返回空, 不联网。
    monkeypatch.setattr(orch, "_yahoo_batch", lambda plist: {})
    from tracker.providers.global_stocks import GlobalStocksProvider

    monkeypatch.setattr(
        GlobalStocksProvider,
        "fetch_quote",
        lambda self, p, prefer_first=False: (_ for _ in ()).throw(
            RuntimeError("no network")
        ),
    )
    orch.get_quotes(["AAPL", "MSFT"], use_longport=True)
    assert seen == [["MSFT"]]


# ---------------------------------------------------------------------------
# 其余收敛: 极端字段 / 多空抵消 / ICS NaT / 钱包 RPC
# ---------------------------------------------------------------------------


def test_dotted_us_class_and_preferred_symbols_are_normalized():
    from tracker.symbols import normalize, parse

    assert normalize("BRK.B") == "BRK-B"
    assert normalize("WFC.PL") == "WFC-PL"
    assert parse("BRK.B").yahoo == "BRK-B"
    assert parse("WFC.PL").yahoo == "WFC-PL"
    assert parse("BRK.B").type == "global"


def test_finite_float_shared_helper():
    from tracker.util import finite_float

    assert finite_float(None) is None
    assert finite_float("") is None
    assert finite_float("nan") is None
    assert finite_float("inf") is None
    assert finite_float(object()) is None
    assert finite_float("12.5") == 12.5
    assert finite_float(0) == 0.0


def test_build_view_non_scalar_quantity_is_issue_not_crash():
    from tracker.analytics import build_view

    q = Quote("AAPL", "Apple", 100.0, 99.0, 1.0, "USD")
    view, issues = build_view(
        [{"symbol": "AAPL", "quantity": [1]}], {"AAPL": q}, {"USD": 1.0}
    )
    assert view.empty
    assert any("数量无法解析" in i for i in issues)


def test_nav_holdings_net_zero_dropped():
    from tracker.nav import _holdings_by_symbol

    agg, issues = _holdings_by_symbol(
        {
            "holdings": [
                {"symbol": "AAPL", "quantity": 10, "avg_cost": 100},
                {"symbol": "AAPL", "quantity": -10, "avg_cost": 200},
            ]
        }
    )
    assert agg == {}
    assert any("抵消" in i for i in issues)


def test_importer_mixed_long_short_cost_is_unknown():
    from tracker import importer

    merged, _ = importer.merge_holdings(
        [],
        [
            {"symbol": "AAPL", "quantity": 10, "avg_cost": 100},
            {"symbol": "AAPL", "quantity": -5, "avg_cost": 200},
        ],
    )
    assert merged[0]["quantity"] == 5.0
    assert merged[0]["avg_cost"] is None


def test_importer_cross_source_fills_missing_cost():
    from tracker import importer

    merged, _ = importer.merge_holdings(
        [{"symbol": "BTC-USD", "quantity": 1, "import_source": "wallet:a"}],
        [{"symbol": "BTC-USD", "quantity": 1, "avg_cost": 50000}],
    )
    assert merged[0]["avg_cost"] == 50000
    assert merged[0]["quantity"] == 2.0


def test_ics_skips_nat_date():
    from tracker.ics import build_ics

    text = build_ics(
        [{"symbol": "AAPL", "type": "earnings", "date": pd.NaT}]
    )
    assert "DTSTART" not in text


def test_ashare_to_float_rejects_inf_and_handles_signed_currency():
    from tracker.ashare_sync import _to_float

    assert _to_float("inf") is None
    assert _to_float("1e999") is None
    assert _to_float("-¥1,000") == -1000.0


def test_wallet_rpc_error_wins_over_null_result(monkeypatch):
    from tracker import wallet

    class Resp:
        status_code = 200
        text = ""

        def json(self):
            return {
                "jsonrpc": "2.0",
                "result": None,
                "error": {"code": -32000, "message": "boom"},
            }

    class Req:
        def post(self, *args, **kwargs):
            return Resp()

    monkeypatch.setattr(wallet, "_requests", lambda: Req())
    with pytest.raises(RuntimeError, match="RPC 错误"):
        wallet._rpc_post("https://rpc.example", {"method": "eth_call"})


def test_wallet_token_decimals_inf_is_readable_error():
    from tracker import wallet

    with pytest.raises(ValueError, match="decimals"):
        wallet._validate_token_entry(
            {"symbol": "X", "decimals": float("inf")}, "test"
        )


def test_partial_longport_history_is_not_disk_cached(monkeypatch):
    """长桥翻页中途失败的截断序列只展示, 不能按完整窗口写 30 分钟缓存."""
    from tracker import prices

    df = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-01-02", "2026-01-05"]),
            "open": [10.0, 11.0],
            "high": [10.5, 11.5],
            "low": [9.5, 10.5],
            "close": [10.0, 11.0],
            "volume": [1.0, 1.0],
        }
    )
    df.attrs["partial"] = True
    writes: list = []
    monkeypatch.setattr(prices, "get_history", lambda *a, **k: df)
    monkeypatch.setattr(prices.cache_mod, "get_ohlc_cached", lambda *a, **k: None)
    monkeypatch.setattr(
        prices.cache_mod, "set_ohlc_cached", lambda *a, **k: writes.append(a)
    )
    out = prices.get_ohlc("0700.HK", months=60, use_longport=True)
    assert len(out) == 2
    assert writes == []


def test_snapshot_forwards_source_strict(monkeypatch):
    from tracker.services import snapshot as snap

    q = Quote("AAPL", "Apple", 100.0, 99.0, 1.0, "USD", source="ibkr")
    seen: dict = {}

    def fake_quotes(symbols, **kwargs):
        seen.update(kwargs)
        return {s: q for s in symbols}, {}, []

    monkeypatch.setattr(snap.prices, "get_quotes", fake_quotes)
    monkeypatch.setattr(snap, "get_fx_rates", lambda *a, **k: ({"USD": 1.0}, []))
    snap.take_snapshot({"holdings": [{"symbol": "AAPL", "quantity": 1}]})
    assert seen["source_strict"] is False
    snap.take_snapshot(
        {"holdings": [{"symbol": "AAPL", "quantity": 1}]}, source_strict=True
    )
    assert seen["source_strict"] is True


def test_clear_source_caches_clears_ah_page(monkeypatch):
    from tracker import cache as cache_mod
    from tracker.providers import orchestration
    from tracker.ui import fundamentals_page, index_page, kline_page, portfolio_page
    from tracker.ui import settings_page

    called: list[str] = []

    class DummyCache:
        def clear(self):
            called.append("dummy")

    for name in (
        "clear_quotes",
        "clear_fundamentals",
        "clear_fx",
        "clear_ohlc",
    ):
        monkeypatch.setattr(cache_mod, name, lambda: 0)
    monkeypatch.setattr(orchestration, "neg_clear_all", lambda: None)
    monkeypatch.setattr(kline_page, "clear_kline_caches", lambda: None)
    monkeypatch.setattr(index_page, "cached_index_kline", DummyCache())
    monkeypatch.setattr(portfolio_page, "cached_quotes", DummyCache())
    monkeypatch.setattr(portfolio_page, "cached_fx", DummyCache())
    monkeypatch.setattr(
        fundamentals_page, "clear_ah_caches", lambda: called.append("ah")
    )

    settings_page._clear_source_caches()
    assert "ah" in called


def test_portfolio_nav_guard_ignores_unsaved_editor_changes():
    from tracker.ui.portfolio_page import _holdings_unchanged

    saved = [{"symbol": "AAPL", "quantity": 2, "avg_cost": 100}]
    assert _holdings_unchanged(
        [{"symbol": "AAPL", "quantity": 2.0, "avg_cost": 100.0}], saved
    )
    # 别名归一后仍算同一持仓
    assert _holdings_unchanged(
        [{"symbol": "600519.SH", "quantity": 1}],
        [{"symbol": "600519.SS", "quantity": 1}],
    )
    assert not _holdings_unchanged(
        [{"symbol": "AAPL", "quantity": 100, "avg_cost": 100}], saved
    )


def test_init_restricts_longport_credentials(tmp_path, monkeypatch):
    import stat

    from tracker.cli import init as init_cli

    monkeypatch.setattr(init_cli, "data_home", lambda: tmp_path)
    init_cli.cmd_init(SimpleNamespace(force=True, json=True))
    mode = stat.S_IMODE((tmp_path / "longport.json").stat().st_mode)
    assert mode == 0o600


def test_history_summary_cleans_inconsistent_ohlc(monkeypatch, capsys):
    from tracker.cli import history as history_cli

    df = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-01-02", "2026-01-05"]),
            "open": [10.0, 11.0],
            "high": [10.5, 11.5],
            "low": [9.5, 10.5],
            "close": [10.0, 12.0],  # 第二行 close > high, 属脏数据
            "volume": [1.0, 1.0],
        }
    )
    monkeypatch.setattr(history_cli.prices, "get_history", lambda *a, **k: df)
    history_cli.cmd_history(
        SimpleNamespace(
            symbol="AAPL",
            months=1,
            akshare=False,
            ibkr=False,
            longport=False,
            crypto_source="auto",
            json=False,
            rows=5,
        )
    )
    out = capsys.readouterr().out
    assert "共 1 个交易日" in out
    assert "区间高 10.500 / 低 9.500" in out
