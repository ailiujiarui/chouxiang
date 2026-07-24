from __future__ import annotations

import os
import ctypes
from ctypes import wintypes
from collections.abc import Callable
from pathlib import Path
from threading import Event, Thread

from nailong_agent.activity_collector import ForegroundActivitySource, ForegroundWindow


class _LastInputInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


class _MonitorInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


def idle_seconds_from_ticks(*, current_tick: int, last_input_tick: int) -> float:
    """Return idle duration from the 32-bit Windows tick counter."""
    return ((current_tick - last_input_tick) & 0xFFFF_FFFF) / 1000


def is_fullscreen_rectangle(window: tuple[int, int, int, int], monitor: tuple[int, int, int, int]) -> bool:
    return window == monitor


def read_idle_seconds(user32, kernel32) -> float | None:
    info = _LastInputInfo(cbSize=ctypes.sizeof(_LastInputInfo))
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        return None
    return idle_seconds_from_ticks(current_tick=kernel32.GetTickCount(), last_input_tick=info.dwTime)


def read_fullscreen_state(user32, hwnd) -> bool:
    window = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(window)):
        return False
    monitor_handle = user32.MonitorFromWindow(hwnd, 2)
    if not monitor_handle:
        return False
    monitor = _MonitorInfo(cbSize=ctypes.sizeof(_MonitorInfo))
    if not user32.GetMonitorInfoW(monitor_handle, ctypes.byref(monitor)):
        return False
    return is_fullscreen_rectangle(_rectangle_values(window), _rectangle_values(monitor.rcMonitor))


def _rectangle_values(rectangle: wintypes.RECT) -> tuple[int, int, int, int]:
    return rectangle.left, rectangle.top, rectangle.right, rectangle.bottom


class NullForegroundActivitySource:
    """Portable source used when Win32 foreground hooks are unavailable."""

    def start(self, on_change: Callable[[ForegroundWindow], None]) -> None:
        return None

    def stop(self) -> None:
        return None


def create_foreground_source() -> ForegroundActivitySource:
    if os.name != "nt":
        return NullForegroundActivitySource()
    return WindowsForegroundActivitySource()


class WindowsForegroundActivitySource:
    """Dedicated Win32 foreground hook thread that never reads window titles."""

    _EVENT_SYSTEM_FOREGROUND = 0x0003
    _WINEVENT_OUTOFCONTEXT = 0
    _WM_QUIT = 0x0012
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    def __init__(self, *, on_error: Callable[[Exception], None] | None = None) -> None:
        self.on_error = on_error
        self._callback: Callable[[ForegroundWindow], None] | None = None
        self._thread: Thread | None = None
        self._thread_id: int | None = None
        self._started = Event()
        self._stopped = Event()

    def start(self, on_change: Callable[[ForegroundWindow], None]) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._callback = on_change
        self._started.clear()
        self._stopped.clear()
        self._thread = Thread(target=self._run, name="nailong-foreground-hook", daemon=True)
        self._thread.start()
        self._started.wait(2.0)

    def stop(self) -> None:
        self._stopped.set()
        if self._thread_id is not None:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, self._WM_QUIT, 0, 0)
        if self._thread is not None:
            self._thread.join(2.0)
            self._thread = None
        self._thread_id = None

    def _run(self) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        callback_type = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_long, ctypes.c_long, ctypes.c_uint, ctypes.c_uint)

        def on_event(_, event, hwnd, *__):
            if event != self._EVENT_SYSTEM_FOREGROUND or self._stopped.is_set():
                return
            try:
                window = _foreground_window(user32, kernel32, hwnd, self._PROCESS_QUERY_LIMITED_INFORMATION)
                if window is not None and self._callback is not None:
                    self._callback(window)
            except Exception as exc:
                if self.on_error is not None:
                    self.on_error(exc)

        procedure = callback_type(on_event)
        self._thread_id = kernel32.GetCurrentThreadId()
        hook = user32.SetWinEventHook(
            self._EVENT_SYSTEM_FOREGROUND, self._EVENT_SYSTEM_FOREGROUND, 0, procedure, 0, 0, self._WINEVENT_OUTOFCONTEXT
        )
        self._started.set()
        if not hook:
            if self.on_error is not None:
                self.on_error(RuntimeError("foreground hook unavailable"))
            return
        message = wintypes.MSG()
        try:
            while not self._stopped.is_set() and user32.GetMessageW(ctypes.byref(message), 0, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        finally:
            user32.UnhookWinEvent(hook)


def _foreground_window(user32, kernel32, hwnd, access: int) -> ForegroundWindow | None:
    process_id = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
    if not process_id.value:
        return None
    process = kernel32.OpenProcess(access, False, process_id.value)
    if not process:
        return None
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        length = ctypes.c_ulong(len(buffer))
        if not kernel32.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(length)):
            return None
        return ForegroundWindow(
            process_id=process_id.value,
            executable_name=Path(buffer.value).name,
            idle_seconds=read_idle_seconds(user32, kernel32),
            is_fullscreen=read_fullscreen_state(user32, hwnd),
        )
    finally:
        kernel32.CloseHandle(process)
