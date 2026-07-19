# 代码审判助手与 Nailong 桌宠产品重设计

日期：2026-07-19
状态：待批准实施

## 产品目标

产品分两阶段交付，但共享同一个分析核心：

1. **代码审判助手**：输入 Python 代码或 GitHub 仓库 URL，自动完成 AST 分析、多 Agent 对抗化简、验证和人格化报告。
2. **Nailong 桌宠**：具有完整桌宠属性的常驻角色，并把代码审判助手作为核心能力接入陪伴、触发、进度演出和结果互动。

Dashboard 是完整分析工作台；Nailong 不是 Dashboard 的缩小版，也不是托盘通知器。

## 第一阶段：代码审判助手

### 输入

首屏只保留：

- Python 源码或 canonical GitHub 仓库 URL；
- 可选目标文件、ref 和 pytest；
- 审判目标；
- 人格：毒舌 reviewer 或傲娇雌小鬼；
- 开始按钮。

测试是证据增强项，不是使用门槛。仓库 URL 只读 clone，不包含任何远程写能力。

### 统一分析任务

```text
Input Adapter
  -> AST Analyst
  -> Minimizer
  -> Defender
  -> Adversary
  -> Judge
  -> Persona Reporter
```

- **AST Analyst**：解析符号、复杂度、控制流、数据流近似、热点和允许修改区域。
- **Minimizer**：提出候选代码和化简理由。
- **Defender**：检查行为保持、API、边界条件和可维护性风险。
- **Adversary**：生成反例、性质测试和变异攻击。
- **Judge**：根据证据裁决候选，不受人格影响。
- **Persona Reporter**：把结构化裁决渲染为毒舌或傲娇报告，不修改事实。

无论是否提供测试，都必须执行完整的 Agent 辩论；区别只在证据等级和允许给出的结论。

### 证据等级

| 等级 | 输入与执行 | 允许结论 |
| --- | --- | --- |
| `STATIC` | AST、静态安全、Agent 辩论 | 建议候选，未运行验证 |
| `GENERATED_TESTS` | 系统生成测试、对抗测试、变异测试 | 自动推导测试下通过 |
| `USER_TESTS` | 用户提供 pytest + 自动攻击 | 用户测试下验证通过 |
| `REPOSITORY_TESTS` | 仓库原测试 + 自动攻击 | 仓库测试下验证通过 |

系统生成测试不能提升为用户或仓库证据。UI 和报告必须始终显示证据等级。

### 无测试工作流

1. AST Analyst 产出符号和热点。
2. Minimizer 提出候选。
3. Defender 提取可观察行为和风险。
4. Adversary 生成有界 pytest 和性质测试。
5. 原代码与候选在同一个 hardened Docker 边界分别执行生成测试。
6. 运行变异测试并记录生成测试强度。
7. Judge 输出 `STATIC` 或 `GENERATED_TESTS` 结论，不冒充充分验证。

生成测试失败、无法导入或依赖缺失时仍生成审查报告，但不宣称候选可用。

### 有测试与仓库工作流

保留现有 AST Guard、Docker pytest、对抗测试、变异测试、Reward、trajectory 和 artifacts。仓库任务自动发现 pytest 配置和测试路径；用户可以覆盖目标，但不能输入命令、依赖安装脚本或 Docker 参数。

### 模型模式

- 产品模式默认使用已配置的真实 DeepSeek。
- 没有 Key 时明确显示“演示模式”，不能把 deterministic mock 描述成通用能力。
- Mock 只用于 CI、内置 demo 和离线回归。
- `/capabilities` 必须返回 `product_mode=deepseek|demo` 和具体限制。

### 人格报告

人格贯穿报告表现，但不参与技术裁决。报告固定包含：

1. 人格化开场判词；
2. AST 体检与热点；
3. 多 Agent 对抗摘要；
4. 原始与候选代码；
5. LOC、CC、变异击杀率和 Reward；
6. 证据等级与未验证边界；
7. 风险和最终裁决。

傲娇雌小鬼人格需要稳定口吻、称呼、节奏和情绪变化，但只攻击代码结构，不攻击作者身份、外貌、能力或群体属性。

## 第二阶段：Nailong 桌宠

### 不可妥协的桌宠属性

- 透明、置顶、可拖拽的桌面角色；
- 待机、观察、思考、吐槽、得意、困倦、庆祝和担忧等可见状态；
- 动画状态机、气泡和点击反馈；
- 托盘、暂停、安静模式、退出和本地历史清理；
- 冷却、去重、每小时打扰预算、会议/全屏抑制；
- 即使分析服务离线，也能保持本地待机、互动和固定人格回复。

不能把桌宠实现成普通窗口、Dashboard 浮层、托盘菜单或通知组件。

### 与分析核心的关系

```text
Desktop Pet State + User Gesture + Authorized IDE Context
  -> Analysis Request
  -> Shared Control API / Analysis Engine
  -> Progress Events
  -> Pet Animation and Bubble
  -> Short Verdict
  -> Open Full Workbench on Demand
```

- 用户拖入 `.py` 文件、粘贴代码、选择仓库 URL或从受支持 IDE 显式发送上下文。
- 桌宠发起与 Dashboard 相同的分析任务，不复制分析逻辑。
- Agent 进度映射为桌宠状态和动画，例如 AST 扫描为观察、对抗测试为紧张、Judge 通过为得意。
- 桌宠展示短判词和关键指标；完整 diff、证据和报告在工作台打开。
- 分析结果可以影响短期情绪，但不能永久覆盖桌宠人格状态机。

### 隐私

- 默认不采集截图、OCR、剪贴板、完整窗口标题、终端全文或源代码正文。
- 代码只能通过用户粘贴、拖放、文件选择或 IDE 显式授权进入分析。
- 活动采集只用于桌宠陪伴与打扰策略，不自动上传或触发代码审判。
- `private` 和 `blocked` 事件不能进入模型。

## 共享契约

新增核心事件：

- `AnalysisRequested`
- `AnalysisProgressed`
- `AnalysisCompleted`
- `AnalysisFailed`
- `PetStateChanged`
- `PopupDecision`

分析事件包含 task ID、输入类型、阶段、证据等级和脱敏摘要，不通过桌宠 EventBus 传递完整源码或 Token。

## 非目标

- GitHub Webhook 和远程写入。
- 第一阶段支持非 Python 语言。
- 用自动生成测试冒充真实回归测试。
- 让人格文案影响安全校验或 Judge。
- 用桌宠自动监控和上传用户代码。

## 验收

- 任意可解析 Python 代码在无用户测试时也进入完整多 Agent 辩论并输出有证据等级的报告。
- 有测试或仓库任务保留严格安全验证。
- 默认 UI 不再暴露运维控制面作为主要体验。
- DeepSeek 与 demo 模式在 UI 中不可混淆。
- Nailong 具有独立可运行的桌宠状态、动画和交互；分析服务离线时仍可陪伴。
- 桌宠与 Dashboard 对同一 task 展示一致裁决和证据。
