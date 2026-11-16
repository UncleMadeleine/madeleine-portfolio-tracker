"""WebDAV 同步: 条件拉取 / 条件推送 / 冲突存档 / 认证 / 存储层自动接入."""

from __future__ import annotations

import base64
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

import pytest

from tracker import storage
from tracker import webdav as dav


def _etag(body: bytes) -> str:
    return '"' + hashlib.sha1(body).hexdigest() + '"'


class _FakeDAV:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.lock = threading.Lock()
        self.requests: list[tuple[str, str, dict]] = []
        self.auth: tuple[str, str] | None = None
        self.ignore_conditional = False

    def auth_header(self) -> str | None:
        if self.auth is None:
            return None
        raw = f"{self.auth[0]}:{self.auth[1]}".encode("utf-8")
        return "Basic " + base64.b64encode(raw).decode("ascii")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def dav(self) -> _FakeDAV:
        return self.server.dav  # type: ignore[attr-defined]

    def log_message(self, *args):  # noqa: D102 - 测试安静
        pass

    def _send(self, status: int, body: bytes = b"", headers=None) -> None:
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD" and status != 304:
            self.wfile.write(body)

    def _name(self) -> str:
        return unquote(self.path.lstrip("/"))

    def _authorized(self) -> bool:
        expected = self.dav.auth_header()
        if expected is None:
            return True
        if self.headers.get("Authorization") == expected:
            return True
        self._send(401, b"unauthorized", {"WWW-Authenticate": 'Basic realm="dav"'})
        return False

    def do_MKCOL(self):
        self.dav.requests.append(("MKCOL", self.path, dict(self.headers)))
        if not self._authorized():
            return
        self._send(201, b"")

    def do_HEAD(self):
        self.dav.requests.append(("HEAD", self.path, dict(self.headers)))
        if not self._authorized():
            return
        with self.dav.lock:
            body = self.dav.files.get(self._name())
        if body is None:
            self._send(404, b"")
            return
        self._send(
            200,
            body,
            {"ETag": _etag(body), "Content-Length": str(len(body))},
        )

    def do_GET(self):
        self.dav.requests.append(("GET", self.path, dict(self.headers)))
        if not self._authorized():
            return
        with self.dav.lock:
            body = self.dav.files.get(self._name())
        if body is None:
            self._send(404, b"")
            return
        etag = _etag(body)
        if self.headers.get("If-None-Match") == etag:
            self._send(304, b"", {"ETag": etag})
            return
        self._send(200, body, {"ETag": etag})

    def do_PUT(self):
        self.dav.requests.append(("PUT", self.path, dict(self.headers)))
        if not self._authorized():
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        name = self._name()
        with self.dav.lock:
            current = self.dav.files.get(name)
            current_etag = _etag(current) if current is not None else None
            if not self.dav.ignore_conditional:
                if_match = self.headers.get("If-Match")
                if_none = self.headers.get("If-None-Match")
                if if_match is not None and if_match != current_etag:
                    self._send(412, b"etag mismatch")
                    return
                if if_none == "*" and current is not None:
                    self._send(412, b"already exists")
                    return
            self.dav.files[name] = body
        self._send(201, b"", {"ETag": _etag(body)})


@pytest.fixture
def dav_server():
    fake = _FakeDAV()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.dav = fake  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    fake.url = f"http://127.0.0.1:{server.server_port}/"
    try:
        yield fake
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _configure(monkeypatch, tmp_path, url: str, **overrides) -> None:
    cfg = {
        "enabled": True,
        "url": url,
        "timeout": 3,
        "poll_interval": 0,
        "files": ["portfolio.json", "watchlist.json"],
    }
    cfg.update(overrides)
    p = tmp_path / "webdav.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv(dav.CONFIG_ENV, str(p))
    dav.reset_cache()


def test_disabled_by_default(tmp_path):
    local = tmp_path / "portfolio.json"
    local.write_text('{"holdings": []}', encoding="utf-8")
    assert dav.pull("portfolio.json", local)["action"] == "disabled"
    assert dav.push("portfolio.json", local)["action"] == "disabled"


