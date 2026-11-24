"""K线纯计算: OHLC 清洗 / 均线 / 技术指标 / 重采样 / 摘要.

从 charting.py 拆出, 不依赖 plotly 或前端资源; CLI --json 与页面指标共用。
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
from pandas.tseries.frequencies import to_offset

OHLC_COLUMNS = ("date", "open", "high", "low", "close", "volume")
DEFAULT_MA: tuple[int, ...] = (5, 20, 60)

# 默认中国看盘软件配色: 红涨绿跌; green_up=True 时切换为国际配色 (绿涨红跌)
CN_UP_COLOR, CN_DOWN_COLOR = "#ef232a", "#14b143"
INTL_UP_COLOR, INTL_DOWN_COLOR = "#089981", "#f23648"


def up_down_colors(green_up: bool = False) -> tuple[str, str]:
    """涨跌配色: cn=红涨绿跌, green_up=True 时切换为国际绿涨红跌."""
    return (
        (INTL_UP_COLOR, INTL_DOWN_COLOR)
        if green_up
        else (CN_UP_COLOR, CN_DOWN_COLOR)
    )


MA_PALETTE = (
    "#f7d774",
    "#4ea1f3",
    "#c883f0",
    "#ff9f43",
    "#e15b64",
    "#2ccbc3",
    "#8d9db6",
)

# 技术指标配色 (与 MA 调色板区分, 便于辨识)
BOLL_UPPER_COLOR = "#e15b64"  # 布林带上轨 - 红
BOLL_MIDDLE_COLOR = "#f7d774"  # 布林带中轨 - 黄
BOLL_LOWER_COLOR = "#2ccbc3"  # 布林带下轨 - 青
MACD_DIF_COLOR = "#f7d774"  # MACD DIF 线 - 黄
MACD_DEA_COLOR = "#4ea1f3"  # MACD DEA 线 - 蓝
RSI_COLOR = "#c883f0"  # RSI 线 - 紫
KDJ_K_COLOR = "#f7d774"  # KDJ K 线 - 黄
KDJ_D_COLOR = "#4ea1f3"  # KDJ D 线 - 蓝
KDJ_J_COLOR = "#e15b64"  # KDJ J 线 - 红

PERIOD_LABELS = {"daily": "日K", "weekly": "周K", "monthly": "月K"}


def clean_ohlc(df: pd.DataFrame, *, allow_nonpositive: bool = False) -> pd.DataFrame:
    """清洗日线数据, 保证绘图与统计准确:

    - date 解析失败 / OHLC 任一缺失的行丢弃
    - 逻辑矛盾行丢弃 (high < low, high < max(open,close), low > min(open,close), 价格<=0)
    - 按日期升序排序, 重复日期保留最后一条

    allow_nonpositive: 允许 0 与负值 (利率/利差类序列, 如美债 10Y-2Y 倒挂为负)。
    默认仍按行情价格剔除 <=0 的坏数据。
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
    # inf/负成交量与 NaN 一样污染绘图/统计/JSON 序列化: 非有限置 0, 负值截到 0
    vol = pd.to_numeric(out["volume"], errors="coerce").fillna(0.0)
    out["volume"] = vol.where(np.isfinite(vol), 0.0).clip(lower=0.0)
    out = out.dropna(subset=["date", "open", "high", "low", "close"])
    # inf 与 NaN 一样会污染绘图/统计/JSON 序列化 (指标与畸形源数据均可产出)
    out = out[np.isfinite(out[["open", "high", "low", "close"]]).all(axis=1)]
    body_hi = out[["open", "close"]].max(axis=1)
    body_lo = out[["open", "close"]].min(axis=1)
    ok = (
        (out["high"] >= out["low"] - 1e-9)
        & (out["high"] >= body_hi - 1e-9)
        & (out["low"] <= body_lo + 1e-9)
    )
    if not allow_nonpositive:
        # 任一价格 <=0 都是无效行情 (停牌/坏数据), 会污染区间高低点与绘图
        ok &= (out[["open", "high", "low", "close"]] > 0).all(axis=1)
    out = out[ok]
    # 稳定排序: 重复日期保留输入中最后一条 (来源覆盖顺序有意义)
    out = out.sort_values("date", kind="stable").drop_duplicates(
        subset="date", keep="last"
    )
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


