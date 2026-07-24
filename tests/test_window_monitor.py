# -*- coding: utf-8 -*-
"""window_monitor 单元测试"""

import pytest

from refactor_agent.window_monitor import (
    AppCategory,
    WindowInfo,
    WindowMonitor,
    _detect_terminal_type,
    _parse_editor_file,
    classify_process,
)


class TestClassifyProcess:
    def test_ide(self):
        assert classify_process("Code.exe") == AppCategory.IDE
        assert classify_process("PYCHARM64.exe") == AppCategory.IDE

    def test_terminal(self):
        assert classify_process("cmd.exe") == AppCategory.TERMINAL

    def test_browser(self):
        assert classify_process("chrome.exe") == AppCategory.BROWSER

    def test_unknown(self):
        assert classify_process("xyz.exe") == AppCategory.OTHER

    def test_empty(self):
        assert classify_process("") == AppCategory.OTHER


class TestParseEditorFile:
    def test_vscode(self):
        assert _parse_editor_file("main.py - proj - Visual Studio Code", "Code.exe") == "main.py"

    def test_pycharm(self):
        assert _parse_editor_file("app.py - myproj - PyCharm", "pycharm64.exe") == "app.py"

    def test_notepadpp(self):
        assert _parse_editor_file("cfg.ini - Notepad++", "notepad++.exe") == "cfg.ini"

    def test_unknown_editor(self):
        assert _parse_editor_file("some title", "unknown.exe") is None

    def test_empty_title(self):
        assert _parse_editor_file("", "Code.exe") is None


class TestDetectTerminalType:
    def test_powershell(self):
        assert _detect_terminal_type("PS", "powershell.exe") == "powershell"

    def test_cmd(self):
        assert _detect_terminal_type("CMD", "cmd.exe") == "cmd"

    def test_wt_powershell(self):
        assert _detect_terminal_type("PowerShell - WT", "WindowsTerminal.exe") == "powershell"

    def test_wt_wsl(self):
        assert _detect_terminal_type("ubuntu - WT", "WindowsTerminal.exe") == "wsl"

    def test_unknown(self):
        assert _detect_terminal_type("x", "notepad.exe") is None


class TestWindowInfo:
    def test_defaults(self):
        wi = WindowInfo()
        assert wi.title == ""
        assert wi.process_name == ""

    def test_equality(self):
        assert WindowInfo(title="a", process_name="b") == WindowInfo(title="a", process_name="b", hwnd=999)
        assert WindowInfo(title="a") != WindowInfo(title="b")


class TestWindowMonitor:
    def test_should_allow_empty(self):
        m = WindowMonitor()
        assert m._should_allow("anything.exe") is True

    def test_should_allow_whitelist(self):
        m = WindowMonitor(process_whitelist={"Code.exe"})
        assert m._should_allow("Code.exe") is True
        assert m._should_allow("chrome.exe") is False

    def test_should_allow_blacklist(self):
        m = WindowMonitor(process_blacklist={"explorer.exe"})
        assert m._should_allow("explorer.exe") is False
        assert m._should_allow("code.exe") is True

    def test_should_allow_whitelist_overrides_blacklist(self):
        m = WindowMonitor(process_whitelist={"code.exe"}, process_blacklist={"code.exe"})
        assert m._should_allow("code.exe") is True

    def test_should_allow_default_deny(self):
        m = WindowMonitor(default_deny=True)
        assert m._should_allow("anything.exe") is False

    def test_whitelist_with_default_deny(self):
        m = WindowMonitor(process_whitelist={"code.exe"}, default_deny=True)
        assert m._should_allow("code.exe") is True
        assert m._should_allow("chrome.exe") is False

    def test_screenshot_disabled_raises(self):
        m = WindowMonitor()
        with pytest.raises(RuntimeError, match="截图未启用"):
            m.capture_screenshot()

    def test_ocr_disabled_raises(self):
        m = WindowMonitor()
        with pytest.raises(RuntimeError, match="OCR 未启用"):
            m.ocr_screenshot()

    def test_screenshot_ocr_flags(self):
        m = WindowMonitor(enable_screenshot=True, enable_ocr=True)
        assert m.screenshot_enabled is True
        assert m.ocr_enabled is True
        assert m.running is False


# ---------------------------------------------------------------------------
# 集成测试（mock Windows API）
# ---------------------------------------------------------------------------

@pytest.fixture
def _mock_win_api(monkeypatch):
    class FakeProcess:
        @staticmethod
        def name():
            return "Code.exe"

    class FakePSUtil:
        NoSuchProcess = type("NoSuchProcess", (Exception,), {})
        AccessDenied = type("AccessDenied", (Exception,), {})

        @staticmethod
        def Process(pid):
            return FakeProcess()

    class FakeWin32Process:
        @staticmethod
        def GetWindowThreadProcessId(hwnd):
            return (None, 1234)

    class FakeWin32Gui:
        @staticmethod
        def GetForegroundWindow():
            return 42

        @staticmethod
        def GetWindowText(hwnd):
            return "test.py - proj - Visual Studio Code"

    monkeypatch.setattr(
        "refactor_agent.window_monitor.WindowMonitor._ensure_imports",
        lambda self: (
            setattr(self, "_win32gui", FakeWin32Gui()),
            setattr(self, "_win32process", FakeWin32Process()),
            setattr(self, "_psutil", FakePSUtil()),
        ),
    )
    return FakeWin32Gui, FakeWin32Process, FakePSUtil


class TestWindowMonitorIntegration:
    def test_start_stop(self, _mock_win_api):
        m = WindowMonitor()
        m.start()
        assert m.running is True
        m.stop()
        assert m.running is False

    def test_window_change_callback(self, _mock_win_api):
        events = []

        def on_change(wi: WindowInfo):
            events.append(wi)

        m = WindowMonitor(on_window_change=on_change, poll_interval=0.05)
        m.start()
        import time
        time.sleep(0.3)
        m.stop()
        assert len(events) >= 1
        assert events[0].process_name == "Code.exe"

    def test_blacklist_filters(self, _mock_win_api):
        events = []
        m = WindowMonitor(
            on_window_change=lambda wi: events.append(wi),
            process_blacklist={"code.exe"},
            poll_interval=0.05,
        )
        m.start()
        import time
        time.sleep(0.3)
        m.stop()
        assert len(events) == 0

    def test_double_start_safe(self, _mock_win_api):
        m = WindowMonitor()
        m.start()
        m.start()  # 不应抛异常
        m.stop()