def test_pull_then_etag_304(monkeypatch, tmp_path, dav_server):
    _configure(monkeypatch, tmp_path, dav_server.url)
    remote = b'{"base_currency": "CNY", "holdings": [{"symbol": "AAPL"}]}'
    dav_server.files["portfolio.json"] = remote
    local = tmp_path / "portfolio.json"

    result = dav.pull("portfolio.json", local, raise_errors=True)
    assert result["action"] == "pulled"
    assert local.read_bytes() == remote

    dav_server.requests.clear()
    result = dav.pull("portfolio.json", local, raise_errors=True)
    assert result["action"] == "unchanged"
    assert dav_server.requests[0][0] == "GET"
    assert dav_server.requests[0][2]["If-None-Match"] == _etag(remote)


def test_remote_change_pulls_when_local_clean(monkeypatch, tmp_path, dav_server):
    _configure(monkeypatch, tmp_path, dav_server.url)
    local = tmp_path / "portfolio.json"
    dav_server.files["portfolio.json"] = b'{"v": 1}'
    dav.pull("portfolio.json", local, raise_errors=True)

    dav_server.files["portfolio.json"] = b'{"v": 2}'
    result = dav.pull("portfolio.json", local, raise_errors=True)
    assert result["action"] == "pulled"
    assert json.loads(local.read_text(encoding="utf-8"))["v"] == 2


def test_push_creates_remote(monkeypatch, tmp_path, dav_server):
    _configure(monkeypatch, tmp_path, dav_server.url)
    local = tmp_path / "watchlist.json"
    local.write_text('{"watchlist": [{"symbol": "TSLA"}]}', encoding="utf-8")

    result = dav.push("watchlist.json", local, raise_errors=True)
    assert result["action"] == "pushed"
    assert json.loads(dav_server.files["watchlist.json"])["watchlist"][0]["symbol"] == (
        "TSLA"
    )


def test_push_existing_remote_without_state_conflicts(
    monkeypatch, tmp_path, dav_server
):
    _configure(monkeypatch, tmp_path, dav_server.url)
    dav_server.files["portfolio.json"] = b'{"v": "remote"}'
    local = tmp_path / "portfolio.json"
    local.write_text('{"v": "local"}', encoding="utf-8")

    with pytest.raises(dav.WebDAVConflict):
        dav.push("portfolio.json", local, raise_errors=True)
    assert json.loads(dav_server.files["portfolio.json"])["v"] == "remote"


def test_conflict_keeps_local_and_archives_remote(
    monkeypatch, tmp_path, dav_server
):
    _configure(monkeypatch, tmp_path, dav_server.url)
    local = tmp_path / "portfolio.json"
    dav_server.files["portfolio.json"] = b'{"v": 1}'
    dav.pull("portfolio.json", local, raise_errors=True)

    # 两端各改一次
    dav_server.files["portfolio.json"] = b'{"v": "remote"}'
    local.write_text('{"v": "local"}', encoding="utf-8")

    result = dav.pull("portfolio.json", local, raise_errors=False)
    assert result["action"] == "conflict"
    assert json.loads(local.read_text(encoding="utf-8"))["v"] == "local"
    archived = list((tmp_path / "tracker-home" / "var" / "webdav-conflicts").glob("*"))
    assert archived
    assert json.loads(archived[0].read_text(encoding="utf-8"))["v"] == "remote"

    # 冲突未解决时重复 poll 不再复制/告警; 普通保存也不会自动覆盖远端
    result = dav.pull("portfolio.json", local, raise_errors=True)
    assert result["action"] == "conflict" and result.get("suppressed")
    assert len(list(archived[0].parent.glob("*"))) == 1
    result = dav.push("portfolio.json", local, raise_errors=True)
    assert result["action"] == "conflict" and result.get("suppressed")
    assert json.loads(dav_server.files["portfolio.json"])["v"] == "remote"


def test_force_pull_and_push_resolve_conflict(monkeypatch, tmp_path, dav_server):
    _configure(monkeypatch, tmp_path, dav_server.url)
    local = tmp_path / "portfolio.json"
    dav_server.files["portfolio.json"] = b'{"v": 1}'
    dav.pull("portfolio.json", local, raise_errors=True)
    dav_server.files["portfolio.json"] = b'{"v": "remote"}'
    local.write_text('{"v": "local"}', encoding="utf-8")

    result = dav.pull("portfolio.json", local, force=True, raise_errors=True)
    assert result["action"] == "pulled-forced"
    assert json.loads(local.read_text(encoding="utf-8"))["v"] == "remote"

    result = dav.push("portfolio.json", local, force=True, raise_errors=True)
    assert result["action"] == "pushed"
    assert json.loads(dav_server.files["portfolio.json"])["v"] == "remote"


