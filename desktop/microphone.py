"""麦克风：点一次、录一句、交出去转文字（P3-b「听懂」）。

规矩沿用 P0–P2，麦克风这块尤其要守：

1. **只在你点菜单的时候录**——没有常驻监听、没有 VAD 循环、没有"一直在听"；
2. **录到的 wav 交出去就删**（`mic_keep_clip` 可改成保留，方便你自己听）；
3. **锁屏时不录**（复用 `pc_state` 的读数）：你人不在，就不该开麦；
4. **太安静就直说"没听到"**，不把空音频送去识别、更不编一句"你刚才说……"；
5. 录音只在桌宠进程内、写在本机 `clips/`，除了送去识别（见 `asr.py`）之外不发给任何人。

录制走 Qt（`QAudioSource`）：实测本机 `16 kHz / 单声道 / Int16` 被支持，正好是识别要的格式，
所以不需要额外依赖、也不需要重采样（`audioop` 在 Python 3.13 已被移除，所以 RMS 用纯 `array` 算）。
"""

import math
import time
import wave
from array import array
from dataclasses import dataclass
from pathlib import Path

import paths

try:  # 打包版也带了 QtMultimedia（见 build_exe.py），这里仍然容错，缺了不影响其它功能
    from PySide6.QtCore import QBuffer, QByteArray, QEventLoop, QIODevice, QTimer
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtMultimedia import QAudio, QAudioFormat, QAudioSource, QMediaDevices
except Exception:  # noqa: BLE001 - 没有 Qt 就当没有麦克风
    QAudioSource = None

CLIPS = paths.BASE / "clips"
SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2  # Int16
DEFAULT_SECONDS = 5.0
MIN_SECONDS = 1.0
MAX_SECONDS = 15.0
# 低于这个 RMS 就认为"这一句里没人说话"（16k Int16 满量程是 32767；实测安静房间的底噪远小于它）
MIN_RMS = 120.0
COOLDOWN_SECONDS = 3.0
KEEP_CLIPS = 10


@dataclass
class Clip:
    """One recording."""

    path: str = ""
    seconds: float = 0.0
    rms: float = 0.0
    peak: int = 0
    device: str = ""
    error: str = ""

    def describe(self) -> str:
        """One-line summary for the bubble/log.

        Returns:
            Chinese summary; the error text when the recording failed.
        """
        if self.error:
            return f"没录成：{self.error}"
        return f"{self.seconds:.1f} 秒　{self.device}　音量 {self.rms:.0f}/{self.peak}"


def rms_of(pcm: bytes) -> float:
    """Root-mean-square level of raw 16-bit samples.

    Args:
        pcm: Raw little-endian Int16 bytes.

    Returns:
        The RMS level (0.0 for empty or unreadable input).
    """
    if not pcm:
        return 0.0
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    if not samples:
        return 0.0
    total = 0
    for value in samples:
        total += value * value
    return math.sqrt(total / len(samples))


def peak_of(pcm: bytes) -> int:
    """Largest absolute sample value.

    Args:
        pcm: Raw little-endian Int16 bytes.

    Returns:
        The peak level (0 when there is nothing to read).
    """
    if not pcm:
        return 0
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    return max((abs(value) for value in samples), default=0)


def normalize_seconds(value) -> float:  # noqa: ANN001 - 可能来自配置，什么都可能
    """Clamp a requested duration into the allowed range.

    Args:
        value: Requested seconds (may be missing, a string, or nonsense).

    Returns:
        A duration between ``MIN_SECONDS`` and ``MAX_SECONDS``.
    """
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return DEFAULT_SECONDS
    if not math.isfinite(seconds):
        return DEFAULT_SECONDS
    return min(MAX_SECONDS, max(MIN_SECONDS, seconds))


def is_audible(rms: float, threshold: float = MIN_RMS) -> bool:
    """Was there anything worth transcribing?

    Args:
        rms: Measured level.
        threshold: Level below which we call it silence.

    Returns:
        True when the clip is loud enough to be worth sending to recognition.
    """
    return rms >= threshold


def devices() -> list[str]:
    """Microphone names Qt can see.

    Returns:
        Device descriptions (empty when Qt Multimedia or the Qt app is missing).

    Note:
        **必须先有 QGuiApplication**：实测在没有 Qt 应用实例的进程里调
        ``QMediaDevices.audioInputs()`` 会**卡住不返回**（写离线测试时踩到，整个测试超时），
        所以这里和 :func:`record` 一样先检查实例。
    """
    if QAudioSource is None or QGuiApplication.instance() is None:
        return []
    try:
        return [str(device.description() or "") for device in QMediaDevices.audioInputs()]
    except Exception:  # noqa: BLE001 - 枚举失败就当没有
        return []


