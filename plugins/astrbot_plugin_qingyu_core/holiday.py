"""Holiday greetings: which day is a holiday, when it may be sent, who got one.

Why this module owns the calendar: she has to greet **on the day**, so the
"what time is it / what day is it" part must be testable without a live bot.
Every function that reads the clock takes an injectable ``now``, which keeps
``migration_tools/test_holiday.py`` free of clock mocking.

Two layers of data:

- Solar holidays are fixed dates and live in :data:`SOLAR` (``MM-DD`` -> name and
  level).
- Lunar holidays have no fixed solar date, so their dates are configuration
  data: ``plugin_data/qingyu_holidays.json`` keeps explicit per-year entries
  (``{"date": "2026-09-25", "name": "中秋", "level": 1}``). Only one verified
  date ships as a default (:data:`DEFAULT_DATES`); every other lunar holiday has
  to be added by hand with ``/祝福 设 2027-02-06 春节`` so that a guessed date can
  never make her greet on the wrong day.

Levels: 1 is on by default, 2 stays off until ``level2_enabled`` is true.
Windows: 08:00-23:59 by default, per-holiday overrides in :data:`WINDOWS`; the
greeting may be sent at **any** moment inside the window, not only on the hour.
Dedupe lives in the ``meta`` table under ``greet:<group>:<holiday>:<date>``.
"""

import json
import time
from datetime import datetime
from pathlib import Path

from astrbot.core import logger
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from . import store

PATH = Path(get_astrbot_plugin_data_path()) / "qingyu_holidays.json"
# The config file is tiny, but one incoming message asks for it twice (group
# switch and pending greeting); a short cache avoids the second disk read.
CACHE_SECONDS = 5.0
DEFAULT_WINDOW = ("08:00", "23:59")
WINDOWS: dict[str, tuple[str, str]] = {
    "除夕": ("16:00", "23:59"),
    "春节": ("00:00", "12:00"),
    "元旦": ("00:00", "12:00"),
    "中秋": ("17:00", "23:59"),
}
SOLAR: dict[str, tuple[str, int]] = {
    "01-01": ("元旦", 1),
    "10-01": ("国庆", 1),
    "02-14": ("情人节", 2),
    "03-08": ("妇女节", 2),
    "05-01": ("劳动节", 2),
    "06-01": ("儿童节", 2),
    "09-10": ("教师节", 2),
    "12-24": ("平安夜", 2),
    "12-25": ("圣诞", 2),
}
# Lunar holidays: level and window are known, the dates are not (see the module
# docstring). Level 2 names stay off until the switch is turned on.
LUNAR_LEVELS: dict[str, int] = {
    "除夕": 1,
    "春节": 1,
    "中秋": 1,
    "元宵": 2,
    "端午": 2,
    "七夕": 2,
    "重阳": 2,
    "腊八": 2,
}
# 2026-09-25 is the only lunar date verified so far; do not add guesses here.
DEFAULT_DATES: tuple[dict, ...] = ({"date": "2026-09-25", "name": "中秋", "level": 1},)
DEFAULT_MODE_B = ("除夕", "元旦", "春节")
DEFAULT_MIN_GAP_SECONDS = 180
MAX_MIN_GAP_SECONDS = 3600
# The proactive (mode B) task wakes up this often, and only greets a group that
# spoke within ACTIVE_WINDOW_SECONDS: a silent group needs no greeting.
MODE_B_INTERVAL_SECONDS = 300
ACTIVE_WINDOW_SECONDS = 24 * 3600

_cache: dict = {}
_cache_at = 0.0


def _solar_names() -> set[str]:
    """Names of the fixed-date holidays.

    Returns:
        The solar holiday names.
    """
    return {name for name, _level in SOLAR.values()}


def level_of(name: str) -> int:
    """Default level of a holiday name.

    Args:
        name: Holiday name.

    Returns:
        1 or 2. Unknown names get 2, so a typo stays off by default.
    """
    for holiday_name, level in SOLAR.values():
        if holiday_name == name:
            return level
    return LUNAR_LEVELS.get(name, 2)


