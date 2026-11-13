"""组合页纯展示 helper: 数值格式/选中行解析/无行情明细/配色/按代码饼图."""

from __future__ import annotations

import pandas as pd
import plotly.express as px

from tracker.analytics import by_symbol
from tracker.symbols import parse

from . import settings as S, theme

SYMBOL_TOP = 20
SYMBOL_LABEL_MIN_SHARE = 0.035


def fmt(v, digits: int = 2) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    return f"{v:,.{digits}f}"


def _norm_sym(sym: str) -> str:
    """代码 → Yahoo 规范形式; 无法识别时退回原始大写 (调用方随后报错)."""
    try:
        return parse(sym).yahoo
    except ValueError:
        return sym.strip().upper()


def _selected_symbols(event, frame: pd.DataFrame) -> list[str]:
    """从 st.dataframe 的选择事件取选中行代码 (按行位置映射回 frame)."""
    if event is None or frame.empty:
        return []
    try:
        rows = list(event.selection.rows)
    except (AttributeError, KeyError, TypeError):
        return []
    out: list[str] = []
    for i in rows:
        if not isinstance(i, int) or i < 0 or i >= len(frame):
            continue
        sym = frame.iloc[i].get("symbol")
        if sym:
            out.append(_norm_sym(str(sym)))
    return list(dict.fromkeys(out))


def _detail_with_unpriced(view: pd.DataFrame, holdings: list[dict]) -> pd.DataFrame:
    """持仓明细表: 在估值视图后补上无行情/无法估值的持仓."""
    priced = set(view["symbol"]) if not view.empty else set()
    extra: list[dict] = []
    for h in holdings:
        sym = _norm_sym(str(h.get("symbol", "")))
        if not sym or sym in priced:
            continue
        priced.add(sym)
        qty = h.get("quantity")
        try:
            qty = float(qty)
        except (TypeError, ValueError):
            qty = None
        cost = h.get("avg_cost")
        try:
            cost = float(cost) if cost not in (None, "") else None
        except (TypeError, ValueError):
            cost = None
        try:
            market = parse(sym).market_label
        except ValueError:
            market = ""
        extra.append(
            {
                "symbol": sym,
                "name": "",
                "market": market,
                "currency": "",
                "source": "行情缺失",
                "price": None,
                "change_pct": None,
                "quantity": qty,
                "avg_cost": cost,
                "market_value": None,
                "cost": None,
                "pnl": None,
                "pnl_pct": None,
                "weight": None,
                "weight_pct": None,
                "today_pnl": None,
            }
        )
    if not extra:
        return view
    return pd.concat([view, pd.DataFrame(extra)], ignore_index=True)


def _merged_issue_lines(errors: dict[str, str], issues: list[str]) -> list[str]:
    """合并取数错误与视图问题, 按完整文本去重."""
    return list(dict.fromkeys([f"{k}: {v}" for k, v in errors.items()] + issues))


def _pnl_color(v) -> str:
    """涨跌单元格字体色: 涨/跌按全局配色 (0/缺失不着色)。"""
    if v is None or (isinstance(v, float) and pd.isna(v)) or v == 0:
        return ""
    up_c, down_c = S.up_down_colors()
    return f"color: {up_c}" if v > 0 else f"color: {down_c}"


def _symbol_pie(view: pd.DataFrame):
    """「按代码」资产配置饼图: 市值前 SYMBOL_TOP 名逐个展示, 其余合并为「其他」."""
    sym_view = by_symbol(view, top=SYMBOL_TOP)
    fig = px.pie(
        sym_view,
        names="symbol",
        values="market_value",
        color_discrete_sequence=theme.CHART_COLORS,
        title=(
            f"按代码 (Top {SYMBOL_TOP} + 其他)"
            if view["symbol"].nunique() > SYMBOL_TOP
            else "按代码"
        ),
    )
    total_mv = float(sym_view["market_value"].sum())
    if total_mv:
        fig.update_traces(
            text=[
                f"{v / total_mv:.1%}" if v / total_mv >= SYMBOL_LABEL_MIN_SHARE else ""
                for v in sym_view["market_value"]
            ],
            textinfo="text",
        )
    theme.style_chart(fig)
    if len(sym_view) > 10:
        fig.update_layout(legend=dict(font=dict(size=10)))
    return fig
