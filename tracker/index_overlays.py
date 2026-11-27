"""指数K线可叠加指标: 比率曲线 + FRED OAS 信用利差.

金银比/金油比的定义与取数契约见 ``tracker.index_ratios``; 本模块补充
单序列叠加指标, 把 FRED (ICE BofA) 信用利差 OAS 作为 CDS 的免费代理
画在指数K线上:

- IX.IG_OAS  美国投资级信用利差 (BAMLC0A0CM)
- IX.BBB_OAS 美国 BBB 级信用利差 (BAMLC0A4CBBB)
- IX.HY_OAS  美国高收益信用利差 (BAMLH0A0HYM2)
- IX.CCC_OAS 美国 CCC 级信用利差 (BAMLH0A3HYC)

OAS 单位为百分点 (3.85 = 3.85%), 与指数点位/比率的量级不同, 前端统一
放在独立左侧价轴, 不改变蜡烛与均线的右侧价格轴。FRED 对 ICE BofA 系列
只提供约 3 年滚动窗口, 超出部分自然从有数据处开始。

注意: OAS 是现金债利差, 不是严格意义上的 CDS 报价; 在拿到 Markit/ICE
的 CDX/iTraxx 数据前, 它是最接近且零成本的信用风险代理。
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
import math

import pandas as pd

from . import charting
from .index_ratios import RATIO_PRESETS
from .symbols import INDEX_CATALOG


@dataclass(frozen=True)
class SeriesPreset:
    """一个可直接叠加的单序列指标 (目前为 FRED OAS 信用利差)."""

    key: str
    symbol: str
    label: str
    color: str
    precision: int = 2
    unit: str = "%"


# 颜色与 MA/技术指标调色板错开, 避免同一张图上出现同色曲线。
OAS_PRESETS: tuple[SeriesPreset, ...] = (
    SeriesPreset(
        key="ig_oas",
        symbol="IX.IG_OAS",
        label="美国投资级信用利差 (IG OAS)",
        color="#4ea1f3",
    ),
    SeriesPreset(
        key="bbb_oas",
        symbol="IX.BBB_OAS",
        label="美国 BBB 级信用利差 (BBB OAS)",
        color="#2ccbc3",
    ),
    SeriesPreset(
        key="hy_oas",
        symbol="IX.HY_OAS",
        label="美国高收益信用利差 (HY OAS)",
        color="#ff9f43",
    ),
    SeriesPreset(
        key="ccc_oas",
        symbol="IX.CCC_OAS",
        label="美国 CCC 级信用利差 (CCC OAS)",
        color="#e15b64",
    ),
)

SERIES_PRESET_BY_KEY: dict[str, SeriesPreset] = {p.key: p for p in OAS_PRESETS}
SERIES_PRESET_BY_LABEL: dict[str, SeriesPreset] = {p.label: p for p in OAS_PRESETS}
SERIES_PRESET_BY_SYMBOL: dict[str, SeriesPreset] = {p.symbol: p for p in OAS_PRESETS}


def series_preset_for_label(label: str) -> SeriesPreset | None:
    """下拉框展示名 → 单序列预设; 「无」/比率标签/未知标签返回 None."""
    return SERIES_PRESET_BY_LABEL.get(str(label or "").strip())


def series_preset_for_index(symbol: str) -> SeriesPreset | None:
    """指数代码 (IX.HY_OAS 等) → 单序列预设; 其它指数返回 None."""
    return SERIES_PRESET_BY_SYMBOL.get(str(symbol or "").strip().upper())


def overlay_option_labels() -> list[str]:
    """叠加指标下拉选项: 「无」+ 比率预设 + OAS 信用利差预设."""
    return ["无"] + [p.label for p in RATIO_PRESETS] + [p.label for p in OAS_PRESETS]


def _validate_presets() -> None:
    """导入时自检: 只引用已收录的 FRED 指数, 且下拉标签不重名.

    预设写错 (目录改名/删条目) 时应在开发阶段直接暴露, 而不是等页面
    取数时报「未收录的指数代码」; 标签重名会让 st.selectbox 出现重复项,
    用户无法区分两条叠加曲线。
    """
    for p in OAS_PRESETS:
        key = p.symbol[3:]
        entry = INDEX_CATALOG.get(key)
        if entry is None:
            raise ValueError(f"叠加指标 {p.key} 引用了未收录指数: {p.symbol}")
        if "fred" not in entry:
            raise ValueError(f"叠加指标 {p.key} 必须指向 FRED 指数: {p.symbol}")
    labels = overlay_option_labels()
    if len(labels) != len(set(labels)):
        raise ValueError("叠加指标下拉标签存在重名")


_validate_presets()


def build_series_overlay(
    main_df: pd.DataFrame,
    series_df: pd.DataFrame,
    preset: SeriesPreset,
    *,
    period: str = "daily",
) -> dict:
    """把单序列指标对齐到主图并生成 overlay payload.

    - 主图与指标都先按当前周期重采样; 主图只用来提供 x 轴日期
    - 指标日期与主图不完全一致时 (不同市场休市日), 取「≤ 当日」的最近
      观测值, 与前端 ``valAt`` 的十字光标口径一致
    - 指标区间外 (FRED 约 3 年窗口) 不向前/向后外推, 直接截断
    - 均值按实际画出的 (已对齐) 值计算, 与图例「较均值」一致
    """
    main = charting.resample_ohlc(
        charting.clean_ohlc(main_df, allow_nonpositive=True),
        period,
        allow_nonpositive=True,
    )
    series = charting.resample_ohlc(
        charting.clean_ohlc(series_df, allow_nonpositive=True),
        period,
        allow_nonpositive=True,
    )
    if main.empty or series.empty:
        raise ValueError(f"{preset.label} 或主图无有效数据")

    s_dates = series["date"].dt.strftime("%Y-%m-%d").tolist()
    s_vals = [float(v) for v in series["close"]]
    first, last = s_dates[0], s_dates[-1]

    aligned: list[tuple[str, float]] = []
    for t in main["date"].dt.strftime("%Y-%m-%d"):
        if t < first or t > last:
            continue
        j = bisect_right(s_dates, t) - 1
        if j < 0 or not math.isfinite(s_vals[j]):
            continue
        aligned.append((t, s_vals[j]))
    if not aligned:
        raise ValueError(f"{preset.label} 与主图日期区间无交集")

    precision = int(preset.precision)
    mean = sum(v for _, v in aligned) / len(aligned)
    return {
        "name": preset.label,
        "kind": "series",
        "symbol": preset.symbol,
        "color": preset.color,
        "unit": preset.unit,
        "precision": precision,
        "mean": round(mean, precision),
        "data": [{"time": t, "value": round(v, precision)} for t, v in aligned],
    }
