"""Streamlit「K线」独立页面入口 (python -m streamlit run kline_app.py).

任意代码实时查询, 不预加载; 与主页面 app.py 平级。
"""
import streamlit as st

import kline_page

st.set_page_config(page_title="K线查询", page_icon="🕯", layout="wide")

st.title("🕯 K线")
st.caption(
    "独立 K线查询: 输入任意代码即可 (不限于持仓/自选)。"
    "数据在点击「查询」后才实时获取, 页面启动零预加载。"
)
kline_page.render_kline_controls(prefer_akshare=False)
