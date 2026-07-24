# Nailong 隐私接口契约

> 2026-07-24 更新：持久化活动模型、原始信号边界、事件去重和时间窗口聚合以
> `2026-07-24-unified-activity-event.md` 为准；本文的授权与远程访问规则继续有效。

日期：2026-07-21  
状态：桌宠活动采集、存储与远程推理接入的实现契约

## 范围与边界

本契约约束奶龙桌宠的活动采集、活动记录和低置信度远程推理路径。
它不改变 `refactor_agent` 既有的代码审判 API，也不允许桌宠通过活动识别自动读取、提交或上传源代码。

默认状态为零采集、零远程上传。活动陪伴授权和代码审判请求是两条独立授权路径。

## 已实现接口

### `PrivacyConsent`

位置：`src/nailong_agent/privacy.py`

```python
@dataclass(frozen=True)
class PrivacyConsent:
    activity_collection_enabled: bool = False
    remote_inference_enabled: bool = False
    decision_recorded: bool = True

    @classmethod
    def unanswered(cls) -> PrivacyConsent: ...
```

- `activity_collection_enabled`：是否允许本机有限活动信号进入桌宠流程；
- `remote_inference_enabled`：是否允许已脱敏的活动摘要发送到远程模型；
- `decision_recorded`：是否已完成首次授权选择。首次启动通过 `unanswered()` 表示未选择；
- `remote_inference_enabled=True` 不应由默认值或环境变量隐式开启，必须来自明确用户选择。

### `PrivacyPolicy`

位置：`src/nailong_agent/privacy.py`

```python
class PrivacyPolicy:
    def admit_activity(self, event: ActivityEvent) -> CollectionDecision: ...
    def prepare_remote_summary(self, event: ActivityEvent) -> str | None: ...
    def redact_text_for_remote(self, value: str) -> str: ...
```

`admit_activity()` 是所有采集器的强制入口。它会拒绝以下事件：

- 未授权、`private` 或 `blocked` 事件；
- 会议窗口或 `is_meeting_likely=True` 信号；
- 密码、Token、Secret、SSH、Auth、`.env`、私钥等敏感标题、路径或元数据。

当事件被允许时，输出的 `CollectionDecision.event` 已执行数据最小化：

- `window_title_summary` 必为 `None`；
- `activity_hint` 必为 `None`；
- 仅保留 `idle_seconds`、`is_fullscreen`、`is_meeting_likely` 三个元数据键；
- `application_id` 被归一化为应用类别，例如 `Code.exe` 变为 `code`。

`prepare_remote_summary()` 在没有单独远程授权时返回 `None`；允许时仅返回最小化后的固定格式摘要，不能返回原始标题、代码、终端文本、截图、OCR 或剪贴板内容。

### `CollectionDecision`

位置：`src/nailong_agent/privacy.py`

```python
@dataclass(frozen=True)
class CollectionDecision:
    allowed: bool
    reason: str
    event: ActivityEvent | None = None
```

调用方只能在 `allowed is True` 且 `event is not None` 时继续发布、持久化或分类。`reason` 可用于本地调试和审计，不得携带原始敏感内容。

### `PrivacyStore`

位置：`src/nailong_agent/privacy_store.py`

```python
class PrivacyStore:
    def load_consent(self) -> PrivacyConsent | None: ...
    def save_consent(self, consent: PrivacyConsent) -> None: ...
    def append_minimized_activity(self, event: ActivityEvent) -> None: ...
    def clear_activity_history(self) -> int: ...
    def activity_count(self) -> int: ...
```

该 Store 使用独立 SQLite 数据库，默认由 `DesktopProcess` 放在与锁文件相同目录的 `nailong_privacy.sqlite` 中。

- `append_minimized_activity()` 会再次拒绝未经最小化的事件；
- 数据库不含窗口标题、原始代码、剪贴板、截图、OCR、终端正文或密钥列；
- `clear_activity_history()` 只删除 `pet_activity_events`，不删除授权选择，也不影响 `refactor_agent` 的运行记录。

### `DesktopProcess` 与 Renderer 扩展

位置：`src/nailong_agent/app.py`、`src/nailong_agent/renderer.py`

