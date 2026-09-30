"""随身设备模拟器 / 本机语音对话（PC 版）：把"她"接到耳机上。

链路与眼镜方案一致（只是把"设备"换成了这台电脑 + 蓝牙耳机）：

    麦克风 → VAD 断句 → 本机离线识别 → **只把文字**发给 AstrBot（``glasses`` 平台）
    她回话时服务端把**裸 PCM** 推下来 → 这里直接喂声卡（设备端零解码）

四种跑法（默认连 ``ws://127.0.0.1:6200``）：

    python glasses_sim.py --list-devices                 # 先看有哪些录音/放音设备
    python glasses_sim.py --text "在吗"                   # 只测文本链路（最快，不碰麦克风）
    python glasses_sim.py --mic --input WH-CH520          # 按回车说一句（默认 5 秒）
    python glasses_sim.py --vad --input WH-CH520 --rounds 20   # 常开麦：自动断句，连续对话

**蓝牙耳机（WH-CH520 这种）要注意的点**（本脚本已经处理）：

1. 蓝牙耳机在 Windows 上通常有两个身份：``WH-CH520 Stereo``（A2DP，音质好但没有麦克风）
   和 ``WH-CH520 Hands-Free AG Audio``（免提/HFP，有麦克风但音质差）。**要说话就得用免提那一个**，
   所以带 ``--mic``/``--vad`` 时脚本会**优先挑免提设备**（录音和放音都走它，避免两个 profile 打架）。
2. 免提设备通常只支持 8/16 kHz 单声道，而我们收到的是 24 kHz 单声道 PCM。
   脚本会**先问设备支持什么格式再决定**，需要时**重采样/补声道**（不会出现"没声音"或"变声"）。
3. 录音会打印**电平（dBFS）**：太低说明麦克风没开或增益太小（Windows 声音设置里调）。
4. 断线会**自动重连**（最多几次），适合长时间挂着对话。

每一轮打印一行实测：VAD / 识别 / 一等回复文字 / 第一段音频，并追加到
``migration_tools/glasses_m0_timings.csv``。

复用桌宠已验证的模块：``listening.py``（silero VAD、断句、写 wav）与
``local_asr.py``（sherpa-onnx Paraformer，纯离线）。音频用 PySide6 的 ``QAudioSource`` /
``QAudioSink``（和 ``voice_player.py`` 同一个思路）。
"""

import argparse
import asyncio
import csv
import json
import math
import statistics
import sys
import time
import wave
from array import array
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import local_asr  # noqa: E402 - 复用桌宠的本机识别
import listening  # noqa: E402 - 复用桌宠的 VAD 与断句

from PySide6.QtCore import QCoreApplication  # noqa: E402
from PySide6.QtMultimedia import (  # noqa: E402
    QAudioFormat,
    QAudioSink,
    QAudioSource,
    QMediaDevices,
)

URL = "ws://127.0.0.1:6200"
CSV_PATH = BASE.parent / "migration_tools" / "glasses_m0_timings.csv"
FFMPEG = Path(r"PROJECT_ROOT\.tools\ffmpeg\bin\ffmpeg.exe")
# 一段回复多久没有新片段就算"说完了"（与桌宠 petlink 的判定一致）
IDLE_DONE_SECONDS = 1.5
# 单轮的兜底上限
TURN_LIMIT_SECONDS = 60.0
# 她说完之后、重新开麦之前等一会儿：把蓝牙延迟与室内余音放过去，免得她听见自己
LISTEN_GUARD_SECONDS = 0.6
# 短于这个长度的一句，先别急着发出去——再等一小会儿看你是不是还有下半句
SHORT_SEGMENT_SECONDS = 2.0
# 等下半句的窗口（拿不到下半句就按原样发出去）
SHORT_MERGE_WAIT_SECONDS = 0.6
# 放音格式的候选顺序：先试"原样"，再试补立体声，再试常见的 48k/44.1k/16k
PLAY_RATE_CANDIDATES = (48000, 44100, 32000, 16000, 8000)

ROUND_LINES = (
    "在吗",
    "今天外面冷不冷",
    "我出门了",
    "随便说点什么",
    "我今天有点累",
    "你在干嘛呢",
    "帮我想个晚饭",
    "晚安",
)


# ------------------------------------------------------------------ 设备与格式


def make_format(rate: int, channels: int) -> QAudioFormat:
    """构造一个 Qt 音频格式（Int16）。

    Args:
        rate: 采样率。
        channels: 声道数。

    Returns:
        QAudioFormat。
    """
    fmt = QAudioFormat()
    fmt.setSampleRate(rate)
    fmt.setChannelCount(channels)
    fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
    return fmt


def list_devices() -> None:
    """打印所有录音/放音设备，以及它们能不能吃我们需要的格式。"""
    print("=== 放音设备 ===")
    default_out = QMediaDevices.defaultAudioOutput()
    for device in QMediaDevices.audioOutputs():
        mark = "（默认）" if device.id() == default_out.id() else ""
        ok16 = device.isFormatSupported(make_format(16000, 1))
        ok24 = device.isFormatSupported(make_format(24000, 1))
        ok48 = device.isFormatSupported(make_format(48000, 2))
        print(
            f"  · {device.description()}{mark}"
            f"　16k单={ok16} 24k单={ok24} 48k双={ok48}",
        )
    print("=== 录音设备 ===")
    default_in = QMediaDevices.defaultAudioInput()
    for device in QMediaDevices.audioInputs():
        mark = "（默认）" if device.id() == default_in.id() else ""
        ok16 = device.isFormatSupported(make_format(16000, 1))
        ok48 = device.isFormatSupported(make_format(48000, 1))
        print(f"  · {device.description()}{mark}　16k单={ok16} 48k单={ok48}")


