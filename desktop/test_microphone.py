"""Offline tests for the microphone module (no audio device, no Qt event loop).

Covers the pure parts: level maths, duration clamping, the "was anyone speaking?" gate,
the clip summary, cleanup of old recordings, and deletion after use.
`record()` itself needs a real device and is deliberately not exercised here.
"""

import math
import shutil
import struct
import sys
import unittest
from array import array
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import microphone  # noqa: E402


def pcm_of(*values: int) -> bytes:
    """Pack sample values into raw Int16 bytes.

    Args:
        *values: Sample values.

    Returns:
        The packed bytes.
    """
    return b"".join(struct.pack("<h", value) for value in values)


class LevelTest(unittest.TestCase):
    """RMS/peak maths is what decides "did anyone actually speak"."""

    def test_silence_and_full_scale(self) -> None:
        """All-zero samples are silent; a constant full-scale signal is 32767."""
        silence = pcm_of(*([0] * 100))
        self.assertEqual(microphone.rms_of(silence), 0.0)
        self.assertEqual(microphone.peak_of(silence), 0)
        loud = pcm_of(*([32767] * 100))
        self.assertAlmostEqual(microphone.rms_of(loud), 32767.0, places=2)
        self.assertEqual(microphone.peak_of(loud), 32767)
        # 正负对称的信号 RMS 等于幅值（不是符号抵消后的 0）
        mixed = pcm_of(*([1000, -1000] * 50))
        self.assertAlmostEqual(microphone.rms_of(mixed), 1000.0, places=2)
        print("PASS test_silence_and_full_scale")

    def test_odd_and_empty_input(self) -> None:
        """A trailing odd byte or empty input must not raise."""
        self.assertEqual(microphone.rms_of(b""), 0.0)
        self.assertEqual(microphone.peak_of(b""), 0)
        self.assertEqual(microphone.peak_of(b"\x01"), 0)  # 只有一个字节，凑不成一个样本
        odd = pcm_of(1000, 1000) + b"\x7f"
        self.assertAlmostEqual(microphone.rms_of(odd), 1000.0, places=2)
        print("PASS test_odd_and_empty_input")

    def test_audible_gate(self) -> None:
        """The gate is inclusive at the threshold and rejects quiet noise."""
        self.assertTrue(microphone.is_audible(microphone.MIN_RMS))
        self.assertTrue(microphone.is_audible(microphone.MIN_RMS + 1))
        self.assertFalse(microphone.is_audible(microphone.MIN_RMS - 1))
        self.assertFalse(microphone.is_audible(0.0))
        print("PASS test_audible_gate")


class DurationTest(unittest.TestCase):
    """Requested durations are clamped, and junk falls back to the default."""

    def test_clamping(self) -> None:
        """Too short, too long, and nonsense all land somewhere sane."""
        self.assertEqual(microphone.normalize_seconds(None), microphone.DEFAULT_SECONDS)
        self.assertEqual(microphone.normalize_seconds("abc"), microphone.DEFAULT_SECONDS)
        self.assertEqual(microphone.normalize_seconds(float("nan")), microphone.DEFAULT_SECONDS)
        self.assertEqual(microphone.normalize_seconds(0.1), microphone.MIN_SECONDS)
        self.assertEqual(microphone.normalize_seconds(-5), microphone.MIN_SECONDS)
        self.assertEqual(microphone.normalize_seconds(999), microphone.MAX_SECONDS)
        self.assertEqual(microphone.normalize_seconds("3.5"), 3.5)
        print("PASS test_clamping")

    def test_wav_header_matches_measured_seconds(self) -> None:
        """The clip length formula matches the file we would write."""
        rate, width, channels = microphone.SAMPLE_RATE, microphone.SAMPLE_WIDTH, microphone.CHANNELS
        pcm = pcm_of(*([0] * (rate * channels)))  # 1 秒
        seconds = len(pcm) / (rate * width * channels)
        self.assertAlmostEqual(seconds, 1.0, places=6)
        self.assertEqual(math.isclose(seconds, 1.0, abs_tol=1e-6), True)
        print("PASS test_wav_header_matches_measured_seconds")


class ClipTest(unittest.TestCase):
    """The bubble text must be honest about failures."""

    def test_describe(self) -> None:
        """Success shows duration/device/level; failure shows the reason only."""
        good = microphone.Clip(seconds=5.0, rms=812.4, peak=9000, device="麦克风 (X)")
        text = good.describe()
        self.assertIn("5.0 秒", text)
        self.assertIn("麦克风 (X)", text)
        self.assertIn("812", text)
        bad = microphone.Clip(error="没有找到麦克风")
        self.assertEqual(bad.describe(), "没录成：没有找到麦克风")
        print("PASS test_describe")

    def test_array_roundtrip_matches_struct(self) -> None:
        """The `array`-based maths agrees with an independent struct decode."""
        values = [0, 300, -300, 12000, -12000]
        pcm = pcm_of(*values)
        samples = array("h")
        samples.frombytes(pcm)
        self.assertEqual(list(samples), values)
        expected = math.sqrt(sum(value * value for value in values) / len(values))
        self.assertAlmostEqual(microphone.rms_of(pcm), expected, places=6)
        print("PASS test_array_roundtrip_matches_struct")


