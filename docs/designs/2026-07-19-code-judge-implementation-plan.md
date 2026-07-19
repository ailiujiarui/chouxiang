# 代码审判助手与 Nailong 桌宠实施计划

日期：2026-07-19
状态：待批准实施

## 阶段一：核心产品闭环

### 1. 统一输入与证据模型

- 新增 `AnalysisRequest`、`AnalysisMode`、`EvidenceLevel` 和 `AnalysisResult`。
- 统一 Snippet、文件和仓库 URL adapter。
- 将测试改为可选输入。
- 保留旧任务读取迁移，但新 UI 只使用分析任务语义。

### 2. 无测试多 Agent 工作流

- REVIEW 不再走静态报告捷径。
- AST Analyst、Minimizer、Defender、Adversary 和 Judge 全部执行。
- Adversary 生成有界测试；原始与候选在 Docker 中对照运行。
- 失败时降级证据等级，不伪造成功。

### 3. 有测试和仓库工作流

- 复用 AST Guard、pytest、变异测试和 artifacts。
- 仓库自动发现测试配置与目标文件。
- 统一本地完成状态，移除产品层 `DRY_RUN` 术语。

### 4. 人格报告引擎

- 从 LLM `insult_review` 单字段升级为结构化 `PersonaReport`。
- 定义 STRICT 与 TSUNDERE 模板、词汇边界、长度和 fallback。
- 人格只消费 Judge 事实，不修改候选或 Reward。
- Mock 也通过同一 renderer，避免追加一句固定话术。

### 5. Dashboard 产品重构

- 第一屏改为代码/URL、测试、目标、人格和开始按钮。
- 同屏展示 Agent 对抗进度、候选 diff、证据等级和最终报告。
- 任务运维、allowlist、benchmark 移入次级管理页。
- 明确 DeepSeek/演示模式，不允许 mock 冒充真实能力。

### 6. 验证与 review

- 覆盖无测试、生成测试、用户测试、仓库测试四类证据。
- 覆盖人格不影响技术结果。
- 真实 DeepSeek smoke 需单独使用用户授权的 Key。
- 完整测试、Docker smoke、UI AppTest、凭据扫描和 code review。

## 阶段二：Nailong 桌宠

### 7. 桌宠状态与资产协议

- 定义 `PetState`、动作、情绪、动画 clip、气泡和资源 manifest。
- 实现待机、思考、吐槽、得意、困倦、庆祝和担忧状态机。
- 保持无分析服务时的离线互动。

### 8. 桌宠交互外壳

- PySide6 透明置顶窗口、拖拽、点击和右键交互。
- 托盘暂停、安静模式、退出和历史清理。
- 多显示器定位、DPI、任务栏和全屏抑制。

### 9. 分析助手桥接

- 文件拖放、粘贴、文件选择和仓库 URL 输入。
- 通过共享 API 创建分析 task，订阅进度事件。
- Agent 阶段映射为动画，完成后显示短判词并可打开工作台。
- EventBus 只传 task metadata，不传完整源码。

### 10. 活动与打扰策略

- 本地规则识别 coding、debugging、meeting、fullscreen、idle。
- 冷却、去重、安静时段和每小时预算。
- 活动识别不能自动读取或提交代码。

### 11. 桌宠验收

- 桌宠属性、动画和互动可以独立演示。
- 分析服务离线时不崩溃并保持陪伴。
- 分析进度与 Dashboard task 一致。
- 隐私、会议/全屏抑制和本地历史删除测试通过。

## 实施纪律

- 每个阶段先更新对应设计，再写测试和代码。
- 第一阶段通过完整 code review 后，才开始第二阶段。
- 不在未授权情况下调用真实模型、提交、推送或部署。
