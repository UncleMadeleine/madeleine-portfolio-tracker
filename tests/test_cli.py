"""CLI 测试: 离线部分 (watchlist 增删改查 / 缓存管理 / JSON 快照)."""
import json

import pandas as pd
import pytest
from tracker import cli
from tracker.cache import set_cached
from tracker.prices import Quote
from tracker.snapshot import snapshot_json


def _run(capsys, *argv):
    cli.main(list(argv))
    return capsys.readouterr().out


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class TestWatchlistCli:
    def test_add_new_entry(self, tmp_path, capsys):
        f = tmp_path / "w.json"
        f.write_text(json.dumps({"watchlist": []}), encoding="utf-8")
        out = _run(
            capsys, "watchlist", "add", "AAPL", "--file", str(f),
            "--list", "科技", "--upper1", "250", "--note", "苹果",
        )
        assert "新增 AAPL" in out
        data = _load(f)
        assert data["watchlist"] == [
            {"symbol": "AAPL", "lists": ["科技"], "upper_1": 250.0, "note": "苹果"}
        ]

    def test_add_merges_existing(self, tmp_path, capsys):
        f = tmp_path / "w.json"
        f.write_text(
            json.dumps(
                {"watchlist": [{"symbol": "TSLA", "lists": ["科技"], "upper_1": 420}]}
            ),
            encoding="utf-8",
        )
        out = _run(
            capsys, "watchlist", "add", "TSLA", "--file", str(f),
            "--list", "美股", "--upper1", "500",
        )
        assert "更新 TSLA" in out
        data = _load(f)
        e = data["watchlist"][0]
        assert e["lists"] == ["科技", "美股"]
        assert e["upper_1"] == 500.0

    def test_add_invalid_symbol_skipped(self, tmp_path, capsys):
        f = tmp_path / "w.json"
        f.write_text(json.dumps({"watchlist": []}), encoding="utf-8")
        out = _run(capsys, "watchlist", "add", "BAD.ZZ", "--file", str(f))
        assert "跳过" in out
        assert _load(f)["watchlist"] == []

    def test_remove_normalized_hk(self, tmp_path, capsys):
        f = tmp_path / "w.json"
        f.write_text(
            json.dumps({"watchlist": [{"symbol": "0700.HK", "lists": ["默认"]}]}),
            encoding="utf-8",
        )
        out = _run(capsys, "watchlist", "remove", "00700.HK", "--file", str(f))
        assert "0700.HK" in out
        assert _load(f)["watchlist"] == []

    def test_remove_missing(self, tmp_path, capsys):
        f = tmp_path / "w.json"
        f.write_text(json.dumps({"watchlist": [{"symbol": "AAPL"}]}), encoding="utf-8")
        out = _run(capsys, "watchlist", "remove", "NVDA", "--file", str(f))
        assert "未找到" in out
        assert len(_load(f)["watchlist"]) == 1

    def test_remove_from_one_list_keeps_entry(self, tmp_path, capsys):
        f = tmp_path / "w.json"
        f.write_text(
            json.dumps({"watchlist": [{"symbol": "AAPL", "lists": ["科技", "美股"]}]}),
            encoding="utf-8",
        )
        out = _run(
            capsys, "watchlist", "remove", "AAPL", "--file", str(f), "--list", "科技"
        )
        assert "已删除" in out
        assert _load(f)["watchlist"] == [{"symbol": "AAPL", "lists": ["美股"]}]

    def test_remove_from_last_list_deletes_entry(self, tmp_path, capsys):
        f = tmp_path / "w.json"
        f.write_text(
            json.dumps({"watchlist": [{"symbol": "AAPL", "lists": ["科技"]}]}),
            encoding="utf-8",
        )
        _run(capsys, "watchlist", "remove", "AAPL", "--file", str(f), "--list", "科技")
        assert _load(f)["watchlist"] == []

    def test_remove_not_in_scope_list_keeps_entry(self, tmp_path, capsys):
        f = tmp_path / "w.json"
        f.write_text(
            json.dumps({"watchlist": [{"symbol": "AAPL", "lists": ["默认"]}]}),
            encoding="utf-8",
        )
        out = _run(
            capsys, "watchlist", "remove", "AAPL", "--file", str(f), "--list", "科技"
        )
        assert "未找到" in out
        assert _load(f)["watchlist"] == [{"symbol": "AAPL", "lists": ["默认"]}]

    def test_list_no_quotes(self, tmp_path, capsys):
        f = tmp_path / "w.json"
        f.write_text(
            json.dumps(
                {"watchlist": [{"symbol": "AAPL", "lists": ["科技"], "upper_1": 250}]}
            ),
            encoding="utf-8",
        )
        out = _run(capsys, "watchlist", "list", "--file", str(f), "--no-quotes")
        assert "AAPL" in out
        assert "科技" in out
        assert "upper_1=250" in out



