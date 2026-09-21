"""桌宠聊天：走 AstrBot 面板的 webchat 通道（和 migration_tools/ask_panel.py 同一条路）。

- 后台线程跑 asyncio，通过 Qt 信号把流式片段送回 UI 线程；
- **本地留一份对话记录**（``chat_log.jsonl``）：面板每条 WebSocket 连接都会新开一个会话
  （``live_chat_service.py:157`` 每次连接都生成新的 ``webchat_live!...``），所以她那边记不住
  上下文；桌宠在开新会话的第一句前，会把上次聊过的几句当"回顾"带上，保持连续性。
  阶段 D 换成平台适配器后，会话 id 由我们固定，就不再需要这个补丁；
- 面板通道下 qingyu_core 会跳过（``SKIP_PLATFORMS``），所以这段聊天**不消耗她的精力、
  不涨好感**——那也是阶段 D 才打通的。
"""

import asyncio
import http.cookiejar
import json
import os
import threading
import time
import urllib.error
import urllib.request

import paths
from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

BASE = "http://127.0.0.1:6185/api/v1"
WS_URL = "ws://127.0.0.1:6185/api/v1/unified-chat/ws"
PANEL_USER = "astrbot"
PANEL_PASSWORD = os.environ.get("QINGYU_PANEL_PASSWORD", "")
IDLE_STOP_SECONDS = 6.0

ROOT = paths.BASE
LOG = ROOT / "chat_log.jsonl"
# 回顾时最多带几句、每句截多长
RECAP_TURNS = 3
RECAP_CHARS = 60


def load_history(limit: int = 40) -> list[dict]:
    """Read the local transcript.

    Args:
        limit: How many of the newest turns to return.

    Returns:
        List of ``{"t": timestamp, "q": question, "a": answer}``.
    """
    if not LOG.exists():
        return []
    turns: list[dict] = []
    try:
        for line in LOG.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict) and item.get("q"):
                turns.append(item)
    except OSError:
        return []
    return turns[-limit:]


def save_turn(question: str, answer: str) -> None:
    """Append one exchange to the transcript.

    Args:
        question: What the user said.
        answer: Her reply.
    """
    if not question:
        return
    try:
        with LOG.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {"t": int(time.time()), "q": question, "a": answer},
                    ensure_ascii=False,
                )
                + "\n",
            )
    except OSError:
        pass


def recap_text(history: list[dict]) -> str:
    """Build a short recap of the last exchanges.

    Args:
        history: Turns from :func:`load_history`.

    Returns:
        A one-paragraph recap, or an empty string.
    """
    lines = []
    for turn in history[-RECAP_TURNS:]:
        question = str(turn.get("q", ""))[:RECAP_CHARS]
        answer = str(turn.get("a", ""))[:RECAP_CHARS]
        lines.append(f"我问过「{question}」，你说过「{answer}」")
    if not lines:
        return ""
    return "（接着上次在桌面上的聊天：" + "；".join(lines) + "）"


