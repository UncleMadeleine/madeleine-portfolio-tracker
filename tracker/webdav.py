"""WebDAV 远端数据文件同步: portfolio.json / watchlist.json.

目标: 让持仓与自选以远端 WebDAV 目录为准, 同时保留一份本地缓存, 这样
离线 / 远端故障时 CLI 与 Streamlit 仍能打开 (不会因为网络问题整个不可用).

设计:
- 配置放数据目录的 `webdav.json` (可被 env TRACKER_WEBDAV_CONFIG 覆盖),
  默认 `enabled=false`, 未配置时本模块完全 no-op。
- 本地 portfolio.json / watchlist.json 是远端文件的缓存镜像, 不是第二份
  权威数据: 读取前按 ETag 条件拉取, 保存后条件 PUT 回远端。
- 条件请求 (If-None-Match / If-Match / If-None-Match: *) 负责乐观并发:
  两端都改过时默认保留本地并记录冲突, 远端版本另存到
  `var/webdav-conflicts/`, 不静默覆盖任何一边。
- 自动同步失败 (网络 / 401 / 冲突) 默认只告警, 不阻断本地读写; 需要
  严格失败的自动化场景可设 `"strict": true`, 或用 CLI `webdav pull/push`
  (命令本身总是把错误抛给调用方)。

只同步 `files` 里列出的文件名 (默认 portfolio.json + watchlist.json);
settings.json / 券商凭据等仍只留在本地。远端只允许数据目录下的文件名,
不接受路径分隔符, 避免配置写错时把文件写到 WebDAV 的其它位置。
"""

from __future__ import annotations

import threading
import time
import warnings
from pathlib import Path

from .paths import data_home
from .webdav_client import (  # noqa: F401 - 兼容再导出
    CONFIG_ENV,
    CONFIG_NAME,
    DEFAULT_FILES,
    DEFAULTS,
    WebDAVConflict,
    WebDAVError,
    WebDAVUnavailable,
    _archive,
    _atomic_write,
    _auth_header,
    _ensure_collection,
    _file_name,
    _load_state,
    _remote_url,
    _request,
    _save_state,
    _sha,
    _state_path,
    config_path,
    enabled_for,
    load_config,
    managed_files,
    reset_cache,
)

# 进程内自动拉取节流: (url, name, local_path) -> monotonic 时间
_last_pull: dict[tuple[str, str, str], float] = {}
_sync_lock = threading.RLock()


def _raise_errors(cfg: dict, override: bool | None) -> bool:
    return cfg["strict"] if override is None else bool(override)


def _offline(
    cfg: dict,
    state: dict,
    name: str,
    err: Exception,
    *,
    raise_errors: bool | None,
    action: str,
) -> dict:
    st = state["files"].setdefault(name, {})
    st["last_error"] = str(err)
    st["last_error_at"] = time.time()
    _save_state(cfg, state)
    if _raise_errors(cfg, raise_errors):
        if isinstance(err, WebDAVUnavailable):
            raise err
        raise WebDAVError(str(err)) from err
    warnings.warn(
        f"WebDAV 同步失败, 继续使用本地缓存: {err}", RuntimeWarning, stacklevel=3
    )
    return {"name": name, "action": action, "error": str(err)}


def _conflict_result(name: str, st: dict, *, suppressed: bool = False) -> dict:
    conflict = st.get("conflict") or {}
    out = {
        "name": name,
        "action": "conflict",
        "error": conflict.get("message") or "本地与远端存在未解决的冲突",
    }
    if conflict.get("archived"):
        out["archived"] = conflict["archived"]
    if suppressed:
        out["suppressed"] = True
    return out


def _conflict(
    cfg: dict,
    state: dict,
    name: str,
    message: str,
    *,
    raise_errors: bool | None,
    **extra,
) -> dict:
    st = state["files"].setdefault(name, {})
    st["conflict"] = {
        "at": time.time(),
        "message": message,
        **{k: str(v) for k, v in extra.items() if v is not None},
    }
    _save_state(cfg, state)
    if _raise_errors(cfg, raise_errors):
        raise WebDAVConflict(message)
    warnings.warn(message, RuntimeWarning, stacklevel=3)
    return {
        "name": name,
        "action": "conflict",
        "error": message,
        **extra,
    }


