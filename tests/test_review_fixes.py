"""全模块审查修复的回归测试 (对应各处正确性缺陷)。

每个测试钉住一个已修复的行为, 防止未来回归。纯逻辑, 不联网。
"""

import numpy as np
import pandas as pd

from tracker.analytics import build_view
from tracker.prices import Quote
from tracker import importer
from tracker import charting
from tracker.symbols import INDEX_CATALOG, parse
from tracker.storage import dedupe_watch_entries


def _mk_quote(sym, price, ccy="USD", chg=1.0):
    return Quote(
        symbol=sym,
        name=sym,
        price=price,
        prev_close=price / (1 + chg / 100) if chg is not None else None,
        change_pct=chg,
        currency=ccy,
    )


# ---------------------------------------------------------------------------
# D1: build_view 丢失 quantity 键 (commit 54daae1) → UI/导出/快照补回持股数量
# ---------------------------------------------------------------------------

def test_build_view_includes_quantity():
    holdings = [{"symbol": "AAPL", "quantity": 10, "avg_cost": 100.0}]
    quotes = {"AAPL": _mk_quote("AAPL", 150.0)}
    view, issues = build_view(holdings, quotes, {"USD": 7.0})
    assert issues == []
    assert "quantity" in view.columns
    assert float(view.iloc[0]["quantity"]) == 10.0


# C4: 手改 JSON 数量/成本为非数字字符串 → 记 issue 跳过/按缺失, 不崩
def test_build_view_non_numeric_quantity_skips_not_crash():
    holdings = [{"symbol": "AAPL", "quantity": "abc", "avg_cost": 150.0}]
    view, issues = build_view(holdings, {}, {"USD": 7.0})
    assert view.empty
    assert any("数量无法解析" in i for i in issues)


def test_build_view_non_numeric_cost_becomes_none():
    holdings = [{"symbol": "AAPL", "quantity": 10, "avg_cost": "N/A"}]
    quotes = {"AAPL": _mk_quote("AAPL", 150.0)}
    view, issues = build_view(holdings, quotes, {"USD": 7.0})
    assert not view.empty
    assert pd.isna(view.iloc[0]["cost"])
    assert any("成本无法解析" in i for i in issues)


# ---------------------------------------------------------------------------
# D7: RSI 用真正的 Wilder 播种 (预热段 NaN + 稳态吻合教科书)
# ---------------------------------------------------------------------------

def _wilder_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    d = close.diff()
    g = d.clip(lower=0)
    lo = -d.clip(upper=0)
    n = len(close)
    ag = np.full(n, np.nan)
    al = np.full(n, np.nan)
    if n <= period:
        return pd.Series(ag, index=close.index)
    ag[period] = g.iloc[1 : period + 1].mean()
    al[period] = lo.iloc[1 : period + 1].mean()
    for i in range(period + 1, n):
        ag[i] = (ag[i - 1] * (period - 1) + g.iloc[i]) / period
        al[i] = (al[i - 1] * (period - 1) + lo.iloc[i]) / period
    with np.errstate(all="ignore"):
        rs = ag / al
        r = 100 - 100 / (1 + rs)
    r[al == 0] = 100.0
    r[(ag == 0) & (al == 0)] = 50.0
    return pd.Series(r, index=close.index)


def test_rsi_warmup_is_nan():
    rng = np.random.default_rng(7)
    close = pd.Series(100 + np.cumsum(rng.standard_normal(40)))
    rsi = charting.calc_rsi(pd.DataFrame({"close": close}), 14)
    # 前 period 根为预热段, 应全 NaN (旧实现在这里产出伪造值)
    assert rsi.iloc[:14].isna().all()
    assert rsi.iloc[14:].notna().all()


def test_rsi_matches_wilder_steady_state():
    rng = np.random.default_rng(11)
    close = pd.Series(100 + np.cumsum(rng.standard_normal(60)))
    rsi = charting.calc_rsi(pd.DataFrame({"close": close}), 14)
    ref = _wilder_rsi(close, 14)
    assert float((rsi.iloc[14:] - ref.iloc[14:]).abs().max()) < 1e-9


# ---------------------------------------------------------------------------
# D2/D3: 低价币精度 (round(,6)/round(,4) 曾把 SHIB 级价格归零)
# ---------------------------------------------------------------------------

def _shib_df():
    idx = pd.date_range("2024-01-01", periods=5, freq="D")
    return pd.DataFrame(
        {
            "date": idx,
            "open": [1.2e-5] * 5,
            "high": [1.6e-5] * 5,
            "low": [1.1e-5] * 5,
            "close": [1.234e-5, 1.301e-5, 1.255e-5, 1.402e-5, 1.5e-5],
            "volume": [1, 2, 3, 4, 5],
        }
    )