def calc_macd(
    df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9
) -> pd.DataFrame:
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

    首 period 根为预热段 (涨跌幅样本不足) → NaN; 之后用 Wilder 递推:
    avg = (prev_avg*(period-1) + 当期涨/跌) / period, 种子为前 period 个
    涨/跌幅的简单平均。全涨 (avg_loss=0) → 100; 无波动 (两者均 0) → 50。

    注: 不用 ewm(adjust=False) —— 它以首根单值播种, 会在预热段产出伪造值
    且与 Wilder 口径长期偏差 (最多十余点)。
    """
    close = df["close"].astype(float)
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    n = len(close)
    rsi = pd.Series(np.nan, index=close.index, dtype=float)
    if n <= period:
        return rsi
    # Wilder 种子: 前 period 个涨/跌幅 (index 1..period) 的简单平均, 置于 index period
    ag = float(gain.iloc[1 : period + 1].mean())
    al = float(loss.iloc[1 : period + 1].mean())
    avg_gain = [ag]
    avg_loss = [al]
    for i in range(period + 1, n):
        ag = (ag * (period - 1) + float(gain.iloc[i])) / period
        al = (al * (period - 1) + float(loss.iloc[i])) / period
        avg_gain.append(ag)
        avg_loss.append(al)
    avg_gain = np.asarray(avg_gain, dtype=float)
    avg_loss = np.asarray(avg_loss, dtype=float)
    with np.errstate(invalid="ignore", divide="ignore"):
        rs = avg_gain / avg_loss
        vals = 100 - 100 / (1 + rs)
    vals[avg_loss == 0] = 100.0  # 全涨 → 100
    vals[(avg_gain == 0) & (avg_loss == 0)] = 50.0  # 无波动 → 50
    rsi.iloc[period:] = vals
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


def _price_precision(med: float) -> int:
    """按价格中位数定显示精度: 大价 2 位; 低价币 (SHIB/PEPE ~1e-5) 逐级加细,
    保证至少 3 位有效数字, 不至于显示成 0.0000 (单只K线与加密货币对比共用).
    """
    if med <= 0 or not math.isfinite(med):
        return 4
    if med >= 20:
        return 2
    if med >= 1:
        return 3
    return min(10, max(4, math.ceil(-math.log10(med)) + 3))


def _candle_rows(dates, df: pd.DataFrame, digits: int) -> list[dict]:
    """OHLC DataFrame → lightweight-charts 蜡烛行 (按 digits 位取整)."""
    return [
        {
            "time": t,
            "open": round(float(o), digits),
            "high": round(float(h), digits),
            "low": round(float(lo), digits),
            "close": round(float(c), digits),
        }
        for t, o, h, lo, c in zip(
            dates, df["open"], df["high"], df["low"], df["close"]
        )
    ]


def _to_line_data(dates: pd.Series, srs: pd.Series, precision: int = 6) -> list[dict]:
    """将 pandas Series 转为 lightweight-charts line data [{time, value}, ...], 跳过 NaN/inf.

    inf 不能被 pd.notna 拦下 (notna(inf) 为 True), 而 allow_nan=False 的 JSON
    序列化会直接抛 TypeError, 故显式剔除。
    """
    return [
        {"time": t, "value": round(float(v), precision)}
        for t, v in zip(dates, srs)
        if pd.notna(v) and math.isfinite(float(v))
    ]


def _month_rule() -> str:
    """pandas 3.x 移除了 'M'; 用偏移解析探测而非版本字符串比较 (2.10+ 会误判)."""
    try:
        to_offset("ME")
        return "ME"
    except ValueError:
        return "M"


def resample_ohlc(
    df: pd.DataFrame, period: str, *, allow_nonpositive: bool = False
) -> pd.DataFrame:
    """日线重采样为周K (W-FRI) / 月K; open=首日开, high=最高, low=最低, close=末日收, volume=求和.

    已经处于目标粒度 (或更粗) 的数据直接清洗返回: 调用方常先 resolve_period
    再无条件 resample (K线页/CLI/对比 payload), 对原生周K/月K源 (水泥网、
    Cameco 等) 二次重采样会把周一/月末标签改写成 W-FRI/月末, 甚至跨周合并。
    allow_nonpositive 透传给 clean_ohlc, 供利率/利差等可负值序列使用。
    """
    if df is None or df.empty:
        return pd.DataFrame(columns=list(OHLC_COLUMNS))
    if period == "daily":
        return df.reset_index(drop=True)[list(OHLC_COLUMNS)]
    if period in PERIOD_ORDER:
        native = infer_native_period(df)
        if PERIOD_ORDER.index(period) <= PERIOD_ORDER.index(native):
            return clean_ohlc(df, allow_nonpositive=allow_nonpositive)[
                list(OHLC_COLUMNS)
            ]
    rule = "W-FRI" if period == "weekly" else _month_rule()
    out = (
        clean_ohlc(df, allow_nonpositive=allow_nonpositive)
        .set_index("date")
        .resample(rule)
        .agg(
            {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
            }
        )
        .dropna(subset=["open", "high", "low", "close"])
        .reset_index()
    )
    return out[list(OHLC_COLUMNS)]


PERIOD_ORDER = ("daily", "weekly", "monthly")


def infer_native_period(df: pd.DataFrame) -> str:
    """从K线数据推断原生粒度 (相邻bar日期中位间隔): 日/周/月.

    数据源只给周K (如水泥网 CEMPI 周K端点) 时中位间隔 ~7 天 → "weekly"。
    样本不足 (<3 根) 无法推断, 按最细粒度 "daily" 放行 (不虚构降级)。
    """
    if df is None or df.empty or "date" not in df:
        return "daily"
    d = pd.to_datetime(df["date"], errors="coerce").dropna().sort_values()
    if len(d) < 3:
        return "daily"
    med = float(d.diff().dt.days.median())
    if med <= 2:
        return "daily"
    if med <= 10:
        return "weekly"
    return "monthly"


def available_periods(native: str) -> list[str]:
    """原生粒度下可选的周期: 细于原生的一律不可选 (重采样只会日→周→月单向变粗)."""
    return list(PERIOD_ORDER[PERIOD_ORDER.index(native) :])


def resolve_period(df: pd.DataFrame, requested: str) -> str:
    """请求周期细于数据原生粒度时降级到原生粒度, 返回生效周期.

    数据层兜底: 目录/源标注缺失或漂移时, 周K数据请求日K不再伪装成日K。
    """
    if requested not in PERIOD_ORDER:
        return requested
    native = infer_native_period(df)
    if PERIOD_ORDER.index(requested) >= PERIOD_ORDER.index(native):
        return requested
    return native


def _non_trading_days(dates: pd.Series) -> list[pd.Timestamp]:
    """区间内不在数据中的日历日 (周末+节假日), 用于 rangebreaks 消除图表空隙."""
    if len(dates) < 2:
        return []
    idx = pd.DatetimeIndex(pd.to_datetime(dates))
    full = pd.date_range(idx.min(), idx.max(), freq="D")
    return list(full.difference(idx))


def summarize_ohlc(
    df: pd.DataFrame, mas=DEFAULT_MA, *, allow_nonpositive: bool = False
) -> dict:
    """K线摘要统计 (用于终端输出 / --json / 页面指标)."""
    df = clean_ohlc(df, allow_nonpositive=allow_nonpositive)
    if df.empty:
        return {"bars": 0}
    first, last = df.iloc[0], df.iloc[-1]
    # 跨零/负值序列的百分比涨跌没有意义 (利差从 -0.4 到 -0.2 并非 -50%),
    # 仅当首尾收盘都为正是才给出百分比。
    change_pct = (
        (float(last["close"]) / float(first["close"]) - 1) * 100
        if float(first["close"]) > 0 and float(last["close"]) > 0
        else None
    )
    hi = df.loc[df["high"].idxmax()]
    lo = df.loc[df["low"].idxmin()]
    # 精度按价格中位数定: 硬编码 4 位会把 SHIB/PEPE 一类低价币的摘要
    # (终端/--json/页面指标) 全部归零; 正常价位仍保持 4 位 (max(4, ...)).
    nd = max(4, _price_precision(float(df["close"].median())))
    ma_vals: dict[str, float | None] = {}
    for n, srs in compute_ma(df, mas).items():
        v = srs.iloc[-1]
        ma_vals[f"ma{n}"] = None if pd.isna(v) else round(float(v), nd)
    return {
        "bars": int(len(df)),
        "first_date": first["date"].strftime("%Y-%m-%d"),
        "last_date": last["date"].strftime("%Y-%m-%d"),
        "first_close": round(float(first["close"]), nd),
        "last_open": round(float(last["open"]), nd),
        "last_high": round(float(last["high"]), nd),
        "last_low": round(float(last["low"]), nd),
        "last_close": round(float(last["close"]), nd),
        "last_volume": float(last["volume"]) if pd.notna(last["volume"]) else None,
        "change_pct": round(change_pct, 4) if change_pct is not None else None,
        # 展示精度: 低价币 (SHIB/PEPE 等 ~1e-5) 若调用方硬编码 3 位小数会全部
        # 显示成 0.000; CLI/UI 用这个字段格式化摘要 (与上面四舍五入口径一致)。
        "precision": nd,
        "period_high": round(float(hi["high"]), nd),
        "period_high_date": hi["date"].strftime("%Y-%m-%d"),
        "period_low": round(float(lo["low"]), nd),
        "period_low_date": lo["date"].strftime("%Y-%m-%d"),
        "ma": ma_vals,
    }
