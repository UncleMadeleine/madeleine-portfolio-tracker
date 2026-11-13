"""全站视觉层: 深色玻璃拟态主题 (global CSS + 页头组件).

Streamlit 自带控件只能靠 ``.streamlit/config.toml`` 调色, 版式/卡片/光效
仍需一层 CSS。本模块把主题集中到一处, 各页面只需:

    from tracker.ui import theme
    theme.inject()                     # app.py 每次渲染调用一次
    theme.page_header("组合总览", eyebrow="PORTFOLIO")

配色参照现代行情面板: 近黑底 + 青/紫霓虹点缀 + 玻璃卡片; 涨跌语义色仍由
``tracker.ui.settings`` 决定 (红涨绿跌 / 绿涨红跌), 本模块不覆盖。

选择器尽量只用 Streamlit 的 ``data-testid`` / ``st-key-*`` 稳定钩子, 避免
依赖版本易变的 emotion 类名; 升级 Streamlit 后个别样式失效不会影响功能。
"""

from __future__ import annotations

import html
import json

import streamlit as st

from ..ctx_menu_js import CTX_MENU_KIT_JS
from ..paths import read_asset

# 与 .streamlit/config.toml 保持同步的主色 (CSS 内无法读 config, 故常量集中于此)

# 页面主区最大宽度 (wide 布局下避免超宽屏元素被拉散)
_MAX_WIDTH = "1560px"


# ---------- 全局右键菜单 ----------
# Streamlit 的表格 (Glide Data Grid) 与图表默认弹浏览器原生菜单, 在数据面板里
# 既没有「复制整行」也没有「查看 K 线」, 与桌面行情软件的习惯不符。这里用
# components.v2 挂一个 0 高度的全局脚本组件:
#   * 表格: 右键落在可见单元格上时, 用无障碍表格里同位置的 td 反查行列与文本;
#   * 图表 (Plotly): 右键落在画布上时提供重置视图 / 下载图片;
#   * 输入框、正文等仍保留浏览器原生菜单 (复制/粘贴/翻译是用户预期行为)。
# 菜单 DOM 追加到 document.body, 复用本模块的全局 CSS, 避免被 ShadowRoot 裁剪。
_TABLE_CONTEXT_MENU_JS = CTX_MENU_KIT_JS + read_asset("context_menu.js")


_TRACKPAD_GUARD_JS = read_asset("trackpad_guard.js")


_CONTEXT_MENU = None
_TRACKPAD_GUARD = None


def _context_menu():
    """惰性注册全局右键菜单组件, 并返回当前运行时可用的渲染器.

    不能在 import 时只注册一次: AppTest / 多 Runtime 场景下组件注册表可能在
    脚本运行前被清空, 之后挂载会报 "Component is not registered"。
    """
    global _CONTEXT_MENU
    from streamlit.components.v2 import get_bidi_component_manager

    if _CONTEXT_MENU is None or get_bidi_component_manager().get(
        "fp_context_menu"
    ) is None:
        _CONTEXT_MENU = st.components.v2.component(
            "fp_context_menu",
            js=_TABLE_CONTEXT_MENU_JS,
        )
    return _CONTEXT_MENU


def _trackpad_guard():
    """惰性注册触控板手势护栏组件 (Safari 兜底, 防止横滑误触后退)."""
    global _TRACKPAD_GUARD
    from streamlit.components.v2 import get_bidi_component_manager

    if _TRACKPAD_GUARD is None or get_bidi_component_manager().get(
        "fp_trackpad_guard"
    ) is None:
        _TRACKPAD_GUARD = st.components.v2.component(
            "fp_trackpad_guard",
            js=_TRACKPAD_GUARD_JS,
        )
    return _TRACKPAD_GUARD


# 右键菜单有副作用的操作 (删除) 经组件 trigger 回到 Python, 暂存在 session
# state, 由组合页在下一轮渲染时消费 (弹现有确认框 + 锁内读-改-写)。
CONTEXT_ACTION_KEY = "_ctx_action"
_CONTEXT_NONCE_KEY = "_ctx_action_nonce"


