"""轻语桌宠（阶段 A）：一个总在最前、能拖能点的透明小窗，显示她此刻的状态。

用法：
    python pet.py                 正常启动
    python pet.py --selftest      打印状态→表情映射与当前快照，不弹窗
    python pet.py --shot x.png    把当前表情渲染成图片（用来检查画得对不对）
"""

import json
import os
import random
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, QPoint, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QBrush,
    QColor,
    QIcon,
    QPainter,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QInputDialog,
    QLineEdit,
    QMenu,
    QSystemTrayIcon,
    QWidget,
)

import asr
import camera
import faces
import listening
import microphone
import paths
import pc_state
import screen
import sprite
import state as state_mod
import weather
from asr import ASRError
from bubble import Bubble
from chat import ChatClient, ChatWindow
from petlink import PetLinkClient

ROOT = paths.BASE
CONFIG = paths.CONFIG
LOG = paths.LOG
CHAR_WIDTH = 200
CHAR_HEIGHT = 250
# 她"刚说过话"之后，气泡里那句话保留多久
SPEAK_BUBBLE_MS = 9000
MAX_LOG_LINES = 300


def note(message: str) -> None:
    """Append one line to ``pet_log.txt`` (keeps the file short).

    桌宠是被双击拉起来的、没有控制台，崩了会静默消失；这份日志用来事后查
    "到底起没起、什么时候没的、报了什么错"。

    Args:
        message: Text to log.
    """
    try:
        stamp = time.strftime("%m-%d %H:%M:%S")
        lines = []
        if LOG.exists():
            lines = LOG.read_text(encoding="utf-8", errors="replace").splitlines()
        lines.append(f"[{stamp}] {message}")
        LOG.write_text("\n".join(lines[-MAX_LOG_LINES:]) + "\n", encoding="utf-8")
    except OSError:
        pass


