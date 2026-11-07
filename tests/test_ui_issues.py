"""批量失败项紧凑展示 (tracker.ui.issues) 测试.

回归点: 同一次批量取数/导入失败很多代码时, 逐条 warning 会纵向铺满页面;
渲染层应只保留一行原因计数, 完整明细按需折叠。
"""

from __future__ import annotations

from streamlit.testing.v1 import AppTest

from tracker.ui.issues import summarize_issues


def test_summarize_groups_reasons_and_hides_tail():
    total, top, hidden = summarize_issues(
        [
            "AAPL: 行情缺失",
            "MSFT: 行情缺失",
            "0700.HK: 行情缺失",
            "BAD!!: 代码无法识别",
            "BTC-USD: 市值缺失",
            "X: 解析错误",
        ],
        max_reasons=2,
    )
    assert total == 6
    assert top == [("行情缺失", 3), ("代码无法识别", 1)]
    assert hidden == 2


def test_many_issues_render_one_summary_line():
    script = """
from tracker.ui.issues import render_issue_summary
render_issue_summary(
    [f"SYM{i}: 行情缺失" for i in range(30)],
    noun="自选数据问题",
    hint="详情见上方「数据问题」",
    details=False,
)
"""
    at = AppTest.from_string(script).run()
    assert not at.exception
    assert len(at.caption) == 1
    text = at.caption[0].value
    assert "共 30 条自选数据问题" in text
    assert "行情缺失 ×30" in text
    assert "详情见上方「数据问题」" in text
    assert not at.warning


def test_single_issue_keeps_warning():
    script = """
from tracker.ui.issues import render_issue_summary
render_issue_summary(["BAD!!: 代码无法识别"], noun="自选数据问题")
"""
    at = AppTest.from_string(script).run()
    assert not at.exception
    assert len(at.warning) == 1
    assert "BAD!!: 代码无法识别" in at.warning[0].value


def test_details_are_collapsed_into_expander():
    script = """
from tracker.ui.issues import render_issue_summary
render_issue_summary(
    ["AAPL: 行情缺失", "MSFT: 市值缺失"],
    noun="导入跳过项",
    details_label="查看跳过明细",
)
"""
    at = AppTest.from_string(script).run()
    assert not at.exception
    assert len(at.caption) == 1
    assert "共 2 条导入跳过项" in at.caption[0].value
    # 带 icon 的 expander 在 AppTest 里呈现为 status 元素 (label 仍是展开器标题)
    assert len(at.status) == 1
    assert "查看跳过明细 (2)" in at.status[0].label