def _rank(name: str, want: str, hands_free_first: bool) -> int:
    """给候选设备打分（越小越优先）。

    Args:
        name: 设备名。
        want: 用户给的关键词（空 = 用系统默认）。
        hands_free_first: 是否优先"免提/HFP"设备（要开麦时必须优先，否则两个 profile 会打架）。

    Returns:
        分数。
    """
    lowered = name.lower()
    score = 0
    if want and want.lower() in lowered:
        score -= 100  # 命中关键词的最优先
    hands_free = "hands-free" in lowered or "免提" in name or "hfp" in lowered
    if hands_free_first:
        score -= 20 if hands_free else 0
    else:
        score -= 20 if not hands_free else 0
    if "stereo" in lowered:
        score -= 5 if not hands_free_first else 0
    return score


def pick_device(kind: str, want: str, *, hands_free_first: bool):  # noqa: ANN201 - QAudioDevice
    """按名字挑一个录音/放音设备。

    Args:
        kind: ``"input"`` 或 ``"output"``。
        want: 设备名关键词（空串 = 系统默认设备）。
        hands_free_first: 优先"免提/HFP"设备。

    Returns:
        选中的 QAudioDevice。

    Raises:
        SystemExit: 找不到设备时（把候选列出来让人选）。
    """
    devices = QMediaDevices.audioInputs() if kind == "input" else QMediaDevices.audioOutputs()
    default = (
        QMediaDevices.defaultAudioInput()
        if kind == "input"
        else QMediaDevices.defaultAudioOutput()
    )
    if not devices:
        raise SystemExit(f"没有可用的{'录音' if kind == 'input' else '放音'}设备")
    if not want:
        if kind == "input":
            return default
        # 放音：要开麦时优先免提（同一个 profile），否则默认设备
        if hands_free_first:
            hands = [d for d in devices if "hands-free" in d.description().lower()]
            if hands:
                return hands[0]
        return default
    ranked = sorted(devices, key=lambda d: _rank(d.description(), want, hands_free_first))
    best = ranked[0]
    if want.lower() not in best.description().lower():
        names = "\n".join(f"  · {d.description()}" for d in devices)
        raise SystemExit(f"没找到名字里带「{want}」的{'录音' if kind == 'input' else '放音'}设备。\n可选：\n{names}")
    return best


def negotiate_play(device, wanted_rate: int, wanted_channels: int) -> tuple[QAudioFormat, bool]:  # noqa: ANN001
    """问设备支持什么格式，挑一个最接近的（拿不到原样就重采样）。

    Args:
        device: QAudioDevice。
        wanted_rate: 我们手上的采样率。
        wanted_channels: 我们手上的声道数。

    Returns:
        ``(要用的 QAudioFormat, 是否需要处理音频)``。
    """
    if device.isFormatSupported(make_format(wanted_rate, wanted_channels)):
        return make_format(wanted_rate, wanted_channels), False
    candidates = [(wanted_rate, 2 if wanted_channels == 1 else 1)]
    candidates += [(rate, 1) for rate in PLAY_RATE_CANDIDATES]
    candidates += [(rate, 2) for rate in PLAY_RATE_CANDIDATES]
    for rate, channels in candidates:
        fmt = make_format(rate, channels)
        if device.isFormatSupported(fmt):
            return fmt, True
    return make_format(16000, 1), True


def negotiate_capture(device, wanted_rate: int):  # noqa: ANN001, ANN201 - QAudioDevice
    """挑一个能用的录音格式（拿不到 16k 就先录、再降到 16k 给我们用）。

    Args:
        device: QAudioDevice。
        wanted_rate: 我们想要的采样率（16 kHz）。

    Returns:
        ``(要用的 QAudioFormat, 是否需要降到 16k)``。
    """
    if device.isFormatSupported(make_format(wanted_rate, 1)):
        return make_format(wanted_rate, 1), False
    for rate in PLAY_RATE_CANDIDATES:
        fmt = make_format(rate, 1)
        if device.isFormatSupported(fmt):
            return fmt, True
    fmt = make_format(48000, 2)
    if device.isFormatSupported(fmt):
        return fmt, True
    return make_format(wanted_rate, 1), False


