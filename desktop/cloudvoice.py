"""云端语音合成（豆包 2.0 双向流式）：**整段送出去、边收边放**。

为什么是这套（2026-09-26 实测定的，替代掉已回滚的本机方案）：

| 指标 | 本机 sherpa（已回滚） | Edge 免费 | **豆包 2.0 双向流式** |
| --- | --- | --- | --- |
| 首包 | 2.0–2.25 s | 1.4 s（冷）/ 0.3 s（热） | **0.41–0.45 s** |
| 停顿自然度 | 差（每句单独合成，有接缝） | 好 | 好（整段一次成） |
| 音色 | 电子味重 | 尚可 | 主人自己复刻的音色 |
| 输出 | 8 k/24 k wav | mp3（要解码） | **裸 PCM，零解码** |

**这一版和回滚那版最大的差别**：**不再分句**。回滚那版为了压低首包把回答切成一句句单独合成，
结果句间韵律断裂（主人原话"断句和停顿不自然"）。现在整段一次送出去，服务端自己处理停顿，
首包依然只有 0.4 秒——两个目标终于同时满足了。

线程模型（回滚那版的教训：合成绝不能占用界面线程）：

- **后台线程**跑一个 asyncio 循环，负责 WebSocket（连接保活、发文本、收 PCM）；
- 收到 PCM 丢进线程安全队列，**界面线程的定时器**把它喂给 `QAudioSink`；
- 两边不共享可变状态，只用队列 + "轮次号"（`_gen`）作废过期数据。

两个容易写错的地方（都按实测/文档处理了）：

1. `QAudioSink.write()` **只会吃掉缓冲区放得下的那部分**，返回值小于传入长度时剩下的必须留着
   下次再喂——直接忽略返回值就会丢音频（听起来像"跳字"）；
2. "服务端发完了"和"声卡放完了"是**两件事**：前者只决定还要不要继续收，
   后者才决定这一轮什么时候结束、麦克风什么时候能重新开。
"""

import asyncio
import json
import queue
import threading
import time
import uuid

from PySide6.QtCore import QObject, QTimer, Signal

try:  # 打包版带了 QtMultimedia；缺了就当"不能出声"，不影响别的功能
    from PySide6.QtMultimedia import QAudioFormat, QAudioSink
except Exception:  # noqa: BLE001 - 可选依赖
    QAudioFormat = None  # type: ignore[assignment]
    QAudioSink = None  # type: ignore[assignment]

import websockets

URL = "wss://openspeech.bytedance.com/api/v3/tts/bidirection"
FULL_CLIENT = 0b0001
FULL_SERVER = 0b1001
AUDIO_ONLY = 0b1011
ERROR_MSG = 0b1111
WITH_EVENT = 0b0100
JSON_SER = 0b0001

EV_START_CONNECTION = 1
EV_FINISH_CONNECTION = 2
EV_CONNECTION_STARTED = 50
EV_CONNECTION_FAILED = 51
EV_CONNECTION_FINISHED = 52
EV_START_SESSION = 100
EV_CANCEL_SESSION = 101
EV_FINISH_SESSION = 102
EV_SESSION_STARTED = 150
EV_SESSION_CANCELED = 151
EV_SESSION_FINISHED = 152
EV_SESSION_FAILED = 153
EV_TASK_REQUEST = 200
EV_TTS_RESPONSE = 352

