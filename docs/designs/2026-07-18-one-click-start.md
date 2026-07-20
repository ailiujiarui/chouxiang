# 一键启动设计

> 认证更新（2026-07-20）：本文关于“默认或强制 Admin Token”的描述已被 `2026-07-20-local-no-admin-token.md` 取代。当前一键启动默认为 localhost 单用户无令牌模式；只有显式配置 `REFACTOR_AGENT_ADMIN_TOKEN` 时才启用 Bearer 校验。

日期：2026-07-18
状态：已实现、验证并完成 code review

## 目标

在 Windows 开发环境中通过一个 PowerShell 脚本启动可用的本地 API 和 Dashboard，不要求先配置真实 GitHub 或 DeepSeek 凭据。

## 启动方式

新增 `scripts/start.ps1`：

```powershell
./scripts/start.ps1
```

脚本执行前检查 Docker Desktop、Docker Compose 和仓库根目录；支持参数：

- `-Build`：重新构建镜像；
- `-Down`：停止并删除本次 Compose 服务；
- `-ApiPort` / `-DashboardPort`：覆盖宿主机端口；
- `-PythonBaseImage`：同时覆盖应用和 sandbox 的 Python 基础镜像；
- `-Follow`：前台跟随日志，默认后台启动。

脚本不读取或打印密钥，不修改用户全局环境变量。端口冲突在启动前给出明确错误。

## Compose 服务

在 `compose.yaml` 增加 `api` 服务：

- 与 Dashboard 使用同一镜像和 SQLite volume；
- 监听容器 `8000`，宿主机默认 `127.0.0.1:8000`；
- 运行 `serve --host 0.0.0.0 --port 8000`；
- 默认使用 `REFACTOR_AGENT_MOCK_LLM=true`、Docker sandbox、本地 Admin Token 和示例仓库 allowlist；
- 这些默认值只用于本地演示，不得作为生产凭据使用；环境变量可以覆盖它们。

Dashboard 依赖 API 健康检查后启动，默认使用 `http://api:8000` 容器内地址；宿主机访问地址仍为 `http://127.0.0.1:8501`。

## 安全与边界

- 默认 mock + local-only 不会调用 DeepSeek；系统已删除分支、推送、PR 和 Issue 评论能力。
- API 仍强制 Docker sandbox 和 Admin Token；脚本只提供本地演示 token，不暴露到命令输出。
- 真实部署必须显式设置 DeepSeek、Admin Token 和仓库 allowlist，并使用独立部署编排。
- `-Down` 只操作本项目 Compose project，不删除宿主机 `.runs` 或 Docker volume，除非用户显式执行 Compose volume 删除。

## 文档和验收

- README 和 `docker/README.md` 增加 Windows 一键启动、停止、访问地址和默认模式说明。
- 测试脚本参数解析、路径检查、Compose 配置渲染和默认服务命令；不在 CI 中要求 Docker Desktop。
- 本地可用 Docker 时验证 API `/health` 和 Dashboard `/_stcore/health` 返回 200。
- 完成 code review 和自我修复后，不自动提交、推送或部署。

## 实施记录

- 已新增 `api` Compose 服务、Dashboard 健康依赖和 localhost 端口绑定。
- 已新增 `scripts/start.ps1`，支持构建、停止、端口覆盖和跟随日志，并等待 API/Dashboard 健康检查。
- 默认配置为 mock + local-only，停止命令不删除 volume。
- 契约测试覆盖 Docker 检查、健康检查、Compose 依赖和安全默认值。
- Docker Desktop 未运行时脚本已验证会用明确错误退出。
- Code review 修复了 Dashboard CLI 默认地址覆盖 Compose 容器内 API 地址的问题，现显式传入 `http://api:8000`。
- DaoCloud registry mirror 出现 TLS/token 超时时，启动器必须把 `-PythonBaseImage` 同时传给 sandbox `docker build --build-arg` 和 Compose，错误信息应指出失败的是基础镜像拉取并显示可执行的覆盖示例。
- 实际容器启动日志证明 API 镜像缺少 Docker CLI；Debian Trixie 已将 CLI 拆为独立 `docker-cli` 包，因此应用镜像只安装客户端并通过挂载的宿主 socket 工作，不安装或启动容器内 Docker daemon。
- 最终 Docker smoke 通过：API `/health` 返回 200、Dashboard `/_stcore/health` 返回 200，API 容器通过挂载 socket 读取到 Docker Server 版本 `29.6.1`。
