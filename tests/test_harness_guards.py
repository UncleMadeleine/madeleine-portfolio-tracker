"""测试 harness 护栏自检: 禁网夹具真的生效.

任何人新建测试时若忘打桩而打真实网络, 会在这里以外的地方快速失败而不是
挂起; 本文件保证护栏本身不被误删/失效 (本地回路放行, xdist 通信不受影响)。
"""

from __future__ import annotations

import socket

import pytest


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
