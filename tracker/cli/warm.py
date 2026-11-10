"""warm 子命令: 主动预热/保温行情、汇率与财经日历缓存.

CLI 每次执行只按需回源; 需要常驻保温时用本子命令 (不依赖 Streamlit):

  tracker warm                         # 预热一次 (持仓+自选)
  tracker warm --symbols AAPL TSLA     # 额外代码也预热
  tracker warm --interval 300          # 常驻, 启动预热一次后每 5 分钟检查
"""

from __future__ import annotations

import json
import time
from datetime import datetime

from ._common import _finish_with_error, _print_json


def _summary_text(result: dict) -> str:
    """人类可读的预热摘要."""
    lines = ["=== 缓存预热 ==="]
    q = result.get("quotes")
    if q is None:
        lines.append("行情: 跳过")
    elif q.get("skipped"):
        lines.append("行情: 无持仓/自选代码, 跳过")
    else:
        symbols = q.get("symbols") or []
        lines.append(f"行情: {q.get('ok', 0)}/{len(symbols)} 只成功")
        errors = q.get("errors") or {}
        if errors:
            lines.append(f"  失败 {len(errors)} 只:")
            for sym, msg in list(errors.items())[:5]:
                lines.append(f"    {sym}: {msg}")
            if len(errors) > 5:
                lines.append(f"    … 另有 {len(errors) - 5} 只")
    fx = result.get("fx")
    if fx:
        curs = ", ".join(fx.get("currencies") or []) or "—"
        lines.append(
            f"汇率: {fx.get('base', '')} → {curs} ({fx.get('ok', 0)} 组)"
        )
        missing = fx.get("missing") or []
        if missing:
            lines.append(f"  缺失: {', '.join(missing)}")
    elif q is not None and not q.get("skipped"):
        lines.append("汇率: 无需更新")
    cal = result.get("calendar")
    if cal is None:
        lines.append("日历: 跳过")
    else:
        lines.append(
            f"日历: {cal.get('months', 0)} 个月窗口, "
            f"{cal.get('events', 0)} 个事件"
        )
        issues = cal.get("issues") or []
        if issues:
            lines.append(f"  注记: {'; '.join(issues[:3])}")
    lines.append(f"耗时: {float(result.get('elapsed_seconds') or 0):.2f}s")
    return "\n".join(lines)


def _warm_kwargs(args) -> dict:
    include_quotes = not getattr(args, "calendar_only", False)
    include_calendar = not getattr(args, "quotes_only", False)
    return {
        "include_quotes": include_quotes,
        "include_calendar": include_calendar,
        "extra_symbols": tuple(getattr(args, "symbols", None) or ()),
        "refresh": bool(getattr(args, "refresh", False)),
    }


def _run_resident(args, kwargs: dict) -> None:
    """常驻保温: 启动先预热一次, 之后每 interval 秒检查一次."""
    from ..services import warm

    interval = int(args.interval)
    while True:
        at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            result = warm.warm_once(**kwargs)
        except Exception as e:  # noqa: BLE001 - 常驻进程不因单轮失败退出
            result = {"error": f"{type(e).__name__}: {e}"}
            if args.json:
                print(
                    json.dumps(
                        {"at": at, **result}, ensure_ascii=False, default=str
                    ),
                    flush=True,
                )
            else:
                print(f"[{at}] ⚠ 预热失败: {result['error']}", flush=True)
        else:
            if args.json:
                print(
                    json.dumps(
                        {"at": at, **result}, ensure_ascii=False, default=str
                    ),
                    flush=True,
                )
            else:
                print(f"[{at}]", flush=True)
                print(_summary_text(result), flush=True)
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            break


def cmd_warm(args) -> None:
    """预热/保温行情、汇率与财经日历缓存."""
    from ..services import warm

    kwargs = _warm_kwargs(args)
    if getattr(args, "interval", None):
        try:
            _run_resident(args, kwargs)
        except KeyboardInterrupt:
            if not args.json:
                print("\n已停止保温。")
        return

    try:
        result = warm.warm_once(**kwargs)
    except Exception as e:  # noqa: BLE001 - 终端给可读错误, 不裸 traceback
        _finish_with_error(f"缓存预热失败: {e}")
    if args.json:
        _print_json(result)
    else:
        print(_summary_text(result))
