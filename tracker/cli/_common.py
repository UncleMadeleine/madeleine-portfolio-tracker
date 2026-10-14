"""CLI 子命令共享工具 (JSON 序列化 / 代码归一化 / 错误退出)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pandas as pd

from ..symbols import parse

VERSION = "1.0.0"


def _records(df: pd.DataFrame) -> list[dict]:
    """DataFrame -> records, NaN/NaT 转 None 便于 JSON 序列化."""
    if df is None or df.empty:
        return []
    return df.astype(object).where(pd.notnull(df), None).to_dict(orient="records")


def _sym(e: dict) -> str:
    """归一化条目中的 symbol 为 Yahoo 代码."""
    raw = str(e.get("symbol", "")).strip()
    try:
        return parse(raw).yahoo
    except ValueError:
        return raw.upper()


def _print_json(payload) -> None:
    """以缩进 JSON 打印机器可读结果."""
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _finish_with_error(msg: str, code: int = 2) -> None:
    """打印错误到 stderr 并以指定退出码结束."""
    print(f"❌ {msg}", file=sys.stderr)
    raise SystemExit(code)


def atomic_write_text(path: str | Path, text: str) -> None:
    """原子写文本文件 (tmp + os.replace), 目录不存在则创建.

    与 storage._write_json 同一策略: 写入中途中断不会留下半截文件,
    目标目录不存在时自动建目录 (否则直接抛 FileNotFoundError traceback).
    """
    import tempfile

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=f".{p.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _sanitize(d: dict) -> dict:
    """dict 中 NaN/None 统一为 None, 便于 CSV/MD 导出."""
    return {
        k: (None if (v is None or (isinstance(v, float) and pd.isna(v))) else v)
        for k, v in d.items()
    }


def _fmt_num(v, digits: int = 2) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    return f"{v:,.{digits}f}"


def _fmt_pct(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    return f"{v:+.1f}%"
