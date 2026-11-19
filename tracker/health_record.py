"""健康观测记录: span / record / flush 与展示常量.

provider 热路径只依赖本模块; 聚合/报告逻辑见 health_report.py。
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager

# 健康状态的中文展示口径 (CLI doctor 与 Streamlit 健康页共用)
STATUS_ICON = {
    "ok": "✅",
    "degraded": "⚠️",
    "down": "❌",
    "idle": "·",
    "unknown": "?",
}
STATUS_TEXT = {
    "ok": "正常",
    "degraded": "降级",
    "down": "异常",
    "idle": "未使用",
    "unknown": "无数据",
}
KIND_TEXT = {
    "quote": "行情",
    "history": "K线",
    "fx": "汇率",
    "calendar": "日历",
    "other": "其它",
}
#: 估值快照 / 财报链接的磁盘缓存 TTL (与 valuation/filings 的 6h 对齐)
FUNDAMENTALS_TTL = 6 * 3600


def age_text(seconds: float | None) -> str:
    """秒数 → 紧凑年龄文案 (s/m/h/d); None 显示为 —."""
    if seconds is None:
        return "—"
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


#: 显式关闭记录 (测试/只读数据目录): 置 1 后 span/record 直接 no-op
DISABLE_ENV = "TRACKER_HEALTH_DISABLE"
#: 事件保留期与总量上限: 健康数据只用于近期排障, 不做长期审计
RETENTION_SECONDS = 7 * 86400
MAX_EVENTS = 20000
#: 内存缓冲: 攒够一批再写, 避免每只代码一次 connect/commit
BUFFER_MAX = 20

#: 各取数类型「偏慢」的阈值 (毫秒): 超过即 degraded, 但不影响成功率
SLOW_MS = {"quote": 5000.0, "history": 15000.0, "fx": 5000.0}
#: 一次回源不足这个次数时不因偶发失败判 down (冷启动首请求失败很常见)
_MIN_ATTEMPTS_FOR_DOWN = 3

_buffer: list[tuple] = []
_buffer_lock = threading.Lock()
_flush_count = 0


# ---------------------------------------------------------------------------
# 记录
# ---------------------------------------------------------------------------


def enabled() -> bool:
    return os.environ.get(DISABLE_ENV, "").strip() not in ("1", "true", "yes")


def _short_error(value, limit: int = 400) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def record(
    kind: str,
    source: str,
    ok: bool,
    latency_ms: float | None = None,
    symbol: str | None = None,
    error=None,
) -> None:
    """记录一次取数尝试 (非阻塞: 进内存缓冲, 满批后落盘)."""
    if not enabled():
        return
    row = (
        time.time(),
        str(kind),
        str(source or "unknown"),
        1 if ok else 0,
        float(latency_ms) if latency_ms is not None else None,
        str(symbol) if symbol else None,
        _short_error(error),
    )
    with _buffer_lock:
        _buffer.append(row)
        full = len(_buffer) >= BUFFER_MAX
    if full:
        flush()


def flush() -> None:
    """把缓冲区写入缓存库; 失败只丢弃本批 (诊断数据可再生)."""
    global _flush_count
    with _buffer_lock:
        if not _buffer:
            return
        rows = _buffer[:]
        _buffer.clear()
        _flush_count += 1
        should_prune = _flush_count % 50 == 0
    try:
        from . import cache as cache_mod

        cache_mod.add_health_events(rows)
        if should_prune:
            cache_mod.prune_health_events(time.time() - RETENTION_SECONDS, MAX_EVENTS)
    except Exception:  # noqa: BLE001 - 观测失败不影响主流程
        pass


class _Span:
    """``span`` 的可变结果: 调用方可改写 ok/error 表达「无异常但无数据」."""

    __slots__ = ("ok", "error", "detail")

    def __init__(self) -> None:
        self.ok = True
        self.error = None
        self.detail = None


@contextmanager
def span(kind: str, source: str, symbol: str | None = None):
    """包住一次真实回源; 退出时记录成功/失败与耗时 (异常原样抛出)."""
    sp = _Span()
    if not enabled():
        yield sp
        return
    started = time.perf_counter()
    try:
        yield sp
    except BaseException as e:
        elapsed = (time.perf_counter() - started) * 1000.0
        record(kind, source, False, elapsed, symbol, e)
        raise
    else:
        elapsed = (time.perf_counter() - started) * 1000.0
        record(
            kind,
            source,
            sp.ok,
            elapsed,
            symbol,
            None if sp.ok else (sp.error or "无数据"),
        )


# ---------------------------------------------------------------------------
# 来源识别
# ---------------------------------------------------------------------------

# 源函数没有统一接口传递 source id: ordered_sources 会优先写 __source_id__,
# 这里再按函数名兜底 (单 provider 直接调用 / 旧调用方)。新增源函数时若忘记
# 标注, 契约测试 tests/test_provider_contract.py 会把它抓出来。
_SOURCE_BY_NAME = {
    "_yahoo_quote": "yfinance",
    "_yahoo_batch": "yfinance",
    "_yahoo_history": "yfinance",
    "_yf_quote": "yfinance",
    "_yf_history": "yfinance",
    "_akshare_quote": "akshare",
    "_akshare_history": "akshare",
    "_ak_index_history": "akshare",
    "_ak_futures_history": "akshare",
    "_hl_quote": "hyperliquid",
    "_hl_history": "hyperliquid",
    "_binance_quote": "binance",
    "_binance_history": "binance",
    "_gate_quote": "gate",
    "_gate_history": "gate",
    "_ccement_history": "ccement",
    "_hsi_history": "hsi",
    "_cameco_history": "cameco",
}


def source_id(fn) -> str:
    """源函数 → 规范来源 id (与 source_order.CATALOG 同名)."""
    explicit = getattr(fn, "__source_id__", None)
    if explicit:
        return str(explicit)
    name = getattr(fn, "__name__", "")
    return _SOURCE_BY_NAME.get(name, name or "unknown")


def label(source: str) -> str:
    """来源 id → 展示名 (复用设置页的 SOURCE_META, 避免两处文案漂移)."""
    source = {"gate.io": "gate", "yahoo": "yfinance", "yf": "yfinance"}.get(
        source, source
    )
    try:
        from .source_order import SOURCE_META

        meta = SOURCE_META.get(source)
        if meta is not None:
            return meta.label
    except Exception:  # noqa: BLE001 - 展示层兜底
        pass
    return {
        "cfets": "CFETS 人民币中间价",
        "ccement": "水泥网指数",
        "hsi": "恒生指数官网",
        "cameco": "Cameco 铀价",
        "unknown": "未知来源",
    }.get(source, source)


# ---------------------------------------------------------------------------
