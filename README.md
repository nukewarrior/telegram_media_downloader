# Telegram 媒体归档器

一个面向单一部署者的 Telegram 媒体归档工作台。当前版本实现了完整的桌面 UI 流程、FastAPI/SQLite 接口、真实 Telegram 网页授权、任务状态 SSE、归档索引和单容器部署骨架。

## 快速启动

```bash
cp .env.example .env
./run.sh start
```

默认监听所有本机网卡，因此可通过 `http://<宿主机-IP>:8000`（例如 `http://10.11.11.180:8000`）访问。服务当前没有应用层认证：任何能访问此 HTTP 地址的人都可操作已连接账号与查看归档。只应部署在可信局域网，绝不可暴露公网；如需限制为本机访问，在 `.env` 设置 `HOST_BIND=127.0.0.1`。

容器使用 Docker 默认的 `bridge` 网络模式；本项目只有一个服务，端口通过 `HOST_BIND:8000` 发布到宿主机。

## 服务管理与数据目录

`run.sh` 管理单个本地实例，并把当前宿主机用户的 UID/GID 传入容器，确保服务可写入 bind mount。默认数据目录是仓库内的 `data/`，已被 Git 忽略；其中会保存 SQLite 数据库、Telegram Session、下载媒体和缩略图。

```bash
# 构建并后台启动，使用仓库内 data/
./run.sh start

# 使用当前工作目录下的相对路径（相对路径不以 run.sh 所在目录为准）
./run.sh -d ./data start

# 使用 NAS 或其他绝对路径
./run.sh -d /mnt/nas/telegram-archive restart

# 输出更详细的业务日志（默认 INFO）
./run.sh --log-level DEBUG restart

# 暂停服务 / 移除容器和网络；两者都会保留数据目录
./run.sh -d ./data stop
./run.sh -d ./data down
```

`start` 每次都会构建当前镜像；`restart` 会额外强制重建容器。两者成功后都会持续跟随最近 100 条服务日志，按 `Ctrl+C` 只停止日志显示，不会停止容器。`stop` 与 `down` 在数据目录不存在时会安全失败，防止路径拼写错误。`down` 不会使用 `--volumes`，也不会删除 `-d` 指定目录中的任何文件。

### 运行日志

`./run.sh start`、`./run.sh restart` 和 `docker logs -f telegram-media-archiver` 显示人类可读的紧凑单行日志，例如：

```text
15:42:37 INFO    download.completed             Download completed | task_id=12 message_id=8821 size=175.8MiB duration=6.2s
```

服务启动、错误、HTTP 访问和业务事件仍会同时写入 `data/logs/` 的完整结构化 JSONL；终端格式化不会改变文件日志。日志使用宿主机本地时区（带 UTC 偏移）；容器只读挂载 `/etc/localtime` 来继承该时区。HTTP 访问日志只记录方法、路径、状态、客户端地址和耗时，绝不记录查询参数或请求体。使用 `-l` / `--log-level` 指定 `DEBUG`、`INFO`、`WARNING`、`ERROR` 或 `CRITICAL`，未指定时为 `INFO`。直接使用 Docker Compose 时，可在 `.env` 设置 `LOG_LEVEL` 作为容器默认值；`run.sh` 始终以其参数（或默认的 `INFO`）传入等级。

每天首次写日志时创建 `data/logs/telegram-media-archiver-YYYY-MM-DD.jsonl`；每行仍是一个完整 JSON 对象，例如：

```json
{"timestamp":"2026-08-02T15:42:37+08:00","level":"INFO","logger":"telegram_media_archiver","event":"download.completed","message":"Download completed","size_bytes":184320000}
```

跨日后旧文件会压缩为 `.jsonl.gz`，活动日期在内最多保留 30 天。可用 `zgrep 'download.rate_limited' data/logs/*.jsonl.gz` 检查历史事件。文件日志会在 `run.sh down` 后继续保留；Docker 的实时副本仅保留 3 个、每个最多 10 MiB，供 `docker logs` 快速查看。文件系统不可写、磁盘满或压缩失败时，服务会继续运行并在 stdout 输出限频告警。

业务日志可能包含聊天标题、文件名和归档路径。日志不会写入 API Hash、验证码、两步验证密码、Telegram 会话令牌、HTTP 查询参数或请求体。

### 下载归档目录

今后完成的新下载会保存为：

