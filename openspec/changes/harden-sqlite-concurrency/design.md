## Context

本设计以 `origin/main@7bc4dd3` 为实现基线。当前仓库的 SQLite 拓扑不是“一个 Store”：

- `SQLiteRunStore` 管理 `refactor_agent.sqlite`。FastAPI 请求、`GitHubJobWorker`、heartbeat、分析事件发布、CLI 与 Dashboard 会通过不同连接访问它；`create_app()` 还会让 API 与后台 Worker 共享同一 Store 实例，而 Store 的每个方法再打开独立连接。
- `NotificationStore` 管理 `nailong_notifications.sqlite`。SSE subscriber、notification delivery pump、ACK、免打扰和偏好设置可从不同线程访问它。
- `PrivacyStore` 管理 `nailong_privacy.sqlite`。活动采集、授权设置和“清除历史”可并发访问它。

`NotificationStore` 与 `PrivacyStore` 当前都使用 `timeout=30`、`foreign_keys=ON`、`busy_timeout=30000`；主 `SQLiteRunStore` 只调用 `sqlite3.connect(path)`，且三个 Store 都未显式管理 WAL。现有测试主要验证顺序状态转换，没有用独立连接制造 API/Worker 或桌宠线程之间的真实锁竞争。

本地审计时 Python 3.14.0 绑定 SQLite 3.50.4；项目又允许 Python 3.11+，Docker 使用可变的 `python:3.12-slim`，因此不能假设所有运行环境都带有相同 SQLite 修复。SQLite 官方说明 WAL 可并行读写但仍只有一个 writer、不能用于网络文件系统，并在 2026 年披露了影响多连接 WAL 写入/检查点的低概率 WAL-reset 缺陷。Gitea 的可借鉴点是把 `SQLITE_TIMEOUT` 与 `SQLITE_JOURNAL_MODE` 作为显式运行配置，而不是把 WAL 当作无条件常量；Datasette 的单写入队列作为高竞争时的后续升级路径，不在本次默认引入。

参考：

