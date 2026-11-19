"""fundamentals 子命令: 估值快照 / 历史 PE·PB / 最新财报原文链接.

面向 Agent 的机器可读输出与页面完全同源 (tracker.valuation / tracker.filings),
不复制任何取数或归一逻辑:
  - 快照: valuation.fetch_snapshot (内存 + 磁盘 6h)
  - 历史: valuation.fetch_history (百度股市通, A股/港股/美股)
  - 财报: filings.fetch_filings (官方披露库, 6h)
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime

import pandas as pd

from .. import filings, valuation
from ._common import _finish_with_error, _print_json, _records

_METRICS = {
    "pe": valuation.METRIC_PE,
    "pb": valuation.METRIC_PB,
}


def _fmt_ratio(value: float | None) -> str:
    return "—" if value is None else f"{float(value):.2f}"


def _fmt_pct(value: float | None) -> str:
    return "—" if value is None else f"{float(value):.2f}%"


def _fmt_price(value: float | None) -> str:
    if value is None:
        return "—"
    v = float(value)
    if v >= 100:
        return f"{v:,.2f}"
    if v >= 1:
        return f"{v:,.3f}"
    return f"{v:.4f}"


def _fmt_cap(value: float | None) -> str:
    if value is None:
        return "—"
    v = float(value)
    if abs(v) >= 1e12:
        return f"{v / 1e12:,.2f}万亿"
    if abs(v) >= 1e8:
        return f"{v / 1e8:,.2f}亿"
    if abs(v) >= 1e4:
        return f"{v / 1e4:,.2f}万"
    return f"{v:,.2f}"


def _fmt_ts(value: float) -> str:
    if not value:
        return "—"
    return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M")


def _snapshot_dict(snap: valuation.ValuationSnapshot) -> dict:
    """快照 dataclass → JSON 友好 dict (保留原始数值, 另给格式化时间)."""
    data = asdict(snap)
    data["fetched_at_iso"] = _fmt_ts(snap.fetched_at)
    return data


def _history_dict(
    df: pd.DataFrame, metric: str, months: int
) -> dict:
    """历史曲线 → {metric, label, months, summary, points}."""
    hist = df.copy()
    hist["date"] = pd.to_datetime(hist["date"]).dt.strftime("%Y-%m-%d")
    return {
        "metric": metric,
        "label": valuation.METRIC_LABELS.get(metric, metric),
        "months": int(months),
        "summary": valuation.summarize_history(df),
        "points": _records(hist),
    }


def _print_snapshot(row: dict) -> None:
    snap = row.get("snapshot")
    if snap is None:
        print(f"⚠ {row.get('symbol')}: {row.get('error') or '无估值数据'}")
        return
    print(
        f"\n=== 基本面 {snap['symbol']} "
        f"({snap['market_label']} · {snap['currency']}) ==="
    )
    pe_dyn = (
        f" · PE(动态) {_fmt_ratio(snap['pe_dynamic'])}"
        if snap.get("pe_dynamic") is not None
        else ""
    )
    print(
        f"  PE(TTM) {_fmt_ratio(snap['pe_ttm'])}"
        f"{pe_dyn}"
        f" · PB {_fmt_ratio(snap['pb'])}"
        f" · 股息率 {_fmt_pct(snap['dividend_yield_pct'])}"
        f" · 5 年平均股息率 {_fmt_pct(snap['dividend_yield_5y_avg_pct'])}"
    )
    print(
        f"  Forward PE {_fmt_ratio(snap['forward_pe'])}"
        f" · EPS(TTM) {_fmt_price(snap['eps_ttm'])}"
        f" · BVPS {_fmt_price(snap['book_value'])}"
        f" · 派息率 {_fmt_pct(snap['payout_ratio_pct'])}"
    )
    print(
        f"  总市值 {_fmt_cap(snap['market_cap'])} {snap['currency']}"
        f" · 数据源 {snap['source']}"
        f" · 快照 {snap.get('fetched_at_iso') or _fmt_ts(snap['fetched_at'])}"
    )


def _print_history(hist: dict) -> None:
    summary = hist.get("summary") or {}
    if not summary:
        return
    print(
        f"  历史 {hist['label']} (近 {hist['months']} 个月):"
        f" 最新 {_fmt_ratio(summary['latest'])}"
        f" · 均值 {_fmt_ratio(summary['mean'])}"
        f" · 区间 {_fmt_ratio(summary['low'])} ~ {_fmt_ratio(summary['high'])}"
        f" · 分位 {summary['percentile']:.0f}%"
    )
    print(
        f"    ({summary['first_date']} ~ {summary['last_date']},"
        f" {summary['bars']} 个交易日)"
    )


def cmd_fundamentals(args) -> None:
    """查询估值快照 / 历史 PE·PB / 最新财报链接, 支持 --json."""
    symbols = list(args.symbols)
    metric = _METRICS.get(args.history) if args.history else None
    results: list[dict] = []

    for raw in symbols:
        row: dict = {"symbol": raw}
        try:
            # normalize_stock_input 负责简写补后缀、拒绝 crypto/index,
            # 并返回规范 Yahoo 代码 (与估值页同一入口)。
            symbol = valuation.normalize_stock_input(raw)
            row["symbol"] = symbol
            row["snapshot"] = _snapshot_dict(
                valuation.fetch_snapshot(symbol, refresh=args.refresh)
            )
        except Exception as e:  # noqa: BLE001 - 单票失败不阻断其它代码
            row["error"] = str(e)
            results.append(row)
            continue

        if args.filings:
            try:
                row["filings"] = [
                    asdict(item)
                    for item in filings.fetch_filings(
                        symbol, refresh=args.refresh
                    )
                ]
            except Exception as e:  # noqa: BLE001 - 财报链接失败不影响快照
                row["filings_error"] = str(e)

        if metric is not None:
            try:
                df = valuation.fetch_history(
                    symbol,
                    metric=metric,
                    months=args.months,
                    refresh=args.refresh,
                )
                row["history"] = _history_dict(df, metric, args.months)
            except Exception as e:  # noqa: BLE001 - 历史失败不影响快照
                row["history_error"] = str(e)

        results.append(row)

    if args.json:
        _print_json({"results": results})
    else:
        for row in results:
            _print_snapshot(row)
            for item in row.get("filings") or []:
                print(
                    f"  · {item['title']}"
                    f" · {item['kind']} · {item['date']} · {item['source']}"
                )
                print(f"    {item['url']}")
            if row.get("filings_error"):
                print(f"  ⚠ 财报链接: {row['filings_error']}")
            if row.get("history"):
                _print_history(row["history"])
            if row.get("history_error"):
                print(f"  ⚠ 历史曲线: {row['history_error']}")

    if results and all("snapshot" not in row for row in results):
        _finish_with_error("没有可取的基本面数据")
