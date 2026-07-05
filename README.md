# Refactor Agent

本项目实现 `plan.md` 中的本地闭环 MVP：读取 Python 文件和 Issue 描述，计算 LOC/圈复杂度，调用 DeepSeek 或 mock LLM 生成修复代码，在隔离工作区运行 `pytest`，失败时最多自愈 3 次，成功后输出指标报告并写入 SQLite。

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

## GitHub Webhook Mode

启动网关：

```powershell
$env:GITHUB_WEBHOOK_SECRET="your-webhook-secret"
$env:GITHUB_TOKEN="ghp_..."
$env:DEEPSEEK_API_KEY="sk-..."
refactor-agent serve --host 0.0.0.0 --port 8000
```

Webhook URL 使用 `/webhooks/github` 或 `/webhook/github`。支持 `issues.opened` 和 `issue_comment.created`。

Issue 正文或评论里需要显式写目标文件，测试路径可省略：

```text
target: src/package/module.py
tests: tests
```

常用环境变量：

- `REFACTOR_AGENT_DRY_RUN=true`：只克隆、运行自愈闭环，不 push 分支、不创建 PR。
- `REFACTOR_AGENT_MOCK_LLM=true`：Webhook 模式使用内置 mock LLM。
- `REFACTOR_AGENT_GITHUB_WORKSPACE_ROOT=.github-workspaces`：GitHub 克隆目录。
- `REFACTOR_AGENT_RUN_ROOT=.runs`：沙箱运行目录。
- `REFACTOR_AGENT_MAX_RETRY=3`：最大自愈尝试次数。
