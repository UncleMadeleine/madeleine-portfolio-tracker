"""自选股观察与价格阈值提醒 (支持两级阈值)."""
from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd

from .prices import Quote
from .symbols import parse

DEFAULT_WATCHLIST = Path(__file__).resolve().parent.parent / "watchlist.json"

STATUS_UPPER_1 = "🟠 突破上限 I"
STATUS_UPPER_2 = "🔴 突破上限 II"
STATUS_LOWER_1 = "🟡 跌破下限 I"
STATUS_LOWER_2 = "🟢 跌破下限 II"
STATUS_WITHIN = "⚪ 区间内"

_STATUS_RANK = {
    STATUS_UPPER_2: 1,
    STATUS_UPPER_1: 2,
    STATUS_LOWER_1: 3,
    STATUS_LOWER_2: 4,
    STATUS_WITHIN: 5,
}


def load_watchlist(path: str | Path = DEFAULT_WATCHLIST) -> dict:
    p = Path(path)
    if not p.exists():
        return {"watchlists": {"默认": []}}
    with open(p, encoding="utf-8") as f:
        raw = json.load(f)
    if "watchlists" in raw:
        return raw
    if "watchlist" in raw:
        return {"watchlists": {"默认": raw.get("watchlist", [])}}
    return {"watchlists": {"默认": []}}


def save_watchlist(data: dict, path: str | Path = DEFAULT_WATCHLIST) -> None:
    Path(path).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def list_names(data: dict) -> list[str]:
    return list((data or {}).get("watchlists", {}).keys())


def merge_entries(data: dict) -> list[dict]:
    """合并所有子列表条目 (按 symbol 去重, 保留首次出现)."""
    seen: dict[str, dict] = {}
    for name in list_names(data):
        for e in (data.get("watchlists") or {}).get(name, []):
            sym = str(e.get("symbol", "")).strip()
            if sym and sym not in seen:
                seen[sym] = e
    return list(seen.values())


def entries_for(data: dict, name: str | None = None) -> list[dict]:
    """取某子列表条目; name 为空或不存在时返回合并的全量."""
    wl = (data or {}).get("watchlists", {})
    if name and name in wl:
        return wl[name]
    return merge_entries(data)


def _num(v) -> float | None:
    if v in (None, ""):
        return None
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _normalize_entry(e: dict) -> dict:
    out = dict(e)
    for old, new in (("upper", "upper_1"), ("lower", "lower_1")):
        if old in out and new not in out:
            out[new] = out.pop(old)
    return out


def build_watchlist_view(
    entries, quotes: dict[str, Quote]
) -> tuple[pd.DataFrame, list[str]]:
    rows: list[dict] = []
    issues: list[str] = []
    for e in entries:
        e = _normalize_entry(e)
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
        upper_1 = _num(e.get("upper_1"))
        upper_2 = _num(e.get("upper_2"))
        lower_1 = _num(e.get("lower_1"))
        lower_2 = _num(e.get("lower_2"))
        if upper_2 is not None and price >= upper_2:
            status = STATUS_UPPER_2
        elif upper_1 is not None and price >= upper_1:
            status = STATUS_UPPER_1
        elif lower_2 is not None and price <= lower_2:
            status = STATUS_LOWER_2
        elif lower_1 is not None and price <= lower_1:
            status = STATUS_LOWER_1
        else:
            status = STATUS_WITHIN
        dist: dict[str, float | None] = {}
        if upper_1 is not None and price > 0:
            dist["dist_upper_1_pct"] = (upper_1 / price - 1) * 100
        if upper_2 is not None and price > 0:
            dist["dist_upper_2_pct"] = (upper_2 / price - 1) * 100
        if lower_1 is not None and lower_1 > 0:
            dist["dist_lower_1_pct"] = (price / lower_1 - 1) * 100
        if lower_2 is not None and lower_2 > 0:
            dist["dist_lower_2_pct"] = (price / lower_2 - 1) * 100
        rows.append(
            {
                "symbol": p.yahoo,
                "name": q.name or "",
                "market": p.market_label,
                "currency": q.currency,
                "price": price,
                "change_pct": q.change_pct,
                "upper_1": upper_1,
                "upper_2": upper_2,
                "lower_1": lower_1,
                "lower_2": lower_2,
                "status": status,
                "note": str(e.get("note") or ""),
                **dist,
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


def sort_watchlist(view: pd.DataFrame, mode: str = "default") -> pd.DataFrame:
    if view.empty:
        return view
    if mode == "severity":
        ranked = view.copy()
        ranked["status_rank"] = ranked["status"].map(_STATUS_RANK)
        return ranked.sort_values(["status_rank", "symbol"], ascending=[True, True]).drop(columns=["status_rank"])
    if mode == "change_desc":
        return view.sort_values(["change_pct", "symbol"], ascending=[False, True])
    if mode == "change_asc":
        return view.sort_values(["change_pct", "symbol"], ascending=[True, True])
    return view.sort_values(["triggered", "symbol"], ascending=[False, True])