def _sync_state(
    st: dict,
    *,
    etag: str | None,
    local_sha: str,
    remote_sha: str | None,
    now: float,
) -> None:
    st["etag"] = etag
    st["local_sha256"] = local_sha
    if remote_sha is not None:
        st["remote_sha256"] = remote_sha
    st["checked_at"] = now
    st["synced_at"] = now
    st.pop("conflict", None)
    st.pop("last_error", None)
    st.pop("last_error_at", None)
    st.pop("remote_missing", None)


# ---------------------------------------------------------------------------
# 拉取 / 推送
# ---------------------------------------------------------------------------


def pull(
    name: str,
    local_path: str | Path,
    *,
    force: bool = False,
    cfg: dict | None = None,
    raise_errors: bool | None = None,
) -> dict:
    """把远端文件拉到本地缓存.

    force=True: 忽略本地修改/冲突, 用远端覆盖本地 (本地版本先存档)。
    返回 {"name", "action", ...}; action 取值:
    disabled / pulled / unchanged / remote-missing / offline / conflict /
    pulled-forced / pulled-remote-wins。
    """
    name = _file_name(name) or name
    cfg = cfg or load_config()
    lp = Path(local_path)
    if not enabled_for(name, cfg):
        return {"name": name, "action": "disabled"}

    with _sync_lock:
        state = _load_state(cfg)
        st = state["files"].get(name, {})
        etag = st.get("etag")
        url = _remote_url(cfg, name)
        headers = {}
        if etag and not force:
            headers["If-None-Match"] = etag
        try:
            status, resp_headers, remote = _request("GET", url, cfg, headers=headers)
        except WebDAVUnavailable as e:
            return _offline(
                cfg, state, name, e, raise_errors=raise_errors, action="offline"
            )

        now = time.time()
        if status == 304:
            st["checked_at"] = now
            st.pop("last_error", None)
            _save_state(cfg, state)
            if st.get("conflict"):
                # 冲突未解决且两端都没再变化: 不重复下载/存档/告警
                return _conflict_result(name, st, suppressed=True)
            return {"name": name, "action": "unchanged"}
        if status == 404:
            st["checked_at"] = now
            st["remote_missing"] = True
            _save_state(cfg, state)
            return {"name": name, "action": "remote-missing"}
        if status >= 400:
            err = WebDAVError(
                f"拉取 {name} 失败 (HTTP {status}): "
                f"{remote[:200].decode('utf-8', 'replace')}"
            )
            return _offline(
                cfg, state, name, err, raise_errors=raise_errors, action="error"
            )

        remote_sha = _sha(remote)
        remote_etag = resp_headers.get("ETag")
        st.pop("remote_missing", None)

        local = lp.read_bytes() if lp.exists() else None
        if local is None:
            _atomic_write(lp, remote)
            _sync_state(
                st,
                etag=remote_etag,
                local_sha=remote_sha,
                remote_sha=remote_sha,
                now=now,
            )
            state["files"][name] = st
            _save_state(cfg, state)
            return {"name": name, "action": "pulled", "path": str(lp)}

        local_sha = _sha(local)
        if local_sha == remote_sha:
            _sync_state(
                st,
                etag=remote_etag,
                local_sha=local_sha,
                remote_sha=remote_sha,
                now=now,
            )
            state["files"][name] = st
            _save_state(cfg, state)
            return {"name": name, "action": "unchanged"}

        # 已有未解决冲突: 只有远端又变了才重新存档/告警, 否则静默返回冲突,
        # 避免每次 poll 都复制一份冲突文件并把警告刷屏。
        conflict = st.get("conflict") or {}
        if conflict and not force:
            prev_remote = conflict.get("remote_sha256")
            prev_local = conflict.get("local_sha256")
            st["etag"] = remote_etag
            st["remote_sha256"] = remote_sha
            st["checked_at"] = now
            if remote_sha != prev_remote:
                archived = _archive(name, "remote", remote)
                state["files"][name] = st
                return _conflict(
                    cfg,
                    state,
                    name,
                    f"{name} 冲突期间远端又发生变化, 新版本已存档到 {archived}; "
                    f"确认后用 `tracker webdav pull --force` 或 "
                    f"`tracker webdav push --force` 解决",
                    raise_errors=raise_errors,
                    archived=str(archived),
                    local_sha256=local_sha,
                    remote_sha256=remote_sha,
                )
            if local_sha != prev_local:
                conflict["local_sha256"] = local_sha
            state["files"][name] = st
            _save_state(cfg, state)
            return _conflict_result(name, st, suppressed=True)

        # 本地相对上次同步是否改过。没有状态 (首次启用 / state 被删) 时按
        # “改过”处理: 不拿远端静默覆盖一份来历不明的本地组合。
        local_dirty = st.get("local_sha256") != local_sha

        if not local_dirty:
            _atomic_write(lp, remote)
            _sync_state(
                st,
                etag=remote_etag,
                local_sha=remote_sha,
                remote_sha=remote_sha,
                now=now,
            )
            state["files"][name] = st
            _save_state(cfg, state)
            return {"name": name, "action": "pulled", "path": str(lp)}

        if force:
            archived = _archive(name, "local", local)
            _atomic_write(lp, remote)
            _sync_state(
                st,
                etag=remote_etag,
                local_sha=remote_sha,
                remote_sha=remote_sha,
                now=now,
            )
            state["files"][name] = st
            _save_state(cfg, state)
            return {
                "name": name,
                "action": "pulled-forced",
                "path": str(lp),
                "archived": str(archived),
            }

        if cfg["conflict"] == "remote-wins":
            archived = _archive(name, "local", local)
            _atomic_write(lp, remote)
            _sync_state(
                st,
                etag=remote_etag,
                local_sha=remote_sha,
                remote_sha=remote_sha,
                now=now,
            )
            state["files"][name] = st
            _save_state(cfg, state)
            return {
                "name": name,
                "action": "pulled-remote-wins",
                "path": str(lp),
                "archived": str(archived),
            }

        # keep-local: 保留本地, 远端存档, 等用户 pull --force / push --force
        archived = _archive(name, "remote", remote)
        st["etag"] = remote_etag
        st["remote_sha256"] = remote_sha
        st["checked_at"] = now
        state["files"][name] = st
        return _conflict(
            cfg,
            state,
            name,
            f"{name} 本地与远端都修改过: 已保留本地, 远端版本存档到 {archived}; "
            f"确认后用 `tracker webdav pull --force` 或 "
            f"`tracker webdav push --force` 解决",
            raise_errors=raise_errors,
            archived=str(archived),
            local_sha256=local_sha,
            remote_sha256=remote_sha,
        )


