"""全局测试夹具: 防止测试打扰真实浏览器 / 真实网络.

1. webbrowser.open 一律 no-op —— CLI 的 --open 分支、长桥 OAuth 引导等路径
   即使未被 mock 也不会弹出浏览器标签页。
2. 非本地回路的 socket 连接/DNS 立即报错 —— 任何遗漏 mock 的测试一旦试图打
   真实网络就快速失败, 而不是时快时慢或无限挂起 (曾因 openbb anyio 死锁 +
   无 mock 的在线搜索把整套测试拖死超过 10 分钟)。@pytest.mark.network
   标记的用例显式放行 (默认 -m "not network" 不跑, 见 pytest.ini)。
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
def _no_network(monkeypatch, request):
    if "network" in request.keywords:  # 显式联调用例 (@pytest.mark.network) 放行
        return
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


@pytest.fixture(autouse=True)
def _isolated_data_home(tmp_path, monkeypatch):
    """数据隔离: 数据目录 / 磁盘缓存 / HTML 输出目录全部指向 tmp_path.

    storage/cache/cli 的路径常量是 import 时解析的 (源码树 → 仓库根),
    不隔离的话测试会读写开发者真实的 portfolio.json / quotes_cache.db。
    TRACKER_HOME 一并置位, 覆盖测试期间新 import 模块的解析;
    argparse 默认值在 build_parser() 时才读模块全局, 也要改。
    """
    home = tmp_path / "tracker-home"
    var = home / "var"
    monkeypatch.setenv("TRACKER_HOME", str(home))

    import tracker.cache as cache_mod
    import tracker.cli as cli_pkg
    import tracker.cli.compare as cli_compare
    import tracker.cli.index_kline as cli_index_kline
    import tracker.cli.kline as cli_kline
    import tracker.storage as storage
    import tracker.watchlist as watchlist_mod
    from tracker import longport_oauth
    from tracker.ui import longport_login

    monkeypatch.setattr(storage, "PORTFOLIO_PATH", home / "portfolio.json")
    monkeypatch.setattr(storage, "WATCHLIST_PATH", home / "watchlist.json")
    monkeypatch.setattr(storage, "SETTINGS_PATH", home / "settings.json")
    monkeypatch.setattr(watchlist_mod, "DEFAULT_WATCHLIST", home / "watchlist.json")
    monkeypatch.setattr(cache_mod, "CACHE_DB", var / "quotes_cache.db")
    monkeypatch.setattr(cli_kline, "VAR_DIR", var)
    monkeypatch.setattr(cli_compare, "VAR_DIR", var)
    monkeypatch.setattr(cli_index_kline, "VAR_DIR", var)
    monkeypatch.setattr(longport_oauth, "_STATE_PATH", var / "longport_oauth_state.json")
    monkeypatch.setattr(
        longport_login, "_STATE_PATH", var / "longport_oauth_state.json"
    )
    # argparse 默认值 (--portfolio / --watchlist-file / --file)
    monkeypatch.setattr(cli_pkg, "DEFAULT_PORTFOLIO", home / "portfolio.json")
    monkeypatch.setattr(cli_pkg, "DEFAULT_WATCHLIST", home / "watchlist.json")
