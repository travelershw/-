"""用摄像头看她一眼：**单帧、用完即弃、只在明确的点击之后**。

摄像头比截图敏感得多，所以约定比 :mod:`screen` 更严：

1. **只在明确的点击后抓**（右键菜单）。没有任何定时抓拍；
2. 一次只抓**一帧**，发出去之后就把文件删掉，不留在 `shots/` 里；
3. 抓之前先把桌宠和气泡**藏起来**（免得拍到自己）；
4. 拿不到就报错——**不猜、不拿旧帧顶替**；
5. 她可以**提议**看一眼，但提议只是气泡上的一句话，**必须由你点菜单**才算同意。

提议与抓拍的频率判断都写成纯函数（:func:`should_propose` / :func:`can_capture`），
时间由调用方注入，所以能离线测试、不依赖真实摄像头。
"""

import time
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QEventLoop, QTimer

import paths

ROOT = paths.BASE
SHOTS = paths.SHOTS
# 传给模型前缩到这个宽度（同 screen.py 的口径）
MAX_WIDTH = 1600
# 抓拍冷却：连着点也只认一次
CAPTURE_COOLDOWN_SECONDS = 30
# 提议的冷却与每日上限：避免变成骚扰
PROPOSE_COOLDOWN_SECONDS = 2 * 3600
PROPOSE_DAILY_LIMIT = 3
# 你"在电脑前"的判定：闲置不超过这么久、且没锁屏
PRESENT_IDLE_SECONDS = 300
# 抓完多久删文件：留一点时间让上传读完，然后立刻清掉
DELETE_AFTER_SECONDS = 60
# 暖机：摄像头刚 start() 出来的头几帧还没曝光稳定，常常是黑的（2026-09-25 实测
# 用户拿到一张几乎全黑的图，而 PNG 有 807 KB——说明是"很暗 + 噪点"，不是纯色黑）。
# 但**不能死等固定时长**：那是白等。改成"够亮就走，最晚等到 WARMUP_MAX_MS"。
WARMUP_MIN_MS = 600
WARMUP_MAX_MS = 2500
BRIGHT_ENOUGH = 40
# 图片规格：摄像头拍的是人/房间，没有小字要看，所以缩小到 1280 长边、存 JPEG。
# 之前是 1600x900 PNG ≈ 807 KB，base64 后约 1.1 MB 要**上传到模型服务商**——
# 上行带宽才是"读图慢"的大头（本机传输几乎不花时间）。
MAX_LONG_EDGE = 1280
JPEG_QUALITY = 85


@dataclass
class Frame:
    """One camera frame."""

    path: str = ""
    width: int = 0
    height: int = 0
    label: str = ""
    bytes: int = 0
    brightness: int = -1
    warmup_ms: int = 0
    error: str = ""

    def describe(self) -> str:
        """One-line summary for the bubble/log.

        Returns:
            Chinese summary.
        """
        if self.error:
            return f"摄像头没拍成：{self.error}"
        return (
            f"{self.label}　{self.width}x{self.height}　{self.bytes / 1024:.0f} KB"
            f"　亮度 {self.brightness}/255　抓图耗时 {self.warmup_ms} ms"
        )


