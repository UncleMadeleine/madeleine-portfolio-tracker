"""钱包净值曲线: 链上历史余额 × 历史币价 → 每日净值.

数据流:

1. :mod:`tracker.wallet_history` 回放每个 UTC 自然日的余额;
2. 每个代币按 ``SYMBOL-USD`` 取历史日线 (稳定币按 1 USD);
3. 统一用 USD→基础货币的历史汇率折算, 汇总成每日总市值。

与 ``nav --backfill`` 的区别: 本模块用的是**每个时点真实的链上余额**,
而不是「当前持仓 × 历史价」, 因此能反映历史上的转入/转出/交易。
限制来自链上数据本身: 质押、rebasing/fee-on-transfer 代币、以及浏览器
索引缺失仍可能造成少量偏差, 见 ``tracker.wallet_history`` 模块说明。
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pandas as pd

from . import fx as fx_mod
from . import prices
from . import wallet_history
from .symbols import STABLE_USD_QUOTES
from .util import finite_float as _num

_WALLET_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9._-]*$")
_MAX_DAYS = 3650

#: 包装/流动性质押代币 → 底层资产的价格代理。没有独立行情时仍能按主币估值,
#: 比直接丢弃这些持仓更接近真实净值。
_PRICE_ALIASES = {
    "WETH": "ETH",
    "WSTETH": "ETH",
    "STETH": "ETH",
    "RETH": "ETH",
    "CBETH": "ETH",
    "WBTC": "BTC",
    "BTCB": "BTC",
    "WMATIC": "MATIC",
    "JITOSOL": "SOL",
    "JUPSOL": "SOL",
    "MSOL": "SOL",
    "BNSOL": "SOL",
}


def _stable_symbol(symbol: str) -> bool:
    """稳定币符号判定 (兼容 USDt / USDT.e 这类跨链写法)."""
    s = str(symbol or "").strip().upper()
    s = s.split(".")[0].split("-")[0]
    return s in STABLE_USD_QUOTES


def _quote_symbol(symbol: str) -> str | None:
    s = str(symbol or "").strip().upper()
    # 跨链桥/包装代币常见后缀 (WETH.e / BTC.b / USDC.e): 取主符号再向行情源
    # 查询; 这类代币的流动性价格通常与主币同步, 比直接查 WETH.E-USD 可靠。
    s = s.split(".")[0]
    if not s or not _WALLET_SYMBOL_RE.fullmatch(s):
        return None
    s = _PRICE_ALIASES.get(s, s)
    return f"{s}-USD"


def _price_series(
    symbol: str,
    index: pd.DatetimeIndex,
    *,
    months: int,
    crypto_source: str,
    refresh: bool,
) -> tuple[pd.Series | None, str | None]:
    """某代币的 USD 价格日线; 返回 (series, 失败原因)."""
    if _stable_symbol(symbol):
        return pd.Series(1.0, index=index, dtype=float), None
    quote = _quote_symbol(symbol)
    if not quote:
        return None, f"{symbol}: 代码含非法字符, 无法取价"
    try:
        df = prices.get_ohlc(
            quote,
            months=months,
            refresh=refresh,
            crypto_source=crypto_source,
        )
    except Exception as e:  # noqa: BLE001 - 单币失败不拖垮整条曲线
        return None, f"{quote}: 历史行情获取失败 ({e})"
    if df is None or df.empty or "close" not in df.columns:
        return None, f"{quote}: 无历史行情"
    s = df[["date", "close"]].copy()
    s["date"] = pd.to_datetime(s["date"], errors="coerce")
    if getattr(s["date"].dt, "tz", None) is not None:
        s["date"] = s["date"].dt.tz_localize(None)
    s["date"] = s["date"].dt.normalize()
    s["close"] = pd.to_numeric(s["close"], errors="coerce")
    s = s.dropna(subset=["date", "close"]).sort_values("date")
    s = s.drop_duplicates("date", keep="last")
    if s.empty:
        return None, f"{quote}: 历史行情为空"
    series = s.set_index("date")["close"].reindex(index).ffill().bfill()
    if series.isna().all():
        return None, f"{quote}: 窗口内无有效收盘价"
    return series.astype(float), None


def _fx_series(
    base: str,
    index: pd.DatetimeIndex,
    start: date,
    end: date,
    *,
    refresh: bool,
) -> pd.Series:
    """1 USD → 基础货币 的历史折算序列; 缺历史时退回即期汇率."""
    base = (base or "USD").upper()
    if base == "USD":
        return pd.Series(1.0, index=index, dtype=float)
    df = fx_mod.get_fx_history(
        "USD", base, start.isoformat(), end.isoformat(), refresh=refresh
    )
    if df is not None and not df.empty and "close" in df.columns:
        s = df[["date", "close"]].copy()
        s["date"] = pd.to_datetime(s["date"], errors="coerce")
        if getattr(s["date"].dt, "tz", None) is not None:
            s["date"] = s["date"].dt.tz_localize(None)
        s["date"] = s["date"].dt.normalize()
        s["close"] = pd.to_numeric(s["close"], errors="coerce")
        s = s.dropna(subset=["date", "close"]).drop_duplicates("date", keep="last")
        if not s.empty:
            return (
                s.set_index("date")["close"]
                .reindex(index)
                .ffill()
                .bfill()
                .astype(float)
            )
    rate = fx_mod.get_rate("USD", base)
    if rate is None or not math.isfinite(float(rate)) or float(rate) <= 0:
        raise wallet_history.WalletHistoryError(
            f"缺少 USD→{base} 历史汇率, 无法折算钱包净值"
        )
    return pd.Series(float(rate), index=index, dtype=float)


def compute_wallet_nav(
    chain: str,
    address: str,
    *,
    base: str = "USD",
    days: int = 180,
    tokenlist: str | None = None,
    config_path: str | None = None,
    crypto_source: str = "auto",
    refresh: bool = False,
    max_assets: int = 30,
) -> dict[str, Any]:
    """查询钱包历史净值曲线.

    返回结构 (可直接 JSON 序列化)::

        {
          "chain": "eth", "address": "0x...", "scope": "wallet:eth:0x...",
          "base": "USD", "days": 180, "start": "2026-05-19", "end": "2026-11-14",
          "providers": ["Routescan"], "truncated": false,
          "rows": [{"day": "...", "total_value": 123.4, "holdings": 2, ...}],
          "assets": [{"symbol": "ETH", "quantity": ..., "price_symbol": "ETH-USD",
                      "priced": true, "first_day": ..., "last_day": ...}],
          "issues": ["..."], "notes": ["..."]
        }

    网络/配置错误抛 :class:`tracker.wallet_history.WalletHistoryError`;
    单个代币无历史价会记入 ``issues`` 并从总市值中排除 (不中断查询)。
    """
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 180
    days = max(1, min(days, _MAX_DAYS))
    base = str(base or "USD").strip().upper() or "USD"
    cfg = wallet_history.load_config(config_path)

    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days - 1)
    ledger = wallet_history.fetch_ledger(
        chain,
        address,
        start,
        end,
        cfg=cfg,
        tokenlist=tokenlist,
        refresh=refresh,
    )
    days_list, balances, replay_issues = wallet_history.reconstruct_daily_balances(
        ledger, start, end
    )
    if not days_list:
        raise wallet_history.WalletHistoryError("查询窗口为空")

    index = pd.DatetimeIndex([pd.Timestamp(d) for d in days_list])
    months = max(1, math.ceil(days / 30) + 2)
    usd_fx = _fx_series(base, index, start, end, refresh=refresh)

    issues: list[str] = list(replay_issues)
    value_cols: dict[str, pd.Series] = {}
    asset_rows: list[dict[str, Any]] = []
    ignored: list[str] = []
    trusted = ledger.trusted or set(ledger.assets)
    candidates: list[tuple[str, list[float], wallet_history.Asset]] = []
    for key, vals in balances.items():
        if not any(abs(v) > 1e-12 for v in vals):
            continue
        asset = ledger.assets.get(key)
        if asset is None:
            continue
        if key not in trusted:
            ignored.append(asset.symbol)
            continue
        candidates.append((key, vals, asset))
    # 钱包可能有几十上百个「有价值」代币; 逐个取历史价会让查询从秒级变成
    # 分钟级。原生币与稳定币优先保留, 其余按当前 USD 估值排序; 被跳过的
    # 资产在 notes 里明确说明, 避免用户误以为曲线覆盖了全部资产。
    try:
        limit = max(1, min(int(max_assets or 30), 500))
    except (TypeError, ValueError):
        limit = 30

    def _rank(item: tuple[str, list[float], wallet_history.Asset]):
        key, vals, asset = item
        # 原生币必留; 其余先按浏览器给出的当前 USD 估值降序 (稳定币只作为
        # 同估值时的次级优先, 避免把价值更高的非稳定币挤掉), 最后按数量。
        return (
            0 if key == "native" else 1,
            -float(ledger.current_usd.get(key) or 0.0),
            0 if _stable_symbol(asset.symbol) else 1,
            -(abs(vals[-1]) if vals else 0.0),
        )

    candidates.sort(key=_rank)
    skipped_assets = [a.symbol for _, _, a in candidates[limit:]]
    candidates = candidates[:limit]

    for key, vals, asset in candidates:
        series, reason = _price_series(
            asset.symbol,
            index,
            months=months,
            crypto_source=crypto_source,
            refresh=refresh,
        )
        nonzero = [d for d, v in zip(days_list, vals) if abs(v) > 1e-12]
        current_qty = float(ledger.current.get(key) or 0.0)
        row = {
            "asset_key": key,
            "symbol": asset.symbol,
            "kind": asset.kind,
            "decimals": asset.decimals,
            "quantity": round(current_qty, 12),
            "value_usd": (
                round(float(ledger.current_usd[key]), 2)
                if key in ledger.current_usd
                else None
            ),
            "price_symbol": (
                None if _stable_symbol(asset.symbol) else _quote_symbol(asset.symbol)
            ),
            "priced": series is not None,
            "first_day": nonzero[0].isoformat() if nonzero else None,
            "last_day": nonzero[-1].isoformat() if nonzero else None,
        }
        asset_rows.append(row)
        if series is None:
            if reason:
                issues.append(reason)
            continue
        qty = pd.Series([float(v) for v in vals], index=index, dtype=float)
        value_cols[key] = qty * series * usd_fx

    if value_cols:
        values = pd.DataFrame(value_cols, index=index)
        total = values.sum(axis=1, min_count=1)
    else:
        total = pd.Series(float("nan"), index=index, dtype=float)
        issues.append("没有可定价的资产, 曲线为空 (检查代币代码/历史行情源)")

    rows: list[dict[str, Any]] = []
    for i, day in enumerate(days_list):
        value = _num(total.iloc[i] if len(total) else None)
        if value is None:
            continue
        holdings = 0
        for key in value_cols:
            asset = ledger.assets.get(key)
            vals = balances.get(key) or []
            if i < len(vals) and abs(vals[i]) > 1e-12 and asset is not None:
                holdings += 1
        rows.append(
            {
                "day": day.isoformat(),
                "total_value": value,
                "total_cost": None,
                "total_pnl": None,
                "total_pnl_pct": None,
                "today_pnl": None,
                "cost_coverage": None,
                "holdings": holdings,
                "source": "wallet-replay",
            }
        )

    # 元数据里保留未定价资产, UI/CLI 可以明确告诉用户「曲线少了什么」
    asset_rows.sort(key=lambda x: (not x["priced"], -abs(float(x["quantity"] or 0))))
    notes = list(ledger.notes)
    ignored = list(dict.fromkeys(ignored))
    if ignored:
        sample = ", ".join(ignored[:8]) + ("…" if len(ignored) > 8 else "")
        notes.append(
            f"已忽略 {len(ignored)} 个未识别或当前估值过低的代币持仓 "
            f"(避免对空投垃圾币逐个取价): {sample}"
        )
    skipped_assets = list(dict.fromkeys(skipped_assets))
    if skipped_assets:
        sample = ", ".join(skipped_assets[:8]) + (
            "…" if len(skipped_assets) > 8 else ""
        )
        notes.append(
            f"资产数量超过单次估值上限 ({limit} 个), 已按当前估值优先保留大头, "
            f"未计入: {sample}"
        )
    return {
        "chain": ledger.chain,
        "address": ledger.address,
        "scope": f"wallet:{ledger.chain}:{ledger.address}",
        "base": base,
        "days": days,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "providers": list(ledger.providers),
        "truncated": bool(ledger.truncated),
        "rows": rows,
        "assets": asset_rows,
        "ignored_assets": ignored[:50],
        "ignored_count": len(ignored),
        "skipped_assets": skipped_assets[:50],
        "skipped_count": len(skipped_assets),
        "max_assets": limit,
        "issues": list(dict.fromkeys(issues)),
        "notes": list(dict.fromkeys(notes)),
    }


__all__ = ["compute_wallet_nav"]