```python
DesktopProcess(..., privacy_store: PrivacyStore | None = None)

class PrivacyControlsRenderer(Protocol):
    def request_privacy_consent(self) -> PrivacyConsent | None: ...
    def configure_privacy_controls(
        self, *, on_clear_activity_history: Callable[[], int]
    ) -> None: ...
```

`privacy_store` 是新增的可选依赖注入参数，便于测试和其他进程使用不同数据库。

`PrivacyControlsRenderer` 是可选扩展，而不是对既有 `PopupRenderer` 的破坏性修改：

- 实现扩展的 PySide6 Renderer 会显示首次授权对话框，并提供托盘“删除本地活动记录”；
- 旧 Renderer 不实现这些方法仍可启动；此时系统保存“拒绝采集”的选择，保持 fail-closed。

## 活动采集器接入规范

活动采集器不得直接调用 `EventBus.publish()` 或 `PrivacyStore.append_minimized_activity()`。

```python
raw_event = ActivityEvent(
    source="window",
    application_id=process_name,
    window_title_summary=raw_window_title,
    metadata=raw_signals,
)

decision = privacy_policy.admit_activity(raw_event)
if decision.allowed and decision.event is not None:
    privacy_store.append_minimized_activity(decision.event)
    event_bus.publish(decision.event.envelope())
```

采集器可以在内存中读取原始窗口标题以完成敏感判断，但不得记录、打印、广播或上传该标题。

## 后续 DeepSeek 对接规范

当前仓库尚未实现桌宠的远程推理客户端。现有 `refactor_agent.llm.RefactorClient` 是代码重构接口，参数包含代码和重构 Prompt，不能直接用于桌宠。

后续低置信度活动识别应新增桌宠专用接口：

```python
class PetInferenceClient(Protocol):
    def infer(self, summary: str) -> ActivityClassification: ...
```

调用顺序固定如下：

```python
summary = privacy_policy.prepare_remote_summary(raw_event)
if summary is not None:
    classification = pet_inference_client.infer(summary)
```

- `summary is None` 时不得调用任何远程客户端；
- 客户端只能接收 `summary`，不得扩展为接收 `ActivityEvent`、窗口标题、源代码或任意 Prompt；
- DeepSeek 的连接配置、Provider 选择和错误处理可以参考 `refactor_agent.llm.DeepSeekClient`，但必须使用桌宠专用 Prompt；
- 在发送前可对固定文案调用 `redact_text_for_remote()` 做额外防护，但这不能替代 `prepare_remote_summary()`。

## 验证与兼容性

相关测试：

```powershell
python -m pytest tests/test_nailong_scaffold.py tests/test_nailong_privacy.py -q
python -m compileall -q src tests
git diff --check
```

当前兼容性承诺：

- 保留 `ActivityEvent`、`EventEnvelope`、`EventBus` 与 `PopupRenderer` 的既有名称和行为；
- 新增隐私能力仅通过 `nailong_agent` 模块暴露；
- 不改动既有 FastAPI、Dashboard、SQLiteRunStore 或代码审判 LLM 接口；
- 未实现隐私扩展接口的第三方 Renderer 继续可用，且默认不采集。

## 未来迁移规划

当前隐私接口位于 `nailong_agent`，用于支撑桌宠活动采集与远程推理。

若未来扩展为整个 Refactor Agent 的统一安全与隐私框架，计划将其中具有通用性的能力迁移到共享模块，例如：
refactor_agent/privacy/
    consent.py
    redaction.py
    remote_gate.py

其中：

- `PrivacyConsent`：迁移为项目级授权模型；
- `redact_text_for_remote()`：连同其正则规则一并迁移为全项目共享脱敏能力；
- `admit_activity()` & `prepare_remote_summary()`：依赖桌面活动语义（`ActivityEvent`、meeting 检测），仍保留在桌宠模块，仅其中"是否允许出网"的权限判断部分下沉到 `remote_gate.py`；
- `PrivacyStore`：继续负责桌宠活动记录，表结构与桌宠语义强绑定，不迁移；项目级运行记录若未来需要类似"清空历史"能力，可通过共享的 `RetentionPolicy` 接口各自独立实现，而非迁移数据本身。

迁移过程中将保持现有接口兼容，避免影响已有桌宠实现。
