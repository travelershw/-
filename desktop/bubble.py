"""气泡窗：无边框、不抢焦点、不吃点击，只负责在她头顶显示一句话。"""

from PySide6.QtCore import QPoint, QRectF, Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QWidget

MAX_WIDTH = 260
PADDING = 12
TAIL = 9
BG = QColor(252, 250, 246, 242)
BORDER = QColor(120, 112, 128, 150)
TEXT = QColor(48, 44, 56)
DEFAULT_MS = 7000


class Bubble(QWidget):
    """A single speech bubble that fades out on its own."""

    def __init__(self) -> None:
        super().__init__(None)
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.WindowTransparentForInput
            | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self._text = ""
        self._font = QFont("Microsoft YaHei UI", 10)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide)

    def show_text(self, text: str, anchor: QPoint, msec: int = DEFAULT_MS) -> None:
        """Show a line above an anchor point.

        Args:
            text: Message to display.
            anchor: Screen point the bubble tail should point at (top-centre of the pet).
            msec: How long to keep it visible.
        """
        text = (text or "").strip()
        if not text:
            return
        self._text = text
        metrics = QFontMetrics(self._font)
        rect = metrics.boundingRect(
            QRectF(0, 0, MAX_WIDTH - 2 * PADDING, 1000).toRect(),
            Qt.TextWordWrap,
            text,
        )
        width = max(120, rect.width() + 2 * PADDING)
        height = rect.height() + 2 * PADDING + TAIL
        self.resize(width, height)
        self.move(anchor.x() - width // 2, anchor.y() - height)
        self.show()
        self.raise_()
        self._hide_timer.start(max(1500, msec))

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """Draw the rounded bubble and its tail.

        Args:
            event: Paint event (unused).
        """
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        body = QRectF(1, 1, self.width() - 2, self.height() - TAIL - 1)
        painter.setPen(QPen(BORDER, 1.4))
        painter.setBrush(QBrush(BG))
        painter.drawRoundedRect(body, 12, 12)
        tail = [
            QPoint(int(self.width() / 2) - 8, int(body.bottom()) - 1),
            QPoint(int(self.width() / 2) + 8, int(body.bottom()) - 1),
            QPoint(int(self.width() / 2), int(body.bottom()) + TAIL),
        ]
        painter.setBrush(QBrush(BG))
        painter.drawPolygon(tail)
        painter.setPen(QPen(TEXT))
        painter.setFont(self._font)
        painter.drawText(
            body.adjusted(PADDING, PADDING, -PADDING, -PADDING),
            Qt.TextWordWrap | Qt.AlignLeft | Qt.AlignVCenter,
            self._text,
        )
