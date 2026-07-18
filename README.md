# Refactor Agent

面向 Python 的本地代码审查与安全精简工具。当前只提供三条流程：

1. 粘贴代码并进行只读静态审查；
2. 粘贴代码和 pytest，执行完整本地验证精简；
3. 从 allowlist GitHub 仓库只读克隆代码并执行本地验证精简。

项目不接收 GitHub Webhook，不创建分支、commit、push、Pull Request 或 Issue 评论。

## 安全执行链

验证精简使用真实 LangGraph 节点执行：

```text
prepare -> minimizer -> ast_guard -> pytest -> adversary -> mutation -> judge -> finalize
```

- LLM 候选按不可信完整文件处理。
- AST Guard 只应用请求、符号或 traceback 定位到的区域。
- 新 import 默认拒绝，危险调用、公开 API 和目标区域外修改会被拒绝。
- API/Worker 强制使用无网络、非 root、只读根文件系统和资源受限的 Docker sandbox。
- 本地 CLI 的 subprocess 模式只适用于可信代码，不是安全 sandbox。
- 源码、diff、pytest、对抗测试、变异测试和报告保存在 SQLite 与 `.runs`。

## 一键启动

需要 Docker Desktop。Windows PowerShell：

```powershell
.\scripts\start.ps1 -Build
```

默认地址：

- Dashboard：`http://127.0.0.1:8501`
- API：`http://127.0.0.1:8000`
- 本地 Admin Token：`local-admin-secret`

默认使用 mock LLM，不调用真实 DeepSeek。停止服务：

```powershell
.\scripts\start.ps1 -Down
```

端口和基础镜像可以覆盖：

```powershell
.\scripts\start.ps1 -Build `
  -ApiPort 18000 `
  -DashboardPort 18501 `
  -PythonBaseImage "your-registry.example.com/library/python:3.12-slim"
```

## CLI

安装开发依赖：

```powershell
python -m pip install -e .[dev]
```

内置演示：

```powershell
refactor-agent demo
refactor-agent demo-suite --sandbox-backend auto
```

审查文件或 stdin；REVIEW 不执行代码：

```powershell
refactor-agent snippet --source snippet.py --mode review --persona tsundere
Get-Content snippet.py | refactor-agent snippet --source - --mode review
```

验证精简必须提供 pytest，测试固定从 `snippet` 模块导入：

```powershell
refactor-agent snippet `
  --source snippet.py `
  --tests test_snippet.py `
  --mode verified-refactor `
  --sandbox-backend docker
```

现有本地项目可以使用文件入口：

```powershell
refactor-agent run --target app.py --issue issue.md --tests tests
```

只读 GitHub URL 入口：

```powershell
refactor-agent github-url `
  --repo-url https://github.com/owner/repository `
  --issue-text "精简 calculate 函数" `
  --target src/package/module.py `
  --tests tests
```

该 CLI 入口只克隆和读取用户显式提供的仓库，候选与报告留在本地；Dashboard/API 的 URL 入口额外要求仓库 allowlist。

## 控制 API

启动：

```powershell
$env:REFACTOR_AGENT_ADMIN_TOKEN="local-admin-secret"
$env:REFACTOR_AGENT_ALLOWED_REPOSITORIES="owner/repository"
$env:REFACTOR_AGENT_SANDBOX_BACKEND="docker"
$env:REFACTOR_AGENT_MOCK_LLM="true"
refactor-agent serve --host 127.0.0.1 --port 8000
```

控制 API 提供：

- `/health`、`/capabilities`
- `/jobs`、任务详情、事件、取消和重试
- `/jobs/snippet`、`/jobs/url`
- `/runs`、trajectory 和运行产物
- `/benchmarks`
- `/admin/repository-allowlist`

Snippet、URL 提交、取消、重试和 allowlist 管理需要 Admin Token。代码和测试会持久化在本地任务数据中，不要提交密钥。

## 模式和状态

| 入口 | 行为 | 完成语义 |
| --- | --- | --- |
| Snippet REVIEW | 静态分析，不执行代码 | 只读审查完成 |
| Snippet VERIFIED | 完整安全执行链 | 本地验证完成 |
| GitHub URL | 只读克隆后完整安全执行链 | 本地验证完成 |

旧数据库中的 `GITHUB_WEBHOOK` 和 `DRY_RUN` 枚举仅为历史兼容保留。旧 Webhook 任务可读取，但 Worker 会拒绝执行，API 会拒绝重试。

## DeepSeek

使用真实模型：

```powershell
$env:DEEPSEEK_API_KEY="<set-in-environment>"
$env:REFACTOR_AGENT_MOCK_LLM="false"
```

可选变量：`DEEPSEEK_BASE_URL`、`DEEPSEEK_MODEL`。密钥不会写入 SQLite、日志或运行产物。

## 验证

```powershell
pytest -q
python -m compileall -q src tests
docker compose config --quiet
git diff --check
```

架构边界见 `phase4-reliability-benchmark-dashboard-design.md`，Docker 说明见 `docker/README.md`。
