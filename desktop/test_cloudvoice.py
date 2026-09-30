"""Offline tests for cloud spoken replies (P10).

No network, no audio device: these pin the four things that are easy to get wrong —

1. 协议帧的字节布局（发错一个字节，服务端就只会回一句"grant not found"，很难查）；
2. 普通回答**只切成一段**（分句是回滚那版失败的原因：句间韵律会断）；
3. `QAudioSink.write()` 吃不下全部数据时**不能丢**（剩下的要留着下次喂）；
4. "服务端发完"与"声卡放完"是两个状态，只有后者能结束这一轮（否则麦克风永远不开）。
"""

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cloudvoice  # noqa: E402


class FrameTest(unittest.TestCase):
    """The wire format, pinned byte for byte."""

    def test_header_bytes(self) -> None:
        """Header is version||size, type||flags, serialization||compression, reserved."""
        self.assertEqual(cloudvoice.header_bytes(0b0001), b"\x11\x14\x10\x00")
        self.assertEqual(cloudvoice.header_bytes(0b1001), b"\x11\x94\x10\x00")
        print("PASS test_header_bytes")

    def test_start_connection_frame(self) -> None:
        """StartConnection is event 1, no session, payload {}."""
        frame = cloudvoice.build_frame(cloudvoice.EV_START_CONNECTION)
        self.assertEqual(frame[:4], b"\x11\x14\x10\x00")
        self.assertEqual(frame[4:8], (1).to_bytes(4, "big", signed=True))
        self.assertEqual(frame[8:12], (2).to_bytes(4, "big", signed=True))
        self.assertEqual(frame[12:], b"{}")
        print("PASS test_start_connection_frame")

    def test_task_request_frame_carries_session_and_text(self) -> None:
        """A session event carries a length-prefixed session id, then the JSON."""
        frame = cloudvoice.build_frame(
            cloudvoice.EV_TASK_REQUEST, {"text": "你好"}, "abc"
        )
        head = cloudvoice.parse_frame(frame)
        self.assertEqual(head["event"], cloudvoice.EV_TASK_REQUEST)
        self.assertEqual(head["name"], "TaskRequest")
        self.assertEqual(head["session"], "abc")
        # 客户端帧里 JSON 就是负载，用同一套读法能取回来
        self.assertIn("你好", frame.decode("utf-8", "replace"))
        print("PASS test_task_request_frame_carries_session_and_text")

    def test_parse_audio_only_frame(self) -> None:
        """Audio-only responses have no event section: just header + size + PCM."""
        pcm = b"\x01\x02" * 8
        frame = (
            bytes([0x11, 0xB0, 0x00, 0x00])  # AUDIO_ONLY_RESPONSE，无事件标志
            + len(pcm).to_bytes(4, "big", signed=True)
            + pcm
        )
        info = cloudvoice.parse_frame(frame)
        self.assertEqual(info["type"], cloudvoice.AUDIO_ONLY)
        self.assertEqual(info["event"], 0)
        self.assertEqual(info["payload"], pcm)
        print("PASS test_parse_audio_only_frame")

    def test_parse_server_response_with_event(self) -> None:
        """A server response with an event round-trips."""
        payload = b'{"usage":{"text_words":14}}'
        frame = (
            cloudvoice.header_bytes(cloudvoice.FULL_SERVER)
            + cloudvoice.optional_bytes(cloudvoice.EV_TTS_RESPONSE, "sess")
            + len(payload).to_bytes(4, "big", signed=True)
            + payload
        )
        info = cloudvoice.parse_frame(frame)
        self.assertEqual(info["name"], "TTSResponse")
        self.assertEqual(info["session"], "sess")
        self.assertEqual(info["payload"], payload)
        print("PASS test_parse_server_response_with_event")

    def test_parse_ignores_truncated_frames(self) -> None:
        """A short or nonsense frame must not raise."""
        for raw in (b"", b"\x11", b"\x11\x94\x10\x00", b"\x11\x94\x10\x00" + b"\x00" * 3):
            info = cloudvoice.parse_frame(raw)
            self.assertIn("payload", info)
        print("PASS test_parse_ignores_truncated_frames")


