"""共享小工具."""

from __future__ import annotations

import math
import threading
from concurrent.futures import Future


def finite_float(value: object) -> float | None:
    """可选数值归一: None/空串/不可解析/NaN/±Inf → None, 其余转 float.

    配置、缓存、数据源响应和持仓文件都会出现 ``"nan"``/``inf``/``None`` 等
    非有限字面量。各模块曾各写一份同样的归一函数, 口径容易漂移 (例如只挡
    NaN 不挡 Inf), 统一收敛到这里。
    """
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


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
