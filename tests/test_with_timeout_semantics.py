"""with_timeout 参数求值语义的回归测试.

背景: 曾以为 `with_timeout(fn, timeout, slow_arg())` 会把参数求值算进超时预算,
并据此做过无效改动。实际 `with_timeout(fn, timeout, *args)` 的实参在调用它
之前就已在主线程求值完毕, daemon 线程只执行已解析的可调用对象。本测试锁定
该语义 (历史/汇率等取数路径依赖它把慢速参数置于计时区外)。
"""

from __future__ import annotations

import time

import pytest

from tracker.util import with_timeout


def test_args_are_evaluated_outside_the_timeout_budget():
    """慢速参数求值不计入超时: 3s 求值 + 0.1s 超时必须成功返回."""

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
