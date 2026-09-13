"""应用设置与 portfolio.json 读写测试 (纯文件操作, 不依赖 Streamlit 运行时)."""
from __future__ import annotations

import json

from tracker.ui import settings as S


def test_save_portfolio_file_stamps_authoritative_type(tmp_path, monkeypatch):
    """页面保存持仓时必须补写权威 type 字段 (与 CLI/IBKR 导入路径一致)."""
    monkeypatch.setattr(S, "PORTFOLIO_PATH", tmp_path / "portfolio.json")
    S.save_portfolio_file(
        {
            "base_currency": "CNY",
            "holdings": [
                {"symbol": "600519.SH", "quantity": 10, "avg_cost": 1500.0},
                {"symbol": "BTCUSDT", "quantity": 2},
                {"symbol": "BRK-B", "quantity": 1},
            ],
        }
    )
    data = json.loads((tmp_path / "portfolio.json").read_text(encoding="utf-8"))
    assert [h["type"] for h in data["holdings"]] == ["cn", "crypto", "global"]
    assert data["base_currency"] == "CNY"


def test_save_portfolio_file_keeps_other_keys(tmp_path, monkeypatch):
    """保存持仓不得丢掉文件中的文档/自定义键, 且 NaN 单元格被清理."""
    path = tmp_path / "portfolio.json"
    path.write_text(
        json.dumps({"_说明": "文档", "base_currency": "USD", "holdings": []}),
        encoding="utf-8",
    )
    monkeypatch.setattr(S, "PORTFOLIO_PATH", path)
    S.save_portfolio_file(
        {"holdings": [{"symbol": "AAPL", "quantity": 10, "avg_cost": float("nan")}]}
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["_说明"] == "文档"
    assert data["base_currency"] == "USD"
    assert data["holdings"] == [{"symbol": "AAPL", "quantity": 10, "type": "global"}]
