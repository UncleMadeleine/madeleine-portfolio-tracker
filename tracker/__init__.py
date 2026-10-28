"""Madeleine Portfolio Tracker — 本地多市场投资组合追踪 (多数据源 + Streamlit/CLI 双前端)."""

#: 单一版本来源 (pyproject.toml 动态读取; CLI --version 与此一致)
__version__ = "1.1.0"

from .analytics import build_view, summarize
from .fx import get_fx_rates, get_rate
from .prices import Quote, get_history, get_quote, get_quotes
from .symbols import MARKET_META, Market, normalize, parse
from .services.watchlist import sort_watchlist

__all__ = [
    "MARKET_META",
    "Market",
    "Quote",
    "build_view",
    "get_fx_rates",
    "get_history",
    "get_quote",
    "get_quotes",
    "get_rate",
    "normalize",
    "parse",
    "sort_watchlist",
    "summarize",
]
