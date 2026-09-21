"""统一导入管道测试: merge_holdings / apply_import / wallet_rows (纯离线)."""
import json

import pytest

from tracker import importer


def _pf(tmp_path, holdings, base="CNY", extra=None):
    p = tmp_path / "p.json"
    data = {"base_currency": base, "holdings": holdings}
    if extra:
        data.update(extra)
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def _load(p):
    return json.loads(p.read_text(encoding="utf-8"))


class TestMergeHoldings:
    def test_append_adds_new(self):
        merged, stats = importer.merge_holdings(
            [{"symbol": "AAPL", "quantity": 10, "avg_cost": 150}],
            [{"symbol": "600519.SS", "quantity": 5, "avg_cost": 1400}],
            importer.MODE_APPEND,
        )
        assert [h["symbol"] for h in merged] == ["AAPL", "600519.SS"]
        assert stats == {"added": ["600519.SS"], "updated": []}

    def test_append_updates_existing_quantity_keeps_cost(self):
        """追加模式下导入行缺 avg_cost 时保留原成本 (如钱包再导入不覆盖手填成本)."""
        merged, stats = importer.merge_holdings(
            [{"symbol": "AAPL", "quantity": 10, "avg_cost": 150}],
            [{"symbol": "AAPL", "quantity": 20}],
            importer.MODE_APPEND,
        )
        assert merged == [{"symbol": "AAPL", "quantity": 20.0, "avg_cost": 150}]
        assert stats["updated"] == ["AAPL"]
        assert stats["added"] == []

    def test_append_updates_cost_when_provided(self):
        merged, stats = importer.merge_holdings(
            [{"symbol": "AAPL", "quantity": 10, "avg_cost": 150}],
            [{"symbol": "AAPL", "quantity": 20, "avg_cost": 180}],
            importer.MODE_APPEND,
        )
        assert merged[0]["avg_cost"] == 180

    def test_append_normalizes_symbols(self):
        """同一证券的不同写法 (00700.HK / 0700.HK) 按 Yahoo 规范去重."""
        merged, stats = importer.merge_holdings(
            [{"symbol": "0700.HK", "quantity": 100}],
            [{"symbol": "00700.HK", "quantity": 200}],
            importer.MODE_APPEND,
        )
        assert len(merged) == 1
        assert merged[0]["quantity"] == 200.0
        assert stats["updated"] == ["0700.HK"]

    def test_overwrite_replaces_all(self):
        merged, stats = importer.merge_holdings(
            [{"symbol": "AAPL", "quantity": 10}, {"symbol": "NVDA", "quantity": 3}],
            [{"symbol": "600519.SS", "quantity": 5, "avg_cost": 1400}],
            importer.MODE_OVERWRITE,
        )
        assert [h["symbol"] for h in merged] == ["600519.SS"]
        assert stats["added"] == ["600519.SS"]

    def test_unknown_mode_raises(self):
        with pytest.raises(ValueError, match="未知导入模式"):
            importer.merge_holdings([], [], "bogus")

    def test_append_sums_same_symbol_from_different_wallet_sources(self):
        """不同链/地址的钱包导入同名代币 (eth 与 bsc 的 USDT-USD) 累加, 不互相覆盖."""
        existing = [{"symbol": "USDT-USD", "quantity": 100.0, "type": "crypto",
                     "import_source": "wallet:eth:0xaaa"}]
        rows = [{"symbol": "USDT-USD", "quantity": 50.0, "type": "crypto",
                 "import_source": "wallet:bsc:0xbbb"}]
        merged, stats = importer.merge_holdings(existing, rows, mode="append")
        assert len(merged) == 1
        assert merged[0]["quantity"] == 150.0
        assert stats == {"added": [], "updated": ["USDT-USD"]}

    def test_append_same_wallet_source_is_idempotent(self):
        """同一地址重复导入按代码覆盖, 不累加 (重复执行不翻倍)."""
        existing = [{"symbol": "USDT-USD", "quantity": 100.0, "type": "crypto",
                     "import_source": "wallet:eth:0xaaa"}]
        rows = [{"symbol": "USDT-USD", "quantity": 100.0, "type": "crypto",
                 "import_source": "wallet:eth:0xaaa"}]
        merged, _ = importer.merge_holdings(existing, rows, mode="append")
        assert merged[0]["quantity"] == 100.0

    def test_append_reimport_each_source_stays_idempotent(self):
        """跨源累加后再导入任一来源: 只替换该来源分量, 总量不变 (不二次翻倍)."""
        a = {"symbol": "USDT-USD", "quantity": 100.0, "type": "crypto",
             "import_source": "wallet:eth:0xaaa"}
        b = {"symbol": "USDT-USD", "quantity": 50.0, "type": "crypto",
             "import_source": "wallet:bsc:0xbbb"}
        merged, _ = importer.merge_holdings([dict(a)], [dict(b)], mode="append")
        assert merged[0]["quantity"] == 150.0
        # 重复导入 B: B 分量替换 50→50, 总量仍 150
        merged, _ = importer.merge_holdings(merged, [dict(b)], mode="append")
        assert merged[0]["quantity"] == 150.0
        # 重复导入 A: A 分量替换 100→100, 总量仍 150
        merged, _ = importer.merge_holdings(merged, [dict(a)], mode="append")
        assert merged[0]["quantity"] == 150.0

    def test_append_reimport_with_changed_balance_recomputes_total(self):
        """来源余额变化 (链上转出) → 该分量更新, 其它来源分量保留."""
        a = {"symbol": "USDT-USD", "quantity": 100.0, "type": "crypto",
             "import_source": "wallet:eth:0xaaa"}
        b = {"symbol": "USDT-USD", "quantity": 50.0, "type": "crypto",
             "import_source": "wallet:bsc:0xbbb"}
        merged, _ = importer.merge_holdings([dict(a)], [dict(b)], mode="append")
        b_moved = dict(b, quantity=70.0)  # B 地址余额 50 → 70
        merged, _ = importer.merge_holdings(merged, [b_moved], mode="append")
        assert merged[0]["quantity"] == 170.0
        # A 再导入不受 B 变化影响
        merged, _ = importer.merge_holdings(merged, [dict(a)], mode="append")
        assert merged[0]["quantity"] == 170.0

    def test_append_plain_import_has_no_source_ledger(self):
        """无 import_source 的普通导入 (IBKR/券商文件) 不写 source_quantities."""
        merged, _ = importer.merge_holdings(
            [], [{"symbol": "AAPL", "quantity": 10}], mode="append"
        )
        assert "source_quantities" not in merged[0]


