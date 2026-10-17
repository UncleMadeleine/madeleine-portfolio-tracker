"""本轮代码审查修复的回归测试 (每个用例对应一个已修复的 bug).

覆盖: 阈值距离口径/去重/基本面优先级/负缓存/Gate 翻页/LSE 尾点/BVPS 缓存
COALESCE/长桥域过滤/TRON 字段名/tokenlist 校验/空头收益率/低价币精度/
JSON NaN 安全/UI 闭包绑定与列格式。均为不联网的单元/组件级测试。
"""

from __future__ import annotations

import json

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from tracker import charting
from tracker.analytics import build_view, summarize
from tracker.ibkr import ibkr_to_yahoo
from tracker.providers import crypto
from tracker.providers import orchestration as orch
from tracker.providers.base import Quote
from tracker.services.rules import evaluate_thresholds
from tracker.services.watchlist import (
    build_watchlist_view,
    entries_for,
    fetch_watchlist_view,
    merge_entries,
)
from tracker.storage import _clean_rows, dedupe_watch_entries, normalize_watch_entry
from tracker.symbols import parse


# ---------------------------------------------------------------------------
# 阈值规则: dist_lower_2_pct 缺失 + 下限距离口径不统一
# ---------------------------------------------------------------------------


class TestThresholdDistances:
    def test_dist_lower_2_computed(self):
        """lower_2 的距离键必须计算 (report/snapshot/UI 都声明了该列)."""
        _, dist = evaluate_thresholds({"lower_1": 190.0, "lower_2": 150.0}, 180.0)
        assert "dist_lower_2_pct" in dist
        assert abs(dist["dist_lower_2_pct"] - (150 / 180 - 1) * 100) < 1e-9

    def test_lower_distance_uses_unified_basis(self):
        """距离口径统一为「触发还需变动百分之几」: 上下限同基准 (阈值/现价).

        修复前下限用 (现价/阈值-1): 现价 150/下限 100 显示 +50%, 而实际
        只需下跌 33.33% 即触发, 与上限口径不可比。
        """
        _, dist = evaluate_thresholds({"lower_1": 100.0}, 150.0)
        assert abs(dist["dist_lower_1_pct"] - (100 / 150 - 1) * 100) < 1e-9
        _, dist_u = evaluate_thresholds({"upper_1": 200.0}, 150.0)
        assert abs(dist_u["dist_upper_1_pct"] - (200 / 150 - 1) * 100) < 1e-9

    def test_zero_price_no_distance(self):
        """现价 <=0 时不算距离 (除零保护)."""
        _, dist = evaluate_thresholds({"upper_1": 10.0, "lower_1": 5.0}, 0.0)
        assert dist == {}


# ---------------------------------------------------------------------------
# 自选条目: 去重 / 无阈值条目优先级 / 请求过滤 / issue 去重
# ---------------------------------------------------------------------------


