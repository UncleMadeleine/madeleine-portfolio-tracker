"""with_timeout 参数求值语义 + OpenBB 单例的回归测试.

背景: 曾以为 `with_timeout(_obb().equity.price.quote, _QUOTE_TIMEOUT, ...)`
会把 `_obb()` 的冷启动 (本机实测 5.5s) 算进 5.0s 超时预算, 导致首次 quote
必然超时, 并据此把 `_obb()` 移出 with_timeout —— 那是错的。

`with_timeout(fn, timeout, *args, **kwargs)` 的实参在调用它之前就已在主线程
求值完毕, daemon 线程只执行已解析的可调用对象。所以 `_obb()` 无论写在哪里
都在计时区外, 那类改动语义上无效。

本测试锁定该语义, 并锁定单例本身的真实收益 (进程内只导入一次)。
"""

from __future__ import annotations

import threading
import time

import pytest

from tracker.providers import global_stocks as gs
from tracker.util import with_timeout


# ---------- with_timeout 参数求值在计时区外 ----------


def test_args_are_evaluated_outside_the_timeout_budget():
    """慢速参数求值不计入超时: 3s 求值 + 0.1s 超时必须成功返回.

    这是「把 _obb() 移出 with_timeout 无效」的根本原因: 参数本来就在外面。
    """

    def slow_arg():
        time.sleep(3.0)
        return "evaluated"

    def fn(x):
        return f"got:{x}"

    t0 = time.time()
    result = with_timeout(fn, 0.1, slow_arg())
    elapsed = time.time() - t0

    assert result == "got:evaluated"
    # 若参数求值在计时区内, 0.1s 就会抛 TimeoutError; 实测走满了 3s
    assert elapsed > 2.5


def test_timeout_still_fires_when_the_call_itself_is_slow():
    """反向验证: 慢在 fn 内部时超时仍然生效 (排除上面的测试是假阳性)."""

    def slow_fn():
        time.sleep(5.0)
        return "too_late"

    t0 = time.time()
    with pytest.raises(Exception):
        with_timeout(slow_fn, 0.1)
    assert time.time() - t0 < 2.0


# ---------- OpenBB 单例 ----------


@pytest.fixture(autouse=True)
def _reset_singleton():
    gs._obb_singleton = None
    yield
    gs._obb_singleton = None


def test_obb_singleton_imports_once(monkeypatch):
    """并发调用只触发一次导入, 且后续返回同一对象.

    不 mock `__import__` (对 `from openbb import obb` 的 IMPORT_FROM 不可靠),
    改为把已导入的 openbb 模块对象直接注入 sys.modules, 用真实 import 路径走一遍。
    """
    import sys
    import types

    import_count = {"n": 0}
    fake_obb = types.SimpleNamespace(marker="fake-obb")

    # 真实 __import__ 会命中 sys.modules 缓存; 这里用一个自定义模块类计数触达次数
    class CountingModule(types.ModuleType):
        def __getattr__(self, name):
            if name == "obb":
                import_count["n"] += 1
                return fake_obb
            raise AttributeError(name)

    counting = CountingModule("openbb")
    monkeypatch.setitem(sys.modules, "openbb", counting)

    results = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        results.append(gs._obb())

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert import_count["n"] == 1, "双检锁下并发首次调用只应导入一次"
    assert results == [fake_obb] * 8, "所有调用方拿到同一实例"


def test_obb_singleton_does_not_cache_failure(monkeypatch):
    """导入失败不缓存: 单例保持 None, 下次调用重试."""
    import sys
    import types

    calls = {"n": 0}

    class FailingModule(types.ModuleType):
        def __getattr__(self, name):
            if name == "obb":
                calls["n"] += 1
                raise ImportError("openbb 未安装")
            raise AttributeError(name)

    monkeypatch.setitem(sys.modules, "openbb", FailingModule("openbb"))

    for _ in range(2):
        with pytest.raises(ImportError):
            gs._obb()

    assert gs._obb_singleton is None
    assert calls["n"] == 2, "失败不得缓存, 每次都应重新尝试"
