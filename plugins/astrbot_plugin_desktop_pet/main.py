"""desktop_pet：把桌宠接进 AstrBot 管线的平台适配器（阶段 D）。

为什么需要它（阶段 B 的面板通道不够用）：

- 面板每条 WebSocket 连接都会新开一个会话（``live_chat_service.py:157``），所以她记不住
  桌面上的上下文；
- ``qingyu_core`` 跳过 ``webchat`` 平台，面板聊天**不消耗她的精力、不涨好感**，桌宠里的
  "我"和 QQ 里的"我"在她心里是两个人。

这个适配器在 ``127.0.0.1:6198`` 起一个 WebSocket 服务端，桌宠连上来以后：

| 方向 | 行为 |
|------|------|
| 桌宠 → 她 | 消息按 ``user_id``（默认你 QQ）投进管线，会话固定为 ``desktop_pet:FriendMessage:<user_id>``，所以**记忆、好感、精力心情与 QQ 完全打通**，AstrBot 的会话历史也天然持久 |
| 她 → 桌宠 | ``send_by_session`` 把回复推给所有连着的桌宠；她也可以主动弹消息（主动消息能力已在 meta 里声明） |

桌宠那侧是 ``pet_desktop/petlink.py``。
"""

import asyncio
import hmac
import json
import time
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
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

PLUGIN_VERSION = "1.2.1"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 6198
# 配了密钥时，客户端必须在这么多秒内先发认证帧
AUTH_TIMEOUT = 10.0


class DesktopPetEvent(AstrMessageEvent):
    """事件子类：把 AstrBot 的回复真正送进桌宠。

    基类 ``AstrMessageEvent.send()`` 只上报指标、不投递，投递要靠平台自己的事件子类
    （webchat 就是这么做的：``WebChatMessageEvent._send``）。所以这里重写 ``send`` /
    ``send_streaming``，把文本推给连着的桌宠。
    """

    def __init__(
        self,
        message_str: str,
        message_obj: AstrBotMessage,
        platform_meta: PlatformMetadata,
        session_id: str,
        platform: "DesktopPetPlatform",
    ) -> None:
        """Wrap a message and remember the adapter.

        Args:
            message_str: Plain text of the message.
            message_obj: The message object.
            platform_meta: Platform metadata.
            session_id: Session id.
            platform: The adapter instance that will deliver replies.
        """
        super().__init__(message_str, message_obj, platform_meta, session_id)
        self._pet_platform = platform

    async def send(self, message: MessageChain | None) -> None:
        """Deliver one reply to the desktop pet.

        Args:
            message: Message chain to send (None means "nothing to send").
        """
        if message is not None:
            try:
                text = message.get_plain_text()
            except Exception:  # noqa: BLE001 - 取不到文本就当没有
                text = ""
            if text:
                await self._pet_platform.push_reply(text)
        await super().send(MessageChain([]))

    async def send_streaming(
        self,
        generator: AsyncGenerator[MessageChain, None],
        use_fallback: bool = False,
    ) -> None:
        """Deliver a streamed reply segment by segment.

        Args:
            generator: Stream of message chains.
            use_fallback: Unused; kept for signature compatibility.
        """
        async for chain in generator:
            await self.send(chain)
        self._has_send_oper = True


