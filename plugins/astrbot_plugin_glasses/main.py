r"""glasses：轻语智能眼镜的平台适配器（M0）。

**派生自** ``astrbot_plugin_desktop_pet``（同一套 WS 服务端 + JSON 帧 + 流式片段），
在它基础上加了眼镜要的三样东西：

| # | 能力 | 帧 |
|---|------|----|
| 1 | **下行音频**：她这轮要出声时（结果链里带 ``Record``），把那个 wav 拆成**裸 PCM** 分块推给设备，设备端零解码 | ``audio_begin`` → 二进制帧 → ``audio_end`` |
| 2 | **上行音频**：设备把整句 wav 传上来，落盘后作为 ``Record`` 组件交给管线（AstrBot 打开 STT 后即可转文字，见方案 §4.3） | ``audio`` + 二进制帧 |
| 3 | **状态与控制**：她这轮的心情/精力/好感/动作，以及按键来的 ``hush``/``mute``/``mode`` | ``state`` / ``control`` |

设计取舍（与方案 v2 对齐）：

- 桌面通道（``desktop_pet``）**一行不改**，眼镜是独立平台，会话是
  ``glasses:FriendMessage:<身份>``，与 QQ、桌宠互不干扰（"通道隔离"）；
- 记忆/好感是否共享由**身份**决定：默认沿用同一个 ``user_id``（她是同一个她），
  想彻底隔离就把配置里的 ``user_id`` 改成别的（方案 §4.2 ②）；
- 协议全貌见 ``轻语智能眼镜方案.md`` §5；设备侧实现是 ``pet_desktop/glasses_sim.py``。
"""

import asyncio
import base64
import hmac
import json
import struct
import time
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from urllib.parse import unquote, urlparse

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain, Record
from astrbot.api.platform import register_platform_adapter
from astrbot.api.star import Context, Star, register
from astrbot.core import logger
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform import (
    AstrBotMessage,
    MessageMember,
    MessageType,
    Platform,
    PlatformMetadata,
)
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

PLUGIN_VERSION = "0.1.0"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 6200
# 配了密钥时，客户端必须在这么多秒内先发认证帧
AUTH_TIMEOUT = 10.0
# 每帧音频分块大小（字节）：24 kHz 单声道 16 bit ≈ 48 KB/s，16 KB ≈ 0.33 s
AUDIO_CHUNK_BYTES = 16 * 1024
# `hush` 之后这么多秒内不再推她的声音（设备侧同时会自己停播）
HUSH_SECONDS = 5.0
# 上行音频落盘的目录（放插件数据目录下，方便排查）
UPLOAD_DIR_NAME = "glasses_uploads"


def _wav_to_pcm(path: Path) -> tuple[bytes, int, int]:
    """把 wav 拆成裸 PCM 与格式信息。

    Args:
        path: 本地 wav 文件。

    Returns:
        ``(pcm_bytes, sample_rate, channels)``；不是 wav 时抛 ValueError。

    Raises:
        ValueError: 文件不是 RIFF/WAVE，或者缺 ``fmt``/``data`` 块。
    """
    raw = path.read_bytes()
    if len(raw) < 44 or raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise ValueError("不是 wav")
    sample_rate = 0
    channels = 0
    data = b""
    offset = 12
    while offset + 8 <= len(raw):
        chunk_id = raw[offset : offset + 4]
        (chunk_size,) = struct.unpack("<I", raw[offset + 4 : offset + 8])
        body = raw[offset + 8 : offset + 8 + chunk_size]
        if chunk_id == b"fmt " and len(body) >= 16:
            _, channels, sample_rate = struct.unpack("<HHI", body[:8])
        elif chunk_id == b"data":
            data = body
        offset += 8 + chunk_size + (chunk_size % 2)
    if not sample_rate or not data:
        raise ValueError("wav 缺 fmt/data 块")
    return data, sample_rate, channels