def _pick_device(name: str):  # noqa: ANN202 - QAudioDevice
    """Choose an input device by (partial) name.

    Args:
        name: Configured device name; empty means "system default".

    Returns:
        The matching QAudioDevice, or the default one.
    """
    wanted = str(name or "").strip().lower()
    device = QMediaDevices.defaultAudioInput()
    if wanted:
        for candidate in QMediaDevices.audioInputs():
            if wanted in str(candidate.description() or "").lower():
                return candidate
    return device


def record(seconds=None, device: str = "") -> Clip:  # noqa: ANN001 - 配置值
    """Record one clip to a wav file.

    Args:
        seconds: Requested duration (clamped by :func:`normalize_seconds`).
        device: Device name substring; empty means the system default.

    Returns:
        The clip (``error`` set when recording was impossible).
    """
    clip = Clip()
    if QAudioSource is None:
        clip.error = "这个版本没带 QtMultimedia"
        return clip
    if QGuiApplication.instance() is None:
        clip.error = "没有图形环境"
        return clip
    duration = normalize_seconds(seconds)
    fmt = QAudioFormat()
    fmt.setSampleRate(SAMPLE_RATE)
    fmt.setChannelCount(CHANNELS)
    fmt.setSampleFormat(QAudioFormat.Int16)
    try:
        target = _pick_device(device)
        if target is None or target.isNull():
            clip.error = "没有找到麦克风"
            return clip
        clip.device = str(target.description() or "")
        if not target.isFormatSupported(fmt):
            # 不做重采样：宁可如实说"这个设备不支持"，也不要悄悄录成别的格式后再出错
            clip.error = f"{clip.device} 不支持 16kHz 单声道"
            return clip
        source = QAudioSource(target, fmt)
        # 关键：**不要**写 `QBuffer(QByteArray())`。那个 QByteArray 是临时对象，Python 一 GC，
        # Qt 还在往已被回收的内存里灌音频 —— 实测会以原生方式崩溃（弹
        # "python.exe - 应用程序错误 / 0xFFFFFFFFFFFFFFFF 内存不能为 read"，Python 层抓不到）。
        # 不传参数的 QBuffer 用自己的内部缓冲区，生命周期跟着 QBuffer 走，才是安全写法。
        buffer = QBuffer()
        buffer.open(QIODevice.WriteOnly)
        source.start(buffer)
        # 这里**不能**写 `source.error() != QAudio.Error.NoError`：PySide6 6.x 里
        # `source.error()` 返回的是模块级的 `Error` 枚举，而 `QAudio.Error.NoError` 是**另一个**
        # 枚举类，同名却不相等（实测 `==` 与 `is` 都是 False），于是"明明没出错"也会被判成失败。
        # 按名字比就与枚举类无关了。
        error = source.error()
        if getattr(error, "name", str(error)) != "NoError":
            clip.error = f"打开麦克风失败：{error}"
            return clip
        loop = QEventLoop()
        QTimer.singleShot(int(duration * 1000), loop.quit)
        loop.exec()
        source.stop()
        pcm = bytes(buffer.data())
    except Exception as exc:  # noqa: BLE001 - 录音失败不该把桌宠带崩
        clip.error = f"{type(exc).__name__}: {exc}"
        return clip
    if not pcm:
        clip.error = "一个字都没录到"
        return clip
    clip.seconds = len(pcm) / (SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS)
    clip.rms = rms_of(pcm)
    clip.peak = peak_of(pcm)
    CLIPS.mkdir(parents=True, exist_ok=True)
    path = CLIPS / f"mic_{time.strftime('%m%d_%H%M%S')}.wav"
    try:
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(CHANNELS)
            handle.setsampwidth(SAMPLE_WIDTH)
            handle.setframerate(SAMPLE_RATE)
            handle.writeframes(pcm)
    except OSError as exc:
        clip.error = f"写文件失败：{exc}"
        return clip
    clip.path = str(path)
    _trim()
    return clip


def _trim() -> None:
    """Keep only the newest ``KEEP_CLIPS`` recordings."""
    files = sorted(CLIPS.glob("mic_*.wav"), key=lambda path: path.stat().st_mtime)
    for old in files[:-KEEP_CLIPS]:
        try:
            old.unlink()
        except OSError:
            pass


def drop(path: str) -> bool:
    """Delete one recording (used right after it has been transcribed).

    Args:
        path: File to remove.

    Returns:
        True when the file is gone.
    """
    if not path:
        return False
    try:
        Path(path).unlink()
        return True
    except OSError:
        return False


def text_hint() -> str:
    """What to send together with the transcript.

    Returns:
        A Chinese instruction telling her this came from the microphone.
    """
    return "（这句是我刚对着麦克风说的）"
