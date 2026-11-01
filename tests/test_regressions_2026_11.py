"""代码审查回归: 2026-11 修复批次的边界用例 (全部离线)."""

from __future__ import annotations

import json
import os
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd
import pytest

from tracker import cache as cache_mod
from tracker import fundamentals, mcap, storage, symbols
from tracker.providers.base import Quote


# ---------------------------------------------------------------------------
# storage: 空 symbol / 设置合并 / 旧格式 / 备份 / 畸形入参
# ---------------------------------------------------------------------------


def test_load_watchlist_null_symbol_is_dropped(tmp_path):
    """JSON null 不能被 str(None) 变成幽灵美股 "NONE"."""
    p = tmp_path / "watchlist.json"
    p.write_text(
        json.dumps({"watchlist": [{"symbol": None, "note": "x"}]}),
        encoding="utf-8",
    )
    assert storage.load_watchlist(p)["watchlist"] == []


def test_save_settings_partial_keeps_existing_values(tmp_path):
    """局部保存只覆盖传入键, 不能把其它开关重置成默认值."""
    p = tmp_path / "settings.json"
    p.write_text(
        json.dumps(
            {
                "color_scheme": "intl",
                "use_ibkr": True,
                "crypto_source": "binance",
            }
        ),
        encoding="utf-8",
    )
    storage.save_settings({"prefer_akshare": True}, p)
    data = storage.load_settings(p)
    assert data["prefer_akshare"] is True
    assert data["color_scheme"] == "intl"
    assert data["use_ibkr"] is True
    assert data["crypto_source"] == "binance"


@pytest.mark.parametrize("raw", ["false", "0", "no", "off"])
def test_load_settings_string_false_is_false(tmp_path, raw):
    """手改 settings.json 写 "false" 时 bool() 不能把它当 True."""
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"use_ibkr": raw}), encoding="utf-8")
    assert storage.load_settings(p)["use_ibkr"] is False


def test_load_watchlist_legacy_group_with_string_lists(tmp_path):
    """旧 watchlists 分组里 lists 是逗号字符串/null 时不能崩, 组名要并集."""
    p = tmp_path / "watchlist.json"
    p.write_text(
        json.dumps(
            {
                "watchlists": {
                    "科技": [{"symbol": "AAPL", "lists": "成长"}],
                    "美股": [{"symbol": "AAPL", "lists": None}],
                }
            }
        ),
        encoding="utf-8",
    )
    entry = storage.load_watchlist(p)["watchlist"][0]
    assert set(entry["lists"]) == {"成长", "科技", "美股"}


def test_recover_then_save_keeps_good_backup(tmp_path):
    """主文件损坏、从 .bak 恢复后保存, 不能把坏文件覆盖到好备份上."""
    p = tmp_path / "portfolio.json"
    storage.save_portfolio({"holdings": [{"symbol": "AAPL"}]}, p)
    storage.save_portfolio({"holdings": [{"symbol": "MSFT"}]}, p)  # .bak = AAPL
    p.write_text("{broken", encoding="utf-8")
    storage.save_portfolio({"holdings": [{"symbol": "GOOG"}]}, p)
    bak = json.loads((tmp_path / "portfolio.json.bak").read_text(encoding="utf-8"))
    assert bak["holdings"][0]["symbol"] == "AAPL"


def test_save_portfolio_rejects_non_list_holdings(tmp_path):
    """畸形 holdings 不能静默清空已有持仓."""
    p = tmp_path / "portfolio.json"
    storage.save_portfolio({"holdings": [{"symbol": "AAPL"}]}, p)
    with pytest.raises(storage.CorruptDataError):
        storage.save_portfolio({"holdings": "AAPL"}, p)
    assert storage.load_portfolio(p)["holdings"][0]["symbol"] == "AAPL"


def test_save_watchlist_rejects_non_list(tmp_path):
    p = tmp_path / "watchlist.json"
    storage.save_watchlist({"watchlist": [{"symbol": "AAPL"}]}, p)
    with pytest.raises(storage.CorruptDataError):
        storage.save_watchlist({"watchlist": {"symbol": "MSFT"}}, p)
    assert storage.load_watchlist(p)["watchlist"][0]["symbol"] == "AAPL"


# ---------------------------------------------------------------------------
# symbols
# ---------------------------------------------------------------------------


def test_normalize_hk_does_not_pad_non_numeric_codes():
    assert symbols.normalize("ABC.HK") == "ABC.HK"
    assert symbols.normalize("A.HK") == "A.HK"
    assert symbols.normalize("700.HK") == "0700.HK"


# ---------------------------------------------------------------------------
# cache: 只读库不能拖垮取数
# ---------------------------------------------------------------------------


