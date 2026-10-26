"""2026-10-27 代码审查修复的回归测试 (每个用例对应一个已修复的缺陷).

覆盖: 存储编码/缺键/设置丢键、自选 lists 字符串、旧阈值键残留、缓存库
「锁竞争误判损坏」、LSE 美元计价线市值币种、批量导入同代码多行丢量、
services 层循环导入、券商文件总量列/前导零、数字搜索原码优先级、
长桥 static_info 分块失败后续块。

全部离线 (网络函数 monkeypatch / 纯本地文件)。
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from tracker import ashare_sync, cache as cache_mod, importer, mcap, storage
from tracker.storage import dedupe_watch_entries, normalize_watch_entry


# ---------------------------------------------------------------------------
# storage: 编码兜底 / 必需键补齐 / 设置未知键保留
# ---------------------------------------------------------------------------


def test_load_portfolio_gbk_encoded_file(tmp_path):
    """GBK 另存的 portfolio.json 仍可读 (中文 Windows 编辑器常见)."""
    p = tmp_path / "portfolio.json"
    p.write_text(
        json.dumps(
            {
                "base_currency": "CNY",
                "holdings": [{"symbol": "AAPL", "note": "苹果"}],
            },
            ensure_ascii=False,
        ),
        encoding="gbk",
    )
    data = storage.load_portfolio(p)
    assert data["holdings"][0]["note"] == "苹果"


def test_load_portfolio_unreadable_encoding_raises_readable_error(tmp_path):
    """既非 UTF-8 也非 GBK 的字节 → CorruptDataError, 不是裸 UnicodeDecodeError."""
    p = tmp_path / "portfolio.json"
    p.write_bytes(b'{"holdings": "\xff\xff"}')
    with pytest.raises(storage.CorruptDataError):
        storage.load_portfolio(p)


def test_load_portfolio_fills_missing_required_keys(tmp_path):
    """手写文件缺 base_currency/holdings 时补齐 (compare --metric mcap 直接索引)."""
    p = tmp_path / "portfolio.json"
    p.write_text(json.dumps({"holdings": [{"symbol": "AAPL", "quantity": 1}]}))
    data = storage.load_portfolio(p)
    assert data["base_currency"] == storage.DEFAULT_BASE_CURRENCY
    assert data["holdings"]

    p2 = tmp_path / "p2.json"
    p2.write_text(json.dumps({"base_currency": "USD"}))
    assert storage.load_portfolio(p2)["holdings"] == []


def test_save_settings_keeps_unknown_keys(tmp_path):
    """保存设置不丢文件里的自定义键 (与 portfolio/watchlist 同语义)."""
    p = tmp_path / "settings.json"
    p.write_text(
        json.dumps({"my_note": "保留我", "color_scheme": "cn"}, ensure_ascii=False),
        encoding="utf-8",
    )
    storage.save_settings({"color_scheme": "intl"}, p)
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["my_note"] == "保留我"
    assert data["color_scheme"] == "intl"
    assert data["crypto_source"] == "auto"  # 缺失默认键补齐


def test_save_settings_still_writable_when_file_corrupt(tmp_path):
    """settings 是轻量可重建配置: 损坏文件不该让设置页永久无法保存."""
    p = tmp_path / "settings.json"
    p.write_text("not json{", encoding="utf-8")
    storage.save_settings({"color_scheme": "intl"}, p)
    assert storage.load_settings(p)["color_scheme"] == "intl"


# ---------------------------------------------------------------------------
# storage: 自选 lists 字符串 / 旧阈值键残留
# ---------------------------------------------------------------------------


def test_save_watchlist_normalizes_lists_string(tmp_path):
    """lists 写成逗号字符串时按列表写入, 不能按字符拆成 ["科","技"]."""
    p = tmp_path / "watchlist.json"
    storage.save_watchlist(
        {"watchlist": [{"symbol": "AAPL", "lists": "科技,美股"}]}, p
    )
    entry = json.loads(p.read_text(encoding="utf-8"))["watchlist"][0]
    assert entry["lists"] == ["科技", "美股"]


def test_dedupe_watch_entries_tolerates_string_lists():
    """去重函数被直接调用时 lists 是字符串也不能崩."""
    merged = dedupe_watch_entries(
        [
            {"symbol": "AAPL", "lists": "科技"},
            {"symbol": "AAPL", "lists": "美股"},
        ]
    )
    assert len(merged) == 1
    assert set(merged[0]["lists"]) == {"科技", "美股"}


def test_normalize_watch_entry_removes_legacy_threshold_keys():
    """旧键与新键并存时旧键必须删除 (否则成为永久死数据)."""
    e = normalize_watch_entry(
        {"symbol": "AAPL", "upper": 5.0, "upper_1": 7.0, "lower": 3.0}
    )
    assert "upper" not in e and "lower" not in e
    assert e["upper_1"] == 7.0
    assert e["lower_1"] == 3.0


# ---------------------------------------------------------------------------
# cache: 锁竞争不能被误判为库损坏
# ---------------------------------------------------------------------------


def test_ensure_db_does_not_quarantine_locked_database(tmp_path, monkeypatch):
    """database is locked 属瞬时错误: 不能把正在使用的库 os.replace 隔离."""
    db = tmp_path / "cache.db"
    db.write_bytes(b"")
    monkeypatch.setattr(cache_mod, "CACHE_DB", db)

    def boom(con):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(cache_mod, "_create_schema", boom)
    cache_mod._ensure_db()
    assert db.exists()
    assert not list(tmp_path.glob("cache.db.corrupt-*"))


def test_ensure_db_still_recovers_corrupt_file(tmp_path, monkeypatch):
    """真正的坏库仍走隔离重建 (缓存可再生)."""
    db = tmp_path / "cache.db"
    db.write_bytes(b"this is definitely not a sqlite database")
    monkeypatch.setattr(cache_mod, "CACHE_DB", db)
    cache_mod._ensure_db()
    assert db.exists()
    assert list(tmp_path.glob("cache.db.corrupt-*"))
    with sqlite3.connect(db) as con:
        tables = {
            r[0]
            for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "quotes" in tables


# ---------------------------------------------------------------------------
# mcap: 市值币种跟随实际报价币种 (LSE 美元计价线)
# ---------------------------------------------------------------------------


def _price_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-01-02", "2026-01-05"]),
            "open": [10.0, 11.0],
            "high": [10.0, 11.0],
            "low": [10.0, 11.0],
            "close": [10.0, 11.0],
            "volume": [0.0, 0.0],
        }
    )


def test_mcap_currency_follows_usd_quoted_lse(monkeypatch):
    """CSPX.L (LSE 美元计价) 的市值币种必须是 USD, 不能按 parse 的 GBP 折算."""
    monkeypatch.setattr(
        mcap,
        "_shares_history",
        lambda y: pd.DataFrame(
            {"date": pd.to_datetime(["2025-01-01"]), "close": [100.0]}
        ),
    )
    monkeypatch.setattr(mcap, "_price_currency_for", lambda p: "USD")
    res = mcap.history_for("CSPX.L", _price_frame())
    assert res.df is not None
    assert res.currency == "USD"


def test_price_currency_pence_and_usd_lines(monkeypatch):
    """GBp 便士折成 GBP; 美元计价的 .IL (KAP.IL) 保留 USD."""
    import tracker.providers.global_stocks as gs

    monkeypatch.setattr(gs, "_yahoo_quote_currency", lambda y: "GBp")
    assert mcap._price_currency_for(_parse("VOD.L")) == "GBP"
    monkeypatch.setattr(gs, "_yahoo_quote_currency", lambda y: "USD")
    assert mcap._price_currency_for(_parse("KAP.IL")) == "USD"


def _parse(symbol: str):
    from tracker.symbols import parse

    return parse(symbol)


# ---------------------------------------------------------------------------
# importer: 同一批导入同代码多行必须相加, 不能后行覆盖前行
# ---------------------------------------------------------------------------


def test_merge_holdings_sums_duplicate_rows_in_one_batch():
    merged, stats = importer.merge_holdings(
        [],
        [
            {"symbol": "600519.SS", "quantity": 100},
            {"symbol": "600519.SS", "quantity": 50},
        ],
    )
    assert len(merged) == 1
    assert merged[0]["quantity"] == 150.0
    assert stats["added"] == ["600519.SS"]


def test_merge_holdings_batch_sum_is_idempotent_against_existing():
    """重复同步同一账户: 批量内先相加, 再用总和替换旧值 (不翻倍)."""
    merged, _ = importer.merge_holdings(
        [{"symbol": "600519.SS", "quantity": 100}],
        [
            {"symbol": "600519.SS", "quantity": 30},
            {"symbol": "600519.SS", "quantity": 20},
        ],
    )
    assert len(merged) == 1
    assert merged[0]["quantity"] == 50.0


# ---------------------------------------------------------------------------
# services 层循环导入
# ---------------------------------------------------------------------------


def test_services_snapshot_importable_without_cli():
    """独立导入 use-case 层不能触发 services.snapshot ↔ cli 的循环导入."""
    root = Path(__file__).resolve().parents[1]
    r = subprocess.run(
        [
            sys.executable,
            "-c",
            "from tracker.services.snapshot import take_snapshot; print('ok')",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode == 0, r.stderr


# ---------------------------------------------------------------------------
# A股券商文件: 总量列优先 / Excel 数值代码补前导零
# ---------------------------------------------------------------------------


def test_ashare_prefers_total_over_available(tmp_path):
    """总持仓 1000、可用 0 (T+1 当日买入) 时不能整只丢掉."""
    p = tmp_path / "positions.csv"
    p.write_text(
        "证券代码,证券名称,总持仓,可用余额,成本价\n"
        "600519,贵州茅台,1000,0,1500\n",
        encoding="gbk",
    )
    rows, skipped = ashare_sync.parse_positions_file(p)
    assert skipped == []
    assert rows[0]["symbol"] == "600519.SS"
    assert rows[0]["quantity"] == 1000.0


def test_ashare_code_pads_numeric_excel_code():
    """Excel 数值单元格 1 → 000001.SZ (深市平安银行), 不是「非6位代码」."""
    assert ashare_sync.code_to_yahoo("1") == "000001.SZ"
    assert ashare_sync.code_to_yahoo("1.0") == "000001.SZ"
    assert ashare_sync.code_to_yahoo("2594") == "002594.SZ"
    assert ashare_sync.code_to_yahoo("600519") == "600519.SS"
    # 5 位数字不是 A股丢前导零的形态, 仍拒绝
    assert ashare_sync.code_to_yahoo("60051") is None


# ---------------------------------------------------------------------------
# search: 6 位原码优先于去零后的港股代码
# ---------------------------------------------------------------------------


def test_resolve_symbol_prefers_literal_six_digit_code(monkeypatch):
    from tracker import search

    hits = [
        {"code": "0700.HK", "name": "腾讯控股", "market": "港股", "type": "global"},
        {"code": "000700.SZ", "name": "模塑科技", "market": "A股", "type": "cn"},
    ]
    monkeypatch.setattr(search, "search_symbols", lambda q, limit=None: list(hits))
    out = search.resolve_symbol("000700")
    assert out[0]["code"] == "000700.SZ"


# ---------------------------------------------------------------------------
# longport: static_info 单块失败不丢后续块的名称/币种
# ---------------------------------------------------------------------------


def test_static_names_continues_after_chunk_failure(monkeypatch):
    from tracker import longport as lp

    monkeypatch.setattr(lp, "_BATCH", 2)
    calls = {"n": 0}

    class Info:
        def __init__(self, symbol, currency):
            self.symbol = symbol
            self.currency = currency
            self.name_cn = ""
            self.name_en = ""
            self.name_hk = ""

    def fake_ctx(fn, *args, cfg=None, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("rate limited")
        return [Info("700.HK", "HKD"), Info("9070.HK", "USD")]

    monkeypatch.setattr(lp, "_ctx_call", fake_ctx)
    _names, currencies = lp.static_names_longport(
        ["700.HK", "9070.HK", "AAPL.US", "MSFT.US"]
    )
    assert calls["n"] == 2, "首块失败后必须继续处理后续块"
    assert currencies == {"700.HK": "HKD", "9070.HK": "USD"}


# ---------------------------------------------------------------------------
# LSE 历史: 英镑计价 ETF 不能被当成便士 ÷100
# ---------------------------------------------------------------------------


def _fake_obb(df: pd.DataFrame):
    class _Res:
        def to_dataframe(self):
            return df.set_index("date")

    class _Price:
        def historical(self, **kwargs):
            return _Res()

    class _Equity:
        price = _Price()

    class _Obb:
        equity = _Equity()

    return lambda: _Obb()


def _lse_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-10-23", "2026-10-24"]),
            "open": [108.0, 109.0],
            "high": [109.0, 110.0],
            "low": [107.0, 108.0],
            "close": [109.01, 109.5],
            "volume": [1.0, 1.0],
        }
    )


def test_lse_gbp_quoted_history_not_divided(monkeypatch):
    """Yahoo 报 GBP 的是真英镑报价 (VUSA.L), 历史价不能再 ÷100."""
    from tracker.providers import global_stocks as gs

    monkeypatch.setattr(gs, "_obb", _fake_obb(_lse_df()))
    monkeypatch.setattr(gs, "_yahoo_quote_currency", lambda y: "GBP")
    out = gs._yahoo_history(_parse("VUSA.L"), "2026-01-01")
    assert out["close"].iloc[-1] == pytest.approx(109.5)


def test_lse_gbp_pence_history_still_divided(monkeypatch):
    """Yahoo 报 GBp 的仍是便士报价 (VOD.L), 历史价 ÷100 折成英镑."""
    from tracker.providers import global_stocks as gs

    monkeypatch.setattr(gs, "_obb", _fake_obb(_lse_df()))
    monkeypatch.setattr(gs, "_yahoo_quote_currency", lambda y: "GBp")
    out = gs._yahoo_history(_parse("VOD.L"), "2026-01-01")
    assert out["close"].iloc[-1] == pytest.approx(1.095)


# ---------------------------------------------------------------------------
# 指数: 中国指数声明 akshare 优先时必须真有 yfinance 兜底代码
# ---------------------------------------------------------------------------


def test_cn_indices_have_yf_fallback_code():
    from tracker.symbols import INDEX_CATALOG

    for key, entry in INDEX_CATALOG.items():
        if "ak" in entry:
            assert entry.get("yf"), f"IX.{key} 声明 akshare 优先但没有 yfinance 兜底代码"


# ---------------------------------------------------------------------------
# 行情: prefer_akshare 时港股不能被 yfinance 批量抢先
# ---------------------------------------------------------------------------


def test_prefer_akshare_skips_yahoo_batch_for_hk(monkeypatch):
    from tracker.providers import orchestration as orch
    from tracker.providers.base import Quote

    batch_syms: list[str] = []
    fetched: list[tuple[str, bool]] = []

    def fake_batch(plist):
        batch_syms.extend(p.yahoo for p in plist)
        return {}

    def fake_fetch(domain, plist, prefer, errors, crypto_source="auto"):
        fetched.extend((p.yahoo, prefer) for p in plist)
        return {
            p.yahoo: Quote(
                symbol=p.yahoo,
                name=p.yahoo,
                price=1.0,
                prev_close=1.0,
                change_pct=0.0,
                currency="HKD",
            )
            for p in plist
        }

    monkeypatch.setattr(orch, "_yahoo_batch", fake_batch)
    monkeypatch.setattr(orch, "_fetch_domain", fake_fetch)
    orch.get_quotes(["0700.HK", "AAPL"], prefer_akshare=True)
    assert "0700.HK" not in batch_syms
    assert "AAPL" in batch_syms
    assert ("0700.HK", True) in fetched


# ---------------------------------------------------------------------------
# 精度: 低价币的摘要与指标不能归零
# ---------------------------------------------------------------------------


def _low_price_df(n: int = 40) -> pd.DataFrame:
    import math as _math

    close = [1.2e-5 + _math.sin(i / 3) * 2e-6 for i in range(n)]
    return pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=n, freq="D"),
            "open": close,
            "high": [c * 1.01 for c in close],
            "low": [c * 0.99 for c in close],
            "close": close,
            "volume": [0.0] * n,
        }
    )


def test_summarize_low_price_not_rounded_to_zero():
    from tracker import charting

    s = charting.summarize_ohlc(_low_price_df())
    assert s["last_close"] > 0
    assert s["period_high"] > 0
    assert s["period_low"] > 0
    assert s["ma"]["ma5"] > 0


def test_kline_payload_low_price_indicators_not_zero():
    from tracker import charting

    payload = charting.kline_payload(
        _low_price_df(), "SHIB-USD", indicators={"macd": {}, "boll": {}}
    )
    assert payload["precision"] >= 8
    assert any(p["value"] != 0 for p in payload["macd"]["dif"])
    assert any(p["value"] != 0 for p in payload["macd"]["hist"])
    assert any(
        p["value"] != 0 for series in payload["boll"] for p in series["data"]
    )


# ---------------------------------------------------------------------------
# K线缓存键: 显式数据源偏好不能被旧缓存吞掉
# ---------------------------------------------------------------------------


def test_kline_cache_key_includes_source_flags(monkeypatch):
    from tracker import prices

    calls: list[dict] = []

    def fake_history(symbol, months=12, **kwargs):
        calls.append(kwargs)
        base = 200.0 if kwargs.get("prefer_akshare") else 100.0
        return pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-10-20"]),
                "open": [base],
                "high": [base],
                "low": [base],
                "close": [base],
                "volume": [0.0],
            }
        )

    monkeypatch.setattr(prices, "get_history", fake_history)
    cache_mod.clear()
    default = prices.get_ohlc("0700.HK", months=12)
    akshare = prices.get_ohlc("0700.HK", months=12, prefer_akshare=True)
    assert default["close"].iloc[0] == pytest.approx(100.0)
    assert akshare["close"].iloc[0] == pytest.approx(200.0)
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# Gate: 短页不等于区间结束 (上市日晚于请求起点时不能截断后续数据)
# ---------------------------------------------------------------------------


def test_gate_history_continues_after_short_first_window(monkeypatch):
    from tracker.providers import crypto

    listing = int(pd.Timestamp("2023-05-01", tz="UTC").timestamp())
    calls = {"n": 0}

    def fake_gate_get(path, params):
        calls["n"] += 1
        frm, to = int(params["from"]), int(params["to"])
        ts = max(frm, listing)
        ts -= ts % 86_400
        rows = []
        while ts <= to and len(rows) < 1000:
            rows.append([str(ts), "0", "1.0", "1.1", "0.9", "1.0", "100", "1"])
            ts += 86_400
        return rows

    monkeypatch.setattr(crypto, "_gate_get", fake_gate_get)
    df = crypto._gate_history(_parse("SUI-USD"), "2021-09-23", "2026-10-27")
    assert calls["n"] > 1, "首窗不满 1000 根时不能直接结束"
    assert df["date"].max() >= pd.Timestamp("2026-10-20")
    assert len(df) > 1000


# ---------------------------------------------------------------------------
# 东财不可达时 A股搜索仍走本地代码直查
# ---------------------------------------------------------------------------


def test_cn_search_local_fallback_when_eastmoney_down(monkeypatch):
    from tracker.providers import cn_stocks

    monkeypatch.setattr(cn_stocks, "suggest_merged", lambda q: None)
    provider = cn_stocks.CNStocksProvider()
    out = provider.search("600519.SS")
    assert [e.code for e in out] == ["600519.SS"]


# ---------------------------------------------------------------------------
# provider 路由: 日股/韩股 Market 枚举也要能解析到 global
# ---------------------------------------------------------------------------


def test_resolve_market_jp_kr_to_global():
    from tracker.providers.base import resolve
    from tracker.symbols import Market

    assert resolve(Market.JP).name == "global"
    assert resolve(Market.KR).name == "global"
    assert resolve(Market.US).name == "global"
