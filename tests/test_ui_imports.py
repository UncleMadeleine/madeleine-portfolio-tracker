"""UI 包导入完整性 (静态检查).

重构把用例层搬到 services/ 后, 页面里遗留的失效 import 会让整个 Streamlit 应用
在启动时 ImportError 直接白屏 —— 这里静态校验每个 `from tracker.* import X`
的 X 都真实存在 (不导入 app.py 本身, 避免执行整页脚本).
"""
from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

_UI_DIR = Path(__file__).resolve().parent.parent / "tracker" / "ui"


def _tracker_imports(path: Path) -> list[tuple[str, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.startswith("tracker")
        ):
            out.extend((node.module, alias.name) for alias in node.names)
    return out


@pytest.mark.parametrize("path", sorted(_UI_DIR.glob("*.py")), ids=lambda p: p.name)
def test_ui_imports_resolve(path):
    missing = []
    for module, name in _tracker_imports(path):
        if hasattr(importlib.import_module(module), name):
            continue
        try:  # `from tracker import wallet` 形式: 名字是子模块, 尚未绑定到包上
            importlib.import_module(f"{module}.{name}")
        except ImportError:
            missing.append(f"{module}.{name}")
    assert missing == [], f"{path.name} 引用了不存在的符号: {missing}"
