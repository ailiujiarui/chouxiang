# 安全与架构整改摘要

## 已实现

- Webhook 使用签名、allowlist、请求体限制和最小权限配置。
- Git 命令不携带凭据，子进程环境会移除已知凭据变量。
- 生产 Webhook 任务通过 hardened Docker 执行：非 root、只读文件系统、网络隔离和进程限制。
- SQLite 控制面持久化任务、事件、租约、重试、取消、截止时间和运行产物；过期租约可恢复，旧租约不能写入终态。
- LangGraph 节点承载真实执行流程；AST 目标由 Issue、路径、traceback 和复杂度共同选择。
- 运行产物有路径校验、大小限制和脱敏；基准测试使用固定 manifest、缓存校验和 Docker 边界。

## 当前边界

- 本地可信子进程模式仍可用于开发；生产 Webhook 使用 hardened Docker。
- 当前实现仅支持 Python；Java、TypeScript、Go、Rust 等后端需要独立设计和基准验证。
- 未执行真实 DeepSeek、真实 GitHub 写操作和真实外部 Docker 基准，因此不作相关成功率声明。
- 不包含生产部署、PR 合并、远程推送或外部 Webhook 重放。

## 验证结果

- 完整测试、编译检查、差异检查和凭据扫描已通过。
- 确定性基准在两次运行中除时间戳和耗时外一致；安全样例成功，不安全导入按预期拒绝。
- 具体测试数量和最新实现证据以 `phase4-reliability-benchmark-dashboard-design.md` 为准。
