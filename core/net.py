"""统一的 HTTP 客户端构造与重试。

为什么需要这个模块：

1. 这台服务器（新加坡 Oracle）只分配了 IPv4，容器里连全局 IPv6 地址都没有，
   只有 ``::1``；但夸克/阿里的域名会返回 AAAA 记录。
   一旦客户端先尝试 IPv6，就会立刻 ``[Errno 101] Network is unreachable``，
   而且部分实现不会回退到 IPv4 —— 表现为"二维码生成失败"。
   这里统一用 ``local_address="0.0.0.0"`` 强制绑定 IPv4，从源头绕开。

2. 新加坡到阿里 CDN 的链路抖动很大（实测 0.7s ~ 8s，偶发握手超过 20s），
   单次请求超时是常态。``with_retry()`` 对网络类异常做有限次退避重试。
"""

from __future__ import annotations

import time
from typing import Any, Callable, TypeVar

T = TypeVar("T")

# 网络类异常的重试次数与退避基数（秒）
DEFAULT_ATTEMPTS = 3
DEFAULT_DELAY = 1.5


def build_client(
    httpx_mod: Any,
    timeout: float = 45.0,
    connect_timeout: float = 12.0,
    **kwargs: Any,
) -> Any:
    """构造一个强制走 IPv4 的 httpx.Client。

    ``connect_timeout`` 单独收紧到 12 秒：到阿里 CDN 的 TCP 连接本身很快，
    卡住的是 TLS 握手；用原来 20 秒甚至 45 秒的宽超时只会让用户干等，
    快速失败 + 重试反而更快拿到结果。
    ``timeout`` 是读取/写入的超时，上传大文件时需要放宽。
    """
    transport_kwargs = kwargs.pop("transport_kwargs", None) or {}
    transport_kwargs.setdefault("local_address", "0.0.0.0")
    transport = httpx_mod.HTTPTransport(**transport_kwargs)

    timeout_obj = httpx_mod.Timeout(timeout, connect=connect_timeout)
    kwargs.setdefault("follow_redirects", True)
    return httpx_mod.Client(transport=transport, timeout=timeout_obj, **kwargs)


def is_retryable(exc: BaseException) -> bool:
    """判断异常是否值得重试（只重试网络层的抖动，不重试业务错误）。"""
    try:
        import httpx  # type: ignore
    except Exception:  # pragma: no cover - httpx 是运行期依赖
        return False

    if isinstance(exc, httpx.TransportError):
        # ConnectError / ConnectTimeout / ReadTimeout / PoolTimeout ...
        return True
    return False


def with_retry(
    fn: Callable[[], T],
    attempts: int = DEFAULT_ATTEMPTS,
    delay: float = DEFAULT_DELAY,
    on_retry: Callable[[int, BaseException], None] | None = None,
) -> T:
    """执行 ``fn``，网络类异常时按 1x, 2x, ... 退避重试。"""
    last: BaseException | None = None
    for i in range(max(1, attempts)):
        try:
            return fn()
        except BaseException as exc:  # noqa: BLE001
            last = exc
            if i == attempts - 1 or not is_retryable(exc):
                raise
            if on_retry is not None:
                on_retry(i + 1, exc)
            time.sleep(delay * (i + 1))
    assert last is not None
    raise last
