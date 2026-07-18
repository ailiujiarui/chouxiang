# 删除 GitHub Webhook 自动交付流程设计

日期：2026-07-18
状态：已实现、验证并完成 code review

> 实现说明：为保持旧导入和数据库迁移兼容，控制 API 的主体文件暂保留 `webhook.py` 文件名，生产启动入口已经切换为 `control_api.py`，且该模块不再包含任何 Webhook 路由或解析逻辑。

## 目标

彻底删除由 GitHub Webhook 触发、并可能创建分支、提交、推送、Pull Request 或 Issue 评论的第四条流程。项目只保留：

1. Snippet 只读审查；
2. Snippet 本地验证精简；
3. allowlist GitHub 仓库 URL 的只读克隆与本地验证。

## 删除内容

- 删除 `/webhook/github` 和 `/webhooks/github` 路由。
- 删除 GitHub payload 解析、HMAC 签名校验和 sender allowlist。
- 删除 GitHub API 客户端、branch/commit/push、PR 创建和 Issue 评论代码。
- 删除 `GITHUB_TOKEN`、`GITHUB_WEBHOOK_SECRET`、`REFACTOR_AGENT_ALLOWED_SENDERS`、`REFACTOR_AGENT_DRY_RUN` 等只服务于自动交付的运行配置。
- 删除 `github-url` CLI 中任何可能暗示远程写入的参数或行为；保留只读 URL 本地执行入口。
- 删除相应测试，并将仍有价值的克隆、凭据隔离和路径安全测试迁移到只读仓库测试。

## 保留与重构

- 从 `github.py` 提取只读 `GitRepositoryManager`、canonical clone URL、认证隔离和 checkout cleanup 到 `repository_checkout.py`。
- `LocalRepositoryRefactorService` 继续只读克隆 allowlist 仓库，永不创建 branch、commit、push 或 PR。
- FastAPI 模块改名为 `control_api.py`，只包含 health、capabilities、任务、Snippet、URL、allowlist、run/artifact 和 benchmark API。
- CLI `serve` 改为启动本地控制 API，不再要求 Webhook secret、sender 或 GitHub write token。
- Compose 删除 Webhook/GitHub 写配置，只保留 Admin Token、仓库 allowlist、Docker 和 mock/DeepSeek 配置。

## 遗留数据

SQLite 中可能已有 `GITHUB_WEBHOOK` job。为避免破坏数据库迁移和历史查询：

- schema 和反序列化暂时保留该枚举值；
- 不再提供创建入口；
- Worker 遇到遗留 `GITHUB_WEBHOOK` QUEUED 任务时直接标记 FAILED，原因是该流程已删除；
- Dashboard 仍可读取历史终态记录，但不允许 retry。

## 状态语义

- Snippet VERIFIED 与 GitHub URL 本地验证不再使用模糊的“试运行完成”文案，按 job kind 展示“本地验证完成”。
- `DRY_RUN` 仅作为旧数据库兼容状态保留，不再代表一个可配置的远程交付开关。

## 安全边界

- 代码库中不存在 `git push`、PR 创建或 Issue 评论执行路径。
- GitHub URL 只接受 canonical HTTPS 仓库 URL和 allowlist 仓库；克隆凭据不写入命令、日志或 `.git/config`。
- Snippet 和 URL VERIFIED 继续强制 Docker、AST Guard、pytest、对抗测试、变异测试和 Judge。
- 控制 API 继续使用 Admin Token；只读查询接口保持现有行为。

## 文档与验收

- 更新 README、Docker、主设计和一键启动文档，删除 Webhook/PR/push 说明。
- 搜索生产代码，确保不存在 `git push`、`create_pull_request`、`create_issue_comment`、Webhook 路由或写 token 配置。
- 运行迁移测试，证明旧 Webhook 记录可读但不可执行/重试。
- 运行 URL、Snippet、Worker、Dashboard、API 聚焦测试和完整测试。
- 运行 `compileall`、`git diff --check`、Compose 配置检查和 Docker health smoke。
- 完成 code review 并修复所有 Critical/High 问题；不自动提交、推送或部署。

## 实施记录

- 删除两个 Webhook 路由、签名/payload 解析、sender allowlist 和远程写配置。
- 删除 GitHub API、branch、commit、push、Pull Request 和 Issue 评论实现；`github.py` 仅保留只读 clone、canonical origin、认证隔离、路径校验和 cleanup。
- Worker 只分派 `SNIPPET` 与 `DASHBOARD_URL`；遗留 `GITHUB_WEBHOOK` 任务直接失败且 API 拒绝 retry。
- Dashboard 将本地 `DRY_RUN` 显示为“本地验证完成”，遗留任务显示为“遗留任务（已禁用）”。
- README、Docker、安全、allowlist 和主架构文档已同步为三条本地流程。
- 完整测试：`206 passed`，一个既有 Starlette/httpx 弃用警告；compileall、Compose config 和 `git diff --check` 通过。
- 生产代码扫描未发现远程交付类、push/PR/评论调用或 Webhook 路由。
- Docker smoke：Control API healthy，Dashboard health 200；两个 Webhook 路径不在 OpenAPI，Snippet/URL 路径存在。
- Code review 修复了 local `DRY_RUN` 文案、遗留任务 retry、canonical clone identity 和残留配置/文档问题。
- 未提交或推送。
