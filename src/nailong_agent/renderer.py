from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from nailong_agent.events import PopupDecision


class PopupRenderer(Protocol):
    def start(self) -> None: ...

    def show(self, decision: PopupDecision) -> None: ...

    def stop(self) -> None: ...

    def exec(self) -> int: ...


class NullRenderer:
    """Headless renderer used by tests and command-line smoke runs."""

    def __init__(self) -> None:
        self.decisions: list[PopupDecision] = []
        self.started = False

    def start(self) -> None:
        self.started = True

    def show(self, decision: PopupDecision) -> None:
        if decision.action == "show":
            self.decisions.append(decision)

    def stop(self) -> None:
        self.started = False

    def exec(self) -> int:
        return 0


class PySide6Renderer:
    """Minimal transparent pet window and popup adapter.

    PySide6 is imported lazily so the core event model remains usable on CI and
    on machines that only run collectors or headless tests.
    """

    def __init__(self, *, on_quit: Callable[[], None] | None = None) -> None:
        try:
            from PySide6.QtCore import QObject, Qt, QTimer, Signal
            from PySide6.QtGui import QAction
            from PySide6.QtWidgets import QApplication, QLabel, QMenu, QSystemTrayIcon, QWidget
        except ImportError as exc:
            raise RuntimeError("PySide6 is required for the desktop renderer; install refactor-agent[desktop].") from exc

        self._QApplication = QApplication
        self._QLabel = QLabel
        self._QMenu = QMenu
        self._QSystemTrayIcon = QSystemTrayIcon
        self._QWidget = QWidget
        self._QTimer = QTimer
        self._Qt = Qt
        self._app = QApplication.instance() or QApplication([])
        self._pet_window = QWidget()
        self._pet_window.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self._pet_window.setAttribute(Qt.WA_TranslucentBackground)
        self._pet_window.setFixedSize(180, 120)
        pet_label = QLabel("奶龙待机中", self._pet_window)
        pet_label.setAlignment(Qt.AlignCenter)
        pet_label.setStyleSheet(
            "background:#FDE68A; color:#713F12; border:2px solid #F59E0B; border-radius:18px; padding:12px;"
        )
        pet_label.setGeometry(0, 0, 180, 120)
        self._popups: list[QLabel] = []
        self._on_quit = on_quit
        self._tray = None
        self._paused = False

        class Bridge(QObject):
            decision = Signal(object)

        self._bridge = Bridge()
        self._bridge.decision.connect(self._show_on_ui_thread)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self._tray = QSystemTrayIcon(self._pet_window)
            menu = QMenu()
            pause_action = QAction("暂停", menu)
            pause_action.triggered.connect(self._toggle_pause)
            menu.addAction(pause_action)
            quit_action = QAction("退出", menu)
            quit_action.triggered.connect(self._quit)
            menu.addAction(quit_action)
            self._tray.setContextMenu(menu)

    def start(self) -> None:
        screen = self._app.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            self._pet_window.move(area.right() - self._pet_window.width() - 24, area.bottom() - self._pet_window.height() - 24)
        self._pet_window.show()
        if self._tray is not None:
            self._tray.show()

    def show(self, decision: PopupDecision) -> None:
        if decision.action == "show" and not self._paused:
            self._bridge.decision.emit(decision)

    def stop(self) -> None:
        if self._tray is not None:
            self._tray.hide()
        self._pet_window.close()
        for popup in self._popups:
            popup.close()
        self._popups.clear()

    def exec(self) -> int:
        return self._app.exec()

    def _show_on_ui_thread(self, decision: PopupDecision) -> None:
        popup = self._QLabel(decision.message or "奶龙有话想说", self._pet_window)
        popup.setWindowFlags(self._Qt.Tool | self._Qt.FramelessWindowHint | self._Qt.WindowStaysOnTopHint)
        popup.setStyleSheet("background:#FEF3C7; color:#451A03; border:1px solid #D97706; padding:10px;")
        popup.adjustSize()
        popup.show()
        self._popups.append(popup)
        self._QTimer.singleShot(decision.display_seconds * 1000, lambda: self._close_popup(popup))

    def _close_popup(self, popup: object) -> None:
        popup.close()
        if popup in self._popups:
            self._popups.remove(popup)

    def _toggle_pause(self) -> None:
        self._paused = not self._paused

    def _quit(self) -> None:
        if self._on_quit is not None:
            self._on_quit()
        self._app.quit()
