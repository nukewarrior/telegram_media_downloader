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

# 暂停服务 / 移除容器和网络；两者都会保留数据目录
./run.sh -d ./data stop
./run.sh -d ./data down
```

`start` 每次都会构建当前镜像；`restart` 会额外强制重建容器。两者成功后都会持续跟随最近 100 条服务日志，按 `Ctrl+C` 只停止日志显示，不会停止容器。`stop` 与 `down` 在数据目录不存在时会安全失败，防止路径拼写错误。`down` 不会使用 `--volumes`，也不会删除 `-d` 指定目录中的任何文件。

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
