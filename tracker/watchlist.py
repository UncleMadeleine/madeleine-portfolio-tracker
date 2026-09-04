"""自选股观察与价格阈值提醒."""
from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd

from .prices import Quote
from .symbols import parse

DEFAULT_WATCHLIST = Path(__file__).resolve().parent.parent / "watchlist.json"

STATUS_UPPER = "🔴 高于上限"
STATUS_LOWER = "🟢 低于下限"
STATUS_WITHIN = "⚪ 区间内"


def load_watchlist(path: str | Path = DEFAULT_WATCHLIST) -> dict:
    p = Path(path)
    if not p.exists():
        return {"watchlist": []}
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def save_watchlist(data: dict, path: str | Path = DEFAULT_WATCHLIST) -> None:
    Path(path).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _num(v) -> float | None:
    if v in (None, ""):
        return None
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def build_watchlist_view(
    entries, quotes: dict[str, Quote]
) -> tuple[pd.DataFrame, list[str]]:
    rows: list[dict] = []
    issues: list[str] = []
    for e in entries:
        raw = str(e.get("symbol", "")).strip()
        if not raw:
            continue
        try:
            p = parse(raw)
        except ValueError as err:
            issues.append(str(err))
            continue
        q = quotes.get(p.yahoo)
        if q is None or q.price is None or not math.isfinite(q.price):
            issues.append(f"{p.yahoo}: 行情缺失")
            continue
        price = q.price
        upper = _num(e.get("upper"))
        lower = _num(e.get("lower"))
        if upper is not None and price >= upper:
            status = STATUS_UPPER
        elif lower is not None and price <= lower:
            status = STATUS_LOWER
        else:
            status = STATUS_WITHIN
        dist_upper = (
            (upper / price - 1) * 100 if (upper is not None and price > 0) else None
        )
        dist_lower = (
            (price / lower - 1) * 100 if (lower is not None and lower > 0) else None
        )
        rows.append(
            {
                "symbol": p.yahoo,
                "name": q.name or "",
                "market": p.market_label,
                "currency": q.currency,
                "price": price,
                "change_pct": q.change_pct,
                "upper": upper,
                "lower": lower,
                "status": status,
                "dist_upper_pct": dist_upper,
                "dist_lower_pct": dist_lower,
                "note": str(e.get("note") or ""),
            }
        )
    df = pd.DataFrame(rows)
    if not df.empty:
        df["triggered"] = df["status"] != STATUS_WITHIN
        df = df.sort_values(
            ["triggered", "symbol"], ascending=[False, True]
        ).reset_index(drop=True)
    return df, issues


def triggered_entries(view: pd.DataFrame) -> pd.DataFrame:
    if view.empty or "triggered" not in view.columns:
        return view.iloc[0:0]
    return view[view["triggered"]]