EVENT_NAMES = {
    # 上行（我们也发这些，名字留着好排查）
    EV_START_CONNECTION: "StartConnection",
    EV_FINISH_CONNECTION: "FinishConnection",
    EV_START_SESSION: "StartSession",
    EV_CANCEL_SESSION: "CancelSession",
    EV_FINISH_SESSION: "FinishSession",
    EV_TASK_REQUEST: "TaskRequest",
    # 下行
    EV_CONNECTION_STARTED: "ConnectionStarted",
    EV_CONNECTION_FAILED: "ConnectionFailed",
    EV_CONNECTION_FINISHED: "ConnectionFinished",
    EV_SESSION_STARTED: "SessionStarted",
    EV_SESSION_CANCELED: "SessionCanceled",
    EV_SESSION_FINISHED: "SessionFinished",
    EV_SESSION_FAILED: "SessionFailed",
    EV_TTS_RESPONSE: "TTSResponse",
}
# 这几个事件不带会话 id（其余带）
NO_SESSION_EVENTS = (EV_CONNECTION_STARTED, EV_CONNECTION_FAILED, EV_CONNECTION_FINISHED)

PUMP_MS = 40
# 一轮"在说"最多多久（秒）：卡住就自愈——否则界面侧拿"在说"当"别开麦"的理由，麦克风永远开不起来
SPEAK_TIMEOUT_SECONDS = 120.0
# 音频已经放完、但服务端迟迟不发"会话结束"时的宽限（秒）——到点就当这一段结束
IDLE_GRACE_SECONDS = 2.0
# 一次请求最多多少字（服务端对文本长度有限制；只有超长回答才会切块）
MAX_CHUNK_CHARS = 300
CONNECT_TIMEOUT = 20


def header_bytes(message_type: int, flags: int = WITH_EVENT) -> bytes:
    """Build the 4-byte protocol header.

    Args:
        message_type: Message type nibble.
        flags: Message-type specific flags nibble.

    Returns:
        Four bytes.
    """
    return bytes([(0b0001 << 4) | 0b0001, (message_type << 4) | flags, JSON_SER << 4, 0])


def optional_bytes(event: int, session: str | None = None) -> bytes:
    """Encode the optional section (event number + optional session id).

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


def build_frame(event: int, body: dict | None = None, session: str | None = None) -> bytes:
    """Build one client frame.

    Args:
        event: Event number.
        body: JSON body (``None`` sends ``{}``).
        session: Session id for the events that need one.

    Returns:
        The frame bytes.
    """
    raw = json.dumps(body if body is not None else {}, ensure_ascii=False).encode()
    return (
        header_bytes(FULL_CLIENT)
        + optional_bytes(event, session)
        + len(raw).to_bytes(4, "big", signed=True)
        + raw
    )


def parse_frame(message: bytes) -> dict:
    """Parse one server frame.

    Args:
        message: Raw frame.

    Returns:
        ``{"type", "event", "name", "session", "payload"}``（解析不动的地方留空）。
    """
    info = {
        "type": (message[1] >> 4) if len(message) > 1 else -1,
        "event": 0,
        "name": "",
        "session": "",
        "payload": b"",
    }
    offset = 4
    if len(message) > 1 and (message[1] & 0x0F) == WITH_EVENT and offset + 4 <= len(message):
        info["event"] = int.from_bytes(message[offset : offset + 4], "big", signed=True)
        info["name"] = EVENT_NAMES.get(info["event"], str(info["event"]))
        offset += 4
        if info["event"] not in NO_SESSION_EVENTS and offset + 4 <= len(message):
            size = int.from_bytes(message[offset : offset + 4], "big", signed=True)
            offset += 4
            if 0 < size <= len(message) - offset:
                info["session"] = message[offset : offset + size].decode("utf-8", "replace")
                offset += size
    if offset + 4 <= len(message):
        size = int.from_bytes(message[offset : offset + 4], "big", signed=True)
        if 0 <= size <= len(message) - offset - 4:
            info["payload"] = message[offset + 4 : offset + 4 + size]
    return info


def split_for_api(text: str, limit: int = MAX_CHUNK_CHARS) -> list[str]:
    """Cut a very long reply into API-sized pieces.

    Args:
        text: Her reply.
        limit: Max characters per request.

    Returns:
        One or more pieces; a normal reply stays in **one** piece
        （分句是回滚那版失败的原因，所以这里只在超过长度上限时才切）。
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    pieces: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = max((rest.rfind(char, 0, limit) for char in "。！？!?；;…\n"), default=-1)
        if cut <= 0:
            cut = limit - 1
        pieces.append(rest[: cut + 1].strip())
        rest = rest[cut + 1 :].strip()
    if rest:
        pieces.append(rest)
    return pieces


