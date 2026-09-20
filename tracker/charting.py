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

# 技术指标配色 (与 MA 调色板区分, 便于辨识)
BOLL_UPPER_COLOR = "#e15b64"   # 布林带上轨 - 红
BOLL_MIDDLE_COLOR = "#f7d774"  # 布林带中轨 - 黄
BOLL_LOWER_COLOR = "#2ccbc3"   # 布林带下轨 - 青
MACD_DIF_COLOR = "#f7d774"     # MACD DIF 线 - 黄
MACD_DEA_COLOR = "#4ea1f3"     # MACD DEA 线 - 蓝
RSI_COLOR = "#c883f0"          # RSI 线 - 紫
KDJ_K_COLOR = "#f7d774"        # KDJ K 线 - 黄
KDJ_D_COLOR = "#4ea1f3"        # KDJ D 线 - 蓝
KDJ_J_COLOR = "#e15b64"        # KDJ J 线 - 红

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
        # 任一价格 <=0 都是无效行情 (停牌/坏数据), 会污染区间高低点与绘图
        & (out[["open", "high", "low", "close"]] > 0).all(axis=1)
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


# ---------- 技术指标计算 (纯函数, 便于测试) ----------

def calc_macd(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    """MACD 指标 (指数平滑异同移动平均线).

    DIF = 快线EMA - 慢线EMA; DEA = DIF 的 signal 周期EMA; MACD柱 = 2×(DIF-DEA).
    返回 DataFrame: 列 dif / dea / macd.
    """
    close = df["close"].astype(float)
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False).mean()
    macd_hist = 2 * (dif - dea)
    return pd.DataFrame({"dif": dif, "dea": dea, "macd": macd_hist})


def calc_rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """RSI 相对强弱指标 (Wilder 平滑法).

    周期内平均涨幅 / (平均涨幅 + 平均跌幅) × 100, 取值 0~100.
    首值无前日数据 → NaN; 全涨 (avg_loss=0) → 100; 无波动 → 50.
    """
    close = df["close"].astype(float)
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    rsi[avg_loss == 0] = 100.0  # avg_loss=0 (全涨) → RSI=100 (NaN 不受影响)
    rsi[(avg_gain == 0) & (avg_loss == 0)] = 50.0  # 无波动 → 50
    return rsi


def calc_kdj(df: pd.DataFrame, n: int = 9, m1: int = 3, m2: int = 3) -> pd.DataFrame:
    """KDJ 随机指标.

    RSV = (close - 最低低) / (最高高 - 最低低) × 100;
    K = RSV 的 m1 周期 EMA; D = K 的 m2 周期 EMA; J = 3K - 2D.
    返回 DataFrame: 列 k / d / j.
    """
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    lowest = low.rolling(window=n, min_periods=1).min()
    highest = high.rolling(window=n, min_periods=1).max()
    rsv = (close - lowest) / (highest - lowest).replace(0, np.nan) * 100
    rsv = rsv.fillna(50.0)  # 最高=最低 (无波动) → RSV=50
    k = rsv.ewm(alpha=1 / m1, adjust=False).mean()
    d = k.ewm(alpha=1 / m2, adjust=False).mean()
    j = 3 * k - 2 * d
    return pd.DataFrame({"k": k, "d": d, "j": j})


def calc_boll(df: pd.DataFrame, period: int = 20, std: float = 2.0) -> pd.DataFrame:
    """布林带 (Bollinger Bands).

    中轨 = close 的 period 周期 SMA; 上下轨 = 中轨 ± std 倍标准差.
    返回 DataFrame: 列 upper / middle / lower.
    """
    close = df["close"].astype(float)
    middle = close.rolling(window=period, min_periods=period).mean()
    sd = close.rolling(window=period, min_periods=period).std(ddof=0)
    upper = middle + std * sd
    lower = middle - std * sd
    return pd.DataFrame({"upper": upper, "middle": middle, "lower": lower})


