"""数据目录 / 运行目录 / 包内资源解析 (与安装位置无关).

背景: 旧版把 portfolio.json / watchlist.json / settings.json / var/ 全部锚定
仓库根 (Path(__file__).parent.parent)。源码树里跑没问题, 但 pip install 之后
__file__ 落在 site-packages —— 用户数据会写进包目录 (只读安装直接崩, 升级
还被冲掉), 故统一收敛到本模块:

数据目录 (portfolio.json 等用户数据 + var/ 运行产物) 解析优先级:
  1. 环境变量 TRACKER_HOME (Docker / 多实例显式指定, 最高优先)
  2. 源码树 / editable 安装: 仓库根 (与旧版行为一致, 老用户零迁移)
  3. 平台用户数据目录: Linux XDG (~/.local/share/tracker) /
     macOS (~/Library/Application Support/tracker) /
     Windows (%APPDATA%\\tracker)

包内资源 (配置模板 tracker/templates/, 前端资产 tracker/assets/) 始终从包内
读, 随 wheel 分发, 与数据目录分离 —— 模板用 `tracker init` 复制到数据目录。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: 包目录 (tracker/)
PACKAGE_DIR = Path(__file__).resolve().parent
#: 包内配置模板目录 (随 wheel 分发; tracker init 的来源)
TEMPLATES_DIR = PACKAGE_DIR / "templates"

# 仓库根判定标记: 任一存在即认为处于源码树 / editable 安装
_REPO_MARKERS = (".git", "pyproject.toml", "README.md", "requirements.txt")


def _legacy_root() -> Path:
    """包的上级目录: 源码树时是仓库根; pip 安装时是 site-packages。"""
    return PACKAGE_DIR.parent


def _looks_like_repo(root: Path) -> bool:
    return any((root / m).exists() for m in _REPO_MARKERS)


def _platform_data_dir() -> Path:
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")
        return Path(base) / "tracker"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "tracker"
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "tracker"


def data_home() -> Path:
    """用户数据目录 (portfolio.json 等用户数据 + var/ 运行产物)。"""
    env = os.environ.get("TRACKER_HOME")
    if env:
        return Path(env).expanduser()
    root = _legacy_root()
    if _looks_like_repo(root):
        return root  # 源码树 / editable: 数据留在仓库根 (与旧版一致)
    return _platform_data_dir()


def var_dir() -> Path:
    """运行产物目录 (行情缓存 SQLite / K线 HTML / 长桥 OAuth 状态文件)。"""
    return data_home() / "var"
