"""财经日历公共常量与进度/缓存元数据 helper.

从 events.py 拆出: 各数据源共用的 TTL、展示标签、进度累加器与缓存元数据
合并逻辑。不依赖网络。
"""

from __future__ import annotations

from . import cache as cache_mod

# 单票缓存 TTL (秒): 有事件 6h (与 fundamentals 一致的低频报表口径)
CAL_TTL = cache_mod.CALENDAR_TTL
# 单票空结果负缓存 TTL: Yahoo 无数据的标的 24h 内不重试
CAL_NEG_TTL = 86400
# 陈旧可渲染期: 超过正/负缓存 TTL 但未超过该年龄的日历仍先展示, 由页面/
# 后台线程静默重取 (与行情 cache.STALE_TTL 同构的 stale-while-revalidate)。
# 日历是低频数据, 展示 7 天内的旧结果远比首屏干等整批网络请求可用。
CAL_STALE_TTL = 7 * 86400

# 事件类型 → 展示名 (CLI/UI 共用)
EVENT_TYPE_LABELS = {
    "earnings": "财报",
    "ex_dividend": "除息/除权",
    "record_date": "股权登记",
    "dividend": "派息日",
}
DATE_STATUS_LABELS = {
    "planned": "预约",
    "changed": "已变更",
    "actual": "已披露",
    "estimate": "估计",
}


class _ProgressReporter:
    """取数进度累加器: 计划总量可后续追加 (长桥兜底按需才知道工作量).

    对外仍是 fetch_calendar(progress=(done, total, label)) 的旧契约:
    label 是当前代码/阶段的可读文本, total 可能随阶段增加。UI 用
    done/total 画进度条, CLI 直接打印文本。
    """

    def __init__(self, cb=None):
        self._cb = cb
        self.done = 0
        self.total = 0

    def plan(self, n: int) -> None:
        if n > 0:
            self.total += int(n)

    def step(self, label: str | None = None) -> None:
        self.done += 1
        if self._cb is not None:
            # total 只会追加不会回退; done 超过计划量时至少保证分母不倒退
            self._cb(self.done, max(self.total, self.done), label)

    def finish(self) -> None:
        if self._cb is not None:
            total = max(self.total, self.done)
            self._cb(total, total, None)


def _merge_cache_meta(meta: dict, part: dict | None) -> None:
    """合并分片缓存元数据 (stale/missing/stale_age/used) 到累计 meta."""
    if not part:
        return
    meta["stale"] = bool(meta.get("stale")) or bool(part.get("stale"))
    meta["missing"] = bool(meta.get("missing")) or bool(part.get("missing"))
    meta["used"] = bool(meta.get("used")) or bool(part.get("used"))
    try:
        meta["stale_age"] = max(
            float(meta.get("stale_age") or 0.0),
            float(part.get("stale_age") or 0.0),
        )
    except (TypeError, ValueError):
        pass


def _is_valid_eps(v: float) -> bool:
    from math import isfinite

    return isfinite(v) and v > 0
