"""
网盘扫码登录模块

支持：
- 115 网盘：基于 p115client 的二维码登录（_vendor 内置）
- 夸克网盘：基于官方 UOP 扫码接口

设计说明：
- 所有方法都是同步阻塞的，调用方需要放到线程池（asyncio.to_thread）执行
- start() 返回二维码内容字符串，由调用方渲染成图片发到群里
- poll() 返回状态字符串，调用方按状态推进
- finish() 返回 Cookie 字符串
"""

from __future__ import annotations

import uuid
from pathlib import Path
from urllib.parse import urlencode

_VENDOR_DIR = Path(__file__).resolve().parent.parent / "_vendor"


class QRLoginError(Exception):
    """扫码登录异常"""


def _ensure_vendor_path() -> None:
    """把插件内置的 _vendor 目录加入 sys.path"""
    import sys

    vendor = str(_VENDOR_DIR)
    if _VENDOR_DIR.is_dir() and vendor not in sys.path:
        sys.path.insert(0, vendor)


def render_qr_png(content: str, out_path: Path) -> Path:
    """把二维码内容渲染成 PNG 图片"""
    try:
        import qrcode  # type: ignore
    except Exception as e:  # noqa: BLE001
        raise QRLoginError(f"缺少 qrcode 依赖，无法生成二维码: {e}") from e

    img = qrcode.make(content)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(out_path))
    return out_path


class QRLogin115:
    """115 网盘扫码登录"""

    # 状态映射
    _STATUS = {
        0: "waiting",
        1: "scanned",
        2: "ok",
        -1: "expired",
        -2: "canceled",
    }

    def __init__(self, app: str = "web", timeout: int = 180):
        _ensure_vendor_path()
        try:
            from p115client import P115Client  # type: ignore
        except Exception as e:  # noqa: BLE001
            raise QRLoginError(f"115 依赖缺失（p115client）: {e}") from e
        self._cls = P115Client
        self.app = app or "web"
        self.timeout = timeout
        self._token: dict | None = None
        self._uid: str = ""

    def start(self) -> str:
        """获取二维码，返回二维码内容字符串"""
        try:
            resp = self._cls.login_qrcode_token()
        except Exception as e:  # noqa: BLE001
            raise QRLoginError(f"获取 115 二维码失败: {e}") from e

        data = (resp or {}).get("data") or {}
        uid = str(data.get("uid") or "")
        if not uid:
            raise QRLoginError(f"115 二维码接口返回异常: {resp}")

        qr_content = data.pop("qrcode", "") or f"https://115.com/scan/dg-{uid}"
        self._token = data
        self._uid = uid
        return qr_content

    def poll(self) -> str:
        """查询扫码状态：waiting / scanned / ok / expired / canceled"""
        if not self._token:
            raise QRLoginError("尚未调用 start()")
        try:
            resp = self._cls.login_qrcode_scan_status(self._token)
        except Exception:
            # 轮询期间的网络抖动视为等待，避免误报失败
            return "waiting"
        status = ((resp or {}).get("data") or {}).get("status")
        return self._STATUS.get(status, "waiting")

    def finish(self) -> str:
        """扫码确认后换取 Cookie 字符串"""
        try:
            resp = self._cls.login_qrcode_scan_result(self._uid, app=self.app)
        except Exception as e:  # noqa: BLE001
            raise QRLoginError(f"115 获取登录凭证失败: {e}") from e

        data = (resp or {}).get("data") or {}
        cookie = data.get("cookie")

        if isinstance(cookie, dict) and cookie:
            return "; ".join(f"{k}={v}" for k, v in cookie.items())
        if isinstance(cookie, str) and cookie:
            return cookie

        # 有些版本直接平铺在 data 里
        flat = {k: v for k, v in data.items() if isinstance(v, str) and k.isupper()}
        if flat:
            return "; ".join(f"{k}={v}" for k, v in flat.items())

        raise QRLoginError(f"115 登录结果解析失败: {resp}")


