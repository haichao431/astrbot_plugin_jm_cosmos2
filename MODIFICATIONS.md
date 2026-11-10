# 相对上游的改动

本仓库是 [GEMILUXVII/astrbot_plugin_jm_cosmos](https://github.com/GEMILUXVII/astrbot_plugin_jm_cosmos) 的修改版，
基于 **v2.7.6**。

原项目使用 **AGPL-3.0** 许可证，本修改版同样遵循 AGPL-3.0，完整许可证见 [LICENSE](LICENSE)。

---

## 功能变更总览

在原有的「下载完成后上传到单个网盘」基础上，改为：

1. **双网盘**（115、夸克）可**同时启用、并发上传**，也可只启用其中一个
2. 新增**「发送方式」开关**：`发送群文件` / `发送网盘链接`
3. 网盘链接模式下**只发链接不发文件**，链接**永久有效并带提取码**
4. 新增**扫码登录**命令，Cookie 自动写入插件配置

---

## 文件级改动

### 新增 `core/cloud_login.py`

网盘扫码登录模块，不依赖 AstrBot 运行时，可单独测试。

| 类 / 函数 | 作用 |
|---|---|
| `render_qr_png(content, out_path)` | 把二维码内容渲染成 PNG（png 依赖 `qrcode` + `Pillow`） |
| `QRLogin115(app="web", timeout=180)` | 115 扫码登录：`start()` / `poll()` / `finish()` |
| `QRLoginQuark(timeout=180)` | 夸克扫码登录：`start()` / `poll()` / `finish()` / `close()` |
| `QRLoginError` | 登录失败异常 |

- 115 走 `p115client` 的 `login_qrcode_token` → `login_qrcode_scan_status` → `login_qrcode_scan_result`，
  状态码 `0 等待 / 1 已扫 / 2 成功 / -1 过期 / -2 取消`
- 夸克走 `uop.quark.cn` 的 `getTokenForQrcodeLogin` → `getServiceTicketByQrcodeToken`，
  再用 service ticket 换 `pan.quark.cn` 的 Cookie

### 修改 `core/cloud_upload.py`

| 改动 | 说明 |
|---|---|
| `CloudUploadResult` 新增字段 | `file_id`、`passcode`，供后续建分享链接使用 |
| 新增 `_quark_request(method, endpoint, json_body, params, base)` | 统一夸克请求，`base` 可选 `pc`（`drive-pc.quark.cn`）/ `share`（`drive.quark.cn`） |
| 新增 `create_share_link(...)` / `create_share_link_async(...)` | 为已上传文件创建分享链接，返回 `(url, passcode)` |
| 新增 `_share_115(...)` | 115 分享：`share_duration=0` 表示长期；`file_ids` 先用文件 fid，失败自动改用 pickcode 重试 |
| 新增 `_115_latest_share_code()` | 接口偶尔不返回分享码时，退化为取最近一条分享 |
| 新增 `_share_quark(...)` | 夸克分享：`expired_type=1`（永久）、`url_type=2`（私密链接带提取码），轮询 `task` 直到 `status==2`，再调 `share/password` 拿 `share_url` |
| `_upload_115(...)` 改为返回 `file_id` | 不再在上传函数里自动分享，分享与上传解耦 |
| 新增 `_115_find_file(cid, name)` | 上传响应缺 `fid` 时，用文件名在目录里反查 |
| `_upload_quark(...)` 捕获 `fid` | `_quark_finish` 现在返回 data；失败时用 `_quark_find_fid(parent_fid, name)` 兜底 |

> 夸克上传的 OSS 分片校验（`X-Oss-Hash-Ctx` 增量 SHA1 状态）沿用了原实现。

### 修改 `core/base/config.py`

新增属性（均从 `self.plugin_config` 读取并做类型容错）：

| 属性 | 说明 |
|---|---|
| `cloud_send_mode` | 返回 `"link"` / `"file"`，兼容中文选项名与旧配置 |
| `cloud_115_enabled` / `cloud_115_cookie` / `cloud_115_dir` | 115 独立配置 |
| `cloud_quark_enabled` / `cloud_quark_cookie` / `cloud_quark_dir` | 夸克独立配置 |
| `cloud_share_passcode` | 分享提取码，空则由调用方随机生成 |

保留了旧的 `cloud_upload_provider` / `cloud_upload_cookie` / `cloud_upload_remote_dir` 作为**回退**，
老配置文件升级后仍能识别。

### 修改 `core/__init__.py`

导出 `QRLogin115`、`QRLoginQuark`、`QRLoginError`、`render_qr_png`。

### 修改 `_conf_schema.json`

移除旧的 6 个单网盘配置项，新增 10 个：

| 配置项 | 类型 | 默认 |
|---|---|---|
| `cloud_send_mode` | 选择器：`发送群文件` / `发送网盘链接` | `发送群文件` |
| `cloud_115_enabled` | bool | `false` |
| `cloud_115_cookie` | string | `""` |
| `cloud_115_dir` | string | `""` |
| `cloud_quark_enabled` | bool | `false` |
| `cloud_quark_cookie` | string | `""` |
| `cloud_quark_dir` | string | `""` |
| `cloud_share_passcode` | string | `""` |
| `cloud_upload_delete_local` | bool | `true` |
| `cloud_upload_timeout` | int | `300` |

### 修改 `main.py`

**发送流程重写**

- 删除 `_cloud_upload_configured` / `_cloud_upload_packed`
- 新增 `_cloud_accounts()`：返回当前启用且填了 Cookie 的网盘列表
- 新增 `_cloud_link_mode()`：是否「发送网盘链接」
- 新增 `_generate_passcode()`：未配置提取码时随机生成 4 位
- 新增 `_make_uploader(account)`：按账号构造 `CloudUploader`
- 新增 `_upload_and_share(account, path, passcode)`：一个网盘的上传 + 建分享
- 新增 `_format_cloud_notes(results)`：把多网盘的链接拼成一条消息
- `_emit_packed_file(...)` 重写：
  - **群文件模式**：完全不上传，行为与原版一致
  - **网盘链接模式**：`asyncio.gather` 并发上传所有已启用网盘 → 各自建永久带码分享 → 只发链接
  - 所有网盘都失败时**自动退回发文件**，避免用户拿不到内容
  - 链接模式但没配置任何网盘时，提示一句后仍发文件

**115 去重**

- `_check_115_exists()` 现在只在「链接模式 + 115 已启用」时执行
  （否则群文件模式下会误跳过下载）
- `_ensure_115_share_link()` 改为真正创建带提取码的分享链接

**新增命令**

| 命令 | 作用 |
|---|---|
| `/jm115login` | 发 115 登录二维码，扫码后自动写 Cookie 并启用 115 |
| `/jmquarklogin` | 发夸克登录二维码，同上 |
| `/jmcloud` | 查看当前发送方式与两个网盘的启用 / Cookie / 目录状态 |

三个命令均通过 `_check_permission` 做管理员校验。
二维码轮询用 `asyncio.to_thread` + `asyncio.sleep(2)`，不阻塞事件循环；有效期 180 秒，过期/取消会提示重发。

### 修改 `utils/formatter.py`

`format_help()` 新增「网盘命令」分组，并补充发送方式的说明。

---

## 兼容性说明

- **配置向后兼容**：旧配置里的 `cloud_upload_provider=115` 会被识别为「启用 115」
- **行为变化**：旧的「上传后仍然发文件」已被「发送方式」开关取代；
  升级后默认是「发送群文件」，不会意外上传
- **`_vendor` 未入库**：115 上传依赖的 `p115client` 等随插件市场分发包提供，
  本仓库不包含（体积大且含平台二进制）。自行部署时若缺依赖，
  需要把 `_vendor` 一并放入插件目录

## 已知限制

- 夸克上传/分享接口为**非公开 API**，官方改动后可能失效
- 115 分享接口对 `file_ids` 的期望在文件 id / pickcode 之间不一致，代码做了双重尝试
- 扫码登录依赖 `qrcode` 与 `Pillow`，容器内已自带，其他环境需 `pip install qrcode pillow`
