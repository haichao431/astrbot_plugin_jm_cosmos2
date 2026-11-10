"""
JM-Cosmos II 核心模块

提供下载、浏览、认证、配置、打包、配额等核心功能。
"""

from .auth import JMAuthManager
from .base import JMClientMixin, JMConfigManager
from .browser import JMBrowser
from .cloud_login import QRLogin115, QRLoginError, QRLoginQuark, render_qr_png
from .cloud_upload import CloudFileInfo, CloudUploader, CloudUploadResult
from .downloader import DownloadResult, JMDownloadManager
from .errors import classify_exception
from .jmcomic_loader import is_jmcomic_available
from .packer import JMPacker
from .quota import DownloadQuotaManager
from .subscribe import SubscriptionManager

# 集中管理 jmcomic 库的可用性检查
JMCOMIC_AVAILABLE = is_jmcomic_available()

__all__ = [
    "JMCOMIC_AVAILABLE",
    "CloudUploader",
    "CloudUploadResult",
    "CloudFileInfo",
    "QRLogin115",
    "QRLoginQuark",
    "QRLoginError",
    "render_qr_png",
    "DownloadQuotaManager",
    "JMAuthManager",
    "JMBrowser",
    "JMClientMixin",
    "JMConfigManager",
    "JMDownloadManager",
    "DownloadResult",
    "JMPacker",
    "SubscriptionManager",
    "classify_exception",
]
