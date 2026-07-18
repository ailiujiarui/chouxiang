# 文档精简设计

## 目标

减少重复和过时说明，使 README、主架构设计和专项设计成为当前实现的唯一主要入口。

## 保留

- `README.md`：安装、运行和用户可见功能。
- `phase4-reliability-benchmark-dashboard-design.md`：可靠性、基准测试和 Dashboard 主设计及实现证据。
- `repository-allowlist-dashboard-design.md`：allowlist 的独立策略和 API 边界。
- `docker/README.md`：Docker 运行说明。

## 合并或压缩

- 将 `security-remediation.md` 的已实现安全措施和 `architecture-remediation.md` 的当前架构边界合并为 `security-architecture-remediation.md`。
- 将 `plan.md` 改为历史方案说明，删除与当前实现冲突的操作细节。
- 将 Nailong 设计压缩为当前 scaffold 能力和明确的后续范围。

## 删除

- 已完成且与主设计重复的 superpowers 实施计划和中文化 spec。
- 已实现且已被主设计覆盖的 Dashboard URL 提交设计。

## 验证

- 所有 README 和设计文档中的路径、命令和功能描述与源码及测试一致。
- 不修改源代码、测试、配置或部署文件。