class TestWatchlistViewFixes:
    def test_merge_entries_dedupes_same_symbol(self):
        """同代码多条 (外部编辑/旧文件) 读路径去重, lists 并集."""
        data = {
            "watchlist": [
                {"symbol": "AAPL", "lists": ["科技"], "upper_1": 150.0},
                {"symbol": "AAPL", "lists": ["美股"], "upper_1": 150.0},
            ]
        }
        merged = merge_entries(data)
        assert len(merged) == 1
        assert set(merged[0]["lists"]) == {"科技", "美股"}
        # entries_for 同样去重 (report/snapshot 的入口)
        assert len(entries_for(data, "科技")) == 1

    def test_load_watchlist_dedupes(self, tmp_path):
        from tracker.storage import load_watchlist

        p = tmp_path / "wl.json"
        p.write_text(
            json.dumps(
                {
                    "watchlist": [
                        {"symbol": "AAPL", "lists": ["A"]},
                        {"symbol": "AAPL", "lists": ["B"]},
                    ]
                }
            ),
            encoding="utf-8",
        )
        loaded = load_watchlist(p)
        assert len(loaded["watchlist"]) == 1
        assert set(loaded["watchlist"][0]["lists"]) == {"A", "B"}

    def test_metric_only_entries_honor_handwritten_and_columns(self):
        """无阈值的 pb/mcap 条目: 手写值优先, book_value/market_cap 列回落.

        修复前 else 分支直接取行情现值, 手写 pb/mcap 被忽略且两列恒空,
        与带阈值条目的口径不一致。
        """
        entries = [
            {"symbol": "AAPL", "metric": "mcap"},  # 无阈值, 有行情市值
            {"symbol": "NVDA", "metric": "pb", "pb": 20.0},  # 无阈值, 手写 BVPS
        ]
        quotes = {
            "AAPL": Quote(
                symbol="AAPL", name="Apple", price=200.0, prev_close=190.0,
                change_pct=5.0, currency="USD", market_cap=3e12,
            ),
            "NVDA": Quote(
                symbol="NVDA", name="NV", price=100.0, prev_close=99.0,
                change_pct=1.0, currency="USD",
            ),
        }
        view, issues = build_watchlist_view(entries, quotes)
        row = view.set_index("symbol")
        assert row.loc["AAPL", "market_cap"] == 3e12
        assert row.loc["AAPL", "base_value"] == 3e12
        # 手写 BVPS=20 优先 (行情无 book_value), 列同样回落
        assert row.loc["NVDA", "book_value"] == 20.0
        assert row.loc["NVDA", "base_value"] == pytest.approx(5.0)
        assert issues == []

    def test_fundamentals_not_fetched_for_tracking_only(self, monkeypatch):
        """无阈值仅跟踪的 pb/mcap 条目不发基本面请求."""
        calls = []

        def boom(symbols):
            calls.append(list(symbols))
            return {s: None for s in symbols}

        monkeypatch.setattr("tracker.fundamentals.fetch_book_values", boom)
        monkeypatch.setattr("tracker.fundamentals.fetch_market_caps", boom)
        entries = [{"symbol": "AAPL", "metric": "pb"}]  # 无阈值
        quotes = {
            "AAPL": Quote(
                symbol="AAPL", name="Apple", price=200.0, prev_close=190.0,
                change_pct=5.0, currency="USD",
            )
        }
        view, _ = build_watchlist_view(entries, quotes)
        assert calls == []
        assert view.iloc[0]["base_value"] is None or pd.isna(
            view.iloc[0]["base_value"]
        )

    def test_fund_syms_uses_normalized_symbol(self, monkeypatch):
        """别名条目 (600519.SH) 也能按归一代码取到基本面 (键不漂移)."""
        fetched = {}

        def fake_bv(symbols):
            fetched.update({s: 12.5 for s in symbols})
            return dict(fetched)

        monkeypatch.setattr("tracker.fundamentals.fetch_book_values", fake_bv)
        entries = [{"symbol": "600519.SH", "metric": "pb", "upper_1": 5.0}]
        quotes = {
            "600519.SS": Quote(
                symbol="600519.SS", name="Kweichow", price=1500.0,
                prev_close=1480.0, change_pct=1.3, currency="CNY",
            )
        }
        view, issues = build_watchlist_view(entries, quotes)
        assert "600519.SS" in fetched
        assert view.iloc[0]["base_value"] == pytest.approx(1500.0 / 12.5)
        assert issues == []

    def test_parse_error_issue_not_duplicated(self, monkeypatch):
        """无法解析的代码在取数层/视图层只记一条 issue."""
        monkeypatch.setattr(
            "tracker.prices.get_quotes",
            lambda syms, **kw: ({}, {str(s): f"无法识别的交易所后缀: {s}" for s in syms}, []),
        )
        _, issues = fetch_watchlist_view([{"symbol": "BAD.ZZ"}])
        assert issues == ["BAD.ZZ: 无法识别的交易所后缀: BAD.ZZ"]


# ---------------------------------------------------------------------------
# storage: metric 钉死 / 去重 / 非有限值清洗
# ---------------------------------------------------------------------------


