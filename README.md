# Sing Reactor

Sing Reactor 是 B站逐句练唱工具。它把 B站视频、可信歌词时间轴与浏览器原生 `<video>` 的真实播放进度连接起来，便于按句跳转和反复练习；既支持 Windows 本机运行，也支持由受控 HTTPS 反向代理提供网页与 API。

本文是当前运行、使用与维护的唯一基准；产品范围说明见 `docs/prd-sing-reactor-cn.md`。

## 当前能力与边界

已实现：

- 识别 `bilibili.com`、其子域和 `b23.tv` 链接，获取标题、BVID、CID、时长与公开字幕。
- 匹配和校准逐句歌词；必要时使用 Whisper 音频锚点，但不把 Whisper 转写本身直接当作最终歌词。
- 下载、合并或转码本地 MP4，由原生 `<video>` 播放并按 `currentTime` 高亮歌词。
- 点击歌词跳转、逐句暂停、单句循环。
- 保留少量 `KNOWN_*` 与 `KNOWN_VIDEO_OFFSETS` 作为兼容性兜底；通用歌词对齐不依赖逐视频偏移。
- 后端 `POST /api/align-lyrics` 手工歌词对齐接口；当前前端没有入口。
- 当前 UI 是蓝白单页，包含链接输入、视频播放器和歌词面板。

明确未实现：

- 录音、音高/节奏检测、评分、账号、云同步、练习历史、歌单导入。
- 本地音频上传、本地 LRC 上传、前端粘贴歌词或手工校准界面。
- 缓存管理 UI、多人访问、认证、授权曲库或对外服务能力。

## 快速使用

1. 双击 `start.cmd`；也可在 PowerShell 中运行 `start.ps1`。普通启动由该窗口托管新建服务并等待回车；使用 `start.ps1 -NoWait` 时启动成功后立即返回。
2. 浏览器会打开 `http://127.0.0.1:4190/index.html`；若未自动打开，可手动访问该地址。重复启动会安全复用已验证的健康实例，随后启动器返回，双击打开的窗口也会关闭。
3. 粘贴 B站视频链接，点击“识别”，或按 Enter。
4. 点击任意歌词跳转到该句；按需启用“逐句暂停”或“单句循环”。
5. 普通启动新建服务后，可回到创建服务的启动窗口按回车，正常停止 API 与网页服务并删除实例清单；`-NoWait` 或复用实例没有持续托管窗口，可运行 `start.ps1 -StopExisting` 安全停止。

页面右上角提供浏览器插件下载入口，当前版本为 `0.1.1`。下载 ZIP 后，按压缩包内的 `INSTALL.md` 完成安装。

## 线上部署

生产站点为 `https://singreactor.archeo.cn`。Nginx 提供静态网页，并把 `/api/identify`、`/api/align-lyrics`、`/api/video` 和 `/api/health` 代理到仅监听服务器回环地址的主 API。浏览器插件原有的 `/auth/session`、`/transcribe`、`/transcribe/jobs/*` 与 `/health` 继续代理到独立的插件服务，两套服务共享域名但不共享进程或接口前缀。

部署模板位于 `deploy/`：

- `sing-reactor-web.service`：主 API 的 systemd 服务，默认使用 `/opt/sing-reactor-web/current`。
- `sing-reactor-web.env.example`：生产环境变量示例；实际配置保存在服务器 `/etc/sing-reactor-web.env`。
- `singreactor.archeo.cn.nginx`：静态站点、主 API 与插件 API 的共存路由，并包含识别和媒体接口限流。

主 API 的生产进程仍绑定 `127.0.0.1:18768`，不得改为公网监听。当前网页发布目录为 `/var/www/singreactor.archeo.cn`，主 API release 位于 `/opt/sing-reactor-web/releases/`；切换 release 时更新 `/opt/sing-reactor-web/current` 软链接并重启 `sing-reactor-web.service`。

## 识别与校准链路

当前顺序如下：

1. **B站公开字幕**：优先使用公开字幕，并优先中文、非自动字幕。
2. **网易云 / LRCLIB 文字 + 完整 ASR 时间**：没有可靠公开字幕时，先取得线上歌词文字，再对整段音频运行 Whisper，并用全局单调序列对齐把 ASR 词级 `start/end` 分配给线上歌词行。最终展示文字始终来自线上候选，不直接采用 Whisper 错字。
3. **序列融合接受门**：综合行覆盖、字符覆盖、平均文本相似度、时间跨度、锚点分布、最长连续漏行和 ASR 质量；只命中少量副歌、锚点集中片头或连续漏行过长都会拒绝。缺少词时间戳时可退到 segment 内字符插值，并明确标记 segment mode。
4. **同步 LRC fallback**：完整 Whisper 不可用或序列融合拒绝时，同步 LRC 才尝试原有三段采样固定偏移/线性校准；仍失败则保留原时间并标记 fallback。LRCLIB 纯文本没有安全时间轴可退，继续尝试后续来源。
5. **内置/手工歌词**：同样复用完整 ASR 序列融合核心；Whisper 只提供时间与验证证据，不单独输出为最终歌词。
6. **yt-dlp 字幕 / OCR 兜底**：继续尝试视频自带或自动字幕；仍失败时，用 FFmpeg 稀疏抽帧并通过 Node.js / `tesseract.js` OCR 画面歌词。
7. **无可靠结果**：明确提示未找到歌词，不伪造结果。