def push(
    name: str,
    local_path: str | Path,
    *,
    force: bool = False,
    cfg: dict | None = None,
    raise_errors: bool | None = None,
) -> dict:
    """把本地缓存推回远端.

    force=True: 不做 If-Match 条件检查, 直接覆盖远端 (远端版本不额外存档)。
    """
    name = _file_name(name) or name
    cfg = cfg or load_config()
    lp = Path(local_path)
    if not enabled_for(name, cfg):
        return {"name": name, "action": "disabled"}
    if not lp.exists():
        return {"name": name, "action": "no-local"}

    with _sync_lock:
        local = lp.read_bytes()
        local_sha = _sha(local)
        state = _load_state(cfg)
        st = state["files"].get(name, {})
        url = _remote_url(cfg, name)

        if st.get("conflict") and not force:
            # 冲突未解决时保存本地但不自动推远端: 否则一次普通保存就会
            # 静默覆盖另一端, 把冲突变成单边覆盖。用 push --force 显式解决。
            return _conflict_result(name, st, suppressed=True)

        headers: dict[str, str] = {
            "Content-Type": "application/json; charset=utf-8"
        }
        if not force:
            etag = st.get("etag")
            if etag:
                headers["If-Match"] = etag
            else:
                # 首次推送: 远端已存在就报冲突, 不能静默把另一台设备的文件盖掉
                headers["If-None-Match"] = "*"

        status, resp_headers, body = _put(url, cfg, local, headers)
        if status in (404, 409) and cfg.get("create_dir"):
            try:
                _ensure_collection(cfg)
            except WebDAVError as e:
                return _offline(
                    cfg, state, name, e, raise_errors=raise_errors, action="error"
                )
            status, resp_headers, body = _put(url, cfg, local, headers)

        now = time.time()
        if status in (200, 201, 204):
            etag = resp_headers.get("ETag") or st.get("etag")
            _sync_state(
                st,
                etag=etag,
                local_sha=local_sha,
                remote_sha=local_sha,
                now=now,
            )
            state["files"][name] = st
            _save_state(cfg, state)
            return {"name": name, "action": "pushed", "path": str(lp)}

        if status in (412, 428):
            return _push_conflict(
                cfg,
                state,
                name,
                lp,
                local,
                local_sha,
                url,
                raise_errors=raise_errors,
            )

        snippet = body[:200].decode("utf-8", "replace")
        return _offline(
            cfg,
            state,
            name,
            WebDAVError(f"推送 {name} 失败 (HTTP {status}): {snippet}"),
            raise_errors=raise_errors,
            action="error",
        )


