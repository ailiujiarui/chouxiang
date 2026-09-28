# 代码审判产品适配层设计

> 认证更新（2026-07-20）：本文中的 Admin Token 必填约束已被 `2026-07-20-local-no-admin-token.md` 取代；默认 localhost 单用户模式不需要令牌，显式配置后仍强制校验。

日期：2026-07-18
状态：已实现，但产品决策已被 `2026-07-19-code-judge-product-redesign.md` 取代

> 本文仅保留为历史实施记录。REVIEW 静态捷径、用户必须提供测试以及人格仅为展示参数等决策不再作为后续实现依据。

## 目标

在不削弱现有 AST、测试、Docker 和证据链的前提下，提供“粘贴 Python 代码即可审查或安全精简”的产品入口，并把报告语气改为可选择的人格模板。

## 用户入口

Dashboard 的“任务”页新增“粘贴代码”区域，输入：

- Python 源码；
- 精简要求；
- 可选 pytest 测试源码；
- 模式：`REVIEW`（只读审查）或 `VERIFIED_REFACTOR`（验证后改写）；
- 报告人格：`STRICT` 或 `TSUNDERE`。

同时新增 CLI `snippet` 命令，源码和测试可以来自文件或 stdin。CLI 与 Dashboard 调用同一应用服务，不重复实现业务规则。

## 模式边界

### REVIEW

- 允许没有测试，仅执行 Python 语法、AST、复杂度和安全静态分析。
- 可以调用配置好的 LLM 生成候选建议和人格化 review，但不写入候选文件、不运行候选代码、不产生成功状态或 Reward。
- 报告必须显示“未执行、未验证”，不能使用“修复成功”“测试通过”等结论。

### VERIFIED_REFACTOR

- 必须提供 pytest 测试源码。
- Worker 将源码与测试写入一次性临时项目，固定文件名为 `snippet.py` 和 `test_snippet.py`。
- 完整复用现有 `RefactorOrchestrator`，经过 baseline pytest、AST rewrite、Docker pytest、对抗测试、变异测试和 Judge。
- API/Worker 模式只允许 Docker；本地 CLI 可显式使用可信 subprocess，并继续显示非安全沙箱提示。

## 持久化任务

新增 `RepositoryJobKind.SNIPPET`，沿用现有任务状态、租约、取消、截止时间、事件和运行产物。Snippet 任务不关联 GitHub 仓库，使用内部身份 `local/snippet`，永不进入 clone、branch、push、PR 或 Issue 评论路径。

`POST /jobs/snippet` 使用 Admin Token，认证发生在读取请求体之前。请求限制：

- 源码最多 128 KiB；
- 测试最多 128 KiB；
- 精简要求最多 32 KiB；
- 只接受 UTF-8 Python 文本；
- 禁止客户端传入路径、命令、环境变量、依赖安装或 Docker 参数。

源码会进入现有 SQLite durable payload 和有界运行产物，因此界面必须明确提示它会被本地持久化；API 不接收密钥字段，现有脱敏仍覆盖日志和报告。

## 人格模板

人格是展示层参数，不参与 Judge、Reward、状态迁移或安全判断：

- `STRICT`：当前资深 reviewer 风格，只批评代码。
- `TSUNDERE`：稳定的傲娇语气和轻度挑衅，但仍只针对代码结构；禁止性、身份、外貌、能力羞辱，禁止仇恨、威胁和真实人身攻击。

LLM 输出继续使用结构化 JSON。报告保存原始验证证据，并在独立“人格点评”段落渲染模板结果。

## API 与 Dashboard

- `GET /capabilities` 增加 `snippet_submission`、可用模式和人格列表。
- `POST /jobs/snippet` 创建任务并返回 Job ID。
- `DashboardApiClient` 只在该控制请求中发送 Admin Token。
- Dashboard 表单根据模式强制测试输入；提交成功后跳转到现有任务、执行过程和代码变更视图。
- 现有 URL 提交和 allowlist 行为不变；GitHub Webhook 自动交付随后已按独立设计删除。

## GitHub 链接范围

本阶段继续支持仓库 URL，不扩展到任意文件、PR 或 Gist URL。原因是三类链接的 ref、权限、fork 和测试目录语义不同，需要单独设计；不得通过字符串转换绕过现有仓库 allowlist。

## 测试与验收

- 请求认证、体积限制、模式校验和 UTF-8/语法错误。
- Snippet 任务的持久化、租约、取消、截止时间和 Worker 分派。
- REVIEW 不执行 pytest、不声称验证成功。
- VERIFIED_REFACTOR 缺少测试时拒绝；有测试时完整复用 orchestrator。
- Snippet 服务没有 Git/GitHub 依赖，无法触发任何远程写操作。
- STRICT/TSUNDERE 只改变文案，不改变候选、验证结果和 Reward。
- CLI 文件输入与 stdin；Dashboard AppTest 覆盖两种模式和表单状态。
- 更新 README 和主设计，运行聚焦测试、完整测试、compileall、`git diff --check` 和凭据扫描。
- 实施后完成完整 code review，自修复所有 Critical/High 问题；不自动提交、推送或部署。

## 非目标

- JavaScript、TypeScript、Java、Go 或其他语言。
- 自动生成可靠测试来替代用户测试。
- 安装任意第三方依赖。
- 任意 GitHub 文件、PR、Gist 或非 GitHub URL。
- 绕过 Admin Token、Docker、AST allowlist 或安全检查。

## 实施记录

- 新增持久化 `SNIPPET` 任务、`REVIEWED` 运行状态和兼容旧 SQLite 的 schema migration。
- REVIEW 仅执行静态分析并生成明确的未执行、未验证报告；VERIFIED_REFACTOR 固定使用 `snippet.py`/`test_snippet.py` 并复用完整 orchestrator。
- CLI 支持文件与 stdin；Dashboard/API 支持两种模式和 STRICT/TSUNDERE 人格。
- Worker 对 Snippet 使用独立服务，不检查仓库 allowlist，也没有 Git/GitHub 服务依赖。
- Review 修复：明确测试必须从 `snippet` 模块导入；增加确定性人格点评，使 mock 与真实模型模式保持产品语义一致。
- 最终验证：`249 passed`，一个既有 Starlette/httpx 弃用警告；`compileall`、`git diff --check` 和凭据扫描通过。
- 未运行真实 DeepSeek 或 Docker Snippet；subprocess mock 集成测试已跑通完整 VERIFIED_REFACTOR 流程。
- 未提交、推送或部署。