### 完整 ASR 序列融合与采样 fallback

首次处理没有公开字幕的视频时，完整 Whisper 通常需要取得或复用视频、提取 WAV 并完成整段 CPU 推理，因此会比后续缓存命中慢。系统会根据线上歌词文字脚本推断识别语言提示（中文歌词使用 `zh`），并以该语言身份校验缓存。序列核心限制每行候选数量、候选字符/时间跨度和 beam 宽度，以全局顺序处理口白、漏识别、跨 segment、重复副歌和一段跨多行；未匹配行只在左右可靠锚点之间按规范化文字长度插值，不对头尾大段外推。

三段采样仅是同步歌词的低成本 fallback。满足条件时，系统在媒体约 20%、50%、80% 位置采样：60–119 秒媒体每段 16 秒，至少 120 秒媒体每段 24 秒，合计约 48 或 72 秒。

系统将采样锚点与歌词行匹配，并在两种校正中选择：

- **固定偏移**：整条时间轴统一提前或延后。
- **线性缩放**：在偏移之外对时间轴做轻微伸缩，以修正逐渐累积的漂移。

只有匹配数量、三段覆盖、文本相似度、残差和时间跨度等达到高置信阈值才改写时间轴。采样失败、工具缺失或置信度不足时，保留在线同步歌词原样；高置信优先于“强行校准”。

## 播放边界

前端使用原生 `<video>`，播放、暂停、拖动、逐句暂停和单句循环均以真实 `currentTime` 为准。视频通过本地 API 提供并支持单个 HTTP Range 请求。

如果用户点击的歌词时间超出当前视频长度，界面会提示“这句超出了当前视频长度”，并要求更换完整视频链接。当前没有在识别完成阶段对全部歌词执行完整覆盖预检，因此不能保证提前发现每一条超时歌词。

## 启动、停止与日志

只推荐 `start.cmd` / `start.ps1`：

- 启动脚本通过项目根目录的 `.sing-reactor.processes.json` 记录进程 PID、创建时间、角色、端口和运行时路径，并结合端口 owner 与 HTTP 产品身份防止 PID 复用或误认。
- 重复双击时，若实例清单和两个服务均通过严格校验，会提示“已有实例正在运行”、直接打开网页并返回成功；并发双击由项目专属互斥锁串行处理。
- 对没有清单的旧版实例，仅在两个端口、Python 路径、命令角色与 HTTP 身份全部匹配时安全纳管；网页身份检查接受 `Sing Reactor` 主标题带副标题。任一端口属于未知或无法充分证明归属的程序时会列出端口、PID、进程名并拒绝启动，绝不会按端口自动结束进程。
- 本地启动时网页和 API 仅绑定回环地址；启动成功后打开本机网页。服务器部署时 API 进程同样只监听服务器回环地址，由 Nginx 公开指定 HTTPS 路由。
- 保持创建服务的启动窗口打开；按回车正常停止两个服务并删除实例清单。窗口异常关闭后子进程可能继续运行，下次启动会用清单恢复识别并复用。
- 日志位于项目根目录：`.server.out.log`、`.server.err.log`、`.web.out.log`、`.web.err.log`。自动化检查可使用 `start.ps1 -NoBrowser -ProbeOnly`；`-NoWait` 用于启动后立即返回，之后可用 `-StopExisting` 安全停止已验证实例。

## 依赖与安装边界

- Windows PowerShell。
- Python 3，启动与安装脚本固定使用：
  `%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe`
- 支持原生 MP4 与 Range 的桌面浏览器，以及访问 B站、歌词服务和必要模型源的网络。
- Python 视频/语音依赖为 `yt-dlp`、`imageio-ffmpeg`、`faster-whisper`。`requirements.txt` 只固定这三个直接 Python 依赖；pip 会自行解析其传递依赖。
- `install-tools.ps1` 只把 Python 依赖安装到 `.tools/python`，不安装 Node.js、`tesseract.js`、OCR 语言数据或 Whisper 模型，也不改变其他安装行为。
- Whisper 模型不会随安装脚本下载。运行时优先使用有效的本地 `.models` 模型或 `SING_REACTOR_WHISPER_MODEL`，否则可能按模型名从 Hugging Face 下载；当前使用 CPU `int8`。
- OCR 另需 Node.js、`tesseract.js`、FFmpeg、`yt-dlp` 和 `chi_sim` / `chi_tra` / `eng` 语言数据。仓库根目录虽可能存在 `.traineddata`，当前 `createWorker` 没有显式设置 `langPath`，因此不保证自动使用这些根目录文件；是否离线命中取决于 `tesseract.js` 的解析与缓存环境。
- `package.json` 只精确指定直接依赖 `tesseract.js`；仓库没有 Node 锁文件，因此不声称其传递依赖被完整锁定。

