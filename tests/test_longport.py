"""长桥 (LongPort) provider 单测: 符号映射 / 配置 / 降级 / 行情与历史接线 / 持仓导入.

不联网: SDK 层全部用假对象替换; 真实网络行为由 smoke 覆盖。
"""
import math
from datetime import datetime
import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import pytest

from tracker import importer, longport as lp
from tracker.providers import orchestration as orch
from tracker.symbols import parse


# ---------------------------------------------------------------------------
# 代码映射
# ---------------------------------------------------------------------------

def test_yahoo_to_longport_roundtrip():
    cases = [
        ("0700.HK", "700.HK"),
        ("600519.SS", "600519.SH"),
        ("000001.SZ", "000001.SZ"),
        ("AAPL", "AAPL.US"),
        ("BRK-B", "BRK-B.US"),  # 美股类别股: 单字母连字符不误判
    ]
    for yahoo, expected in cases:
        got = lp.yahoo_to_longport(yahoo)
        assert got == expected, yahoo
        assert lp.longport_to_yahoo(expected) == yahoo, expected


def test_yahoo_to_longport_unsupported_markets():
    # 长桥不支持德英加澳与北交所
    for sym in ("SAP.DE", "BP.L", "RY.TO", "BHP.AX", "830799.BJ"):
        assert lp.yahoo_to_longport(sym) is None, sym


def test_longport_to_yahoo_hk_zero_padding():
    assert lp.longport_to_yahoo("700.HK") == "0700.HK"
    assert lp.longport_to_yahoo("5.HK") == "0005.HK"
    assert lp.longport_to_yahoo("8083.HK") == "8083.HK"


def test_longport_to_yahoo_us_and_unknown():
    assert lp.longport_to_yahoo("AAPL.US") == "AAPL"
    assert lp.longport_to_yahoo("AAPL") is None  # 缺 region 不是合法长桥代码
    assert lp.longport_to_yahoo("XXX.DD") is None


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

def test_load_config_falls_back_to_example(tmp_path, monkeypatch):
    monkeypatch.setattr(lp, "DEFAULT_CONFIG", tmp_path / "nope.json")
    monkeypatch.delenv("LONGPORT_CONFIG", raising=False)
    cfg = lp.load_config()
    assert cfg["auth"] == "oauth"
    assert cfg["callback_port"] == 60355


def test_load_config_real_overrides_example(tmp_path, monkeypatch):
    real = tmp_path / "longport.json"
    real.write_text('{"auth": "apikey", "app_key": "K", "app_secret": "S", "access_token": "T", "_comment": "x"}')
    cfg = lp.load_config(real)
    assert cfg["auth"] == "apikey"
    assert cfg["app_key"] == "K"
    assert "_comment" not in cfg


def test_build_config_apikey_missing_credentials(tmp_path, monkeypatch):
    real = tmp_path / "longport.json"
    real.write_text('{"auth": "apikey"}')
    with patch.object(lp, "load_config", return_value=lp.load_config(real)):
        with pytest.raises(RuntimeError, match="app_key"):
            lp.build_config()


def test_build_config_oauth_placeholder_fast_fails(tmp_path):
    # 模板占位 client_id 必须快速报错, 绝不能发起阻塞式授权流
    cfg = lp.load_config("longport.example.json")
    with pytest.raises(RuntimeError, match="client_id"):
        lp.build_config(cfg)


def test_build_config_unknown_auth(tmp_path):
    real = tmp_path / "longport.json"
    real.write_text('{"auth": "basic"}')
    with pytest.raises(RuntimeError, match="未知认证方式"):
        lp.build_config(lp.load_config(real))


# ---------------------------------------------------------------------------
# 行情 / 历史 (SDK 假对象)
# ---------------------------------------------------------------------------

def _fake_sdk(api=None):
    """monkeypatch lp._sdk 返回指定 openapi 模块替身."""
    return patch.object(lp, "_sdk", return_value=api)


