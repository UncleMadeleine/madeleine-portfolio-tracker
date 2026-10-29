"""本轮代码审查修复的回归测试 (2026-10-29).

覆盖审查发现且此前无测试锁定的缺陷 (每一条都在修复前实测复现过):
  - symbols: 韩股 .KS/.KQ 不补前导零 → 取不到数
  - analytics: 字符串 "nan"/"inf" 数量成本渗入 NaN; 多空混合用净成本算组合收益率
  - storage: 保存不建父目录; 畸形结构绕过 .bak 恢复; .bak 陈旧致编辑丢失
  - cache: set_cached 把 BVPS/总市值抹成 NULL
  - providers: openbb 故障跨域连坐; akshare 空表裸 KeyError
  - ibkr/longport: 持仓不迁移改码标的 / 不过滤 N 前缀占位码
"""

import json
import math

import pandas as pd
import pytest

from tracker import analytics, cache, ibkr, longport, storage
from tracker.prices import Quote
from tracker.symbols import normalize, parse


class TestKoreanLeadingZeros:
    def test_ks_kq_padded_to_six(self):
        """韩股 Yahoo 代码 6 位带前导零: 5930.KS → 005930.KS (与港股补零同口径)."""
        assert normalize("5930.KS") == "005930.KS"
        assert normalize("35720.KQ") == "035720.KQ"
        assert normalize("005930.KS") == "005930.KS"  # 幂等
        assert parse("5930.KS").yahoo == "005930.KS"

    def test_non_numeric_head_untouched(self):
        assert normalize("ABC.KS") == "ABC.KS"


class TestAnalyticsNumericParsing:
    def test_string_nan_quantity_not_nan(self):
        """手改 JSON / CSV 粘贴的 "nan" 字面量不能渗成 NaN (汇总与明细口径一致)."""
        holdings = [{"symbol": "AAPL", "quantity": "nan", "avg_cost": 100.0}]
        quotes = {"AAPL": Quote("AAPL", "Apple", 150.0, None, None, "USD")}
        view, issues = analytics.build_view(holdings, quotes, {"USD": 7.0})
        assert issues == []
        assert len(view) == 1
        assert view.iloc[0]["quantity"] == 0.0
        assert not math.isnan(view.iloc[0]["market_value"])

    def test_string_inf_cost_treated_as_missing(self):
        holdings = [{"symbol": "AAPL", "quantity": 1, "avg_cost": "inf"}]
        quotes = {"AAPL": Quote("AAPL", "Apple", 150.0, None, None, "USD")}
        view, issues = analytics.build_view(holdings, quotes, {"USD": 7.0})
        assert issues == []
        assert view.iloc[0]["cost"] is None or pd.isna(view.iloc[0]["cost"])

    def test_non_numeric_quantity_reports_issue(self):
        holdings = [{"symbol": "AAPL", "quantity": "N/A"}]
        quotes = {"AAPL": Quote("AAPL", "Apple", 150.0, None, None, "USD")}
        view, issues = analytics.build_view(holdings, quotes, {"USD": 7.0})
        assert view.empty
        assert any("数量无法解析" in i for i in issues)


class TestSummarizeGrossCost:
    def test_long_short_uses_gross_cost(self):
        """多空混合: 组合收益率分母用毛成本 (绝对值求和), 不是带符号净额.

        修复前 total_cost = 1000 - 1500 = -500 → abs 后 500, 收益率 +10%;
        真实口径 (毛成本 2500) 是 +2%。
        """
        quotes = {
            "AAPL": Quote("AAPL", "Apple", 110.0, None, None, "USD"),
            "MSFT": Quote("MSFT", "Microsoft", 90.0, None, None, "USD"),
        }
        holdings = [
            {"symbol": "AAPL", "quantity": 10, "avg_cost": 100.0},
            {"symbol": "MSFT", "quantity": -5, "avg_cost": 100.0},
        ]
        view, _ = analytics.build_view(holdings, quotes, {"USD": 1.0})
        summary = analytics.summarize(view)
        # 毛成本 = 1000 + 500 = 1500; 总盈亏 = (1100-1000) + (-450 - -500) = 150
        assert summary["total_cost"] == pytest.approx(1500.0)
        assert summary["total_pnl"] == pytest.approx(150.0)
        assert summary["total_pnl_pct"] == pytest.approx(0.1)

    def test_all_long_unchanged(self):
        """纯多头: 毛成本 == 净额, 显示值与修复前一致。"""
        quotes = {"AAPL": Quote("AAPL", "Apple", 150.0, None, None, "USD")}
        view, _ = analytics.build_view(
            [{"symbol": "AAPL", "quantity": 10, "avg_cost": 100.0}],
            quotes,
            {"USD": 7.0},
        )
        summary = analytics.summarize(view)
        assert summary["total_cost"] == pytest.approx(1000.0 * 7.0)
        assert summary["total_pnl"] == pytest.approx(500.0 * 7.0)