def resample_int16(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """线性重采样（16 bit 单声道）。够用：我们只在设备不支持原采样率时才用。

    Args:
        pcm: 输入 PCM。
        src_rate: 输入采样率。
        dst_rate: 目标采样率。

    Returns:
        重采样后的 PCM。
    """
    if src_rate == dst_rate or not pcm:
        return pcm
    usable = len(pcm) // 2 * 2
    source = array("h")
    source.frombytes(pcm[:usable])
    if not len(source):
        return b""
    count = max(1, int(len(source) * dst_rate / src_rate))
    out = array("h", bytes(2 * count))
    ratio = src_rate / dst_rate
    last = len(source) - 1
    for index in range(count):
        pos = index * ratio
        left = int(pos)
        frac = pos - left
        if left >= last:
            out[index] = source[last]
            continue
        first = source[left]
        second = source[left + 1]
        out[index] = int(first + (second - first) * frac)
    return out.tobytes()


def map_channels(pcm: bytes, src_channels: int, dst_channels: int) -> bytes:
    """声道数转换（单↔双）。

    Args:
        pcm: 输入 PCM。
        src_channels: 输入声道数。
        dst_channels: 目标声道数。

    Returns:
        转换后的 PCM。
    """
    if src_channels == dst_channels or not pcm:
        return pcm
    usable = len(pcm) // 2 * 2
    source = array("h")
    source.frombytes(pcm[:usable])
    out = array("h")
    if dst_channels > src_channels:
        for value in source:
            for _ in range(dst_channels // max(1, src_channels)):
                out.append(value)
    else:
        step = max(1, src_channels // dst_channels)
        for index in range(0, len(source) - step + 1, step):
            out.append(source[index])
    return out.tobytes()


def pcm_dbfs(pcm: bytes) -> float:
    """算一段 PCM 的电平（dBFS），用来判断"麦克风到底有没有在录"。

    Args:
        pcm: 16 bit 单声道 PCM。

    Returns:
        dBFS（静音约 -90，正常说话 -30 ~ -12）。
    """
    usable = len(pcm) // 2 * 2
    if not usable:
        return -120.0
    samples = array("h")
    samples.frombytes(pcm[:usable])
    total = 0
    for value in samples:
        total += float(value) * float(value)
    rms = math.sqrt(total / len(samples))
    if rms <= 1e-6:
        return -120.0
    return 20 * math.log10(rms / 32768.0)


# ------------------------------------------------------------------ 音频进出


def read_platform_secret(platform_id: str = "glasses") -> str:
    """从 AstrBot 的配置里读设备通道的密钥（本机用，省得每次手敲）。

    注意：只有"跑 AstrBot 的那台机器"读得到；在别的机器上要用 `--secret 密钥本身`。

    Args:
        platform_id: 平台配置里的 id。

    Returns:
        密钥；读不到就返回空串。
    """
    config_path = Path.home() / ".astrbot" / "data" / "cmd_config.json"
    try:
        data = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001 - 读不到就当没配
        return ""
    for entry in data.get("platform") or []:
        if isinstance(entry, dict) and str(entry.get("id")) == platform_id:
            return str(entry.get("secret") or "")
    return ""


def resolve_secret(value: str) -> str:
    """把 ``--secret auto`` 展开成真正的密钥。

    Args:
        value: 命令行给的值（``auto`` 或密钥本身）。

    Returns:
        真正用来连接的密钥。
    """
    if value.strip().lower() != "auto":
        return value
    secret = read_platform_secret()
    if secret:
        print("[sim] 已从 AstrBot 配置里读到密钥（--secret auto）")
    else:
        print("[sim] 没读到密钥：本机 AstrBot 配置里 glasses 平台的 secret 是空的？")
    return secret


class AudioIO:
    """按名字挑设备、协商格式、必要时重采样；录音与放音都走这里。"""

    def __init__(self, input_name: str = "", output_name: str = "", *, capture: bool) -> None:
        """选好设备并协商格式。

        Args:
            input_name: 录音设备关键词（空 = 默认）。
            output_name: 放音设备关键词（空 + capture = 优先免提）。
            capture: 这一轮会不会开麦（开了就优先免提 profile）。
        """
        self.input = pick_device("input", input_name, hands_free_first=True)
        self.output = pick_device(
            "output", output_name, hands_free_first=bool(capture or input_name),
        )
        self.in_format, self.in_needs_convert = negotiate_capture(
            self.input, listening.SAMPLE_RATE,
        )
        print(
            f"[sim] 录音设备：{self.input.description()}"
            f"（{self.in_format.sampleRate()} Hz / {self.in_format.channelCount()} 声道）",
        )
        print(
            f"[sim] 放音设备：{self.output.description()}",
        )

    def to_mono_16k(self, pcm: bytes) -> bytes:
        """把录到的 PCM 统一成 16 kHz 单声道（VAD 与识别只吃这个）。

        Args:
            pcm: 设备格式的 PCM。

        Returns:
            16 kHz 单声道 PCM。
        """
        if self.in_format.channelCount() > 1:
            pcm = map_channels(pcm, self.in_format.channelCount(), 1)
        if self.in_format.sampleRate() != listening.SAMPLE_RATE:
            pcm = resample_int16(pcm, self.in_format.sampleRate(), listening.SAMPLE_RATE)
        return pcm

    def record_seconds(self, seconds: float) -> bytes:
        """录固定一段（按键说一句）。

        Args:
            seconds: 录多久。

        Returns:
            16 kHz 单声道 PCM。
        """
        source = QAudioSource(self.input, self.in_format)
        device = source.start()
        if device is None:
            source.stop()
            raise RuntimeError(f"打不开录音设备：{self.input.description()}")
        chunks = bytearray()
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            data = device.readAll()
            if data:
                chunks += bytes(data.data())
            time.sleep(0.02)
        source.stop()
        pcm = self.to_mono_16k(bytes(chunks))
        print(f"[sim] 录到 {len(pcm) / 2 / listening.SAMPLE_RATE:.2f} 秒，"
              f"电平 {pcm_dbfs(pcm):.1f} dBFS"
              f"{'（太小了：检查麦克风有没有开/系统里增益）' if pcm_dbfs(pcm) < -45 else ''}")
        return pcm

    def record_until_silence(
        self,
        max_seconds: float = 20.0,
        silence_ms: float = listening.DEFAULT_SILENCE_MS,
        *,
        wait_speech_seconds: float = 0.0,
    ) -> tuple[bytes, float]:
        """常开麦录一句：VAD 判停就返回。

        Args:
            max_seconds: 最长录多久（从开始说话算起）。
            silence_ms: 静音多久算说完。
            wait_speech_seconds: 允许等多久开始说话（0 = 不等，直接按 max_seconds 计）。

        Returns:
            ``(16 kHz 单声道 PCM, 从开麦到判停的秒数)``；没听到人说话就返回 ``(b"", 用时)``。
        """
        vad = listening.load_vad(silence_ms)
        watcher = listening.SegmentWatcher(vad)
        source = QAudioSource(self.input, self.in_format)
        device = source.start()
        if device is None:
            source.stop()
            raise RuntimeError(f"打不开录音设备：{self.input.description()}")
        started = time.monotonic()
        hard_deadline = started + wait_speech_seconds + max_seconds
        speech_started: float | None = None
        pending = bytearray()
        frame_bytes = listening.WINDOW * 2 * self.in_format.channelCount()
        try:
            while time.monotonic() < hard_deadline:
                data = device.readAll()
                if data:
                    pending += bytes(data.data())
                    while len(pending) >= frame_bytes:
                        frame = bytes(pending[:frame_bytes])
                        del pending[:frame_bytes]
                        mono = self.to_mono_16k(frame)
                        if speech_started is None and pcm_dbfs(mono) > -45:
                            # 用音量判"开始说话了"：比读 VAD 内部状态可靠（各版本接口不一样）
                            speech_started = time.monotonic()
                            print("[sim] 听到你说话了……", flush=True)
                        segments = watcher.feed(listening.pcm_to_float(mono))
                        if segments:
                            pcm = _samples_to_pcm(segments[0])
                            used = time.monotonic() - (speech_started or started)
                            print(f"[sim] 录到 {len(pcm) / 2 / listening.SAMPLE_RATE:.2f} 秒，"
                                  f"电平 {pcm_dbfs(pcm):.1f} dBFS", flush=True)
                            return pcm, used
                time.sleep(0.02)
        finally:
            source.stop()
        print("[sim] 这段时间没听到人说话（电平提示见上面）")
        return b"", time.monotonic() - started

    def open_capture(self):  # noqa: ANN201 - (QAudioSource, QIODevice)
        """打开麦克风（"按住说话"那种需要自己控制起止的场合用）。

        Returns:
            ``(source, io)``：记得用 :meth:`close_capture` 关掉。

        Raises:
            RuntimeError: 设备打不开（被别的程序独占等）。
        """
        source = QAudioSource(self.input, self.in_format)
        device = source.start()
        if device is None:
            source.stop()
            raise RuntimeError(f"打不开录音设备：{self.input.description()}")
        return source, device

    @staticmethod
    def close_capture(source) -> None:  # noqa: ANN001 - QAudioSource
        """关掉麦克风。

        Args:
            source: :meth:`open_capture` 返回的 source。
        """
        try:
            source.stop()
        except Exception:  # noqa: BLE001 - 关不掉就算了
            pass

    async def record_seconds_async(self, seconds: float) -> bytes:
        """录固定一段，但**不阻塞事件循环**（对话时 WebSocket 的心跳还要跑）。

        为什么必须有这个版本：以前在协程里直接跑阻塞录音，事件循环被卡住几十秒，
        服务端的心跳（ping）等不到回应，就把连接踢了——实测表现是
        ``ConnectionClosedError: no close frame received or sent``，
        而且她刚生成好的那句话也丢了（2026-09-29 踩到）。

        Args:
            seconds: 录多久。

        Returns:
            16 kHz 单声道 PCM。
        """
        source = QAudioSource(self.input, self.in_format)
        device = source.start()
        if device is None:
            source.stop()
            raise RuntimeError(f"打不开录音设备：{self.input.description()}")
        chunks = bytearray()
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            data = device.readAll()
            if data:
                chunks += bytes(data.data())
            await asyncio.sleep(0.02)
        source.stop()
        pcm = self.to_mono_16k(bytes(chunks))
        print(f"[sim] 录到 {len(pcm) / 2 / listening.SAMPLE_RATE:.2f} 秒，"
              f"电平 {pcm_dbfs(pcm):.1f} dBFS", flush=True)
        return pcm

    async def record_until_silence_async(
        self,
        max_seconds: float = 20.0,
        silence_ms: float = listening.DEFAULT_SILENCE_MS,
        *,
        wait_speech_seconds: float = 0.0,
        min_level: float | None = None,
        on_level=None,  # noqa: ANN001 - 回调，GUI 拿它刷电平条
        on_speech=None,  # noqa: ANN001 - 回调，GUI 显示"听到你说话了"
    ) -> tuple[bytes, float]:
        """常开麦录一句（异步版，理由同上面），VAD 判停就返回。

        Args:
            max_seconds: 最长录多久。
            silence_ms: 静音多久算说完。
            wait_speech_seconds: 允许等多久开始说话。
            min_level: 判定"这段太轻，丢掉"的 RMS 门限（默认 0.004 ≈ −48 dBFS）。
                屋里还有别人说话时，把它调高一点能少收别人的声音。
            on_level: 每帧电平回调，参数是 dBFS（给界面用的）。
            on_speech: 第一次检测到人声时回调一次。

        Returns:
            ``(16 kHz 单声道 PCM, 从开麦到判停的秒数)``。
        """
        vad = listening.load_vad(silence_ms)
        watcher = listening.SegmentWatcher(
            vad, min_level=0.004 if min_level is None else min_level,
        )
        onset_db = -45.0 if min_level is None else 20 * math.log10(max(1e-6, min_level))
        source, device = self.open_capture()
        started = time.monotonic()
        hard_deadline = started + wait_speech_seconds + max_seconds
        speech_started: float | None = None
        pending = bytearray()
        frame_bytes = listening.WINDOW * 2 * self.in_format.channelCount()
        try:
            while time.monotonic() < hard_deadline:
                data = device.readAll()
                if data:
                    pending += bytes(data.data())
                    while len(pending) >= frame_bytes:
                        frame = bytes(pending[:frame_bytes])
                        del pending[:frame_bytes]
                        mono = self.to_mono_16k(frame)
                        db = pcm_dbfs(mono)
                        if on_level is not None:
                            on_level(db)
                        if speech_started is None and db > onset_db:
                            speech_started = time.monotonic()
                            print("[sim] 听到你说话了……", flush=True)
                            if on_speech is not None:
                                on_speech()
                        segments = watcher.feed(listening.pcm_to_float(mono))
                        if segments:
                            pcm = _samples_to_pcm(segments[0])
                            used = time.monotonic() - (speech_started or started)
                            print(f"[sim] 录到 {len(pcm) / 2 / listening.SAMPLE_RATE:.2f} 秒，"
                                  f"电平 {pcm_dbfs(pcm):.1f} dBFS", flush=True)
                            return pcm, used
                        # 太轻的一段被 watcher 丢掉了：当成"还没开始说"
                        if watcher.dropped and speech_started is not None:
                            speech_started = None
                await asyncio.sleep(0.02)
        finally:
            source.stop()
        print("[sim] 这段时间没听到人说话", flush=True)
        return b"", time.monotonic() - started

    async def play_pcm_async(self, pcm: bytes, rate: int, channels: int) -> float:
        """异步放音（边写边让出事件循环）。

        Args:
            pcm: 裸 PCM。
            rate: 采样率。
            channels: 声道数。

        Returns:
            播放耗时（秒）。
        """
        fmt, needs_convert = negotiate_play(self.output, rate, channels)
        data = pcm
        if needs_convert:
            data = map_channels(data, channels, fmt.channelCount())
            data = resample_int16(data, rate, fmt.sampleRate())
        sink = QAudioSink(self.output, fmt)
        io = sink.start()
        if io is None:
            sink.stop()
            print(f"[sim] 放音失败：{self.output.description()} 打不开", flush=True)
            return 0.0
        started = time.monotonic()
        view = memoryview(data)
        offset = 0
        while offset < len(view) and time.monotonic() - started < 60:
            free = sink.bytesFree()
            if free <= 0:
                await asyncio.sleep(0.02)
                continue
            piece = view[offset : offset + free]
            io.write(bytes(piece))
            offset += len(piece)
        expected_us = int(
            len(data) / max(1, fmt.sampleRate() * fmt.channelCount() * 2) * 1_000_000,
        )
        while time.monotonic() - started < 120:
            state_name = getattr(sink.state(), "name", "")
            played_us = getattr(sink, "processedUSecs", lambda: 0)()
            if state_name == "IdleState" or (expected_us and played_us >= expected_us - 100_000):
                break
            await asyncio.sleep(0.02)
        sink.stop()
        return time.monotonic() - started

    def play_pcm(self, pcm: bytes, rate: int, channels: int) -> float:
        """把 PCM 放出来（阻塞到放完），返回播放耗时。

        Args:
            pcm: 裸 PCM。
            rate: 采样率。
            channels: 声道数。

        Returns:
            播放耗时（秒）。
        """
        fmt, needs_convert = negotiate_play(self.output, rate, channels)
        data = pcm
        if needs_convert:
            data = map_channels(data, channels, fmt.channelCount())
            data = resample_int16(data, rate, fmt.sampleRate())
        sink = QAudioSink(self.output, fmt)
        io = sink.start()
        if io is None:
            sink.stop()
            print(f"[sim] 放音失败：{self.output.description()} 打不开")
            return 0.0
        started = time.monotonic()
        view = memoryview(data)
        offset = 0
        # 写数据：`bytesFree()` 在 **sink** 上（不是 start() 返回的设备），
        # 这是桌宠 cloudvoice 踩出来的用法；写不完的部分留着下一轮写。
        while offset < len(view) and time.monotonic() - started < 60:
            free = sink.bytesFree()
            if free <= 0:
                time.sleep(0.02)
                continue
            piece = view[offset : offset + free]
            io.write(bytes(piece))
            offset += len(piece)
        # 等放完：不能只看 IdleState（蓝牙/免提设备有时不报），再用 processedUSecs 对账
        expected_us = int(
            len(data) / max(1, fmt.sampleRate() * fmt.channelCount() * 2) * 1_000_000,
        )
        while time.monotonic() - started < 120:
            state_name = getattr(sink.state(), "name", "")
            played_us = getattr(sink, "processedUSecs", lambda: 0)()
            if state_name == "IdleState" or (expected_us and played_us >= expected_us - 100_000):
                break
            time.sleep(0.02)
        sink.stop()
        return time.monotonic() - started


def _samples_to_pcm(samples) -> bytes:  # noqa: ANN001 - float 列表
    """float 样本 → 16 bit PCM。

    Args:
        samples: -1..1 的浮点样本。

    Returns:
        PCM 字节。
    """
    out = array("h")
    for value in samples:
        clipped = max(-1.0, min(1.0, float(value)))
        out.append(int(clipped * 32767))
    return out.tobytes()


def read_wav_pcm(path: Path) -> tuple[bytes, int, Path]:
    """读一个 wav 的 PCM 与采样率（不是 16 kHz 单声道就用本机 ffmpeg 转一下）。

    Args:
        path: wav 路径。

    Returns:
        ``(PCM 字节, 采样率, 可用来识别的 16k 文件路径)``。

    Raises:
        FileNotFoundError: 文件不存在。
        RuntimeError: 转换失败或格式仍不对。
    """
    if not path.is_file():
        raise FileNotFoundError(path)
    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        pcm = handle.readframes(handle.getnframes())
    if (rate, channels, width) == (listening.SAMPLE_RATE, 1, 2):
        return pcm, rate, path
    if not FFMPEG.is_file():
        raise RuntimeError(f"{path.name} 不是 16 kHz/单声道/16 bit，而且找不到 ffmpeg 转换")
    target = path.with_name(path.stem + "_16k.wav")
    import subprocess

    subprocess.run(
        [str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
         "-ar", str(listening.SAMPLE_RATE), "-ac", "1", "-c:a", "pcm_s16le", str(target)],
        check=True,
    )
    with wave.open(str(target), "rb") as handle:
        return handle.readframes(handle.getnframes()), handle.getframerate(), target


# ------------------------------------------------------------------ 对话循环


class DeviceSim:
    """假装自己是那台随身设备（或者就是"这台电脑 + 耳机"）。"""

    def __init__(
        self,
        url: str = URL,
        secret: str = "",
        *,
        audio: AudioIO | None = None,
        no_play: bool = False,
    ) -> None:
        """记住连接与音频参数。

        Args:
            url: 通道地址。
            secret: 共享密钥（本机可留空）。
            audio: 已选好设备的音频对象（只跑文本链路时可以为 None）。
            no_play: True = 收到音频不播（只计时）。
        """
        self.url = url
        self.secret = secret
        self.audio = audio
        self.no_play = no_play
        self.rows: list[dict] = []

    async def connect(self):  # noqa: ANN202 - websockets client
        """连上并完成认证握手（带重试）。

        Returns:
            websockets 连接。

        Raises:
            RuntimeError: 连不上。
        """
        import websockets

        last: Exception | None = None
        for attempt in range(5):
            try:
                ws = await websockets.connect(self.url, max_size=None)
                await ws.send(json.dumps({"type": "auth", "secret": self.secret}))
                hello = json.loads(await asyncio.wait_for(ws.recv(), 5))
                print(
                    f"[sim] 已连上 {self.url}"
                    f"（hello: {hello.get('user_name')}/{hello.get('user_id')}）",
                )
                return ws
            except Exception as exc:  # noqa: BLE001 - 重试
                last = exc
                await asyncio.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"连不上 {self.url}：{last}")

    async def one_turn(  # noqa: ANN001 - ws / 回调
        self,
        ws,
        text: str,
        *,
        kind: str = "text",
        on_event=None,
        on_audio=None,
        images: list[str] | None = None,
    ) -> dict:
        """发一句话，收到她的文字与音频，返回这一轮的实测数据。

        Args:
            ws: websocket 连接。
            text: 要说的文字。
            kind: 这一轮怎么来的（text/mic/vad/file），只用于记录。
            on_event: 可选回调 ``on_event(事件名, 内容)``：
                ``reply``（她的文字片段）、``audio_begin``（开始出声）、``done``（这轮结束）。
                界面用它做实时显示。
            on_audio: 可选回调 ``on_audio(pcm_chunk)``：**每收到一段音频就回调一次**，
                用来做"边说边转发/边播"（手机中转就靠它）。
            images: 要一起发过去的本地图片路径（给她"看"）。

        Returns:
            本轮的时间与音频统计。
        """
        sent_at = time.monotonic()
        payload: dict = {"type": "message", "text": text}
        if images:
            payload["images"] = list(images)
        await ws.send(json.dumps(payload, ensure_ascii=False))
        if on_event is not None:
            on_event("sent", text)
        first_text: float | None = None
        first_audio: float | None = None
        audio_end: float | None = None
        pcm = bytearray()
        rate = 24000
        channels = 1
        reply = ""
        while time.monotonic() - sent_at < TURN_LIMIT_SECONDS:
            try:
                raw = await asyncio.wait_for(ws.recv(), IDLE_DONE_SECONDS)
            except (TimeoutError, asyncio.TimeoutError):
                if first_audio is not None or first_text is not None:
                    break  # 一段时间没有新片段 = 这轮说完了
                continue
            now = time.monotonic()
            if isinstance(raw, (bytes, bytearray)):
                if on_audio is not None:
                    on_audio(bytes(raw))
                if first_audio is None:
                    first_audio = now - sent_at
                    if on_event is not None:
                        on_event("audio_begin", None)
                pcm += bytes(raw)
                continue
            frame = json.loads(raw)
            kind_name = frame.get("type")
            if kind_name == "reply":
                if first_text is None:
                    first_text = now - sent_at
                chunk = str(frame.get("text") or "")
                reply += chunk
                if on_event is not None and chunk:
                    on_event("reply", chunk)
            elif kind_name == "audio_begin":
                if first_audio is None:
                    first_audio = now - sent_at
                    if on_event is not None:
                        on_event("audio_begin", None)
                rate = int(frame.get("sample_rate") or 24000)
                channels = int(frame.get("channels") or 1)
            elif kind_name == "audio_end":
                audio_end = now - sent_at
                break
        played = 0.0
        audio_secs = len(pcm) / (rate * channels * 2) if pcm else 0.0
        if pcm and not self.no_play and self.audio is not None:
            played = await self.audio.play_pcm_async(bytes(pcm), rate, channels)
        row = {
            "kind": kind,
            "text": text,
            "reply": reply[:60],
            "first_text_ms": round((first_text or -1) * 1000),
            "first_audio_ms": round((first_audio or -1) * 1000),
            "audio_end_ms": round((audio_end or -1) * 1000),
            "audio_secs": round(audio_secs, 2),
            "audio_bytes": len(pcm),
            "played_secs": round(played, 2),
        }
        print(
            f"[sim] 「{text[:20]}」→ 第一段音频 {row['first_audio_ms']} ms｜"
            f"音频 {row['audio_secs']} s（{row['audio_bytes']} 字节）"
            f"｜播了 {row['played_secs']} s",
            flush=True,
        )
        if on_event is not None:
            on_event("done", row)
        return row

    def save(self) -> None:
        """把这一轮记录追加到 CSV。"""
        if not self.rows:
            return
        CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
        exists = CSV_PATH.exists()
        with CSV_PATH.open("a", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(self.rows[0].keys()))
            if not exists:
                writer.writeheader()
            writer.writerows(self.rows)
        print(f"[sim] {len(self.rows)} 轮已追加到 {CSV_PATH}")

    def summary(self) -> None:
        """打印分段统计（中位数/最小/最大）。"""
        audios = [r["first_audio_ms"] for r in self.rows if r["first_audio_ms"] >= 0]
        asrs = [r["asr_ms"] for r in self.rows if r.get("asr_ms") is not None]
        if asrs:
            print(f"[sim] 识别（本机）：中位 {statistics.median(asrs):.0f} ms"
                  f"（{min(asrs)}–{max(asrs)}）")
        if audios:
            print(f"[sim] 她开口：中位 {statistics.median(audios):.0f} ms"
                  f"（{min(audios)}–{max(audios)}）")


def transcribe(clip: Path, pcm: bytes, *, keep: bool) -> tuple[str, int]:
    """把一段 16k PCM 写成 wav 交给本机识别（默认识别完就删）。

    Args:
        clip: 写到哪里。
        pcm: 16 kHz 单声道 PCM。
        keep: 是否保留录音文件。

    Returns:
        ``(文字, 识别耗时毫秒)``。
    """
    clip.parent.mkdir(parents=True, exist_ok=True)
    samples = listening.pcm_to_float(pcm)
    if not listening.write_wav(samples, clip):
        raise RuntimeError("写 wav 失败")
    started = time.monotonic()
    text = local_asr.transcribe_file(str(clip))
    cost = round((time.monotonic() - started) * 1000)
    if not keep:
        clip.unlink(missing_ok=True)
    return text, cost


async def run(args) -> int:  # noqa: ANN001 - argparse
    """跑完用户选的模式。

    Args:
        args: 命令行参数。

    Returns:
        进程退出码。
    """
    needs_capture = bool(args.mic or args.vad or args.file)
    needs_play = not args.no_play
    audio = None
    if needs_capture or needs_play or args.input or args.output:
        audio = AudioIO(args.input, args.output, capture=needs_capture or bool(args.input))
    sim = DeviceSim(args.url, args.secret, audio=audio, no_play=args.no_play)

    async def send_turn(current_ws, text: str, *, kind: str):  # noqa: ANN001, ANN202
        """发一轮；断线就重连再试一次（长时间挂着的场景很常见）。

        Args:
            current_ws: 当前连接。
            text: 要说的文字。
            kind: 这一轮怎么来的（记录用）。

        Returns:
            ``(连上的 ws, 本轮实测数据)``。
        """
        try:
            return current_ws, await sim.one_turn(current_ws, text, kind=kind)
        except Exception as exc:  # noqa: BLE001 - 断线/超时都重连一次
            print(f"[sim] 发送时断线了（{type(exc).__name__}: {exc}），重连后重试这一句")
            new_ws = await sim.connect()
            return new_ws, await sim.one_turn(new_ws, text, kind=kind)

    try:
        ws = await sim.connect()
    except RuntimeError as exc:
        print(f"[sim] {exc}")
        return 2
    try:
        if args.text and not needs_capture:
            _ws, row = await send_turn(ws, args.text, kind="text")
            sim.rows.append(row)
        elif args.file:
            pcm, rate, usable = read_wav_pcm(Path(args.file))
            text, asr_ms = transcribe(
                BASE / "clips" / f"sim_{int(time.time())}.wav", pcm, keep=args.keep_clip,
            )
            print(f"[sim] 识别：{text}（{asr_ms} ms，音频 {len(pcm) / (rate * 2):.2f} 秒）")
            ws, row = await send_turn(ws, text, kind="file")
            row["asr_ms"] = asr_ms
            sim.rows.append(row)
        elif args.mic:
            for index in range(args.rounds):
                if args.mic_prompt:
                    input(f"[sim] 第 {index + 1}/{args.rounds} 轮：按回车开始录 {args.seconds} 秒…")
                else:
                    print(f"[sim] 第 {index + 1}/{args.rounds} 轮：现在开始录 {args.seconds} 秒，请说话")
                pcm = await audio.record_seconds_async(args.seconds)
                if not pcm:
                    print("[sim] 这一轮没拿到音频，跳过")
                    continue
                level = pcm_dbfs(pcm)
                text, asr_ms = transcribe(
                    BASE / "clips" / f"sim_{int(time.time())}.wav",
                    pcm,
                    keep=args.keep_clip,
                )
                print(f"[sim] 识别：{text}（{asr_ms} ms，电平 {level:.1f} dBFS）")
                if not text.strip():
                    print("[sim] 识别结果为空，跳过这一轮")
                    continue
                ws, row = await send_turn(ws, text, kind="mic")
                row["asr_ms"] = asr_ms
                row["level_dbfs"] = round(level, 1)
                sim.rows.append(row)
        else:
            # 没听清 / 识别为空就重新听（最多让 3 次），免得噪声把额度吃光。
            done = 0
            misses = 0
            while done < args.rounds and misses < 3:
                await asyncio.sleep(LISTEN_GUARD_SECONDS)  # 等余音与蓝牙延迟过去再开麦
                pcm, _waited = await audio.record_until_silence_async(
                    wait_speech_seconds=(
                        args.wait_speech if done == 0 and misses == 0 else 0.0
                    ),
                    silence_ms=args.silence_ms,
                )
                if not pcm:
                    misses += 1
                    print(f"[sim] 这一轮没拿到音频（第 {misses} 次），继续听")
                    continue
                # 句子中间的停顿会被 VAD 当成"说完了"。所以**短句再等一小会儿**：
                # 如果紧接着又听到话，就把两段拼起来当成一句重新识别（桌宠也是这么干的，
                # 2026-09-29 实测第 3 句就被切成了半句）。
                if len(pcm) / 2 / listening.SAMPLE_RATE < SHORT_SEGMENT_SECONDS:
                    for _ in range(2):
                        more, _ = await audio.record_until_silence_async(
                            max_seconds=SHORT_MERGE_WAIT_SECONDS,
                            silence_ms=args.silence_ms,
                            wait_speech_seconds=SHORT_MERGE_WAIT_SECONDS,
                        )
                        if not more:
                            break
                        pcm += more
                        print("[sim] 你接着说了，拼成一句重新识别")
                        if len(pcm) / 2 / listening.SAMPLE_RATE >= SHORT_SEGMENT_SECONDS:
                            break
                level = pcm_dbfs(pcm)
                text, asr_ms = transcribe(
                    BASE / "clips" / f"sim_{int(time.time())}.wav",
                    pcm,
                    keep=args.keep_clip,
                )
                print(f"[sim] 识别：{text}（{asr_ms} ms，电平 {level:.1f} dBFS）")
                if not text.strip():
                    misses += 1
                    print(f"[sim] 识别结果为空（第 {misses} 次），继续听")
                    continue
                ws, row = await send_turn(ws, text, kind="vad")
                row["asr_ms"] = asr_ms
                row["level_dbfs"] = round(level, 1)
                sim.rows.append(row)
                done += 1
    except Exception as exc:  # noqa: BLE001 - 出错也要把已跑的数据存下来
        print(f"[sim] 出错：{type(exc).__name__}: {exc}")
    finally:
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass
    sim.save()
    sim.summary()
    return 0


def main() -> int:
    """解析参数并开跑。

    Returns:
        进程退出码。
    """
    parser = argparse.ArgumentParser(description="轻语随身设备模拟器 / 蓝牙耳机对话")
    parser.add_argument("--url", default=URL, help="通道地址")
    parser.add_argument("--secret", default="", help="共享密钥；填 auto = 从本机 AstrBot 配置里读")
    parser.add_argument("--list-devices", action="store_true", help="列出录音/放音设备后退出")
    parser.add_argument("--input", default="", help="录音设备名关键词（如 WH-CH520）")
    parser.add_argument("--output", default="", help="放音设备名关键词（如 WH-CH520）")
    parser.add_argument("--text", default="", help="只发这一句（冒烟测试）")
    parser.add_argument("--file", default="", help="拿现成 wav 走一遍识别 + 对话")
    parser.add_argument("--mic", action="store_true", help="录固定时长（按键模式）")
    parser.add_argument("--vad", action="store_true", help="常开麦：VAD 自动断句")
    parser.add_argument("--seconds", type=float, default=5.0, help="--mic 每轮录多久")
    parser.add_argument("--silence-ms", type=float, default=listening.DEFAULT_SILENCE_MS,
                        help="静音多久算说完（默认 600）")
    parser.add_argument("--wait-speech", type=float, default=45.0,
                        help="--vad 第一轮等多久开始说话（默认 45 秒）")
    parser.add_argument("--rounds", type=int, default=1, help="跑几轮")
    parser.add_argument("--texts", nargs="*", default=[], help="用这几句轮着发（不碰麦克风）")
    parser.add_argument("--no-play", action="store_true", help="收到音频不播出声（只记时延）")
    parser.add_argument("--no-mic-prompt", dest="mic_prompt", action="store_false",
                        help="--mic 不按回车，直接录（方便无人值守）")
    parser.add_argument("--keep-clip", action="store_true", help="保留录音文件（默认识别完就删）")
    args = parser.parse_args()
    args.secret = resolve_secret(args.secret)

    app = QCoreApplication.instance() or QCoreApplication(sys.argv)
    if args.list_devices:
        list_devices()
        return 0
    if not args.mic and not args.vad:
        print("[sim] 提示：不加 --mic/--vad 就不会用麦克风（只测文本链路）")
    if (args.mic or args.vad) and (not local_asr.is_ready()):
        print(f"[sim] 本机识别模型不可用：{local_asr.describe()}")
        return 2
    code = asyncio.run(run(args))
    del app
    return code


if __name__ == "__main__":
    raise SystemExit(main())
