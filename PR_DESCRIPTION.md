活动识别器增强：轻量层优先采纳上游判断，利用event_count和summary减少LLM调用

改动文件：activity_recognizer.py（+66行）、test_activity_recognizer.py（+175行）、设计文档一份

问题

轻量分类器之前只看应用名（browser就是READING、code就是CODING），不管上游隐私层已经判断出来的活动类型。导致隐私层从窗口标题检测到debug判了DEBUGGING（置信度0.7），但轻量层因为窗口是browser直接覆盖成READING。另外event_count和summary里的activity信息完全没被利用。

改了什么

轻量层的判断链路现在有四段，按顺序fallback：

第一段，优先采纳隐私层的dominant_activity。如果不是UNKNOWN也不是IDLE，直接用，置信度至少0.65保证不掉到LLM。

第二段，应用名映射。browser对应READING，code、ide、terminal对应CODING。

第三段，summary解析。如果应用映射返回UNKNOWN，从summary的activity=xxx字段提取，比如summary里写了activity=coding就直接用。

第四段，event_count调置信度。事件数大于等于10且判为CODING，加信到0.78。事件数小于等于2且判为READING，加信到0.75。事件数小于等于2且判为CODING，降到0.62，让它掉到LLM去判断是不是真的在写代码。

LLM调用也改了两处：system_prompt里列出了合法活动枚举值，减少模型返回无效值。LLM失败时写debug日志，之前是静默吞掉无法排查。

顺带修了pyproject.toml里classifiers字段放错位置导致pip install失败的问题。

效果

之前隐私层判了DEBUGGING、WRITING、GAMING、MEDIA（置信度0.7）的窗口全部掉到LLM。现在在轻量层就被拦截，不调LLM。

测试

测试从4个扩到17个，覆盖上游采纳、置信度下限、event_count加减信、summary解析、LLM降级路径、向后兼容。所有nailong模块34个测试全绿。
