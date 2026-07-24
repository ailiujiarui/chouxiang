# -*- coding: utf-8 -*-
"""
Windows 桌面活动采集模块

功能:
- 获取当前活动窗口标题、进程名、应用分类
- 可选：编辑器文件名解析、终端类型识别
- 截图与 OCR（默认关闭，需用户明确授权）
- 进程白名单 / 黑名单过滤

使用方式:
    from refactor_agent.window_monitor import WindowMonitor, WindowInfo

    def on_change(wi: WindowInfo):
        print(f"[{wi.app_category}] {wi.process_name}: {wi.title}")

    # 最简用法
    m = WindowMonitor(on_window_change=on_change)
    m.start()

    # 完整用法
    m = WindowMonitor(
        on_window_change=on_change,
        enable_editor_state=True,
        enable_screenshot=False,
        enable_ocr=False,
        process_whitelist={"Code.exe", "WindowsTerminal.exe"},
        process_blacklist={"explorer.exe"},
        default_deny=False,
    )
    m.start()
    # ... 运行中 ...
    m.stop()
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 应用分类
# ---------------------------------------------------------------------------

class AppCategory(StrEnum):
    IDE = "IDE"
    TERMINAL = "TERMINAL"
    BROWSER = "BROWSER"
    EDITOR = "EDITOR"
    CHAT = "CHAT"
    OTHER = "OTHER"


_PROCESS_CATEGORY_MAP: dict[str, AppCategory] = {
    "code.exe": AppCategory.IDE,
    "devenv.exe": AppCategory.IDE,
    "pycharm64.exe": AppCategory.IDE,
    "idea64.exe": AppCategory.IDE,
    "rider64.exe": AppCategory.IDE,
    "webstorm64.exe": AppCategory.IDE,
    "cursor.exe": AppCategory.IDE,
    "windsurf.exe": AppCategory.IDE,
    "code-insiders.exe": AppCategory.IDE,
    "notepad++.exe": AppCategory.EDITOR,
    "sublime_text.exe": AppCategory.EDITOR,
    "sublimetext.exe": AppCategory.EDITOR,
    "windowsterminal.exe": AppCategory.TERMINAL,
    "cmd.exe": AppCategory.TERMINAL,
    "powershell.exe": AppCategory.TERMINAL,
    "pwsh.exe": AppCategory.TERMINAL,
    "terminal.exe": AppCategory.TERMINAL,
    "conhost.exe": AppCategory.TERMINAL,
    "chrome.exe": AppCategory.BROWSER,
    "firefox.exe": AppCategory.BROWSER,
    "msedge.exe": AppCategory.BROWSER,
    "brave.exe": AppCategory.BROWSER,
    "opera.exe": AppCategory.BROWSER,
    "qq.exe": AppCategory.CHAT,
    "wechat.exe": AppCategory.CHAT,
    "wechatapp.exe": AppCategory.CHAT,
    "dingtalk.exe": AppCategory.CHAT,
    "teams.exe": AppCategory.CHAT,
    "slack.exe": AppCategory.CHAT,
    "feishu.exe": AppCategory.CHAT,
    "lark.exe": AppCategory.CHAT,
}


def classify_process(process_name: str) -> AppCategory:
    """根据进程名判断应用类型"""
    if not process_name:
        return AppCategory.OTHER
    return _PROCESS_CATEGORY_MAP.get(process_name.lower(), AppCategory.OTHER)


# ---------------------------------------------------------------------------
# IDE 标题解析
# ---------------------------------------------------------------------------

_VSCODE_TITLE_RE = re.compile(
    r"^(?P<file>.+?)\s+[-—]\s+.+?\s+[-—]\s+(?:Visual Studio Code|Cursor|Windsurf)"
)
_JETBRAINS_TITLE_RE = re.compile(
    r"^(?P<file>.+?)\s+[-—]\s+.+?\s+[-—]\s+(?:PyCharm|IntelliJ IDEA|WebStorm|Rider|GoLand|CLion)"
)
_JETBRAINS_BRACKET_RE = re.compile(
    r"^(?P<file>.+?)\s+[-—]\s+\[.+\]\s+[-—]\s+(?:PyCharm|IntelliJ IDEA|WebStorm|Rider)"
)
_NOTEPADPP_TITLE_RE = re.compile(r"^(?P<file>.+?)\s+[-—]\s+Notepad\+\+")
_SUBLIME_TITLE_RE = re.compile(r"^(?P<file>.+?)(?:\s*\(.+?\))?\s+[-—]\s+Sublime Text")
_GENERIC_EDITOR_RE = re.compile(
    r"^(?P<file>.+\.(?:py|js|ts|jsx|tsx|rs|go|java|cpp|c|h|hpp|css|html|json|xml|yaml|yml|md|txt|toml|cfg|ini))\s+[-—]"
)


def _parse_editor_file(title: str, process_name: str) -> str | None:
    """从窗口标题解析正在编辑的文件名"""
    if not title:
        return None
    pname = process_name.lower()
    if pname in ("code.exe", "cursor.exe", "windsurf.exe", "code-insiders.exe"):
        m = _VSCODE_TITLE_RE.match(title)
        if m:
            return m.group("file").strip()
    if pname in ("pycharm64.exe", "idea64.exe", "webstorm64.exe", "rider64.exe"):
        for regex in (_JETBRAINS_TITLE_RE, _JETBRAINS_BRACKET_RE):
            m = regex.match(title)
            if m:
                return m.group("file").strip()
    if pname == "notepad++.exe":
        m = _NOTEPADPP_TITLE_RE.match(title)
        if m:
            return m.group("file").strip()
    if pname in ("sublime_text.exe", "sublimetext.exe"):
        m = _SUBLIME_TITLE_RE.match(title)
        if m:
            return m.group("file").strip()
    m = _GENERIC_EDITOR_RE.match(title)
    if m:
        return m.group("file").strip()
    return None


# ---------------------------------------------------------------------------
# 终端类型识别
# ---------------------------------------------------------------------------

def _detect_terminal_type(title: str, process_name: str) -> str | None:
    """识别终端类型"""
    pname = process_name.lower()
    if pname == "windowsterminal.exe":
        t = title.lower()
        if "powershell" in t or "pwsh" in t:
            return "powershell"
        if "command prompt" in t or "cmd" in t:
            return "cmd"
        if any(kw in t for kw in ("wsl", "ubuntu", "debian")):
            return "wsl"
        return "windows_terminal"
    if pname == "powershell.exe":
        return "powershell"
    if pname == "pwsh.exe":
        return "pwsh"
    if pname == "cmd.exe":
        return "cmd"
    if pname == "conhost.exe":
        t = title.lower()
        if "powershell" in t:
            return "powershell"
        return "cmd"
    return None


# ---------------------------------------------------------------------------
# WindowInfo
# ---------------------------------------------------------------------------

@dataclass
class WindowInfo:
    """活动窗口信息"""
    title: str = ""
    process_name: str = ""
    hwnd: int = 0
    pid: int = 0
    app_category: AppCategory = AppCategory.OTHER
    terminal_type: str | None = None
    editor_file: str | None = None

    def __eq__(self, other: object) -> bool:
        if isinstance(other, WindowInfo):
            return self.title == other.title and self.process_name == other.process_name
        return False

    def __hash__(self) -> int:
        return hash((self.title, self.process_name))


# ---------------------------------------------------------------------------
# 便捷函数：一次性获取活动窗口
# ---------------------------------------------------------------------------

def get_active_window_info() -> WindowInfo:
    """获取当前活动窗口信息（一次性调用）"""
    try:
        import win32gui
        import win32process
        import psutil

        hwnd = win32gui.GetForegroundWindow()
        title = win32gui.GetWindowText(hwnd)
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        try:
            name = psutil.Process(pid).name()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            name = ""
        return _build_window_info(hwnd, title, pid, name)
    except ImportError:
        logger.warning("缺少 pywin32 / psutil，无法获取活动窗口")
        return WindowInfo()
    except Exception:
        logger.debug("获取活动窗口失败", exc_info=True)
        return WindowInfo()


# ---------------------------------------------------------------------------
# WindowMonitor
# ---------------------------------------------------------------------------

class WindowMonitor:
    """Windows 桌面活动监控器

    轮询前台窗口，检测窗口切换并通过回调通知。
    """

    def __init__(
        self,
        on_window_change: Callable[[WindowInfo], None] | None = None,
        poll_interval: float = 0.2,
        enable_editor_state: bool = False,
        enable_screenshot: bool = False,
        enable_ocr: bool = False,
        process_whitelist: set[str] | None = None,
        process_blacklist: set[str] | None = None,
        default_deny: bool = False,
    ):
        self._on_window_change = on_window_change
        self._poll_interval = poll_interval
        self._enable_editor_state = enable_editor_state
        self._enable_screenshot = enable_screenshot
        self._enable_ocr = enable_ocr

        # 进程过滤
        self._whitelist: set[str] = {p.lower() for p in process_whitelist} if process_whitelist else set()
        self._blacklist: set[str] = {p.lower() for p in process_blacklist} if process_blacklist else set()
        self._default_deny = bool(default_deny)

        # 运行时状态
        self._last_window: WindowInfo | None = None
        self._last_screenshot = None  # PIL Image | None
        self._running = False

        # 轮询线程
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

        # 进程名缓存
        self._process_cache: dict[int, str] = {}

        # 延迟导入
        self._win32gui: object = None
        self._win32process: object = None
        self._psutil: object = None
        self._ImageGrab: object = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self) -> None:
        """启动窗口监控"""
        if self._running:
            return
        self._ensure_imports()
        if self._enable_screenshot:
            self._ensure_screenshot_imports()

        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        logger.info("WindowMonitor: 已启动")

    def stop(self) -> None:
        """停止窗口监控"""
        if not self._running:
            return
        self._stop_event.set()
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._thread = None
        logger.info("WindowMonitor: 已停止")

    # ------------------------------------------------------------------
    # 公开属性
    # ------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._running

    @property
    def last_window(self) -> WindowInfo | None:
        return self._last_window

    @property
    def last_screenshot(self):
        return self._last_screenshot

    @property
    def screenshot_enabled(self) -> bool:
        return self._enable_screenshot

    @property
    def ocr_enabled(self) -> bool:
        return self._enable_ocr

    # ------------------------------------------------------------------
    # 截图
    # ------------------------------------------------------------------

    def capture_screenshot(self):
        """手动截取当前屏幕。需要 enable_screenshot=True。"""
        if not self._enable_screenshot:
            raise RuntimeError("截图未启用，请在构造时设置 enable_screenshot=True")
        self._ensure_screenshot_imports()
        img = self._ImageGrab.grab()
        self._last_screenshot = img
        return img

    # ------------------------------------------------------------------
    # OCR
    # ------------------------------------------------------------------

    def ocr_screenshot(self, image=None) -> str:
        """对截图进行 OCR，返回文字。需要 enable_ocr=True。"""
        if not self._enable_ocr:
            raise RuntimeError("OCR 未启用，请在构造时设置 enable_ocr=True")
        try:
            import pytesseract
        except ImportError:
            raise RuntimeError("需要 pytesseract: pip install pytesseract") from None

        img = image or self._last_screenshot
        if img is None:
            img = self.capture_screenshot()
        return pytesseract.image_to_string(img) or ""

    # ------------------------------------------------------------------
    # 轮询循环
    # ------------------------------------------------------------------

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception:
                logger.debug("WindowMonitor: 轮询异常", exc_info=True)
            self._stop_event.wait(self._poll_interval)

    def _tick(self) -> None:
        current = self._get_active_window()
        if current is None:
            return

        if self._last_window is None or current != self._last_window:
            self._last_window = current
            if self._should_allow(current.process_name):
                if self._enable_screenshot:
                    self._capture_to_cache()
                self._safe_callback(self._on_window_change, current)

    # ------------------------------------------------------------------
    # 进程过滤
    # ------------------------------------------------------------------

    def _should_allow(self, process_name: str) -> bool:
        """uBlock 风格过滤：白名单 > 黑名单 > 隐式拒绝 > default_deny > 放行"""
        pname = process_name.lower()
        if self._whitelist and pname in self._whitelist:
            return True
        if self._blacklist and pname in self._blacklist:
            return False
        if self._whitelist:
            return False
        if self._default_deny:
            return False
        return True

    # ------------------------------------------------------------------
    # 活动窗口获取
    # ------------------------------------------------------------------

    def _get_active_window(self) -> WindowInfo | None:
        try:
            hwnd = self._win32gui.GetForegroundWindow()
            title = self._win32gui.GetWindowText(hwnd)
            _, pid = self._win32process.GetWindowThreadProcessId(hwnd)
            name = self._resolve_name(pid)
            return _build_window_info(hwnd, title, pid, name, enable_editor_state=self._enable_editor_state)
        except Exception:
            logger.debug("获取活动窗口失败", exc_info=True)
            return None

    def _resolve_name(self, pid: int) -> str:
        if pid in self._process_cache:
            return self._process_cache[pid]
        try:
            name = self._psutil.Process(pid).name()
        except (self._psutil.NoSuchProcess, self._psutil.AccessDenied):
            name = ""
        self._process_cache[pid] = name
        return name

    def _capture_to_cache(self) -> None:
        try:
            self._last_screenshot = self._ImageGrab.grab()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 依赖
    # ------------------------------------------------------------------

    def _ensure_imports(self) -> None:
        if self._win32gui is not None:
            return
        try:
            import win32gui
            import win32process
            import psutil
            self._win32gui = win32gui
            self._win32process = win32process
            self._psutil = psutil
        except ImportError as e:
            raise RuntimeError("需要 pywin32 和 psutil: pip install pywin32 psutil") from e

    def _ensure_screenshot_imports(self) -> None:
        if self._ImageGrab is not None:
            return
        try:
            from PIL import ImageGrab
            self._ImageGrab = ImageGrab
        except ImportError as e:
            raise RuntimeError("截图需要 Pillow: pip install Pillow") from e

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_callback(callback, *args) -> None:
        if callback is None:
            return
        try:
            callback(*args)
        except Exception:
            logger.debug("WindowMonitor: 回调异常", exc_info=True)


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

def _build_window_info(
    hwnd: int, title: str, pid: int, process_name: str,
    *, enable_editor_state: bool = False,
) -> WindowInfo:
    """根据原始窗口数据构建 WindowInfo"""
    category = classify_process(process_name)

    terminal_type: str | None = None
    if category == AppCategory.TERMINAL:
        terminal_type = _detect_terminal_type(title, process_name)

    editor_file: str | None = None
    if enable_editor_state and category in (AppCategory.IDE, AppCategory.EDITOR):
        editor_file = _parse_editor_file(title, process_name)

    return WindowInfo(
        title=title,
        process_name=process_name,
        hwnd=hwnd,
        pid=pid,
        app_category=category,
        terminal_type=terminal_type,
        editor_file=editor_file,
    )
