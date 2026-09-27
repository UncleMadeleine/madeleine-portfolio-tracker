"""全局测试夹具: 防止测试打扰真实浏览器.

任何测试中调用 webbrowser.open 一律 no-op —— CLI 的 --open 分支、长桥 OAuth
引导等路径即使未被 mock 也不会弹出浏览器标签页。
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_browser(monkeypatch):
    monkeypatch.setattr("webbrowser.open", lambda *a, **kw: True)
