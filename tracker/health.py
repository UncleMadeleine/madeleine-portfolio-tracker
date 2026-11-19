"""数据源健康观测: 记录真实回源结果, 聚合成功率/耗时/最近失败.

实现拆分:
- health_record.py  span / record / flush 与展示常量 (provider 热路径)
- health_report.py  summary / coverage / integrity / report / probe
本模块仅做稳定 API 的再导出, 保持 ``tracker.health`` 导入路径不变。
"""

from __future__ import annotations

from .health_record import (  # noqa: F401 - 兼容再导出
    BUFFER_MAX,
    DISABLE_ENV,
    FUNDAMENTALS_TTL,
    KIND_TEXT,
    MAX_EVENTS,
    RETENTION_SECONDS,
    SLOW_MS,
    STATUS_ICON,
    STATUS_TEXT,
    age_text,
    enabled,
    flush,
    label,
    record,
    source_id,
    span,
)
from .health_report import (  # noqa: F401 - 兼容再导出
    coverage,
    integrity,
    probe,
    report,
    summary,
)

__all__ = [
    "DISABLE_ENV",
    "KIND_TEXT",
    "RETENTION_SECONDS",
    "STATUS_ICON",
    "STATUS_TEXT",
    "age_text",
    "coverage",
    "flush",
    "integrity",
    "label",
    "probe",
    "record",
    "report",
    "source_id",
    "span",
    "summary",
]
