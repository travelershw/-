"""Offline tests for the screenshot-format switch (PNG default, JPEG optional).

不真抓屏：只测格式归一化、落盘路径后缀，以及"保留最近 N 张"的清理逻辑
（清理会临时把 ``screen.SHOTS`` 指到测试目录，绝不碰真实 shots/）。
"""

import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import screen  # noqa: E402


class NormalizeFormatTest(unittest.TestCase):
    """Any setting lands on exactly one of the two supported formats."""

    def test_jpeg_aliases(self) -> None:
        """jpeg/jpg (any case, padded) mean JPEG."""
        for value in ("jpeg", "JPEG", "jpg", "Jpg", "  jpeg  "):
            self.assertEqual(screen.normalize_format(value), "jpeg", value)
        print("PASS test_jpeg_aliases")

    def test_defaults_to_png(self) -> None:
        """PNG is the default, and junk does not slip into another format."""
        for value in (None, "", "png", "PNG", "webp", "gif", 123, object()):
            self.assertEqual(screen.normalize_format(value), "png", repr(value))
        print("PASS test_defaults_to_png")


class ShotPathTest(unittest.TestCase):
    """The file suffix must follow the chosen format."""

    def test_suffix_follows_format(self) -> None:
        """PNG by default, .jpg when JPEG is on."""
        self.assertEqual(screen.shot_path("0925_120000").name, "shot_0925_120000.png")
        self.assertEqual(
            screen.shot_path("0925_120000", "png").name, "shot_0925_120000.png"
        )
        self.assertEqual(
            screen.shot_path("0925_120000", "jpeg").name, "shot_0925_120000.jpg"
        )
        print("PASS test_suffix_follows_format")


class TrimTest(unittest.TestCase):
    """Cleanup keeps the newest shots and knows both suffixes."""

    def test_trims_mixed_formats(self) -> None:
        """Old PNGs and old JPEGs are both trimmed down to KEEP_SHOTS total."""
        original = screen.SHOTS
        scratch = Path(__file__).resolve().parent / "_shot_format_test"
        try:
            # 用工作区里的临时目录而不是 tempfile：Windows 沙箱下 tempfile 的 0700
            # 目录会在清理时被拒绝（WinError 5），和 test_core_offline 踩的是同一个坑。
            scratch.mkdir(parents=True, exist_ok=True)
            screen.SHOTS = scratch
            total = screen.KEEP_SHOTS + 3
            for index in range(total):
                suffix = ".png" if index % 2 else ".jpg"
                path = screen.SHOTS / f"shot_0901_{index:06d}{suffix}"
                path.write_bytes(b"x")
            screen._trim()
            left = sorted(screen.SHOTS.glob("shot_*"))
            self.assertEqual(len(left), screen.KEEP_SHOTS)
            # 最新的留着（index = total-1 → 偶数 → .jpg），最早的三张被删掉
            newest = total - 1
            self.assertIn(
                screen.SHOTS / f"shot_0901_{newest:06d}.jpg",
                left,
            )
            self.assertNotIn(screen.SHOTS / "shot_0901_000000.jpg", left)
            print("PASS test_trims_mixed_formats")
        finally:
            screen.SHOTS = original
            shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
