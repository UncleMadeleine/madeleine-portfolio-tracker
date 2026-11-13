"""K线页纯 helper: 代码归一化/对比选择解析/区间涨跌摘要."""

from __future__ import annotations

import streamlit as st

from tracker import charting
from tracker.symbols import parse, type_for_symbol

from . import settings


def normalize_or_none(s: str) -> str | None:
    try:
        return parse(s).yahoo
    except ValueError:
        return None


def _normalize_selection(sel_raw, *, crypto_only: bool = False):
    """对比代码输入归一化/去重; 返回 (合法代码, 非法输入)."""
    sel, bad = [], []
    for s in sel_raw:
        y = normalize_or_none(s)
        if y is None:
            bad.append(s)
        elif crypto_only and type_for_symbol(y) != "crypto":
            bad.append(f"{s} (非加密货币)")
        else:
            sel.append(y)
    return list(dict.fromkeys(sel)), bad


def _render_period_changes(frames: dict, period: str) -> None:
    """区间涨跌摘要 (价格对比/加密货币对比共用)."""
    chg = []
    for sym, d in frames.items():
        dd = charting.resample_ohlc(d, period) if period != "daily" else d
        if len(dd) >= 2:
            pct = float(dd["close"].iloc[-1]) / float(dd["close"].iloc[0]) - 1
            up_tag, down_tag = settings.up_down_tags()
            chg.append(
                f"{sym} :{up_tag}[{pct:+.2%}]"
                if pct >= 0
                else f"{sym} :{down_tag}[{pct:+.2%}]"
            )
    if chg:
        st.markdown("区间涨跌: " + " · ".join(chg))
