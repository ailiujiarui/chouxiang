# Nailong 主动通知更新

日期：2026-07-24  
状态：已实现，待 Pull Request review

## 更新目标

将 Refactor Agent 的分析进度接入 Nailong 桌面端，使通知先经过策略判断和持久化，再由桌面端决定是否显示弹窗。分析事件不会直接触发 UI。

本次没有新增独立的 FastAPI 通知接口；桌面端通过现有的 `/analysis/events/stream` SSE 事件流接收分析事件。

## 已实现能力

- 分析事件按类型生成鼓励、轻微吐槽、调试提示、pytest 庆祝、最终裁决庆祝和失败提醒。
- 长任务达到预计时限的三分之一时生成一次工作提醒。
- 通知意图写入 SQLite，支持事件去重、消费游标、重启恢复和终态通知优先级。
- 普通通知使用 5–15 分钟随机冷却；终态通知绕过普通冷却。
- 两次弹窗开始之间默认至少间隔 30 秒。
- 免打扰期间不显示普通通知；退出免打扰时可将被抑制的终态结果汇总为一条通知。
- PySide6 桌面端显示通知气泡，并在显示、关闭或失败后回写 ACK。
- 通知文案支持深色主题，系统托盘可控制免打扰状态。
- 隐私默认 fail-closed；非公开分析事件不会进入通知链路。

每日最大弹窗次数目前没有实现，也没有对应配置接口。

## 事件与通知映射

| 分析事件 | 通知类型 | 默认行为 |
| --- | --- | --- |
| `TASK_STARTED` | `encouragement` | 鼓励开始工作 |
| `PHASE_STARTED`（第 2 轮 minimizer） | `light_tease` | 轻微吐槽 |
| `AST_REJECTED`、`PYTEST_FAILED`、`ADVERSARY_FAILED` | `debug_hint` | 调试提示 |
| `PYTEST_PASSED` | `pytest_celebration` | 测试通过提示 |
| `FINAL_VERDICT_PASSED`、`TASK_COMPLETED` | `final_celebration` | 最终完成庆祝 |
| `TASK_FAILED`、`TASK_TIMED_OUT`、`TASK_CANCELLED` | `terminal_failure` | 终态失败提示 |

## 主要接口

位置：`src/nailong_agent/`

```python
NotificationService.ingest_analysis_event(event)
NotificationService.poll_long_tasks()
NotificationService.lease_next()
NotificationService.acknowledge(notification_id, outcome)
NotificationService.set_do_not_disturb(enabled)
NotificationService.get_status()
```

- `NotificationPolicy.candidate_for(event)`：事件到通知类型、文案和优先级的映射。
- `NotificationStore`：SQLite 持久化、去重、冷却、终态抑制和通知状态。
- `AnalysisEventSubscriber`：从 SSE 事件流读取并恢复消费游标。
- `NotificationDeliveryPump`：每秒轮询待显示通知并发布 `PopupDecision`。
- `DesktopProcess`：连接事件总线、渲染器和通知 ACK。

## 启动与验证

首次安装桌面端依赖并启动：

```powershell
python -m pip install -e ".[desktop]"
.\scripts\start.ps1 -Build -Desktop
```

默认通知数据库为 `.runs\\nailong_notifications.sqlite`。可用以下脚本验证完整链路：

```powershell
python scripts\\trigger_notification_demo.py
python scripts\\trigger_notification_demo.py --live --database .runs\\nailong_notifications.sqlite
```

前者使用 `NullRenderer` 做无界面冒烟测试；后者向运行中的桌面端共享数据库写入一个 `FINAL_VERDICT_PASSED` 事件。

相关回归测试：

```powershell
python -m pytest tests/test_notification_pipeline.py tests/test_analysis_events.py -q
```

## 兼容性边界

- 保留现有 `AnalysisEvent`、`EventBus`、`PopupRenderer` 和 Refactor Agent HTTP API。
- `DesktopProcess` 的通知、隐私存储和渲染器扩展均为可选依赖，旧的无通知启动方式仍可用。
- 通知数据与代码审判运行数据分开存储；通知服务不读取源代码内容。
