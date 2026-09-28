"""测试 harness 护栏自检: 禁网夹具 / network marker / 模块 import 纯度.

三层不变量:
1. 禁网夹具生效 (非本地回路 socket 立即报错), 且本地回路放行 (xdist 通信不受影响);
2. @pytest.mark.network marker 能放行禁网 (默认 -m "not network" 不跑这些用例,
   联调时用 `pytest -m network` 显式运行);
3. import 任何 tracker 子模块都不联网/不做重 IO —— 曾因 app.py 顶层拉行情把
   整套测试拖死, 本测试遍历全包, 任何模块级副作用立即撞红。
"""

from __future__ import annotations

import importlib
import pkgutil
import socket
import time

import pytest

import tracker

_REAL_GETADDRINFO = socket.getaddrinfo  # collection 期捕获, 夹具打桩前的真身


def test_external_dns_blocked():
    with pytest.raises(OSError, match="blocked in tests"):
        socket.getaddrinfo("finance.yahoo.com", 443)


def test_external_connect_blocked():
    s = socket.socket()
    try:
        with pytest.raises(OSError, match="network blocked in tests"):
            s.connect(("1.1.1.1", 443))
    finally:
        s.close()


def test_loopback_allowed():
    # xdist worker ↔ controller / 本地服务不受禁网影响
    assert socket.getaddrinfo("localhost", 0)


@pytest.mark.network
def test_network_marker_disables_guard():
    """marker 管路验证: 标记用例里禁网夹具应放行 (不打桩)."""
    assert socket.getaddrinfo is _REAL_GETADDRINFO


def test_all_tracker_modules_import_clean():
    """import 任何 tracker 子模块都不得联网/做重 IO (禁网夹具下网络调用秒挂)."""
    mods = [m.name for m in pkgutil.walk_packages(tracker.__path__, "tracker.")]
    assert mods
    t0 = time.monotonic()
    for name in mods:
        importlib.import_module(name)
    assert time.monotonic() - t0 < 15, "import 全包耗时长, 疑似有模块级重计算"
