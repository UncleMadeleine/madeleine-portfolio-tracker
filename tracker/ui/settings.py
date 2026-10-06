"""UI 显示设置: 涨跌配色 / 基础货币展示, 设置页与各页面共用.

settings.json / portfolio.json 的读写统一在 tracker.storage;
本模块只保留 Streamlit 展示层辅助 (配色常量与配色映射)。
"""

from __future__ import annotations

from tracker.charting import (
    CN_DOWN_COLOR,
    CN_UP_COLOR,
    INTL_DOWN_COLOR,
    INTL_UP_COLOR,
)

# 涨跌配色: cn = 红涨绿跌 (A股软件习惯, 默认) / intl = 绿涨红跌 (国际配色)
SCHEME_CN = "cn"
SCHEME_INTL = "intl"
SCHEME_LABELS = {SCHEME_CN: "红涨绿跌 (A股习惯)", SCHEME_INTL: "绿涨红跌 (国际)"}

BASE_CURRENCIES = ["CNY", "USD", "EUR", "HKD"]

# 加密货币域数据源偏好: 值 = settings.json crypto_source (与 storage.CRYPTO_SOURCES 一致),
# 标签 = 生效链路 (指定源前置, 链内其余源按默认相对顺序兜底)
CRYPTO_SOURCE_LABELS = {
    "auto": "默认链 (Binance → Gate.io → Hyperliquid → yfinance)",
    "binance": "Binance 优先 (Binance → Gate.io → Hyperliquid → yfinance)",
    "gate": "Gate.io 优先 (Gate.io → Binance → Hyperliquid → yfinance)",
    "hyperliquid": "Hyperliquid 优先 (Hyperliquid → Binance → Gate.io → yfinance)",
    "yfinance": "yfinance 优先 (yfinance → Binance → Gate.io → Hyperliquid)",
}

# 设置页链路说明用的短链 (与 CRYPTO_SOURCE_LABELS 同序)
CRYPTO_SOURCE_CHAINS = {
    "auto": "Binance → Gate.io → Hyperliquid → yfinance",
    "binance": "Binance → Gate.io → Hyperliquid → yfinance",
    "gate": "Gate.io → Binance → Hyperliquid → yfinance",
    "hyperliquid": "Hyperliquid → Binance → Gate.io → yfinance",
    "yfinance": "yfinance → Binance → Gate.io → Hyperliquid",
}


def green_up(settings: dict | None = None) -> bool:
    """True = 绿涨红跌 (国际配色)."""
    if settings is None:
        from tracker.storage import load_settings

        settings = load_settings()
    return settings.get("color_scheme") == SCHEME_INTL


def up_down_colors(settings: dict | None = None) -> tuple[str, str]:
    """(涨色, 跌色) 十六进制, 与 K线组件配色一致."""
    return (
        (INTL_UP_COLOR, INTL_DOWN_COLOR)
        if green_up(settings)
        else (CN_UP_COLOR, CN_DOWN_COLOR)
    )


def up_down_tags(settings: dict | None = None) -> tuple[str, str]:
    """Streamlit 内建 :red[]/:green[] 标签按方案映射 (涨, 跌)."""
    return ("green", "red") if green_up(settings) else ("red", "green")


def delta_color(settings: dict | None = None) -> str:
    """st.metric 的 delta_color: 让正 delta 显示为涨色."""
    return "normal" if green_up(settings) else "inverse"
