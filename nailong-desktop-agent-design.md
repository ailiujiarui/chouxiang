# 奶龙桌宠 Agent 架构与任务认领设计

日期：2026-07-17
状态：基础骨架已完成（2026-07-18）；采集、识别、人格和策略业务逻辑仍待分别认领实现

## 1. 产品边界

奶龙桌宠是独立于现有代码重构 Agent 的桌面产品层。它观察经过用户授权的本机活动，将原始信号归一化为有限上下文，识别活动类型，再依据奶龙人格和打扰策略决定是否弹窗以及说什么。

```text
DesktopCollectors
  -> ActivityEvent
  -> ContextAggregator
  -> ActivityClassifier
  -> PersonalityDecisionAgent
  -> PopupPolicy
  -> DesktopPopupRenderer
```

可以复用现有项目的 LLM Provider 抽象思路、Pydantic 数据模型、SQLite 持久化模式和可选 LangGraph 状态机；不复用 Python AST、pytest、变异测试、代码重构 Prompt 和 Streamlit 仪表盘作为桌宠 UI。

## 2. 任务认领表

| 负责人 | 任务包 | 主要交付物 | 输入 | 输出 | 完成标准 |
| --- | --- | --- | --- | --- | --- |
| ccc | 奶龙人格定义 | `NailongPersona` 人格规范、语气词库、禁用表达、情绪状态和示例对话 | 产品定位、活动分类结果 | 版本化人格配置与测试样例 | 同一场景输出风格一致，不攻击用户，不泄露隐私，不产生越权建议 |
| ccc | 人格决策 Agent | `PersonalityDecisionAgent`、人格状态转移、固定回复与可选 LLM 回复协议 | `ActivitySnapshot`、`ActivityClassification`、近期人格状态 | `PersonalityResponseProposal` | 支持纯规则离线运行；LLM 失败时可回退；输出严格结构化，不直接控制 UI |
| 晨西 | 活动识别器 | `ActivityClassifier`、活动分类体系、置信度与证据规则 | 脱敏后的 `ActivitySnapshot` | `ActivityClassification` | 高频场景优先走确定性规则；低置信度才允许调用模型；错误分类可解释、可测试 |
| 星| 弹窗策略 | `PopupPolicy`、冷却时间、安静时段、打扰预算、暂停与紧急抑制 | `PersonalityResponseProposal`、用户状态、最近弹窗历史 | `PopupDecision` | 策略可以独立单测；任何 Agent 都不能绕过策略直接弹窗 |
| 星 | 弹窗规则定义 | 场景到弹窗优先级、展示时长、合并/丢弃规则、敏感场景禁弹规则 | 活动类型、置信度、窗口状态、用户设置 | 规则配置和验收用例 | 开会、全屏、演示、安静模式默认不打扰；重复消息被限流 |
| 待认领 | Windows 活动采集 | 前台窗口、进程、空闲时长和 IDE 状态采集器 | Windows 用户授权 | `ActivityEvent` | 默认不采集屏幕、剪贴板、文件正文；采集失败不影响主进程 |
| 待认领 | 上下文聚合 | `ContextAggregator`、时间窗、去重和脱敏 | `ActivityEvent` 流 | `ActivitySnapshot` | 原始事件有界缓存；发送给模型前完成敏感信息过滤 |
| 待认领 | 桌宠外壳与渲染 | PySide6 托盘、桌宠窗口、弹窗动画和设置页 | `PopupDecision` | 可见桌宠交互 | 渲染层不调用模型；支持暂停、退出、安静模式和历史删除 |
| 待认领 | 存储与集成 | 配置、SQLite 表、模块装配、可选 `PetGraphState` | 各模块结构化结果 | 可恢复的本地状态 | 只保存脱敏摘要；密钥不入库；模块可替换、可端到端测试 |

## 3. 共同接口契约

### 3.1 ActivityEvent

```text
event_id: str
occurred_at: datetime
source: window | process | idle | ide
application_id: str
window_title_summary: str | null
activity_hint: str | null
sensitivity: public | private | blocked
metadata: dict[str, scalar]
```

采集器不得在默认模式中放入截图、OCR、剪贴板、完整源代码、终端全文、密码、Token、SSH 内容或认证文件。

### 3.2 ActivitySnapshot

```text
window_started_at: datetime
window_ended_at: datetime
dominant_application: str | null
normalized_signals: list[str]
idle_seconds: int
is_fullscreen: bool
is_meeting_likely: bool
sensitivity: public | private | blocked
```

### 3.3 ActivityClassification

```text
activity: coding | debugging | reading | writing | meeting | gaming | media | idle | unknown
confidence: float
evidence: list[str]
classifier: rules | llm
```

晨西负责维护分类枚举和识别规则。新增分类必须同步通知 ccc 和弹窗策略负责人，避免下游出现未处理分支。

### 3.4 PersonalityResponseProposal

```text
persona_version: str
emotion: cheerful | curious | concerned | sleepy | celebrating | neutral
message: str
intent: encourage | remind | celebrate | ask | stay_silent
priority: low | normal | high
expires_in_seconds: int
```