def test_cache_write_failure_is_best_effort(tmp_path, monkeypatch):
    db = tmp_path / "cache.db"
    monkeypatch.setattr(cache_mod, "CACHE_DB", db)
    cache_mod._ensure_db()
    os.chmod(db, 0o444)
    try:
        cache_mod.set_cached(
            {
                "AAPL": Quote(
                    symbol="AAPL",
                    name="Apple",
                    price=1.0,
                    prev_close=1.0,
                    change_pct=0.0,
                    currency="USD",
                )
            }
        )
    finally:
        os.chmod(db, 0o644)


# ---------------------------------------------------------------------------
# IBKR: 历史便士折算 + qualify 失败不串位
# ---------------------------------------------------------------------------


class _Bar:
    def __init__(self, d, close):
        self.date = d
        self.open = close
        self.high = close
        self.low = close
        self.close = close
        self.volume = 1


def test_ibkr_history_lse_pence_is_converted(monkeypatch):
    import tracker.ibkr as ibkr_mod

    class FakeIB:
        def qualifyContracts(self, *contracts):
            return list(contracts)

        def reqHistoricalData(self, *a, **kw):
            return [_Bar(date(2025, 1, 2), 8768.0)]

    monkeypatch.setattr(ibkr_mod, "_get_client", lambda cfg: FakeIB())
    data, reason = ibkr_mod.get_history_ibkr(
        [symbols.parse("LSEG.L")],
        months=1,
        start_date="2025-01-01",
        end_date="2025-01-10",
        cfg={"exchanges": {}},
    )
    assert reason is None
    assert data["LSEG.L"]["close"].iloc[0] == pytest.approx(87.68)


def test_ibkr_quote_pairing_survives_filtered_qualify(monkeypatch):
    """旧版 qualifyContracts 过滤失败项时, 后续合约不能整体错位."""
    import tracker.ibkr as ibkr_mod

    class Contract:
        def __init__(self):
            self.symbol = ""
            self.exchange = ""
            self.currency = ""
            self.secType = ""
            self.tradingClass = ""
            self.conId = 0

    class Ticker:
        def __init__(self, contract, price):
            self.contract = contract
            self._price = price

        def marketPrice(self):
            return self._price

        def __getattr__(self, name):
            return None

    class FakeIB:
        def qualifyContracts(self, *contracts):
            # 模拟旧版: 只返回成功项, 中间 MSFT 失败被过滤掉
            return [contracts[0], contracts[2]]

        def reqTickers(self, *contracts):
            return [Ticker(c, 10.0 + i) for i, c in enumerate(contracts)]

    fake_mod = SimpleNamespace(Contract=Contract)
    out = ibkr_mod._ibkr_chunk_quotes(
        FakeIB(),
        fake_mod,
        [symbols.parse("AAPL"), symbols.parse("MSFT"), symbols.parse("GOOG")],
        {"exchanges": {}},
    )
    assert set(out) == {"AAPL", "GOOG"}
    assert out["AAPL"].price == pytest.approx(10.0)
    assert out["GOOG"].price == pytest.approx(11.0)


# ---------------------------------------------------------------------------
# 基本面批量失败不写负缓存
# ---------------------------------------------------------------------------


