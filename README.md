# Refactor Agent

## Architecture and Validation

- The default control plane is a real LangGraph state graph: `MINIMIZER -> DEFENDER -> ADVERSARY -> JUDGE`. Set `REFACTOR_AGENT_GRAPH_BACKEND=loop` for the deterministic fallback.
- LLM output is treated as an untrusted full-file proposal. The system independently computes the AST diff and only writes back selected hotspot functions or methods.
- Imports, public symbols, signatures, decorators, class structure, and non-target functions must remain unchanged. Violations are rejected before sandbox execution and become retry feedback.
- Both entry points are supported: `refactor-agent ...` and `python -m refactor_agent.cli ...`.
- On Windows, start Docker Desktop and wait for `docker version` to report a Server version before selecting `--sandbox-backend docker`.
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

同样的热点信息会被注入 DeepSeek prompt，让 Agent 优先处理复杂度最高的函数/方法，而不是盲目重写整个文件。

## Docker Sandbox

默认沙箱后端是 `subprocess`。如果要启用无网络、限 CPU/内存的 Docker 后端，先构建镜像：

```powershell
docker build -f docker\sandbox.Dockerfile -t refactor-agent-sandbox:py312 .
refactor-agent demo --sandbox-backend docker
```

也可以用自动模式，有 Docker daemon 时使用 Docker，否则回退到 subprocess：

```powershell
refactor-agent demo --sandbox-backend auto
```

Webhook 模式可用环境变量：

- `REFACTOR_AGENT_SANDBOX_BACKEND=subprocess|docker|auto`
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

The arena is an optional Streamlit extra:

```powershell
python -m pip install -e .[dashboard]
refactor-agent dashboard --host 127.0.0.1 --port 8501
refactor-agent arena-export --output arena-report.md
```

## GitHub Webhook Mode

启动网关：

```powershell
$env:GITHUB_WEBHOOK_SECRET="your-webhook-secret"
$env:GITHUB_TOKEN="ghp_..."
$env:DEEPSEEK_API_KEY="sk-..."
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

Webhook 作业会写入 SQLite。可以用 HTTP 或 CLI 查询：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/jobs
refactor-agent jobs --limit 10
```
