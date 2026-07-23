from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from nailong_agent.events import PopupDecision
from nailong_agent.privacy import PrivacyConsent


@dataclass(frozen=True)
class BubblePlacement:
    x: int
    y: int
    tail_x: int


def place_bubble_above_pet(
    *,
    available: tuple[int, int, int, int],
    pet: tuple[int, int, int, int],
    bubble_size: tuple[int, int],
    screen_margin: int = 12,
    pet_gap: int = 6,
    tail_margin: int = 28,
) -> BubblePlacement:
    """Place a bubble above the pet and clamp it to the usable screen."""

    available_x, available_y, available_width, _ = available
    pet_x, pet_y, pet_width, _ = pet
    bubble_width, bubble_height = bubble_size
    minimum_x = available_x + screen_margin
    maximum_x = max(minimum_x, available_x + available_width - screen_margin - bubble_width)
    pet_center_x = pet_x + pet_width // 2
    x = min(max(pet_center_x - bubble_width // 2, minimum_x), maximum_x)
    minimum_y = available_y + screen_margin
    y = max(minimum_y, pet_y - bubble_height - pet_gap)
    tail_x = min(max(pet_center_x - x, tail_margin), max(tail_margin, bubble_width - tail_margin))
    return BubblePlacement(x=x, y=y, tail_x=tail_x)


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
            from PySide6.QtCore import QObject, QRectF, Qt, QTimer, Signal
            from PySide6.QtGui import (
                QAction,
                QColor,
                QFont,
                QPainter,
                QPainterPath,
                QPen,
                QTextDocument,
                QTextOption,
            )
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

        class SpeechBubble(QWidget):
            _horizontal_padding = 18
            _top_padding = 10
            _bottom_padding = 9
            _tail_height = 18
            _border_inset = 2
            _corner_radius = 20

            def __init__(
                bubble_self,
                message: str,
                *,
                maximum_width: int,
                maximum_height: int,
                parent: QWidget | None = None,
            ) -> None:
                super().__init__(parent)
                bubble_self.setWindowFlags(
                    Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus
                )
                bubble_self.setAttribute(Qt.WA_TranslucentBackground)
                bubble_self.setAttribute(Qt.WA_ShowWithoutActivating)
                bubble_self._tail_x = 60
                bubble_self._document = QTextDocument(bubble_self)
                bubble_self._document.setDocumentMargin(0)
                font = QFont(QApplication.font())
                font.setPointSize(10)
                bubble_self._document.setDefaultFont(font)
                text_options = bubble_self._document.defaultTextOption()
                text_options.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
                bubble_self._document.setDefaultTextOption(text_options)
                bubble_self._document.setPlainText(message)

                bubble_self._document.setTextWidth(-1)
                ideal_width = int(bubble_self._document.idealWidth()) + bubble_self._horizontal_padding * 2
                minimum_width = min(210, maximum_width)
                width = min(max(minimum_width, ideal_width), maximum_width)
                bubble_self._document.setTextWidth(width - bubble_self._horizontal_padding * 2)
                desired_height = int(bubble_self._document.size().height()) + (
                    bubble_self._top_padding
                    + bubble_self._bottom_padding
                    + bubble_self._tail_height
                    + bubble_self._border_inset * 2
                )
                height = min(max(58, desired_height), max(58, maximum_height))
                bubble_self.setFixedSize(width, height)

            def set_tail_x(bubble_self, tail_x: int) -> None:
                bubble_self._tail_x = tail_x

            def paintEvent(bubble_self, event: object) -> None:
                del event
                painter = QPainter(bubble_self)
                painter.setRenderHint(QPainter.Antialiasing, True)
                pen = QPen(QColor("#171717"), 3.2)
                pen.setCapStyle(Qt.RoundCap)
                pen.setJoinStyle(Qt.RoundJoin)
                painter.setPen(pen)
                painter.setBrush(QColor("#FFFFFF"))

                inset = bubble_self._border_inset
                left = float(inset)
                top = float(inset)
                right = float(bubble_self.width() - inset)
                body_bottom = float(bubble_self.height() - bubble_self._tail_height - inset)
                radius = float(
                    min(
                        bubble_self._corner_radius,
                        max(8, int((body_bottom - top) / 2)),
                    )
                )
                tail_x = float(bubble_self._tail_x)
                path = QPainterPath()
                path.moveTo(left + radius, top)
                path.lineTo(right - radius, top)
                path.quadTo(right, top, right, top + radius)
                path.lineTo(right, body_bottom - radius)
                path.quadTo(right, body_bottom, right - radius, body_bottom)
                path.lineTo(tail_x + 12, body_bottom)
                path.lineTo(tail_x + 3, float(bubble_self.height() - inset))
                path.lineTo(tail_x - 10, body_bottom)
                path.lineTo(left + radius, body_bottom)
                path.quadTo(left, body_bottom, left, body_bottom - radius)
                path.lineTo(left, top + radius)
                path.quadTo(left, top, left + radius, top)
                path.closeSubpath()
                painter.drawPath(path)

                text_left = left + bubble_self._horizontal_padding
                text_top = top + bubble_self._top_padding
                text_width = right - left - bubble_self._horizontal_padding * 2
                text_height = body_bottom - text_top - bubble_self._bottom_padding
                painter.save()
                painter.translate(text_left, text_top)
                painter.setClipRect(QRectF(0, 0, text_width, text_height))
                bubble_self._document.drawContents(painter, QRectF(0, 0, text_width, text_height))
                painter.restore()

        self._SpeechBubble = SpeechBubble
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
        pet_geometry = self._pet_window.frameGeometry()
        screen = self._QApplication.screenAt(pet_geometry.center()) or self._app.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        screen_margin = 12
        pet_gap = 6
        maximum_width = max(120, min(420, area.width() - screen_margin * 2))
        maximum_height = max(58, pet_geometry.top() - area.top() - screen_margin - pet_gap)
        popup = self._SpeechBubble(
            decision.message or "奶龙有话想说",
            maximum_width=maximum_width,
            maximum_height=maximum_height,
            parent=self._pet_window,
        )
        placement = place_bubble_above_pet(
            available=(area.x(), area.y(), area.width(), area.height()),
            pet=(pet_geometry.x(), pet_geometry.y(), pet_geometry.width(), pet_geometry.height()),
            bubble_size=(popup.width(), popup.height()),
            screen_margin=screen_margin,
            pet_gap=pet_gap,
        )
        popup.set_tail_x(placement.tail_x)
        popup.move(placement.x, placement.y)
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
