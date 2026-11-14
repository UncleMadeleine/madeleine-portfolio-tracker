"""CLI 子命令共享工具 (JSON 序列化 / 代码归一化 / 错误退出)."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path

from .. import __version__ as VERSION  # noqa: F401  (单一版本来源, 经 cli 再导出)
from ..symbols import parse
from ..util import records as _util_records


def months_arg(value: str) -> int:
    """--months 校验: 1..12 个自然月, 防止误传超大值拉几十个报表期.

    放在 _common 而非 cli.calendar: build_parser 需要它, 但不应因此导入
    calendar → events → pandas (--help 轻量路径)。
    """
    try:
        n = int(value)
    except (TypeError, ValueError) as e:
        raise argparse.ArgumentTypeError(f"--months 需为整数: {value!r}") from e
    if not 1 <= n <= 12:
        raise argparse.ArgumentTypeError("--months 需在 1..12 之间")
    return n


def _records(df) -> list[dict]:
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


def parse_symbols_or_fail(symbols, *, as_json: bool):
    """解析 CLI 输入代码; 全部无效时不写盘并退出 (portfolio/watchlist 共用)."""
    parsed: list = []
    skipped: list[str] = []
    for s in symbols:
        try:
            parsed.append(parse(s))
        except ValueError as e:
            skipped.append(f"跳过 {s}: {e}")
    if not parsed:
        if as_json:
            _print_json({"changed": [], "skipped": skipped})
        else:
            for s in skipped:
                print(f"⚠ {s}")
        _finish_with_error("没有可添加的有效代码")
    return parsed, skipped


def normalize_targets(symbols) -> set[str]:
    """删除命令的代码归一化: 非法输入保留大写原串, 便于精确报告未找到."""
    targets: set[str] = set()
    for s in symbols:
        try:
            targets.add(parse(s).yahoo)
        except ValueError:
            targets.add(s.strip().upper())
    return targets


def _source_strict(args) -> bool:
    """命令行是否显式指定了数据源偏好.

    显式指定时不能让 5 分钟磁盘缓存里的其它源结果短路 (与 ``quote`` 的
    ``--ibkr`` 语义一致): 否则 ``snapshot --ibkr`` 在 TTL 内仍输出上次
    yfinance 的价格。未指定时保持缓存优先, 首屏/CLI 才不会被拖慢。
    """
    return bool(
        getattr(args, "akshare", False)
        or getattr(args, "ibkr", False)
        or getattr(args, "longport", False)
        or getattr(args, "crypto_source", "auto") != "auto"
    )


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
        # newline="": ICS/CSV 文本自身已含 CRLF, 交给 Windows 的换行翻译
        # 会变成 \r\r\n; 其它文本保持原样写入, 由调用方决定换行。
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
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


def _is_na(v) -> bool:
    """标量缺失值判断 (NaN/NaT/pd.NA/None); 按需导入 pandas 保持 CLI 冷启动轻量."""
    if v is None:
        return True
    if isinstance(v, float):
        return math.isnan(v)
    if isinstance(v, str):
        return False
    try:
        import pandas as pd

        return bool(pd.isna(v))
    except (ImportError, TypeError, ValueError):
        return False


def _sanitize(d: dict) -> dict:
    """dict 中 NaN/None 统一为 None, 便于 CSV/MD 导出."""
    return {k: (None if _is_na(v) else v) for k, v in d.items()}


def _fmt_num(v, digits: int = 2) -> str:
    if _is_na(v):
        return "—"
    return f"{v:,.{digits}f}"


def _fmt_pct(v) -> str:
    if _is_na(v):
        return "—"
    return f"{v:+.1f}%"
