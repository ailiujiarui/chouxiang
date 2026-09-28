# 奶龙主动说话可靠性修复设计

日期：2026-08-08
状态：已实施，code review 通过

## 真实故障证据

当前运行状态证明采集与设置都正常：

- 活动采集授权和 DeepSeek 脱敏推理授权均已开启；
- 未暂停、未开启免打扰，今日弹窗预算剩余 12 次；
- `pet_activity_events` 已写入 12 条最小化事件；
- `consumed_personality_events`、`notification_intents` 和人格活动窗口均为 0。

代码中 `ActivityEventAggregator.flush()` 只有测试调用。真实桌宠只在下一条事件跨越 60 秒
窗口边界时才结算上一窗口；如果用户停止切换窗口，最后一个窗口永远不会进入识别、
人格和通知链路。此外，当前前台事件大多归类为 `other/unknown`，而人格策略将
`unknown`、`idle` 和 `coding` 全部设为 `stay_silent`。

## 目标

- 活动窗口到期后即使没有新事件，也能在可预测时间内进入人格决策。
- Codex、Cursor 等常见开发工具能归入安全的 `code/ide` 类别，不保存原始进程路径。
- 中度傲娇人格在持续编码和长时间待机时偶尔说话，而不是永久沉默。
- 保留现有冷却、每日预算、暂停、免打扰、会议和隐私优先级。
- 不增加 TTS；“说话”仍指奶龙上方的文字气泡。

## 到期窗口调度

### Aggregator

为 `ActivityEventAggregator` 增加 `flush_due(now)`：

- 只有当前窗口存在、包含事件并且 `now >= window_ended_at` 时才返回窗口；
- 未到期时返回 `None`，不得提前缩短 60 秒聚合窗口；
- 保留现有 `flush()` 作为显式测试/关闭辅助接口。

### Orchestrator lifecycle

`ActivityPersonalityOrchestrator` 增加 `start()` / `stop()`：

- 后台调度线程每 5 秒调用一次到期检查；
- `start()` 和 `stop()` 幂等，停止使用 `Event.wait()`，不得使用不可中断 sleep；
- 聚合器访问由 `Lock` 保护，EventBus 线程和调度线程不会并发修改窗口；
- 仅在锁内执行 ingest/flush，识别、人格决策和 SQLite 通知写入在锁外执行；
- 单次分类失败不得终止调度线程，也不得绕过隐私边界。

`DesktopProcess` 在 EventBus 启动后、活动采集器启动前启动 orchestrator；关闭时先停止
采集器，再停止 orchestrator，最后停止 EventBus。注入的旧 orchestrator 没有生命周期
方法时仍保持兼容。

## 应用识别

只扩展本地规范化映射，不保存原始标题或路径：

- `codex`、`cursor`、`windsurf`、`code`、`zed` -> `code`；
- `pycharm64`、`idea64`、`devenv` -> `ide`；
- Windows IDE 临时提示识别相同的开发工具进程名。

不认识的应用继续归为 `other`，不得为了多说话而猜测敏感窗口。

## 中度傲娇说话策略

- `STANDARD` 强度下，`coding` 从 `stay_silent` 改为 `encourage`，使用简短中度傲娇文案。
- `idle` 从 `stay_silent` 改为 `remind`，只在现有 5 分钟系统 idle 门槛之后触发。
- `LOW` 和 `HIGH` 提供同一事实语义但强度不同的本地备用文案。
- `unknown`、会议、娱乐和敏感活动继续保持静默。
- 低于人格响应置信度阈值的活动继续静默；不因用户要求“多说话”而放宽可信度。
- 所有主动气泡仍经过 5–15 分钟普通冷却、30 秒最小弹窗间隔、每日 12 次预算、
  暂停和免打扰策略。测试/编译终态沿用原有优先级。

## 测试与验收

- 单事件窗口在没有后续事件时，于窗口到期后自动生成一条人格通知。
- 未到期窗口不会提前生成通知；重复 tick 不会重复通知。
- orchestrator 重复启动/停止不产生重复线程，停止后不再处理窗口。
- EventBus ingest 与定时 flush 并发时不丢事件、不重复结算。
- Codex/Cursor/IDE 映射只输出标准类别，不泄露路径或窗口标题。
- 中度傲娇 `coding` 和 `idle` 生成气泡；`unknown`、会议和低置信活动保持静默。
- 真实桌宠在启用授权后能从活动事件生成通知意图并显示气泡。
- 桌宠聚焦测试、完整 pytest、compileall 和 `git diff --check` 通过。

## 非目标

- 不增加语音合成、录音或音频播放。
- 不缩短现有隐私 idle 门槛、冷却时间或每日弹窗预算。
- 不采集源代码、窗口标题、文件路径、终端正文、剪贴板、截图或 OCR。
- 不允许每个桌面事件调用 DeepSeek；只有聚合后的低置信公开窗口可进入现有远程分类。

## 实施与复核结果

- 已增加到期窗口调度；无后续桌面事件时，完整 60 秒窗口仍会进入识别与人格决策。
- 调度器支持幂等启停、并发互斥和异常后继续运行；桌面进程按设计管理其生命周期。
- Codex、Cursor、Windsurf、Zed、Visual Studio 和 JetBrains 工具只映射到安全应用类别。
- 中度人格对高置信编码和长时间 idle 生成文字气泡；未知、会议、娱乐和低置信活动保持静默。
- 完整 code review 已修复停止期间潜在重复调度线程问题，并保留失败日志。
- 聚焦回归测试 `104 passed`；完整测试 `591 passed, 11 skipped`；`compileall` 和 `git diff --check` 通过。