class TestStorageFixes:
    def test_explicit_price_metric_round_trips(self):
        """显式 price 落盘: 遗留 mcap 覆盖键不会在下次加载时把基准推断回去."""
        e = normalize_watch_entry(
            {"symbol": "AAPL", "metric": "price", "mcap": 3e12, "upper_1": 250.0}
        )
        assert e["metric"] == "price"
        assert normalize_watch_entry(e)["metric"] == "price"

    def test_clean_rows_drops_inf_and_nested_nan(self):
        """±Infinity 与嵌套 NaN 都要清掉 (allow_nan=False 否则炸写盘)."""
        rows = [
            {"symbol": "A", "quantity": float("inf")},
            {"symbol": "B", "source_quantities": {"": float("nan"), "w": 1.0}},
            {"symbol": "C", "quantity": 2.0},
        ]
        clean = _clean_rows(rows)
        assert "quantity" not in clean[0]
        assert clean[1]["source_quantities"] == {"": None, "w": 1.0}
        assert clean[2] == {"symbol": "C", "quantity": 2.0}
        # 序列化不再抛 ValueError
        json.dumps({"holdings": clean}, allow_nan=False)

    def test_dedupe_keeps_first_non_empty_field(self):
        merged = dedupe_watch_entries(
            [
                {"symbol": "AAPL", "lists": ["A"], "note": ""},
                {"symbol": "AAPL", "lists": ["B"], "note": "关注"},
            ]
        )
        assert len(merged) == 1
        assert merged[0]["note"] == "关注"
        assert set(merged[0]["lists"]) == {"A", "B"}


# ---------------------------------------------------------------------------
# 行情缓存 / 负缓存 / 批量超时
# ---------------------------------------------------------------------------


class TestCacheFixes:
    def test_fundamental_cache_coalesces_both_fields(self, tmp_path, monkeypatch):
        """mcap 写入不冲掉已缓存的 BVPS, BVPS 写入也不冲掉 mcap."""
        from tracker import cache as cache_mod

        monkeypatch.setenv(
            "TRACKER_CACHE_DB", str(tmp_path / "q.db")
        )
        importlib = __import__("importlib")
        importlib.reload(cache_mod)
        cache_mod.set_fundamental_cached("AAPL", 12.5)
        cache_mod.set_fundamental_cached("AAPL", None, market_cap=3e12)
        row = cache_mod.get_fundamental_cached("AAPL", ttl=99999)
        assert row["book_value"] == 12.5
        assert row["market_cap"] == 3e12
        cache_mod.set_fundamental_cached("AAPL", 4.0)
        row2 = cache_mod.get_fundamental_cached("AAPL", ttl=99999)
        assert row2["book_value"] == 4.0
        assert row2["market_cap"] == 3e12

    def test_neg_clear_all(self):
        orch.neg_mark("X")
        assert orch.neg_cached("X")
        orch.neg_clear_all()
        assert not orch.neg_cached("X")

    def test_disk_cache_hit_clears_negative(self, monkeypatch):
        """磁盘缓存命中同样是成功: 清出负缓存 (修复前只有源链路径清)."""
        orch.neg_mark("Z")
        monkeypatch.setattr(
            orch.cache_mod,
            "get_cached",
            lambda syms, ttl=300: {
                "Z": Quote(
                    symbol="Z", name="Z", price=1.0, prev_close=1.0,
                    change_pct=0.0, currency="USD",
                )
            },
        )
        quotes, _, _ = orch.get_quotes(["Z"])
        assert "Z" in quotes
        assert not orch.neg_cached("Z")

    def test_base_exception_does_not_destroy_batch(self, monkeypatch):
        """worker 抛 SystemExit 等 BaseException: 单代码记错, 不炸掉整批."""

        class _Provider:
            def fetch_quote(self, p, prefer_first=False):
                if p.yahoo == "BAD":
                    raise SystemExit("worker exit")
                return Quote(
                    symbol=p.yahoo, name=p.yahoo, price=1.0, prev_close=1.0,
                    change_pct=0.0, currency="USD",
                )

        monkeypatch.setattr(orch, "resolve", lambda t: _Provider())
        plist = [parse("BAD"), parse("OK1"), parse("OK2")]
        errors: dict[str, str] = {}
        out = orch._fetch_domain("global", plist, False, errors)
        assert sorted(out) == ["OK1", "OK2"]
        assert "BAD" in errors


# ---------------------------------------------------------------------------
# crypto: Gate 翻页窗口 / 坏响应不占缓存 / 源索引派生
# ---------------------------------------------------------------------------