def test_kline_payload_preserves_low_price_precision():
    p = charting.kline_payload(_shib_df(), "SHIB-USD")
    close0 = p["candles"][0]["close"]
    assert abs(close0 - 1.234e-5) < 1e-12  # 未被 round(,6) 截成 1.2e-5
    assert p["precision"] >= 8


def test_compare_payload_raw_mode_not_zeroed():
    cp = charting.compare_payload({"SHIB-USD": _shib_df()}, normalize=False)
    v0 = cp["lines"][0]["data"][0]["value"]
    assert abs(v0 - 1.234e-5) < 1e-12  # 未被 round(,4) 归零
    assert cp["precision"] >= 8


# D4: clean_ohlc 过滤 volume 的 inf/负值
def test_clean_ohlc_filters_inf_and_negative_volume():
    df = pd.DataFrame(
        {
            "date": ["2025-01-06", "2025-01-07"],
            "open": [10.0, 11.0],
            "high": [12.0, 13.0],
            "low": [9.0, 10.0],
            "close": [11.0, 12.0],
            "volume": [np.inf, -5.0],
        }
    )
    out = charting.clean_ohlc(df)
    assert list(out["volume"]) == [0.0, 0.0]
    assert np.isfinite(out["volume"]).all()


# ---------------------------------------------------------------------------
# D10(2): export MD 报表 pnl_pct 按百分数 (比值 0.5 → +50.00%, 非 0.50)
# ---------------------------------------------------------------------------

def test_export_md_pnl_pct_as_percent():
    from tracker.cli.export import _export_md

    view = pd.DataFrame(
        [
            {
                "symbol": "AAPL",
                "name": "Apple",
                "market": "美股",
                "price": 150.0,
                "change_pct": 2.56,
                "quantity": 10,
                "avg_cost": 100.0,
                "market_value": 1500.0,
                "cost": 1000.0,
                "pnl": 500.0,
                "pnl_pct": 0.5,
                "today_pnl": 10.0,
            }
        ]
    )
    summary = {
        "total_value": 1500.0,
        "total_pnl": 500.0,
        "total_pnl_pct": 0.5,
        "today_pnl": 10.0,
    }
    md = _export_md("USD", view, summary, [])
    assert "+50.00%" in md  # 比值按百分数显示


# ---------------------------------------------------------------------------
# D6: US02Y (^IRX 实为 13 周国库券) 更正为 US3M
# ---------------------------------------------------------------------------

def test_us3m_in_catalog_and_us02y_removed():
    assert "US3M" in INDEX_CATALOG
    assert "US02Y" not in INDEX_CATALOG
    assert INDEX_CATALOG["US3M"]["yf"] == "^IRX"


# ---------------------------------------------------------------------------
# storage dedupe: 显式 0 (upper_1=0) 不被后一条 truthy 值覆盖
# ---------------------------------------------------------------------------

def test_dedupe_preserves_explicit_zero_threshold():
    merged = dedupe_watch_entries(
        [
            {"symbol": "AAPL", "lists": ["A"], "upper_1": 0},
            {"symbol": "AAPL", "lists": ["B"], "upper_1": 150},
        ]
    )
    assert merged[0]["upper_1"] == 0


# ---------------------------------------------------------------------------
# importer: avg_cost=None 不抹掉已手填成本
# ---------------------------------------------------------------------------

def test_merge_holdings_preserves_cost_when_new_is_none():
    existing = [{"symbol": "AAPL", "quantity": 10, "avg_cost": 150.0}]
    rows = [{"symbol": "AAPL", "quantity": 20, "avg_cost": None}]
    merged, _stats = importer.merge_holdings(
        existing, rows, mode=importer.MODE_APPEND
    )
    assert float(merged[0]["quantity"]) == 20.0  # 数量更新
    assert merged[0].get("avg_cost") == 150.0  # 成本不被 None 抹掉


# ---------------------------------------------------------------------------
# services #7: 快照 issues 同代码去重 + 格式统一 (symbol: 原因)
# ---------------------------------------------------------------------------

def test_snapshot_issues_deduped(monkeypatch):
    import tracker.cli  # noqa: F401 - 先初始化 cli 包, 避开 services.snapshot↔cli 的循环导入
    from tracker.services import snapshot as snap

    try:
        parse("BAD.ZZ")
    except ValueError as e:
        reason = str(e)

    def fake_get_quotes(symbols, **kw):
        return {}, {"BAD.ZZ": reason}, []

    monkeypatch.setattr(snap.prices, "get_quotes", fake_get_quotes)
    portfolio = {"base_currency": "CNY", "holdings": [{"symbol": "BAD.ZZ", "quantity": 1}]}
    _base, _view, _summary, _wview, issues = snap.take_snapshot(portfolio, {})
    hits = [i for i in issues if "BAD.ZZ" in i]
    assert len(hits) == 1  # build_view 与 get_quotes 各记一条, 去重后仅一条
