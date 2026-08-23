# 本机优先的一键启动简化设计

日期：2026-08-08
状态：已实施、review 通过

## 问题

当前 `start.cmd` 会构建 sandbox 镜像，又会通过 Compose 构建应用镜像。应用镜像为了
在容器内调用宿主 Docker daemon，还要执行 `apt-get install docker-cli git`。这次实际
启动正是在该步骤因 Debian 软件源失败，导致整个产品无法启动。

这层应用容器不是安全边界。真正执行不可信代码的是独立 sandbox 容器；API 只需要在
宿主机调用已有的 `docker.exe run` 即可继续使用相同的无网络、只读、非 root、资源受限
sandbox。

## 目标

- 用户仍然只双击根目录 `start.cmd` 和 `stop.cmd`。
- 不再构建应用镜像，不再通过 Compose 启动日常产品。
- API、Dashboard 和奶龙共用仓库 `.venv`，在 Windows 本机后台运行。
- Docker 只负责构建并运行 `refactor-agent-sandbox:py312`。
- 不增加高级参数或新的用户配置入口。

## 启动流程

1. 检查 Python、Git、Docker CLI 和 Docker daemon。
2. 创建仓库 `.venv`；仅在依赖缺失时安装 `.[desktop,dashboard]`。
3. 构建轻量 sandbox 镜像。该镜像只包含 Python 和 pytest，不安装 apt 软件包。
4. 固定设置本机运行目录和安全配置：
   - `REFACTOR_AGENT_RUN_ROOT=.runs`
   - `REFACTOR_AGENT_DATABASE=.runs/refactor_agent.sqlite`
   - `REFACTOR_AGENT_GITHUB_WORKSPACE_ROOT=.runs/github-workspaces`
   - `REFACTOR_AGENT_SANDBOX_BACKEND=docker`
5. 使用 `.venv` 后台启动 API、Streamlit Dashboard 和奶龙，并分别记录 PID。
6. 日志写入 `.runs/logs`；健康检查失败时直接显示日志尾部。
7. API 和 Dashboard 健康后打开 `http://127.0.0.1:8501`。

## 停止与重复启动

- `stop.cmd` 只停止 PID、解释器路径和命令行都匹配本仓库的三个进程。
- PID 文件丢失时，仅扫描并停止严格匹配本仓库路径的进程。
- 重复执行 `start.cmd` 时复用健康的本仓库进程，不重复启动。
- 停止不删除 `.runs`、数据库、日志或 sandbox 镜像。
- 旧 Compose volume 不自动删除；它退出日常启动链路后仍可人工恢复历史数据。

## 配置与安全

- `.env` 仍是唯一配置入口，只读取现有 allowlist，不打印密钥。
- 默认真实 DeepSeek；没有 Key 时仅禁用 LLM 任务，不隐式切换 mock。
- API 固定要求 Docker sandbox，绝不回退到本机 subprocess 执行用户代码。
- Dashboard 和 API 仅绑定 `127.0.0.1`。

## 代码与文档范围

- 重写 `scripts/start.ps1` 和 `scripts/stop.ps1` 的进程管理。
- `start.cmd`、`stop.cmd` 和固定端口不变。
- `compose.yaml` 与 `docker/app.Dockerfile` 保留为开发/部署材料，但不再属于一键启动。
- 更新 README、`docker/README.md`、启动契约测试和旧启动设计说明。

## 验收

- `start.ps1` 不包含 `docker compose` 或应用镜像构建。
- 本机 API、Dashboard、奶龙均能启动，API 与 Dashboard 健康检查返回 200。
- sandbox 镜像存在，API 能通过宿主 Docker CLI 执行一次隔离测试。
- 第二次启动不产生重复进程；`stop.cmd` 停止三个进程且保留数据。
- PowerShell 语法、完整 pytest、compileall、Dockerfile/Compose 静态校验和
  `git diff --check` 通过。
- pytest 在导入 API 模块前必须把默认数据库和运行目录隔离到系统临时目录，测试不得
  打开一键启动创建的 `.runs/refactor_agent.sqlite`。

## 实施结果

- 本机 API、Dashboard 和奶龙真实启动成功，健康检查均返回 200。
- 第二次启动复用三个原有 PID，没有创建重复进程。
- 受限 sandbox 真实运行并返回 `sandbox-ok`。
- `stop.cmd` 停止三个进程，8000/8501 端口和匹配进程均归零；`.runs` 和 sandbox
  镜像保留。
- 完整测试：573 passed、11 skipped、1 个既有 Starlette/httpx 弃用警告。
