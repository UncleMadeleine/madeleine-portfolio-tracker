"""Streamlit 取数进度条: 组合页/日历页共用的逐代码节流回调."""

from __future__ import annotations

import time

import streamlit as st


class FetchProgress:
    """逐代码进度条 (with 语句使用; 退出时自动清空).

    大组合逐代码回调可达数百次: 进度增幅 <1% 且距上次 <0.2s 时跳过,
    避免把 Streamlit 增量消息刷爆 (完成时 frac=1 仍必刷)。
    """

    def __init__(self, prefix: str, text: str):
        self._prefix = prefix
        self._bar = st.progress(0.0, text=text)
        self._last_frac = -1.0
        self._last_at = 0.0

    def __call__(self, done, total, label) -> None:
        total = max(int(total or 0), int(done or 0), 1)
        frac = min(max(float(done) / total, 0.0), 1.0)
        now = time.monotonic()
        if (
            frac < 1.0
            and frac - self._last_frac < 0.01
            and now - self._last_at < 0.2
        ):
            return
        self._last_frac, self._last_at = frac, now
        text = f"{self._prefix} {done}/{total}"
        if label:
            text += f" · {label}"
        self._bar.progress(frac, text=text)

    def __enter__(self) -> "FetchProgress":
        return self

    def __exit__(self, *exc) -> None:
        self._bar.empty()
