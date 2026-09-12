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
        return {"watchlist": []}
    with open(p, encoding="utf-8") as f:
        raw = json.load(f)
    if "watchlist" in raw:
        entries = []
        for e in raw["watchlist"]:
            e = dict(e)
            e["lists"] = parse_lists(e.get("lists"))
            entries.append(e)
        return {"watchlist": entries}
    if "watchlists" in raw:
        merged: dict[str, dict] = {}
        for name, lst in raw["watchlists"].items():
            for e in lst:
                sym = str(e.get("symbol", "")).strip()
                if not sym:
                    continue
                if sym not in merged:
                    merged[sym] = dict(e)
                    merged[sym].setdefault("lists", [])
                if name not in merged[sym]["lists"]:
                    merged[sym]["lists"].append(name)
        entries = list(merged.values())
        for e in entries:
            if not e["lists"]:
                e["lists"] = ["默认"]
        return {"watchlist": entries}
    return {"watchlist": []}


def save_watchlist(data: dict, path: str | Path = DEFAULT_WATCHLIST) -> None:
    Path(path).write_text(
        json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )


def list_names(data: dict) -> list[str]:
    names: list[str] = []
    for e in (data or {}).get("watchlist", []):
        for n in e.get("lists", []) or []:
            if n and n not in names:
                names.append(n)
    return names or ["默认"]


def merge_entries(data: dict) -> list[dict]:
    """全部条目 (扁平, 一个代码一条, 天然去重)."""
    return [
        e for e in (data or {}).get("watchlist", [])
        if str(e.get("symbol", "")).strip()
    ]


def entries_for(data: dict, name: str | None = None) -> list[dict]:
    """按所属列表过滤条目; name 为空返回全部."""
    if not name:
        return merge_entries(data)
    return [
        e for e in (data or {}).get("watchlist", [])
        if name in (e.get("lists", []) or [])
    ]


def parse_lists(v) -> list[str]:
    """把逗号分隔字符串/列表解析为归属列表; 空则回退默认."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return ["默认"]
    if isinstance(v, list):
        items = [str(x).strip() for x in v]
    else:
        items = [x.strip() for x in str(v).replace("，", ",").split(",")]
    return [x for x in items if x] or ["默认"]


def _num(v) -> float | None:
    if v in (None, ""):
        return None
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _note_str(v) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return ""
    return str(v)


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
                "note": _note_str(e.get("note")),
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
