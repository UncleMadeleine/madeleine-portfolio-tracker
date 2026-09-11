"""Streamlit 多页面入口: python -m streamlit run run_app.py.

两个平级页面:
- 📈 投资组合 (app.py): 持仓 + 自选提醒 + 资产配置 + 走势对比
- 🕯 K线 (kline_app.py): 任意代码实时查询, 输入后才取数
"""
import streamlit as st

pages = [
    st.Page("app.py", title="投资组合", icon="📈", default=True),
    st.Page("kline_app.py", title="K线", icon="🕯"),
]
st.navigation(pages).run()
