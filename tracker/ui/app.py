"""Streamlit 投资组合追踪入口: st.navigation 多页面路由.

运行:
  源码树   streamlit run tracker/ui/app.py (仓库根执行)
  已安装   tracker-ui (pip 安装后的控制台入口, 见本文件 cli_main)

每个页面有独立 URL (组合 / · K线 /kline · 指数 /index · 导入 /import ·
设置 /settings), 当前页由浏览器地址决定 —— 刷新、书签、前进后退都不丢页。
页面渲染逻辑在各 *_page.py 模块; 行情/汇率缓存在 portfolio_page.py。
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

# streamlit 以裸脚本执行本文件, 源码树运行时仓库根不在 sys.path — 手动引导,
# 否则 `import tracker` (及其导入的相对包路径) 失败。
# pip 安装后 tracker 已在 site-packages, 无需 (也不该) 再插路径。
_ROOT = Path(__file__).resolve().parent.parent.parent
if (_ROOT / "tracker" / "__init__.py").exists() and str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tracker import storage  # noqa: E402
from tracker.ui.import_page import render_import_page  # noqa: E402
from tracker.ui.index_page import render_index_page  # noqa: E402
from tracker.ui.kline_page import render_kline_page  # noqa: E402
from tracker.ui.portfolio_page import (  # noqa: E402
    _post_import,
    ensure_background_refresher,
    render_portfolio_page,
)
from tracker.ui.settings_page import render_settings_page  # noqa: E402


def main() -> None:
    st.set_page_config(page_title="投资组合追踪", page_icon="📈", layout="wide")
    # 进程级后台保温: 服务长期运行时持续刷新行情/汇率磁盘缓存 (没有任何页面
    # 打开也更新, 后来者打开即是新鲜数据)。幂等, 每次 rerun 重复调用无副作用
    ensure_background_refresher()

    def _run_kline() -> None:
        settings = storage.load_settings()
        render_kline_page(
            settings["prefer_akshare"],
            settings["use_ibkr"],
            bool(settings.get("use_longport")),
            settings["crypto_source"],
        )

    pages = [
        st.Page(
            render_portfolio_page,
            title="组合",
            icon=":material/pie_chart:",
            default=True,
        ),
        st.Page(
            _run_kline,
            title="K线",
            icon=":material/candlestick_chart:",
            url_path="kline",
        ),
        st.Page(
            render_index_page,
            title="指数K线",
            icon=":material/insights:",
            url_path="index",
        ),
        st.Page(
            lambda: render_import_page(on_saved=_post_import),
            title="导入",
            icon=":material/download:",
            url_path="import",
        ),
        st.Page(
            render_settings_page,
            title="设置",
            icon=":material/settings:",
            url_path="settings",
        ),
    ]
    page = st.navigation(pages, position="sidebar")
    page.run()


def cli_main() -> None:
    """tracker-ui 控制台脚本入口: 转交 `streamlit run <包内 app.py>`.

    用法与 `streamlit run tracker/ui/app.py` 等价, 额外参数原样透传:
        tracker-ui                                # 默认 http://localhost:8501
        tracker-ui --server.port 8600 --server.headless true

    已安装时: app.py 位于 site-packages, 用 importlib.resources 定位
    (as_file 保证 zip 安装也拿到真实路径); 同时把包内深色主题
    (templates/streamlit-config.toml) 落到 ~/.streamlit/config.toml
    (仅当用户还没有自己的配置时), 保持与源码树 .streamlit/config.toml 同款外观。
    """
    import shutil
    from importlib.resources import as_file, files

    from ..paths import TEMPLATES_DIR

    # 首次运行补齐主题配置 (用户已创建则不碰)
    try:
        user_cfg = Path.home() / ".streamlit" / "config.toml"
        packaged_cfg = TEMPLATES_DIR / "streamlit-config.toml"
        if packaged_cfg.exists() and not user_cfg.exists():
            user_cfg.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(packaged_cfg, user_cfg)
    except OSError:
        pass  # 主题是锦上添花, 失败不阻塞启动

    try:
        from streamlit.web import cli as st_cli
    except ImportError:  # 旧版本 streamlit
        from streamlit import cli as st_cli

    with as_file(files(__package__).joinpath("app.py")) as app_path:
        sys.argv = ["streamlit", "run", str(app_path), *sys.argv[1:]]
        st_cli.main()


if __name__ == "__main__":
    main()