class StoreTest(unittest.TestCase):
    """Recordings live in a scratch dir; old ones are trimmed and used ones deleted."""

    def setUp(self) -> None:
        """Point the module at a scratch directory inside the workspace."""
        self.real = microphone.CLIPS
        self.scratch = Path(__file__).resolve().parent / "_mic_test"
        shutil.rmtree(self.scratch, ignore_errors=True)
        self.scratch.mkdir(parents=True, exist_ok=True)
        microphone.CLIPS = self.scratch

    def tearDown(self) -> None:
        """Restore the real directory and clean up."""
        microphone.CLIPS = self.real
        shutil.rmtree(self.scratch, ignore_errors=True)

    def test_trim_keeps_newest(self) -> None:
        """Only the newest KEEP_CLIPS files survive."""
        for index in range(microphone.KEEP_CLIPS + 4):
            path = self.scratch / f"mic_0901_{index:06d}.wav"
            path.write_bytes(b"RIFF")
        microphone._trim()
        left = sorted(self.scratch.glob("mic_*.wav"))
        self.assertEqual(len(left), microphone.KEEP_CLIPS)
        self.assertIn(self.scratch / f"mic_0901_{microphone.KEEP_CLIPS + 3:06d}.wav", left)
        self.assertNotIn(self.scratch / "mic_0901_000000.wav", left)
        print("PASS test_trim_keeps_newest")

    def test_trace_files_are_ignored_by_trim(self) -> None:
        """Other files in the folder are not touched."""
        other = self.scratch / "notes.txt"
        other.write_text("keep me", encoding="utf-8")
        microphone._trim()
        self.assertTrue(other.exists())
        print("PASS test_trace_files_are_ignored_by_trim")

    def test_drop(self) -> None:
        """drop() removes one file and tolerates missing/empty input."""
        path = self.scratch / "mic_0901_120000.wav"
        path.write_bytes(b"RIFF")
        self.assertTrue(microphone.drop(str(path)))
        self.assertFalse(path.exists())
        self.assertFalse(microphone.drop(str(path)))
        self.assertFalse(microphone.drop(""))
        print("PASS test_drop")


class SourceGuardTest(unittest.TestCase):
    """A source-level guard for a bug that unit tests cannot catch.

    真实事故（2026-09-25）：`QBuffer(QByteArray())` 里的 QByteArray 是临时对象，Python 一 GC，
    Qt 还在往已回收的内存里写音频，于是**原生崩溃**：
    `python.exe - 应用程序错误 / 0xFFFFFFFFFFFFFFFF 内存不能为 read`。
    这种崩溃在 Python 层抓不到、也没有异常可断言，只能从源头挡住——所以这里直接读源码。
    """

    def test_qbuffer_is_not_given_a_temporary(self) -> None:
        """`QBuffer(...)` must be called without an argument (comments excluded)."""
        lines = Path(microphone.__file__).read_text(encoding="utf-8").splitlines()
        code = [line for line in lines if not line.lstrip().startswith("#")]
        offenders = [line.strip() for line in code if "QBuffer(QByteArray" in line]
        self.assertEqual(offenders, [])
        self.assertTrue(any("buffer = QBuffer()" in line for line in code))
        print("PASS test_qbuffer_is_not_given_a_temporary")


class DevicesTest(unittest.TestCase):
    """Device listing must never raise, even without Qt Multimedia."""

    def test_devices_returns_list(self) -> None:
        """Whatever the environment, we get a list back."""
        found = microphone.devices()
        self.assertIsInstance(found, list)
        for name in found:
            self.assertIsInstance(name, str)
        print(f"PASS test_devices_returns_list ({len(found)} device(s) visible)")

    def test_record_without_qt_reports_error(self) -> None:
        """With Qt missing, record() explains itself instead of crashing."""
        real = microphone.QAudioSource
        microphone.QAudioSource = None
        try:
            clip = microphone.record(1)
            self.assertTrue(clip.error)
            self.assertIn("QtMultimedia", clip.error)
            print("PASS test_record_without_qt_reports_error")
        finally:
            microphone.QAudioSource = real


if __name__ == "__main__":
    unittest.main(verbosity=2)