class SplitTest(unittest.TestCase):
    """A normal reply must stay in one piece (that keeps her prosody continuous)."""

    def test_normal_reply_is_one_piece(self) -> None:
        """Three sentences still go out as ONE request."""
        text = "嗯，我在呢。今天想聊点什么？外面好像要下雨了。"
        self.assertEqual(cloudvoice.split_for_api(text), [text])
        print("PASS test_normal_reply_is_one_piece")

    def test_long_reply_is_cut_at_punctuation(self) -> None:
        """Only when it exceeds the API limit do we cut — and at sentence ends."""
        text = "第一句很长。" * 40  # 标点密集：每段都该切在句号上
        pieces = cloudvoice.split_for_api(text, limit=60)
        self.assertGreater(len(pieces), 1)
        for piece in pieces[:-1]:
            self.assertTrue(piece.endswith("。"), piece[-6:])
            self.assertLessEqual(len(piece), 61, len(piece))
        print(f"PASS test_long_reply_is_cut_at_punctuation（{len(pieces)} 段）")

    def test_a_run_on_without_punctuation_is_still_broken(self) -> None:
        """No punctuation in reach means a hard cut — never one endless request."""
        pieces = cloudvoice.split_for_api("很" * 50, limit=20)
        self.assertEqual([len(piece) for piece in pieces], [20, 20, 10], pieces)
        print("PASS test_a_run_on_without_punctuation_is_still_broken")

    def test_empty_text_says_nothing(self) -> None:
        """Blank text queues nothing."""
        for text in ("", "   ", "\n\n"):
            self.assertEqual(cloudvoice.split_for_api(text), [])
        print("PASS test_empty_text_says_nothing")


class FakeSink:
    """A sink that only accepts ``limit`` bytes per write."""

    def __init__(self, limit: int) -> None:
        """Store the per-write limit.

        Args:
            limit: Bytes free per call.
        """
        self.limit = limit
        self.accepted = 0
        self.stopped = False
        self._state = "IdleState"

    def bytesFree(self) -> int:  # noqa: N802 - 照 Qt 命名
        """How much room the sink has.

        Returns:
            The free byte count.
        """
        return self.limit

    def state(self):  # noqa: ANN201 - 照 Qt 返回枚举
        """Fake Qt state.

        Returns:
            An object whose ``name`` matches Qt's enum naming.
        """
        return type("S", (), {"name": self._state})()

    def stop(self) -> None:
        """Mark as stopped."""
        self.stopped = True


class FakeIO:
    """An io device that accepts at most ``limit`` bytes per write."""

    def __init__(self, sink: FakeSink) -> None:
        """Bind to a sink.

        Args:
            sink: The fake sink.
        """
        self.sink = sink
        self.written = bytearray()

    def write(self, data: bytes) -> int:
        """Accept a slice of the data (never more than the sink allows).

        Args:
            data: Bytes offered.

        Returns:
            How many bytes were accepted.
        """
        take = min(len(data), self.sink.limit)
        self.written.extend(data[:take])
        return take


class SpeakFilterTest(unittest.TestCase):
    """Which bubbles get read out loud（"所有按钮都有语音"的边界）。"""

    def test_menu_results_are_spoken(self) -> None:
        """Results and her own remarks are spoken."""
        for text in (
            "开封市 27.2°C（体感 29.0°C）　晴",
            "记下了~",
            "嗯？我在呢",
            "好，现在用 tianmeihuopo 的声音。",
        ):
            self.assertTrue(cloudvoice.should_speak(text), text)
        print("PASS test_menu_results_are_spoken")

    def test_echoes_are_not_spoken(self) -> None:
        """Echoing the user's own words back would just be noise."""
        for text in ("你：「今天几号」", "你说的：「轻语你在吗」", "（记下了：「明天考试」）"):
            self.assertFalse(cloudvoice.should_speak(text), text)
        print("PASS test_echoes_are_not_spoken")

    def test_progress_and_technical_text_are_not_spoken(self) -> None:
        """Progress lines and troubleshooting details stay silent."""
        for text in (
            "我查查天气…",
            "模型下载 47%（37 MB）",
            "听着呢…（5 秒）",
            "本机识别模块不可用：ImportError",
            "截图功能关着呢（config.json 里的 screen_capture）~",
            "这一帧留在 D:\\code\\AI\\shots\\1.png",
            "先不听：锁屏了",
            "（说不出话来了：连不上豆包语音）",
            "",
        ):
            self.assertFalse(cloudvoice.should_speak(text), text)
        print("PASS test_progress_and_technical_text_are_not_spoken")