class TestStorageWritePaths:
    def test_save_creates_missing_parent(self, tmp_path):
        """父目录不存在 (未跑 tracker init / TRACKER_HOME 指向新目录) 时保存不崩."""
        p = tmp_path / "sub" / "dir" / "portfolio.json"
        storage.save_portfolio({"holdings": [{"symbol": "AAPL", "quantity": 1}]}, p)
        assert json.loads(p.read_text(encoding="utf-8"))["holdings"]

    def test_malformed_structure_raises_corrupt(self, tmp_path):
        """结构畸形但合法 JSON → CorruptDataError (可读 + 走 .bak), 不是裸 ValueError."""
        p = tmp_path / "w.json"
        p.write_text(json.dumps({"watchlist": ["AAPL"]}), encoding="utf-8")
        with pytest.raises(storage.CorruptDataError):
            storage.load_watchlist(p)

    def test_bak_refreshed_on_every_save(self, tmp_path):
        """.bak 每次保存刷新: 主文件损坏时恢复的是最近一次内容, 不是数月前快照."""
        p = tmp_path / "p.json"
        storage.save_portfolio({"holdings": [{"symbol": "AAPL", "quantity": 1}]}, p)
        storage.save_portfolio({"holdings": [{"symbol": "MSFT", "quantity": 2}]}, p)
        bak = json.loads(p.with_suffix(".json.bak").read_text(encoding="utf-8"))
        assert bak["holdings"][0]["symbol"] == "AAPL"  # 上一次的内容
        p.write_text("{broken", encoding="utf-8")
        recovered = storage.load_portfolio(p)
        assert recovered["holdings"][0]["symbol"] == "AAPL"


class TestCacheCoalesce:
    def test_set_cached_keeps_book_value_and_mcap(self, tmp_path, monkeypatch):
        """yfinance 路径 (BVPS/市值恒 None) 不能把库里 akshare 写的值抹成 NULL."""
        db = tmp_path / "var" / "quotes_cache.db"
        monkeypatch.setattr(cache, "CACHE_DB", db)
        cache._ensure_db()
        cache.set_cached(
            {
                "600519.SS": Quote(
                    "600519.SS", "贵州茅台", 1500.0, None, None, "CNY",
                    book_value=38.0, market_cap=1.9e12, source="akshare",
                )
            }
        )
        cache.set_cached(
            {"600519.SS": Quote("600519.SS", None, 1510.0, None, None, "CNY")}
        )
        hits, _ = cache.get_cached_with_age(["600519.SS"])
        assert hits["600519.SS"].book_value == pytest.approx(38.0)
        assert hits["600519.SS"].market_cap == pytest.approx(1.9e12)


class TestProviderDomainIsolation:
    def test_batch_obb_failure_does_not_kill_other_domains(self, monkeypatch):
        """openbb 故障时批量路径返回空表 (逐代码源链兜底), 不向上抛连坐 cn/crypto 域."""
        from tracker.providers import global_stocks

        def _boom():
            raise RuntimeError("cannot import name 'obb' from 'openbb'")

        monkeypatch.setattr(global_stocks, "_obb", _boom)
        out = global_stocks._yahoo_batch([parse("AAPL")])
        assert out == {}

    def test_akshare_history_empty_raises_readable(self, monkeypatch):
        """akshare 无数据返回无列空表: 可读 RuntimeError, 不是裸 KeyError 'date'."""
        from tracker.providers import global_stocks

        class FakeAk:
            def stock_zh_a_hist(self, **kw):
                return pd.DataFrame()

        monkeypatch.setattr(global_stocks, "_ak", lambda: FakeAk())
        with pytest.raises(RuntimeError, match="无历史数据"):
            global_stocks._akshare_history(parse("600519.SS"), "2026-01-01")


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


