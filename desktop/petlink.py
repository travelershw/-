"""桌宠 ↔ desktop_pet 平台适配器的本地连接（阶段 D）。

比面板通道好在：会话 id 固定、用你 QQ 的身份投进管线，所以她**记得你在桌面说过的话**，
桌宠聊天也会消耗她的精力、涨好感—— QQ 私聊和桌面是同一个"我"。

协议（都是 JSON 文本帧）：
- 她 → 桌宠：``{"type": "hello", ...}`` 连接问候；``{"type": "reply", "text": "..."}`` 她说的话
- 桌宠 → 她：``{"type": "message", "text": "..."}``
"""

import asyncio
import json
import threading

from PySide6.QtCore import QObject, Signal

URL = "ws://127.0.0.1:6198"
IDLE_STOP_SECONDS = 5.0
# 断线之后**不自动重连**：只提示一次，由用户右键 →「重连 AstrBot」手动重试。
# （自动重连会在对方没开机时每 8 秒敲一次门，把两边日志刷满，也让人误以为"她还在"。）


class PetLinkClient(QObject):
    """WebSocket client for the desktop_pet platform adapter."""

    chunk = Signal(str)
    done = Signal(str)
    failed = Signal(str)
    ready = Signal(str)

    def __init__(self, url: str = URL, secret: str = "") -> None:
        super().__init__()
        self.url = url
        self.secret = secret
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._outbox: asyncio.Queue[str] | None = None
        self._stop = threading.Event()
        self.connected = False
        # 密钥不对时不要再无限重连（会把对方日志刷爆）
        self._auth_rejected = False
        # 有没有收到过 hello——用来区分"连上过又断了"和"压根没连上"
        self._greeted = False

    def start(self) -> None:
        """Start the worker thread (idempotent)."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="pet-link", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Ask the worker to stop."""
        self._stop.set()

    def send(self, text: str, image: str | None = None) -> None:
        """Queue a message (optionally with a screenshot) for her.

        Args:
            text: What the user typed.
            image: Local image path to attach, when capturing the screen.
        """
        text = (text or "").strip()
        if not text and not image:
            return
        if self._loop is None or self._outbox is None:
            return
        payload = json.dumps(
            {"text": text, "image": image} if image else {"text": text},
            ensure_ascii=False,
        )
        asyncio.run_coroutine_threadsafe(self._outbox.put(payload), self._loop)

    def _run(self) -> None:
        """Own the asyncio loop for this connection (one attempt, no retry)."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._outbox = asyncio.Queue()
        try:
            self._loop.run_until_complete(self._session())
        except Exception as exc:  # noqa: BLE001 - 连不上就提示一次，不重试
            self.connected = False
            if self._auth_rejected:
                self.failed.emit("对方拒绝了连接：密钥不对（或对方要求密钥）")
            else:
                self.failed.emit(
                    f"连不上 AstrBot（{type(exc).__name__}）"
                    "——确认对方开着、地址和密钥对；改好设置后右键→「重连 AstrBot」。",
                )
        finally:
            self.connected = False
            if self._loop is not None:
                self._loop.close()
                self._loop = None

    async def _session(self) -> None:
        """One connection's lifetime (auth handshake first).

        连接结束（无论正常断开还是被拒）就返回，**不在这里重连**。
        """
        import websockets  # 延迟导入：没装 websockets 时 UI 仍能起来

        async with websockets.connect(self.url, max_size=None) as ws:
            # 第一帧必须是认证帧：服务端配了密钥就校验，没配也无所谓
            await ws.send(json.dumps({"type": "auth", "secret": self.secret}))
            self.connected = True
            receiver = asyncio.create_task(self._receive(ws))
            sender = asyncio.create_task(self._send_loop(ws))
            stopper = asyncio.create_task(self._watch_stop())
            _, pending = await asyncio.wait(
                {receiver, sender, stopper},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if getattr(ws, "close_code", None) == 1008:
                self._auth_rejected = True
                self.failed.emit("对方拒绝了连接：密钥不对（或对方要求密钥）")
            elif self._stop.is_set():
                return
            elif self._greeted:
                self.failed.emit(
                    "和 AstrBot 的连接断了（已停止自动重连）"
                    "——右键→「重连 AstrBot」可以再试。",
                )
            else:
                self.failed.emit("桌宠通道连接结束（没收到对方的问候）")

    async def _watch_stop(self) -> None:
        """Wait until the UI asks us to stop."""
        while not self._stop.is_set():
            await asyncio.sleep(0.3)

    async def _send_loop(self, ws) -> None:  # noqa: ANN001 - websockets client
        """Send queued messages.

        Args:
            ws: Open websocket.
        """
        try:
            while True:
                payload = await self._outbox.get()
                try:
                    item = json.loads(payload)
                except ValueError:
                    item = {"text": payload}
                frame: dict = {"type": "message", "text": item.get("text") or ""}
                if item.get("image"):
                    frame["images"] = [item["image"]]
                await ws.send(json.dumps(frame, ensure_ascii=False))
        except Exception:  # noqa: BLE001 - 连接被对方关掉是正常情况，别抛给事件循环
            return

    async def _receive(self, ws) -> None:  # noqa: ANN001 - websockets client
        """Handle her replies.

        Args:
            ws: Open websocket.
        """
        chunks: list[str] = []
        last_data = 0.0
        loop = asyncio.get_running_loop()
        try:
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                except (TimeoutError, asyncio.TimeoutError):
                    if chunks and loop.time() - last_data > IDLE_STOP_SECONDS:
                        self.done.emit("".join(chunks).strip())
                        chunks = []
                    continue
                try:
                    payload = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(payload, dict):
                    continue
                kind = payload.get("type")
                if kind == "hello":
                    self._greeted = True
                    self.ready.emit(
                        f"{payload.get('user_name') or '我'}"
                        f"({payload.get('user_id') or '?'})",
                    )
                elif kind == "reply":
                    text = str(payload.get("text") or "")
                    if text:
                        chunks.append(text)
                        last_data = loop.time()
                        self.chunk.emit(text)
        except Exception:  # noqa: BLE001 - 同上：断开/被拒都当作正常收尾
            return


def test_astrbot(url: str, secret: str = "", timeout: float = 8.0) -> tuple[bool, str]:
    """Check a desktop_pet channel: connect, authenticate, wait for ``hello``.

    Args:
        url: WebSocket address (``ws://host:port``).
        secret: Shared secret, when the remote requires one.
        timeout: Seconds to wait for the greeting.

    Returns:
        ``(ok, message)``.
    """
    import asyncio as _asyncio

    async def run() -> tuple[bool, str]:
        import websockets

        try:
            async with websockets.connect(url, max_size=None, open_timeout=timeout) as ws:
                await ws.send(json.dumps({"type": "auth", "secret": secret}))
                raw = await _asyncio.wait_for(ws.recv(), timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - 任何异常都变成一句人话
            text = f"{type(exc).__name__}: {exc}"
            if "1008" in text or "bad secret" in text:
                return False, "对方拒绝了：密钥不对"
            return False, f"连不上：{text}"
        try:
            data = json.loads(raw)
        except ValueError:
            return False, f"收到非预期内容：{str(raw)[:60]}"
        if isinstance(data, dict) and data.get("type") == "hello":
            return (
                True,
                f"连通成功：{data.get('user_name') or '对方'}"
                f"（{data.get('user_id') or '?'}）",
            )
        return False, f"对方回了 {str(data)[:60]}"

    return _asyncio.run(run())


def selftest(question: str = "桌面通道连上了吗？回一句短的", image: str = "") -> None:
    """Connect, send one message (optionally with an image), print her answer.

    Args:
        question: Message to send.
        image: Local image path to attach (screenshot test).
    """
    import sys

    from PySide6.QtCore import QCoreApplication, QTimer

    app = QCoreApplication(sys.argv)
    client = PetLinkClient()

    def on_done(text: str) -> None:
        print(f"[轻语] {text}")
        client.stop()
        app.quit()

    client.done.connect(on_done)
    client.ready.connect(lambda who: print(f"[通道] 身份 {who}"))
    client.failed.connect(lambda message: print(f"[提示] {message}"))
    if image:
        print(f"[附图] {image}")
    client.start()
    QTimer.singleShot(2500, lambda: client.send(question, image or None))
    QTimer.singleShot(120000, app.quit)
    app.exec()


if __name__ == "__main__":
    import sys

    argv = sys.argv[1:]
    sys.exit(
        selftest(
            argv[0] if argv else "桌宠连上了吗？就回一句短的",
            argv[1] if len(argv) > 1 else "",
        ),
    )