def window_of(name: str) -> tuple[str, str]:
    """Sending window of a holiday.

    Args:
        name: Holiday name.

    Returns:
        ``(start, end)`` as ``"HH:MM"`` strings.
    """
    return WINDOWS.get(name, DEFAULT_WINDOW)


def window_text(name: str) -> str:
    """Human-readable sending window.

    Args:
        name: Holiday name.

    Returns:
        Text such as ``"17:00-23:59"``.
    """
    start, end = window_of(name)
    return f"{start}-{end}"


def _to_minutes(text: str) -> int:
    """Convert a ``"HH:MM"`` bound into minutes since midnight.

    Args:
        text: Window bound.

    Returns:
        Minutes since midnight, 0 when the text is malformed.
    """
    hour, _, minute = str(text).partition(":")
    if not (hour.isdigit() and minute.isdigit()):
        return 0
    return min(23, int(hour)) * 60 + min(59, int(minute))


def in_window(name: str, now: datetime | None = None) -> bool:
    """Whether a holiday may be greeted at this moment.

    Args:
        name: Holiday name.
        now: Injectable clock; defaults to the local current time.

    Returns:
        True inside the window (both ends inclusive), so the greeting is a
        "same-day catch-up" and does not have to land on the hour.
    """
    moment = now or datetime.now()
    start, end = window_of(name)
    minutes = moment.hour * 60 + moment.minute
    return _to_minutes(start) <= minutes <= _to_minutes(end)


