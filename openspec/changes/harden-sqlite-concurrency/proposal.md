## Why

当前控制 API、后台 Worker、heartbeat 与分析事件发布会通过不同连接并发写入同一个 `refactor_agent.sqlite`，但主存储仍依赖 `sqlite3.connect()` 默认锁等待策略；桌宠的两个 SQLite Store 虽显式设置了 30 秒等待，却与主存储配置分叉，也没有 WAL 或跨模块并发回归测试。需要建立一套统一、可验证、可安全回退的 SQLite 连接与并发契约，减少短暂锁竞争导致的任务失败，同时保持单机部署和现有数据格式。

## What Changes

- 新增统一 SQLite 连接策略，覆盖 `SQLiteRunStore`、`NotificationStore` 与 `PrivacyStore`，统一连接超时、`busy_timeout`、外键、行工厂和运行时能力检查。
- 为每个数据库文件提供 `auto`、`wal`、`delete` 三种 journal mode；仅在本地文件系统、运行时 SQLite 已包含已知 WAL 并发修复且 `PRAGMA journal_mode=WAL` 验证成功时启用 WAL；不安全运行时遇到已经处于 WAL 的文件时拒绝加入，避免新旧运行时混用。
- 保持写事务短小：任务认领、lease、状态转换和事件写入在事务内原子完成，LLM、Docker、Git、HTTP 与等待操作不得发生在事务内。
- 增加结构化数据库并发错误与启动诊断，使锁等待超时、WAL 不可用和不安全运行时能够被区分、记录和测试。
- 增加跨模块并发测试：控制 API 与 Worker、Worker 与 heartbeat/事件发布、通知订阅与投递/设置、活动采集与隐私清理，以及三个独立数据库同时写入；测试使用独立连接和同步屏障制造真实竞争，不以顺序调用冒充并发。
- 保留 SQLite 单写入者模型；若压力测试证明 bounded wait 仍不足，后续可单独引入 Datasette 风格的单写入队列，本变更不提前加入该复杂度。

## Capabilities

### New Capabilities

- `sqlite-concurrency-hardening`: 定义统一 SQLite 连接配置、安全 WAL 启用、短事务边界、并发状态正确性、诊断和跨模块测试要求。

### Modified Capabilities

无。

## Impact

- 主要代码：`src/refactor_agent/store.py`、`src/refactor_agent/config.py`、`src/refactor_agent/job_worker.py`、`src/refactor_agent/webhook.py`、`src/nailong_agent/notification_store.py`、`src/nailong_agent/privacy_store.py`、桌宠配置与启动路径，以及新增共享 SQLite 模块。
- 测试：`tests/test_store.py`、`tests/test_control_api.py`、`tests/test_notification_pipeline.py`、`tests/test_nailong_privacy.py`，并新增专门的 SQLite 并发集成测试。
- 运维：本地与 Docker 启动时会报告 SQLite 版本、实际 journal mode 和 busy timeout；强制 WAL 配置可能因不安全版本或不支持的文件系统而拒绝启动。
- 数据兼容：不修改现有表结构和业务 API；WAL 会产生同目录的 `-wal`/`-shm` 文件，备份流程必须把它们视为数据库持久状态的一部分或先执行受控 checkpoint。