def should_speak(text: str) -> bool:
    """Should this bubble be read out loud?

    为什么要有这个判断（而不是"全部念"或"全部不念"）：

    - **回声不念**：气泡里"你：「…」"是把她听到的话显示给主人看，念出来等于她重复你的话；
    - **进度不念**：以"…"结尾或带百分号的都是进度/等待提示（"我查查天气…"、"下载 47%"），
      念出来只会打断她自己的话；
    - **技术细节不念**：路径、config.json 这类是给主人排错看的，读出来又长又难听；
    - 其余的（菜单结果、她主动说的、应答词）都念——这就是"所有按钮都有语音"。

    Args:
        text: The bubble text.

    Returns:
        True when it is worth speaking.
    """
    text = (text or "").strip()
    if not text:
        return False
    if text.startswith(("你：「", "你说的：「", "（记下了", "先听", "先不")):
        return False
    # 进度/等待类：中文省略号或百分号一出现就认定是"过程提示"，不念。
    # （她的正式回答不走这个判断：那条路是显式念的，所以正文里有"……"也不会被漏掉。）
    if "…" in text or "%" in text or text.endswith("..."):
        return False
    if any(
        marker in text
        for marker in (
            "config.json",
            "\\",
            "说不出话",
            "上一条还在",
            "缓一下",
            # 异常/技术细节念出来又长又难听（"本机识别模块不可用：ImportError"）
            "Error",
            "Exception",
            "Traceback",
            "不可用",
        )
    ):
        return False
    return True