class TestCryptoFixes:
    def test_gate_history_paginates_within_point_limit(self, monkeypatch):
        """单请求窗口截到 1000 点以内: 长区间翻页拼全 (修复前全区间 400)."""
        spans = []

        def fake_get(path, params):
            spans.append((params["to"] - params["from"]) / 86400 + 1)
            if spans[-1] > 1000:
                raise RuntimeError("Gate.io 400: range too broad")
            ts = params["from"]
            return [
                [ts + i * 86400, 100, 50, 51, 49, 50, 10, True]
                for i in range(int(spans[-1]))
            ]

        monkeypatch.setattr(crypto, "_gate_get", fake_get)
        df = crypto._gate_history(parse("BTC-USD"), "2023-01-01", "2025-12-15")
        assert len(spans) >= 2
        assert all(s <= 1000 for s in spans)
        assert len(df) == (pd.Timestamp("2025-12-15") - pd.Timestamp("2023-01-01")).days + 1

    def test_bad_spot_response_not_cached(self, monkeypatch):
        """lastPrice<=0 的坏响应不写缓存 (不会把错误连播 5 秒)."""
        crypto._spot_cache.clear()
        monkeypatch.setattr(crypto, "_get", lambda path, params: {"lastPrice": "0"})
        # 坏响应照常返回 (由 _binance_quote 抛错降级), 但不占缓存
        assert crypto._binance_spot_raw("BADUSDT") == {"lastPrice": "0"}
        assert "BADUSDT" not in crypto._spot_cache
        monkeypatch.setattr(crypto, "_get", lambda path, params: {"lastPrice": "5"})
        assert crypto._binance_spot_raw("GOODUSDT") == {"lastPrice": "5"}
        assert "GOODUSDT" in crypto._spot_cache

    def test_source_index_derived_from_prefers(self):
        from tracker.providers.crypto import (
            CRYPTO_SOURCE_PREFERS,
            _CRYPTO_SOURCE_INDEX,
        )

        expected = {
            s: i for i, s in enumerate(s for s in CRYPTO_SOURCE_PREFERS if s != "auto")
        }
        for k, v in expected.items():
            assert _CRYPTO_SOURCE_INDEX[k] == v
        assert _CRYPTO_SOURCE_INDEX["gate.io"] == _CRYPTO_SOURCE_INDEX["gate"]


# ---------------------------------------------------------------------------
# IBKR / 长桥 / analytics / wallet
# ---------------------------------------------------------------------------


class TestIbkrFixes:
    def test_lse_trailing_dot_stripped(self):
        """LSE 合约 symbol 带尾点 (BP.): 不剥会拼出 BP..L 且 parse 不报错."""
        assert ibkr_to_yahoo("BP.", "LSE", "", "GBP") == "BP.L"
        assert ibkr_to_yahoo("CSPX.", "LSE", "", "GBX") == "CSPX.L"
        # 空格类别股不受影响
        assert ibkr_to_yahoo("BRK B", "SMART", "NYSE", "USD") == "BRK-B"


class TestLongportFixes:
    def test_crypto_index_filtered_from_batch(self, monkeypatch):
        """crypto/index 不映射成长桥伪代码 (整批报错会拖丢所有股票行情)."""
        captured = []

        class _Ctx:
            def quote(self, syms):
                captured.extend(syms)
                return []

        monkeypatch.setattr(
            "tracker.longport._ctx_call",
            lambda fn, *a, **kw: fn(_Ctx(), *a) if a else fn(_Ctx()),
        )
        from tracker import longport as lp

        lp.get_quotes_longport(
            [parse("AAPL"), parse("BTC-USD"), parse("IX.DXY")]
        )
        assert captured == ["AAPL.US"]

    def test_positions_to_rows_keeps_short_and_zero_cost(self):
        from tracker import longport as lp

        class Pos:
            def __init__(self, symbol, quantity, cost_price):
                self.symbol = symbol
                self.quantity = quantity
                self.cost_price = cost_price
                self.currency = "USD"

        rows, skipped = lp.positions_to_rows(
            [
                Pos("AAPL.US", -10, 100.0),  # 空头保留负数量
                Pos("MSFT.US", 5, 0),  # 0 成本是合法值, 不当缺失
                Pos("TSLA.US", 0, 50.0),  # 零数量跳过
            ]
        )
        by_sym = {r["symbol"]: r for r in rows}
        assert by_sym["AAPL"]["quantity"] == -10.0
        assert by_sym["MSFT"]["avg_cost"] == 0.0
        assert "TSLA" not in by_sym


class TestAnalyticsFixes:
    def test_short_position_pnl_pct_sign(self):
        """空头盈亏比例按成本绝对值: 空头盈利显示为正 (修复前符号翻转)."""
        quotes = {
            "XYZ": Quote(
                symbol="XYZ", name="X", price=80.0, prev_close=90.0,
                change_pct=-11.1, currency="USD",
            )
        }
        view, _ = build_view(
            [{"symbol": "XYZ", "quantity": -10, "avg_cost": 100}],
            quotes,
            {"USD": 7.2},
        )
        row = view.iloc[0]
        assert row["pnl"] == pytest.approx(1440.0)
        assert row["pnl_pct"] == pytest.approx(0.2)
        assert summarize(view)["total_pnl_pct"] == pytest.approx(0.2)


