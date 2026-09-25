"""Offline tests for the local (no-upload) recognizer.

No model and no inference here — only the parts that decide *what* to run and *how* to fail:
tag stripping, model-file selection, wav format validation, the download/fetch rules
(including the "half a file" trap), and the messages shown when things are missing.
"""

import bz2
import io
import shutil
import struct
import sys
import tarfile
import unittest
import urllib.error
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import asr  # noqa: E402
import local_asr  # noqa: E402


class FakeResponse:
    """Context-manager stand-in for an HTTP response."""

    def __init__(self, body: bytes, total: int | None = None) -> None:
        self._body = body
        self.headers = {"Content-Length": str(total if total is not None else len(body))}

    def read(self, size: int = -1) -> bytes:
        """Return (a slice of) the body."""
        if size is None or size < 0:
            chunk, self._body = self._body, b""
            return chunk
        chunk, self._body = self._body[:size], self._body[size:]
        return chunk

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc) -> bool:
        return False


def make_wav(path: Path, seconds: float = 0.5, rate: int = 16000, channels: int = 1, width: int = 2) -> Path:
    """Write a small wav file.

    Args:
        path: Destination.
        seconds: Duration.
        rate: Sample rate.
        channels: Channel count.
        width: Sample width in bytes.

    Returns:
        The path.
    """
    frames = int(rate * seconds) * channels
    samples = [1000, -1000] * (frames // 2)
    payload = b"".join(
        struct.pack("<h", max(-32768, min(32767, value))) for value in samples
    )
    if width == 1:
        payload = bytes((abs(value) % 256) for value in samples)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(width)
        handle.setframerate(rate)
        handle.writeframes(payload)
    return path


def make_archive(members: dict[str, bytes]) -> bytes:
    """Build a tar.bz2 archive in memory.

    Args:
        members: ``{name: content}``.

    Returns:
        The compressed archive bytes.
    """
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as bundle:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            bundle.addfile(info, io.BytesIO(content))
    return bz2.compress(raw.getvalue())


class ParseTextTest(unittest.TestCase):
    """Model tags and odd whitespace must not reach her."""

    def test_strips_tags(self) -> None:
        """SenseVoice-style tags are removed, Paraformer text passes through."""
        self.assertEqual(local_asr.parse_text("<|zh|><|NEUTRAL|>今天天气不错"), "今天天气不错")
        self.assertEqual(local_asr.parse_text("今天天气不错"), "今天天气不错")
        self.assertEqual(local_asr.parse_text("<|en|>hello<|Speech|>"), "hello")
        self.assertEqual(local_asr.parse_text("你好\u3000世界"), "你好 世界")
        print("PASS test_strips_tags")

    def test_edge_cases(self) -> None:
        """Empty, tag-only, and unbalanced brackets do not raise."""
        for value in ("", None, "<|zh|>", "<", ">", "a<b>c"):
            self.assertIsInstance(local_asr.parse_text(value), str)
        self.assertEqual(local_asr.parse_text("<|zh|>"), "")
        self.assertEqual(local_asr.parse_text("a<b>c"), "ac")
        print("PASS test_edge_cases")


class ModelFilesTest(unittest.TestCase):
    """Which files count as "the model is here"."""

    def setUp(self) -> None:
        """Use a scratch model directory."""
        self.real = local_asr.MODELS
        self.scratch = Path(__file__).resolve().parent / "_local_asr_test"
        shutil.rmtree(self.scratch, ignore_errors=True)
        (self.scratch / local_asr.MODEL_NAME).mkdir(parents=True)
        local_asr.MODELS = self.scratch

    def tearDown(self) -> None:
        """Restore and clean up."""
        local_asr.MODELS = self.real
        shutil.rmtree(self.scratch, ignore_errors=True)

    def where(self) -> Path:
        """The scratch model directory."""
        return self.scratch / local_asr.MODEL_NAME

    def test_prefers_int8(self) -> None:
        """With both files on disk, the int8 one wins."""
        (self.where() / "model.onnx").write_bytes(b"fp32")
        (self.where() / "model.int8.onnx").write_bytes(b"int8")
        (self.where() / "tokens.txt").write_text("a 1\n", encoding="utf-8")
        files = local_asr.model_files()
        self.assertIsNotNone(files)
        self.assertEqual(files[0].name, "model.int8.onnx")
        print("PASS test_prefers_int8")

    def test_missing_pieces(self) -> None:
        """No tokens, no onnx, or no directory all mean "not ready"."""
        self.assertIsNone(local_asr.model_files())  # 只有空目录
        (self.where() / "model.onnx").write_bytes(b"fp32")
        self.assertIsNone(local_asr.model_files())  # 缺 tokens.txt
        (self.where() / "tokens.txt").write_text("a 1\n", encoding="utf-8")
        self.assertIsNotNone(local_asr.model_files())
        self.assertFalse(local_asr.is_ready() is False)
        shutil.rmtree(self.where())
        self.assertFalse(local_asr.is_ready())
        self.assertIn("还没装模型", local_asr.describe())
        print("PASS test_missing_pieces")

    def test_describe_when_ready(self) -> None:
        """The description names the file and its size."""
        (self.where() / "model.int8.onnx").write_bytes(b"x" * (1024 * 1024))
        (self.where() / "tokens.txt").write_text("a 1\n", encoding="utf-8")
        text = local_asr.describe()
        self.assertIn("model.int8.onnx", text)
        self.assertIn("1 MB", text)
        print("PASS test_describe_when_ready")


class WavTest(unittest.TestCase):
    """Only the exact format we record is accepted — never silently resampled."""

    def setUp(self) -> None:
        """Scratch directory for wav files."""
        self.scratch = Path(__file__).resolve().parent / "_local_asr_test"
        shutil.rmtree(self.scratch, ignore_errors=True)
        self.scratch.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        """Clean up."""
        shutil.rmtree(self.scratch, ignore_errors=True)

    def test_reads_and_scales(self) -> None:
        """Samples come back as floats in -1..1."""
        path = make_wav(self.scratch / "ok.wav")
        samples = local_asr.wav_samples(str(path))
        self.assertGreater(len(samples), 1000)
        self.assertAlmostEqual(max(samples), 1000 / 32768.0, places=6)
        self.assertAlmostEqual(min(samples), -1000 / 32768.0, places=6)
        print("PASS test_reads_and_scales")

    def test_rejects_other_formats(self) -> None:
        """Wrong rate/channels/width each produce a clear refusal."""
        cases = (
            ("rate.wav", 8000, 1, 2),
            ("channels.wav", 16000, 2, 2),
            ("width.wav", 16000, 1, 1),
        )
        for name, rate, channels, width in cases:
            path = make_wav(self.scratch / name, rate=rate, channels=channels, width=width)
            with self.assertRaises(asr.ASRError) as caught:
                local_asr.wav_samples(str(path))
            self.assertIn("16kHz 单声道 16 位", str(caught.exception))
        print("PASS test_rejects_other_formats")

    def test_missing_file(self) -> None:
        """A missing file is reported, not crashed on."""
        with self.assertRaises(asr.ASRError) as caught:
            local_asr.wav_samples(str(self.scratch / "nope.wav"))
        self.assertIn("读不到录音文件", str(caught.exception))
        print("PASS test_missing_file")


class LoadTest(unittest.TestCase):
    """The two ways loading can fail must each say what is actually missing."""

    def setUp(self) -> None:
        """Reset the cached recognizer between tests."""
        local_asr._recognizer = None
        self.real_files = local_asr.model_files

    def tearDown(self) -> None:
        """Restore globals."""
        local_asr._recognizer = None
        local_asr.model_files = self.real_files

    def test_missing_model_message(self) -> None:
        """No model on disk -> tell the user how to get one."""
        local_asr.model_files = lambda root=None: None  # noqa: ARG005
        with self.assertRaises(asr.ASRError) as caught:
            local_asr.load()
        self.assertIn("还没有识别模型", str(caught.exception))
        print("PASS test_missing_model_message")

    def test_missing_engine_message(self) -> None:
        """Engine not installed -> tell the user the pip command."""
        local_asr.model_files = lambda root=None: (
            Path("model.int8.onnx"),
            Path("tokens.txt"),
        )
        hidden = sys.modules.get("sherpa_onnx")
        sys.modules["sherpa_onnx"] = None  # import 会抛 ImportError
        try:
            with self.assertRaises(asr.ASRError) as caught:
                local_asr.load()
            self.assertIn("没装本机识别引擎", str(caught.exception))
            self.assertIn("pip install sherpa-onnx", str(caught.exception))
            print("PASS test_missing_engine_message")
        finally:
            if hidden is not None:
                sys.modules["sherpa_onnx"] = hidden
            else:
                sys.modules.pop("sherpa_onnx", None)


class FetchTest(unittest.TestCase):
    """Downloading the model: mirror-first, size checks, cleanup, honest errors."""

    def setUp(self) -> None:
        """Scratch model dir, stubbed urlopen, tiny fake "big enough" threshold."""
        self.real_modules = local_asr.MODELS
        self.real_bytes = local_asr.TARBALL_BYTES
        self.real_min = local_asr.MIN_MODEL_BYTES
        self.real_urlopen = urllib.request.urlopen
        self.scratch = Path(__file__).resolve().parent / "_local_asr_test"
        shutil.rmtree(self.scratch, ignore_errors=True)
        self.scratch.mkdir(parents=True, exist_ok=True)
        local_asr.MODELS = self.scratch
        local_asr.MIN_MODEL_BYTES = 4  # 测试里的假 onnx 只有几个字节

    def tearDown(self) -> None:
        """Restore everything."""
        local_asr.MODELS = self.real_modules
        local_asr.TARBALL_BYTES = self.real_bytes
        local_asr.MIN_MODEL_BYTES = self.real_min
        urllib.request.urlopen = self.real_urlopen
        shutil.rmtree(self.scratch, ignore_errors=True)

    def test_mirror_files_path(self) -> None:
        """The mirror path downloads the two files and reports readiness."""
        seen: list[str] = []

        def fake(request, timeout=None):  # noqa: ANN001, ARG001
            url = request.full_url
            seen.append(url)
            if url.endswith(local_asr.MODEL_FILE):
                return FakeResponse(b"onnx-bytes")
            return FakeResponse(b"a 1\n")

        urllib.request.urlopen = fake
        text = local_asr.fetch()
        self.assertIn("装好了", text)
        self.assertIn("hf-mirror", text)
        self.assertTrue(local_asr.is_ready())
        self.assertTrue(all("hf-mirror" in url for url in seen), seen)
        print("PASS test_mirror_files_path")

    def test_falls_back_to_huggingface(self) -> None:
        """If the mirror is unreachable, the direct source is tried next."""
        seen: list[str] = []

        def fake(request, timeout=None):  # noqa: ANN001, ARG001
            url = request.full_url
            seen.append(url)
            if "hf-mirror" in url:
                raise urllib.error.URLError("mirror down")
            return FakeResponse(b"onnx-bytes" if url.endswith(local_asr.MODEL_FILE) else b"a 1\n")

        urllib.request.urlopen = fake
        text = local_asr.fetch()
        self.assertIn("huggingface", text)
        self.assertTrue(any("hf-mirror" in url for url in seen))
        self.assertTrue(any("huggingface.co" in url for url in seen))
        print("PASS test_falls_back_to_huggingface")

    def test_falls_back_to_official_tarball(self) -> None:
        """File sources failing drops through to the official tar.bz2."""
        archive = make_archive(
            {
                f"{local_asr.MODEL_NAME}/model.int8.onnx": b"onnx",
                f"{local_asr.MODEL_NAME}/tokens.txt": b"a 1\n",
            }
        )
        local_asr.TARBALL_BYTES = len(archive)

        def fake(request, timeout=None):  # noqa: ANN001, ARG001
            url = request.full_url
            if "github.com" in url:
                return FakeResponse(archive)
            raise urllib.error.URLError("no mirror")

        urllib.request.urlopen = fake
        text = local_asr.fetch()
        self.assertIn("官方 release", text)
        self.assertTrue(local_asr.is_ready())
        self.assertEqual(list(self.scratch.glob("*.tar.bz2")), [])
        print("PASS test_falls_back_to_official_tarball")

    def test_all_sources_fail_is_reported(self) -> None:
        """Every failure reason is collected, and nothing is left behind."""

        def boom(request, timeout=None):  # noqa: ANN001, ARG001
            raise urllib.error.URLError("everything down")

        urllib.request.urlopen = boom
        with self.assertRaises(asr.ASRError) as caught:
            local_asr.fetch()
        message = str(caught.exception)
        self.assertIn("两条路都没成", message)
        self.assertIn("hf-mirror", message)
        self.assertIn("huggingface", message)
        self.assertIn("官方 release", message)
        self.assertEqual(list(self.scratch.rglob("*.tar.bz2")), [])
        self.assertFalse(local_asr.is_ready())
        print("PASS test_all_sources_fail_is_reported")

    def test_tiny_model_file_is_rejected(self) -> None:
        """A suspiciously small onnx file is deleted instead of being accepted."""
        local_asr.MIN_MODEL_BYTES = 1024

        def fake(request, timeout=None):  # noqa: ANN001, ARG001
            url = request.full_url
            if "github.com" in url:
                raise urllib.error.URLError("no official")
            return FakeResponse(b"tiny" if url.endswith(local_asr.MODEL_FILE) else b"a 1\n")

        urllib.request.urlopen = fake
        with self.assertRaises(asr.ASRError):
            local_asr.fetch()
        self.assertFalse(local_asr.is_ready())
        print("PASS test_tiny_model_file_is_rejected")

    def test_already_present_is_a_no_op(self) -> None:
        """With a model on disk, fetch() does not touch the network."""
        where = self.scratch / local_asr.MODEL_NAME
        where.mkdir(parents=True)
        (where / local_asr.MODEL_FILE).write_bytes(b"onnx")
        (where / local_asr.TOKENS_FILE).write_text("a 1\n", encoding="utf-8")

        def boom(*_args, **_kwargs):
            raise AssertionError("fetch() must not hit the network")

        urllib.request.urlopen = boom
        self.assertIn("已经在本机", local_asr.fetch())
        print("PASS test_already_present_is_a_no_op")


class DropTest(unittest.TestCase):
    """Removing the model frees the disk and resets the cached recognizer."""

    def test_drop(self) -> None:
        """drop_model() deletes the directory."""
        real = local_asr.MODELS
        scratch = Path(__file__).resolve().parent / "_local_asr_test"
        shutil.rmtree(scratch, ignore_errors=True)
        (scratch / local_asr.MODEL_NAME).mkdir(parents=True)
        local_asr.MODELS = scratch
        try:
            self.assertTrue(local_asr.drop_model())
            self.assertFalse(scratch.exists())
            self.assertIsNone(local_asr._recognizer)
            print("PASS test_drop")
        finally:
            local_asr.MODELS = real
            shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
