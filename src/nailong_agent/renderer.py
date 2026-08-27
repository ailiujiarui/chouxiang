from __future__ import annotations

import logging
from collections.abc import Callable

from nailong_agent.events import PetExpression, PopupDecision
from nailong_agent.health import NailongHealthSnapshot
from nailong_agent.pet_state import PetPersonalityState
from nailong_agent.privacy import PrivacyConsent
from nailong_agent.renderer_core import (
    MOUTH_TEXT,
    PopupDeliveryResult,
    clamp_window_position,
    decision_to_pet_state,
    personality_state_to_pet_state,
    place_bubble_above_pet,
)
from nailong_agent.windows_activity import foreground_window_is_fullscreen


logger = logging.getLogger(__name__)


class PySide6Renderer:
    """Text-only Nailong face with a status bubble above the pet."""

    def __init__(
        self,
        *,
        on_quit: Callable[[], None] | None = None,
        on_click: Callable[[], None] | None = None,
        fullscreen_probe: Callable[[], bool] = foreground_window_is_fullscreen,
    ) -> None:
        try:
            from PySide6.QtCore import QObject, QPoint, QRectF, Qt, QTimer, Signal
            from PySide6.QtGui import (
                QAction,
                QColor,
                QFont,
                QPainter,
                QPainterPath,
                QPen,
                QTextCharFormat,
                QTextCursor,
                QTextDocument,
                QTextOption,
            )
            from PySide6.QtWidgets import (
                QApplication,
                QCheckBox,
                QLabel,
                QMessageBox,
                QMenu,
                QPushButton,
                QSystemTrayIcon,
                QWidget,
            )
        except ImportError as exc:
            raise RuntimeError(
                "PySide6 is required for the desktop renderer; install refactor-agent[desktop]."
            ) from exc

        self._QApplication = QApplication
        self._QCheckBox = QCheckBox
        self._QLabel = QLabel
        self._QMessageBox = QMessageBox
        self._QMenu = QMenu
        self._QPoint = QPoint
        self._QSystemTrayIcon = QSystemTrayIcon
        self._QWidget = QWidget
        self._QTimer = QTimer
        self._Qt = Qt
        self._app = QApplication.instance() or QApplication([])
        self._fullscreen_probe = fullscreen_probe
        self._on_popup_delivery: PopupDeliveryResult | None = None

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
                on_first_paint: Callable[[], None] | None = None,
            ) -> None:
                super().__init__(parent)
                bubble_self.setWindowFlags(
                    Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus
                )
                bubble_self.setAttribute(Qt.WA_TranslucentBackground)
                bubble_self.setAttribute(Qt.WA_ShowWithoutActivating)
                bubble_self._tail_x = 60
                bubble_self._on_first_paint = on_first_paint
                bubble_self._first_paint_reported = False
                bubble_self._document = QTextDocument(bubble_self)
                bubble_self._document.setDocumentMargin(0)
                font = QFont(QApplication.font())
                font.setPointSize(10)
                bubble_self._document.setDefaultFont(font)
                text_options = bubble_self._document.defaultTextOption()
                text_options.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
                bubble_self._document.setDefaultTextOption(text_options)
                bubble_self._document.setPlainText(message)
                text_format = QTextCharFormat()
                text_format.setForeground(QColor("#171717"))
                text_cursor = QTextCursor(bubble_self._document)
                text_cursor.select(QTextCursor.Document)
                text_cursor.mergeCharFormat(text_format)

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
                if not bubble_self._first_paint_reported:
                    bubble_self._first_paint_reported = True
                    if bubble_self._on_first_paint is not None:
                        QTimer.singleShot(0, bubble_self._on_first_paint)

        self._SpeechBubble = SpeechBubble
        self._pet_window = QWidget()
        self._pet_window.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self._pet_window.setAttribute(Qt.WA_TranslucentBackground)
        self._pet_window.setFixedSize(240, 190)
        self._on_click = on_click
        self._drag_origin = None
        self._drag_window_origin = None
        self._dragging = False
        self._drag_threshold = QApplication.startDragDistance()

        renderer = self

        class PetBody(QLabel):
            def mousePressEvent(body_self, event: object) -> None:
                if event.button() == Qt.LeftButton:
                    renderer._begin_drag(event.globalPosition().toPoint())
                    event.accept()
                    return
                super().mousePressEvent(event)

            def mouseMoveEvent(body_self, event: object) -> None:
                if event.buttons() & Qt.LeftButton:
                    renderer._update_drag(event.globalPosition().toPoint())
                    event.accept()
                    return
                super().mouseMoveEvent(event)

            def mouseReleaseEvent(body_self, event: object) -> None:
                if event.button() == Qt.LeftButton:
                    renderer._end_drag()
                    event.accept()
                    return
                super().mouseReleaseEvent(event)

        self._body = PetBody(self._pet_window)
        self._body.setGeometry(0, 0, 240, 150)
        self._body.setCursor(Qt.OpenHandCursor)
        self._body.setStyleSheet(
            "background:#FDE68A; border:2px solid #F59E0B; border-radius:28px;"
        )
        face_font = QFont("Microsoft YaHei UI", 13)
        self._left_eye = QLabel("（眼睛）", self._pet_window)
        self._right_eye = QLabel("（眼睛）", self._pet_window)
        self._mouth = QLabel(MOUTH_TEXT[PetExpression.NEUTRAL], self._pet_window)
        for label in (self._left_eye, self._right_eye, self._mouth):
            label.setFont(face_font)
            label.setAlignment(Qt.AlignCenter)
            label.setAttribute(Qt.WA_TransparentForMouseEvents)
            label.setStyleSheet("color:#713F12; background:transparent; border:none;")
        self._left_eye.setGeometry(24, 36, 88, 34)
        self._right_eye.setGeometry(128, 36, 88, 34)
        self._mouth.setGeometry(62, 88, 116, 36)
        self._settings_button = QPushButton("设置", self._pet_window)
        self._settings_button.setGeometry(72, 156, 96, 28)
        self._settings_button.setCursor(Qt.PointingHandCursor)
        self._settings_button.setStyleSheet(
            "QPushButton { background:#FFFFFF; color:#713F12; border:1px solid #D97706; "
            "border-radius:6px; font:12px 'Microsoft YaHei UI'; }"
            "QPushButton:hover { background:#FFFBEB; }"
            "QPushButton:pressed { background:#FEF3C7; }"
        )
        self._popups: list[QLabel] = []
        self._popup_timers: list[QTimer] = []
        self._on_quit = on_quit
        self._on_clear_activity_history: Callable[[], int] | None = None
        self._get_privacy_consent: Callable[[], PrivacyConsent] | None = None
        self._on_save_privacy_consent: Callable[[PrivacyConsent], None] | None = None
        self._on_set_do_not_disturb: Callable[[bool], None] | None = None
        self._on_set_manual_pause: Callable[[bool], None] | None = None
        self._get_manual_pause: Callable[[], bool] | None = None
        self._get_do_not_disturb: Callable[[], bool] | None = None
        self._on_set_game_tease: Callable[[bool], None] | None = None
        self._get_game_tease: Callable[[], bool] | None = None
        self._on_set_python_review: Callable[[bool], None] | None = None
        self._get_python_review: Callable[[], bool] | None = None
        self._get_health_snapshot: Callable[[], NailongHealthSnapshot] | None = None
        self._tray = None

        self._settings_menu = QMenu(self._pet_window)
        self._privacy_action = QAction("活动陪伴授权...", self._pet_window)
        self._privacy_action.setEnabled(False)
        self._privacy_action.triggered.connect(self._edit_privacy_consent)
        self._dnd_action = QAction("全天免打扰", self._pet_window)
        self._dnd_action.setCheckable(True)
        self._dnd_action.setEnabled(False)
        self._dnd_action.triggered.connect(self._set_do_not_disturb)
        self._pause_action = QAction("暂停监听", self._pet_window)
        self._pause_action.setCheckable(True)
        self._pause_action.setEnabled(False)
        self._pause_action.triggered.connect(self._set_manual_pause)
        self._game_tease_action = QAction("游戏中允许轻微吐槽", self._pet_window)
        self._game_tease_action.setCheckable(True)
        self._game_tease_action.setEnabled(False)
        self._game_tease_action.triggered.connect(self._set_game_tease)
        self._python_review_action = QAction("自动评测已保存的 Python 代码", self._pet_window)
        self._python_review_action.setCheckable(True)
        self._python_review_action.setEnabled(False)
        self._python_review_action.triggered.connect(self._set_python_review)
        self._test_bubble_action = QAction("立即测试气泡", self._pet_window)
        self._test_bubble_action.triggered.connect(self._show_test_bubble)
        self._health_action = QAction("当前状态...", self._pet_window)
        self._health_action.setEnabled(False)
        self._health_action.triggered.connect(self._show_health_status)
        self._clear_history_action = QAction("删除本地活动记录", self._pet_window)
        self._clear_history_action.setEnabled(False)
        self._clear_history_action.triggered.connect(self._clear_activity_history)
        self._quit_action = QAction("退出奶龙", self._pet_window)
        self._quit_action.triggered.connect(self._quit)
        self._settings_menu.addAction(self._privacy_action)
        self._settings_menu.addSeparator()
        self._settings_menu.addAction(self._pause_action)
        self._settings_menu.addAction(self._dnd_action)
        self._settings_menu.addAction(self._game_tease_action)
        self._settings_menu.addAction(self._python_review_action)
        self._settings_menu.addAction(self._health_action)
        self._settings_menu.addAction(self._test_bubble_action)
        self._settings_menu.addSeparator()
        self._settings_menu.addAction(self._clear_history_action)
        self._settings_menu.addAction(self._quit_action)
        self._settings_menu.aboutToShow.connect(self._sync_settings_actions)
        self._settings_button.clicked.connect(self._show_settings_menu)

        class Bridge(QObject):
            decision = Signal(object)

        self._bridge = Bridge()
        self._bridge.decision.connect(self._show_on_ui_thread)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self._tray = QSystemTrayIcon(self._pet_window)
            self._tray.setIcon(self._app.style().standardIcon(self._app.style().StandardPixmap.SP_ComputerIcon))  # 占位图标
            menu = QMenu()
            menu.addAction(self._privacy_action)
            menu.addSeparator()
            menu.addAction(self._pause_action)
            menu.addAction(self._dnd_action)
            menu.addAction(self._game_tease_action)
            menu.addAction(self._python_review_action)
            menu.addAction(self._health_action)
            menu.addAction(self._test_bubble_action)
            menu.addSeparator()
            menu.addAction(self._clear_history_action)
            menu.addAction(self._quit_action)
            self._tray.setContextMenu(menu)

    def start(self) -> None:
        screen = self._app.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            self._pet_window.move(
                area.right() - self._pet_window.width() - 24,
                area.bottom() - self._pet_window.height() - 24,
            )
        self._pet_window.show()
        if self._tray is not None:
            self._tray.show()
        self.show(
            PopupDecision(
                action="show",
                reason="idle",
                message="（待机中）",
                display_seconds=30,
            )
        )

    def show(self, decision: PopupDecision) -> bool:
        if decision.action == "show":
            self._bridge.decision.emit(decision)
            return True
        return False

    def can_present_popup(self) -> bool:
        return not self._fullscreen_probe()

    def configure_popup_delivery(self, on_result: PopupDeliveryResult) -> None:
        self._on_popup_delivery = on_result

    def _handle_pet_click(self) -> None:
        if self._on_click is not None:
            self._on_click()
            return
        self.show(
            PopupDecision(
                action="show",
                reason="pet_click",
                message="哼，本龙听见了。有什么事就说。",
                display_seconds=5,
            )
        )

    def _begin_drag(self, global_position: object) -> None:
        self._drag_origin = global_position
        self._drag_window_origin = self._pet_window.pos()
        self._dragging = False
        self._body.setCursor(self._Qt.ClosedHandCursor)

    def _update_drag(self, global_position: object) -> bool:
        if self._drag_origin is None or self._drag_window_origin is None:
            return False
        delta = global_position - self._drag_origin
        if not self._dragging and delta.manhattanLength() < self._drag_threshold:
            return False
        self._dragging = True
        desired = self._drag_window_origin + delta
        screen = self._QApplication.screenAt(global_position) or self._app.primaryScreen()
        if screen is None:
            return False
        area = screen.availableGeometry()
        x, y = clamp_window_position(
            available=(area.x(), area.y(), area.width(), area.height()),
            window_size=(self._pet_window.width(), self._pet_window.height()),
            desired=(desired.x(), desired.y()),
        )
        self._pet_window.move(x, y)
        self._reposition_popups()
        return True

    def _end_drag(self) -> bool:
        was_dragging = self._dragging
        self._drag_origin = None
        self._drag_window_origin = None
        self._dragging = False
        self._body.setCursor(self._Qt.OpenHandCursor)
        if not was_dragging:
            self._handle_pet_click()
        return was_dragging

    def apply_personality_state(self, state: PetPersonalityState) -> None:
        self._mouth.setText(MOUTH_TEXT[personality_state_to_pet_state(state).expression])

    def stop(self) -> None:
        if self._tray is not None:
            self._tray.hide()
        self._pet_window.close()
        self._dismiss_all_popups()

    def exec(self) -> int:
        return self._app.exec()

    def request_privacy_consent(self) -> PrivacyConsent | None:
        current = self._get_privacy_consent() if self._get_privacy_consent is not None else None
        return self._request_privacy_consent(current)

    def _request_privacy_consent(self, current: PrivacyConsent | None) -> PrivacyConsent:
        dialog = self._QMessageBox(self._pet_window)
        dialog.setIcon(self._QMessageBox.Information)
        dialog.setWindowTitle("奶龙活动陪伴授权")
        dialog.setText("是否允许奶龙仅在本机识别有限的桌面活动信号？")
        dialog.setInformativeText(
            "默认不会采集截图、OCR、剪贴板、完整窗口标题、终端正文或源代码。"
            "密码、Token、SSH/Auth 文件和会议窗口会被禁止采集。"
        )
        dialog.setStandardButtons(self._QMessageBox.Yes | self._QMessageBox.No)
        dialog.setDefaultButton(
            self._QMessageBox.Yes
            if current is not None and current.activity_collection_enabled
            else self._QMessageBox.No
        )
        remote = self._QCheckBox("同时允许将脱敏摘要发送给 DeepSeek（默认关闭）")
        remote.setChecked(bool(current and current.remote_inference_enabled))
        dialog.setCheckBox(remote)
        accepted = dialog.exec() == self._QMessageBox.Yes
        return PrivacyConsent(
            activity_collection_enabled=accepted,
            remote_inference_enabled=accepted and remote.isChecked(),
            python_review_enabled=bool(current and current.python_review_enabled),
        )

    def configure_privacy_controls(
        self,
        *,
        on_clear_activity_history: Callable[[], int],
        get_privacy_consent: Callable[[], PrivacyConsent] | None = None,
        on_save_privacy_consent: Callable[[PrivacyConsent], None] | None = None,
    ) -> None:
        self._on_clear_activity_history = on_clear_activity_history
        self._get_privacy_consent = get_privacy_consent
        self._on_save_privacy_consent = on_save_privacy_consent
        self._privacy_action.setEnabled(
            self._get_privacy_consent is not None and self._on_save_privacy_consent is not None
        )
        self._clear_history_action.setEnabled(True)

    def configure_notification_controls(
        self,
        *,
        on_set_do_not_disturb: Callable[[bool], None],
        get_do_not_disturb: Callable[[], bool],
        on_set_manual_pause: Callable[[bool], None] | None = None,
        get_manual_pause: Callable[[], bool] | None = None,
        on_set_game_tease: Callable[[bool], None] | None = None,
        get_game_tease: Callable[[], bool] | None = None,
        on_set_python_review: Callable[[bool], None] | None = None,
        get_python_review: Callable[[], bool] | None = None,
    ) -> None:
        self._on_set_do_not_disturb = on_set_do_not_disturb
        self._on_set_manual_pause = on_set_manual_pause
        self._get_manual_pause = get_manual_pause
        self._get_do_not_disturb = get_do_not_disturb
        self._on_set_game_tease = on_set_game_tease
        self._get_game_tease = get_game_tease
        self._on_set_python_review = on_set_python_review
        self._get_python_review = get_python_review
        self._dnd_action.setChecked(get_do_not_disturb())
        self._dnd_action.setEnabled(True)
        self._pause_action.setChecked(get_manual_pause() if get_manual_pause else False)
        self._pause_action.setEnabled(on_set_manual_pause is not None)
        self._game_tease_action.setChecked(get_game_tease() if get_game_tease else False)
        self._game_tease_action.setEnabled(on_set_game_tease is not None)
        self._python_review_action.setChecked(get_python_review() if get_python_review else False)
        self._python_review_action.setEnabled(on_set_python_review is not None)

    def configure_health_controls(
        self,
        *,
        get_health_snapshot: Callable[[], NailongHealthSnapshot],
    ) -> None:
        self._get_health_snapshot = get_health_snapshot
        self._health_action.setEnabled(True)

    def _show_health_status(self) -> None:
        if self._get_health_snapshot is None:
            return
        summary = self._get_health_snapshot().redacted_summary()
        dialog = self._QMessageBox(self._pet_window)
        dialog.setIcon(self._QMessageBox.Information)
        dialog.setWindowTitle("奶龙当前状态")
        dialog.setText("本地运行诊断")
        dialog.setInformativeText(summary)
        dialog.setStandardButtons(self._QMessageBox.Close)
        copy_button = dialog.addButton("复制脱敏摘要", self._QMessageBox.ActionRole)
        dialog.exec()
        if dialog.clickedButton() is copy_button:
            self._app.clipboard().setText(summary)

    def _show_settings_menu(self) -> None:
        self._sync_settings_actions()
        self._settings_menu.ensurePolished()
        hint = self._settings_menu.sizeHint()
        anchor = self._settings_button.mapToGlobal(self._settings_button.rect().topLeft())
        screen = self._QApplication.screenAt(anchor) or self._app.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            x = min(max(anchor.x(), area.left()), max(area.left(), area.right() - hint.width() + 1))
            above_y = anchor.y() - hint.height() - 6
            y = above_y if above_y >= area.top() else min(area.bottom() - hint.height() + 1, anchor.y() + 34)
            anchor = self._QPoint(x, y)
        self._settings_menu.popup(anchor)

    def _sync_settings_actions(self) -> None:
        if self._get_do_not_disturb is not None:
            self._dnd_action.setChecked(self._get_do_not_disturb())
        if self._get_manual_pause is not None:
            self._pause_action.setChecked(self._get_manual_pause())
        if self._get_game_tease is not None:
            self._game_tease_action.setChecked(self._get_game_tease())
        if self._get_python_review is not None:
            self._python_review_action.setChecked(self._get_python_review())
        if self._get_privacy_consent is not None:
            consent = self._get_privacy_consent()
            state = "已开启" if consent.activity_collection_enabled else "已关闭"
            self._privacy_action.setText(f"活动陪伴授权（{state}）...")

    def _edit_privacy_consent(self) -> None:
        if self._get_privacy_consent is None or self._on_save_privacy_consent is None:
            return
        consent = self._request_privacy_consent(self._get_privacy_consent())
        self._on_save_privacy_consent(consent)
        self._sync_settings_actions()
        if self._tray is not None:
            state = "已开启" if consent.activity_collection_enabled else "已关闭"
            self._tray.showMessage("奶龙", f"活动陪伴授权{state}。")

    def _set_manual_pause(self, enabled: bool) -> None:
        if self._on_set_manual_pause is not None:
            self._on_set_manual_pause(enabled)

    def _set_game_tease(self, enabled: bool) -> None:
        if self._on_set_game_tease is not None:
            self._on_set_game_tease(enabled)

    def _set_python_review(self, enabled: bool) -> None:
        if enabled and not self._confirm_python_review():
            self._python_review_action.blockSignals(True)
            self._python_review_action.setChecked(False)
            self._python_review_action.blockSignals(False)
            return
        if self._on_set_python_review is not None:
            self._on_set_python_review(enabled)

    def _confirm_python_review(self) -> bool:
        dialog = self._QMessageBox(self._pet_window)
        dialog.setIcon(self._QMessageBox.Warning)
        dialog.setWindowTitle("自动 Python 评测授权")
        dialog.setText("允许奶龙把刚保存的 Python 源码发送给 DeepSeek 做静态评测吗？")
        dialog.setInformativeText(
            "只读取启动目录或 NAILONG_PYTHON_REVIEW_ROOTS 下的 .py 文件；源码不会写入活动数据库。"
        )
        dialog.setStandardButtons(self._QMessageBox.Yes | self._QMessageBox.No)
        dialog.setDefaultButton(self._QMessageBox.No)
        return dialog.exec() == self._QMessageBox.Yes

    def _show_test_bubble(self) -> None:
        if not self.can_present_popup():
            reason = (
                self._get_health_snapshot().silence_reason
                if self._get_health_snapshot is not None
                else "fullscreen_blocked"
            )
            if self._tray is not None:
                self._tray.showMessage(
                    "奶龙",
                    f"测试气泡未显示：{reason}。退出全屏后可再次测试。",
                )
            return
        self.show(
            PopupDecision(
                action="show",
                reason="local_test_bubble",
                message="哼，本龙在说话。现在总该看见了吧。",
                display_seconds=12,
            )
        )

    def _show_on_ui_thread(self, decision: PopupDecision) -> None:
        try:
            self._create_popup_on_ui_thread(decision)
        except Exception:
            logger.exception("popup rendering failed")
            self._report_popup_delivery(decision, "failed")

    def _create_popup_on_ui_thread(self, decision: PopupDecision) -> None:
        # Close all existing popups and cancel their timers so at most one bubble is visible.
        self._dismiss_all_popups()
        state = decision_to_pet_state(decision)
        self._mouth.setText(MOUTH_TEXT[state.expression])
        pet_geometry = self._pet_window.frameGeometry()
        screen = self._QApplication.screenAt(pet_geometry.center()) or self._app.primaryScreen()
        if screen is None:
            self._report_popup_delivery(decision, "failed")
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
            on_first_paint=lambda: self._report_popup_delivery(decision, "shown"),
        )
        self._position_popup(popup, screen_margin=screen_margin, pet_gap=pet_gap)
        popup.show()
        popup.raise_()
        self._popups.append(popup)
        timer = self._QTimer(self._pet_window)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: self._close_popup(popup))
        timer.start(decision.display_seconds * 1000)
        self._popup_timers.append(timer)

    def _report_popup_delivery(self, decision: PopupDecision, outcome: str) -> None:
        if decision.dedupe_key and self._on_popup_delivery is not None:
            self._on_popup_delivery(decision.dedupe_key, outcome)

    def _position_popup(self, popup: object, *, screen_margin: int = 12, pet_gap: int = 6) -> None:
        pet_geometry = self._pet_window.frameGeometry()
        screen = self._QApplication.screenAt(pet_geometry.center()) or self._app.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        placement = place_bubble_above_pet(
            available=(area.x(), area.y(), area.width(), area.height()),
            pet=(pet_geometry.x(), pet_geometry.y(), pet_geometry.width(), pet_geometry.height()),
            bubble_size=(popup.width(), popup.height()),
            screen_margin=screen_margin,
            pet_gap=pet_gap,
        )
        popup.set_tail_x(placement.tail_x)
        popup.move(placement.x, placement.y)

    def _reposition_popups(self) -> None:
        for popup in self._popups:
            self._position_popup(popup)

    def _dismiss_all_popups(self) -> None:
        """Close every visible popup and cancel their auto-close timers."""
        for timer in self._popup_timers:
            timer.stop()
        self._popup_timers.clear()
        for popup in self._popups:
            popup.close()
        self._popups.clear()

    def _close_popup(self, popup: object) -> None:
        popup.close()
        if popup in self._popups:
            self._popups.remove(popup)

    def _set_do_not_disturb(self, enabled: bool) -> None:
        if self._on_set_do_not_disturb is not None:
            self._on_set_do_not_disturb(enabled)

    def _clear_activity_history(self) -> None:
        deleted = self._on_clear_activity_history() if self._on_clear_activity_history is not None else 0
        message = f"已删除 {deleted} 条本地活动记录。"
        self.show(
            PopupDecision(
                action="show",
                reason="activity_history_cleared",
                message=message,
                display_seconds=5,
            )
        )
        if self._tray is not None:
            self._tray.showMessage("奶龙", message)

    def _quit(self) -> None:
        if self._on_quit is not None:
            self._on_quit()
        self._app.quit()