class ChatClient(QObject):
    """Panel chat client running its own asyncio loop in a worker thread."""

    chunk = Signal(str)
    done = Signal(str)
    failed = Signal(str)
    ready = Signal(str)

    def __init__(self, session_id: str = "", carry_over: bool = True) -> None:
        super().__init__()
        self.session_id = session_id
        self.carry_over = carry_over
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._outbox: asyncio.Queue[str] | None = None
        self._stop = threading.Event()
        self._first_sent = False
        self._last_question = ""

    # ---------------------------------------------------------------- 生命周期

    def start(self) -> None:
        """Start the worker thread (idempotent)."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="pet-chat", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Ask the worker to finish."""
        self._stop.set()

    def send(self, text: str) -> None:
        """Queue a message to send.

        Args:
            text: What the user typed.
        """
        text = (text or "").strip()
        if not text or self._loop is None or self._outbox is None:
            return
        self._last_question = text
        payload = text
        if self.carry_over and not self._first_sent:
            recap = recap_text(load_history())
            if recap:
                payload = f"{recap}\n{text}"
        self._first_sent = True
        asyncio.run_coroutine_threadsafe(self._outbox.put(payload), self._loop)

    # ---------------------------------------------------------------- 线程内部

    def _run(self) -> None:
        """Own the asyncio loop for the whole chat session."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._session())
        except Exception as exc:  # noqa: BLE001 - 任何异常都只报给 UI
            self.failed.emit(f"聊天连接出错：{type(exc).__name__}: {exc}")
        finally:
            self._loop.close()
            self._loop = None

    def _login(self) -> str:
        """Log in to the dashboard and return a bearer token.

        Returns:
            The token.
        """
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        request = urllib.request.Request(
            BASE + "/auth/login",
            data=json.dumps(
                {"username": PANEL_USER, "password": PANEL_PASSWORD}
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
        with opener.open(request, timeout=30) as response:
            return json.loads(response.read().decode())["data"]["token"]

    def _new_session(self, token: str) -> str:
        """Create a fresh panel chat session.

        Args:
            token: Bearer token.

        Returns:
            The new session id.
        """
        request = urllib.request.Request(
            BASE + "/chat/sessions/new",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode()).get("data") or {}
        return str(data.get("session_id") or "")

    async def _session(self) -> None:
        """Connect, bind a session, then pump messages until stopped."""
        import websockets  # 延迟导入：没装 websockets 时 UI 仍能起来

        token = await asyncio.to_thread(self._login)
        if not self.session_id:
            self.session_id = await asyncio.to_thread(self._new_session, token)
        self.ready.emit(self.session_id)
        self._outbox = asyncio.Queue()

        async with websockets.connect(f"{WS_URL}?token={token}", max_size=None) as ws:
            await ws.send(
                json.dumps({"ct": "chat", "t": "bind", "session_id": self.session_id}),
            )
            await asyncio.sleep(0.6)
            receiver = asyncio.create_task(self._receive(ws))
            sender = asyncio.create_task(self._send_loop(ws))
            stopper = asyncio.create_task(self._watch_stop())
            tasks = {receiver, sender, stopper}
            done = set()
            pending = set(tasks)
            try:
                done, pending = await asyncio.wait(
                    tasks,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                # Retrieve done-task exceptions and always tear down the rest,
                # even when _session itself is cancelled from outside.
                for task in done:
                    if not task.cancelled():
                        task.exception()
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
        # No automatic reconnect: stay silent on a user stop, report once otherwise.
        if not self._stop.is_set():
            self.failed.emit(
                "和 AstrBot 面板的连接断了（已停止自动重连）"
                "——右键→「重连 AstrBot」可以再试。",
            )

    async def _watch_stop(self) -> None:
        """Wait until the UI asks us to stop."""
        while not self._stop.is_set():  # noqa: ASYNC110 - poll loop matches original behavior
            await asyncio.sleep(0.3)

    async def _send_loop(self, ws) -> None:  # noqa: ANN001 - websockets client
        """Send queued messages, one at a time.

        Args:
            ws: Open websocket.
        """
        index = 0
        while True:
            text = await self._outbox.get()
            index += 1
            await ws.send(
                json.dumps(
                    {
                        "ct": "chat",
                        "t": "send",
                        "message_id": f"pet-{index}",
                        "message": [{"type": "plain", "text": text}],
                        "flags": {"enable_streaming": True},
                    },
                ),
            )

    async def _receive(self, ws) -> None:  # noqa: ANN001 - websockets client
        """Collect her streamed answer and report it.

        Args:
            ws: Open websocket.
        """
        chunks: list[str] = []
        last_data = 0.0
        loop = asyncio.get_running_loop()
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
            except (TimeoutError, asyncio.TimeoutError):
                if chunks and loop.time() - last_data > IDLE_STOP_SECONDS:
                    answer = "".join(chunks).strip()
                    save_turn(self._last_question, answer)
                    self.done.emit(answer)
                    chunks = []
                continue
            try:
                payload = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            if payload.get("type") == "plain" and isinstance(payload.get("data"), str):
                chunks.append(payload["data"])
                last_data = loop.time()
                self.chunk.emit(payload["data"])
            elif payload.get("type") in {"image", "file", "record", "video"}:
                line = f"[{payload.get('type')}] {str(payload.get('data'))[:120]}"
                chunks.append(line)
                self.chunk.emit(line)
                last_data = loop.time()
            if payload.get("t") in {"end", "done", "finish"} and chunks:
                answer = "".join(chunks).strip()
                save_turn(self._last_question, answer)
                self.done.emit(answer)
                chunks = []


class ChatWindow(QWidget):
    """小聊天窗：上面是记录，下面一行输入。"""

    def __init__(self, client: ChatClient, on_reply=None, on_shot=None) -> None:  # noqa: ANN001
        super().__init__(None)
        self.client = client
        self.on_reply = on_reply
        self.on_shot = on_shot
        self.setWindowTitle("跟轻语说话")
        self.resize(420, 460)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)

        self.view = QTextBrowser(self)
        self.view.setFont(QFont("Microsoft YaHei UI", 10))
        self.view.setOpenExternalLinks(True)
        self.input = QLineEdit(self)
        self.input.setPlaceholderText("说点什么…（回车发送）")
        self.input.returnPressed.connect(self.submit)
        send = QPushButton("发送", self)
        send.clicked.connect(self.submit)
        shot = QPushButton("📷 截图", self)
        shot.setToolTip("抓一张屏幕截图，跟这句话一起发给她")
        shot.clicked.connect(self.send_with_screen)

        row = QHBoxLayout()
        row.addWidget(self.input)
        row.addWidget(shot)
        row.addWidget(send)
        layout = QVBoxLayout(self)
        layout.addWidget(self.view)
        layout.addLayout(row)

        client.chunk.connect(self._on_chunk)
        client.done.connect(self._on_done)
        client.failed.connect(self._on_failed)
        self._pending = ""
        self._streaming = False

    def rebind(self, client: ChatClient) -> None:
        """Swap in a new client (after a manual reconnect) and rewire signals.

        Args:
            client: The new client whose signals the window should follow.
        """
        if self.client is not None:
            for signal in ("chunk", "done", "failed"):
                try:
                    getattr(self.client, signal).disconnect(
                        getattr(self, f"_on_{signal}")
                    )
                except (RuntimeError, TypeError):
                    pass
        self.client = client
        # Drop any half-finished answer from the old client so it cannot leak
        # into the new client's reply.
        self._pending = ""
        self._streaming = False
        client.chunk.connect(self._on_chunk)
        client.done.connect(self._on_done)
        client.failed.connect(self._on_failed)

    def submit(self) -> None:
        """Send whatever is in the input box."""
        text = self.input.text().strip()
        if not text:
            return
        self.input.clear()
        self._append("我", text)
        self._pending = ""
        self.client.send(text)

    def send_with_screen(self) -> None:
        """Attach a fresh screenshot to whatever is in the input box."""
        text = self.input.text().strip()
        self.input.clear()
        if self.on_shot is None:
            self._append("系统", "这条通道不支持截图。")
            return
        self._append("我", f"{text or '（没写字，直接发截图）'}　📷 屏幕截图")
        self._pending = ""
        self.on_shot(text)

    def _append(self, who: str, text: str) -> None:
        """Append one line to the log.

        Args:
            who: Speaker label.
            text: Message text.
        """
        colour = "#2b6cb0" if who == "我" else "#7b341e"
        safe = text.replace("\n", "<br>")
        self.view.append(f'<b style="color:{colour}">{who}：</b>{safe}')
        self.view.verticalScrollBar().setValue(self.view.verticalScrollBar().maximum())

    def _on_chunk(self, text: str) -> None:
        """Stream a piece of her answer into the log.

        Args:
            text: Text fragment.
        """
        self._pending += text
        cursor = self.view.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.view.setTextCursor(cursor)
        if not self._streaming:
            self.view.append('<b style="color:#7b341e">轻语：</b>')
            self._streaming = True
        self.view.insertPlainText(text)
        self.view.verticalScrollBar().setValue(self.view.verticalScrollBar().maximum())
        if self.on_reply:
            self.on_reply(self._pending)

    def _on_done(self, text: str) -> None:
        """Finish the current answer.

        Args:
            text: Full answer text.
        """
        self._streaming = False
        if self.on_reply and text:
            self.on_reply(text)

    def _on_failed(self, message: str) -> None:
        """Show a connection problem.

        Args:
            message: Error text.
        """
        self._append("系统", message)


def selftest(
    question: str = "桌宠连上了吗？就回一句短的", session_id: str = ""
) -> None:
    """Send one message through the panel and print the answer.

    Args:
        question: Message to send.
        session_id: Reuse this会话 id（验证"关掉再打开她是否还记得"）.
    """
    import sys

    from PySide6.QtCore import QCoreApplication

    app = QCoreApplication(sys.argv)
    client = ChatClient(session_id)

    def on_done(text: str) -> None:
        print(f"[轻语] {text}")
        client.stop()
        app.quit()

    client.done.connect(on_done)
    client.failed.connect(lambda message: (print(f"[错误] {message}"), app.quit()))
    client.ready.connect(lambda session: print(f"[会话] {session}"))
    client.start()
    QTimer.singleShot(1500, lambda: client.send(question))
    QTimer.singleShot(90000, app.quit)
    app.exec()


if __name__ == "__main__":
    import sys

    from PySide6.QtCore import QTimer

    args = sys.argv[1:]
    sys.exit(
        selftest(
            args[0] if args else "桌宠连上了吗？就回一句短的",
            args[1] if len(args) > 1 else "",
        )
    )
