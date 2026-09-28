# Nailong 桌面端打包

把奶龙桌宠打成 Windows 免安装桌面程序，用于真实场景测试。

## 构建

```powershell
.venv\Scripts\python.exe -m pip install pyinstaller
.\packaging\build_desktop.ps1
```

产物：`dist\Nailong\Nailong.exe`（onedir，需连同整个 `dist\Nailong` 文件夹一起分发）。

## 运行与配置

- 双击 `Nailong.exe` 启动；首次会弹出活动陪伴/授权对话框。
- 配置来源（优先级：真实环境变量 > 同目录 `.env`）：
  - `DEEPSEEK_API_KEY`：真实锐评必需；缺失时自动降级为本地 mock。
  - `NAILONG_PYTHON_REVIEW_ROOTS`：分号分隔的代码根目录；留空时用启动目录。
  - `NAILONG_ANALYSIS_URL`：连接本地 Refactor Agent API 时填写，如 `http://127.0.0.1:8000`。
  - `NAILONG_DATA_DIR`：数据目录，默认 `%LOCALAPPDATA%\Nailong`。
- 在同目录放一个 `.env`（可从仓库根 `.env.example` 复制），例如：

```dotenv
DEEPSEEK_API_KEY=sk-...
NAILONG_PYTHON_REVIEW_ROOTS=C:\path\to\your\project
```

- 命令行仍可用：`Nailong.exe --headless --data-dir <dir>` 用于无界面自检。
- 单实例：重复启动会被 `nailong-agent.lock` 拒绝。

## 说明

- 打包只包含奶龙桌宠（PySide6 + `nailong_agent` 及其依赖的 `refactor_agent` 模块：AST、LLM、SQLite 运行时）。
- 不含 Docker/Refactor Agent API；需要完整代码审判工作台时另行运行 `start.cmd`。
- 桌面端会读取并本地分析前台 IDE 的 `.py` 文件（需授权），自动 Python 锐评由 `nailong_agent.python_review` 提供。
