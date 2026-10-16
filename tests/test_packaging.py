"""打包分发相关回归: 数据目录解析 / 配置模板落地 / charting 无 plotly 可导入.

覆盖 tracker/paths.py 的三级解析、tracker init 子命令、pyproject.toml 的
console scripts 与 extras、版本单一来源, 以及 plotly 惰性化后核心依赖
(openbb/akshare/pandas) 用户可正常 import charting (K线 --json 路径)。
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from tracker import paths


class TestDataHomeResolution:
    """数据目录三级优先: TRACKER_HOME > 仓库根 (源码树) > 平台用户目录."""

    def test_env_override_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRACKER_HOME", str(tmp_path / "custom"))
        assert paths.data_home() == tmp_path / "custom"
        assert paths.var_dir() == tmp_path / "custom" / "var"

    def test_repo_root_in_source_tree(self, monkeypatch):
        # 测试进程跑在仓库里 (.git/pyproject.toml 在): 数据目录 = 仓库根,
        # 与旧版 (Path(__file__).parent.parent) 行为一致, 老用户零迁移
        monkeypatch.delenv("TRACKER_HOME", raising=False)
        assert paths.data_home() == paths._legacy_root()

    def test_platform_dir_when_installed(self, tmp_path, monkeypatch):
        # 模拟 pip 安装: 包上级是 site-packages (无仓库标记) → 平台用户目录
        monkeypatch.delenv("TRACKER_HOME", raising=False)
        monkeypatch.setattr(paths, "_legacy_root", lambda: tmp_path / "site-packages")
        monkeypatch.setattr(paths, "_platform_data_dir", lambda: tmp_path / "platform")
        assert paths.data_home() == tmp_path / "platform"

    def test_templates_are_package_resources(self):
        # 模板必须随包分发 (wheel 内 tracker/templates/), 安装后才能 tracker init
        assert paths.TEMPLATES_DIR == paths.PACKAGE_DIR / "templates"
        for name in ("portfolio", "watchlist", "settings", "ibkr", "longport"):
            tpl = paths.TEMPLATES_DIR / f"{name}.example.json"
            assert tpl.exists(), tpl
            assert isinstance(json.loads(tpl.read_text(encoding="utf-8")), dict)


class TestStorageFollowsDataHome:
    """storage 常量在 import 时按 data_home() 解析; 重载后跟随 TRACKER_HOME."""

    _NAMES = ("PORTFOLIO_PATH", "WATCHLIST_PATH", "SETTINGS_PATH")

    def test_constants_resolve_from_env(self, tmp_path, monkeypatch):
        import importlib

        import tracker.storage as storage

        try:
            monkeypatch.setenv("TRACKER_HOME", str(tmp_path))
            importlib.reload(storage)
            assert storage.PORTFOLIO_PATH == tmp_path / "portfolio.json"
            assert storage.WATCHLIST_PATH == tmp_path / "watchlist.json"
            assert storage.SETTINGS_PATH == tmp_path / "settings.json"
        finally:
            # env 还原后再重载, 避免把 tmp 路径泄漏给后续测试
            monkeypatch.undo()
            importlib.reload(storage)
            home = paths.data_home()
            assert storage.PORTFOLIO_PATH == home / "portfolio.json"

    def test_var_artifacts_follow_tracker_home(self, tmp_path):
        # 全新进程里 import: CLI 的 VAR_DIR 常量按 TRACKER_HOME 解析 (子进程,
        # 避开本进程 conftest 的隔离重绑, 验证的是真实 import 期行为)
        code = (
            "import os, sys\n"
            f"os.environ['TRACKER_HOME'] = {str(tmp_path)!r}\n"
            "from tracker.cli import compare, index_kline, kline\n"
            "from tracker import cache, longport_oauth\n"
            "for m in (kline, compare, index_kline):\n"
            "    assert str(m.VAR_DIR) == os.path.join(sys.argv[1], 'var'), m.VAR_DIR\n"
            "assert str(cache.CACHE_DB) == os.path.join(sys.argv[1], 'var', 'quotes_cache.db')\n"
            "assert longport_oauth._STATE_PATH.name == 'longport_oauth_state.json'\n"
        )
        out = subprocess.run(
            [sys.executable, "-c", code, str(tmp_path)],
            capture_output=True,
            text=True,
            cwd=str(paths._legacy_root()),
            timeout=120,
        )
        assert out.returncode == 0, out.stderr


class TestInitCommand:
    """tracker init: 包内模板 → 数据目录 (幂等, --force 覆盖, --json)."""

    def _run(self, argv):
        from tracker.cli import build_parser

        args = build_parser().parse_args(argv)
        args.func(args)

    def test_init_copies_templates(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setenv("TRACKER_HOME", str(home))
        self._run(["init"])
        for name in ("portfolio", "watchlist", "settings", "ibkr", "longport"):
            dst = home / f"{name}.json"
            assert dst.exists()
            assert dst.read_text(encoding="utf-8") == (
                paths.TEMPLATES_DIR / f"{name}.example.json"
            ).read_text(encoding="utf-8")

    def test_init_idempotent_and_force(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setenv("TRACKER_HOME", str(home))
        self._run(["init"])
        (home / "portfolio.json").write_text('{"custom": 1}', encoding="utf-8")
        self._run(["init"])  # 已存在 → 跳过, 不覆盖用户数据
        assert json.loads((home / "portfolio.json").read_text(encoding="utf-8")) == {
            "custom": 1
        }
        self._run(["init", "--force"])  # --force → 模板覆盖
        assert "holdings" in json.loads(
            (home / "portfolio.json").read_text(encoding="utf-8")
        )

    def test_init_json_output(self, tmp_path, monkeypatch, capsys):
        home = tmp_path / "home"
        monkeypatch.setenv("TRACKER_HOME", str(home))
        self._run(["init", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is True
        assert payload["data_home"] == str(home)
        assert len(payload["created"]) == 5


class TestVersionSingleSource:
    """版本号单一来源: tracker.__version__ (pyproject 动态读它)."""

    def test_cli_version_matches_package(self):
        import tracker
        from tracker.cli import VERSION
        from tracker.cli import _common

        assert VERSION == tracker.__version__
        assert _common.VERSION == tracker.__version__

    def test_pyproject_declares_packaging_contract(self):
        tomllib = pytest.importorskip("tomllib")
        data = tomllib.loads(
            (paths._legacy_root() / "pyproject.toml").read_text(encoding="utf-8")
        )
        project = data["project"]
        assert project["name"] == "madeleine-tracker"
        assert project["dynamic"] == ["version"]
        assert data["tool"]["setuptools"]["dynamic"]["version"] == {
            "attr": "tracker.__version__"
        }
        # console scripts: tracker (CLI) + tracker-ui (Streamlit)
        scripts = project["scripts"]
        assert scripts["tracker"] == "tracker.cli:main"
        assert scripts["tracker-ui"] == "tracker.ui.app:cli_main"
        # 依赖拆 extras: 核心不拖 streamlit/plotly/ib_async/longport
        assert "streamlit" not in project["dependencies"]
        assert "plotly" not in project["dependencies"]
        for extra in ("ui", "ibkr", "longport", "dev"):
            assert extra in project["optional-dependencies"]
        # 包内资源随 wheel 分发
        pkg_data = data["tool"]["setuptools"]["package-data"]["tracker"]
        assert "templates/*.json" in pkg_data
        assert "assets/*.js" in pkg_data


class TestChartingWithoutPlotly:
    """plotly 惰性化: 未装 plotly 也能 import charting (K线 --json / 页面常量)."""

    def test_import_and_asset_without_plotly(self):
        code = (
            "import sys\n"
            "sys.modules['plotly'] = None\n"
            "sys.modules['plotly.graph_objects'] = None\n"
            "sys.modules['plotly.subplots'] = None\n"
            "import tracker.charting as c\n"
            "assert 'plotly' not in sys.modules or sys.modules['plotly'] is None\n"
            "src = c._lwc_source()\n"
            "assert len(src) > 10000  # vendored lightweight-charts 从包内 assets/ 读到\n"
        )
        out = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            cwd=str(paths._legacy_root()),
            timeout=120,
        )
        assert out.returncode == 0, out.stderr

    def test_ui_entry_points_importable(self):
        from tracker.ui.app import cli_main, main

        assert callable(main) and callable(cli_main)