def test_get_quotes_longport_unavailable_without_config(monkeypatch, tmp_path):
    # 未配置 (无 client_id) → 快速降级: ({}, reason), 绝不挂起
    monkeypatch.delenv("LONGPORT_CONFIG", raising=False)
    monkeypatch.setattr(lp, "DEFAULT_CONFIG", tmp_path / "nope.json")
    quotes, reason = lp.get_quotes_longport([parse("AAPL"), parse("0700.HK")])
    assert quotes == {}
    assert isinstance(reason, str) and reason


def test_get_quotes_longport_maps_and_skips_unsupported(monkeypatch):
    captured = {}

    class FakeCtx:
        def quote(self, symbols):
            captured["symbols"] = list(symbols)
            return [
                SimpleNamespace(
                    symbol="700.HK", last_done=Decimal("338.0"), prev_close=Decimal("334.8"),
                    open=Decimal("340"), high=Decimal("341"), low=Decimal("333"),
                    timestamp=datetime(2026, 9, 24), volume=100, turnover=Decimal("1"),
                ),
                SimpleNamespace(
                    symbol="AAPL.US", last_done=Decimal("150.0"), prev_close=Decimal("150.0"),
                    open=Decimal("150"), high=Decimal("150"), low=Decimal("150"),
                    timestamp=datetime(2026, 9, 24), volume=1, turnover=Decimal("1"),
                ),
            ]

    api = SimpleNamespace(
        QuoteContext=lambda cfg: FakeCtx(),
        Language=SimpleNamespace(ZH_CN=1, ZH_HK=2, EN=3),
    )
    with _fake_sdk(api):
        with patch.object(lp, "_get_quote_ctx", return_value=FakeCtx()):
            quotes, reason = lp.get_quotes_longport(
                [parse("0700.HK"), parse("AAPL"), parse("SAP.DE")]
            )
    assert reason is None
    assert captured["symbols"] == ["700.HK", "AAPL.US"]  # SAP.DE 不可映射, 已剔除
    q = quotes["0700.HK"]
    assert q.price == pytest.approx(338.0)
    assert q.prev_close == pytest.approx(334.8)
    assert q.change_pct == pytest.approx((338.0 - 334.8) / 334.8 * 100)
    assert q.currency == "HKD"
    assert quotes["AAPL"].currency == "USD"


def test_get_quotes_longport_batch_failure_reports_reason(monkeypatch):
    class BoomCtx:
        def quote(self, symbols):
            raise RuntimeError("rate limit")

    with patch.object(lp, "_get_quote_ctx", return_value=BoomCtx()):
        quotes, reason = lp.get_quotes_longport([parse("AAPL")])
    assert quotes == {}
    assert "rate limit" in reason


def test_get_history_longport_returns_sorted_df(monkeypatch):
    candles = [
        SimpleNamespace(timestamp=datetime(2026, 9, 23), open=Decimal("1"), high=Decimal("2"),
                        low=Decimal("0.5"), close=Decimal("1.5"), volume=10),
        SimpleNamespace(timestamp=datetime(2026, 9, 24), open=Decimal("1.5"), high=Decimal("3"),
                        low=Decimal("1"), close=Decimal("2"), volume=20),
    ]

    class FakeCtx:
        def history_candlesticks_by_date(self, symbol, period, adjust, start, end):
            assert symbol == "700.HK"
            return candles

    with patch.object(lp, "_get_quote_ctx", return_value=FakeCtx()):
        df = lp.get_history_longport(parse("0700.HK"), "2026-09-01", "2026-09-30")
    assert list(df["date"]) == ["2026-09-23", "2026-09-24"]  # 升序
    assert float(df["close"].iloc[-1]) == 2.0
    assert float(df["volume"].iloc[-1]) == 20


def test_get_history_longport_unsupported_or_unavailable(monkeypatch):
    # 不支持的市场 → None (回退默认源); 连接失败 → None
    assert lp.get_history_longport(parse("SAP.DE"), "2026-01-01") is None
    with patch.object(lp, "_get_quote_ctx", return_value=None):
        assert lp.get_history_longport(parse("AAPL"), "2026-01-01") is None


