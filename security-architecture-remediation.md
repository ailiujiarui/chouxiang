# 当前安全与架构边界

- 系统仅执行本地 Snippet 和只读 GitHub URL 任务。
- GitHub Webhook、branch、commit、push、Pull Request 和 Issue 评论能力已经删除。
- API 使用 Admin Token；仓库 URL 额外使用持久化 allowlist。
- Worker 使用 SQLite 事务、租约、heartbeat、取消和 deadline。
- 候选代码通过 AST Guard，并在 hardened Docker sandbox 中验证。
- 运行产物有路径校验、大小限制和凭据脱敏。
- 当前仅支持 Python；其他语言需要独立设计与 benchmark。
- 不包含生产部署、远程写入或外部事件接收。
