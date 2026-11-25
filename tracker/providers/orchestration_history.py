"""单代码历史K线编排: 显式 source_order (含 IBKR/长桥) 或默认源链."""

from __future__ import annotations

import pandas as pd

from .. import health
from ..symbols import Market, ParsedSymbol, parse as _parse_symbol
from .orchestration import _route_provider


def _fetch_ordered_history(
    p: ParsedSymbol,
    order: list[str],
    *,
    months: int,
    start_date: str,
    end_date: str,
    use_ibkr: bool,
    use_longport: bool,
    prefer_akshare: bool,
) -> pd.DataFrame:
    """严格按 source_order 顺序取单代码历史K线 (IBKR/长桥也参与排序)."""
    import warnings

    from .. import eodhd as eodhd_mod
    from .. import source_order as so
    from .stock_akshare import _akshare_history
    from .stock_yahoo import _yahoo_history

    if p.type == "global" and prefer_akshare:
        order = so.prefer_akshare_order(order)

    last_err: Exception | None = None
    for source_id in order:
        if source_id == "ibkr" and (not use_ibkr or p.type != "global"):
            continue
        if source_id == "longport" and not use_longport:
            continue
        if source_id == "eodhd":
            cfg = eodhd_mod.load_config()
            if not eodhd_mod.available(cfg):
                continue
        if source_id == "akshare_em" and p.market not in (
            Market.HK,
            Market.CN,
            Market.BJ,
        ):
            continue
        try:
            with health.span("history", source_id, p.yahoo) as sp:
                if source_id == "ibkr":
                    from .. import ibkr as ibkr_mod

                    hist, reason = ibkr_mod.get_history_ibkr(
                        [p], months, start_date=start_date, end_date=end_date
                    )
                    if p.yahoo in hist and not hist[p.yahoo].empty:
                        return hist[p.yahoo]
                    sp.ok = False
                    sp.error = reason or "无历史数据"
                    if reason:
                        warnings.warn(
                            f"IBKR 历史数据不可用: {reason} (已按顺序尝试下一源)"
                        )
                elif source_id == "longport":
                    from .. import longport as lp_mod

                    df = lp_mod.get_history_longport(p, start_date, end_date)
                    if df is not None and not df.empty:
                        return df
                    sp.ok = False
                    sp.error = "无历史数据"
                elif source_id == "eodhd":
                    df = eodhd_mod.history(p, start_date, end_date)
                    if df is not None and not df.empty:
                        return df
                    sp.ok = False
                    sp.error = "无历史数据"
                elif source_id == "yfinance":
                    df = _yahoo_history(p, start_date, end_date)
                    if df is not None and not df.empty:
                        return df
                    sp.ok = False
                    sp.error = "无历史数据"
                elif source_id == "akshare_em":
                    df = _akshare_history(p, start_date, end_date)
                    if df is not None and not df.empty:
                        return df
                    sp.ok = False
                    sp.error = "无历史数据"
        except Exception as e:  # noqa: BLE001 - 降级到下一源
            last_err = e
            if source_id == "ibkr":
                warnings.warn(f"IBKR 历史数据异常: {e}")
            elif source_id == "longport":
                warnings.warn(f"长桥历史数据异常: {e}")
    raise RuntimeError(
        f"{p.yahoo}: 历史数据获取失败 "
        f"({str(last_err).strip() if last_err else '所有数据源均未返回数据'})"
    )


def get_history(
    symbol: str,
    months: int = 12,
    start_date: str | None = None,
    end_date: str | None = None,
    prefer_akshare: bool = False,
    use_ibkr: bool = False,
    use_longport: bool = False,
    crypto_source: str = "auto",
) -> pd.DataFrame:
    """单代码历史K线: 显式 source_order (含 IBKR/长桥) 或原默认链."""
    from datetime import date, timedelta

    from .. import source_order as so

    p = _parse_symbol(symbol)
    if start_date is None:
        start_date = (date.today() - timedelta(days=months * 31)).isoformat()
    if end_date is None:
        end_date = date.today().isoformat()
    if p.type in ("global", "cn"):
        order = so.configured(p.type, "history")
        if order is not None:
            return _fetch_ordered_history(
                p,
                order,
                months=months,
                start_date=start_date,
                end_date=end_date,
                use_ibkr=use_ibkr,
                use_longport=use_longport,
                prefer_akshare=prefer_akshare,
            )
    if use_ibkr:
        try:
            from .. import ibkr as ibkr_mod

            with health.span("history", "ibkr", p.yahoo) as sp:
                ib_hist, reason = ibkr_mod.get_history_ibkr(
                    [p], months, start_date=start_date, end_date=end_date
                )
                if p.yahoo in ib_hist:
                    return ib_hist[p.yahoo]
                sp.ok = False
                sp.error = reason or "无历史数据"
                if reason:
                    import warnings

                    warnings.warn(f"IBKR 历史数据不可用: {reason} (已回退)")
        except Exception as e:  # noqa: BLE001 - IBKR 属可选增强
            import warnings

            warnings.warn(f"IBKR 历史数据异常: {e}")
    if use_longport:
        try:
            from .. import longport as lp_mod

            with health.span("history", "longport", p.yahoo) as sp:
                lp_hist = lp_mod.get_history_longport(p, start_date, end_date)
                if lp_hist is not None:
                    return lp_hist
                sp.ok = False
                sp.error = "无历史数据"
        except Exception as e:  # noqa: BLE001 - 长桥属可选增强
            import warnings

            warnings.warn(f"长桥历史数据异常: {e}")
    provider = _route_provider(p)
    # crypto 域: prefer_akshare 无意义, 复用参数位传源偏好 (字符串); 其它域原样布尔
    prefer = crypto_source if p.type == "crypto" else prefer_akshare
    return provider.fetch_history(p, start_date, end_date, prefer_first=prefer)
