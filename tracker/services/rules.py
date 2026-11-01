"""兼容再导出: 阈值规则已提升为顶层 `tracker.rules` (纯领域模块).

保留旧导入路径 `tracker.services.rules`; 新代码请直接从 `tracker.rules`
导入。放在 services 下会迫使 storage 反向依赖用例层, 已解耦。
"""

from ..rules import (
    METRIC_MCAP,
    METRIC_PB,
    METRIC_PRICE,
    STATUS_LOWER_1,
    STATUS_LOWER_2,
    STATUS_NO_DATA,
    STATUS_RANK,
    STATUS_UPPER_1,
    STATUS_UPPER_2,
    STATUS_WITHIN,
    entry_book_value,
    entry_market_cap,
    evaluate_thresholds,
    metric_for_entry,
    metric_value_for_quote,
    parse_thresholds,
)

__all__ = [
    "METRIC_MCAP",
    "METRIC_PB",
    "METRIC_PRICE",
    "STATUS_LOWER_1",
    "STATUS_LOWER_2",
    "STATUS_NO_DATA",
    "STATUS_RANK",
    "STATUS_UPPER_1",
    "STATUS_UPPER_2",
    "STATUS_WITHIN",
    "entry_book_value",
    "entry_market_cap",
    "evaluate_thresholds",
    "metric_for_entry",
    "metric_value_for_quote",
    "parse_thresholds",
]
