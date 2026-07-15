# 仪表盘 GitHub URL 本地简化任务设计

日期：2026-07-15
状态：已实施、验证、自审并于 2026-07-15 获得集成许可

实施证据：2026-07-15 完整测试 `233 passed`；白名单、URL/API/Worker/Dashboard 聚焦测试 `92 passed`；编译、差异格式和 local-only 写路径扫描通过。真实 DeepSeek URL 案例按设计未自动执行。

## 目标

在中文运维仪表盘中增加 GitHub 仓库 URL 提交入口。用户填写仓库 URL、简化要求和可选路径后，系统通过现有 SQLite durable queue 与 Docker Worker 克隆仓库、调用服务端配置的 LLM、运行完整验证链路，并在仪表盘展示候选代码、diff、轨迹和报告。

URL 提交任务固定为本地结果模式：不创建远程分支、不推送、不创建 Pull Request、不评论 Issue。该限制独立于 `REFACTOR_AGENT_DRY_RUN`，不能通过表单或 API 请求覆盖。

## 已确认需求

- 仅允许 `REFACTOR_AGENT_ALLOWED_REPOSITORIES` 白名单中的 GitHub 仓库。
- 只生成本地候选与报告，不产生任何 GitHub 写操作。
- 目标文件路径可留空；留空时使用现有 Issue-aware locator 自动定位。
- 测试路径默认 `tests`，允许手动填写仓库内相对路径。
- LLM 使用 Worker 的服务端配置；仪表盘不接收或保存模型密钥。
- 提交、取消和重试均要求 Admin Token。

## 方案比较

### 方案 A：Streamlit 直接调用 `github-url` CLI

改动最少，但会让 Web UI 直接启动 Git 和 Python 子进程。旧 CLI 还接受任意 Git URL，并默认允许宿主机 subprocess，违反现有 Dashboard 不直接执行命令、Webhook 仅允许 canonical GitHub 仓库及 Docker 隔离的安全边界，因此拒绝。

### 方案 B：新增独立 URL 后台线程

可以避开现有 GitHub Job 模型，但会重复实现队列、租约、超时、取消、重试和事件记录。服务重启恢复和并发状态容易与现有 Worker 不一致，因此拒绝。

### 方案 C：Admin API 入队并由 local-only 服务执行

复用现有 SQLite durable queue、租约、事件、取消、重试、截止时间和 Dashboard 任务视图。Worker 按任务来源分派到独立的只读仓库执行服务；该服务不持有 GitHub 写 API 客户端，也没有 commit/push/PR/comment 路径。采用此方案。

## 用户界面

在“任务”页签顶部增加折叠面板“从 GitHub URL 创建本地简化任务”，包含：

| 字段 | 必填 | 规则 |
| --- | --- | --- |
| 仓库 URL | 是 | 仅 `https://github.com/<owner>/<repo>`，允许末尾 `.git` |
| 分支或标签 | 否 | 留空时使用仓库默认分支 |
| 目标文件 | 否 | 仓库内 Python 相对路径；留空时自动定位 |
| 测试路径 | 是 | 默认 `tests`，仓库内相对路径 |
| 简化要求 | 是 | 1 至 32768 个字符 |

面板显示当前服务端能力：Docker 后端、LangGraph/loop 后端，以及“真实 DeepSeek”或“本地 Mock”模型模式。不得显示 API Key、GitHub Token、Webhook Secret 或 Admin Token 内容。

提交按钮只根据 Admin Token 和服务端 URL submission 能力启用。Streamlit `form` 内字段在提交前不会同步到服务端，因此禁止用 form 内字段值控制 `form_submit_button.disabled`，否则会形成无法首次提交的死锁。

用户点击提交后，当前批次字段值先执行本地必填检查，再调用 API 完成 URL、白名单、路径和 ref 权威校验。成功后显示 Job ID，刷新任务表并选中新任务；校验失败保留用户输入并显示中文错误。页面刷新不得自动重复提交。

## API

新增：

