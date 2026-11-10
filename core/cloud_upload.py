"""
云盘上传模块

支持把打包好的漫画文件上传到：
- 115 网盘（基于 p115client，同目录 _vendor 内置）
- 夸克网盘（基于官方 Web 接口，Cookie 认证）

V2 增强功能：
- 修复 _resolve_115_dir 返回错误类型的问题
- 新增 lookup_file_by_prefix: 按文件名前缀搜索 115 目录中的文件
- 新增 create_share_link: 对已上传文件创建分享链接
- 新增 delete_file: 删除 115 中的文件
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import mimetypes
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_VENDOR_DIR = Path(__file__).resolve().parent.parent / "_vendor"


@dataclass
class CloudUploadResult:
    """云盘上传结果"""

    success: bool
    provider: str
    remote_path: str = ""
    message: str = ""
    pickcode: str = ""
    share_url: str = ""
    file_id: str = ""
    passcode: str = ""


@dataclass
class CloudFileInfo:
    """网盘文件信息"""

    file_id: str
    name: str
    size: int
    pickcode: str = ""
    sha1: str = ""


class CloudUploadError(Exception):
    """云盘上传异常"""


def _ensure_vendor_path() -> None:
    """把插件内置的 _vendor 目录加入 sys.path（p115client 等）"""
    vendor = str(_VENDOR_DIR)
    if _VENDOR_DIR.is_dir() and vendor not in sys.path:
        sys.path.insert(0, vendor)


class CloudUploader:
    """云盘上传器（115 / 夸克）"""

    PROVIDERS = ("none", "115", "quark")

    def __init__(
        self,
        provider: str = "none",
        cookie: str = "",
        remote_dir: str = "",
        timeout: int = 300,
    ):
        self.provider = (provider or "none").strip().lower()
        self.cookie = (cookie or "").strip()
        self.remote_dir = (remote_dir or "").strip()
        self.timeout = max(30, int(timeout or 300))
        self._client_115 = None

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------
    def is_enabled(self) -> bool:
        return self.provider in ("115", "quark") and bool(self.cookie)

    def _get_115_client(self):
        """获取（并缓存）115 客户端"""
        if self._client_115 is not None:
            return self._client_115
        _ensure_vendor_path()
        try:
            from p115client import P115Client  # type: ignore
        except Exception as e:
            raise CloudUploadError(f"115 依赖缺失: {e}") from e
        try:
            self._client_115 = P115Client(self.cookie)
        except Exception as e:
            raise CloudUploadError(f"115 Cookie 无效: {e}") from e
        return self._client_115

    def upload(self, file_path: Path) -> CloudUploadResult:
        """同步上传一个文件（调用方应放到线程池执行）"""
        if self.provider == "none":
            return CloudUploadResult(False, self.provider, message="未启用云盘上传")
        if not self.cookie:
            return CloudUploadResult(False, self.provider, message="未配置网盘 Cookie")
        path = Path(file_path)
        if not path.is_file():
            return CloudUploadResult(False, self.provider, message=f"文件不存在: {path}")
        try:
            if self.provider == "115":
                return self._upload_115(path)
            if self.provider == "quark":
                return self._upload_quark(path)
        except CloudUploadError as e:
            return CloudUploadResult(False, self.provider, message=str(e))
        except Exception as e:
            return CloudUploadResult(False, self.provider, message=f"上传异常: {type(e).__name__}: {e}")
        return CloudUploadResult(False, self.provider, message=f"未知网盘类型: {self.provider}")

    async def upload_async(self, file_path: Path) -> CloudUploadResult:
        """异步封装"""
        return await asyncio.to_thread(self.upload, file_path)

    # ------------------------------------------------------------------
    # 115 目录 & 文件查找 / 分享 / 删除
    # ------------------------------------------------------------------
    def resolve_115_dir(self) -> int:
        """解析配置的目录路径为 115 目录 ID（失败返回 0 即根目录）"""
        return self._resolve_115_dir(self._get_115_client())

    def lookup_file_by_prefix(self, prefix: str) -> CloudFileInfo | None:
        """
        在 115 配置目录中按文件名前缀搜索文件。
        匹配规则：文件名以 prefix 开头，或匹配 prefix_timestamp 格式。
        返回第一个匹配的文件信息，未找到返回 None。
        """
        client = self._get_115_client()
        cid = self._resolve_115_dir(client)
        if cid == 0:
            return None
        try:
            resp = client.fs_files(cid)
        except Exception:
            return None
        if not isinstance(resp, dict):
            return None
        files = resp.get("data") or resp.get("files") or []
        if isinstance(files, dict):
            files = files.get("list", [])
        if not isinstance(files, list):
            return None
        pattern = re.compile(rf"^{re.escape(prefix)}(_\d+)?\.zip$")
        for f in files:
            fname = f.get("n") or f.get("name") or ""
            if pattern.match(fname):
                return CloudFileInfo(
                    file_id=str(f.get("fid") or f.get("id") or f.get("aid", "")),
                    name=fname,
                    size=int(f.get("s") or f.get("size", 0)),
                    pickcode=f.get("pc") or f.get("pickcode") or "",
                    sha1=f.get("sha") or f.get("sha1") or "",
                )
        return None

    async def lookup_file_by_prefix_async(self, prefix: str) -> CloudFileInfo | None:
        """异步版 lookup_file_by_prefix"""
        return await asyncio.to_thread(self.lookup_file_by_prefix, prefix)

    def create_share_link(
        self,
        file_id: str,
        name: str = "",
        passcode: str = "",
        expire_days: int = 0,
        pickcode: str = "",
    ) -> tuple[str, str]:
        """
        为已上传的文件创建分享链接。

        Args:
            file_id: 网盘文件 ID
            name: 文件名（用作分享标题）
            passcode: 提取码，空字符串表示不要提取码
            expire_days: 有效期天数，0 表示永久
            pickcode: 115 的文件提取码，file_id 方式失败时作为兜底

        Returns:
            (分享链接, 实际提取码)
        """
        if self.provider == "115":
            return self._share_115(file_id, name, passcode, expire_days, pickcode)
        if self.provider == "quark":
            return self._share_quark(file_id, name, passcode, expire_days)
        raise CloudUploadError(f"未知网盘类型: {self.provider}")

    async def create_share_link_async(
        self,
        file_id: str,
        name: str = "",
        passcode: str = "",
        expire_days: int = 0,
        pickcode: str = "",
    ) -> tuple[str, str]:
        return await asyncio.to_thread(
            self.create_share_link, file_id, name, passcode, expire_days, pickcode
        )

    # ------------------------- 115 分享 -------------------------
    def _share_115(
        self,
        file_id: str,
        name: str,
        passcode: str,
        expire_days: int,
        pickcode: str = "",
    ) -> tuple[str, str]:
        client = self._get_115_client()

        def _send(ids: str) -> dict:
            payload: dict[str, Any] = {
                "file_ids": ids,
                "share_title": name or "share",
                # 115 的 share_duration 单位是天，0 表示长期
                "share_duration": 0 if expire_days <= 0 else int(expire_days),
            }
            if passcode:
                payload["password"] = passcode
            return client.share_send(payload)

        # 115 不同接口版本对 file_ids 的期望不一致（文件 id 或 pickcode），
        # 先按文件 id 试，失败且拿到 pickcode 时再重试一次。
        candidates = [str(file_id)]
        if pickcode and str(pickcode) != str(file_id):
            candidates.append(str(pickcode))

        last_resp: dict = {}
        code = ""
        for ids in candidates:
            if not ids:
                continue
            try:
                resp = _send(ids)
            except Exception as e:  # noqa: BLE001
                last_resp = {"error": str(e)}
                continue
            if not isinstance(resp, dict):
                last_resp = {"error": f"返回异常: {resp}"}
                continue
            last_resp = resp
            if resp.get("state") is False:
                continue
            data = resp.get("data") or {}
            code = (
                data.get("share_code")
                or resp.get("share_code")
                or data.get("share_id")
                or ""
            )
            if code:
                break

        if not code:
            # 接口偶尔不直接返回分享码，退化为取最近一条分享
            code = self._115_latest_share_code()
        if not code:
            raise CloudUploadError(
                "115 无法获取分享码: "
                f"{last_resp.get('error') or last_resp.get('message') or last_resp}"
            )

        url = f"https://115.com/s/{code}"
        if passcode:
            url += f"?password={passcode}"
        return url, passcode

    def _115_latest_share_code(self) -> str:
        """获取最近一条 115 分享的 share_code（兜底）"""
        try:
            client = self._get_115_client()
            resp = client.share_list(0)
        except Exception:
            return ""
        data = (resp or {}).get("data") or {}
        items = data.get("list") if isinstance(data, dict) else None
        if not items:
            return ""
        first = items[0] or {}
        return str(first.get("share_code") or first.get("share_id") or "")

    # ------------------------- 夸克分享 -------------------------
    def _share_quark(
        self, file_id: str, name: str, passcode: str, expire_days: int
    ) -> tuple[str, str]:
        payload: dict[str, Any] = {
            "fid_list": [str(file_id)],
            "title": name or "share",
            "url_type": 2 if passcode else 1,
            "expired_type": 1 if expire_days <= 0 else 2,
        }
        if passcode:
            payload["passcode"] = passcode
        if expire_days > 0:
            payload["expired_at"] = int(
                (time.time() + expire_days * 86400) * 1000
            )

        data = self._quark_request("POST", "share", json_body=payload, base="share")
        task_id = (data or {}).get("task_id") or ""
        share_id = str((data or {}).get("share_id") or "")

        if task_id and not share_id:
            for index in range(20):
                task = self._quark_request(
                    "GET",
                    "task",
                    params={"task_id": task_id, "retry_index": index},
                )
                status = (task or {}).get("status")
                if status == 2:
                    share_id = str((task or {}).get("share_id") or "")
                    break
                if status == 3:
                    raise CloudUploadError(
                        f"夸克分享任务失败: {(task or {}).get('message') or task}"
                    )
                time.sleep(0.6)

        if not share_id:
            raise CloudUploadError("夸克分享创建超时，未获得 share_id")

        info = self._quark_request(
            "POST", "share/password", json_body={"share_id": share_id}, base="share"
        )
        url = (info or {}).get("share_url") or f"https://pan.quark.cn/s/{share_id}"
        real_passcode = (info or {}).get("passcode") or passcode
        if real_passcode and real_passcode not in url:
            url += f"?pwd={real_passcode}"
        return url, real_passcode

    def delete_file(self, file_id: str) -> bool:
        """删除 115 中的文件"""
        client = self._get_115_client()
        try:
            resp = client.fs_delete(file_id)
            if isinstance(resp, dict) and resp.get("state") is False:
                raise CloudUploadError(f"删除文件失败: {resp}")
            return True
        except CloudUploadError:
            raise
        except Exception as e:
            raise CloudUploadError(f"删除文件异常: {e}") from e

    # ------------------------------------------------------------------
    # 115 上传
    # ------------------------------------------------------------------
    def _upload_115(self, path: Path) -> CloudUploadResult:
        client = self._get_115_client()
        cid = self._resolve_115_dir(client)
        try:
            resp = client.upload_file(str(path), pid=cid, timeout=self.timeout)
        except Exception as e:
            raise CloudUploadError(f"115 上传失败: {e}") from e

        item = resp[0] if isinstance(resp, list) and resp else resp
        if not isinstance(item, dict):
            raise CloudUploadError(f"115 上传返回异常: {resp}")
        if item.get("state") is False:
            raise CloudUploadError(f"115 上传被拒绝: {item}")

        data = item.get("data") or {}
        pickcode = data.get("pickcode") or data.get("pick_code") or ""
        file_id = str(
            data.get("file_id")
            or data.get("fid")
            or data.get("id")
            or data.get("aid")
            or ""
        )

        # 上传接口不一定回传文件 ID，目录里按文件名兜底查一次
        if not file_id or file_id == "0":
            found = self._115_find_file(cid, path.name)
            if found:
                file_id = found.get("fid", "") or ""
                pickcode = pickcode or found.get("pickcode", "") or ""

        remote_path = f"{self.remote_dir or '/'}/{path.name}".replace("//", "/")
        return CloudUploadResult(
            True,
            "115",
            remote_path=remote_path,
            message=f"pickcode={pickcode}" if pickcode else "",
            pickcode=pickcode,
            file_id=file_id,
        )

    def _115_find_file(self, cid: int, name: str) -> dict:
        """在 115 指定目录中按文件名查找，返回 fid/pickcode"""
        try:
            client = self._get_115_client()
            resp = client.fs_files(cid)
        except Exception:
            return {}
        if not isinstance(resp, dict):
            return {}
        files = resp.get("data") or resp.get("files") or []
        if isinstance(files, dict):
            files = files.get("list", [])
        if not isinstance(files, list):
            return {}
        for f in files:
            fname = f.get("n") or f.get("name") or ""
            if fname == name:
                return {
                    "fid": str(f.get("fid") or f.get("id") or ""),
                    "pickcode": f.get("pc") or f.get("pickcode") or "",
                }
        return {}

    def _resolve_115_dir(self, client: Any) -> int:
        """
        把配置里的目录路径解析为 115 的目录 ID，失败则回退根目录 0。
        fs_dir_getid 返回的 id 是字符串格式，需转为 int。
        """
        target = (self.remote_dir or "").strip()
        if not target or target == "/":
            return 0
        if target.isdigit():
            return int(target)
        try:
            resp = client.fs_dir_getid(target)
            if isinstance(resp, dict):
                dir_id = resp.get("id") or 0
                return int(dir_id)
        except Exception:
            pass
        # fallback: 尝试创建目录（可能已存在）
        try:
            resp = client.fs_mkdir(target)
            if isinstance(resp, dict):
                cid = resp.get("cid") or resp.get("file_id") or ""
                if cid:
                    return int(cid)
        except Exception:
            pass
        return 0

    # ------------------------------------------------------------------
    # 夸克网盘（保留原实现，略压缩）
    # ------------------------------------------------------------------
    def _upload_quark(self, path: Path) -> CloudUploadResult:
        try:
            import httpx
        except Exception as e:
            raise CloudUploadError(f"夸克上传依赖缺失: {e}") from e
        self._httpx = httpx
        file_size = path.stat().st_size
        file_name = path.name
        mime_type, _ = mimetypes.guess_type(str(path))
        if not mime_type:
            mime_type = "application/octet-stream"
        md5_hash, sha1_hash = self._quark_hashes(path)
        parent_fid = self._quark_resolve_dir()
        pre = self._quark_pre_upload(file_name, file_size, parent_fid, mime_type)
        task_id = pre.get("task_id") or ""
        if not task_id:
            raise CloudUploadError(f"夸克预上传失败: {pre}")
        auth_info = pre.get("auth_info", "") or ""
        upload_id = pre.get("upload_id", "") or ""
        obj_key = pre.get("obj_key", "") or ""
        bucket = pre.get("bucket", "ul-zb") or "ul-zb"
        callback_info = pre.get("callback", {}) or {}
        self._quark_update_hash(task_id, md5_hash, sha1_hash)
        if file_size < 5 * 1024 * 1024:
            self._quark_upload_single(path, task_id, auth_info, upload_id, obj_key, bucket, callback_info, mime_type)
        else:
            self._quark_upload_multipart(path, task_id, auth_info, upload_id, obj_key, bucket, callback_info, mime_type)
        finish_data = self._quark_finish(task_id, obj_key)

        file_id = str((finish_data or {}).get("fid") or "")
        if not file_id:
            file_id = self._quark_find_fid(parent_fid, file_name)

        remote_path = f"{self.remote_dir or '/'}/{file_name}".replace("//", "/")
        return CloudUploadResult(True, "quark", remote_path=remote_path, file_id=file_id)

    def _quark_find_fid(self, parent_fid: str, name: str) -> str:
        """上传完成后在目标目录里按文件名查 fid"""
        try:
            data = self._quark_request(
                "GET",
                "file/sort",
                params={
                    "pdir_fid": parent_fid,
                    "_page": 1,
                    "_size": 200,
                    "_sort": "file_name:asc",
                    "_fetch_total": 1,
                    "_fetch_sub_dirs": 0,
                    "fetch_all_file": 1,
                },
            )
        except Exception:
            return ""
        for item in (data or {}).get("list") or []:
            if not isinstance(item, dict) or item.get("dir"):
                continue
            if item.get("file_name") == name:
                return str(item.get("fid") or "")
        return ""

    def _quark_headers(self) -> dict:
        return {
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
            "origin": "https://pan.quark.cn",
            "referer": "https://pan.quark.cn/",
            "accept": "application/json, text/plain, */*",
            "accept-language": "zh-CN,zh;q=0.9",
            "content-type": "application/json",
            "cookie": self.cookie,
        }

    def _quark_post(self, endpoint: str, payload: dict) -> dict:
        return self._quark_request("POST", endpoint, json_body=payload)

    def _quark_request(
        self,
        method: str,
        endpoint: str,
        json_body: dict | None = None,
        params: dict | None = None,
        base: str = "pc",
    ) -> dict:
        """夸克 API 请求，返回响应中的 data 字段"""
        root = (
            "https://drive.quark.cn/1/clouddrive"
            if base == "share"
            else "https://drive-pc.quark.cn/1/clouddrive"
        )
        url = f"{root}/{endpoint.lstrip('/')}"
        query = {"pr": "ucpro", "fr": "pc", "uc_param_str": ""}
        if params:
            query.update(params)

        with self._httpx.Client(timeout=self.timeout) as client:
            if method.upper() == "GET":
                resp = client.get(url, params=query, headers=self._quark_headers())
            else:
                resp = client.post(
                    url, json=json_body or {}, params=query, headers=self._quark_headers()
                )

        if resp.status_code >= 400:
            raise CloudUploadError(
                f"夸克接口 {endpoint} HTTP {resp.status_code}: {resp.text[:200]}"
            )
        try:
            data = resp.json()
        except Exception as e:
            raise CloudUploadError(f"夸克接口 {endpoint} 返回非 JSON: {e}") from e
        if data.get("code") not in (0, None) and not data.get("status"):
            raise CloudUploadError(
                f"夸克接口 {endpoint} 失败: {data.get('message') or data}"
            )
        return data.get("data") or {}

    @staticmethod
    def _quark_hashes(path: Path) -> tuple[str, str]:
        md5_hash = hashlib.md5()
        sha1_hash = hashlib.sha1()
        with open(path, "rb") as f:
            while chunk := f.read(1024 * 1024):
                md5_hash.update(chunk)
                sha1_hash.update(chunk)
        return md5_hash.hexdigest(), sha1_hash.hexdigest()

    def _quark_resolve_dir(self) -> str:
        target = (self.remote_dir or "").strip()
        if not target or target == "/":
            return "0"
        if target.isdigit():
            return target
        current = "0"
        prefix = ""
        for part in [p for p in target.split("/") if p]:
            prefix = f"{prefix}/{part}"
            current = self._quark_ensure_folder(current, part, prefix)
        return current

    def _quark_ensure_folder(self, parent_fid: str, name: str, full_path: str) -> str:
        try:
            data = self._quark_post("file/info/path_list", {"file_path": [full_path], "namespace": "0"})
            for item in data or []:
                if isinstance(item, dict) and item.get("fid"):
                    return str(item["fid"])
        except Exception:
            pass
        try:
            data = self._quark_post("file", {"pdir_fid": parent_fid, "file_name": name, "dir_init_lock": False})
            fid = (data or {}).get("fid")
            if fid:
                return str(fid)
        except Exception as e:
            raise CloudUploadError(f"夸克创建目录 {name} 失败: {e}") from e
        return parent_fid

    def _quark_pre_upload(self, file_name: str, file_size: int, parent_fid: str, mime_type: str) -> dict:
        now = int(time.time() * 1000)
        payload = {
            "ccp_hash_update": True, "parallel_upload": True, "pdir_fid": parent_fid,
            "dir_name": "", "size": file_size, "file_name": file_name,
            "format_type": mime_type, "l_updated_at": now, "l_created_at": now,
        }
        return self._quark_post("file/upload/pre", payload)

    def _quark_update_hash(self, task_id: str, md5_hash: str, sha1_hash: str) -> None:
        self._quark_post("file/update/hash", {"task_id": task_id, "md5": md5_hash, "sha1": sha1_hash})

    def _quark_auth_put(self, task_id, mime_type, part_number, auth_info, upload_id, obj_key, bucket, hash_ctx=""):
        oss_date = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
        ua = "aliyun-sdk-js/1.0.0 Chrome Mobile 139.0.0.0 on Google Nexus 5 (Android 6.0)"
        meta = f"PUT\n\n{mime_type}\n{oss_date}\nx-oss-date:{oss_date}"
        if hash_ctx:
            meta += f"\nx-oss-hash-ctx:{hash_ctx}"
        meta += f"\nx-oss-user-agent:{ua}\n/{bucket}/{obj_key}?partNumber={part_number}&uploadId={upload_id}"
        data = self._quark_post("file/upload/auth", {"task_id": task_id, "auth_info": auth_info, "auth_meta": meta})
        ak = data.get("auth_key", "") or ""
        url = f"https://{bucket}.pds.quark.cn/{obj_key}?partNumber={part_number}&uploadId={upload_id}"
        hdrs = {"Content-Type": mime_type, "x-oss-date": oss_date, "x-oss-user-agent": ua}
        if ak:
            hdrs["authorization"] = ak
        if hash_ctx:
            hdrs["X-Oss-Hash-Ctx"] = hash_ctx
        return {"upload_url": url, "headers": hdrs}

    def _quark_auth_complete(self, task_id, auth_info, upload_id, obj_key, bucket, xml_data, callback_info):
        oss_date = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
        xml_md5 = base64.b64encode(hashlib.md5(xml_data.encode()).digest()).decode()
        cb64 = base64.b64encode(json.dumps(callback_info, separators=(",", ":")).encode()).decode()
        ua = "aliyun-sdk-js/1.0.0 Chrome 139.0.0.0 on OS X 10.15.7 64-bit"
        meta = f"POST\n{xml_md5}\napplication/xml\n{oss_date}\nx-oss-callback:{cb64}\nx-oss-date:{oss_date}\nx-oss-user-agent:{ua}\n/{bucket}/{obj_key}?uploadId={upload_id}"
        data = self._quark_post("file/upload/auth", {"task_id": task_id, "auth_meta": meta, "auth_info": auth_info})
        ak = data.get("auth_key", "") or ""
        url = f"https://{bucket}.pds.quark.cn/{obj_key}?uploadId={upload_id}"
        hdrs = {"Content-Type": "application/xml", "x-oss-date": oss_date, "x-oss-user-agent": ua, "x-oss-callback": cb64, "Content-MD5": xml_md5}
        if ak:
            hdrs["authorization"] = ak
        return {"upload_url": url, "headers": hdrs}

    def _quark_put_part(self, upload_url, headers, data):
        with self._httpx.Client(timeout=self.timeout) as client:
            resp = client.put(upload_url, content=data, headers=headers)
        if resp.status_code != 200:
            raise CloudUploadError(f"夸克 OSS 上传失败 HTTP {resp.status_code}: {resp.text[:200]}")
        etag = (resp.headers.get("etag") or "").strip('"')
        if not etag:
            raise CloudUploadError("夸克 OSS 上传成功但未返回 ETag")
        return etag

    def _quark_upload_single(self, path, task_id, auth_info, upload_id, obj_key, bucket, callback_info, mime_type):
        auth = self._quark_auth_put(task_id, mime_type, 1, auth_info, upload_id, obj_key, bucket)
        buffer = path.read_bytes()
        etag = self._quark_put_part(auth["upload_url"], auth["headers"], buffer)
        xml = '<?xml version="1.0" encoding="UTF-8"?>\n<CompleteMultipartUpload>\n<Part>\n<PartNumber>1</PartNumber>\n<ETag>"' + etag + '"</ETag>\n</Part>\n</CompleteMultipartUpload>'
        pa = self._quark_auth_complete(task_id, auth_info, upload_id, obj_key, bucket, xml, callback_info)
        with self._httpx.Client(timeout=self.timeout) as client:
            resp = client.post(pa["upload_url"], content=xml, headers=pa["headers"])
        if resp.status_code not in (200, 203):
            raise CloudUploadError(f"夸克 OSS 合并失败 HTTP {resp.status_code}: {resp.text[:200]}")

    def _quark_upload_multipart(self, path, task_id, auth_info, upload_id, obj_key, bucket, callback_info, mime_type):
        file_size = path.stat().st_size
        cs = 4 * 1024 * 1024
        pc = (file_size + cs - 1) // cs
        uploaded = []
        with open(path, "rb") as f:
            for idx in range(pc):
                pn = idx + 1
                f.seek(idx * cs)
                d = f.read(cs)
                hc = ""
                if idx > 0:
                    hc = self._quark_incremental_hash_ctx(path, idx * cs)
                auth = self._quark_auth_put(task_id, mime_type, pn, auth_info, upload_id, obj_key, bucket, hc)
                etag = self._quark_put_part(auth["upload_url"], auth["headers"], d)
                uploaded.append((pn, etag))
        xml_parts = "\n".join(f"<Part>\n<PartNumber>{n}</PartNumber>\n<ETag>\"{e}\"</ETag>\n</Part>" for n, e in uploaded)
        xml = f'<?xml version="1.0" encoding="UTF-8"?>\n<CompleteMultipartUpload>\n{xml_parts}\n</CompleteMultipartUpload>'
        pa = self._quark_auth_complete(task_id, auth_info, upload_id, obj_key, bucket, xml, callback_info)
        with self._httpx.Client(timeout=self.timeout) as client:
            resp = client.post(pa["upload_url"], content=xml, headers=pa["headers"])
        if resp.status_code not in (200, 203):
            raise CloudUploadError(f"夸克 OSS 合并失败 HTTP {resp.status_code}: {resp.text[:200]}")

    @staticmethod
    def _quark_incremental_hash_ctx(path, processed_bytes):
        import struct
        with open(path, "rb") as f:
            prev = f.read(processed_bytes)
        h0, h1, h2, h3, h4 = 0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0
        fb = len(prev) - (len(prev) % 64)
        for i in range(0, fb, 64):
            block = prev[i:i+64]
            w = [struct.unpack(">I", block[j:j+4])[0] for j in range(0, 64, 4)]
            for t in range(16, 80):
                w.append(((w[t-3] ^ w[t-8] ^ w[t-14] ^ w[t-16]) << 1 | (w[t-3] ^ w[t-8] ^ w[t-14] ^ w[t-16]) >> 31) & 0xFFFFFFFF)
            a, b, c, d, ev = h0, h1, h2, h3, h4
            for t in range(80):
                if t < 20:
                    fv = (b & c) | ((~b) & d)
                    k = 0x5A827999
                elif t < 40:
                    fv = b ^ c ^ d
                    k = 0x6ED9EBA1
                elif t < 60:
                    fv = (b & c) | (b & d) | (c & d)
                    k = 0x8F1BBCDC
                else:
                    fv = b ^ c ^ d
                    k = 0xCA62C1D6
                temp = (((a << 5) | (a >> 27)) + fv + ev + k + w[t]) & 0xFFFFFFFF
                ev, d, c, b, a = d, c, ((b << 30) | (b >> 2)) & 0xFFFFFFFF, a, temp
            h0 = (h0 + a) & 0xFFFFFFFF
            h1 = (h1 + b) & 0xFFFFFFFF
            h2 = (h2 + c) & 0xFFFFFFFF
            h3 = (h3 + d) & 0xFFFFFFFF
            h4 = (h4 + ev) & 0xFFFFFFFF
        ctx = {"hash_type": "sha1", "h0": str(h0), "h1": str(h1), "h2": str(h2), "h3": str(h3), "h4": str(h4), "Nl": str(processed_bytes * 8), "Nh": "0", "data": "", "num": "0"}
        return base64.b64encode(json.dumps(ctx, separators=(",", ":")).encode()).decode()

    def _quark_finish(self, task_id, obj_key):
        payload = {"task_id": task_id}
        if obj_key:
            payload["obj_key"] = obj_key
        return self._quark_post("file/upload/finish", payload)
