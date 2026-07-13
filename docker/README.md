# Docker 记忆库运行说明

这个项目的轨迹记忆库仍然使用 SQLite，但默认数据库路径可以交给 Docker volume 管理：

- 容器内记忆库：`/data/refactor_agent.sqlite`
- 容器内运行目录：`/data/runs`
- Docker volume：`refactor-agent-memory`

## 构建镜像

```powershell
docker compose build refactor-agent
```

## 在 Docker 记忆库里跑一次 demo

```powershell
docker compose run --rm refactor-agent demo --timeout 30
docker compose run --rm refactor-agent memories --limit 10
```

真实 DeepSeek 调用时，临时传入 API Key，避免 `docker compose config` 误打印密钥：

```powershell
docker compose run --rm -e DEEPSEEK_API_KEY refactor-agent run --target src/refactor_agent/metrics.py --issue issue.md --tests tests
```

## 迁移本机已有记忆库到 Docker volume

如果本机已经有 `.runs/refactor_agent.sqlite`，可以复制到 Docker volume：

```powershell
docker compose run --rm --entrypoint sh refactor-agent -lc "cp /workspace/repo/.runs/refactor_agent.sqlite /data/refactor_agent.sqlite"
docker compose run --rm refactor-agent memories --limit 10
```

## 启动大屏

```powershell
docker compose up dashboard
```

访问：

```text
http://127.0.0.1:8501
```

## 说明

Webhook 执行必须使用 Docker。当前容器以非 root 用户运行，并启用只读根文件系统、无网络、capability 清空、`no-new-privileges`、PID/CPU/内存限制。宿主机 `subprocess` 后端只适用于可信本地代码，不属于安全沙箱。

SQLite 不是数据库服务，所以这里没有单独起一个“memory db server”。更稳的做法是让 `refactor-agent` 容器把 SQLite 文件写入 Docker volume。这样容器删了也不丢记忆，换机器时只要迁移 Docker volume 或导出 `/data/refactor_agent.sqlite` 即可。

## 镜像源提示

清华 TUNA 提供的是 Docker CE 等软件包镜像，不是稳定可用的 Docker Hub registry mirror。不要把下面这个地址写进 Docker daemon 的 `registry-mirrors`：

```text
https://docker.mirrors.tuna.tsinghua.edu.cn
```

如果 Docker Hub 直连不可用，需要换成实际可用的 registry mirror，或者提前在本机导入 `python:3.12-slim` 基础镜像。当前 `docker/app.Dockerfile` 支持用构建参数替换基础镜像：

```powershell
$env:PYTHON_BASE_IMAGE="your-registry.example.com/library/python:3.12-slim"
docker compose build refactor-agent
```
