"""存储层 (portfolio.json / watchlist.json / settings.json) 读写测试 (纯文件操作)."""
from __future__ import annotations

import json

import pytest

from tracker import storage


def test_save_portfolio_stamps_authoritative_type(tmp_path):
    """保存持仓时必须补写权威 type 字段 (与 CLI/导入路径一致)."""
    path = tmp_path / "portfolio.json"
    storage.save_portfolio(
        {
            "base_currency": "CNY",
            "holdings": [
                {"symbol": "600519.SH", "quantity": 10, "avg_cost": 1500.0},
                {"symbol": "BTCUSDT", "quantity": 2},
                {"symbol": "BRK-B", "quantity": 1},
            ],
        },
        path,
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    assert [h["type"] for h in data["holdings"]] == ["cn", "crypto", "global"]
    assert data["base_currency"] == "CNY"


def test_save_portfolio_keeps_other_keys(tmp_path):
    """保存持仓不得丢掉文件中的文档/自定义键, 且 NaN 单元格被清理."""
    path = tmp_path / "portfolio.json"
    path.write_text(
        json.dumps({"_说明": "文档", "base_currency": "USD", "holdings": []}),
        encoding="utf-8",
    )
    storage.save_portfolio(
        {"holdings": [{"symbol": "AAPL", "quantity": 10, "avg_cost": float("nan")}]},
        path,
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["_说明"] == "文档"
    assert data["base_currency"] == "USD"
    assert data["holdings"] == [{"symbol": "AAPL", "quantity": 10, "type": "global"}]


def test_load_portfolio_missing_file_returns_empty(tmp_path):
    """文件缺失返回空组合 (UI/CLI 行为一致)."""
    data = storage.load_portfolio(tmp_path / "nope.json")
    assert data == {"base_currency": "CNY", "holdings": []}


def test_load_watchlist_stamps_type(tmp_path):
    """读取侧同样补写权威 type (手改 JSON 无效)."""
    path = tmp_path / "watchlist.json"
    path.write_text(
        json.dumps({"watchlist": [{"symbol": "TSLA", "lists": ["科技"], "type": "cn"}]}),
        encoding="utf-8",
    )
    data = storage.load_watchlist(path)
    assert data["watchlist"][0]["type"] == "global"


def test_save_watchlist_stamps_type_and_migrates_keys(tmp_path):
    path = tmp_path / "watchlist.json"
    storage.save_watchlist(
        {"watchlist": [{"symbol": "0700.HK", "upper": 300}]}, path
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["watchlist"] == [
        {"symbol": "0700.HK", "upper_1": 300, "type": "global"}
    ]


def test_settings_roundtrip_and_defaults(tmp_path):
    path = tmp_path / "settings.json"
    assert storage.load_settings(path) == storage.DEFAULT_SETTINGS
    storage.save_settings({"color_scheme": "intl"}, path)
    assert storage.load_settings(path)["color_scheme"] == "intl"
    path.write_text("not json{", encoding="utf-8")
    assert storage.load_settings(path) == storage.DEFAULT_SETTINGS


def test_backup_file(tmp_path):
    src = tmp_path / "p.json"
    assert storage.backup_file(src) is None
    src.write_text("{}", encoding="utf-8")
    assert storage.backup_file(src) == str(src) + ".bak"
    assert (tmp_path / "p.json.bak").exists()


def test_load_portfolio_recovers_from_backup_when_corrupt(tmp_path):
    """portfolio.json 损坏时从 .bak 恢复, 不静默返回空组合 (否则下次保存会清空数据)."""
    path = tmp_path / "portfolio.json"
    path.write_text("{ truncated", encoding="utf-8")
    (tmp_path / "portfolio.json.bak").write_text(
        json.dumps({"base_currency": "USD", "holdings": [{"symbol": "AAPL", "quantity": 3}]}),
        encoding="utf-8",
    )
    data = storage.load_portfolio(path)
    assert data["holdings"] == [{"symbol": "AAPL", "quantity": 3}]


def test_load_portfolio_corrupt_without_backup_raises_readable_error(tmp_path):
    """无 .bak 可恢复时报可读错误, 而不是裸 JSONDecodeError."""
    path = tmp_path / "portfolio.json"
    path.write_text("{ truncated", encoding="utf-8")
    with pytest.raises(ValueError, match="不是合法 JSON"):
        storage.load_portfolio(path)


def test_load_watchlist_corrupt_without_backup_raises_readable_error(tmp_path):
    path = tmp_path / "watchlist.json"
    path.write_text("{ truncated", encoding="utf-8")
    with pytest.raises(ValueError, match="不是合法 JSON"):
        storage.load_watchlist(path)


def test_save_portfolio_failure_leaves_original_intact(tmp_path):
    """写盘失败不得破坏已有文件 —— 原地截断写会留下半截 JSON."""
    path = tmp_path / "portfolio.json"
    storage.save_portfolio({"holdings": [{"symbol": "AAPL", "quantity": 1}]}, path)
    before = path.read_text(encoding="utf-8")
    with pytest.raises(TypeError):
        storage.save_portfolio(
            {"holdings": [{"symbol": "AAPL", "quantity": object()}]}, path
        )
    assert path.read_text(encoding="utf-8") == before
    assert [p.name for p in tmp_path.iterdir()] == ["portfolio.json"]
