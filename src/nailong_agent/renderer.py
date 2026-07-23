from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from nailong_agent.events import PopupDecision
from nailong_agent.privacy import PrivacyConsent


class PopupRenderer(Protocol):
    def start(self) -> None: ...

    def show(self, decision: PopupDecision) -> bool | None: ...

    def stop(self) -> None: ...

    def exec(self) -> int: ...


class PrivacyControlsRenderer(Protocol):
    """Optional renderer extension; legacy renderers remain compatible."""

    def request_privacy_consent(self) -> PrivacyConsent | None: ...

    def configure_privacy_controls(self, *, on_clear_activity_history: Callable[[], int]) -> None: ...


class NotificationControlsRenderer(Protocol):
    """Optional all-day do-not-disturb control exposed by desktop renderers."""

    def configure_notification_controls(
        self,
        *,
        on_set_do_not_disturb: Callable[[bool], None],
        get_do_not_disturb: Callable[[], bool],
    ) -> None: ...


class NullRenderer:
    """Headless renderer used by tests and command-line smoke runs."""

    def __init__(self) -> None:
        self.decisions: list[PopupDecision] = []
        self.started = False
        self.consent_response: PrivacyConsent | None = None
        self.consent_requested = False
        self._on_clear_activity_history: Callable[[], int] | None = None
        self._on_set_do_not_disturb: Callable[[bool], None] | None = None
        self._get_do_not_disturb: Callable[[], bool] | None = None

    def start(self) -> None:
        self.started = True

    def show(self, decision: PopupDecision) -> bool:
        if decision.action == "show":
            self.decisions.append(decision)
            return True
        return False

    def stop(self) -> None:
        self.started = False

    def exec(self) -> int:
        return 0

    def request_privacy_consent(self) -> PrivacyConsent | None:
        self.consent_requested = True
        return self.consent_response

    def configure_privacy_controls(self, *, on_clear_activity_history: Callable[[], int]) -> None:
        self._on_clear_activity_history = on_clear_activity_history

    def configure_notification_controls(
        self,
        *,
        on_set_do_not_disturb: Callable[[bool], None],
        get_do_not_disturb: Callable[[], bool],
    ) -> None:
        self._on_set_do_not_disturb = on_set_do_not_disturb
        self._get_do_not_disturb = get_do_not_disturb

    def set_do_not_disturb(self, enabled: bool) -> None:
        if self._on_set_do_not_disturb is None:
            raise RuntimeError("notification controls are not configured")
        self._on_set_do_not_disturb(enabled)


class PySide6Renderer:
    """Minimal transparent pet window and popup adapter.

    PySide6 is imported lazily so the core event model remains usable on CI and
    on machines that only run collectors or headless tests.
    """

    def __init__(self, *, on_quit: Callable[[], None] | None = None) -> None:
        try:
            from PySide6.QtCore import QObject, Qt, QTimer, Signal
            from PySide6.QtGui import QAction
            from PySide6.QtWidgets import QApplication, QCheckBox, QLabel, QMessageBox, QMenu, QSystemTrayIcon, QWidget
        except ImportError as exc:
            raise RuntimeError("PySide6 is required for the desktop renderer; install refactor-agent[desktop].") from exc

        self._QApplication = QApplication
        self._QCheckBox = QCheckBox
        self._QLabel = QLabel
        self._QMessageBox = QMessageBox
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
        self._on_clear_activity_history: Callable[[], int] | None = None
        self._on_set_do_not_disturb: Callable[[bool], None] | None = None
        self._tray = None
        self._dnd_action = None

        class Bridge(QObject):
            decision = Signal(object)

        self._bridge = Bridge()
        self._bridge.decision.connect(self._show_on_ui_thread)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self._tray = QSystemTrayIcon(self._pet_window)
            self._tray.setIcon(self._app.style().standardIcon(self._app.style().StandardPixmap.SP_ComputerIcon))  # 占位图标
            menu = QMenu()
            self._dnd_action = QAction("全天免打扰", menu)
            self._dnd_action.setCheckable(True)
            self._dnd_action.setEnabled(False)
            self._dnd_action.triggered.connect(self._set_do_not_disturb)
            menu.addAction(self._dnd_action)
            clear_history_action = QAction("删除本地活动记录", menu)
            clear_history_action.triggered.connect(self._clear_activity_history)
            menu.addAction(clear_history_action)
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

    def show(self, decision: PopupDecision) -> bool:
        if decision.action == "show":
            self._bridge.decision.emit(decision)
            return True
        return False

    def stop(self) -> None:
        if self._tray is not None:
            self._tray.hide()
        self._pet_window.close()
        for popup in self._popups:
            popup.close()
        self._popups.clear()

    def exec(self) -> int:
        return self._app.exec()

    def request_privacy_consent(self) -> PrivacyConsent | None:
        dialog = self._QMessageBox(self._pet_window)
        dialog.setIcon(self._QMessageBox.Information)
        dialog.setWindowTitle("奶龙活动陪伴授权")
        dialog.setText("是否允许奶龙仅在本机识别有限的桌面活动信号？")
        dialog.setInformativeText(
            "默认不会采集截图、OCR、剪贴板、完整窗口标题、终端正文或源代码。"
            "密码、Token、SSH/Auth 文件和会议窗口会被禁止采集。"
        )
        dialog.setStandardButtons(self._QMessageBox.Yes | self._QMessageBox.No)
        dialog.setDefaultButton(self._QMessageBox.No)
        remote = self._QCheckBox("同时允许将脱敏摘要发送给 DeepSeek（默认关闭）")
        remote.setChecked(False)
        dialog.setCheckBox(remote)
        accepted = dialog.exec() == self._QMessageBox.Yes
        return PrivacyConsent(
            activity_collection_enabled=accepted,
            remote_inference_enabled=accepted and remote.isChecked(),
        )

    def configure_privacy_controls(self, *, on_clear_activity_history: Callable[[], int]) -> None:
        self._on_clear_activity_history = on_clear_activity_history

    def configure_notification_controls(
        self,
        *,
        on_set_do_not_disturb: Callable[[bool], None],
        get_do_not_disturb: Callable[[], bool],
    ) -> None:
        self._on_set_do_not_disturb = on_set_do_not_disturb
        if self._dnd_action is not None:
            self._dnd_action.setChecked(get_do_not_disturb())
            self._dnd_action.setEnabled(True)

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

    def _set_do_not_disturb(self, enabled: bool) -> None:
        if self._on_set_do_not_disturb is not None:
            self._on_set_do_not_disturb(enabled)

    def _clear_activity_history(self) -> None:
        deleted = self._on_clear_activity_history() if self._on_clear_activity_history is not None else 0
        if self._tray is not None:
            self._tray.showMessage("奶龙", f"已删除 {deleted} 条本地活动记录。")

    def _quit(self) -> None:
        if self._on_quit is not None:
            self._on_quit()
        self._app.quit()
