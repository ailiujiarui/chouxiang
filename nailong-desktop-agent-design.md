# Nailong Desktop Agent 设计摘要

## 当前实现

桌面 Agent 是独立于 `refactor_agent` 的可选包。目前提供：

- Pydantic 事件信封和 `ActivityEvent`、`PopupDecision` 等共享模型；
- 有界、可停止、可观测的进程内 EventBus；
- Null/headless renderer 和可选 PySide6 renderer；
- 单实例锁、托盘入口、优雅退出和 `python -m nailong_agent`；
- headless 模式，供 CI 和 smoke test 使用。

## 边界

桌面 Agent 不参与 Python 重构工作流，不直接调用模型，也不采集截图、剪贴板、源代码或认证信息。渲染层只消费结构化的 `PopupDecision`。

## 后续范围

Windows 活动采集、分类、人格决策、弹窗策略和视觉/声音资产尚未实现，必须通过上述事件模型接入，并保持离线可测试。

安装和运行命令见 `README.md`，当前代码行为以 `tests/test_nailong_scaffold.py` 为准。
