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

import base64
import hashlib
import json
import os
import ssl
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings
from pathlib import Path

from . import config
from .paths import data_home, var_dir

CONFIG_ENV = "TRACKER_WEBDAV_CONFIG"
CONFIG_NAME = "webdav.json"

#: 默认只接管这两个文件; 远端名与本地名一致
DEFAULT_FILES = ["portfolio.json", "watchlist.json"]

DEFAULTS: dict = {
    "enabled": False,
    # WebDAV 目录 URL, 例如 Nextcloud:
    # https://cloud.example.com/remote.php/dav/files/alice/tracker/
    "url": "",
    "username": "",
    "password": "",
    # 优先从环境变量取密码 (容器里不必把密码落盘); 未设置时回退 password
    "password_env": "TRACKER_WEBDAV_PASSWORD",
    "username_env": "",
    "timeout": 15.0,
    "verify_ssl": True,
    # 自动拉取的最小间隔 (秒): Streamlit 每次交互都会 load, 不能每次都打远端
    "poll_interval": 30.0,
    # 冲突策略: keep-local(默认, 保留本地并把远端版本存档) /
    # remote-wins(远端覆盖本地, 本地版本存档)
    "conflict": "keep-local",
    # true: 自动同步失败直接抛错; false: 告警后继续用本地缓存
    "strict": False,
    # PUT 遇到目录不存在时是否尝试 MKCOL 远端目录
    "create_dir": True,
    "files": DEFAULT_FILES,
}

_CONFLICT_CHOICES = ("keep-local", "remote-wins")


class WebDAVError(RuntimeError):
    """WebDAV 同步失败的可读错误基类."""


class WebDAVUnavailable(WebDAVError):
    """网络 / TLS / 认证等无法访问远端."""


class WebDAVConflict(WebDAVError):
    """本地与远端都修改过, 且当前策略要求人工处理."""


# 进程内自动拉取节流: (url, name, local_path) -> monotonic 时间
_last_pull: dict[tuple[str, str, str], float] = {}
_sync_lock = threading.RLock()
_umask_cache: int | None = None


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


