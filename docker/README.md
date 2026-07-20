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

## 已删除能力

API 不接收 GitHub Webhook，不包含 GitHub write token，也不会创建 branch、commit、push、Pull Request 或 Issue 评论。GitHub URL 任务只读克隆 allowlist 仓库并在本地保存结果。