def install_crash_log() -> None:
    """Make unhandled exceptions land in the log file instead of vanishing."""

    def hook(exc_type, exc_value, exc_tb) -> None:  # noqa: ANN001 - sys hook
        import traceback

        note(
            "崩溃：" + "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        )

    sys.excepthook = hook


class PreviewWindow(QWidget):
    """小预览窗：显示"她此刻能看到的画面"（就是你自己的摄像头画面）。

    类似视频通话里那个自画面：不是必需，但没有它你不知道她到底看到了什么。
    不抢焦点、不吃点击（点它也不会打断你在别的程序里的输入）。
    """

    def __init__(self) -> None:
        super().__init__(None)
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.WindowTransparentForInput
            | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self._image = None
        self._title = ""
        self._last_paint = 0.0

    def set_image(self, image, title: str = "") -> None:  # noqa: ANN001 - QImage
        """Show one frame (throttled: repainting faster than ~10 fps is wasted work).

        Args:
            image: Frame to show.
            title: Small caption under the picture.
        """
        self._image = image
        self._title = title
        now = time.monotonic()
        if now - self._last_paint < 0.1:
            return
        self._last_paint = now
        self._resize_to_image()
        self.show()
        self.raise_()
        self.update()

    def _resize_to_image(self) -> None:
        """Fit the window to the frame plus room for the caption."""
        if self._image is None:
            return
        width, height = self._image.width(), self._image.height()
        self.resize(max(120, width), max(60, height + 18))

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """Draw the frame with a thin border and a caption.

        Args:
            event: Paint event (unused).
        """
        painter = QPainter(self)
        painter.fillRect(self.rect(), QBrush(QColor(24, 22, 28, 235)))
        if self._image is not None:
            painter.drawImage(0, 0, self._image)
        if self._title:
            painter.setPen(QColor(228, 224, 232))
            painter.drawText(4, self.height() - 5, self._title)
        painter.setPen(QColor(120, 112, 128))
        painter.drawRect(0, 0, self.width() - 1, self.height() - 1)


class NetSignals(QObject):
    """把后台线程的网络结果送回 UI 线程。

    天气/地名查询都要走网络（最坏 8 秒超时），**绝不能在 Qt 主线程里等**——
    主线程卡住会让桌宠整个僵住（这个项目已经因为事件循环冻结吃过一次亏）。
    用 Qt 信号从工作线程发出去，Qt 会自动排队到主线程执行。
    """

    weather = Signal(object)  # weather.Weather
    place = Signal(object, str)  # (list[weather.Place], 错误文本), 查询用的原文
    heard = Signal(object, str, str)  # (microphone.Clip, 转写文字, 错误文本)
    model = Signal(str, str)  # (下载结果文本, 错误文本)
    model_progress = Signal(int, int)  # 已下字节, 总字节
    transcribed = Signal(str, str, str)  # (wav 路径, 文字, 错误文本) —— 对话模式专用


def fetch_weather(lat: float, lon: float, place: str):  # noqa: ANN202 - weather.Weather
    """Fetch one reading on a worker thread; no exception may escape it.

    工作线程里抛异常不会有人接，线程会**静默死掉**、``_weather_busy`` 永远卡在 True
    （以后再也点不出天气）。所以这里把任何异常都变成带 ``error`` 的结果交回主线程。

    Args:
        lat: Latitude.
        lon: Longitude.
        place: Display name.

    Returns:
        A ``weather.Weather`` (``error`` set when the fetch blew up).
    """
    try:
        return weather.read(lat, lon, place)
    except Exception as exc:  # noqa: BLE001 - 后台线程必须自己兜住一切
        return weather.Weather(
            place=place,
            latitude=lat,
            longitude=lon,
            fetched_at=time.time(),
            error=f"{type(exc).__name__}: {exc}",
        )


def fetch_places(query: str) -> tuple[list, str]:
    """Look a place name up on a worker thread, same no-escape rule as above.

    Args:
        query: What the user typed.

    Returns:
        ``(candidates, error)`` — the error is empty on success.
    """
    try:
        return weather.search_place(query), ""
    except Exception as exc:  # noqa: BLE001 - 后台线程必须自己兜住一切
        return [], f"{type(exc).__name__}: {exc}"


def record_and_transcribe(seconds, device: str, options: dict) -> tuple[object, str, str]:
    """Record one clip and transcribe it — on a worker thread, never raising.

    录音 + 上传识别都要花时间（录 5 秒就是 5 秒），**绝不能放在 UI 线程**；
    而工作线程里抛异常只会静默杀死线程、让 `_mic_busy` 永远卡住，所以这里把一切
    都变成 ``(clip, text, error)`` 三段交回主线程。

    Args:
        seconds: Recorded duration.
        device: Device name substring.
        options: ``asr`` config block (``base_url`` / ``model`` / ``api_key``).

    Returns:
        ``(clip, text, error)`` — exactly one of text/error carries a value.
    """
    try:
        clip = microphone.record(seconds, device)
    except Exception as exc:  # noqa: BLE001 - 后台线程必须自己兜住一切
        return microphone.Clip(error=f"{type(exc).__name__}: {exc}"), "", ""
    if clip.error:
        return clip, "", clip.error
    if not microphone.is_audible(clip.rms):
        return clip, "", f"这句太轻了（音量 {clip.rms:.0f}），没听到你说什么"
    try:
        text, error = transcribe_clip(clip.path, options)
    except Exception as exc:  # noqa: BLE001 - 兜底，理论上 transcribe_clip 不抛
        return clip, "", f"{type(exc).__name__}: {exc}"
    return clip, text, error


def local_models_ready() -> bool:
    """Are both local models (recognizer + silence detector) on disk?

    Returns:
        True when neither download is needed.
    """
    try:
        import local_asr  # noqa: PLC0415
    except Exception:  # noqa: BLE001 - 模块缺失即视为没装好
        return False
    return local_asr.is_ready() and listening.is_ready()


def local_model_summary() -> str:
    """One line describing what local models are installed.

    Returns:
        Chinese summary.
    """
    parts = []
    try:
        import local_asr  # noqa: PLC0415

        parts.append(local_asr.describe())
    except Exception:  # noqa: BLE001
        parts.append("本机识别：模块不可用")
    parts.append(listening.describe())
    return "；".join(parts)


def listening_drop(path: str) -> None:
    """Delete one segment wav (kept tiny so callers read clearly).

    Args:
        path: File to remove.
    """
    if not path:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


def transcribe_clip(path: str, options: dict) -> tuple[str, str]:
    """Transcribe an existing wav on a worker thread, never raising.

    Args:
        path: Wav file to recognize.
        options: The ``asr`` config block.

    Returns:
        ``(text, error)`` — one of them is always empty.
    """
    try:
        return asr.transcribe_with(path, options), ""
    except ASRError as exc:
        return "", str(exc)
    except Exception as exc:  # noqa: BLE001 - 后台线程必须自己兜住一切
        return "", f"{type(exc).__name__}: {exc}"


def fetch_model(progress=None) -> tuple[str, str]:  # noqa: ANN001 - 可选回调
    """Download the local models (recognizer + silence detector) on a worker thread.

    Args:
        progress: Optional ``callable(done, total)`` forwarded to the downloader.

    Returns:
        ``(message, error)`` — one of them is always empty.
    """
    messages: list[str] = []
    try:
        import local_asr  # noqa: PLC0415 - 只在真的要下模型时才需要它

        messages.append(local_asr.fetch(progress=progress))
    except Exception as exc:  # noqa: BLE001 - 后台线程必须自己兜住
        return "", f"识别模型没下成：{exc}"
    try:
        messages.append(listening.fetch_vad(progress=progress))
    except Exception as exc:  # noqa: BLE001 - 同上
        return "", f"识别模型好了，但静音检测模型没下成：{exc}"
    return "；".join(messages), ""


DEFAULT_CONFIG = {
    # -1 表示"第一次运行时放到右下角"
    "x": -1,
    "y": -1,
    "scale": 1.0,
    "poll_ms": 2000,
    "pinned_group": "",
    "click_through": False,
    "always_on_top": True,
    "chat_session_id": "",
    "muted_events": False,
    "carry_over": True,
    "chat_transport": "desktop",
    "desktop_url": "ws://127.0.0.1:6198",
    "desktop_secret": "",
    "screen_capture": True,
    # 截图格式：png（默认，无损，截图里的小字更清楚）或 jpeg（体积小得多，上传快）。
    # 这张图要上传到模型服务商，所以在网速慢的时候切 jpeg 能明显缩短"看看屏幕"的等待。
    "shot_format": "png",
    # 本机使用状态采集（闲置/锁屏/前台程序/摄像头），只写本地 pc_state.json，不上传。
    "pc_state": True,
    # 摄像头：capture 是总开关（关掉后连菜单都不抓）；propose 只是"她可以提议看一眼"，
    # 提议本身不抓拍——真正的抓拍永远要你点菜单。
    "camera_capture": True,
    "camera_propose": True,
    # 想自己看抓到的画面时打开它：那一帧不删，留在 shots/ 里（默认用完即弃）。
    "camera_keep_frame": False,
    # 环境传感器（P2）：天气/空气质量/日出日落，只读网络、不用硬件。
    # 位置必须由你给定——**不拿 IP 去猜**；没填经纬度就只提示"还没设位置"。
    "weather": True,
    "weather_place": "",
    "weather_lat": 0.0,
    "weather_lon": 0.0,
    # 麦克风（P3-b「听懂」）：默认**关**——麦克风比摄像头更敏感，得你显式打开。
    # 打开后也只在**你点菜单**时录一句（没有常驻监听），锁屏时不录，录完的 wav 交出去就删。
    "mic_listen": False,
    "mic_seconds": 5.0,
    "mic_device": "",
    "mic_keep_clip": False,
    # 对话模式（P4-b）：麦克风常开、由静音检测自动断句，说完就接话。
    # 默认关；只在"对话模式"里工作；锁屏不听；她说话/思考时不听（防自激）；
    # 一段时间没交互自动退出。`voice` 是"她出声"的开关，**本轮先不做**，只留位。
    "conversation": False,
    "vad_silence_ms": 700,
    "vad_min_speech_ms": 250,
    "conversation_idle_seconds": 180,
    "voice": False,
    # 视频通话（P5）：摄像头常开、随时能"看到你"，但**只有说话时才把那一帧交出去**。
    # 默认关；关掉立刻释放摄像头；预览小窗显示"她此刻能看到的画面"。
    "video_call": False,
    "video_preview": True,
    # 语音转文字：默认走**本机**引擎（sherpa-onnx + 本地模型），声音不出这台机器。
    # engine 改成 "openai" 就切回云端（OpenAI 兼容接口，音频会上传）；
    # base_url/model/api_key 只在云端路线用；api_key 留空时会去读 AstrBot 那份 key。
    "asr": {
        "engine": "local",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-asr",
        "api_key": "",
    },
    # 独立版（分发给别人用）：local 自带大脑、填自己的 API Key；astrbot 连本机的 AstrBot
    "mode": "astrbot",
    "llm": {},
    "vision": {},
}


def load_config() -> dict:
    """Read ``config.json``, filling in anything missing.

    Returns:
        The merged configuration.
    """
    data = dict(DEFAULT_CONFIG)
    try:
        # utf-8-sig：用户用记事本编辑过 config.json 会带上 BOM，普通 utf-8 读会直接抛错
        stored = json.loads(CONFIG.read_text(encoding="utf-8-sig"))
        if isinstance(stored, dict):
            data.update({key: stored[key] for key in stored if key in DEFAULT_CONFIG})
    except (OSError, ValueError):
        pass
    return data


def save_config(data: dict) -> None:
    """Persist the configuration.

    Args:
        data: Configuration to write.
    """
    try:
        CONFIG.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


class PetWindow(QWidget):
    """The always-on-top pet window itself."""

    def __init__(self) -> None:
        super().__init__(None)
        self.config = load_config()
        self.state = self._read_state()
        self.look = faces.pick(self.state)
        self.started = time.time()
        self._dragging = False
        self._drag_offset = QPoint()
        self._press_pos = QPoint()
        self._last_reason = self.state.mood_reason
        self._speaking_until = 0.0
        # 事件从"现在"开始数，免得一打开桌宠就把历史事件全冒一遍
        self._event_cursor = state_mod.last_event_id()
        # 通道是否连着（断线不自动重连，所以状态要能看出来）
        self._link_ok = True
        # 已经冒过泡的看门狗提醒时间戳（同一条只提醒一次）
        self._alert_seen = int(state_mod.read_alert().get("ts") or 0)
        # 有立绘就按立绘的长宽比定窗口（不然会留一圈空白）
        self._sprite_loaded = sprite.load().width > 0

        self.setWindowTitle("轻语")
        self.setAttribute(Qt.WA_TranslucentBackground)
        size = self._window_size()
        self.setFixedSize(*size)
        self._apply_flags()
        if self.config["x"] < 0 or self.config["y"] < 0:
            self.reset_position()
        else:
            self.move(self.config["x"], self.config["y"])
        self.setWindowIcon(self._make_icon())
        self._apply_mask()

        self.bubble = Bubble()
        self._poll = QTimer(self)
        self._poll.timeout.connect(self.refresh)
        self._poll.start(int(self.config["poll_ms"]))
        self._anim = QTimer(self)
        self._anim.timeout.connect(self.update)
        self._anim.start(80)
        # 本机状态每 10 秒采一次：量小、纯本机，拿不到就记 None（见 pc_state）
        self._pc = QTimer(self)
        self._pc.timeout.connect(self.collect_pc_state)
        self._pc.start(10_000)
        self.collect_pc_state()
        # 摄像头：只记账（时间戳/当天计数），提议由 _camera_timer 走
        self._camera_last_capture = 0.0
        self._camera_last_offer = 0.0
        self._camera_offers: list[float] = []
        self._last_talk_at = 0.0
        self._camera_timer = QTimer(self)
        self._camera_timer.timeout.connect(self.maybe_propose_camera)
        self._camera_timer.start(60_000)
        # 环境传感器（P2）：网络在后院线程跑，结果用信号回主线程
        self._net = NetSignals()
        self._net.weather.connect(self.on_weather)
        self._net.place.connect(self.on_place)
        self._net.heard.connect(self.on_heard)
        self._net.model.connect(self.on_model)
        self._net.model_progress.connect(self.on_model_progress)
        self._net.transcribed.connect(self.on_transcribed)
        self._weather_busy = False
        self._geo_busy = False
        self._mic_busy = False
        self._mic_last = 0.0
        self._model_busy = False
        self._model_announced = 0.0
        # 对话模式（P4）：常开麦克风 + 四态（待机/在听/在想/在说）
        self._listener = None
        self._thinking = False
        self._conversation_deadline = 0.0
        self._stream_text = ""
        # 视频通话（P5）：常开摄像头 + 预览小窗
        self._video = None
        self._preview = None
        self._state_timer = QTimer(self)
        self._state_timer.timeout.connect(self.check_conversation)
        self._state_timer.start(15_000)
        self._build_menu()
        self._build_tray()
        # 启动就连上桌面通道：她要能随时把消息弹到桌面上（不只是你打开聊天窗时）
        self._client = None
        if str(self.config.get("chat_transport") or "desktop") == "desktop":
            self._client = self._make_client()

    # ---------------------------------------------------------------- 窗口行为

    def _window_size(self) -> tuple[int, int]:
        """Window size: the design box times the configured scale.

        窗口**必须**保持设计框的比例（220:250），否则 ``paintEvent`` 里的缩放会把立绘
        横向或纵向拉变形（之前按图片比例改窗口就踩了这个坑：640x640 的图被拉宽 13.6%）。
        图片比例不匹配时，多余的是透明边距，由窗口遮罩处理，点不到。

        Returns:
            ``(width, height)`` in pixels.
        """
        scale = float(self.config["scale"])
        return int(faces.DESIGN_W * scale), int(faces.DESIGN_H * scale)

    def _view(self) -> tuple[float, float, float]:
        """设计坐标 → 窗口像素的等比缩放与居中偏移。

        ``paintEvent`` 和窗口遮罩都必须用同一套变换：遮罩以前是按设计坐标（220x250）算的，
        而窗口在放大后会变成 275x312 之类，遮罩就把立绘右下角切掉了——"改大小立绘崩坏"
        就是这么来的。

        Returns:
            ``(scale, dx, dy)``.
        """
        scale = min(self.width() / faces.DESIGN_W, self.height() / faces.DESIGN_H)
        dx = (self.width() - faces.DESIGN_W * scale) / 2
        dy = (self.height() - faces.DESIGN_H * scale) / 2
        return scale, dx, dy

    def _apply_mask(self) -> None:
        """Make the transparent parts of the window click-through."""
        pixmap = sprite.pixmap_for(self.look.key)
        if pixmap is None:
            self.clearMask()
            return
        scale, dx, dy = self._view()
        self.setMask(
            sprite.window_mask(
                pixmap,
                faces.DESIGN_W,
                faces.DESIGN_H,
                faces.FEET_Y,
                scale=scale,
                dx=dx,
                dy=dy,
            ),
        )

    def _apply_flags(self) -> None:
        """Apply the window flags from the configuration."""
        flags = Qt.FramelessWindowHint | Qt.Tool
        if self.config["always_on_top"]:
            flags |= Qt.WindowStaysOnTopHint
        if self.config["click_through"]:
            flags |= Qt.WindowTransparentForInput
        self.setWindowFlags(flags)
        self.show()

    def _make_icon(self) -> QIcon:
        """Render a small tray icon from the character.

        Returns:
            The icon.
        """
        pixmap = QPixmap(64, 64)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.translate(32, 62)
        painter.scale(0.29, 0.29)
        faces.paint_character(painter, self.look, t=0.0)
        painter.end()
        return QIcon(pixmap)

    def _build_menu(self) -> None:
        """Create the right-click / tray menu."""
        menu = QMenu(self)
        act_status = QAction("她现在的状态", self)
        act_status.triggered.connect(self.show_status)
        act_line = QAction("说句话", self)
        act_line.triggered.connect(self.say_line)
        act_chat = QAction("跟她聊天", self)
        act_chat.triggered.connect(self.open_chat)
        act_recent = QAction("她刚在群里说了什么", self)
        act_recent.triggered.connect(self.show_recent_events)
        act_screen = QAction("看看我的屏幕", self)
        act_screen.setToolTip("抓一张屏幕截图发给她（只在你点的时候抓）")
        act_screen.triggered.connect(lambda: self.look_at_screen())
        act_pc = QAction("电脑状态", self)
        act_pc.setToolTip("看本机状态采集读到了什么（闲置时间/锁屏/前台程序/摄像头）")
        act_pc.triggered.connect(self.show_pc_state)
        act_cam = QAction("用摄像头看一眼", self)
        act_cam.setToolTip("只抓一帧、发完就删；只有你点这里才会拍")
        act_cam.triggered.connect(self.look_at_camera)
        self.act_camera_on = QAction("允许摄像头", self, checkable=True)
        self.act_camera_on.setToolTip("关掉后她连提议都不会提，更不会拍")
        self.act_camera_on.setChecked(bool(self.config.get("camera_capture", True)))
        self.act_camera_on.triggered.connect(self.toggle_camera)
        self.act_jpeg = QAction("截图用 JPEG（上传快）", self, checkable=True)
        self.act_jpeg.setToolTip(
            "截图要上传给模型；JPEG 通常体积更小、读图更快，代价是字迹不如 PNG 清晰"
        )
        self.act_jpeg.setChecked(
            screen.normalize_format(self.config.get("shot_format")) == "jpeg"
        )
        self.act_jpeg.triggered.connect(self.toggle_jpeg)
        act_weather = QAction("今天天气", self)
        act_weather.setToolTip("查你设的那个地方的天气/空气质量/日出日落（只读网络，不用硬件）")
        act_weather.triggered.connect(self.show_weather)
        act_place = QAction("设置位置…", self)
        act_place.setToolTip("填城市名（例：北京市）；我用它查经纬度存到本机，不会拿 IP 猜")
        act_place.triggered.connect(self.set_location)
        act_hear = QAction("听一句（5 秒）", self)
        act_hear.setToolTip("录一句话转成文字发给她；只有你点这里才会开麦，录完就删")
        act_hear.triggered.connect(self.hear_once)
        self.act_mic_on = QAction("允许麦克风", self, checkable=True)
        self.act_mic_on.setToolTip("默认关。关着时连录都不会录；开着也只在你说「听一句」时录")
        self.act_mic_on.setChecked(bool(self.config.get("mic_listen", False)))
        self.act_mic_on.triggered.connect(self.toggle_mic)
        act_asr_key = QAction("语音识别 Key…", self)
        act_asr_key.setToolTip("只在 asr.engine=openai（云端）时用；本机引擎不需要 Key")
        act_asr_key.triggered.connect(self.set_asr_key)
        act_model = QAction("下载本机模型…", self)
        act_model.setToolTip("本机识别用的中文模型（78 MB）+ 静音检测模型（0.6 MB），只下一次")
        act_model.triggered.connect(self.download_asr_model)
        self.act_conversation = QAction("对话模式（不用点，直接说）", self, checkable=True)
        self.act_conversation.setToolTip(
            "开着时麦克风常开、由静音检测自动断句；锁屏不听、超时自动退出、随时可关"
        )
        self.act_conversation.setChecked(bool(self.config.get("conversation", False)))
        self.act_conversation.triggered.connect(self.toggle_conversation)
        self.act_video = QAction("视频通话（她能一直看到你）", self, checkable=True)
        self.act_video.setToolTip(
            "摄像头常开、你说完一句她就能看到当时的你；右上角小窗显示她看到什么。默认关。"
        )
        self.act_video.setChecked(bool(self.config.get("video_call", False)))
        self.act_video.triggered.connect(self.toggle_video_call)
        act_setup = QAction("设置 API Key…", self)
        act_setup.triggered.connect(self.open_setup)
        act_link = QAction("重连 AstrBot", self)
        act_link.setToolTip("断线不会自动重连，点这里手动再连一次")
        act_link.triggered.connect(self.reconnect_link)
        menu.addAction(act_status)
        menu.addAction(act_recent)
        menu.addAction(act_screen)
        menu.addAction(act_pc)
        menu.addAction(act_cam)
        menu.addAction(self.act_camera_on)
        menu.addAction(self.act_jpeg)
        menu.addAction(act_weather)
        menu.addAction(act_place)
        menu.addAction(act_hear)
        menu.addAction(self.act_mic_on)
        menu.addAction(self.act_conversation)
        menu.addAction(self.act_video)
        menu.addAction(act_asr_key)
        menu.addAction(act_model)
        menu.addAction(act_line)
        menu.addAction(act_chat)
        menu.addAction(act_link)
        menu.addAction(act_setup)
        menu.addSeparator()
        self.act_through = QAction("鼠标穿透", self, checkable=True)
        self.act_through.setChecked(self.config["click_through"])
        self.act_through.triggered.connect(self.toggle_click_through)
        self.act_top = QAction("总在最前", self, checkable=True)
        self.act_top.setChecked(self.config["always_on_top"])
        self.act_top.triggered.connect(self.toggle_on_top)
        menu.addAction(self.act_through)
        menu.addAction(self.act_top)
        menu.addSeparator()
        act_reset = QAction("回到右下角", self)
        act_reset.triggered.connect(self.reset_position)
        act_art = QAction("重新读取立绘", self)
        act_art.triggered.connect(self.reload_art)
        size_menu = menu.addMenu("大小")
        # QActionGroup：互斥勾选。之前没用 group，点完"大 125%"旧的那项还留着勾，
        # 菜单里能同时出现好几个 ✓。
        self.size_group = QActionGroup(self)
        self.size_group.setExclusive(True)
        self._size_actions: list[tuple[float, QAction]] = []
        for label, value in (
            ("小 60%", 0.6),
            ("中 80%", 0.8),
            ("标准 100%", 1.0),
            ("大 125%", 1.25),
            ("很大 150%", 1.5),
        ):
            action = QAction(label, self, checkable=True)
            action.setChecked(abs(float(self.config["scale"]) - value) < 0.01)
            action.triggered.connect(lambda _=False, v=value: self.set_scale(v))
            self.size_group.addAction(action)
            self._size_actions.append((value, action))
            size_menu.addAction(action)
        act_quit = QAction("退出桌宠", self)
        act_quit.triggered.connect(QApplication.quit)
        menu.addAction(act_reset)
        menu.addAction(act_art)
        menu.addAction(act_quit)
        self.menu = menu

    def _build_tray(self) -> None:
        """Create the tray icon with the same menu."""
        self.tray = QSystemTrayIcon(self._make_icon(), self)
        self.tray.setToolTip("轻语")
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._on_tray)
        self.tray.show()

    def _on_tray(self, reason) -> None:  # noqa: ANN001 - Qt enum
        """Left-clicking the tray icon shows the status.

        Args:
            reason: Activation reason from Qt.
        """
        if reason == QSystemTrayIcon.Trigger:
            self.show_status()

    # ---------------------------------------------------------------- 轮询与绘制

    def _read_state(self):
        """Read her state from whichever source this build uses.

        Returns:
            A :class:`state.PetState`.
        """
        if str(self.config.get("mode") or "astrbot") == "local":
            return state_mod.read_local_state()
        return state_mod.read_state(self.config["pinned_group"])

    def refresh(self) -> None:
        """Re-read her state, then show whatever she just did in the groups."""
        self.state = self._read_state()
        look = faces.pick(self.state)
        if look.key != self.look.key:
            self.look = look
            self.setWindowIcon(self._make_icon())
            self._apply_mask()
            self.update()
        reason = self.state.mood_reason
        if reason and reason != self._last_reason:
            self._last_reason = reason
            effect = f"（{self.state.mood_effect}）" if self.state.mood_effect else ""
            self.say(f"{reason}{effect}", 8000)
        self._check_alert()
        self._drain_events()

    def _check_alert(self) -> None:
        """Show the watchdog's QQ-offline alert once (QQ 断了只能靠桌面提醒）。"""
        alert = state_mod.read_alert()
        if not alert or alert["ts"] <= self._alert_seen:
            return
        self._alert_seen = alert["ts"]
        note(f"看门狗提醒：{alert['text'][:120]}")
        self.say(f"⚠️ {alert['text']}", 20000)

    def _drain_events(self) -> None:
        """Show the group events that happened since the last poll."""
        events = state_mod.read_events(after_id=self._event_cursor, limit=10)
        for item in events:
            self._event_cursor = max(self._event_cursor, int(item["id"]))
        if self.config.get("muted_events") or not events:
            return
        # 只冒"她说了话"这一类；心情事件已经体现在表情和气泡上了。
        # 再加一道 group_id 判断：没有群号的事件（私聊/桌面通道）不该出现在群里动静里，
        # 否则气泡会变成「【】回话：…」。
        spoken = [
            item
            for item in events
            if item["kind"] in {"speak", "chime"} and str(item.get("group_id") or "")
        ]
        if not spoken:
            return
        lines = []
        for item in spoken[-2:]:
            mark = "插嘴" if item["kind"] == "chime" else "回话"
            lines.append(f"【{item['group_name']}】{mark}：{item['text'][:110]}")
        self._speaking_until = time.time() + 2.5
        self.say("\n".join(lines), SPEAK_BUBBLE_MS)

    def show_recent_events(self) -> None:
        """Bubble up her latest group lines."""
        events = [
            item
            for item in state_mod.read_events(after_id=0, limit=60)
            if item["kind"] in {"speak", "chime"} and str(item.get("group_id") or "")
        ]
        if not events:
            self.say("她在群里还没说过话呢。", 5000)
            return
        lines = []
        for item in events[-3:]:
            when = time.strftime("%H:%M", time.localtime(int(item["ts"])))
            lines.append(f"{when}【{item['group_name']}】{item['text'][:90]}")
        self.say("\n".join(lines), 15000)

    def say(self, text: str, msec: int = 7000) -> None:
        """Show a bubble above her head.

        Args:
            text: Line to show.
            msec: How long to keep it.
        """
        anchor = QPoint(self.x() + self.width() // 2, self.y() + 8)
        self.bubble.show_text(text, anchor, msec)

    def say_line(self) -> None:
        """Say one of her idle lines for the current look."""
        options = faces.LINES.get(self.look.key) or faces.LINES["smile"]
        self._speaking_until = time.time() + 1.6
        self.say(random.choice(options), 5000)

    def show_status(self) -> None:
        """Bubble up the numeric status."""
        self.refresh()
        text = self.state.describe()
        if str(self.config.get("mode") or "astrbot") == "local":
            brain_client = getattr(self, "_brain", None)
            if brain_client is not None:
                text = f"{text}\n{brain_client.stats()}"
        elif not self._link_ok:
            text = f"{text}\n⚠ 与 AstrBot 的连接断了（右键→「重连 AstrBot」）"
        self.say(text, 12000)

    def toggle_click_through(self) -> None:
        """Flip mouse pass-through and remember it."""
        self.config["click_through"] = self.act_through.isChecked()
        save_config(self.config)
        self._apply_flags()

    def toggle_on_top(self) -> None:
        """Flip always-on-top and remember it."""
        self.config["always_on_top"] = self.act_top.isChecked()
        save_config(self.config)
        self._apply_flags()

    def reset_position(self) -> None:
        """Move her to the bottom-right corner of the primary screen."""
        screen = QApplication.primaryScreen().availableGeometry()
        self.move(
            screen.right() - self.width() - 40, screen.bottom() - self.height() - 20
        )
        self._remember_position()

    def set_scale(self, value: float) -> None:
        """Resize her, keeping the bottom-centre spot on screen.

        Args:
            value: New scale factor.
        """
        self.config["scale"] = float(value)
        save_config(self.config)
        before = self.geometry()
        anchor = before.center()
        anchor.setY(before.bottom())
        self.setFixedSize(*self._window_size())
        self.move(anchor.x() - self.width() // 2, anchor.y() - self.height())
        self._remember_position()
        self._apply_mask()
        self.setWindowIcon(self._make_icon())
        self._sync_size_menu()
        self.update()
        self.say(f"大小改成 {int(value * 100)}% 啦~", 3000)

    def _sync_size_menu(self) -> None:
        """Tick the menu entry that matches the current scale (and only that one)."""
        current = float(self.config.get("scale", 1.0))
        for value, action in getattr(self, "_size_actions", []):
            action.setChecked(abs(current - value) < 0.01)

    def reload_art(self) -> None:
        """Re-read ``assets/`` so new art shows up without restarting."""
        info = sprite.load(force=True)
        self._sprite_loaded = info.width > 0
        self.setFixedSize(*self._window_size())
        self._apply_mask()
        self.setWindowIcon(self._make_icon())
        self._remember_position()
        self.update()
        if info.width:
            extra = f"，另有 {len(info.looks)} 个分表情图" if info.looks else ""
            self.say(
                f"换上新立绘啦（{info.name or '未命名'} {info.width}x{info.height}{extra}）",
                6000,
            )
        else:
            self.say("assets 里没找到图片，先用我画的小人顶着~", 6000)

    def _remember_position(self) -> None:
        """Store the current position."""
        self.config["x"], self.config["y"] = self.x(), self.y()
        save_config(self.config)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """Draw her.

        Args:
            event: Paint event (unused).
        """
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        look = self.look
        if time.time() < self._speaking_until:
            look = faces.speak_look(look)
        if not self.state.online:
            painter.setOpacity(0.62)
        # 设计坐标 220x250 → 等比缩放并居中（和遮罩用同一套变换）
        scale, dx, dy = self._view()
        painter.translate(dx, dy)
        painter.scale(scale, scale)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(0, 0, 0, 26)))
        painter.drawEllipse(QRectF(66, 236, 88, 14))
        faces.paint_character(painter, look, t=time.time() - self.started)

    # ---------------------------------------------------------------- 鼠标交互

    def open_chat(self) -> None:
        """Open (or raise) the chat window, starting a transport lazily.

        默认走 ``desktop_pet`` 平台通道（阶段 D）：会话固定、身份是你的 QQ，她记得住、
        好感精力也算数；连不上时自动回落到面板通道（阶段 B）。
        """
        if getattr(self, "_chat_window", None) is None:
            self._chat_window = ChatWindow(
                self._client or self._make_client(),
                on_shot=self.look_at_screen,
            )
        self._chat_window.show()
        self._chat_window.raise_()
        self._chat_window.activateWindow()

    def _make_client(self):
        """Build and start the chat client for the configured mode.

        独立模式（``mode: local``）：她自己直连大模型，只要填了 API Key 就能用；
        AstrBot 模式（默认）：连本机的 desktop_pet 平台通道，记忆/好感与 QQ 打通。

        Returns:
            A started client emitting ``chunk`` / ``done`` / ``failed``.
        """
        if str(self.config.get("mode") or "astrbot") == "local":
            import brain

            client = brain.LocalBrainClient(self.config)
            client.ready.connect(lambda who: self.say(f"就绪：{who}", 3000))
            self._brain = client
            self._link_ok = True
            client.done.connect(self._on_chat_reply)
            client.chunk.connect(self.on_chat_chunk)
            client.failed.connect(self._on_link_problem)
            client.start()
            if not (self.config.get("llm") or {}).get("api_key"):
                QTimer.singleShot(1200, self.open_setup)
            return client
        transport = str(self.config.get("chat_transport") or "desktop")
        if transport == "desktop":
            client = PetLinkClient(
                str(self.config.get("desktop_url") or "ws://127.0.0.1:6198"),
                str(self.config.get("desktop_secret") or ""),
            )
            client.ready.connect(lambda who: self.say(f"桌面通道就绪（{who}）", 3000))
            # 断线**不自动重连**，只提示一次；要重试就右键→「重连 AstrBot」
            client.failed.connect(self._on_link_problem)
            client.ready.connect(lambda _who: setattr(self, "_link_ok", True))
        else:
            client = ChatClient(
                self.config.get("chat_session_id", ""),
                carry_over=bool(self.config.get("carry_over", True)),
            )
            client.ready.connect(self._remember_session)
        client.done.connect(self._on_chat_reply)
        client.chunk.connect(self.on_chat_chunk)
        client.start()
        return client

    def _on_link_problem(self, message: str) -> None:
        """只提示，不自动重连、也不自动换通道。

        Args:
            message: Failure text from the desktop client.
        """
        window = getattr(self, "_chat_window", None)
        if window is not None:
            window._append("系统", message)
        self._link_ok = False
        self.say(message, 15000)
        note(f"通道提示：{message}")

    def reconnect_link(self) -> None:
        """Tear the link down and try once more (user-triggered)."""
        if str(self.config.get("mode") or "astrbot") == "local":
            self.say("独立模式不用重连——直接说话就行~", 5000)
            return
        old = self._client
        if old is not None:
            try:
                old.stop()
            except Exception:  # noqa: BLE001 - 关不掉也无所谓
                pass
        self._client = self._make_client()
        window = getattr(self, "_chat_window", None)
        if window is not None:
            window.rebind(self._client)
        self.say("好，我再连一次试试~", 4000)

    def open_setup(self) -> None:
        """打开 API 设置窗口（首次运行或右键菜单都能进）。"""
        from setup_window import SetupDialog

        dialog = SetupDialog(self.config, self)
        if dialog.exec():
            save_config(self.config)
            note("API 设置已更新")
            client = getattr(self, "_brain", None)
            if client is not None:
                client.config = self.config
            self.say("设置好啦，可以跟我说话了~", 4000)

    def show_own_state(self) -> None:
        """独立模式下显示她自己的数值（不读 AstrBot 的库）。"""
        client = getattr(self, "_brain", None)
        if client is None:
            self.say("独立模式还没起来~", 4000)
            return
        self.say(client.stats(), 12000)

    def collect_pc_state(self) -> None:
        """Write one snapshot of this machine's usage state.

        纯本机行为：不联网、不弹窗、不写数据库；拿不到的读数留 None。
        采集频率由 ``self._pc`` 定时器控制（10 秒）。
        """
        if not self.config.get("pc_state", True):
            return
        pc_state.write_state()

    def show_pc_state(self) -> None:
        """Tell the user what the local state collection currently reads."""
        state = pc_state.snapshot()
        pc_state.write_state(state)
        self.say(pc_state.summary(state), 9000)

    def look_at_screen(self, text: str = "") -> None:
        """抓一张屏幕截图发给她（只在你点的时候抓）。

        抓之前先把桌宠/气泡/聊天窗藏起来，免得她"看到自己"；抓完立刻恢复。

        Args:
            text: 附带的问题；空着就用默认那句"看看我屏幕上有什么"。
        """
        if not self.config.get("screen_capture", True):
            self.say("截图功能关着呢（config.json 里的 screen_capture）~", 5000)
            return
        client = self._client or self._make_client()
        shot = self._take_shot()
        if shot.error:
            self.say(shot.describe(), 6000)
            note(f"截图失败：{shot.error}")
            return
        note(f"截图 {shot.describe()}")
        self._last_shot = shot
        self.say(f"我看看…（{shot.width}x{shot.height}）", 4000)
        client.send(text or screen.latest_text_hint(), shot.path)

    def _take_shot(self):  # noqa: ANN202 - screen.Shot
        """Hide the pet, grab the screen, then show it again.

        Returns:
            The captured shot (or one carrying ``error``).
        """
        windows = [self, self.bubble, getattr(self, "_chat_window", None)]
        hidden = [win for win in windows if win is not None and win.isVisible()]
        for win in hidden:
            win.hide()
        QApplication.processEvents()
        time.sleep(0.2)  # 给 DWM 一点时间真的把窗口撤下去
        try:
            return screen.capture(image_format=self.config.get("shot_format"))
        finally:
            for win in hidden:
                win.show()

    def toggle_jpeg(self, checked: bool) -> None:
        """Switch the screenshot format between PNG and JPEG.

        Args:
            checked: New state from the menu item.
        """
        self.config["shot_format"] = "jpeg" if checked else "png"
        save_config(self.config)
        self.say(
            "截图改成 JPEG 了，传得快一点，小字可能糊一点~"
            if checked
            else "截图换回 PNG 了，字会更清楚。",
            6000,
        )

    def show_weather(self) -> None:
        """Report the weather for the configured place (menu 「今天天气」）。

        缓存 15 分钟内就直接用，省一次请求；过期就后台刷新，并在等待期间先把旧读数
        摆出来**标明时间**——宁可说"这是 16:00 的读数"，也不假装它是最新的。
        """
        if not self.config.get("weather", True):
            self.say("天气功能关着呢（config.json 里的 weather）~", 5000)
            return
        lat = float(self.config.get("weather_lat") or 0.0)
        lon = float(self.config.get("weather_lon") or 0.0)
        place = str(self.config.get("weather_place") or "")
        if not lat and not lon:
            self.say("还没设位置呢——右键点「设置位置…」，说个城市名（比如 北京市）。", 8000)
            return
        cached = weather.read_cache()
        if cached is not None and weather.is_fresh(cached.fetched_at):
            cached.stale = False
            note(f"天气（15 分钟内的缓存）{cached.describe()}")
            self.say(cached.describe(), 12000)
            return
        if self._weather_busy:
            self.say("还在查呢，等我一下~", 4000)
            return
        self._weather_busy = True
        if cached is not None:
            cached.stale = True
            self.say(f"先看上次的：{cached.describe()}", 9000)
        else:
            self.say("我查查天气…", 4000)
        threading.Thread(
            target=lambda: self._net.weather.emit(fetch_weather(lat, lon, place)),
            name="pet-weather",
            daemon=True,
        ).start()

    def on_weather(self, result) -> None:  # noqa: ANN001 - weather.Weather
        """Show a finished reading and remember it (runs on the UI thread).

        Args:
            result: The reading delivered by the worker thread.
        """
        self._weather_busy = False
        # 只有真拿到温度才覆盖缓存，免得一次断网把上次的好读数冲掉
        if result.temperature is not None:
            weather.write_cache(result)
        note("天气 " + result.summary().replace("\n", "　"))
        self.say(result.describe(), 15000)

    def download_asr_model(self) -> None:
        """Fetch the local recognition model (menu 「下载识别模型…」）."""
        if self._model_busy:
            self.say("模型还在下呢，下完我会说一声~", 5000)
            return
        if local_models_ready():
            self.say(local_model_summary(), 12000)
            return
        self._model_busy = True
        self._model_announced = 0.0
        self.say("开始下本机模型（识别 78 MB + 静音检测 0.6 MB，只下一次）…", 6000)
        note("开始下载本机模型（识别 + 静音检测）")
        threading.Thread(
            target=lambda: self._net.model.emit(
                *fetch_model(progress=lambda done, total: self._net.model_progress.emit(done, total))
            ),
            name="pet-model",
            daemon=True,
        ).start()

    def on_model_progress(self, done: int, total: int) -> None:
        """Show download progress at most every 5% (runs on the UI thread).

        Args:
            done: Bytes downloaded.
            total: Expected bytes (0 when the server did not say).
        """
        if not total:
            return
        share = done / total
        if share - self._model_announced >= 0.05:
            self._model_announced = share
            self.say(f"模型下载 {share * 100:.0f}%（{done / 1024 / 1024:.0f} MB）", 4000)

    def on_model(self, text: str, error: str) -> None:
        """Report the finished model download (runs on the UI thread).

        Args:
            text: Success line.
            error: Failure line.
        """
        self._model_busy = False
        if error:
            note(f"下载识别模型失败：{error}")
            self.say(error, 15000)
            return
        note(f"识别模型就绪：{text}")
        self.say(text, 12000)

    # ------------------------------------------------------------ 对话模式（P4）

    def state_text(self) -> str:
        """Human-readable pet state, for the tray tooltip.

        Returns:
            One of 待机 / 在听 / 在看你 / 在想 / 在说.
        """
        if time.time() < getattr(self, "_speaking_until", 0.0):
            return "在说"
        if self._thinking:
            return "在想"
        if self._video_on() and self._video is not None and self._video.is_open():
            return "在看你"
        if self._listening_now():
            return "在听"
        return "待机"

    def _conversation_on(self) -> bool:
        """Is conversation mode switched on?

        Returns:
            The configuration flag.
        """
        return bool(self.config.get("conversation", False))

    def _listening_now(self) -> bool:
        """Is the microphone actually open for conversation mode?

        Returns:
            True while the VAD listener is capturing.
        """
        return bool(self._listener is not None and self._listener.is_running())

    def toggle_conversation(self, checked: bool) -> None:
        """Turn conversation mode on/off (menu 「对话模式」）.

        Args:
            checked: New state from the menu item.
        """
        self.config["conversation"] = bool(checked)
        save_config(self.config)
        if checked:
            self._conversation_touch()
            self._start_listening()
        else:
            self._stop_listening()
            self.say("好，对话模式关了，麦克风也关了。", 6000)
        note(f"对话模式 {'开' if checked else '关'}")

    def _start_listening(self) -> None:
        """Open the microphone if every gate allows it."""
        if self._listener is None:
            self._listener = listening.Listener(
                silence_ms=listening.normalize_ms(
                    self.config.get("vad_silence_ms"), listening.DEFAULT_SILENCE_MS
                ),
                min_speech_ms=listening.normalize_ms(
                    self.config.get("vad_min_speech_ms"), listening.DEFAULT_MIN_SPEECH_MS
                ),
            )
            self._listener.utterance.connect(self.on_utterance)
            self._listener.failed.connect(self.on_listen_failed)
        readings = pc_state.name_of(pc_state.read_state())
        allowed, reason = listening.should_listen(
            conversation=self._conversation_on(),
            locked=readings.get("locked"),
            thinking=self._thinking,
            speaking=time.time() < getattr(self, "_speaking_until", 0.0),
            has_device=bool(microphone.devices()),
            already_running=self._listening_now(),
            mic_enabled=bool(self.config.get("mic_listen", False)),
        )
        if not allowed:
            if reason and reason != "已经在听了":
                self.say(f"先不听：{reason}", 6000)
            return
        if self._listener.start():
            self._conversation_touch()
            minutes = max(1, int(self._idle_seconds() // 60))
            self.say(
                f"对话模式开着呢——直接说话就行，你停顿一下我就接。"
                f"{minutes} 分钟没动静我会自己退出。",
                9000,
            )
            note(f"开始连续听（静音 {self._listener.silence_ms:.0f}ms 判停）")

    def _stop_listening(self) -> None:
        """Close the microphone (used when pausing, exiting, or locking)."""
        if self._listener is None:
            return
        was = self._listener.is_running()
        self._listener.stop()
        if was:
            note("停止连续听")

    def _resume_after_reply(self) -> None:
        """Continue conversation mode if the user still wants it."""
        if self._conversation_on():
            self._conversation_touch()
            self._start_listening()

    def _idle_seconds(self) -> float:
        """Configured auto-exit window, clamped to something sane.

        Returns:
            Seconds between 30 and 3600.
        """
        try:
            value = float(self.config.get("conversation_idle_seconds") or 180)
        except (TypeError, ValueError):
            return 180.0
        return min(3600.0, max(30.0, value))

    def _conversation_touch(self) -> None:
        """Refresh the "still talking" deadline."""
        self._conversation_deadline = time.time() + self._idle_seconds()

    def check_conversation(self) -> None:
        """Auto-exit after a quiet spell, and pause while the screen is locked.

        这个定时器就是"你走了它还开着麦"的解药：超过设定时间没听到任何一句，
        自动退回点击式并告诉你一声；锁屏则立刻暂停（人不在就不该收音）。
        摄像头同理：锁屏就把「视频通话」也停掉。
        """
        readings = pc_state.name_of(pc_state.read_state())
        if readings.get("locked") is True:
            if self._video_on() and self._video is not None and self._video.is_open():
                self._stop_video()
                self.say("你锁屏了，视频通话先停。", 5000)
                note("锁屏 -> 暂停视频通话")
            if not self._conversation_on():
                return
            if self._listening_now():
                self._stop_listening()
                self.say("你锁屏了，我先不听。", 5000)
                note("锁屏 -> 暂停连续听")
            return
        if not self._conversation_on():
            return
        if time.time() >= getattr(self, "_conversation_deadline", 0.0):
            self.config["conversation"] = False
            save_config(self.config)
            if getattr(self, "act_conversation", None) is not None:
                self.act_conversation.setChecked(False)
            self._stop_listening()
            self.say("有一会儿没说话了，我先退出对话模式（想聊再点一下）。", 9000)
            note("对话模式超时自动退出")

    def on_utterance(self, utterance) -> None:  # noqa: ANN001 - listening.Utterance
        """Handle one finished sentence: recognize it, then hand it to her.

        Args:
            utterance: The segment the VAD just closed.
        """
        if self._thinking:
            # 已经在等她的回复了：这一句丢掉，免得排队堆成好几问
            listening_drop(utterance.path)
            return
        self._conversation_touch()
        self._stop_listening()  # 想/说期间不听：既防自激，也避免一句话被切成两半
        self._thinking = True
        self.say(f"听到了（{utterance.seconds:.1f} 秒），我听听是什么…", 3500)
        options = dict(self.config.get("asr") or {})
        threading.Thread(
            target=lambda: self._net.transcribed.emit(
                utterance.path, *transcribe_clip(utterance.path, options)
            ),
            name="pet-talk-asr",
            daemon=True,
        ).start()

    def on_transcribed(self, path: str, text: str, error: str) -> None:
        """Send a recognized sentence to her (runs on the UI thread).

        Args:
            path: The wav that was recognized.
            text: Transcript, empty on failure.
            error: Failure text, empty on success.
        """
        if path and not bool(self.config.get("mic_keep_clip", False)):
            listening_drop(path)
        if error or not text:
            self._thinking = False
            note(f"对话模式识别失败：{error or '（空）'}")
            self.say(error or "没听出文字，再说一次？", 8000)
            self._resume_after_reply()
            return
        note(f"对话模式听成：{text}")
        self.say(f"你：「{text}」", 2500)
        self._stream_text = ""
        client = self._client or self._make_client()
        # 视频通话开着：把**此刻**这一帧一起给她——这就是"她能看到你"的核心，
        # 而不是每帧都上传（那样既费 token 也没意义）。
        frame = self.live_frame()
        if frame is not None:
            note(f"视频通话随话附帧　{frame.describe()}")
            client.send(text, frame.path)
            if not bool(self.config.get("camera_keep_frame", False)):
                QTimer.singleShot(
                    camera.DELETE_AFTER_SECONDS * 1000,
                    lambda path=frame.path: camera.drop(path),
                )
            return
        client.send(text)

    def on_chat_chunk(self, text: str) -> None:
        """Show her answer while it is still being generated (streaming bubble).

        Args:
            text: One streamed fragment.
        """
        if not text:
            return
        self._stream_text = (getattr(self, "_stream_text", "") + text)[-400:]
        anchor = QPoint(self.x() + self.width() // 2, self.y() + 8)
        self.bubble.show_text(self._stream_text + "▌", anchor, 20000)

    def on_listen_failed(self, message: str) -> None:
        """Report a listener problem and stop pretending we are listening.

        Args:
            message: Failure text.
        """
        note(f"连续听失败：{message}")
        self.say(message, 12000)
        self._stop_listening()

    # ------------------------------------------------------------ 视频通话（P5）

    def _video_on(self) -> bool:
        """Is video call switched on?

        Returns:
            The configuration flag.
        """
        return bool(self.config.get("video_call", False))

    def toggle_video_call(self, checked: bool) -> None:
        """Open/close the always-on camera (menu 「视频通话」）.

        Args:
            checked: New state from the menu item.
        """
        self.config["video_call"] = bool(checked)
        save_config(self.config)
        if checked:
            self._start_video()
        else:
            self._stop_video()
            self.say("视频通话关了，摄像头也松开了。", 6000)
        note(f"视频通话 {'开' if checked else '关'}")

    def _start_video(self) -> None:
        """Start the camera session and the preview window.

        **"视频通话"本身就要配着麦克风才有意义**（她的画面是跟着你的话走的），
        所以这里顺手把「允许麦克风」和「对话模式」也打开，并把菜单勾选同步——
        2026-09-25 的教训：这两个开关原来各自独立、文案还写着"你说完一句我就能看到你"，
        用户于是只开了视频、说话时画面是关着的，看起来就是"这功能没用"。
        """
        if not self.config.get("camera_capture", True):
            self.say("摄像头总开关关着呢（「允许摄像头」先勾上）。", 8000)
            return
        turned_on: list[str] = []
        if not self.config.get("mic_listen", False):
            self.config["mic_listen"] = True
            if getattr(self, "act_mic_on", None) is not None:
                self.act_mic_on.setChecked(True)
            turned_on.append("允许麦克风")
        if not self._conversation_on():
            self.config["conversation"] = True
            if getattr(self, "act_conversation", None) is not None:
                self.act_conversation.setChecked(True)
            turned_on.append("对话模式")
        if turned_on:
            save_config(self.config)
        if self._video is None:
            self._video = camera.Session(self)
            self._video.failed.connect(self.on_video_failed)
            self._video.updated.connect(self.on_video_frame)
        if not camera.devices():
            self.say("没找到摄像头。", 7000)
            return
        if not self._video.start():
            return
        if self.config.get("video_preview", True) and self._preview is None:
            self._preview = PreviewWindow()
            self._preview.move(self.x() - camera.PREVIEW_MAX_EDGE - 12, self.y())
        if turned_on:
            self.say(
                "视频通话开着——同时帮你打开了「" + "」「".join(turned_on) + "」。\n"
                "直接说话就行，说完那一句她就看到当时的你（摄像头一直开着，"
                "但只有说话那一刻的画面会发出去，发完就删）。",
                12000,
            )
        else:
            self.say("视频通话开着——说完一句她就看到当时的你。", 8000)
        note(f"视频通话开始（摄像头 {self._video.device_name()}；同时打开：{turned_on or '无'}）")
        # 视频要配着"听"才有意义：顺手把连续听也起起来
        if self._conversation_on() and not self._listening_now():
            self._conversation_touch()
            self._start_listening()

    def _stop_video(self) -> None:
        """Release the camera and close the preview."""
        if self._preview is not None:
            self._preview.hide()
            self._preview = None
        if self._video is not None and self._video.is_open():
            self._video.stop()
            note("视频通话停止（摄像头已释放）")

    def on_video_frame(self) -> None:
        """Paint the newest frame into the preview window."""
        if self._preview is None or self._video is None:
            return
        image = self._video.latest()
        if image is None:
            return
        width, height = camera.preview_size(image.width(), image.height())
        self._preview.set_image(image.scaled(width, height), f"她看到的是这个　{self._video.device_name()}")
        if self._preview.x() > self.x():  # 桌宠挪到左边时，预览也跟着换边
            self._preview.move(max(0, self.x() - width - 12), self.y())

    def on_video_failed(self, message: str) -> None:
        """Report a camera problem and close the mode.

        Args:
            message: Failure text from the session.
        """
        note(f"视频通话失败：{message}")
        self.say(message, 10000)
        self.config["video_call"] = False
        save_config(self.config)
        if getattr(self, "act_video", None) is not None:
            self.act_video.setChecked(False)
        self._stop_video()

    def live_frame(self) -> camera.Frame | None:
        """The current frame, when video call is on and a picture exists.

        Returns:
            A saved frame ready to send, or None when video call is off/not ready.
        """
        if not self._video_on() or self._video is None or not self._video.is_open():
            return None
        frame = self._video.snapshot()
        if frame.error:
            note(f"取实时帧失败：{frame.error}")
            return None
        return frame

    def set_asr_key(self) -> None:
        """Store a speech-to-text key (menu 「语音识别 Key…」)."""
        current = str((self.config.get("asr") or {}).get("api_key") or "")
        text, ok = QInputDialog.getText(
            self,
            "语音识别 Key",
            "留空 = 用 AstrBot 里已经配好的那家；\n填了就优先用你填的（只存本机 config.json）",
            QLineEdit.Password,
            current,
        )
        if not ok:
            return
        block = dict(self.config.get("asr") or {})
        block["api_key"] = str(text or "").strip()
        self.config["asr"] = block
        save_config(self.config)
        note("语音识别 Key " + ("已更新" if block["api_key"] else "已清空（改用 AstrBot 里的）"))
        self.say("记下了~" if block["api_key"] else "好，改用 AstrBot 里的那份 Key。", 5000)

    def toggle_mic(self, checked: bool) -> None:
        """Remember whether the microphone feature is allowed.

        Args:
            checked: New state from the menu item.
        """
        self.config["mic_listen"] = bool(checked)
        save_config(self.config)
        self.say(
            "麦克风开了——但只在你点「听一句」时才会录，录完就删~"
            if checked
            else "好，麦克风关了，我不会再录。",
            6000,
        )

    def hear_once(self) -> None:
        """Record one sentence, transcribe it, and hand it to her (menu 「听一句」).

        和"看屏幕/看一眼"完全同构：**只有你点这一下才会开麦**，没有常驻监听；
        锁屏时不录（你人不在，不该开麦）；录到的 wav 发完就删（除非开了 `mic_keep_clip`）。
        """
        if not self.config.get("mic_listen", False):
            self.say("麦克风关着呢——右键点「允许麦克风」我才会听。", 7000)
            return
        if self._mic_busy:
            self.say("上一条还在识别呢~", 4000)
            return
        now = time.time()
        if now - self._mic_last < microphone.COOLDOWN_SECONDS:
            self.say("刚录过一句，缓一下~", 4000)
            return
        readings = pc_state.name_of(pc_state.read_state())
        if readings.get("locked") is True:
            # 锁屏＝人不在。这里刻意**不录**，而不是"录了但不说"
            self.say("你锁屏了吧？我先不录。", 6000)
            return
        if not microphone.devices():
            self.say("没找到麦克风（QtMultimedia 没拿到设备）。", 8000)
            return
        options = dict(self.config.get("asr") or {})
        if str(options.get("engine") or "local").lower() == "local":
            # 先检查模型，别录完 5 秒才发现没模型——那时候你话已经说完了
            try:
                import local_asr  # noqa: PLC0415

                if not local_asr.is_ready():
                    self.say("本机还没下识别模型——右键点「下载识别模型…」（约 78 MB）。", 12000)
                    return
            except Exception as exc:  # noqa: BLE001 - 模块缺失
                self.say(f"本机识别模块不可用：{type(exc).__name__}", 9000)
                return
        seconds = microphone.normalize_seconds(self.config.get("mic_seconds"))
        self._mic_busy = True
        self._mic_last = now
        self.say(f"听着呢…（{seconds:.0f} 秒）", 4000)
        device = str(self.config.get("mic_device") or "")
        threading.Thread(
            target=lambda: self._net.heard.emit(
                *record_and_transcribe(seconds, device, options)
            ),
            name="pet-hear",
            daemon=True,
        ).start()

    def on_heard(self, clip, text: str, error: str) -> None:  # noqa: ANN001 - microphone.Clip
        """Deliver a finished transcription (runs on the UI thread).

        Args:
            clip: The recording (may carry its own error).
            text: Transcript, empty on failure.
            error: Failure message, empty on success.
        """
        self._mic_busy = False
        keep = bool(self.config.get("mic_keep_clip", False))
        if clip.path and not keep:
            microphone.drop(clip.path)
        if error:
            note(f"听一句失败：{error}（{clip.describe()}）")
            self.say(error, 10000)
            return
        note(f"听成：{text}（{clip.describe()}）")
        self.say(f"你说的：「{text}」", 6000)
        client = self._client or self._make_client()
        # 视频通话开着时，「听一句」也顺手把此刻那一帧带上（同一套规矩：发完就删）
        frame = self.live_frame()
        if frame is not None:
            note(f"视频通话随话附帧　{frame.describe()}")
            client.send(text, frame.path)
            if not bool(self.config.get("camera_keep_frame", False)):
                QTimer.singleShot(
                    camera.DELETE_AFTER_SECONDS * 1000,
                    lambda path=frame.path: camera.drop(path),
                )
            return
        client.send(text)

    def set_location(self) -> None:
        """Ask for a city name and store the coordinates it resolves to (menu 「设置位置…」）。"""
        if self._geo_busy:
            self.say("还在查上一个地名呢~", 4000)
            return
        current = str(self.config.get("weather_place") or "")
        text, ok = QInputDialog.getText(
            self,
            "设置位置",
            f"城市名（现在：{current or '还没设'}）\n例：北京市 / 杭州市",
            text=current,
        )
        query = str(text or "").strip()
        if not ok or not query:
            return
        self._geo_busy = True
        self.say(f"我查查「{query}」在哪…", 5000)
        threading.Thread(
            target=lambda: self._net.place.emit(fetch_places(query), query),
            name="pet-geo",
            daemon=True,
        ).start()

    def on_place(self, payload, query: str) -> None:  # noqa: ANN001 - (list, str)
        """Store the best geocoding hit, or explain why nothing was stored.

        Args:
            payload: ``(candidates, error)`` from the worker thread.
            query: What the user typed.
        """
        self._geo_busy = False
        places, error = payload
        if error:
            note(f"地名查询失败「{query}」：{error}")
            self.say(f"查「{query}」没成功：{error}", 8000)
            return
        if not places:
            note(f"地名查不到「{query}」")
            self.say(
                f"没找到「{query}」这个城市——试着加个「市」再试，比如「{query}市」。",
                9000,
            )
            return
        best = places[0]
        self.config["weather_place"] = best.name
        self.config["weather_lat"] = best.latitude
        self.config["weather_lon"] = best.longitude
        save_config(self.config)
        note(
            f"位置设为 {best.describe()} {best.latitude:.4f},{best.longitude:.4f}（候选 {len(places)} 个）"
        )
        extra = f"（另有 {len(places) - 1} 个同名的地方）" if len(places) > 1 else ""
        self.say(
            f"记下了：{best.describe()}　{best.latitude:.2f},{best.longitude:.2f}{extra}",
            10000,
        )

    def toggle_camera(self, checked: bool) -> None:
        """Remember whether the camera feature is allowed.

        Args:
            checked: New state from the menu item.
        """
        self.config["camera_capture"] = bool(checked)
        save_config(self.config)
        self.say(
            "摄像头开着——但只在你点「用摄像头看一眼」时才会拍~"
            if checked
            else "好，摄像头关了，我不会再拍。",
            6000,
        )

    def maybe_propose_camera(self) -> None:
        """Offer to take a look when you are around but quiet.

        **This never captures anything** — it only puts a line in the bubble. The
        camera runs only after you click the menu item (see :meth:`look_at_camera`).
        """
        if not self.config.get("camera_propose", True):
            return
        now = time.time()
        values = pc_state.name_of(pc_state.read_state())
        cameras = values.get("cameras")
        offers_today = len(
            [stamp for stamp in self._camera_offers if now - stamp < 86400],
        )
        if not camera.should_propose(
            now=now,
            enabled=bool(self.config.get("camera_capture", True)),
            has_camera=bool(isinstance(cameras, list) and cameras),
            idle_seconds=values.get("idle_seconds"),
            locked=values.get("locked"),
            last_offer_at=self._camera_last_offer,
            offers_today=offers_today,
            last_interaction_at=self._last_talk_at,
        ):
            return
        self._camera_last_offer = now
        self._camera_offers.append(now)
        note("提议看一眼（只提议，等她点菜单）")
        self.say("你在呀？想让我看一眼你吗——右键「用摄像头看一眼」我就看~", 12000)

    def look_at_camera(self) -> None:
        """Capture exactly one frame and send it (only after an explicit click).

        抓之前先把桌宠/气泡/聊天窗藏起来（外接摄像头有可能拍到屏幕），抓完立刻恢复；
        发出去之后延时删除这一帧——**用完即弃，不留在 shots 里**。
        """
        now = time.time()
        has_camera = bool(camera.devices())
        if not camera.can_capture(
            now=now,
            enabled=bool(self.config.get("camera_capture", True)),
            has_camera=has_camera,
            last_capture_at=self._camera_last_capture,
        ):
            self.say("摄像头关着，或者刚看过一眼——等一下再说~", 5000)
            return
        self._camera_last_capture = now
        self._last_talk_at = now
        client = self._client or self._make_client()
        windows = [self, self.bubble, getattr(self, "_chat_window", None)]
        hidden = [win for win in windows if win is not None and win.isVisible()]
        for win in hidden:
            win.hide()
        QApplication.processEvents()
        try:
            frame = camera.capture()
        finally:
            for win in hidden:
                win.show()
        if frame.error:
            self.say(frame.describe(), 6000)
            note(frame.describe())
            return
        note(f"摄像头一帧　{frame.describe()}")
        self.say(f"我看看…（{frame.width}x{frame.height}）", 4000)
        client.send(camera.text_hint(), frame.path)
        if self.config.get("camera_keep_frame", False):
            self.say(f"这一帧留在 {frame.path}", 6000)
            return
        QTimer.singleShot(
            camera.DELETE_AFTER_SECONDS * 1000,
            lambda: camera.drop(frame.path),
        )

    def _remember_session(self, session_id: str) -> None:
        """Store the panel session id for the log.

        Args:
            session_id: Session id reported by the client.
        """
        self.config["chat_session_id"] = session_id
        save_config(self.config)

    def _on_chat_reply(self, text: str) -> None:
        """Show her answer above the pet and animate her mouth.

        Args:
            text: Her reply.
        """
        if not text:
            return
        self._speaking_until = time.time() + 2.5
        self._stream_text = ""
        self._thinking = False
        self.say(text, SPEAK_BUBBLE_MS)
        # 她的回复到齐了：对话模式继续听下一句
        self._resume_after_reply()

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """Start a drag or remember a click.

        Args:
            event: Mouse event.
        """
        if event.button() == Qt.LeftButton:
            self._dragging = True
            self._press_pos = event.globalPosition().toPoint()
            self._drag_offset = self._press_pos - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """Drag her around.

        Args:
            event: Mouse event.
        """
        if self._dragging and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """Finish a drag, or treat it as a click.

        Args:
            event: Mouse event.
        """
        if event.button() != Qt.LeftButton:
            return
        moved = (event.globalPosition().toPoint() - self._press_pos).manhattanLength()
        self._dragging = False
        self._remember_position()
        if moved <= 4:
            self.say_line()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """Double click opens the chat window.

        Args:
            event: Mouse event.
        """
        self.open_chat()

    def contextMenuEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """Right click opens the menu.

        Args:
            event: Context menu event.
        """
        self.menu.popup(event.globalPos())

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """Persist the position on exit.

        Args:
            event: Close event.
        """
        self._remember_position()
        event.accept()


def screenshot(path: str) -> None:
    """Render every look into one PNG grid (checks the drawing without a screen).

    Args:
        path: Output image path.
    """
    keys = ["calm", "smile", "happy", "shy", "down", "huffy", "sleepy", "surprised"]
    cell_w, cell_h = faces.DESIGN_W, faces.DESIGN_H
    canvas = QPixmap(cell_w * 4, cell_h * 2)
    canvas.fill(QColor(245, 245, 250))
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.Antialiasing)
    for index, key in enumerate(keys):
        look = faces.LOOKS[key]
        column, row = index % 4, index // 4
        painter.save()
        painter.translate(column * cell_w, row * cell_h)
        painter.setPen(QColor(120, 120, 140))
        painter.drawRect(0, 0, cell_w - 1, cell_h - 1)
        painter.drawText(8, 20, f"{look.key} / {look.label}")
        faces.paint_character(painter, look, t=1.2)
        painter.restore()
    painter.end()
    canvas.save(path)
    print(f"已渲染表情图: {path}（{canvas.width()}x{canvas.height()}）")


def main() -> None:
    """Entry point."""
    args = sys.argv[1:]
    if "--selftest" in args:
        QApplication(sys.argv)  # 立绘要用 QPixmap，先建 app
        print("=== 状态 → 表情")
        faces.selftest()
        print("=== 当前快照")
        state_mod.main()
        print("=== 立绘")
        sprite.selftest()
        return
    if "--shot" in args:
        app = QApplication(sys.argv)
        screenshot(args[args.index("--shot") + 1])
        app.quit()
        return

    if "--ask" in args:
        # 命令行自检：不开窗口，直接问一句并把答案写进 pet_log.txt。
        # 打包发出去之后用户遇到问题，可以让他在命令行跑这个，日志里就有证据。
        # 独立模式问她自己的大脑；连 AstrBot 模式则走桌面通道，验证地址与密钥。
        question = (
            args[args.index("--ask") + 1]
            if len(args) > args.index("--ask") + 1
            else "你好"
        )
        paths.ensure_layout()
        from PySide6.QtCore import QCoreApplication, QTimer

        config = load_config()
        core = QCoreApplication(sys.argv)
        finals: list[str] = []

        def finish(text: str) -> None:
            finals.append(text)
            note(f"[自检] 问：{question}\n[自检] 答：{text}")
            core.quit()

        def give_up(message: str) -> None:
            note(f"[自检] 失败：{message}")
            core.quit()

        if str(config.get("mode") or "astrbot") == "local":
            import brain

            client = brain.LocalBrainClient(config)
        else:
            client = PetLinkClient(
                str(config.get("desktop_url") or "ws://127.0.0.1:6198"),
                str(config.get("desktop_secret") or ""),
            )
        client.done.connect(finish)
        client.failed.connect(give_up)
        client.ready.connect(lambda who: note(f"[自检] {who}"))
        client.start()
        QTimer.singleShot(3000, lambda: client.send(question))
        QTimer.singleShot(180000, core.quit)
        core.exec()
        print(finals[-1] if finals else "（没拿到回答，看 pet_log.txt）")
        return

    install_crash_log()
    paths.ensure_layout()
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    info = sprite.load()
    note(
        f"启动 pid={os.getpid()} 立绘={info.name or '（占位小人）'} "
        f"{info.width}x{info.height}{' 已抠背景' if info.cut else ''}"
        f"{' 圆角边框' if info.framed else ''} 窗口={int(faces.DESIGN_W * 1)}x{int(faces.DESIGN_H * 1)}",
    )
    window = PetWindow()
    note(
        f"窗口就绪 位置=({window.x()}, {window.y()}) 大小={window.width()}x{window.height()}"
    )
    window.say_line()
    if "--bubble" in args:
        window.say(args[args.index("--bubble") + 1], 20000)
    code = app.exec()
    note(f"退出 code={code}")
    sys.exit(code)


if __name__ == "__main__":
    main()