ccc 负责该结果的内容质量，但不能决定最终是否展示。

### 3.5 PopupDecision

```text
action: show | defer | drop
reason: str
message: str | null
priority: low | normal | high
display_seconds: int
dedupe_key: str | null
```

弹窗策略是展示前的唯一出口。`DesktopPopupRenderer` 只接受 `action=show` 的结果。

## 4. 分工边界

- ccc 不读取原始桌面事件，只接收脱敏后的活动分类和快照摘要。
- 晨西不生成奶龙文案，也不决定是否弹窗。
- 弹窗策略不改写人格文案；只负责展示、延迟或丢弃。长度超限等展示约束通过结构化校验返回人格决策层重试。
- UI 渲染层不包含分类、人格或策略逻辑，也不能直接调用 LLM。
- 共享模型由集成负责人维护，接口变更必须先更新本文档再改代码。

## 5. 第一版弹窗规则基线

| 场景 | 默认动作 | 规则 |
| --- | --- | --- |
| 用户开会、共享屏幕或演示 | `drop` | 不展示任何主动弹窗 |
| 全屏游戏或全屏视频 | `defer` | 退出全屏后重新检查是否过期 |
| 安静模式或手动暂停 | `drop` | 用户主动恢复前持续生效 |
| 连续编码超过设定时长 | `show` | 普通优先级，提醒休息；同类消息进入冷却 |
| 测试或构建刚成功 | `show` | 普通优先级，短时庆祝；需要可靠事件证据 |
| 测试或构建重复失败 | `show` | 最多一次关切提示，禁止连续催促 |
| 活动识别置信度低 | `drop` | 不为制造互动而猜测用户行为 |
| 相同语义消息重复出现 | `drop` | 使用 `dedupe_key` 去重 |

默认值建议：同类弹窗冷却 30 分钟，每小时最多 2 次主动弹窗，每日最多 12 次；这些值必须由用户设置覆盖。

## 6. 隐私与安全规则

- 截图、OCR、剪贴板、原始代码和会议内容默认关闭，并要求单独显式授权。
- 窗口标题、网页内容、代码和终端输出都视为不可信输入，不能作为系统指令拼入 Prompt。
- 高置信度活动由本地规则识别，不为每个桌面事件调用模型。
- 模型只接收有界、脱敏、结构化摘要；`sensitivity=blocked` 的事件不得进入模型。
- SQLite 只保存脱敏摘要、策略决策和人格状态，不保存原始敏感内容或 API Key。
- 必须提供全局暂停、安静模式、本地历史删除和敏感采集器逐项开关。

## 7. 集成顺序

1. 三位负责人先确认共享数据模型和枚举。
2. 晨西基于固定样例完成规则活动识别器。
3. ccc 基于固定分类结果完成人格规范和离线决策 Agent。
4. 弹窗策略负责人基于固定提案完成策略与规则测试。
5. 集成负责人接入本地事件总线和 SQLite，不启用真实模型。
6. UI 负责人接入 PySide6 渲染，只消费 `PopupDecision`。
7. 通过离线端到端测试后，再单独评审是否开启 DeepSeek 低置信度路径。

## 8. MVP 验收

- 支持编码、调试、阅读、会议、全屏、空闲和未知活动的本地识别。
- 奶龙人格在固定测试集中保持一致语气，并能在模型不可用时完整回退。
- 会议、全屏、安静模式和冷却规则没有绕过路径。
- 桌宠支持托盘暂停、恢复、退出和删除本地历史。
- 默认配置下不采集截图、剪贴板、源代码正文或认证信息。
- 单元测试覆盖分类、人格、弹窗策略和跨模块契约；Windows 集成测试覆盖前台窗口变化和弹窗抑制。
- 完成代码后先进行完整 code review 和自我修复，再另行申请提交、推送或部署许可。

## 9. 待确认事项

- 当前按“你负责弹窗策略和弹窗规则”记录；如果“星”是另一位成员，需要补充其具体任务包。
- 桌宠角色视觉资产、动画规格和声音能力暂未认领，且不进入首个纯逻辑里程碑。
- 第一阶段是否完全禁用真实模型，建议答案为是；先用固定人格回复验证隐私和打扰策略。

## 10. 本次骨架实施范围

本次只建立产品层的可运行边界，不实现真实 Windows 采集、活动识别、奶龙人格或 DeepSeek 调用：

- `nailong_agent.events`：统一的 Pydantic 事件和结果模型。
- `nailong_agent.event_bus`：有界、可停止、可观测的进程内事件总线。
- `nailong_agent.renderer`：渲染协议、无界面测试渲染器和可选 PySide6 桌宠/弹窗渲染器。
- `nailong_agent.app`：进程启动、单实例锁、托盘入口和优雅退出。
- `nailong_agent.__main__`：`python -m nailong_agent` 模块入口。

业务模块后续只能通过上述结构化模型接入；不得让渲染层直接调用模型，也不得将桌宠逻辑并入 `refactor_agent.orchestrator`。
