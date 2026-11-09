"""存储层测试: JSON 读写 / 编码兜底 / 备份恢复 / 并发锁 / 清洗与去重.

由历史批次文件 (review/audit/bugfix) 按被测模块重组而来, 便于改 storage 时
直接定位全部相关用例。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tracker import storage
from tracker.storage import _clean_rows, dedupe_watch_entries, normalize_watch_entry


def test_dedupe_preserves_explicit_zero_threshold():
    merged = dedupe_watch_entries(
        [
            {"symbol": "AAPL", "lists": ["A"], "upper_1": 0},
            {"symbol": "AAPL", "lists": ["B"], "upper_1": 150},
        ]
    )
    assert merged[0]["upper_1"] == 0


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

    def test_save_does_not_write_bak(self, tmp_path):
        """保存不再自动生成 .bak (历史备份文件仍可用于读取恢复)."""
        p = tmp_path / "p.json"
        storage.save_portfolio({"holdings": [{"symbol": "AAPL", "quantity": 1}]}, p)
        storage.save_portfolio({"holdings": [{"symbol": "MSFT", "quantity": 2}]}, p)
        assert json.loads(p.read_text(encoding="utf-8"))["holdings"][0]["symbol"] == "MSFT"
        assert not p.with_suffix(".json.bak").exists()


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

    def test_read_only_lock_file_does_not_block_save(self, tmp_path):
        """另一 UID 留下的 0644/0444 锁文件不能让保存直接 PermissionError."""
        p = tmp_path / "p.json"
        lock = tmp_path / "p.json.lock"
        lock.write_bytes(b"")
        lock.chmod(0o400)
        try:
            storage.save_portfolio({"holdings": []}, p)
        finally:
            lock.chmod(0o600)
        assert json.loads(p.read_text(encoding="utf-8"))["holdings"] == []

    def test_new_lock_file_follows_umask(self, tmp_path):
        """新建锁文件不能固定 0644: 跨 UID 场景要与数据文件同样可写."""
        import os

        p = tmp_path / "p.json"
        old = os.umask(0o077)
        try:
            storage.save_portfolio({"holdings": []}, p)
        finally:
            os.umask(old)
        assert (tmp_path / "p.json.lock").stat().st_mode & 0o777 == 0o600

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


def test_load_settings_gbk_and_non_finite_literals(tmp_path):
    """GBK settings.json 不能静默回退默认; NaN/Infinity 读侧归一不炸保存."""
    p = tmp_path / "settings.json"
    p.write_text(
        '{"color_scheme": "intl", "use_ibkr": true, "bad": NaN}',
        encoding="gbk",
    )
    data = storage.load_settings(p)
    assert data["color_scheme"] == "intl"
    assert data["use_ibkr"] is True
    # 非有限字面量不进入已知键; 保存时不因 allow_nan=False 抛错
    storage.save_settings({"use_longport": True}, p)
    assert storage.load_settings(p)["use_longport"] is True


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


def test_save_watchlist_drops_non_finite_numbers(tmp_path):
    """NaN/inf 阈值不能炸写盘 (json allow_nan=False), 应直接剔除."""
    from tracker import storage

    p = tmp_path / "w.json"
    storage.save_watchlist(
        {
            "watchlist": [
                {
                    "symbol": "AAPL",
                    "upper_1": float("nan"),
                    "lower_1": float("inf"),
                    "note": "关注",
                }
            ]
        },
        p,
    )
    e = json.loads(p.read_text(encoding="utf-8"))["watchlist"][0]
    assert "upper_1" not in e and "lower_1" not in e
    assert e["note"] == "关注"
    assert e["symbol"] == "AAPL"


def test_structural_corruption_recovers_from_bak(tmp_path):
    """合法 JSON 但 holdings 结构畸形时也要走 .bak 恢复.

    修复前校验发生在 _read_json_or_recover 返回之后, 完好的 .bak 被忽略,
    load/edit/save(不提供 holdings) 会永久失败。
    """
    p = tmp_path / "portfolio.json"
    p.write_text(json.dumps({"holdings": "AAPL"}), encoding="utf-8")
    Path(str(p) + ".bak").write_text(
        json.dumps({"holdings": [{"symbol": "MSFT", "quantity": 2}]}),
        encoding="utf-8",
    )
    assert storage.load_portfolio(p)["holdings"][0]["symbol"] == "MSFT"
    # 只改 base_currency 的保存也应先恢复旧持仓, 不能因结构畸形失败
    storage.save_portfolio({"base_currency": "USD"}, p)
    saved = json.loads(p.read_text(encoding="utf-8"))
    assert saved["base_currency"] == "USD"
    assert saved["holdings"][0]["symbol"] == "MSFT"


def test_parse_lists_string_sentinels_are_dropped():
    """逗号字符串里的 nan/None 占位不能变成真实列表名."""
    assert storage.parse_lists("nan") == ["默认"]
    assert storage.parse_lists("科技, nan, 消费, inf") == ["科技", "消费"]
    assert storage.parse_lists(["科技", None, float("nan"), "消费"]) == ["科技", "消费"]


def test_load_rejects_non_object_top_level(tmp_path):
    """合法 JSON 但顶层非对象 (如把数组写进 portfolio.json) 不能静默返回 {}.

    修复前 _read_json 对顶层非 dict 返回 {}, 下一次保存会把该文件
    覆盖成空组合 —— 与 _read_json_or_recover 注释承诺的数据保护矛盾。
    """
    import json as _json
    import os

    from tracker import storage

    p = tmp_path / "portfolio.json"
    p.write_text('[{"symbol": "AAPL", "quantity": 1}]', encoding="utf-8")
    # 无 .bak: 直接抛可读错误, 不静默回空
    with pytest.raises(storage.CorruptDataError):
        storage.load_portfolio(p)
    # 有 .bak: 能从备份恢复
    backup = Path(str(p) + ".bak")
    backup.write_text(
        _json.dumps({"base_currency": "CNY", "holdings": []}),
        encoding="utf-8",
    )
    data = storage.load_portfolio(p)
    assert data["base_currency"] == "CNY"
    # 恢复后备份文件仍在 (不删档)
    assert os.path.exists(backup)
    # watchlist 同路径 (独立文件, 无 .bak)
    p2 = tmp_path / "watchlist.json"
    p2.write_text('"just a string"', encoding="utf-8")
    with pytest.raises(storage.CorruptDataError):
        storage.load_watchlist(p2)
