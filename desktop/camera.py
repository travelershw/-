"""用摄像头看她/看你：单帧点击抓拍，以及"视频通话"式的常开摄像头。

摄像头比截图敏感得多，所以约定比 :mod:`screen` 更严：

1. **只在明确的点击后抓**（右键菜单）。没有任何定时抓拍；
2. 一次只抓**一帧**，发出去之后就把文件删掉，不留在 `shots/` 里；
3. 抓之前先把桌宠和气泡**藏起来**（免得拍到自己）；
4. 拿不到就报错——**不猜、不拿旧帧顶替**；
5. 她可以**提议**看一眼，但提议只是气泡上的一句话，**必须由你点菜单**才算同意。

:class:`Session` 是 P5 的"视频通话"：摄像头**常开**、随时取最新一帧（零等待），
但**只在你说完一句话时才把那一帧交出去**——不是每一帧都上传，也不落盘。
关掉菜单立刻 `stop()`；预览窗口给你看的是"她此刻能看到的画面"。

提议与抓拍的频率判断都写成纯函数（:func:`should_propose` / :func:`can_capture`），
时间由调用方注入，所以能离线测试、不依赖真实摄像头。
"""

import time
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QEventLoop, QObject, QTimer, Signal

import paths

ROOT = paths.BASE
SHOTS = paths.SHOTS
# 传给模型前缩到这个宽度（同 screen.py 的口径）
MAX_WIDTH = 1600
# "视频通话"常开会话里，一帧最长边长（也是发给模型前的大小）
SESSION_MAX_EDGE = 1280
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


PREVIEW_MAX_EDGE = 240  # 预览小窗用的边长（只看清脸部轮廓就够）


def preview_size(width: int, height: int, max_edge: int = PREVIEW_MAX_EDGE) -> tuple[int, int]:
    """Shrink a frame to preview size, keeping the aspect ratio.

    Args:
        width: Source width.
        height: Source height.
        max_edge: Longest edge of the preview.

    Returns:
        ``(width, height)`` for the preview, at least 1x1.
    """
    if width <= 0 or height <= 0:
        return (1, 1)
    longest = max(width, height)
    if longest <= max_edge:
        return (width, height)
    scale = max_edge / longest
    return (max(1, int(width * scale)), max(1, int(height * scale)))


def motion_of(previous, current, *, samples: int = 48) -> float:  # noqa: ANN001 - QImage
    """How different two frames are, 0.0 (identical) … 1.0 (完全变了).

    缩到 ``samples x samples`` 再逐点比：1280x720 逐像素在 Python 里太慢（92 万次），
    而"有没有人/在不在动"根本不需要那个精度。**只在本机算，不落盘、不上传。**

    Args:
        previous: Earlier frame (None means "no previous frame").
        current: Current frame.
        samples: Grid edge used for the comparison.

    Returns:
        Mean absolute difference of grayscale, scaled to 0 … 1.
    """
    if previous is None or current is None:
        return 0.0
    try:
        left = previous.scaled(samples, samples)
        right = current.scaled(samples, samples)
    except Exception:  # noqa: BLE001 - 空图等
        return 0.0
    if left.isNull() or right.isNull() or left.size() != right.size():
        return 0.0
    total = 0
    for y in range(left.height()):
        for x in range(left.width()):
            total += abs(left.pixelColor(x, y).value() - right.pixelColor(x, y).value())
    return total / (left.width() * left.height() * 255)


def save_frame(image, label: str, brightness: int = 0, warmup_ms: int = 0) -> Frame:  # noqa: ANN001
    """Write one captured image with the usual缩图/格式/命名 rules.

    Args:
        image: QImage to save.
        label: Human label for the bubble/log.
        brightness: Measured brightness (for the log).
        warmup_ms: How long the camera needed to settle (0 for an already-open camera).

    Returns:
        The frame (``error`` set when it could not be written).
    """
    if image is None or image.isNull():
        return Frame(error="没有画面")
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
        label=label,
        bytes=target.stat().st_size,
        brightness=brightness,
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