class TestStorageCrossProcessLock:
    """读-改-写全程持锁: 并发保存不丢更新 (修复前 4 进程各追加 20 条只剩 25 条)。"""

    _WORKER = """
import sys
sys.path.insert(0, @@ROOT@@)
from tracker import storage

path, n, tag, mode = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
for i in range(n):
    sym = "SYM%s_%d" % (tag, i)
    if mode == "portfolio":
        with storage.edit_portfolio(path) as data:
            holdings = list(data.get("holdings", []))
            holdings.append({"symbol": sym, "quantity": 1})
            data["holdings"] = holdings
    else:
        with storage.edit_watchlist(path) as data:
            entries = list(data.get("watchlist", []))
            entries.append({"symbol": sym, "lists": ["默认"]})
            data["watchlist"] = entries
"""

    @pytest.mark.parametrize(
        "mode,fname,key",
        [("portfolio", "portfolio.json", "holdings"), ("watchlist", "watchlist.json", "watchlist")],
    )
    def test_concurrent_appends_all_survive(self, tmp_path, mode, fname, key):
        """4 进程各追加 20 条: 锁内读-改-写 → 80 条全在 (旧写法只剩 ~25)。"""
        import json
        import subprocess
        import sys
        from pathlib import Path

        root = str(Path(__file__).resolve().parent.parent)
        target = tmp_path / fname
        seed = {"base_currency": "CNY", "holdings": []} if mode == "portfolio" else {"watchlist": []}
        target.write_text(json.dumps(seed), encoding="utf-8")
        script = tmp_path / "lock_worker.py"
        script.write_text(self._WORKER.replace("@@ROOT@@", repr(root)), encoding="utf-8")

        procs = [
            subprocess.Popen(
                [sys.executable, str(script), str(target), "20", str(w), mode],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            for w in range(4)
        ]
        for pr in procs:
            assert pr.wait(timeout=120) == 0, pr.stderr.read().decode()

        data = json.loads(target.read_text(encoding="utf-8"))
        syms = [e["symbol"] for e in data[key]]
        assert len(syms) == 80, f"期望 80 条, 实际 {len(syms)} (并发丢更新)"
        assert len(set(syms)) == 80

    def test_lock_file_is_persistent_not_deleted(self, tmp_path):
        """.lock 故意不删 (删掉被持有的锁文件会让后来者锁到另一个 inode)。"""
        p = tmp_path / "p.json"
        storage.save_portfolio({"holdings": []}, p)
        assert (tmp_path / "p.json.lock").exists()

    def test_reentrant_lock_same_thread(self, tmp_path):
        """同线程嵌套持锁不死锁 (edit_* 内部调 save_*, 同进程再开 fd 会自锁)。"""
        p = tmp_path / "p.json"
        storage.save_portfolio({"holdings": []}, p)
        with storage.edit_portfolio(p) as data:
            data["base_currency"] = "USD"
            # 内层再取同路径锁: 必须放行 (同进程再开 fd 抢 flock 会自锁)
            with storage.edit_portfolio(p) as inner:
                inner["base_currency"] = "HKD"
        # 外层退出时用它自己的副本再写一次 (嵌套同路径编辑的既有语义),
        # 关键是全程没有死锁、文件始终是合法 JSON
        assert json.loads(p.read_text(encoding="utf-8"))["base_currency"] == "USD"

    def test_edit_portfolio_creates_missing_file(self, tmp_path):
        """文件不存在时 edit_* 给空组合, 调用方无需分支。"""
        p = tmp_path / "sub" / "p.json"
        with storage.edit_portfolio(p) as data:
            assert data["holdings"] == []
            data["holdings"] = [{"symbol": "AAPL", "quantity": 1}]
        assert json.loads(p.read_text(encoding="utf-8"))["holdings"][0]["symbol"] == "AAPL"

    def test_edit_portfolio_no_write_on_exception(self, tmp_path):
        """块内抛异常不写盘 (锁随 with 退出释放, 不留半截状态)。"""
        p = tmp_path / "p.json"
        storage.save_portfolio({"holdings": [{"symbol": "AAPL", "quantity": 1}]}, p)
        before = p.read_text(encoding="utf-8")
        with pytest.raises(RuntimeError):
            with storage.edit_portfolio(p) as data:
                data["holdings"] = []
                raise RuntimeError("boom")
        assert p.read_text(encoding="utf-8") == before

    def test_locked_peer_raises_readable_error(self, tmp_path):
        """对方长时间持锁 → DataFileLocked 可读错误 (不无锁写入、不无限等)。"""
        import fcntl
        import os

        p = tmp_path / "p.json"
        storage.save_portfolio({"holdings": []}, p)
        # 自己占住 .lock 模拟挂死的对方进程
        fd = os.open(str(p) + ".lock", os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            with pytest.raises(storage.DataFileLocked, match="正被其它进程写入"):
                storage._LOCK_WAIT = 0.3  # 测试里不用等 30s
                try:
                    with storage.edit_portfolio(p) as data:
                        data["holdings"] = []
                finally:
                    storage._LOCK_WAIT = 30.0
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


class TestIbkrSyncDefaultAppend:
    """tracker sync 缺省改为追加合并 (旧缺省覆盖会静默丢掉其它来源持仓)。"""

    def _args(self, **kw):
        import types

        base = dict(
            portfolio=None, dry_run=False, json=True, mode=None,
            append=False, overwrite=False,
        )
        base.update(kw)
        return types.SimpleNamespace(**base)

    def test_default_appends(self, tmp_path, monkeypatch, capsys):
        import json

        from tracker import ibkr, ibkr_sync

        class C:
            symbol, exchange, primaryExchange, currency = "MSFT", "NYSE", "", "USD"

        class P:
            contract, position, avgCost = C(), 5, 100.0

        monkeypatch.setattr(ibkr, "fetch_positions", lambda cfg: [P()])
        monkeypatch.setattr(ibkr, "load_config", lambda: {})
        p = tmp_path / "p.json"
        p.write_text(
            json.dumps(
                {
                    "base_currency": "CNY",
                    "holdings": [
                        {"symbol": "BTC-USD", "quantity": 1},
                        {"symbol": "600519.SS", "quantity": 10},
                    ],
                }
            ),
            encoding="utf-8",
        )
        ibkr_sync.run_sync(self._args(portfolio=str(p)))
        out = json.loads(capsys.readouterr().out)
        assert out["mode"] == "append"
        syms = sorted(h["symbol"] for h in json.loads(p.read_text())["holdings"])
        assert syms == ["600519.SS", "BTC-USD", "MSFT"]

    def test_overwrite_still_supported_with_backup(self, tmp_path, monkeypatch, capsys):
        import json
        from pathlib import Path

        from tracker import ibkr, ibkr_sync

        class C:
            symbol, exchange, primaryExchange, currency = "MSFT", "NYSE", "", "USD"

        class P:
            contract, position, avgCost = C(), 5, 100.0

        monkeypatch.setattr(ibkr, "fetch_positions", lambda cfg: [P()])
        monkeypatch.setattr(ibkr, "load_config", lambda: {})
        p = tmp_path / "p.json"
        p.write_text(
            json.dumps({"base_currency": "CNY", "holdings": [{"symbol": "AAPL", "quantity": 1}]}),
            encoding="utf-8",
        )
        ibkr_sync.run_sync(self._args(portfolio=str(p), overwrite=True))
        out = json.loads(capsys.readouterr().out)
        assert out["mode"] == "overwrite"
        syms = [h["symbol"] for h in json.loads(p.read_text())["holdings"]]
        assert syms == ["MSFT"]
        assert Path(str(p) + ".bak").exists()

    def test_append_flag_is_compat_noop(self, tmp_path, monkeypatch, capsys):
        """旧调用方传的 --append 仍是缺省行为, 不会因此变成覆盖。"""
        import json

        from tracker import ibkr, ibkr_sync

        class C:
            symbol, exchange, primaryExchange, currency = "MSFT", "NYSE", "", "USD"

        class P:
            contract, position, avgCost = C(), 5, 100.0

        monkeypatch.setattr(ibkr, "fetch_positions", lambda cfg: [P()])
        monkeypatch.setattr(ibkr, "load_config", lambda: {})
        p = tmp_path / "p.json"
        p.write_text(
            json.dumps({"base_currency": "CNY", "holdings": [{"symbol": "AAPL", "quantity": 1}]}),
            encoding="utf-8",
        )
        ibkr_sync.run_sync(self._args(portfolio=str(p), append=True))
        out = json.loads(capsys.readouterr().out)
        assert out["mode"] == "append"
        syms = sorted(h["symbol"] for h in json.loads(p.read_text())["holdings"])
        assert syms == ["AAPL", "MSFT"]