def brightness_of(image) -> int:  # noqa: ANN001 - QImage
    """Average brightness 0-255, sampled on a grid (no numpy).

    Args:
        image: QImage.

    Returns:
        Mean of the sampled RGB values; 0 for a null image.
    """
    if image is None or image.isNull():
        return 0
    step_x = max(1, image.width() // 48)
    step_y = max(1, image.height() // 27)
    total = 0
    count = 0
    for y in range(0, image.height(), step_y):
        for x in range(0, image.width(), step_x):
            color = image.pixelColor(x, y)
            total += color.red() + color.green() + color.blue()
            count += 3
    return int(total / count) if count else 0


def devices() -> list[str]:
    """Camera names Qt can see.

    Returns:
        Device descriptions; empty when there is none or QtMultimedia is missing.
    """
    try:
        from PySide6.QtMultimedia import QMediaDevices  # noqa: PLC0415 - 可选依赖
    except Exception:  # noqa: BLE001 - 打包版可能没带 QtMultimedia
        return []
    try:
        return [
            str(device.description() or "") for device in QMediaDevices.videoInputs()
        ]
    except Exception:  # noqa: BLE001 - 枚举失败就当没有
        return []


def should_propose(
    *,
    now: float,
    enabled: bool,
    has_camera: bool,
    idle_seconds: float | None,
    locked: bool | None,
    last_offer_at: float,
    offers_today: int,
    last_interaction_at: float,
) -> bool:
    """Whether she may offer to take a look right now.

    Args:
        now: Wall-clock seconds.
        enabled: User has not switched the feature off.
        has_camera: A camera is present.
        idle_seconds: How long the user has been idle (None = unknown).
        locked: Whether the session is locked (None = unknown).
        last_offer_at: When she last offered.
        offers_today: How many times she offered today.
        last_interaction_at: Last time the user talked to her.

    Returns:
        True when an offer is allowed.
    """
    if not enabled or not has_camera:
        return False
    if locked is True:
        return False
    if idle_seconds is None or idle_seconds > PRESENT_IDLE_SECONDS:
        return False
    if offers_today >= PROPOSE_DAILY_LIMIT:
        return False
    if last_offer_at and now - last_offer_at < PROPOSE_COOLDOWN_SECONDS:
        return False
    # 刚聊过就别提议：她应该在跟你说话，而不是要求看摄像头
    if last_interaction_at and now - last_interaction_at < PROPOSE_COOLDOWN_SECONDS:
        return False
    return True


def can_capture(*, now: float, enabled: bool, has_camera: bool, last_capture_at: float) -> bool:
    """Whether a capture is allowed right now.

    Args:
        now: Wall-clock seconds.
        enabled: User has not switched the feature off.
        has_camera: A camera is present.
        last_capture_at: When the last capture happened.

    Returns:
        True when a capture is allowed.
    """
    if not enabled or not has_camera:
        return False
    return not (last_capture_at and now - last_capture_at < CAPTURE_COOLDOWN_SECONDS)


def capture(timeout_ms: int = 6000) -> Frame:
    """Grab exactly one frame from the default camera.

    Args:
        timeout_ms: How long to wait for the first frame.

    Returns:
        The frame (``error`` set when it failed).
    """
    try:
        from PySide6.QtGui import QImage  # noqa: PLC0415
        from PySide6.QtMultimedia import (  # noqa: PLC0415
            QCamera,
            QMediaCaptureSession,
            QMediaDevices,
            QVideoSink,
        )
    except Exception as exc:  # noqa: BLE001 - 打包版没带 QtMultimedia
        return Frame(error=f"没带 QtMultimedia（{type(exc).__name__}）")

    device = QMediaDevices.defaultVideoInput()
    if device is None or device.isNull():
        return Frame(error="没找到摄像头")

    frames: list[QImage] = []
    sink = QVideoSink()
    session = QMediaCaptureSession()
    camera = QCamera(device)
    session.setCamera(camera)
    session.setVideoSink(sink)

    loop = QEventLoop()
    guard = QTimer()
    guard.setSingleShot(True)
    guard.timeout.connect(loop.quit)

    def _on_frame(frame) -> None:  # noqa: ANN001 - QVideoFrame
        image = frame.toImage()
        if not image.isNull():
            frames.append(image)
            guard.stop()
            loop.quit()

    sink.videoFrameChanged.connect(_on_frame)
    best = None
    best_score = -1
    seen = 0
    try:
        camera.start()
        started = time.monotonic()
        while True:
            elapsed_ms = (time.monotonic() - started) * 1000
            if elapsed_ms >= timeout_ms:
                break
            # 够亮就走人（不等满）：只有在画面还偏暗时才继续等
            if best is not None and elapsed_ms >= WARMUP_MIN_MS:
                if best_score >= BRIGHT_ENOUGH or elapsed_ms >= WARMUP_MAX_MS:
                    break
            guard.start(int(min(500, timeout_ms - elapsed_ms)))
            loop.exec()
            while frames:
                image = frames.pop(0)
                seen += 1
                score = brightness_of(image)
                if score > best_score:
                    best_score, best = score, image
        warmup_ms = int((time.monotonic() - started) * 1000)
    except Exception as exc:  # noqa: BLE001 - 摄像头被占用等
        return Frame(error=f"{type(exc).__name__}: {exc}")
    finally:
        try:
            camera.stop()
        except Exception:  # noqa: BLE001 - 停不下来也不能影响返回
            pass

    if best is None:
        return Frame(error=f"{timeout_ms // 1000} 秒内没拿到画面（被别的程序占着？）")
    image = best
    if max(image.width(), image.height()) > MAX_LONG_EDGE:
        image = (
            image.scaledToWidth(MAX_LONG_EDGE)
            if image.width() >= image.height()
            else image.scaledToHeight(MAX_LONG_EDGE)
        )
    SHOTS.mkdir(parents=True, exist_ok=True)
    target = SHOTS / f"cam_{int(time.time())}.jpg"
    if not image.save(str(target), "JPEG", JPEG_QUALITY):
        return Frame(error="画面存不下来")
    return Frame(
        path=str(target),
        width=image.width(),
        height=image.height(),
        label=f"摄像头 {device.description()}（{seen} 帧里最亮的一帧）",
        bytes=target.stat().st_size,
        brightness=best_score,
        warmup_ms=warmup_ms,
    )


def drop(path: str) -> bool:
    """Delete a frame file (用完即弃).

    Args:
        path: File to remove.

    Returns:
        True when it is gone.
    """
    if not path:
        return True
    try:
        Path(path).unlink(missing_ok=True)
        return True
    except OSError:
        return False


def text_hint() -> str:
    """What to tell her when a frame is attached.

    Returns:
        Chinese prompt.
    """
    return "看一眼我现在什么样，随口说一句就好"
