"""eodhd 子命令: BYOK 配置状态与连通性自检 (不写持仓/不碰行情缓存)."""

from __future__ import annotations

from .. import eodhd as eod
from ._common import _finish_with_error, _print_json


def _print_status(info: dict) -> None:
    print(f"配置文件: {info['config_path']}")
    print(f"可用: {'是' if info['available'] else '否'}")
    if not info["available"]:
        print(f"原因: {info['reason']}")
    print(f"enabled: {info['enabled']}")
    print(
        f"token: {info['token_masked'] or '未配置'}"
        f" (来源: {info['token_source'] or '—'}; env: {info['token_env']})"
    )
    print(f"历史K线优先级: {info['priority']}")
    print(f"实时行情模式: {info['quotes']}")
    print(f"基本面 (PB/市值): {info['fundamentals']}")
    print(f"复权口径: {info['adjustment']}")
    print(
        f"LSE 便士折算: {'开' if info['lse_pence'] else '关'}"
        + (
            f" (豁免: {', '.join(info['lse_gbp_symbols'])})"
            if info["lse_gbp_symbols"]
            else ""
        )
    )


def _print_probe(result: dict) -> None:
    print(f"[{result['symbol']}]")
    if not result["available"]:
        print(f"  不可用: {result['reason']}")
        return
    q = result.get("quote")
    if q:
        print(
            f"  行情: {q['price']} {q['currency']}"
            f" (昨收 {q['prev_close']}, {q['change_pct']}%)"
        )
    h = result.get("history")
    if h:
        print(
            f"  历史: {h['rows']} 行 ({h['first']} ~ {h['last']}),"
            f" 最新收盘 {h['last_close']}"
        )
    f = result.get("fundamentals")
    if f:
        print(
            f"  基本面: BVPS {f['book_value']} / 市值 {f['market_cap']} {f['currency'] or ''}"
        )
    for err in result.get("errors") or []:
        print(f"  ❌ {err}")


def cmd_eodhd(args) -> None:
    """eodhd status | eodhd test SYMBOL..."""
    action = args.action
    if action == "status":
        info = eod.status(getattr(args, "config", None))
        if getattr(args, "json", False):
            _print_json(info)
            return
        _print_status(info)
        return

    if action == "test":
        symbols = list(getattr(args, "symbols", None) or [])
        if not symbols:
            _finish_with_error(
                "eodhd test 需要至少一个代码, 如: tracker eodhd test AAPL"
            )
        results = [eod.probe(s, getattr(args, "config", None)) for s in symbols]
        if getattr(args, "json", False):
            _print_json({"ok": True, "results": results})
        else:
            for r in results:
                _print_probe(r)
        if all(r.get("errors") for r in results):
            raise SystemExit(2)
        return

    _finish_with_error(f"未知 eodhd 动作: {action}")
