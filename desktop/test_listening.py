"""Offline tests for conversation mode (continuous listening).

No microphone, no model, no Qt event loop: the VAD is faked so the *decision* logic is what
gets tested — when we are allowed to listen, how frames are cut into sentences, when a quiet
segment is thrown away, and how the downloaded model is verified.
"""

import shutil
import struct
import sys
import unittest
import urllib.error
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import listening  # noqa: E402
import model_store  # noqa: E402


def floats(count: int, value: float = 0.5) -> list[float]:
    """Build a list of identical float samples.

    Args:
        count: How many samples.
        value: Sample value.

    Returns:
        The sample list.
    """
    return [value] * count


def pcm16(values) -> bytes:  # noqa: ANN001 - 数值列表
    """Pack sample values into raw Int16 bytes.

    Args:
        values: Integer sample values.

    Returns:
        Packed bytes.
    """
    return b"".join(struct.pack("<h", value) for value in values)


class FakeVad:
    """A VAD stub that closes a segment every ``every`` accepted frames."""

    def __init__(self, every: int = 4, segment_frames: int = 4) -> None:
        self.every = every
        self.segment_frames = segment_frames
        self.accepted = 0
        self._ready: list = []

    def accept_waveform(self, frame) -> None:  # noqa: ANN001
        """Count frames and occasionally declare one finished."""
        self.accepted += 1
        if self.accepted % self.every == 0:
            self._ready.append(list(frame) * self.segment_frames)

    def empty(self) -> bool:
        """Is there no finished segment waiting?"""
        return not self._ready

    @property
    def front(self):  # noqa: ANN201
        """The oldest finished segment."""
        return type("Seg", (), {"samples": self._ready[0], "start": 0})()

    def pop(self) -> None:
        """Drop the oldest finished segment."""
        self._ready.pop(0)


class ShouldListenTest(unittest.TestCase):
    """Every gate in the always-on listener, one at a time."""

    def base(self, **overrides):
        """Default allowed kwargs with overrides applied."""
        args = {
            "conversation": True,
            "locked": False,
            "thinking": False,
            "speaking": False,
            "has_device": True,
            "already_running": False,
        }
        args.update(overrides)
        return args

    def test_allowed_when_everything_is_clear(self) -> None:
        """The happy path returns (True, "")."""
        allowed, reason = listening.should_listen(**self.base())
        self.assertTrue(allowed)
        self.assertEqual(reason, "")
        print("PASS test_allowed_when_everything_is_clear")

    def test_each_gate_blocks_on_its_own(self) -> None:
        """Turning conversation off, locking, thinking, speaking, or no device blocks it."""
        cases = {
            "conversation off": ("conversation", False),
            "locked": ("locked", True),
            "thinking": ("thinking", True),
            "speaking": ("speaking", True),
            "no device": ("has_device", False),
            "already running": ("already_running", True),
        }
        for label, (key, value) in cases.items():
            allowed, reason = listening.should_listen(**self.base(**{key: value}))
            self.assertFalse(allowed, label)
            self.assertTrue(reason, label)
        # 自激防线单独点出来：她还在说的时候绝对不能收音
        allowed, reason = listening.should_listen(**self.base(speaking=True))
        self.assertFalse(allowed)
        self.assertIn("说话", reason)
        print("PASS test_each_gate_blocks_on_its_own")

    def test_unknown_lock_state_does_not_block(self) -> None:
        """An unreadable lock state means 'unknown', not 'locked'."""
        allowed, _reason = listening.should_listen(**self.base(locked=None))
        self.assertTrue(allowed)
        print("PASS test_unknown_lock_state_does_not_block")