```
data/downloads/<群组名>__chat-<ChatID>/<YYYY>/<MM>/<文件名>__msg-<消息ID>.<扩展名>
```

目录日期取 Telegram 消息发送时间换算为任务创建时固定的 IANA 时区后的年月；首次引导会要求确认时区，之后可在设置页修改以影响未来新建任务。群组名取创建任务时记录的名称，因此群组日后改名不会移动已有文件。Chat ID 和消息 ID 分别避免同名群组、同名文件混档。已有下载保持原有路径，服务不会自动迁移或重组它们。

首次访问会依次完成：

1. 填写 Telegram API ID 和 API Hash；
2. 确认用于归档目录年月的 IANA 时区；
3. 连接 Telegram，或跳过并在任务中心稍后输入带国家区号的手机号、验证码及两步验证密码；
4. 选择一个聊天、设置筛选、扫描并创建归档任务。

已完成的登录会话存放在 `data/sessions/telegram.session`，因此只要使用同一个 `-d` 数据目录，容器重启或长时间未操作都无需重新登录。验证码尚未验证时的登录上下文仅保留十分钟且不写入磁盘；服务重启后需重新发送验证码。若你在 Telegram 的设备管理中撤销该会话，页面会提示重新连接，但不会删除已有归档数据。

新建立的 Telegram 登录会话会使用 `Telegram Media Downloader` 和项目发布版本作为设备/应用标识；当前版本为 `0.1.0`。正在复用的旧会话不会自动迁移，如需在 Telegram 的设备列表中看到新标识，请退出后重新登录。设备列表中的 API 应用名由所填写的 API ID 决定，活动地点由 Telegram 根据网络位置判断，本项目不修改这两项。

### 归档目的地

设置页可以管理多个归档目的地：本地目录或 WebDAV。创建任务时必须选择一个已启用的目的地；目的地会写入任务并在之后保持不变，同一条 Telegram 消息可以分别归档到不同目的地。

原有的 `DOWNLOAD_ROOT` 会自动登记为系统本地目的地，既有文件不会移动，旧任务和旧归档仍按原路径读取。新本地归档沿用上面的聊天、年月和消息 ID 路径规则；WebDAV 则在配置的远端根路径下使用相同的相对路径。

WebDAV 第一版支持 URL、用户名、密码和远端根路径。例如 URL 填 `http://10.11.11.11:5244/dav`、远端根路径填 `snail/media/telegram` 时，实际归档根目录是 `/dav/snail/media/telegram/`；项目只会在这个目录下按需创建聊天、年月子目录，不会尝试创建 `/dav` 服务入口。连接测试会验证目标根目录，并执行临时文件的 `PUT → MOVE → DELETE`，因此 WebDAV 账号必须具备目录访问、上传、改名和删除权限。上传先写入数据卷中的本地临时文件，再以远端 `.part` 名称上传，最后通过 WebDAV `MOVE` 提交；提交失败时任务会进入可重试状态并保留临时文件，不会在 Telegram 端重复下载已完成的临时副本。归档页通过后端代理读取 WebDAV 原文件，支持预览、下载和 Range 请求，缩略图仍保存在本地派生缓存中。

## 开发模式

后端：

```bash
python -m venv .venv
.venv/bin/pip install -r backend/requirements.txt
DATA_DIR=./data .venv/bin/uvicorn app.main:app --app-dir backend --reload
```

前端：

```bash
cd frontend
npm install
npm run dev
```

## 演示模式与当前范围

- 默认 `DEMO_MODE=false`，网页会调用 Telegram 发送并验证真实验证码。仅在 `.env` 显式设置 `DEMO_MODE=true` 时，账号连接、聊天、扫描和示例任务可直接用于验证 UI；验证码可输入任意六码，界面会显示“演示模式”标识。若你的旧 `.env` 中仍是 `DEMO_MODE=true`，请将其改为 `false` 后重启服务。
- SQLite 持久化 API 凭据、任务和归档元数据；`API Hash` 永不从读取接口返回。
- 归档页会为图片生成本地 JPEG 缩略图、为视频抽取本地首帧；历史归档在后台单线程补齐，缩略图与原文件均通过受控归档接口读取。浏览器不能解码的视频会保留封面并允许下载原文件，不进行服务端转码。

不要将无认证的服务直接暴露到公网；Telegram Session 与 API 凭据是高敏感数据。