def _record_path(component) -> str:  # noqa: ANN001 - AstrBot 的 Record
    """尽最大努力拿到语音段指向的本地文件。

    Args:
        component: AstrBot 的 Record 组件。

    Returns:
        本地路径；拿不到时返回空串。
    """
    candidate = str(getattr(component, "path", "") or "")
    if candidate and Path(candidate).is_file():
        return candidate
    raw = str(getattr(component, "file", "") or "")
    if raw.startswith("file://"):
        parsed = urlparse(raw)
        candidate = unquote(parsed.path)
        # Windows 下会是 /D:/... 这种形式
        if candidate.startswith("/") and len(candidate) > 2 and candidate[2] == ":":
            candidate = candidate[1:]
        if Path(candidate).is_file():
            return candidate
    if raw and Path(raw).is_file():
        return raw
    return ""


class GlassesEvent(AstrMessageEvent):
    """事件子类：把她的回复（文本 + 声音）真正推到眼镜上。

    基类的 ``send()`` 只上报指标、不投递；投递得由平台自己的事件子类来做
    （``desktop_pet`` 与 ``webchat`` 都是这么干的）。
    """

    def __init__(
        self,
        message_str: str,
        message_obj: AstrBotMessage,
        platform_meta: PlatformMetadata,
        session_id: str,
        platform: "GlassesPlatform",
    ) -> None:
        """记住平台实例，回复要经它推出去。

        Args:
            message_str: 纯文本消息。
            message_obj: AstrBot 消息对象。
            platform_meta: 平台元数据。
            session_id: 会话 id。
            platform: 拥有这条连接的适配器实例。
        """
        super().__init__(message_str, message_obj, platform_meta, session_id)
        self._glasses = platform

    def _state_payload(self) -> dict | None:
        """把这轮的状态（心情/精力/好感/动作）打包成 ``state`` 帧。

        Returns:
            状态帧内容；拿不到快照时返回 None。
        """
        snapshot = self.get_extra("qingyu.snapshot")
        person = getattr(snapshot, "person", None)
        mood = getattr(snapshot, "mood", None)
        if mood is None and person is None:
            return None
        plan = self.get_extra("qingyu.plan")
        return {
            "type": "state",
            "mood": getattr(mood, "mood", None),
            "energy": getattr(mood, "energy", None),
            "affection": getattr(person, "affection", None),
            "familiarity": getattr(person, "familiarity", None),
            "action": str(getattr(plan, "action", "") or ""),
            "umo": self.unified_msg_origin,
        }

    async def send(self, message: MessageChain | None) -> None:
        """投递一条回复：先推文本，再推音频，最后推状态。

        Args:
            message: 要发出的消息链（None 表示什么也不发）。
        """
        if message is None:
            return
        chain = list(getattr(message, "chain", None) or [])
        try:
            text = message.get_plain_text()
        except Exception:  # noqa: BLE001 - 取不到文本就当没有
            text = ""
        if text:
            await self._glasses.push_reply(text)
        state = self._state_payload()
        if state:
            await self._glasses._broadcast(state)
        for part in chain:
            if type(part).__name__ == "Record":
                await self._glasses.push_audio(part)
        await super().send(message)

    async def send_streaming(
        self,
        generator: AsyncGenerator[MessageChain, None],
        use_fallback: bool = False,
    ) -> None:
        """流式投递：每个片段立刻推给设备（她"边说边出字"）。

        Args:
            generator: 消息链片段流。
            use_fallback: 框架参数，这里不用。
        """
        state = self._state_payload()
        if state:
            await self._glasses._broadcast(state)
        audio_parts: list = []
        async for chain in generator:
            try:
                text = chain.get_plain_text()
            except Exception:  # noqa: BLE001
                text = ""
            if text:
                await self._glasses.push_reply(text)
            audio_parts.extend(
                part for part in (getattr(chain, "chain", None) or [])
                if type(part).__name__ == "Record"
            )
        for part in audio_parts:
            await self._glasses.push_audio(part)
        self._has_send_oper = True