class TestApplyImport:
    def test_append_writes_and_backs_up(self, tmp_path):
        p = _pf(tmp_path, [{"symbol": "AAPL", "quantity": 10}])
        res = importer.apply_import(
            [{"symbol": "NVDA", "quantity": 3}], p, mode=importer.MODE_APPEND
        )
        assert res["written"] is True
        assert res["backup"] == str(p) + ".bak"
        assert (tmp_path / "p.json.bak").exists()
        data = _load(p)
        assert [h["symbol"] for h in data["holdings"]] == ["AAPL", "NVDA"]
        assert data["base_currency"] == "CNY"
        assert res["added"] == ["NVDA"]

    def test_overwrite_preserves_other_keys(self, tmp_path):
        """覆盖只替换 holdings, 文件中的文档键与 base_currency 保留."""
        p = _pf(tmp_path, [{"symbol": "AAPL", "quantity": 10}],
                base="USD", extra={"_说明": "文档"})
        res = importer.apply_import(
            [{"symbol": "600519.SS", "quantity": 5}],
            p, mode=importer.MODE_OVERWRITE,
        )
        assert res["written"] is True
        data = _load(p)
        assert data["_说明"] == "文档"
        assert data["base_currency"] == "USD"
        assert [h["symbol"] for h in data["holdings"]] == ["600519.SS"]

    def test_dry_run_no_write(self, tmp_path):
        p = _pf(tmp_path, [{"symbol": "AAPL", "quantity": 10}])
        res = importer.apply_import(
            [{"symbol": "NVDA", "quantity": 3}], p, dry_run=True
        )
        assert res["written"] is False
        assert len(_load(p)["holdings"]) == 1
        assert not (tmp_path / "p.json.bak").exists()

    def test_empty_rows_no_write_even_overwrite(self, tmp_path):
        """空结果 + 覆盖模式不得清空文件 (防采集失败误删全部持仓)."""
        p = _pf(tmp_path, [{"symbol": "AAPL", "quantity": 10}])
        res = importer.apply_import([], p, mode=importer.MODE_OVERWRITE)
        assert res["written"] is False
        assert len(_load(p)["holdings"]) == 1

    def test_type_stamped_on_save(self, tmp_path):
        p = _pf(tmp_path, [])
        importer.apply_import([{"symbol": "ETH-USD", "quantity": 2}], p)
        data = _load(p)
        assert data["holdings"][0]["type"] == "crypto"


class TestWalletRows:
    def test_converts_holdings(self):
        raw = {
            "chain": "eth",
            "address": "0xabc",
            "holdings": [
                {"symbol": "ETH-USD", "quantity": 2.0, "contract": None,
                 "source": "native", "chain": "eth"},
                {"symbol": "USDT-USD", "quantity": 100.0,
                 "contract": "0xdac17f...", "source": "erc20", "chain": "eth"},
            ],
        }
        rows = importer.wallet_rows(raw)
        assert [(r["symbol"], r["quantity"], r["type"]) for r in rows] == [
            ("ETH-USD", 2.0, "crypto"),
            ("USDT-USD", 100.0, "crypto"),
        ]
        # 同一地址产出的行共享同一溯源键 (合并时据此判定幂等覆盖)
        assert len({r["import_source"] for r in rows}) == 1

    def test_empty(self):
        assert importer.wallet_rows({"holdings": []}) == []
        assert importer.wallet_rows({}) == []
