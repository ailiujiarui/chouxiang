# 当前安全与架构边界

- 系统只执行本地 Snippet 和只读 GitHub URL 任务。
- GitHub Webhook、branch、commit、push、Pull Request 和 Issue 评论能力已经删除。
- 默认一键启动是只绑定 localhost 的单用户模式，不强制管理员令牌。
- 显式配置 `REFACTOR_AGENT_ADMIN_TOKEN` 后，提交、取消、重试和 allowlist 管理操作必须携带正确 Bearer Token。
- 仓库 URL 始终额外经过 canonical URL 校验和持久化 allowlist 检查。
- Worker 使用 SQLite 事务、租约、heartbeat、取消和 deadline。
- 候选代码通过 AST Guard，并在 hardened Docker sandbox 中验证。
- 运行产物有路径校验、大小限制和凭据脱敏。
- 当前只支持 Python；其他语言需要独立设计和 benchmark。
- 当前不包含生产部署、远程写入或外部事件接收能力。

默认无令牌模式不能暴露到不可信网络。需要跨机器访问或端口转发时，必须显式启用认证，并在外层增加 TLS、网络访问控制和独立密钥管理。
