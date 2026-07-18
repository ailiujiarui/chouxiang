# Repository Allowlist Dashboard Design

状态：当前实现

## 目标

控制 Dashboard/API 可以只读克隆哪些 GitHub 仓库。Allowlist 不授予任何远程写权限。

## 模型

有效 allowlist 是两类记录的并集：

1. `REFACTOR_AGENT_ALLOWED_REPOSITORIES` 环境配置，只读且不能通过 API 删除；
2. SQLite `repository_allowlist` 记录，可由 Admin API 添加和删除。

仓库 identity 规范化为小写 `owner/repository`。空集合表示拒绝所有仓库。

## API

- `GET /admin/repository-allowlist`
- `POST /admin/repository-allowlist`
- `DELETE /admin/repository-allowlist/{owner}/{repository}`

接口要求 Admin Token。只接受 canonical identity 或 `https://github.com/owner/repository`；拒绝其他 host、credential、port、query、fragment、wildcard 和嵌套路径。

## Worker 边界

URL 提交和 Worker clone 前都重新检查有效 allowlist。删除条目会阻止尚未分派的任务，但不会强制终止已经进入 clone 或 sandbox 的任务；后者由取消和 deadline 控制。

Allowlist 仅允许只读 clone。系统不存在 branch、commit、push、Pull Request 或 Issue 评论能力。

## 持久化与审计

- `repository_allowlist` 保存动态条目。
- `repository_allowlist_events` 追加 ADD/REMOVE 事件。
- 添加、容量检查和事件写入使用 `BEGIN IMMEDIATE`。
- 环境记录不可由 API 删除，重复添加和删除不存在记录保持幂等。

## Dashboard

未提供 Admin Token 时不请求 allowlist API。认证后显示来源、添加时间和可删除性，并支持添加/移除 SQLite 条目。Token 只保存在 Streamlit session state，并只发送到控制请求。
