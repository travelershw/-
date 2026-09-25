"""Offline tests for the speech-to-text client (no network, no key required).

The important regression here is `pick_key`: AstrBot stores keys as a **list**, and passing
that list into an `Authorization` header produces a 401 that looks like a bad key but is
actually a formatting bug (I hit exactly that while probing Zhipu's ASR endpoint).
"""

import io
import json
import shutil
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import asr  # noqa: E402


class FakeResponse:
    """Minimal stand-in for an ``http.client.HTTPResponse`` used as a context manager."""

    def __init__(self, body: str, status: int = 200) -> None:
        self._body = body.encode("utf-8")
        self.status = status

    def read(self) -> bytes:
        """Return the body."""
        return self._body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc) -> bool:
        return False


class EndpointTest(unittest.TestCase):
    """URL building has to tolerate trailing slashes and already-complete URLs."""

    def test_build(self) -> None:
        """Slash count must not matter."""
        self.assertEqual(
            asr.endpoint("https://open.bigmodel.cn/api/paas/v4"),
            "https://open.bigmodel.cn/api/paas/v4/audio/transcriptions",
        )
        self.assertEqual(
            asr.endpoint("https://open.bigmodel.cn/api/paas/v4/"),
            "https://open.bigmodel.cn/api/paas/v4/audio/transcriptions",
        )
        self.assertEqual(
            asr.endpoint(""),
            f"{asr.DEFAULT_BASE_URL}/audio/transcriptions",
        )
        self.assertEqual(asr.endpoint(None), f"{asr.DEFAULT_BASE_URL}/audio/transcriptions")
        already = "https://example.test/v1/audio/transcriptions"
        self.assertEqual(asr.endpoint(already), already)
        print("PASS test_build")


class PickKeyTest(unittest.TestCase):
    """The AstrBot list-form bug must never come back."""

    def test_string_and_list(self) -> None:
        """A plain string passes through; a list yields its first usable entry."""
        self.assertEqual(asr.pick_key("abc"), "abc")
        self.assertEqual(asr.pick_key("  abc  "), "abc")
        self.assertEqual(asr.pick_key(["abc"]), "abc")
        self.assertEqual(asr.pick_key(["", "  ", "second"]), "second")
        print("PASS test_string_and_list")

    def test_nothing_usable(self) -> None:
        """Everything else yields an empty string."""
        for value in (None, [], [None, 5], {"a": 1}, 7, ""):
            self.assertEqual(asr.pick_key(value), "", repr(value))
        print("PASS test_nothing_usable")

    def test_header_is_never_a_list(self) -> None:
        """A list key must not leak into the header as `['...']`."""
        key = asr.pick_key(["id.secret"])
        self.assertEqual(key, "id.secret")
        self.assertNotIn("[", key)
        print("PASS test_header_is_never_a_list")


class ParseResponseTest(unittest.TestCase):
    """Different services wrap the transcript differently."""

    def test_shapes(self) -> None:
        """text/result/transcript and the OpenAI choices form all work."""
        self.assertEqual(asr.parse_response({"text": "你好"}), "你好")
        self.assertEqual(asr.parse_response({"result": " 你好 "}), "你好")
        self.assertEqual(asr.parse_response({"transcription": "你好"}), "你好")
        self.assertEqual(
            asr.parse_response({"choices": [{"message": {"content": "你好"}}]}), "你好"
        )
        self.assertEqual(asr.parse_response("你好"), "你好")
        print("PASS test_shapes")

    def test_empty(self) -> None:
        """Empty or unusable payloads give an empty string, never a guess."""
        for payload in ({}, {"text": ""}, {"text": "   "}, {"text": None}, [], None, 5, ""):
            self.assertEqual(asr.parse_response(payload), "", repr(payload))
        print("PASS test_empty")


