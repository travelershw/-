"""气泡窗：无边框、不抢焦点、不吃点击，只负责在她头顶显示一句话。"""

from PySide6.QtCore import QPoint, QRectF, Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QWidget

MAX_WIDTH = 260
PADDING = 12
TAIL = 9
# 边框宽：paintEvent 里 body 从 (1,1) 开始、宽高各减 2，所以可用的文字宽度/高度都要把这 2px 扣掉。
# 这里踩过坑（2026-09-25）：量尺寸时按 `MAX_WIDTH - 2*PADDING` 算，画的时候却在 `body` 上再缩
# `PADDING`——于是**画布比量尺寸时窄 4px、矮 1px**，换行位置都不一样，多出来的那一行被切在底部，
# 再被 AlignVCenter 上下各切一半。用户看到的就是"气泡上下被裁掉一部分"（2 倍缩放屏上更明显）。
# 注意别用 `BORDER` 这个名字，下面是同名的边框**颜色**。
BORDER_WIDTH = 1
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
        # 量尺寸用的框必须**和真正画文字的那个框完全一致**（宽度：去掉边框与左右内边距），
        # 否则换行位置不同，最后一行会被切掉。
        inner_width = MAX_WIDTH - 2 * PADDING - 2 * BORDER_WIDTH
        rect = metrics.boundingRect(
            QRectF(0, 0, inner_width, 1000).toRect(),
            Qt.TextWordWrap,
            text,
        )
        width = max(120, rect.width() + 2 * PADDING + 2 * BORDER_WIDTH)
        # 高度多给 2px 富余：字体度量在不同缩放/字体回退下会有 1px 级别的出入，
        # 宁可气泡略微宽松，也不要把首行/末行切掉。
        height = rect.height() + 2 * PADDING + TAIL + 2 * BORDER_WIDTH + 2
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
