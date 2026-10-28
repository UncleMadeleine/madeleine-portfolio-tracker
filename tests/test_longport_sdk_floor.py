"""longport SDK 版本下限护栏.

`tracker.events` 的长桥日历路径依赖 `longport.openapi.CalendarCategory` /
`CalendarContext`, 这两个符号自 longport 4.3.0 才有 (pyproject/requirements 均
声明 `longport>=4.3.0`)。

装到旧版时 (例如 3.0.23), `events._fetch_lp_month_events` 会按设计优雅降级,
返回「长桥日历不可用: ImportError: cannot import name 'CalendarCategory'」,
而 TestLongportBatchCache 的 4 个用例随之失败, 报错信息完全指不到真实原因
(SDK 版本)。本测试把根因变成一个明确的断言。

若未安装 longport, 跳过 (可选依赖, 用时惰性导入)。
"""

from __future__ import annotations

import pytest

longport = pytest.importorskip("longport", reason="longport 未安装 (可选依赖)")

# 与 pyproject.toml / requirements.txt 的 longport 下限保持一致
REQUIRED_MAJOR_MINOR = (4, 3)

_REQUIRED_SYMBOLS = ("CalendarCategory", "CalendarContext")


def _installed_version_str() -> str:
    import importlib.metadata as metadata

    return metadata.version("longport")


def test_longport_meets_declared_minimum():
    """安装版本必须满足声明的 >= 4.3.0."""
    from packaging.version import Version

    installed = Version(_installed_version_str())
    required = Version(f"{REQUIRED_MAJOR_MINOR[0]}.{REQUIRED_MAJOR_MINOR[1]}")

    assert installed >= required, (
        f"longport {installed} 低于声明下限 {required};\n"
        f"  升级: pip install 'longport>={required}'\n"
        f"  否则 tracker.events 的长桥日历会降级, "
        f"TestLongportBatchCache 的用例会因含义不明的 ImportError 失败"
    )


def test_calendar_symbols_exist():
    """events.py 用到的 CalendarCategory/CalendarContext 必须存在.

    这是 4.3.0 下限存在的真正理由; 单独断言可让符号被移动/改名时
    报错指向这里, 而不是一堆下游 ImportError。
    """
    import longport.openapi as openapi

    missing = [s for s in _REQUIRED_SYMBOLS if not hasattr(openapi, s)]
    assert not missing, f"longport.openapi 缺少 {missing}; 需要 longport>=4.3.0"


def test_calendar_category_members_used_by_events():
    """events.py 按名字取 CalendarCategory.<member>, 这些成员必须存在."""
    from longport.openapi import CalendarCategory

    for member in ("Report", "Dividend", "Ipo", "Meeting", "Split"):
        assert hasattr(CalendarCategory, member), (
            f"CalendarCategory.{member} 不存在; events 的 {member} 类事件会失败"
        )


def test_calendar_context_has_finance_calendar():
    """CalendarContext.finance_calendar 是分页取日历的入口."""
    from longport.openapi import CalendarContext

    assert hasattr(CalendarContext, "finance_calendar")
