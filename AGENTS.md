# AGENTS.md

Sing Reactor 是 Windows 本机运行的 B站逐句练唱工具，当前产品阶段为本地技术原型。

## 启动、停止、测试

- 启动并由窗口托管：`start.cmd`
- 后台启动：`powershell -NoProfile -ExecutionPolicy Bypass -File .\start.ps1 -NoWait`
- 停止已验证实例：`powershell -NoProfile -ExecutionPolicy Bypass -File .\start.ps1 -StopExisting`
- 测试：`python -m unittest discover -s tests -p "test*.py"`
- 语法检查：`python -m py_compile server\server.py server\http_safety.py tests\test_server.py`、`node --check app\app.js`、`node --check server\ocr_frame.js`

## 技术栈

- Windows PowerShell 启动器；Python 标准库 HTTP API；原生 HTML/CSS/JavaScript 前端。
- yt-dlp、FFmpeg、faster-whisper；OCR 兜底使用 Node.js 与 tesseract.js。

## 关键目录

- `app/`：蓝白单页 UI、播放器与歌词交互。
- `server/`：API、URL 安全、媒体、歌词对齐、ASR 与 OCR。
- `tests/`：Python unittest。
- `.cache/`、`.models/`、`.tools/`：运行缓存、模型与工具依赖，不是源码清理目标。

## 必须遵守

- 本地开发时网页与 API 只绑定本机回环地址。服务器部署时 API 进程仍只监听服务器回环地址，只允许通过 `singreactor.archeo.cn` 的 HTTPS Nginx 反向代理公开指定路由；不得开放 API 裸端口。
- 输入、重定向与最终 URL 必须保持 HTTP/HTTPS、允许域名、公网 IP、无 userinfo、默认端口等安全校验。
- 不新增按 BVID 或歌曲写死的时间特例；现有 `KNOWN_*` / `KNOWN_VIDEO_OFFSETS` 仅作兼容兜底。
- 最终歌词采用线上文字，ASR 提供完整词序列时间与验证证据。
- `start.ps1` 必须保持 UTF-8 BOM，并兼容 Windows PowerShell 5.1。
- 修改 HTML `<title>` 时必须同步或兼容启动器网页身份验证。
- 不删除模型、工具缓存或运行清单；按现有启动/停止与缓存规则处理。
- 临时调试插桩只在明确需要时加入，确认结束后从现役代码清理。

## 当前状态 / 下一步

- 当前已具备链接识别、本地视频播放、线上歌词文字 + ASR 时间对齐及逐句练习。
- 下一步只按明确需求推进手工歌词 UI、导入、录音评分或缓存管理等未实现范围。
