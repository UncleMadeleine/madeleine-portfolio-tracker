"""进程内后台刷新协调器 (同 key 去重 + 最短重试间隔 + 完成记账).

行情/汇率与财经日历原先各写一份几乎相同的协调逻辑 (锁、inflight 集合、
完成时间戳、历史裁剪、后台线程)。这里收敛为一个可复用类, 行为保持一致:

- 同一 key 不并发 (多标签页/连续 rerun 只发一次网络);
- 失败后按 ``min_interval`` 节流, 避免看门狗高频重试;
- 完成时间戳只保留最近 ``history`` 条, 防长驻服务内存无限增长;
- 后台线程只执行调用方传入的纯取数函数, 不碰任何 Streamlit API。
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

__all__ = ["RefreshCoordinator"]


class RefreshCoordinator:
    """一个数据面的刷新协调器.

    ``lock`` / ``inflight`` / ``done_at`` 公开, 便于测试和旧调用方直接
    观察或重置状态 (与原先模块级变量同对象语义)。
    """

    def __init__(
        self,
        *,
        name: str,
        min_interval: float,
        history: int = 64,
        log_label: str = "后台刷新",
    ) -> None:
        self.name = name
        self.min_interval = float(min_interval)
        self.history = int(history)
        self.log_label = log_label
        self.lock = threading.Lock()
        self.inflight: set[tuple] = set()
        self.done_at: dict[tuple, float] = {}

    def state(self, key: tuple) -> tuple[bool, float | None]:
        """返回 (是否正在刷新, 最近完成时间戳)."""
        with self.lock:
            return key in self.inflight, self.done_at.get(key)

    def start(self, key: tuple, fn) -> bool:
        """启动一次后台刷新; 返回是否真正发起 (去重/节流命中时 False)."""
        now = time.time()
        with self.lock:
            if key in self.inflight:
                return False
            if now - self.done_at.get(key, 0.0) < self.min_interval:
                return False
            self.inflight.add(key)
            self._prune_locked()

        def _run() -> None:
            try:
                fn()
            except Exception:  # noqa: BLE001 - 旧数据继续展示, 下轮重试
                logger.warning(
                    "%s失败 (key=%s), 下轮重试",
                    self.log_label,
                    key,
                    exc_info=True,
                )
            finally:
                with self.lock:
                    self.inflight.discard(key)
                    self.done_at[key] = time.time()

        threading.Thread(target=_run, name=self.name, daemon=True).start()
        return True

    def _prune_locked(self) -> None:
        if len(self.done_at) <= self.history:
            return
        for old in sorted(self.done_at, key=self.done_at.get)[
            : len(self.done_at) - self.history
        ]:
            self.done_at.pop(old, None)
