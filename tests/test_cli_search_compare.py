"""search/compare CLI + resolve_symbol 测试 (不联网, provider/搜索层全桩)."""
from __future__ import annotations

import json

import pandas as pd
import pytest
from tracker import cli, search as search_mod
from tracker.cli.compare import resolve_inputs


def _run(capsys, *argv):
    cli.main(list(argv))
    return capsys.readouterr().out


def _fake_grouped(monkeypatch, mapping):
    """mapping: query -> grouped dict (cn/global/crypto 三组)."""
    def fake(query, limit_per_domain=5):
        q = (query or "").strip()
        g = mapping.get(q)
        if g is None:
            return {t: [] for t in ("cn", "global", "crypto")}
        return g

    monkeypatch.setattr(search_mod, "search_grouped", fake)


# ---------- resolve_symbol (宽松输入 → 规范代码) ----------


def test_resolve_valid_code_short_circuits(monkeypatch):
    """合法代码直接 parse 归一, 不发网络请求 (600519.SH → 600519.SS)."""
    def boom(query, limit_per_domain=5):
        raise AssertionError("合法代码不应走在线搜索")

    monkeypatch.setattr(search_mod, "search_grouped", boom)
    hits = search_mod.resolve_symbol("600519.SH")
    assert hits[0]["code"] == "600519.SS"
    assert hits[0]["type"] == "cn"


def test_resolve_bare_digits_prefers_padded_hk(monkeypatch):
    """裸数字: 补零/原码精确命中排最前 (700/00700 → 0700.HK), 裸数字假代码被丢弃."""
    _fake_grouped(monkeypatch, {
        "700": {
            "cn": [
                {"code": "600700.SS", "name": "*ST数码", "market": "A股", "type": "cn"},
                {"code": "000700.SZ", "name": "模塑科技", "market": "A股", "type": "cn"},
            ],
            "global": [{"code": "0700.HK", "name": "腾讯控股", "market": "港股", "type": "global"}],
            "crypto": [],
        },
        "00700": {
            "cn": [{"code": "000700.SZ", "name": "模塑科技", "market": "A股", "type": "cn"}],
            "global": [{"code": "0700.HK", "name": "腾讯控股", "market": "港股", "type": "global"}],
            "crypto": [],
        },
    })
    for q in ("700", "00700"):
        hits = search_mod.resolve_symbol(q)
        assert hits[0]["code"] == "0700.HK"
        assert all(not (h["code"].isdigit() and h["type"] == "global") for h in hits)


def test_resolve_non_ascii_no_hits_returns_empty(monkeypatch):
    """非 ASCII 搜索无果且 parse 无法给出可取数形态: 返回 [] (不产假代码)."""
    _fake_grouped(monkeypatch, {})
    assert search_mod.resolve_symbol("某某股不存在") == []


# ---------- search 子命令 ----------


def test_search_cli_json(monkeypatch, capsys):
    """search --json: results[].code 为规范代码, 可直接喂 kline."""
    _fake_grouped(monkeypatch, {
        "腾讯": {
            "cn": [],
            "global": [{"code": "0700.HK", "name": "腾讯控股", "market": "港股", "type": "global"}],
            "crypto": [],
        },
    })
    out = _run(capsys, "search", "腾讯", "--json")
    data = json.loads(out)
    assert data["results"][0]["code"] == "0700.HK"
    assert data["results"][0]["type"] == "global"


def test_search_cli_empty(monkeypatch, capsys):
    _fake_grouped(monkeypatch, {})
    out = _run(capsys, "search", "不存在的股")
    assert "无匹配" in out


# ---------- compare 子命令 ----------


def _fake_ohlc_factory(values_by_code):
    def fake(code, months=12, prefer_akshare=False, **kw):
        n = 30
        idx = pd.date_range("2024-01-01", periods=n, freq="D")
        base = values_by_code[code]
        return pd.DataFrame({
            "date": idx,
            "open": base,
            "high": base * 1.1,
            "low": base * 0.9,
            "close": [base + i * 0.1 for i in range(n)],
            "volume": 100.0,
        })

    return fake


@pytest.fixture
def compare_env(monkeypatch):
    monkeypatch.setattr(
        cli.prices, "get_ohlc",
        _fake_ohlc_factory({"AAPL": 100.0, "0700.HK": 300.0, "600519.SS": 1500.0}),
    )
    _fake_grouped(monkeypatch, {
        "腾讯": {
            "cn": [],
            "global": [{"code": "0700.HK", "name": "腾讯控股", "market": "港股", "type": "global"}],
            "crypto": [],
        },
    })



def test_compare_cli_loose_input_resolves(compare_env, capsys, tmp_path):
    """compare 宽松输入: 「腾讯」自动解析为 0700.HK; 提示走 stderr, stdout 纯 JSON."""
    cli.main(["compare", "AAPL", "腾讯", "--months", "3", "--json",
              "--output", str(tmp_path / "x.html")])
    captured = capsys.readouterr()
    data = json.loads(captured.out)  # stdout 必须是纯 JSON
    assert data["codes"] == ["AAPL", "0700.HK"]
    assert any("腾讯" in line for line in captured.err.splitlines())


def test_compare_cli_json_series_and_change(compare_env, capsys, tmp_path):
    """compare --json: 每代码归一化序列 (起点=100) + 区间涨跌; 输入可直接喂 kline."""
    out = _run(capsys, "compare", "AAPL", "0700.HK", "--months", "3", "--json",
               "--output", str(tmp_path / "x.html"))
    data = json.loads(out)
    assert data["codes"] == ["AAPL", "0700.HK"]
    aapl, hk = data["lines"]
    assert aapl["first"] == 100.0
    assert aapl["change_pct"] == pytest.approx(2.9, abs=0.1)
    assert hk["first"] == 100.0
    # 0700.HK 起点 300 vs AAPL 起点 100: 归一化后序列可比
    assert aapl["series"][0]["value"] == 100.0




def test_compare_needs_two_valid_codes(compare_env, capsys):
    """有效代码不足 2 个: 报错退出 (退出码 2)."""
    with pytest.raises(SystemExit) as e:
        _run(capsys, "compare", "AAPL", "不存在的股", "--json")
    assert e.value.code == 2


def test_resolve_inputs_dedupes_and_warns():
    codes, warnings = resolve_inputs(["AAPL", "AAPL", "不存在的股"])
    assert codes == ["AAPL"]
    assert any("无法识别" in w for w in warnings)