```http
POST /jobs/url
Authorization: Bearer <Admin Token>
Content-Type: application/json
```

请求模型：

```json
{
  "repository_url": "https://github.com/owner/repo",
  "refactor_request": "简化 calculate 函数并保持公开行为不变",
  "branch": null,
  "target_path": null,
  "tests_path": "tests"
}
```

成功返回 `202` 和脱敏后的 Job 记录。错误语义：

- `400`：URL、分支、路径或简化要求格式错误；
- `401`：Admin Token 缺失或无效；
- `403`：仓库不在白名单；
- `409`：相同 Job ID 冲突；
- `503`：Worker 配置不满足 Docker/LLM 执行条件。

新增只读 `GET /capabilities`，仅返回非敏感字段：

```json
{
  "sandbox_backend": "docker",
  "graph_backend": "langgraph",
  "llm_mode": "deepseek",
  "url_submission": true
}
```

Dashboard API 客户端新增 `get_capabilities()` 和 `submit_url_job(...)`。Admin Token 只出现在提交请求的 Authorization header，不进入 URL、日志、SQLite 事件或 Streamlit 持久化状态。

## 输入验证

URL 解析使用结构化 URL API，不使用字符串切片：

- scheme 必须为 `https`；
- hostname 必须严格等于 `github.com`；
- 禁止 username、password、任何显式端口、query 和 fragment；
- path 必须严格包含 owner/repo 两段，可选 `.git` 后缀；
- owner/repo 必须匹配现有严格名称语法；
- 服务端从解析结果生成 canonical `https://github.com/<owner>/<repo>.git`，不保存或执行用户原始 URL。

仓库身份转换为小写后检查 `REFACTOR_AGENT_ALLOWED_REPOSITORIES`。目标路径和测试路径使用现有 `normalize_repo_path`，拒绝绝对路径、`..`、空路径和后续 symlink escape。目标文件非空时必须以 `.py` 结尾。

分支或标签最多 200 字符，仅允许 Git ref 安全字符，并拒绝前导 `-`、`..`、`//`、`@{`、控制字符、空白、反斜杠和 `.lock` 结尾。

## Durable Job 模型

将现有 Job 输入扩展为显式来源：

- `GITHUB_WEBHOOK`：现有签名 Webhook 任务，可按服务端配置发布变更；
- `DASHBOARD_URL`：Admin API 创建的 local-only 任务，永远禁止发布。

Job payload 新增 `job_kind`。URL 任务的 `issue_number` 为 `null`，`issue_title` 使用固定的“Dashboard URL 本地简化任务”，`delivery_id` 使用服务端生成的 `dashboard:<uuid>`，`default_branch` 可为空。

SQLite `github_jobs` 增加 `job_kind`，并允许 `issue_number` 为空。现有数据迁移为 `GITHUB_WEBHOOK`；“同仓库同 Issue 仅一个活动任务”的唯一约束仅应用于非空 Issue 编号。URL 提交每次生成独立 Job，不依赖客户端幂等键。

公开 Job 响应继续排除 `payload_json`。任务表新增“来源”列，URL 任务的 Issue 编号显示 `-`。

## Worker 与执行边界

Worker 根据 `job_kind` 分派：

- `GITHUB_WEBHOOK` -> 现有 `GitHubAutomationService`；
- `DASHBOARD_URL` -> 新增 `LocalRepositoryRefactorService`。

`LocalRepositoryRefactorService` 的职责限定为：

1. 使用从 `repo_full_name` 推导的 canonical GitHub URL 克隆白名单仓库；
2. 可使用现有临时 AskPass 完成私有仓库只读克隆，但不把 Token 放进 URL 或 checkout；
3. 不创建本地发布分支，不写回 checkout，不调用 GitHub API；
4. 解析目标文件和测试路径，调用现有 `RefactorOrchestrator`；
5. 将候选、diff、日志、轨迹和报告写入现有 run root；
6. 成功状态记录为 `DRY_RUN`，失败保持 `FAILED`；
7. 无论成功、失败、取消或超时都清理克隆 checkout，除非显式本地 debug 保留配置已启用。

