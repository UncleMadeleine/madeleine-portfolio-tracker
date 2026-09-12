"""K线图 (蜡烛图): 含成交量 / 均线 / 周月K / 非交易日断轴.

纯函数模块: 输入 OHLC DataFrame (date/open/high/low/close/volume, 升序),
输出可渲染的 plotly Figure (CLI HTML 导出) 或 lightweight-charts
自包含 HTML (Streamlit 页面内嵌, TradingView 同款拖拽/触控板交互).
不做任何网络请求.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd
from pandas.tseries.frequencies import to_offset
import plotly.graph_objects as go
from plotly.subplots import make_subplots

OHLC_COLUMNS = ("date", "open", "high", "low", "close", "volume")
DEFAULT_MA: tuple[int, ...] = (5, 20, 60)

# 默认中国看盘软件配色: 红涨绿跌; green_up=True 时切换为国际配色 (绿涨红跌)
CN_UP_COLOR, CN_DOWN_COLOR = "#ef232a", "#14b143"
INTL_UP_COLOR, INTL_DOWN_COLOR = "#089981", "#f23648"
VOLUME_OPACITY = 0.75
MA_PALETTE = ("#f7d774", "#4ea1f3", "#c883f0", "#ff9f43", "#e15b64", "#2ccbc3", "#8d9db6")

PERIOD_LABELS = {"daily": "日K", "weekly": "周K", "monthly": "月K"}


def clean_ohlc(df: pd.DataFrame) -> pd.DataFrame:
    """清洗日线数据, 保证绘图与统计准确:

    - date 解析失败 / OHLC 任一缺失的行丢弃
    - 逻辑矛盾行丢弃 (high < low, high < max(open,close), low > min(open,close), 价格<=0)
    - 按日期升序排序, 重复日期保留最后一条
    """
    empty = pd.DataFrame(columns=list(OHLC_COLUMNS))
    if df is None or df.empty:
        return empty
    out = df.copy()
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    for c in ("open", "high", "low", "close"):
        if c not in out.columns:
            out[c] = np.nan
        out[c] = pd.to_numeric(out[c], errors="coerce")
    if "volume" not in out.columns:
        out["volume"] = 0.0
    out["volume"] = pd.to_numeric(out["volume"], errors="coerce").fillna(0.0)
    out = out.dropna(subset=["date", "open", "high", "low", "close"])
    body_hi = out[["open", "close"]].max(axis=1)
    body_lo = out[["open", "close"]].min(axis=1)
    ok = (
        (out["high"] >= out["low"] - 1e-9)
        & (out["high"] >= body_hi - 1e-9)
        & (out["low"] <= body_lo + 1e-9)
        & (out["close"] > 0)
    )
    out = out[ok]
    # 稳定排序: 重复日期保留输入中最后一条 (来源覆盖顺序有意义)
    out = out.sort_values("date", kind="stable").drop_duplicates(subset="date", keep="last")
    return out.reset_index(drop=True)[list(OHLC_COLUMNS)]


def parse_ma_periods(spec: str | int | list | tuple) -> tuple[int, ...]:
    """'5,20,60' -> (5, 20, 60); 无效/过小(<2)的周期忽略, 结果去重升序."""
    if isinstance(spec, str):
        parts = [p.strip() for p in spec.split(",") if p.strip()]
    elif isinstance(spec, (int, np.integer)):
        parts = [str(spec)]
    else:
        parts = [str(p).strip() for p in (spec or []) if p is not None]
    periods: set[int] = set()
    for p in parts:
        try:
            n = int(p)
        except (TypeError, ValueError):
            continue
        if n >= 2:
            periods.add(n)
    return tuple(sorted(periods))


def compute_ma(df: pd.DataFrame, periods) -> dict[int, pd.Series]:
    """收盘价简单移动平均; 不足一个窗口的前段为 NaN, 窗口大于样本数则不计算."""
    close = df["close"].astype(float)
    out: dict[int, pd.Series] = {}
    for n in parse_ma_periods(periods):
        if n <= len(df):
            out[n] = close.rolling(window=n, min_periods=n).mean()
    return out


def _month_rule() -> str:
    """pandas 3.x 移除了 'M'; 用偏移解析探测而非版本字符串比较 (2.10+ 会误判)."""
    try:
        to_offset("ME")
        return "ME"
    except ValueError:
        return "M"


def resample_ohlc(df: pd.DataFrame, period: str) -> pd.DataFrame:
    """日线重采样为周K (W-FRI) / 月K; open=首日开, high=最高, low=最低, close=末日收, volume=求和."""
    if period == "daily" or df is None or df.empty:
        return clean_ohlc(df)
    rule = "W-FRI" if period == "weekly" else _month_rule()
    out = (
        clean_ohlc(df)
        .set_index("date")
        .resample(rule)
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        .dropna(subset=["open", "high", "low", "close"])
        .reset_index()
    )
    return out[list(OHLC_COLUMNS)]


def _non_trading_days(dates: pd.Series) -> list[pd.Timestamp]:
    """区间内不在数据中的日历日 (周末+节假日), 用于 rangebreaks 消除图表空隙."""
    if len(dates) < 2:
        return []
    idx = pd.DatetimeIndex(pd.to_datetime(dates))
    full = pd.date_range(idx.min(), idx.max(), freq="D")
    return list(full.difference(idx))


def build_candlestick_fig(
    df: pd.DataFrame,
    symbol: str,
    currency: str | None = None,
    mas=DEFAULT_MA,
    show_volume: bool = True,
    green_up: bool = False,
    period: str = "daily",
    title: str | None = None,
    height: int = 680,
) -> go.Figure:
    """构建蜡烛图: 价格主图 + 成交量副图 + MA 均线 + 非交易日断轴 + 区间快捷按钮."""
    df = clean_ohlc(df)
    if df.empty:
        raise ValueError(f"{symbol}: 无有效K线数据")
    up, down = (INTL_UP_COLOR, INTL_DOWN_COLOR) if green_up else (CN_UP_COLOR, CN_DOWN_COLOR)
    dates = df["date"]

    if show_volume:
        fig = make_subplots(
            rows=2, cols=1, shared_xaxes=True,
            row_heights=[0.74, 0.26], vertical_spacing=0.03,
        )
        price_row, vol_row = 1, 2
    else:
        fig = make_subplots(rows=1, cols=1)
        price_row, vol_row = 1, None

    candle = go.Candlestick(
        x=dates, open=df["open"], high=df["high"], low=df["low"], close=df["close"],
        name=PERIOD_LABELS.get(period, "日K"),
        increasing=dict(line=dict(color=up, width=1.5), fillcolor=up),
        decreasing=dict(line=dict(color=down, width=1.5), fillcolor=down),
        whiskerwidth=0.2,
        hovertemplate=(
            "<b>%{x|%Y-%m-%d}</b><br>"
            "开 %{open:.2f}  高 %{high:.2f}<br>"
            "低 %{low:.2f}  收 %{close:.2f}<extra></extra>"
        ),
    )
    fig.add_trace(candle, row=price_row, col=1)
    for i, (n, srs) in enumerate(compute_ma(df, mas).items()):
        fig.add_trace(
            go.Scatter(
                x=dates, y=srs, mode="lines", name=f"MA{n}",
                line=dict(color=MA_PALETTE[i % len(MA_PALETTE)], width=1.6, shape="spline"),
                connectgaps=False,
                hovertemplate=f"MA{n} %{{y:.2f}}<extra></extra>",
            ),
            row=price_row, col=1,
        )
    if vol_row is not None:
        vol_colors = np.where(df["close"] >= df["open"], up, down)
        fig.add_trace(
            go.Bar(
                x=dates, y=df["volume"], name="成交量",
                marker_color=vol_colors,
                opacity=0.5, showlegend=False,
                hovertemplate="量 %{y:,.0f}<extra></extra>",
            ),
            row=vol_row, col=1,
        )

    breaks = [dict(values=_non_trading_days(dates))]
    buttons = [
        dict(count=1, label="1个月", step="month", stepmode="backward"),
        dict(count=3, label="3个月", step="month", stepmode="backward"),
        dict(count=6, label="6个月", step="month", stepmode="backward"),
        dict(count=1, label="1年", step="year", stepmode="backward"),
        dict(step="all", label="全部"),
    ]
    if title is None:
        title = f"{symbol} · {PERIOD_LABELS.get(period, '日K')}" + (
            f" · {currency}" if currency else ""
        )
    last_close = float(df["close"].iloc[-1])
    up_marker = "▲" if last_close >= float(df["open"].iloc[-1]) else "▼"
    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="#0e1117",
        plot_bgcolor="#0e1117",
        title=dict(text=title, x=0.5, xanchor="center", font=dict(size=15)),
        height=height,
        margin=dict(l=8, r=64, t=48, b=12),
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.0, x=0, bgcolor="rgba(0,0,0,0)"),
        font=dict(family="system-ui, -apple-system, sans-serif", size=12, color="#d1d5db"),
        dragmode="pan",
        xaxis=dict(
            showspikes=True, spikemode="across", spikethickness=1,
            spikecolor="rgba(120,140,180,0.4)", spikesnap="cursor",
        ),
        yaxis=dict(
            showspikes=True, spikemode="across", spikethickness=1,
            spikecolor="rgba(120,140,180,0.4)", spikesnap="cursor",
        ),
        shapes=[
            dict(
                type="line", xref="paper", x0=0, x1=1,
                yref="y", y0=last_close, y1=last_close,
                line=dict(color="rgba(200,210,230,0.5)", width=1, dash="dot"),
            ),
        ],
        annotations=[
            dict(
                x=1.0, y=last_close, xref="paper", yref="y",
                xanchor="left", yanchor="middle",
                text=f"{up_marker} {last_close:.2f}",
                showarrow=False,
                font=dict(size=11, color="#e8eaed"),
                bgcolor="rgba(40,44,52,0.85)",
                bordercolor="rgba(120,140,180,0.3)",
                borderwidth=1, borderpad=3,
            ),
        ],
    )
    fig.update_xaxes(
        rangeslider_visible=False, showgrid=False,
        rangebreaks=breaks,
        showline=True, linecolor="rgba(120,140,180,0.2)",
    )
    fig.update_xaxes(rangeselector=dict(buttons=buttons, bgcolor="rgba(30,34,40,0.8)"), row=price_row, col=1)
    fig.update_yaxes(
        side="right", showgrid=True, gridcolor="rgba(120,140,180,0.06)",
        showline=True, linecolor="rgba(120,140,180,0.2)",
        tickfont=dict(size=11),
    )
    if vol_row is not None:
        fig.update_yaxes(row=vol_row, col=1, tickformat="~s", showgrid=False)
    return fig


def fig_to_html(fig: go.Figure) -> str:
    """导出为自包含 HTML (内嵌 plotly.js, 离线可打开)."""
    return fig.to_html(
        include_plotlyjs=True,
        full_html=True,
        config={
            "displaylogo": False,
            "scrollZoom": True,
            "modeBarButtonsToRemove": ["select2d", "lasso2d"],
        },
    )


# ---------- Streamlit 页面渲染: lightweight-charts (TradingView 开源内核) ----------
#
# 与 plotly 的关键交互差异 (券商 App 通用习惯):
#   拖动 = 平移 (带惯性); 滚轮/捏合 = 围绕光标缩放时间轴;
#   触控板双指横向滑动 = 平移 (wheel deltaX), 双指纵向/捏合 = 缩放 (deltaY);
#   价格轴随可见区间自动缩放; 十字光标 + 顶部 OHLC 信息栏.

_LWC_ASSET = (
    Path(__file__).parent / "assets" / "lightweight-charts.standalone.production.js"
)


@lru_cache(maxsize=1)
def _lwc_source() -> str:
    """vendored lightweight-charts v5 standalone (Apache-2.0, 离线内嵌)."""
    return _LWC_ASSET.read_text(encoding="utf-8")


def _rgba(hex_color: str, alpha: float) -> str:
    h = hex_color.lstrip("#")
    return f"rgba({int(h[0:2], 16)},{int(h[2:4], 16)},{int(h[4:6], 16)},{alpha})"


# 组件 DOM 结构 (st.components.v2, isolate_styles=True → ShadowRoot 内渲染).
KLINE_COMPONENT_HTML = (
    '<div class="lwc-wrap"><div class="lwc-chart"></div>'
    '<div class="lwc-legend"></div></div>'
)

KLINE_COMPONENT_CSS = """
.lwc-wrap { position: relative; width: 100%; height: 100%; }
.lwc-chart { position: absolute; inset: 0; }
.lwc-legend {
  position: absolute; left: 10px; top: 6px; z-index: 3; pointer-events: none;
  font: 12px/1.8 system-ui, -apple-system, sans-serif; color: #9aa4b2;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 96%;
}
"""

# 组件 JS (ES module, 默认导出在挂载/数据变化时执行, 返回值作清理函数).
# component.data 为 kline_payload() 生成的配置; parentElement 为 ShadowRoot.
_LWC_COMPONENT_MODULE = r"""
export default function (component) {
  var CFG = typeof component.data === 'string'
    ? JSON.parse(component.data) : component.data;
  var root = component.parentElement;
  var el = root.querySelector('.lwc-chart');
  var legend = root.querySelector('.lwc-legend');
  if (CFG.height) { el.parentElement.style.height = CFG.height + 'px'; }
  var LC = window.LightweightCharts;
  var chart = LC.createChart(el, CFG.options);
  var candle = chart.addSeries(LC.CandlestickSeries, CFG.candleOpts);
  candle.setData(CFG.candles);
  var maSeries = CFG.mas.map(function (m) {
    var s = chart.addSeries(LC.LineSeries, {
      color: m.color, lineWidth: 2,
      priceLineVisible: false, lastValueVisible: false,
      crosshairMarkerVisible: true, crosshairMarkerRadius: 3,
      priceFormat: { type: 'price', precision: CFG.precision, minMove: CFG.minMove }
    });
    s.setData(m.data);
    return { name: m.name, color: m.color, series: s, last: m.data[m.data.length - 1].value };
  });
  var volSeries = null;
  if (CFG.volume.length) {
    volSeries = chart.addSeries(LC.HistogramSeries, {
      priceFormat: { type: 'volume' }, priceScaleId: '',
      priceLineVisible: false, lastValueVisible: false
    }, 1);
    volSeries.setData(CFG.volume);
    try {
      var panes = chart.panes();
      if (panes.length > 1) { panes[1].setStretchFactor(0.32); }
    } catch (e) {}
  }

  var timeIdx = {};
  CFG.candles.forEach(function (b, i) { timeIdx[b.time] = i; });
  function fp(v) { return v == null ? '—' : Number(v).toFixed(CFG.precision); }
  function fv(v) {
    if (v == null) return '—';
    if (v >= 1e8) return (v / 1e8).toFixed(2) + '亿';
    if (v >= 1e4) return (v / 1e4).toFixed(1) + '万';
    return Math.round(v).toString();
  }
  function span(txt, color) {
    return '<span style="color:' + (color || '#c9d1d9') + '">' + txt + '</span>';
  }
  function show(param) {
    var bar = null, i = CFG.candles.length - 1, vol = null, maVals = null;
    if (param && param.seriesData) {
      var d = param.seriesData.get(candle);
      if (d && typeof d.open === 'number') { bar = d; i = timeIdx[d.time]; }
      if (volSeries) { var v = param.seriesData.get(volSeries); vol = v ? v.value : null; }
      maVals = maSeries.map(function (m) {
        var mv = param.seriesData.get(m.series);
        return { name: m.name, color: m.color, value: mv ? mv.value : null };
      });
    }
    if (!bar) {
      bar = CFG.candles[i];
      vol = CFG.volume.length ? CFG.volume[i].value : null;
      maVals = maSeries.map(function (m) { return { name: m.name, color: m.color, value: m.last }; });
    }
    var prev = i > 0 ? CFG.candles[i - 1].close : bar.open;
    var pct = prev ? (bar.close / prev - 1) * 100 : 0;
    var dir = bar.close >= bar.open ? CFG.up : CFG.down;
    var pctColor = pct >= 0 ? CFG.up : CFG.down;
    var h = span(CFG.title + ' ', '#e8eaed') + ' ' + span(bar.time, '#8b949e') + '&nbsp;&nbsp;'
      + '开' + span(fp(bar.open)) + ' 高' + span(fp(bar.high), dir)
      + ' 低' + span(fp(bar.low), dir) + ' 收' + span(fp(bar.close), dir)
      + ' ' + span((pct >= 0 ? '+' : '') + pct.toFixed(2) + '%', pctColor);
    if (vol != null) { h += ' 量' + span(fv(vol)); }
    maVals.forEach(function (m) { h += ' ' + span(m.name + ' ' + fp(m.value), m.color); });
    legend.innerHTML = h;
  }
  chart.subscribeCrosshairMove(show);
  show(null);

  var n = CFG.candles.length;
  try {
    chart.timeScale().setVisibleLogicalRange({ from: n - Math.min(n, CFG.initBars), to: n - 1 + 4 });
  } catch (e) { chart.timeScale().fitContent(); }
  return function () { try { chart.remove(); } catch (e) {} };
}
"""


def kline_component_js() -> str:
    """组件完整 JS 源: vendored lightweight-charts (UMD → window.LightweightCharts) + 挂载逻辑."""
    return _lwc_source() + "\n" + _LWC_COMPONENT_MODULE


def kline_payload(
    df: pd.DataFrame,
    symbol: str,
    currency: str | None = None,
    mas=DEFAULT_MA,
    show_volume: bool = True,
    green_up: bool = False,
    period: str = "daily",
    height: int = 680,
    init_bars: int = 140,
) -> dict:
    """生成传给 K线组件 (st.components.v2) 的数据 payload.

    组件交互: 拖动平移 (带惯性) / 滚轮·捏合缩放时间轴 / 触控板双指横滑平移 /
    十字光标 OHLC 信息栏 / 价格轴随可见区间自适应 / 副图分隔线可拖拽 / 双击轴复位.
    """
    df = clean_ohlc(df)
    if df.empty:
        raise ValueError(f"{symbol}: 无有效K线数据")
    up, down = (INTL_UP_COLOR, INTL_DOWN_COLOR) if green_up else (CN_UP_COLOR, CN_DOWN_COLOR)
    dates = df["date"].dt.strftime("%Y-%m-%d")
    candles = [
        {
            "time": t,
            "open": round(float(o), 6), "high": round(float(h), 6),
            "low": round(float(l), 6), "close": round(float(c), 6),
        }
        for t, o, h, l, c in zip(dates, df["open"], df["high"], df["low"], df["close"])
    ]
    up_vol, down_vol = _rgba(up, 0.55), _rgba(down, 0.55)
    vols = [
        {"time": t, "value": float(v), "color": up_vol if c >= o else down_vol}
        for t, v, c, o in zip(dates, df["volume"], df["close"], df["open"])
    ]
    ma_series = []
    for i, (n, srs) in enumerate(compute_ma(df, mas).items()):
        data = [
            {"time": t, "value": round(float(v), 6)}
            for t, v in zip(dates, srs)
            if pd.notna(v)
        ]
        if data:
            ma_series.append(
                {"name": f"MA{n}", "color": MA_PALETTE[i % len(MA_PALETTE)], "data": data}
            )
    med = float(df["close"].median())
    precision = 2 if med >= 20 else (3 if med >= 1 else 4)
    last = df.iloc[-1]
    title = f"{symbol} · {PERIOD_LABELS.get(period, '日K')}" + (
        f" · {currency}" if currency else ""
    )
    return {
        "title": title,
        "up": up,
        "down": down,
        "precision": precision,
        "minMove": 10 ** -precision,
        "initBars": init_bars,
        "height": height,
        "candles": candles,
        "volume": vols if show_volume else [],
        "mas": ma_series,
        "options": {
            "autoSize": True,
            "layout": {
                "background": {"type": "solid", "color": "#0e1117"},
                "textColor": "#c9d1d9",
                "fontSize": 12,
                "fontFamily": "system-ui, -apple-system, sans-serif",
                "panes": {
                    "separatorColor": "rgba(120,140,180,0.25)",
                    "separatorHoverColor": "rgba(120,140,180,0.6)",
                    "enableResize": True,
                },
            },
            "grid": {
                "vertLines": {"color": "rgba(120,140,180,0.07)"},
                "horzLines": {"color": "rgba(120,140,180,0.07)"},
            },
            "crosshair": {
                "mode": 0,
                "vertLine": {
                    "color": "rgba(150,170,200,0.55)", "width": 1,
                    "style": 2, "labelBackgroundColor": "#2a2e39",
                },
                "horzLine": {
                    "color": "rgba(150,170,200,0.55)", "width": 1,
                    "style": 2, "labelBackgroundColor": "#2a2e39",
                },
            },
            "rightPriceScale": {"borderColor": "rgba(120,140,180,0.25)"},
            "timeScale": {
                "borderColor": "rgba(120,140,180,0.25)",
                "rightOffset": 4, "barSpacing": 9, "minBarSpacing": 1.2,
                "timeVisible": False, "secondsVisible": False,
            },
            "handleScroll": {
                "pressedMouseMove": True, "mouseWheel": True,
                "horzTouchDrag": True, "vertTouchDrag": False,
            },
            "handleScale": {
                "mouseWheel": True, "pinch": True,
                "axisPressedMouseMove": True, "axisDoubleClickReset": True,
            },
            "kineticScroll": {"touch": True, "mouse": True},
            "localization": {"locale": "zh-CN"},
        },
        "candleOpts": {
            "upColor": up, "downColor": down,
            "wickUpColor": up, "wickDownColor": down,
            "borderVisible": False,
            "priceLineVisible": True, "priceLineStyle": 2,
            "priceLineColor": _rgba(up if last["close"] >= last["open"] else down, 0.7),
            "priceFormat": {
                "type": "price", "precision": precision, "minMove": 10 ** -precision,
            },
        },
    }


def summarize_ohlc(df: pd.DataFrame, mas=DEFAULT_MA) -> dict:
    """K线摘要统计 (用于终端输出 / --json / 页面指标)."""
    df = clean_ohlc(df)
    if df.empty:
        return {"bars": 0}
    first, last = df.iloc[0], df.iloc[-1]
    change_pct = (
        (float(last["close"]) / float(first["close"]) - 1) * 100
        if float(first["close"]) > 0
        else None
    )
    hi = df.loc[df["high"].idxmax()]
    lo = df.loc[df["low"].idxmin()]
    ma_vals: dict[str, float | None] = {}
    for n, srs in compute_ma(df, mas).items():
        v = srs.iloc[-1]
        ma_vals[f"ma{n}"] = None if pd.isna(v) else round(float(v), 4)
    return {
        "bars": int(len(df)),
        "first_date": first["date"].strftime("%Y-%m-%d"),
        "last_date": last["date"].strftime("%Y-%m-%d"),
        "first_close": round(float(first["close"]), 4),
        "last_open": round(float(last["open"]), 4),
        "last_high": round(float(last["high"]), 4),
        "last_low": round(float(last["low"]), 4),
        "last_close": round(float(last["close"]), 4),
        "last_volume": float(last["volume"]) if pd.notna(last["volume"]) else None,
        "change_pct": round(change_pct, 4) if change_pct is not None else None,
        "period_high": round(float(hi["high"]), 4),
        "period_high_date": hi["date"].strftime("%Y-%m-%d"),
        "period_low": round(float(lo["low"]), 4),
        "period_low_date": lo["date"].strftime("%Y-%m-%d"),
        "ma": ma_vals,
    }
