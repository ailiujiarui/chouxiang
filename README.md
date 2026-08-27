# 奶龙桌宠

一个会观察、吐槽、提醒和陪你写代码的 Windows 桌宠。奶龙常驻桌面，
用纯文字表情和上方气泡表达状态；它背后的代码审判与 DeepSeek 评测能力，
负责把"看到了什么"和"该怎么提醒你"变成具体、带点傲娇的回应。

## 仓库结构：引擎是主，桌宠是伴

本仓库同时包含两个组件：

- **`refactor-agent`（主产品）**：`src/refactor_agent/`。本地闭环代码重构与
  静态审查引擎，提供 CLI、控制 API、Streamlit Dashboard 和 Benchmark；
  统一分析流程见下文"执行链"。
- **`nailong`（桌面伴生产品）**：`src/nailong_agent/`。Windows 桌面宠物，
  通过 SSE 订阅 `refactor-agent` 的分析事件，消费引擎产出；两者以事件契约
  （`AnalysisEvent`）和本地 API 解耦，奶龙不直接调用引擎内部实现。

日常开发以引擎为主，奶龙是它的展示层。桌面端默认不采集活动、不上传源码，
一切采集与评测均需用户显式授权。

奶龙当前支持：

1. 常驻桌面、拖动、设置、暂停监听、免打扰和本地活动记录清理；
2. 识别编码、调试、测试失败、编译成功、长时间 idle、会议和娱乐等状态；
3. 用户授权后，检测已保存的 Python 文件并调用真实 DeepSeek 做静态评测；
4. 通过中度傲娇气泡输出鼓励、轻微吐槽、调试提示和评测报告；
5. 通过后台代码审判能力完成 AST 分析、多 Agent 对抗、验证和人格化审查。

## 奶龙为什么有点抽象

这里的“抽象”不是随机胡闹，而是奶龙表达状态的方式：

- 眼睛和嘴巴直接用文字表示，表情变化靠“（大笑）”“（得意）”“（困倦）”等状态文字完成；
- 待机、编码、调试、测试失败或庆祝等状态，会在奶龙上方变成一句短气泡；
- 语气是中度傲娇：先说具体观察到的代码或活动，再补一句“本龙只是顺便提醒”的态度，不攻击用户本人；
- 它也会选择安静：遇到免打扰、暂停、全屏、会议、冷却或隐私未授权时，不会强行冒泡。

抽象度（`abstraction_level` 偏好，默认 `LITERAL`，持久化于桌宠设置）：

- `LITERAL`：直白提醒，现状不变；
- `POETIC`：把状态写成一句隐喻，如“你的光标像一只认真的萤火虫，正挨个点亮分号”；
- `SURREAL`：更荒诞的表达，并在安静状态下偶发打破第四面墙的元评论，如“本龙偶尔怀疑，自己只是你终端里一场还不错的幻觉”。

抽象只改变措辞，绝不改变技术事实、优先级或打断策略；识别仍然以本地分类为准。

奶龙不是 Dashboard 浮层，也不是普通通知器；它是一个有状态、有边界、偶尔嘴硬的桌面陪伴角色。

产品本身不接收 GitHub Webhook，不创建分支、commit、push、Pull Request 或
Issue 评论；以上描述的是产品运行时的对外行为，与本仓库的开发协作流程无关。

## 执行链

统一分析流程为：

```text
Input Adapter
  -> AST Analyst
  -> Minimizer
  -> Defender
  -> Adversary
  -> Judge
  -> Persona Reporter
```

- LLM 生成的候选文件始终按不可信输入处理。
- AST Guard 限制允许修改的区域，并拒绝危险调用、新增高风险 import、公开 API 破坏和越界修改。
- API/Worker 使用无网络、非 root、只读根文件系统和资源受限的 Docker sandbox。
- 用户测试、系统生成测试、对抗测试、变异测试和 Judge 共同形成可追溯证据。
- 报告明确标记 `STATIC`、`GENERATED_TESTS`、`USER_TESTS` 或 `REPOSITORY_TESTS` 证据等级。
- 人格只影响报告措辞，不参与技术裁决和安全判断。

## 一键启动