class TestPortfolioCli:
    def test_add_new_holding(self, tmp_path, capsys):
        f = tmp_path / "p.json"
        f.write_text(json.dumps({"base_currency": "CNY", "holdings": []}), encoding="utf-8")
        out = _run(
            capsys, "portfolio", "add", "AAPL", "--portfolio", str(f),
            "--quantity", "10", "--avg-cost", "180",
        )
        assert "新增 AAPL" in out
        data = _load(f)
        assert data["holdings"] == [
            {"symbol": "AAPL", "quantity": 10.0, "avg_cost": 180.0}
        ]

    def test_add_updates_existing(self, tmp_path, capsys):
        f = tmp_path / "p.json"
        f.write_text(
            json.dumps(
                {"base_currency": "CNY", "holdings": [{"symbol": "AAPL", "quantity": 5, "avg_cost": 150}]}
            ),
            encoding="utf-8",
        )
        out = _run(
            capsys, "portfolio", "add", "AAPL", "--portfolio", str(f),
            "--quantity", "10", "--avg-cost", "180",
        )
        assert "更新 AAPL" in out
        h = _load(f)["holdings"][0]
        assert h["quantity"] == 10.0
        assert h["avg_cost"] == 180.0

    def test_add_without_quantity_errors(self, tmp_path, capsys):
        f = tmp_path / "p.json"
        f.write_text(json.dumps({"base_currency": "CNY", "holdings": []}), encoding="utf-8")
        with pytest.raises(SystemExit):
            _run(capsys, "portfolio", "add", "AAPL", "--portfolio", str(f))

    def test_add_normalizes_hk(self, tmp_path, capsys):
        f = tmp_path / "p.json"
        f.write_text(json.dumps({"base_currency": "CNY", "holdings": []}), encoding="utf-8")
        _run(capsys, "portfolio", "add", "00700.HK", "--portfolio", str(f),
              "--quantity", "100", "--avg-cost", "330")
        assert _load(f)["holdings"][0]["symbol"] == "0700.HK"

    def test_remove(self, tmp_path, capsys):
        f = tmp_path / "p.json"
        f.write_text(
            json.dumps(
                {"base_currency": "CNY", "holdings": [
                    {"symbol": "AAPL", "quantity": 10, "avg_cost": 180},
                    {"symbol": "NVDA", "quantity": 5, "avg_cost": 90},
                ]}
            ),
            encoding="utf-8",
        )
        out = _run(capsys, "portfolio", "remove", "AAPL", "--portfolio", str(f))
        assert "AAPL" in out
        data = _load(f)
        assert len(data["holdings"]) == 1
        assert data["holdings"][0]["symbol"] == "NVDA"

    def test_remove_missing(self, tmp_path, capsys):
        f = tmp_path / "p.json"
        f.write_text(
            json.dumps(
                {"base_currency": "CNY", "holdings": [{"symbol": "AAPL", "quantity": 10}]}
            ),
            encoding="utf-8",
        )
        out = _run(capsys, "portfolio", "remove", "NVDA", "--portfolio", str(f))
        assert "未找到" in out
        assert len(_load(f)["holdings"]) == 1

    def test_list_json(self, tmp_path, capsys):
        f = tmp_path / "p.json"
        f.write_text(
            json.dumps(
                {"base_currency": "USD", "holdings": [{"symbol": "AAPL", "quantity": 10, "avg_cost": 180}]}
            ),
            encoding="utf-8",
        )
        out = _run(capsys, "portfolio", "list", "--portfolio", str(f), "--json")
        data = json.loads(out)
        assert data["base_currency"] == "USD"
        assert data["holdings"][0]["symbol"] == "AAPL"

    def test_set_base(self, tmp_path, capsys):
        f = tmp_path / "p.json"
        f.write_text(
            json.dumps(
                {"base_currency": "CNY", "holdings": [{"symbol": "AAPL", "quantity": 10}]}
            ),
            encoding="utf-8",
        )
        out = _run(capsys, "portfolio", "set-base", "USD", "--portfolio", str(f))
        assert "CNY" in out and "USD" in out
        assert _load(f)["base_currency"] == "USD"

