"""长桥 OAuth 状态文件协议 (UI 父进程 ↔ 授权子进程共用).

子进程把 ``waiting``/``ok``/``error`` 状态原子写入 JSON 文件, UI 轮询读取。
读写逻辑收敛在这里, 避免 ``longport_oauth`` 与 ``ui.longport_login`` 各写一份
临时文件/时间戳/损坏容错逻辑而逐渐分叉。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

STATE_FILENAME = "longport_oauth_state.json"


def write_state(path: str | Path, payload: dict) -> None:
    """原子写状态 (tmp + replace), 附带当前时间戳; I/O 失败静默忽略."""
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({**payload, "ts": time.time()}, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(target)
    except OSError:
        pass


def read_state(path: str | Path, max_age: float | None = None) -> dict | None:
    """读取状态; 文件缺失/损坏/非对象/超过 max_age 返回 None."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if max_age is not None and time.time() - data.get("ts", 0) >= max_age:
        return None
    return data