def test_remote_wins_policy(monkeypatch, tmp_path, dav_server):
    _configure(monkeypatch, tmp_path, dav_server.url, conflict="remote-wins")
    local = tmp_path / "watchlist.json"
    dav_server.files["watchlist.json"] = b'{"v": 1}'
    dav.pull("watchlist.json", local, raise_errors=True)
    dav_server.files["watchlist.json"] = b'{"v": "remote"}'
    local.write_text('{"v": "local"}', encoding="utf-8")

    result = dav.pull("watchlist.json", local, raise_errors=True)
    assert result["action"] == "pulled-remote-wins"
    assert json.loads(local.read_text(encoding="utf-8"))["v"] == "remote"


def test_basic_auth_and_status_check(monkeypatch, tmp_path, dav_server):
    dav_server.auth = ("alice", "secret")
    _configure(
        monkeypatch,
        tmp_path,
        dav_server.url,
        username="alice",
        password="secret",
    )
    local = tmp_path / "portfolio.json"
    dav_server.files["portfolio.json"] = b'{"holdings": []}'
    dav.pull("portfolio.json", local, raise_errors=True)

    info = dav.status(
        {"portfolio.json": local, "watchlist.json": tmp_path / "watchlist.json"},
        check=True,
    )
    assert info["enabled"] is True
    assert info["files"]["portfolio.json"]["remote"]["exists"] is True
    assert info["files"]["portfolio.json"]["remote"]["etag"] == _etag(
        dav_server.files["portfolio.json"]
    )


def test_password_env_overrides_file(monkeypatch, tmp_path, dav_server):
    dav_server.auth = ("alice", "from-env")
    _configure(
        monkeypatch,
        tmp_path,
        dav_server.url,
        username="alice",
        password="wrong",
    )
    monkeypatch.setenv("TRACKER_WEBDAV_PASSWORD", "from-env")
    dav.reset_cache()
    local = tmp_path / "portfolio.json"
    dav_server.files["portfolio.json"] = b'{"holdings": []}'
    assert dav.pull("portfolio.json", local, raise_errors=True)["action"] == "pulled"


def test_offline_falls_back_to_cache(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, "http://127.0.0.1:1/dav/", timeout=1)
    local = tmp_path / "portfolio.json"
    local.write_text('{"holdings": []}', encoding="utf-8")

    with pytest.warns(RuntimeWarning):
        result = dav.pull("portfolio.json", local)
    assert result["action"] == "offline"
    assert local.exists()
    with pytest.raises(dav.WebDAVUnavailable):
        dav.pull("portfolio.json", local, raise_errors=True)


def test_storage_auto_sync(monkeypatch, tmp_path, dav_server):
    _configure(monkeypatch, tmp_path, dav_server.url)
    local = storage.PORTFOLIO_PATH

    storage.save_portfolio(
        {"holdings": [{"symbol": "AAPL", "quantity": 2}]}, local
    )
    assert json.loads(dav_server.files["portfolio.json"])["holdings"][0]["symbol"] == (
        "AAPL"
    )

    # 模拟另一台设备改远端; 本地删除后 load 应自动拉回
    dav_server.files["portfolio.json"] = (
        '{"base_currency": "CNY", "holdings": [{"symbol": "MSFT"}]}'.encode()
    )
    local.unlink()
    data = storage.load_portfolio(local)
    assert data["holdings"][0]["symbol"] == "MSFT"


def test_storage_auto_sync_watchlist(monkeypatch, tmp_path, dav_server):
    _configure(monkeypatch, tmp_path, dav_server.url)
    local = storage.WATCHLIST_PATH
    storage.save_watchlist({"watchlist": [{"symbol": "TSLA"}]}, local)
    assert json.loads(dav_server.files["watchlist.json"])["watchlist"][0]["symbol"] == (
        "TSLA"
    )


def test_custom_portfolio_path_is_not_pushed(monkeypatch, tmp_path, dav_server):
    """CLI --portfolio 指向备份/临时文件时不能覆盖远端默认组合."""
    _configure(monkeypatch, tmp_path, dav_server.url)
    other = tmp_path / "backup.json"
    storage.save_portfolio({"holdings": [{"symbol": "MSFT"}]}, other)
    assert "portfolio.json" not in dav_server.files