class ConvertTest(unittest.TestCase):
    """PCM maths used by the framing and the level gate."""

    def test_pcm_to_float(self) -> None:
        """16-bit samples scale into -1 … 1."""
        samples = listening.pcm_to_float(pcm16([0, 16384, -16384, 32767]))
        self.assertEqual(len(samples), 4)
        self.assertAlmostEqual(samples[1], 0.5, places=4)
        self.assertAlmostEqual(samples[2], -0.5, places=4)
        self.assertGreater(samples[3], 0.999)
        self.assertEqual(listening.pcm_to_float(b""), [])
        self.assertEqual(len(listening.pcm_to_float(b"\x01")), 0)  # 半个样本，丢掉
        print("PASS test_pcm_to_float")

    def test_rms(self) -> None:
        """Silence is 0, a constant signal is its own magnitude."""
        self.assertEqual(listening.rms_of([]), 0.0)
        self.assertEqual(listening.rms_of([0.0] * 100), 0.0)
        self.assertAlmostEqual(listening.rms_of([0.5] * 100), 0.5, places=6)
        print("PASS test_rms")


class NormalizeTest(unittest.TestCase):
    """Millisecond settings are clamped, and junk falls back."""

    def test_clamping(self) -> None:
        """Too small, too large, and nonsense all land somewhere sane."""
        self.assertEqual(listening.normalize_ms(None, 700), 700)
        self.assertEqual(listening.normalize_ms("abc", 700), 700)
        self.assertEqual(listening.normalize_ms(float("nan"), 700), 700)
        self.assertEqual(listening.normalize_ms(1, 700), 50)
        self.assertEqual(listening.normalize_ms(99999, 700), 5000)
        self.assertEqual(listening.normalize_ms("850", 700), 850)
        self.assertLess(listening.DEFAULT_MIN_SPEECH_MS, listening.DEFAULT_SILENCE_MS)
        print("PASS test_clamping")


class PopOrderVad:
    """A VAD stub that mimics the real one: samples are gone once popped.

    真实 `sherpa_onnx` 的 `vad.front` 给的是指向内部缓冲的视图，`pop()` 之后就读不出样本了。
    如果代码先 pop 再读，表现是"检测到句子但样本为空"，被 `continue` 静默跳过——
    一句都发不出去，而且**不报错**。这条测试就是钉住读取顺序。
    """

    def __init__(self, samples: list[float]) -> None:
        self._segments = [samples]
        self._popped = False

    def accept_waveform(self, frame) -> None:  # noqa: ANN001
        """Nothing to do: the segment is preloaded."""

    def empty(self) -> bool:
        """Is there nothing to take?"""
        return self._popped

    @property
    def front(self):  # noqa: ANN201
        """A view that only yields samples while it has not been popped."""
        vad = self

        class Seg:
            @property
            def samples(self):  # noqa: ANN201
                return [] if vad._popped else vad._segments[0]

        return Seg()

    def pop(self) -> None:
        """Mark the segment as consumed."""
        self._popped = True


class SegmentWatcherTest(unittest.TestCase):
    """Frames in, whole sentences out — including the quiet-segment filter."""

    def test_frames_are_buffered_until_a_window_is_full(self) -> None:
        """Less than one window produces no VAD call at all."""
        vad = FakeVad()
        watcher = listening.SegmentWatcher(vad)
        self.assertEqual(watcher.feed(floats(listening.WINDOW - 1)), [])
        self.assertEqual(vad.accepted, 0)
        watcher.feed([0.5])
        self.assertEqual(vad.accepted, 1)
        print("PASS test_frames_are_buffered_until_a_window_is_full")

    def test_segments_come_out_whole(self) -> None:
        """A finished segment is returned with all its samples."""
        vad = FakeVad(every=2, segment_frames=3)
        watcher = listening.SegmentWatcher(vad)
        out = watcher.feed(floats(listening.WINDOW * 2))
        self.assertEqual(len(out), 1)
        self.assertEqual(len(out[0]), listening.WINDOW * 3)
        print("PASS test_segments_come_out_whole")

    def test_quiet_segments_are_dropped(self) -> None:
        """A segment below the level gate is thrown away, not sent to recognition."""
        vad = FakeVad(every=1, segment_frames=1)
        watcher = listening.SegmentWatcher(vad, min_level=0.2)
        self.assertEqual(watcher.feed(floats(listening.WINDOW, 0.01)), [])
        self.assertEqual(watcher.dropped, 1)
        loud = watcher.feed(floats(listening.WINDOW, 0.9))
        self.assertEqual(len(loud), 1)
        print("PASS test_quiet_segments_are_dropped")

    def test_partial_frames_are_kept_for_next_time(self) -> None:
        """A trailing partial window is buffered, not lost."""
        vad = FakeVad()
        watcher = listening.SegmentWatcher(vad)
        watcher.feed(floats(listening.WINDOW + 100))
        self.assertEqual(len(watcher._frames), 100)
        print("PASS test_partial_frames_are_kept_for_next_time")

    def test_samples_are_read_before_pop(self) -> None:
        """Reading after pop() would silently yield nothing — must not happen."""
        samples = floats(listening.WINDOW, 0.8)
        watcher = listening.SegmentWatcher(PopOrderVad(samples))
        out = watcher.feed(floats(listening.WINDOW))
        self.assertEqual(len(out), 1, "读取顺序写反了：pop 之后才读 samples 会拿到空")
        self.assertEqual(len(out[0]), len(samples))
        self.assertEqual(watcher.dropped, 0)
        print("PASS test_samples_are_read_before_pop")


