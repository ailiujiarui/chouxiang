# 本地可靠性、基准测试与 Dashboard 设计

日期：2026-07-18
状态：当前实现

## 系统边界

系统是本地 Python 审查与安全精简工具，只接受 Snippet 或用户主动提交的 GitHub 仓库 URL。GitHub Webhook 自动交付流程已删除。

```text
Snippet / allowlist URL
  -> Admin API
  -> SQLite queue and lease
  -> Local Worker
  -> Docker sandbox
  -> artifacts and Dashboard
```

不存在 branch、commit、push、Pull Request 或 Issue 评论路径。

## 控制面

SQLite 保存任务、事件、租约、截止时间、运行、benchmark 和 allowlist。任务状态包括：

- `QUEUED`
- `RUNNING`
- `CANCEL_REQUESTED`
- `CANCELLED`
- `TIMED_OUT`
- `SUCCESS`
- `FAILED`
- `DRY_RUN`（旧 schema 兼容；本地任务显示为“本地验证完成”）

状态变更与事件在同一事务提交。Worker 使用租约和 heartbeat；失去租约的 Worker 不能写终态。取消和 deadline 在图节点、clone 和 sandbox 边界协作生效。

旧数据库的 `GITHUB_WEBHOOK` 任务保持可读，但 Worker 直接标记失败，API 拒绝 retry。

## Snippet

### REVIEW

- 只执行语法、AST、复杂度和静态安全分析。
- 不执行代码、pytest、对抗测试或变异测试。
- 保存 `REVIEWED` 报告，不产生 Reward。

### VERIFIED_REFACTOR

- 输入源码、pytest、要求和人格。
- 固定物化为 `snippet.py` 与 `test_snippet.py`。
- 执行完整 LangGraph、AST Guard、pytest、对抗测试、变异测试和 Judge。
- 结果仅保存到本地。

## GitHub URL

- API 只接受 canonical `https://github.com/owner/repository`。
- 仓库必须在环境或 SQLite allowlist 中。
- Worker 在 clone 前重新检查 allowlist。
- clone origin 必须保持 canonical URL，认证信息不写入命令、日志或 `.git/config`。
- checkout 只作为输入；候选写入独立运行 workspace，结束后按配置清理 checkout。

## 安全执行

- API Worker 强制 Docker backend。
- sandbox 禁用网络，使用非 root、只读根文件系统、capability 清空、`no-new-privileges` 和资源限制。
- AST Guard 拒绝危险调用、未授权 import、公开 API 变化和目标区域外修改。
- 日志、事件和产物有大小限制、路径检查与凭据脱敏。

## Dashboard

Dashboard 是薄 API 客户端，不直接访问 SQLite 或执行 Git/Docker。界面包含：

1. 任务、Snippet/URL 提交、allowlist、取消和重试；
2. 节点轨迹与日志；
3. 原始代码、候选、diff 和 AST 证据；
4. benchmark 结果与比较。

Snippet 和 URL 的 `DRY_RUN` 显示为“本地验证完成”。旧 Webhook 记录显示为“遗留任务（已禁用）”，不能取消或重试。

## Benchmark

外部 benchmark 使用固定 manifest、完整 commit SHA、匿名 canonical clone、缓存 hash 和 Docker-only runner。结果保存 provider、model、token、cost、失败分类、指标和 normalized hash。真实 provider 结果不能由 mock 结果替代声明。

## API

只读：`/health`、`/capabilities`、`/jobs`、`/runs`、trajectory、artifacts 和 benchmarks。

Admin：`/jobs/snippet`、`/jobs/url`、cancel、retry 和 repository allowlist。

API 不提供 `/webhook/github` 或 `/webhooks/github`。

## 验收

- Snippet REVIEW 永不执行代码。
- VERIFIED 与 URL 使用 Docker 和完整验证链。
- allowlist、body/path limit、租约、取消、deadline 和 artifact 防逃逸测试通过。
- 生产代码扫描不存在 GitHub 写操作或 Webhook 路由。
- 完整测试、compileall、Compose config、Docker health 和 `git diff --check` 通过。