该服务不接收 `GitHubApiClient`，不暴露 commit、push、PR 或 comment 方法。这样 local-only 约束由依赖边界保证，而不是依赖一个容易被忽略的布尔判断。

Docker、总截止时间、协作取消、租约心跳、重试上限、AST 守卫和产物脱敏继续使用现有实现。测试容器仍不得接收 GitHub、DeepSeek、Webhook 或 Admin 凭据。

## 错误处理

- API 校验失败不创建 Job 或事件。
- 克隆失败、分支不存在、自动定位失败和测试路径不存在进入 `FAILED`，错误经过现有脱敏和长度限制。
- URL 任务取消与手动重试沿用现有状态机。
- Worker 重启后 URL 任务与 Webhook 任务使用相同租约恢复逻辑。
- 模型不可用或 Docker 不可用时拒绝提交或快速失败，不在 Dashboard 中无限排队。
- URL 任务即使服务端 `dry_run=false` 也不得进入 GitHub 写路径。

## 测试与验收

### API 与验证

- 接受 canonical GitHub HTTPS URL 和可选 `.git`。
- 拒绝 HTTP、非 GitHub host、子域名、凭据 URL、端口、query、fragment、多余 path 和畸形名称。
- 拒绝非白名单仓库、路径穿越、非 Python 目标、非法 ref 和超长请求。
- 证明缺失/错误 Admin Token 无法创建任务。
- 证明响应、事件和 SQLite 公开字段不包含 Token 或原始带凭据 URL。

### Worker 与安全

- URL Job 通过同一队列被租约、取消、重试和恢复。
- local-only 服务调用 clone 和 orchestrator，但永不调用 branch、write-back、commit、push、PR 或 Issue comment。
- 即使 `dry_run=false` 且配置了 GitHub Token，URL Job 仍只产生 `DRY_RUN` 本地结果。
- 私有只读克隆使用临时 AskPass，`.git/config` 不含凭据，测试环境不含任何宿主机密钥。
- checkout 在成功、失败、取消和超时后均被清理。

### Dashboard

- AppTest 覆盖表单字段、默认值、Admin Token 禁用状态、中文错误和成功后的 Job ID。
- 回归测试证明填写 form 字段前提交按钮在 Admin Token 与服务能力有效时可点击，并在提交批次中校验必填值，避免 form 状态同步死锁。
- API 客户端只在提交请求发送 Admin header。
- 任务表正确显示来源，URL Job 的 Issue 编号为 `-`。
- 提交后可以在执行、代码变更和任务事件视图查看结果。

### 最终门禁

- Dashboard/API/Worker/GitHub 聚焦测试通过。
- 完整 `pytest -q` 通过。
- Docker local-only mock 案例完成且无网络、无 GitHub 写操作。
- 使用真实 DeepSeek 至少运行一个白名单 URL 案例前，需要单独验收许可；不在实现阶段自动执行。
- `git diff --check`、编译检查和凭据扫描通过。
- 完整代码审查修复所有 Critical 和 High 问题后，才允许请求提交或推送许可。

## 文档同步

实施时更新 `README.md`、`phase4-reliability-benchmark-dashboard-design.md` 和实施记录，明确：

- Dashboard URL 任务固定 local-only；
- 仓库 URL 与目标/测试路径规则；
- Worker 的模型模式由服务端配置决定；
- 如何将仓库加入白名单并从仪表盘提交；
- 候选和报告的位置，以及不会自动推送的边界。

## 非目标

- 不允许任意 Git host、SSH URL、本地路径或上传压缩包。
- 不允许用户在表单中输入 API Key、GitHub Token、测试命令或 Docker 参数。
- 不允许 URL 任务创建分支、提交、推送、PR 或评论。
- 不修改旧 `github-url` CLI 的行为；其安全收敛另行设计。
- 不提交、推送、合并或部署本次改动，除非后续获得单独许可。
