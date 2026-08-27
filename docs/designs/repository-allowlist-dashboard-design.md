# Repository Allowlist Dashboard Design

状态：当前实现，认证规则由 `docs/designs/2026-07-20-local-no-admin-token.md` 更新。

## 目标

控制 Dashboard/API 可以只读克隆哪些 GitHub 仓库。Allowlist 不授予任何远程写权限。

## 模型

有效 allowlist 是两类记录的并集：

1. `REFACTOR_AGENT_ALLOWED_REPOSITORIES` 环境配置，只读且不能通过 API 删除；
2. SQLite `repository_allowlist` 记录，可通过管理 API 添加和删除。

仓库 identity 规范化为小写 `owner/repository`。空集合表示拒绝所有仓库。

## API

- `GET /admin/repository-allowlist`
- `POST /admin/repository-allowlist`
- `DELETE /admin/repository-allowlist/{owner}/{repository}`

默认 localhost 单用户模式无需令牌。显式配置 `REFACTOR_AGENT_ADMIN_TOKEN` 后，上述接口必须携带正确 Bearer Token。接口只接受 canonical identity 或 `https://github.com/owner/repository`，拒绝其他 host、credential、port、query、fragment、wildcard 和嵌套路径。

## Worker 边界

URL 提交和 Worker clone 前都会重新检查有效 allowlist。删除条目会阻止尚未分派的任务，但不会强制终止已经进入 clone 或 sandbox 的任务；后者由取消和 deadline 控制。

Allowlist 只允许只读 clone。系统不存在 branch、commit、push、Pull Request 或 Issue 评论能力。

## 持久化与审计

- `repository_allowlist` 保存动态条目。
- `repository_allowlist_events` 追加 ADD/REMOVE 事件。
- 添加、容量检查和事件写入使用 `BEGIN IMMEDIATE`。
- 环境记录不可由 API 删除；重复添加和删除不存在记录保持幂等。

## Dashboard

Dashboard 先读取 `/capabilities`。`admin_token_required=false` 时直接读取和管理 allowlist；为 `true` 时显示密码输入，并在控制请求中发送 Token。Token 只保存在当前 Streamlit 会话，不进入 URL、日志、SQLite 或运行产物。