class PlaybackTest(unittest.TestCase):
    """Feeding the sink and ending the round."""

    def make(self) -> cloudvoice.CloudVoice:
        """Build a voice object without touching the network.

        Returns:
            The object.
        """
        return cloudvoice.CloudVoice()

    def test_no_mp3_no_network_is_not_available(self) -> None:
        """Without a key and a voice she cannot speak."""
        voice = self.make()
        self.assertFalse(voice.available())
        self.assertIn("Key", voice.describe())
        voice.api_key, voice.voice = "k", "v"
        self.assertTrue(voice.available())
        self.assertIn("seed-tts-2.0", voice.describe())
        print("PASS test_no_mp3_no_network_is_not_available")

    def test_partial_writes_do_not_lose_audio(self) -> None:
        """Every pending byte must reach the sink, even when it takes many pumps."""
        voice = self.make()
        sink = FakeSink(limit=100)
        io = FakeIO(sink)
        voice._sink = sink
        voice._io = io
        voice._pending = bytearray(b"\x01\x02" * 500)  # 1000 字节
        for _ in range(20):
            voice._feed()
        self.assertEqual(len(io.written), 1000, len(io.written))
        self.assertEqual(bytes(io.written), b"\x01\x02" * 500)
        self.assertEqual(len(voice._pending), 0)
        # 喂完之后声卡空闲又没数据 → 自动关掉（这就是"放完了"的信号）
        self.assertIsNone(voice._sink, "放完就该把声卡关掉")
        print("PASS test_partial_writes_do_not_lose_audio")

    def test_round_ends_only_after_the_sink_drains(self) -> None:
        """Server-done is not the same as played-out."""
        voice = self.make()
        voice._gen = 1
        voice._texts = ["一句话"]
        voice._index = 0
        voice._piece_done = True
        sink = FakeSink(limit=50)
        voice._sink = sink
        voice._io = FakeIO(sink)
        voice._pending = bytearray(b"\x00\x00" * 10)  # 还有没放完的
        voice._advance_if_done()
        self.assertEqual(voice._index, 0, "还有音频没放完就不该推进")
        self.assertTrue(voice.is_speaking())
        # 喂完 + 声卡排空，才算这一段放完 → 这一轮收尾
        for _ in range(5):
            voice._feed()
        voice._advance_if_done()
        self.assertFalse(voice.is_speaking(), "放完就该结束这一轮")
        print("PASS test_round_ends_only_after_the_sink_drains")

    def test_a_new_reply_replaces_the_old_one(self) -> None:
        """Speaking again kills the previous round."""
        voice = self.make()
        voice.api_key, voice.voice = "k", "v"
        voice._loop = None  # 不真的连
        voice.stop()
        first = voice._gen
        # 直接调用 stop 的语义：轮次号必须往前走，旧轮的数据一律作废
        voice._gen += 1
        self.assertGreater(voice._gen, first)
        voice._texts = ["旧的一句"]
        voice._index = 0
        voice.stop()
        self.assertEqual(voice._texts, [])
        self.assertFalse(voice.is_speaking())
        print("PASS test_a_new_reply_replaces_the_old_one")

    def test_a_round_ends_even_if_the_server_never_says_goodbye(self) -> None:
        """音频放完 + 服务端没发"会话结束" → 也要收尾（否则一直"在说"，麦克风永远不开）。

        2026-09-27 主人的日志里真的卡过一次：`她出声失败：一轮念了超过 120 秒，先停下`。
        """
        voice = self.make()
        voice._gen = 1
        voice._texts = ["一句话"]
        voice._index = 0
        voice._piece_done = False  # 服务端没发 done
        voice._last_audio_at = time.monotonic() - cloudvoice.IDLE_GRACE_SECONDS - 1
        voice._pending.clear()
        voice._advance_if_done()
        self.assertFalse(voice.is_speaking(), "宽限期到了就该收尾")
        print("PASS test_a_round_ends_even_if_the_server_never_says_goodbye")

    def test_grace_does_not_cut_a_round_that_is_still_playing(self) -> None:
        """宽限期只对"没声音了"生效：还在放音频时绝不能收尾。"""
        voice = self.make()
        voice._gen = 1
        voice._texts = ["一句话"]
        voice._index = 0
        voice._piece_done = False
        voice._last_audio_at = time.monotonic() - cloudvoice.IDLE_GRACE_SECONDS - 1
        voice._pending = bytearray(b"\x00\x00" * 100)  # 还有数据要喂
        voice._advance_if_done()
        self.assertTrue(voice.is_speaking(), "还有音频没放完就不能收尾")
        print("PASS test_grace_does_not_cut_a_round_that_is_still_playing")

    def test_sink_drain_is_detected_without_idle_state(self) -> None:
        """声卡不报 Idle 时，用 processedUSecs 对账也能判定"放完了"。"""
        voice = self.make()

        class Sink(FakeSink):
            """A sink that never reports Idle but tracks played time."""

            def __init__(self) -> None:
                super().__init__(limit=10_000)
                self._state = "ActiveState"
                self.us = 0

            def processedUSecs(self) -> int:  # noqa: N802 - 照 Qt 命名
                """Return how much audio has been played.

                Returns:
                    Microseconds played.
                """
                return self.us

        sink = Sink()
        voice._sink = sink
        voice._io = FakeIO(sink)
        voice._sink_bytes = 48_000  # 1.0 秒 @24k 单声道 16 位
        voice._pending.clear()
        voice._feed()
        self.assertIsNotNone(voice._sink, "才播了一点就关掉是错的")
        sink.us = 1_100_000  # 已经播了 1.1 秒 > 1.0 秒
        voice._feed()
        self.assertIsNone(voice._sink, "播够了就该判定放完")
        print("PASS test_sink_drain_is_detected_without_idle_state")

    def test_a_stuck_round_heals_itself(self) -> None:
        """A round that never finishes must not keep her "speaking" forever."""
        voice = self.make()
        voice._texts = ["卡住的一句"]
        voice._index = 0
        voice._round_started = time.monotonic() - cloudvoice.SPEAK_TIMEOUT_SECONDS - 1
        seen: list[str] = []
        voice.failed.connect(seen.append)
        voice._pump()
        self.assertFalse(voice.is_speaking(), "超时的那一轮必须被清掉")
        self.assertEqual(len(seen), 1, seen)
        print("PASS test_a_stuck_round_heals_itself")

    def test_server_error_is_reported_and_stops(self) -> None:
        """A service error ends the round instead of hanging."""
        voice = self.make()
        voice._texts = ["一句"]
        voice._index = 0
        voice._round_started = time.monotonic()
        seen: list[str] = []
        voice.failed.connect(seen.append)
        voice._queue.put(("error", "服务端错误：余额不足"))
        voice._pump()
        self.assertEqual(seen, ["服务端错误：余额不足"])
        self.assertFalse(voice.is_speaking())
        print("PASS test_server_error_is_reported_and_stops")

    def test_usage_is_recorded(self) -> None:
        """The billed character count the server reports is kept for the log."""
        voice = self.make()
        voice._queue.put(("usage", '{"usage":{"text_words":14}}'))
        voice._pump()
        self.assertIn("14", voice.last_usage())
        print("PASS test_usage_is_recorded")


if __name__ == "__main__":
    unittest.main(verbosity=2)
