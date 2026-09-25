"""世界状态层：把散落的状态收成一份"快照"。

慢变量（好感/信任/熟悉度）按人，快变量（精力/心情）按群，外加时间上下文与
最近行为。所有模块都只通过这份快照读状态，不再各自去翻 JSON。
"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from astrbot.core import logger
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from . import store

AFFECTION_STORE = Path(get_astrbot_plugin_data_path()) / "qingyu_affection.json"
# 关系插件没给出信任值时的兜底（和 Person.trust 的默认值保持一致）。
# 注意：**不要**引用另一个插件里的常量——2026-09-25 我在这里写了 `INITIAL_TRUST`（它定义在
# qingyu_affection 里），结果 `sync_affection` 在插件加载时抛 NameError，**整个 qingyu_core
# 加载失败**。插件之间不走 import，常量必须各自定义。
DEFAULT_TRUST = 60
# 心情回落到中位的速度：每小时回落多少点。
MOOD_DECAY_PER_HOUR = 6.0
MOOD_MIN = 30
MOOD_MAX = 90
WEEKDAYS = ["一", "二", "三", "四", "五", "六", "日"]
# 群里连续说话超过这个条数就算灌水，会压低说话意愿。
FLOOD_WINDOW_SECONDS = 60
FLOOD_MESSAGES = 6
# 精力按"消息量"掉，但不是每来一条就掉一点——水群里那样十分钟就见底，
# "还有精神"这个信号会永远不生效。每 ENERGY_DRAIN_EVERY 条扣 1 点：
# 每小时 20 条以上才需要扣，所以每小时 120 条以内的群她完全不累；
# 真·水群（每小时两三百条）也要好几个小时才会磨到下限。想更宽松就调大这个数。
ENERGY_WINDOW_SECONDS = 300
ENERGY_DRAIN_EVERY = 20
# ---- 阶段二：心情事件源 ----
# 群里说她好话 / 骂她，直接改心情。只认明确信号，避免误判。
PRAISE_WORDS = (
    "谢谢", "谢了", "厉害", "太强", "牛", "好评", "赞", "可爱", "喜欢你", "爱你",
    "乖", "有用", "帮大忙", "靠谱", "真棒",
)
ABUSE_WORDS = (
    "滚", "傻", "蠢", "笨", "闭嘴", "垃圾", "废物", "神经病", "去死", "烦人", "弱智",
)
PRAISE_MOOD, PRAISE_ENERGY = 2, 2
ABUSE_MOOD, ABUSE_ENERGY = -3, -1
# 被点名（有人专门跟她说话）会让她有点精神。
DIRECT_MOOD = 1
# 她说过话之后，多久没人搭理就算"被无视"。
IGNORED_AFTER_SECONDS = 180
IGNORED_MIN_MESSAGES = 3
IGNORED_MOOD, IGNORED_ENERGY = -2, -1
# 终于有人接她的话。
ANSWERED_MOOD = 2
# 她自己开口的消耗（说话费神）。
SPEAK_ENERGY, SPEAK_MOOD = -2, 1
MOOD_REASON_KEY = "mood_last:{group}"
IGNORED_KEY = "ignored_check:{group}"


@dataclass
class Person:
    """一个人在系统里的样子。"""

    uid: str
    nickname: str = ""
    affection: int = 60
    trust: int = 60
    familiarity: int = 0
    msg_count: int = 0
    last_seen: int = 0


@dataclass
class Mood:
    """轻语在当前群的状态。"""

    group_id: str
    energy: int = 70
    mood: int = 65
    updated_at: int = 0


@dataclass
class Clock:
    """时间上下文，影响回复长度与语气。"""

    now: datetime
    hour: int
    weekday: str
    is_night: bool
    is_exam_week: bool

    def describe(self) -> str:
        """Return a one-line description used in prompts.

        Returns:
            A Chinese description of the current time context.
        """
        flags = []
        if self.is_night:
            flags.append("深夜")
        if self.is_exam_week:
            flags.append("考试周")
        suffix = f"（{'、'.join(flags)}）" if flags else ""
        return (
            f"{self.now.year}年{self.now.month:02d}月{self.now.day:02d}日 "
            f"{self.hour:02d}:{self.now.minute:02d}（星期{self.weekday}）{suffix}"
        )


@dataclass
class Snapshot:
    """一次决策需要的全部状态。"""

    person: Person
    mood: Mood
    clock: Clock
    group_id: str
    recent: list[dict] = field(default_factory=list)
    flood: bool = False
    seconds_since_bot: float = 99999.0
    chimes_today: int = 0

    def describe(self) -> str:
        """Return a compact description for logs and prompts.

        Returns:
            A short Chinese summary of the state.
        """
        return (
            f"{self.person.nickname or self.person.uid} 好感 {self.person.affection} "
            f"熟悉 {self.person.familiarity} | 精力 {self.mood.energy} 心情 {self.mood.mood} "
            f"| {self.clock.describe()}"
        )


def _decay(value: int, baseline: int, updated_at: int, now_ts: int) -> int:
    """Move a fast variable back towards its baseline over time.

    Args:
        value: Stored value.
        baseline: Value the state decays towards.
        updated_at: When it was last written.
        now_ts: Current timestamp.

    Returns:
        The decayed value.
    """
    if updated_at <= 0:
        return value
    hours = max(0.0, (now_ts - updated_at) / 3600.0)
    if hours <= 0:
        return value
    delta = value - baseline
    if delta == 0:
        return value
    shrink = min(abs(delta), MOOD_DECAY_PER_HOUR * hours)
    decayed = value - shrink if delta > 0 else value + shrink
    return int(max(MOOD_MIN, min(MOOD_MAX, decayed)))


def is_exam_week(now: datetime) -> bool:
    """Whether the date is likely inside an exam period.

    Args:
        now: Current local time.

    Returns:
        True for the two common exam windows in a Chinese university year.
    """
    month, day = now.month, now.day
    if month == 1 and day >= 5:
        return True
    if month == 6 and 10 <= day <= 30:
        return True
    if month == 7 and day <= 10:
        return True
    return False


def build_clock(now: datetime | None = None) -> Clock:
    """Build the time context for a decision.

    Args:
        now: Override for tests; defaults to the current local time.

    Returns:
        The clock context.
    """
    current = now or datetime.now()
    return Clock(
        now=current,
        hour=current.hour,
        weekday=WEEKDAYS[current.weekday()],
        is_night=current.hour >= 23 or current.hour < 7,
        is_exam_week=is_exam_week(current),
    )


def remember_person(
    connection,
    uid: str,
    nickname: str = "",
    *,
    ts: int | None = None,
) -> None:
    """Upsert a person and bump their familiarity counters.

    Args:
        connection: Open core database connection.
        uid: Sender id.
        nickname: Display name as seen now.
        ts: Event timestamp; defaults to now.
    """
    stamp = ts or store.now()
    if not uid:
        return
    row = connection.execute(
        "SELECT nickname, aliases FROM persons WHERE uid = ?",
        (uid,),
    ).fetchone()
    if row is None:
        connection.execute(
            "INSERT INTO persons (uid, nickname, aliases, first_seen, last_seen, msg_count)"
            " VALUES (?, ?, '', ?, ?, 1)",
            (uid, nickname, stamp, stamp),
        )
    else:
        aliases = row["aliases"] or ""
        known = [item for item in aliases.split(",") if item]
        if nickname and nickname != row["nickname"] and nickname not in known:
            known.append(nickname)
        connection.execute(
            "UPDATE persons SET nickname = ?, aliases = ?, last_seen = ?,"
            " msg_count = msg_count + 1 WHERE uid = ?",
            (
                nickname or row["nickname"],
                ",".join(known[-5:]),
                stamp,
                uid,
            ),
        )
    connection.execute(
        "INSERT INTO relations (uid, affection, trust, familiarity, updated_at)"
        " VALUES (?, 60, 60, 1, ?)"
        " ON CONFLICT(uid) DO UPDATE SET"
        " familiarity = familiarity + 1, updated_at = excluded.updated_at",
        (uid, stamp),
    )
    connection.commit()


def sync_affection(connection) -> int:
    """Mirror the relationship store into ``relations`` (affection + trust).

    读取路径已经改成现读（:func:`read_affection_store`），这里只做**入库镜像**，
    方便统计与排障；`familiarity` 由本插件自己维护，**不在这里覆盖**。

    Args:
        connection: Open core database connection.

    Returns:
        How many rows were updated.
    """
    data = read_affection_store()
    if not data:
        return 0
    stamp = store.now()
    changed = 0
    for uid, values in data.items():
        affection = values.get("affection") if isinstance(values, dict) else values
        if not isinstance(affection, int | float):
            continue
        trust = values.get("trust") if isinstance(values, dict) else None
        cursor = connection.execute(
            "INSERT INTO relations (uid, affection, trust, familiarity, updated_at)"
            " VALUES (?, ?, ?, 0, ?)"
            " ON CONFLICT(uid) DO UPDATE SET affection = excluded.affection,"
            " trust = COALESCE(excluded.trust, relations.trust),"
            " updated_at = excluded.updated_at",
            (
                str(uid),
                int(affection),
                int(trust) if isinstance(trust, int | float) else DEFAULT_TRUST,
                stamp,
            ),
        )
        changed += max(1, cursor.rowcount)
    connection.commit()
    return changed


def touch_group(connection, group_id: str, umo: str, ts: int) -> None:
    """Record that a group saw activity, and store the group identity.

    Args:
        connection: Open core database connection.
        group_id: Platform group id.
        umo: Unified message origin for the group.
        ts: Event timestamp.
    """
    if not group_id:
        return
    connection.execute(
        "INSERT INTO mood (group_id, energy, mood, updated_at) VALUES (?, 70, 65, ?)"
        " ON CONFLICT(group_id) DO NOTHING",
        (group_id, ts),
    )
    store.set_meta(connection, f"umo:{group_id}", umo)


def group_of(connection, umo: str) -> str:
    """Find the group id stored for a session.

    Args:
        connection: Open core database connection.
        umo: Unified message origin.

    Returns:
        The group id, or an empty string for non-group sessions.
    """
    row = connection.execute(
        "SELECT value FROM meta WHERE key LIKE 'umo:%' AND value = ? LIMIT 1",
        (umo,),
    ).fetchone()
    if not row:
        return ""
    return str(row["value"]).split(":")[-1]


def bump_mood(
    connection,
    group_id: str,
    *,
    energy: int = 0,
    mood: int = 0,
    ts: int | None = None,
    reason: str = "",
) -> None:
    """Change the fast variables for a group, clamped to their range.

    Args:
        connection: Open core database connection.
        group_id: Platform group id.
        energy: Energy delta.
        mood: Mood delta.
        ts: Timestamp for the update.
        reason: Why the state changed; kept for ``/轻语状态`` and the log.
    """
    if not group_id or (energy == 0 and mood == 0):
        return
    stamp = ts or store.now()
    current = load_mood(connection, group_id, stamp)
    connection.execute(
        "INSERT INTO mood (group_id, energy, mood, updated_at) VALUES (?, ?, ?, ?)"
        " ON CONFLICT(group_id) DO UPDATE SET energy = ?, mood = ?, updated_at = ?",
        (
            group_id,
            max(MOOD_MIN, min(MOOD_MAX, current.energy + energy)),
            max(MOOD_MIN, min(MOOD_MAX, current.mood + mood)),
            stamp,
            max(MOOD_MIN, min(MOOD_MAX, current.energy + energy)),
            max(MOOD_MIN, min(MOOD_MAX, current.mood + mood)),
            stamp,
        ),
    )
    if reason:
        store.set_meta(
            connection,
            MOOD_REASON_KEY.format(group=group_id),
            f"{reason}｜精力 {energy:+d} 心情 {mood:+d}｜"
            f"{datetime.fromtimestamp(stamp):%m-%d %H:%M}",
        )
    connection.commit()
    if not reason:
        return  # 按消息量扣精力这种常规动作不打日志
    updated = load_mood(connection, group_id, stamp)
    logger.info(
        f"qingyu_core: 心情事件 [{group_id}] {reason} "
        f"精力 {energy:+d} 心情 {mood:+d} -> 精力 {updated.energy} 心情 {updated.mood}",
    )


def react_to_message(
    connection,
    *,
    group_id: str,
    umo: str,
    text: str,
    is_wake: bool,
    ts: int,
) -> list[str]:
    """Derive mood/energy changes from one incoming message (阶段二).

    事件源：被夸、被怼、被点名、被无视、终于有人接话。

    Args:
        connection: Open core database connection.
        group_id: Platform group id.
        umo: Unified message origin.
        text: Message text.
        is_wake: Whether the bot was addressed.
        ts: Event timestamp.

    Returns:
        The list of applied events, for logging and tests.
    """
    if not group_id:
        return []
    events: list[str] = []
    body = text or ""
    abuse = next((word for word in ABUSE_WORDS if word in body), "")
    praise = next((word for word in PRAISE_WORDS if word in body), "")

    if abuse:
        bump_mood(
            connection,
            group_id,
            mood=ABUSE_MOOD,
            energy=ABUSE_ENERGY,
            ts=ts,
            reason=f"被怼（“{abuse}”）",
        )
        events.append(f"被怼 mood{ABUSE_MOOD:+d} 精力{ABUSE_ENERGY:+d}")
    elif praise:
        bump_mood(
            connection,
            group_id,
            mood=PRAISE_MOOD,
            energy=PRAISE_ENERGY,
            ts=ts,
            reason=f"被夸（“{praise}”）",
        )
        events.append(f"被夸 mood{PRAISE_MOOD:+d} 精力{PRAISE_ENERGY:+d}")

    if is_wake:
        bump_mood(connection, group_id, mood=DIRECT_MOOD, ts=ts, reason="有人专门找她说话")
        events.append(f"被点名 mood{DIRECT_MOOD:+d}")

    # 被无视 / 被接话：看她上一次开口之后发生了什么。
    key = IGNORED_KEY.format(group=group_id)
    pending = store.get_meta(connection, key, "")
    if pending.isdigit() and int(pending) > 0:
        spoke_at = int(pending)
        waited = ts - spoke_at
        if waited >= IGNORED_AFTER_SECONDS:
            if is_wake:
                bump_mood(
                    connection,
                    group_id,
                    mood=ANSWERED_MOOD,
                    ts=ts,
                    reason="终于有人接她的话",
                )
                events.append(f"被接话 mood{ANSWERED_MOOD:+d}")
                store.set_meta(connection, key, "0")
            else:
                since = recent_message_count(
                    connection,
                    umo,
                    ts,
                    max(waited, 1),
                )
                if since >= IGNORED_MIN_MESSAGES:
                    bump_mood(
                        connection,
                        group_id,
                        mood=IGNORED_MOOD,
                        energy=IGNORED_ENERGY,
                        ts=ts,
                        reason=f"说了话没人理（{since} 条之后）",
                    )
                    events.append(f"被无视 mood{IGNORED_MOOD:+d} 精力{IGNORED_ENERGY:+d}")
                    store.set_meta(connection, key, "0")
    return events


def note_bot_spoke(connection, group_id: str, ts: int) -> None:
    """她开口了：记下时间，之后用来判断有没有人接。

    Args:
        connection: Open core database connection.
        group_id: Platform group id.
        ts: When she spoke.
    """
    if not group_id:
        return
    store.set_meta(connection, IGNORED_KEY.format(group=group_id), str(ts))
    bump_mood(
        connection,
        group_id,
        energy=SPEAK_ENERGY,
        mood=SPEAK_MOOD,
        ts=ts,
        reason="她自己说了话",
    )


def repeat_asker(
    connection,
    umo: str,
    uid: str,
    ts: int,
    *,
    window_seconds: int = 120,
    threshold: int = 3,
) -> bool:
    """Whether this person has been pestering her in the last couple of minutes.

    Args:
        connection: Open core database connection.
        umo: Unified message origin.
        uid: Sender id.
        ts: Current timestamp.
        window_seconds: How far back to count.
        threshold: How many messages count as pestering.

    Returns:
        True when they sent at least ``threshold`` messages in the window.
    """
    row = connection.execute(
        "SELECT COUNT(*) AS n FROM turns WHERE umo = ? AND uid = ? AND ts >= ?",
        (umo, uid, ts - window_seconds),
    ).fetchone()
    return bool(row and int(row["n"]) >= threshold)


def last_mood_reason(connection, group_id: str) -> str:
    """Read the most recent mood-change reason.

    Args:
        connection: Open core database connection.
        group_id: Platform group id.

    Returns:
        A short description, or an empty string.
    """
    return store.get_meta(connection, MOOD_REASON_KEY.format(group=group_id), "")


def load_mood(connection, group_id: str, ts: int | None = None) -> Mood:
    """Read a group's fast variables, applying decay since the last update.

    Args:
        connection: Open core database connection.
        group_id: Platform group id.
        ts: Current timestamp.

    Returns:
        The decayed mood state.
    """
    stamp = ts or store.now()
    row = connection.execute(
        "SELECT energy, mood, updated_at FROM mood WHERE group_id = ?",
        (group_id,),
    ).fetchone()
    if row is None:
        return Mood(group_id=group_id, updated_at=stamp)
    return Mood(
        group_id=group_id,
        energy=_decay(int(row["energy"]), 70, int(row["updated_at"]), stamp),
        mood=_decay(int(row["mood"]), 65, int(row["updated_at"]), stamp),
        updated_at=int(row["updated_at"]),
    )


_STORE_CACHE: dict = {"mtime": -1.0, "data": {}}


def read_affection_store() -> dict:
    """Read the relationship store written by ``qingyu_affection`` (mtime-cached).

    为什么要**现读**而不是每 5 分钟同步一次：那个延迟会让"她刚被夸完，语气还是旧的"，
    这是最不真实的一种表现（2026-09-25 从 `AFFECTION_SYNC_SECONDS = 300` 改成现读）。
    文件很小，按 mtime 缓存，所以每条消息只多一次 `stat`。

    Returns:
        ``{uid: {"affection": int, "trust": int, ...}}``（空字典表示还没写过）。
    """
    try:
        stamp = AFFECTION_STORE.stat().st_mtime
    except OSError:
        return {}
    if stamp == _STORE_CACHE["mtime"]:
        return _STORE_CACHE["data"]
    raw = {}
    try:
        raw = json.loads(AFFECTION_STORE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    people: dict = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            if isinstance(value, bool):
                continue
            if isinstance(value, int | float):  # 旧格式：就是一个好感度分数
                people[str(key)] = {"affection": int(value), "trust": None}
            elif isinstance(value, dict):
                people[str(key)] = {
                    "affection": (
                        int(value["affection"])
                        if isinstance(value.get("affection"), int | float)
                        else None
                    ),
                    "trust": (
                        int(value["trust"])
                        if isinstance(value.get("trust"), int | float)
                        else None
                    ),
                }
    _STORE_CACHE["mtime"], _STORE_CACHE["data"] = stamp, people
    return people


def load_person(connection, uid: str) -> Person:
    """Read one person together with their slow variables.

    好感与信任**以关系插件的存储为准**（它是唯一写入方），现读、不吃同步延迟；
    熟悉度仍由本插件维护（每条消息 +1）。

    Args:
        connection: Open core database connection.
        uid: Sender id.

    Returns:
        The person, with defaults filled in when unknown.
    """
    person_row = connection.execute(
        "SELECT nickname, last_seen, msg_count FROM persons WHERE uid = ?",
        (uid,),
    ).fetchone()
    relation_row = connection.execute(
        "SELECT affection, trust, familiarity FROM relations WHERE uid = ?",
        (uid,),
    ).fetchone()
    person = Person(uid=uid)
    if person_row:
        person.nickname = str(person_row["nickname"] or "")
        person.last_seen = int(person_row["last_seen"] or 0)
        person.msg_count = int(person_row["msg_count"] or 0)
    if relation_row:
        person.affection = int(relation_row["affection"])
        person.trust = int(relation_row["trust"])
        person.familiarity = int(relation_row["familiarity"])
    fresh = read_affection_store().get(str(uid)) or {}
    if isinstance(fresh.get("affection"), int):
        person.affection = fresh["affection"]
    if isinstance(fresh.get("trust"), int):
        person.trust = fresh["trust"]
    return person


def recent_messages(connection, group_id: str, limit: int = 30) -> list[dict]:
    """Read the recent decision rows of a group.

    Args:
        connection: Open core database connection.
        group_id: Platform group id.
        limit: How many rows to return.

    Returns:
        A list of dicts with ts/uid/action, oldest first.
    """
    rows = connection.execute(
        "SELECT ts, uid, action FROM turns WHERE umo LIKE ? ORDER BY ts DESC LIMIT ?",
        (f"%{group_id}%", limit),
    ).fetchall()
    return [dict(row) for row in reversed(rows)]


def detect_flood(connection, umo: str, ts: int) -> bool:
    """Whether the group has been flooding in the last minute.

    Args:
        connection: Open core database connection.
        umo: Unified message origin.
        ts: Current timestamp.

    Returns:
        True when the group posted more than ``FLOOD_MESSAGES`` rows recently.
    """
    return recent_message_count(connection, umo, ts, FLOOD_WINDOW_SECONDS) >= FLOOD_MESSAGES


def recent_message_count(
    connection,
    umo: str,
    ts: int,
    window_seconds: int,
) -> int:
    """How many messages this session saw inside a time window.

    Args:
        connection: Open core database connection.
        umo: Unified message origin.
        ts: Current timestamp.
        window_seconds: Length of the window.

    Returns:
        The number of recorded turns in the window.
    """
    row = connection.execute(
        "SELECT COUNT(*) AS n FROM turns WHERE umo = ? AND ts >= ?",
        (umo, ts - window_seconds),
    ).fetchone()
    return int(row["n"]) if row else 0


def seconds_since_bot(connection, umo: str, ts: int) -> float:
    """How long ago the bot last spoke in this session.

    Args:
        connection: Open core database connection.
        umo: Unified message origin.
        ts: Current timestamp.

    Returns:
        Seconds since the last bot turn, or a large number when unknown.
    """
    row = connection.execute(
        "SELECT ts FROM turns WHERE umo = ? AND action IN ('respond', 'chime')"
        " ORDER BY ts DESC LIMIT 1",
        (umo,),
    ).fetchone()
    if not row:
        return 99999.0
    return max(0.0, ts - int(row["ts"]))


def chimes_today(connection, umo: str, ts: int) -> int:
    """How many times the bot chimed in on this session today.

    Args:
        connection: Open core database connection.
        umo: Unified message origin.
        ts: Current timestamp.

    Returns:
        The number of chime turns since local midnight.
    """
    midnight = int(
        datetime.fromtimestamp(ts)
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .timestamp(),
    )
    row = connection.execute(
        "SELECT COUNT(*) AS n FROM turns WHERE umo = ? AND action = 'chime' AND ts >= ?",
        (umo, midnight),
    ).fetchone()
    return int(row["n"]) if row else 0


def snapshot(connection, umo: str, uid: str, group_id: str, ts: int) -> Snapshot:
    """Collect everything one decision needs.

    Args:
        connection: Open core database connection.
        umo: Unified message origin.
        uid: Sender id.
        group_id: Platform group id.
        ts: Current timestamp.

    Returns:
        The state snapshot.
    """
    return Snapshot(
        person=load_person(connection, uid),
        mood=load_mood(connection, group_id, ts),
        clock=build_clock(),
        group_id=group_id,
        recent=recent_messages(connection, group_id, limit=20),
        flood=detect_flood(connection, umo, ts),
        seconds_since_bot=seconds_since_bot(connection, umo, ts),
        chimes_today=chimes_today(connection, umo, ts),
    )


def log_turn(
    connection,
    *,
    turn_id: str,
    umo: str,
    uid: str,
    action: str,
    reason: str,
    speak_score: float,
    mode: str = "",
    planned_action: str = "",
    recall_ids: list[int] | None = None,
    injected: bool = False,
    ts: int | None = None,
) -> None:
    """Write one decision row.

    Args:
        connection: Open core database connection.
        turn_id: Identifier shared with the recall log.
        umo: Unified message origin.
        uid: Sender id.
        action: Action that actually happened.
        reason: Human-readable reason.
        speak_score: Computed willingness score.
        mode: Extra mode label (for example ``shadow``).
        planned_action: What she would have done if she could speak up.
        recall_ids: Memory ids injected for this turn.
        injected: Whether context was actually injected.
        ts: Event timestamp.
    """
    connection.execute(
        "INSERT INTO turns (turn_id, ts, umo, uid, action, planned_action, reason,"
        " speak_score, mode, recall_ids, injected)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            turn_id,
            ts or store.now(),
            umo,
            uid,
            action,
            planned_action,
            reason,
            float(speak_score),
            mode,
            ",".join(str(item) for item in (recall_ids or [])),
            1 if injected else 0,
        ),
    )
    connection.commit()


def finish_turn(
    connection,
    turn_id: str,
    *,
    reply_chars: int = 0,
    prompt_tokens: int = 0,
    latency_ms: int = 0,
) -> None:
    """Fill in the outcome columns of a turn once the reply is known.

    Args:
        connection: Open core database connection.
        turn_id: Identifier of the turn.
        reply_chars: Length of the reply that was sent.
        prompt_tokens: Prompt tokens reported by the provider.
        latency_ms: Time from decision to reply.
    """
    connection.execute(
        "UPDATE turns SET reply_chars = ?, prompt_tokens = ?, latency_ms = ?"
        " WHERE turn_id = ?",
        (reply_chars, prompt_tokens, latency_ms, turn_id),
    )
    connection.commit()
    logger.debug(f"qingyu_core: turn {turn_id} finished")