@register_platform_adapter(
    "glasses",
    "智能眼镜：随身设备的 WebSocket 通道（文本 + 裸 PCM 上下行 + 状态帧）",
    default_config_tmpl={
        "type": "glasses",
        "enable": True,
        "id": "glasses",
        "host": DEFAULT_HOST,
        "port": DEFAULT_PORT,
        "user_id": "10001",
        "user_name": "示例用户",
        "secret": "",
    },
    adapter_display_name="智能眼镜",
    support_streaming_message=True,
)
class GlassesPlatform(Platform):
    """WebSocket server that the glasses (or the PC simulator) connects to."""

    def __init__(
        self,
        config: dict,
        settings: dict,
        event_queue: asyncio.Queue,
    ) -> None:
        """保存配置。

        Args:
            config: 这个平台的配置块。
            settings: 全局平台设置（不用）。
            event_queue: 事件总线读的队列。
        """
        super().__init__(config, event_queue)
        self.settings = settings
        self.host = str(config.get("host") or DEFAULT_HOST)
        self.port = int(config.get("port") or DEFAULT_PORT)
        self.user_id = str(config.get("user_id") or "0")
        self.user_name = str(config.get("user_name") or "眼镜用户")
        # 共享密钥：本机用可以留空；要出门连（暴露端口）就必须填。
        self.secret = str(config.get("secret") or "").strip()
        self.data_dir = Path(get_astrbot_plugin_data_path())
        self._clients: set = set()
        self._lock = asyncio.Lock()
        # 每连接的待接收音频信息（上行二进制帧要用）
        self._pending_audio: dict = {}
        # 设备说了"别说了"之后的一小段时间里不再推音频
        self._hush_until = 0.0
        self._meta = PlatformMetadata(
            name="glasses",
            description="智能眼镜通道",
            id=str(config.get("id") or "glasses"),
            support_proactive_message=True,
        )

    # ---------------------------------------------------------------- 平台接口

    def meta(self) -> PlatformMetadata:
        """平台元数据（它的 id 会成为 umo 前缀）。

        Returns:
            元数据对象。
        """
        return self._meta

    def run(self):
        """起 WS 服务端，直到被取消。

        Returns:
            服务端协程。
        """
        return self._serve()

    async def terminate(self) -> None:
        """关掉所有连接并停止服务。"""
        async with self._lock:
            clients = list(self._clients)
            self._clients.clear()
        for client in clients:
            try:
                await client.close()
            except Exception:  # noqa: BLE001 - 关不掉就算了
                pass
        logger.info("glasses: 已停止")

    def create_event(self, message: AstrBotMessage) -> AstrMessageEvent:
        """把收到的消息包成我们自己的事件类。

        Args:
            message: 消息对象。

        Returns:
            知道怎么回复回眼镜的事件。
        """
        return GlassesEvent(
            message_str=message.message_str,
            message_obj=message,
            platform_meta=self.meta(),
            session_id=message.session_id,
            platform=self,
        )

    async def push_reply(self, text: str) -> int:
        """把一段回复文本推给所有连着的眼镜。

        Args:
            text: 回复文本（流式片段也算一段）。

        Returns:
            收到这条的设备数。
        """
        return await self._broadcast(
            {"type": "reply", "text": text, "ts": int(time.time())},
        )

    async def push_audio(self, component) -> int:  # noqa: ANN001 - AstrBot 的 Record
        """把这个语音段拆成裸 PCM 推给设备。

        设备端零解码：拿到 PCM 直接喂 I2S/声卡即可（豆包本来就出 24 kHz 单声道 PCM）。

        Args:
            component: 结果链里的 Record 组件。

        Returns:
            成功推到的设备数；没推出去（没文件/不是 wav/刚被 hush）时返回 0。
        """
        if time.time() < self._hush_until:
            logger.info("glasses: 设备刚说过「别说了」，这轮不推音频")
            return 0
        path = _record_path(component)
        if not path:
            logger.warning("glasses: 这条语音没有本地文件，跳过音频下推")
            return 0
        try:
            pcm, sample_rate, channels = _wav_to_pcm(Path(path))
        except Exception as exc:  # noqa: BLE001 - 不是 wav 就只发文本
            logger.warning(f"glasses: 语音不是 wav（{type(exc).__name__}: {exc}），跳过音频下推")
            return 0
        sent = await self._broadcast_binary(
            {
                "type": "audio_begin",
                "format": "pcm16",
                "sample_rate": sample_rate,
                "channels": channels,
                "bytes": len(pcm),
            },
            pcm,
        )
        if sent:
            await self._broadcast({"type": "audio_end", "reason": "finished"})
            logger.info(
                f"glasses: 推了 {len(pcm)} 字节 PCM（{sample_rate} Hz/{channels} 声道）给 {sent} 个设备"
            )
        return sent

    async def push_audio_stream(
        self,
        chunks,
        *,
        sample_rate: int = 24000,
        channels: int = 1,
    ) -> tuple[int, int]:
        """边收边推裸 PCM：第一块到手就发 ``audio_begin``，最后发 ``audio_end``。

        这是"她说第一句时你就听见了"的关键：不整句等合成完（那是 5 秒级的差距），
        豆包那边一段一段给，这里就一段一段推。

        Args:
            chunks: 异步生成器，产出 ``(pcm_bytes, billed)``；``pcm_bytes`` 为空表示结束。
            sample_rate: 采样率（跟合成配置一致）。
            channels: 声道数。

        Returns:
            ``(推到的设备数, 服务端报的计费字数)``。
        """
        if time.time() < self._hush_until:
            logger.info("glasses: 设备刚说过「别说了」，这轮不合成音频")
            return 0, 0
        async with self._lock:
            clients = list(self._clients)
        if not clients:
            logger.info("glasses: 没有设备连着，这轮不推音频")
            return 0, 0
        began = False
        total = 0
        billed = 0
        reason = "finished"
        begin_frame = json.dumps(
            {
                "type": "audio_begin",
                "format": "pcm16",
                "sample_rate": sample_rate,
                "channels": channels,
            },
            ensure_ascii=False,
        )
        async for pcm, chunk_billed in chunks:
            if chunk_billed:
                billed = int(chunk_billed)
            if not pcm:
                continue
            if time.time() < self._hush_until:
                # 说着话被按键打断：让设备立刻收尾
                reason = "cancelled"
                break
            try:
                if not began:
                    for client in clients:
                        await client.send(begin_frame)
                    began = True
                for offset in range(0, len(pcm), AUDIO_CHUNK_BYTES):
                    piece = pcm[offset : offset + AUDIO_CHUNK_BYTES]
                    for client in clients:
                        await client.send(piece)
                total += len(pcm)
            except Exception as exc:  # noqa: BLE001 - 设备掉了就停
                logger.warning(f"glasses: 推音频中断（{type(exc).__name__}）")
                reason = "cancelled"
                break
        if began:
            await self._broadcast({"type": "audio_end", "reason": reason})
        logger.info(
            f"glasses: 流式推了 {total} 字节 PCM（{sample_rate} Hz，{reason}）给 {len(clients)} 台设备"
        )
        return len(clients) if began else 0, billed

    async def send_by_session(
        self,
        session: MessageSession,
        message_chain: MessageChain,
    ) -> None:
        """她主动说话时，推给所有连着的眼镜。

        Args:
            session: 目标会话（眼镜最多一台）。
            message_chain: 要发的消息链。
        """
        try:
            text = message_chain.get_plain_text()
        except Exception:  # noqa: BLE001 - 拿不到文本就当空
            text = ""
        if text:
            await self.push_reply(text)
        await super().send_by_session(session, message_chain)

    # ---------------------------------------------------------------- 内部

    async def _serve(self) -> None:
        """一直收连接，直到被取消。"""
        try:
            import websockets
        except ImportError:
            logger.error("glasses: 缺少 websockets 依赖，眼镜通道起不来")
            return
        async with websockets.serve(
            self._handler,
            self.host,
            self.port,
            max_size=None,
            # 设备通道**不发心跳**：设备"等主人说话"的那几十秒里，事件循环可能被阻塞
            # （录音是阻塞循环，见 glasses_sim 的说明），心跳等不到回应就会把连接踢掉。
            # 2026-09-29 实测踩到：`ConnectionClosedError: no close frame received or sent`，
            # 那一轮她刚生成好的音频也丢了。断线交给设备侧的"重连再试一次"兜底。
            ping_interval=None,
        ):
            logger.info(f"glasses: 眼镜通道已监听 ws://{self.host}:{self.port}")
            await asyncio.Future()

    async def _handler(self, ws) -> None:  # noqa: ANN001 - websockets connection
        """处理一台设备（配了密钥时先认证）。

        Args:
            ws: websocket 连接。
        """
        peer = getattr(ws, "remote_address", None)
        if self.secret:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=AUTH_TIMEOUT)
            except (TimeoutError, asyncio.TimeoutError):
                logger.warning(f"glasses: {peer} 没在 {AUTH_TIMEOUT} 秒内认证，断开")
                await ws.close(1008, "auth required")
                return
            if not self._authorised(raw):
                logger.warning(f"glasses: {peer} 密钥不对，已拒绝")
                await ws.close(1008, "bad secret")
                return
            logger.info(f"glasses: {peer} 认证通过")
        async with self._lock:
            self._clients.add(ws)
        logger.info(f"glasses: 设备已连接 {peer}（共 {len(self._clients)} 台）")
        try:
            await ws.send(
                json.dumps(
                    {
                        "type": "hello",
                        "user_id": self.user_id,
                        "user_name": self.user_name,
                        "port": self.port,
                    },
                    ensure_ascii=False,
                ),
            )
            async for raw in ws:
                if isinstance(raw, (bytes, bytearray)):
                    await self._on_binary(ws, bytes(raw))
                    continue
                if self.secret and self._is_auth_frame(raw):
                    continue
                await self._on_frame(ws, raw)
        except Exception as exc:  # noqa: BLE001 - 断线是常态
            logger.debug(f"glasses: 连接结束（{type(exc).__name__}）")
        finally:
            async with self._lock:
                self._clients.discard(ws)
            self._pending_audio.pop(id(ws), None)
            logger.info("glasses: 设备已断开")

    @staticmethod
    def _is_auth_frame(raw) -> bool:  # noqa: ANN001 - raw frame
        """这一帧是不是认证帧。

        Args:
            raw: 原始 websocket 载荷。

        Returns:
            是认证帧返回 True。
        """
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return False
        return isinstance(data, dict) and data.get("type") == "auth"

    def _authorised(self, raw) -> bool:  # noqa: ANN001 - raw frame
        """比对认证帧里的密钥。

        Args:
            raw: 原始 websocket 载荷。

        Returns:
            密钥一致返回 True。
        """
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return False
        if not isinstance(data, dict) or data.get("type") != "auth":
            return False
        return hmac.compare_digest(str(data.get("secret") or ""), self.secret)

    async def _on_binary(self, ws, blob: bytes) -> None:  # noqa: ANN001 - ws
        """收一个二进制帧：上行音频的正文。

        只有先收到 ``{"type":"audio", ...}`` 说明"接下来这段是音频"，才会写盘。

        Args:
            ws: websocket 连接。
            blob: 二进制音频数据。
        """
        info = self._pending_audio.pop(id(ws), None)
        if not info:
            logger.warning(f"glasses: 收到 {len(blob)} 字节二进制帧，但前面没有 audio 帧，忽略")
            return
        await self._accept_audio(ws, blob, info)

    async def _accept_audio(self, ws, blob: bytes, info: dict) -> None:  # noqa: ANN001 - ws
        """把上行的整句音频落盘并投进管线。

        Args:
            ws: websocket 连接。
            blob: 音频字节（wav）。
            info: 来自 ``audio`` 帧的元信息。
        """
        upload_dir = self.data_dir / UPLOAD_DIR_NAME
        try:
            upload_dir.mkdir(parents=True, exist_ok=True)
            target = upload_dir / f"up_{int(time.time())}_{uuid.uuid4().hex[:6]}.wav"
            target.write_bytes(blob)
        except Exception as exc:  # noqa: BLE001 - 写不下去就放弃这条
            logger.error(f"glasses: 上行音频落盘失败（{type(exc).__name__}: {exc}）")
            return
        text = str(info.get("text") or "").strip()
        record = Record.fromFileSystem(path=str(target))
        logger.info(
            f"glasses: 收到上行音频 {len(blob)} 字节（{info.get('secs') or '?'} 秒）→ {target.name}",
        )
        self._commit(ws, text=text, images=[], audio=record, raw=info)

    def _commit(
        self,
        ws,  # noqa: ANN001 - ws
        *,
        text: str,
        images: list[str],
        audio=None,  # noqa: ANN001 - Record | None
        raw: dict | None = None,
    ) -> None:
        """组一条管线事件（文本 / 图片 / 语音）。

        Args:
            ws: websocket 连接（暂未用，留给将来区分多台设备）。
            text: 文本内容。
            images: 本地图片路径。
            audio: 语音组件。
            raw: 原始帧，塞进 ``raw_message`` 方便排查。
        """
        message = AstrBotMessage()
        message.type = MessageType.FRIEND_MESSAGE
        message.self_id = "glasses"
        message.session_id = self.user_id
        message.message_id = uuid.uuid4().hex
        message.sender = MessageMember(user_id=self.user_id, nickname=self.user_name)
        components: list = []
        if text:
            components.append(Plain(text))
        for item in images:
            try:
                components.append(Image.fromFileSystem(path=item))
            except Exception as exc:  # noqa: BLE001 - 单张图坏了不该整条消息丢掉
                logger.warning(f"glasses: 附图失败 {item}: {exc}")
        if audio is not None:
            components.append(audio)
        message.message = components
        message.message_str = text
        message.raw_message = raw or {}
        message.timestamp = int(time.time())
        self.commit_event(self.create_event(message))
        logger.info(
            "glasses: 收到设备消息 "
            f"{text[:40] or '（只有图/语音）'}"
            f"{f'（附图 {len(images)} 张）' if images else ''}"
            f"{'（带语音）' if audio is not None else ''}",
        )

    async def _on_frame(self, ws, raw) -> None:  # noqa: ANN001 - ws / raw frame
        """把一个文本帧变成管线事件或控制动作。

        帧格式（JSON 文本）::

            {"type": "message", "text": "我出门了"}
            {"type": "message", "text": "", "images": ["D:/.../shot.png"]}
            {"type": "audio", "format": "wav", "sample_rate": 16000, "secs": 3.2}
            （紧接着一个二进制帧，里面是 wav 字节）
            {"type": "control", "action": "hush"|"mute"|"wake"|"mode", "value": "home"}
            {"type": "ping", "ts": 10006}

        Args:
            ws: websocket 连接。
            raw: 原始 websocket 载荷。
        """
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return
        if not isinstance(data, dict):
            return
        kind = str(data.get("type") or "")
        if kind == "ping":
            await self._send_one(ws, {"type": "pong", "ts": data.get("ts")})
            return
        if kind == "control":
            await self._on_control(ws, data)
            return
        if kind == "audio":
            payload = data.get("data")
            if isinstance(payload, str) and payload:
                try:
                    blob = base64.b64decode(payload)
                except Exception as exc:  # noqa: BLE001 - 坏数据就丢掉
                    logger.warning(f"glasses: audio 帧的 base64 解不开（{exc}）")
                    return
                await self._accept_audio(ws, blob, data)
            else:
                # 正文走下一个二进制帧
                self._pending_audio[id(ws)] = data
            return
        if kind != "message":
            logger.debug(f"glasses: 不认识的帧 {kind}")
            return
        text = str(data.get("text") or "").strip()
        raw_images = data.get("images")
        if isinstance(raw_images, str):
            raw_images = [raw_images]
        if not isinstance(raw_images, (list, tuple)):
            raw_images = []
        images = [str(item) for item in raw_images if str(item).strip()]
        images = [item for item in images if Path(item).is_file()][:3]
        if not text and not images:
            return
        self._commit(ws, text=text, images=images, raw=data)

    async def _on_control(self, ws, data: dict) -> None:  # noqa: ANN001 - ws
        """处理按键来的控制帧。

        Args:
            ws: websocket 连接。
            data: 控制帧内容。
        """
        action = str(data.get("action") or "")
        value = str(data.get("value") or "")
        if action == "hush":
            self._hush_until = time.time() + HUSH_SECONDS
            logger.info(f"glasses: 设备说「别说了」（{HUSH_SECONDS} 秒内不推音频）")
        elif action == "mode":
            logger.info(f"glasses: 设备切到「{value or '未知'}」模式")
        elif action in {"mute", "wake"}:
            logger.info(f"glasses: 设备按键 {action}{f'（{value}）' if value else ''}")
        else:
            logger.debug(f"glasses: 不认识的控制动作 {action}")
        await self._send_one(ws, {"type": "control_ack", "action": action})

    async def _send_one(self, ws, payload: dict) -> None:  # noqa: ANN001 - ws
        """给一台设备发一个 JSON 帧。

        Args:
            ws: websocket 连接。
            payload: 可 JSON 化的内容。
        """
        try:
            await ws.send(json.dumps(payload, ensure_ascii=False))
        except Exception:  # noqa: BLE001 - 单条失败不影响别的
            async with self._lock:
                self._clients.discard(ws)

    async def _broadcast(self, payload: dict) -> int:
        """给所有设备发一个 JSON 帧。

        Args:
            payload: 可 JSON 化的内容。

        Returns:
            发到的设备数。
        """
        async with self._lock:
            clients = list(self._clients)
        if not clients:
            return 0
        body = json.dumps(payload, ensure_ascii=False)
        sent = 0
        for client in clients:
            try:
                await client.send(body)
                sent += 1
            except Exception:  # noqa: BLE001 - 单条失败不影响别的
                async with self._lock:
                    self._clients.discard(client)
        return sent

    async def _broadcast_binary(self, header: dict, blob: bytes) -> int:
        """先发一个说明帧，再发音频二进制帧。

        Args:
            header: ``audio_begin`` 帧内容。
            blob: 裸 PCM 字节。

        Returns:
            发到的设备数。
        """
        async with self._lock:
            clients = list(self._clients)
        if not clients:
            return 0
        body = json.dumps(header, ensure_ascii=False)
        sent = 0
        for client in clients:
            try:
                await client.send(body)
                for offset in range(0, len(blob), AUDIO_CHUNK_BYTES):
                    await client.send(blob[offset : offset + AUDIO_CHUNK_BYTES])
                sent += 1
            except Exception:  # noqa: BLE001 - 单条失败不影响别的
                async with self._lock:
                    self._clients.discard(client)
        return sent


