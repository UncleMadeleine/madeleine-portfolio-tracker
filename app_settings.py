"""应用显示设置 (settings.json): 涨跌配色 / 数据源偏好, 设置页与各页面共用.

基础货币仍存于 portfolio.json (CLI snapshot/--base 共享同一来源), 但由「设置」页编辑;
本模块同时提供 portfolio.json 的读写助手, 供 app.py (持仓编辑) 与 settings_page.py 复用.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from tracker.charting import (
    CN_DOWN_COLOR,
    CN_UP_COLOR,
    INTL_DOWN_COLOR,
    INTL_UP_COLOR,
)
from tracker.symbols import type_for_symbol

SETTINGS_PATH = Path(__file__).parent / "settings.json"
PORTFOLIO_PATH = Path(__file__).parent / "portfolio.json"

# 涨跌配色: cn = 红涨绿跌 (A股软件习惯, 默认) / intl = 绿涨红跌 (国际配色)
SCHEME_CN = "cn"
SCHEME_INTL = "intl"
SCHEME_LABELS = {SCHEME_CN: "红涨绿跌 (A股习惯)", SCHEME_INTL: "绿涨红跌 (国际)"}
DEFAULT_SETTINGS = {"color_scheme": SCHEME_CN, "prefer_akshare": False, "use_ibkr": False}

BASE_CURRENCIES = ["CNY", "USD", "EUR", "HKD"]


def load_settings() -> dict:
    """读取设置; 文件缺失/损坏时回退默认值, 未知键与非法值忽略."""
    data = dict(DEFAULT_SETTINGS)
    if SETTINGS_PATH.exists():
        try:
            raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            raw = None
        if isinstance(raw, dict):
            data.update({k: v for k, v in raw.items() if k in DEFAULT_SETTINGS})
    if data["color_scheme"] not in SCHEME_LABELS:
        data["color_scheme"] = SCHEME_CN
    data["prefer_akshare"] = bool(data["prefer_akshare"])
    data["use_ibkr"] = bool(data["use_ibkr"])
    return data


def save_settings(data: dict) -> None:
    SETTINGS_PATH.write_text(
        json.dumps({**DEFAULT_SETTINGS, **data}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def green_up(settings: dict | None = None) -> bool:
    """True = 绿涨红跌 (国际配色)."""
    if settings is None:
        settings = load_settings()
    return settings.get("color_scheme") == SCHEME_INTL


def up_down_colors(settings: dict | None = None) -> tuple[str, str]:
    """(涨色, 跌色) 十六进制, 与 K线组件配色一致."""
    return (INTL_UP_COLOR, INTL_DOWN_COLOR) if green_up(settings) else (CN_UP_COLOR, CN_DOWN_COLOR)


def up_down_tags(settings: dict | None = None) -> tuple[str, str]:
    """Streamlit 内建 :red[]/:green[] 标签按方案映射 (涨, 跌)."""
    return ("green", "red") if green_up(settings) else ("red", "green")


def delta_color(settings: dict | None = None) -> str:
    """st.metric 的 delta_color: 让正 delta 显示为涨色."""
    return "normal" if green_up(settings) else "inverse"


# ---- portfolio.json 读写 (app.py 持仓编辑 与 设置页基础货币共用) ----


def load_portfolio_file() -> dict:
    if PORTFOLIO_PATH.exists():
        return json.loads(PORTFOLIO_PATH.read_text(encoding="utf-8"))
    return {"base_currency": "CNY", "holdings": []}


def _clean_rows(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        clean = {}
        for k, v in r.items():
            if isinstance(v, float) and pd.isna(v):
                continue
            if v is None:
                continue
            clean[k] = v
        out.append(clean)
    return out


def _stamp_type(row: dict) -> dict:
    """持仓条目补写权威 type 字段 (系统维护, 用户不可见不可改)."""
    sym = str(row.get("symbol", "")).strip()
    if sym:
        try:
            row["type"] = type_for_symbol(sym)
        except Exception:
            pass
    return row


def save_portfolio_file(data: dict) -> None:
    """保存持仓/基础货币, 保留文件中其它键 (_说明 等文档/自定义字段)."""
    merged = load_portfolio_file()
    merged.update(data)
    merged["holdings"] = [
        _stamp_type(r) for r in _clean_rows(merged.get("holdings", []))
    ]
    PORTFOLIO_PATH.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