# ---------------------------------------------------------------------------
# orchestration 接线
# ---------------------------------------------------------------------------

def _fake_quote(symbol: str):
    from tracker.providers.base import Quote

    return Quote(symbol, "x", 10.0, 9.0, 11.1, "USD")


def test_orchestration_longport_prefetch_used(monkeypatch):
    """use_longport=True 时前置命中, 后续源链不再取数."""
    from tracker.providers.base import Quote
    from tracker import cache as cache_mod

    def fake_lp(parsed, cfg=None):
        p = parsed[0]
        return {p.yahoo: Quote(p.yahoo, "x", 10.0, 9.0, 11.1, "USD")}, None

    monkeypatch.setattr("tracker.longport.get_quotes_longport", fake_lp)
    cache_mod.clear()
    quotes, errors, notes = orch.get_quotes(["AAPL"], use_longport=True)
    assert "AAPL" in quotes and errors == {} and notes == []
    cache_mod.clear()


def test_orchestration_longport_unavailable_note(monkeypatch):
    monkeypatch.setattr(
        "tracker.longport.get_quotes_longport", lambda parsed, cfg=None: ({}, "连接失败")
    )
    # 回退链全部 mock: 测试只关心 note 与"前置失败不阻塞主链", 不联网
    monkeypatch.setattr(
        orch, "_yahoo_batch", lambda plist: {p.yahoo: _fake_quote(p.yahoo) for p in plist}
    )
    from tracker import cache as cache_mod

    cache_mod.clear()
    quotes, errors, notes = orch.get_quotes(["AAPL"], use_longport=True)
    assert any("长桥不可用" in n for n in notes)
    assert "AAPL" in quotes  # 回退默认源取数
    cache_mod.clear()