需要 Windows 和 Python 3.11 或更高版本；Docker Desktop 可选。首次使用时：

```text
1. 将 .env.example 复制为 .env
2. 在 .env 中填写 DEEPSEEK_API_KEY
3. 双击 start.cmd
```

启动器会在项目 `.venv` 中准备 API、Dashboard 和奶龙依赖，并在 Windows 本机
后台启动这三个进程。Docker 只构建和运行用于执行不可信代码的安全 sandbox，
不再构建应用镜像，也不通过 Compose 启动日常产品。启动完成后会自动打开
Dashboard；后续仍然只需双击 `start.cmd`。

**Docker 可选**：检测不到 Docker 或 daemon 未运行时，启动器降级为本地
`subprocess` 沙箱并给出警告。降级模式只保留**审查模式**（`REVIEW`，不执行
代码）：所有需要执行不可信生成代码的任务（`verified-refactor`、URL 仓库分析）
会被 `/capabilities` 与 API 层双重禁用，产品不会在宿主机上直接运行 LLM 生成的代码。
要启用完整执行能力，请安装并启动 Docker Desktop。

启动弹性（全部可选，在 `.env` 中设置）：

- `REFACTOR_AGENT_STARTUP_TIMEOUT_SECONDS=120`：API/Dashboard 健康等待上限（指数退避重试）。
- `REFACTOR_AGENT_STARTUP_PORT_FALLBACK=1`：默认端口被占用时自动顺延寻找空闲端口；不设置时给出占用进程的 PID 与命令行并提示。
- `REFACTOR_AGENT_MAX_RESTARTS=3`：服务启动初期退出时的自动重试次数。
- `REFACTOR_AGENT_WATCHDOG=1`：后台 watchdog 每 10 秒检测三个服务进程，异常退出自动重启（`stop.cmd` 会先停掉 watchdog）。

日志默认按时间戳滚动保留最近 5 份（`.runs\logs\*.log.<时间戳>`），启动前会清理残留 PID 文件；Dashboard 启动失败时降级为"API + 奶龙"，奶龙启动失败时不影响 API/Dashboard。

默认地址：

- Dashboard：`http://127.0.0.1:8501`
- API：`http://127.0.0.1:8000`

默认是仅绑定 localhost 的本地单用户模式，不设置管理员令牌，Dashboard 可直接提交和管理本地任务。这个默认值不代表网络部署安全；不要把端口转发到不可信网络。

默认配置为真实 DeepSeek LLM。缺少 `DEEPSEEK_API_KEY` 时产品仍会启动，
但 `/capabilities` 会报告 LLM 不可用并禁用相关任务入口；系统不会静默切换到 mock。
仅在明确的离线演示或测试场景，在 `.env` 中设置
`REFACTOR_AGENT_MOCK_LLM=true`。

停止全部服务但保留本地数据：

```text
双击 stop.cmd
```

