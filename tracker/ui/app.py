"""Streamlit 投资组合追踪入口: st.navigation 多页面路由.

运行: python -m streamlit run tracker/ui/app.py (仓库根执行)。

每个页面有独立 URL (组合 / · K线 /kline · 指数 /index · 导入 /import ·
设置 /settings), 当前页由浏览器地址决定 —— 刷新、书签、前进后退都不丢页。
页面渲染逻辑在各 *_page.py 模块; 行情/汇率缓存在 portfolio_page.py。
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

# streamlit 以裸脚本执行本文件, 此时仓库根不在 sys.path — 手动引导,
# 否则 `import tracker` (及其导入的相对包路径) 失败。
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tracker import storage  # noqa: E402
from tracker.ui.import_page import render_import_page  # noqa: E402
from tracker.ui.index_page import render_index_page  # noqa: E402
from tracker.ui.kline_page import render_kline_page  # noqa: E402
from tracker.ui.portfolio_page import _post_import, render_portfolio_page  # noqa: E402
from tracker.ui.settings_page import render_settings_page  # noqa: E402


def main() -> None:
    st.set_page_config(page_title="投资组合追踪", page_icon="📈", layout="wide")

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


if __name__ == "__main__":
    main()