def test_orchestration_no_longport_by_default(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("不应调用 longport")

    monkeypatch.setattr("tracker.longport.get_quotes_longport", _boom)
    monkeypatch.setattr(
        orch, "_yahoo_batch", lambda plist: {p.yahoo: _fake_quote(p.yahoo) for p in plist}
    )
    from tracker import cache as cache_mod

    cache_mod.clear()
    quotes, errors, notes = orch.get_quotes(["AAPL"], use_longport=False)
    assert notes == []
    cache_mod.clear()


def test_orchestration_get_history_longport_passthrough(monkeypatch):
    df = pd.DataFrame({"date": ["2026-09-24"], "open": [1.0], "high": [1.0],
                       "low": [1.0], "close": [1.0], "volume": [1]})
    monkeypatch.setattr("tracker.longport.get_history_longport", lambda p, s, e: df)
    out = orch.get_history("0700.HK", months=3, use_longport=True)
    assert float(out["close"].iloc[0]) == 1.0


# ---------------------------------------------------------------------------
# 持仓导入
# ---------------------------------------------------------------------------

class FakeMarket:
    def __init__(self, name):
        self._name = name

    def __str__(self):
        return f"Market.{self._name}"


def _pos(symbol, qty, cost, ccy, market):
    return SimpleNamespace(
        symbol=symbol, symbol_name=symbol,
        quantity=Decimal(str(qty)), cost_price=Decimal(str(cost)),
        currency=ccy, market=FakeMarket(market),
        available_quantity=Decimal(str(qty)), init_quantity=None,
    )


def test_positions_to_rows_mapping_and_skips():
    positions = [
        _pos("700.HK", 100, 457.53, "HKD", "HK"),
        _pos("AAPL.US", 10.5, 180.25, "USD", "US"),
        _pos("600519.SH", 200, 1700.0, "CNY", "CN"),
        _pos("9999.HK", 0, 1.0, "HKD", "HK"),      # 零仓位跳过
        _pos("XXX.DD", 10, 1.0, "USD", "US"),       # 非法形态跳过
    ]
    rows, skipped = lp.positions_to_rows(positions)
    assert len(rows) == 3 and len(skipped) == 1
    by = {r["symbol"]: r for r in rows}
    assert by["0700.HK"]["quantity"] == 100
    assert by["0700.HK"]["avg_cost"] == pytest.approx(457.53)
    assert by["0700.HK"]["currency"] == "HKD"
    assert by["AAPL"]["quantity"] == 10.5
    assert by["600519.SS"]["type"] == "cn"


def test_collect_longport_via_importer(monkeypatch):
    monkeypatch.setattr(
        lp, "fetch_stock_positions",
        lambda cfg=None: [_pos("700.HK", 100, 457.5, "HKD", "HK")],
    )
    rows, skipped = importer.collect_longport()
    assert rows[0]["symbol"] == "0700.HK"
    assert rows[0]["type"] == "global"
    assert skipped == []


def test_fetch_stock_positions_wraps_sdk_errors(monkeypatch):
    class Api:
        def TradeContext(self, cfg):
            raise RuntimeError("token expired")

    with _fake_sdk(Api()):
        with patch.object(lp, "build_config", return_value=object()):
            with pytest.raises(RuntimeError, match="长桥持仓查询失败"):
                lp.fetch_stock_positions()


# ---------------------------------------------------------------------------
# OAuth 子进程入口 (tracker.longport_oauth)
# ---------------------------------------------------------------------------

def test_longport_oauth_module_writes_error_without_client_id(tmp_path, monkeypatch):
    """占位/缺失 client_id → 子进程脚本写 error 状态并退出码 2."""
    import subprocess
    import sys
    from tracker import longport_oauth

    state_file = tmp_path / "state.json"
    monkeypatch.setattr(longport_oauth, "_STATE_PATH", state_file)
    monkeypatch.setattr(
        longport_oauth.longport, "load_config",
        lambda path=None: {"auth": "oauth", "client_id": "在此填入 OAuth 客户端注册返回的 client_id"},
    )
    # 直接调 main (不 spawn), 验证状态写入与退出码
    rc = longport_oauth.main()
    assert rc == 2
    data = json.loads(state_file.read_text(encoding="utf-8"))
    assert data["status"] == "error"
    assert "client_id" in data["message"]


def test_longport_oauth_module_success_flow(tmp_path, monkeypatch):
    """SDK mock: build 回调 → waiting 状态 + URL; from_oauth 验证 → ok."""
    import subprocess
    import sys
    from tracker import longport_oauth

    state_file = tmp_path / "state.json"
    monkeypatch.setattr(longport_oauth, "_STATE_PATH", state_file)
    monkeypatch.setattr(
        longport_oauth.longport, "load_config",
        lambda path=None: {"auth": "oauth", "client_id": "cid-9", "callback_port": 60355},
    )

    class FakeBuilder:
        def __init__(self, cid, port=None):
            assert cid == "cid-9"

        def build(self, on_open):
            on_open("https://fake.authorize/url")
            return object()

    fake_api = SimpleNamespace(
        OAuthBuilder=FakeBuilder,
        Language=SimpleNamespace(ZH_CN=1, ZH_HK=2, EN=3),
        Config=SimpleNamespace(from_oauth=lambda oauth, **kw: object()),
    )
    monkeypatch.setattr(longport_oauth.longport, "_sdk", lambda: fake_api)
    rc = longport_oauth.main()
    assert rc == 0
    data = json.loads(state_file.read_text(encoding="utf-8"))
    assert data["status"] == "ok"
    # waiting 中间态被覆盖为 ok (同文件原子替换); 单独验证 waiting 分支:
    longport_oauth._write_state({"status": "waiting", "url": "https://x"})
    data = json.loads(state_file.read_text(encoding="utf-8"))
    assert data["status"] == "waiting" and data["url"] == "https://x"


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------

def test_settings_default_use_longport(tmp_path):
    from tracker import storage

    data = storage.load_settings(tmp_path / "missing.json")
    assert data["use_longport"] is False
    storage.save_settings({"use_longport": True}, tmp_path / "s.json")
    data = storage.load_settings(tmp_path / "s.json")
    assert data["use_longport"] is True