class TestWalletFixes:
    ADDR = "TJNtFduS4oebw3jgGKCYmgSpTdyPieb6Ha"

    def test_tron_unfrozen_v2_and_frozen_energy_object(self, monkeypatch):
        """unfrozenV2 (非 unfrozen) + frozen_balance_for_energy 是 Frozen 对象."""
        from tracker import wallet as wallet_mod

        body = {
            "data": [
                {
                    "balance": 1_000_000,
                    "frozenV2": [{"amount": 124_000_000, "type": "ENERGY"}],
                    "unfrozenV2": [{"unfreeze_amount": 10_000_000}],
                    "account_resource": {
                        "frozen_balance_for_energy": {
                            "frozen_balance": 150_000_000,
                            "expire_time": 123,
                        }
                    },
                    "delegated_frozenV2_balance_for_bandwidth": 5_000_000,
                }
            ]
        }
        monkeypatch.setattr(
            wallet_mod, "_tron_get", lambda hosts, path, timeout: body
        )
        res = wallet_mod.import_wallet("tron", self.ADDR)
        trx = next(h for h in res["holdings"] if h["symbol"] == "TRX-USD")
        assert trx["quantity"] == pytest.approx(290.0)  # 1+124+10+150+5

    @pytest.mark.parametrize(
        "payload",
        [
            {"eth": {"0xabc": {"symbol": "USDT"}}},  # 缺 decimals
            {"eth": {"0xabc": "USDT"}},  # 条目非对象
            {"eth": ["x"]},  # 链值非对象
            ["x"],  # 顶层非对象
            {"eth": {"0xabc": {"symbol": "USDT", "decimals": None}}},
            {"eth": {"0xabc": {"symbol": "USDT", "decimals": -1}}},
        ],
    )
    def test_malformed_tokenlist_raises_value_error(self, payload, tmp_path):
        from tracker import wallet as wallet_mod

        p = tmp_path / "tl.json"
        p.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ValueError):
            wallet_mod._load_tokenlist(str(p))

    def test_tokenlist_decimals_string_coerced(self, tmp_path):
        from tracker import wallet as wallet_mod

        p = tmp_path / "tl.json"
        p.write_text(
            json.dumps({"ETH": {"0xAbC": {"symbol": "USDT", "decimals": "6"}}}),
            encoding="utf-8",
        )
        loaded = wallet_mod._load_tokenlist(str(p))
        assert loaded["eth"]["0xabc"] == {"symbol": "USDT", "decimals": 6}


# ---------------------------------------------------------------------------
# charting: 低价币精度 / 对比模式视口锚点时间轴
# ---------------------------------------------------------------------------


class TestChartingFixes:
    def test_low_price_precision(self):
        """SHIB/PEPE (~1e-5) 单只K线精度自适应, 不显示成 0.0000."""
        idx = pd.date_range("2024-01-01", periods=5, freq="D")
        df = pd.DataFrame(
            {
                "date": idx,
                "open": 1e-5,
                "high": 1.1e-5,
                "low": 0.9e-5,
                "close": 1e-5,
                "volume": 1.0,
            }
        )
        payload = charting.kline_payload(df, "SHIB-USD", currency="USD")
        assert payload["precision"] >= 6
        # 高价股仍是 2 位
        df2 = df.assign(open=100.0, high=101.0, low=99.0, close=100.0)
        assert charting.kline_payload(df2, "AAPL")["precision"] == 2

    def test_compare_payloads_carry_axis(self):
        """对比/加密货币对比 payload 带 axis 时间轴 (视口锚点索引空间)."""
        f1 = pd.DataFrame(
            {
                "date": pd.date_range("2024-01-01", periods=3),
                "open": 1.0,
                "high": 5.0,
                "low": 0.5,
                "close": [1.0, 2.0, 3.0],
                "volume": 0.0,
            }
        )
        f2 = pd.DataFrame(
            {
                "date": pd.date_range("2024-01-02", periods=3),
                "open": 2.0,
                "high": 6.0,
                "low": 1.5,
                "close": [2.0, 3.0, 4.0],
                "volume": 0.0,
            }
        )
        cp = charting.compare_payload({"AAA": f1, "BBB": f2})
        cc = charting.crypto_compare_payload({"AAA": f1, "BBB": f2})
        assert cp["axis"] == [
            "2024-01-01",
            "2024-01-02",
            "2024-01-03",
            "2024-01-04",
        ]
        assert cc["axis"] == cp["axis"]


