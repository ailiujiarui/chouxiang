# 活动识别器增强计划

日期：2026-07-27
负责人：Luo（activity-classifier 分支）

## 我的模块是什么

奶龙桌宠有7个模块串成一条流水线。我的模块是第6个——活动识别器。

```
Windows桌面 → 采集器 → 隐私过滤 → 去重聚合 → 60秒窗口 → 【活动识别器】 → 人格决策 → 弹窗 → 桌面气泡
```

输入：一个60秒聚合窗口（`ActivityWindow`），告诉我这60秒里：
- `dominant_application`：前台最多的是什么应用（code/browser/terminal/other）
- `dominant_activity`：隐私层已经做过一次判断（debugging/coding/reading/...）
- `confidence`：隐私层给的置信度（0~1）
- `summary`：脱敏摘要（格式固定 `"application=xxx; activity=xxx; source=xxx"`）

输出：一个活动分类（`ActivityClassification`）：
- `activity`：最终判定的活动类型
- `confidence`：对这个判定的信心
- `classifier`：用哪一层判的（rules/lightweight/llm）

## 三级识别架构

### 第一层：规则层（0行计算，免费）

两个硬规则，打中就直接返回：

| 条件 | 输出 | 置信度 |
|------|------|--------|
| 空闲 | IDLE | 1.0 |
| 有明确活动 + 置信度≥0.8 | 直接用隐私层的判断 | 原值 |

隐私层给 TEST_FAILED/TEST_SUCCEEDED/COMPILE_SUCCEEDED 置信度 0.9，这些直接在这一层就返回了。

### 第二层：轻量层（启发式，免费）

分两段判断：

1. **优先采纳上游判断**（7月27日新增）：如果 `dominant_activity` 不是 UNKNOWN 不是 IDLE，直接用，置信度至少 0.65
2. **应用名映射**（兜底）：browser→READING、code/ide/terminal→CODING

隐私层给 DEBUGGING/CODING/READING/WRITING/GAMING/MEDIA 置信度 0.7。之前 0.7 < 0.8 不被规则层捕获，又因为轻量层只看 application 不 adoptivity，导致 debugging 窗口被错误判成 CODING 或 READING。**7月27日的改动就是修这个——让轻量层先看看上游已经给了什么判断。**

### 第三层：LLM层（调DeepSeek，花钱）

只有前两层都搞不定（置信度 < 0.65）时才调用。调之前经过隐私脱敏。

## 已完成的改动（Phase 1）

**`_lightweight_classification` 增加上游判断优先**：

```python
# 改动前：只看 application 映射
application_scores = {"browser": READING, "code": CODING, ...}
return application_scores.get(window.dominant_application, UNKNOWN)

# 改动后：先看 dominant_activity，再看 application
if window.dominant_activity not in (UNKNOWN, IDLE):
    return 直接用 dominant_activity  # 新增
# 否则走原来的 application 映射
```

效果：隐私层判了 DEBUGGING 的窗口不会再被轻量层覆盖成 CODING。

## 后续增强（待讨论优先级）

### Phase 2：扩充 application 映射表

当前只有4个映射。扩充方向：
- 补充 explorer、office、各种IDE变体
- 区分 coding 子类（terminal+高频事件→DEBUGGING、IDE+低频→READING）

### Phase 3：解析 summary 信息

当前 summary 格式是固定的 `"application=xxx; activity=xxx; source=xxx"`。
如果隐私层未来在 summary 里放了更多 hints，轻量层可以解析利用。

### Phase 4：组合信号启发式

- `event_count` 高 + code → 活跃编码
- `event_count` 低 + browser → 阅读
- 全屏 + 非IDE → GAMING/MEDIA

### Phase 5：分类器命中率统计

埋点统计三层各占多少比例，用数据驱动规则迭代。

## 需要补充的测试