@register(
    "glasses_tools",
    "migration",
    "智能眼镜的辅助指令：/眼镜通道 看连接与配置",
    PLUGIN_VERSION,
)
class GlassesTools(Star):
    """Small helper commands for the glasses channel."""

    def __init__(self, context: Context) -> None:
        """保存 context。

        Args:
            context: AstrBot 插件上下文。
        """
        super().__init__(context)

    @filter.command("眼镜通道")
    async def glasses_link_status(self, event: AstrMessageEvent):
        """看眼镜通道的配置与当前连接数（管理员）。

        Args:
            event: 命令事件。

        Yields:
            状态文本。
        """
        if not event.is_admin():
            yield event.plain_result("这个只有管理员能看哦~")
            return
        instances = []
        try:
            for platform in self.context.platform_manager.platform_insts:
                if platform.meta().name == "glasses":
                    instances.append(platform)
        except Exception as exc:  # noqa: BLE001 - 拿不到就当没起
            yield event.plain_result(f"读平台列表失败：{type(exc).__name__}: {exc}")
            return
        if not instances:
            yield event.plain_result(
                "眼镜通道没在跑。检查配置里有没有 id=glasses 的平台"
                "（面板「平台」页新增，或走 /api/config/platform/bots）。",
            )
            return
        lines = []
        for platform in instances:
            count = len(getattr(platform, "_clients", ()) or ())
            lines.append(
                f"· ws://{platform.host}:{platform.port}　设备连接 {count} 台　"
                f"身份 {platform.user_name}({platform.user_id})",
            )
        lines.append("设备那侧：pet_desktop\\glasses_sim.py（M0 用 PC 模拟）")
        yield event.plain_result("\n".join(lines))
