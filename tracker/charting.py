"""K线图 (蜡烛图): 含成交量 / 均线 / 周月K / 非交易日断轴.

纯函数模块: 输入 OHLC DataFrame (date/open/high/low/close/volume, 升序),
输出可渲染的 plotly Figure (CLI HTML 导出) 或 lightweight-charts
自包含 HTML (Streamlit 页面内嵌, TradingView 同款拖拽/触控板交互).
不做任何网络请求.

实现按职责拆分:
- charting_core.py     清洗 / 指标 / 重采样 / 摘要 (无 plotly)
- charting_plotly.py   plotly 蜡烛图与 HTML 导出 (plotly 惰性导入)
- charting_payload.py  lightweight-charts 前端 payload
本模块仅做稳定 API 的再导出, 保持既有 ``tracker.charting`` 导入路径不变。
"""

from __future__ import annotations


from .charting_core import (  # noqa: F401 - 兼容再导出
    OHLC_COLUMNS,
    DEFAULT_MA,
    CN_UP_COLOR,
    CN_DOWN_COLOR,
    INTL_UP_COLOR,
    INTL_DOWN_COLOR,
    up_down_colors,
    MA_PALETTE,
    BOLL_UPPER_COLOR,
    BOLL_MIDDLE_COLOR,
    BOLL_LOWER_COLOR,
    MACD_DIF_COLOR,
    MACD_DEA_COLOR,
    RSI_COLOR,
    KDJ_K_COLOR,
    KDJ_D_COLOR,
    KDJ_J_COLOR,
    PERIOD_LABELS,
    clean_ohlc,
    parse_ma_periods,
    compute_ma,
    calc_macd,
    calc_rsi,
    calc_kdj,
    calc_boll,
    _price_precision,
    _candle_rows,
    _to_line_data,
    _month_rule,
    resample_ohlc,
    PERIOD_ORDER,
    infer_native_period,
    available_periods,
    resolve_period,
    _non_trading_days,
    summarize_ohlc,
)

from .charting_plotly import (  # noqa: F401 - 兼容再导出
    build_candlestick_fig,
    fig_to_html,
)

from .charting_payload import (  # noqa: F401 - 兼容再导出
    _lwc_source,
    _rgba,
    KLINE_COMPONENT_HTML,
    KLINE_COMPONENT_CSS,
    _LWC_COMPONENT_MODULE,
    _lwc_options,
    kline_component_js,
    _base_payload,
    kline_payload,
    RATIO_COLOR,
    COMPARE_PALETTE,
    build_ratio_series,
    compare_payload,
    line_payload,
    crypto_compare_payload,
)

__all__ = [
    "CN_UP_COLOR",
    "CN_DOWN_COLOR",
    "INTL_UP_COLOR",
    "INTL_DOWN_COLOR",
    "BOLL_LOWER_COLOR",
    "BOLL_MIDDLE_COLOR",
    "BOLL_UPPER_COLOR",
    "COMPARE_PALETTE",
    "DEFAULT_MA",
    "KDJ_D_COLOR",
    "KDJ_J_COLOR",
    "KDJ_K_COLOR",
    "KLINE_COMPONENT_CSS",
    "KLINE_COMPONENT_HTML",
    "MACD_DEA_COLOR",
    "MACD_DIF_COLOR",
    "MA_PALETTE",
    "OHLC_COLUMNS",
    "PERIOD_LABELS",
    "PERIOD_ORDER",
    "RATIO_COLOR",
    "RSI_COLOR",
    "available_periods",
    "build_candlestick_fig",
    "build_ratio_series",
    "calc_boll",
    "calc_kdj",
    "calc_macd",
    "calc_rsi",
    "clean_ohlc",
    "compare_payload",
    "compute_ma",
    "crypto_compare_payload",
    "fig_to_html",
    "infer_native_period",
    "kline_component_js",
    "kline_payload",
    "line_payload",
    "parse_ma_periods",
    "resample_ohlc",
    "resolve_period",
    "summarize_ohlc",
    "up_down_colors",
]
