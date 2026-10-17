"""_yahoo_batch 分批 + 限流早停测试 (离线, mock).

回归背景: 组合页 675 自选冷启动, _yahoo_batch 把全部代码塞进单个 OpenBB
quote 请求 → Yahoo 429 (crumb rate-limited) / 401 (Invalid Crumb), 且限流
是 IP 级 —— 随后单代码行情与 K线历史请求全部遭殃。修复: 按 50 个/块拆分
+ 批间暂停; 连续空批判定已被限流, 放弃剩余块 (未取到的代码由逐代码源链
兜底, 失败进负缓存, TTL 内不重试)。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tracker.providers import global_stocks as gs
from tracker.symbols import parse


class _Item:
    def __init__(self, d: dict):
        self._d = d

    def model_dump(self) -> dict:
        return self._d


class _Res:
    def __init__(self, items: list[_Item]):
        self.results = items


def _ok(syms: list[str]) -> _Res:
    return _Res(
        [
            _Item(
                {
                    "symbol": s,
                    "last_price": 10.0,
                    "prev_close": 9.0,
                    "currency": "USD",
                }
            )
            for s in syms
        ]
    )


def _empty(syms: list[str]) -> _Res:
    return _Res([])


@pytest.fixture
def patch_chunk(monkeypatch):
    """缩小块大小 + 屏蔽批间 sleep, 让测试快而可读."""
    monkeypatch.setattr(gs, "_YAHOO_CHUNK", 3)
    monkeypatch.setattr(gs, "time", SimpleNamespace(sleep=lambda s: None))


def _run(monkeypatch, script, symbols):
    """以给定脚本跑 _yahoo_batch, 返回 (结果 dict, 每次 quote 调用收到的 symbol 列表).

    script 可以是单个 callable (每次调用都用它) 或按调用序取值的 callable 列表。
    """
    calls: list[list[str]] = []

    def quote(symbol=None, provider=None):
        s = [symbol] if isinstance(symbol, str) else list(symbol)
        calls.append(s)
        fn = script if callable(script) else script[len(calls) - 1]
        return fn(s)

    monkeypatch.setattr(
        gs,
        "_obb",
        lambda: SimpleNamespace(
            equity=SimpleNamespace(price=SimpleNamespace(quote=quote))
        ),
    )
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
    """块调用抛异常 (如批量超时按空结果处理): 计入连空, 达到上限同样早停."""

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
    monkeypatch.setattr(gs, "time", SimpleNamespace(sleep=lambda s: sleeps.append(s)))
    syms = [f"S{i:03d}" for i in range(12)]  # 4 块
    _out, calls = _run(monkeypatch, lambda s: _ok(s), syms)
    assert len(calls) == 4
    assert len(sleeps) == 3
