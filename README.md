# Telegram 媒体归档器

一个面向单一部署者的 Telegram 媒体归档工作台。当前版本实现了完整的桌面 UI 流程、FastAPI/SQLite 接口、任务状态 SSE、归档索引和单容器部署骨架。

## 快速启动

```bash
cp .env.example .env
./run.sh start
```

默认监听所有本机网卡，因此可通过 `http://<宿主机-IP>:8000`（例如 `http://10.11.11.180:8000`）访问。服务当前没有应用层认证，只应部署在可信局域网；如需限制为本机访问，在 `.env` 设置 `HOST_BIND=127.0.0.1`。

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

首次访问会依次完成：

1. 填写 Telegram API ID 和 API Hash；
2. 在任务中心连接 Telegram；
3. 选择一个聊天、设置筛选、扫描并创建归档任务。

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

## 当前行为与下一步

- `DEMO_MODE=true` 时，账号连接、聊天、扫描和示例任务可直接用于验证 UI；验证码可输入任意六码。
- SQLite 持久化 API 凭据、任务和归档元数据；`API Hash` 永不从读取接口返回。
- 归档页已经具备图片/视频缩略图状态字段和失败降级 UI；实际的 Pillow/FFmpeg 缩略图 worker、Telethon 登录网关、历史扫描与断点下载 worker 是下一阶段的服务实现。

不要将无认证的服务直接暴露到公网；Telegram Session 与 API 凭据是高敏感数据。
