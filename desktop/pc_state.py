"""本机使用状态采集：闲置多久、是否锁屏、前台是哪个程序、有没有摄像头。

设计约定（与项目其它部分一致）：

- **只在本机读写**：结果原子写进 ``pc_state.json``，不上传、不进数据库；
- **缺就是缺**：拿不到的读数一律是 ``None``，绝不猜、绝不用别的值顶替；
- **不采集内容**：只取进程名，不取窗口标题、不取屏幕内容（截图是另一个功能，
  由你点「看看我的屏幕」才发生）；
- **非 Windows 上全部返回 None**，函数都能安全调用。

每个读数都带自己的时间戳：传感器会掉线，过期数据不能当"现在"用。
"""

import ctypes
import json
import os
import sys
import time
from ctypes import wintypes
from pathlib import Path

import paths

STATE_FILE = paths.BASE / "pc_state.json"
# 前台程序每这么多秒才重新查一次：它变化不频繁，省一点开销。
FOREGROUND_CACHE_SECONDS = 5.0
_cache: dict = {}
_cache_at = 0.0


class _LastInputInfo(ctypes.Structure):
    """LASTINPUTINFO: ctypes.wintypes does not define it."""

    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def _reading(value, now: float) -> dict:
    """Wrap one value with its timestamp.

    Args:
        value: The measured value (may be None).
        now: Wall-clock seconds.

    Returns:
        A ``{"value": ..., "ts": ...}`` dict.
    """
    return {"value": value, "ts": now}


def idle_seconds() -> float | None:
    """How long the mouse and keyboard have been untouched.

    Returns:
        Seconds since the last input, or None when unavailable.
    """
    if sys.platform != "win32":
        return None
    try:
        info = _LastInputInfo()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        return max(0.0, (ctypes.windll.kernel32.GetTickCount() - info.dwTime) / 1000.0)
    except (AttributeError, OSError, ValueError):
        return None


def session_locked() -> bool | None:
    """Whether the workstation is locked.

    ``OpenInputDesktop`` fails while the lock screen owns the input desktop, which
    is the cheapest reliable probe; failures with other error codes stay unknown.

    Returns:
        True when locked, False when not, None when it cannot be told.
    """
    if sys.platform != "win32":
        return None
    try:
        user32 = ctypes.windll.user32
        handle = user32.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_SWITCHDESKTOP
        if not handle:
            return True
        user32.CloseDesktop(handle)
        return False
    except (AttributeError, OSError, ValueError):
        return None


def foreground_app() -> str | None:
    """Image name of the process owning the foreground window (never its title).

    Returns:
        Something like ``"Code.exe"``, or None when unavailable.
    """
    if sys.platform != "win32":
        return None
    try:
        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32
        window = user32.GetForegroundWindow()
        if not window:
            return None
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(window, ctypes.byref(pid))
        if not pid.value:
            return None
        # PROCESS_QUERY_LIMITED_INFORMATION: 拿得到的权限最小，够读进程名
        handle = kernel32.OpenProcess(0x1000, False, pid.value)
        if not handle:
            return None
        try:
            size = wintypes.DWORD(1024)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not kernel32.QueryFullProcessImageNameW(
                handle, 0, buffer, ctypes.byref(size)
            ):
                return None
            return Path(buffer.value).name or None
        finally:
            kernel32.CloseHandle(handle)
    except (AttributeError, OSError, ValueError):
        return None


def camera_devices() -> list[str] | None:
    """Names of the video capture devices Qt can see.

    QtMultimedia ships with PySide6 but is excluded from the packaged build, so a
    frozen pet simply reports None here instead of failing.

    Returns:
        Device descriptions (possibly empty), or None when unavailable.
    """
    try:
        from PySide6.QtMultimedia import QMediaDevices  # noqa: PLC0415 - 可选依赖
    except Exception:  # noqa: BLE001 - 打包版没有这个模块
        return None
    try:
        return [
            str(device.description() or "") for device in QMediaDevices.videoInputs()
        ]
    except Exception:  # noqa: BLE001 - 设备枚举失败就当未知
        return None


