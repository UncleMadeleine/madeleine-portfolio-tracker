"""长桥 UI 登录引导: 授权子进程管理 + 状态轮询渲染 (导入页/设置页共用).

SDK 的 OAuthBuilder.build() 会持有 GIL 阻塞整个解释器, 不能在 UI 进程内运行,
因此授权放在独立子进程 (python -m tracker.longport_oauth) 中:
  - 启动: subprocess.Popen, 子进程先写状态文件 waiting (含授权 URL), 授权完成写 ok
  - 轮询: 每次 rerun 读 var/longport_oauth_state.json 渲染对应界面
  - token 由 SDK 持久化到 ~/.longport/openapi/tokens/<client_id> 并自动刷新
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import streamlit as st

from tracker import longport

_STATE_PATH = (
    Path(__file__).resolve().parent.parent.parent / "var" / "longport_oauth_state.json"
)
_STALE_SECONDS = 600.0  # 状态文件超过 10 分钟视为过期流程


def _read_state() -> dict | None:
    try:
        data = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict) and time.time() - data.get("ts", 0) < _STALE_SECONDS:
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return None


def _write_state(payload: dict) -> None:
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STATE_PATH.write_text(
            json.dumps({**payload, "ts": time.time()}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass


def _popens() -> subprocess.Popen | None:
    """已存在的登录子进程 (状态未完成且进程存活); UI rerun 间复用."""
    proc = st.session_state.get("lp_oauth_proc")
    if proc is not None and proc.poll() is None:
        return proc
    return None


def _start_login() -> None:
    _write_state({"status": "starting"})
    proc = subprocess.Popen(
        [sys.executable, "-m", "tracker.longport_oauth"],
        cwd=str(Path(__file__).resolve().parent.parent.parent),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    st.session_state["lp_oauth_proc"] = proc


def _client_id_configured() -> bool:
    cfg = longport.load_config()
    cid = str(cfg.get("client_id") or "").strip()
    return bool(cid) and "填入" not in cid


def render_login_section(key_prefix: str = "lp") -> None:
    """长桥登录状态卡片: 状态 + 登录/打开授权页/重试/取消操作."""
    if not _client_id_configured():
        st.warning(
            "尚未配置长桥 OAuth 客户端: 复制 longport.example.json 为 longport.json, "
            "按其中说明注册 OAuth 客户端并填入 client_id。",
            icon=":material/key_off:",
        )
        return

    # 状态文件是权威: 子进程 (可能来自别的页面会话) 写的 waiting/error 都要如实展示
    state = _read_state()
    status = (state or {}).get("status", "idle")

    if status == "ok":
        st.success(
            "长桥已登录。token 缓存于 ~/.longport/openapi/tokens/ (SDK 自动刷新)。",
            icon=":material/check_circle:",
        )
        proc = st.session_state.pop("lp_oauth_proc", None)
        if proc is not None and proc.poll() is None:
            proc.terminate()
        _STATE_PATH.unlink(missing_ok=True)
        if st.button("退出重登", icon=":material/logout:", key=f"{key_prefix}_logout"):
            _start_login()
            st.rerun()
        return

    if status == "waiting":
        st.info(
            "1. 点击下方按钮打开长桥授权页并登录; 2. 授权后本页自动显示结果。",
            icon=":material/open_in_new:",
        )
        st.link_button(
            "打开长桥授权页",
            (state or {}).get("url") or "",
            icon=":material/link:",
            use_container_width=True,
        )
        if st.button("取消", icon=":material/close:", key=f"{key_prefix}_cancel"):
            _cancel()
            st.rerun()
        st.button("刷新状态", icon=":material/refresh:", key=f"{key_prefix}_refresh")
        return

    if status == "starting":
        st.info("正在启动授权流程…", icon=":material/hourglass_top:")
        if st.button("取消", icon=":material/close:", key=f"{key_prefix}_cancel0"):
            _cancel()
            st.rerun()
        st.button("刷新状态", icon=":material/refresh:", key=f"{key_prefix}_refresh0")
        return

    if status == "error":
        st.error(
            f"登录失败: {(state or {}).get('message', '未知错误')}",
            icon=":material/error:",
        )

    if st.button(
        "登录长桥账户" if status != "error" else "重试登录",
        icon=":material/login:",
        key=f"{key_prefix}_start",
        type="primary",
    ):
        _start_login()
        st.rerun()


def _cancel() -> None:
    proc = st.session_state.pop("lp_oauth_proc", None)
    if proc is not None and proc.poll() is None:
        proc.terminate()
    st.session_state["lp_oauth_view"] = "idle"
    try:
        _STATE_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def login_state_label() -> str:
    """紧凑状态文案 (供设置页摘要展示)."""
    if st.session_state.get("lp_oauth_view") == "ok":
        return "已登录"
    state = _read_state()
    if state is None:
        return "未登录"
    return {
        "starting": "正在启动授权…",
        "waiting": "等待浏览器授权…",
        "ok": "已登录",
        "error": "登录失败",
    }.get(state.get("status", ""), "未登录")
