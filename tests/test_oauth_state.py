"""OAuth 状态文件读写 (父进程/授权子进程协议) 的离线测试."""

from __future__ import annotations

import json
import time

from tracker.oauth_state import read_state, write_state


def test_write_and_read_state(tmp_path):
    p = tmp_path / "state.json"
    write_state(p, {"status": "waiting", "url": "https://example.test"})
    data = read_state(p, max_age=60)
    assert data is not None
    assert data["status"] == "waiting"
    assert data["url"] == "https://example.test"


def test_read_state_stale_and_malformed_timestamps(tmp_path):
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"status": "ok", "ts": time.time() - 3600}), encoding="utf-8")
    assert read_state(p, max_age=60) is None

    # 手改/半截写入的 ts 不能抛 TypeError, 按过期处理
    p.write_text(json.dumps({"status": "ok", "ts": "not-a-number"}), encoding="utf-8")
    assert read_state(p, max_age=60) is None
