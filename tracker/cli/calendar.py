"""calendar 子命令: 组合/自选的财报与重大事件日历 (默认本月+下月)."""

from __future__ import annotations

import sys
from datetime import date

from .. import events as events_mod
from ..ics import build_ics
from ..services.watchlist import entries_for
from ..storage import load_portfolio
from ..symbols import parse
from ..watchlist import load_watchlist
from ._common import (  # noqa: F401 - months_arg 兼容旧导入路径
    _finish_with_error,
    _print_json,
    atomic_write_text,
    months_arg,
)

_WEEKDAY_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _scope_symbols(args) -> tuple[list[str], dict[str, str]]:
    """确定日历范围代码与名称映射.

    指定 positions symbols → 直接用 (解析失败原样大写兜底);
    否则 = 持仓 + 自选 (可 --watchlist 过滤), 名称映射取 watchlist 的
    name 字段 (组合持仓 schema 无名称)。
    """
    if args.symbols:
        syms: list[str] = []
        for s in args.symbols:
            try:
                syms.append(parse(s).yahoo)
            except ValueError:
                syms.append(str(s).strip().upper())
        return syms, {}
    try:
        portfolio = load_portfolio(args.portfolio)
    except Exception:  # noqa: BLE001 - 主持仓读不到日历照常 (自选域照旧)
        portfolio = {}
    try:
        wdata = load_watchlist(args.watchlist_file)
    except Exception:  # noqa: BLE001
        wdata = {}
    holdings = [h for h in portfolio.get("holdings", []) if h.get("symbol")]
    watch_name = args.watchlist.strip() if args.watchlist else None
    wentries = entries_for(wdata or {}, watch_name)
    watched = [w for w in wentries if w.get("symbol")]
    scope = [*holdings, *watched]
    if not scope:
        _finish_with_error(
            "组合与自选均无代码或无匹配列表, 请先 portfolio add / watchlist add"
        )
    syms = []
    names: dict[str, str] = {}
    for e in scope:
        raw = str(e["symbol"]).strip()
        try:
            yahoo = parse(raw).yahoo
        except ValueError:
            continue
        syms.append(yahoo)
        nm = str(e.get("name") or "").strip()
        if nm and yahoo not in names:
            names[yahoo] = nm
    return list(dict.fromkeys(syms)), names


def _progress_printer():
    """单票取数进度回调 (仅未命中缓存的代码, --json 不挂)."""

    def cb(done, total, symbol):
        if symbol:
            print(f"ℹ 日历取数 {done}/{total}: {symbol}", flush=True)

    return cb


def _write_stdout(text: str) -> None:
    """原样输出 UTF-8 文本 (ics 已含 CRLF, 避免 Windows 文本模式二次转换)."""
    buf = getattr(sys.stdout, "buffer", None)
    if buf is not None:
        buf.write(text.encode("utf-8"))
        buf.flush()
    else:  # 测试/被替换的 stdout 没有 buffer 时走文本接口
        sys.stdout.write(text)


def cmd_calendar(args) -> None:
    """组合/自选财报与重大事件日历; --json 输出机器可读结果, --ics 导出日历."""
    ics = bool(getattr(args, "ics", False))
    output = getattr(args, "output", None)
    if ics and args.json:
        _finish_with_error("--ics 与 --json 不能同时使用")
    if output and not ics:
        _finish_with_error("--output 需与 --ics 一起使用")

    symbols, names = _scope_symbols(args)
    progress = None if (args.json or ics) else _progress_printer()
    result = events_mod.fetch_calendar(
        symbols,
        months=args.months,
        refresh=args.refresh,
        use_longport=bool(args.longport),
        names=names,
        progress=progress,
    )

    if ics:
        text = build_ics(result["events"])
        if output:
            try:
                atomic_write_text(output, text)
            except OSError as e:
                _finish_with_error(f"日历导出失败: {output} ({e})")
            print(
                f"✅ 日历已导出: {output} · {len(result['events'])} 个事件"
                f" (窗口 {result['window']['start']} → {result['window']['end']})"
            )
        else:
            _write_stdout(text)
        return

    if args.json:
        _print_json(result)
        return

    w0 = date.fromisoformat(result["window"]["start"])
    w1 = date.fromisoformat(result["window"]["end"])
    evs = result["events"]
    print(f"\n=== 财经日历 · {w0} → {w1} · {len(evs)} 个事件 ===")
    if result["issues"]:
        for it in result["issues"]:
            print(f"⚠ {it}")
    if not evs:
        print("窗口内无财报/分红事件。")
        return
    cur_day = None
    for ev in evs:
        d = ev["date"] if isinstance(ev["date"], date) else date.fromisoformat(str(ev["date"])[:10])
        ds = d.isoformat()
        if ds != cur_day:
            cur_day = ds
            print(f"\n{ds} ({_WEEKDAY_CN[d.weekday()]})")
        typ = events_mod.EVENT_TYPE_LABELS.get(ev.get("type"), ev.get("type") or "?")
        status = events_mod.DATE_STATUS_LABELS.get(
            ev.get("date_status"), ev.get("date_status") or ""
        )
        bits = [f"  {ev['symbol']} · {typ}"]
        if status:
            bits.append(f"[{status}]")
        nm = ev.get("name")
        if nm:
            bits.append(nm)
        note = ev.get("note")
        if note:
            bits.append(note)
        src = ev.get("source")
        if src:
            bits.append(f"({src})")
        print("  ".join(bits))
    src_bits = [f"{k}×{v}" for k, v in result["sources"].items() if v]
    print(f"\n来源: {' · '.join(src_bits) if src_bits else '—'}")
