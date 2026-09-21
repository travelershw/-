"""SQLite storage for the 轻语 core: one file, one schema, one place to migrate.

Tables (see 轻语Agent架构设计.md §四):

- ``persons``    people seen in the chats (uid, nickname, aliases)
- ``relations``  slow variables per person (affection, trust, familiarity)
- ``mood``       fast variables per group (energy, mood, updated_at)
- ``memories``   episodic memories (kind, text, confidence, weight, usage)
- ``turns``      one row per decision, for observability and replay
- ``recall_log`` which memories were injected and whether they were used
- ``tool_calls`` which LLM tool ran in a turn (阶段六：周报要看查证与表情包)
"""

import sqlite3
import time
from pathlib import Path

from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

DB_PATH = Path(get_astrbot_plugin_data_path()) / "qingyu.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS persons (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    uid         TEXT NOT NULL UNIQUE,
    nickname    TEXT NOT NULL DEFAULT '',
    aliases     TEXT NOT NULL DEFAULT '',
    first_seen  INTEGER NOT NULL,
    last_seen   INTEGER NOT NULL,
    msg_count   INTEGER NOT NULL DEFAULT 0,
    tags        TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS relations (
    uid         TEXT PRIMARY KEY,
    affection   INTEGER NOT NULL DEFAULT 60,
    trust       INTEGER NOT NULL DEFAULT 60,
    familiarity INTEGER NOT NULL DEFAULT 0,
    updated_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS mood (
    group_id    TEXT PRIMARY KEY,
    energy      INTEGER NOT NULL DEFAULT 70,
    mood        INTEGER NOT NULL DEFAULT 65,
    updated_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS memories (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    uid          TEXT NOT NULL DEFAULT '',
    group_id     TEXT NOT NULL DEFAULT '',
    kind         TEXT NOT NULL DEFAULT 'fact',
    text         TEXT NOT NULL,
    confidence   REAL NOT NULL DEFAULT 0.5,
    weight       REAL NOT NULL DEFAULT 1.0,
    created_at   INTEGER NOT NULL,
    last_used_at INTEGER NOT NULL DEFAULT 0,
    use_count    INTEGER NOT NULL DEFAULT 0,
    source       TEXT NOT NULL DEFAULT '',
    related_uid  TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_memories_uid ON memories (uid, weight DESC);
CREATE INDEX IF NOT EXISTS idx_memories_group ON memories (group_id, created_at DESC);

CREATE TABLE IF NOT EXISTS turns (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_id       TEXT NOT NULL,
    ts            INTEGER NOT NULL,
    umo           TEXT NOT NULL,
    uid           TEXT NOT NULL,
    action        TEXT NOT NULL,
    planned_action TEXT NOT NULL DEFAULT '',
    reason        TEXT NOT NULL DEFAULT '',
    speak_score   REAL NOT NULL DEFAULT 0,
    mode          TEXT NOT NULL DEFAULT '',
    recall_ids    TEXT NOT NULL DEFAULT '',
    injected      INTEGER NOT NULL DEFAULT 0,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    reply_chars   INTEGER NOT NULL DEFAULT 0,
    latency_ms    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_turns_ts ON turns (ts DESC);
CREATE INDEX IF NOT EXISTS idx_turns_action ON turns (action);

CREATE TABLE IF NOT EXISTS recall_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_id   TEXT NOT NULL,
    memory_id INTEGER NOT NULL,
    ts        INTEGER NOT NULL,
    used      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tool_calls (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_id TEXT NOT NULL DEFAULT '',
    tool    TEXT NOT NULL,
    ts      INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tool_calls_ts ON tool_calls (ts DESC);

CREATE TABLE IF NOT EXISTS pet_events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       INTEGER NOT NULL,
    umo      TEXT NOT NULL DEFAULT '',
    group_id TEXT NOT NULL DEFAULT '',
    kind     TEXT NOT NULL,
    text     TEXT NOT NULL DEFAULT '',
    extra    TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_pet_events_ts ON pet_events (ts DESC);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# 后来加的列：老库要先补上再跑新代码。
MIGRATIONS = (
    ("turns", "planned_action", "TEXT NOT NULL DEFAULT ''"),
    # 阶段五：关系记忆——这条旧事还牵着另一个人。
    ("memories", "related_uid", "TEXT NOT NULL DEFAULT ''"),
)
# 依赖新列的索引，必须等列补好再建（老库里还没有这些列）。
POST_MIGRATION_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_turns_planned ON turns (planned_action)",
    "CREATE INDEX IF NOT EXISTS idx_memories_related ON memories (related_uid)",
)


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """Open the core database, creating the schema on first use.

    Args:
        db_path: Override for tests; defaults to ``plugin_data/qingyu.db``.

    Returns:
        An open connection with row access by column name.
    """
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(SCHEMA)
    _apply_migrations(connection)
    connection.commit()
    return connection


def _apply_migrations(connection: sqlite3.Connection) -> None:
    """Add columns that were introduced after the first release.

    Args:
        connection: Open connection whose tables already exist.
    """
    for table, column, definition in MIGRATIONS:
        columns = {
            row["name"]
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column in columns:
            continue
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    for statement in POST_MIGRATION_INDEXES:
        connection.execute(statement)


def get_meta(connection: sqlite3.Connection, key: str, default: str = "") -> str:
    """Read one key from the ``meta`` table.

    Args:
        connection: Open connection.
        key: Key to read.
        default: Value returned when the key is absent.

    Returns:
        The stored value or ``default``.
    """
    row = connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else default


def set_meta(connection: sqlite3.Connection, key: str, value: str) -> None:
    """Write one key into the ``meta`` table.

    Args:
        connection: Open connection.
        key: Key to write.
        value: Value to store.
    """
    connection.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    connection.commit()


def now() -> int:
    """Current wall-clock time in seconds.

    Returns:
        The current timestamp.
    """
    return int(time.time())