@register_platform_adapter(
    "desktop_pet",
    "桌面桌宠：本机 WebSocket 通道，桌宠里说的话算你自己的消息（记忆/好感/心情与 QQ 打通）",
    default_config_tmpl={
        "type": "desktop_pet",
        "enable": True,
        "id": "desktop_pet",
        "host": DEFAULT_HOST,
        "port": DEFAULT_PORT,
        "user_id": "desktop-user",
        "user_name": "Desktop user",
        "secret": "",
    },
    adapter_display_name="桌面桌宠",
    support_streaming_message=True,
)
class DesktopPetPlatform(Platform):
    """WebSocket server that the desktop pet connects to."""

    def __init__(
        self,
        config: dict,
        settings: dict,
        event_queue: asyncio.Queue,
    ) -> None:
        """Store the adapter configuration.

        Args:
            config: This bot's configuration block.
            settings: Global platform settings (unused).
            event_queue: Queue the event bus reads incoming events from.
        """
        super().__init__(config, event_queue)
        self.settings = settings
        self.host = str(config.get("host") or DEFAULT_HOST)
        self.port = int(config.get("port") or DEFAULT_PORT)
        self.user_id = str(config.get("user_id") or "0")
        self.user_name = str(config.get("user_name") or "桌面用户")
        # 共享密钥：本机用可以留空；要给别人连（暴露端口）就必须填，客户端连上后
        # 第一帧得是 {"type":"auth","secret":"..."}，不然直接断开。
        self.secret = str(config.get("secret") or "").strip()
        self._clients: set = set()
        self._lock = asyncio.Lock()
        self._meta = PlatformMetadata(
            name="desktop_pet",
            description="桌面桌宠通道",
            id=str(config.get("id") or "desktop_pet"),
            support_proactive_message=True,
        )

    # ---------------------------------------------------------------- 平台接口

    def meta(self) -> PlatformMetadata:
        """Platform metadata (its id becomes the umo prefix).

        Returns:
            The metadata object.
        """
        return self._meta

    def run(self):
        """Start the websocket server; runs until the task is cancelled.

        Returns:
            The server coroutine.
        """
        return self._serve()

    async def terminate(self) -> None:
        """Close every client and stop serving."""
        async with self._lock:
            clients = list(self._clients)
            self._clients.clear()
        for client in clients:
            try:
                await client.close()
            except Exception:  # noqa: BLE001 - 关不掉就算了
                pass
        logger.info("desktop_pet: 已停止")

    def create_event(self, message: AstrBotMessage) -> AstrMessageEvent:
        """Wrap an incoming message in our own event class.

        Args:
            message: The message object.

        Returns:
            The event that knows how to reply into the pet.
        """
        return DesktopPetEvent(
            message_str=message.message_str,
            message_obj=message,
            platform_meta=self.meta(),
            session_id=message.session_id,
            platform=self,
        )

    async def push_reply(self, text: str) -> int:
        """Send one reply text to every connected pet.

        Args:
            text: Reply text.

        Returns:
            How many pets received it.
        """
        return await self._broadcast(
            {"type": "reply", "text": text, "ts": int(time.time())},
        )

    async def send_by_session(
        self,
        session: MessageSession,
        message_chain: MessageChain,
    ) -> None:
        """Push her proactive message to every connected pet.

        Args:
            session: Target session (there is at most one desktop).
            message_chain: Message to deliver.
        """
        text = ""
        try:
            text = message_chain.get_plain_text()
        except Exception:  # noqa: BLE001 - 拿不到文本就当空
            text = ""
        if text:
            await self.push_reply(text)
        await super().send_by_session(session, message_chain)

    # ---------------------------------------------------------------- 内部

    async def _serve(self) -> None:
        """Accept connections until cancelled."""
        try:
            import websockets
        except ImportError:
            logger.error("desktop_pet: 缺少 websockets 依赖，桌宠通道起不来")
            return
        async with websockets.serve(
            self._handler,
            self.host,
            self.port,
            max_size=None,
        ):
            logger.info(f"desktop_pet: 桌宠通道已监听 ws://{self.host}:{self.port}")
            await asyncio.Future()

    async def _handler(self, ws) -> None:  # noqa: ANN001 - websockets connection
        """Handle one connected pet (auth first when a secret is configured).

        Args:
            ws: The websocket connection.
        """
        peer = getattr(ws, "remote_address", None)
        if self.secret:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=AUTH_TIMEOUT)
            except (TimeoutError, asyncio.TimeoutError):
                logger.warning(
                    f"desktop_pet: {peer} 没在 {AUTH_TIMEOUT} 秒内发认证，断开"
                )
                await ws.close(1008, "auth required")
                return
            if not self._authorised(raw):
                logger.warning(f"desktop_pet: {peer} 密钥不对，已拒绝")
                await ws.close(1008, "bad secret")
                return
            logger.info(f"desktop_pet: {peer} 认证通过")
        async with self._lock:
            self._clients.add(ws)
        logger.info(f"desktop_pet: 桌宠已连接 {peer}（共 {len(self._clients)} 个）")
        try:
            await ws.send(
                json.dumps(
                    {
                        "type": "hello",
                        "user_id": self.user_id,
                        "user_name": self.user_name,
                    },
                    ensure_ascii=False,
                ),
            )
            async for raw in ws:
                if self.secret and self._is_auth_frame(raw):
                    continue
                await self._on_frame(raw)
        except Exception as exc:  # noqa: BLE001 - 断线是常态
            logger.debug(f"desktop_pet: 连接结束（{type(exc).__name__}）")
        finally:
            async with self._lock:
                self._clients.discard(ws)
            logger.info("desktop_pet: 桌宠已断开")

    def _is_auth_frame(self, raw) -> bool:  # noqa: ANN001 - raw frame
        """Whether a frame is the auth handshake.

        Args:
            raw: Raw websocket payload.

        Returns:
            True when it is an ``auth`` frame.
        """
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return False
        return isinstance(data, dict) and data.get("type") == "auth"

    def _authorised(self, raw) -> bool:  # noqa: ANN001 - raw frame
        """Check the auth frame's secret against the configured one.

        Args:
            raw: Raw websocket payload.

        Returns:
            True when the secret matches.
        """
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return False
        if not isinstance(data, dict) or data.get("type") != "auth":
            return False
        return hmac.compare_digest(str(data.get("secret") or ""), self.secret)

    async def _on_frame(self, raw) -> None:  # noqa: ANN001 - raw text frame
        """Turn one incoming frame into a pipeline event.

        帧格式（都是 JSON 文本）::

            {"type": "message", "text": "帮我看看", "images": ["D:/.../shot.png"]}

        ``images`` 可选：桌宠的"看看我的屏幕"会把截图路径放进来，这里附成 ``Image``
        组件，管线就会走正常的图片流程（主模型不支持视觉时 AstrBot 会用配好的
        图片描述模型转成文字）。

        Args:
            raw: Raw websocket payload.
        """
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return
        if not isinstance(data, dict):
            return
        if data.get("type") == "ping":
            return
        text = str(data.get("text") or "").strip()
        images = [str(item) for item in (data.get("images") or []) if str(item).strip()]
        images = [item for item in images if Path(item).is_file()][:3]
        if not text and not images:
            return
        message = AstrBotMessage()
        message.type = MessageType.FRIEND_MESSAGE
        message.self_id = "desktop_pet"
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
                logger.warning(f"desktop_pet: 附图失败 {item}: {exc}")
        message.message = components
        message.message_str = text
        message.raw_message = data
        message.timestamp = int(time.time())
        self.commit_event(self.create_event(message))
        logger.info(
            f"desktop_pet: 收到桌宠消息 {text[:40] or '（只有图）'}"
            f"{f'（附图 {len(images)} 张）' if images else ''}",
        )

    async def _broadcast(self, payload: dict) -> int:
        """Send a frame to every connected pet.

        Args:
            payload: JSON-serialisable frame.

        Returns:
            How many pets received it (0 when nobody is connected).
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


@register(
    "desktop_pet_tools",
    "migration",
    "桌面桌宠的辅助指令：/桌宠通道 看连接与配置",
    PLUGIN_VERSION,
)
class DesktopPetTools(Star):
    """Small helper commands for the desktop channel."""

    def __init__(self, context: Context) -> None:
        super().__init__(context)

    @filter.command("桌宠通道")
    async def pet_link_status(self, event: AstrMessageEvent):
        """看桌宠通道的配置与当前连接数（管理员）。

        Args:
            event: Command event.

        Yields:
            Status text.
        """
        if not event.is_admin():
            yield event.plain_result("这个只有管理员能看哦~")
            return
        instances = []
        try:
            for platform in self.context.platform_manager.platform_insts:
                if platform.meta().name == "desktop_pet":
                    instances.append(platform)
        except Exception as exc:  # noqa: BLE001 - 拿不到就当没起
            yield event.plain_result(f"读平台列表失败：{type(exc).__name__}: {exc}")
            return
        if not instances:
            yield event.plain_result(
                "桌宠通道没在跑。检查配置里有没有 id=desktop_pet 的平台，"
                "或发 /桌宠通道 之前先在面板里加上它。",
            )
            return
        lines = []
        for platform in instances:
            count = len(getattr(platform, "_clients", ()) or ())
            lines.append(
                f"· ws://{platform.host}:{platform.port}　"
                f"桌宠连接 {count} 个　身份 {platform.user_name}({platform.user_id})",
            )
        lines.append("桌宠那侧：pet_desktop\\启动桌宠.bat")
        yield event.plain_result("\n".join(lines))
