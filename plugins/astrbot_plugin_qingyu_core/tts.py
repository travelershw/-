"""QQ 端语音合成：豆包 2.0 双向流式（和桌宠同一个音色、同一套协议）。

**为什么 QQ 端自己实现一份**，而不是直接用 AstrBot 的 TTS 供应商体系：

1. **音色要一致**：桌宠那边用的是主人自己复刻的音色（`ICL_...`），AstrBot 自带的
   `volcengine_tts` 走的是**旧版 v1 接口**（appid + token + cluster），而主人的订阅是
   **2.0**（v3 接口），v1 那条路实测只回 `grant not found`；
2. **不依赖 ffmpeg**：AstrBot 的 edge_tts 那条要把 mp3 转 wav，本机没装系统 ffmpeg；
   而豆包 2.0 可以**直接吐 mp3**，NapCat 自带的 ffmpeg 插件（`native/ffmpeg/*.node`）
   会自己转成 QQ 语音；
3. 协议已经在桌宠那边实测通了，这里只是换成 asyncio 版。

协议要点（和 `pet_desktop/cloudvoice.py` 同源）：
请求头 `X-Api-Key` / `X-Api-Resource-Id`（`seed-tts-2.0`）/ `X-Api-Connect-Id` /
`X-Control-Require-Usage-Tokens-Return: *`（让服务端回报计费字数）；
帧 = 4 字节头 +（事件 int32 + 会话 id）+ 长度前缀 JSON 负载。

配置放 ``plugin_data/qingyu_tts.json``（**Key 不进代码库**）：
``{"api_key": "...", "resource_id": "seed-tts-2.0", "voice": "...", "format": "mp3", ...}``
"""

import asyncio
import json
import time
import uuid
from pathlib import Path

from astrbot.core import logger
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

URL = "wss://openspeech.bytedance.com/api/v3/tts/bidirection"
CONFIG_PATH = Path(get_astrbot_plugin_data_path()) / "qingyu_tts.json"
CACHE_DIR = Path(get_astrbot_plugin_data_path()) / "tts_cache"
# 合成出来的音频留几份（发出去要等管道读完，不能马上删）
MAX_CACHE_FILES = 20
# QQ 语音别太长：超了就截（并告诉主人截了）
MAX_CHARS = 200
# 一次请求最多合成多少字：超过就按标点切成多段、逐段合成（**不是**丢掉）。
# 2026-10-04 实测：她回了一长串餐厅清单（约 250 字），流式那条路没做任何长度处理，
# 整段请求被云端拒掉 → 一个字的声音都没有（用户报"长回答没有声音"）。
SPEECH_CHUNK_CHARS = 200
# 一条回复最多念多少字（再长就截断，免得念两分钟）
SPEECH_TOTAL_CHARS = 600
CACHE_SECONDS = 5.0
CONNECT_TIMEOUT = 20
READ_TIMEOUT = 60

FULL_CLIENT = 0b0001
ERROR_MSG = 0b1111
WITH_EVENT = 0b0100
JSON_SER = 0b0001

EV_START_CONNECTION = 1
EV_FINISH_CONNECTION = 2
EV_CONNECTION_STARTED = 50
EV_CONNECTION_FAILED = 51
EV_START_SESSION = 100
EV_FINISH_SESSION = 102
EV_SESSION_FINISHED = 152
EV_SESSION_FAILED = 153
EV_TASK_REQUEST = 200
EV_TTS_RESPONSE = 352

DEFAULTS: dict = {
    "api_key": "",
    "resource_id": "seed-tts-2.0",
    "voice": "zh_female_vv_uranus_bigtts",
    # **必须是 wav**（2026-09-27 实测踩坑）：
    # AstrBot 的 `Record.convert_to_base64()` 发送前会统一转成 **wav**，
    # 而 `media_utils._convert_audio_file()` 只在"扩展名与魔数都对得上"时才跳过转码——
    # 给 mp3 就会去调系统 ffmpeg（本机没装）→ 报 `ffmpeg not found`，语音发不出去。
    # 所以这里向服务端要 pcm，自己封一个标准 WAV（RIFF+WAVE）交给 AstrBot，
    # 它直接放行；到了 NapCat 那边再由它**自带**的 ffmpeg 插件转成 QQ 语音。
    "format": "wav",
    "sample_rate": 24000,
    "speech_rate": 0,
    "loudness_rate": 0,
}
# 我们要的文件格式 → 向服务端要的音频格式（wav 由 pcm 自己封容器）
REQUEST_FORMAT = {"wav": "pcm", "pcm": "pcm", "mp3": "mp3", "ogg_opus": "ogg_opus"}

