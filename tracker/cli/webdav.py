"""webdav 子命令: 远端 portfolio/watchlist 同步状态 / 手动拉取 / 手动推送."""

from __future__ import annotations

from pathlib import Path

from .. import storage
from .. import webdav as dav
from ..webdav import WebDAVError
from ._common import _finish_with_error, _print_json

_TARGETS = {
    "portfolio": "portfolio.json",
    "portfolio.json": "portfolio.json",
    "watchlist": "watchlist.json",
    "watchlist.json": "watchlist.json",
}


def _local_paths() -> dict[str, str]:
    return {
        "portfolio.json": str(storage.PORTFOLIO_PATH),
        "watchlist.json": str(storage.WATCHLIST_PATH),
    }


def _names(target: str, cfg: dict) -> list[str]:
    """命令目标 → 文件名列表; all = 配置里接管的文件与本地两份的交集."""
    if target == "all":
        return list(cfg.get("files") or [])
    name = _TARGETS.get(target)
    if name is None:
        _finish_with_error(f"未知目标: {target} (可选 portfolio / watchlist / all)")
    return [name]


def _print_status(info: dict) -> None:
    print(f"配置文件: {info['config_path']}")
    print(f"远端: {info['url'] or '未配置'}")
    print(f"启用: {'是' if info['enabled'] else '否'}")
    print(f"冲突策略: {info['conflict']} (strict={info['strict']})")
    if not info["files"]:
        print("接管文件: 无")
        return
    print("接管文件:")
    for name, f in info["files"].items():
        print(f"  [{name}] {f['local_path']}")
        sync_label = (
            "未启用"
            if f["in_sync"] is None
            else ("是" if f["in_sync"] else "否")
        )
        print(
            f"    本地: {'存在' if f['local_exists'] else '缺失'}"
            f" / 同步: {sync_label}"
        )
        if f.get("etag"):
            print(f"    ETag: {f['etag']}")
        if f.get("synced_at"):
            print(f"    上次同步: {f['synced_at']}")
        if f.get("conflict"):
            print(f"    ⚠ 冲突: {f['conflict'].get('message', '')}")
        if f.get("last_error"):
            print(f"    ⚠ 最近错误: {f['last_error']}")
        remote = f.get("remote")
        if remote is not None:
            if remote.get("exists") is False:
                print("    远端: 文件不存在")
            elif remote.get("error"):
                print(f"    远端: 不可用 ({remote['error']})")
            else:
                print(
                    f"    远端: 存在"
                    f" (ETag={remote.get('etag') or '—'},"
                    f" {remote.get('content_length') or '?'} bytes)"
                )
    if not info["enabled"]:
        print(
            "提示: 在 webdav.json 中设置 enabled=true 与 url, "
            "或用 env TRACKER_WEBDAV_CONFIG 指定配置文件"
        )


def cmd_webdav(args) -> None:
    """webdav status | webdav pull [TARGET] | webdav push [TARGET]."""
    cfg = dav.load_config(getattr(args, "config", None))
    action = args.webdav_action
    as_json = bool(getattr(args, "json", False))

    if action == "status":
        info = dav.status(
            _local_paths(),
            cfg=cfg,
            check=bool(getattr(args, "check", False)),
            config_file=getattr(args, "config", None),
        )
        if as_json:
            _print_json(info)
        else:
            _print_status(info)
        return

    if action not in ("pull", "push"):
        _finish_with_error(f"未知 webdav 动作: {action}")

    if not info_enabled(cfg):
        _finish_with_error(
            "WebDAV 未启用: 请配置 webdav.json 的 enabled=true / url, "
            "或设置 TRACKER_WEBDAV_CONFIG"
        )

    local_paths = _local_paths()
    force = bool(getattr(args, "force", False))
    results = []
    for name in _names(getattr(args, "target", "all"), cfg):
        lp = local_paths.get(name)
        if lp is None:
            _finish_with_error(f"不支持的文件: {name}")
        try:
            # 与 storage 的读-改-写共用同一把本地锁: 手动 pull/push 时不与
            # Streamlit/CLI 的保存交错覆盖本地缓存。
            with storage._file_lock(Path(lp)):
                if action == "pull":
                    result = dav.pull(
                        name, lp, force=force, cfg=cfg, raise_errors=True
                    )
                else:
                    result = dav.push(
                        name, lp, force=force, cfg=cfg, raise_errors=True
                    )
        except WebDAVError as e:
            _finish_with_error(str(e))
        results.append(result)
        if not as_json:
            _print_result(result)
    if as_json:
        _print_json({"ok": True, "action": action, "results": results})


def info_enabled(cfg: dict) -> bool:
    return bool(cfg.get("enabled") and cfg.get("url"))


def _print_result(result: dict) -> None:
    action = result.get("action")
    if action == "disabled":
        print(f"⏭️  {result['name']}: 未启用 WebDAV")
    elif action == "remote-missing":
        print(f"⏭️  {result['name']}: 远端不存在, 本地保持不变")
    elif action == "no-local":
        print(f"⏭️  {result['name']}: 本地文件不存在, 未推送")
    elif action == "unchanged":
        print(f"✅ {result['name']}: 已是最新")
    elif action in ("pulled", "pulled-forced", "pulled-remote-wins"):
        print(f"⬇️  {result['name']}: 已从远端拉取")
    elif action == "pushed":
        print(f"⬆️  {result['name']}: 已推送到远端")
    elif action == "conflict":
        print(f"⚠️  {result['name']}: 冲突, 本地与远端均未覆盖")
    else:
        print(f"❌ {result['name']}: {result.get('error') or action}")
    if result.get("archived"):
        print(f"    存档: {result['archived']}")