# ---------------------------------------------------------------------------
# CLI: --json NaN 安全
# ---------------------------------------------------------------------------


class TestCliJsonFixes:
    def test_records_nan_to_none(self):
        from tracker.cli._common import _records

        df = pd.DataFrame(
            {
                "date": ["2024-01-01"],
                "open": [float("nan")],
                "close": [1.5],
                "volume": [float("nan")],
            }
        )
        text = json.dumps(_records(df))
        assert "NaN" not in text
        assert json.loads(text)[0]["open"] is None


# ---------------------------------------------------------------------------
# UI: 搜索下拉闭包绑定 / 持仓列格式
# ---------------------------------------------------------------------------

_UI_SCRIPT = """
import tracker.ui.kline_page as kp
kp.set_quick_symbols(["AAPL"])
kp.render_kline_page(False)
"""


def _fake_ohlc(symbol, months=12, prefer_akshare=False, **kw):
    idx = pd.date_range("2024-01-01", periods=30, freq="D")
    return pd.DataFrame(
        {
            "date": idx,
            "open": 1.0,
            "high": 1.2,
            "low": 0.9,
            "close": [1.0 + i * 0.01 for i in range(30)],
            "volume": 100.0,
        }
    )


def _fake_grouped(query, limit_per_domain=8):
    # cn 与 global 均非空: 从 cn (第一个非空域) 选中曾因闭包延迟绑定失效
    return {
        "cn": [{"code": "600519.SS", "name": "贵州茅台", "market": "A", "type": "cn"}],
        "global": [
            {"code": "NVDA", "name": "NVIDIA", "market": "US", "type": "global"}
        ],
        "crypto": [],
    }


@pytest.fixture
def kline_app(monkeypatch):
    import tracker.prices as prices
    import tracker.search as search_mod
    import tracker.ui.kline_page as kline_page

    monkeypatch.setattr(prices, "get_ohlc", _fake_ohlc)
    monkeypatch.setattr(search_mod, "search_grouped", _fake_grouped)
    monkeypatch.setattr(kline_page, "_KLINE_CHART", lambda **kw: None)
    kline_page.cached_kline.clear()
    return AppTest.from_string(_UI_SCRIPT, default_timeout=30).run()


class TestUiFixes:
    def test_pick_from_non_last_domain_works(self, kline_app):
        """从第一个非空域 (cn) 的下拉选中: 回填代码 (修复前 key 漂移到最后域)."""
        app = kline_app
        app.text_input("kline_search_query").set_value("茅台").run()
        cn = next(s for s in app.selectbox if any("600519" in o for o in s.options))
        cn.set_value(next(o for o in cn.options if "600519" in o)).run()
        ti = [t for t in app.text_input if t.key == "kline_symbol"][0]
        assert ti.value == "600519.SS"

    def test_quantity_format_is_valid_printf(self):
        """数量列格式必须是合法 printf (%,!.6f 会让前端抛 SyntaxError)."""
        import re

        src = open("tracker/ui/portfolio_page.py", encoding="utf-8").read()
        assert "%,!.6f" not in src
        # 所有 format 串只含合法 flag: % 后跟 符号/千分位/宽度/精度/类型,
        # 尾部 %% 是字面百分号 (合法); ! 不是合法 flag
        bad = [
            f
            for f in re.findall(r'format="([^"]+)"', src)
            if f.startswith("%")
            and not re.fullmatch(
                r"%[+0\-,'\s#]*\d*(\.\d+)?[bcdeEfFgGiosxX](%%)?", f
            )
        ]
        assert not bad, f"非法 printf 格式: {bad}"

    def test_pnl_pct_uses_percent_format(self):
        """pnl_pct 是小数比值: 用 percent 预定义格式 (自动乘 100)."""
        src = open("tracker/ui/portfolio_page.py", encoding="utf-8").read()
        assert '"盈亏%", format="percent"' in src
        assert '"盈亏%", format="%+.2f%%"' not in src