_config_cache: dict | None = None
_config_at = 0.0


class TTSError(RuntimeError):
    """语音合成失败（消息里带给人看的原因）。"""


def config() -> dict:
    """Read the TTS settings (cached for a few seconds).

    Returns:
        The merged settings; missing file means "not configured yet".
    """
    global _config_cache, _config_at
    now = time.time()
    if _config_cache is None or now - _config_at >= CACHE_SECONDS:
        values = dict(DEFAULTS)
        try:
            # utf-8-sig：这个文件是要给人手工改的（里面放 Key）。用记事本或
            # PowerShell 的 `Set-Content -Encoding utf8` 存过就会带 BOM，
            # 普通 utf-8 读会直接抛 JSONDecodeError（2026-09-27 实测踩到）。
            stored = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
            if isinstance(stored, dict):
                values.update({k: v for k, v in stored.items() if k in DEFAULTS})
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            logger.warning(f"qingyu_core: 语音配置读不了（{type(exc).__name__}），用默认值")
        _config_cache, _config_at = values, now
    return dict(_config_cache)


def save(patch: dict) -> str:
    """Write part of the TTS settings.

    Args:
        patch: Keys to update.

    Returns:
        A chat-ready line.
    """
    global _config_cache, _config_at
    values = config()
    values.update({k: v for k, v in patch.items() if k in DEFAULTS})
    CONFIG_PATH.write_text(
        json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _config_cache, _config_at = values, time.time()
    logger.info(f"qingyu_core: 更新语音配置（音色 {values['voice']}）")
    return "语音配置已更新。"


def available() -> bool:
    """Can we speak on QQ right now?

    Returns:
        True when an API key is configured.
    """
    return bool(str(config().get("api_key") or "").strip())


def describe() -> str:
    """One line about the current voice settings.

    Returns:
        Chinese description.
    """
    values = config()
    if not values["api_key"]:
        return f"还没配 Key（{CONFIG_PATH}）"
    return f"{values['voice']}（{values['resource_id']}，{values['format']}）"


def api_format(wanted: str) -> str:
    """Which audio format to ask the service for.

    Args:
        wanted: The file format we want to end up with.

    Returns:
        The format to request (``wav`` is built from ``pcm`` locally).
    """
    return REQUEST_FORMAT.get(str(wanted), "pcm")


def prepare_text(raw: str) -> tuple[str, str]:
    """Clean a line for speaking and cap its length.

    Args:
        raw: Text the caller wants spoken.

    Returns:
        ``(text, note)`` — the note is empty unless something was trimmed.
    """
    text = " ".join(str(raw or "").split())
    if len(text) <= MAX_CHARS:
        return text, ""
    return text[:MAX_CHARS], f"太长了，我只念前 {MAX_CHARS} 字"


def split_for_speech(text: str, limit: int = SPEECH_CHUNK_CHARS) -> list[str]:
    """把一段话按标点切成若干小段,每段单独发一次合成请求。

    为什么必须切:云端合成的**单次请求有长度限制**,一段 250 字的回复会被整段拒掉,
    结果是一个字的声音都没有(2026-10-04 实测:"长回答没有声音")。
    切成小段之后长回复会被**完整念出来**,而不是失败或者只剩前 200 字。

    Args:
        text: 想说的话(可以带换行/列表符号)。
        limit: 每段最多多少字。

    Returns:
        段落列表;空输入返回空列表。相邻的短片段会尽量合并,免得一顿一顿的。
    """
    cleaned = " ".join(str(text or "").split())
    if not cleaned:
        return []
    pieces: list[str] = []
    current = ""
    for char in cleaned:
        current += char
        # 句末标点、列表符号后断开;单段过长也硬断(避免一整句没有标点)
        if char in "。！？!?；;，" or len(current) >= limit:
            pieces.append(current)
            current = ""
    if current:
        pieces.append(current)
    merged: list[str] = []
    for piece in pieces:
        if merged and len(merged[-1]) + len(piece) <= limit:
            merged[-1] += piece
        else:
            merged.append(piece)
    # 总长兜底:一条回复最多念 SPEECH_TOTAL_CHARS 字(再长要念两分钟,不如只念开头)
    capped: list[str] = []
    total = 0
    for piece in merged:
        if total >= SPEECH_TOTAL_CHARS:
            break
        room = SPEECH_TOTAL_CHARS - total
        capped.append(piece[:room])
        total += len(piece)
    return [piece for piece in capped if piece]


def wrap_wav(pcm: bytes, sample_rate: int, channels: int = 1, bits: int = 16) -> bytes:
    """Wrap raw PCM into a canonical WAV container.

    为什么要自己封：AstrBot 发语音前会把它转成 wav，而"跳过转码"的条件是
    **扩展名和魔数都对得上**（`RIFF` + `WAVE`）。自己封 44 字节头最省事、也最可靠。

    Args:
        pcm: Raw little-endian PCM samples.
        sample_rate: Sample rate in Hz.
        channels: Channel count.
        bits: Bits per sample.

    Returns:
        The complete WAV file bytes.
    """
    import struct  # noqa: PLC0415 - 只有这里需要

    byte_rate = sample_rate * channels * bits // 8
    block_align = channels * bits // 8
    header = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
    header += b"fmt " + struct.pack(
        "<IHHIIHH",
        16,  # fmt 块长度
        1,  # PCM
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bits,
    )
    header += b"data" + struct.pack("<I", len(pcm))
    return header + pcm


def _header_bytes(message_type: int, flags: int = WITH_EVENT) -> bytes:
    """Build the 4-byte protocol header.

    Args:
        message_type: Message type nibble.
        flags: Message-type specific flags nibble.

    Returns:
        Four bytes.
    """
    return bytes([(0b0001 << 4) | 0b0001, (message_type << 4) | flags, JSON_SER << 4, 0])


def _optional_bytes(event: int, session: str | None = None) -> bytes:
    """Encode the optional section (event + session id).

    Args:
        event: Event number.
        session: Session id when the event carries one.

    Returns:
        The encoded bytes.
    """
    out = bytearray(event.to_bytes(4, "big", signed=True))
    if session is not None:
        raw = session.encode()
        out.extend(len(raw).to_bytes(4, "big", signed=True))
        out.extend(raw)
    return bytes(out)


def _frame(event: int, body: dict | None = None, session: str | None = None) -> bytes:
    """Build one client frame.

    Args:
        event: Event number.
        body: JSON body (``None`` sends ``{}``).
        session: Session id for events that need one.

    Returns:
        The frame bytes.
    """
    raw = json.dumps(body if body is not None else {}, ensure_ascii=False).encode()
    return (
        _header_bytes(FULL_CLIENT)
        + _optional_bytes(event, session)
        + len(raw).to_bytes(4, "big", signed=True)
        + raw
    )


def _parse(message: bytes) -> dict:
    """Parse one server frame.

    Args:
        message: Raw frame.

    Returns:
        ``{"type", "event", "payload"}``.
    """
    info = {
        "type": (message[1] >> 4) if len(message) > 1 else -1,
        "event": 0,
        "payload": b"",
    }
    offset = 4
    if len(message) > 1 and (message[1] & 0x0F) == WITH_EVENT and offset + 4 <= len(message):
        info["event"] = int.from_bytes(message[offset : offset + 4], "big", signed=True)
        offset += 4
        if info["event"] not in (
            EV_CONNECTION_STARTED,
            EV_CONNECTION_FAILED,
        ) and offset + 4 <= len(message):
            size = int.from_bytes(message[offset : offset + 4], "big", signed=True)
            offset += 4
            if 0 < size <= len(message) - offset:
                offset += size
    if offset + 4 <= len(message):
        size = int.from_bytes(message[offset : offset + 4], "big", signed=True)
        if 0 <= size <= len(message) - offset - 4:
            info["payload"] = message[offset + 4 : offset + 4 + size]
    return info


async def _pcm_stream(text: str, extra: dict | None = None):
    """与豆包双向流式会话：边收边产出裸 PCM。

    Args:
        text: What to say.
        extra: Extra ``req_params`` (语速/音调/情绪指令等；覆盖同名默认值）。

    Yields:
        ``(pcm_bytes, billed)``：正常片段 billed 为 0；最后一条 ``pcm_bytes`` 为空、
        带上服务端回报的计费字数（拿不到就是 0）。

    Raises:
        TTSError: When it is not configured, unreachable, or the service refuses.
    """
    values = config()
    key = str(values["api_key"] or "").strip()
    if not key:
        raise TTSError(f"还没配语音 Key（写进 {CONFIG_PATH} 的 api_key）")
    text = text.strip()
    if not text:
        raise TTSError("没有要念的内容")
    try:  # 延迟导入：AstrBot 运行时自带 websockets，缺了也只影响这一个功能
        import websockets  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        raise TTSError(f"这个环境没有 websockets（{type(exc).__name__}）") from exc

    headers = {
        "X-Api-Key": key,
        "X-Api-Resource-Id": str(values["resource_id"]),
        "X-Api-Connect-Id": str(uuid.uuid4()),
        # 让服务端在会话结束时回报本次计费字数
        "X-Control-Require-Usage-Tokens-Return": "*",
    }
    session = uuid.uuid4().hex
    wanted = str(values["format"])
    # 要 wav 就先要 pcm，回头自己封容器（见 wrap_wav 的说明）
    request_format = REQUEST_FORMAT.get(wanted, "pcm")
    params = {
        "speaker": str(values["voice"]),
        "audio_params": {
            "format": request_format,
            "sample_rate": int(values["sample_rate"]),
        },
        "additions": "{}",
        "speech_rate": int(values["speech_rate"]),
        "loudness_rate": int(values["loudness_rate"]),
        # true = 去掉 markdown / emoji（不然 "**你好**" 会被念成"星星你好星星"）
        "disable_markdown_filter": True,
        "disable_emoji_filter": True,
    }
    if extra:
        # 微调口子：语速/音量/音调（post_process.pitch）/情绪指令（context_texts）等
        params.update(extra)
    billed = 0
    produced = 0
    try:
        async with websockets.connect(
            URL,
            additional_headers=headers,
            max_size=1 << 30,
            open_timeout=CONNECT_TIMEOUT,
        ) as ws:
            await ws.send(_frame(EV_START_CONNECTION))
            while True:
                info = _parse(await asyncio.wait_for(ws.recv(), timeout=CONNECT_TIMEOUT))
                if info["event"] == EV_CONNECTION_FAILED:
                    raise TTSError(
                        "连上了但被拒：" + info["payload"].decode("utf-8", "replace")[:160],
                    )
                if info["event"] == EV_CONNECTION_STARTED:
                    break
            await ws.send(
                _frame(
                    EV_START_SESSION,
                    {
                        "user": {"uid": "qingyu"},
                        "event": EV_START_SESSION,
                        "namespace": "BidirectionalTTS",
                        "req_params": params,
                    },
                    session,
                )
            )
            await ws.send(
                _frame(
                    EV_TASK_REQUEST,
                    {
                        "user": {"uid": "qingyu"},
                        "event": EV_TASK_REQUEST,
                        "namespace": "BidirectionalTTS",
                        "req_params": {**params, "text": text},
                    },
                    session,
                )
            )
            await ws.send(_frame(EV_FINISH_SESSION, None, session))
            while True:
                info = _parse(await asyncio.wait_for(ws.recv(), timeout=READ_TIMEOUT))
                if info["type"] == ERROR_MSG:
                    raise TTSError(
                        "服务端报错：" + info["payload"].decode("utf-8", "replace")[:160],
                    )
                if info["event"] == EV_SESSION_FAILED:
                    raise TTSError(
                        "合成失败：" + info["payload"].decode("utf-8", "replace")[:160],
                    )
                if info["event"] == EV_TTS_RESPONSE and info["payload"]:
                    produced += len(info["payload"])
                    # 这一句就是"边说边播"的地基：收到一段就交出去，不攒到最后
                    yield info["payload"], 0
                elif info["event"] == EV_SESSION_FINISHED:
                    if info["payload"]:
                        try:
                            billed = int(
                                (json.loads(info["payload"]).get("usage") or {}).get(
                                    "text_words", 0
                                )
                            )
                        except (ValueError, AttributeError):
                            billed = 0
                    break
    except TTSError:
        raise
    except asyncio.TimeoutError as exc:
        raise TTSError("等音频超时了") from exc
    except Exception as exc:  # noqa: BLE001 - 网络/鉴权问题都给一句人话
        raise TTSError(f"连不上豆包语音：{type(exc).__name__}: {exc}") from exc
    if not produced:
        raise TTSError("服务端没返回音频")
    yield b"", billed


async def synthesize(text: str, extra: dict | None = None) -> tuple[bytes, str, int]:
    """Turn one line into audio（整句收完再给，QQ/桌宠用这条）。

    Args:
        text: What to say.
        extra: Extra ``req_params`` (语速/音调/情绪指令等；覆盖同名默认值）。

    Returns:
        ``(audio_bytes, extension, billed_chars)`` — billed chars is 0 when the
        server did not report it.

    Raises:
        TTSError: When it is not configured, unreachable, or the service refuses.
    """
    values = config()
    wanted = str(values["format"])
    audio = bytearray()
    billed = 0
    # 长回复**切段逐段合成**,拼成一条完整音频(见 split_for_speech 的说明)
    spoken = split_for_speech(text)
    for piece in spoken:
        async for chunk, chunk_billed in _pcm_stream(piece, extra):
            if chunk:
                audio.extend(chunk)
            if chunk_billed:
                billed = chunk_billed
    if wanted in ("wav", "pcm"):
        # 自己封 WAV：扩展名与魔数都对得上，AstrBot 就不会去找系统 ffmpeg
        return (
            wrap_wav(bytes(audio), int(values["sample_rate"])),
            "wav",
            billed,
        )
    return bytes(audio), wanted, billed


async def stream(text: str, extra: dict | None = None):
    """边收边出：每收到一段裸 PCM 就交出去（设备通道"边说边播"用这条）。

    Args:
        text: What to say.
        extra: Extra ``req_params`` (语速/音调/情绪指令等；覆盖同名默认值）。

    Yields:
        ``(pcm_bytes, billed)``：``pcm_bytes`` 为空的那一条表示结束，带上计费字数。

    Raises:
        TTSError: When it is not configured, unreachable, or the service refuses.
    """
    # 长回复切段逐段流出去(整段超过云端单次请求上限会被整段拒掉,一个字都念不出来)
    for piece in split_for_speech(text):
        async for chunk, billed in _pcm_stream(piece, extra):
            yield chunk, billed


def sample_rate() -> int:
    """当前配置的采样率（设备要知道拿什么率播）。

    Returns:
        Sample rate in Hz.
    """
    return int(config()["sample_rate"])



def save_audio(data: bytes, extension: str) -> Path:
    """Write synthesized audio into the cache directory (and trim old files).

    Args:
        data: Audio bytes.
        extension: File extension (``mp3`` / ``wav`` …).

    Returns:
        The written path.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    target = CACHE_DIR / f"qingyu_{int(time.time())}_{uuid.uuid4().hex[:6]}.{extension}"
    target.write_bytes(data)
    # 只留最近几份：发出去要等管道读完，所以不能立刻删
    files = sorted(CACHE_DIR.glob("qingyu_*"), key=lambda path: path.stat().st_mtime)
    for stale in files[:-MAX_CACHE_FILES]:
        try:
            stale.unlink()
        except OSError:
            continue
    return target
