"""对话模式：连续听、自动断句（P4-b）。

和「听一句（点一次录一句）」的区别只有一个：**麦克风在对话模式里一直开着**，
由 silero VAD 判断"你这句话说完了没有"，说完就把这一整句交给识别。

守的规矩（比 P3 更严，因为这里是常开）：

1. **默认关**，且只在"对话模式"里工作——进出都由你决定，随时可关；
2. **锁屏不听**、**她正在想/正在说的时候不听**（否则会把自己的声音或上一句的尾巴吃回来，
   这是常开麦克风最容易出的自激）；
3. **一点没交互就自动退出**对话模式（免得你走了它还在听一整天）；
4. 采集与推理**都不在 UI 线程**：Qt 只负责把 PCM 读出来丢进队列，VAD 在后台线程跑；
5. 说完的那一段 wav 存在本机 `clips/`，交给识别后**按既有规矩删除**。

VAD 模型（silero，实测 629 KB）与识别模型分开存：`models/vad/silero_vad.onnx`。
"""

import queue
import threading
import time
import wave
from array import array
from dataclasses import dataclass
from pathlib import Path

import model_store
import paths

try:
    from PySide6.QtCore import QObject, QTimer, Signal
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtMultimedia import QAudioFormat, QAudioSource, QMediaDevices
except Exception:  # noqa: BLE001 - 没有 Qt 就当没有麦克风
    QAudioSource = None

SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2
WINDOW = 512  # silero VAD 固定吃 512 个采样点
CLIPS = paths.BASE / "clips"
VAD_DIR = model_store.MODELS / "vad"
VAD_FILE = VAD_DIR / "silero_vad.onnx"
# 实测：GitHub 629 KB、镜像 1770 KB；两个都可达，按顺序试
VAD_SOURCES = (
    (
        "GitHub",
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx",
    ),
    ("hf-mirror", "https://hf-mirror.com/csukuangfj/vad/resolve/main/silero_vad.onnx"),
)
DEFAULT_SILENCE_MS = 600  # 静音多久算"这句说完了"（700→450 太急、又回到 600：句中停顿被切碎才是识别错的主因）
DEFAULT_MIN_SPEECH_MS = 250  # 短于这个的当咳嗽/环境声，丢掉
MAX_UTTERANCE_SECONDS = 20.0
QUEUE_SECONDS = 8.0
IDLE_EXIT_SECONDS = 180  # 对话模式里多久没说话就自动退出


def is_ready() -> bool:
    """Is the VAD model on disk?

    Returns:
        True when the silero model file exists.
    """
    return VAD_FILE.is_file() and VAD_FILE.stat().st_size > 100_000


def describe() -> str:
    """One line about the VAD model, for the bubble/log.

    Returns:
        Chinese description.
    """
    if not is_ready():
        return f"静音检测模型还没下（{VAD_FILE}）"
    return f"静音检测：{VAD_FILE.name}（{VAD_FILE.stat().st_size / 1024:.0f} KB）"


def fetch_vad(progress=None) -> str:  # noqa: ANN001 - 可选回调
    """Download the silero VAD model.

    Args:
        progress: Optional ``callable(done, total)``.

    Returns:
        A Chinese result line.

    Raises:
        OSError: When every source failed.
    """
    if is_ready():
        return f"静音检测模型已经在本机了（{describe()}）"
    label, size = model_store.fetch_first(VAD_SOURCES, VAD_FILE, progress)
    if not is_ready():
        VAD_FILE.unlink(missing_ok=True)
        raise OSError(f"下到的文件不像 VAD 模型（{size} 字节，来自 {label}）")
    return f"静音检测模型装好了（来自 {label}）：{describe()}"


