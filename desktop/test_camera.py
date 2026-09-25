"""Offline tests for the camera rules: propose / capture limits, cleanup, video-call bits.

不碰真实摄像头：只测纯决策函数、结果对象、图像换算与"用完即弃"的删除逻辑。
（P5 的常开会话 `camera.Session` 需要真实设备，这里只验证它在没有设备时**优雅失败**。）"""

import os
import struct
import sys
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import camera  # noqa: E402
from PySide6.QtGui import QImage  # noqa: E402

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


class PreviewSizeTest(unittest.TestCase):
    """The preview window fits the frame into a small box, keeping the shape."""

    def test_shrinks_landscape_and_portrait(self) -> None:
        """Longest edge becomes the cap, aspect ratio preserved."""
        self.assertEqual(camera.preview_size(1280, 720), (240, 135))
        self.assertEqual(camera.preview_size(720, 1280), (135, 240))
        print("PASS test_shrinks_landscape_and_portrait")

    def test_small_frames_are_untouched(self) -> None:
        """Already-small frames are not blown up."""
        self.assertEqual(camera.preview_size(160, 120), (160, 120))
        self.assertEqual(camera.preview_size(240, 240), (240, 240))
        print("PASS test_small_frames_are_untouched")

    def test_junk_never_returns_zero(self) -> None:
        """Zero/negative sizes can't produce a 0x0 window."""
        self.assertEqual(camera.preview_size(0, 0), (1, 1))
        self.assertEqual(camera.preview_size(-5, 100), (1, 1))
        print("PASS test_junk_never_returns_zero")


def solid(value: int) -> QImage:
    """Build a solid-grey image.

    Args:
        value: Grey level 0 … 255.

    Returns:
        The image.
    """
    image = QImage(64, 48, QImage.Format_RGB32)
    image.fill(value)
    return image


class MotionTest(unittest.TestCase):
    """Motion is what tells "有没有人在动" without uploading anything."""

    def test_identical_is_zero(self) -> None:
        """Two identical frames mean no motion."""
        self.assertEqual(camera.motion_of(solid(120), solid(120)), 0.0)
        print("PASS test_identical_is_zero")

    def test_big_change_is_large(self) -> None:
        """Black → white is a full-scale difference."""
        self.assertAlmostEqual(camera.motion_of(solid(0), solid(255)), 1.0, places=2)
        self.assertGreater(camera.motion_of(solid(0), solid(128)), 0.4)
        print("PASS test_big_change_is_large")

    def test_no_previous_frame(self) -> None:
        """The first frame has nothing to compare against."""
        self.assertEqual(camera.motion_of(None, solid(100)), 0.0)
        self.assertEqual(camera.motion_of(solid(100), None), 0.0)
        self.assertEqual(camera.motion_of(None, None), 0.0)
        print("PASS test_no_previous_frame")


class SaveFrameTest(unittest.TestCase):
    """Frames written for sending follow the usual naming/size rules."""

    def setUp(self) -> None:
        """Point the module at a scratch shots directory."""
        self.real = camera.SHOTS
        camera.SHOTS = SCRATCH
        SCRATCH.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        """Restore and clean up."""
        camera.SHOTS = self.real
        for path in SCRATCH.glob("cam_*.jpg"):
            path.unlink(missing_ok=True)
        SCRATCH.rmdir()

    def test_writes_and_shrinks(self) -> None:
        """A huge frame is scaled down and saved as a readable JPEG."""
        big = QImage(4000, 3000, QImage.Format_RGB32)
        big.fill(90)
        frame = camera.save_frame(big, "测试帧", brightness=90)
        self.assertEqual(frame.error, "")
        self.assertLessEqual(max(frame.width, frame.height), camera.MAX_LONG_EDGE)
        self.assertIn("测试帧", frame.label)
        self.assertGreater(frame.bytes, 0)
        self.assertTrue(Path(frame.path).is_file())
        self.assertTrue(Path(frame.path).name.startswith("cam_"))
        print("PASS test_writes_and_shrinks")

    def test_empty_input_is_reported(self) -> None:
        """No image means an error, never a bogus file."""
        self.assertTrue(camera.save_frame(None, "空").error)
        self.assertTrue(camera.save_frame(QImage(), "空").error)
        print("PASS test_empty_input_is_reported")


class SessionTest(unittest.TestCase):
    """The always-on session must fail loudly and harmlessly when it cannot open."""

    def test_session_without_device_reports_failure(self) -> None:
        """Either it opens, or it emits a failure and stays closed (never raises)."""
        from PySide6.QtCore import QCoreApplication  # noqa: PLC0415

        app = QCoreApplication.instance() or QCoreApplication([])
        session = camera.Session()
        problems: list[str] = []
        session.failed.connect(problems.append)
        opened = session.start()
        if opened:
            # 这台机器（或这个受限进程）真的能看到摄像头：验证常开会话的基本契约
            self.assertTrue(session.is_open())
            self.assertTrue(camera.devices())
            session.stop()
            self.assertFalse(session.is_open())
            print("PASS test_session_without_device_reports_failure (真开起来了，走的是开着的分支)")
        else:
            self.assertTrue(problems, "失败时必须说清原因")
            self.assertFalse(session.is_open())
            self.assertIsNone(session.latest())
            self.assertEqual(session.motion(), 0.0)
            self.assertIn("摄像头", problems[0])
            print(f"PASS test_session_without_device_reports_failure ({problems[0]})")
        self.assertIsNotNone(app)

    def test_snapshot_before_any_frame(self) -> None:
        """Asking for a frame before one arrived is an error, not a crash."""
        session = camera.Session()
        frame = session.snapshot()
        self.assertTrue(frame.error)
        self.assertIn("还没有画面", frame.error)
        print("PASS test_snapshot_before_any_frame")

    def test_stop_is_idempotent(self) -> None:
        """Closing an already-closed session is fine."""
        session = camera.Session()
        session.stop()
        session.stop()
        self.assertFalse(session.is_open())
        self.assertEqual(session.frames_seen(), 0)
        print("PASS test_stop_is_idempotent")


if __name__ == "__main__":
    unittest.main(verbosity=2)