def _stash_context_action(result) -> None:
    """把组件 trigger 收到的右键操作写入 session_state (同一 trigger 只消费一次)."""
    raw = getattr(result, "ctx_action", None)
    if not raw:
        return
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return
    if not isinstance(payload, dict):
        return
    nonce = str(payload.get("nonce") or "")
    if nonce and st.session_state.get(_CONTEXT_NONCE_KEY) == nonce:
        return
    if nonce:
        st.session_state[_CONTEXT_NONCE_KEY] = nonce
    st.session_state[CONTEXT_ACTION_KEY] = payload


def inject() -> None:
    """注入全局主题样式 (幂等: 每次 rerun 重新渲染即可, 无状态副作用)."""
    st.html(f"<style>{_CSS}{_CONTEXT_MENU_CSS}</style>")
    # 0 高度脚本组件: 接管表格/图表的右键菜单, 自身不占版面
    result = _context_menu()(
        key="fp_context_menu",
        height=0,
        on_ctx_action_change=lambda: None,
    )
    _stash_context_action(result)
    # 触控板双指横滑不触发浏览器前进/后退 (尤其在侧边栏持仓表附近)
    _trackpad_guard()(key="fp_trackpad_guard", height=0)


def page_header(title: str, eyebrow: str = "", right_html: str = "") -> None:
    """渲染统一页头: 左侧眉题 + 大标题, 右侧可放状态胶囊.

    ``right_html`` 由调用方拼装 (时间戳/数据源等), 已是可信 HTML; 不处理
    用户输入, 避免把任意文本当标记渲染。
    """
    eyebrow_html = f'<div class="fp-eyebrow">{eyebrow}</div>' if eyebrow else ""
    right = f'<div class="fp-page-header-right">{right_html}</div>' if right_html else ""
    st.markdown(
        '<div class="fp-page-header">'
        f'<div class="fp-page-header-text">{eyebrow_html}'
        f'<h1 class="fp-page-title">{title}</h1></div>'
        f"{right}</div>",
        unsafe_allow_html=True,
    )


def chip(text: str, icon: str = "", title: str = "") -> str:
    """状态胶囊 HTML (配合 page_header 的 right_html 使用)."""
    icon_html = f'<span class="fp-chip-icon">{html.escape(icon)}</span>' if icon else ""
    title_attr = f' title="{html.escape(title)}"' if title else ""
    return (
        f'<span class="fp-chip"{title_attr}>'
        f"{icon_html}{html.escape(text)}</span>"
    )


# 图表分类配色: 青-紫-琥珀-绿-红-靛-灰, 与页面霓虹主色一致 (首色避开涨跌红绿)
CHART_COLORS = [
    "#00d4ff",
    "#bf5af2",
    "#ff9f0a",
    "#32d74b",
    "#ff453a",
    "#7c6cff",
    "#f7d774",
    "#94a3b8",
]


def style_chart(fig, *, transparent: bool = True):
    """给 Plotly 图套上统一深色外观 (透明背景可透出页面光晕)."""
    layout = dict(
        template="plotly_dark",
        colorway=CHART_COLORS,
        font=dict(
            family=_FONT_STACK,
            size=12,
            color="rgba(246, 247, 255, 0.78)",
        ),
        legend=dict(
            bgcolor="rgba(0, 0, 0, 0)",
            font=dict(size=11),
        ),
    )
    if transparent:
        layout["paper_bgcolor"] = "rgba(0, 0, 0, 0)"
        layout["plot_bgcolor"] = "rgba(0, 0, 0, 0)"
    # 只给已有标题的图补样式: 无标题时塞一个只有样式的 title 对象, plotly
    # 会把空 text 渲染成字面量 "undefined" (图中央凭空多一行字)。
    if fig.layout.title.text:
        layout["title"] = dict(
            text=fig.layout.title.text,
            font=dict(size=13, color="rgba(246, 247, 255, 0.86)"),
            x=0.5,
            xanchor="center",
        )
    fig.update_layout(**layout)
    return fig


_FONT_STACK = (
    "Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', "
    "'PingFang SC', 'Microsoft YaHei', sans-serif"
)


# 右键菜单样式独立成串; 菜单挂在 document.body 上, 选择器是全局作用域。
_CONTEXT_MENU_CSS = read_asset("context_menu.css")
_CSS = read_asset("theme.css")
