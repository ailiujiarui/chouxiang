# 桌宠气泡堆叠修复

**日期:** 2026-08-02
**分支:** main
**关联 PR:** 无（直接提交）

---

## 问题

桌宠气泡在收到高频连续通知时会出现堆叠，旧气泡不会被自动关闭，导致桌面同时显示多个重叠气泡。

### 根因

[`PySide6Renderer._show_on_ui_thread`](../src/nailong_agent/renderer.py#L360) 在每次收到 `PopupDecision` 时创建新的 `SpeechBubble` widget，但没有关闭之前已有的气泡。`QTimer.singleShot` 无法取消，旧气泡的自动关闭定时器仍然活跃。

### 执行路径

```
收到 PopupDecision (action="show")
  → _show_on_ui_thread()
    → 创建 SpeechBubble()        ← 不检查已有气泡
    → self._popups.append(popup)  ← 旧气泡仍在列表中
    → QTimer.singleShot(...)      ← 旧定时器仍在运行
```

---

## 修复

### 已修改文件

**`src/nailong_agent/renderer.py`**

三个改动点：

1. **`__init__`** — 新增 `self._popup_timers: list[QTimer]` 跟踪自动关闭定时器，取代不可取消的 `QTimer.singleShot`。

2. **新增 `_dismiss_all_popups()`** — 关闭所有可见气泡、停止所有活跃定时器、清空两个列表。
   ```python
   def _dismiss_all_popups(self) -> None:
       for timer in self._popup_timers:
           timer.stop()
       self._popup_timers.clear()
       for popup in self._popups:
           popup.close()
       self._popups.clear()
   ```

3. **`_show_on_ui_thread`** — 入口第一行调用 `self._dismiss_all_popups()`，保证最多一个气泡可见。新定时器用 `QTimer` 实例代替 `singleShot`，并加入 `_popup_timers` 列表。

4. **`stop()`** — 用 `_dismiss_all_popups()` 替代手动遍历清理，确保所有定时器被停止。

### 行为变更

| 场景 | 修复前 | 修复后 |
| --- | --- | --- |
| 高频连续通知 (10 次) | 10 个气泡堆叠 | 始终只有 1 个气泡 |
| 新气泡到达 | 旧气泡保留 | 旧气泡立即关闭、定时器取消 |
| `action="defer"` | 无影响 | 不影响当前可见气泡 |
| renderer.stop() | 气泡关闭、定时器可能残留 | 所有定时器全部停止 |

---

## 测试

**`tests/test_nailong_scaffold.py`** — 新增 3 个测试用例：

| 测试 | 验证内容 |
| --- | --- |
| `test_null_renderer_at_most_one_active_popup_when_frequent_notifications_arrive` | NullRenderer 正确记录所有通知，可通过只保留最新一条来模拟替换 |
| `test_pyside_renderer_dismisses_old_bubble_when_new_one_shown` | 新气泡出现 → 旧气泡被关闭 + 旧定时器被取消；defer 行为不影响当前气泡 |
| `test_pyside_renderer_timer_cleanup_prevents_dangling_timers` | 10 次高频通知 → 1 气泡可见 + 1 定时器活跃；stop() 后定时器全部停止 |

### 测试结果

```
tests/test_nailong_scaffold.py  19 passed (含 3 新增)
tests/test_notification_pipeline.py 11 passed
tests/test_nailong_privacy.py   7 passed
─────────────────────────────────────────
总计                            37 passed
```

---

## 架构影响

- **无 breaking change** — `PopupRenderer` 协议、`NullRenderer`、`PopupDecision`、`EventBus` 均未改变。
- 气泡渲染现在遵循"至多一个活跃气泡"契约，与通知存储层的 `minimum_popup_start_spacing_seconds` 串行化策略正交互补：存储层控制"创建间隔"，渲染层控制"显示互斥"。
- Timer 生命周期与 Popup widget 生命周期解耦：Timer 先于 widget 被取消，避免"widget 已关闭但 timer 仍然回调"的悬垂引用。
