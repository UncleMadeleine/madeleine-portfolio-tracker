"""longport-login 子命令: 终端发起长桥 OAuth 登录.

打印授权链接 → 用户在浏览器登录长桥并授权 → SDK 本地回调接收 code →
token 持久化到 ~/.longport/openapi/tokens/<client_id> (自动刷新)。
"""

from __future__ import annotations

import webbrowser

from .. import longport
from ._common import _finish_with_error


_ROOT = str(__import__("pathlib").Path(__file__).resolve().parent.parent.parent)


def cmd_longport_login(args) -> None:
    """阻塞式 OAuth 登录: 打开浏览器 → 等待本地回调 → 验证凭据."""

    cfg = longport.load_config()
    client_id = str(cfg.get("client_id") or "").strip()
    if not client_id or "填入" in client_id:
        _finish_with_error(
            "longport.json 缺少有效 client_id。"
            "请先注册 OAuth 客户端 (见 longport.example.json 中的注册命令), "
            "把返回的 client_id 填入 longport.json。"
        )
    if args.json:
        # JSON 模式: 不自动开浏览器, 只产出授权 URL。SDK build(on_open) 会占住
        # GIL 阻塞等回调 (线程内也不释放), 只能用子进程: on_open 把 URL 写入
        # 临时文件后 os._exit, 父进程轮询读取。子进程中断授权流, 本次 URL 作废,
        # 用户完成授权后需重跑本命令完成回调 (token 由 SDK 缓存)。
        import json as _json
        import os
        import subprocess
        import sys
        import tempfile
        import time as _time

        with tempfile.NamedTemporaryFile("w", suffix=".url", delete=False) as tf:
            url_file = tf.name
        child_code = (
            "import os, sys\n"
            "sys.path.insert(0, %r)\n"
            "from tracker import longport\n"
            "cfg = longport.load_config()\n"
            "try:\n"
            "    longport.oauth_login(cfg, on_open_url=lambda u: (\n"
            "        open(%r, 'w').write(u), os._exit(0)))\n"
            "except Exception as e:\n"
            "    open(%r, 'w').write('ERROR:' + str(e))\n"
            "    os._exit(1)\n"
        ) % (_ROOT, url_file, url_file)
        try:
            proc = subprocess.Popen(
                [sys.executable, "-c", child_code],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            url = None
            for _ in range(30):
                _time.sleep(1)
                if proc.poll() is not None and not os.path.exists(url_file):
                    break
                try:
                    content = open(url_file, encoding="utf-8").read().strip()
                except FileNotFoundError:
                    continue
                if content:
                    url = content
                    break
        finally:
            proc.kill()
            try:
                os.unlink(url_file)
            except OSError:
                pass
        if not url:
            _finish_with_error("获取授权 URL 超时 (30s), 请检查网络后重试")
        if url.startswith("ERROR:"):
            _finish_with_error(f"登录失败: {url[6:]}")
        if args.output:
            from pathlib import Path

            Path(args.output).write_text(url + "\n", encoding="utf-8")
        print(
            _json.dumps({"status": "ok", "authorization_url": url}, ensure_ascii=False)
        )
        return
    try:
        import longport.openapi as api

        port = cfg.get("callback_port")
        builder = (
            api.OAuthBuilder(client_id, int(port))
            if port
            else api.OAuthBuilder(client_id)
        )
        oauth = builder.build(
            lambda url: (print(f"\n授权链接: {url}\n"), _try_open(url))
        )
        lang = longport._language(api, cfg)
        kwargs = {"language": lang} if lang is not None else {}
        api.Config.from_oauth(oauth, **kwargs)
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 - 显式报错退出
        _finish_with_error(f"登录失败: {e}")
    longport.reset()
    print("✅ 长桥登录成功, token 已缓存 (自动刷新)。现在可以使用:")
    print("   tracker import longport          # 导入长桥持仓")
    print("   tracker quote AAPL --longport    # 长桥行情优先")
    print("   UI 导入页 → 长桥账户 标签页")


def _try_open(url: str) -> None:
    try:
        webbrowser.open(url)
        print("(浏览器未自动打开时, 请手动复制上方链接到浏览器)")
    except Exception:  # noqa: BLE001 - 无桌面环境等场景
        pass
