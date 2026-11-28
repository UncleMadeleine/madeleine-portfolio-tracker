"""Yahoo 直连取数适配层 (tracker.yahoo_data) 的离线回归测试.

覆盖从 OpenBB 薄封装迁回 yfinance 后必须保持的口径:
- 历史K线列名统一为 date/open/high/low/close/volume, 时区剥离,
  end_date 按闭区间处理 (yfinance 的 end 是开区间);
- 基本面字段沿用旧 OpenBB metrics 名称, 5 年平均股息率 /100 成小数比例;
- 缺 52WeekChange 的快照视为无数据 (与旧 provider 一致)。
"""

from __future__ import annotations

import pandas as pd
import pytest

from tracker import yahoo_data


class _FakeYf:
    def __init__(self, df: pd.DataFrame, info: dict | None = None):
        self.df = df
        self.info_data = info or {}
        self.download_calls: list[dict] = []
        self.info_calls: list[str] = []

    def download(self, **kwargs):
        self.download_calls.append(kwargs)
        return self.df

    def Ticker(self, symbol):  # noqa: N802 - 模拟 yfinance 的公开 API 名
        fake = self

        class _T:
            def get_info(self):
                fake.info_calls.append(symbol)
                return fake.info_data

        return _T()


def _multiindex_df() -> pd.DataFrame:
    idx = pd.DatetimeIndex(["2026-11-09", "2026-11-10"], tz="America/New_York")
    cols = pd.MultiIndex.from_product(
        [["AAPL"], ["Open", "High", "Low", "Close", "Volume"]]
    )
    return pd.DataFrame(
        [
            [1.0, 2.0, 0.5, 1.5, 100.0],
            [1.6, 2.5, 1.0, 2.0, 200.0],
        ],
        index=idx,
        columns=cols,
    )


def _install(monkeypatch, fake: _FakeYf) -> None:
    monkeypatch.setattr(yahoo_data, "_yf", lambda: fake)


def test_history_normalizes_columns_and_honors_closed_end_date(monkeypatch):
    fake = _FakeYf(_multiindex_df())
    _install(monkeypatch, fake)

    out = yahoo_data.history("aapl", "2026-11-09", "2026-11-10")

    assert list(out.columns) == ["date", "open", "high", "low", "close", "volume"]
    assert out["date"].dt.tz is None
    assert out["date"].tolist() == [
        pd.Timestamp("2026-11-09"),
        pd.Timestamp("2026-11-10"),
    ]
    params = fake.download_calls[0]
    assert params["tickers"] == "AAPL"
    assert params["auto_adjust"] is False
    # 闭区间 end → yfinance 开区间 end 需 +1 天
    assert params["end"] == "2026-11-11"


def test_history_ensure_volume_adds_zero_column(monkeypatch):
    idx = pd.DatetimeIndex(["2026-11-09"])
    df = pd.DataFrame(
        [[1.0, 2.0, 0.5, 1.5]], index=idx, columns=["Open", "High", "Low", "Close"]
    )
    _install(monkeypatch, _FakeYf(df))

    out = yahoo_data.history("^GSPC", "2026-11-01", ensure_volume=True)

    assert "volume" in out.columns
    assert out["volume"].iloc[0] == 0.0


def test_history_empty_raises_readable(monkeypatch):
    _install(monkeypatch, _FakeYf(pd.DataFrame()))
    with pytest.raises(RuntimeError, match="无历史数据"):
        yahoo_data.history("ZZZZ", "2026-11-01")


def test_metrics_from_info_maps_legacy_openbb_fields():
    row = yahoo_data.metrics_from_info(
        "AAPL",
        {
            "52WeekChange": 0.12,
            "marketCap": 100.0,
            "trailingPE": 10.0,
            "forwardPE": 9.0,
            "priceToBook": 2.0,
            "dividendYield": 0.3,
            "fiveYearAvgDividendYield": 0.46,
            "payoutRatio": 0.2,
            "trailingEps": 1.5,
            "bookValue": 5.0,
            "currency": "USD",
        },
    )

    assert row is not None
    assert row["symbol"] == "AAPL"
    assert row["market_cap"] == 100.0
    assert row["pe_ratio"] == 10.0
    # 旧 OpenBB 把 5 年平均股息率的百分数 /100 成小数比例
    assert row["dividend_yield_5y_avg"] == pytest.approx(0.0046)
    assert row["book_value"] == 5.0


def test_metrics_from_info_ignores_incomplete_snapshot():
    # 无 52WeekChange: 旧 OpenBB 同样会丢弃该代码 (crypto/ETF 常见)
    assert yahoo_data.metrics_from_info("BTC-USD", {"marketCap": 1.0}) is None
    assert yahoo_data.metrics_from_info("AAPL", {}) is None


def test_metrics_row_raises_when_no_metrics(monkeypatch):
    _install(monkeypatch, _FakeYf(pd.DataFrame(), info={"marketCap": 1.0}))
    with pytest.raises(RuntimeError, match="未返回估值指标"):
        yahoo_data.metrics_row("BTC-USD")
