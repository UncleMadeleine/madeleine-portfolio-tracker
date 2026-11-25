"""新浪单代码行情兜底: 沪深/港股字段映射与东财失败原因透传.

背景: 东财 push2 行情域名整段不可达时 (curl 52 Empty reply), 所有 A股/港股
实时行情都会失败。新浪 hq.sinajs.cn 与东财独立, 作为 ``sina`` 源参与源链;
北交所新浪返回全 0 占位, 不作为兜底。
"""

from __future__ import annotations

import time

import pytest

from tracker.providers import stock_akshare as gs
from tracker.symbols import parse


class _Resp:
    def __init__(self, text: str):
        self.encoding = None
        self.text = text


class _Req:
    def __init__(self, text: str):
        self._text = text

    def get(self, *_a, **_k):
        return _Resp(self._text)


@pytest.fixture(autouse=True)
def _clear_sina_cache(monkeypatch):
    monkeypatch.setattr(gs, "_sina_quote_cache", {})
    monkeypatch.setattr(gs, "_ak_quote_errors", {})


def test_sina_symbol_mapping():
    assert gs._sina_symbol(parse("600585.SS")) == "sh600585"
    assert gs._sina_symbol(parse("000001.SZ")) == "sz000001"
    assert gs._sina_symbol(parse("900901.SS")) == "sh900901"  # 沪 B
    assert gs._sina_symbol(parse("200012.SZ")) == "sz200012"  # 深 B
    assert gs._sina_symbol(parse("0700.HK")) == "rt_hk00700"
    # 北交所新浪接口返回全 0 占位, 不参与兜底
    assert gs._sina_symbol(parse("830799.BJ")) is None
    assert gs._sina_symbol(parse("AAPL")) is None


def test_sina_stock_quote_a_share_field_mapping(monkeypatch):
    body = (
        "海螺水泥,17.850,17.850,17.860,17.920,17.680,17.850,17.860,"
        "19615894,349452829.000,800,17.850,400,17.840,2000,17.830,"
        "3400,17.810,26600,17.800,15000,17.860,60000,17.870,188599,"
        "17.880,173700,17.890,83300,17.900,2026-11-24,15:34:59,00,"
    )
    monkeypatch.setattr(gs, "_requests", lambda: _Req(f'var hq_str_sh600585="{body}";'))

    q = gs._sina_quote(parse("600585.SS"))
    assert q.symbol == "600585.SS"
    assert q.name == "海螺水泥"
    assert q.price == 17.86
    assert q.prev_close == 17.85
    assert q.change_pct == pytest.approx((17.86 / 17.85 - 1) * 100)
    assert q.currency == "CNY"
    assert q.source == "akshare_sina"


def test_sina_stock_quote_hk_field_mapping(monkeypatch):
    body = (
        "TENCENT,腾讯控股,352.000,344.000,353.000,340.000,341.600,"
        "-2.400,-0.698,341.400,341.600,10123426303.240,29393105,"
        "12.426,0.000,631.641,333.000,2026/11/24,16:08:15,"
        "100|0,N|Y|Y,341.000|324.000|358.000,0|||0.000|0.000|0.000, |0,Y"
    )
    monkeypatch.setattr(
        gs, "_requests", lambda: _Req(f'var hq_str_rt_hk00700="{body}";')
    )

    q = gs._sina_quote(parse("0700.HK"))
    assert q.name == "腾讯控股"
    assert q.price == 341.6
    assert q.prev_close == 344.0
    assert q.change_pct == pytest.approx(-0.698)
    assert q.currency == "HKD"
    assert q.source == "akshare_sina"


def test_sina_quote_rejects_zero_price(monkeypatch):
    body = (
        "停牌股,0.000,0.000,0.000,0.000,0.000,"
        + ",".join(["0.000"] * 24)
        + ",2026-11-24,09:00:00,00,"
    )
    monkeypatch.setattr(gs, "_requests", lambda: _Req(f'var hq_str_sz000001="{body}";'))
    with pytest.raises(RuntimeError, match="新浪无有效最新价"):
        gs._sina_quote(parse("000001.SZ"))


def test_akshare_quote_error_carries_real_reason(monkeypatch):
    """东财连不上时, 错误信息要带出 Empty reply/超时, 而不是只有「不可用」."""
    monkeypatch.setattr(gs, "_ak_quote_raw", lambda p: None)
    monkeypatch.setattr(
        gs,
        "_ak_quote_errors",
        {"1.600585": (time.time(), "ConnectionError: Empty reply from server")},
    )
    with pytest.raises(RuntimeError) as excinfo:
        gs._akshare_quote(parse("600585.SS"))
    msg = str(excinfo.value)
    assert "akshare 行情不可用 (CN)" in msg
    assert "Empty reply from server" in msg