系统 FFmpeg 优先；没有时可使用 `imageio-ffmpeg` 提供的可执行文件。缺少 Whisper 只会关闭需要音频锚点的路径；缺少 Node/OCR 依赖只会关闭最终 OCR 兜底。

## 缓存与清理

自动管理的三类持久缓存：

- `.cache/videos/`：本地 MP4 及下载过程可能留下的媒体旁路文件。
- `.cache/audio/`：Whisper 使用的 16 kHz 单声道 WAV。
- `.cache/speech/`：普通 `.json` 是 schema v3 的完整 Whisper 分段、词级时间戳、质量字段与模型元数据；元数据同时记录 `languageHint` / `requestedLanguage`。`.sampled.json` 是 schema v3 的三段短采样结果。仅 schema、模型标识、语言身份和结构校验一致时复用；空 `segments` 会被拒绝且不会写入缓存。采样校准优先复用有效的完整 JSON，否则复用窗口配置匹配的 sampled JSON。

清理规则：

- 文件 TTL 为 7 天；三目录合计上限为 2 GiB。
- 相关缓存访问会触发清理，但最多每 5 分钟执行一次。
- 超过 1 小时的点号临时文件会被删除；超出容量后按修改时间从旧到新删除。
- 视频和 WAV 命中时刷新修改时间；完整或采样语音 JSON 命中时不刷新，因此语音 JSON 仍按原修改时间过期或参与容量淘汰。
- `.cache/huggingface/`、`.models/`、`.tools/python/` 和 OCR 系统临时目录不计入上述 2 GiB。OCR 临时文件通常由临时上下文清理。
- 当前没有缓存管理 UI。

人工清理时先停止服务，再删除 `.cache/videos/`、`.cache/audio/`、`.cache/speech/` 中不需要的内容；下次会重新下载或计算。若要回收下载模型空间，可另行删除 `.cache/huggingface/`。不要把 `.models/` 或 `.tools/python/` 当普通缓存删除，除非准备重新提供模型或依赖。

## 安全边界

- 本地网页绑定 `127.0.0.1:4190`，API 默认绑定 `127.0.0.1:18768`。生产环境 API 仍监听服务器 `127.0.0.1:18768`，公网只能通过 `https://singreactor.archeo.cn/api/*` 访问。
- 输入 URL、重定向目标和最终 URL 均校验：只允许 HTTP/HTTPS；host 必须是 `b23.tv`、`bilibili.com` 或其允许的子域；拒绝 userinfo、非默认端口、IP 字面量、DNS 失败以及解析到环回/私网/链路本地等非公网 IP 的地址。
- CORS 默认仅允许 `http://127.0.0.1:4190` 与 `http://localhost:4190`；生产环境通过 `SING_REACTOR_ALLOWED_ORIGINS` 增加站点 HTTPS 来源。其他带 `Origin` 的请求返回 403。
- CORS 不是认证。生产环境不开放 API 裸端口，只由 Nginx 代理明确列出的 API 路由，并对识别、对齐和媒体请求执行限流与连接数限制。
- POST JSON 请求体上限 1 MiB，手工歌词 UTF-8 上限 512 KiB。
- 视频只接受单个 Range；无效或多段 Range 返回 HTTP 416。

## 排障

- **页面打不开**：确认启动窗口没有 Python 或端口错误；手动访问 `http://127.0.0.1:4190/index.html`，再查看 `.web.err.log`。
- **页面提示识别服务未启动**：检查 `18768` 端口与 `.server.err.log`。
- **端口占用**：错误会列出冲突端口、PID 和进程名。健康的 Sing Reactor 会被复用；未知或仅部分匹配的占用者不会被脚本结束，请确认其用途后手工处理。
- **视频准备失败**：检查 `yt-dlp`、FFmpeg、网络和 `.cache/videos/`；必要时停止服务后删除对应视频缓存并重试。
- **Whisper 首次很慢或失败**：首次完整转写通常最慢，后续会复用通过 schema/模型校验的缓存；检查模型是否可用、模型源网络、CPU、磁盘空间和 `.cache/huggingface/` 写权限。
- **某些版本仍无法可靠对齐**：Live、明显改编、口白占比很高、线上歌词本身错误或歌曲结构差异过大时会触发拒绝或同步 LRC fallback，不保证强行生成时间轴。
- **OCR 无结果**：检查 Node.js、`tesseract.js`、语言数据、FFmpeg 与 `yt-dlp`。OCR 是稀疏兜底，不保证适用于动态字幕、花体字或复杂背景。
- **歌词跳转超出视频**：更换包含完整歌曲的视频链接。

## 版权说明

程序不绕过登录、付费、DRM、地域限制或平台权限。为本地播放、校准和 OCR，程序会在本机保存视频、WAV、语音锚点和可能下载的模型；这不是零缓存方案。

B站字幕、网易云/LRCLIB 歌词及视频内容的可访问不代表获得再利用授权。缓存仅供当前用户在本机练习和技术处理，不应公开分享、再分发、上传仓库或用于规避平台规则；用户应自行确认内容使用权与相关服务条款。
