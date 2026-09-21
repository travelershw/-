"""桌宠要显示的状态：只读 AstrBot 的两个库，绝不写、绝不碰它的进程。

数据来源（都已实测是实时更新的）：

- ``qingyu.db`` 的 ``mood``          每群精力/心情（她此刻的状态）
- ``qingyu.db`` 的 ``meta``           ``mood_last:<群号>`` = "被夸｜精力 +2 心情 +2｜09-17 20:55"
- ``qingyu.db`` 的 ``relations``     桌宠代表的那个人在她心里的好感/熟悉
- ``qingyu.db`` 的 ``turns``         她最近在干什么（observe/respond/chime）+ 原因
- ``data_v4.db`` 的 ``umo_aliases``  群名（气泡里显示"示例用户"）

"她还在不在"用两个端口判断：AstrBot 面板 6185（在跑）、NapCat 6199（QQ 在线）。
"""

import json
import os
import socket
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

DATA = Path.home() / ".astrbot" / "data"
CORE_DB = DATA / "plugin_data" / "qingyu.db"
MAIN_DB = DATA / "data_v4.db"
CHIME_STATE = DATA / "plugin_data" / "random_chime_state.json"
# 看门狗判定 QQ/NapCat 掉线时写的提醒文件（QQ 断了，桌面是她唯一还能说话的地方）
ALERT_FILE = Path.home() / ".astrbot" / "logs" / "napcat_alert.json"
# 桌宠代表谁：用你自己的 QQ，这样好感/熟悉跟 QQ 私聊是同一份。
ME_UID = os.environ.get("QINGYU_USER_ID", "desktop-user")
PANEL_PORT = 6185
NAPCAT_PORT = 6199
# 深夜（跟她决策里的定义一致）。
NIGHT_START, NIGHT_END = 23, 6