def _put(
    url: str, cfg: dict, body: bytes, headers: dict[str, str]
) -> tuple[int, object, bytes]:
    return _request("PUT", url, cfg, data=body, headers=headers)


def _push_conflict(
    cfg: dict,
    state: dict,
    name: str,
    lp: Path,
    local: bytes,
    local_sha: str,
    url: str,
    *,
    raise_errors: bool | None,
) -> dict:
    """412: 远端在本次写入前已被改过. 取回远端版本再按策略处理."""
    try:
        status, resp_headers, remote = _request("GET", url, cfg)
    except WebDAVUnavailable as e:
        return _offline(
            cfg, state, name, e, raise_errors=raise_errors, action="offline"
        )

    now = time.time()
    if status == 404:
        # 另一端删除了远端文件: 本地保存有效, 重新创建
        status, resp_headers, body = _put(
            url,
            cfg,
            local,
            {
                "Content-Type": "application/json; charset=utf-8",
                "If-None-Match": "*",
            },
        )
        if status in (200, 201, 204):
            st = state["files"].setdefault(name, {})
            _sync_state(
                st,
                etag=resp_headers.get("ETag"),
                local_sha=local_sha,
                remote_sha=local_sha,
                now=now,
            )
            _save_state(cfg, state)
            return {"name": name, "action": "pushed", "path": str(lp)}
        return _offline(
            cfg,
            state,
            name,
            WebDAVError(
                f"推送 {name} 失败 (HTTP {status}): "
                f"{body[:200].decode('utf-8', 'replace')}"
            ),
            raise_errors=raise_errors,
            action="error",
        )

    if status >= 400:
        return _offline(
            cfg,
            state,
            name,
            WebDAVError(
                f"读取远端 {name} 失败 (HTTP {status}): "
                f"{remote[:200].decode('utf-8', 'replace')}"
            ),
            raise_errors=raise_errors,
            action="error",
        )

    remote_sha = _sha(remote)
    if remote_sha == local_sha:
        st = state["files"].setdefault(name, {})
        _sync_state(
            st,
            etag=resp_headers.get("ETag"),
            local_sha=local_sha,
            remote_sha=remote_sha,
            now=now,
        )
        _save_state(cfg, state)
        return {"name": name, "action": "unchanged"}

    archived = _archive(name, "remote", remote)
    st = state["files"].setdefault(name, {})
    st["checked_at"] = now
    st["etag"] = resp_headers.get("ETag")
    st["remote_sha256"] = remote_sha
    state["files"][name] = st

    if cfg["conflict"] == "remote-wins":
        archived_local = _archive(name, "local", local)
        _atomic_write(lp, remote)
        _sync_state(
            st,
            etag=resp_headers.get("ETag"),
            local_sha=remote_sha,
            remote_sha=remote_sha,
            now=now,
        )
        _save_state(cfg, state)
        return {
            "name": name,
            "action": "pulled-remote-wins",
            "path": str(lp),
            "archived": str(archived_local),
        }

    return _conflict(
        cfg,
        state,
        name,
        f"{name} 推送冲突: 远端已被其它设备修改, 已保留本地, "
        f"远端版本存档到 {archived}; 确认后用 "
        f"`tracker webdav pull --force` 或 `tracker webdav push --force` 解决",
        raise_errors=raise_errors,
        archived=str(archived),
        local_sha256=local_sha,
        remote_sha256=remote_sha,
    )