def _as_bool(value, default: bool = False) -> bool:
    """手改配置的布尔值宽松归一 (与 storage._as_bool 口径一致)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("true", "1", "yes", "on"):
            return True
        if s in ("false", "0", "no", "off", ""):
            return False
    return default


def config_path(explicit: str | Path | None = None) -> Path:
    """配置文件路径: 显式 path > env TRACKER_WEBDAV_CONFIG > 数据目录/webdav.json."""
    return config.resolve_config_path(
        explicit, CONFIG_ENV, data_home() / CONFIG_NAME
    )


def _file_name(value) -> str | None:
    """只接受不含目录的文件名, 防止配置把文件写到远端别处."""
    s = str(value or "").strip()
    if not s or Path(s).name != s or s in (".", ".."):
        return None
    return s


def load_config(explicit: str | Path | None = None) -> dict:
    """读取并归一 WebDAV 配置; 缺失/损坏时返回默认 (enabled=False)."""
    raw = config.load_json_object(config_path(explicit))
    cfg = dict(DEFAULTS)
    cfg["files"] = list(DEFAULT_FILES)
    cfg.update({k: v for k, v in raw.items() if not str(k).startswith("_")})

    cfg["enabled"] = _as_bool(cfg.get("enabled"), False)
    cfg["verify_ssl"] = _as_bool(cfg.get("verify_ssl"), True)
    cfg["strict"] = _as_bool(cfg.get("strict"), False)
    cfg["create_dir"] = _as_bool(cfg.get("create_dir"), True)

    url = str(cfg.get("url") or "").strip()
    if url and not url.endswith("/"):
        url += "/"
    cfg["url"] = url

    files = cfg.get("files")
    if isinstance(files, str):
        files = [files]
    if not isinstance(files, list):
        files = list(DEFAULT_FILES)
    cleaned = []
    for item in files:
        name = _file_name(item)
        # 目前只有 storage 的 portfolio/watchlist 接入自动同步; 其它文件名
        # 写进配置也不会被调用, 直接忽略以免 status 显示一个永远不同步的项。
        if name in DEFAULT_FILES and name not in cleaned:
            cleaned.append(name)
    cfg["files"] = cleaned or list(DEFAULT_FILES)

    try:
        cfg["timeout"] = max(1.0, float(cfg.get("timeout") or 15.0))
    except (TypeError, ValueError):
        cfg["timeout"] = 15.0
    try:
        cfg["poll_interval"] = max(0.0, float(cfg.get("poll_interval") or 0.0))
    except (TypeError, ValueError):
        cfg["poll_interval"] = 30.0

    if str(cfg.get("conflict") or "").strip().lower() not in _CONFLICT_CHOICES:
        cfg["conflict"] = "keep-local"
    else:
        cfg["conflict"] = str(cfg["conflict"]).strip().lower()

    # 密码/用户名 env 优先于配置文件 (容器/CI 不必落盘凭据)
    password_env = str(cfg.get("password_env") or "").strip()
    if password_env and os.environ.get(password_env):
        cfg["password"] = os.environ[password_env]
    username_env = str(cfg.get("username_env") or "").strip()
    if username_env and os.environ.get(username_env):
        cfg["username"] = os.environ[username_env]
    cfg["username"] = str(cfg.get("username") or "")
    cfg["password"] = str(cfg.get("password") or "")
    return cfg


def enabled_for(name: str, cfg: dict | None = None) -> bool:
    """该文件是否由 WebDAV 接管."""
    cfg = cfg or load_config()
    return (
        bool(cfg.get("enabled"))
        and bool(cfg.get("url"))
        and _file_name(name) in (cfg.get("files") or [])
    )


def managed_files(cfg: dict | None = None) -> list[str]:
    cfg = cfg or load_config()
    return list(cfg.get("files") or []) if cfg.get("enabled") else []


def reset_cache() -> None:
    """清空进程内缓存 (测试 / 手改配置后需要立即生效时调用)."""
    with _sync_lock:
        _last_pull.clear()
    config.clear_cache()


# ---------------------------------------------------------------------------
# HTTP / WebDAV 基础操作
# ---------------------------------------------------------------------------


def _auth_header(cfg: dict) -> str | None:
    user = cfg.get("username") or ""
    pwd = cfg.get("password") or ""
    if not user and not pwd:
        return None
    token = base64.b64encode(f"{user}:{pwd}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _remote_url(cfg: dict, name: str) -> str:
    return cfg["url"].rstrip("/") + "/" + urllib.parse.quote(name, safe="/")


def _request(
    method: str,
    url: str,
    cfg: dict,
    *,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, object, bytes]:
    """发一个 WebDAV 请求; HTTP 错误返回 (status, headers, body).

    只有网络层错误 (DNS / 超时 / TLS / 拒绝连接) 抛 WebDAVUnavailable,
    HTTP 4xx/5xx 交给调用方按状态码处理。
    """
    hdrs = {
        "User-Agent": "madeleine-tracker-webdav/1",
        "Accept": "application/json, text/plain, */*",
    }
    auth = _auth_header(cfg)
    if auth:
        hdrs["Authorization"] = auth
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    context = None
    if url.lower().startswith("https://") and not cfg.get("verify_ssl", True):
        context = ssl._create_unverified_context()  # noqa: SLF001 - 显式关闭校验
    try:
        with urllib.request.urlopen(  # noqa: S310 - 用户显式配置的 WebDAV 地址
            req, timeout=cfg.get("timeout", 15.0), context=context
        ) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise WebDAVUnavailable(f"无法访问 WebDAV {url}: {reason}") from e


def _ensure_collection(cfg: dict) -> None:
    """MKCOL 远端目录 (已存在返回 405, 视为成功)."""
    status, _, body = _request("MKCOL", cfg["url"].rstrip("/") + "/", cfg)
    if status in (200, 201, 204, 405):
        return
    snippet = body[:200].decode("utf-8", "replace")
    raise WebDAVError(
        f"创建 WebDAV 目录失败 (HTTP {status}): {cfg['url']} {snippet}".strip()
    )


# ---------------------------------------------------------------------------
# 本地原子写 / 状态文件
# ---------------------------------------------------------------------------


def _umask() -> int:
    global _umask_cache
    if _umask_cache is None:
        cur = os.umask(0o022)
        os.umask(cur)
        _umask_cache = cur
    return _umask_cache


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        try:
            mode = path.stat().st_mode & 0o7777 if path.exists() else 0o666 & ~_umask()
            os.chmod(tmp, mode)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _state_path() -> Path:
    return var_dir() / "webdav_state.json"


def _load_state(cfg: dict) -> dict:
    p = _state_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict) or data.get("base_url") != cfg["url"]:
        # URL 换过 (或首次启用): 旧 ETag/哈希都对不上新目录, 从头开始
        return {"base_url": cfg["url"], "files": {}}
    if not isinstance(data.get("files"), dict):
        data["files"] = {}
    return data


def _save_state(cfg: dict, state: dict) -> None:
    state["base_url"] = cfg["url"]
    payload = json.dumps(state, ensure_ascii=False, indent=2, allow_nan=False)
    _atomic_write(_state_path(), payload.encode("utf-8"))


def _archive(name: str, kind: str, body: bytes) -> Path:
    """把冲突的一方存到 var/webdav-conflicts/, 返回路径."""
    d = var_dir() / "webdav-conflicts"
    d.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    p = d / f"{name}.{stamp}.{kind}.json"
    i = 1
    while p.exists():
        p = d / f"{name}.{stamp}-{i}.{kind}.json"
        i += 1
    _atomic_write(p, body)
    return p


# ---------------------------------------------------------------------------
# 冲突 / 错误处理
# ---------------------------------------------------------------------------


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