class DescribeErrorTest(unittest.TestCase):
    """Errors must be turned into something a human can act on."""

    def test_real_zhipu_error(self) -> None:
        """The exact 429 we measured: 'no balance / no resource pack'."""
        body = json.dumps({"error": {"code": "1113", "message": "余额不足或无可用资源包,请充值。"}})
        text = asr.describe_error(429, body)
        self.assertIn("余额不足", text)
        self.assertIn("资源包", text)
        self.assertIn("充值", text)
        print("PASS test_real_zhipu_error")

    def test_statuses(self) -> None:
        """401/404/other statuses each say something specific."""
        self.assertIn("Key 不对", asr.describe_error(401, "{}"))
        self.assertIn("404", asr.describe_error(404, "{}"))
        self.assertIn("500", asr.describe_error(500, "boom"))
        self.assertIn("boom", asr.describe_error(500, "boom"))
        self.assertTrue(asr.describe_error(500, "") != "")
        print("PASS test_statuses")


class AstrbotKeyTest(unittest.TestCase):
    """Reading the key AstrBot already configured (list form, per source id)."""

    def setUp(self) -> None:
        """Point the module at a scratch AstrBot-style config."""
        self.real = asr.ASTRBOT_CONFIG
        self.scratch = Path(__file__).resolve().parent / "_asr_test"
        shutil.rmtree(self.scratch, ignore_errors=True)
        self.scratch.mkdir(parents=True, exist_ok=True)
        asr.ASTRBOT_CONFIG = self.scratch / "cmd_config.json"

    def tearDown(self) -> None:
        """Restore the path and clean up."""
        asr.ASTRBOT_CONFIG = self.real
        shutil.rmtree(self.scratch, ignore_errors=True)

    def test_reads_first_key_of_the_list(self) -> None:
        """The list form is unwrapped, and the requested source id is honoured."""
        asr.ASTRBOT_CONFIG.write_text(
            json.dumps(
                {
                    "provider_sources": [
                        {"id": "deepseek", "key": ["sk-deepseek"]},
                        {"id": "zhipu", "key": ["id.secret", "second"]},
                    ]
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual(asr.astrbot_key(), "id.secret")
        self.assertEqual(asr.astrbot_key("deepseek"), "sk-deepseek")
        self.assertEqual(asr.astrbot_key("openai"), "")
        print("PASS test_reads_first_key_of_the_list")

    def test_missing_or_broken_config(self) -> None:
        """No file, broken JSON, or a non-dict all give an empty key."""
        self.assertEqual(asr.astrbot_key(), "")
        asr.ASTRBOT_CONFIG.write_text("{not json", encoding="utf-8")
        self.assertEqual(asr.astrbot_key(), "")
        asr.ASTRBOT_CONFIG.write_text("[1, 2]", encoding="utf-8")
        self.assertEqual(asr.astrbot_key(), "")
        print("PASS test_missing_or_broken_config")


class TranscribeTest(unittest.TestCase):
    """The request we actually send (shape, auth header, body) and its failure modes."""

    def setUp(self) -> None:
        """Create a tiny wav and capture the outgoing request."""
        self.real_urlopen = urllib.request.urlopen
        self.real_config = asr.ASTRBOT_CONFIG
        self.scratch = Path(__file__).resolve().parent / "_asr_test"
        shutil.rmtree(self.scratch, ignore_errors=True)
        self.scratch.mkdir(parents=True, exist_ok=True)
        asr.ASTRBOT_CONFIG = self.scratch / "cmd_config.json"  # 空的：没有可借的 key
        self.wav = self.scratch / "mic_0901_120000.wav"
        self.wav.write_bytes(b"RIFF....WAVEfmt " + b"\x00" * 40)
        self.seen: list[urllib.request.Request] = []

    def tearDown(self) -> None:
        """Restore globals and clean up."""
        urllib.request.urlopen = self.real_urlopen
        asr.ASTRBOT_CONFIG = self.real_config
        shutil.rmtree(self.scratch, ignore_errors=True)

    def test_success_and_request_shape(self) -> None:
        """A 200 returns the text, and the body carries model + the audio bytes."""

        def fake_urlopen(request, timeout=None):  # noqa: ANN001, ARG001
            self.seen.append(request)
            return FakeResponse(json.dumps({"text": "今天天气不错"}))

        urllib.request.urlopen = fake_urlopen
        text = asr.transcribe(str(self.wav), api_key="id.secret", model="glm-asr")
        self.assertEqual(text, "今天天气不错")
        request = self.seen[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer id.secret")
        body = request.data
        self.assertIn(b'name="model"', body)
        self.assertIn(b"glm-asr", body)
        self.assertIn(b'filename="mic_0901_120000.wav"', body)
        self.assertIn(self.wav.read_bytes(), body)
        self.assertTrue(request.full_url.endswith("/audio/transcriptions"))
        print("PASS test_success_and_request_shape")

    def test_no_key_is_refused_before_sending(self) -> None:
        """Without a key we explain ourselves and send nothing."""
        calls = []
        urllib.request.urlopen = lambda *a, **k: calls.append(a)  # noqa: ARG005
        with self.assertRaises(asr.ASRError) as caught:
            asr.transcribe(str(self.wav), api_key="")
        self.assertIn("Key", str(caught.exception))
        self.assertEqual(calls, [])
        print("PASS test_no_key_is_refused_before_sending")

    def test_http_error_is_mapped(self) -> None:
        """The 429 body from Zhipu surfaces as an actionable message."""

        def fake_urlopen(request, timeout=None):  # noqa: ANN001, ARG001
            raise urllib.error.HTTPError(
                request.full_url,
                429,
                "Too Many Requests",
                {},
                io.BytesIO(json.dumps({"error": {"message": "余额不足或无可用资源包,请充值。"}}).encode()),
            )

        urllib.request.urlopen = fake_urlopen
        with self.assertRaises(asr.ASRError) as caught:
            asr.transcribe(str(self.wav), api_key="id.secret")
        self.assertIn("余额不足", str(caught.exception))
        print("PASS test_http_error_is_mapped")

    def test_unreachable_service(self) -> None:
        """A network failure is reported, not swallowed."""

        def fake_urlopen(request, timeout=None):  # noqa: ANN001, ARG001
            raise urllib.error.URLError("dns boom")

        urllib.request.urlopen = fake_urlopen
        with self.assertRaises(asr.ASRError) as caught:
            asr.transcribe(str(self.wav), api_key="id.secret")
        self.assertIn("连不上", str(caught.exception))
        print("PASS test_unreachable_service")

    def test_empty_transcript_is_an_error(self) -> None:
        """No text back means failure — we never invent what the user said."""

        def fake_urlopen(request, timeout=None):  # noqa: ANN001, ARG001
            return FakeResponse(json.dumps({"text": ""}))

        urllib.request.urlopen = fake_urlopen
        with self.assertRaises(asr.ASRError) as caught:
            asr.transcribe(str(self.wav), api_key="id.secret")
        self.assertIn("没返回文字", str(caught.exception))
        print("PASS test_empty_transcript_is_an_error")

    def test_missing_file(self) -> None:
        """A deleted recording is reported as such."""
        with self.assertRaises(asr.ASRError) as caught:
            asr.transcribe(str(self.scratch / "nope.wav"), api_key="id.secret")
        self.assertIn("读不到录音文件", str(caught.exception))
        print("PASS test_missing_file")

    def test_borrows_astrbot_key_when_unset(self) -> None:
        """With no key of our own, AstrBot's configured key is used."""
        asr.ASTRBOT_CONFIG.write_text(
            json.dumps({"provider_sources": [{"id": "zhipu", "key": ["borrowed.key"]}]}),
            encoding="utf-8",
        )

        def fake_urlopen(request, timeout=None):  # noqa: ANN001, ARG001
            self.seen.append(request)
            return FakeResponse(json.dumps({"text": "借来的 key"}))

        urllib.request.urlopen = fake_urlopen
        self.assertEqual(asr.transcribe(str(self.wav)), "借来的 key")
        self.assertEqual(self.seen[0].get_header("Authorization"), "Bearer borrowed.key")
        print("PASS test_borrows_astrbot_key_when_unset")


if __name__ == "__main__":
    unittest.main(verbosity=2)
