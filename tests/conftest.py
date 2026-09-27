"""全局测试夹具: 防止测试打扰真实浏览器 / 真实网络.

1. webbrowser.open 一律 no-op —— CLI 的 --open 分支、长桥 OAuth 引导等路径
   即使未被 mock 也不会弹出浏览器标签页。
2. 非本地回路的 socket 连接/DNS 立即报错 —— 任何遗漏 mock 的测试一旦试图打
   真实网络就快速失败, 而不是时快时慢或无限挂起 (曾因 openbb anyio 死锁 +
   无 mock 的在线搜索把整套测试拖死超过 10 分钟)。
"""

from __future__ import annotations

import socket

import pytest

_LOCAL_HOSTS = {"127.0.0.1", "::1", "0.0.0.0", "localhost", "::ffff:127.0.0.1"}


def _is_local(host: object) -> bool:
    return isinstance(host, str) and host.lower() in _LOCAL_HOSTS


@pytest.fixture(autouse=True)
def _no_browser(monkeypatch):
    monkeypatch.setattr("webbrowser.open", lambda *a, **kw: True)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    real_connect = socket.socket.connect
    real_getaddrinfo = socket.getaddrinfo

    def guarded_connect(self, address):
        host = address[0] if isinstance(address, tuple) else None
        if _is_local(host):
            return real_connect(self, address)
        raise OSError(f"network blocked in tests: {address!r}")

    def guarded_getaddrinfo(host, *args, **kwargs):
        if _is_local(host):
            return real_getaddrinfo(host, *args, **kwargs)
        raise OSError(f"DNS blocked in tests: {host!r}")

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
