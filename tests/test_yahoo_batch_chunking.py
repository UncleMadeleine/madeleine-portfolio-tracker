"""_yahoo_batch 分批 + 限流早停/熔断测试 (离线, mock).

回归背景: Yahoo 限流是 IP 级的。行情改用 v7 ``finance/quote`` 批量接口
(50 个/请求), 不再逐代码 ``Ticker.get_info()``; 连续空批/超时打开进程级
熔断, 冷却期跳过逐代码回源, 避免 600+ 自选一轮打出几百条超时。
"""

from __future__ import annotations

import time as _time
from types import SimpleNamespace

import pytest

from tracker.providers import stock_yahoo as gs
from tracker.symbols import parse


def _row(sym: str) -> dict:
    return {
        "symbol": sym,
        "regularMarketPrice": 10.0,
        "regularMarketPreviousClose": 9.0,
        "regularMarketChangePercent": 1.0,
        "currency": "USD",
        "longName": sym,
    }


def _ok(syms: list[str]) -> dict:
    return {"quoteResponse": {"result": [_row(s) for s in syms]}}


def _empty(syms: list[str]) -> dict:
    return {"quoteResponse": {"result": []}}


@pytest.fixture
def patch_chunk(monkeypatch):
    """缩小块大小 + 屏蔽批间 sleep, 让测试快而可读."""
    monkeypatch.setattr(gs, "_YAHOO_CHUNK", 3)
    monkeypatch.setattr(
        gs, "time", SimpleNamespace(sleep=lambda s: None, time=_time.time)
    )


def _run(monkeypatch, script, symbols):
    """以给定脚本跑 _yahoo_batch, 返回 (结果 dict, 每次请求收到的 symbol 列表).

    script 可以是单个 callable (每次调用都用它) 或按调用序取值的 callable 列表。
    """
    calls: list[list[str]] = []

    def fake_json(syms, timeout=gs._BATCH_TIMEOUT):
        calls.append(list(syms))
        fn = script if callable(script) else script[len(calls) - 1]
        return fn(syms)

    monkeypatch.setattr(gs, "_yahoo_quote_json", fake_json)
    out = gs._yahoo_batch([parse(s) for s in symbols])
    return out, calls


def test_all_symbols_chunked_not_single_request(monkeypatch, patch_chunk):
    """核心回归: 120 个代码必须拆成多块请求, 单块不超过 _YAHOO_CHUNK."""
    syms = [f"S{i:03d}" for i in range(120)]
    out, calls = _run(monkeypatch, lambda s: _ok(s), syms)
    assert len(calls) > 1, "不得把全部代码塞进单个请求"
    assert all(len(c) <= gs._YAHOO_CHUNK for c in calls)
    assert set(out) == set(syms)


def test_consecutive_empty_chunks_stop_remaining(monkeypatch, patch_chunk):
    """连续空批 (限流迹象): 放弃剩余块, 不再持续撞击数据源."""
    syms = [f"S{i:03d}" for i in range(30)]  # 10 块
    out, calls = _run(monkeypatch, _empty, syms)
    assert out == {}
    assert len(calls) == gs._EMPTY_STREAK_GIVEUP, "空批达到上限即停, 剩余块不请求"


def test_success_resets_empty_streak(monkeypatch, patch_chunk):
    """空批后出现成功块: 连空计数清零, 后续块继续请求 (限流可能是间歇性的)."""
    syms = [f"S{i:03d}" for i in range(15)]  # 5 块
    script = [_empty, _ok, _empty, _ok, _ok]
    out, calls = _run(monkeypatch, script, syms)
    assert len(calls) == 5, "成功块应重置连空计数, 5 块全部执行"
    # 成功的块是第 2/4/5 块 → S003-S005 与 S009-S014
    assert set(out) == set(syms[3:6] + syms[9:])


def test_chunk_exception_counts_as_empty(monkeypatch, patch_chunk):
    """块调用抛异常 (如批量超时): 计入连空, 达到上限同样早停."""

    def boom(syms):
        raise TimeoutError("batch hung")

    syms = [f"S{i:03d}" for i in range(30)]
    out, calls = _run(monkeypatch, boom, syms)
    assert out == {}
    assert len(calls) == gs._EMPTY_STREAK_GIVEUP