def _valid_day(text: str) -> bool:
    """Whether the text is a real ``YYYY-MM-DD`` date.

    Args:
        text: Candidate date text.

    Returns:
        True when :func:`datetime.strptime` accepts it.
    """
    try:
        datetime.strptime(str(text), "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _default_dates() -> list[dict]:
    """Copy of the built-in lunar dates.

    Returns:
        Mutable copies, so callers cannot mutate the module constant.
    """
    return [dict(item) for item in DEFAULT_DATES]


def _clean_dates(raw) -> list[dict]:
    """Keep the well-formed entries of a ``dates`` list.

    Args:
        raw: Value read from the config file.

    Returns:
        Entries shaped ``{"date", "name", "level"}``; bad ones are dropped with
        a warning instead of raising.
    """
    dates: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in raw:
        if not isinstance(item, dict):
            logger.warning(
                "qingyu_core: holiday config date entry is not an object, dropped"
            )
            continue
        day = str(item.get("date") or "").strip()
        name = str(item.get("name") or "").strip()
        if not _valid_day(day) or not name or len(name) > 8:
            logger.warning(
                f"qingyu_core: holiday config date entry ignored (date={day!r} name={name!r})"
            )
            continue
        level = item.get("level")
        if level not in (1, 2):
            level = level_of(name)
        if (day, name) in seen:
            continue
        seen.add((day, name))
        dates.append({"date": day, "name": name, "level": int(level)})
    return dates


def _normalize(data: dict) -> dict:
    """Turn the parsed config file into the effective config.

    Args:
        data: Parsed JSON object (possibly empty).

    Returns:
        ``{"level2_enabled", "min_gap_seconds", "mode_b", "groups", "dates"}``
        with every unusable value replaced by its default.
    """
    level2 = data.get("level2_enabled", False)
    if not isinstance(level2, bool):
        logger.warning(
            "qingyu_core: holiday config level2_enabled is not true/false, using false"
        )
        level2 = False

    gap = data.get("min_gap_seconds", DEFAULT_MIN_GAP_SECONDS)
    if (
        isinstance(gap, bool)
        or not isinstance(gap, (int, float))
        or not 0 <= float(gap) <= MAX_MIN_GAP_SECONDS
    ):
        logger.warning(
            f"qingyu_core: holiday config min_gap_seconds={gap!r} outside "
            f"0~{MAX_MIN_GAP_SECONDS}, using {DEFAULT_MIN_GAP_SECONDS}"
        )
        gap = DEFAULT_MIN_GAP_SECONDS

    raw_mode_b = data.get("mode_b", list(DEFAULT_MODE_B))
    if not isinstance(raw_mode_b, list):
        logger.warning(
            "qingyu_core: holiday config mode_b is not a list, using the default set"
        )
        mode_b = list(DEFAULT_MODE_B)
    else:
        mode_b = [str(item) for item in raw_mode_b if str(item) in _all_names()]
        if len(mode_b) != len(raw_mode_b):
            logger.warning(
                "qingyu_core: holiday config mode_b has unknown names, dropped"
            )

    groups: dict[str, bool] = {}
    raw_groups = data.get("groups", {})
    if not isinstance(raw_groups, dict):
        logger.warning("qingyu_core: holiday config groups is not an object, ignored")
    else:
        for key, value in raw_groups.items():
            if isinstance(value, bool):
                groups[str(key)] = value
            else:
                logger.warning(
                    f"qingyu_core: holiday config group {key!r} is not true/false, ignored"
                )

    raw_dates = data.get("dates")
    if isinstance(raw_dates, list):
        dates = _clean_dates(raw_dates)
    elif raw_dates is None:
        dates = _default_dates()
    else:
        logger.warning(
            "qingyu_core: holiday config dates is not a list, using the built-in date"
        )
        dates = _default_dates()

    return {
        "level2_enabled": level2,
        "min_gap_seconds": int(gap),
        "mode_b": mode_b,
        "groups": groups,
        "dates": dates,
    }


def _all_names() -> set[str]:
    """Every holiday name the calendar knows about.

    Returns:
        Solar names plus lunar names.
    """
    return _solar_names() | set(LUNAR_LEVELS)


def _read() -> dict:
    """Read the config file, falling back to defaults on any problem.

    Returns:
        The effective config. Missing files, broken JSON and out-of-range values
        all degrade to defaults with a warning instead of raising.
    """
    try:
        raw = PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _normalize({})
    except OSError as exc:
        logger.warning(
            f"qingyu_core: holiday config unreadable ({type(exc).__name__}), using defaults"
        )
        return _normalize({})
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("qingyu_core: holiday config is not valid JSON, using defaults")
        return _normalize({})
    if not isinstance(data, dict):
        logger.warning(
            "qingyu_core: holiday config is not a JSON object, using defaults"
        )
        return _normalize({})
    return _normalize(data)


def load() -> dict:
    """Effective holiday config, cached for a few seconds.

    Returns:
        The merged config (built-in defaults when the file is missing or broken).
    """
    global _cache, _cache_at
    now = time.monotonic()
    if now - _cache_at >= CACHE_SECONDS:
        _cache_at = now
        _cache = _read()
    return _cache


def forget_cache() -> None:
    """Force the next :func:`load` to hit the file."""
    global _cache_at
    _cache_at = 0.0


def entries_on(day: str, config: dict | None = None) -> list[dict]:
    """Holidays that fall on one calendar day.

    Args:
        day: Day as ``"YYYY-MM-DD"``.
        config: Effective config; read from disk when omitted.

    Returns:
        Entries shaped ``{"date", "name", "level", "window", "mode_b"}``, level 1
        first and names sorted, so the caller always sees a stable order.
    """
    cfg = config or load()
    month_day = str(day)[5:]
    found: dict[str, dict] = {}
    solar = SOLAR.get(month_day)
    if solar:
        name, level = solar
        found[name] = {"date": day, "name": name, "level": level}
    for item in cfg["dates"]:
        if item["date"] == day:
            found[item["name"]] = {
                "date": day,
                "name": item["name"],
                "level": item["level"],
            }
    entries = []
    for item in found.values():
        entries.append(
            {
                **item,
                "window": window_text(item["name"]),
                "mode_b": item["name"] in cfg["mode_b"],
            }
        )
    entries.sort(key=lambda entry: (entry["level"], entry["name"]))
    return entries


def today(now: datetime | None = None, config: dict | None = None) -> list[dict]:
    """Holidays that fall on the current day.

    Args:
        now: Injectable clock; defaults to the local current time.
        config: Effective config; read from disk when omitted.

    Returns:
        The same shape as :func:`entries_on`.
    """
    moment = now or datetime.now()
    return entries_on(moment.date().isoformat(), config)


def greet_key(group_id: str, name: str, day: str) -> str:
    """Key that remembers one group's greeting for one holiday on one day.

    Args:
        group_id: Platform group id.
        name: Holiday name.
        day: Day as ``"YYYY-MM-DD"``.

    Returns:
        The ``meta`` key ``greet:<group>:<holiday>:<date>``.
    """
    return f"greet:{group_id}:{name}:{day}"


def already_greeted(connection, group_id: str, name: str, day: str) -> bool:
    """Whether this group already got its greeting for that day.

    Args:
        connection: Open core database connection.
        group_id: Platform group id.
        name: Holiday name.
        day: Day as ``"YYYY-MM-DD"``.

    Returns:
        True when the greeting was recorded before.
    """
    return bool(store.get_meta(connection, greet_key(group_id, name, day), ""))


def mark_greeted(connection, group_id: str, name: str, day: str, ts: int) -> None:
    """Record that this group got its greeting.

    Args:
        connection: Open core database connection.
        group_id: Platform group id.
        name: Holiday name.
        day: Day as ``"YYYY-MM-DD"``.
        ts: Timestamp to store.
    """
    store.set_meta(connection, greet_key(group_id, name, day), str(int(ts)))


def pending(
    connection,
    group_id: str,
    *,
    now: datetime | None = None,
    mode_b: bool | None = None,
    config: dict | None = None,
) -> dict | None:
    """Today's holiday this group still owes a greeting for, if any.

    Args:
        connection: Open core database connection.
        group_id: Platform group id.
        now: Injectable clock; defaults to the local current time.
        mode_b: ``True`` keeps only the proactively greeted holidays, ``False``
            keeps only the message-triggered ones, ``None`` keeps both.
        config: Effective config; read from disk when omitted.

    Returns:
        The first enabled entry that is inside its window and not greeted yet,
        otherwise None.
    """
    cfg = config or load()
    moment = now or datetime.now()
    day = moment.date().isoformat()
    for entry in entries_on(day, cfg):
        if entry["level"] == 2 and not cfg["level2_enabled"]:
            continue
        if mode_b is not None and entry["mode_b"] != mode_b:
            continue
        if not in_window(entry["name"], moment):
            continue
        if already_greeted(connection, group_id, entry["name"], day):
            continue
        return entry
    return None


def min_gap_seconds(config: dict | None = None) -> int:
    """Smallest gap between her last message and a holiday wake-up.

    Args:
        config: Effective config; read from disk when omitted.

    Returns:
        Seconds.
    """
    return int((config or load())["min_gap_seconds"])


def group_override(group_id: str, config: dict | None = None) -> bool | None:
    """Explicit per-group switch from the config file.

    Args:
        group_id: Platform group id.
        config: Effective config; read from disk when omitted.

    Returns:
        True/False when the group is listed, otherwise None (follow the
        interjection whitelist).
    """
    return (config or load())["groups"].get(str(group_id))


def greet_instruction(name: str, cap: int) -> str:
    """Instruction asking her for one holiday line.

    Args:
        name: Holiday name.
        cap: Character budget for the greeting.

    Returns:
        One bracketed instruction, shared by the message-triggered and the
        proactive path so that both sound the same.
    """
    return (
        f"【今天是{name}。群里还没人提，你自己先冒个泡：像普通群友过节那样随口说一句，"
        "别写成通知，也不要群发腔（不要「祝大家…」、不要排比、不要堆感叹号），"
        "可以提一件在吃什么、在干嘛、想起谁这样的具体小事。"
        f"一句到两句、{cap} 个字以内；只输出要发的那句话，不要引号、不要解释。】"
    )


def greet_prompt(name: str) -> str:
    """User prompt for the one-off proactive call.

    Args:
        name: Holiday name.

    Returns:
        A short prompt; the instruction itself travels in the system prompt of
        that standalone call.
    """
    return f"今天是{name}，现在发一句吧。"


def _patch(changes: dict) -> str | None:
    """Merge keys into the config file, keeping every other key.

    Args:
        changes: Keys to write.

    Returns:
        An error message, or None when the write succeeded.
    """
    try:
        data = json.loads(PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data.update(changes)
    data["updated_at"] = int(time.time())
    try:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        return f"写节日配置文件失败：{exc}"
    forget_cache()
    return None


def save_date(day: str, name: str) -> tuple[dict | None, str]:
    """Add one explicit holiday date to the config file.

    Args:
        day: Date as ``"YYYY-MM-DD"``.
        name: Holiday name, for example 春节 or 中秋.

    Returns:
        ``(entry, message)``; ``entry`` is None when the input was rejected.
    """
    day = str(day or "").strip()
    name = str(name or "").strip()
    if not _valid_day(day):
        return None, "日期要写成 YYYY-MM-DD，比如：/祝福 设 2027-02-06 春节"
    if not name or len(name) > 8:
        return None, "节日名要写 1~8 个字，比如：/祝福 设 2027-02-06 春节"
    entry = {"date": day, "name": name, "level": level_of(name)}
    dates = [
        item for item in load()["dates"] if item["date"] != day or item["name"] != name
    ]
    dates.append(entry)
    dates.sort(key=lambda item: (item["date"], item["name"]))
    error = _patch({"dates": dates})
    if error:
        return None, error
    tail = (
        "（level 2 默认不发，配置里 level2_enabled 改成 true 才会发）"
        if entry["level"] == 2
        else ""
    )
    return entry, (
        f"记下了：{day} 是{name}，窗口 {window_text(name)}，level {entry['level']}{tail}。"
    )


def set_group(group_id: str, enabled: bool) -> str:
    """Switch holiday greetings on or off for one group.

    Args:
        group_id: Platform group id.
        enabled: New state.

    Returns:
        A chat-ready message.
    """
    groups = dict(load()["groups"])
    groups[str(group_id)] = bool(enabled)
    error = _patch({"groups": groups})
    if error:
        return error
    if enabled:
        return (
            f"好，{group_id} 的节日祝福开了（level 1 的节日都会发；"
            "level 2 的节日默认不发）。"
        )
    return f"好，{group_id} 的节日祝福关了。"


def table_text() -> str:
    """Render the holiday table for ``/祝福 列表``.

    Returns:
        Multi-line text: the solar table, the configured lunar dates, the
        switches, and how to fill in a missing lunar date.
    """
    config = load()
    lines = ["公历节日（每年固定）："]
    for month_day, (name, level) in sorted(SOLAR.items()):
        lines.append(f"· {name} {month_day}　level {level}　窗口 {window_text(name)}")
    filled: dict[str, list[str]] = {}
    for item in config["dates"]:
        filled.setdefault(item["name"], []).append(
            f"{item['date']}（level {item['level']}）"
        )
    lines.append("农历节日（日期是配置数据，代码里不猜）：")
    for name in LUNAR_LEVELS:
        dates = filled.get(name)
        if dates:
            lines.append(f"· {name}：{'、'.join(dates)}　窗口 {window_text(name)}")
        else:
            lines.append(f"· {name}：还没填日期")
    for name, dates in filled.items():
        if name not in LUNAR_LEVELS and name not in _solar_names():
            lines.append(f"· {name}（自己加的）：{'、'.join(dates)}")
    lines.append("· 农历节日需自行补日期（/祝福 设 2027-02-06 春节）")
    mode_b = "、".join(config["mode_b"]) or "无"
    lines.append(
        f"主动发（不用等人先说话）：{mode_b}，每 {MODE_B_INTERVAL_SECONDS // 60} 分钟看一眼"
    )
    if config["level2_enabled"]:
        lines.append("级别：level 1 与 level 2 都会发")
    else:
        lines.append(
            "级别：level 1 会发；level 2 默认不发（level2_enabled 改成 true 才发）"
        )
    lines.append(f"叫醒门槛：距上次开口 ≥ {config['min_gap_seconds']} 秒")
    lines.append(f"配置文件：{PATH}")
    return "\n".join(lines)