class QRLoginQuark:
    """夸克网盘扫码登录"""

    TOKEN_API = "https://uop.quark.cn/cas/ajax/getTokenForQrcodeLogin"
    TICKET_API = "https://uop.quark.cn/cas/ajax/getServiceTicketByQrcodeToken"
    USER_API = "https://pan.quark.cn/account/info"
    QR_BASE = "https://su.quark.cn/4_eMHBJ"
    CLIENT_ID = "532"

    # 等待扫码 / 失败状态码
    _WAIT_CODES = {50004001}
    _FAIL_CODES = {50004002, 50004003, 50004004}

    def __init__(self, timeout: int = 180):
        try:
            import httpx  # type: ignore
        except Exception as e:  # noqa: BLE001
            raise QRLoginError(f"夸克登录依赖缺失（httpx）: {e}") from e

        self._httpx = httpx
        self.timeout = timeout
        self._token: str = ""
        self._service_ticket: str = ""
        self._client = httpx.Client(
            timeout=20.0,
            follow_redirects=True,
            headers={
                "user-agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
                ),
                "accept": "application/json, text/plain, */*",
                "accept-language": "zh-CN,zh;q=0.9",
                "origin": "https://pan.quark.cn",
                "referer": "https://pan.quark.cn/",
            },
        )

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass

    def start(self) -> str:
        """获取二维码 token，返回二维码内容字符串"""
        request_id = str(uuid.uuid4())
        try:
            resp = self._client.get(
                self.TOKEN_API,
                params={
                    "client_id": self.CLIENT_ID,
                    "v": "1.2",
                    "request_id": request_id,
                },
            )
            payload = resp.json()
        except Exception as e:  # noqa: BLE001
            raise QRLoginError(f"获取夸克二维码失败: {e}") from e

        token = (
            ((payload.get("data") or {}).get("members") or {}).get("token") or ""
        )
        if payload.get("status") != 2000000 or not token:
            raise QRLoginError(
                f"夸克二维码接口返回异常: {payload.get('message') or payload}"
            )

        self._token = token
        query = urlencode(
            {
                "token": token,
                "client_id": self.CLIENT_ID,
                "ssb": "weblogin",
                "uc_param_str": "",
                "uc_biz_str": (
                    "S:custom|OPT:SAREA@0|OPT:IMMERSIVE@1|OPT:BACK_BTN_STYLE@0"
                ),
            }
        )
        return f"{self.QR_BASE}?{query}"

    def poll(self) -> str:
        """查询扫码状态：waiting / ok / expired / error"""
        if not self._token:
            raise QRLoginError("尚未调用 start()")

        try:
            resp = self._client.get(
                self.TICKET_API,
                params={
                    "client_id": self.CLIENT_ID,
                    "v": "1.2",
                    "token": self._token,
                    "request_id": str(uuid.uuid4()),
                },
            )
            payload = resp.json()
        except Exception:
            return "waiting"

        status = payload.get("status")
        if status == 2000000:
            ticket = (
                ((payload.get("data") or {}).get("members") or {}).get(
                    "service_ticket"
                )
                or ""
            )
            if ticket:
                self._service_ticket = ticket
                return "ok"
            return "waiting"
        if status in self._FAIL_CODES:
            return "expired"
        return "waiting"

    def finish(self) -> str:
        """用 service ticket 换取 Cookie 字符串"""
        if not self._service_ticket:
            raise QRLoginError("尚未完成扫码确认")

        try:
            self._client.get(
                self.USER_API,
                params={"st": self._service_ticket, "lw": "scan"},
            )
        except Exception as e:  # noqa: BLE001
            raise QRLoginError(f"夸克获取账号信息失败: {e}") from e

        cookies: list[str] = []
        for c in self._client.cookies.jar:
            domain = getattr(c, "domain", "") or ""
            if "quark.cn" in domain:
                cookies.append(f"{c.name}={c.value}")

        if not cookies:
            raise QRLoginError("夸克登录成功但未拿到 Cookie，请重试")

        return "; ".join(cookies)

    def verify(self, cookie: str) -> str:
        """验证 Cookie 是否可用，返回昵称（失败抛异常）"""
        try:
            resp = self._httpx.get(
                "https://pan.quark.cn/1/clouddrive/member",
                params={"pr": "ucpro", "fr": "pc"},
                headers={
                    "cookie": cookie,
                    "user-agent": "Mozilla/5.0",
                    "accept": "application/json",
                },
                timeout=20.0,
            )
            data = (resp.json() or {}).get("data") or {}
        except Exception as e:  # noqa: BLE001
            raise QRLoginError(f"夸克 Cookie 验证失败: {e}") from e

        if not data:
            raise QRLoginError("夸克 Cookie 无效或已过期")
        return data.get("nickname") or data.get("member_type") or "已登录"