def test_pause_between_chunks(monkeypatch):
    """批间暂停: N 个块之间 sleep N-1 次 (首块前不停)."""
    sleeps: list[float] = []
    monkeypatch.setattr(gs, "_YAHOO_CHUNK", 3)
    monkeypatch.setattr(
        gs,
        "time",
        SimpleNamespace(sleep=lambda s: sleeps.append(s), time=_time.time),
    )
    syms = [f"S{i:03d}" for i in range(12)]  # 4 块
    _out, calls = _run(monkeypatch, lambda s: _ok(s), syms)
    assert len(calls) == 4
    assert len(sleeps) == 3


def test_batch_empty_trips_circuit_and_skips_single_quote(monkeypatch, patch_chunk):
    """连续空批 → 打开熔断; 单代码回源直接失败, 不再打 Yahoo.

    回归背景: Yahoo 429 后, 600+ 只代码逐只超时, 一轮保温打出几百条失败
    并持续延长限流窗口。
    """
    syms = [f"S{i:03d}" for i in range(9)]  # 3 块, 第 2 块后熔断
    out, calls = _run(monkeypatch, _empty, syms)
    assert out == {}
    assert len(calls) == gs._EMPTY_STREAK_GIVEUP
    assert gs.quote_throttled()

    monkeypatch.setattr(
        gs, "_yahoo_quote_json", lambda *a, **k: pytest.fail("熔断期不应再请求 Yahoo")
    )
    with pytest.raises(RuntimeError, match="限流冷却中"):
        gs._yahoo_quote(parse("AAPL"))

    # 熔断期批量调用直接返回空, 不产生网络请求
    assert gs._yahoo_batch([parse("AAPL")]) == {}


def test_batch_success_clears_circuit(monkeypatch, patch_chunk):
    syms = [f"S{i:03d}" for i in range(3)]
    gs._trip_throttle()
    assert gs.quote_throttled()
    # 直接绕过批量入口的熔断检查, 模拟冷却到期后首个成功批
    monkeypatch.setattr(gs, "quote_throttled", lambda: False)
    out, _calls = _run(monkeypatch, lambda s: _ok(s), syms)
    assert set(out) == set(syms)
    assert not gs.quote_throttled()


def test_v7_quote_rows_parse_price_and_currency():
    """v7 quote 行 → Quote: 价格/昨收/涨跌/币种; 便士与 B 股口径复用旧守卫."""
    from tracker.providers.stock_yahoo import _quote_from_v7

    aapl = _quote_from_v7(
        parse("AAPL"),
        {
            "symbol": "AAPL",
            "regularMarketPrice": 350.28,
            "regularMarketPreviousClose": 354.07,
            "regularMarketChangePercent": -1.07,
            "currency": "USD",
            "longName": "Apple Inc.",
        },
    )
    assert aapl is not None
    assert aapl.price == 350.28
    assert aapl.prev_close == 354.07
    assert aapl.change_pct == pytest.approx(-1.07)
    assert aapl.currency == "USD"
    assert aapl.name == "Apple Inc."

    vod = _quote_from_v7(
        parse("VOD.L"),
        {
            "symbol": "VOD.L",
            "regularMarketPrice": 101.55,
            "regularMarketPreviousClose": 102.9,
            "currency": "GBp",
        },
    )
    assert vod is not None
    assert vod.currency == "GBP"
    assert vod.price == pytest.approx(1.0155)  # 便士 ÷100

    b_share = _quote_from_v7(
        parse("900901.SS"),
        {"symbol": "900901.SS", "regularMarketPrice": 0.444, "currency": "USD"},
    )
    assert b_share is not None
    assert b_share.currency == "USD"

    # 无价格 (如可转债) 返回 None, 交给下一源
    assert (
        _quote_from_v7(
            parse("110081.SS"),
            {"symbol": "110081.SS", "regularMarketPrice": None},
        )
        is None
    )