class CloudVoice(QObject):
    """Speak her replies through the cloud, streaming PCM straight to the speaker."""

    started = Signal()
    finished = Signal()
    failed = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        """Set up an idle client (nothing connects until the first line).

        Args:
            parent: Qt parent.
        """
        super().__init__(parent)
        self.api_key = ""
        self.resource_id = "seed-tts-2.0"
        self.voice = ""
        self.speech_rate = 0
        self.loudness_rate = 0
        self.sample_rate = 24000
        self._queue: queue.Queue = queue.Queue()
        self._gen = 0
        self._texts: list[str] = []
        self._index = 0
        self._piece_done = False
        self._pending = bytearray()
        self._sink = None
        self._io = None
        self._sink_bytes = 0
        self._last_audio_at = 0.0
        self._spoke = False
        self._round_started = 0.0
        self._usage = ""
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws = None
        self._ready = threading.Event()
        self._stop_flag = threading.Event()
        self.timer = QTimer(self)
        self.timer.setInterval(PUMP_MS)
        self.timer.timeout.connect(self._pump)

    # ---------------------------------------------------------------- 配置

    def configure(self, config: dict) -> None:
        """Read the ``tts`` config block.

        Args:
            config: The pet's ``tts`` dict (api_key / voice / ...).
        """
        self.api_key = str(config.get("api_key") or "").strip()
        self.resource_id = str(config.get("resource_id") or "seed-tts-2.0").strip()
        self.voice = str(config.get("voice") or "").strip()
        self.speech_rate = int(config.get("speech_rate") or 0)
        self.loudness_rate = int(config.get("loudness_rate") or 0)
        rate = int(config.get("sample_rate") or 24000)
        self.sample_rate = rate if rate in (16000, 22050, 24000, 32000, 44100, 48000) else 24000

    def available(self) -> bool:
        """Can she speak with the current settings?

        Returns:
            True when QtMultimedia is present and both key and voice are configured.
        """
        return bool(QAudioSink is not None and self.api_key and self.voice)

    def describe(self) -> str:
        """One line about the current voice, for the log.

        Returns:
            Chinese description.
        """
        if not self.api_key:
            return "还没填豆包语音 Key（tts.api_key）"
        if not self.voice:
            return "还没选音色（tts.voice）"
        return f"{self.voice}（{self.resource_id}，{self.sample_rate} Hz）"

    def last_usage(self) -> str:
        """What the server said this round cost.

        Returns:
            e.g. ``{"usage":{"text_words":14}}`` or an empty string.
        """
        return self._usage

    def is_speaking(self) -> bool:
        """Is sound coming out right now (or about to)?

        Returns:
            True between ``speak()`` and the end of the last chunk.
        """
        return self._sink is not None or self._index < len(self._texts) or self._piece_done

    # ---------------------------------------------------------------- 对外

    def speak(self, text: str) -> bool:
        """Start speaking one reply (any previous one is cut off).

        Args:
            text: Her reply.

        Returns:
            True when something was queued to say.
        """
        pieces = split_for_api(text)
        if not pieces or not self.available():
            return False
        self.stop()
        self._gen += 1
        self._texts = pieces
        self._index = 0
        self._piece_done = False
        self._spoke = False
        self._usage = ""
        self._last_audio_at = 0.0
        self._round_started = time.monotonic()
        self._start_worker()
        self._send(self._gen, pieces[0])
        self.timer.start()
        return True

    def stop(self) -> None:
        """Cut off whatever is being said and forget this round."""
        self._gen += 1
        self._texts = []
        self._index = 0
        self._piece_done = False
        self._pending.clear()
        self._round_started = 0.0
        self._last_audio_at = 0.0
        self._close_sink()
        self._queue = queue.Queue()
        self.timer.stop()

    def shutdown(self) -> None:
        """Close the background connection (called when the pet exits)."""
        self.stop()
        self._stop_flag.set()
        loop = self._loop
        if loop is not None and loop.is_running():
            asyncio.run_coroutine_threadsafe(self._close_connection(), loop)

    # ---------------------------------------------------------------- 后台线程

    def _start_worker(self) -> None:
        """Start the asyncio thread on first use."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_flag.clear()
        self._ready.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="qingyu-tts")
        self._thread.start()
        self._ready.wait(timeout=5)

    def _run_loop(self) -> None:
        """Own an asyncio loop for the websocket inside this thread."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._ready.set()
        try:
            loop.run_until_complete(self._worker())
        except Exception as exc:  # noqa: BLE001 - 线程里不该把异常吞掉
            self._queue.put(("error", f"语音线程结束：{type(exc).__name__}: {exc}"))
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()

    def _send(self, gen: int, text: str) -> None:
        """Hand one text to the worker.

        Args:
            gen: Round number.
            text: Text to speak.
        """
        loop = self._loop
        if loop is None or not loop.is_running():
            self._queue.put(("error", "语音线程没起来"))
            return
        asyncio.run_coroutine_threadsafe(self._speak_one(gen, text), loop)

    async def _connect(self) -> bool:
        """Make sure we have a live websocket (kept warm between replies).

        Returns:
            True when connected.
        """
        ws = self._ws
        if ws is not None and getattr(getattr(ws, "state", None), "name", "") == "OPEN":
            return True
        headers = {
            "X-Api-Key": self.api_key,
            "X-Api-Resource-Id": self.resource_id,
            "X-Api-Connect-Id": str(uuid.uuid4()),
            # 让服务端把"这次扣了多少字"一起报回来
            "X-Control-Require-Usage-Tokens-Return": "*",
        }
        try:
            ws = await websockets.connect(
                URL,
                additional_headers=headers,
                max_size=1 << 30,
                open_timeout=CONNECT_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001 - 交给界面报错
            status = getattr(getattr(exc, "response", None), "status_code", None)
            hint = "（Key 不对、或这个 Key 没开通该资源）" if status in (401, 403) else ""
            self._queue.put(
                ("error", f"连不上豆包语音：{type(exc).__name__} {status or ''}{hint}")
            )
            return False
        self._ws = ws
        await ws.send(build_frame(EV_START_CONNECTION))
        while True:
            info = parse_frame(await asyncio.wait_for(ws.recv(), timeout=CONNECT_TIMEOUT))
            if info["event"] == EV_CONNECTION_FAILED:
                self._queue.put(
                    ("error", "建连被拒：" + info["payload"].decode("utf-8", "replace")[:160])
                )
                await self._close_connection()
                return False
            if info["event"] == EV_CONNECTION_STARTED:
                return True

    async def _close_connection(self) -> None:
        """Close the websocket if it is open."""
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.send(build_frame(EV_FINISH_CONNECTION))
                await ws.close()
            except Exception:  # noqa: BLE001 - 关不掉就算了
                pass

    async def _worker(self) -> None:
        """Idle until stopped (each reply runs in its own ``_speak_one``)."""
        while not self._stop_flag.is_set():
            await asyncio.sleep(0.5)
        await self._close_connection()

    async def _speak_one(self, gen: int, text: str) -> None:
        """Send one text and stream the audio back into the queue.

        Args:
            gen: Round number (a newer round makes this one give up).
            text: Text to speak.
        """
        if not await self._connect():
            return
        ws = self._ws
        session = uuid.uuid4().hex
        params = {
            "speaker": self.voice,
            # 流式推荐 pcm：连解码都省了
            "audio_params": {"format": "pcm", "sample_rate": self.sample_rate},
            "additions": "{}",
            "speech_rate": self.speech_rate,
            "loudness_rate": self.loudness_rate,
            # true = 去掉 markdown / emoji（否则 "**你好**" 会被念成"星星你好星星"）
            "disable_markdown_filter": True,
            "disable_emoji_filter": True,
        }
        try:
            await ws.send(
                build_frame(
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
                build_frame(
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
            await ws.send(build_frame(EV_FINISH_SESSION, None, session))
        except Exception as exc:  # noqa: BLE001
            self._queue.put(("error", f"发送失败：{type(exc).__name__}: {exc}"))
            await self._close_connection()
            return

        while True:
            if gen != self._gen:
                try:  # 被打断：让服务端别算了
                    await ws.send(build_frame(EV_CANCEL_SESSION, None, session))
                except Exception:  # noqa: BLE001
                    pass
                return
            try:
                message = await asyncio.wait_for(ws.recv(), timeout=30)
            except Exception as exc:  # noqa: BLE001
                self._queue.put(("error", f"收音频失败：{type(exc).__name__}: {exc}"))
                await self._close_connection()
                return
            info = parse_frame(message)
            if info["type"] == ERROR_MSG:
                self._queue.put(
                    ("error", "服务端错误：" + info["payload"].decode("utf-8", "replace")[:160])
                )
                return
            if info["event"] == EV_SESSION_FAILED:
                self._queue.put(
                    ("error", "合成失败：" + info["payload"].decode("utf-8", "replace")[:160])
                )
                return
            if info["event"] == EV_TTS_RESPONSE and info["payload"]:
                self._queue.put(("audio", info["payload"]))
            elif info["event"] == EV_SESSION_FINISHED:
                if info["payload"]:
                    # 服务端在这里回报本次计费字数（usage.text_words）
                    self._queue.put(("usage", info["payload"].decode("utf-8", "replace")))
                self._queue.put(("done", ""))
                return

    # ---------------------------------------------------------------- 界面线程

    def _pump(self) -> None:
        """Move data from the queue to the speaker, and end the round when done."""
        if self._round_started and time.monotonic() - self._round_started > SPEAK_TIMEOUT_SECONDS:
            self._round_started = 0.0
            self.failed.emit(f"一轮念了超过 {SPEAK_TIMEOUT_SECONDS:.0f} 秒，先停下")
            self.stop()
            return
        while True:
            try:
                kind, payload = self._queue.get_nowait()
            except queue.Empty:
                break
            if kind == "error":
                self.failed.emit(str(payload))
                self.stop()
                return
            if kind == "usage":
                self._usage = str(payload)
                continue
            if kind == "audio":
                self._pending.extend(payload)
                self._last_audio_at = time.monotonic()
                continue
            if kind == "done":
                self._piece_done = True
                continue
        self._feed()
        self._advance_if_done()

    def _feed(self) -> None:
        """Write as much pending PCM into the sink as it will take."""
        if self._pending:
            if self._sink is None and not self._open_sink():
                return
            free = self._sink.bytesFree()
            if free > 0 and self._io is not None:
                take = min(free, len(self._pending))
                # write() 只吃放得下的部分，剩下的留着下次喂（不然会丢音频）
                self._io.write(bytes(self._pending[:take]))
                del self._pending[:take]
                self._sink_bytes += take
                if not self._spoke:
                    self._spoke = True
                    self.started.emit()
            return
        if self._sink is None:
            return
        # 数据喂完了 → 判断"这一段的声音是不是放完了"。**不能只看 IdleState**：
        # 耳机被拔掉/输出设备切换时，声卡可能永远不回到空闲（2026-09-27 主人的日志里
        # 就卡过一轮，靠 120 秒自愈阀才停下）。所以再拿 processedUSecs 对一下账：
        # 已经播过的时间 >= 这一轮写进去的音频时长，就认定放完了。
        state = getattr(self._sink.state(), "name", "")
        played_us = getattr(self._sink, "processedUSecs", lambda: 0)()
        expected_us = self._sink_bytes / (self.sample_rate * 2) * 1_000_000
        if state == "IdleState" or (expected_us and played_us >= expected_us - 100_000):
            self._close_sink()

    def _advance_if_done(self) -> None:
        """Move to the next piece, or wrap up the round."""
        if self._sink is not None or self._pending:
            return
        # 正常的收尾是服务端发"会话结束"；但它偶尔不发（或socket静默掉了），
        # 这时只要音频早就放完、也没再来新数据，就按"这一段结束"处理——
        # 否则界面侧会一直以为"她在说"，麦克风永远不开（2026-09-27 的卡死就是这种）。
        if not self._piece_done:
            if not self._last_audio_at or time.monotonic() - self._last_audio_at < IDLE_GRACE_SECONDS:
                return
            self._piece_done = True
        if self._index < len(self._texts):
            self._index += 1
        self._piece_done = False
        if self._index < len(self._texts):
            self._send(self._gen, self._texts[self._index])
        else:
            self._finish_round()

    def _open_sink(self) -> bool:
        """Create the audio sink for the configured sample rate.

        Returns:
            True when the sink is ready.
        """
        fmt = QAudioFormat()
        fmt.setSampleRate(self.sample_rate)
        fmt.setChannelCount(1)
        fmt.setSampleFormat(QAudioFormat.Int16)
        try:
            self._sink = QAudioSink(fmt)
        except Exception as exc:  # noqa: BLE001 - 没声卡时给句明白话
            self.failed.emit(f"打不开播放设备：{type(exc).__name__}: {exc}")
            self.stop()
            return False
        error = self._sink.error()
        if getattr(error, "name", "NoError") != "NoError":
            self.failed.emit(f"播放设备有问题：{getattr(error, 'name', error)}")
            self.stop()
            return False
        self._io = self._sink.start()
        self._sink_bytes = 0
        return True

    def _close_sink(self) -> None:
        """Stop and drop the sink (打断时要立刻安静，不留尾巴）。"""
        sink, self._sink = self._sink, None
        self._io = None
        if sink is not None:
            sink.stop()

    def _finish_round(self) -> None:
        """Wrap up a round that finished on its own."""
        self.timer.stop()
        self._texts = []
        self._index = 0
        self._piece_done = False
        self._round_started = 0.0
        if self._spoke:
            self._spoke = False
            self.finished.emit()