- [SQLite WAL](https://www.sqlite.org/wal.html)
- [SQLite busy_timeout](https://www.sqlite.org/pragma.html#pragma_busy_timeout)
- [Gitea database configuration](https://docs.gitea.com/administration/config-cheat-sheet#database-database)
- [Datasette serialized write queue](https://docs.datasette.io/en/stable/internals.html#await-db-execute-write-sql-params-none-block-true)

## Goals / Non-Goals

**Goals:**

- 为三个 Store 建立同一份连接策略和同一套验证入口，消除默认值分叉。
- 让短暂写锁竞争在有界时间内自动收敛，而不是把普通竞争升级为任务失败。
- 在安全、受支持的单机文件系统上启用 WAL，使 API/SSE/Dashboard 读取不被短写事务长期阻塞。
- 保持 SQLite 的单 writer 事实，并用原子 SQL/CAS 保证任务认领、lease、取消、重试和完成不会丢更新或重复生效。
- 用线程、独立连接和至少一个跨进程场景验证真实组合并发，而不是只写单方法单元测试。
- 提供可诊断、可灰度、可回退的 journal mode 与 timeout 行为，不修改现有业务表结构。

**Non-Goals:**

- 不把三个 SQLite 文件合并成一个数据库，也不建立跨数据库事务。
- 不承诺多个 writer 真正并行；WAL 只改善 reader/writer 并行。
- 不在本次引入 SQLAlchemy 重写、外部数据库、Redis/Celery 或单写入队列。
- 不通过无限重试掩盖长事务、磁盘故障或永久锁。
- 不改变 LLM、Docker、Git、HTTP 或通知业务行为。

## Decisions

### 1. 建立共享的 SQLite runtime policy

新增共享模块（建议 `src/refactor_agent/sqlite_runtime.py`），提供不可变的 `SQLitePolicy`、连接工厂、数据库文件初始化和诊断函数。`nailong_agent` 已依赖 `refactor_agent` 类型，可复用该模块而不形成新的反向依赖。

建议默认策略：

| 项目 | 默认值 | 作用域 |
| --- | --- | --- |
| `busy_timeout_ms` | `5000` | 每个连接 |
| `journal_mode` | `auto` | 每个数据库文件 |
| `foreign_keys` | `ON` | 每个连接 |
| `row_factory` | `sqlite3.Row` | 每个连接 |
| `synchronous` | 不改动 SQLite 默认值 | 每个连接/数据库 |
| `wal_autocheckpoint` | 保持 SQLite 默认 1000 页 | 每个连接 |

连接工厂同时设置 Python `sqlite3.connect(timeout=busy_timeout_ms / 1000)` 和 `PRAGMA busy_timeout=busy_timeout_ms`，避免驱动参数与数据库 busy handler 表达不同策略。每次连接后读取并断言实际 `busy_timeout` 与 `foreign_keys`；journal mode 的切换只在 Store 初始化、启动并发线程之前执行，不在每次业务写入时反复切换。

替代方案是分别保留三个 `_connect()`；拒绝该方案，因为当前主 Store 与桌宠 Store 已经发生 5 秒默认值、30 秒显式值和外键设置差异，后续仍会继续漂移。

### 2. 采用 Gitea 风格的可配置 journal mode，而不是无条件 WAL

新增统一配置 `REFACTOR_AGENT_SQLITE_JOURNAL_MODE=auto|wal|delete` 和 `REFACTOR_AGENT_SQLITE_BUSY_TIMEOUT_MS`。`AppSettings` 与 `NailongSettings` 从同一变量构造 `SQLitePolicy`，测试可直接注入较短 timeout，不依赖修改全局环境。

模式行为：

| 配置 | 行为 |
| --- | --- |
| `delete` | 明确使用 rollback journal；仍启用统一 busy timeout。 |
| `auto` | 仅当数据库位于受支持的本地文件系统、运行时属于已知修复版本且 `PRAGMA journal_mode=WAL` 返回 `wal` 时启用。若当前文件不是 WAL，门禁不通过时保持 `delete` 并记录结构化原因；若文件已是 WAL 而当前运行时不安全，则拒绝启动，不能让不安全进程加入现有 WAL 数据库。 |
| `wal` | 强制要求 WAL；版本、文件系统或 PRAGMA 验证失败时拒绝启动，不静默降级。 |

`journal_mode=WAL` 是数据库文件的持久状态，但应用仍在启动时读取实际值并报告。首次切换允许在 busy timeout 内做有限重试；运行中不自动来回切换。

WAL 启用后首阶段保持 SQLite 默认 checkpoint 策略，不新增后台 checkpoint 线程，也不修改 `synchronous`。这是为了先隔离“并发改善”与“耐久性/检查点调优”两个变量。诊断信息记录 WAL 页数或 checkpoint 失败可以后续增加，但本次只要求能够查询实际 mode。

### 3. 对当前 WAL 运行时缺陷采取显式门禁

`auto` 模式只信任 SQLite 官方已知修复线：`>=3.51.3`，以及官方列出的 3.50.7、3.44.6 或更高的同维护线补丁。未知发行版回移不由版本号自动推断；运维若确认发行版已回移修复，应先升级到可识别版本或显式选择经过审批的运行镜像。

当前开发机的 SQLite 3.50.4 因此不会在 `auto` 下开启 WAL。Docker/CI 必须打印 `sqlite3.sqlite_version` 并使用满足门禁的镜像后，才能把部署配置切到强制 `wal`。这比直接在现有所有 Python 3.11+ 环境上强开 WAL 更符合“并发稳定性”目标。

### 4. 保持事务短小，并明确允许的事务内容

允许在一个写事务内完成：

- 任务或通知的条件查询与原子状态更新；
- lease owner/generation、审计事件和分析事件的同事务写入；
- 小规模 schema migration 和本地记录批量落库。

禁止在写事务内执行：

- LLM、HTTP、Git/GitHub、Docker、pytest、文件系统扫描；
- `sleep`、等待线程/进程、UI 回调、事件总线投递；
- 无上限循环或不受控的大批量处理。

现有 Worker 的“短事务认领 → 事务外执行 processor → 短事务完成”结构保留。heartbeat 使用独立连接进行单条条件 UPDATE。测试会用锁持有时长证明普通路径能在 timeout 内释放，而不是仅靠增大 timeout。

### 5. WAL/busy timeout 不替代业务并发控制

主任务状态仍通过 `BEGIN IMMEDIATE` 和条件 UPDATE/CAS 保护。所有状态写入必须把预期状态、lease owner 或 generation 放入 `WHERE`，并检查 `rowcount`；竞争失败返回领域冲突或 lease-lost，而不是覆盖新状态。

`create_github_job()` 改为纯 INSERT 或 `ON CONFLICT DO NOTHING` 后读取既有记录；不得用全行 upsert 覆盖正在运行的 lease。通用 `save_github_job()` 删除、收窄为迁移专用接口或替换为字段白名单方法。业务表上的 `INSERT OR REPLACE` 将逐项审计：只有“整行完全由本调用拥有且无引用副作用”的表可保留，否则改为显式 `ON CONFLICT ... DO UPDATE`。

替代方案是只加 WAL 与 timeout；拒绝该方案，因为它只能减少物理锁错误，不能防止两个 Worker 逻辑上重复认领或旧 owner 覆盖新 owner。

### 6. 错误、诊断与回退必须可观察

复用现有 `DATABASE_LOCKED` 分类，但补充不含路径/SQL/密钥的结构化字段：操作类别、journal mode、timeout、等待是否耗尽。启动诊断或 `/capabilities` 的受控字段报告：SQLite 版本、请求/实际 journal mode、busy timeout、是否通过 WAL 安全门禁；不暴露数据库绝对路径。

不对所有写操作再叠加盲目应用级重试。busy handler 已提供有界等待；只有幂等的启动 journal-mode 协商可有限重试。这样避免把 API 尾延迟放大到 timeout × retries，也避免对非幂等写入重复执行。

### 7. 并发验证采用组合矩阵，而不是单 Store 单元测试

新增 `tests/test_sqlite_concurrency.py`，使用 `threading.Barrier`、`Event`、独立 Store/连接和 `BEGIN IMMEDIATE` 锁持有器确定性控制顺序；不得用固定长 `sleep` 猜测竞态。测试 timeout 注入为 100–500ms，生产默认仍为 5000ms。

必须覆盖：

| 数据库 | 并发参与者 | 必须断言 |
| --- | --- | --- |
| 主库 | API submit × Worker claim | 两者在短竞争后成功；任务只创建、认领一次；事件顺序一致。 |
| 主库 | API cancel × heartbeat renew | 最终状态符合状态机；过期 owner 不能续租或覆盖取消。 |
| 主库 | Worker completion × 第二 Worker reclaim | 只有合法 owner 完成；竞争者得到领域冲突，不是随机锁异常。 |
| 主库 | analysis-event emit × heartbeat renew × API read | 两个写入均保存，读取获得完整快照，序列无重复。 |
| 主库 | 两个并发重复 submit | 唯一 delivery/active-job 约束收敛到一条 job，不发生全行覆盖。 |
| 通知库 | SSE ingest × delivery lease/ACK × preference/DND update | 意图不重复、budget/状态不丢失，ACK 只作用一次。 |
| 隐私库 | activity append × clear history/consent update | 结果属于可解释的串行顺序，授权记录永不被历史清理删除。 |
| 三个库 | 主任务写入 × 通知写入 × 隐私写入 | 不存在错误的全局进程锁；三个独立文件都成功提交。 |
| 主库 WAL | 长读 × Worker 短写 | 读取快照与写入同时完成，不出现脏读。 |
| 主库 timeout | 写锁持有超过上限 | 在有界窗口返回 `DATABASE_LOCKED`，没有部分业务写入。 |
| 主库跨进程 | API/CLI 或 Dashboard reader × Worker writer | 同主机独立进程下实际 mode、等待和快照行为一致。 |

每个组合在 `delete` 与安全 `wal` 模式下运行适用子集；WAL 专属场景只在修复版 SQLite 环境执行，CI 必须至少有一个满足门禁的 job，不能全部 skip。循环压力层重复关键组合若干轮并验证最终计数/状态，但不以高循环次数替代确定性屏障测试。

### 8. 暂不采用单写入队列

Datasette 通过单写连接队列彻底序列化写入，这对插件生态和高写竞争很合适。本项目当前已经有数据库级状态机、lease 和多个进程入口；立刻引入进程内队列既不能覆盖跨进程写入，又会增加关闭、崩溃恢复和 backpressure 语义。因此先实施 WAL、bounded wait、短事务和 CAS。

若上线指标显示持续出现 timeout、P95 写等待逼近阈值或 WAL checkpoint 饥饿，再单独设计持久 action queue 或迁移 PostgreSQL，不在本变更内隐式扩张。

## Risks / Trade-offs

- [旧 Python 绑定易受 WAL-reset 缺陷影响] → `auto` 版本门禁；CI/Docker 固定已修复 SQLite；强制 WAL 或不安全运行时打开既有 WAL 文件时拒绝启动。
- [WAL 不支持网络文件系统] → 本变更只支持本机磁盘或同主机容器卷；无法证明时 `auto` 保持 `delete`，`wal` 拒绝启动。
- [长 reader 导致 checkpoint 饥饿和 WAL 增长] → 保持分页/短读事务，首阶段沿用自动 checkpoint，并在后续指标证明需要时再增加手动 checkpoint。
- [5 秒 timeout 增加 API 尾延迟] → 写事务必须短，测试锁等待边界；锁耗尽返回 409/503 类受控错误而不是无限重试。
- [共享 helper 成为跨包耦合点] → helper 只依赖标准库、没有业务模型；Store 仍拥有 schema 和领域 SQL。
- [并发测试在 CI 中不稳定] → 使用 Barrier/Event 和显式锁持有器，避免依赖 CPU 调度和随机 sleep；压力测试与确定性测试分层。
- [journal mode 切换影响备份习惯] → 文档明确 `-wal`/`-shm` 属于运行状态，备份前使用受控 checkpoint/停机流程；rollback 有明确步骤。
- [现有全行 upsert/REPLACE 覆盖并发字段] → 在实现前列出所有写入口，业务状态表改为 INSERT-only/CAS，测试验证旧 owner 无法覆盖。

## Implementation Baseline and Audit

- 实施分支：`feat/sqlite-concurrency-hardening`。
- 实施基线：`main@7bc4dd34a60b47320fe17dc37a37b812de949e57`；评审材料从原工作树复制到独立 worktree，未携带 `feat/p0-production-hardening` 的未提交文件。
- 本地运行时：CPython `3.14.0` / SQLite `3.50.4`，不通过 WAL 安全门禁。当前 Docker daemon 未运行，原 `python:3.12-slim` 构建参数又是可变标签，因此不能把它视为已记录的安全 SQLite 运行时；CI 的 `wal-safe` job 使用 conda-forge 的 Python 3.12 与 `sqlite>=3.51.3`，显式打印版本、断言门禁通过并运行 WAL/跨进程矩阵。

生产写路径审计：

| 所有者 | 数据库文件 | 连接入口 | 写事务与冲突写入 |
| --- | --- | --- | --- |
| `SQLiteRunStore` | `AppSettings.resolved_database_path`，默认 `.runs/refactor_agent.sqlite` | `store.py::_connect()` 是主库唯一直接 `sqlite3.connect()`；API、Worker、heartbeat、CLI、Dashboard 通过 Store 的独立连接访问 | job claim/transition/cancel/retry/finish、allowlist 使用 `BEGIN IMMEDIATE`；`runs`、`benchmark_runs`、`trajectory_memory` 使用 `INSERT OR REPLACE`；job create 使用会覆盖 lease/status 的全行 upsert |
| `NotificationStore` | `NailongSettings.notification_database`，默认 `.runs/nailong_notifications.sqlite` | `notification_store.py::_connect()` | event/personality ingest、long reminder、DND、lease 使用 `BEGIN IMMEDIATE`；budget/task 状态使用字段级 upsert；ACK 使用状态条件 UPDATE |
| `PrivacyStore` | `NailongSettings.privacy_database`，默认 `.runs/nailong_privacy.sqlite` | `privacy_store.py::_connect()` | consent 使用字段级 upsert；activity/window 使用 INSERT-or-ignore；clear-history 删除活动表但不删除 consent |

审计确认生产代码只有上述三个 `sqlite3.connect()`。schema migration 也只通过 Store 初始化连接运行；测试中的裸连接仅用于构造旧 schema 或验证数据库，不属于生产连接策略。实现阶段将把三个入口统一到共享工厂、移除 job 全行 upsert，并把三个 `INSERT OR REPLACE` 改为显式 owned-field upsert，避免 REPLACE 的 delete/insert 语义。

## Migration Plan

1. 从最新 `main` 创建实现分支，并带入本 OpenSpec 变更；不得直接在当前落后且分叉的 `feat/p0-production-hardening` 代码基线上实现。
2. 新增共享 policy、配置解析和诊断，先以 `delete` 模式运行现有测试，证明重构没有改变业务语义。
3. 迁移 `SQLiteRunStore`、`NotificationStore`、`PrivacyStore` 到共享连接工厂；审计并收窄全行 upsert/REPLACE。
4. 增加确定性线程并发测试和 API/Worker 组合测试，在 rollback journal 模式先通过。
5. 增加 WAL 安全门禁、实际 mode 验证和修复版 SQLite CI job，再运行 WAL 读写、checkpoint 与跨进程组合。
6. Docker/启动脚本打印 SQLite 诊断；先以 `auto` 灰度，观察锁等待和错误分类，再在固定安全镜像中配置 `wal`。
7. 验证完整测试矩阵、数据库重启、备份/恢复和 `delete` 回退后再提交实现。

回退不需要 schema migration：停止写入进程，执行受控 checkpoint，设置 `journal_mode=delete`，确认返回 `delete` 后重新启动。若 WAL 初始化失败且未发生业务写入，`auto` 可直接保持 rollback journal；不得在活跃写入期间强制切换。

## Open Questions

- 生产默认最终保持 `auto`，还是在 Docker 镜像固定安全 SQLite 后改为强制 `wal`？本设计建议先 `auto` 灰度，再由部署配置强制 `wal`。
- 是否接受统一 5000ms timeout，还是像 Gitea 一样允许部署设置到 20000ms？本设计建议默认 5000ms、允许配置，但 CI 固定验证 5000ms 契约。
- SQLite 运行诊断放入公开 `/capabilities` 的最小字段，还是只写启动日志并提供管理员 CLI？本设计建议公开版本/mode、隐藏路径和内部错误细节。
