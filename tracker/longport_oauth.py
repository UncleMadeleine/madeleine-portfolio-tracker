"""长桥 OAuth 登录子进程入口: python -m tracker.longport_oauth.

独立进程运行 SDK 的 OAuthBuilder.build() — 它会持有 GIL 阻塞整个解释器,
不能嵌入 Streamlit 主进程/线程; 放在子进程里则互不干扰。

状态协议: 全程写 JSON 状态文件 var/longport_oauth_state.json,
  {"status": "waiting"|"ok"|"error", "url": ..., "message": ..., "ts": ...}
UI 侧 (longport_login.py) 用 subprocess.Popen 启动本模块, 轮询状态文件渲染。
token 由 SDK 持久化到 ~/.longport/openapi/tokens/<client_id> 并自动刷新。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from . import longport

_STATE_PATH = Path(__file__).resolve().parent.parent / "var" / "longport_oauth_state.json"


def _write_state(payload: dict) -> None:
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STATE_PATH.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({**payload, "ts": time.time()}, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(_STATE_PATH)  # 原子替换
    except OSError:
        pass


def main() -> int:
    cfg = longport.load_config()

    client_id = str(cfg.get("client_id") or "").strip()
    if not client_id or "填入" in client_id:
        _write_state({"status": "error", "message": "longport.json 缺少有效 client_id"})
        print("❌ longport.json 缺少有效 client_id", file=sys.stderr)
        return 2

    def on_open(url: str) -> None:
        _write_state({"status": "waiting", "url": url})
        print(f"请在浏览器中打开以下链接完成长桥授权:\n  {url}", flush=True)
        try:
            import webbrowser

            webbrowser.open(url)
        except Exception:  # noqa: BLE001 - 无桌面环境
            pass

    try:
        api = longport._sdk()
        port = cfg.get("callback_port")
        builder = (
            api.OAuthBuilder(client_id, int(port)) if port else api.OAuthBuilder(client_id)
        )
        oauth = builder.build(on_open)
        lang = longport._language(api, cfg)
        kwargs = {"language": lang} if lang is not None else {}
        api.Config.from_oauth(oauth, **kwargs)  # 验证凭据真实可用
    except Exception as e:  # noqa: BLE001 - 失败透出
        _write_state({"status": "error", "message": str(e)[:300]})
        print(f"❌ 登录失败: {e}", file=sys.stderr)
        return 1
    _write_state({"status": "ok"})
    print("✅ 长桥登录成功, token 已缓存 (SDK 自动刷新)。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
