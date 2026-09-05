"""共享小工具."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as _TimeoutError


def with_timeout(fn, timeout: float, *args, **kwargs):
    """在线程中执行 fn 并强制超时, 避免阻塞型调用卡死."""
    with ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(fn, *args, **kwargs)
        try:
            return future.result(timeout=timeout)
        except _TimeoutError:
            future.cancel()
            raise RuntimeError(f"{getattr(fn, '__name__', fn)} 超时({timeout}s)")
