"""Offline tests for pc_state: shape, staleness, atomic write, tolerance.

不依赖真实设备状态：读数用桩替换，时间用注入值，写入落在临时目录里。
"""

import json
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pc_state  # noqa: E402

SCRATCH = Path(__file__).resolve().parent / "_pc_state_test"


class SnapshotShapeTest(unittest.TestCase):
    """A snapshot must carry every reading with its own timestamp."""

    @classmethod
    def setUpClass(cls) -> None:
        """Point the module at a scratch state file."""
        shutil.rmtree(SCRATCH, ignore_errors=True)
        SCRATCH.mkdir(parents=True)
        cls.originals = {
            name: getattr(pc_state, name)
            for name in ("idle_seconds", "session_locked", "foreground_app", "camera_devices")
        }

    @classmethod
    def tearDownClass(cls) -> None:
        """Restore the real collectors and remove the scratch folder."""
        for name, func in cls.originals.items():
            setattr(pc_state, name, func)
        shutil.rmtree(SCRATCH, ignore_errors=True)

    def test_snapshot_uses_injected_clock(self) -> None:
        """Every reading is stamped with the injected time, not the wall clock."""
        pc_state.idle_seconds = lambda: 12.5
        pc_state.session_locked = lambda: False
        pc_state.foreground_app = lambda: "Code.exe"
        pc_state.camera_devices = lambda: ["HD WebCam"]
        state = pc_state.snapshot(now=1_700_000_000.0)
        self.assertEqual(state["updated_at"], 1_700_000_000.0)
        for name in ("idle_seconds", "locked", "foreground", "cameras"):
            self.assertIn(name, state["readings"])
            self.assertEqual(state["readings"][name]["ts"], 1_700_000_000.0)
        self.assertEqual(pc_state.name_of(state)["foreground"], "Code.exe")
        print("PASS test_snapshot_uses_injected_clock")

    def test_missing_reading_is_omitted(self) -> None:
        """A None value is 'unknown' and must not appear as if it were measured."""
        state = {
            "updated_at": 100.0,
            "readings": {
                "idle_seconds": {"value": None, "ts": 100.0},
                "locked": {"value": True, "ts": 100.0},
            },
        }
        values = pc_state.name_of(state)
        self.assertNotIn("idle_seconds", values)
        self.assertEqual(values["locked"], True)
        self.assertIsNone(pc_state.age_of(state, "idle_seconds", now=200.0))
        self.assertEqual(pc_state.age_of(state, "locked", now=200.0), 100.0)
        print("PASS test_missing_reading_is_omitted")

    def test_describe_boundaries(self) -> None:
        """Idle durations read naturally at the boundaries."""
        self.assertEqual(pc_state.describe(None), "不知道")
        self.assertEqual(pc_state.describe(5), "5 秒")
        self.assertEqual(pc_state.describe(59.9), "59 秒")
        self.assertEqual(pc_state.describe(60), "1 分钟")
        self.assertEqual(pc_state.describe(3599), "59 分钟")
        self.assertEqual(pc_state.describe(3600), "1.0 小时")
        print("PASS test_describe_boundaries")

    def test_summary_covers_states(self) -> None:
        """The bubble line mentions what is actually known."""
        worked = pc_state.summary(
            {
                "updated_at": 1.0,
                "readings": {
                    "idle_seconds": {"value": 300, "ts": 1.0},
                    "locked": {"value": True, "ts": 1.0},
                    "foreground": {"value": "Code.exe", "ts": 1.0},
                    "cameras": {"value": [], "ts": 1.0},
                },
            },
        )
        self.assertIn("5 分钟", worked)
        self.assertIn("屏幕锁着", worked)
        self.assertIn("Code.exe", worked)
        self.assertIn("没看到摄像头", worked)
        unknown = pc_state.summary({"updated_at": 1.0, "readings": {}})
        self.assertIn("不知道", unknown)
        print("PASS test_summary_covers_states")

    def test_write_state_is_atomic_and_readable(self) -> None:
        """The state file is valid JSON and leaves no temp file behind."""
        target = SCRATCH / "pc_state.json"
        state = {"updated_at": 5.0, "readings": {"locked": {"value": False, "ts": 5.0}}}
        written = pc_state.write_state(state, path=target)
        self.assertEqual(written, target)
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), state)
        self.assertFalse(target.with_name(target.name + ".tmp").exists())
        print("PASS test_write_state_is_atomic_and_readable")

    def test_read_state_tolerates_broken_input(self) -> None:
        """Missing, broken, and non-dict files all read as an empty dict."""
        missing = SCRATCH / "nope.json"
        self.assertEqual(pc_state.read_state(missing), {})
        broken = SCRATCH / "broken.json"
        broken.write_text("{not json", encoding="utf-8")
        self.assertEqual(pc_state.read_state(broken), {})
        listed = SCRATCH / "list.json"
        listed.write_text("[1, 2]", encoding="utf-8")
        self.assertEqual(pc_state.read_state(listed), {})
        print("PASS test_read_state_tolerates_broken_input")

    def test_write_state_reports_failure(self) -> None:
        """An unwritable target returns None instead of raising."""
        bad = SCRATCH / "as_dir" / "pc_state.json"
        (SCRATCH / "as_dir").write_text("this is a file, not a folder", encoding="utf-8")
        self.assertIsNone(pc_state.write_state({"updated_at": 1.0}, path=bad))
        print("PASS test_write_state_reports_failure")

    def test_helpers_accept_garbage(self) -> None:
        """Garbage input never raises: it is simply treated as unknown."""
        for state in ({}, {"readings": None}, {"readings": {"locked": 3}}, []):
            self.assertEqual(pc_state.name_of(state if isinstance(state, dict) else {}), {})
        self.assertIsNone(pc_state.age_of({}, "locked"))
        self.assertIsNone(pc_state.age_of({"readings": {"locked": {"value": 1}}}, "locked"))
        print("PASS test_helpers_accept_garbage")


if __name__ == "__main__":
    unittest.main(verbosity=2)
