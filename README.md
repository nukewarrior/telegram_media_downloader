# Telegram 媒体归档器

一个面向单一部署者的 Telegram 媒体归档工作台。当前版本实现了完整的桌面 UI 流程、FastAPI/SQLite 接口、任务状态 SSE、归档索引和单容器部署骨架。

## 快速启动

```bash
cp .env.example .env
docker compose up --build
```

默认仅监听本机的 `http://127.0.0.1:8000`。如要在局域网访问，请先配置反向代理与认证，再修改 `docker-compose.yml` 的端口绑定。

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
