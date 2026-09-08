"""组合视图与指标计算 (纯函数, 便于离线测试)."""
from __future__ import annotations

import math

import pandas as pd

from .prices import Quote
from .symbols import parse


def build_view(
    holdings, quotes: dict[str, Quote], fx: dict[str, float]
) -> tuple[pd.DataFrame, list[str]]:
    rows: list[dict] = []
    issues: list[str] = []
    for h in holdings:
        raw = str(h.get("symbol", "")).strip()
        if not raw:
            continue
        try:
            p = parse(raw)
        except ValueError as e:
            issues.append(str(e))
            continue
        qty = float(h.get("quantity") or 0)
        cost = h.get("avg_cost")
        cost = float(cost) if cost not in (None, "") else None
        q = quotes.get(p.yahoo)
        if q is None or q.price is None or not math.isfinite(q.price):
            issues.append(f"{p.yahoo}: 行情缺失")
            continue
        rate = fx.get(q.currency)
        if rate is None:
            issues.append(f"{p.yahoo}: 缺少 {q.currency} 汇率")
            continue
        mv_local = qty * q.price
        mv = mv_local * rate
        cost_base = qty * cost * rate if cost is not None else None
        pnl = mv - cost_base if cost_base is not None else None
        pnl_pct = pnl / cost_base if cost_base else None
        today = None
        if q.change_pct is not None and q.change_pct > -100:
            today = mv - mv / (1 + q.change_pct / 100)
        rows.append(
            {
                "symbol": p.yahoo,
                "name": q.name or "",
                "market": p.market_label,
                "currency": q.currency,
                "price": q.price,
                "change_pct": q.change_pct,
                "quantity": qty,
                "avg_cost": cost,
                "market_value": mv,
                "cost": cost_base,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "today_pnl": today,
            }
        )
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("market_value", ascending=False).reset_index(drop=True)
        df["weight"] = df["market_value"] / df["market_value"].sum()
        df["weight_pct"] = df["weight"] * 100
    return df, issues


def summarize(view: pd.DataFrame) -> dict:
    empty = pd.Series(dtype=float)
    if view.empty:
        return {
            "total_value": 0.0,
            "total_cost": None,
            "total_pnl": None,
            "total_pnl_pct": None,
            "today_pnl": None,
            "by_market": empty,
            "by_currency": empty,
        }
    total_value = float(view["market_value"].sum())
    total_cost = (
        float(view["cost"].sum()) if view["cost"].notna().any() else None
    )
    total_pnl = float(view["pnl"].sum()) if view["pnl"].notna().any() else None
    today = float(view["today_pnl"].sum()) if view["today_pnl"].notna().any() else None
    return {
        "total_value": total_value,
        "total_cost": total_cost,
        "total_pnl": total_pnl,
        "total_pnl_pct": (total_pnl / total_cost) if total_cost else None,
        "today_pnl": today,
        "by_market": view.groupby("market")["market_value"].sum().sort_values(ascending=False),
        "by_currency": view.groupby("currency")["market_value"].sum().sort_values(ascending=False),
    }
