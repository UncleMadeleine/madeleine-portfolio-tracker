"""批量行情编排 (orchestration.get_quotes) 并发回退与负缓存测试 (离线, mock).

回归背景: 组合页 690 代码批量取数, 全球域 yfinance 批量未命中的死代码逐个
串行走源链, 单个 10s+ (OpenBB 内部重试), 串行总计 10 分钟级 → 页面 spinner 卡死。
修复: ① 逐代码源链回退域内并发; ② 全部源失败的代码进进程内负缓存, TTL 内不再重试。
"""

from __future__ import annotations

import time

import pytest

import tracker.cache as cache_mod
import tracker.providers.orchestration as orch
from tracker.cache import get_cached
from tracker.providers.base import Quote


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """临时缓存 DB + 清空负缓存, 每个测试互不污染."""
    monkeypatch.setattr(cache_mod, "CACHE_DB", tmp_path / "test_quotes_cache.db")
    cache_mod._ensure_db()
    orch._neg_cache.clear()


def _q(symbol: str, price: float = 10.0) -> Quote:
    return Quote(
        symbol=symbol,
        name=symbol,
        price=price,
        prev_close=price,
        change_pct=0.0,
        currency="USD",
    )


def _patch_batch(monkeypatch, hits: set[str]):
    """全球域 yfinance 批量只命中 hits 里的代码, 其余留给逐代码源链."""

    def fake_batch(parsed):
        return {p.yahoo: _q(p.yahoo) for p in parsed if p.yahoo in hits}

    monkeypatch.setattr(orch, "_yahoo_batch", fake_batch)


def test_failed_symbols_do_not_block_others(monkeypatch):
    """域内一个死代码慢失败不阻断其它代码: 成功项全部收进 quotes."""
    calls: list[str] = []

    monkeypatch.setattr(orch, "resolve", lambda t: _FakeProvider(calls, {"SLOW"}))
    _patch_batch(monkeypatch, set())  # 全部走逐代码源链

    quotes, errors, _notes = orch.get_quotes(
        ["AAPL", "MSFT", "SLOW"], use_ibkr=False, use_longport=False
    )
    assert set(quotes) == {"AAPL", "MSFT"}
    assert "SLOW" in errors
    assert set(calls) == {"AAPL", "MSFT", "SLOW"}


def test_negative_cache_skips_refetch(monkeypatch):
    """全部源失败的代码进负缓存: TTL 内第二次调用不再触达源链."""
    calls: list[str] = []

    class _Prov:
        name = "global"

        def fetch_quote(self, p, prefer_first=False):
            calls.append(p.yahoo)
            raise RuntimeError(f"{p.yahoo}: dead")

    monkeypatch.setattr(orch, "resolve", lambda t: _Prov())
    _patch_batch(monkeypatch, set())

    q1, e1, _ = orch.get_quotes(["DEAD"], use_ibkr=False, use_longport=False)
    assert not q1 and "DEAD" in e1
    n1 = len(calls)

    q2, e2, _ = orch.get_quotes(["DEAD"], use_ibkr=False, use_longport=False)
    assert not q2
    assert "DEAD" in e2
    assert "负缓存" in e2["DEAD"]
    assert len(calls) == n1, "TTL 内负缓存命中不得重试源链"


def test_negative_cache_expires(monkeypatch):
    """负缓存过期后恢复重试."""
    calls: list[str] = []

    class _Prov:
        name = "global"

        def fetch_quote(self, p, prefer_first=False):
            calls.append(p.yahoo)
            raise RuntimeError("dead")

    monkeypatch.setattr(orch, "resolve", lambda t: _Prov())
    _patch_batch(monkeypatch, set())
    monkeypatch.setattr(orch, "_NEG_TTL", 0.05)

    orch.get_quotes(["DEAD"], use_ibkr=False, use_longport=False)
    n1 = len(calls)
    time.sleep(0.06)
    orch.get_quotes(["DEAD"], use_ibkr=False, use_longport=False)
    assert len(calls) > n1, "TTL 过期后应重新尝试源链"


def test_success_clears_negative_cache(monkeypatch):
    """负缓存命中后代码恢复 (如数据源修复): 下次取数成功并清出负缓存."""
    state = {"dead": True}
    calls: list[str] = []

    class _Prov:
        name = "global"

        def fetch_quote(self, p, prefer_first=False):
            calls.append(p.yahoo)
            if state["dead"]:
                raise RuntimeError("dead")
            return _q(p.yahoo)

    monkeypatch.setattr(orch, "resolve", lambda t: _Prov())
    _patch_batch(monkeypatch, set())
    monkeypatch.setattr(orch, "_NEG_TTL", 0.05)

    q1, e1, _ = orch.get_quotes(["RECOVER"], use_ibkr=False, use_longport=False)
    assert not q1 and "RECOVER" in e1

    time.sleep(0.06)
    state["dead"] = False
    q2, e2, _ = orch.get_quotes(["RECOVER"], use_ibkr=False, use_longport=False)
    assert "RECOVER" in q2 and "RECOVER" not in e2
    assert "RECOVER" not in orch._neg_cache
    # 恢复后的行情应回写磁盘缓存
    assert "RECOVER" in get_cached(["RECOVER"])


class _FakeProvider:
    """全球域桩: SLOW 慢失败, 其余立即成功."""

    def __init__(self, calls: list[str], slow_symbols: set[str]):
        self.calls = calls
        self.slow_symbols = slow_symbols
        self.name = "global"

    def fetch_quote(self, p, prefer_first=False):
        self.calls.append(p.yahoo)
        if p.yahoo in self.slow_symbols:
            time.sleep(0.8)
            raise RuntimeError(f"{p.yahoo}: slow dead")
        return _q(p.yahoo)


def test_negative_cache_isolated_between_processes(monkeypatch):
    """负缓存只记失败: 成功代码不受同批次失败代码牵连."""
    calls: list[str] = []
    monkeypatch.setattr(orch, "resolve", lambda t: _FakeProvider(calls, {"BAD"}))
    _patch_batch(monkeypatch, set())

    quotes, errors, _ = orch.get_quotes(
        ["GOOD1", "GOOD2", "BAD"], use_ibkr=False, use_longport=False
    )
    assert set(quotes) == {"GOOD1", "GOOD2"}
    assert set(errors) == {"BAD"}
    assert orch._neg_cache.keys() == {"BAD"}
