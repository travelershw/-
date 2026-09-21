"""抓屏幕：桌宠的"看看我的屏幕"功能。

设计上守两条线：

1. **只在你点的时候抓**（右键菜单 / 聊天窗的 📷 按钮），没有任何定时或后台截图；
2. 抓之前先把桌宠和气泡**藏起来**，免得她"看到自己"，抓完再显示。

抓完存到 ``pet_desktop/shots/``，并把图交给 desktp_pet 通道发进管线——主模型不支持视觉时
AstrBot 会自动用配好的图片描述模型转成文字（配置里 ``default_image_caption_provider_id``）。
"""

import time
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter, QPixmap

import paths

ROOT = paths.BASE
SHOTS = paths.SHOTS
# 传给模型前先缩到这个宽度（屏幕 1440 宽的话基本是原尺寸；4K/多屏时才明显缩小）
MAX_WIDTH = 1600
KEEP_SHOTS = 20


@dataclass
class Shot:
    """One screenshot."""

    path: str = ""
    width: int = 0
    height: int = 0
    screens: int = 1
    label: str = ""
    bytes: int = 0
    error: str = ""

    def describe(self) -> str:
        """One-line summary for the bubble/log.

        Returns:
            Chinese summary.
        """
        if self.error:
            return f"截图失败：{self.error}"
        return f"{self.label}　{self.width}x{self.height}　{self.bytes / 1024:.0f} KB"


def _grab_screen(screen) -> QPixmap:  # noqa: ANN001 - QScreen
    """Grab one screen.

    Args:
        screen: QScreen to capture.

    Returns:
        The screenshot pixmap.
    """
    return screen.grabWindow(0)


def capture(which: str = "primary", hide: bool = False) -> Shot:
    """Take a screenshot of the primary screen or of every screen combined.

    Args:
        which: ``primary``（鼠标所在的那块屏）或 ``all``（所有屏拼成一张）.
        hide: 抓之前先让调用方把窗口藏起来（由调用方负责，参数只用于日志）.

    Returns:
        The captured shot (``error`` set on failure).
    """
    shot = Shot()
    app = QGuiApplication.instance()
    if app is None:
        shot.error = "没有图形环境"
        return shot
    screens = QGuiApplication.screens()
    if not screens:
        shot.error = "没有找到显示器"
        return shot
    shot.screens = len(screens)
    try:
        if which == "all" and len(screens) > 1:
            union = screens[0].geometry()
            for screen in screens[1:]:
                union = union.united(screen.geometry())
            canvas = QImage(union.size(), QImage.Format_ARGB32)
            canvas.fill(Qt.black)
            painter = QPainter(canvas)
            for screen in screens:
                geometry = screen.geometry()
                pixmap = _grab_screen(screen)
                painter.drawPixmap(geometry.topLeft() - union.topLeft(), pixmap)
            painter.end()
            image = canvas
            shot.label = f"全部 {len(screens)} 块屏 {union.width()}x{union.height()}"
        else:
            cursor = QGuiApplication.primaryScreen().availableGeometry()
            target = QGuiApplication.screenAt(cursor.center()) or screens[0]
            pixmap = _grab_screen(target)
            image = pixmap.toImage()
            geometry = target.geometry()
            # 注意：屏幕的"逻辑尺寸"可能小于像素尺寸（Windows 缩放），抓到的图是像素尺寸
            shot.label = (
                f"{'主屏' if target is screens[0] else '屏幕'} "
                f"{geometry.width()}x{geometry.height()}"
                f"（像素 {image.width()}x{image.height()}，缩放 {image.width() / max(1, geometry.width()):.2f}）"
            )
    except Exception as exc:  # noqa: BLE001 - 抓屏失败不该把桌宠带崩
        shot.error = f"{type(exc).__name__}: {exc}"
        return shot

    if image.width() > MAX_WIDTH:
        image = image.scaledToWidth(MAX_WIDTH, Qt.SmoothTransformation)
    SHOTS.mkdir(parents=True, exist_ok=True)
    path = SHOTS / f"shot_{time.strftime('%m%d_%H%M%S')}.png"
    if not image.save(str(path), "PNG"):
        shot.error = "写文件失败"
        return shot
    shot.path = str(path)
    shot.width = image.width()
    shot.height = image.height()
    shot.bytes = path.stat().st_size
    _trim()
    return shot


def _trim() -> None:
    """Keep only the newest ``KEEP_SHOTS`` screenshots."""
    files = sorted(SHOTS.glob("shot_*.png"), key=lambda path: path.stat().st_mtime)
    for path in files[:-KEEP_SHOTS]:
        try:
            path.unlink()
        except OSError:
            pass


def latest_text_hint() -> str:
    """Default question used when the user just clicks "看看我的屏幕".

    Returns:
        A Chinese instruction to send together with the screenshot.
    """
    return "看看我屏幕上有什么，用一两句话说说你看到了什么、有没有值得提醒我的地方。"


def selftest() -> None:
    """Capture once and report what came back (no Qt window needed)."""
    shot = capture()
    print(f"    {shot.describe()}")
    if shot.path:
        image = QImage(shot.path)
        print(f"    文件可读: {not image.isNull()}　尺寸 {image.width()}x{image.height()}")
        print(f"    屏幕数: {shot.screens}　目录: {SHOTS}")


if __name__ == "__main__":
    import sys

    QGuiApplication(sys.argv)
    selftest()
