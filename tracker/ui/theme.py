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

import streamlit as st

# 与 .streamlit/config.toml 保持同步的主色 (CSS 内无法读 config, 故常量集中于此)
BG = "#0a0a0f"
SURFACE = "#141428"
PRIMARY = "#00d4ff"
ACCENT = "#bf5af2"
UP_CN = "#ff453a"
DOWN_CN = "#32d74b"

# 页面主区最大宽度 (wide 布局下避免超宽屏元素被拉散)
_MAX_WIDTH = "1560px"


def inject() -> None:
    """注入全局主题样式 (幂等: 每次 rerun 重新渲染即可, 无状态副作用)."""
    st.html(f"<style>{_CSS}</style>")


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


_CSS = f"""
:root {{
  --fp-bg: {BG};
  --fp-surface: {SURFACE};
  --fp-border: rgba(255, 255, 255, 0.09);
  --fp-border-strong: rgba(255, 255, 255, 0.16);
  --fp-text: #f6f7ff;
  --fp-text-2: rgba(246, 247, 255, 0.66);
  --fp-text-3: rgba(246, 247, 255, 0.4);
  --fp-cyan: {PRIMARY};
  --fp-purple: {ACCENT};
  --fp-amber: #ff9f0a;
  --fp-radius: 16px;
  --fp-radius-sm: 11px;
  --fp-mono: "JetBrains Mono", "SF Mono", "Cascadia Code", ui-monospace,
    "SFMono-Regular", Menlo, Consolas, monospace;
  --fp-sans: "Inter", "SF Pro Text", -apple-system, BlinkMacSystemFont,
    "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
}}

/* ---------- 背景: 近黑底 + 两团低透明度霓虹光晕 ---------- */
html, body, .stApp {{
  background-color: var(--fp-bg) !important;
  color: var(--fp-text);
}}
.stApp {{
  background-image:
    radial-gradient(1100px 620px at 8% -12%, rgba(0, 212, 255, 0.11), transparent 62%),
    radial-gradient(900px 540px at 96% -4%, rgba(191, 90, 242, 0.12), transparent 58%),
    radial-gradient(700px 700px at 50% 115%, rgba(0, 212, 255, 0.045), transparent 65%),
    linear-gradient(180deg, #0a0a10 0%, #0b0b15 48%, #08080e 100%) !important;
  background-attachment: fixed;
}}
[data-testid="stAppViewContainer"] {{ background: transparent; }}
[data-testid="stDecoration"] {{ display: none; }}
[data-testid="stAppViewContainer"] > .main {{ background: transparent; }}

/* ---------- 主区容器: 统一留白, 超宽屏居中 ---------- */
[data-testid="stMainBlockContainer"] {{
  max-width: {_MAX_WIDTH};
  margin: 0 auto;
  padding-top: 4.75rem;
  padding-bottom: 4rem;
}}
[data-testid="stVerticalBlock"] {{ gap: 0.9rem; }}

/* ---------- 顶部工具条: 玻璃 + 细边 ---------- */
[data-testid="stHeader"] {{
  background: linear-gradient(180deg, rgba(12, 12, 20, 0.82), rgba(10, 10, 16, 0.55)) !important;
  backdrop-filter: blur(20px) saturate(160%);
  -webkit-backdrop-filter: blur(20px) saturate(160%);
  border-bottom: 1px solid rgba(255, 255, 255, 0.07);
}}
[data-testid="stHeader"]::after {{
  content: "";
  position: absolute;
  left: 0;
  right: 0;
  bottom: -1px;
  height: 1px;
  background: linear-gradient(90deg, transparent, rgba(0, 212, 255, 0.45), rgba(191, 90, 242, 0.35), transparent);
  opacity: 0.65;
}}

/* ---------- 侧边栏: 玻璃面板 + 胶囊导航 ---------- */
[data-testid="stSidebar"] {{
  background:
    radial-gradient(600px 320px at 0% 0%, rgba(0, 212, 255, 0.07), transparent 60%),
    linear-gradient(180deg, rgba(18, 18, 32, 0.92), rgba(9, 9, 15, 0.97)) !important;
  border-right: 1px solid var(--fp-border);
  backdrop-filter: blur(24px) saturate(150%);
}}
[data-testid="stSidebarContent"] {{ padding-top: 0.35rem; }}
[data-testid="stSidebarNav"] {{ padding: 0.15rem 0.55rem 0.35rem; }}
[data-testid="stSidebarNavLinkContainer"] {{ border-radius: 10px; }}
[data-testid="stSidebarNavLink"] {{
  border-radius: 10px;
  padding-top: 0.42rem;
  padding-bottom: 0.42rem;
  color: var(--fp-text-2);
  transition: background 160ms ease, color 160ms ease, transform 160ms ease;
}}
[data-testid="stSidebarNavLink"]:hover {{
  background: rgba(255, 255, 255, 0.055);
  color: var(--fp-text);
}}
[data-testid="stSidebarNavLink"][aria-current="page"] {{
  background: linear-gradient(135deg, rgba(0, 212, 255, 0.16), rgba(191, 90, 242, 0.13));
  box-shadow: inset 0 0 0 1px rgba(0, 212, 255, 0.24);
  color: #ffffff;
}}
[data-testid="stSidebarHeader"] {{ padding-bottom: 0.35rem; }}
[data-testid="stSidebarUserContent"] {{ padding-top: 0.35rem; }}
[data-testid="stSidebar"] hr {{ border-color: var(--fp-border); }}

/* ---------- 标题 / 文本 ---------- */
h1, h2, h3, h4 {{
  font-family: var(--fp-sans);
  letter-spacing: -0.018em;
  color: var(--fp-text);
}}
[data-testid="stMarkdownContainer"] p {{ color: rgba(246, 247, 255, 0.82); }}
[data-testid="stCaptionContainer"] p,
[data-testid="stCaptionContainer"] {{
  color: var(--fp-text-3);
  font-size: 0.78rem;
  line-height: 1.55;
}}
[data-testid="stMarkdownContainer"] h3 {{
  font-size: 1.22rem;
  font-weight: 650;
  margin-bottom: 0.35rem;
}}

/* ---------- 页头 ---------- */
.fp-page-header {{
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 1rem;
  flex-wrap: wrap;
  margin: 0.15rem 0 0.35rem;
}}
.fp-eyebrow {{
  font-family: var(--fp-mono);
  font-size: 0.64rem;
  font-weight: 600;
  letter-spacing: 0.22em;
  text-transform: uppercase;
  color: rgba(0, 212, 255, 0.72);
  margin-bottom: 0.3rem;
}}
.fp-page-title {{
  margin: 0;
  font-family: var(--fp-sans);
  font-size: clamp(1.5rem, 1.15rem + 1.4vw, 2.05rem);
  font-weight: 720;
  letter-spacing: -0.03em;
  line-height: 1.12;
  background: linear-gradient(100deg, #ffffff 0%, #d9e6ff 45%, #a9d9ff 78%, #e3c7ff 100%);
  -webkit-background-clip: text;
  background-clip: text;
  -webkit-text-fill-color: transparent;
}}
.fp-page-header-right {{ display: flex; align-items: center; gap: 0.5rem; }}
/* 查看范围下拉: 不占满整行, 留出呼吸感 (窄屏仍全宽) */
.st-key-watch_scope {{ max-width: 380px; }}
.fp-chip {{
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  padding: 0.34rem 0.72rem;
  border-radius: 999px;
  border: 1px solid var(--fp-border);
  background: rgba(255, 255, 255, 0.045);
  color: var(--fp-text-2);
  font-size: 0.75rem;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
  backdrop-filter: blur(12px);
}}
.fp-chip::before {{
  content: "";
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--fp-cyan);
  box-shadow: 0 0 8px rgba(0, 212, 255, 0.9);
}}
.fp-chip-icon {{ color: var(--fp-cyan); }}

/* ---------- 卡片: 所有 border=True 容器 + metric ---------- */
[data-testid="stVerticalBlockBorderWrapper"] {{
  background:
    linear-gradient(135deg, rgba(255, 255, 255, 0.055), rgba(0, 212, 255, 0.018) 48%, rgba(191, 90, 242, 0.032));
  border: 1px solid var(--fp-border) !important;
  border-radius: var(--fp-radius) !important;
  box-shadow:
    0 18px 40px -22px rgba(0, 0, 0, 0.85),
    inset 0 1px 0 rgba(255, 255, 255, 0.055);
  backdrop-filter: blur(18px) saturate(140%);
  -webkit-backdrop-filter: blur(18px) saturate(140%);
  transition: border-color 200ms ease, box-shadow 200ms ease;
}}
[data-testid="stVerticalBlockBorderWrapper"]:hover {{
  border-color: rgba(0, 212, 255, 0.2) !important;
}}
[data-testid="stVerticalBlockBorderWrapper"] [data-testid="stVerticalBlockBorderWrapper"] {{
  background: rgba(255, 255, 255, 0.025);
  box-shadow: none;
}}

[data-testid="stMetric"] {{
  position: relative;
  overflow: hidden;
  padding: 1rem 1.15rem 0.95rem;
  border-radius: var(--fp-radius);
  border: 1px solid var(--fp-border);
  background:
    radial-gradient(120% 90% at 0% 0%, rgba(0, 212, 255, 0.075), transparent 55%),
    linear-gradient(150deg, rgba(255, 255, 255, 0.055), rgba(20, 20, 40, 0.5));
  box-shadow:
    0 18px 40px -24px rgba(0, 0, 0, 0.9),
    inset 0 1px 0 rgba(255, 255, 255, 0.06);
  backdrop-filter: blur(16px);
  transition: transform 220ms ease, border-color 220ms ease, box-shadow 220ms ease;
}}
[data-testid="stMetric"]::after {{
  content: "";
  position: absolute;
  top: 0;
  left: 12%;
  right: 12%;
  height: 1.5px;
  background: linear-gradient(90deg, transparent, rgba(0, 212, 255, 0.75), rgba(191, 90, 242, 0.55), transparent);
  opacity: 0.8;
}}
[data-testid="stMetric"]:hover {{
  transform: translateY(-2px);
  border-color: rgba(0, 212, 255, 0.3);
  box-shadow:
    0 26px 48px -26px rgba(0, 0, 0, 0.95),
    0 0 0 1px rgba(0, 212, 255, 0.12),
    inset 0 1px 0 rgba(255, 255, 255, 0.08);
}}
[data-testid="stMetricLabel"] {{
  color: var(--fp-text-2) !important;
  font-size: 0.74rem !important;
  font-weight: 550;
  letter-spacing: 0.06em;
}}
[data-testid="stMetricValue"] {{
  font-family: var(--fp-mono);
  font-size: clamp(1.35rem, 1.05rem + 0.9vw, 1.85rem) !important;
  font-weight: 700;
  letter-spacing: -0.035em;
  color: #ffffff;
  font-variant-numeric: tabular-nums;
}}
[data-testid="stMetricDelta"] {{
  font-family: var(--fp-mono);
  font-size: 0.76rem !important;
  font-variant-numeric: tabular-nums;
}}
/* 首页 KPI 首卡 (总市值): 青色描边更醒目 */
.st-key-fp_kpi [data-testid="stMetric"]:first-child {{
  border-color: rgba(0, 212, 255, 0.28);
  background:
    radial-gradient(130% 100% at 0% 0%, rgba(0, 212, 255, 0.14), transparent 58%),
    linear-gradient(150deg, rgba(255, 255, 255, 0.06), rgba(16, 24, 42, 0.55));
}}
.st-key-fp_kpi [data-testid="stMetric"]:first-child [data-testid="stMetricValue"] {{
  font-size: clamp(1.55rem, 1.15rem + 1.15vw, 2.15rem) !important;
}}

/* ---------- 标签页: 胶囊轨道 (兼容 1.65 react-aria 与旧 baseweb 结构) ---------- */
[data-testid="stTabs"] [role="tablist"],
[data-testid="stTabs"] [data-baseweb="tab-list"] {{
  gap: 0.3rem;
  padding: 0.3rem;
  width: fit-content;
  max-width: 100%;
  border-radius: 13px;
  border: 1px solid var(--fp-border);
  background: rgba(255, 255, 255, 0.03);
  backdrop-filter: blur(14px);
}}
[data-testid="stTab"],
[data-testid="stTabs"] [data-baseweb="tab"] {{
  border-radius: 9px;
  padding: 0.38rem 0.95rem;
  color: var(--fp-text-2);
  font-size: 0.84rem;
  transition: background 160ms ease, color 160ms ease;
}}
[data-testid="stTab"]:hover,
[data-testid="stTabs"] [data-baseweb="tab"]:hover {{
  background: rgba(255, 255, 255, 0.05);
  color: var(--fp-text);
}}
[data-testid="stTab"][data-selected="true"],
[data-testid="stTabs"] [aria-selected="true"][data-baseweb="tab"] {{
  background: linear-gradient(135deg, rgba(0, 212, 255, 0.18), rgba(191, 90, 242, 0.16));
  color: #ffffff;
  box-shadow: inset 0 0 0 1px rgba(0, 212, 255, 0.22);
}}
[data-testid="stTabs"] .react-aria-SelectionIndicator,
[data-testid="stTabs"] [data-baseweb="tab-highlight"],
[data-testid="stTabs"] [data-baseweb="tab-border"] {{
  display: none;
}}
[data-testid="stTabPanel"],
[data-testid="stTabs"] [role="tabpanel"] {{ padding-top: 0.9rem; }}

/* ---------- 按钮 ---------- */
.stButton > button,
[data-testid="stDownloadButton"] > button,
[data-testid="stFormSubmitButton"] > button {{
  border-radius: 10px;
  border: 1px solid var(--fp-border-strong);
  background: rgba(255, 255, 255, 0.045);
  color: var(--fp-text);
  font-weight: 550;
  transition: transform 150ms ease, border-color 150ms ease, box-shadow 150ms ease,
    background 150ms ease;
}}
.stButton > button:hover,
[data-testid="stDownloadButton"] > button:hover,
[data-testid="stFormSubmitButton"] > button:hover {{
  border-color: rgba(0, 212, 255, 0.5);
  background: rgba(0, 212, 255, 0.08);
  box-shadow: 0 0 18px -6px rgba(0, 212, 255, 0.55);
  color: #ffffff;
}}
.stButton > button:active,
[data-testid="stDownloadButton"] > button:active {{ transform: scale(0.985); }}
.stButton > button[kind="primary"],
[data-testid="stFormSubmitButton"] > button[kind="primary"] {{
  border: none;
  background: linear-gradient(135deg, #22e0ff, #7d7bff 55%, #bf5af2);
  color: #051019;
  font-weight: 650;
  box-shadow: 0 10px 26px -12px rgba(0, 212, 255, 0.8);
}}
.stButton > button[kind="primary"]:hover,
[data-testid="stFormSubmitButton"] > button[kind="primary"]:hover {{
  color: #051019;
  box-shadow: 0 14px 32px -12px rgba(0, 212, 255, 0.95);
  filter: brightness(1.05);
}}

/* ---------- 输入类控件 ---------- */
[data-baseweb="input"],
[data-baseweb="textarea"],
[data-baseweb="select"] > div,
[data-testid="stNumberInput"] [data-baseweb="input"],
[data-testid="stTextInput"] [data-baseweb="input"] {{
  background: rgba(255, 255, 255, 0.04) !important;
  border-color: var(--fp-border) !important;
  border-radius: 10px !important;
  transition: border-color 150ms ease, box-shadow 150ms ease;
}}
[data-baseweb="input"]:focus-within,
[data-baseweb="textarea"]:focus-within,
[data-baseweb="select"] > div:focus-within {{
  border-color: rgba(0, 212, 255, 0.55) !important;
  box-shadow: 0 0 0 3px rgba(0, 212, 255, 0.11) !important;
}}
[data-baseweb="select"] [data-baseweb="popover"] [role="listbox"] {{
  background: #14142a;
  border: 1px solid var(--fp-border);
  border-radius: 12px;
}}
[data-testid="stWidgetLabel"] p {{
  color: var(--fp-text-2);
  font-size: 0.8rem;
  font-weight: 500;
}}
[data-testid="stRadio"] [role="radiogroup"] {{ gap: 0.3rem; }}

/* ---------- 表格 ---------- */
[data-testid="stDataFrame"],
[data-testid="stDataEditor"] {{
  border: 1px solid var(--fp-border);
  border-radius: 13px;
  overflow: hidden;
  box-shadow: 0 16px 36px -26px rgba(0, 0, 0, 0.9);
}}
[data-testid="stDataFrame"] [data-testid="stElementToolbar"] {{ border-radius: 8px; }}

/* ---------- 提示条 / 折叠面板 ---------- */
[data-testid="stAlert"] {{
  border-radius: 12px;
  border: 1px solid var(--fp-border);
  background: rgba(255, 255, 255, 0.04);
  backdrop-filter: blur(12px);
}}
[data-testid="stExpander"] {{
  border: 1px solid var(--fp-border);
  border-radius: 12px;
  background: rgba(255, 255, 255, 0.028);
  overflow: hidden;
  backdrop-filter: blur(12px);
}}
[data-testid="stExpander"] summary {{ border-radius: 12px; }}
[data-testid="stExpander"] summary:hover {{ background: rgba(255, 255, 255, 0.04); }}

/* ---------- 图表容器去白边 ---------- */
[data-testid="stPlotlyChart"] {{ border-radius: 12px; overflow: hidden; }}

/* ---------- 滚动条 ---------- */
* {{ scrollbar-width: thin; scrollbar-color: rgba(255, 255, 255, 0.16) transparent; }}
::-webkit-scrollbar {{ width: 9px; height: 9px; }}
::-webkit-scrollbar-track {{ background: transparent; }}
::-webkit-scrollbar-thumb {{
  background: rgba(255, 255, 255, 0.14);
  border-radius: 999px;
  border: 2px solid transparent;
  background-clip: content-box;
}}
::-webkit-scrollbar-thumb:hover {{ background: rgba(0, 212, 255, 0.4); background-clip: content-box; }}

/* ---------- 窄屏 ---------- */
@media (max-width: 640px) {{
  [data-testid="stMainBlockContainer"] {{ padding-top: 3.75rem; }}
  .st-key-watch_scope {{ max-width: none; }}
  .fp-page-title {{ font-size: 1.45rem; }}
  [data-testid="stMetric"] {{ padding: 0.8rem 0.9rem; }}
  [data-testid="stMetricValue"] {{ font-size: 1.3rem !important; }}
  [data-testid="stTabs"] [data-baseweb="tab-list"] {{
    width: 100%;
    overflow-x: auto;
    flex-wrap: nowrap;
  }}
}}
"""
