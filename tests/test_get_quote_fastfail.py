"""单代码行情快速失败测试: get_quote 接入进程内负缓存 (离线, mock).

回归背景: 一个死代码单码查询在 OpenBB/yfinance 内部重试下 8s+ 才报错,
且 get_quote 原先不走负缓存 —— CLI 循环多码查询 (snapshot/report) 每次都要
重新吃满失败延迟。修复: get_quote 与批量路径共用负缓存, TTL 内直接快速失败。
"""

from __future__ import annotations

import pytest

import tracker.prices as prices
from tracker.providers import orchestration as orch


@pytest.fixture(autouse=True)
def _isolate():
    orch._neg_cache.clear()
    yield
    orch._neg_cache.clear()


def test_get_quote_marks_and_fast_fails(monkeypatch):
    """第一次失败进负缓存; 第二次不再触达源链, 立即报错."""
    calls = []

    class FakeProvider:
        name = "global"

        def fetch_quote(self, p, prefer_first=False):
            calls.append(p.yahoo)
            raise RuntimeError("yfinance 无行情结果")

    from tracker.providers import base as base_mod

    monkeypatch.setattr(base_mod, "resolve", lambda t: FakeProvider())

    with pytest.raises(RuntimeError):
        prices.get_quote("ZZ0001")
    assert orch.neg_cached("ZZ0001")

    # 第二次: 负缓存命中, 源链零调用
    with pytest.raises(RuntimeError, match="负缓存"):
        prices.get_quote("ZZ0001")
    assert calls == ["ZZ0001"]


def test_negative_cache_expires_then_quote_succeeds(monkeypatch):
    """负缓存 TTL 过期后取数成功, 并清出负缓存 (语义: 只挡 TTL 内重试)."""
    from tracker.providers.base import Quote

    class FakeProvider:
        name = "global"

        def fetch_quote(self, p, prefer_first=False):
            return Quote(
                symbol=p.yahoo,
                name=p.yahoo,
                price=10.0,
                prev_close=10.0,
                change_pct=0.0,
                currency="USD",
            )

    from tracker.providers import base as base_mod

    monkeypatch.setattr(base_mod, "resolve", lambda t: FakeProvider())

    # 模拟 TTL 过期: 标记时间戳前移
    import time as _time

    orch._neg_cache["GLD"] = _time.time() - orch._NEG_TTL - 1
    assert not orch.neg_cached("GLD")

    q = prices.get_quote("GLD")
    assert q.price == 10.0
    # 过期条目按 TTL 判定不算缓存 (neg_cached False); 成功取数不再续期
    assert not orch.neg_cached("GLD")
