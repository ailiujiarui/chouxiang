# Refactor Agent

## Architecture and Validation

- LangGraph executes the real `prepare -> minimizer -> ast_guard -> pytest -> adversary -> mutation -> judge -> finalize` workflow. `loop` is a compatibility executor over the same node methods and legal routing table.
- LLM output is treated as an untrusted full-file proposal. The system independently computes the AST diff and only writes back Issue/traceback-selected functions, methods, or explicitly referenced top-level statements.
- New imports are denied by default. `--allow-import` and `REFACTOR_AGENT_ALLOWED_IMPORTS` can admit absolute, non-wildcard roots; dangerous imports/calls, removed imports, public API changes, signatures, decorators, and target-external changes remain blocked.
- Both entry points are supported: `refactor-agent ...` and `python -m refactor_agent.cli ...`.
- Webhook mode is fail-closed and requires Docker. Local `subprocess` mode is trusted-code execution, not a security sandbox.
- A webhook can be validated without a public tunnel by posting a GitHub-compatible `issues.opened` JSON body to `/webhooks/github`, with `X-GitHub-Event: issues` and an `X-Hub-Signature-256: sha256=...` HMAC generated from `GITHUB_WEBHOOK_SECRET`.

本项目实现 `plan.md` 中的本地闭环 MVP：读取 Python 文件和 Issue 描述，计算 LOC/圈复杂度，调用 DeepSeek 或 mock LLM 生成修复代码，在隔离工作区运行 `pytest`，失败时最多自愈 3 次，成功后输出指标报告并写入 SQLite。

当前内核已经升级为轻量多 Agent 流程：

- `MinimizerAgent`：调用 DeepSeek/mock LLM 生成极简候选代码。
- `AST guard`：用 Python 原生 `ast` 提取函数签名、Native CC、高复杂度子树，并在进沙箱前拒绝语法错误、危险调用和 public API 删除。
- `AdversaryAgent`：先基于 AST 自动生成边界 pytest 对抗测试，再执行 AST mutation testing，验证候选代码是否经得住额外攻击。
- `Sandbox profiler`：用 `timeit` 记录目标模块导入耗时，并用 `tracemalloc` 记录 pytest 运行峰值内存。
- `JudgeAgent`：用 `Reward = ΔCC * 3 + ΔLOC + MutationKillRate * 10 - RetryCount * 2` 评分，并把轨迹写入 `.runs/<run_id>/trajectory.jsonl`。

## Quick Start

```powershell
python -m pip install -e .[dev]
refactor-agent demo
```

`demo` 默认使用内置 mock LLM，不需要 API Key。真实 DeepSeek 调用需要：

```powershell
$env:DEEPSEEK_API_KEY="sk-..."
refactor-agent run --target path\to\file.py --issue path\to\issue.md --tests path\to\tests
```

可选环境变量：

- `DEEPSEEK_BASE_URL`，默认 `https://api.deepseek.com`
- `DEEPSEEK_MODEL`，默认 `deepseek-chat`

运行产物保存在 `.runs/<run_id>/workspace`，原始文件不会被直接覆盖。
每次运行的自愈轨迹保存在 `.runs/<run_id>/trajectory.jsonl`。

## AST Hotspot Analysis

可以单独查看目标文件的 AST 语义摘要和高复杂度热点子树：

```powershell
refactor-agent ast-hotspots --target src\refactor_agent\orchestrator.py --max-regions 2
```

Issue 中的 qualified name、函数名、traceback 行号和 `path.py:line` 证据优先于复杂度。没有明确证据时才回退到复杂度最高的函数；仅含模块语句的文件不会被猜测修改。显式模块目标使用 `module:<line>:<AST-type>`，例如 `module:1:Assign`。

允许候选新增标准库或项目依赖时，必须显式授权导入根：

```powershell
refactor-agent run --target app.py --issue issue.md --tests tests --allow-import math
```

## Execution Graph

默认后端是 `langgraph`。故障隔离或调试时可切换到调用同一组节点的 `loop` 后端：

```powershell
$env:REFACTOR_AGENT_GRAPH_BACKEND="loop"
refactor-agent demo
refactor-agent state-machine
```

