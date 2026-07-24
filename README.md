# Refactor Agent

面向 Python 的本地代码审判与安全精简工具。当前支持：

1. 粘贴 Python 代码，执行 AST 分析、多 Agent 对抗和人格化审查；
2. 粘贴 Python 代码并提供 pytest，执行完整的本地验证精简；
3. 只读克隆 allowlist 中的 GitHub 仓库，在本地完成分析与验证。

项目不接收 GitHub Webhook，不创建分支、commit、push、Pull Request 或 Issue 评论。

## 执行链

统一分析流程为：

```text
Input Adapter
  -> AST Analyst
  -> Minimizer
  -> Defender
  -> Adversary
  -> Judge
  -> Persona Reporter
```

- LLM 生成的候选文件始终按不可信输入处理。
- AST Guard 限制允许修改的区域，并拒绝危险调用、新增高风险 import、公开 API 破坏和越界修改。
- API/Worker 使用无网络、非 root、只读根文件系统和资源受限的 Docker sandbox。
- 用户测试、系统生成测试、对抗测试、变异测试和 Judge 共同形成可追溯证据。
- 报告明确标记 `STATIC`、`GENERATED_TESTS`、`USER_TESTS` 或 `REPOSITORY_TESTS` 证据等级。
- 人格只影响报告措辞，不参与技术裁决和安全判断。

## 一键启动

需要 Docker Desktop 和 Windows PowerShell：

```powershell
.\scripts\start.ps1 -Build
```

默认地址：

- Dashboard：`http://127.0.0.1:8501`
- API：`http://127.0.0.1:8000`

默认是仅绑定 localhost 的本地单用户模式，不设置管理员令牌，Dashboard 可直接提交和管理本地任务。这个默认值不代表网络部署安全；不要把端口转发到不可信网络。

默认使用 mock LLM，仅适合内置演示和离线回归。停止服务但保留本地数据：

```powershell
.\scripts\start.ps1 -Down
```

端口、基础镜像和 Python 包索引可以覆盖：

```powershell
.\scripts\start.ps1 -Build `
  -ApiPort 18000 `
  -DashboardPort 18501 `
  -PythonBaseImage "your-registry.example.com/library/python:3.12-slim" `
  -PipIndexUrl "https://pypi.org/simple"
```

### Nailong 桌面主动通知

需要额外安装桌面依赖，并在启动服务时追加 `-Desktop`：

```powershell
python -m pip install -e ".[desktop]"
.\scripts\start.ps1 -Build -Desktop
```

桌面端通过现有分析事件流接收任务状态，按冷却、免打扰和终态优先级规则显示弹窗。默认数据目录为 `.runs`，其中包含 `nailong-agent.lock`、`nailong_privacy.sqlite` 和 `nailong_notifications.sqlite`；可通过 `NAILONG_DATA_DIR` 或启动脚本的 `-NailongDataDir` 修改。`NAILONG_ANALYSIS_URL` 和 `NAILONG_DEEPSEEK_MODEL` 可提供桌面端默认连接配置。桌宠数据库不会保存 API Key、源代码、原始窗口内容、截图、OCR、剪贴板或终端正文。完整的事件映射、接口和验证方式见 [`docs/designs/2026-07-24-nailong-proactive-notifications-update.md`](docs/designs/2026-07-24-nailong-proactive-notifications-update.md)。

## 可选认证

本地单用户启动不需要管理员令牌。需要额外保护提交、取消、重试和 allowlist 管理操作时，可在启动前显式设置：

```powershell
$env:REFACTOR_AGENT_ADMIN_TOKEN="<strong-random-secret>"
.\scripts\start.ps1
```

此时 `/capabilities` 返回 `admin_token_required=true`，Dashboard 才显示管理员令牌输入框；未携带正确 Bearer Token 的控制请求会返回 `401`。令牌不会写入 SQLite 或运行产物。

## DeepSeek

使用真实模型：

```powershell
$env:DEEPSEEK_API_KEY="<set-in-environment>"
$env:REFACTOR_AGENT_MOCK_LLM="false"
.\scripts\start.ps1
```

可选变量为 `DEEPSEEK_BASE_URL` 和 `DEEPSEEK_MODEL`。API Key 不会写入 SQLite、日志或运行产物。

## CLI

安装开发依赖：

```powershell
python -m pip install -e .[dev]
```

审查文件或 stdin，`REVIEW` 不执行用户代码：

```powershell
refactor-agent snippet --source snippet.py --mode review --persona tsundere
Get-Content snippet.py | refactor-agent snippet --source - --mode review
```

提供 pytest 后执行验证精简：

```powershell
refactor-agent snippet `
  --source snippet.py `
  --tests test_snippet.py `
  --mode verified-refactor `
  --sandbox-backend docker
```

只读 GitHub URL：

```powershell
refactor-agent github-url `
  --repo-url https://github.com/owner/repository `
  --issue-text "简化 calculate 函数" `
  --target src/package/module.py `
  --tests tests
```

## 控制 API

主要接口：

- `/health`、`/capabilities`
- `/analysis`、`/jobs/snippet`、`/jobs/url`
- `/jobs`、任务详情、事件、取消和重试
- `/runs`、trajectory、运行产物和 `/benchmarks`
- `/admin/repository-allowlist`

仓库 URL 仍必须通过 canonical URL 校验和持久化 allowlist。源码和测试会保存在本地任务数据中，不要提交密钥。

## 验证

```powershell
pytest -q
python -m compileall -q src tests
docker compose config --quiet
git diff --check
```

Docker 细节见 `docker/README.md`，当前产品设计见 `docs/designs/2026-07-19-code-judge-product-redesign.md`。
