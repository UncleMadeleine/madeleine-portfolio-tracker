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


def positive_float(value: object) -> float | None:
    """可选数值归一: 仅保留 >0 的有限值 (价格/市值/汇率口径)."""
    f = finite_float(value)
    return f if f is not None and f > 0 else None


def non_negative_float(value: object) -> float | None:
    """可选数值归一: 仅保留 >=0 的有限值 (比率/成本口径)."""
    f = finite_float(value)
    return f if f is not None and f >= 0 else None


def first_column(df, *names: str):
    """按候选列名找列 (数据源列名偶有空格/别名漂移), 找不到返回 None."""
    by_name = {str(c).strip(): c for c in df.columns}
    for name in names:
        if name in by_name:
            return by_name[name]
    return None


def _strip_tz(value):
    """单个日期值剥时区 (非 Timestamp 原样返回, 由 to_datetime 解析)."""
    import pandas as pd

    if isinstance(value, pd.Timestamp) and value.tzinfo is not None:
        return value.tz_localize(None)
    return value


def normalize_dates(values):
    """日期列 → naive datetime; 兼容字符串/aware/naive 混合输入.

    直接对混合 aware/naive 的列调用 ``pd.to_datetime`` 会把 aware 项静默
    转成 NaT, 丢失整段日期; 逐元素剥时区后再解析更稳。
    """
    import pandas as pd

    if isinstance(values, (pd.Series, pd.Index)):
        clean = values.map(_strip_tz)
    else:
        clean = pd.Series(values).map(_strip_tz)
    return pd.to_datetime(clean, errors="coerce")


def canonical_or_upper(raw: object) -> str:
    """代码 → Yahoo 规范形式; 无法识别时退回去空白大写 (调用方随后报错)."""
    from .symbols import canonical

    text = str(raw or "").strip()
    return canonical(text) or text.upper()


def format_price(value: object) -> str:
    """价格精度随量级: >=100 两位, >=1 三位, 其余四位; 缺失显示破折号."""
    v = finite_float(value)
    if v is None:
        return "—"
    if v >= 100:
        return f"{v:,.2f}"
    if v >= 1:
        return f"{v:,.3f}"
    return f"{v:.4f}"


def format_ratio(value: object) -> str:
    """PE/PB 等倍数: 缺失显示破折号 (不把 None 显示成 0)."""
    v = finite_float(value)
    return "—" if v is None else f"{v:.2f}"


def format_pct(value: object) -> str:
    """百分数 (输入已是 0-100 口径): 缺失显示破折号."""
    v = finite_float(value)
    return "—" if v is None else f"{v:.2f}%"


def as_bool(value: object, default: bool = False) -> bool:
    """宽松布尔归一: 手改 JSON 的 "false"/0/"" 不能被 bool() 当 True.

    storage / webdav / eodhd 读取配置时共用同一口径, 避免各写一份后
    "0"/"off"/空串的处理逐渐漂移。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("true", "1", "yes", "on"):
            return True
        if s in ("false", "0", "no", "off", ""):
            return False
    return default


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