报告、`trajectory.jsonl` 和 Mermaid 状态机均来自实际执行节点轨迹。

## Reproducible Benchmark

The built-in benchmark remains the fast, host-compatible regression suite. The external benchmark is defined by `benchmarks/manifest.toml`, pins eight cases across three public repositories to full commit SHAs, and requires Docker:

```powershell
docker build -f docker\Dockerfile.benchmark -t refactor-agent-benchmark:py312 .
refactor-agent benchmark --manifest benchmarks\manifest.toml --provider mock --output-dir benchmark-results\external
refactor-agent benchmark --manifest benchmarks\manifest.toml --provider deepseek --output-dir benchmark-results\deepseek
refactor-agent benchmark --manifest benchmarks\manifest.toml --provider mock --compare <previous-run-id>
```

External runs use anonymous canonical GitHub clone URLs, a local bare cache under `.benchmark-cache`, exact detached commits, no container network, and a hash-pinned `pytest==9.1.1` toolchain. JSON, Markdown, token use, cost, failure category, and normalized result hashes are persisted to SQLite. Provider keys are never stored in benchmark evidence.

六案例 mock 基准覆盖普通函数、低复杂度命名目标、类方法、模块语句、弱测试对抗自愈和不安全候选拒绝：

```powershell
refactor-agent benchmark --output-dir benchmark-results --run-root .runs\benchmark
```

命令同时生成 `benchmark.json` 和 `benchmark.md`。2026-07-13 本地连续两轮结果在排除时间戳和运行耗时后完全一致：样本数 6，5 个安全案例成功且变异击杀率均为 100%，1 个不安全导入案例按预期拒绝。该结果不是跨仓库成功率或复杂度改善率结论。

## Docker Sandbox

本地 CLI 默认后端是 `subprocess`，仅用于可信代码。Webhook 强制使用无网络、非 root、只读根文件系统、能力清空、PID/CPU/内存受限的 Docker 后端：

```powershell
docker build -f docker\sandbox.Dockerfile -t refactor-agent-sandbox:py312 .
refactor-agent demo --sandbox-backend docker
```

也可以用自动模式，有 Docker daemon 时使用 Docker，否则回退到 subprocess：

```powershell
refactor-agent demo --sandbox-backend auto
```

Webhook 模式可用环境变量：

- `REFACTOR_AGENT_SANDBOX_BACKEND=subprocess|docker|auto`（Webhook 启动时必须为 `docker`）
- `REFACTOR_AGENT_SANDBOX_DOCKER_IMAGE=refactor-agent-sandbox:py312`
- `REFACTOR_AGENT_SANDBOX_MEMORY=256m`
- `REFACTOR_AGENT_SANDBOX_CPUS=1.0`

## Live Demo Arena

## Docker Memory Store

轨迹记忆库可以放进 Docker volume，容器内默认路径是 `/data/refactor_agent.sqlite`：

```powershell
docker compose build refactor-agent
docker compose run --rm refactor-agent demo --timeout 30
docker compose run --rm refactor-agent memories --limit 10
```

迁移本机已有 `.runs/refactor_agent.sqlite` 到 Docker volume：

```powershell
docker compose run --rm --entrypoint sh refactor-agent -lc "cp /workspace/repo/.runs/refactor_agent.sqlite /data/refactor_agent.sqlite"
```

更多说明见 `docker/README.md`。

Built-in demo cases:

```powershell
refactor-agent demo-cases
refactor-agent demo --case leap-year --sandbox-backend auto
refactor-agent demo --case add-maze --sandbox-backend auto
refactor-agent demo --case business-day --sandbox-backend auto
refactor-agent demo --case adversarial-weekend --mock-fail-times 1 --sandbox-backend auto
refactor-agent demo-suite --sandbox-backend auto
refactor-agent state-machine
```

`demo-suite` 会按路演顺序连续运行内置案例，写入同一个 SQLite，并输出中文总战报。跑完后直接打开竞技场就能看到对抗回合和指标图表。

竞技场和运维仪表盘使用可选的 Streamlit 依赖：

