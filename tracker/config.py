"""共享配置读取: 路径解析 / 容错 JSON 读取 / 分层合并.

IBKR / 长桥 / EODHD 三套 BYOK 配置此前各写一份 `_resolve_config_path` /
`_load_file` / `load_config`, 行为逐渐分叉 (BOM 容错、mtime 缓存、损坏
容错、~ 展开各不相同)。本模块收敛为单一实现, 各领域模块只保留字段校验
与领域默认值。

约定:
- 配置文件始终是 JSON object; 缺失 / 损坏 / 顶层非对象一律按空配置处理
  (可选数据源配置错误不应阻断主流程)。
- 真实配置覆盖包内模板, 模板覆盖代码内兜底默认值。
- 以 `_` 开头的键视为说明/保留字段, 不合并进运行时配置。
"""

from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path

# 进程内文件缓存: (mtime_ns, size) → 解析结果. 自选 600+ 时源链会反复读
# 同一份配置; 有 mtime 校验, 手改文件无需重启进程即可生效。
_file_cache: dict[Path, tuple[tuple[int, int], dict]] = {}
_file_lock = threading.Lock()


def resolve_config_path(
    explicit: str | Path | None,
    env_var: str,
    default_path: Path,
    example_path: Path | None = None,
    *,
    require_exists: bool = False,
) -> Path:
    """配置路径: 显式 path > 环境变量 > 数据目录真实配置 > 包内模板.

    require_exists=True 时, 显式/env 指向不存在的文件则继续向下查找
    (长桥历史行为); False 时直接采用 (IBKR / EODHD 历史行为)。
    default_path 不存在且无模板时仍返回 default_path, 由调用方按缺失处理。
    """
    for raw in (explicit, os.environ.get(env_var)):
        if not raw:
            continue
        p = Path(raw).expanduser()
        if not require_exists or p.exists():
            return p
    if default_path.exists():
        return default_path
    return example_path if example_path is not None else default_path


def load_json_object(path: str | Path) -> dict:
    """读取 JSON 对象; 缺失/损坏/非对象返回 {} (配置错误不阻断取数).

    - utf-8-sig: 兼容编辑器存成的带 BOM 文件
    - mtime_ns + size 缓存: 反复读同一份配置时只解析一次
    - 返回深拷贝: 调用方修改 cfg 不会污染缓存
    """
    p = Path(path)
    try:
        st = p.stat()
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        return {}
    with _file_lock:
        hit = _file_cache.get(p)
    if hit is not None and hit[0] == key:
        return copy.deepcopy(hit[1])
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    with _file_lock:
        _file_cache[p] = (key, data)
    return copy.deepcopy(data)


def clear_cache() -> None:
    """清空配置缓存 (写入配置 / 测试替换环境变量后调用)."""
    with _file_lock:
        _file_cache.clear()


def merge_config(
    base: dict,
    *layers: dict,
    ignore_prefix: str | None = "_",
    deep_keys: tuple[str, ...] = (),
) -> dict:
    """分层合并配置 (后者覆盖前者), 跳过说明键.

    deep_keys 中的字典键做一层合并 (IBKR 的 exchanges: 模板默认交易所与用户
    自定义交易所互补), 其余键整体替换。所有值均深拷贝, 避免共享嵌套结构。
    """
    out: dict = {k: copy.deepcopy(v) for k, v in base.items()}
    for layer in layers:
        for k, v in layer.items():
            if ignore_prefix and str(k).startswith(ignore_prefix):
                continue
            if k in deep_keys and isinstance(v, dict):
                cur = out.get(k)
                merged = copy.deepcopy(cur) if isinstance(cur, dict) else {}
                merged.update(copy.deepcopy(v))
                out[k] = merged
            else:
                out[k] = copy.deepcopy(v)
    return out