def snapshot(now: float | None = None) -> dict:
    """Collect one full set of readings.

    Args:
        now: Injectable clock (tests pass a fixed value).

    Returns:
        ``{"updated_at": float, "readings": {name: {"value":..., "ts":...}}}``.
    """
    global _cache, _cache_at
    moment = time.time() if now is None else now
    if moment - _cache_at >= FOREGROUND_CACHE_SECONDS or not _cache:
        _cache_at = moment
        _cache = {"foreground": foreground_app(), "camera": camera_devices()}
    return {
        "updated_at": moment,
        "readings": {
            "idle_seconds": _reading(idle_seconds(), moment),
            "locked": _reading(session_locked(), moment),
            "foreground": _reading(_cache.get("foreground"), moment),
            "cameras": _reading(_cache.get("camera"), moment),
        },
    }


def write_state(state: dict | None = None, path: Path | None = None) -> Path | None:
    """Write a snapshot atomically (temp file + ``os.replace``).

    Args:
        state: Snapshot to store; collected when omitted.
        path: Target file; defaults to :data:`STATE_FILE`.

    Returns:
        The written path, or None when writing failed.
    """
    target = path or STATE_FILE
    payload = state if state is not None else snapshot()
    temp = target.with_name(target.name + ".tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temp, target)
        return target
    except OSError:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        return None


def read_state(path: Path | None = None) -> dict:
    """Read a stored snapshot, tolerating anything broken.

    Args:
        path: Source file; defaults to :data:`STATE_FILE`.

    Returns:
        The snapshot, or an empty dict when missing/broken.
    """
    target = path or STATE_FILE
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def age_of(state: dict, name: str, now: float | None = None) -> float | None:
    """How old one reading is.

    Args:
        state: A snapshot.
        name: Reading name.
        now: Injectable clock.

    Returns:
        Seconds, or None when the reading is absent or has no value.
    """
    reading = (state.get("readings") or {}).get(name)
    if not isinstance(reading, dict) or reading.get("value") is None:
        return None
    stamp = reading.get("ts")
    if not isinstance(stamp, (int, float)):
        return None
    moment = time.time() if now is None else now
    return max(0.0, moment - float(stamp))


def name_of(state: dict) -> dict:
    """Pull the plain values out of a snapshot.

    Args:
        state: A snapshot.

    Returns:
        ``{name: value}`` with missing readings omitted.
    """
    out: dict = {}
    for name, reading in (state.get("readings") or {}).items():
        if isinstance(reading, dict) and reading.get("value") is not None:
            out[name] = reading["value"]
    return out


def describe(seconds: float | None) -> str:
    """Human text for an idle duration.

    Args:
        seconds: Idle seconds (None when unknown).

    Returns:
        Chinese phrase.
    """
    if seconds is None:
        return "不知道"
    if seconds < 60:
        return f"{int(seconds)} 秒"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟"
    return f"{seconds / 3600:.1f} 小时"


def summary(state: dict | None = None) -> str:
    """One-line Chinese summary for the pet bubble.

    Args:
        state: Snapshot to describe; collected when omitted.

    Returns:
        Chat-ready text.
    """
    data = state if state is not None else snapshot()
    values = name_of(data)
    idle = values.get("idle_seconds")
    locked = values.get("locked")
    app = values.get("foreground")
    cameras = values.get("cameras")
    parts = [f"你闲置了 {describe(idle)}" if idle is not None else "闲置时间不知道"]
    if locked is True:
        parts.append("屏幕锁着")
    elif locked is False:
        parts.append("屏幕没锁")
    if app:
        parts.append(f"前台是 {app}")
    if isinstance(cameras, list):
        parts.append("有摄像头" if cameras else "没看到摄像头")
    return "；".join(parts)
