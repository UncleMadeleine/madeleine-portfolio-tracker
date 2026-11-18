"""指数域的比率指标: 金银比 / 金油比.

同一份预设既用于**派生指数** (IX.GOLDSILVER / IX.GOLDOIL, 可像普通指数一样
在指数页/CLI 直接查询), 也用于指数K线上的可选叠加曲线。

比率 = 分子收盘价 / 分母收盘价, 两条腿都是指数域代码 (IX.<KEY>),
全部用美元计价, 因此不需要汇率或单位换算:

- 金银比 = COMEX 黄金 (美元/盎司) ÷ COMEX 白银 (美元/盎司)
- 金油比 = COMEX 黄金 (美元/盎司) ÷ WTI 原油 (美元/桶)

日期对齐 (不同市场休市日)、周期重采样、精度与区间均值统一由
``charting.build_ratio_series`` 处理; 本模块只维护预设与取数契约。
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from . import charting
from .symbols import INDEX_CATALOG, index_label


@dataclass(frozen=True)
class RatioPreset:
    """一个可叠加的比率预设 (分子/分母均为 IX.<KEY> 指数代码)."""

    key: str
    label: str
    numerator: str
    denominator: str

    @property
    def formula(self) -> str:
        """「COMEX 黄金 ÷ COMEX 白银」形式的可读公式."""
        return (
            f"{index_label(self.numerator[3:])} ÷ {index_label(self.denominator[3:])}"
        )

    @property
    def symbols(self) -> tuple[str, str]:
        """需要取数的两个指数代码 (分子, 分母)."""
        return (self.numerator, self.denominator)


RATIO_PRESETS: tuple[RatioPreset, ...] = (
    RatioPreset(
        key="gold_silver",
        label="金银比",
        numerator="IX.XAU",
        denominator="IX.XAG",
    ),
    RatioPreset(
        key="gold_oil",
        label="金油比",
        numerator="IX.XAU",
        denominator="IX.WTI",
    ),
)

PRESET_BY_LABEL: dict[str, RatioPreset] = {p.label: p for p in RATIO_PRESETS}
PRESET_BY_KEY: dict[str, RatioPreset] = {p.key: p for p in RATIO_PRESETS}

# 比率对应的派生指数代码: 让比率可以像普通指数一样在指数页下拉框 /
# index-kline 子命令里直接选择 (底层仍是两条腿相除, 由 provider 缓存结果)。
DERIVED_INDEX_BY_PRESET: dict[str, str] = {
    "gold_silver": "IX.GOLDSILVER",
    "gold_oil": "IX.GOLDOIL",
}
PRESET_BY_INDEX: dict[str, RatioPreset] = {
    code: PRESET_BY_KEY[key] for key, code in DERIVED_INDEX_BY_PRESET.items()
}


def preset_for_label(label: str) -> RatioPreset | None:
    """下拉框展示名 → 预设; 「无」/未知标签返回 None."""
    return PRESET_BY_LABEL.get(str(label or "").strip())


def preset_for_index(symbol: str) -> RatioPreset | None:
    """派生比率指数代码 (IX.GOLDSILVER / IX.GOLDOIL) → 预设; 普通指数返回 None."""
    return PRESET_BY_INDEX.get(str(symbol or "").strip().upper())


def _validate_presets() -> None:
    """导入时自检: 两条腿必须是已收录指数且不是同一代码.

    预设写错 (目录改名/删条目) 时应在开发阶段直接暴露, 而不是等页面
    取数时报「未收录的指数代码」。
    """
    for p in RATIO_PRESETS:
        for code in p.symbols:
            if code[3:] not in INDEX_CATALOG:
                raise ValueError(f"比率预设 {p.key} 引用了未收录指数: {code}")
        if p.numerator == p.denominator:
            raise ValueError(f"比率预设 {p.key} 的分子与分母相同")
        # 派生指数目录项必须与本模块的预设一一对应 (ratio 字段回指预设 key)
        code = DERIVED_INDEX_BY_PRESET.get(p.key)
        if not code or code[3:] not in INDEX_CATALOG:
            raise ValueError(f"比率预设 {p.key} 缺少对应的派生指数目录项")
        if INDEX_CATALOG[code[3:]].get("ratio") != p.key:
            raise ValueError(f"派生指数 {code} 的 ratio 字段必须为 {p.key}")


_validate_presets()


def build_ratio_curve(
    frames: dict[str, pd.DataFrame],
    preset_key: str,
    *,
    period: str = "daily",
) -> dict:
    """按预设计算比率曲线 payload (供 K 线组件叠加绘制).

    frames: {IX.<KEY>: OHLC DataFrame}, 必须包含预设的分子与分母。
    返回结构与 ``charting.build_ratio_series`` 一致 (data/mean/precision/...),
    可直接放在 kline_payload 的 overlay 字段。
    """
    preset = PRESET_BY_KEY.get(preset_key)
    if preset is None:
        raise ValueError(f"未知的比率预设: {preset_key}")
    return charting.build_ratio_series(
        frames,
        preset.numerator,
        preset.denominator,
        period=period,
    )


def ratio_option_labels() -> list[str]:
    """下拉框选项: 「无」+ 全部预设展示名."""
    return ["无"] + [p.label for p in RATIO_PRESETS]