def normalize_ms(value, fallback: float) -> float:  # noqa: ANN001 - 配置值
    """Clamp a millisecond setting.

    Args:
        value: Raw setting (may be missing or nonsense).
        fallback: Value to use when the setting is unusable.

    Returns:
        Milliseconds in a sane range (50 … 5000).
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if number != number:  # NaN
        return fallback
    return min(5000.0, max(50.0, number))


def pcm_to_float(pcm: bytes) -> list[float]:
    """Convert raw 16-bit samples to floats in -1 … 1.

    Args:
        pcm: Raw little-endian Int16 bytes.

    Returns:
        Float samples.
    """
    if not pcm:
        return []
    raw = array("h")
    raw.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    return [value / 32768.0 for value in raw]


def rms_of(samples) -> float:  # noqa: ANN001 - float 列表
    """Level of one frame, used for the "太轻了" gate.

    Args:
        samples: Float samples.

    Returns:
        RMS in 0 … 1 (0.0 for empty input).
    """
    if not samples:
        return 0.0
    total = 0.0
    for value in samples:
        total += value * value
    return (total / len(samples)) ** 0.5


def should_listen(
    *,
    conversation: bool,
    locked: bool | None,
    thinking: bool,
    speaking: bool,
    has_device: bool,
    already_running: bool,
    mic_enabled: bool = True,
) -> tuple[bool, str]:
    """Decide whether the always-on listener may run right now.

    Args:
        conversation: Whether the user turned conversation mode on.
        locked: Screen-lock reading (None = unknown).
        thinking: She is waiting for a reply.
        speaking: She is currently talking.
        has_device: A microphone is visible.
        already_running: The listener is already running.
        mic_enabled: The 麦克风总开关 (``mic_listen``) is on.

    Returns:
        ``(allowed, reason)`` — the reason is empty when allowed.
    """
    if not conversation:
        return False, "对话模式关着"
    if not mic_enabled:
        # 总开关就是总开关：对话模式不该绕过它。以前漏了这一条，
        # 于是"麦克风总开关关着"时对话模式照样能录——2026-09-25 补上。
        return False, "麦克风总开关关着（先勾「允许麦克风」）"
    if locked is True:
        return False, "你锁屏了"
    if thinking or speaking:
        # 自激防线：她还在说话/还在想的时候不收音
        return False, "她正在说话"
    if not has_device:
        return False, "没有麦克风"
    if already_running:
        return False, "已经在听了"
    return True, ""


@dataclass
class Utterance:
    """One finished sentence."""

    path: str = ""
    seconds: float = 0.0
    level: float = 0.0
    error: str = ""

    def describe(self) -> str:
        """One-line summary for the bubble/log.

        Returns:
            Chinese summary; the error text when there is nothing usable.
        """
        if self.error:
            return f"没听清：{self.error}"
        # level 是 0…1 的 RMS；这里**不能**用 :.0f —— 0.099 会被显示成"音量 0"，
        # 看起来像没录到东西（真踩过）
        return f"{self.seconds:.1f} 秒　音量 {self.level:.2f}"


def write_wav(samples, path: Path) -> bool:  # noqa: ANN001 - float 列表
    """Write float samples as a 16 kHz mono 16-bit wav.

    Args:
        samples: Float samples in -1 … 1.
        path: Destination path.

    Returns:
        True when the file was written.
    """
    payload = array("h", (int(max(-1.0, min(1.0, value)) * 32767) for value in samples))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(CHANNELS)
            handle.setsampwidth(SAMPLE_WIDTH)
            handle.setframerate(SAMPLE_RATE)
            handle.writeframes(payload.tobytes())
    except (OSError, ValueError):
        return False
    return True


# 一句话中间停顿一下，VAD 就会把它切成两段。**切在词中间**是识别出错的一大来源
# （尤其人名/数字），所以把相邻两段的**音频拼起来重新识别一次**，而不是分别识别再拼文字——
# 后者丢掉了跨段的上下文（2026-09-25 加）。
SHORT_SEGMENT_SECONDS = 1.2  # 短于这个的段很可能是半句话，值得等一等合并
MERGE_WINDOW_MS = 600  # 等一下看看有没有下一段


def merge_wavs(paths, target: Path) -> bool:  # noqa: ANN001 - 路径列表
    """Concatenate several 16 kHz mono wav files into one.

    Args:
        paths: Source wav paths, in order.
        target: Destination path.

    Returns:
        True when the merged file was written.
    """
    frames = bytearray()
    params = None
    for item in paths:
        try:
            with wave.open(str(item), "rb") as handle:
                current = (
                    handle.getnchannels(),
                    handle.getsampwidth(),
                    handle.getframerate(),
                )
                if params is None:
                    params = current
                elif params != current:
                    return False  # 格式不一致就别硬拼
                frames.extend(handle.readframes(handle.getnframes()))
        except (OSError, wave.Error):
            return False
    if params is None or not frames:
        return False
    channels, width, rate = params
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(target), "wb") as handle:
            handle.setnchannels(channels)
            handle.setsampwidth(width)
            handle.setframerate(rate)
            handle.writeframes(bytes(frames))
    except OSError:
        return False
    return True


def load_vad(silence_ms: float = DEFAULT_SILENCE_MS, min_speech_ms: float = DEFAULT_MIN_SPEECH_MS):
    """Build the silero VAD.

    Args:
        silence_ms: Silence duration that ends a sentence.
        min_speech_ms: Shortest accepted speech.

    Returns:
        A ``sherpa_onnx.VoiceActivityDetector``.

    Raises:
        RuntimeError: When the engine or the model is missing.
    """
    try:
        import sherpa_onnx  # noqa: PLC0415 - 可选依赖
    except Exception as exc:  # noqa: BLE001 - 没装引擎
        raise RuntimeError("没装本机识别引擎（pip install sherpa-onnx）") from exc
    if not is_ready():
        raise RuntimeError(f"还没有静音检测模型（{VAD_FILE}）")
    config = sherpa_onnx.VadModelConfig()
    config.silero_vad.model = str(VAD_FILE)
    config.silero_vad.threshold = 0.5
    config.silero_vad.min_silence_duration = silence_ms / 1000.0
    config.silero_vad.min_speech_duration = min_speech_ms / 1000.0
    config.silero_vad.max_speech_duration = MAX_UTTERANCE_SECONDS
    config.sample_rate = SAMPLE_RATE
    config.num_threads = 1
    return sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=30)


class SegmentWatcher:
    """Feed frames into the VAD and hand back finished sentences.

    纯逻辑、不碰 Qt：喂 float 帧进去，够了就把一段样本返回。
    """

    def __init__(self, vad, *, min_level: float = 0.004) -> None:  # noqa: ANN001 - VAD 对象
        self.vad = vad
        self.min_level = min_level
        self.pending: list[float] = []
        self._frames: list[float] = []
        self.dropped = 0

    def feed(self, samples: list[float]) -> list[list[float]]:
        """Push samples in, get finished segments out.

        Args:
            samples: Float samples (any length).

        Returns:
            Zero or more finished segments.
        """
        self._frames.extend(samples)
        done: list[list[float]] = []
        while len(self._frames) >= WINDOW:
            frame, self._frames = self._frames[:WINDOW], self._frames[WINDOW:]
            self.vad.accept_waveform(frame)
            while not self.vad.empty():
                segment = self.vad.front
                # **必须先读 samples 再 pop**：`front` 给的是指向 VAD 内部缓冲的视图，
                # pop 之后就读不出样本了。顺序写反的表现是"检测到句子但样本为空"，
                # 然后被下面的 `continue` 静默跳过——一句都发不出去，还不报错。
                segment_samples = list(getattr(segment, "samples", []) or [])
                self.vad.pop()
                if not segment_samples:
                    continue
                if rms_of(segment_samples) < self.min_level:
                    # 太轻的一段（咳嗽、键盘、空调）：丢掉，别送去识别
                    self.dropped += 1
                    continue
                done.append(segment_samples)
        return done


class Listener(QObject):
    """Always-on capture for conversation mode.

    Qt 侧只做两件事：定时把 PCM 从 ``QAudioSource`` 读出来、丢进队列；
    真正的 VAD 推理在后台线程里，结果用信号回主线程。
    """

    utterance = Signal(object)  # Utterance
    failed = Signal(str)
    level = Signal(float)

    def __init__(self, *, silence_ms: float = DEFAULT_SILENCE_MS, min_speech_ms: float = DEFAULT_MIN_SPEECH_MS) -> None:
        super().__init__()
        self.silence_ms = silence_ms
        self.min_speech_ms = min_speech_ms
        self._source = None
        self._io = None
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._timer = QTimer(self)
        self._timer.setInterval(50)

    def is_running(self) -> bool:
        """Is the microphone currently open?

        Returns:
            True while capturing.
        """
        return self._source is not None

    def start(self) -> bool:
        """Open the microphone and begin watching for sentences.

        Returns:
            True when capture started.
        """
        if self._source is not None:
            return True
        if QAudioSource is None or QGuiApplication.instance() is None:
            self.failed.emit("这个环境没有 QtMultimedia")
            return False
        if not is_ready():
            self.failed.emit(f"还没有静音检测模型（右键「下载本机模型…」）")
            return False
        fmt = QAudioFormat()
        fmt.setSampleRate(SAMPLE_RATE)
        fmt.setChannelCount(CHANNELS)
        fmt.setSampleFormat(QAudioFormat.Int16)
        device = QMediaDevices.defaultAudioInput()
        if device is None or device.isNull() or not device.isFormatSupported(fmt):
            self.failed.emit("默认麦克风不支持 16kHz 单声道")
            return False
        try:
            self._source = QAudioSource(device, fmt)
            self._io = self._source.start()  # 拉模式：我们自己定时读
        except Exception as exc:  # noqa: BLE001 - 打开失败不该把桌宠带崩
            self._source = None
            self._io = None
            self.failed.emit(f"打开麦克风失败：{type(exc).__name__}: {exc}")
            return False
        if self._io is None:
            self.stop()
            self.failed.emit("麦克风没给出数据流")
            return False
        self._stop.clear()
        self._queue = queue.Queue()
        self._thread = threading.Thread(target=self._work, name="pet-listen", daemon=True)
        self._thread.start()
        self._timer.timeout.connect(self._pump)
        self._timer.start()
        return True

    def stop(self) -> None:
        """Close the microphone and stop the worker."""
        self._timer.stop()
        try:
            self._timer.timeout.disconnect(self._pump)
        except (RuntimeError, TypeError):
            pass
        self._stop.set()
        if self._source is not None:
            try:
                self._source.stop()
            except RuntimeError:
                pass
        self._source = None
        self._io = None
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.5)

    def _pump(self) -> None:
        """Read whatever the microphone produced and queue it (UI thread, cheap)."""
        if self._io is None:
            return
        try:
            chunk = bytes(self._io.readAll().data())
        except RuntimeError:
            return
        if chunk:
            self._queue.put(chunk)

    def _work(self) -> None:
        """Run the VAD on a worker thread and emit finished sentences."""
        try:
            vad = load_vad(self.silence_ms, self.min_speech_ms)
        except Exception as exc:  # noqa: BLE001 - 后台线程必须自己兜住
            self.failed.emit(f"静音检测起不来：{exc}")
            return
        watcher = SegmentWatcher(vad)
        counter = 0
        while not self._stop.is_set():
            try:
                pcm = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            samples = pcm_to_float(pcm)
            if not samples:
                continue
            self.level.emit(rms_of(samples))
            try:
                finished = watcher.feed(samples)
            except Exception as exc:  # noqa: BLE001 - 推理出错就报一次、别静默死掉
                self.failed.emit(f"静音检测出错：{type(exc).__name__}: {exc}")
                return
            for segment in finished:
                counter += 1
                stamp = time.strftime("%m%d_%H%M%S")
                path = CLIPS / f"talk_{stamp}_{counter:02d}.wav"
                if not write_wav(segment, path):
                    self.failed.emit("写这一段录音失败")
                    continue
                self.utterance.emit(
                    Utterance(
                        path=str(path),
                        seconds=len(segment) / SAMPLE_RATE,
                        level=rms_of(segment),
                    )
                )
