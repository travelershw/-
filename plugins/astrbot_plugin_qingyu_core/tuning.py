"""运行时调参：不改代码、不重载就能改插嘴门槛（测试期用）。

门槛原本是 ``decide.py`` 里的常量，改一次要走"改文件 → 复制到 live 目录 → 重载插件"三步，
连着试几个值很烦。这里把可调的值放进 ``plugin_data/qingyu_tuning.json``，决策时读一次
（缓存 5 秒），群里用 ``/插嘴阈值`` 就能改——**代码里的常量始终是默认值，这里只是临时覆盖**，
删掉文件或发 ``/插嘴阈值 默认`` 就回去了。
"""

import json
import time
from pathlib import Path

from astrbot.core import logger
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from . import decide

PATH = Path(get_astrbot_plugin_data_path()) / "qingyu_tuning.json"
# 每这么多秒才重新读一次文件，别每条群消息都摸磁盘。
CACHE_SECONDS = 5.0
# 允许覆盖的键：键 -> (中文名, 最小值, 最大值)
KEYS: dict[str, tuple[str, float, float]] = {
    "chime_at": ("插嘴门槛", 0.2, 5.0),
    "chime_daily_cap": ("每日插嘴上限", 1.0, 60.0),
    # 机会点（L2）：概率超过它就算"时机到了"，不等随机闹钟。调高＝更挑时机也更安静。
    "chance_due": ("机会点门槛", 0.05, 0.95),
}
_DECIMAL_KEYS = ("chime_at", "chance_due")
_cache: dict[str, float] = {}
_cache_at = 0.0


def _default(key: str) -> float:
    """代码里的默认值。

    Args:
        key: Tuning key.

    Returns:
        The constant from :mod:`decide`.
    """
    if key == "chime_at":
        return float(decide.CHIME_AT)
    if key == "chance_due":
        return float(decide.CHANCE_DUE_AT)
    return float(decide.CHIME_DAILY_CAP)


def fmt(key: str, value: float) -> str:
    """把值写成给群里看的文本（概率/门槛保留小数，上限取整）。

    Args:
        key: Tuning key.
        value: Value to render.

    Returns:
        The display string.
    """
    if key not in _DECIMAL_KEYS:
        return f"{float(value):g}"
    text = f"{float(value):.2f}".rstrip("0")
    return text if not text.endswith(".") else text + "0"


def _read() -> dict[str, float]:
    """Parse the tuning file, dropping missing or out-of-range values.

    Returns:
        The accepted overrides.
    """
    try:
        raw = PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        logger.warning(f"qingyu_core: 调参文件读不了（{type(exc).__name__}），用默认值")
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("qingyu_core: 调参文件不是合法 JSON，用默认值")
        return {}
    if not isinstance(data, dict):
        return {}
    values: dict[str, float] = {}
    for key, (label, low, high) in KEYS.items():
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if low <= float(value) <= high:
            values[key] = float(value)
        else:
            logger.warning(f"qingyu_core: 调参 {label}={value} 超出 {low}~{high}，忽略")
    return values


def load() -> dict[str, float]:
    """Read the runtime overrides (cached for a few seconds).

    Returns:
        Accepted overrides, empty when there is no tuning file.
    """
    global _cache, _cache_at
    now = time.time()
    if now - _cache_at >= CACHE_SECONDS:
        _cache_at = now
        _cache = _read()
    return _cache


def chime_at() -> float:
    """Currently effective chime threshold.

    Returns:
        The override, or the code default.
    """
    return float(load().get("chime_at", _default("chime_at")))


def daily_cap() -> int:
    """Currently effective daily chime cap.

    Returns:
        The override, or the code default.
    """
    return int(load().get("chime_daily_cap", _default("chime_daily_cap")))


def chance_due() -> float:
    """Currently effective opportunity threshold (L2).

    Returns:
        The override, or the code default.
    """
    return float(load().get("chance_due", _default("chance_due")))


def save(key: str, value: float) -> str:
    """Write one override and return a chat-ready message.

    Args:
        key: Tuning key.
        value: New value.

    Returns:
        Text describing the result.
    """
    label, low, high = KEYS[key]
    if not low <= value <= high:
        return f"{label}只能填 {low:g}~{high:g} 之间的数哦~"
    try:
        data = json.loads(PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data[key] = value
    data["updated_at"] = int(time.time())
    try:
        PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        return f"写调参文件失败：{exc}"
    _forget_cache()
    logger.info(
        f"qingyu_core: 运行时调参 {label} = {fmt(key, value)}"
        f"（默认 {fmt(key, _default(key))}）",
    )
    return (
        f"{label}已改成 {fmt(key, value)}"
        f"（临时值，默认 {fmt(key, _default(key))}）。"
        "想再变就再发一次，想还原发「/插嘴阈值 默认」。"
    )


def clear() -> str:
    """Drop every override and return a chat-ready message.

    Returns:
        Text describing the result.
    """
    try:
        PATH.unlink(missing_ok=True)
    except OSError as exc:
        return f"删调参文件失败：{exc}"
    _forget_cache()
    logger.info("qingyu_core: 运行时调参已还原成代码默认值")
    return (
        f"好，临时值都清了：插嘴门槛 {fmt('chime_at', _default('chime_at'))}、"
        f"机会点门槛 {fmt('chance_due', _default('chance_due'))}、"
        f"每日上限 {fmt('chime_daily_cap', _default('chime_daily_cap'))}。"
    )


def describe() -> str:
    """Describe the effective values next to their defaults.

    Returns:
        Two lines for the status command.
    """
    overrides = load()
    lines = []
    for key, (label, _low, _high) in KEYS.items():
        default = _default(key)
        current = overrides.get(key)
        if current is None:
            lines.append(f"· {label}：{fmt(key, default)}（默认值）")
        else:
            lines.append(
                f"· {label}：{fmt(key, current)}（临时值，默认 {fmt(key, default)}）",
            )
    return "\n".join(lines)


def _forget_cache() -> None:
    """Force the next read to hit the file."""
    global _cache_at
    _cache_at = 0.0

