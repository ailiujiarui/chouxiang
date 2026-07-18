# 早期方案存档

本文件记录项目早期的 ReAct/LLM 重构设想，仅供历史参考，不代表当前实现。

当前实现以 `README.md` 和 `phase4-reliability-benchmark-dashboard-design.md` 为准：执行图、受控 AST 定位、SQLite 控制面、Docker 隔离、基准测试和 Dashboard 均已按这些文档实现。

早期方案中关于直接调用 GitHub 创建 PR、固定 SQLite 表结构、仅依赖本地 pytest 沙箱以及按复杂度单独选择 AST 目标的描述已经废弃。