def pull_if_enabled(
    name: str,
    local_path: str | Path,
    *,
    force: bool = False,
    ttl: float | None = None,
) -> dict | None:
    """按 poll_interval 节流的自动拉取 (存储层读取前调用)."""
    cfg = load_config()
    if not enabled_for(name, cfg):
        return None
    if ttl is None:
        ttl = cfg.get("poll_interval", 30.0)
    key = (cfg["url"], name, str(Path(local_path)))
    now = time.monotonic()
    with _sync_lock:
        last = _last_pull.get(key)
        if not force and last is not None and now - last < ttl:
            return None
        _last_pull[key] = now
    return pull(name, local_path, force=force, cfg=cfg)


def push_if_enabled(name: str, local_path: str | Path) -> dict | None:
    """保存后自动推送 (存储层写入后调用); 不做节流."""
    cfg = load_config()
    if not enabled_for(name, cfg):
        return None
    return push(name, local_path, cfg=cfg)


def remote_info(
    name: str, *, cfg: dict | None = None, raise_errors: bool = True
) -> dict:
    """查询远端文件元信息 (HEAD, 不支持时回退 GET), 供 CLI status --check."""
    name = _file_name(name) or name
    cfg = cfg or load_config()
    if not enabled_for(name, cfg):
        return {"name": name, "exists": None, "error": "disabled"}
    url = _remote_url(cfg, name)
    try:
        status, headers, body = _request("HEAD", url, cfg)
        if status in (405, 501):
            status, headers, body = _request("GET", url, cfg)
    except WebDAVUnavailable as e:
        if raise_errors:
            raise
        return {"name": name, "exists": None, "error": str(e)}
    if status == 404:
        return {"name": name, "exists": False}
    if status >= 400:
        return {
            "name": name,
            "exists": None,
            "error": f"HTTP {status}: {body[:200].decode('utf-8', 'replace')}",
        }
    return {
        "name": name,
        "exists": True,
        "etag": headers.get("ETag"),
        "last_modified": headers.get("Last-Modified"),
        "content_length": headers.get("Content-Length"),
    }


def status(
    local_paths: dict[str, str | Path] | None = None,
    *,
    cfg: dict | None = None,
    check: bool = False,
    config_file: str | Path | None = None,
) -> dict:
    """汇总配置 / 本地缓存 / 上次同步状态 (CLI webdav status 的数据源)."""
    cfg = cfg or load_config(config_file)
    site_enabled = bool(cfg.get("enabled") and cfg.get("url"))
    state = _load_state(cfg)
    paths = dict(local_paths or {})
    files: dict[str, dict] = {}
    for name in cfg["files"]:
        st = dict(state["files"].get(name) or {})
        lp = Path(paths.get(name) or (data_home() / name))
        body = None
        if lp.exists():
            try:
                body = lp.read_bytes()
            except OSError:
                body = None
        local_sha = _sha(body) if body is not None else None
        entry = {
            "local_path": str(lp),
            "local_exists": lp.exists(),
            "local_sha256": local_sha,
            "etag": st.get("etag"),
            "remote_sha256": st.get("remote_sha256"),
            "synced_at": _iso(st.get("synced_at")),
            "checked_at": _iso(st.get("checked_at")),
            "remote_missing": bool(st.get("remote_missing")),
            "conflict": st.get("conflict"),
            "last_error": st.get("last_error"),
            "last_error_at": _iso(st.get("last_error_at")),
            "in_sync": (
                bool(
                    body is not None
                    and st.get("local_sha256") == local_sha
                    and not st.get("conflict")
                    and not st.get("last_error")
                )
                if site_enabled
                else None
            ),
        }
        if check:
            remote = remote_info(name, cfg=cfg)
            entry["remote"] = remote
            if remote.get("exists") is False:
                entry["remote_changed"] = True
            elif remote.get("etag") and entry.get("etag"):
                entry["remote_changed"] = remote["etag"] != entry["etag"]
            else:
                entry["remote_changed"] = None
            if entry["remote_changed"]:
                entry["in_sync"] = False
        files[name] = entry
    return {
        "config_path": str(config_path(config_file)),
        "enabled": bool(cfg.get("enabled") and cfg.get("url")),
        "url": cfg.get("url") or None,
        "conflict": cfg.get("conflict"),
        "strict": bool(cfg.get("strict")),
        "poll_interval": cfg.get("poll_interval"),
        "files": files,
    }


def _iso(ts) -> str | None:
    if not ts:
        return None
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))
    except (TypeError, ValueError, OSError):
        return None