当前4个测试覆盖了规则层、轻量层、LLM层和隐私拦截。需要新增：
- 轻量层采纳上游 DEBUGGING（验证改动生效）
- 轻量层采纳上游 WRITING
- 上游 UNKNOWN 时正常 fallback 到 application 映射
- dominant_activity=IDLE 不进入轻量层（已被规则层捕获）<｜end▁of▁thinking｜>文档写好了。

<｜｜DSML｜｜tool_calls>
<｜｜DSML｜｜invoke name="write_file">
<｜｜DSML｜｜parameter name="content" string="true"># 活动识别器增强计划

日期：2026-07-27
负责人：Luo（activity-classifier 分支）

## 我的模块是什么

奶龙桌宠有7个模块串成一条流水线。我的模块是第6个——活动识别器。

```
Windows桌面 → 采集器 → 隐私过滤 → 去重聚合 → 60秒窗口 → 【活动识别器】 → 人格决策 → 弹窗 → 桌面气泡
```

输入：一个60秒聚合窗口（`ActivityWindow`），告诉我这60秒里：
- `dominant_application`：前台最多的是什么应用（code/browser/terminal/other）
- `dominant_activity`：隐私层已经做过一次判断（debugging/coding/reading/...）
- `confidence`：隐私层给的置信度（0~1）
- `summary`：脱敏摘要（格式固定 `"application=xxx; activity=xxx; source=xxx"`）

输出：一个活动分类（`ActivityClassification`）：
- `activity`：最终判定的活动类型
- `confidence`：对这个判定的信心
- `classifier`：用哪一层判的（rules/lightweight/llm）

## 三级识别架构

### 第一层：规则层（0行计算，免费）

两个硬规则，打中就直接返回：

| 条件 | 输出 | 置信度 |
|------|------|--------|
| 空闲 | IDLE | 1.0 |
| 有明确活动 + 置信度≥0.8 | 直接用隐私层的判断 | 原值 |

隐私层给 TEST_FAILED/TEST_SUCCEEDED/COMPILE_SUCCEEDED 置信度 0.9，这些直接在这一层就返回了。

### 第二层：轻量层（启发式，免费）

分两段判断：

1. **优先采纳上游判断**（7月27日新增）：如果 `dominant_activity` 不是 UNKNOWN 不是 IDLE，直接用，置信度至少 0.65
2. **应用名映射**（兜底）：browser→READING、code/ide/terminal→CODING

隐私层给 DEBUGGING/CODING/READING/WRITING/GAMING/MEDIA 置信度 0.7。之前 0.7 < 0.8 不被规则层捕获，又因为轻量层只看 application 不 adoptivity，导致 debugging 窗口被错误判成 CODING 或 READING。7月27日的改动就是修这个——让轻量层先看看上游已经给了什么判断。

### 第三层：LLM层（调DeepSeek，花钱）

只有前两层都搞不定（置信度 < 0.65）时才调用。调之前经过隐私脱敏。

## 已完成的改动

`_lightweight_classification` 增加上游判断优先：如果 dominant_activity 不是 UNKNOWN/IDLE，直接采纳——不调到 LLM。

效果：隐私层判了 DEBUGGING 的窗口不会再被轻量层覆盖成 CODING。

## 后续增强方向

### 扩充 application 映射表
当前4个映射太少。补充 explorer、office、IDE 变体等。

### 解析 summary 信息
当前 summary 固定格式 `"application=xxx; activity=xxx; source=xxx"`。如果隐私层未来放更多 hints，轻量层可解析利用。

### 组合信号启发式
event_count 高+code→活跃编码、event_count 低+browser→阅读、全屏+非IDE→GAMING/MEDIA。

### 分类器命中率统计
埋点统计三层各占多少比例，用数据驱动规则迭代。

## 待补充的测试

- 轻量层采纳上游 DEBUGGING（验证改动生效）
- 轻量层采纳上游 WRITING
- 上游 UNKNOWN 时正常 fallback 到 application 映射
