"""WebDAV 配置 / HTTP 客户端 / 本地状态文件.

从 webdav.py 拆出: 同步算法 (pull/push/冲突策略) 留在 webdav.py, 本模块只
负责配置解析、条件请求、原子写与 ETag 状态持久化。
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
from pathlib import Path

from . import config
from .paths import data_home, var_dir
from .util import as_bool as _as_bool
from .util import current_umask as _umask

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



# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


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