SQLite 默认使用统一的 5000ms `busy_timeout` 和 `auto` journal 策略；可通过 `REFACTOR_AGENT_SQLITE_JOURNAL_MODE=auto|wal|delete` 与 `REFACTOR_AGENT_SQLITE_BUSY_TIMEOUT_MS` 调整。WAL 只会在安全 SQLite 版本和本地文件系统上启用，备份与回退步骤见 [`docker/README.md`](docker/README.md#wal-备份与回退)。

### 奶龙桌宠启动与主动通知

奶龙随 `start.cmd` 自动启动。首次运行会在项目 `.venv` 中安装完整本地产品依赖，
不会修改全局 Python 环境。重复启动会复用现有 API、Dashboard 和奶龙进程，
不会打开第二套产品。运行日志位于 `.runs/logs`。

奶龙身体下方的“设置”按钮可重新设置活动陪伴授权、暂停监听、开启全天免打扰、
删除本地活动记录或退出桌宠。按住奶龙黄色身体可拖动窗口；短按仍显示本地回应气泡，
拖动时已有气泡会跟随移动。

桌面端通过现有分析事件流接收任务状态，按冷却、免打扰和终态优先级规则显示弹窗。默认数据目录为 `.runs`，其中包含 `nailong-agent.lock`、`nailong_privacy.sqlite` 和 `nailong_notifications.sqlite`。`NAILONG_DEEPSEEK_MODEL` 可在 `.env` 中覆盖桌宠模型。桌宠数据库不会保存 API Key、源代码、原始窗口内容、截图、OCR、剪贴板或终端正文。完整的事件映射、接口和验证方式见 [`docs/designs/2026-07-24-nailong-proactive-notifications-update.md`](docs/designs/2026-07-24-nailong-proactive-notifications-update.md)。

#### 奶龙活动监听

活动监听默认由 `NAILONG_ACTIVITY_LISTENER_ENABLED=true` 创建；可设置为 `false`，或在本次启动使用 `--no-activity-listener` 关闭。该开关只控制是否创建监听器，已持久化的 `activity_listener_enabled`、手动暂停和应用白名单/黑名单仍在每次事件处理时生效。

首次启动必须由用户明确授权活动采集。Windows 实现响应前台窗口变化，并每 15 秒采样系统空闲秒数：连续空闲达到 5 分钟时只发布一次 idle 事件，恢复活动后才允许下一次发布；同时读取进程可执行文件名和窗口是否覆盖当前显示器。为完成本地敏感/会议拦截和有限的 IDE 状态识别，窗口标题可能只在采集线程内短暂读取；原始标题会在隐私边界后丢弃，不会进入统一事件、EventBus、SQLite、日志或远程推理。编辑器/终端正文、代码、剪贴板、截图和 OCR 默认不会采集。会议应用和敏感应用会在入库前被隐私策略阻止。非 Windows 平台使用空实现，不采集任何活动。

只允许通过隐私策略后的统一最小化事件写入 `nailong_privacy.sqlite`，其中只包含归一化应用类别、活动类型、置信度和固定格式摘要。空闲、全屏、会议等原始采集信号只在本地隐私边界内用于分类和拦截，不会写入统一事件。可在桌宠隐私控制中使用“删除本地活动记录”清除活动事件和聚合记录，首次授权选择不会被删除。

## 奶龙自动 Python 评测

桌宠默认不会上传源码。要启用自动评测：

1. 在 `.env` 中配置真实的 `DEEPSEEK_API_KEY`。
2. 启动桌宠并完成本地活动采集授权。
3. 打开奶龙设置，开启“自动评测已保存的 Python 代码”，并确认源码上传提示。
4. 默认扫描桌宠启动目录；跨项目时用 `NAILONG_PYTHON_REVIEW_ROOTS` 指定目录（Windows 使用分号分隔）。

只有在检测到高置信度 Python 编码活动并保存了 `.py` 文件时才会触发；暂停、免打扰、全屏、敏感文件、冷却或每日预算耗尽时保持安静。评测结果会通过奶龙气泡显示，源码不会写入活动数据库。

## 可选认证

本地单用户启动不需要管理员令牌。需要额外保护提交、取消、重试和 allowlist 管理操作时，在本地 `.env` 中设置：

```dotenv
REFACTOR_AGENT_ADMIN_TOKEN=<strong-random-secret>
```

此时 `/capabilities` 返回 `admin_token_required=true`，Dashboard 才显示管理员令牌输入框；未携带正确 Bearer Token 的控制请求会返回 `401`。令牌不会写入 SQLite 或运行产物。

## DeepSeek

使用真实模型：

```dotenv
DEEPSEEK_API_KEY=<set-in-local-.env>
REFACTOR_AGENT_MOCK_LLM=false
```

离线演示（不会调用 DeepSeek）：

```dotenv
REFACTOR_AGENT_MOCK_LLM=true
```

可选变量为 `DEEPSEEK_BASE_URL` 和 `DEEPSEEK_MODEL`。API Key 不会写入 SQLite、日志或运行产物。

## CLI

`cli.py` 只处理命令参数、终端输出和命令编排；本地文件、Demo 与只读 GitHub URL
共用 `local_refactor.py` 的执行服务，路径和环境默认值由 `cli_config.py` 统一解析，
Demo Suite 案例循环由 `demo_suite_service.py` 执行，Mock/DeepSeek 与沙箱参数不会在各命令中重复装配。
Benchmark 的内置/Manifest 分支、历史对比和证据文件由 `benchmark_service.py` 统一编排。
Snippet 的任务构造、执行和报告定位由 `snippet_submission.py` 负责，CLI 只处理输入适配。
只读 GitHub URL 的 checkout、请求构造和本地执行由 `github_url_submission.py` 统一处理。
Jobs 与 trajectory memory 的只读查询和文本格式由 `cli_queries.py` 统一提供。
Streamlit 依赖检测、环境组装和子进程执行由 `dashboard_launcher.py` 负责。
Orchestrator 的源码、日志、变异结果和报告落盘由 `orchestrator_artifacts.py` 独立负责。
运行轨迹与安全分析事件由 `orchestrator_observability.py` 统一记录，事件发布失败不会中断重构执行图。
初始执行状态、节点跳转、重试终止判定和辩论轮次收束由 `orchestrator_state.py` 统一管理。
最终 `RunRecord` 与成功/失败 trajectory memory 由 `orchestrator_persistence.py` 按稳定顺序持久化。
Prepare 执行节点由 `orchestrator_prepare.py` 负责历史记忆注入、基线分析、隔离工作区复制和沙箱预检。
Minimizer 执行节点由 `orchestrator_minimizer.py` 负责目标区域选择、候选提案、LLM usage 累积和失败终止。
AST Guard 执行节点由 `orchestrator_ast_guard.py` 负责受控重写、验证、拒绝事件和重试路由。
Pytest 执行节点由 `orchestrator_pytest.py` 负责候选写入、沙箱回归测试、验证事件和失败重试。
Adversary 执行节点由 `orchestrator_adversary.py` 负责规则批评、对抗测试、事件、轨迹和失败重试。
Mutation/性能节点由 `orchestrator_mutation.py` 负责组合测试、变异挑战、post 指标和性能证据。
Judge 执行节点由 `orchestrator_judge.py` 负责评分、裁决、轮次收束和重试/终止路由。
Finalize 执行节点由 `orchestrator_finalize.py` 负责持久化终态、装配结果和发布最终分析事件。
报告渲染由 `orchestrator_report.py` 负责。

安装开发依赖：

```powershell
python -m pip install -e .[dev]
# 或使用 uv（推荐，使用仓库内 uv.lock 做精确锁定）：
uv sync --extra desktop --extra dashboard
```

审查文件或 stdin，`REVIEW` 不执行用户代码，默认 `subprocess` 后端、无需 Docker：

```powershell
refactor-agent snippet --source snippet.py --mode review --persona tsundere
Get-Content snippet.py | refactor-agent snippet --source - --mode review
```

提供 pytest 后执行验证精简（Docker 可选；未配置 Docker 时用 `subprocess`，
仅在执行不可信生成的代码时才需要 Docker sandbox）：

```powershell
refactor-agent snippet `
  --source snippet.py `
  --tests test_snippet.py `
  --mode verified-refactor
```

只读 GitHub URL：

```powershell
refactor-agent github-url `
  --repo-url https://github.com/owner/repository `
  --issue-text "简化 calculate 函数" `
  --target src/package/module.py `
  --tests tests
```

## 控制 API

主要接口：

- `/health`、`/capabilities`
- `/analysis`、`/jobs/snippet`、`/jobs/url`
- `/jobs`、任务详情、事件、取消和重试
- `/runs`、trajectory、运行产物和 `/benchmarks`
- `/admin/repository-allowlist`

仓库 URL 仍必须通过 canonical URL 校验和持久化 allowlist。源码和测试会保存在本地任务数据中，不要提交密钥。

## 验证

```powershell
pytest -q
ruff check src tests
python -m compileall -q src tests
docker compose config --quiet
git diff --check
```

Docker 细节见 `docker/README.md`，当前产品设计见 `docs/designs/2026-07-19-code-judge-product-redesign.md`，
核心模块依赖规则见 `docs/architecture/core-module-boundaries.md`。

## 许可证

本项目采用 [GNU Affero General Public License v3.0 或更高版本](LICENSE)。
如果你向他人分发本项目或运行修改后的网络服务，必须遵守 AGPL-3.0-or-later，
包括向交互用户提供对应源代码的义务。完整条款见 [`LICENSE`](LICENSE)。
