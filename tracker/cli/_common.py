"""CLI 子命令共享工具 (JSON 序列化 / 代码归一化 / 错误退出)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pandas as pd

from .. import __version__ as VERSION  # noqa: F401  (单一版本来源, 经 cli 再导出)
from ..symbols import parse
from ..util import records as _util_records


def _records(df: pd.DataFrame) -> list[dict]:
    """DataFrame -> records, NaN/NaT 转 None 便于 JSON 序列化 (实现见 util).

    兼容既有 `from ._common import _records` 导入路径; 实现放 util 是为了
    避免 services.snapshot → cli._common → cli.__init__ 的循环导入。
    """
    return _util_records(df)


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
    目标目录不存在时自动建目录 (否则直接抛 FileNotFoundError traceback)。
    权限按普通 open() 语义 (mkstemp 是 0600, 直接 replace 会让 export/report
    产出的文件其它用户/容器内其它 UID 读不到)。
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
        try:
            os.chmod(
                tmp,
                p.stat().st_mode & 0o7777
                if p.exists()
                else 0o666 & ~_current_umask(),
            )
        except OSError:
            pass
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _current_umask() -> int:
    """读取当前 umask (os.umask 是读-写原子对, 取完立刻还原)。"""
    cur = os.umask(0o022)
    os.umask(cur)
    return cur


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
