"""批量失败项的紧凑展示: 一行摘要 + 可选折叠详情.

同一次批量取数/导入里大量代码失败时, 逐条 ``st.warning`` 会纵向铺满页面,
把真正要看的内容挤出首屏。这里统一收敛为「共 N 条 + 原因计数」一行, 完整
明细按需折叠; 已有统一「数据问题」展开器的页面只显示摘要, 不重复渲染。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

import streamlit as st


def _reason(text: str) -> str:
    """从 "<代码>: <原因>" 中取原因; 没有分隔符时用原文."""
    _, sep, tail = text.rpartition(": ")
    return tail.strip() if sep and tail.strip() else text


def _short(text: str, limit: int = 40) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def summarize_issues(
    items: Iterable[str], max_reasons: int = 3
) -> tuple[int, list[tuple[str, int]], int]:
    """去重后按原因聚合, 返回 (总数, [(原因, 数量), ...], 未展示数量).

    原因按出现次数降序; 超过 ``max_reasons`` 的尾部只给出合计条数, 避免
    异常文本各不相同 (如每行一个解析错误) 时摘要本身又变成纵向长列表。
    """
    uniq = list(dict.fromkeys(str(i).strip() for i in items if str(i).strip()))
    counts = Counter(_reason(i) for i in uniq)
    ordered = sorted(counts.items(), key=lambda kv: -kv[1])
    top = ordered[:max_reasons]
    hidden = sum(count for _, count in ordered[max_reasons:])
    return len(uniq), top, hidden


def render_issue_summary(
    items: Iterable[str],
    *,
    noun: str = "数据问题",
    hint: str = "",
    details: bool = True,
    details_label: str = "查看全部",
    key: str | None = None,
    max_reasons: int = 3,
) -> None:
    """渲染批量失败摘要.

    - 只有 1 条时保持普通 warning (单条不会占空间, 也更有提示性);
    - 多条时只留一行「共 N 条 + 原因 ×数量」, 明细按 ``details`` 折叠;
    - ``details=False`` 用于页面上方已有完整明细展开器的场景 (只提示去处)。
    """
    uniq = list(dict.fromkeys(str(i).strip() for i in items if str(i).strip()))
    if not uniq:
        return
    if len(uniq) == 1:
        st.warning(uniq[0], icon=":material/warning:")
        return

    total, top, hidden = summarize_issues(uniq, max_reasons=max_reasons)
    summary = " · ".join(f"{_short(reason)} ×{count}" for reason, count in top)
    if hidden:
        summary += f" · 其他 {hidden} 条"
    suffix = f"（{hint}）" if hint else ""
    st.caption(f":material/warning: 共 {total} 条{noun}: {summary}{suffix}")
    if details:
        with st.expander(
            f"{details_label} ({total})",
            icon=":material/list:",
            key=key,
        ):
            for item in uniq:
                st.write(f"- {item}")