def _to_line_data(dates: pd.Series, srs: pd.Series, precision: int = 6) -> list[dict]:
    """将 pandas Series 转为 lightweight-charts line data [{time, value}, ...], 跳过 NaN."""
    return [
        {"time": t, "value": round(float(v), precision)}
        for t, v in zip(dates, srs)
        if pd.notna(v)
    ]


def _month_rule() -> str:
    """pandas 3.x 移除了 'M'; 用偏移解析探测而非版本字符串比较 (2.10+ 会误判)."""
    try:
        to_offset("ME")
        return "ME"
    except ValueError:
        return "M"


def resample_ohlc(df: pd.DataFrame, period: str) -> pd.DataFrame:
    """日线重采样为周K (W-FRI) / 月K; open=首日开, high=最高, low=最低, close=末日收, volume=求和."""
    if df is None or df.empty:
        return pd.DataFrame(columns=list(OHLC_COLUMNS))
    if period == "daily":
        return df.reset_index(drop=True)[list(OHLC_COLUMNS)]
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
# component.data 为 kline_payload()/compare_payload() 生成的配置;
# CFG.mode === 'compare' 时渲染多条折线 (多股对比), 否则为单只蜡烛图.
# parentElement 为 ShadowRoot.
_LWC_COMPONENT_MODULE = r"""
export default function (component) {
  var CFG = typeof component.data === 'string'
    ? JSON.parse(component.data) : component.data;
  var root = component.parentElement;
  var el = root.querySelector('.lwc-chart');
  var legend = root.querySelector('.lwc-legend');
  // data 变化时 Streamlit 会重新执行本函数但不调用上一次的 cleanup (仅 unmount 时调用),
  // 残留的旧 chart 实例会叠层并遮挡新图 — 挂载前先清空容器。
  while (el.firstChild) { el.removeChild(el.firstChild); }
  if (CFG.height) { el.parentElement.style.height = CFG.height + 'px'; }
  var LC = window.LightweightCharts;
  var chart = LC.createChart(el, CFG.options);
  var isCompare = CFG.mode === 'compare';
  var candle = null, volSeries = null, tracks = [], indSeries = [];

  function addLine(color, data) {
    var s = chart.addSeries(LC.LineSeries, {
      color: color, lineWidth: 2,
      priceLineVisible: false, lastValueVisible: false,
      crosshairMarkerVisible: true, crosshairMarkerRadius: 3,
      priceFormat: { type: 'price', precision: CFG.precision, minMove: CFG.minMove }
    });
    s.setData(data);
    return s;
  }
  if (isCompare) {
    tracks = CFG.lines.map(function (ln) {
      return { name: ln.name, color: ln.color, series: addLine(ln.color, ln.data), data: ln.data };
    });
  } else {
    candle = chart.addSeries(LC.CandlestickSeries, CFG.candleOpts);
    candle.setData(CFG.candles);
    tracks = CFG.mas.map(function (m) {
      return { name: m.name, color: m.color, series: addLine(m.color, m.data), data: m.data };
    });
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

    // 技术指标: 布林带叠加主图, MACD/RSI/KDJ 各占独立副图
    if (CFG.boll && CFG.boll.length) {
      CFG.boll.forEach(function (b) {
        tracks.push({ name: b.name, color: b.color, series: addLine(b.color, b.data), data: b.data });
      });
    }
    var indPane = CFG.volume.length ? 2 : 1;
    function addIndLine(color, data, paneIdx) {
      var s = chart.addSeries(LC.LineSeries, {
        color: color, lineWidth: 1,
        priceLineVisible: false, lastValueVisible: false,
        crosshairMarkerVisible: true, crosshairMarkerRadius: 2,
        priceFormat: { type: 'price', precision: 2, minMove: 0.01 }
      }, paneIdx);
      s.setData(data);
      return s;
    }
    if (CFG.macd) {
      var mp = indPane++;
      var difS = addIndLine(CFG.macd.difColor, CFG.macd.dif, mp);
      var deaS = addIndLine(CFG.macd.deaColor, CFG.macd.dea, mp);
      var histS = chart.addSeries(LC.HistogramSeries, {
        priceFormat: { type: 'price', precision: 2, minMove: 0.01 },
        priceLineVisible: false, lastValueVisible: false
      }, mp);
      histS.setData(CFG.macd.hist);
      indSeries.push({ name: 'DIF', color: CFG.macd.difColor, series: difS, data: CFG.macd.dif });
      indSeries.push({ name: 'DEA', color: CFG.macd.deaColor, series: deaS, data: CFG.macd.dea });
    }
    if (CFG.rsi) {
      var rp = indPane++;
      var rsiS = addIndLine(CFG.rsi.color, CFG.rsi.data, rp);
      indSeries.push({ name: 'RSI', color: CFG.rsi.color, series: rsiS, data: CFG.rsi.data });
    }
    if (CFG.kdj) {
      var kp = indPane++;
      var kdjKeys = [['k', 'K', CFG.kdj.kColor], ['d', 'D', CFG.kdj.dColor], ['j', 'J', CFG.kdj.jColor]];
      kdjKeys.forEach(function (kk) {
        var s = addIndLine(kk[2], CFG.kdj[kk[0]], kp);
        indSeries.push({ name: kk[1], color: kk[2], series: s, data: CFG.kdj[kk[0]] });
      });
    }
    // 调整副图高度比例: 主图大, 指标副图小
    try {
      var allPanes = chart.panes();
      if (allPanes.length > 1) {
        allPanes[0].setStretchFactor(3);
        for (var pi = 1; pi < allPanes.length; pi++) {
          allPanes[pi].setStretchFactor(0.5);
        }
      }
    } catch (e) {}
  }

  var timeIdx = {};
  if (!isCompare) {
    CFG.candles.forEach(function (b, i) { timeIdx[b.time] = i; });
  }
  function fp(v) { return v == null ? '—' : Number(v).toFixed(CFG.precision); }
  function fp2(v) { return v == null ? '—' : Number(v).toFixed(2); }
  function fv(v) {
    if (v == null) return '—';
    if (v >= 1e8) return (v / 1e8).toFixed(2) + '亿';
    if (v >= 1e4) return (v / 1e4).toFixed(1) + '万';
    return Math.round(v).toString();
  }
  function span(txt, color) {
    return '<span style="color:' + (color || '#c9d1d9') + '">' + txt + '</span>';
  }
  function tstr(t) {
    if (t == null) return '';
    if (typeof t === 'string') return t;
    if (typeof t === 'object' && t.year) {
      return t.year + '-' + ('0' + t.month).slice(-2) + '-' + ('0' + t.day).slice(-2);
    }
    return String(t);
  }
  // 各市场交易日历不同: 按时间二分查找该系列 ≤ t 的最近取值
  function valAt(tr, t) {
    var lo = 0, hi = tr.data.length - 1, ans = null;
    while (lo <= hi) {
      var mid = (lo + hi) >> 1;
      if (tr.data[mid].time <= t) { ans = tr.data[mid].value; lo = mid + 1; }
      else { hi = mid - 1; }
    }
    return ans;
  }
  function show(param) {
    var h, i;
    if (isCompare) {
      var t = '';
      tracks.forEach(function (tr) {
        var lt = tr.data[tr.data.length - 1].time;
        if (lt > t) { t = lt; }
      });
      if (param && param.time) { t = tstr(param.time); }
      h = span(CFG.title + ' ', '#e8eaed') + ' ' + span(t, '#8b949e') + '&nbsp;&nbsp;';
      tracks.forEach(function (tr) {
        var v = valAt(tr, t);
        var first = tr.data.length ? tr.data[0].value : null;
        var pct = (v != null && first) ? (v / first - 1) * 100 : null;
        h += ' ' + span(tr.name, tr.color) + ' ' + span(fp(v));
        if (pct != null) {
          h += ' ' + span('(' + (pct >= 0 ? '+' : '') + pct.toFixed(2) + '%)',
            pct >= 0 ? CFG.up : CFG.down);
        }
        h += '&nbsp;&nbsp;';
      });
      legend.innerHTML = h;
      return;
    }
    var bar = null, vol = null, maVals = null, indVals = null;
    i = CFG.candles.length - 1;
    if (param && param.seriesData) {
      var d = param.seriesData.get(candle);
      if (d && typeof d.open === 'number') { bar = d; i = timeIdx[d.time]; }
      if (volSeries) { var v = param.seriesData.get(volSeries); vol = v ? v.value : null; }
      maVals = tracks.map(function (m) {
        var mv = param.seriesData.get(m.series);
        return { name: m.name, color: m.color, value: mv ? mv.value : null };
      });
      indVals = indSeries.map(function (t) {
        var mv = param.seriesData.get(t.series);
        return { name: t.name, color: t.color, value: mv ? mv.value : null };
      });
    }
    if (!bar) {
      bar = CFG.candles[i];
      vol = CFG.volume.length ? CFG.volume[i].value : null;
      maVals = tracks.map(function (m) {
        return { name: m.name, color: m.color, value: m.data[m.data.length - 1].value };
      });
      indVals = indSeries.map(function (t) {
        return { name: t.name, color: t.color, value: t.data.length ? t.data[t.data.length - 1].value : null };
      });
    }
    var prev = i > 0 ? CFG.candles[i - 1].close : bar.open;
    var pct = prev ? (bar.close / prev - 1) * 100 : 0;
    var dir = bar.close >= bar.open ? CFG.up : CFG.down;
    var pctColor = pct >= 0 ? CFG.up : CFG.down;
    h = span(CFG.title + ' ', '#e8eaed') + ' ' + span(bar.time, '#8b949e') + '&nbsp;&nbsp;'
      + '开' + span(fp(bar.open)) + ' 高' + span(fp(bar.high), dir)
      + ' 低' + span(fp(bar.low), dir) + ' 收' + span(fp(bar.close), dir)
      + ' ' + span((pct >= 0 ? '+' : '') + pct.toFixed(2) + '%', pctColor);
    if (vol != null) { h += ' 量' + span(fv(vol)); }
    maVals.forEach(function (m) { h += ' ' + span(m.name + ' ' + fp(m.value), m.color); });
    if (indVals) { indVals.forEach(function (t) { h += ' ' + span(t.name + ' ' + fp2(t.value), t.color); }); }
    legend.innerHTML = h;
  }
  chart.subscribeCrosshairMove(show);
  show(null);

  var n = isCompare
    ? Math.max.apply(null, CFG.lines.map(function (l) { return l.data.length; }).concat([1]))
    : CFG.candles.length;
  // 视口还原: 组件因数据更新重建时, 按「锚点 bar 时间 + 分数偏移 + 半宽」还原拖动位置
  // (bar 索引无关, 日/周/月K 通用)。锚点存 window 级 store (按 queryId 键):
  // Streamlit 更新 data 时会重建 .lwc-wrap DOM 节点, DOM 属性随之丢失;
  // queryId 变化 (换代码/深度) 时旧键自然失效回退初始区间。
  if (!window.__lwcViewStore) { window.__lwcViewStore = {}; }
  var viewStore = CFG.queryId ? window.__lwcViewStore[CFG.queryId] : null;
  var restored = null;
  if (viewStore && viewStore.t && CFG.candles.length) {
    var vlo = 0, vhi = CFG.candles.length - 1, vai = -1;
    while (vlo <= vhi) {
      var vmi = (vlo + vhi) >> 1;
      if (CFG.candles[vmi].time === viewStore.t) { vai = vmi; break; }
      if (CFG.candles[vmi].time < viewStore.t) { vlo = vmi + 1; } else { vhi = vmi - 1; }
    }
    if (vai >= 0) {
      restored = {
        from: vai + viewStore.frac - viewStore.half,
        to: vai + viewStore.frac + viewStore.half,
      };
    }
  }
  try {
    chart.timeScale().setVisibleLogicalRange(
      restored || { from: n - Math.min(n, CFG.initBars), to: n - 1 + 4 }
    );
  } catch (e) {
    try { chart.timeScale().fitContent(); } catch (e2) {}
  }
  // 无限拖动: 视口拖近数据左缘 (from<5, 即基本贴到最早的K线) 时通知 Python
  // 向前追加更早历史 (hasMore=false 已到最早). 阈值取小值: 每次扩展后视口距
  // 左缘约一整段扩展量, 不会在扩展 rerun 后立刻自触发形成补数循环。
  // 每次视口变化先写锚点; seq 供 Python 去重, qid 供 Python 拒绝换查询后的残留值,
  // 1.5s 节流避免连续拖动刷屏。
  var lastFired = 0;
  chart.timeScale().subscribeVisibleLogicalRangeChange(function (range) {
    if (!range) return;
    if (CFG.candles.length) {
      var mid = (range.from + range.to) / 2;
      var fl = Math.max(0, Math.min(CFG.candles.length - 1, Math.floor(mid)));
      window.__lwcViewStore[CFG.queryId] = {
        t: CFG.candles[fl].time,
        frac: mid - fl,
        half: (range.to - range.from) / 2,
      };
    }
    if (isCompare || CFG.hasMore === false) return;
    var now = Date.now();
    if (now - lastFired < 1500) return;
    if (range.from < 5) {
      lastFired = now;
      // CCv2 组件对象无 setValue: need_more 是一次性事件 → setTriggerValue
      component.setTriggerValue("need_more", { seq: now, qid: CFG.queryId });
    }
  });

  return function () { try { chart.remove(); } catch (e) {} };
}
"""


