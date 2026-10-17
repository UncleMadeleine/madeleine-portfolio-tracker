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
            # 与取数层口径统一 (symbol: 原因), 便于 take_snapshot 去重
            issues.append(f"{raw}: {e}")
            continue
        # Streamlit data_editor 清空单元格会产生 NaN; `NaN or 0` 仍得 NaN, 需显式判
        raw_qty = h.get("quantity")
        if raw_qty is None or (isinstance(raw_qty, float) and math.isnan(raw_qty)):
            raw_qty = 0
        try:
            qty = float(raw_qty or 0)
        except (TypeError, ValueError):
            # 手改 JSON 成非数字字符串 (Excel 粘贴 "N/A" 等): 记 issue 跳过该行,
            # 不让 ValueError 穿透 snapshot/export/UI 变裸 traceback
            issues.append(f"{p.yahoo}: 数量无法解析 ({raw_qty!r}), 跳过")
            continue
        cost = h.get("avg_cost")
        if cost is None or (isinstance(cost, float) and math.isnan(cost)) or cost == "":
            cost = None
        else:
            try:
                cost = float(cost)
            except (TypeError, ValueError):
                issues.append(f"{p.yahoo}: 成本无法解析 ({cost!r}), 按缺失处理")
                cost = None
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
        # 空头持仓 (IBKR 负数量) cost_base 为负: 收益率按成本绝对值算,
        # 否则盈亏比例符号翻转 (空头盈利显示成亏损)
        pnl_pct = pnl / abs(cost_base) if cost_base else None
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
                "source": getattr(q, "source", None),
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
        total_mv = df["market_value"].sum()
        if total_mv > 0:
            df["weight"] = df["market_value"] / total_mv
            df["weight_pct"] = df["weight"] * 100
        else:
            # 全部持仓市值为 0 (数量全 0 等): 权重无定义, 置 0 避免 NaN 扩散到 UI/JSON
            df["weight"] = 0.0
            df["weight_pct"] = 0.0
    return df, issues


def summarize(view: pd.DataFrame) -> dict:
    empty = pd.Series(dtype=float)
    if view.empty:
        return {
            "total_value": 0.0,
            "total_cost": None,
            "total_pnl": None,
            "total_pnl_pct": None,
            "cost_coverage": None,
            "today_pnl": None,
            "by_market": empty,
            "by_currency": empty,
        }
    total_value = float(view["market_value"].sum())
    cost_mask = view["cost"].notna() & view["pnl"].notna()
    covered_value = float(view.loc[cost_mask, "market_value"].sum())
    total_cost = float(view["cost"].sum()) if cost_mask.any() else None
    total_pnl = float(view["pnl"].sum()) if cost_mask.any() else None
    today = float(view["today_pnl"].sum()) if view["today_pnl"].notna().any() else None
    # 盈亏口径仅覆盖有成本数据的部分; 覆盖率供展示层标注
    cost_coverage = covered_value / total_value if total_value else None
    return {
        "total_value": total_value,
        "total_cost": total_cost,
        "total_pnl": total_pnl,
        "total_pnl_pct": (total_pnl / abs(total_cost)) if total_cost else None,
        "cost_coverage": cost_coverage,
        "today_pnl": today,
        "by_market": view.groupby("market")["market_value"]
        .sum()
        .sort_values(ascending=False),
        "by_currency": view.groupby("currency")["market_value"]
        .sum()
        .sort_values(ascending=False),
    }


def by_symbol(
    view: pd.DataFrame, top: int = 20, other_label: str = "其他"
) -> pd.DataFrame:
    """按股票代码聚合市值 (资产配置饼图用): 前 top 名, 其余合计为「其他」.

    与 summarize 的 by_market/by_currency (返回 Series) 不同: 代码维度动辄
    几十上百个, 全画进饼图不可读, 故只保留市值前 top 名, 尾部合并为一个
    「其他」切片 (恒在末位). 同一代码多行 (重复持仓) 先求和.
    返回列 symbol/market_value, 按市值降序, 可直接喂 px.pie.
    """
    if view.empty:
        return pd.DataFrame(columns=["symbol", "market_value"])
    s = view.groupby("symbol")["market_value"].sum().sort_values(ascending=False)
    if len(s) <= top:
        return s.reset_index()
    tail = s.iloc[top:].sum()
    out = pd.concat([s.head(top), pd.Series({other_label: tail})]).rename_axis("symbol")
    return out.reset_index(name="market_value")
