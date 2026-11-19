"""HTTP JSON 访问与错误脱敏的共享实现.

EODHD / 钱包历史浏览器等外部接口都需要:
- 统一 timeout / headers / 参数过滤;
- 非 2xx / 非 JSON 响应转成可读异常;
- 错误信息里不泄露 API key / token。

各领域模块只保留自己的状态码提示与异常类型转换, 不再各写一份 requests 调用。
"""

from __future__ import annotations

import re

from .deps import requests as _requests

__all__ = ["HTTPError", "request_json", "scrub"]


class HTTPError(RuntimeError):
    """HTTP 请求失败 (网络 / 非 2xx / 非 JSON)."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        body: str = "",
    ) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


def scrub(text: object, secrets=()) -> str:
    """错误信息脱敏: 替换密钥字面量并遮蔽 api_token 查询参数."""
    s = str(text)
    for secret in secrets:
        if secret:
            s = s.replace(str(secret), "***")
    return re.sub(
        r"((?:api_token|apikey)=)[^&\s\"']+",
        r"\1***",
        s,
        flags=re.IGNORECASE,
    )


def request_json(
    method: str,
    url: str,
    *,
    params: dict | None = None,
    data: dict | None = None,
    timeout: float = 15.0,
    headers: dict | None = None,
):
    """发送 HTTP 请求并解析 JSON; 失败统一抛 HTTPError."""
    clean = {k: v for k, v in (params or {}).items() if v is not None}
    kwargs: dict = {"params": clean or None, "timeout": timeout}
    if data is not None:
        kwargs["data"] = data
    if headers:
        kwargs["headers"] = headers
    req = _requests()
    try:
        verb = method.upper()
        if verb == "GET":
            resp = req.get(url, **kwargs)
        elif verb == "POST":
            resp = req.post(url, **kwargs)
        else:
            resp = req.request(verb, url, **kwargs)
    except Exception as e:  # noqa: BLE001 - 网络异常统一转可读错误
        raise HTTPError(f"请求失败: {e}") from e

    status = int(getattr(resp, "status_code", 0) or 0)
    text = getattr(resp, "text", "") or ""
    if status >= 400:
        raise HTTPError(
            f"HTTP {status}: {text[:200]}".strip(),
            status=status,
            body=text,
        )
    try:
        return resp.json()
    except ValueError as e:
        raise HTTPError(
            f"返回非 JSON: {text[:200]}".strip(),
            status=status,
            body=text,
        ) from e
