"""lightweight-charts 页面 payload (K线 / 对比 / 比例线 / 加密货币).

只负责数据整形与前端资源加载, 不做网络请求; 与 plotly CLI 渲染路径隔离。
"""

from __future__ import annotations

from bisect import bisect_right
from functools import lru_cache
import math

import pandas as pd

from .charting_core import (
    BOLL_LOWER_COLOR,
    BOLL_MIDDLE_COLOR,
    BOLL_UPPER_COLOR,
    DEFAULT_MA,
    OHLC_COLUMNS,
    KDJ_D_COLOR,
    KDJ_J_COLOR,
    KDJ_K_COLOR,
    MACD_DEA_COLOR,
    MACD_DIF_COLOR,
    MA_PALETTE,
    PERIOD_LABELS,
    RSI_COLOR,
    _candle_rows,
    _price_precision,
    _to_line_data,
    calc_boll,
    calc_kdj,
    calc_macd,
    calc_rsi,
    clean_ohlc,
    compute_ma,
    resample_ohlc,
    up_down_colors,
)
from .ctx_menu_js import CTX_MENU_KIT_JS
from .paths import read_asset

# ---------- Streamlit 页面渲染: lightweight-charts (TradingView 开源内核) ----------
#
# 与 plotly 的关键交互差异 (券商 App 通用习惯):
#   拖动 = 平移 (带惯性); 滚轮/捏合 = 围绕光标缩放时间轴;
#   触控板双指横向滑动 = 平移 (wheel deltaX), 双指纵向/捏合 = 缩放 (deltaY);
#   触控板捏合跨浏览器兼容: Chromium/Firefox 的 ctrl+wheel 与 Safari gesture 事件;
#   价格轴随可见区间自动缩放; 十字光标 + 顶部 OHLC 信息栏.

@lru_cache(maxsize=1)
def _lwc_source() -> str:
    """vendored lightweight-charts v5 standalone (Apache-2.0, 离线内嵌).

    从包内 assets/ 读 (随 wheel 分发); importlib.resources 对 zip 安装
    同样成立 (自动解到临时文件), 不再依赖 __file__ 相对路径。
    """
    from importlib.resources import files

    asset = files(__package__).joinpath(
        "assets", "lightweight-charts.standalone.production.js"
    )
    return asset.read_text(encoding="utf-8")


def _rgba(hex_color: str, alpha: float) -> str:
    h = hex_color.lstrip("#")
    return f"rgba({int(h[0:2], 16)},{int(h[2:4], 16)},{int(h[4:6], 16)},{alpha})"


# 组件 DOM 结构 (st.components.v2, isolate_styles=True → ShadowRoot 内渲染).
KLINE_COMPONENT_HTML = read_asset("kline_component.html")

KLINE_COMPONENT_CSS = read_asset("kline_component.css")


_LWC_COMPONENT_MODULE = CTX_MENU_KIT_JS + read_asset("kline_component.js")


def _lwc_options() -> dict:
    """lightweight-charts 通用图表选项 (K线与多股对比共用)."""
    return {
        "autoSize": True,
        "layout": {
            "background": {"type": "solid", "color": "#0a0a0f"},
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
                "color": "rgba(150,170,200,0.55)",
                "width": 1,
                "style": 2,
                "labelBackgroundColor": "#2a2e39",
            },
            "horzLine": {
                "color": "rgba(150,170,200,0.55)",
                "width": 1,
                "style": 2,
                "labelBackgroundColor": "#2a2e39",
            },
        },
        "rightPriceScale": {"borderColor": "rgba(120,140,180,0.25)"},
        "timeScale": {
            "borderColor": "rgba(120,140,180,0.25)",
            "rightOffset": 4,
            "barSpacing": 9,
            "minBarSpacing": 1.2,
            "timeVisible": False,
            "secondsVisible": False,
        },
        "handleScroll": {
            "pressedMouseMove": True,
            "mouseWheel": True,
            "horzTouchDrag": True,
            "vertTouchDrag": False,
        },
        "handleScale": {
            "mouseWheel": True,
            "pinch": True,
            "axisPressedMouseMove": True,
            "axisDoubleClickReset": True,
        },
        "kineticScroll": {"touch": True, "mouse": True},
        "localization": {"locale": "zh-CN"},
    }