def _lwc_options() -> dict:
    """lightweight-charts 通用图表选项 (K线与多股对比共用)."""
    return {
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
    }


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
    indicators: dict | None = None,
    has_more: bool = False,
    query_id: str | None = None,
) -> dict:
    """生成传给 K线组件 (st.components.v2) 的数据 payload.

    组件交互: 拖动平移 (带惯性) / 滚轮·捏合缩放时间轴 / 触控板双指横滑平移 /
    十字光标 OHLC 信息栏 / 价格轴随可见区间自适应 / 副图分隔线可拖拽 / 双击轴复位.

    indicators: 可选, 键为指标名 (macd/rsi/kdj/boll), 值为参数 dict;
                如 {"macd": {"fast": 12, "slow": 26, "signal": 9}, "rsi": {"period": 14}}.
    has_more: False 时组件停止「拖近左缘加载更早历史」回调 (指数页/已到上市首日).
    query_id: 查询标识 (代码|深度), 变化时组件丢弃视口锚点回退初始区间.
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
            "low": round(float(lo), 6), "close": round(float(c), 6),
        }
        for t, o, h, lo, c in zip(dates, df["open"], df["high"], df["low"], df["close"])
    ]
    up_vol, down_vol = _rgba(up, 0.55), _rgba(down, 0.55)
    # 指数等标的无成交量语义 (全 0): 省略成交量序列, 副图不渲染全零柱
    has_volume = bool((df["volume"] != 0).any())
    vols = (
        [
            {"time": t, "value": float(v), "color": up_vol if c >= o else down_vol}
            for t, v, c, o in zip(dates, df["volume"], df["close"], df["open"])
        ]
        if has_volume
        else []
    )
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

    # 技术指标计算
    boll_series: list[dict] = []
    macd_data: dict | None = None
    rsi_data: dict | None = None
    kdj_data: dict | None = None
    if indicators:
        if "boll" in indicators:
            bp = indicators["boll"]
            boll = calc_boll(df, period=bp.get("period", 20), std=bp.get("std", 2.0))
            for col, name, color in [
                ("upper", "BOLL上轨", BOLL_UPPER_COLOR),
                ("middle", "BOLL中轨", BOLL_MIDDLE_COLOR),
                ("lower", "BOLL下轨", BOLL_LOWER_COLOR),
            ]:
                data = _to_line_data(dates, boll[col])
                if data:
                    boll_series.append({"name": name, "color": color, "data": data})
        if "macd" in indicators:
            mp = indicators["macd"]
            macd = calc_macd(
                df, fast=mp.get("fast", 12), slow=mp.get("slow", 26), signal=mp.get("signal", 9),
            )
            macd_data = {
                "dif": _to_line_data(dates, macd["dif"]),
                "dea": _to_line_data(dates, macd["dea"]),
                "hist": [
                    {
                        "time": t,
                        "value": round(float(v), 6),
                        "color": _rgba(up, 0.6) if v >= 0 else _rgba(down, 0.6),
                    }
                    for t, v in zip(dates, macd["macd"])
                    if pd.notna(v)
                ],
                "difColor": MACD_DIF_COLOR,
                "deaColor": MACD_DEA_COLOR,
            }
        if "rsi" in indicators:
            rp = indicators["rsi"]
            rsi = calc_rsi(df, period=rp.get("period", 14))
            rsi_data = {"data": _to_line_data(dates, rsi), "color": RSI_COLOR}
        if "kdj" in indicators:
            kp = indicators["kdj"]
            kdj = calc_kdj(df, n=kp.get("n", 9), m1=kp.get("m1", 3), m2=kp.get("m2", 3))
            kdj_data = {
                "k": _to_line_data(dates, kdj["k"]),
                "d": _to_line_data(dates, kdj["d"]),
                "j": _to_line_data(dates, kdj["j"]),
                "kColor": KDJ_K_COLOR,
                "dColor": KDJ_D_COLOR,
                "jColor": KDJ_J_COLOR,
            }

    payload = {
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
        "boll": boll_series,
        "macd": macd_data,
        "rsi": rsi_data,
        "kdj": kdj_data,
        "options": _lwc_options(),
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
    # hasMore=false 告知组件已到最早数据; queryId 用于视口锚点失效判定
    payload["hasMore"] = has_more
    payload["queryId"] = query_id
    return payload


# 多股对比折线配色 (与 MA 调色板区分, 首色取蓝便于与红绿涨跌色区分)
COMPARE_PALETTE = ("#4ea1f3", "#f7d774", "#c883f0", "#ff9f43", "#2ccbc3", "#e15b64", "#8d9db6")


def compare_payload(
    frames: dict[str, pd.DataFrame],
    *,
    normalize: bool = True,
    period: str = "daily",
    green_up: bool = False,
    height: int = 560,
    init_bars: int = 140,
) -> dict:
    """多股对比 payload (复用 K线组件): 每只代码一条收盘价折线.

    normalize=True 时各代码按自身区间首个收盘归一化为 100 起点,
    便于不同币种/量级的代码同图比较; 交易日历不同的市场按各自数据点绘制.
    """
    lines = []
    for i, (sym, df) in enumerate(frames.items()):
        d = resample_ohlc(df, period) if period != "daily" else clean_ohlc(df)
        if d.empty:
            continue
        close = d["close"].astype(float)
        base = float(close.iloc[0])
        vals = close / base * 100 if normalize and base > 0 else close
        data = [
            {"time": t, "value": round(float(v), 4)}
            for t, v in zip(d["date"].dt.strftime("%Y-%m-%d"), vals)
            if pd.notna(v)
        ]
        if data:
            lines.append(
                {"name": sym, "color": COMPARE_PALETTE[i % len(COMPARE_PALETTE)], "data": data}
            )
    if not lines:
        raise ValueError("无有效对比数据")
    names = " vs ".join(ln["name"] for ln in lines[:4]) + (" …" if len(lines) > 4 else "")
    up, down = (INTL_UP_COLOR, INTL_DOWN_COLOR) if green_up else (CN_UP_COLOR, CN_DOWN_COLOR)
    return {
        "mode": "compare",
        "title": f"{names} · {PERIOD_LABELS.get(period, '日K')}"
        + (" · 归一化" if normalize else ""),
        "up": up,
        "down": down,
        "precision": 2,
        "minMove": 0.01,
        "initBars": init_bars,
        "height": height,
        "lines": lines,
        "candles": [],
        "volume": [],
        "mas": [],
        "boll": [],
        "macd": None,
        "rsi": None,
        "kdj": None,
        "options": _lwc_options(),
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
