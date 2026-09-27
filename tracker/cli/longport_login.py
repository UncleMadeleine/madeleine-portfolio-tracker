"""longport-login 子命令: 终端发起长桥 OAuth 登录.

打印授权链接 → 用户在浏览器登录长桥并授权 → SDK 本地回调接收 code →
token 持久化到 ~/.longport/openapi/tokens/<client_id> (自动刷新)。
"""
from __future__ import annotations

import webbrowser

from .. import longport
from ._common import _finish_with_error, _print_json


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
    print("=== 长桥 OAuth 登录 ===")
    print("即将打开浏览器进入长桥授权页; 授权完成后本命令自动结束。")
    if args.json:
        # JSON 模式下不自动开浏览器, 只输出授权 URL 供脚本消费
        try:
            longport.oauth_login(cfg)
        except Exception as e:  # noqa: BLE001 - 显式报错退出
            _finish_with_error(f"登录失败: {e}")
        _print_json({"status": "ok"})
        return
    try:
        import longport.openapi as api

        port = cfg.get("callback_port")
        builder = (
            api.OAuthBuilder(client_id, int(port)) if port else api.OAuthBuilder(client_id)
        )
        oauth = builder.build(lambda url: (print(f"\n授权链接: {url}\n"), _try_open(url)))
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