def kline_component_js() -> str:
    """组件完整 JS 源: vendored lightweight-charts (UMD → window.LightweightCharts) + 挂载逻辑."""
    return _lwc_source() + "\n" + _LWC_COMPONENT_MODULE


def _base_payload(
    *,
    title: str,
    up: str,
    down: str,
    precision: int | None = None,
    init_bars: int = 140,
    height: int = 560,
) -> dict:
    """四种图表 payload 的公共外壳 (K线/对比/折线/加密货币对比共用).

    这些键必须始终存在: 前端 JS 在模式切换时按固定字段读取, 漏一个就会
    让同一组件在另一种图表下进 undefined 分支。precision=None 用于加密
    货币对比模式 (精度按每个币各自给出, 顶层不声明)。
    """
    payload: dict = {
        "title": title,
        "up": up,
        "down": down,
        "initBars": init_bars,
        "height": height,
        "candles": [],
        "volume": [],
        "mas": [],
        "boll": [],
        "macd": None,
        "rsi": None,
        "kdj": None,
        # 单只K线的可选叠加曲线 (如指数页的金银比/金油比): 独立左侧价轴.
        # 固定给出 None, 前端按字段存在性读取, 避免模式切换时进 undefined 分支.
        "overlay": None,
        # 可选水平参考线 (如 10Y-2Y 利差的 0 倒挂分界); 固定字段保证前端
        # 在模式切换时不会读到 undefined.
        "reference": None,
        "referenceLabel": "",
        "options": _lwc_options(),
    }
    if precision is not None:
        payload["precision"] = precision
        payload["minMove"] = float(10 ** (-precision))
    return payload


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
    query_id: str | None = None,
    overlay: dict | None = None,
    allow_nonpositive: bool = False,
    reference: float | None = None,
    reference_label: str | None = None,
) -> dict:
    """生成传给 K线组件 (st.components.v2) 的数据 payload.

    组件交互: 拖动平移 (带惯性) / 滚轮·捏合缩放时间轴 / 触控板双指横滑平移 /
    十字光标 OHLC 信息栏 / 价格轴随可见区间自适应 / 副图分隔线可拖拽 / 双击轴复位.

    indicators: 可选, 键为指标名 (macd/rsi/kdj/boll), 值为参数 dict;
                如 {"macd": {"fast": 12, "slow": 26, "signal": 9}, "rsi": {"period": 14}}.
    query_id: 查询标识 (如代码), 变化时组件丢弃视口锚点回退初始区间.
    overlay: 可选叠加曲线 (如 build_ratio_series 生成的比率线): 画在独立左侧
             价轴, 不改变蜡烛/均线的右侧价格轴。
    allow_nonpositive: 允许 0/负值 (利率与利差序列, 如 10Y-2Y 倒挂为负)。
    reference / reference_label: 主图水平参考线 (如利差的 0 倒挂分界)。
    """
    df = clean_ohlc(df, allow_nonpositive=allow_nonpositive)
    if df.empty:
        raise ValueError(f"{symbol}: 无有效K线数据")
    up, down = up_down_colors(green_up)
    dates = df["date"].dt.strftime("%Y-%m-%d")
    # 精度按价格中位数定 (低价币 SHIB/PEPE ~1e-5 需 8~10 位): 硬编码 round(,6)
    # 会把 1e-5 显示误差放大到百分之几、<1e-6 直接归零, 故蜡烛/MA 统一用
    # nd=max(6, precision), 与对外声明的 priceFormat.precision 一致
    med = float(df["close"].median())
    precision = _price_precision(med)
    nd = max(6, precision)
    candles = _candle_rows(dates, df, nd)
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
            {"time": t, "value": round(float(v), nd)}
            for t, v in zip(dates, srs)
            if pd.notna(v)
        ]
        if data:
            ma_series.append(
                {
                    "name": f"MA{n}",
                    "color": MA_PALETTE[i % len(MA_PALETTE)],
                    "data": data,
                }
            )
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
                data = _to_line_data(dates, boll[col], precision=nd)
                if data:
                    boll_series.append({"name": name, "color": color, "data": data})
        if "macd" in indicators:
            mp = indicators["macd"]
            macd = calc_macd(
                df,
                fast=mp.get("fast", 12),
                slow=mp.get("slow", 26),
                signal=mp.get("signal", 9),
            )
            macd_data = {
                "dif": _to_line_data(dates, macd["dif"], precision=nd),
                "dea": _to_line_data(dates, macd["dea"], precision=nd),
                "hist": [
                    {
                        "time": t,
                        "value": round(float(v), nd),
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
            rsi_data = {
                "data": _to_line_data(dates, rsi, precision=nd),
                "color": RSI_COLOR,
            }
        if "kdj" in indicators:
            kp = indicators["kdj"]
            kdj = calc_kdj(df, n=kp.get("n", 9), m1=kp.get("m1", 3), m2=kp.get("m2", 3))
            kdj_data = {
                "k": _to_line_data(dates, kdj["k"], precision=nd),
                "d": _to_line_data(dates, kdj["d"], precision=nd),
                "j": _to_line_data(dates, kdj["j"], precision=nd),
                "kColor": KDJ_K_COLOR,
                "dColor": KDJ_D_COLOR,
                "jColor": KDJ_J_COLOR,
            }

    payload = _base_payload(
        title=title,
        up=up,
        down=down,
        precision=precision,
        init_bars=init_bars,
        height=height,
    )
    payload.update(
        {
            "candles": candles,
            "volume": vols if show_volume else [],
            "mas": ma_series,
            "boll": boll_series,
            "macd": macd_data,
            "rsi": rsi_data,
            "kdj": kdj_data,
            "overlay": overlay,
            "reference": reference,
            "referenceLabel": reference_label or "",
            "candleOpts": {
                "upColor": up,
                "downColor": down,
                "wickUpColor": up,
                "wickDownColor": down,
                "borderVisible": False,
                "priceLineVisible": True,
                "priceLineStyle": 2,
                "priceLineColor": _rgba(
                    up
                    if last["close"]
                    >= (
                        float(df["close"].iloc[-2])
                        if len(df) > 1
                        else float(last["open"])
                    )
                    else down,
                    0.7,
                ),
                "priceFormat": {
                    "type": "price",
                    "precision": precision,
                    "minMove": 10**-precision,
                },
            },
            # queryId 用于视口锚点失效判定 (换代码后回退初始区间);
            # 未显式传入时按代码兜底, 指数页等复用 kline_payload 的路径
            # 也能在数据更新后保留拖动位置。
            "queryId": query_id or symbol,
            # 右键菜单「复制代码」只复制标的本身, 不把周期/币种/均线一起带上
            "copyText": symbol,
        }
    )
    return payload


# 比例线颜色 (可选叠加线: 两标的收盘价的具体比值, 独立右侧价轴)
RATIO_COLOR = "#ff5da2"


# 多股对比折线配色 (与 MA 调色板区分, 首色取蓝便于与红绿涨跌色区分)
COMPARE_PALETTE = (
    "#4ea1f3",
    "#f7d774",
    "#c883f0",
    "#ff9f43",
    "#2ccbc3",
    "#e15b64",
    "#8d9db6",
)


def _close_rows(df: pd.DataFrame) -> tuple[list[str], list[float]]:
    """有效收盘价行 (日期字符串升序, 跳过 NaN/inf/非正数)."""
    pairs = [
        (t, float(v))
        for t, v in zip(df["date"].dt.strftime("%Y-%m-%d"), df["close"])
        if pd.notna(v) and math.isfinite(float(v)) and float(v) > 0
    ]
    pairs.sort()
    return [p[0] for p in pairs], [p[1] for p in pairs]


def _ohlc_rows(df: pd.DataFrame) -> list[tuple[str, float, float, float, float]]:
    """有效 OHLC 行 (日期字符串升序, 四价均需为正的有限数)."""
    rows = []
    for t, o, h, lo, c in zip(
        df["date"].dt.strftime("%Y-%m-%d"),
        df["open"],
        df["high"],
        df["low"],
        df["close"],
    ):
        vals = (o, h, lo, c)
        if all(
            pd.notna(v) and math.isfinite(float(v)) and float(v) > 0 for v in vals
        ):
            rows.append((t, float(o), float(h), float(lo), float(c)))
    rows.sort()
    return rows


def build_ratio_series(
    frames: dict[str, pd.DataFrame],
    numerator: str,
    denominator: str,
    *,
    period: str = "daily",
    fx_df: pd.DataFrame | None = None,
    fx_pair: str | None = None,
    fx_missing: bool = False,
) -> dict:
    """两标的收盘价比值线 (可选功能): numerator / denominator, 按日期对齐.

    - 不同市场交易日历不同: 分母取「≤ 该日期」的最近一个收盘价
    - fx_df 为 1 分母币种 → 分子币种 的历史汇率日线 (date/close):
      比例 = 分子价 / (分母价 × 汇率), 跨币种比较才不会被汇率波动主导
    - 返回可直接渲染的 payload (data / mean / precision / 汇率元信息)
    """
    if numerator == denominator:
        raise ValueError("比例线的分子与分母不能是同一代码")
    missing = [c for c in (numerator, denominator) if c not in frames]
    if missing:
        raise ValueError(f"比例线代码不在对比数据中: {', '.join(missing)}")

    def _prepared(sym: str) -> pd.DataFrame:
        df = frames[sym]
        return resample_ohlc(df, period) if period != "daily" else clean_ohlc(df)

    num, den = _prepared(numerator), _prepared(denominator)
    if num.empty or den.empty:
        raise ValueError("比例线代码无有效K线数据")

    den_times, den_vals = _close_rows(den)
    fx_times: list[str] = []
    fx_vals: list[float] = []
    if fx_df is not None and not fx_df.empty:
        fx_clean = fx_df.copy()
        fx_clean["date"] = pd.to_datetime(fx_clean["date"], errors="coerce")
        fx_clean["close"] = pd.to_numeric(fx_clean["close"], errors="coerce")
        fx_clean = fx_clean.dropna(subset=["date", "close"])
        fx_clean = fx_clean[fx_clean["close"] > 0]
        if not fx_clean.empty:
            fx_times, fx_vals = _close_rows(fx_clean)

    raw: list[tuple[str, float]] = []
    for t, v in zip(num["date"].dt.strftime("%Y-%m-%d"), num["close"]):
        if pd.isna(v) or not math.isfinite(float(v)) or float(v) <= 0:
            continue
        j = bisect_right(den_times, t) - 1
        if j < 0:
            continue
        rate = 1.0
        if fx_times:
            k = bisect_right(fx_times, t) - 1
            if k < 0:
                continue
            rate = fx_vals[k]
        if rate <= 0:
            continue
        ratio = float(v) / (den_vals[j] * rate)
        if math.isfinite(ratio):
            raw.append((t, ratio))
    if not raw:
        raise ValueError("比例线无可对齐的数据 (两标的日期区间无交集)")

    med = float(pd.Series([v for _, v in raw]).median())
    precision = max(4, _price_precision(med))
    values = [v for _, v in raw]
    mean = sum(values) / len(values)
    return {
        "name": f"{numerator} / {denominator}",
        "numerator": numerator,
        "denominator": denominator,
        "color": RATIO_COLOR,
        "precision": precision,
        "mean": round(mean, precision),
        "fxAdjusted": bool(fx_times),
        "fxPair": fx_pair if fx_times else None,
        "fxMissing": bool(not fx_times and (fx_missing or fx_df is not None)),
        "data": [
            {"time": t, "value": round(v, precision)} for t, v in raw
        ],
    }


def build_ratio_ohlc(
    frames: dict[str, pd.DataFrame],
    numerator: str,
    denominator: str,
    *,
    period: str = "daily",
    fx_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """两标的比值的 OHLC K线 (分子 ÷ 分母), 可直接喂 kline_payload.

    与 build_ratio_series 的区别: 这里返回完整 OHLC DataFrame, 让比值本身
    画成蜡烛图 (Roadmap 的「K 线比值模式」), 而不是叠加折线。

    - 日/周/月对齐与比例线一致: 分母缺该交易日时沿用「≤ 当日」的最近收盘
    - 同日双方都有数据时: open/close 按对应价相除; high/low 用
      分子高 ÷ 分母低、分子低 ÷ 分母高 作为比值当日的上下界
    - fx_df 为 1 分母币种 → 分子币种的历史汇率日线 (date/close)
    - 比值没有成交量语义, volume 统一为 0 (前端会自动省略成交量副图)
    """
    if numerator == denominator:
        raise ValueError("比值的分子与分母不能是同一代码")
    missing = [c for c in (numerator, denominator) if c not in frames]
    if missing:
        raise ValueError(f"比值K线代码不在数据中: {', '.join(missing)}")

    def _prepared(sym: str) -> pd.DataFrame:
        df = frames[sym]
        return resample_ohlc(df, period) if period != "daily" else clean_ohlc(df)

    num_rows = _ohlc_rows(_prepared(numerator))
    den_rows = _ohlc_rows(_prepared(denominator))
    if not num_rows or not den_rows:
        raise ValueError("比值K线代码无有效K线数据")

    den_times = [r[0] for r in den_rows]
    fx_times: list[str] = []
    fx_vals: list[float] = []
    if fx_df is not None and not fx_df.empty:
        fx_clean = fx_df.copy()
        fx_clean["date"] = pd.to_datetime(fx_clean["date"], errors="coerce")
        fx_clean["close"] = pd.to_numeric(fx_clean["close"], errors="coerce")
        fx_clean = fx_clean.dropna(subset=["date", "close"])
        fx_clean = fx_clean[fx_clean["close"] > 0]
        if not fx_clean.empty:
            fx_times, fx_vals = _close_rows(fx_clean)

    rows = []
    for t, o, h, lo, c in num_rows:
        j = bisect_right(den_times, t) - 1
        if j < 0:
            continue
        if den_times[j] == t:
            _, d_open, d_high, d_low, d_close = den_rows[j]
        else:
            # 分母当日休市: 用最近收盘价缩放分子的全部四价, 蜡烛仍保留
            # 分子自身的日内振幅, 与比例线的「≤ 当日」口径一致
            _, _, _, _, d_close = den_rows[j]
            d_open = d_high = d_low = d_close
        rate = 1.0
        if fx_times:
            k = bisect_right(fx_times, t) - 1
            if k < 0:
                continue
            rate = fx_vals[k]
        if rate <= 0:
            continue
        vals = (
            o / d_open / rate,
            h / d_low / rate,
            lo / d_high / rate,
            c / d_close / rate,
        )
        if all(math.isfinite(v) and v > 0 for v in vals):
            rows.append(
                {
                    "date": pd.Timestamp(t),
                    "open": vals[0],
                    "high": vals[1],
                    "low": vals[2],
                    "close": vals[3],
                    "volume": 0.0,
                }
            )
    if not rows:
        raise ValueError("比值K线无可对齐的数据 (两标的日期区间无交集)")
    return pd.DataFrame(rows, columns=list(OHLC_COLUMNS))


def compare_payload(
    frames: dict[str, pd.DataFrame],
    *,
    normalize: bool = True,
    period: str = "daily",
    green_up: bool = False,
    height: int = 560,
    init_bars: int = 140,
    ratio: dict | None = None,
    title: str | None = None,
    query_id: str | None = None,
) -> dict:
    """多股对比 payload (复用 K线组件): 每只代码一条收盘价折线.

    normalize=True 时各代码按自身区间首个收盘归一化为 100 起点,
    便于不同币种/量级的代码同图比较; 交易日历不同的市场按各自数据点绘制.
    ratio: 可选的比例线 payload (build_ratio_series 生成), 同图使用独立纵轴.
    title: 图表标题覆盖 (默认走势对比 · 代码 · 周期; 市值模式标注币种口径).
    """
    lines = []
    precisions = []
    for sym, df in frames.items():
        d = resample_ohlc(df, period) if period != "daily" else clean_ohlc(df)
        if d.empty:
            continue
        close = d["close"].astype(float)
        base = float(close.iloc[0])
        if normalize and base > 0:
            vals = close / base * 100
            nd = 4  # 归一化后 ~100 量级, 4 位足够
        else:
            vals = close
            # 原始价格: 按自身中位数定精度, 避免低价币 (SHIB ~1e-5) 被 round(,4) 归零
            nd = max(6, _price_precision(float(close.median())))
        precisions.append(nd)
        data = [
            {"time": t, "value": round(float(v), nd)}
            for t, v in zip(d["date"].dt.strftime("%Y-%m-%d"), vals)
            if pd.notna(v)
        ]
        if data:
            lines.append(
                {
                    "name": sym,
                    # 用输出序号取色: 输入序号 i 会在前面某只代码被清空
                    # (清洗后无有效K线) 时让第一条可见线拿到 PALETTE[1],
                    # 与「首色取蓝便于与红绿涨跌色区分」的意图不符
                    "color": COMPARE_PALETTE[len(lines) % len(COMPARE_PALETTE)],
                    "data": data,
                }
            )
    if not lines:
        raise ValueError("无有效对比数据")
    names = " vs ".join(ln["name"] for ln in lines[:4]) + (
        " …" if len(lines) > 4 else ""
    )
    up, down = up_down_colors(green_up)
    # 原始价格模式按最细精度展示 (低价币), 归一化模式 ~100 量级用 2 位
    payload_precision = (max(precisions) if precisions else 2) if not normalize else 2
    ratio = ratio or None
    axis = {p["time"] for ln in lines for p in ln["data"]}
    if ratio:
        axis |= {p["time"] for p in ratio.get("data", [])}
    payload = _base_payload(
        title=title
        or (
            f"{names} · {PERIOD_LABELS.get(period, '日K')}"
            + (" · 归一化" if normalize else "")
        ),
        up=up,
        down=down,
        precision=payload_precision,
        init_bars=init_bars,
        height=height,
    )
    payload.update(
        {
            "mode": "compare",
            "lines": lines,
            "ratio": ratio,
            # 全系列时间轴 (并集, 升序去重): 视口锚点的索引空间 (对比模式
            # candles 为空, 不提供 axis 的话拖动位置无法跨重绘还原)
            "axis": sorted(axis),
            # 视口锚点 key: 显式传入优先, 否则按模式/周期/代码集/归一化方式
            # 派生稳定标识。不提供 queryId 时 JS 会把锚点写到 undefined 键,
            # 对比图每次数据更新都会重置缩放位置。
            "queryId": query_id
            or f"compare:{period}:{int(bool(normalize))}:"
            + ",".join(ln["name"] for ln in lines),
            # 右键「复制代码」: 对比模式复制全部参与代码, 空格分隔便于直接粘贴
            "copyText": " ".join(ln["name"] for ln in lines),
        }
    )
    return payload


def line_payload(
    df: pd.DataFrame,
    *,
    value_col: str = "close",
    title: str | None = None,
    series_name: str = "value",
    precision: int = 2,
    reference: float | None = None,
    reference_label: str | None = None,
    color: str | None = None,
    green_up: bool = False,
    height: int = 560,
    init_bars: int = 140,
    query_id: str | None = None,
    unit: str = "",
) -> dict:
    """单序列折线 payload (复用 K线组件的 compare 模式).

    与 compare_payload 的区别: 不按首值归一化, 直接绘制原始值 (适合溢价率 /
    比率 / 单一指标曲线); reference 可画一条水平参考线 (如 0% 同价线),
    unit 只用于图题与悬浮提示的数值后缀 (如 "%"), 不参与坐标换算。
    """
    if df is None or df.empty:
        raise ValueError("无有效折线数据")
    if value_col not in df.columns:
        raise ValueError(f"缺少折线数值列: {value_col}")
    d = pd.DataFrame(
        {
            "date": pd.to_datetime(df["date"], errors="coerce"),
            value_col: pd.to_numeric(df[value_col], errors="coerce"),
        }
    )
    d = d.dropna(subset=["date", value_col])
    d = d[d[value_col].map(lambda v: math.isfinite(float(v)))]
    d = d.sort_values("date", kind="stable").drop_duplicates(
        subset="date", keep="last"
    )
    if d.empty:
        raise ValueError("无有效折线数据")
    data = _to_line_data(
        d["date"].dt.strftime("%Y-%m-%d"), d[value_col], precision=precision
    )
    if not data:
        raise ValueError("无有效折线数据")
    up, down = up_down_colors(green_up)
    payload = _base_payload(
        title=title or series_name,
        up=up,
        down=down,
        precision=precision,
        init_bars=init_bars,
        height=height,
    )
    payload.update(
        {
            "mode": "compare",
            "lines": [
                {
                    "name": series_name,
                    "color": color or COMPARE_PALETTE[0],
                    "data": data,
                }
            ],
            "ratio": None,
            # compare 模式下的水平参考线 (如 AH 溢价 0% 同价线)
            "reference": reference,
            "referenceLabel": reference_label,
            # 数值后缀 (如 "%"): 悬浮提示展示 "30.0%" 而不是裸数字
            "unit": unit,
            # 单序列折线不显示「相对首日涨跌」: 溢价率/比率本身已是百分比,
            # 再叠一个相对涨幅容易被误读成百分点变化
            "showChange": False,
            "queryId": query_id,
            "axis": [p["time"] for p in data],
            "copyText": series_name,
        }
    )
    return payload


def crypto_compare_payload(
    frames: dict[str, pd.DataFrame],
    *,
    period: str = "daily",
    green_up: bool = False,
    height: int = 560,
    init_bars: int = 140,
    query_id: str | None = None,
) -> dict:
    """加密货币对比 payload (复用 K线组件): 每币一个独立窗格的蜡烛图.

    只接受加密货币代码; 不做归一化 —— 各币按原始价格绘制, 独立价轴,
    共享时间轴与十字光标 (窗格分隔线可拖拽调高)。
    """
    coins = []
    for sym, df in frames.items():
        d = resample_ohlc(df, period) if period != "daily" else clean_ohlc(df)
        if d.empty:
            continue
        dates = d["date"].dt.strftime("%Y-%m-%d")
        # 低价币 (SHIB/PEPE ~1e-5) 需要比单只K线更细的价格精度才不至于显示为 0.0000:
        # 与 kline_payload 一致用 nd=max(6, precision) 取整, 保证蜡烛数据不低于
        # 对外声明的 priceFormat.precision (否则溢出统计, UI 显示的位数是空的)。
        precision = _price_precision(float(d["close"].median()))
        nd = max(6, precision)
        candles = _candle_rows(dates, d, nd)
        if not candles:
            continue
        coins.append(
            {
                "name": sym,
                # 同 compare_payload: 输出序号取色, 空数据代码不占位
                "color": COMPARE_PALETTE[len(coins) % len(COMPARE_PALETTE)],
                "precision": precision,
                "minMove": 10**-precision,
                "candles": candles,
            }
        )
    if not coins:
        raise ValueError("无有效对比数据")
    names = " vs ".join(cn["name"] for cn in coins[:4]) + (
        " …" if len(coins) > 4 else ""
    )
    up, down = up_down_colors(green_up)
    payload = _base_payload(
        title=f"{names} · {PERIOD_LABELS.get(period, '日K')}",
        up=up,
        down=down,
        init_bars=init_bars,
        height=height,
    )
    payload.update(
        {
            "mode": "crypto",
            "coins": coins,
            # 全系列时间轴 (并集, 升序去重): 视口锚点的索引空间 (同 compare 模式)
            "axis": sorted({c["time"] for cn in coins for c in cn["candles"]}),
            # 同 compare_payload: 无 queryId 时视口锚点写到 undefined 键
            "queryId": query_id
            or f"crypto:{period}:" + ",".join(cn["name"] for cn in coins),
            "copyText": " ".join(cn["name"] for cn in coins),
        }
    )
    return payload


