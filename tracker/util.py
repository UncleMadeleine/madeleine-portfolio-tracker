"""共享小工具."""

from __future__ import annotations

import threading
from concurrent.futures import Future


def records(df) -> list[dict]:
    """DataFrame → records (NaN/NaT 转 None 便于 JSON 序列化).

    放在 util 而不是 cli/_common: services 层也要用它, 从 cli 导入会触发
    `tracker.cli.__init__` → cli/export → services.snapshot 的循环导入。
    """
    import pandas as pd

    if df is None or df.empty:
        return []
    return df.astype(object).where(pd.notnull(df), None).to_dict(orient="records")


def with_timeout(fn, timeout: float, *args, **kwargs):
    """在守护线程中执行 fn 并强制超时, 避免阻塞型调用卡死.

    使用 daemon 线程而非 ThreadPoolExecutor, 因为后者的 __exit__ 会
    shutdown(wait=True), 即使 future.result 超时也会阻塞等待卡死的
    线程结束, 使超时形同虚设。守护线程在主进程退出时会被强制回收。
    """
    fut: Future = Future()

    def _runner():
        try:
            fut.set_result(fn(*args, **kwargs))
        except BaseException as e:  # noqa: BLE001 - 透传给调用方
            if not fut.done():
                fut.set_exception(e)

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    try:
        return fut.result(timeout=timeout)
    except Exception:
        fut.cancel()
        raise