def test_fundamentals_batch_failure_does_not_negative_cache(monkeypatch):
    monkeypatch.setattr(fundamentals, "_eodhd_metrics", lambda symbols: {})
    monkeypatch.setattr(
        fundamentals, "_fund_metrics", lambda symbols: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    written = []
    monkeypatch.setattr(
        fundamentals.cache_mod,
        "set_fundamental_cached",
        lambda *a, **k: written.append(a),
    )
    monkeypatch.setattr(
        fundamentals.cache_mod, "get_fundamental_cached", lambda *a, **k: None
    )
    out = fundamentals.fetch_book_values(["AAPL"])
    assert out == {"AAPL": None}
    assert written == []


# ---------------------------------------------------------------------------
# mcap: 股本变更日期保留交易所本地日历日
# ---------------------------------------------------------------------------


def test_shares_history_keeps_exchange_local_date(monkeypatch, tmp_path):
    monkeypatch.setattr("tracker.cache.CACHE_DB", tmp_path / "mcap.db")
    cache_mod._ensure_db()

    class FakeTicker:
        def __init__(self, symbol):
            pass

        def get_shares_full(self, start="1980-01-01"):
            return pd.Series(
                [100.0],
                index=pd.DatetimeIndex(["2025-01-06"], tz="Asia/Hong_Kong"),
            )

    monkeypatch.setattr("yfinance.Ticker", FakeTicker)
    df = mcap._shares_history("0700.HK", refresh=True)
    assert df is not None
    assert df["date"].iloc[0] == pd.Timestamp("2025-01-06")


# ---------------------------------------------------------------------------
# 长桥: CNH 归一为 CNY
# ---------------------------------------------------------------------------


def test_longport_cnh_normalized_to_cny(monkeypatch):
    import tracker.longport as lp
    from unittest.mock import patch

    class FakeCtx:
        def quote(self, symbols):
            return [
                SimpleNamespace(
                    symbol="700.HK", last_done=Decimal("10.0"), prev_close=Decimal("9.0")
                )
            ]

        def calc_indexes(self, symbols, indexes):
            return []

    monkeypatch.setattr(lp, "static_names_longport", lambda syms, cfg=None: ({}, {"700.HK": "CNH"}))
    with patch.object(lp, "_get_quote_ctx", return_value=FakeCtx()):
        quotes, reason = lp.get_quotes_longport([symbols.parse("0700.HK")])
    assert reason is None
    assert quotes["0700.HK"].currency == "CNY"


def test_quote_cache_respects_explicit_source(monkeypatch, tmp_path):
    """`quote --ibkr` 不能被 TTL 内 yfinance 缓存短路 (source_strict)."""
    import tracker.providers.orchestration as orch

    monkeypatch.setattr(cache_mod, "CACHE_DB", tmp_path / "quotes.db")
    cache_mod._ensure_db()
    cache_mod.set_cached(
        {
            "AAPL": Quote(
                symbol="AAPL",
                name="Apple",
                price=100.0,
                prev_close=99.0,
                change_pct=1.0,
                currency="USD",
                source="yfinance",
            )
        }
    )

    def fake_ibkr(parsed, cfg=None):
        p = parsed[0]
        return {
            p.yahoo: Quote(
                symbol=p.yahoo,
                name="Apple",
                price=200.0,
                prev_close=199.0,
                change_pct=0.5,
                currency="USD",
                source="ibkr",
            )
        }, None

    monkeypatch.setattr("tracker.ibkr.get_quotes_ibkr", fake_ibkr)
    monkeypatch.setattr(orch, "_yahoo_batch", lambda plist: {})

    quotes, _errors, _notes = orch.get_quotes(
        ["AAPL"], use_ibkr=True, source_strict=True
    )
    assert quotes["AAPL"].source == "ibkr"
    assert quotes["AAPL"].price == pytest.approx(200.0)

    # UI/设置页路径 (source_strict=False) 仍优先用缓存, 首屏不被拖慢
    cache_mod.set_cached(
        {
            "AAPL": Quote(
                symbol="AAPL",
                name="Apple",
                price=100.0,
                prev_close=99.0,
                change_pct=1.0,
                currency="USD",
                source="yfinance",
            )
        }
    )
    quotes, _errors, _notes = orch.get_quotes(["AAPL"], use_ibkr=True)
    assert quotes["AAPL"].price == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# CLI: 空导入不覆盖 / 空 CSV 有表头 / 比例线混合时区
# ---------------------------------------------------------------------------


def test_watchlist_import_empty_does_not_overwrite(tmp_path, capsys, monkeypatch):
    from tracker import cli

    p = tmp_path / "watchlist.json"
    p.write_text(
        json.dumps({"watchlist": [{"symbol": "AAPL", "lists": ["默认"]}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr("tracker.longport.fetch_watchlist_groups", lambda cfg=None: [])
    cli.main(["watchlist", "import-longport", "--file", str(p), "--overwrite"])
    assert storage.load_watchlist(p)["watchlist"][0]["symbol"] == "AAPL"
    assert "未修改文件" in capsys.readouterr().out


def test_export_empty_csv_keeps_header():
    from tracker.cli.export import _export_csv

    out = _export_csv(pd.DataFrame())
    assert out.startswith("\ufeffsymbol,")
    assert "market_value" in out.splitlines()[0]


def test_compare_ratio_handles_mixed_timezone(monkeypatch):
    from tracker import charting
    from tracker.cli import compare

    naive = pd.DataFrame(
        {"date": pd.to_datetime(["2025-01-06"]), "close": [1.0]}
    )
    aware = pd.DataFrame(
        {
            "date": pd.to_datetime(["2025-01-07"]).tz_localize("Asia/Hong_Kong"),
            "close": [2.0],
        }
    )
    monkeypatch.setattr(compare, "resolve_inputs", lambda args: (["AAPL", "0700.HK"], []))
    monkeypatch.setattr("tracker.fx.get_fx_history", lambda *a, **k: None)
    monkeypatch.setattr(charting, "build_ratio_series", lambda *a, **k: {"ok": True})
    out = compare._build_ratio(
        {"AAPL": naive, "0700.HK": aware},
        ["AAPL", "0700.HK"],
        "daily",
        fx_adjust=True,
    )
    assert out == {"ok": True}