class Session(QObject):
    """常开摄像头：一直收帧，随时取"最新一帧"（视频通话用）。

    为什么需要它：`capture()` 每次都新建 QCamera 并等曝光稳定（实测 0.7~1.8 秒），
    而视频通话要的是"你说完话我立刻看到你"——摄像头一直开着，取帧就是**零等待**。

    规矩照旧（甚至更严，因为这是常开摄像头）：

    - 只在你显式打开「视频通话」时才开；关掉菜单就立刻 `stop()`；
    - 收帧回调里**只保留最新一帧**（且是缩过的），**不落盘**；
    真正要发给她的那一帧才写文件，发完按既有规矩删除；
    - 锁屏/她说话期间由调用方决定暂停（这里只提供开关）。
    """

    updated = Signal()  # 有新的一帧（预览窗口用）
    failed = Signal(str)

    def __init__(self, parent=None) -> None:  # noqa: ANN001 - QObject
        super().__init__(parent)
        self._camera = None
        self._session = None
        self._sink = None
        self._latest = None
        self._previous = None
        self._motion = 0.0
        self._frames = 0
        self._device_name = ""

    def is_open(self) -> bool:
        """Is the camera currently running?

        Returns:
            True while frames are arriving.
        """
        return self._camera is not None

    def device_name(self) -> str:
        """Description of the camera in use.

        Returns:
            Device description (empty when closed).
        """
        return self._device_name

    def frames_seen(self) -> int:
        """How many frames arrived since ``start()``.

        Returns:
            Frame count.
        """
        return self._frames

    def motion(self) -> float:
        """Difference between the last two frames, 0 … 1.

        Returns:
            The latest motion reading (0.0 when unknown).
        """
        return self._motion

    def latest(self):  # noqa: ANN201 - QImage
        """The most recent frame.

        Returns:
            The latest QImage, or None before the first frame arrives.
        """
        return self._latest

    def start(self) -> bool:
        """Open the camera and keep it running.

        Returns:
            True when the camera started.
        """
        if self._camera is not None:
            return True
        try:
            from PySide6.QtMultimedia import (  # noqa: PLC0415
                QCamera,
                QMediaCaptureSession,
                QMediaDevices,
                QVideoSink,
            )
        except Exception as exc:  # noqa: BLE001 - 打包版没带 QtMultimedia
            self.failed.emit(f"没带 QtMultimedia（{type(exc).__name__}）")
            return False
        try:
            device = QMediaDevices.defaultVideoInput()
        except Exception as exc:  # noqa: BLE001 - 枚举失败
            self.failed.emit(f"枚举摄像头失败：{type(exc).__name__}: {exc}")
            return False
        if device is None or device.isNull():
            self.failed.emit("没找到摄像头")
            return False
        try:
            self._device_name = str(device.description() or "")
            self._sink = QVideoSink()
            self._session = QMediaCaptureSession()
            self._camera = QCamera(device)
            self._session.setCamera(self._camera)
            self._session.setVideoSink(self._sink)
            self._sink.videoFrameChanged.connect(self._on_frame)
            self._camera.start()
        except Exception as exc:  # noqa: BLE001 - 摄像头被别的程序占着
            self._camera = None
            self.failed.emit(f"打不开摄像头：{type(exc).__name__}: {exc}")
            return False
        return True

    def stop(self) -> None:
        """Close the camera and forget the last frame."""
        camera, self._camera = self._camera, None
        if camera is not None:
            try:
                camera.stop()
            except Exception:  # noqa: BLE001 - 停不下来也不能让退出流程卡住
                pass
        self._session = None
        self._sink = None
        self._latest = None
        self._previous = None

    def _on_frame(self, frame) -> None:  # noqa: ANN001 - QVideoFrame
        """Keep only the newest frame (never written to disk here)."""
        try:
            image = frame.toImage()
        except Exception:  # noqa: BLE001 - 坏帧就丢
            return
        if image.isNull():
            return
        width, height = preview_size(image.width(), image.height(), MAX_LONG_EDGE)
        if (width, height) != (image.width(), image.height()):
            image = image.scaled(width, height)
        self._motion = motion_of(self._previous, image)
        self._previous, self._latest = image, image
        self._frames += 1
        self.updated.emit()

    def snapshot(self, label: str | None = None) -> Frame:
        """Write the current frame to a file, ready to send.

        Args:
            label: Override for the frame label.

        Returns:
            The frame (``error`` set when there is no picture yet).
        """
        image = self._latest
        if image is None:
            return Frame(error="摄像头刚开，还没有画面")
        name = label or f"摄像头 {self._device_name}（视频通话实时帧，第 {self._frames} 帧）"
        return save_frame(image, name, brightness=brightness_of(image), warmup_ms=0)


def text_hint() -> str:
    """What to tell her when a frame is attached.

    Returns:
        Chinese prompt.
    """
    return "看一眼我现在什么样，随口说一句就好"