```powershell
python -m pip install -e .[dashboard]
refactor-agent dashboard --host 127.0.0.1 --port 8501 --api-url http://127.0.0.1:8000
refactor-agent arena-export --output arena-report.md
```

运维仪表盘包含“任务”“执行过程”“代码变更”和“基准测试”四个页签。读取任务与运行视图无需凭据，通过本地 FastAPI 服务获取数据；仓库白名单、取消和重试操作需要在密码输入框中填写管理员令牌。令牌只保存在 Streamlit 会话状态中，并且只通过管理或控制请求头发送。状态展示使用“中文（原始枚举）”格式，Job ID、Run ID、源码、diff 和日志正文保持原样，便于复制和排障。

“任务”页签顶部可以从 GitHub URL 创建本地简化任务。填写白名单仓库 URL、简化要求、可选分支和目标文件后，任务进入同一个 SQLite Worker 队列；目标文件留空时自动定位，测试路径默认 `tests`。该入口固定为 local-only：即使服务端关闭 `REFACTOR_AGENT_DRY_RUN`，也不会创建分支、推送、创建 PR 或评论 Issue。模型模式由 Worker 的 `REFACTOR_AGENT_MOCK_LLM` 和 `DEEPSEEK_API_KEY` 配置决定，并显示在表单上。

同一页签的“仓库白名单”区域用于查看、添加和移除运行期仓库授权。`REFACTOR_AGENT_ALLOWED_REPOSITORIES` 是部署时的只读基线，仪表盘不能删除；界面新增项写入当前 `REFACTOR_AGENT_DATABASE` 指向的 SQLite。实际白名单是两者并集，空集合表示拒绝所有仓库。移除仪表盘条目会立即阻止新的提交和尚未分派的队列任务，但不会强制终止已经进入克隆或沙箱执行的任务。

使用前必须把仓库加入白名单并以 Docker 后端启动 Worker，例如：

```powershell
$env:GITHUB_WEBHOOK_SECRET="local-webhook-secret"
$env:REFACTOR_AGENT_ADMIN_TOKEN="local-admin-secret"
$env:REFACTOR_AGENT_ALLOWED_REPOSITORIES="owner/repository"
$env:REFACTOR_AGENT_ALLOWED_SENDERS="trusted-github-login"
$env:REFACTOR_AGENT_SANDBOX_BACKEND="docker"
$env:REFACTOR_AGENT_DRY_RUN="true"
$env:REFACTOR_AGENT_MOCK_LLM="false"
$env:DEEPSEEK_API_KEY="<set-in-environment>"
refactor-agent serve --host 127.0.0.1 --port 8000
```

URL 仅接受 `https://github.com/owner/repository`。Admin Token、GitHub Token 和 DeepSeek Key 不得填写到 URL 或简化要求中。

## GitHub Webhook Mode

启动网关：

```powershell
$env:GITHUB_WEBHOOK_SECRET="your-webhook-secret"
$env:GITHUB_TOKEN="ghp_..."
$env:DEEPSEEK_API_KEY="sk-..."
$env:REFACTOR_AGENT_ADMIN_TOKEN="separate-admin-secret"
$env:REFACTOR_AGENT_ALLOWED_REPOSITORIES="owner/repository"
$env:REFACTOR_AGENT_ALLOWED_SENDERS="trusted-github-login"
$env:REFACTOR_AGENT_SANDBOX_BACKEND="docker"
refactor-agent serve --host 0.0.0.0 --port 8000
```

Webhook URL 使用 `/webhooks/github` 或 `/webhook/github`。支持 `issues.opened` 和 `issue_comment.created`。

Issue 正文或评论里可以显式写目标文件，测试路径可省略：

```text
target: src/package/module.py
tests: tests
```

如果省略 `target`，Webhook 会在克隆仓库后自动扫描 Python 文件，并根据 Issue 文本里的文件名、函数名、类名和路径片段定位最可能的源码文件。

常用环境变量：