class WavTest(unittest.TestCase):
    """Segments are written as the exact format the recognizer wants."""

    def setUp(self) -> None:
        """Scratch directory."""
        self.scratch = Path(__file__).resolve().parent / "_listen_test"
        shutil.rmtree(self.scratch, ignore_errors=True)
        self.scratch.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        """Clean up."""
        shutil.rmtree(self.scratch, ignore_errors=True)

    def test_round_trip(self) -> None:
        """What we write is 16 kHz mono 16-bit and reads back the same length."""
        path = self.scratch / "seg.wav"
        self.assertTrue(listening.write_wav(floats(listening.SAMPLE_RATE // 2, 0.5), path))
        with wave.open(str(path), "rb") as handle:
            self.assertEqual(handle.getnchannels(), 1)
            self.assertEqual(handle.getframerate(), 16000)
            self.assertEqual(handle.getsampwidth(), 2)
            self.assertAlmostEqual(handle.getnframes() / 16000, 0.5, places=2)
        print("PASS test_round_trip")

    def test_clipping_is_safe(self) -> None:
        """Values outside -1 … 1 are clipped instead of wrapping around."""
        path = self.scratch / "clip.wav"
        self.assertTrue(listening.write_wav([2.0, -2.0, 0.0], path))
        with wave.open(str(path), "rb") as handle:
            frames = handle.readframes(3)
        values = struct.unpack("<3h", frames)
        self.assertEqual(values[0], 32767)
        self.assertEqual(values[1], -32767)
        print("PASS test_clipping_is_safe")

    def test_write_failure_returns_false(self) -> None:
        """An unwritable destination is reported, not raised.

        注意：`write_wav` 会自己建父目录（所以"父目录不存在"不算失败），
        这里用一个**目录**当目标文件，才真的写不进去。
        """
        self.assertFalse(listening.write_wav([0.1], self.scratch))
        print("PASS test_write_failure_returns_false")

    def test_write_creates_parent_directories(self) -> None:
        """A missing parent directory is created rather than treated as an error."""
        path = self.scratch / "deep" / "nested" / "seg.wav"
        self.assertTrue(listening.write_wav([0.1], path))
        self.assertTrue(path.is_file())
        print("PASS test_write_creates_parent_directories")


class UtteranceTest(unittest.TestCase):
    """The bubble text stays honest about failures."""

    def test_describe(self) -> None:
        """Success shows duration/level; failure shows the reason."""
        good = listening.Utterance(seconds=3.2, level=812.0, path="x.wav")
        self.assertIn("3.2 秒", good.describe())
        self.assertIn("812", good.describe())
        self.assertEqual(listening.Utterance(error="没麦").describe(), "没听清：没麦")
        print("PASS test_describe")


class ModelTest(unittest.TestCase):
    """The VAD model download uses the shared verified store."""

    def setUp(self) -> None:
        """Point the module at scratch paths and stub the network."""
        self.real_file = listening.VAD_FILE
        self.real_urlopen = urllib.request.urlopen
        self.scratch = Path(__file__).resolve().parent / "_listen_test"
        shutil.rmtree(self.scratch, ignore_errors=True)
        self.scratch.mkdir(parents=True, exist_ok=True)
        listening.VAD_FILE = self.scratch / "vad" / "silero_vad.onnx"

    def tearDown(self) -> None:
        """Restore."""
        listening.VAD_FILE = self.real_file
        urllib.request.urlopen = self.real_urlopen
        shutil.rmtree(self.scratch, ignore_errors=True)

    def test_ready_requires_a_plausible_file(self) -> None:
        """A missing or tiny file does not count as ready."""
        self.assertFalse(listening.is_ready())
        listening.VAD_FILE.parent.mkdir(parents=True, exist_ok=True)
        listening.VAD_FILE.write_bytes(b"x" * 1000)
        self.assertFalse(listening.is_ready())  # 太小，肯定不是真模型
        listening.VAD_FILE.write_bytes(b"x" * 200_000)
        self.assertTrue(listening.is_ready())
        self.assertIn("silero_vad.onnx", listening.describe())
        print("PASS test_ready_requires_a_plausible_file")

    def test_missing_model_message(self) -> None:
        """describe() says what is missing instead of pretending."""
        self.assertIn("还没下", listening.describe())
        print("PASS test_missing_model_message")

    def test_fetch_reports_every_failed_source(self) -> None:
        """When all sources fail, the error names each one."""

        def boom(*_args, **_kwargs):
            raise urllib.error.URLError("down")

        urllib.request.urlopen = boom
        with self.assertRaises(OSError) as caught:
            listening.fetch_vad()
        message = str(caught.exception)
        for label in ("GitHub", "hf-mirror"):
            self.assertIn(label, message)
        print("PASS test_fetch_reports_every_failed_source")

    def test_fetch_uses_the_mirror_when_the_first_source_fails(self) -> None:
        """The second source is used when the first one is unreachable."""
        payload = b"x" * 200_000

        class Fake:
            headers = {"Content-Length": str(len(payload))}

            def read(self, size: int = -1) -> bytes:
                """Return the payload once."""
                nonlocal payload
                chunk, payload = payload, b""
                return chunk

            def __enter__(self):
                return self

            def __exit__(self, *exc) -> bool:
                return False

        seen: list[str] = []

        def fake(request, timeout=None):  # noqa: ANN001, ARG001
            url = request.full_url
            seen.append("github" if "github" in url else "mirror")
            if "github" in url:
                raise urllib.error.URLError("github down")
            return Fake()

        urllib.request.urlopen = fake
        text = listening.fetch_vad()
        self.assertIn("hf-mirror", text)
        self.assertIn("github", seen)  # 先试过 GitHub，失败了才轮到镜像
        self.assertIn("mirror", seen)
        self.assertTrue(listening.is_ready())
        print("PASS test_fetch_uses_the_mirror_when_the_first_source_fails")


class StoreTest(unittest.TestCase):
    """The shared downloader refuses truncated files."""

    def setUp(self) -> None:
        """Scratch target + stubbed network."""
        self.real = urllib.request.urlopen
        self.scratch = Path(__file__).resolve().parent / "_listen_test"
        shutil.rmtree(self.scratch, ignore_errors=True)
        self.scratch.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        """Restore."""
        urllib.request.urlopen = self.real
        shutil.rmtree(self.scratch, ignore_errors=True)

    def test_truncated_download_is_deleted(self) -> None:
        """Fewer bytes than Content-Length promised means failure, and no leftover file."""
        body = b"partial"

        class Fake:
            headers = {"Content-Length": str(len(body) * 10)}

            def read(self, size: int = -1) -> bytes:
                """Return the short body once."""
                nonlocal body
                chunk, body = body, b""
                return chunk

            def __enter__(self):
                return self

            def __exit__(self, *exc) -> bool:
                return False

        urllib.request.urlopen = lambda *a, **k: Fake()  # noqa: ARG005
        target = self.scratch / "model.onnx"
        with self.assertRaises(OSError) as caught:
            model_store.download("https://example.test/x", target)
        self.assertIn("下载中断", str(caught.exception))
        self.assertFalse(target.exists())
        print("PASS test_truncated_download_is_deleted")


if __name__ == "__main__":
    unittest.main(verbosity=2)
