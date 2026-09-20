"""阈值规则 (纯函数): 两级上下限的状态判定与距离计算.

无任何 I/O: 输入归一化阈值与现价, 输出状态与距离百分比,
便于离线单测, 由 services.watchlist 组装视图时调用。
"""
from __future__ import annotations

import math
from collections.abc import Mapping

STATUS_UPPER_1 = "🟠 突破上限 I"
STATUS_UPPER_2 = "🔴 突破上限 II"
STATUS_LOWER_1 = "🟡 跌破下限 I"
STATUS_LOWER_2 = "🟢 跌破下限 II"
STATUS_WITHIN = "⚪ 区间内"

# 严重度排序: II 级突破 > I 级突破 > 跌破 I > 跌破 II > 区间内
STATUS_RANK = {
    STATUS_UPPER_2: 1,
    STATUS_UPPER_1: 2,
    STATUS_LOWER_1: 3,
    STATUS_LOWER_2: 4,
    STATUS_WITHIN: 5,
}

_THRESHOLD_KEYS = ("upper_1", "upper_2", "lower_1", "lower_2")


def _num(v) -> float | None:
    """阈值字段归一: 空/非有限数值 → None, 其余转 float."""
    if v in (None, ""):
        return None
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def parse_thresholds(entry: dict) -> dict[str, float | None]:
    """条目 → 归一化阈值 (非法/缺失为 None)."""
    return {k: _num(entry.get(k)) for k in _THRESHOLD_KEYS}


def evaluate_thresholds(
    thresholds: Mapping[str, float | None], price: float
) -> tuple[str, dict[str, float | None]]:
    """按两级上下限判定状态并计算距离 (%); 无阈值时返回 (STATUS_WITHIN, {}).

    判定顺序: 上限 II > 上限 I > 下限 II > 下限 I, 边界取等号 (含)。
    dist 仅包含已设置阈值对应的距离键, 调用方须用 .get() 安全访问。
    """
    upper_1 = thresholds.get("upper_1")
    upper_2 = thresholds.get("upper_2")
    lower_1 = thresholds.get("lower_1")
    lower_2 = thresholds.get("lower_2")
    if upper_2 is not None and price >= upper_2:
        status = STATUS_UPPER_2
    elif upper_1 is not None and price >= upper_1:
        status = STATUS_UPPER_1
    elif lower_2 is not None and price <= lower_2:
        status = STATUS_LOWER_2
    elif lower_1 is not None and price <= lower_1:
        status = STATUS_LOWER_1
    else:
        status = STATUS_WITHIN
    dist: dict[str, float | None] = {}
    if upper_1 is not None and price > 0:
        dist["dist_upper_1_pct"] = (upper_1 / price - 1) * 100
    if upper_2 is not None and price > 0:
        dist["dist_upper_2_pct"] = (upper_2 / price - 1) * 100
    if lower_1 is not None and lower_1 > 0:
        dist["dist_lower_1_pct"] = (price / lower_1 - 1) * 100
    if lower_2 is not None and lower_2 > 0:
        dist["dist_lower_2_pct"] = (price / lower_2 - 1) * 100
    return status, dist
