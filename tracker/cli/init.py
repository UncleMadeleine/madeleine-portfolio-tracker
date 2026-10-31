"""init 子命令: 首次使用把包内配置模板复制到数据目录.

pip 安装后用户数据目录不再是仓库根 (见 tracker/paths.py), 包内模板
(tracker/templates/*.example.json) 随 wheel 分发, 本命令负责落地:

    tracker init            # 生成缺失的配置文件 (已存在则跳过)
    tracker init --force    # 用模板覆盖已存在的配置文件
    tracker init --json     # 机器可读输出
"""

from __future__ import annotations

from ..paths import TEMPLATES_DIR, data_home
from ._common import _print_json

#: 模板文件名 → 数据目录目标名
_TEMPLATES = {
    "portfolio": "portfolio.example.json",
    "watchlist": "watchlist.example.json",
    "settings": "settings.example.json",
    "ibkr": "ibkr.example.json",
    "longport": "longport.example.json",
    "eodhd": "eodhd.example.json",
}


def cmd_init(args) -> None:
    """把包内模板复制到数据目录 (幂等; --force 覆盖)。"""
    home = data_home()
    home.mkdir(parents=True, exist_ok=True)
    created, skipped = [], []
    for name, tpl in _TEMPLATES.items():
        src = TEMPLATES_DIR / tpl
        dst = home / f"{name}.json"
        if dst.exists() and not args.force:
            skipped.append(dst)
            continue
        if not src.exists():  # 模板缺失 (异常构建): 跳过而不是报错中断
            continue
        dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        if name == "eodhd":
            # eodhd.json 会存 API token: 首次生成即收紧为仅属主可读写
            try:
                dst.chmod(0o600)
            except OSError:
                pass
        created.append(dst)

    if getattr(args, "json", False):
        _print_json(
            {
                "ok": True,
                "data_home": str(home),
                "created": [str(p) for p in created],
                "skipped": [str(p) for p in skipped],
            }
        )
        return

    print(f"数据目录: {home}")
    for p in created:
        print(f"  ✅ 已生成 {p.name}")
    for p in skipped:
        print(f"  ⏭️  已存在 {p.name} (--force 可覆盖)")
    print("提示: 编辑上述 JSON 即可修改配置; 数据目录可用环境变量 TRACKER_HOME 改位置")
