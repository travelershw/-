"""给她在群里的动静记一份"事件流"，桌宠靠它冒泡（阶段 C）。

为什么不能直接读 AstrBot 的历史表：实测 `platform_message_history` 的最新一行会落后
好几个小时，拿不到"她刚说的那句话"。所以在核心插件自己的发送钩子里记一行，
写进 ``qingyu.db`` 的 ``pet_events``，桌宠只读这张表。

表是环形使用的：只留最近 ``KEEP_EVENTS`` 条，避免长期堆积。
"""

import json
import sqlite3

from astrbot.core import logger

from . import store

KEEP_EVENTS = 500
KINDS = ("speak", "chime", "mood", "tool")


def log(
    connection: sqlite3.Connection,
    *,
    kind: str,
    text: str,
    umo: str = "",
    group_id: str = "",
    extra: dict | str | None = None,
    ts: int | None = None,
) -> int | None:
    """Append one event and trim the table.

    Args:
        connection: Open core database connection.
        kind: One of :data:`KINDS`.
        text: Event text (what she said, or the mood reason).
        umo: Session the event happened in.
        group_id: Group id, when known.
        extra: Small dict (or string) of extra fields.
        ts: Timestamp.

    Returns:
        The new row id, or None when the text is empty.
    """
    cleaned = " ".join(str(text or "").split())
    if not cleaned:
        return None
    if kind not in KINDS:
        kind = "speak"
    if isinstance(extra, dict):
        extra = json.dumps(extra, ensure_ascii=False)
    cursor = connection.execute(
        "INSERT INTO pet_events (ts, umo, group_id, kind, text, extra)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (ts if ts is not None else store.now(), umo, group_id, kind, cleaned[:400], extra or ""),
    )
    connection.execute(
        "DELETE FROM pet_events WHERE id <= (SELECT MAX(id) - ? FROM pet_events)",
        (KEEP_EVENTS,),
    )
    connection.commit()
    return int(cursor.lastrowid)


def recent(connection: sqlite3.Connection, *, after_id: int = 0, limit: int = 20) -> list[dict]:
    """Read events newer than an id.

    Args:
        connection: Open core database connection.
        after_id: Only return rows with a larger id.
        limit: Maximum rows.

    Returns:
        Event dicts in chronological order.
    """
    rows = connection.execute(
        "SELECT id, ts, umo, group_id, kind, text, extra FROM pet_events"
        " WHERE id > ? ORDER BY id ASC LIMIT ?",
        (max(0, int(after_id)), max(1, int(limit))),
    ).fetchall()
    events = []
    for row in rows:
        item = dict(row)
        if item["extra"]:
            try:
                item["extra"] = json.loads(item["extra"])
            except ValueError:
                item["extra"] = {}
        else:
            item["extra"] = {}
        events.append(item)
    return events


def latest_id(connection: sqlite3.Connection) -> int:
    """Highest event id currently stored.

    Args:
        connection: Open core database connection.

    Returns:
        The id, or 0 when the table is empty.
    """
    row = connection.execute("SELECT COALESCE(MAX(id), 0) AS n FROM pet_events").fetchone()
    return int(row["n"])


def stats(connection: sqlite3.Connection) -> dict:
    """Count events by kind.

    Args:
        connection: Open core database connection.

    Returns:
        ``{kind: count}`` plus ``total``.
    """
    rows = connection.execute(
        "SELECT kind, COUNT(*) AS n FROM pet_events GROUP BY kind",
    ).fetchall()
    counts = {str(row["kind"]): int(row["n"]) for row in rows}
    counts["total"] = sum(counts.values())
    return counts


def describe(connection: sqlite3.Connection) -> str:
    """One-line summary for the status command.

    Args:
        connection: Open core database connection.

    Returns:
        Chinese summary text.
    """
    counts = stats(connection)
    if not counts["total"]:
        return "桌宠事件：还没有记录"
    return (
        f"桌宠事件：{counts['total']} 条"
        f"（她说话 {counts.get('speak', 0)}、插嘴 {counts.get('chime', 0)}、"
        f"心情 {counts.get('mood', 0)}、工具 {counts.get('tool', 0)}）"
    )


def selftest() -> None:
    """Write and read a couple of events in a scratch database."""
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(store.SCHEMA)
    log(connection, kind="speak", text="嗯~我在呢", umo="napcat:GroupMessage:1", group_id="1")
    log(connection, kind="mood", text="被夸", extra={"mood": 2}, group_id="1")
    log(connection, kind="bogus", text="类型不对会被纠正")
    log(connection, kind="speak", text="   ")
    events = recent(connection, after_id=0)
    print(f"    写入 {len(events)} 条，最新 id={latest_id(connection)}")
    for event in events:
        print(
            f"    #{event['id']} {event['kind']:6} {event['text'][:20]} extra={event['extra']}",
        )
    print(f"    after_id 过滤: {len(recent(connection, after_id=events[0]['id']))} 条")
    print(f"    {describe(connection)}")
    print(f"    裁剪测试: 写 {KEEP_EVENTS + 20} 条后剩 {_trim_test()} 条")
    connection.close()


def _trim_test() -> int:
    """Check the trim keeps only the newest rows.

    Returns:
        Row count after the trim.
    """
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(store.SCHEMA)
    for index in range(KEEP_EVENTS + 20):
        log(connection, kind="speak", text=f"第 {index} 条")
    count = int(connection.execute("SELECT COUNT(*) FROM pet_events").fetchone()[0])
    connection.close()
    return count


if __name__ == "__main__":
    logger.info("petlog selftest")
    selftest()