- `REFACTOR_AGENT_DRY_RUN=true`：只克隆、运行自愈闭环，不 push 分支、不创建 PR。
- `REFACTOR_AGENT_MOCK_LLM=true`：Webhook 模式使用内置 mock LLM。
- `REFACTOR_AGENT_GITHUB_WORKSPACE_ROOT=.github-workspaces`：GitHub 克隆目录。
- `REFACTOR_AGENT_RUN_ROOT=.runs`：沙箱运行目录。
- `REFACTOR_AGENT_MAX_RETRY=3`：最大自愈尝试次数。
- `REFACTOR_AGENT_ALLOWED_REPOSITORIES=owner/repo`：部署控制的只读仓库白名单基线；首次启动且 SQLite 中没有动态条目时必填。
- `REFACTOR_AGENT_ALLOWED_IMPORTS=math,decimal`：Webhook 可新增导入根；Issue 文本不能授予该权限。
- `REFACTOR_AGENT_ALLOWED_SENDERS=login`：允许触发自动改码的 GitHub 用户 allowlist，必填。
- `REFACTOR_AGENT_ADMIN_TOKEN=...`：创建、取消和重试任务时使用的独立 Bearer Token，必填。
- `REFACTOR_AGENT_RETAIN_CHECKOUTS=false`：默认在作业结束后删除 Git clone。
- `REFACTOR_AGENT_JOB_LEASE_SECONDS=300`：持久 worker 租约时间。
- `REFACTOR_AGENT_JOB_MAX_ATTEMPTS=3`：租约恢复最大次数。

Webhook 作业会写入 SQLite。可以用 HTTP 或 CLI 查询：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/jobs
refactor-agent jobs --limit 10
```

## Operations Control Plane

`REFACTOR_AGENT_JOB_DEADLINE_SECONDS` defaults to 900 seconds and accepts 30 through 7200. Local execution commands expose the same limit through `--deadline`.

Read-only endpoints are `/capabilities`, `/jobs`, `/jobs/{id}`, `/jobs/{id}/events`, `/runs`, `/runs/{id}/trajectory`, `/runs/{id}/artifacts/{name}`, `/benchmarks`, and `/benchmarks/{id}`. URL submission, repository allowlist management, and control endpoints require `Authorization: Bearer <Admin Token>`:

```powershell
$headers = @{Authorization="Bearer $env:REFACTOR_AGENT_ADMIN_TOKEN"}
Invoke-RestMethod -Method Post http://127.0.0.1:8000/jobs/<job-id>/cancel -Headers $headers
Invoke-RestMethod -Method Post http://127.0.0.1:8000/jobs/<job-id>/retry -Headers $headers
Invoke-RestMethod http://127.0.0.1:8000/admin/repository-allowlist -Headers $headers
Invoke-RestMethod -Method Post http://127.0.0.1:8000/admin/repository-allowlist -Headers $headers -ContentType "application/json" -Body '{"repository":"owner/repository"}'
Invoke-RestMethod -Method Delete http://127.0.0.1:8000/admin/repository-allowlist/owner/repository -Headers $headers
```

`POST /jobs/url` accepts a canonical GitHub URL, optional ref/target, test path, and refactor request. It stores only canonical repository identity in the durable payload and always executes through the local-only service.

Queued jobs cancel immediately. Running jobs enter `CANCEL_REQUESTED` and stop cooperatively at the next graph or side-effect checkpoint. Failed, cancelled, and timed-out jobs can be retried only when no PR URL exists. Job state changes and append-only events commit in one SQLite transaction; stale lease owners cannot write terminal state.

## Nailong Desktop Skeleton

The desktop pet is an independent package and does not enter the Python refactor workflow. Install the optional PySide6 dependency to launch the transparent pet window:

```powershell
python -m pip install -e .[desktop]
python -m nailong_agent
```

Run the process shell without a GUI for smoke tests or CI:

```powershell
python -m nailong_agent --headless --lock-path .runs/nailong-agent.lock
python -m pytest -q tests/test_nailong_scaffold.py
```

The first scaffold provides the event envelope, bounded event bus, process lock, tray/pet renderer adapter, and headless renderer. Windows activity collectors, classification, personality decisions, and popup policy are separate follow-up modules.
