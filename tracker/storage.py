"""本地 JSON 存储层: portfolio.json / watchlist.json / settings.json 唯一读写口.

CLI 与 Streamlit 页面共用同一组函数, 保证 schema 归一与权威 type 打标语义单点化:
任何入口写出的文件, 另一入口读到的结构一致。

- type 字段为系统维护的内部域标记 (global/cn/crypto), 读/写两侧均按
  symbols.type_for_symbol() 推导并覆写, 用户手改 JSON 无效。
- portfolio.json 保存时合并磁盘上已有键 (如 _说明 等文档/自定义字段), 不丢用户数据。
- 写盘统一 json.dumps(..., allow_nan=False): NaN/None 单元格在清洗阶段剔除。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

from .symbols import type_for_symbol

# 数据文件锚定仓库根 (包内目录会随部署位置漂移)
_ROOT = Path(__file__).resolve().parent.parent
PORTFOLIO_PATH = _ROOT / "portfolio.json"
WATCHLIST_PATH = _ROOT / "watchlist.json"
SETTINGS_PATH = _ROOT / "settings.json"

DEFAULT_BASE_CURRENCY = "CNY"


class CorruptDataError(ValueError):
    """数据文件损坏且无法从 .bak 恢复 (CLI 据此给出可读错误而非 traceback)."""


# ---------------------------------------------------------------------------
# 通用
# ---------------------------------------------------------------------------

def backup_file(path: str | Path) -> str | None:
    """备份 <path> 为 <path>.bak; 文件不存在返回 None。导入写盘前统一调用。"""
    p = Path(path)
    if not p.exists():
        return None
    backup = str(p) + ".bak"
    shutil.copy(p, backup)
    return backup


def _read_json(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def _read_json_or_recover(path: str | Path) -> dict:
    """读取 JSON; 文件损坏时尝试同名 .bak 恢复, 仍失败则抛出可读错误。

    不静默返回空数据 —— 否则下一次保存会把损坏文件直接覆盖成空组合, 造成数据丢失。
    """
    try:
        return _read_json(path)
    except json.JSONDecodeError as e:
        bak = Path(f"{path}.bak")
        if bak.exists():
            try:
                return _read_json(bak)
            except (json.JSONDecodeError, OSError):
                pass
        raise CorruptDataError(
            f"{path} 不是合法 JSON ({e.msg}, 第 {e.lineno} 行); "
            f"已尝试从 {bak.name} 恢复但失败, 请修复或删除该文件后重试。"
        ) from e


def _write_json(data: dict, path: str | Path) -> None:
    """原子写: 先写同目录临时文件再 os.replace, 中断不会把原文件截断成半截 JSON。"""
    p = Path(path)
    payload = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=f".{p.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _stamp_type(row: dict) -> dict:
    """条目补写权威 type 字段 (系统维护, 用户不可见不可改)."""
    sym = str(row.get("symbol", "")).strip()
    if sym:
        try:
            row["type"] = type_for_symbol(sym)
        except Exception:
            pass
    return row


# ---------------------------------------------------------------------------
# portfolio.json
# ---------------------------------------------------------------------------

def load_portfolio(path: str | Path = PORTFOLIO_PATH) -> dict:
    """读取持仓文件; 文件缺失时返回空组合。"""
    if not Path(path).exists():
        return {"base_currency": DEFAULT_BASE_CURRENCY, "holdings": []}
    return _read_json_or_recover(path)


def _clean_rows(rows: list) -> list[dict]:
    """剔除 NaN/None 单元格, 保证 allow_nan=False 可序列化 (data_editor 空单元格等)。"""
    out = []
    for r in rows:
        clean = {}
        for k, v in dict(r).items():
            if isinstance(v, float) and v != v:  # NaN
                continue
            if v is None:
                continue
            clean[k] = v
        out.append(clean)
    return out


def save_portfolio(data: dict, path: str | Path = PORTFOLIO_PATH) -> None:
    """保存持仓/基础货币 (写盘唯一入口)。

    保留文件中其它键 (_说明 等文档/自定义字段), holdings 逐行清洗并覆写权威 type。
    """
    p = Path(path)
    if p.exists():
        try:
            merged = _read_json(p)
        except (json.JSONDecodeError, OSError):
            merged = {}
    else:
        merged = {}
    merged.update(data)
    merged.setdefault("base_currency", DEFAULT_BASE_CURRENCY)
    merged["holdings"] = [_stamp_type(r) for r in _clean_rows(merged.get("holdings", []))]
    _write_json(merged, p)


# ---------------------------------------------------------------------------
# watchlist.json
# ---------------------------------------------------------------------------

def parse_lists(v) -> list[str]:
    """把逗号分隔字符串/列表解析为归属列表; 空则回退默认。"""
    if v is None or (isinstance(v, float) and v != v):  # NaN
        return ["默认"]
    if isinstance(v, list):
        items = [str(x).strip() for x in v]
    else:
        items = [x.strip() for x in str(v).replace("，", ",").split(",")]
    return [x for x in items if x] or ["默认"]


def normalize_watch_entry(e: dict) -> dict:
    """自选条目 schema 归一: 旧阈值键名迁移 + 权威 type 覆写。"""
    out = dict(e)
    for old, new in (("upper", "upper_1"), ("lower", "lower_1")):
        if old in out and new not in out:
            out[new] = out.pop(old)
    return _stamp_type(out)


def load_watchlist(path: str | Path = WATCHLIST_PATH) -> dict:
    """读取自选; 兼容旧扁平/分组格式并迁移为扁平条目, 读取侧补写权威 type。"""
    p = Path(path)
    if not p.exists():
        return {"watchlist": []}
    raw = _read_json_or_recover(p)
    if "watchlist" in raw:
        entries = []
        for e in raw["watchlist"]:
            e = dict(e)
            e["lists"] = parse_lists(e.get("lists"))
            entries.append(normalize_watch_entry(e))
        return {"watchlist": entries}
    if "watchlists" in raw:
        merged: dict[str, dict] = {}
        for name, lst in raw["watchlists"].items():
            for e in lst:
                sym = str(e.get("symbol", "")).strip()
                if not sym:
                    continue
                if sym not in merged:
                    merged[sym] = dict(e)
                    merged[sym].setdefault("lists", [])
                else:
                    # 同名代码在多个列表中各有一份阈值/备注: 保留先出现的,
                    # 缺失字段 (如只在旧列表设过 upper) 从后出现的补齐
                    for k, v in e.items():
                        if k == "symbol" or k == "lists":
                            continue
                        merged[sym].setdefault(k, v)
                if name not in merged[sym]["lists"]:
                    merged[sym]["lists"].append(name)
        entries = []
        for e in merged.values():
            if not e["lists"]:
                e["lists"] = ["默认"]
            # normalize_watch_entry 返回副本 (不改原对象), 必须接收返回值
            entries.append(normalize_watch_entry(e))
        return {"watchlist": entries}
    return {"watchlist": []}


def save_watchlist(data: dict, path: str | Path = WATCHLIST_PATH) -> None:
    """保存自选 (写盘唯一入口): 逐条 schema 归一 + 权威 type 覆写。"""
    entries = [normalize_watch_entry(dict(e)) for e in data.get("watchlist", [])]
    _write_json({"watchlist": entries}, path)


# ---------------------------------------------------------------------------
# settings.json
# ---------------------------------------------------------------------------

# 涨跌配色: cn = 红涨绿跌 (A股软件习惯, 默认) / intl = 绿涨红跌 (国际配色)
SCHEME_CN = "cn"
SCHEME_INTL = "intl"
DEFAULT_SETTINGS = {"color_scheme": SCHEME_CN, "prefer_akshare": False, "use_ibkr": False}


def load_settings(path: str | Path = SETTINGS_PATH) -> dict:
    """读取设置; 文件缺失/损坏时回退默认值, 未知键与非法值忽略。"""
    data = dict(DEFAULT_SETTINGS)
    p = Path(path)
    if p.exists():
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            raw = None
        if isinstance(raw, dict):
            data.update({k: v for k, v in raw.items() if k in DEFAULT_SETTINGS})
    if data["color_scheme"] not in (SCHEME_CN, SCHEME_INTL):
        data["color_scheme"] = SCHEME_CN
    data["prefer_akshare"] = bool(data["prefer_akshare"])
    data["use_ibkr"] = bool(data["use_ibkr"])
    return data


def save_settings(data: dict, path: str | Path = SETTINGS_PATH) -> None:
    """保存设置 (写盘唯一入口): 与默认键合并, 防止丢键。"""
    _write_json({**DEFAULT_SETTINGS, **data}, path)
