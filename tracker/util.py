"""共享小工具."""
from __future__ import annotations

import threading
from concurrent.futures import Future


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
            fut.set_exception(e)

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    return fut.result(timeout=timeout)
