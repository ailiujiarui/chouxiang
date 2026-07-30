# Docker 运行说明

## 一键启动

```powershell
.\scripts\start.ps1 -Build
```

该脚本启动本地控制 API 和 Dashboard，等待健康检查通过，并按需构建 sandbox 镜像。默认使用 mock LLM。

```text
Dashboard: http://127.0.0.1:8501
API:       http://127.0.0.1:8000
Auth:      local single-user; Admin Token optional
```

默认端口只绑定 localhost，Dashboard 无需令牌即可提交和管理本地任务。若显式设置 `REFACTOR_AGENT_ADMIN_TOKEN`，控制操作恢复 Bearer Token 校验，Dashboard 会按 `/capabilities` 的声明显示令牌输入框。

停止服务但保留 SQLite volume：

```powershell
.\scripts\start.ps1 -Down
```

## 服务

- `api`：本地控制 API、Worker 和 SQLite 控制面。
- `dashboard`：Streamlit UI，通过 `http://api:8000` 访问 API。
- `refactor-agent`：按需运行 CLI 命令的通用容器。

API 容器挂载宿主 Docker socket，并使用 `docker-cli` 启动受限 sandbox。容器内不运行 Docker daemon。

## Sandbox

```powershell
docker build -f docker\sandbox.Dockerfile -t refactor-agent-sandbox:py312 .
docker compose run --rm refactor-agent demo --sandbox-backend docker
```

Sandbox 使用无网络、非 root、只读文件系统、capability 清空、`no-new-privileges`、PID、CPU 和内存限制。

## 基础镜像

镜像代理不可用时：

```powershell
.\scripts\start.ps1 -Build `
  -PythonBaseImage "your-registry.example.com/library/python:3.12-slim" `
  -PipIndexUrl "https://pypi.org/simple"
```

基础镜像和包索引参数会同时传给应用和 sandbox 构建。

## 数据

SQLite 和运行产物保存在 `refactor-agent-memory` volume：

```text
/data/refactor_agent.sqlite
/data/runs
/data/github-workspaces
```

`-Down` 不删除 volume。只有显式执行 `docker compose down -v` 才会删除本地数据。

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
