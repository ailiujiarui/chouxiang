# Docker 运行说明

## 一键启动

将根目录 `.env.example` 复制为 `.env`，填写 DeepSeek Key，然后双击
`start.cmd`。启动器在项目 `.venv` 中启动 API、Dashboard 和奶龙；Docker 只负责
构建并运行安全 sandbox。日常启动不构建应用镜像，也不使用 Compose。

sandbox 镜像构建会复用 Docker 缓存。默认使用真实 DeepSeek；mock 必须在 `.env`
中显式启用。

```text
Dashboard: http://127.0.0.1:8501
API:       http://127.0.0.1:8000
Auth:      local single-user; Admin Token optional
```

默认端口只绑定 localhost，Dashboard 无需令牌即可提交和管理本地任务。若显式设置 `REFACTOR_AGENT_ADMIN_TOKEN`，控制操作恢复 Bearer Token 校验，Dashboard 会按 `/capabilities` 的声明显示令牌输入框。

停止服务但保留 SQLite volume：

双击 `stop.cmd`。停止操作不会删除 `.runs` 数据、日志或 sandbox 镜像。

## 服务

- `api`：本地控制 API、Worker 和 SQLite 控制面。
- `dashboard`：Streamlit UI，通过 `http://api:8000` 访问 API。
- `refactor-agent`：按需运行 CLI 命令的通用容器。

一键启动的本机 API 通过宿主 `docker.exe` 启动受限 sandbox，不需要在应用镜像内
重复安装 Docker CLI。Compose 文件和应用 Dockerfile 仅保留给独立的容器化开发或
部署流程，不属于一键启动链路。

## Sandbox

```powershell
docker build -f docker\sandbox.Dockerfile -t refactor-agent-sandbox:py312 .
docker compose run --rm refactor-agent demo --sandbox-backend docker
```

Sandbox 使用无网络、非 root、只读文件系统、capability 清空、`no-new-privileges`、PID、CPU 和内存限制。

## 本地配置

`.env` 是唯一的启动配置入口，不会提交到 Git。`PYTHON_BASE_IMAGE` 和
`PIP_INDEX_URL` 可用于 sandbox 基础镜像、本机产品依赖和 sandbox Python 包索引。
启动器只读取允许的变量，不打印密钥。

## 数据

一键启动的 SQLite、运行产物和日志保存在仓库 `.runs`：

```text
.runs/refactor_agent.sqlite
.runs/github-workspaces
.runs/logs
```

`stop.cmd` 不删除这些数据。旧版 Compose volume 不会被自动删除或迁移。

### SQLite 并发模式

三个 SQLite Store 共用以下配置：

```text
REFACTOR_AGENT_SQLITE_JOURNAL_MODE=auto  # auto | wal | delete
REFACTOR_AGENT_SQLITE_BUSY_TIMEOUT_MS=5000
```

`auto` 只会在本地文件系统、SQLite 版本通过 WAL 安全门禁且 SQLite 实际返回 `journal_mode=wal` 时启用 WAL；否则新数据库保持 `delete`。若数据库已经是 WAL，而当前运行时不通过门禁，进程会在 API/Worker 启动前失败，避免不安全运行时加入。强制 `wal` 同样失败关闭，不会静默降级。当前识别的修复线为 SQLite `>=3.51.3`、`3.50.7+` 和 `3.44.6+`；CI 的 `wal-safe` job 会打印并验证实际版本。

WAL 仅支持本机文件系统。不要把数据库放在 NFS、SMB/CIFS 或其他网络文件系统；Compose 的本地 Docker volume 属于支持范围。`/capabilities` 和启动日志会报告 SQLite 版本、请求/实际 journal mode、busy timeout 与门禁结果，但不会暴露数据库路径或 SQL。

### WAL 备份与回退

WAL 模式运行时，`refactor_agent.sqlite-wal` 和 `refactor_agent.sqlite-shm` 是数据库状态的一部分。不要在进程仍写入时只复制主 `.sqlite` 文件。备份前应停止 API、Worker、Dashboard 和其他 CLI writer，然后执行 checkpoint；也可以在所有 writer 停止后，把主文件与仍存在的 `-wal`/`-shm` 作为一组处理。

受控回退到 rollback journal：

1. 停止所有会打开数据库的服务和 CLI writer。
2. 对数据库执行 `PRAGMA wal_checkpoint(TRUNCATE)`，确认返回值中的 busy 项为 `0`。
3. 执行 `PRAGMA journal_mode=DELETE`，确认返回值为 `delete`。
4. 设置 `REFACTOR_AGENT_SQLITE_JOURNAL_MODE=delete` 后重新启动服务，并从 `/capabilities` 验证实际模式。

不得在活跃写入期间切换 journal mode，也不得单独删除 `-wal` 或 `-shm` 文件。需要在线备份时应使用 SQLite backup API，而不是文件级单文件复制。

## 已删除能力

API 不接收 GitHub Webhook，不包含 GitHub write token，也不会创建 branch、commit、push、Pull Request 或 Issue 评论。GitHub URL 任务只读克隆 allowlist 仓库并在本地保存结果。