class TestCacheCli:
    def test_cache_info_and_clear(self, tmp_path, capsys, monkeypatch):
        db = tmp_path / "test_quotes_cache.db"
        monkeypatch.setattr("tracker.cache.CACHE_DB", db)
        set_cached(
            {
                "AAPL": Quote(
                    symbol="AAPL", name="Apple", price=150.0, prev_close=148.0,
                    change_pct=1.35, currency="USD",
                )
            }
        )
        out = _run(capsys, "cache", "info")
        assert "总条数: 1" in out
        assert "AAPL" not in out

        out = _run(capsys, "cache", "clear")
        assert "已清空" in out
        out = _run(capsys, "cache", "info", "--json")
        info = json.loads(out)
        assert info["total"] == 0


class TestReportCli:
    def _watchlist(self, tmp_path):
        f = tmp_path / "w.json"
        f.write_text(
            json.dumps(
                {
                    "watchlist": [
                        {"symbol": "TSLA", "lists": ["科技", "美股"], "upper_1": 420, "upper_2": 450, "note": "两级提醒"},
                        {"symbol": "NVDA", "lists": ["科技"], "upper_1": 260},
                    ]
                }
            ),
            encoding="utf-8",
        )
        return f

    def _fake_quotes(self, symbols, prefer_akshare=False, use_ibkr=False):
        data = {
            "TSLA": Quote("TSLA", "Tesla", 500.0, 480.0, 4.17, "USD"),
            "NVDA": Quote("NVDA", "NVIDIA", 120.0, 118.0, 1.69, "USD"),
        }
        return {s: data[s] for s in symbols if s in data}, {}, []

    def test_report_md(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr(cli.prices, "get_quotes", self._fake_quotes)
        out = _run(capsys, "report", "--file", str(self._watchlist(tmp_path)), "-f", "md")
        assert "# 自选监控阈值报告" in out
        assert "触发 1" in out
        assert "### 科技" in out
        assert "### 美股" in out
        assert "🔴 突破上限 II" in out
        assert "距上限I%" in out

    def test_report_json(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr(cli.prices, "get_quotes", self._fake_quotes)
        out = _run(capsys, "report", "--file", str(self._watchlist(tmp_path)), "-f", "json")
        data = json.loads(out)
        assert data["summary"]["total"] == 2
        assert data["summary"]["triggered"] == 1
        assert data["summary"]["by_list"]["科技"]["total"] == 2
        assert len(data["triggered"]) == 1
        assert data["triggered"][0]["symbol"] == "TSLA"

    def test_report_csv(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr(cli.prices, "get_quotes", self._fake_quotes)
        out = _run(capsys, "report", "--file", str(self._watchlist(tmp_path)), "-f", "csv")
        assert out.startswith("\ufefflists,")
        assert "TSLA" in out
        assert "科技、美股" in out

    def test_report_output_file(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr(cli.prices, "get_quotes", self._fake_quotes)
        out_f = tmp_path / "report.md"
        out = _run(
            capsys, "report", "--file", str(self._watchlist(tmp_path)),
            "-f", "md", "-o", str(out_f),
        )
        assert "报告已写入" in out
        assert "# 自选监控阈值报告" in out_f.read_text(encoding="utf-8")

    def test_report_scope_filter(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr(cli.prices, "get_quotes", self._fake_quotes)
        out = _run(
            capsys, "report", "--file", str(self._watchlist(tmp_path)),
            "-f", "json", "-w", "美股",
        )
        data = json.loads(out)
        assert data["scope"] == "美股"
        assert data["summary"]["total"] == 1
        assert data["summary"]["by_list"] == {"美股": {"total": 1, "triggered": 1}}
        assert list(data["by_list"].keys()) == ["美股"]


class TestSnapshotJson:
    def test_snapshot_json_serializable(self):
        view = pd.DataFrame(
            [
                {
                    "symbol": "AAPL", "name": "Apple", "market": "美股", "currency": "USD",
                    "price": 150.0, "change_pct": 1.35, "quantity": 10.0,
                    "avg_cost": 120.0, "market_value": 1500.0, "cost": 1200.0,
                    "pnl": 300.0, "pnl_pct": 0.25, "today_pnl": None,
                }
            ]
        )
        wview = pd.DataFrame(
            [
                {
                    "symbol": "TSLA", "price": 500.0, "upper_1": 420.0,
                    "status": "🟠 突破上限 I", "triggered": True,
                }
            ]
        )
        summary = {
            "total_value": 1500.0, "total_cost": 1200.0, "total_pnl": 300.0,
            "total_pnl_pct": 0.25, "cost_coverage": 1.0, "today_pnl": None,
            "by_market": pd.Series({"美股": 1500.0}),
            "by_currency": pd.Series({"USD": 1500.0}),
        }
        payload = snapshot_json("CNY", view, summary, wview, ["汇率缺失: HKD"])
        s = json.dumps(payload, ensure_ascii=False)
        assert "NaN" not in s
        data = json.loads(s)
        assert data["holdings"][0]["symbol"] == "AAPL"
        assert data["triggered"][0]["status"] == "🟠 突破上限 I"
        assert data["summary"]["total_value"] == 1500.0
        assert data["issues"] == ["汇率缺失: HKD"]
        assert data["summary"]["cost_coverage"] == 1.0


class TestExportCli:
    """export 子命令: 导出快照为 CSV / JSON / Markdown 报表."""

    def _portfolio(self, tmp_path):
        f = tmp_path / "p.json"
        f.write_text(
            json.dumps(
                {
                    "base_currency": "CNY",
                    "holdings": [
                        {"symbol": "AAPL", "quantity": 10, "avg_cost": 180},
                        {"symbol": "600519.SS", "quantity": 100, "avg_cost": 1500},
                    ],
                }
            ),
            encoding="utf-8",
        )
        return f

    def _watchlist(self, tmp_path):
        f = tmp_path / "w.json"
        f.write_text(json.dumps({"watchlist": []}), encoding="utf-8")
        return f

    def _fake_quotes(self, symbols, prefer_akshare=False, use_ibkr=False):
        data = {
            "AAPL": Quote("AAPL", "Apple", 200.0, 195.0, 2.56, "USD"),
            "600519.SS": Quote("600519.SS", "贵州茅台", 1600.0, 1580.0, 1.27, "CNY"),
        }
        return {s: data[s] for s in symbols if s in data}, {}, []

    def _fake_fx(self, base, currencies, use_ibkr=False):
        # 1 USD = 7.2 CNY, 1 CNY = 1 CNY
        rates = {"CNY": 1.0, "USD": 7.2}
        return {c: rates[c] for c in currencies if c in rates}, []

    def _patch(self, monkeypatch):
        monkeypatch.setattr(cli.prices, "get_quotes", self._fake_quotes)
        monkeypatch.setattr("tracker.snapshot.get_fx_rates", self._fake_fx)

    def test_export_json(self, tmp_path, capsys, monkeypatch):
        self._patch(monkeypatch)
        out = _run(
            capsys, "export", "-f", "json",
            "--portfolio", str(self._portfolio(tmp_path)),
            "--watchlist-file", str(self._watchlist(tmp_path)),
        )
        data = json.loads(out)
        assert data["base_currency"] == "CNY"
        assert len(data["holdings"]) == 2
        assert data["holdings"][0]["symbol"] in ("AAPL", "600519.SS")
        assert data["summary"]["total_value"] is not None

    def test_export_csv(self, tmp_path, capsys, monkeypatch):
        self._patch(monkeypatch)
        out = _run(
            capsys, "export", "-f", "csv",
            "--portfolio", str(self._portfolio(tmp_path)),
            "--watchlist-file", str(self._watchlist(tmp_path)),
        )
        assert out.startswith("\ufeffsymbol,")
        assert "AAPL" in out
        assert "600519.SS" in out

    def test_export_md(self, tmp_path, capsys, monkeypatch):
        self._patch(monkeypatch)
        out = _run(
            capsys, "export", "-f", "md",
            "--portfolio", str(self._portfolio(tmp_path)),
            "--watchlist-file", str(self._watchlist(tmp_path)),
        )
        assert "# 投资组合快照" in out
        assert "## 持仓明细" in out
        assert "AAPL" in out
        assert "## 市场分布" in out

    def test_export_output_file(self, tmp_path, capsys, monkeypatch):
        self._patch(monkeypatch)
        out_f = tmp_path / "snapshot.json"
        out = _run(
            capsys, "export", "-f", "json", "-o", str(out_f),
            "--portfolio", str(self._portfolio(tmp_path)),
            "--watchlist-file", str(self._watchlist(tmp_path)),
        )
        assert "快照已导出" in out
        data = json.loads(out_f.read_text(encoding="utf-8"))
        assert len(data["holdings"]) == 2

    def test_export_base_override(self, tmp_path, capsys, monkeypatch):
        self._patch(monkeypatch)
        out = _run(
            capsys, "export", "-f", "json", "--base", "USD",
            "--portfolio", str(self._portfolio(tmp_path)),
            "--watchlist-file", str(self._watchlist(tmp_path)),
        )
        data = json.loads(out)
        assert data["base_currency"] == "USD"


class TestSnapshotCryptoMixed:
    """加密货币与股票混合组合的快照 (离线: mock 行情与汇率)."""

    def _portfolio(self, tmp_path):
        f = tmp_path / "p.json"
        f.write_text(
            json.dumps(
                {
                    "base_currency": "CNY",
                    "holdings": [
                        {"symbol": "AAPL", "quantity": 10, "avg_cost": 180},
                        {"symbol": "BTC-USD", "quantity": 0.5, "avg_cost": 30000},
                    ],
                }
            ),
            encoding="utf-8",
        )
        return f

    def _watchlist(self, tmp_path):
        f = tmp_path / "w.json"
        f.write_text(
            json.dumps(
                {
                    "watchlist": [
                        {
                            "symbol": "ETH-USD", "lists": ["加密货币"],
                            "upper_1": 3000, "upper_2": 3500,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        return f

    def _fake_quotes(self, symbols, prefer_akshare=False, use_ibkr=False):
        data = {
            "AAPL": Quote("AAPL", "Apple", 200.0, 195.0, 2.56, "USD"),
            "BTC-USD": Quote("BTC-USD", None, 60000.0, 58000.0, 3.45, "USD"),
            "ETH-USD": Quote("ETH-USD", None, 3200.0, 3100.0, 3.23, "USD"),
        }
        return {s: data[s] for s in symbols if s in data}, {}, []

    def _fake_fx(self, base, currencies, use_ibkr=False):
        rates = {"CNY": 1.0, "USD": 7.2}
        return {c: rates[c] for c in currencies if c in rates}, []

    def _patch(self, monkeypatch):
        monkeypatch.setattr(cli.prices, "get_quotes", self._fake_quotes)
        monkeypatch.setattr("tracker.snapshot.get_fx_rates", self._fake_fx)

    def test_snapshot_json_mixed_crypto_stock(self, tmp_path, capsys, monkeypatch):
        self._patch(monkeypatch)
        out = _run(
            capsys, "snapshot", "--json",
            "--portfolio", str(self._portfolio(tmp_path)),
            "--watchlist-file", str(self._watchlist(tmp_path)),
        )
        data = json.loads(out)
        # 序列化干净: 无 NaN 字面量
        assert "NaN" not in out
        by_sym = {h["symbol"]: h for h in data["holdings"]}
        assert set(by_sym) == {"AAPL", "BTC-USD"}
        assert by_sym["BTC-USD"]["market"] == "加密货币"
        # BTC: 0.5 * 60000 * 7.2 = 216000; AAPL: 10 * 200 * 7.2 = 14400
        assert by_sym["BTC-USD"]["market_value"] == 216000.0
        assert by_sym["AAPL"]["market_value"] == 14400.0
        assert data["summary"]["total_value"] == 230400.0
        assert data["summary"]["by_market"]["加密货币"] == 216000.0
        assert data["summary"]["by_currency"] == {"USD": 230400.0}
        assert data["issues"] == []

    def test_snapshot_mixed_crypto_watchlist_trigger(self, tmp_path, capsys, monkeypatch):
        """ETH 现价 3200 介于 upper_1 (3000) 与 upper_2 (3500) 之间 → 触发上限 I."""
        self._patch(monkeypatch)
        out = _run(
            capsys, "snapshot", "--json",
            "--portfolio", str(self._portfolio(tmp_path)),
            "--watchlist-file", str(self._watchlist(tmp_path)),
        )
        data = json.loads(out)
        assert data["triggered"][0]["symbol"] == "ETH-USD"
        assert "上限 I" in data["triggered"][0]["status"]

    def test_snapshot_text_mixed_outputs_crypto_rows(self, tmp_path, capsys, monkeypatch):
        """文本快照: 持仓表含 BTC-USD, 市场分布含「加密货币」."""
        self._patch(monkeypatch)
        out = _run(
            capsys, "snapshot",
            "--portfolio", str(self._portfolio(tmp_path)),
            "--watchlist-file", str(self._watchlist(tmp_path)),
        )
        assert "BTC-USD" in out
        assert "加密货币" in out
        assert "浮动盈亏" in out

    def test_export_md_mixed(self, tmp_path, capsys, monkeypatch):
        """混合组合导出 Markdown: 市场分布与加密货币行都在."""
        self._patch(monkeypatch)
        out = _run(
            capsys, "export", "-f", "md",
            "--portfolio", str(self._portfolio(tmp_path)),
            "--watchlist-file", str(self._watchlist(tmp_path)),
        )
        assert "BTC-USD" in out
        assert "加密货币" in out

    def test_portfolio_add_crypto_normalizes_input(self, tmp_path, capsys, monkeypatch):
        """portfolio add BTCUSD → 存储为 Yahoo 规范格式 BTC-USD, 且组合里股票共存."""
        f = tmp_path / "p2.json"
        f.write_text(json.dumps({"base_currency": "CNY", "holdings": []}), encoding="utf-8")
        _run(capsys, "portfolio", "add", "BTCUSD", "--portfolio", str(f),
             "--quantity", "0.5", "--avg-cost", "30000")
        _run(capsys, "portfolio", "add", "600519.SS", "--portfolio", str(f),
             "--quantity", "100", "--avg-cost", "1500")
        holdings = _load(f)["holdings"]
        assert {h["symbol"] for h in holdings} == {"BTC-USD", "600519.SS"}
        assert holdings[0]["quantity"] == 0.5

    def test_watchlist_add_crypto_thresholds(self, tmp_path, capsys, monkeypatch):
        """watchlist add 无分隔符 ETHUSD → 归一为 ETH-USD 并保存两级阈值."""
        f = tmp_path / "w2.json"
        f.write_text(json.dumps({"watchlist": []}), encoding="utf-8")
        _run(capsys, "watchlist", "add", "ETHUSD", "--file", str(f),
             "--list", "加密货币", "--upper1", "3000", "--upper2", "3500")
        e = _load(f)["watchlist"][0]
        assert e["symbol"] == "ETH-USD"
        assert e["upper_1"] == 3000.0
        assert e["upper_2"] == 3500.0


class TestVersion:
    """--version / -v 全局选项."""

    def test_version_long(self, capsys):
        with pytest.raises(SystemExit) as exc:
            cli.main(["--version"])
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "tracker" in out
        assert cli.VERSION in out

    def test_version_short(self, capsys):
        with pytest.raises(SystemExit) as exc:
            cli.main(["-v"])
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert cli.VERSION in out


class TestParser:
    def test_subcommands_parse(self):
        for argv in (
            ["snapshot", "--base", "USD"],
            ["quote", "AAPL"],
            ["watchlist", "list"],
            ["watchlist", "add", "AAPL", "--upper1", "200"],
            ["portfolio", "list"],
            ["portfolio", "add", "AAPL", "--quantity", "10", "--avg-cost", "180"],
            ["portfolio", "remove", "AAPL"],
            ["portfolio", "set-base", "USD"],
            ["export", "-f", "json"],
            ["export", "-f", "csv", "-o", "h.csv"],
            ["export", "-f", "md", "--base", "USD"],
            ["fx", "USD", "CNY"],
            ["history", "AAPL"],
            ["kline", "AAPL"],
            ["kline", "AAPL", "--period", "weekly", "--ma", "10,30", "--refresh"],
            ["report", "-f", "md"],
            ["report", "-f", "json", "-w", "科技", "-o", "x.md"],
            ["sync", "--dry-run"],
            ["cache", "info"],
        ):
            args = cli.build_parser().parse_args(argv)
            assert callable(args.func)
