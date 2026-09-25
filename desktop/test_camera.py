"""Offline tests for the camera rules: propose / capture limits and cleanup.

不碰真实摄像头：只测纯决策函数、结果对象与"用完即弃"的删除逻辑。
"""

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import camera  # noqa: E402

SCRATCH = Path(__file__).resolve().parent / "_camera_test"
NOW = 1_800_000_000.0


class ProposeRuleTest(unittest.TestCase):
    """She may offer to look only when you are present, quiet, and not over the cap."""

    def base(self, **overrides):
        """Build the default argument set, with overrides applied.

        Args:
            **overrides: Fields to replace.

        Returns:
            The kwargs dict for :func:`camera.should_propose`.
        """
        args = {
            "now": NOW,
            "enabled": True,
            "has_camera": True,
            "idle_seconds": 30.0,
            "locked": False,
            "last_offer_at": 0.0,
            "offers_today": 0,
            "last_interaction_at": 0.0,
        }
        args.update(overrides)
        return args

    def test_allows_when_present_and_quiet(self) -> None:
        """Present, unlocked, no recent offer or chat: an offer is allowed."""
        self.assertTrue(camera.should_propose(**self.base()))
        print("PASS test_allows_when_present_and_quiet")

    def test_blocked_cases(self) -> None:
        """Every guard blocks on its own."""
        cases = {
            "switched off": {"enabled": False},
            "no camera": {"has_camera": False},
            "locked": {"locked": True},
            "idle unknown": {"idle_seconds": None},
            "idle too long": {"idle_seconds": camera.PRESENT_IDLE_SECONDS + 1},
            "daily cap": {"offers_today": camera.PROPOSE_DAILY_LIMIT},
            "offered just now": {"last_offer_at": NOW - 60},
            "just talked": {"last_interaction_at": NOW - 60},
        }
        for label, overrides in cases.items():
            with self.subTest(label):
                self.assertFalse(camera.should_propose(**self.base(**overrides)), label)
        # 锁屏状态未知时不该当成"锁着"而误拦
        self.assertTrue(camera.should_propose(**self.base(locked=None)))
        # 冷却刚好过去就可以提
        self.assertTrue(
            camera.should_propose(
                **self.base(last_offer_at=NOW - camera.PROPOSE_COOLDOWN_SECONDS),
            ),
        )
        print("PASS test_blocked_cases")

    def test_capture_rules(self) -> None:
        """Capture needs the feature on, a camera, and the 30s cooldown."""
        self.assertTrue(
            camera.can_capture(now=NOW, enabled=True, has_camera=True, last_capture_at=0.0),
        )
        self.assertFalse(
            camera.can_capture(now=NOW, enabled=False, has_camera=True, last_capture_at=0.0),
        )
        self.assertFalse(
            camera.can_capture(now=NOW, enabled=True, has_camera=False, last_capture_at=0.0),
        )
        self.assertFalse(
            camera.can_capture(now=NOW, enabled=True, has_camera=True, last_capture_at=NOW - 5),
        )
        self.assertTrue(
            camera.can_capture(
                now=NOW,
                enabled=True,
                has_camera=True,
                last_capture_at=NOW - camera.CAPTURE_COOLDOWN_SECONDS,
            ),
        )
        print("PASS test_capture_rules")


class FrameAndCleanupTest(unittest.TestCase):
    """Frame summaries and the 'delete after sending' rule."""

    @classmethod
    def setUpClass(cls) -> None:
        """Create the scratch folder."""
        SCRATCH.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls) -> None:
        """Remove the scratch folder."""
        import shutil

        shutil.rmtree(SCRATCH, ignore_errors=True)

    def test_describe(self) -> None:
        """Failures and successes read differently, and brightness is reported."""
        self.assertIn("没拍成", camera.Frame(error="没找到摄像头").describe())
        ok = camera.Frame(
            path="x.png",
            width=640,
            height=480,
            label="摄像头 X",
            bytes=2048,
            brightness=42,
        )
        text = ok.describe()
        self.assertIn("640x480", text)
        self.assertIn("摄像头 X", text)
        self.assertIn("亮度 42/255", text)
        print("PASS test_describe")

    def test_brightness_of_sees_dark_and_bright(self) -> None:
        """A black frame scores near 0 and a white one near 255."""
        from PySide6.QtGui import QColor, QImage

        black = QImage(40, 30, QImage.Format_RGB32)
        black.fill(QColor(0, 0, 0))
        white = QImage(40, 30, QImage.Format_RGB32)
        white.fill(QColor(255, 255, 255))
        self.assertEqual(camera.brightness_of(black), 0)
        self.assertEqual(camera.brightness_of(white), 255)
        self.assertEqual(camera.brightness_of(None), 0)
        print("PASS test_brightness_of_sees_dark_and_bright")

    def test_drop_removes_the_frame(self) -> None:
        """The frame file is gone after drop, and dropping twice is fine."""
        target = SCRATCH / "cam_test.png"
        target.write_bytes(b"\x89PNG\r\n")
        self.assertTrue(camera.drop(str(target)))
        self.assertFalse(target.exists())
        self.assertTrue(camera.drop(str(target)))
        self.assertTrue(camera.drop(""))
        print("PASS test_drop_removes_the_frame")

    def test_devices_never_raises(self) -> None:
        """Device enumeration returns a list even without hardware."""
        result = camera.devices()
        self.assertIsInstance(result, list)
        print(f"PASS test_devices_never_raises (看到 {len(result)} 个)")

    def test_text_hint_is_short(self) -> None:
        """The prompt sent with a frame stays short and non-technical."""
        hint = camera.text_hint()
        self.assertTrue(hint)
        self.assertLess(len(hint), 60)
        print("PASS test_text_hint_is_short")


class NoAutoCaptureTest(unittest.TestCase):
    """The proposal path must not touch the camera at all."""

    def test_proposal_does_not_capture(self) -> None:
        """should_propose is a pure decision: calling it never grabs a frame."""
        original = camera.capture
        called: list[int] = []

        def boom(*_args, **_kwargs):
            called.append(1)
            raise AssertionError("proposal must not capture")

        camera.capture = boom
        try:
            camera.should_propose(
                now=time.time(),
                enabled=True,
                has_camera=True,
                idle_seconds=10.0,
                locked=False,
                last_offer_at=0.0,
                offers_today=0,
                last_interaction_at=0.0,
            )
        finally:
            camera.capture = original
        self.assertEqual(called, [])
        print("PASS test_proposal_does_not_capture")


if __name__ == "__main__":
    unittest.main(verbosity=2)
