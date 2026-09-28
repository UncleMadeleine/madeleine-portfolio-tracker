"""Streamlit 页面包: 组合 / K线 / 指数K线 / 导入 / 设置 五页, st.navigation 路由.

运行入口: python -m streamlit run tracker/ui/app.py (每页独立 URL, 刷新不丢页)
页面模块间用相对导入; app.py 是 streamlit 以裸脚本执行的入口,
在其中做仓库根 sys.path 引导后使用绝对导入。
"""

from . import settings  # noqa: F401 - 供 `from tracker.ui import settings` 使用