@dataclass
class PetState:
    """One snapshot of her state, as the pet needs it."""

    energy: int = 70
    mood: int = 65
    affection: int = 60
    familiarity: int = 0
    group_id: str = ""
    group_name: str = ""
    mood_reason: str = ""
    mood_effect: str = ""
    mood_at: str = ""
    last_action: str = ""
    last_reason: str = ""
    chimes_today: int = 0
    memories: int = 0
    turn_age: float = 9999.0
    bot_up: bool = False
    qq_up: bool = False
    is_night: bool = False
    error: str = ""
    groups: dict[str, str] = field(default_factory=dict)

    @property
    def online(self) -> bool:
        """Whether both AstrBot and QQ are reachable.

        Returns:
            True when the pet can trust the numbers it shows.
        """
        return self.bot_up and self.qq_up

    def describe(self) -> str:
        """One-line summary for the status menu.

        Returns:
            Human-readable status text.
        """
        where = self.group_name or self.group_id or "未知群"
        return (
            f"在「{where}」｜精力 {self.energy} 心情 {self.mood}\n"
            f"对你的好感 {self.affection}（照过 {self.familiarity} 次面）\n"
            f"记忆 {self.memories} 条｜今天插嘴 {self.chimes_today} 次\n"
            f"最近：{self.mood_reason or '还没有'}\n"
            f"{'她在线' if self.online else '她好像不在（AstrBot 或 QQ 没开）'}"
        )


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    """Check whether something is listening on a local port.

    Args:
        port: TCP port.
        host: Host to probe.

    Returns:
        True when the connect succeeds quickly.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex((host, port)) == 0


def _group_names() -> dict[str, str]:
    """Read group display names from AstrBot's alias table.

    Returns:
        ``{group_id: name}`` for group sessions.
    """
    if not MAIN_DB.exists():
        return {}
    try:
        connection = sqlite3.connect(f"file:{MAIN_DB}?mode=ro", uri=True)
        try:
            rows = connection.execute(
                "SELECT umo, auto_name, user_alias FROM umo_aliases"
                " WHERE umo LIKE '%GroupMessage%'",
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        return {}
    names: dict[str, str] = {}
    for umo, auto_name, alias in rows:
        group_id = str(umo).rsplit(":", 1)[-1]
        names[group_id] = str(alias or auto_name or group_id)
    return names


def _parse_mood_event(text: str) -> tuple[str, str, str]:
    """Split ``"原因｜精力 +2 心情 +2｜09-17 20:55"`` into three parts.

    Args:
        text: Raw value from the meta table.

    Returns:
        ``(reason, effect, time)``.
    """
    parts = [part.strip() for part in (text or "").split("｜")]
    while len(parts) < 3:
        parts.append("")
    return parts[0], parts[1], parts[2]


def read_alert() -> dict:
    """Read the watchdog's QQ-offline alert, when there is one.

    看门狗（`migration_tools/napcat_watchdog.py`）判定 NapCat 掉线时会写这个文件——
    QQ 断了的时候，桌面桌宠是唯一还能把消息递到眼前的渠道。

    Returns:
        ``{"ts": int, "text": str}``，没有提醒时返回空 dict。
    """
    if not ALERT_FILE.exists():
        return {}
    try:
        data = json.loads(ALERT_FILE.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or not data.get("text"):
        return {}
    return {"ts": int(data.get("ts") or 0), "text": str(data["text"])}


def read_local_state() -> PetState:
    """Snapshot for standalone mode: read the pet's own database.

    （分发给别人用时没有 AstrBot，状态存在 `pet_data.db` 里，由 `brain.py` 维护。）

    Returns:
        The snapshot.
    """
    import paths

    state = PetState(
        bot_up=True,
        qq_up=True,
        is_night=not (NIGHT_END <= datetime.now().hour < NIGHT_START),
        group_name="你的桌面",
    )
    if not paths.DATA_DB.exists():
        return state
    try:
        connection = sqlite3.connect(f"file:{paths.DATA_DB}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            rows = {
                str(row["key"]): str(row["value"])
                for row in connection.execute("SELECT key, value FROM state")
            }
            state.energy = int(rows.get("energy", "70") or 70)
            state.mood = int(rows.get("mood", "65") or 65)
            state.affection = int(rows.get("affection", "60") or 60)
            raw = rows.get("mood_last", "")
            if raw:
                state.mood_reason, state.mood_effect, state.mood_at = _parse_mood_event(raw)
            state.memories = int(
                connection.execute("SELECT COUNT(*) FROM memories").fetchone()[0],
            )
        finally:
            connection.close()
    except sqlite3.Error as exc:
        state.error = f"读本地库失败：{exc}"
    return state


def read_state(pinned_group: str = "") -> PetState:
    """Read one snapshot of her state.

    Args:
        pinned_group: Group id to show; empty means "the most recently active".

    Returns:
        The snapshot; on failure it carries ``error`` and keeps the defaults.
    """
    state = PetState(
        bot_up=_port_open(PANEL_PORT),
        qq_up=_port_open(NAPCAT_PORT),
        is_night=not (NIGHT_END <= datetime.now().hour < NIGHT_START),
        groups=_group_names(),
    )
    if not CORE_DB.exists():
        state.error = "找不到 qingyu.db"
        return state
    try:
        connection = sqlite3.connect(f"file:{CORE_DB}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                "SELECT group_id, energy, mood, updated_at FROM mood"
                " ORDER BY updated_at DESC",
            ).fetchall()
            if rows:
                picked = None
                for row in rows:
                    if pinned_group and str(row["group_id"]) == pinned_group:
                        picked = row
                        break
                picked = picked or rows[0]
                state.group_id = str(picked["group_id"])
                state.energy = int(picked["energy"])
                state.mood = int(picked["mood"])
                state.group_name = state.groups.get(state.group_id, state.group_id)

            relation = connection.execute(
                "SELECT affection, familiarity FROM relations WHERE uid = ?",
                (ME_UID,),
            ).fetchone()
            if relation:
                state.affection = int(relation["affection"])
                state.familiarity = int(relation["familiarity"])

            if state.group_id:
                meta = connection.execute(
                    "SELECT value FROM meta WHERE key = ?",
                    (f"mood_last:{state.group_id}",),
                ).fetchone()
                if meta:
                    (
                        state.mood_reason,
                        state.mood_effect,
                        state.mood_at,
                    ) = _parse_mood_event(str(meta["value"]))
                umo = connection.execute(
                    "SELECT value FROM meta WHERE key = ?",
                    (f"umo:{state.group_id}",),
                ).fetchone()
                if umo:
                    turn = connection.execute(
                        "SELECT ts, action, reason FROM turns WHERE umo = ?"
                        " ORDER BY ts DESC LIMIT 1",
                        (str(umo["value"]),),
                    ).fetchone()
                    if turn:
                        state.last_action = str(turn["action"])
                        state.last_reason = str(turn["reason"])
                        state.turn_age = max(0.0, datetime.now().timestamp() - int(turn["ts"]))
                    today = datetime.now().replace(hour=4, minute=0, second=0, microsecond=0)
                    state.chimes_today = int(
                        connection.execute(
                            "SELECT COUNT(*) FROM turns WHERE umo = ? AND action = 'chime'"
                            " AND ts >= ?",
                            (str(umo["value"]), int(today.timestamp())),
                        ).fetchone()[0],
                    )

            state.memories = int(
                connection.execute("SELECT COUNT(*) FROM memories").fetchone()[0],
            )
        finally:
            connection.close()
    except sqlite3.Error as exc:
        state.error = f"读库失败：{exc}"
    return state


def chime_whitelist() -> list[str]:
    """Read which sessions may be chimed in on.

    Returns:
        Session identifiers on the whitelist.
    """
    try:
        data = json.loads(CHIME_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [str(item) for item in data.get("enabled_sessions", [])]


def read_events(after_id: int = 0, limit: int = 20) -> list[dict]:
    """Read her **group** events (she spoke / chimed in / mood changed).

    事件由 qingyu_core 写在 ``pet_events`` 表里——AstrBot 自带的历史表会落后几小时，
    拿不到"她刚说的那句"。

    只返回**群里的**事件（``group_id`` 非空）：私聊/桌面通道的回复不是"群里的动静"，
    桌宠不该拿它冒泡（曾经因为旧版核心插件把桌面回复也记进来，气泡就成了「【】回话：…」）。

    Args:
        after_id: Only return events newer than this id.
        limit: Maximum rows.

    Returns:
        Event dicts with a ``group_name`` filled in.
    """
    if not CORE_DB.exists():
        return []
    names = _group_names()
    try:
        connection = sqlite3.connect(f"file:{CORE_DB}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                "SELECT id, ts, group_id, kind, text FROM pet_events"
                " WHERE id > ? AND group_id != '' ORDER BY id ASC LIMIT ?",
                (max(0, int(after_id)), max(1, int(limit))),
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        return []
    events = []
    for row in rows:
        item = dict(row)
        group_id = str(item["group_id"])
        item["group_name"] = names.get(group_id) or f"群 {group_id}"
        events.append(item)
    return events


def last_event_id() -> int:
    """Highest event id currently stored.

    Returns:
        The id, or 0 when there is nothing.
    """
    if not CORE_DB.exists():
        return 0
    try:
        connection = sqlite3.connect(f"file:{CORE_DB}?mode=ro", uri=True)
        try:
            row = connection.execute(
                "SELECT COALESCE(MAX(id), 0) FROM pet_events",
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        return 0
    return int(row[0] if row else 0)


def main() -> None:
    """Print the snapshot (used by ``pet.py --selftest``)."""
    state = read_state()
    print(state.describe())
    print(f"群名表: {state.groups}")
    print(f"插嘴白名单: {chime_whitelist()}")
    print(f"最近一轮: {state.last_action}（{state.turn_age:.0f} 秒前）{state.last_reason[:40]}")


if __name__ == "__main__":
    main()
